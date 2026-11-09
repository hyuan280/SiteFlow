"""Authenticated, owner-scoped assistant API and snapshot-only SSE transport."""

from __future__ import annotations

import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_readonly_current_session, require_csrf
from app.api.v1.workflow_drafts import _DraftRoute
from app.models.auth import AuthSession
from app.schemas.assistant import (
    AuthorizeRetry, BrowserRecovery, CreateSession, RenameSession, RetryApprovalDecision,
    SendMessage, SetTarget, InteractionResponse,
)
from app.schemas.assistant_settings import (
    AssistantSettingsReset,
    AssistantSettingsUpdate,
    AssistantSettingsView,
)
from app.services.assistant import AssistantError, AssistantManager
from app.services.assistant_retry import authorize_retry, decide_retry_approval
from app.services.assistant_interactions import respond_interaction
from app.services.assistant_settings import AssistantSettingsError, AssistantSettingsService
from app.services.config_ai_generation import _json


class _AssistantRoute(_DraftRoute):
    def get_route_handler(self):
        handler = APIRoute.get_route_handler(self)

        async def bounded(request: Request):
            try:
                if request.method in {"POST", "PUT", "DELETE"}:
                    chunks, size = [], 0
                    try:
                        async with asyncio.timeout(10):
                            async for chunk in request.stream():
                                size += len(chunk)
                                if size > 128 * 1024:
                                    raise HTTPException(413, detail={"code": "request_limit"})
                                chunks.append(chunk)
                    except TimeoutError:
                        raise HTTPException(408, detail={"code": "request_timeout"}) from None
                    request._body = b"".join(chunks)
                response = await handler(request)
            except (AssistantError, AssistantSettingsError) as exc:
                response = JSONResponse(status_code=exc.status, content={"detail": {
                    "code": exc.code, **getattr(exc, "details", {}),
                }})
            except RequestValidationError:
                response = JSONResponse(status_code=422, content={"detail": {"code": "invalid_request"}})
            except HTTPException as exc:
                exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
                raise
            response.headers["Cache-Control"] = "no-store"
            return response

        return bounded


def manager(request: Request) -> AssistantManager:
    return request.app.state.assistant_manager


def stream_owner(request: Request) -> str:
    service = manager(request)
    with service.session_factory() as db:
        return get_readonly_current_session(request, db, service.settings).username


def mutation_owner(
    request: Request, current: AuthSession = Depends(get_readonly_current_session),
) -> str:
    # Explicit current avoids require_csrf's touching get_current_session dependency.
    require_csrf(request, current)
    return current.username


router = APIRouter(prefix="/api/v1/assistant", tags=["assistant"], route_class=_AssistantRoute)


@router.get("/settings", response_model=AssistantSettingsView)
def get_assistant_settings(
    current: AuthSession = Depends(get_readonly_current_session),
    db: Session = Depends(get_db),
) -> AssistantSettingsView:
    return AssistantSettingsService(db).view()


@router.put("/settings", response_model=AssistantSettingsView)
def update_assistant_settings(
    payload: AssistantSettingsUpdate, owner: str = Depends(mutation_owner),
    db: Session = Depends(get_db),
) -> AssistantSettingsView:
    return AssistantSettingsService(db).update(payload)


@router.post("/settings/reset", response_model=AssistantSettingsView)
def reset_assistant_settings(
    payload: AssistantSettingsReset, owner: str = Depends(mutation_owner),
    db: Session = Depends(get_db),
) -> AssistantSettingsView:
    return AssistantSettingsService(db).reset(payload.expected_revision)


@router.get("/sessions")
async def list_sessions(
    current: AuthSession = Depends(get_readonly_current_session),
    service: AssistantManager = Depends(manager),
) -> dict:
    return service.list_sessions(current.username)


@router.post("/sessions")
async def create_session(
    payload: CreateSession, owner: str = Depends(mutation_owner),
    service: AssistantManager = Depends(manager),
) -> dict:
    return service.create(owner, payload)


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: UUID, current: AuthSession = Depends(get_readonly_current_session),
    service: AssistantManager = Depends(manager),
) -> dict:
    return service.detail(current.username, str(session_id))


