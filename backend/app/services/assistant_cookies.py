"""Explicit saved-site cookie authority; cookie values never enter assistant state."""

from __future__ import annotations

import asyncio
import hashlib
import time
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select, update

from app.models.assistant_workflow import AssistantWorkflowAction
from app.models.execution import SiteJob
from app.models.workflow import Site, SiteCredential
from app.schemas.assistant import CookieClearInteraction
from app.services.assistant import AssistantError
from app.services.assistant_retry import fingerprint
from app.services.assistant_urls import origin
from app.services.assistant_workflows import AssistantWorkflows, workflow_binding
from app.services.credentials import CredentialService


def bound_cookie_site(db, target):
    # An explicit destination wins even when its binding is stale: never fall back.
    for field, private in (("workflow_site_id", "_workflow_binding"),
                           ("site_id", "_credential_binding")):
        if target.get(field) is not None or target.get(private) is not None:
            site = db.get(Site, target.get(field)) if target.get(field) is not None else None
            try:
                valid = (site is not None and target.get(private) == workflow_binding(site)
                         and origin(site.base_url) == origin(target["base_url"]))
            except (ValueError, TypeError, KeyError):
                valid = False
            if not valid:
                raise AssistantError("cookie_binding_changed")
            return site
    return None


def saved_cookies_configured(db, site_id):
    return db.scalar(select(SiteCredential.site_id).where(
        SiteCredential.site_id == site_id,
        SiteCredential.cookies_ciphertext.is_not(None), SiteCredential.cookies_ciphertext != b"",
        SiteCredential.cookies_nonce.is_not(None), SiteCredential.cookies_nonce != b"",
    )) is not None


def private_cookie_seed(db, manager, target, generation):
    site = bound_cookie_site(db, target)
    if site is None:
        return {}
    try:
        if not isinstance(generation, str) or UUID(generation).version != 4:
            raise ValueError
        generation = str(UUID(generation))
        rows = CredentialService(db, manager.crypto).resolve_cookies(site.id) or []
        host, target_origin = urlsplit(target["base_url"]).hostname, origin(target["base_url"])
        cookies = []
        for cookie in rows:
            try:
                if "domain" in cookie:
                    allowed = cookie["domain"] in {host, "." + host}
                    # Ambiguous domain+URL rows must satisfy both boundaries.
                    if "url" in cookie:
                        allowed = allowed and origin(cookie["url"]) == target_origin
                else:
                    allowed = isinstance(cookie.get("url"), str) and origin(cookie["url"]) == target_origin
            except (ValueError, TypeError):
                continue
            if allowed:
                cookies.append(cookie)
        from app.services.config_ai_generation import _json

        if len(cookies) > 300 or len(_json(cookies).encode()) > 256 * 1024:
            raise ValueError
        return {"cookies": cookies, "cookie_generation": generation} if cookies else {}
    except Exception:
        raise AssistantError("cookie_restore_unavailable") from None


def _scope_identity(tools, target):
    scope = tools.manager.browsers.get(tools.session_id)
    lane = "proxy_interaction" if target.get("proxy") else "interaction"
    if (not scope or scope.owner != tools.owner or not scope.ready or scope.status != "ready"
            or scope.deadline <= time.monotonic() or scope.lane_key != lane
            or scope.site_origin != origin(target["base_url"])):
        raise AssistantError("browser_reopen_required")
    return {"session_id": scope.session_id, "lane_key": scope.lane_key,
            "origin": scope.site_origin, "token_fingerprint": hashlib.sha256(scope.token.encode()).hexdigest()}


def _context(db, tools, data):
    if ("_takeover" in data
            or (tools.manager._active_turns.get(tools.session_id) or {}).get("_takeover_action_id")):
        raise AssistantError("takeover_tool_denied")
    target = data.get("target") or {}
    if target.get("confirmed") is not True:
        raise AssistantError("target_confirmation_required")
    if target.get("browser_consent") is not True:
        raise AssistantError("browser_consent_required")
    site = bound_cookie_site(db, target)
    if site is None:
        raise AssistantError("cookie_site_required")
    scope = _scope_identity(tools, target)
    if db.scalar(select(SiteJob.id).where(
            SiteJob.site_id == site.id, SiteJob.status.in_(("queued", "running"))).limit(1)):
        raise AssistantError("site_run_active")
    return target, site, scope


def _cookie_snapshot(db, site_id):
    row = db.execute(select(
        SiteCredential.cookies_ciphertext, SiteCredential.cookies_nonce,
        SiteCredential.cookie_count, SiteCredential.key_version,
    ).where(SiteCredential.site_id == site_id)).mappings().one_or_none()
    return dict(row) if row is not None else None


def _cookie_fingerprint(snapshot):
    return fingerprint({key: value.hex() if isinstance(value, bytes) else value
                        for key, value in snapshot.items()} if snapshot is not None else None)


def _action(db, tools, operation_id):
    return db.scalar(select(AssistantWorkflowAction).where(
        AssistantWorkflowAction.session_id == tools.session_id,
        AssistantWorkflowAction.owner == tools.owner,
        AssistantWorkflowAction.kind == "cookie_clear",
        AssistantWorkflowAction.request_fingerprint == fingerprint({"operation_id": operation_id})))


