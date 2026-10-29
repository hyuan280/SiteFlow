from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_current_session, get_db, get_settings, require_csrf
from app.core.config import Settings
from app.models.auth import AuthSession
from app.schemas.picker import PickerOpenRequest, PickerSessionView
from app.services.lanes import LaneService, WorkerRequestError

router = APIRouter(
    prefix="/api/v1/picker",
    tags=["picker"],
    dependencies=[Depends(get_current_session)],
)


def get_service(settings: Settings = Depends(get_settings)) -> LaneService:
    return LaneService(settings)


def _raise(exc: WorkerRequestError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/sessions", response_model=PickerSessionView)
async def open_picker(
    payload: PickerOpenRequest,
    service: LaneService = Depends(get_service),
    _session: Session = Depends(get_db),
    _current: AuthSession = Depends(require_csrf),
) -> PickerSessionView:
    try:
        return PickerSessionView.model_validate(
            await service.open_picker(payload.url, payload.lane_key)
        )
    except WorkerRequestError as exc:
        _raise(exc)


@router.get("/sessions/{session_id}", response_model=PickerSessionView)
async def get_picker(
    session_id: str,
    service: LaneService = Depends(get_service),
) -> PickerSessionView:
    try:
        return PickerSessionView.model_validate(await service.get_picker(session_id))
    except WorkerRequestError as exc:
        _raise(exc)


@router.delete("/sessions/{session_id}")
async def close_picker(
    session_id: str,
    service: LaneService = Depends(get_service),
    _current: AuthSession = Depends(require_csrf),
) -> dict:
    try:
        return await service.close_picker(session_id)
    except WorkerRequestError as exc:
        _raise(exc)
