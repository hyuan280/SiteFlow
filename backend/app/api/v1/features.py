from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Any
from app.api.deps import get_db, require_csrf
from app.features.catalog import get_feature
from app.features.base import FeatureConfigError
from app.services.feature_settings import FeatureSettingsService
from app.models.setting import AppSetting

from app.api.deps import get_current_session
from app.features.catalog import list_features
from app.schemas.workflow import FeatureView

router = APIRouter(
    prefix="/api/v1/features",
    tags=["workflow"],
    dependencies=[Depends(get_current_session)],
)


def _view(feature) -> FeatureView:  # type: ignore[no-untyped-def]
    return FeatureView(
        key=feature.key,
        name=feature.name,
        category=feature.category,
        cardinality=feature.cardinality,
        requires=sorted(feature.requires),
        description=feature.description,
        version=feature.version,
        config_schema=feature.config_schema(),
        source=getattr(feature, "plugin_id", "builtin"),
    )


@router.get("", response_model=list[FeatureView])
def get_features(session: Session = Depends(get_db)) -> list[FeatureView]:
    return [FeatureSettingsService(session).view(feature) for feature in list_features()]


class FeatureSettingsUpdate(BaseModel):
    defaults: dict[str, Any]
    required_fields: list[str]
    execution_settings: dict[str, Any] | None = None


@router.put("/{key}/settings", response_model=FeatureView, dependencies=[Depends(require_csrf)])
def update_settings(key: str, payload: FeatureSettingsUpdate, session: Session = Depends(get_db)):
    feature = get_feature(key)
    if feature is None:
        raise HTTPException(404, "Unknown feature")
    try:
        return FeatureSettingsService(session).update(feature, payload.defaults, payload.required_fields, payload.execution_settings)
    except FeatureConfigError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.delete("/{key}/settings", response_model=FeatureView, dependencies=[Depends(require_csrf)])
def reset_settings(key: str, session: Session = Depends(get_db)):
    feature = get_feature(key)
    if feature is None:
        raise HTTPException(404, "Unknown feature")
    row = session.get(AppSetting, f"feature.{feature.key}")
    if row:
        session.delete(row)
        session.commit()
    return FeatureSettingsService(session).view(feature)