def _workflows(tools):
    return AssistantWorkflows(tools.manager, tools.owner, tools.session_id, tools.target)


def prepare_cookie_clear(db, tools, request, data):
    if tools is None or getattr(tools, "_cookie_clear_request", None) != request.operation_id:
        raise AssistantError("cookie_clear_internal_only")
    target, site, scope = _context(db, tools, data)
    row = db.get(SiteCredential, site.id)
    public = {"kind": "authorization", "purpose": "cookie_clear", "options": [], "fields": [],
              "multiple": False, "title": "\u786e\u8ba4\u6e05\u9664\u7ed1\u5b9a\u7ad9\u70b9 Cookie", "description":
              "\u4ec5\u6e05\u9664\u7ed1\u5b9a\u7ad9\u70b9\u5df2\u4fdd\u5b58\u7684 Cookie\uff0c\u4ee5\u53ca\u5f53\u524d\u5171\u4eab\u6d4f\u89c8\u5668\u8f66\u9053\u4e2d\u7cbe\u786e\u4e3b\u673a\u7684 Cookie\u3002\u8bf7\u6838\u5bf9\u4e0b\u65b9\u8303\u56f4\u5e76\u786e\u8ba4\u3002",
              "review": {"site_id": site.id, "name": site.name, "base_url": target["base_url"],
                         "lane_key": scope["lane_key"],
                         "saved_cookie_count": row.cookie_count if row and row.cookies_ciphertext else 0,
                         "warnings": [
                             "\u4ec5\u6e05\u9664\u5f53\u524d\u5171\u4eab\u8f66\u9053\u4e2d host \u548c .host \u7684\u6240\u6709\u8def\u5f84 Cookie\uff0c\u4e0d\u6e05\u9664\u7236\u57df Cookie\u3002",
                             "\u6b64\u8f66\u9053\u4e2d\u540c\u4e00\u4e3b\u673a\u7684\u5176\u4ed6\u6807\u7b7e\u9875\u53ef\u80fd\u9000\u51fa\u767b\u5f55\u3002",
                             "\u4fdd\u7559\u5df2\u4fdd\u5b58\u7684\u8d26\u53f7\u3001\u5bc6\u7801\u548c localStorage\uff0c\u4e0d\u5237\u65b0\u9875\u9762\u3002",
                             "\u540e\u7eed\u65b0\u7684\u6210\u529f\u4efb\u52a1\u4ecd\u53ef\u80fd\u91cd\u65b0\u4fdd\u5b58 Cookie\u3002"]}}
    args = {"operation_id": request.operation_id, "binding": workflow_binding(site),
            "cookie_fingerprint": _cookie_fingerprint(_cookie_snapshot(db, site.id)), "scope": scope,
            "target_fingerprint": fingerprint(target), "target_revision": data.get("_target_revision", 0)}
    return public, args, "cookie_clear:" + request.operation_id


def _result(operation_id, *, cleared=False, code="browser_outcome_unknown"):
    return {"ok": cleared, "purpose": "cookie_clear", "operation_id": operation_id,
            "saved_cookies_cleared": True, "browser_cookies_cleared": cleared,
            "browser_outcome": "cleared" if cleared else "unknown",
            **({"code": code} if not cleared else {})}


def apply_cookie_clear(db, tools, data, args):
    operation_id = args["operation_id"]
    if tools is None or getattr(tools, "_cookie_clear_request", None) != operation_id:
        raise AssistantError("cookie_clear_internal_only")
    target, site, scope = _context(db, tools, data)
    snapshot = _cookie_snapshot(db, site.id)
    if (args["binding"] != workflow_binding(site) or args["scope"] != scope
            or args["cookie_fingerprint"] != _cookie_fingerprint(snapshot)
            or args["target_fingerprint"] != fingerprint(target)
            or args["target_revision"] != data.get("_target_revision", 0)):
        raise AssistantError("cookie_clear_binding_changed")
    if _action(db, tools, operation_id) is not None:
        raise AssistantError("cookie_clear_operation_conflict")
    # Hold the reviewed site identity through cookie CAS and the approval commit.
    locked = db.execute(update(Site).where(
        Site.id == site.id, Site.history_key == args["binding"]["history_key"],
        Site.config_revision == args["binding"]["revision"], Site.base_url == site.base_url,
        True if snapshot is not None else ~select(SiteCredential.site_id).where(
            SiteCredential.site_id == site.id).exists(),
        ~select(SiteJob.id).where(SiteJob.site_id == site.id,
                                 SiteJob.status.in_(("queued", "running"))).exists(),
    ).values(config_revision=Site.config_revision).execution_options(synchronize_session=False))
    if locked.rowcount != 1:
        raise AssistantError("cookie_clear_binding_changed")
    expected = [getattr(SiteCredential, key) == value for key, value in (snapshot or {}).items()]
    cleared = db.execute(update(SiteCredential).where(
        SiteCredential.site_id == site.id, snapshot is not None, *expected,
    ).values(cookies_ciphertext=None, cookies_nonce=None, cookie_count=0, cookies_updated_at=None)
        .execution_options(synchronize_session=False))
    if cleared.rowcount != (1 if snapshot is not None else 0):
        raise AssistantError("cookie_clear_binding_changed")
    cleared_snapshot = ({**snapshot, "cookies_ciphertext": None, "cookies_nonce": None,
                         "cookie_count": 0} if snapshot is not None else None)
    workflows = _workflows(tools)
    action, _ = workflows._action(db, "cookie_clear", {"operation_id": operation_id})
    workflows._identity(action, site)
    workflows._seal(action, {"request": {"operation_id": operation_id}, "binding": args,
                             "cleared_cookie_fingerprint": _cookie_fingerprint(cleared_snapshot),
                             "result": _result(operation_id)})
    return {"operation_id": operation_id, "saved_cookies_cleared": True}


