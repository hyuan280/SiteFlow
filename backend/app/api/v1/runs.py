from __future__ import annotations

import asyncio
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.deps import get_crypto, get_current_session, get_db, get_settings, require_csrf
from app.core.auth import SESSION_COOKIE
from app.core.config import Settings
from app.core.crypto import CryptoService
from app.models.auth import AuthSession
from app.models.execution import JobArtifact
from app.schemas.execution import RunCreate, RunDetail, RunSummary
from app.services.auth import AuthService
from app.services.execution import (
    ExecutionConflictError,
    ExecutionNotFoundError,
    ExecutionService,
    ExecutionValidationError,
)

router = APIRouter(
    prefix="/api/v1/runs",
    tags=["runs"],
    dependencies=[Depends(get_current_session)],
)
stream_router = APIRouter(prefix="/api/v1/runs", tags=["runs"])
artifact_router = APIRouter(
    prefix="/api/v1/artifacts",
    tags=["runs"],
    dependencies=[Depends(get_current_session)],
)

SSE_POLL_SECONDS = 0.8
SSE_HEARTBEAT_SECONDS = 15.0


def get_service(
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    crypto: CryptoService = Depends(get_crypto),
) -> ExecutionService:
    return ExecutionService(session, settings, crypto)


def _raise(exc: Exception) -> None:
    if isinstance(exc, ExecutionNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ExecutionConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("", response_model=list[RunSummary])
def list_runs(
    limit: int = Query(default=50, ge=1, le=200),
    service: ExecutionService = Depends(get_service),
) -> list[RunSummary]:
    return service.list_runs(limit)


@router.post("", response_model=RunDetail, status_code=201)
async def create_run(
    payload: RunCreate,
    request: Request,
    service: ExecutionService = Depends(get_service),
    current: AuthSession = Depends(require_csrf),
) -> RunDetail:
    try:
        detail = service.create_run(payload, current.username)
    except (ExecutionNotFoundError, ExecutionConflictError, ExecutionValidationError) as exc:
        _raise(exc)
    request.app.state.executor.submit(detail.id)
    return service.get_run(detail.id)


@router.get("/{run_id}", response_model=RunDetail)
def get_run(run_id: int, service: ExecutionService = Depends(get_service)) -> RunDetail:
    try:
        return service.get_run(run_id)
    except ExecutionNotFoundError as exc:
        _raise(exc)


@artifact_router.get("/{artifact_id}")
def get_artifact(
    artifact_id: int,
    download: bool = False,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    artifact = session.get(JobArtifact, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="artifact 不存在")
    root = Path(settings.artifact_root).resolve()
    path = (root / artifact.relative_path).resolve()
    if path != root and root not in path.parents:
        raise HTTPException(status_code=404, detail="artifact 路径无效")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="artifact 文件不存在")
    return FileResponse(
        path,
        media_type=artifact.content_type,
        filename=path.name,
        content_disposition_type="attachment" if download else "inline",
    )


def _require_stream_session(request: Request) -> None:
    raw_token = request.cookies.get(SESSION_COOKIE)
    if not raw_token:
        raise HTTPException(status_code=401, detail="未登录")
    app = request.app
    with app.state.session_factory() as session:
        row = AuthService(session, app.state.settings, app.state.bootstrap).get_session(
            raw_token
        )
        if row is None:
            raise HTTPException(status_code=401, detail="会话已失效")


@stream_router.get("/{run_id}/events")
async def stream_run_events(run_id: int, request: Request) -> StreamingResponse:
    _require_stream_session(request)
    app = request.app
    settings: Settings = app.state.settings
    crypto: CryptoService = app.state.crypto
    session_factory = app.state.session_factory

    def load() -> RunDetail | None:
        with session_factory() as session:
            try:
                return ExecutionService(session, settings, crypto).get_run(run_id)
            except ExecutionNotFoundError:
                return None

    initial = await asyncio.to_thread(load)
    if initial is None:
        raise HTTPException(status_code=404, detail=f"运行 {run_id} 不存在")
    initial_payload = initial.model_dump_json()

    async def event_stream():  # type: ignore[no-untyped-def]
        last_payload = initial_payload
        last_beat = time.monotonic()
        yield "retry: 2000\n\n"
        yield f"event: update\ndata: {initial_payload}\n\n"
        while True:
            if await request.is_disconnected():
                break
            await asyncio.sleep(SSE_POLL_SECONDS)
            try:
                detail = await asyncio.to_thread(load)
            except SQLAlchemyError:
                if time.monotonic() - last_beat >= SSE_HEARTBEAT_SECONDS:
                    last_beat = time.monotonic()
                    yield ": keep-alive\n\n"
                continue
            if detail is None:
                yield "event: gone\ndata: {}\n\n"
                break
            payload = detail.model_dump_json()
            if payload != last_payload:
                last_payload = payload
                last_beat = time.monotonic()
                yield f"event: update\ndata: {payload}\n\n"
            elif time.monotonic() - last_beat >= SSE_HEARTBEAT_SECONDS:
                last_beat = time.monotonic()
                yield ": keep-alive\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/{run_id}/cancel", response_model=RunDetail)
async def cancel_run(
    run_id: int,
    request: Request,
    service: ExecutionService = Depends(get_service),
    _current: AuthSession = Depends(require_csrf),
) -> RunDetail:
    try:
        detail = service.cancel_run(run_id)
    except (ExecutionNotFoundError, ExecutionConflictError) as exc:
        _raise(exc)
    await request.app.state.executor.cancel(run_id)
    detail = service.get_run(run_id)
    notify = getattr(request.app.state.executor, "schedule_run_notification", None)
    if detail.finished_at is not None and callable(notify):
        notify(run_id)
    return detail
