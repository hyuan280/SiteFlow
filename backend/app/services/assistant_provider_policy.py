"""Assistant authority for secret providers, separate from plugin approval."""

import json

from sqlalchemy import select

from app.models.plugin import PluginRegistration
from app.services.assistant_categories import Denied
from app.services.assistant_feature_policy import policy_binding


def provider_policy_key(provider_id, capability):
    if (not isinstance(provider_id, str) or not provider_id.startswith("plugin.")
            or not provider_id[7:] or "." in provider_id[7:]
            or capability not in {"secret.credentials", "secret.totp"}):
        raise Denied("credentials_unavailable")
    return f"{provider_id}.{capability}"


def provider_binding(db, provider_id, capability):
    key = provider_policy_key(provider_id, capability)
    row = db.get(PluginRegistration, provider_id[7:], populate_existing=True)
    policy = policy_binding(db, key)
    if (row is None or row.removed_at is not None or not row.enabled
            or row.approved_at is None
            or capability not in json.loads(row.capabilities_json)
            or set(json.loads(row.approved_capabilities_json)) != set(json.loads(row.capabilities_json))
            or policy.get("allowed") is not True):
        raise Denied("credentials_unavailable")
    return {"policy": policy, "generation": row.generation,
            "permission_epoch": row.permission_epoch}


def authorized_providers(db, capability):
    result = []
    for row in db.scalars(select(PluginRegistration).order_by(PluginRegistration.plugin_id)):
        identifier = f"plugin.{row.plugin_id}"
        try:
            provider_binding(db, identifier, capability)
        except Denied:
            continue
        result.append({"id": identifier, "name": row.name, "capability": capability})
    return result
