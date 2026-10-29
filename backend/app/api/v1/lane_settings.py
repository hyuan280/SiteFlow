from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_current_session, get_db, get_settings, require_csrf
from app.core.config import Settings
from app.models.auth import AuthSession
from app.schemas.lane import LaneSettingsUpdate, LaneSettingsView
from app.services.lane_settings import LaneSettingsService, SettingLockedError
from app.services.lanes import LaneService

router = APIRouter(
    prefix="/api/v1/lane-settings",
    tags=["lanes"],
    dependencies=[Depends(get_current_session)],
)


def get_service(
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> LaneSettingsService:
    return LaneSettingsService(session, settings)


@router.get("", response_model=LaneSettingsView)
def get_lane_settings(service: LaneSettingsService = Depends(get_service)) -> LaneSettingsView:
    return service.view()


@router.put("", response_model=LaneSettingsView)
async def update_lane_settings(
    payload: LaneSettingsUpdate,
    service: LaneSettingsService = Depends(get_service),
    settings: Settings = Depends(get_settings),
    _current: AuthSession = Depends(require_csrf),
) -> LaneSettingsView:
    try:
        view = service.update(
            idle_stop_seconds=payload.idle_stop_seconds,
            general_concurrency=payload.general_concurrency,
            proxy_concurrency=payload.proxy_concurrency,
        )
    except SettingLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await LaneService(settings).sync_settings(
        view.idle_stop_seconds.value,
        view.general_concurrency.value,
        view.proxy_concurrency.value,
    )
    return view
