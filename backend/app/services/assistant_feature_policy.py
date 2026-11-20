"""Persist host-owned assistant permissions independently of feature configuration."""

import json

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.features.catalog import FEATURES
from app.models.plugin import PluginRegistration, utcnow
from app.models.setting import AppSetting


def _setting_key(key: str) -> str:
    return f"assistant.feature_policy.{key}"


def policy_binding(db: Session, key: str) -> dict:
    row = db.get(AppSetting, _setting_key(key), populate_existing=True)
    if row is None:
        return {"allowed": key in FEATURES, "revision": 0}
    return json.loads(row.value_json)


def assistant_allowed(db: Session, key: str) -> bool:
    return policy_binding(db, key)["allowed"]


def assistant_explicitly_allowed(db: Session, key: str) -> bool:
    binding = policy_binding(db, key)
    return binding["revision"] > 0 and binding["allowed"] is True


def require_assistant_allowed(db: Session, key: str) -> None:
    if not assistant_allowed(db, key):
        from app.services.assistant_categories import Denied

        raise Denied("assistant_feature_denied")


def set_assistant_allowed(db: Session, key: str, allowed: bool) -> None:
    """Stage a policy change and invalidate plugin grants; the caller commits."""
    if type(allowed) is not bool:
        raise ValueError("assistant_allowed must be a boolean")
    plugin = None
    if key not in FEATURES:
        parts = key.split(".", 2)
        if len(parts) == 3 and parts[0] == "plugin":
            plugin = db.get(PluginRegistration, parts[1], populate_existing=True)
        keys = set() if plugin is None else {
            item["key"] for raw in (plugin.features_json, plugin.captcha_methods_json)
            for item in json.loads(raw)
        }
        if plugin is not None:
            keys.update(
                f"plugin.{plugin.plugin_id}.{capability}"
                for capability in json.loads(plugin.capabilities_json)
                if capability in {"secret.credentials", "secret.totp"}
            )
        if plugin is None or plugin.removed_at is not None or key not in keys:
            raise ValueError("Unknown feature, captcha method or secret provider")
    binding = policy_binding(db, key)
    if binding["revision"] > 0 and binding["allowed"] == allowed:
        return
    if plugin is not None:
        result = db.execute(update(PluginRegistration).where(
            PluginRegistration.plugin_id == plugin.plugin_id,
            PluginRegistration.permission_epoch == plugin.permission_epoch,
            PluginRegistration.generation == plugin.generation,
        ).values(permission_epoch=PluginRegistration.permission_epoch + 1, updated_at=utcnow()),
            execution_options={"synchronize_session": False})
        if result.rowcount != 1:
            from app.services.plugin_runtime import PluginGenerationConflict

            raise PluginGenerationConflict("plugin revision changed")
        db.refresh(plugin)
    row = db.get(AppSetting, _setting_key(key))
    if row is None:
        row = AppSetting(key=_setting_key(key))
        db.add(row)
    row.value_json = json.dumps({"allowed": allowed, "revision": binding["revision"] + 1})
    row.updated_at = utcnow()
    db.flush()
