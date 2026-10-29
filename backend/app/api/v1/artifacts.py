from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_session, get_db, get_settings, require_csrf
from app.core.config import Settings
from app.models.auth import AuthSession
from app.schemas.artifact import ArtifactCleanupResult, ArtifactStatsView
from app.services.artifacts import ArtifactService

router = APIRouter(
    prefix="/api/v1/artifacts",
    tags=["artifacts"],
    dependencies=[Depends(get_current_session)],
)


def get_service(
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ArtifactService:
    return ArtifactService(session, settings)


@router.get("/stats", response_model=ArtifactStatsView)
def artifact_stats(service: ArtifactService = Depends(get_service)) -> ArtifactStatsView:
    return service.stats()


@router.post("/cleanup", response_model=ArtifactCleanupResult)
def cleanup_artifacts(
    service: ArtifactService = Depends(get_service),
    _current: AuthSession = Depends(require_csrf),
) -> ArtifactCleanupResult:
    return service.cleanup()
