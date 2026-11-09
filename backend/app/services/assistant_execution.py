"""Host-authorized, single-step execution on the assistant's existing browser.

``target`` is a private, server-loaded target, never model tool arguments. When
site_id is selected, SetTarget must freeze _credential_binding as
{id, history_key, revision}, with revision taken from Site.config_revision.
The manager must exclude that binding and the operation ledger from model/public
context. This service writes only the encrypted assistant session ledger.
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from copy import deepcopy
import time
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import update

from app.features.catalog import FEATURES
from app.models.assistant import AssistantSession
from app.models.workflow import Site
from app.schemas.assistant import public_url
from app.schemas.workflow import SiteWrite, StepInput
from app.services.assistant_snapshot import POLICY_CODES
from app.services.assistant_retry import (
    GRANTS, READ_ONLY, RECORDS, credential_binding, fingerprint,
    uncertain_operations, valid_grant,
)
from app.services.assistant_tools import failure, safe_snapshot
from app.services.assistant_urls import contains_redaction, project_url, resolve_url
from app.services.bitwarden import BitwardenService
from app.services.config_ai_generation import _has_secret, _json
from app.services.credentials import CredentialService
from app.services.feature_settings import FeatureSettingsService
from app.services.login_evidence import project_success_evidence
from app.services.workflows import _substitute


SUPPORTED_FEATURES = frozenset({
    "flow.open", "action.navigate", "auth.form_login", "auth.password_verify",
    "action.wait", "sign.button", "sign.unit3d_claim", "challenge.altcha",
    "challenge.slider", "action.click",
})
_CODES = {
    "success": {"step_succeeded"},
    "failed": {"step_failed"},
    "error": {"execution_error", "execution_timeout", "execution_canceled"},
    "unknown": {"outcome_uncertain"},
}
_EVIDENCE_CODES = POLICY_CODES | {
    "browser_snapshot_stale", "browser_frame_unreadable", "browser_frame_unavailable",
}
_DIAGNOSTIC_CODES = {
    "reason": _EVIDENCE_CODES | _CODES["error"],
    "evidence_errorcode": _EVIDENCE_CODES | {"browser_snapshot_unavailable"},
}
_HTTP_METHODS = frozenset({
    "GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "CONNECT", "TRACE", "OTHER",
})
_LOGS = ["Step execution started", "Step execution finished"]
_LEDGER = "_execution_operations"


class _Denied(Exception):
    pass


def _origin(url):
    return urlsplit(public_url(url))[:2]


class AssistantExecution:
    def __init__(self, manager, owner: str, session_id: str, target: dict | None, *,
                 retry_authorization: dict | None = None, turn=None, invocation=None):
        self.manager = manager
        self.owner = owner
        self.session_id = session_id
        self.target = deepcopy(target)
        self.retry_authorization = deepcopy(retry_authorization)
        self.turn = turn
        self.invocation = invocation
        self.operation_id = None
        self._reserved = False

    def _bound_site(self, db):
        identifier = self.target.get("site_id")
        binding = self.target.get("_credential_binding")
        if identifier is None:
            if binding is not None:
                raise _Denied("credential_binding_changed")
            return None
        if (type(identifier) is not int or identifier < 1 or not isinstance(binding, dict)
                or set(binding) != {"id", "history_key", "revision"}
                or type(binding["id"]) is not int or binding["id"] != identifier
                or type(binding["revision"]) is not int
                or not isinstance(binding["history_key"], str)):
            raise _Denied("credential_binding_changed")
        site = db.get(Site, identifier)
        if (site is None or site.history_key != binding["history_key"]
                or site.config_revision != binding["revision"]
                or _origin(site.base_url) != _origin(self.target["base_url"])):
            raise _Denied("credential_binding_changed")
        return site

    def _uncertainty_gate(self, db, data, scope, feature):
        if feature in READ_ONLY:
            return
        from app.services.assistant_run_guard import unresolved_assistant_runs

        site_id = (data.get("target") or {}).get("workflow_site_id")
        if site_id and unresolved_assistant_runs(db, site_id):
            raise _Denied("workflow_outcome_uncertain")
        uncertain = uncertain_operations(self.manager, db, self.session_id, data)
        if self._reserved:
            uncertain.discard(self.operation_id)
        if self.retry_authorization:
            original = self.retry_authorization["operation_id"]
            if not valid_grant(data, scope, original, self.retry_authorization["id"],
                               self.operation_id if self._reserved else None):
                raise _Denied("retry_authorization_required")
            uncertain.discard(original)
            # An unknown retry retains the original fact and links to the new
            # uncertainty. Approval of that new operation covers its ancestors.
            pending, seen = [original], set()
            records = data.get(RECORDS, {})
            while pending:
                child = pending.pop()
                if child in seen:
                    continue
                seen.add(child)
                parent = records.get(child, {}).get("retry_of")
                for operation, record in records.items():
                    # retry_of is committed at reservation, before a finalizer
                    # can write the parent's resolution (or a process can crash).
                    if (operation == parent
                            or record.get("resolution", {}).get("retry_operation_id") == child):
                        uncertain.discard(operation)
                        pending.append(operation)
        if uncertain:
            raise _Denied("retry_authorization_required")

    def _authorize(self, scope, feature, *, check_uncertain=True, db=None):
        manager = self.manager
        task = asyncio.current_task()
        if (manager._stopping or not manager.settings.config_ai_enabled
                or (task is not None and task.cancelling())):
            raise _Denied("execution_disabled")
        if (scope is None or manager.browsers.get(self.session_id) is not scope
                or scope.owner != self.owner or not scope.ready
                or scope.deadline <= time.monotonic()
                or scope.lane_key != ("proxy_interaction" if self.target.get("proxy") else "interaction")):
            raise _Denied("browser_reopen_required")
        with (manager.session_factory() if db is None else nullcontext(db)) as db:
            row = manager._owned(db, self.owner, self.session_id)
            data = manager._decrypt(row)
            if row.status != "running" or data.get("target") != self.target:
                raise _Denied("execution_authorization_changed")
            if self.target.get("confirmed") is not True:
                raise _Denied("target_confirmation_required")
            if self.target.get("execution_consent") is not True:
                raise _Denied("execution_consent_required")
            allowed = self.target.get("execution_features", [])
            if (not isinstance(allowed, list) or any(
                    not isinstance(key, str) or key not in SUPPORTED_FEATURES for key in allowed
            ) or feature not in allowed or feature not in SUPPORTED_FEATURES):
                raise _Denied("execution_feature_denied")
            if ((feature.startswith("sign.") or feature in {
                    "action.click", "challenge.altcha", "challenge.slider",
            })
                    and self.target.get("allow_sign") is not True):
                raise _Denied("sign_consent_required")
            if feature.startswith("auth.") and self.target.get("credential_consent") is not True:
                raise _Denied("credential_consent_required")
            _origin(self.target["base_url"])
            self._bound_site(db)
            if check_uncertain:
                if self.operation_id in data.get(_LEDGER, []) and not self._reserved:
                    raise _Denied("operation_already_used")
                self._uncertainty_gate(db, data, scope, feature)

    def _normalize(self, raw):
        step = StepInput.model_validate(raw, strict=True)
        if step.enabled is False or step.condition not in (None, "", "always"):
            raise ValueError()
        feature = FEATURES[step.feature_type]  # Builtins only, never plugin dispatch.
        feature.validate_override(step.config)
        with self.manager.session_factory() as db:
            settings = FeatureSettingsService(db)
            defaults, _, _ = settings.policy(feature)
            supplied = {**defaults, **step.config}
            if step.feature_type in {"flow.open", "action.navigate"}:
                if "retry_count" in step.config and (
                        type(step.config["retry_count"]) is not int or step.config["retry_count"] != 0):
                    raise ValueError()
                supplied["retry_count"] = 0
            feature.config_model.model_validate(supplied, strict=True)
            config = settings.validate(feature, supplied)
        base_url = self.target["base_url"]
        if "?" not in base_url and "#" not in base_url:
            base_url = SiteWrite(name=self.target["name"], base_url=base_url).base_url
        config = _substitute(config, base_url)
        if contains_redaction(config):
            raise ValueError()
        if step.feature_type in {"flow.open", "action.navigate"}:
            url = config["url"]
            config["url"] = resolve_url(self.target["base_url"], url)
            if project_url(config["url"])["url_redacted"]:
                raise ValueError()
            if _origin(config["url"]) != _origin(self.target["base_url"]):
                raise ValueError()
        for key, value in config.items():
            if isinstance(value, list):
                if len(value) > 30 or any(
                        not isinstance(item, str) or not item.strip() or len(item) > 2048
                        for item in value):
                    raise ValueError()
            elif isinstance(value, str) and (not value or len(value) > 4096):
                raise ValueError()
        if step.feature_type == "sign.unit3d_claim" and not config["claim_selector"]:
            raise ValueError()
        normalized = step.model_dump(mode="json")
        normalized.update(config=config, enabled=True, condition="always", on_error="stop")
        if len(_json(normalized).encode()) > 65536:
            raise ValueError()
        return normalized

    def _reserve(self, operation_id, step=None):
        # Persist before releasing credentials or issuing HTTP. Lost responses,
        # target changes and browser replacement must never create a replay window.
        with self.manager.session_factory() as db:
            # Serialize ledger readers across manager processes as well as tasks.
            changed = db.execute(update(AssistantSession).where(
                AssistantSession.id == self.session_id, AssistantSession.owner == self.owner,
                AssistantSession.status == "running",
            ).values(status="running"))
            if changed.rowcount != 1:
                raise _Denied("execution_authorization_changed")
            db.expire_all()
            row = self.manager._owned(db, self.owner, self.session_id)
            data = self.manager._decrypt(row)
            if row.status != "running" or data.get("target") != self.target:
                raise _Denied("execution_authorization_changed")
            operations = data.setdefault(_LEDGER, [])
            if operation_id in operations:
                raise _Denied("operation_already_used")
            if len(operations) >= 256:
                raise _Denied("execution_operation_limit")
            if step is not None and len(data.get(RECORDS, {})) >= 256:
                raise _Denied("execution_operation_limit")
            scope = self.manager.browsers.get(self.session_id)
            if step is not None:
                self._uncertainty_gate(db, data, scope, step["feature_type"])
            if self.retry_authorization:
                original = self.retry_authorization["operation_id"]
                if not valid_grant(data, scope, original, self.retry_authorization["id"]):
                    raise _Denied("retry_authorization_required")
                grant = data[GRANTS][original]
                if grant["status"] != "approved" or step != grant["step"]:
                    raise _Denied("retry_authorization_required")
                grant.update(status="used", retry_operation_id=operation_id)
            operations.append(operation_id)
            if step is not None:
                data.setdefault(RECORDS, {})[operation_id] = {
                    "step": deepcopy(step), "step_fingerprint": fingerprint(step),
                    "target_revision": data.get("_target_revision", 0),
                    "target_fingerprint": fingerprint(self.target),
                    "origin": list(_origin(self.target["base_url"])),
                    "credential_binding": credential_binding(self.target), "state": "reserved",
                    **({"retry_of": self.retry_authorization["operation_id"]}
                       if self.retry_authorization else {}),
                    **({"retry_authorization": deepcopy(grant)} if self.retry_authorization else {}),
                }
            self.manager._encrypt(row, data)
            db.commit()
            if step is not None:
                self.operation_id = operation_id
                self._reserved = True

    def _execution_state(self, state, status=None, code=None):
        # Serialize with every other encrypted payload writer. Do not require
        # running: cancellation/revocation must still leave durable uncertainty.
        with self.manager.session_factory() as db:
            changed = db.execute(update(AssistantSession).where(
                AssistantSession.id == self.session_id, AssistantSession.owner == self.owner,
            ).values(updated_at=AssistantSession.updated_at))
            if changed.rowcount != 1:
                raise _Denied("execution_authorization_changed")
            row = self.manager._owned(db, self.owner, self.session_id)
            data = self.manager._decrypt(row)
            record = data.get(RECORDS, {}).get(self.operation_id)
            if record is None:
                raise _Denied("execution_authorization_changed")
            record.update(state=state)
            if state == "dispatched":
                record["dispatched"] = True
            if status is not None:
                record.update(status=status, code=code)
                if status == "unknown":
                    facts = data.setdefault("_context_safety", [])
                    if not any(fact.get("operation_id") == self.operation_id for fact in facts):
                        facts.append({"operation_id": self.operation_id, "status": "unknown",
                                      "code": "outcome_uncertain", "message_id": None,
                                      "target_revision": record["target_revision"]})
                if record.get("retry_of") and record.get("dispatched"):
                    data[RECORDS][record["retry_of"]]["resolution"] = {
                        "retry_operation_id": self.operation_id, "status": status, "code": code,
                    }
            self.manager._encrypt(row, data)
            db.commit()

    def _credentials(self):
        # The thread owns its database session; no ORM instances cross threads.
        with self.manager.session_factory() as db:
            site = self._bound_site(db)
            mode = self.target.get("credential_mode")
            if mode == "manual" and site is not None:
                secret = CredentialService(db, self.manager.crypto).resolve(site.id)
            elif mode == "bitwarden":
                # Do not inherit custom/root-domain matching or provider IDs from
                # a saved workflow. Authority here is only the selected origin.
                lookup_site = Site(
                    name=self.target["name"], base_url=self.target["base_url"],
                    bitwarden_enabled=True, bitwarden_match_mode="domain",
                    bitwarden_custom_match=None,
                )
                secret = BitwardenService(
                    db, self.manager.settings, self.manager.crypto,
                ).resolve_for_site(lookup_site)
            else:
                raise _Denied("credentials_unavailable")
            values = {"username": secret.username, "password": secret.password}
            if any(not isinstance(value, str) or not value or len(value) > 4096
                   for value in values.values()):
                raise _Denied("credentials_unavailable")
            return values

    def _project(self, raw, scope, operation_id):
        if (not isinstance(raw, dict) or raw.get("session_id") != scope.session_id
                or raw.get("lane_key") != scope.lane_key or raw.get("operation_id") != operation_id
                or not isinstance(raw.get("status"), str) or raw["status"] not in _CODES
                or not isinstance(raw.get("code"), str) or raw["code"] not in _CODES[raw["status"]]):
            raise ValueError()
        snapshots = {}
        for key in ("before", "after"):
            source = raw.get(key)
            if not isinstance(source, dict):
                raise ValueError()
            if ("url" not in source and source.get("elements") == []
                    and raw["status"] in {"error", "unknown"}):
                snapshots[key] = {"elements": []}
                continue
            snapshot = safe_snapshot(source, self.target)
            if not snapshot.get("ok"):
                raise ValueError()
            snapshots[key] = {key: value for key, value in snapshot.items() if key != "ok"}
        source = raw.get("result")
        if not isinstance(source, dict):
            raise ValueError()
        result = {key: value for key, value in source.items()
                  if (key in {"submitted", "verified"} and type(value) is bool)
                  or (key == "count" and type(value) is int and 0 <= value <= 10000)}
        evidence = project_success_evidence(source.get("success_evidence"))
        if evidence is not None:
            result["success_evidence"] = evidence
        logs = list(_LOGS)
        if raw["status"] == "unknown":
            logs.append("Outcome uncertain. Retry detected; host approval will be requested "
                        "when the retry tool is used. Inspect the retained page first.")
        projected = {"ok": raw["status"] == "success", "operation_id": operation_id,
                     "status": raw["status"], "code": raw["code"], "logs": logs,
                     **snapshots, "result": result}
        source = raw.get("diagnostics")
        if isinstance(source, dict):
            diagnostics = {key: source[key] for key, codes in _DIAGNOSTIC_CODES.items()
                           if isinstance(source.get(key), str) and source[key] in codes}
            requests = source.get("blocked_requests")
            if isinstance(requests, list):
                blocked = []
                for request in requests[:5]:
                    if (not isinstance(request, dict)
                            or not isinstance(request.get("code"), str)
                            or request["code"] not in POLICY_CODES
                            or not isinstance(request.get("method"), str)
                            or request["method"] not in _HTTP_METHODS):
                        continue
                    try:
                        view = project_url(request.get("url"))
                    except ValueError:
                        view = {"url": "[redacted]", "url_redacted": True,
                                "redacted_parameters": []}
                    blocked.append({"code": request["code"], "method": request["method"], **view})
                if blocked:
                    diagnostics["blocked_requests"] = blocked
            if diagnostics:
                projected["diagnostics"] = diagnostics
        return projected

    async def execute(self, arguments: dict) -> dict:
        scope = None
        credentials = None
        dispatched = False
        outcome = None
        try:
            if (not isinstance(arguments, dict) or set(arguments) != {"step", "operation_id"}
                    or not isinstance(arguments["operation_id"], str)
                    or not isinstance(arguments["step"], dict)):
                return failure("invalid_arguments")
            try:
                operation_id = str(UUID(arguments["operation_id"]))
            except ValueError:
                return failure("invalid_arguments")
            self.operation_id = operation_id
            if not self.target:
                return failure("target_confirmation_required")
            feature = arguments["step"].get("feature_type")
            if (self.target.get("authorized") is True and isinstance(feature, str)
                    and feature.startswith("auth.") and not self.target.get("credential_consent")):
                return {**failure("credentials_unavailable"),
                        "notice": "Bind a same-origin saved site with credentials or select Bitwarden; do not provide secrets in chat."}
            if not isinstance(feature, str) or feature not in SUPPORTED_FEATURES:
                return failure("execution_feature_denied")
            scope = self.manager.browsers.get(self.session_id)
            self._authorize(scope, feature, check_uncertain=False)
            try:
                step = self._normalize(arguments["step"])
            except Exception:
                return failure("invalid_step")
            if self.turn and not self.retry_authorization and feature not in READ_ONLY:
                from app.services.assistant_retry import eligible_record, run_with_retry_approval
                from app.services.assistant import AssistantError

                with self.manager.session_factory() as db:
                    data = self.manager._decrypt(self.manager._owned(db, self.owner, self.session_id))
                    unresolved = uncertain_operations(self.manager, db, self.session_id, data)
                    matches = []
                    for original in sorted(unresolved):
                        try:
                            record = eligible_record(self.manager, db, self.owner, self.session_id, data, original)
                        except AssistantError:
                            continue
                        if (record["step_fingerprint"] == fingerprint(step)
                                and record.get("target_revision") == data.get("_target_revision", 0)
                                and record.get("target_fingerprint") == fingerprint(self.target)):
                            matches.append(original)
                if unresolved:
                    if len(matches) == 1:
                        return await run_with_retry_approval(
                            self.manager, self.owner, self.session_id, matches[0],
                            turn=self.turn, invocation=self.invocation)
                    return {**failure("retry_step_mismatch"), "operation_ids": sorted(unresolved),
                            "message": "Use retry_step with the original operation_id to request host approval; do not change the step."}
            self._authorize(scope, feature)
            self._reserve(operation_id, step)
            if feature.startswith("auth."):
                try:
                    async with asyncio.timeout(30):
                        credentials = await asyncio.to_thread(self._credentials)
                except _Denied:
                    raise
                except Exception:
                    return failure("credentials_unavailable")
            self._authorize(scope, feature)
            payload = {
                "token": scope.token, "operation_id": operation_id,
                "site_origin": self.target["base_url"], "step": step,
                "execution": {"feature_types": [feature],
                              "allow_sign": self.target.get("allow_sign") is True},
            }
            if credentials is not None:
                payload["credentials"] = dict(credentials)
            if self.retry_authorization:
                payload.update(retry_of=self.retry_authorization["operation_id"],
                               retry_authorization=self.retry_authorization["id"])
            try:
                self._execution_state("dispatched")
                dispatched = True
                raw = await self.manager._worker(f"{scope.session_id}/execute", payload)
            finally:
                payload.pop("credentials", None)
            self._authorize(scope, feature)
            if any(_has_secret(raw, value)
                   for value in [scope.token, *(credentials or {}).values()] if value):
                raise _Denied("secret_output")
            projected = self._project(raw, scope, operation_id)
            outcome = (projected["status"], projected["code"])
            if self.retry_authorization:
                projected["retry_of"] = self.retry_authorization["operation_id"]
            return projected
        except asyncio.CancelledError:
            if scope is not None and self.manager.browsers.get(self.session_id) is scope:
                await self.manager._close_browser(self.session_id)
            raise
        except _Denied as exc:
            if dispatched and self.manager.browsers.get(self.session_id) is scope:
                await self.manager._close_browser(self.session_id)
            return failure(str(exc))
        except Exception:
            if dispatched and self.manager.browsers.get(self.session_id) is scope:
                await self.manager._close_browser(self.session_id)
            return failure("execution_unavailable")
        finally:
            if credentials is not None:
                credentials.clear()
            if self._reserved:
                status, code = outcome or (
                    ("unknown", "outcome_uncertain") if dispatched
                    else ("failed", "execution_not_dispatched")
                )
                self._execution_state("terminal", status, code)
                self._reserved = False
