"""Consent-bound import, full execution and secret-free diagnostic feedback."""

from __future__ import annotations

import asyncio
import hashlib
import re
from types import SimpleNamespace
from urllib.parse import unquote
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from app.models.assistant import AssistantMessage, AssistantSession
from app.models.assistant_workflow import AssistantWorkflowAction
from app.models.execution import JobArtifact, JobStep, Run, SiteJob
from app.models.workflow import Site, SiteCredential
from app.schemas.config_ai import GenerationResult
from app.schemas.execution import RunCreate
from app.schemas.workflow import SiteWrite
from app.services.assistant_lanes import saved_lane
from app.services.assistant_urls import contains_redaction, origin, project_url, resolve_url
from app.services.config_ai_generation import _json, _strict_json
from app.services.login_evidence import project_success_evidence
from app.services.workflow_diagnostics import public_diagnostics
from app.services.workflows import WorkflowError, WorkflowService


class WorkflowDenied(ValueError):
    pass


class _WorkflowWriteService(WorkflowService):
    def _credential_view(self, site_id):
        # Write responses are internal; never decrypt account display metadata.
        from app.schemas.credential import SiteCredentialView

        return SiteCredentialView(configured=False)


def workflow_binding(site):
    return {"id": site.id, "history_key": site.history_key, "revision": site.config_revision}


def bound_workflow(db, target):
    binding = target.get("_workflow_binding")
    site = db.get(Site, target.get("workflow_site_id")) if target.get("workflow_site_id") else None
    from app.services.assistant_execution import _origin

    if (site is None or binding != workflow_binding(site)
            or _origin(site.base_url) != _origin(target["base_url"])):
        raise WorkflowDenied("workflow_binding_changed")
    return site


def _contains_protected(value, excluded=(), *, base_url):
    from app.services.assistant_data import _SENSITIVE

    if isinstance(value, dict):
        for key, child in value.items():
            if child in (None, "", False, [], {}):
                continue
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            metadata = key in {
                "username_selector", "password_selector", "token_selector",
                "username_field", "password_field", "credential_provider_id", "totp_provider_id",
            }
            if key in excluded or (_SENSITIVE.search(normalized) and not metadata):
                return True
            if _contains_protected(child, base_url=base_url):
                return True
    elif isinstance(value, list):
        return any(_contains_protected(child, base_url=base_url) for child in value)
    elif isinstance(value, str):
        if contains_redaction(value):
            return True
        # Public query/fragment bytes are not protected values. Only URL portions
        # actually identified as sensitive by the shared projection block edits.
        for match in re.finditer(r"https?://[^\s<>\"'`]+|^[/#?][^\s]*", value.replace("{base_url}", base_url)):
            try:
                if project_url(resolve_url(base_url, match.group()))["url_redacted"]:
                    return True
            except ValueError:
                return True
    return False


def validate_workflow_urls(resolved, target):
    """Validate authored/resolved URL inputs, not a runtime network sandbox."""
    def check(value, key=""):
        normalized = re.sub(r"[^a-z0-9]", "", unquote(key).lower())
        if normalized.endswith(("url", "urls")):
            values = value if isinstance(value, list) else [value]
            for item in values:
                if item in (None, ""):
                    continue
                if not isinstance(item, str) or contains_redaction(item):
                    raise ValueError
                url = resolve_url(target["base_url"], item.replace("{base_url}", target["base_url"]))
                if project_url(url)["url_redacted"] or origin(url) != origin(target["base_url"]):
                    raise ValueError
        elif isinstance(value, dict):
            for child_key, child in value.items():
                check(child, child_key)
        elif isinstance(value, list):
            for child in value:
                check(child)

    try:
        if contains_redaction(target["base_url"]) or project_url(target["base_url"])["url_redacted"]:
            raise ValueError
        steps = resolved["steps"] if isinstance(resolved, dict) else resolved.steps
        for step in steps:
            if isinstance(step, dict):
                enabled, config = step["enabled"], step["config"]
            else:
                enabled, config = step.enabled, step.config
            if enabled:
                check(config)
    except (ValueError, TypeError, KeyError):
        raise WorkflowDenied("workflow_url_not_authorized") from None


