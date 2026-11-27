from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.api.deps import get_current_session, get_db, require_csrf
from app.schemas.system import ComponentBuild, SystemResources, SystemSettings, SystemVersions
from app.services.bitwarden import DEFAULT_BITWARDEN_URL
from app.services.system_settings import SystemSettingsService

BUILD_INFO_PATH = Path(__file__).resolve().parents[3] / "build-info"

router = APIRouter(
    prefix="/api/v1",
    tags=["system"],
    dependencies=[Depends(get_current_session)],
)


def get_service(session: Session = Depends(get_db)) -> SystemSettingsService:
    return SystemSettingsService(session)


def api_build() -> ComponentBuild:
    try:
        version, _, commit = BUILD_INFO_PATH.read_text(encoding="utf-8").partition("\n")
        return ComponentBuild(version=version or None, commit=commit.strip() or None)
    except (OSError, ValueError):
        return ComponentBuild(version=None, commit=None)


async def fetch_build(client: httpx.AsyncClient, base_url: str) -> ComponentBuild | None:
    try:
        async with asyncio.timeout(2):
            response = await client.get(f"{base_url.rstrip('/')}/version")
            if response.status_code == 404:
                return ComponentBuild(version=None, commit=None)  # Older service.
            response.raise_for_status()
            return ComponentBuild.model_validate(response.json())
    except (httpx.HTTPError, httpx.InvalidURL, TimeoutError, ValueError):
        return None


@router.get("/system/version", response_model=SystemVersions)
async def get_system_versions(request: Request, response: Response) -> SystemVersions:
    response.headers["Cache-Control"] = "no-store"
    async with httpx.AsyncClient(trust_env=False) as client:
        worker, bitwarden = await asyncio.gather(
            fetch_build(client, request.app.state.settings.worker_url),
            fetch_build(client, DEFAULT_BITWARDEN_URL),
        )
    return SystemVersions(api=api_build(), worker=worker, bitwarden=bitwarden)


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