async def clear_bound_site_cookies(tools, arguments):
    from app.services.assistant_interactions import request_interaction, running

    try:
        if not isinstance(arguments, dict) or set(arguments) != {"operation_id"}:
            return {"ok": False, "code": "invalid_arguments"}
        request = CookieClearInteraction.model_validate({"purpose": "cookie_clear", **arguments})
    except ValidationError:
        return {"ok": False, "code": "invalid_arguments"}
    operation_id = request.operation_id
    workflows = _workflows(tools)
    try:
        with tools.manager.session_factory() as db:
            row = tools.manager._owned(db, tools.owner, tools.session_id)
            data = tools.manager._decrypt(row)
            if ("_takeover" in data
                    or (tools.manager._active_turns.get(tools.session_id) or {}).get("_takeover_action_id")):
                return {"ok": False, "code": "takeover_tool_denied"}
            action = _action(db, tools, operation_id)
            if action:
                return workflows._open(action)["result"]
            # A denied/cancelled operation cannot acquire a new approval next turn.
            for item in data.get("_interaction_decisions", {}).values():
                if (item.get("purpose") == "cookie_clear"
                        and item.get("arguments", {}).get("operation_id") == operation_id):
                    return item.get("result") or {"ok": False, "code": "interaction_already_requested"}
            running(tools.manager, db, tools.owner, tools.session_id)
            _context(db, tools, data)
        if getattr(tools, "_cookie_clear_request", None) is not None:
            return {"ok": False, "code": "interaction_already_pending"}
        tools._cookie_clear_request = operation_id
        try:
            approved = await request_interaction(tools, request.model_dump())
            if not approved.get("ok"):
                return approved
            with tools.manager.session_factory() as db:
                action = _action(db, tools, operation_id)
                if action is None:
                    return {"ok": False, "code": "cookie_clear_approval_required"}
                saved = workflows._open(action)
                if action.state != "reserved":
                    return saved["result"]
                result = saved["result"]
                scope = None
                try:
                    _, data = running(tools.manager, db, tools.owner, tools.session_id)
                    _, site, identity = _context(db, tools, data)
                    frozen = saved["binding"]
                    if (identity != frozen["scope"] or fingerprint(data.get("target")) != frozen["target_fingerprint"]
                            or data.get("_target_revision", 0) != frozen["target_revision"]):
                        raise AssistantError("cookie_clear_binding_changed")
                    if (_cookie_fingerprint(_cookie_snapshot(db, site.id))
                            != saved["cleared_cookie_fingerprint"]):
                        raise AssistantError("saved_cookies_changed")
                    scope = tools.manager.browsers[tools.session_id]
                except AssistantError as exc:
                    result = _result(operation_id, code=("saved_cookies_changed"
                                     if exc.code == "saved_cookies_changed" else "browser_scope_changed"))
                # Durable claim before I/O. Unknown is deliberately never retried.
                claimed = db.execute(update(AssistantWorkflowAction).where(
                    AssistantWorkflowAction.id == action.id, AssistantWorkflowAction.state == "reserved"
                ).values(state="unknown"))
                if claimed.rowcount != 1:
                    db.expire_all()
                    return workflows._open(db.get(AssistantWorkflowAction, action.id))["result"]
                saved["result"] = result
                workflows._seal(action, saved)
                db.commit()
            if scope is None:
                return result
            try:
                raw = await tools.manager._worker(f"{scope.session_id}/clear-cookies", {
                    "token": scope.token, "operation_id": operation_id})
                cleared = (raw.get("session_id") == scope.session_id
                           and raw.get("lane_key") == scope.lane_key and raw.get("cleared") is True)
            except Exception:
                cleared = False
            result = _result(operation_id, cleared=cleared)
            with tools.manager.session_factory() as db:
                action = _action(db, tools, operation_id)
                saved = workflows._open(action)
                saved["result"] = result
                action.state = "complete" if cleared else "unknown"
                workflows._seal(action, saved)
                db.commit()
            return result
        finally:
            tools._cookie_clear_request = None
    except AssistantError as exc:
        return {"ok": False, "code": exc.code}
    except asyncio.CancelledError:
        # The approval transaction already saved a truthful unknown result.
        raise