def workflow_candidate(service, target, draft):
    """Replace only local arrays and the host-approved lane, never protected settings."""
    owner_id = None
    if target.get("workflow_site_id") is not None:
        site = bound_workflow(service.session, target)
        owner_id = site.id
        local = service._local_snapshot(site)
        from app.services.config_ai_catalog import ConfigAICatalog

        catalog = ConfigAICatalog(service.session, include_plugins=True)
        for step in local["steps"]:
            # A model cannot reconstruct hidden local values. Require the manual
            # editor rather than silently dropping any of these server originals.
            schema = catalog.get_feature_schema(step["feature_type"])
            protected = set(schema["excluded_fields"]) | {"llm_prompt", "llm_enabled"}
            if _contains_protected(step["config"], protected, base_url=target["base_url"]):
                raise WorkflowDenied("protected_config_changed")
        local.update(expected_revision=site.config_revision, expected_history_key=site.history_key)
    else:
        bitwarden = target.get("credential_mode") == "bitwarden"
        local = {"name": target["name"], "base_url": target["base_url"],
                 "enabled": False, "support_resign": False, "checkin_after": None,
                 "bitwarden_enabled": bitwarden,
                 "credential_provider_id": "builtin.bitwarden" if bitwarden else "builtin.manual",
                 "bitwarden_match_mode": "domain"}
    local.update(draft)
    local["lane_policy"] = saved_lane(target)
    candidate = SiteWrite.model_validate(local)
    # SiteWrite trims trailing slashes from the whole string, including query and
    # fragment values. The host URL is already validated; preserve it verbatim.
    candidate = candidate.model_copy(update={"base_url": local["base_url"]})
    return candidate, owner_id


