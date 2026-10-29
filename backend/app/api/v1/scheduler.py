from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_current_session, get_db, get_settings, require_csrf
from app.core.config import Settings
from app.models.auth import AuthSession
from app.schemas.scheduler import SchedulerSettingsUpdate, SchedulerSettingsView
from app.services.scheduler_settings import (
    SchedulerLockedError,
    SchedulerSettingsService,
    SchedulerValidationError,
)

router = APIRouter(
    prefix="/api/v1/scheduler-settings",
    tags=["scheduler"],
    dependencies=[Depends(get_current_session)],
)


def get_service(
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> SchedulerSettingsService:
    return SchedulerSettingsService(session, settings)


@router.get("", response_model=SchedulerSettingsView)
def get_scheduler_settings(
    service: SchedulerSettingsService = Depends(get_service),
) -> SchedulerSettingsView:
    return service.view()


@router.put("", response_model=SchedulerSettingsView)
def update_scheduler_settings(
    payload: SchedulerSettingsUpdate,
    service: SchedulerSettingsService = Depends(get_service),
    _current: AuthSession = Depends(require_csrf),
) -> SchedulerSettingsView:
    try:
        return service.update(payload)
    except SchedulerLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SchedulerValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
