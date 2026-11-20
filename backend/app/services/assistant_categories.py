"""Host-owned category policy; model-provided categories confer no authority."""

from uuid import uuid4

from sqlalchemy import select, update

from app.features.base import Category
from app.features.catalog import FEATURES
from app.models.assistant import AssistantMessage, AssistantSession


class Denied(Exception):
    pass


def builtin_category(feature_key: str) -> Category:
    feature = FEATURES.get(feature_key) if isinstance(feature_key, str) else None
    if (feature is None or feature_key.startswith("plugin.")
            or feature.category not in {"flow", "auth", "challenge", "sign", "action", "post"}):
        raise Denied("execution_feature_denied")
    return feature.category


def execution_category(db, feature_key: str) -> Category:
    """Resolve approved plugin authority against both the registry and current DB state."""
    from app.services.assistant_feature_policy import assistant_allowed

    if not isinstance(feature_key, str):
        raise Denied("execution_feature_denied")
    if not assistant_allowed(db, feature_key):
        raise Denied("execution_feature_denied")
    if not isinstance(feature_key, str) or not feature_key.startswith("plugin."):
        return builtin_category(feature_key)
    from app.features.plugin_registry import get_plugin_feature, plugin_metadata_for_row
    from app.models.plugin import PluginRegistration
    from app.services.plugin_feature_policy import effective_features, category_setting_key
    from app.models.setting import AppSetting

    feature = get_plugin_feature(feature_key)
    row = db.get(PluginRegistration, feature.plugin_id, populate_existing=True) if feature else None
    if row is None:
        raise Denied("execution_feature_denied")
    db.get(AppSetting, category_setting_key(row.plugin_id), populate_existing=True)
    current, _ = plugin_metadata_for_row(row)
    current = effective_features(db, row.plugin_id, current)
    if feature not in current or feature.category not in {
            "flow", "auth", "challenge", "sign", "action", "post"}:
        raise Denied("execution_feature_denied")
    return feature.category


def execution_features(db):
    from app.features.plugin_registry import plugin_features

    result = {}
    for feature in [*FEATURES.values(), *plugin_features()]:
        try:
            execution_category(db, feature.key)
        except Denied:
            continue
        result[feature.key] = feature
    return result


def execution_binding(db, key):
    from app.services.assistant_feature_policy import policy_binding
    from app.models.setting import AppSetting
    import hashlib
    import json

    execution_category(db, key)
    settings = db.get(AppSetting, f"feature.{key}", populate_existing=True)
    return {"policy": policy_binding(db, key),
            "settings": hashlib.sha256(json.dumps(json.loads(settings.value_json),
                sort_keys=True, separators=(",", ":")).encode()).hexdigest() if settings else None,
            **({"plugin": plugin_execution_binding(db, key)} if key.startswith("plugin.") else {})}


def plugin_execution_binding(db, feature_key: str) -> dict:
    execution_category(db, feature_key)
    from app.features.plugin_registry import get_plugin_feature
    from app.models.plugin import PluginRegistration

    feature = get_plugin_feature(feature_key)
    row = db.get(PluginRegistration, feature.plugin_id)
    return {"generation": row.generation, "permission_epoch": row.permission_epoch,
            "category": feature.category, "version": feature.version,
            "schema": feature.config_schema()}


def auth_budget_context(manager, db, session_id, data, *, turn=None, auth_limit=None):
    """Called under the session write lock; fallback identity survives service recreation."""
    active = manager._active_turns.get(session_id) or {}
    if turn is not None and active and (
            (active.get("id") is not None and turn.get("id") != active["id"])
            or ("_request_id" in turn and turn["_request_id"] != active.get("_request_id"))):
        raise Denied("execution_authorization_changed")
    identity = active.get("_request_id") or active.get("id")
    if not identity:
        identity = db.scalar(select(AssistantMessage.request_id).where(
            AssistantMessage.session_id == session_id, AssistantMessage.role == "user",
            AssistantMessage.request_id.is_not(None),
        ).order_by(AssistantMessage.id.desc()).limit(1))
    if not identity:
        identity = data.get("_execution_turn_id") or data.setdefault("_auth_budget_turn", str(uuid4()))
    limits = data.setdefault("_auth_budget_limits", {})
    if identity not in limits:
        value = active.get("_assistant_settings", {}).get("max_auth_uses_per_turn", auth_limit)
        value = 3 if value is None else value
        if type(value) is not int or not 1 <= value <= 20:
            raise Denied("execution_authorization_changed")
        limits[identity] = value
    return identity, limits[identity]


