"""Database-only reference inspection, independent of the approved runtime catalog."""
import json

from jsonschema import Draft202012Validator
from sqlalchemy import select

from app.features.plugin_registry import build_remote_feature
from app.models.workflow import Site, Template, WorkflowStep
from app.services.feature_settings import FeatureSettingsService
from app.services.secret_providers import credential_provider
from app.services.workflows import _deep_merge, _substitute


def references(session, row, features=None, capabilities=None, methods=None):
    features = json.loads(row.features_json) if features is None else features
    capabilities = json.loads(row.capabilities_json) if capabilities is None else capabilities
    methods = json.loads(row.captcha_methods_json) if methods is None else methods
    catalog = {item["key"]: item for item in features}
    method_keys = {item["key"] for item in methods}
    templates = {item.id: item for item in session.scalars(select(Template))}
    steps = {}
    for step in session.scalars(select(WorkflowStep).order_by(WorkflowStep.id)):
        steps.setdefault((step.owner_type, step.owner_id), []).append(step)
    policy = FeatureSettingsService(session)
    result = []
    owners = [("template", item) for item in templates.values()]
    owners += [("site", item) for item in session.scalars(select(Site))]
    prefix = f"plugin.{row.plugin_id}."
    provider = f"plugin.{row.plugin_id}"
    for kind, owner in owners:
        chain = []
        current = owner if kind == "template" else templates.get(owner.template_id)
        seen = set()
        while current is not None and current.id not in seen:
            seen.add(current.id)
            chain.append(("template", current.id))
            current = templates.get(current.parent_id)
        chain.reverse()
        if kind == "site":
            chain.append((kind, owner.id))
        effective = {}
        for key in chain:
            for step in steps.get(key, []):
                previous = effective.get(step.step_key)
                config = _deep_merge(previous[1] if previous else {}, json.loads(step.config_json))
                enabled = step.enabled if step.enabled is not None else (previous[2] if previous else True)
                effective[step.step_key] = (step, config, enabled)
        if kind == "site" and credential_provider(owner) == provider:
            result.append(dict(owner_type=kind, owner_id=owner.id, name=owner.name, step_key=None, enabled=owner.enabled, reference="credentials", errors=[] if "secret.credentials" in capabilities else ["capability_removed"]))
        for step, config, enabled in effective.values():
            errors = []
            refs = []
            if step.feature_type.startswith(prefix):
                refs.append("feature")
                item = catalog.get(step.feature_type)
                if item is None:
                    errors.append("feature_removed")
                else:
                    feature = build_remote_feature(row.plugin_id, item)
                    defaults, required, _ = policy.policy(feature)
                    merged = {**defaults, **config}
                    if kind == "site":
                        merged = _substitute(merged, owner.base_url)
                    if step.feature_version != feature.version:
                        errors.append("feature_version_changed")
                    try:
                        if any(Draft202012Validator(feature.schema).iter_errors(merged)):
                            errors.append("schema_incompatible")
                    except Exception:
                        errors.append("schema_validation_failed")
                    if any(key not in merged or merged[key] is None for key in required):
                        errors.append("required_fields_missing")
            # Built-in TOTP/captcha steps also inherit persisted global defaults.
            from app.features.catalog import get_feature
            builtin = get_feature(step.feature_type)
            if builtin is not None and not step.feature_type.startswith("plugin."):
                defaults, _, _ = policy.policy(builtin)
                config = {**defaults, **config}
            if step.feature_type == "challenge.2fa" and config.get("totp_provider_id") == provider:
                refs.append("totp")
                if "secret.totp" not in capabilities:
                    errors.append("capability_removed")
            method = config.get("captcha_method")
            if isinstance(method, str) and method.startswith(prefix):
                refs.append("captcha_method")
                if method not in method_keys:
                    errors.append("captcha_method_removed")
            if refs:
                result.append(dict(owner_type=kind, owner_id=owner.id, name=owner.name, step_key=step.step_key, enabled=enabled, reference=",".join(refs), errors=sorted(set(errors))))
    return {"includes_disabled_steps": True, "items": result, "reference_count": len(result), "incompatible_count": sum(bool(item["errors"]) for item in result)}


def snapshot_change(session, row, features, capabilities, methods):
    old = json.loads(row.features_json)
    previous = {"manifest": json.loads(row.manifest_json), "features": old, "captcha_methods": json.loads(row.captcha_methods_json)}
    report = references(session, row, features, capabilities, methods)
    before = {item["key"]: item for item in old}
    changes = []
    for item in features:
        prior = before.pop(item["key"], None)
        if prior and prior.get("config_schema") != item.get("config_schema"):
            changes.append({"feature": item["key"], "code": "schema_required_or_defaults_changed"})
    changes += [{"feature": key, "code": "feature_removed"} for key in before]
    report["changes"] = changes
    row.previous_metadata_json = json.dumps(previous)
    row.change_impacts_json = json.dumps(report)