@router.put("/sessions/{session_id}/title")
async def rename_session(
    session_id: UUID, payload: RenameSession, owner: str = Depends(mutation_owner),
    service: AssistantManager = Depends(manager),
) -> dict:
    return await service.rename(owner, str(session_id), payload)


@router.put("/sessions/{session_id}/target")
async def set_target(
    session_id: UUID, payload: SetTarget, owner: str = Depends(mutation_owner),
    service: AssistantManager = Depends(manager),
) -> dict:
    return await service.set_target(owner, str(session_id), payload)


@router.post("/sessions/{session_id}/messages", status_code=202)
async def send_message(
    session_id: UUID, payload: SendMessage, owner: str = Depends(mutation_owner),
    service: AssistantManager = Depends(manager),
) -> dict:
    return await service.send(owner, str(session_id), payload)


@router.post("/sessions/{session_id}/retry-authorizations")
async def retry_authorization(
    session_id: UUID, payload: AuthorizeRetry, owner: str = Depends(mutation_owner),
    service: AssistantManager = Depends(manager),
) -> dict:
    return await authorize_retry(service, owner, str(session_id), payload)


@router.post("/sessions/{session_id}/browser/screenshot")
async def browser_screenshot(
    session_id: UUID, payload: BrowserRecovery, owner: str = Depends(mutation_owner),
    service: AssistantManager = Depends(manager),
) -> dict:
    return await service.browser_recovery(owner, str(session_id), "screenshot")


@router.post("/sessions/{session_id}/retry-approvals/{approval_id}/decision")
async def retry_approval_decision(
    session_id: UUID, approval_id: UUID, payload: RetryApprovalDecision,
    owner: str = Depends(mutation_owner), service: AssistantManager = Depends(manager),
) -> dict:
    return await decide_retry_approval(
        service, owner, str(session_id), str(approval_id), payload.decision)


@router.post("/sessions/{session_id}/browser/refresh")
async def browser_refresh(
    session_id: UUID, payload: BrowserRecovery, owner: str = Depends(mutation_owner),
    service: AssistantManager = Depends(manager),
) -> dict:
    return await service.browser_recovery(owner, str(session_id), "refresh")


@router.post("/sessions/{session_id}/interactions/{interaction_id}/response")
async def interaction_response(
    session_id: UUID, interaction_id: UUID, payload: InteractionResponse,
    owner: str = Depends(mutation_owner), service: AssistantManager = Depends(manager),
) -> dict:
    return await respond_interaction(service, owner, str(session_id), str(interaction_id), payload)


@router.get("/sessions/{session_id}/images/{image_id}")
def get_image(
    session_id: UUID, image_id: UUID, current: AuthSession = Depends(get_readonly_current_session),
    service: AssistantManager = Depends(manager),
):
    return Response(service.get_image(current.username, str(session_id), str(image_id)),
                    media_type="image/jpeg", headers={"X-Content-Type-Options": "nosniff"})


@router.get("/sessions/{session_id}/events")
async def events(
    session_id: UUID, request: Request, owner: str = Depends(stream_owner),
    service: AssistantManager = Depends(manager),
):
    identifier = str(session_id)
    initial = service.detail(owner, identifier)

    async def snapshots():
        previous = None
        detail = initial
        while not await request.is_disconnected():
            # Recheck login expiry/revocation as well as ownership on each snapshot.
            try:
                with service.session_factory() as db:
                    auth = get_readonly_current_session(request, db, service.settings)
                    if auth.username != owner:
                        return
                detail = service.detail(owner, identifier)
            except (HTTPException, AssistantError):
                return
            encoded = _json(detail)
            if encoded != previous or detail["status"] == "running":
                yield "event: update\ndata: " + encoded + "\n\n"
                previous = encoded
            else:
                yield ": heartbeat\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(snapshots(), media_type="text/event-stream", headers={
        "Cache-Control": "no-store", "X-Accel-Buffering": "no",
    })


@router.post("/sessions/{session_id}/cancel")
async def cancel(
    session_id: UUID, owner: str = Depends(mutation_owner),
    service: AssistantManager = Depends(manager),
) -> dict:
    return await service.cancel(owner, str(session_id))


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: UUID, owner: str = Depends(mutation_owner),
    service: AssistantManager = Depends(manager),
):
    await service.delete(owner, str(session_id))
    return Response(status_code=204)
