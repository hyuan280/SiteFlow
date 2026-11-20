from __future__ import annotations

import json
from dataclasses import replace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.features.base import Category
from app.features.plugin_registry import (
    PLUGIN_FEATURE_CATEGORIES,
    RemoteFeature,
    plugin_metadata_for_row,
)
from app.models.plugin import PluginRegistration
from app.models.setting import AppSetting


CATEGORY_RISKS: dict[Category, tuple[int, str]] = {
    "flow": (1, "低风险：流程控制"),
    "action": (3, "中风险：页面操作"),
    "auth": (4, "高风险：身份认证"),
    "challenge": (3, "中风险：验证挑战"),
    "sign": (4, "高风险：签到操作"),
    "post": (5, "极高风险：提交与后处理"),
}


def category_setting_key(plugin_id: str) -> str:
    return f"plugin.feature_categories.{plugin_id}"


def category_overrides(db: Session, plugin_id: str) -> dict[str, Category]:
    row = db.get(AppSetting, category_setting_key(plugin_id))
    if row is None:
        return {}
    values = json.loads(row.value_json)
    return {key: value for key, value in values.items() if value in PLUGIN_FEATURE_CATEGORIES}


def effective_features(
    db: Session, plugin_id: str, features: list[RemoteFeature]
) -> list[RemoteFeature]:
    overrides = category_overrides(db, plugin_id)
    return [replace(feature, category=overrides.get(feature.key, feature.category)) for feature in features]


def approved_plugin_features(db: Session) -> list[RemoteFeature]:
    """Read current DB approvals, never trusting a potentially stale process registry."""
    features = []
    for row in db.scalars(select(PluginRegistration).execution_options(populate_existing=True)):
        declared, _ = plugin_metadata_for_row(row)
        features.extend(effective_features(db, row.plugin_id, declared))
    return sorted(features, key=lambda feature: (feature.plugin_id, feature.key))
