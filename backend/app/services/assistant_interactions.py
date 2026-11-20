"""Durable, typed user decisions. Model arguments never carry authority."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import re
import time
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import update

from app.models.assistant import AssistantSession
from app.models.workflow import Site
from app.schemas.assistant import InteractionResponse, UserInteraction
from app.services.assistant_urls import contains_redaction, project_url
from app.services.config_ai_generation import _has_secret, _json


PENDING = "_pending_interaction"
LEDGER = "_interaction_decisions"
INTERACTION_TIMEOUT = 180
PUBLIC = ("id", "kind", "purpose", "title", "description", "expires_at", "options",
          "multiple", "fields", "review")
ADAPTER = TypeAdapter(UserInteraction)
SECRET = re.compile(
    r"password|passwd|api[\s_-]*key|secret|cookie|bearer|\btoken\b|access[\s_-]*token|"
    r"private[\s_-]*key|totp|otp|2[\s_-]*fa|mfa|"
    r"(?:verification|authentication|auth|recovery|backup|pass)[\s_\-\u2010-\u2015]*code|"
    r"one[\s_\-\u2010-\u2015]*time[\s_\-\u2010-\u2015]*(?:code|password)|"
    r"\u5bc6\u7801|\u53e3\u4ee4|\u5bc6\u94a5|\u4ee4\u724c|\u9a8c\u8bc1\u7801|\u52a8\u6001\u7801", re.I)


def secret_input(value, key=""):
    """Shared guard for user text and interactive questions, not a secret detector oracle."""
    if key and _has_secret(value, key):
        return True
    if isinstance(value, dict):
        for name, child in value.items():
            if (re.sub(r"[^a-z]", "", name.lower()) in {
                "password", "passwd", "apikey", "secret", "cookie", "authorization",
                "token", "accesstoken", "refreshtoken", "totpsecret", "privatekey",
            } and child not in (None, "", False, [], {})) or secret_input(child, key):
                return True
    elif isinstance(value, list):
        return any(secret_input(child, key) for child in value)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return bool(re.search(
        r"(?:" + SECRET.pattern + r"|authorization)"
        r"[\"']?\s*[=:\uFF1A]\s*\S+|Bearer\s+\S+|-----BEGIN .*PRIVATE KEY-----|"
        r"\b(?:sk-|ghp_)[A-Za-z0-9_-]{16,}", text, re.I))


def pending_interaction(data):
    item = data.get(PENDING)
    return {key: deepcopy(item[key]) for key in PUBLIC} if item and item["status"] == "pending" else None


def unsafe_model_interaction(arguments, key):
    """Reject before storing model question arguments in conversation history."""
    if secret_input(arguments, key):
        return True
    request = arguments.get("request") if isinstance(arguments, dict) else None
    if isinstance(request, dict) and request.get("purpose") == "target":
        target = request.get("target")
        if isinstance(target, dict) and isinstance(target.get("base_url"), str):
            try:
                if project_url(target["base_url"])["url_redacted"]:
                    return True
            except ValueError:
                pass  # Invalid nonsensitive URLs are rejected by the typed parser.
    if isinstance(request, dict) and request.get("purpose") == "question":
        text = [request.get("title", ""), request.get("description", "")]
        fields = request.get("fields", [])
        if isinstance(fields, list):
            text += [str(field.get(k, "")) for field in fields if isinstance(field, dict)
                     for k in ("id", "label")]
        return bool(SECRET.search(" ".join(str(item) for item in text)))
    return False


def terminal(status):
    return {"ok": False, "code": "interaction_" + status, "terminal": True,
            "message": "\u672a\u6388\u4e88\u6743\u9650\u3002\u672c\u8f6e\u8bf7\u52ff\u518d\u6b21\u8bf7\u6c42\u540c\u4e00\u4e8b\u9879\u3002"}


def finish(data, item, status, result=None):
    item["status"] = status
    item["result"] = deepcopy(result if result is not None else terminal(status))
    if (status != "completed" and item.get("purpose") == "question"
            and item.get("arguments", {}).get("required", True)):
        item["result"]["blocking"] = True
    if data.get(PENDING, {}).get("id") == item["id"]:
        data.pop(PENDING, None)


def cancel_interactions(data):
    for item in data.get(LEDGER, {}).values():
        if item["status"] in {"pending", "approved"}:
            finish(data, item, "cancelled")
    data.pop(PENDING, None)


def running(manager, db, owner, sid):
    from app.services.assistant import AssistantError

    manager._owned(db, owner, sid)
    changed = db.execute(update(AssistantSession).where(
        AssistantSession.id == sid, AssistantSession.owner == owner,
        AssistantSession.status == "running",
    ).values(status="running"))
    if changed.rowcount != 1:
        raise AssistantError("interaction_not_running")
    db.expire_all()
    row = manager._owned(db, owner, sid)
    return row, manager._decrypt(row)


def bind_sites(db, target):
    from app.services.assistant import AssistantError
    from app.services.assistant_execution import _origin
    from app.services.assistant_workflows import workflow_binding

    for field, private in (("site_id", "_credential_binding"),
                           ("workflow_site_id", "_workflow_binding")):
        if target.get(field) is not None:
            site = db.get(Site, target[field])
            if site is None or _origin(site.base_url) != _origin(target["base_url"]):
                raise AssistantError("credential_site_mismatch" if field == "site_id"
                                     else "workflow_site_mismatch", 422)
            target[private] = workflow_binding(site)


def grant(target, capability, *, features=None, db=None):
    from app.services.assistant_categories import builtin_category, execution_category, execution_features
    from app.services.assistant_execution import SUPPORTED_FEATURES

    if capability == "execution":
        credentials = target.get("credential_mode") == "bitwarden" or (
            target.get("credential_mode") in {"manual", "site"} and target.get("site_id") is not None)
        available = set(execution_features(db)) if db is not None else SUPPORTED_FEATURES
        category = (lambda key: execution_category(db, key)) if db is not None else builtin_category
        features = set(available if features is None else features)
        features &= available
        features = {key for key in features if credentials or not (
            category(key) == "auth" or key == "challenge.2fa")}
        target["execution_consent"] = True
        target["execution_features"] = sorted(features)
        target["credential_consent"] = credentials and any(
            category(key) == "auth" or key == "challenge.2fa" for key in features)
        target["allow_sign"] = any(category(key) in {"sign", "challenge", "post"} or key in {
            "action.click", "action.fill"} for key in features)
    else:
        target[capability + "_consent"] = True


def apply_defaults(target, values, *, db=None):
    if target and target.get("confirmed") is True:
        for capability in ("execution", "screenshot", "workflow"):
            if values.get("auto_authorize_" + capability) is True:
                grant(target, capability, db=db)


def invalidate_retry_authority(data):
    from app.services.assistant_retry import GRANTS, cancel_approvals

    # Keep used grants and uncertainty records intact; never restore a one-use
    # grant if a later permission change recreates the same target fingerprint.
    cancel_approvals(data)
    for grant_ in data.get(GRANTS, {}).values():
        if grant_.get("status") == "approved":
            grant_["status"] = "expired"


def apply_turn_defaults(manager, owner, sid, values):
    with manager.session_factory() as db:
        row, data = running(manager, db, owner, sid)
        turn = manager._active_turns.get(sid)
        if turn:
            data["_interaction_turn_id"] = turn["id"]
        previous = deepcopy(data.get("target"))
        apply_defaults(data.get("target"), values, db=db)
        if previous != data.get("target"):
            invalidate_retry_authority(data)
        manager._encrypt(row, data)
        db.commit()


def prepare(db, request, data, values, *, tools=None):
    """Build canonical host review and a private, closed set of permitted effects."""
    from app.services.assistant import AssistantError
    from app.services.assistant_categories import execution_features, execution_category

    supported = execution_features(db)

    args = request.model_dump()
    purpose = request.purpose
    if purpose == "cookie_clear":
        from app.services.assistant_cookies import prepare_cookie_clear

        return prepare_cookie_clear(db, tools, request, data)
    if purpose == "scheduler_config":
        from app.services.assistant_scheduler import prepare_scheduler_update

        return prepare_scheduler_update(db, tools, request, data)
    if purpose == "run_cancel":
        from app.services.assistant_operations import prepare_run_cancel

        return prepare_run_cancel(db, tools, request, data)
    if purpose == "template_import":
        from app.services.assistant_templates import prepare_template_import

        return prepare_template_import(db, tools, request, data)
    if purpose == "site_config":
        from app.services.assistant_site_config import prepare_site_config

        return prepare_site_config(db, tools, request, data)
    if purpose == "consumption":
        from app.services.assistant_consumption import prepare_consumption

        return prepare_consumption(db, tools, request, data)
    target = data.get("target")
    public = {"kind": "authorization", "purpose": purpose, "options": [], "fields": [],
              "multiple": False, "review": {}}
    if purpose == "target":
        proposed = request.target.model_dump()
        if contains_redaction(proposed) or project_url(proposed["base_url"])["url_redacted"]:
            raise AssistantError("secret_input", 422)
        proposed.update(confirmed=True, browser_consent=True, proxy=False,
                        execution_consent=False, execution_features=[], credential_consent=False,
                        allow_sign=False, screenshot_consent=False, workflow_consent=False,
                        run_lane_policy="general", trusted_origins=[])
        bind_sites(db, proposed)
        apply_defaults(proposed, values, db=db)
        public.update(title="\u786e\u8ba4\u76ee\u6807\u7f51\u7ad9", description=(
            "\u8bf7\u6838\u5bf9\u7f51\u7ad9\u540d\u79f0\u548c URL\u3002\u786e\u8ba4\u540e\u5141\u8bb8\u6d4f\u89c8\u5e76\u5411\u6a21\u578b\u53d1\u9001\u53ef\u89c1\u9875\u9762\u6587\u672c\uff0c\u4e0d\u53d1\u9001\u51ed\u636e\u3002"
            "\u6267\u884c\u3001\u622a\u56fe\u53ca\u4fdd\u5b58\u5e76\u8fd0\u884c\u9700\u5355\u72ec\u6388\u6743\uff0c\u5168\u5c40\u81ea\u52a8\u6388\u6743\u9664\u5916\u3002\u4fdd\u5b58\u540e\u9ed8\u8ba4\u4f7f\u7528 general \u8fd0\u884c\u8f66\u9053\u3002"),
            review={k: proposed[k] for k in ("name", "base_url")})
        args = {"purpose": purpose, "target": proposed}
        subject = "target"
    elif purpose == "question":
        # Generic confirmations and collected preferences never modify target authority.
        text = " ".join([request.title, request.description] + [
            part for field in request.fields for part in (field.id, field.label)])
        if SECRET.search(text) or secret_input(args):
            raise AssistantError("secret_input", 422)
        public.update({k: args[k] for k in ("kind", "title", "description", "options", "fields", "multiple")})
        from app.services.assistant_retry import fingerprint

        subject = "question:" + fingerprint({k: args[k] for k in (
            "title", "description", "kind", "options", "fields", "multiple", "required")})
        public["review"]["required"] = request.required
    else:
        if not target or target.get("confirmed") is not True:
            raise AssistantError("target_confirmation_required")
        if purpose == "trusted_origins":
            if any(project_url(url)["url_redacted"] for url in request.origins):
                raise AssistantError("secret_input", 422)
            public.update(title="\u786e\u8ba4\u4fe1\u4efb\u6765\u6e90", description=(
                "\u4ec5\u5141\u8bb8\u8df3\u8f6c\u5e76\u89c2\u5bdf\u6240\u5217\u6765\u6e90\uff0c\u4e0d\u5141\u8bb8\u8f6c\u53d1\u51ed\u636e\uff0c\u4e5f\u4e0d\u6388\u6743\u5b8c\u6574\u8fd0\u884c\u8bbf\u95ee\u8fd9\u4e9b\u6765\u6e90\u3002"),
                review={"base_url": target["base_url"], "origins": sorted(request.origins)})
            subject = "trusted_origins:" + _json(sorted(request.origins))
            if len(set(target.get("trusted_origins", [])) | set(request.origins)) > 20:
                raise AssistantError("trusted_origins_limit", 422)
        elif purpose == "proxy":
            public.update(kind="choice", title="\u9009\u62e9\u8f66\u9053\u4e0e\u4ee3\u7406", description={
                "browser": "\u8bf7\u9009\u62e9\u89c2\u5bdf\u6d4f\u89c8\u5668\u662f\u5426\u4f7f\u7528\u5df2\u914d\u7f6e\u4ee3\u7406\uff0c\u4e0d\u6539\u53d8\u5de5\u4f5c\u6d41\u7684\u4fdd\u5b58\u8fd0\u884c\u8f66\u9053\u3002",
                "workflow": "\u8bf7\u9009\u62e9\u5de5\u4f5c\u6d41\u5bfc\u5165\u4fdd\u5b58\u53ca\u5b8c\u6574\u8fd0\u884c\u7684\u8f66\u9053\uff0c\u4e0d\u6539\u53d8\u89c2\u5bdf\u6d4f\u89c8\u5668\u7684\u4ee3\u7406\u504f\u597d\u3002",
                "both": "\u8bf7\u9009\u62e9\u89c2\u5bdf\u6d4f\u89c8\u5668\u7684\u4ee3\u7406\u504f\u597d\uff0c\u540c\u65f6\u8bbe\u7f6e\u5de5\u4f5c\u6d41\u5bfc\u5165\u4fdd\u5b58\u53ca\u5b8c\u6574\u8fd0\u884c\u7684\u8f66\u9053\u3002",
            }[request.scope] + "\u5f53\u524d\u6d4f\u89c8\u5668\u5c06\u5173\u95ed\uff0c\u5df2\u6709\u8349\u6848\u9700\u91cd\u65b0\u5ba1\u9605\u3002\u6a21\u578b\u5efa\u8bae\u4e0d\u662f\u6388\u6743\uff0c\u4ee5\u4f60\u7684\u9009\u62e9\u4e3a\u51c6\uff1b\u6b64\u9009\u62e9\u4e0d\u4f1a\u7acb\u5373\u4fdd\u5b58\u6216\u8fd0\u884c\u3002",
                options=[
                    {"id": "general", "label": "\u901a\u7528\u8f66\u9053\uff08\u4e0d\u4f7f\u7528\u4ee3\u7406\uff09"},
                    {"id": "proxy_general", "label": "\u4ee3\u7406\u901a\u7528\u8f66\u9053\uff08\u4f7f\u7528\u5df2\u914d\u7f6e\u4ee3\u7406\uff09"},
                ],
                review={"scope": request.scope, "name": target["name"], "base_url": target["base_url"],
                        "suggested_lane": "proxy_general" if request.enabled else "general",
                        "browser_implementation": {"general": "interaction", "proxy_general": "proxy_interaction"},
                        "run_lane_policy": {"general": "general", "proxy_general": "proxy_general"},
                        "current_browser_lane": "proxy_interaction" if target.get("proxy") else "interaction",
                        "current_run_lane_policy": target.get("run_lane_policy", "auto")})
            subject = "proxy:" + request.scope
        else:
            if request.capability == "execution":
                if request.feature_type is not None and request.feature_type not in supported:
                    raise AssistantError("execution_feature_denied", 422)
                if request.feature_type and (execution_category(db, request.feature_type) == "auth" or request.feature_type == "challenge.2fa") and not (
                    target.get("credential_mode") == "bitwarden" or (
                        target.get("credential_mode") in {"manual", "site"} and target.get("site_id"))):
                    raise AssistantError("credentials_unavailable", 422)
                reviewed = deepcopy(target)
                grant(reviewed, "execution", db=db)
                args["execution_features"] = reviewed["execution_features"]
            public.update(title={"execution": "\u6388\u6743\u771f\u5b9e\u6267\u884c", "screenshot": "\u6388\u6743\u622a\u56fe\u53d1\u9001",
                                 "workflow": "\u6388\u6743\u4fdd\u5b58\u5e76\u8fd0\u884c"}[request.capability],
                          description={"execution": "\u5141\u8bb8\u5728\u6b64\u76ee\u6807\u4e0a\u6267\u884c\u6240\u5217\u5168\u90e8\u529f\u80fd\uff0c\u5305\u62ec\u4f7f\u7528\u79c1\u6709\u51ed\u636e\u767b\u5f55\uff08\u53ef\u7528\u65f6\uff09\u3001\u7b7e\u5230\u548c\u70b9\u51fb\u3002\u8fd9\u662f\u4e00\u6b21\u6574\u4f53\u6388\u6743\uff0c\u64cd\u4f5c\u4f1a\u4ea7\u751f\u771f\u5b9e\u6548\u679c\u5e76\u53ef\u80fd\u6d88\u8017\u989d\u5ea6\u6216\u8d39\u7528\uff1b\u51ed\u636e\u4e0d\u4f1a\u53d1\u9001\u7ed9\u6a21\u578b\u3002",
                                       "screenshot": "\u5141\u8bb8\u5c06\u906e\u7f69\u540e\u7684\u622a\u56fe\u53d1\u9001\u7ed9\u6a21\u578b\u3002\u8868\u5355\u548c\u5185\u5d4c\u6846\u67b6\u5916\u4ecd\u53ef\u80fd\u5305\u542b\u4e2a\u4eba\u4fe1\u606f\uff0c\u5e76\u53ef\u80fd\u4ea7\u751f\u6a21\u578b\u8d39\u7528\u3002",
                                       "workflow": "\u5141\u8bb8\u5c06\u5df2\u5ba1\u9605\u7684\u5de5\u4f5c\u6d41\u5bfc\u5165\u4fdd\u5b58\uff0c\u5e76\u5bf9\u6b64\u76ee\u6807\u53d1\u8d77\u5b8c\u6574\u771f\u5b9e\u8fd0\u884c\u3002\u4f1a\u4fee\u6539\u7ad9\u70b9\u914d\u7f6e\uff0c\u8fd0\u884c\u53ef\u80fd\u4f7f\u7528\u79c1\u6709\u51ed\u636e\u3001\u4ea7\u751f\u771f\u5b9e\u64cd\u4f5c\u53ca\u989d\u5ea6\u6216\u8d39\u7528\uff1b\u8bf7\u6838\u5bf9\u4e0b\u65b9\u76ee\u6807\u548c\u8fd0\u884c\u8f66\u9053\u3002"}[request.capability],
                          review={"base_url": target["base_url"], "name": target["name"],
                                  "credential_mode": target.get("credential_mode"),
                                  "site_id": target.get("site_id"),
                                  "workflow_site_id": target.get("workflow_site_id"),
                                  "run_lane_policy": target.get("run_lane_policy", "auto"),
                                   "capability": request.capability, "feature_type": request.feature_type})
            if request.capability == "execution":
                public["review"]["execution_features"] = args["execution_features"]
                public["review"]["credential_consent"] = reviewed["credential_consent"]
                public["review"]["allow_sign"] = reviewed["allow_sign"]
            subject = "authorization:" + request.capability
    return public, args, subject


def binding(data, item):
    from app.services.assistant_retry import fingerprint

    return (item["target_revision"] == data.get("_target_revision", 0)
            and item["target_fingerprint"] == fingerprint(data.get("target"))
            and item["turn_id"] == data.get("_interaction_turn_id"))


def validate_response(item, response, key):
    from app.services.assistant import AssistantError

    value = response.model_dump(exclude_none=True)
    if secret_input(value, key):
        raise AssistantError("secret_input", 422)
    if response.decision == "deny":
        return value
    if item["kind"] == "authorization":
        valid = response.selections is None and response.values is None
    elif item["kind"] == "choice":
        selections = response.selections or []
        valid = (response.values is None and bool(selections)
                 and len(selections) == len(set(selections))
                 and (item["multiple"] or len(selections) == 1)
                 and set(selections) <= {option["id"] for option in item["options"]})
    else:
        fields = {field["id"]: field for field in item["fields"]}
        values = response.values or {}
        valid = (response.selections is None and response.values is not None and set(values) <= fields.keys()
                 and all(values.get(field, "").strip() for field in fields if fields[field]["required"]))
        if any(SECRET.search(k + " " + v) for k, v in values.items()):
            raise AssistantError("secret_input", 422)
    if not valid:
        raise AssistantError("invalid_interaction_response", 422)
    return value


async def respond_interaction(manager, owner, sid, interaction_id, response):
    from app.services.assistant import AssistantError

    async with manager._mutation(owner, sid):
        with manager.session_factory() as db:
            # Replays remain read-only even after the turn finishes.
            row = manager._owned(db, owner, sid)
            data = manager._decrypt(row)
            item = next((v for v in data.get(LEDGER, {}).values() if v["id"] == interaction_id), None)
            if item is None:
                raise AssistantError("interaction_not_found", 404)
            response = InteractionResponse.model_validate(response.model_dump(exclude_none=True))
            if item["status"] != "pending":
                if (item["status"] in {"completed", "approved", "denied"}
                        and item.get("response") == response.model_dump(exclude_none=True)):
                    return manager._detail(db, row)
                raise AssistantError("interaction_conflict")
            row, data = running(manager, db, owner, sid)
            item = next(v for v in data[LEDGER].values() if v["id"] == interaction_id)
            if item["status"] != "pending":
                raise AssistantError("interaction_conflict")
            if not binding(data, item):
                finish(data, item, "invalidated")
            elif time.time() >= item["expires_epoch"]:
                finish(data, item, "timed_out")
            else:
                key = manager._interaction_keys.get(interaction_id)
                if key is None and item["kind"] == "input":
                    key = manager._runtime()["api_key"]
                value = validate_response(item, response, key or "")
                item["response"] = value
                if response.decision == "deny":
                    finish(data, item, "denied")
                else:
                    # Authority is applied only by the waiting invocation, never by this route.
                    item["status"] = "approved"
                    data.pop(PENDING, None)
            manager._encrypt(row, data)
            db.commit()
            detail = manager._detail(db, row)
    event = manager._interaction_waiters.get(interaction_id)
    if event:
        event.set()
    if item["status"] not in {"approved", "denied"}:
        raise AssistantError("interaction_" + item["status"])
    return detail


async def request_interaction(tools, arguments):
    from app.services.assistant import AssistantError
    from app.services.assistant_retry import fingerprint

    manager, owner, sid = tools.manager, tools.owner, tools.session_id
    interaction_id, ledger_key = None, None
    try:
        if secret_input(arguments, tools.config.get("api_key", "")):
            raise AssistantError("secret_input", 422)
        request = ADAPTER.validate_python(arguments, strict=True)
        with manager.session_factory() as db:
            row, data = running(manager, db, owner, sid)
            public, args, subject = prepare(db, request, data, tools.config.get("_assistant_settings", {}), tools=tools)
            # Denials are deduplicated for the entire turn, including target revisions.
            ledger_key = tools.turn["id"] + ":" + subject
            ledger = data.setdefault(LEDGER, {})
            if request.purpose == "proxy":
                previous = next((item for item in ledger.values()
                                 if item["turn_id"] == tools.turn["id"] and item["purpose"] == "proxy"
                                 and item["status"] in {"denied", "timed_out", "cancelled", "invalidated"}), None)
                if previous:
                    return deepcopy(previous["result"])
            if ledger_key in ledger:
                if request.purpose in {"site_config", "template_import", "scheduler_config"} and ledger[ledger_key]["status"] == "completed":
                    return terminal("already_requested")
                return deepcopy(ledger[ledger_key].get("result") or terminal("already_requested"))
            if request.purpose == "trusted_origins":
                for previous in ledger.values():
                    if (previous["turn_id"] == tools.turn["id"]
                            and previous["purpose"] == "trusted_origins"
                            and previous["status"] in {"denied", "timed_out", "cancelled", "invalidated"}
                            and set(previous["arguments"]["origins"]) & set(request.origins)):
                        return deepcopy(previous["result"])
            if data.get(PENDING):
                return terminal("already_pending")
            remaining = tools.turn["deadline"] - asyncio.get_running_loop().time()
            if remaining <= 0:
                # Still persist the terminal subject, so later calls cannot reprompt.
                expires = time.time()
            else:
                expires = time.time() + INTERACTION_TIMEOUT
            while len(ledger) >= 256:
                old = next((k for k, v in ledger.items() if v["turn_id"] != tools.turn["id"]
                            and v["status"] not in {"pending", "approved"}), None)
                if old is None:
                    return terminal("limit")
                del ledger[old]
            interaction_id = str(uuid4())
            item = {**public, "id": interaction_id, "status": "pending", "arguments": args,
                    "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat(),
                    "expires_epoch": expires, "turn_id": tools.turn["id"],
                    "target_revision": data.get("_target_revision", 0),
                    "target_fingerprint": fingerprint(data.get("target")),
                    "invocation": deepcopy(tools.call_context)}
            ledger[ledger_key] = item
            data["_interaction_turn_id"] = tools.turn["id"]
            manager._interaction_keys[interaction_id] = tools.config.get("api_key", "")
            if remaining <= 0:
                finish(data, item, "timed_out")
            else:
                data[PENDING] = item
            manager._encrypt(row, data)
            db.commit()
        event = asyncio.Event()
        manager._interaction_waiters[interaction_id] = event
        while True:
            dispose = False
            with manager.session_factory() as db:
                row, data = running(manager, db, owner, sid)
                item = data[LEDGER][ledger_key]
                if item["status"] in {"pending", "approved"}:
                    if not binding(data, item):
                        finish(data, item, "invalidated")
                    elif item["expires_epoch"] <= time.time():
                        finish(data, item, "timed_out")
                    elif item["status"] == "approved":
                        args, purpose = item["arguments"], item["purpose"]
                        target = data.get("target")
                        previous_target = deepcopy(target)
                        site_result = {}
                        if purpose == "target":
                            target = deepcopy(args["target"])
                            bind_sites(db, target)
                            if target != args["target"]:
                                raise AssistantError("interaction_binding_changed")
                            data["target"] = target
                            dispose = True
                        elif purpose == "trusted_origins":
                            target["trusted_origins"] = sorted(set(target.get("trusted_origins", [])) | set(args["origins"]))
                            dispose = True
                        elif purpose == "proxy":
                            lane = item["response"]["selections"][0]
                            if args["scope"] in {"browser", "both"}:
                                target["proxy"] = lane == "proxy_general"
                            if args["scope"] in {"workflow", "both"}:
                                target["run_lane_policy"] = lane
                            dispose = True
                        elif purpose == "authorization":
                            grant(target, args["capability"], features=args.get("execution_features"), db=db)
                        elif purpose == "site_config":
                            from app.services.assistant_site_config import apply_site_config

                            site_result = apply_site_config(db, tools, data, args)
                            dispose = True
                        elif purpose == "consumption":
                            from app.services.assistant_consumption import apply_consumption

                            apply_consumption(db, tools, data, args)
                        elif purpose == "template_import":
                            from app.services.assistant_templates import apply_template_import

                            site_result = apply_template_import(db, tools, data, args)
                        elif purpose == "scheduler_config":
                            from app.services.assistant_scheduler import apply_scheduler_update

                            site_result = apply_scheduler_update(db, tools, data, args)
                        elif purpose == "run_cancel":
                            from app.services.assistant_operations import apply_run_cancel

                            site_result = apply_run_cancel(db, tools, data, args)
                        elif purpose == "cookie_clear":
                            from app.services.assistant_cookies import apply_cookie_clear

                            site_result = apply_cookie_clear(db, tools, data, args)
                        if target != previous_target or purpose == "site_config":
                            invalidate_retry_authority(data)
                        if purpose in {"target", "trusted_origins", "proxy", "site_config"}:
                            data["_target_revision"] = data.get("_target_revision", 0) + 1
                            data.pop("_context_summary", None)
                            data.pop("_workflow_proposal_id", None)
                        result = {"ok": True, "purpose": purpose, **item["response"], **site_result}
                        if purpose == "cookie_clear":
                            result = {"ok": True, "purpose": purpose, **site_result}
                        if purpose not in {"question", "scheduler_config", "run_cancel", "cookie_clear"}:
                            result["target"] = manager._public_target(target)
                        finish(data, item, "completed", result)
                manager._encrypt(row, data)
                db.commit()
                status = item["status"]
                result = deepcopy(item.get("result"))
                tools.target = deepcopy(data.get("target"))
            if dispose:
                await manager._close_browser(sid)
            if status not in {"pending", "approved"}:
                return result
            await manager._wait_for_user(
                tools.turn, event, min(0.25, max(0.001, expires - time.time())))
            event.clear()
    except ValidationError:
        return {"ok": False, "code": "invalid_arguments"}
    except AssistantError as exc:
        return {"ok": False, "code": exc.code, **({
            "notice": "Bind a same-origin saved site with credentials or select Bitwarden; do not provide secrets in chat.",
        } if exc.code == "credentials_unavailable" else {})}
    finally:
        if interaction_id:
            manager._interaction_waiters.pop(interaction_id, None)
            manager._interaction_keys.pop(interaction_id, None)
            with manager.session_factory() as db:
                db.execute(update(AssistantSession).where(AssistantSession.id == sid).values(
                    updated_at=AssistantSession.updated_at))
                row = manager._owned(db, owner, sid)
                data = manager._decrypt(row)
                item = data.get(LEDGER, {}).get(ledger_key)
                if item and item["status"] in {"pending", "approved"}:
                    finish(data, item, "cancelled")
                    manager._encrypt(row, data)
                    db.commit()


async def ensure_permission(tools, capability, feature=None):
    target = tools.target
    if not target or target.get("confirmed") is not True:
        return {"ok": False, "code": "target_confirmation_required"}
    permitted = target.get(capability + "_consent") is True
    if capability == "execution":
        permitted = permitted and feature in target.get("execution_features", [])
    if permitted:
        return {"ok": True}
    arguments = {"purpose": "authorization", "capability": capability}
    if feature is not None:
        arguments["feature_type"] = feature
    return await request_interaction(tools, arguments)
