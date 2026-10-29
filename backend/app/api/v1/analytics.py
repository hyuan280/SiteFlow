from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_session, get_db, get_settings
from app.core.config import Settings
from app.schemas.analytics import FailuresView, MatrixView, TrendsView
from app.services.analytics import AnalyticsService

router = APIRouter(
    prefix="/api/v1/analytics",
    tags=["analytics"],
    dependencies=[Depends(get_current_session)],
)


def get_service(
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AnalyticsService:
    return AnalyticsService(session, settings)


@router.get("/matrix", response_model=MatrixView)
def analytics_matrix(
    days: int = Query(default=14, ge=1, le=365),
    site_id: int | None = None,
    include_disabled: bool = False,
    service: AnalyticsService = Depends(get_service),
) -> MatrixView:
    return service.matrix(days, site_id=site_id, include_disabled=include_disabled)


@router.get("/trends", response_model=TrendsView)
def analytics_trends(
    days: int = Query(default=30, ge=1, le=365),
    site_id: int | None = None,
    service: AnalyticsService = Depends(get_service),
) -> TrendsView:
    return service.trends(days, site_id=site_id)


@router.get("/failures", response_model=FailuresView)
def analytics_failures(
    days: int = Query(default=7, ge=1, le=365),
    site_id: int | None = None,
    status: str | None = Query(default=None),
    lane: str | None = None,
    feature_type: str | None = None,
    q: str | None = None,
    date: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    service: AnalyticsService = Depends(get_service),
) -> FailuresView:
    statuses = {
        item.strip() for item in (status or "").split(",") if item.strip()
    } or None
    return service.failures(
        days,
        site_id=site_id,
        statuses=statuses,
        lane=lane,
        feature_type=feature_type,
        query=q,
        date_value=date,
        page=page,
        page_size=page_size,
    )