class AssistantWorkflows:
    def __init__(self, manager, owner, session_id, target, *, turn=None, auth_limit=None):
        self.manager, self.owner, self.session_id = manager, owner, session_id
        self.target = target
        self.turn, self.auth_limit = turn, auth_limit

    def _context(self, db, *, write=False, operation="run"):
        manager = self.manager
        if write:
            changed = db.execute(update(AssistantSession).where(
                AssistantSession.id == self.session_id, AssistantSession.owner == self.owner,
                AssistantSession.status == "running",
            ).values(status="running"))
            if changed.rowcount != 1:
                raise WorkflowDenied("workflow_authorization_changed")
            db.expire_all()
        row = manager._owned(db, self.owner, self.session_id)
        data = manager._decrypt(row)
        if write:
            from app.services.assistant_takeover import takeover_workflow_authority

            takeover_workflow_authority(self, db, data, operation=operation)
        target = data.get("target") or {}
        if target != self.target:
            raise WorkflowDenied("workflow_authorization_changed")
        if target.get("confirmed") is not True:
            raise WorkflowDenied("target_confirmation_required")
        if target.get("workflow_consent") is not True:
            raise WorkflowDenied("workflow_consent_required")
        if write and (manager._stopping or not manager.settings.config_ai_enabled):
            raise WorkflowDenied("workflow_execution_disabled")
        return row, data, target

    def _service(self, db):
        return _WorkflowWriteService(db, self.manager.settings, self.manager.crypto,
                                     actor=SimpleNamespace(username="assistant"))

    def _seal(self, action, value):
        encrypted = self.manager.crypto.encrypt(f"assistant-workflow:{action.id}", _json(value))
        action.ciphertext, action.nonce, action.key_version = (
            encrypted.ciphertext, encrypted.nonce, encrypted.key_version,
        )

    def _open(self, action):
        return _strict_json(self.manager.crypto.decrypt(
            f"assistant-workflow:{action.id}", action.ciphertext, action.nonce, action.key_version))

    def _action(self, db, kind, identity, proposal_id=None, *, reserve=True):
        fingerprint = hashlib.sha256(_json(identity).encode()).hexdigest()
        action = db.scalar(select(AssistantWorkflowAction).where(
            AssistantWorkflowAction.session_id == self.session_id,
            AssistantWorkflowAction.owner == self.owner, AssistantWorkflowAction.kind == kind,
            AssistantWorkflowAction.request_fingerprint == fingerprint,
        ))
        if action:
            return action, True
        if not reserve:
            return None, False
        action = AssistantWorkflowAction(
            id=str(uuid4()), session_id=self.session_id, owner=self.owner, kind=kind,
            request_fingerprint=fingerprint, proposal_message_id=proposal_id, state="reserved")
        self._seal(action, {})
        db.add(action)
        db.flush()
        return action, False

    @staticmethod
    def _identity(action, site):
        action.site_id, action.site_history_key, action.site_revision = (
            site.id, site.history_key, site.config_revision)

    def import_draft(self, proposal_message_id):
        from app.services.assistant_tools import allowed_features
        from app.services.config_ai_tools import ConfigAITools
        from app.schemas.config_ai import GenerationSite

        with self.manager.session_factory() as db:
            row, data, target = self._context(db, write=True, operation="import")
            message = db.scalar(select(AssistantMessage).where(
                AssistantMessage.id == proposal_message_id,
                AssistantMessage.session_id == self.session_id, AssistantMessage.role == "tool",
                AssistantMessage.status == "success",
            ))
            if message is None:
                raise WorkflowDenied("proposal_unavailable")
            envelope = self.manager._decrypt(message)
            if envelope.get("tool_name") != "submit_workflow_draft":
                raise WorkflowDenied("proposal_unavailable")
            result = _strict_json(envelope["content"])
            if result.get("ok") is not True or result.get("submitted") is not True:
                raise WorkflowDenied("proposal_unavailable")
            action, replay = self._action(db, "import", {
                "proposal": message.id, "revision": envelope.get("_target_revision")}, message.id)
            if replay:
                if action.state != "complete":
                    raise WorkflowDenied("workflow_outcome_uncertain")
                saved = self._open(action)
                if saved.get("target_revision") != data.get("_target_revision", 0):
                    raise WorkflowDenied("proposal_target_changed")
                return {**saved["result"], "replayed": True}
            if envelope.get("_target_revision") != data.get("_target_revision", 0):
                raise WorkflowDenied("proposal_target_changed")
            proposal = GenerationResult.model_validate(result.get("proposal"))
            if proposal.unresolved_questions:
                raise WorkflowDenied("proposal_questions_unresolved")
            if target.get("credential_mode") == "manual":
                from app.services.assistant_execution import AssistantExecution

                if target.get("site_id") is None:
                    raise WorkflowDenied("credential_site_required")
                if AssistantExecution(self.manager, self.owner, self.session_id, target)._bound_site(db) is None:
                    raise WorkflowDenied("credential_site_required")
            service = self._service(db)
            candidate, owner_id = workflow_candidate(service, target, proposal.draft.model_dump(mode="json"))
            preview = service.validate_draft("site", candidate, owner_id, replace_local=True)
            validate_workflow_urls(preview["resolved"], target)
            # Reapply the authoring policy to the EXACT encrypted submission, not
            # model import arguments, against today's feature schemas and defaults.
            from app.services.assistant_tools import _AssistantWorkflowService

            validation = ConfigAITools(_AssistantWorkflowService(
                db, self.manager.settings, self.manager.crypto, target=target),
                GenerationSite(name=target["name"], base_url=target["base_url"]),
                allowed_features(db), include_plugins=True)
            validation._consulted = {step.feature_type for step in proposal.draft.steps}
            checked = validation.dispatch("validate_workflow_draft", {
                "draft": proposal.draft.model_dump(mode="json")})
            if not checked.get("ok"):
                return checked
            from app.services.assistant_takeover import takeover_workflow_authority

            takeover_action = takeover_workflow_authority(self, db, data, operation="import")
            if owner_id is None:
                saved = service.create_site(candidate, _commit=False)
            else:
                saved = service.import_site_config(owner_id, candidate, _commit=False)
            site = db.get(Site, saved.id)
            target = {**target, "workflow_site_id": site.id, "_workflow_binding": workflow_binding(site)}
            if target.get("site_id") == site.id:
                target["_credential_binding"] = workflow_binding(site)
            data["target"] = target
            data["_target_revision"] = data.get("_target_revision", 0) + 1
            data["_workflow_proposal_id"] = message.id
            data.pop("_context_summary", None)
            from app.services.assistant_retry import cancel_approvals

            cancel_approvals(data)
            self.manager._encrypt(row, data)
            self._identity(action, site)
            action.state = "complete"
            credential = db.get(SiteCredential, site.id)
            manual_needed = target.get("credential_mode") == "manual" and not (
                credential and credential.password_ciphertext)
            output = {"ok": True, "site_id": site.id, "site_revision": site.config_revision,
                      "name": site.name, "operation": "created" if owner_id is None else "updated",
                      "history_key": site.history_key, "config_revision": site.config_revision,
                      "enabled": site.enabled,
                      "proposal_message_id": message.id, "lane_policy": site.lane_policy,
                      "lane": service._site_lane(site), "manual_credentials_needed": manual_needed,
                       "notice": ("Saved without copying credentials or browser cookies. "
                                  + ("New site is disabled for scheduled execution; review and enable it manually. "
                                     "\u672a\u542f\u7528\u5b9a\u65f6\u8c03\u5ea6\uff0c\u9700\u7528\u6237\u5ba1\u6838\u542f\u7528\u3002"
                                     if owner_id is None else "Existing enabled and scheduling settings were preserved."))}
            self._seal(action, {"result": output, "target_revision": data["_target_revision"]})
            from app.services.assistant_takeover import takeover_import_receipt

            # Validate before the candidate changes the source binding; consume in
            # the same transaction as the site write and its typed receipt.
            takeover_import_receipt(takeover_action, output)
            db.commit()
            self.target = target
            return output

    async def run_site(self, *, consumption_permit=None):
        from app.services.execution import ExecutionService
        from app.services.secret_providers import credential_provider
        from app.services.assistant_tools import allowed_features
        from app.services.assistant_run_guard import create_guard, unresolved_assistant_runs

        if self.manager.executor is None:
            raise WorkflowDenied("executor_unavailable")
        with self.manager.session_factory() as db:
            _, data, _ = self._context(db)
            from app.services.assistant_takeover import takeover_workflow_authority

            takeover_workflow_authority(self, db, data, operation="run")
        # Discovery holds a browser seat even when the saved run uses another lane.
        # Unlike best-effort cleanup, require confirmation before creating a run.
        scope = self.manager.browsers.get(self.session_id)
        if scope:
            async with asyncio.timeout(5):
                closed = await self.manager._worker(f"{scope.session_id}/close", {"token": scope.token})
            if closed.get("closed") is not True:
                raise WorkflowDenied("browser_close_failed")
            if self.manager.browsers.get(self.session_id) is not scope:
                raise WorkflowDenied("workflow_authorization_changed")
            self.manager.browsers.pop(self.session_id)
        with self.manager.session_factory() as db:
            row, data, target = self._context(db, write=True)
            site = bound_workflow(db, target)
            action, replay = self._action(db, "run", {
                "binding": workflow_binding(site), "proposal": data.get("_workflow_proposal_id")},
                data.get("_workflow_proposal_id"))
            if replay:
                if action.state == "unknown" and action.run_id is not None:
                    return {**self._open(action)["result"], "replayed": True}
                if action.state != "complete":
                    raise WorkflowDenied("workflow_outcome_uncertain")
                prior = db.get(Run, action.run_id)
                return {**self._open(action)["result"], "replayed": True,
                        "status": prior.status if prior else "unknown"}
            if db.scalar(select(SiteJob.id).where(
                    SiteJob.site_id == site.id, SiteJob.status.in_(("queued", "running")))):
                raise WorkflowDenied("site_run_active")
            from app.services.assistant_retry import uncertain_operations

            if uncertain_operations(self.manager, db, self.session_id, data):
                raise WorkflowDenied("retry_authorization_required")
            if (unresolved_assistant_runs(db, site_id=site.id)
                    or db.scalar(select(AssistantWorkflowAction.id).where(
                        AssistantWorkflowAction.kind == "run",
                        AssistantWorkflowAction.site_id == site.id,
                        AssistantWorkflowAction.site_history_key == site.history_key,
                        AssistantWorkflowAction.state == "unknown"))):
                raise WorkflowDenied("workflow_outcome_uncertain")
            service = self._service(db)
            resolved = service.resolve_site(site.id)
            validate_workflow_urls(resolved, target)
            from app.services.assistant_consumption import consume_permit

            consumption_plan = consume_permit(db, self, site, consumption_permit)
            if any(step.feature_type not in allowed_features(db) for step in resolved.steps):
                raise WorkflowDenied("workflow_feature_denied")
            from app.services.assistant_execution import AssistantExecution
            from app.services.assistant_categories import Denied

            helper = AssistantExecution(self.manager, self.owner, self.session_id, target)
            try:
                for step in resolved.steps:
                    if step.enabled:
                        helper._step_bindings(db, {"feature_type": step.feature_type, "config": step.config}, site=site)
            except Denied:
                raise WorkflowDenied("workflow_feature_denied") from None
            if service._site_lane(site) != ("general" if saved_lane(target) == "auto" else saved_lane(target)):
                raise WorkflowDenied("workflow_lane_mismatch")
            mode = target.get("credential_mode")
            if mode == "site":
                source = helper._bound_site(db)
                if source is None or source.id != site.id:
                    raise WorkflowDenied("credential_site_mismatch")
            expected = credential_provider(site) if mode == "site" else ("builtin.bitwarden" if mode == "bitwarden" else "builtin.manual")
            if credential_provider(site) != expected:
                raise WorkflowDenied("workflow_credential_policy_mismatch")
            from app.services.assistant_categories import (
                Denied, auth_budget_context, execution_category, require_auth_capacity,
            )

            auth = any(step.enabled and execution_category(db, step.feature_type) == "auth" for step in resolved.steps)
            auth_budget = None
            if auth:
                try:
                    turn_id, limit = auth_budget_context(
                        self.manager, db, self.session_id, data, turn=self.turn, auth_limit=self.auth_limit)
                    require_auth_capacity(data, turn_id, limit)
                except Denied as exc:
                    raise WorkflowDenied(str(exc)) from None
                auth_budget = {"session_id": self.session_id, "owner": self.owner,
                               "turn_id": turn_id, "limit": limit}
            credential = db.get(SiteCredential, site.id)
            if mode in {"cookie", "none"} and auth:
                raise WorkflowDenied("credential_mode_incompatible")
            if mode == "manual":
                from app.services.assistant_execution import AssistantExecution

                if not credential or not credential.password_ciphertext:
                    raise WorkflowDenied("manual_credentials_needed")
                source = AssistantExecution(self.manager, self.owner, self.session_id, target)._bound_site(db)
                if source is None or source.id != site.id:
                    raise WorkflowDenied("credential_site_mismatch")
            if mode == "cookie" and (target.get("workflow_site_id") != site.id
                    or not credential or not credential.cookies_ciphertext):
                raise WorkflowDenied("saved_cookies_required")
            run = ExecutionService(db, self.manager.settings, self.manager.crypto).create_run(
                RunCreate(site_ids=[site.id]), created_by="assistant", trigger="assistant", _commit=False,
                _allow_disabled=True)
            create_guard(db, run.id, site.id, service)
            self._identity(action, site)
            action.run_id, action.state = run.id, "complete"
            output = {"ok": True, "run_id": run.id, "site_id": site.id, "status": "queued",
                      "trigger": "assistant", "created_by": "assistant", "lane": resolved.lane}
            self._seal(action, {"result": output, "consumption_plan": consumption_plan,
                                "auth_budget": auth_budget})
            self.manager._encrypt(row, data)
            db.commit()
        try:
            self.manager.executor.submit(run.id)
        except Exception:
            # Persisted run identity is final. Never resubmit or create a second
            # run on a tool retry when dispatch acknowledgement was lost.
            output = {**output, "ok": False, "code": "run_submission_uncertain", "submitted": False}
            with self.manager.session_factory() as db:
                saved_action = db.get(AssistantWorkflowAction, action.id)
                saved_action.state = "unknown"
                self._seal(saved_action, {**self._open(saved_action), "result": output})
                db.commit()
        return output

    def feedback(self, run_id, *, cursor=0, limit=50, log=False, offset=0, byte_limit=16000,
                 snapshot_id=None):
        with self.manager.session_factory() as db:
            self.manager._owned(db, self.owner, self.session_id)
            action = db.scalar(select(AssistantWorkflowAction).where(
                AssistantWorkflowAction.session_id == self.session_id,
                AssistantWorkflowAction.owner == self.owner, AssistantWorkflowAction.kind == "run",
                AssistantWorkflowAction.run_id == run_id))
            from app.services.assistant_takeover import takeover_source_job

            source_job_id = takeover_source_job(self.manager, db, self.owner, self.session_id, run_id)
            run = db.get(Run, run_id) if action or source_job_id else None
            if run is None:
                raise WorkflowDenied("run_unavailable")
            jobs = list(db.scalars(select(SiteJob).where(SiteJob.run_id == run.id).order_by(SiteJob.id)))
            if source_job_id is not None:
                jobs = [job for job in jobs if job.id == source_job_id]
            steps = list(db.scalars(select(JobStep).where(
                JobStep.site_job_id.in_([job.id for job in jobs]),
            ).order_by(JobStep.id).limit(1001)))
            # Arbitrary runtime text, labels, URLs and data are not safe logs.
            # Only code-owned diagnostic enums and typed counters cross this boundary.
            from app.services.assistant_snapshot import POLICY_CODES

            codes = POLICY_CODES | {"step_failed", "execution_error", "execution_timeout",
                                    "assistant_configuration_changed",
                                    "assistant_outcome_uncertain", "outcome_uncertain",
                                    "assistant_guard_changed", "workflow_guard_changed",
                                    "model_unavailable", "vision_not_supported", "model_judgment_failed",
                                    "selector_not_found", "login_not_verified", "credential_unavailable",
                                    "input_validation_failed", "success_evidence_missing"}
            diagnostic_values = {
                "field": {"config", "url", "username_selector", "password_selector",
                          "submit_selector", "selector", "button_selector", "claim_selector",
                          "success_selector", "success_when_missing", "success_signals",
                          "code_selector", "input_selector", "image_selector", "iframe_selector",
                          "answer_selector", "question_selector", "refresh_selector",
                          "container_selector", "widget_selector", "checkbox_selector",
                          "token_selector", "handle_selector"},
                "stage": {"step_execution", "input_validation", "fill", "input", "request",
                          "provider_response", "tool_call", "tool_arguments", "content"},
                "cause": {"execution_timeout", "execution_error", "selector_not_found",
                          "input_validation_failed", "success_evidence_missing"},
            }
            statuses = {"queued", "running", "success", "fail", "failed", "error", "unknown",
                        "waiting_human", "canceled", "interrupted", "skipped"}
            items = []
            for index, step in enumerate(steps[:1000]):
                try:
                    data = _strict_json(step.data_json)
                except (ValueError, TypeError):
                    data = {}
                if not isinstance(data, dict):
                    data = {}
                safe = {key: value for key, value in data.items()
                        if ((key in {"submitted", "verified", "matched", "value", "outcome_uncertain"} and type(value) is bool)
                            or (key == "count" and type(value) is int and 0 <= value <= 1_000_000))}
                evidence = project_success_evidence(data.get("success_evidence"))
                if evidence is not None:
                    safe["success_evidence"] = evidence
                for key in ("code", "reason", "errorcode"):
                    if isinstance(data.get(key), str) and data[key] in codes:
                        safe[key] = data[key]
                for key, allowed in diagnostic_values.items():
                    if isinstance(data.get(key), str) and data[key] in allowed:
                        safe[key] = data[key]
                items.append({"cursor": index,
                              "feature_type": step.feature_type if step.feature_type in self._allowed() else "unsupported",
                              "step_key": step.step_key if re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", step.step_key) else "redacted",
                              "status": step.status if step.status in statuses else "unknown", "data": safe})
            output = {"ok": True, "run_id": run.id,
                      "status": run.status if run.status in statuses else "unknown",
                      "truncated": len(steps) > 1000}
            submission = self._open(action).get("result", {}) if action else {}
            if submission.get("submitted") is False:
                output.update(submitted=False, submission_code="run_submission_uncertain")
            elif action is not None and action.state == "unknown":
                output.update(submitted=False, submission_code="workflow_outcome_uncertain")
            output["jobs"] = []
            for job in jobs:
                try:
                    result = _strict_json(job.result_json)
                except (ValueError, TypeError):
                    result = {}
                reason = result.get("reason") if isinstance(result, dict) else None
                output["jobs"].append({"status": job.status if job.status in statuses else "unknown",
                                       **({"reason": reason} if isinstance(reason, str) and reason in codes else {})})
            # JobStep rows are upserted during execution. Pagination indexes are
            # valid only for this projection, not for monotonically increasing IDs.
            current_snapshot = hashlib.sha256(_json({**output, "steps": items}).encode()).hexdigest()
            if ((cursor > 0 or offset > 0) and snapshot_id is None
                    or snapshot_id is not None and snapshot_id != current_snapshot):
                return {"ok": False, "code": "run_snapshot_changed", "run_id": run.id,
                        "status": output["status"], "snapshot_id": current_snapshot,
                        "restart_cursor": 0, "restart_offset": 0}
            output["snapshot_id"] = current_snapshot
            if log:
                encoded = "\n".join(_json(item) for item in items).encode()[:65536]
                output.update(content=encoded[offset:offset + byte_limit].decode("utf-8", errors="replace"),
                              offset=offset, next_offset=offset + byte_limit if offset + byte_limit < len(encoded) else None,
                              truncated=len(steps) > 1000 or sum(len(_json(item)) + 1 for item in items) > 65536,
                              raw_log_available_in_ui=bool(db.scalar(select(JobArtifact.id).where(
                                  JobArtifact.run_id == run.id, JobArtifact.site_job_id.in_([job.id for job in jobs]),
                                  JobArtifact.kind == "log"))),
                              notice="Structured diagnostic log only. Raw artifact messages, URLs and values are withheld; inspect full logs privately in the run UI.")
            else:
                output.update(steps=items[cursor:cursor + limit], cursor=cursor,
                              next_cursor=cursor + limit if cursor + limit < len(items) else None)
            return output

    def _allowed(self):
        from app.services.assistant_tools import allowed_features

        with self.manager.session_factory() as db:
            return allowed_features(db)

    async def dispatch(self, name, arguments):
        try:
            if not isinstance(arguments, dict):
                raise WorkflowDenied("invalid_arguments")
            if name == "import_workflow_draft":
                if (set(arguments) != {"proposal_message_id"}
                        or type(arguments["proposal_message_id"]) is not int or arguments["proposal_message_id"] < 1):
                    raise WorkflowDenied("invalid_arguments")
                return self.import_draft(arguments["proposal_message_id"])
            if name == "run_site":
                if arguments:
                    raise WorkflowDenied("invalid_arguments")
                return await self.run_site()
            allowed = ({"run_id", "cursor", "limit", "wait_seconds", "snapshot_id"}
                       if name == "get_run_results" else {"run_id", "offset", "limit", "snapshot_id"})
            if (set(arguments) - allowed or type(arguments.get("run_id")) is not int
                    or arguments["run_id"] < 1):
                raise WorkflowDenied("invalid_arguments")
            bounds = {"cursor": (0, 2**63 - 1, 0), "offset": (0, 2**31 - 1, 0),
                      "limit": (1, 50 if name == "get_run_results" else 16000, 50 if name == "get_run_results" else 16000),
                      "wait_seconds": (0, 20, 0)}
            snapshot_id = arguments.get("snapshot_id")
            if "snapshot_id" in arguments and (not isinstance(snapshot_id, str)
                    or not re.fullmatch(r"[a-f0-9]{64}", snapshot_id)):
                raise WorkflowDenied("invalid_arguments")
            for key in allowed - {"run_id", "snapshot_id"}:
                low, high, default = bounds[key]
                if type(arguments.get(key, default)) is not int or not low <= arguments.get(key, default) <= high:
                    raise WorkflowDenied("invalid_arguments")
            if name == "get_run_log":
                return self.feedback(arguments["run_id"], log=True, offset=arguments.get("offset", 0),
                                     byte_limit=arguments.get("limit", 16000), snapshot_id=snapshot_id)
            for remaining in range(arguments.get("wait_seconds", 0), -1, -1):
                result = self.feedback(arguments["run_id"], cursor=arguments.get("cursor", 0),
                                       limit=arguments.get("limit", 50), snapshot_id=snapshot_id)
                if not result.get("ok") or result["status"] not in {"queued", "running"} or remaining == 0:
                    return result
                await asyncio.sleep(1)
        except WorkflowDenied as exc:
            return {"ok": False, "code": str(exc)}
        except WorkflowError as exc:
            return {"ok": False, "code": "workflow_invalid", "diagnostics": public_diagnostics(exc.diagnostics)}
        except (IntegrityError, OperationalError):
            return {"ok": False, "code": "workflow_conflict"}
        except (ValidationError, ValueError, KeyError, TypeError):
            return {"ok": False, "code": "workflow_invalid"}
        except Exception:
            return {"ok": False, "code": "workflow_unavailable"}