def require_auth_capacity(data, turn_id, limit):
    if (type(limit) is not int or not 1 <= limit <= 20
            or data.get("_auth_budget_limits", {}).get(turn_id) != limit):
        raise Denied("execution_authorization_changed")
    records = data.get("_execution_records", {})
    if sum(record.get("category") == "auth" and record.get("turn_id") == turn_id
           for record in records.values()) >= limit:
        raise Denied("auth_budget_exhausted")
    if len(records) >= 256:
        raise Denied("execution_operation_limit")


def run_auth_binding(db, run_id, crypto):
    """Private persisted authority, never taken from worker/model payloads."""
    from app.models.assistant_workflow import AssistantWorkflowAction
    from app.services.config_ai_generation import _strict_json

    action = db.scalar(select(AssistantWorkflowAction).where(
        AssistantWorkflowAction.run_id == run_id, AssistantWorkflowAction.kind == "run"))
    if action is None or action.state != "complete":
        raise Denied("execution_authorization_changed")
    saved = _strict_json(crypto.decrypt(
        f"assistant-workflow:{action.id}", action.ciphertext, action.nonce, action.key_version))
    binding = saved.get("auth_budget")
    if (not isinstance(binding, dict) or set(binding) != {"session_id", "owner", "turn_id", "limit"}
            or binding["session_id"] != action.session_id or binding["owner"] != action.owner
            or not isinstance(binding["turn_id"], str)
            or type(binding["limit"]) is not int or not 1 <= binding["limit"] <= 20):
        raise Denied("execution_authorization_changed")
    return action.id, binding


def reserve_run_auth_use(session_factory, settings, crypto, action_id, binding,
                         job_id, attempt, step_key, feature, operation_id):
    """Charge a new job auth lease atomically against its original turn, even after chat ends."""
    from app.models.execution import Run, SiteJob
    from app.models.workflow import Site
    from app.services.assistant_run_guard import validate_guard
    from app.services.config_ai_generation import _json, _strict_json

    with session_factory() as db:
        if execution_category(db, feature) != "auth":
            return
        changed = db.execute(update(AssistantSession).where(
            AssistantSession.id == binding["session_id"], AssistantSession.owner == binding["owner"],
        ).values(updated_at=AssistantSession.updated_at))
        if changed.rowcount != 1:
            raise Denied("execution_authorization_changed")
        db.expire_all()
        job = db.get(SiteJob, job_id)
        run = db.get(Run, job.run_id) if job else None
        site = db.get(Site, job.site_id) if job else None
        if (not job or not run or not site or run.trigger != "assistant" or run.cancel_requested
                or run.status not in {"queued", "running"} or job.status not in {"queued", "running"}
                or job.attempt != attempt or run_auth_binding(db, run.id, crypto) != (action_id, binding)
                or not validate_guard(db, run.id, site, settings, crypto)):
            raise Denied("execution_authorization_changed")
        row = db.get(AssistantSession, binding["session_id"])
        data = _strict_json(crypto.decrypt(
            f"assistant:session:{row.id}", row.ciphertext, row.nonce, row.key_version))
        records = data.setdefault("_execution_records", {})
        if operation_id in records:
            raise Denied("operation_already_used")
        require_auth_capacity(data, binding["turn_id"], binding["limit"])
        records[operation_id] = {
            "category": "auth", "turn_id": binding["turn_id"], "state": "terminal",
            "status": "reserved", "code": "auth_use_reserved", "run_id": run.id,
            "job_id": job.id, "attempt": attempt, "step_key": step_key, "feature_type": feature,
        }
        encrypted = crypto.encrypt(f"assistant:session:{row.id}", _json(data))
        row.ciphertext, row.nonce, row.key_version = encrypted.ciphertext, encrypted.nonce, encrypted.key_version
        db.commit()
