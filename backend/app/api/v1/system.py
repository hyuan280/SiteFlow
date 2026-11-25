from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import get_current_session, get_db, require_csrf
from app.schemas.system import SystemResources, SystemSettings
from app.services.system_settings import SystemSettingsService

router = APIRouter(
    prefix="/api/v1",
    tags=["system"],
    dependencies=[Depends(get_current_session)],
)


def get_service(session: Session = Depends(get_db)) -> SystemSettingsService:
    return SystemSettingsService(session)


@router.get("/system-settings", response_model=SystemSettings)
def get_system_settings(
    service: SystemSettingsService = Depends(get_service),
) -> SystemSettings:
    return service.view()


@router.put(
    "/system-settings", response_model=SystemSettings, dependencies=[Depends(require_csrf)],
)
def update_system_settings(
    payload: SystemSettings,
    service: SystemSettingsService = Depends(get_service),
) -> SystemSettings:
    return service.update(payload)


@router.get("/system-resources", response_model=SystemResources)
def get_system_resources(
    request: Request,
    service: SystemSettingsService = Depends(get_service),
) -> SystemResources:
    if not service.view().show_system_resources:
        raise HTTPException(status_code=403, detail="系统资源监控未开启")
    # A sync route runs blocking sampling and lock waits in FastAPI's thread pool.
    return request.app.state.system_resource_sampler.sample()
