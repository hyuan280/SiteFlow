"""Host-authorized, single-step execution on the assistant's existing browser.

``target`` is a private, server-loaded target, never model tool arguments. When
site_id is selected, SetTarget must freeze _credential_binding as
{id, history_key, revision}, with revision taken from Site.config_revision.
The manager must exclude that binding and the operation ledger from model/public
context. Takeover safety metadata is persisted alongside the encrypted ledger.
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from copy import deepcopy
import time
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from sqlalchemy import update

from app.features.base import IMAGE_CAPTCHA_FEATURES, VISION_FEATURES
from app.features.catalog import FEATURES
from app.models.assistant import AssistantSession
from app.models.workflow import Site
from app.schemas.assistant import public_url
from app.schemas.workflow import SiteWrite, StepInput
from app.services.assistant_categories import (
    Denied as _Denied, auth_budget_context, execution_category, execution_features,
    execution_binding, require_auth_capacity,
)
from app.services.assistant_snapshot import POLICY_CODES
from app.services.assistant_retry import (
    GRANTS, RECORDS, credential_binding, fingerprint,
    uncertain_operations, valid_grant,
)
from app.services.assistant_tools import failure, safe_snapshot
from app.services.assistant_urls import contains_redaction, project_url, resolve_url
from app.services.bitwarden import BitwardenService
from app.services.config_ai_generation import _has_secret, _json
from app.services.credentials import CredentialService
from app.services.feature_settings import FeatureSettingsService
from app.services.integrations import IntegrationService, validate_cfbox_url, validate_twofauth_url
from app.services.login_evidence import project_success_evidence
from app.services.workflows import _substitute


SUPPORTED_FEATURES = frozenset(FEATURES)
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


def _origin(url):
    return urlsplit(public_url(url))[:2]


class AssistantExecution:
    def __init__(self, manager, owner: str, session_id: str, target: dict | None, *,
                 retry_authorization: dict | None = None, turn=None, invocation=None, auth_limit=None):
        self.manager = manager
        self.owner = owner
        self.session_id = session_id
        self.target = deepcopy(target)
        self.retry_authorization = deepcopy(retry_authorization)
        self.turn = turn
        self.invocation = invocation
        self.auth_limit = auth_limit
        self.operation_id = None
        self._execution_turn_id = None
        self._reserved = False
        self._bindings = None
        self._job_id = None

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
        if execution_category(db, feature) != "post":
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
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
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
            if self._job_id is not None:
                from app.models.execution import Run, SiteJob

                job = db.get(SiteJob, self._job_id, populate_existing=True)
                run = db.get(Run, job.run_id, populate_existing=True) if job else None
                if (job is None or job.status != "running" or run is None
                        or run.status != "running" or run.cancel_requested):
                    raise _Denied("execution_authorization_changed")
            if self._reserved:
                record = data.get(RECORDS, {}).get(self.operation_id)
                if (record is None or record.get("feature_bindings") != self._bindings
                        or record.get("state") == "terminal"):
                    raise _Denied("execution_authorization_changed")
                turn_id, _ = auth_budget_context(
                    manager, db, self.session_id, data, turn=self.turn, auth_limit=self.auth_limit)
                if turn_id != self._execution_turn_id:
                    raise _Denied("execution_authorization_changed")
            from app.services.assistant_takeover import takeover_live_allowed

            if self.turn and self.turn.get("_takeover_action_id"):
                active = manager._active_turns.get(self.session_id) or {}
                if any(self.turn.get(key) != active.get(key) for key in (
                        "_takeover_action_id", "_takeover_request_id", "_request_id")):
                    raise _Denied("takeover_tool_denied")
            if (not takeover_live_allowed(manager, db, self.owner, self.session_id, data,
                                          execution=True, feature=feature,
                                          operation_id=self.operation_id if self._reserved else None)
                    or (self.retry_authorization and ("_takeover" in data or
                        (manager._active_turns.get(self.session_id) or {}).get("_takeover_action_id")))):
                raise _Denied("takeover_tool_denied")
            if self.target.get("confirmed") is not True:
                raise _Denied("target_confirmation_required")
            if self.target.get("execution_consent") is not True:
                raise _Denied("execution_consent_required")
            allowed = self.target.get("execution_features", [])
            if (not isinstance(allowed, list) or feature not in allowed or any(
                    not isinstance(key, str) or (key not in FEATURES and (
                        len(key.split(".", 2)) != 3 or not key.startswith("plugin.")))
                    for key in allowed)):
                raise _Denied("execution_feature_denied")
            category = execution_category(db, feature)
            if category == "auth" and self.target.get("credential_mode") in {"cookie", "none"}:
                raise _Denied("credentials_unavailable")
            if self._bindings is not None and self._step_bindings(db, self._normalized_step) != self._bindings:
                raise _Denied("execution_authorization_changed")
            if ((category in {"sign", "challenge", "post"} or feature in {"action.click", "action.fill"})
                    and self.target.get("allow_sign") is not True):
                raise _Denied("sign_consent_required")
            if ((category == "auth" or feature == "challenge.2fa")
                    and self.target.get("credential_consent") is not True):
                raise _Denied("credential_consent_required")
            _origin(self.target["base_url"])
            self._bound_site(db)
            if self.retry_authorization and not valid_grant(
                    data, scope, self.retry_authorization["operation_id"],
                    self.retry_authorization["id"], self.operation_id if self._reserved else None):
                raise _Denied("retry_authorization_required")
            if check_uncertain:
                if self.operation_id in data.get(_LEDGER, []) and not self._reserved:
                    raise _Denied("operation_already_used")
                self._uncertainty_gate(db, data, scope, feature)

    def _normalize(self, raw):
        step = StepInput.model_validate(raw, strict=True)
        if step.enabled is False or step.condition not in (None, "", "always"):
            raise ValueError()
        protected = {"llm_prompt", "llm_enabled", "fields"}
        if set(step.config) & protected:
            raise ValueError()
        with self.manager.session_factory() as db:
            feature = execution_features(db)[step.feature_type]
            remote = hasattr(feature, "plugin_id")
            if remote and any(key in step.config for key, prop in feature.schema.get("properties", {}).items()
                              if prop.get("writeOnly") or prop.get("readOnly") or prop.get("ui:hidden")
                              or prop.get("format") == "password"):
                raise ValueError()
            feature.validate_override(step.config)
            settings = FeatureSettingsService(db)
            defaults, _, _ = settings.policy(feature)
            supplied = {key: value for key, value in {**defaults, **step.config}.items()
                        if key in (feature.schema.get("properties", {}) if remote else
                                   feature.config_model.model_fields) and key not in protected}
            if step.feature_type in {"flow.open", "action.navigate"}:
                if "retry_count" in step.config and (
                        type(step.config["retry_count"]) is not int or step.config["retry_count"] != 0):
                    raise ValueError()
                supplied["retry_count"] = 0
            if remote:
                feature.validate_full(supplied)
            else:
                feature.config_model.model_validate(supplied, strict=True)
            config = settings.validate(feature, supplied)
            if remote and set(config) & protected:
                raise ValueError()
            self._normalized_step = {"feature_type": step.feature_type, "config": config}
            self._bindings = self._step_bindings(db, self._normalized_step)
        config = {key: value for key, value in config.items() if key not in protected}
        base_url = self.target["base_url"]
        if "?" not in base_url and "#" not in base_url:
            base_url = SiteWrite(name=self.target["name"], base_url=base_url).base_url
        config = _substitute(config, base_url)
        from app.services.assistant_workflows import validate_workflow_urls

        validate_workflow_urls({"steps": [{"enabled": True, "config": config}]}, self.target)
        if contains_redaction(config):
            raise ValueError()
        if step.feature_type in {"flow.open", "action.navigate"}:
            url = config["url"]
            config["url"] = resolve_url(self.target["base_url"], url)
            if project_url(config["url"])["url_redacted"]:
                raise ValueError()
            if _origin(config["url"]) != _origin(self.target["base_url"]):
                raise ValueError()
        for key, value in (() if remote or step.feature_type.startswith("post.") else config.items()):
            if isinstance(value, list):
                if len(value) > 30 or any(
                        not isinstance(item, str) or not item.strip() or len(item) > 2048
                        for item in value):
                    raise ValueError()
            elif isinstance(value, str) and (len(value) > 4096 or (not value and key not in {
                    "username_hint", "value", "message", "message_text", "target_step_key",
            })):
                raise ValueError()
        if step.feature_type == "sign.unit3d_claim" and not config["claim_selector"]:
            raise ValueError()
        normalized = step.model_dump(mode="json")
        normalized.update(config=config, enabled=True, condition="always", on_error="stop")
        if len(_json(normalized).encode()) > 65536:
            raise ValueError()
        return normalized

    def _step_bindings(self, db, step, *, site=None):
        bindings = {step["feature_type"]: execution_binding(db, step["feature_type"])}
        from app.services.assistant_provider_policy import provider_binding
        from app.services.secret_providers import credential_provider

        if site is None and self is not None and self.target.get("credential_mode") == "site":
            site = self._bound_site(db)
            if site is None:
                raise _Denied("credentials_unavailable")
        if site is not None:
            provider = credential_provider(site)
            if provider.startswith("plugin."):
                bindings[f"{provider}.secret.credentials"] = provider_binding(db, provider, "secret.credentials")
        if step["feature_type"] == "challenge.2fa":
            provider = step["config"].get("totp_provider_id") or "builtin.twofauth"
            if provider.startswith("plugin."):
                bindings[f"{provider}.secret.totp"] = provider_binding(db, provider, "secret.totp")
            elif provider != "builtin.twofauth":
                raise _Denied("credentials_unavailable")
        method = step["config"].get("captcha_method")
        if isinstance(method, str) and method.startswith("plugin."):
            from app.features.plugin_registry import plugin_captcha_methods, plugin_metadata_for_row
            from app.models.plugin import PluginRegistration
            from app.services.assistant_feature_policy import assistant_allowed, policy_binding

            candidate = next((m for m in plugin_captcha_methods() if m.key == method), None)
            row = db.get(PluginRegistration, candidate.plugin_id, populate_existing=True) if candidate else None
            if row is None or candidate not in plugin_metadata_for_row(row)[1] or not assistant_allowed(db, method):
                raise _Denied("execution_feature_denied")
            bindings[method] = {"policy": policy_binding(db, method), "plugin": {
                "generation": row.generation, "permission_epoch": row.permission_epoch}}
        return bindings

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
                self._authorize(scope, step["feature_type"], db=db)
                category = execution_category(db, step["feature_type"])
                turn_id, auth_limit = auth_budget_context(
                    self.manager, db, self.session_id, data, turn=self.turn, auth_limit=self.auth_limit)
                if category == "auth":
                    require_auth_capacity(data, turn_id, auth_limit)
                    data["_execution_turn_id"] = turn_id
                self._uncertainty_gate(db, data, scope, step["feature_type"])
            if self.retry_authorization:
                original = self.retry_authorization["operation_id"]
                if not valid_grant(data, scope, original, self.retry_authorization["id"]):
                    raise _Denied("retry_authorization_required")
                grant = data[GRANTS][original]
                if grant["status"] != "approved" or step != grant["step"]:
                    raise _Denied("retry_authorization_required")
                if grant.get("automatic") and (
                        grant.get("turn_id") != turn_id or grant.get("invocation") != self.invocation):
                    raise _Denied("retry_authorization_required")
                grant.update(status="used", retry_operation_id=operation_id)
            operations.append(operation_id)
            if step is not None:
                data.setdefault(RECORDS, {})[operation_id] = {
                    "category": category, "turn_id": turn_id,
                    "feature_bindings": deepcopy(self._bindings),
                    "step": deepcopy(step), "step_fingerprint": fingerprint(step),
                    "target_revision": data.get("_target_revision", 0),
                    "target_fingerprint": fingerprint(self.target),
                    "origin": list(_origin(self.target["base_url"])),
                    "credential_binding": credential_binding(self.target), "state": "reserved",
                    **({"takeover_action_id": data["_takeover"]["action_id"]}
                       if "_takeover" in data else {}),
                    **({"retry_of": self.retry_authorization["operation_id"]}
                       if self.retry_authorization else {}),
                    **({"retry_authorization": deepcopy(grant)} if self.retry_authorization else {}),
                }
            from app.services.assistant_takeover import record_takeover_step

            if step is not None:
                record_takeover_step(db, row, operation_id, data[RECORDS])
            self.manager._encrypt(row, data)
            db.commit()
            if step is not None:
                self.operation_id = operation_id
                self._execution_turn_id = turn_id
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
            from app.services.assistant_takeover import record_takeover_step

            record_takeover_step(db, row, self.operation_id, data[RECORDS])
            self.manager._encrypt(row, data)
            db.commit()

    def _credentials(self):
        # The thread owns its database session; no ORM instances cross threads.
        with self.manager.session_factory() as db:
            site = self._bound_site(db)
            mode = self.target.get("credential_mode")
            if mode == "site":
                from app.services.secret_providers import credential_provider

                if site is None:
                    raise _Denied("credentials_unavailable")
                provider = credential_provider(site)
                if provider.startswith("plugin."):
                    return self._provider_secret(site, provider, "auth.form_login", {
                        "bitwarden_match_mode": site.bitwarden_match_mode,
                        "bitwarden_custom_match": site.bitwarden_custom_match,
                    })
                if provider == "builtin.bitwarden":
                    secret = BitwardenService(db, self.manager.settings, self.manager.crypto).resolve_for_site(site)
                elif provider == "builtin.manual":
                    secret = CredentialService(db, self.manager.crypto).resolve(site.id)
                else:
                    raise _Denied("credentials_unavailable")
            elif mode == "manual" and site is not None:
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

    def _provider_secret(self, site, provider, purpose, options):
        from app.services.secret_providers import SecretProviders

        def authorize():
            self._authorize(self.manager.browsers.get(self.session_id), self._normalized_step["feature_type"])

        try:
            # Zero denotes an assistant operation, not a durable workflow job.
            # The callback is pinned to this operation's reserved ledger/bindings.
            return SecretProviders(self.manager.session_factory, self.manager.settings, self.manager.crypto)._remote(
                provider, purpose, 0, site.id, f"assistant-operation:{self.operation_id}",
                site.base_url, options, authorize=authorize)
        except _Denied:
            raise
        except Exception:
            raise _Denied("credentials_unavailable") from None

    def _private_runtime(self, step):
        runtime, secrets = {}, []
        feature, config = step["feature_type"], step["config"]
        with self.manager.session_factory() as db:
            site = self._bound_site(db)
            integrations = IntegrationService(db, self.manager.settings, self.manager.crypto)
            settings = FeatureSettingsService(db)
            if feature == "challenge.2fa":
                provider = config.get("totp_provider_id") or "builtin.twofauth"
                if provider.startswith("plugin."):
                    if site is None or self.target.get("credential_mode") not in {"site", "manual", "bitwarden"}:
                        raise _Denied("credentials_unavailable")
                    value = self._provider_secret(site, provider, feature, config)
                    code = value["code"]
                    return {"totp_code": code}, [code]
                # Establish a real same-origin account source, never a fabricated job.
                credentials = self._credentials()
                secrets.extend(credentials.values())
                credentials.clear()
                url = integrations.resolve_twofauth_url().value
                token = integrations.resolve_twofauth_token().value
                if not url or not token:
                    raise _Denied("credentials_unavailable")
                secrets.extend([url, token])
                base = validate_twofauth_url(url)
                with httpx.Client(timeout=20, follow_redirects=False) as client:
                    headers = {"Authorization": f"Bearer {token}"}
                    response = client.get(f"{base}/api/v1/twofaccounts", headers=headers)
                    response.raise_for_status()
                    body = response.json()
                    accounts = body if isinstance(body, list) else body.get("data", [])
                    if not isinstance(accounts, list) or any(not isinstance(a, dict) for a in accounts):
                        raise _Denied("credentials_unavailable")
                    username = config.get("username_hint", "").strip().casefold()
                    if username:
                        accounts = [a for a in accounts if str(a.get("account") or "").strip().casefold() == username]
                    hint = config["service_hint"].strip().casefold()
                    exact = [a for a in accounts if str(a.get("service") or "").strip().casefold() == hint]
                    matches = exact if len(exact) == 1 else [a for a in accounts if
                        hint in str(a.get("service") or "").casefold()
                        or hint in str(a.get("account") or "").casefold()]
                    if len(matches) != 1:
                        raise _Denied("credentials_unavailable")
                    identifier = int(matches[0]["id"])
                    response = client.get(f"{base}/api/v1/twofaccounts/{identifier}/otp", headers=headers)
                    response.raise_for_status()
                    body = response.json()
                    code = body.get("password") or body.get("otp")
                    if not isinstance(code, str) or not code.isascii() or not code.isdigit() or not 4 <= len(code) <= 10:
                        raise _Denied("credentials_unavailable")
                    runtime["totp_code"] = code
                    secrets.append(code)
            if feature == "challenge.turnstile":
                url = integrations.resolve_cfbox_url().value
                if url:
                    runtime["cfbox"] = {"url": validate_cfbox_url(url),
                                        "api_key": integrations.resolve_cfbox_api_key().value or ""}
                    secrets.extend(runtime["cfbox"].values())
            needs_vision = feature in VISION_FEATURES and (
                feature not in IMAGE_CAPTCHA_FEATURES or config.get("captcha_method") == "vision")
            behavior = settings.execution_settings(FEATURES[feature])
            if feature == "flow.result":
                defaults, _, _ = settings.policy(FEATURES[feature])
                needs_vision = (behavior.llm_enabled and defaults.get("llm_enabled") is True
                                and (config["output_value"] or behavior.judgment_scope == "all"))
                if needs_vision and defaults.get("llm_prompt"):
                    behavior = behavior.model_copy(update={"llm_prompt": defaults["llm_prompt"]})
            if needs_vision:
                try:
                    vision = integrations.model_runtime(
                        "feature_cards", requires_vision=feature != "flow.result" or behavior.use_vision,
                    )
                except Exception:
                    raise _Denied("provider_unavailable") from None
                if not vision:
                    raise _Denied("provider_unavailable")
                vision = {key: vision[key] for key in (
                    "base_url", "api_key", "model", "timeout", "max_tokens", "supports_vision",
                ) if key in vision}
                secrets.extend(vision[key] for key in ("base_url", "api_key", "model"))
                if behavior is not None:
                    vision["execution_settings"] = behavior.model_dump(mode="json")
                    secrets.extend(value for key, value in vision["execution_settings"].items()
                                   if key in {"llm_prompt", "model"} and isinstance(value, str))
                runtime["vision"] = vision
        return runtime, secrets

    def _plugin_runtimes(self, step):
        from app.services.plugin_runtime import PluginRuntimeService

        keys = [step["feature_type"], step["config"].get("captcha_method", "")]
        ids = {key.split(".", 2)[1] for key in keys if key.startswith("plugin.")}
        with self.manager.session_factory() as db:
            self._authorize(self.manager.browsers.get(self.session_id), step["feature_type"], db=db)
            return {key: PluginRuntimeService(db, self.manager.settings).runtime_for_plugin(key)
                    for key in ids}

    def _post_runtime(self, step):
        from datetime import datetime
        from sqlalchemy import select
        from app.models.execution import Run, SiteJob
        from app.models.workflow import SiteResignRecord
        from app.services.assistant_feature_policy import assistant_explicitly_allowed
        from app.services.assistant_workflows import bound_workflow
        from app.services.resign import ResignService

        with self.manager.session_factory() as db:
            self._authorize(self.manager.browsers.get(self.session_id), step["feature_type"], db=db)
            if not assistant_explicitly_allowed(db, step["feature_type"]):
                raise _Denied("consumption_approval_required")
            site = bound_workflow(db, self.target)
            resign = ResignService(db, self.manager.settings)
            dates = resign.pending_dates(site.id, site.resign_max_days) if site.support_resign else []
            unresolved = list(db.scalars(select(SiteResignRecord.resign_date).where(
                SiteResignRecord.site_id == site.id, SiteResignRecord.status == "unknown"
            ).order_by(SiteResignRecord.resign_date)))
            if unresolved or db.scalar(select(SiteJob.id).where(
                    SiteJob.site_id == site.id, SiteJob.status.in_(("queued", "running")))):
                raise _Denied("workflow_outcome_uncertain")
            run = Run(trigger="assistant_operation", status="running", created_by=self.owner,
                      requested_sites_json=_json([site.id]))
            db.add(run)
            db.flush()
            job = SiteJob(run_id=run.id, site_id=site.id, site_name=site.name,
                          lane="proxy_interaction" if self.target.get("proxy") else "interaction",
                          status="running")
            db.add(job)
            db.flush()
            self._job_id = job.id
            runtime = {"job_id": str(job.id), "site_id": str(site.id),
                       "resign_pending_dates": dates, "resign_unresolved_dates": unresolved,
                       "resign_today": datetime.now(resign.timezone).date().isoformat()}
            db.commit()
            return runtime

    def _finish_post_job(self, status):
        from datetime import datetime, timezone
        from app.models.execution import Run, SiteJob

        with self.manager.session_factory() as db:
            job = db.get(SiteJob, self._job_id)
            run = db.get(Run, job.run_id)
            job.status = run.status = {"failed": "fail", "error": "unknown"}.get(status, status)
            job.finished_at = run.finished_at = datetime.now(timezone.utc)
            db.commit()

    def _project(self, raw, scope, operation_id, feature=None):
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
            if source == {"elements": []}:
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
        if feature == "flow.result" and type(source.get("value")) is bool:
            result["value"] = source["value"]
        evidence = project_success_evidence(source.get("success_evidence"))
        if evidence is not None:
            result["success_evidence"] = evidence
        logs = list(_LOGS)
        if raw["status"] == "unknown":
            logs.append("Outcome uncertain. Inspect the retained page first. Non-post retries "
                        "use the category policy; auth attempts share the per-turn budget.")
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
        private_runtime = None
        private_secrets = []
        dispatched = False
        outcome = None
        plugin_token = None
        plugin_runtimes = {}
        job_runtime = None
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
            with self.manager.session_factory() as db:
                category = execution_category(db, feature)
            if (self.target.get("authorized") is True and isinstance(feature, str)
                    and (category == "auth" or feature == "challenge.2fa")
                    and not self.target.get("credential_consent")):
                return {**failure("credentials_unavailable"),
                        "notice": "Bind a same-origin saved site with credentials or select Bitwarden; do not provide secrets in chat."}
            scope = self.manager.browsers.get(self.session_id)
            self._authorize(scope, feature, check_uncertain=False)
            try:
                step = self._normalize(arguments["step"])
            except _Denied:
                raise
            except Exception:
                return failure("invalid_step")
            self._authorize(scope, feature)
            self._reserve(operation_id, step)
            if category == "post":
                job_runtime = self._post_runtime(step)
            plugin_runtimes = self._plugin_runtimes(step)
            if plugin_runtimes or job_runtime:
                from app.services.assistant_plugin_authorization import assistant_plugin_authorization

                plugin_token = assistant_plugin_authorization.register(self, scope, step, plugin_runtimes)
                for runtime in plugin_runtimes.values():
                    private_secrets.extend(runtime[key] for key in ("token", "endpoint") if runtime.get(key))
                    runtime["authorization"] = {
                        "scope": "assistant_operation", "session_id": self.session_id,
                        "operation_id": operation_id, "token": plugin_token,
                    }
                private_secrets.append(plugin_token)
                if job_runtime:
                    job_runtime["secret_access"] = {"token": plugin_token}
            if category == "auth" and feature in FEATURES:
                try:
                    async with asyncio.timeout(30):
                        credentials = await asyncio.to_thread(self._credentials)
                except _Denied:
                    raise
                except Exception:
                    return failure("credentials_unavailable")
            if feature in FEATURES and (category == "challenge" or feature in VISION_FEATURES or feature == "flow.result"):
                try:
                    async with asyncio.timeout(30):
                        private_runtime, runtime_secrets = await asyncio.to_thread(self._private_runtime, step)
                        private_secrets.extend(runtime_secrets)
                except _Denied:
                    raise
                except Exception:
                    return failure("credentials_unavailable" if feature == "challenge.2fa" else "provider_unavailable")
            self._authorize(scope, feature)
            payload = {
                "token": scope.token, "operation_id": operation_id,
                "site_origin": self.target["base_url"], "step": step,
                "plugin_runtimes": plugin_runtimes,
                "categories": {feature: category},
                "feature_schemas": {feature: (self._bindings[feature]["plugin"]["schema"]
                                    if feature.startswith("plugin.") else
                                    FEATURES[feature].config_model.model_json_schema())},
                "execution": {"feature_types": [feature],
                              "allow_sign": self.target.get("allow_sign") is True},
            }
            if credentials is not None:
                payload["credentials"] = dict(credentials)
            if job_runtime:
                payload["job_runtime"] = job_runtime
            if private_runtime:
                payload["private_runtime"] = private_runtime
            if feature == "flow.result" and private_runtime and private_runtime.get("vision"):
                payload["step"] = deepcopy(step)
                payload["step"]["config"]["llm_enabled"] = True
            if self.retry_authorization:
                payload.update(retry_of=self.retry_authorization["operation_id"],
                               retry_authorization=self.retry_authorization["id"])
            try:
                self._execution_state("dispatched")
                dispatched = True
                raw = await self.manager._worker(f"{scope.session_id}/execute", payload)
            finally:
                payload.pop("credentials", None)
                payload.pop("private_runtime", None)
                payload.pop("plugin_runtimes", None)
                payload.pop("job_runtime", None)
            self._authorize(scope, feature)
            if any(_has_secret(raw, value)
                   for value in [scope.token, *(credentials or {}).values(), *private_secrets] if value):
                raise _Denied("secret_output")
            projected = self._project(raw, scope, operation_id, feature)
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
            if plugin_token:
                assistant_plugin_authorization.revoke(plugin_token)
            plugin_runtimes.clear()
            if credentials is not None:
                credentials.clear()
            if private_runtime is not None:
                private_runtime.clear()
            private_secrets.clear()
            if self._reserved:
                status, code = outcome or (
                    ("unknown", "outcome_uncertain") if dispatched
                    else ("failed", "execution_not_dispatched")
                )
                self._execution_state("terminal", status, code)
                if self._job_id is not None:
                    self._finish_post_job(status)
                self._reserved = False
