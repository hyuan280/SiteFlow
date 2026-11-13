from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.main import create_app
from app.models.assistant import AssistantSession
from app.models.assistant_takeover import AssistantJobConfigSnapshot, AssistantTakeoverAction
from app.models.execution import JobArtifact, JobStep, Run, SiteJob
from app.models.setting import AppSetting
from app.models.workflow import Site
from app.schemas.assistant import CreateSession
from app.schemas.assistant_takeover import TakeoverSettingsUpdate, TakeoverValues
from app.schemas.execution import RunCreate
from app.schemas.workflow import SiteWrite, TemplateWrite
from app.services.assistant_settings import AssistantSettingsError
from app.services.assistant_takeover import (
    activate_takeover, capture_job_config, takeover_runtime, takeover_source_job,
    takeover_tool_allowed, takeover_tool_definitions,
)
from app.services.assistant_takeover_settings import KEY, TakeoverSettingsService
from app.services.assistant_tools import AssistantTools
from app.services.assistant_workflows import AssistantWorkflows
from app.services.config_ai_generation import _json
from app.services.execution import ExecutionService
from app.services.workflows import WorkflowService
from tests.helpers import make_settings
from tests.test_assistant_workflows import DRAFT, submit
from tests.test_assistant_api import CONFIG, assistant_client, provider, text  # noqa: F401


OWNER = "takeover-owner"


@pytest.fixture
def ctx(tmp_path):
    app = create_app(make_settings(tmp_path, config_ai_enabled=True))
    manager = app.state.assistant_manager
    with manager.session_factory() as db:
        template = WorkflowService(db, manager.settings, manager.crypto).create_template(
            TemplateWrite(name="Template"))
        site = WorkflowService(db, manager.settings, manager.crypto).create_site(SiteWrite(
            name="Example", base_url="https://example.com/", lane_policy="proxy_general",
            template_id=template.id, checkin_after="14:30", trusted_urls=["^https://example.com/"],
            **DRAFT))
    return app.state.assistant_takeover, manager, site.id


def grant(ctx, **values):
    _, manager, site_id = ctx
    values.setdefault("live_access", False)
    with manager.session_factory() as db:
        service = TakeoverSettingsService(db)
        current = service.view()
        return service.update(TakeoverSettingsUpdate(
            expected_revision=current.revision, confirm=True,
            values=TakeoverValues(enabled=True, site_ids=[site_id], **values)), OWNER)


def failed_job(ctx, *, status="fail", run_status="fail", trigger="manual", historical=False,
               uncertain=False, snapshot=True, cancel=False, other_status=None):
    _, manager, site_id = ctx
    with manager.session_factory() as db:
        run = Run(created_by=OWNER, trigger=trigger, status=run_status, cancel_requested=cancel,
                  created_at=datetime.now(timezone.utc) - (timedelta(days=1) if historical else timedelta()),
                  finished_at=datetime.now(timezone.utc))
        db.add(run)
        db.flush()
        job = SiteJob(run_id=run.id, site_id=site_id, site_name="Example", lane="proxy_general",
                      status=status, attempt=1, result_json=_json({"outcome_uncertain": uncertain}),
                      finished_at=datetime.now(timezone.utc))
        db.add(job)
        db.flush()
        if other_status:
            db.add(SiteJob(run_id=run.id, site_id=site_id, site_name="Other", lane="general",
                           status=other_status, attempt=1))
        if snapshot:
            service = WorkflowService(db, manager.settings, manager.crypto)
            site = db.get(Site, site_id)
            resolved = service.resolve_site(site_id)
            capture_job_config(db, manager.settings, manager.crypto, site, job, {
                "steps": [step.model_dump(mode="json") for step in resolved.steps],
                "signals": [signal.model_dump(mode="json") for signal in resolved.signals],
                "base_url": site.base_url, "lane_key": job.lane,
            })
        db.commit()
        return job.id, run.id


def uncertain_job(ctx, trigger, signal):
    _, manager, site_id = ctx
    with manager.session_factory() as db:
        run = Run(created_by=OWNER, trigger=trigger, status="fail", finished_at=datetime.now(timezone.utc))
        db.add(run)
        db.flush()
        job = SiteJob(run_id=run.id, site_id=site_id, site_name="Unresolved", lane="general",
                      status=signal if isinstance(signal, str) else "fail", attempt=1,
                      result_json=_json(signal if isinstance(signal, dict) else {}),
                      finished_at=datetime.now(timezone.utc))
        db.add(job)
        db.commit()
        return job.id


def queued(ctx):
    takeover, manager, _ = ctx
    takeover.reconcile()
    with manager.session_factory() as db:
        return db.scalar(select(AssistantTakeoverAction))


def tools_for(ctx):
    takeover, manager, _ = ctx
    action = queued(ctx)
    sid = manager.create_takeover(action.id)
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, action.id)
        action.status = "dispatched"
        action.request_id = str(uuid4())
        row = db.get(AssistantSession, sid)
        row.status = "running"
        activate_takeover(manager, db, row, action.id, action.request_id)
        manager._active_turns[sid] = {
            "_takeover_action_id": action.id, "_takeover_request_id": action.request_id,
            "_request_id": action.request_id,
        }
        db.commit()
        target = manager._decrypt(row)["target"]
    return AssistantTools(manager, OWNER, sid, target,
                          turn=manager._active_turns[sid]), action.id


def fake_admit(manager, owner, sid, payload, action_id):
    """Model-free send doubles still establish the host's immutable turn binding."""
    with manager.session_factory() as db:
        row = manager._owned(db, owner, sid)
        activate_takeover(manager, db, row, action_id, str(payload.request_id))
        db.commit()
    manager._active_turns[sid] = {
        "_takeover_action_id": action_id, "_takeover_request_id": str(payload.request_id),
        "_request_id": str(payload.request_id),
    }


def test_settings_cas_server_owner_and_strict_wrapper(ctx):
    _, manager, _ = ctx
    with manager.session_factory() as db:
        default = TakeoverSettingsService(db).view()
        assert default.revision == 0 and not default.values.enabled and default.values.site_ids == []
    policy = grant(ctx)
    assert policy.authorizing_owner == OWNER and policy.effective_from == policy.authorized_at
    with manager.session_factory() as db:
        assert TakeoverSettingsService(db).view() == policy
        with pytest.raises(AssistantSettingsError, match="conflict"):
            TakeoverSettingsService(db).update(TakeoverSettingsUpdate(
                expected_revision=0, confirm=True, values=TakeoverValues()), "attacker")
        row = db.get(AppSetting, KEY)
        row.value_json = '{"revision":1,"values":{"enabled":true}}'
        db.commit()
        with pytest.raises(AssistantSettingsError, match="invalid"):
            TakeoverSettingsService(db).view()


@pytest.mark.parametrize("changes", [
    {"confirm": False}, {"confirm": 1}, {"authorizing_owner": "attacker"},
    {"values": {"enabled": 1}}, {"values": {"max_attempts": 0}},
    {"values": {"max_attempts": 4}}, {"values": {"site_ids": [True]}},
    {"values": {"site_ids": [1, 1]}}, {"values": {"unknown": True}},
])
def test_settings_reject_coercion_and_extra_authority(changes):
    with pytest.raises(ValidationError):
        TakeoverSettingsUpdate.model_validate({
            "expected_revision": 0, "confirm": True, "values": {}, **changes})


@pytest.mark.parametrize("changes,expected", [
    ({}, True), ({"status": "error", "run_status": "error"}, True),
    ({"trigger": "schedule"}, True), ({"historical": True}, False),
    ({"trigger": "assistant"}, False), ({"cancel": True}, False),
    ({"status": "success"}, False), ({"status": "unknown"}, False),
    ({"status": "waiting_human"}, True), ({"status": "deferred"}, False),
    ({"status": "waiting_human", "run_status": "waiting_human"}, True),
    ({"run_status": "waiting_human"}, True),
    ({"status": "interrupted"}, False), ({"run_status": "running"}, False),
    ({"uncertain": True}, True), ({"other_status": "unknown"}, True),
    ({"other_status": "interrupted"}, True), ({"other_status": "success"}, True),
    ({"run_status": "unknown"}, False), ({"run_status": "interrupted"}, False),
])
def test_reconciliation_status_matrix_and_no_historical_backfill(ctx, changes, expected):
    grant(ctx)
    failed_job(ctx, **changes)
    assert bool(queued(ctx)) is expected


def test_snapshot_encrypted_last_attempt_and_dedup_survives_chat_delete(ctx):
    grant(ctx)
    job_id, _ = failed_job(ctx)
    tools, action_id = tools_for(ctx)
    manager = tools.manager
    with manager.session_factory() as db:
        binding = db.get(AssistantJobConfigSnapshot, job_id)
        assert b"https://example.com" not in binding.ciphertext
        assert "snapshot" in json.loads(manager.crypto.decrypt(
            f"assistant-job-config:{job_id}", binding.ciphertext, binding.nonce, binding.key_version))
        row = db.get(AssistantSession, tools.session_id)
        row.status = "idle"
        db.commit()
    asyncio.run(manager.delete(OWNER, tools.session_id))
    ctx[0].reconcile()
    with manager.session_factory() as db:
        assert db.get(AssistantTakeoverAction, action_id) is not None
        assert db.scalar(select(func.count()).select_from(AssistantTakeoverAction)) == 1


def test_dispatch_retry_replaces_only_last_job_binding(ctx):
    grant(ctx, mode="autofix")
    job_id, _ = failed_job(ctx)
    _, manager, site_id = ctx
    with manager.session_factory() as db:
        job = db.get(SiteJob, job_id)
        site = db.get(Site, site_id)
        first = db.get(AssistantJobConfigSnapshot, job_id).ciphertext
        job.attempt = 2
        job.lane = "proxy_interaction"
        service = WorkflowService(db, manager.settings, manager.crypto)
        resolved = service.resolve_site(site_id)
        capture_job_config(db, manager.settings, manager.crypto, site, job, {
            "steps": [step.model_dump(mode="json") for step in resolved.steps],
            "signals": [signal.model_dump(mode="json") for signal in resolved.signals],
            "base_url": site.base_url, "lane_key": job.lane})
        db.commit()
        binding = db.get(AssistantJobConfigSnapshot, job_id)
        assert binding.attempt == 2 and binding.ciphertext != first
        assert db.scalar(select(func.count()).select_from(AssistantJobConfigSnapshot)) == 1
    assert queued(ctx).mode == "autofix"


def test_restart_reconciles_future_failure_but_not_old_jobs(ctx, monkeypatch):
    from app.services.assistant_takeover import AssistantTakeover

    grant(ctx)
    failed_job(ctx, historical=True)
    failed_job(ctx)
    _, manager, _ = ctx
    started_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    monkeypatch.setattr("app.services.assistant_takeover._now", lambda: started_at)
    restarted = AssistantTakeover(manager)
    restarted.reconcile()
    with manager.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AssistantTakeoverAction)) == 0
    future_job, future_run = failed_job(ctx)
    with manager.session_factory() as db:
        db.get(Run, future_run).finished_at = started_at + timedelta(seconds=1)
        db.get(SiteJob, future_job).finished_at = started_at + timedelta(seconds=1)
        db.commit()
    restarted.reconcile()
    restarted.reconcile()
    with manager.session_factory() as db:
        actions = db.scalars(select(AssistantTakeoverAction)).all()
        assert len(actions) == 1 and actions[0].source_job_id == future_job


@pytest.mark.parametrize("name,args", [
    ("run_site", {}), ("execute_step", {}), ("retry_step", {}),
    ("browser_open", {}), ("browser_observe", {}), ("browser_screenshot", {}),
    ("update_site_config", {}), ("request_user_interaction", {}),
    ("update_scheduler", {}), ("cancel_run", {}), ("update_template", {}),
    ("get_site_config", {"site_id": 9999}), ("get_run_results", {"run_id": 9999}),
    ("import_workflow_draft", {"proposal_message_id": 1}),
])
def test_analysis_malicious_tools_denied_even_if_global_settings_enabled(ctx, name, args):
    grant(ctx)
    failed_job(ctx)
    tools, _ = tools_for(ctx)
    assert takeover_tool_allowed(tools, name, args) == {"ok": False, "code": "takeover_tool_denied"}
    config = takeover_runtime(tools.manager, OWNER, tools.session_id, {**tools.turn, "_assistant_settings": {
        "auto_authorize_execution": True, "auto_authorize_workflow": True,
        "auto_authorize_screenshot": True}})
    assert not any(value for key, value in config["_assistant_settings"].items()
                   if key.startswith("auto_authorize_"))


@pytest.mark.parametrize("mode", ["analysis", "autofix"])
def test_shared_dispatch_enforces_persisted_takeover_for_every_write(ctx, mode):
    from app.services.assistant_prompt import tool_definitions

    grant(ctx, mode=mode)
    job_id, run_id = failed_job(ctx)
    tools, _ = tools_for(ctx)
    # Deliberately omit the runtime takeover flag and supply permissive defaults.
    tools.config = {"_assistant_settings": {"auto_authorize_workflow": True,
                    "auto_authorize_execution": True, "auto_authorize_screenshot": True}}
    denied = {"ok": False, "code": "takeover_tool_denied"}
    for name in ("run_site", "execute_step", "retry_step", "browser_open", "browser_screenshot",
                 "update_site_config", "update_scheduler_settings", "create_template_from_site"):
        assert asyncio.run(tools.dispatch(name, {})) == denied
    assert asyncio.run(tools.dispatch("cancel_run", {"run_id": run_id})) == denied
    for purpose in ("target", "authorization", "question", "site_config", "consumption",
                    "template_import", "scheduler_config", "run_cancel"):
        assert asyncio.run(tools.dispatch("request_user_interaction", {
            "request": {"purpose": purpose}})) == denied
    if mode == "analysis":
        assert asyncio.run(tools.dispatch("import_workflow_draft", {"proposal_message_id": 1})) == denied
    definitions = takeover_tool_definitions(tools.manager, OWNER, tools.session_id, tool_definitions())
    names = {item["function"]["name"] for item in definitions}
    assert not {"cancel_run", "update_scheduler_settings", "update_site_config",
                "create_template_from_site", "request_user_interaction", "run_site"} & names
    assert ("import_workflow_draft" in names) == (mode == "autofix")
    assert {"list_run_artifacts", "read_artifact_log", "read_artifact_image"} <= names
    assert asyncio.run(tools.dispatch("list_run_artifacts", {"run_id": run_id})) == denied
    assert asyncio.run(tools.dispatch("list_run_artifacts", {
        "run_id": run_id, "job_id": job_id})) == {"ok": True, "items": [], "offset": 0, "next_offset": None}
    with tools.manager.session_factory() as db:
        artifact = JobArtifact(run_id=run_id, site_job_id=job_id, kind="log", relative_path="missing",
                               content_type="text/plain")
        db.add(artifact)
        db.commit()
        artifact_id = artifact.id
    assert asyncio.run(tools.dispatch("read_artifact_log", {"artifact_id": artifact_id})) != denied
    assert asyncio.run(tools.dispatch("read_artifact_log", {"artifact_id": artifact_id + 1})) == denied


def test_conversation_sends_only_takeover_definitions(ctx, provider):
    from app.services.assistant_prompt import tool_definitions

    grant(ctx)
    failed_job(ctx)
    tools, _ = tools_for(ctx)
    with tools.manager.session_factory() as db:
        tools.manager._message(db, tools.session_id, "user", "Analyze the failed source job")
        db.commit()
    provider[0].append(text("Analysis only"))
    asyncio.run(tools.manager._conversation(OWNER, tools.session_id, dict(CONFIG)))
    request = json.loads(provider[1][0].content)
    expected = takeover_tool_definitions(tools.manager, OWNER, tools.session_id, tool_definitions())
    assert request["tools"] == expected
    assert len(expected) < 37


def test_analysis_direct_import_denied_without_dispatch_gate(ctx):
    grant(ctx)
    failed_job(ctx)
    tools, _ = tools_for(ctx)
    message_id = submit(tools)
    result = asyncio.run(AssistantWorkflows(tools.manager, OWNER, tools.session_id, tools.target).dispatch(
        "import_workflow_draft", {"proposal_message_id": message_id}))
    assert result == {"ok": False, "code": "takeover_write_denied"}


def test_autofix_bound_import_receipt_preserves_metadata_and_never_runs(ctx):
    grant(ctx, mode="autofix")
    job_id, run_id = failed_job(ctx)
    tools, action_id = tools_for(ctx)
    message_id = submit(tools)
    assert takeover_tool_allowed(tools, "import_workflow_draft", {"proposal_message_id": message_id}) is True
    workflows = AssistantWorkflows(tools.manager, OWNER, tools.session_id, tools.target)
    output = asyncio.run(workflows.dispatch("import_workflow_draft", {"proposal_message_id": message_id}))
    assert output["ok"] and output["operation"] == "updated", output
    tools.target = workflows.target
    assert takeover_tool_allowed(tools, "import_workflow_draft", {"proposal_message_id": message_id})["ok"] is False
    assert asyncio.run(workflows.dispatch("run_site", {}))["code"] == "takeover_write_denied"
    with tools.manager.session_factory() as db:
        site = db.get(Site, ctx[2])
        assert site.enabled and site.lane_policy == "proxy_general" and site.checkin_after == "14:30"
        assert site.template_id and json.loads(site.trusted_urls_json) == ["^https://example.com/"]
        receipt = json.loads(db.get(AssistantTakeoverAction, action_id).import_receipt_json)
        assert receipt["type"] == "workflow_import" and receipt["result"]["site_id"] == site.id
        assert db.scalar(select(func.count()).select_from(Run)) == 1
        assert db.get(AssistantJobConfigSnapshot, job_id).revision == 1
        assert takeover_source_job(tools.manager, db, OWNER, tools.session_id, run_id) == job_id


@pytest.mark.parametrize("change", ["disable", "mode", "template", "feature", "site", "target"])
def test_autofix_revalidates_policy_and_effective_source(ctx, change):
    grant(ctx, mode="autofix")
    failed_job(ctx)
    tools, _ = tools_for(ctx)
    message_id = submit(tools)
    with tools.manager.session_factory() as db:
        if change in {"disable", "mode"}:
            policy = TakeoverSettingsService(db).view()
            values = policy.values.model_copy(update={"enabled": change != "disable", "mode": "analysis"})
            TakeoverSettingsService(db).update(TakeoverSettingsUpdate(
                expected_revision=policy.revision, confirm=True, values=values), OWNER)
        elif change in {"template", "site"}:
            service = WorkflowService(db, tools.manager.settings, tools.manager.crypto)
            site = db.get(Site, ctx[2])
            if change == "site":
                site.config_revision += 1
            else:
                from app.models.workflow import Template

                db.get(Template, site.template_id).version += 1
            db.commit()
        elif change == "feature":
            db.add(AppSetting(key="feature.flow.open", value_json='{"unexpected":true}'))
            # Changing feature policy is tested by replacing the effective fingerprint.
            binding = db.scalar(select(AssistantJobConfigSnapshot))
            binding.fingerprint = "0" * 64
            db.commit()
        else:
            row = db.get(AssistantSession, tools.session_id)
            data = tools.manager._decrypt(row)
            data["target"]["workflow_site_id"] = 999
            tools.manager._encrypt(row, data)
            db.commit()
    assert takeover_tool_allowed(tools, "import_workflow_draft", {"proposal_message_id": message_id})["ok"] is False
    result = asyncio.run(AssistantWorkflows(tools.manager, OWNER, tools.session_id, tools.target).dispatch(
        "import_workflow_draft", {"proposal_message_id": message_id}))
    assert not result["ok"]


def test_source_feedback_only_and_definitions_filter(ctx):
    grant(ctx)
    job_id, run_id = failed_job(ctx, other_status="success")
    tools, _ = tools_for(ctx)
    with tools.manager.session_factory() as db:
        db.add(JobStep(site_job_id=job_id, step_key="open", feature_type="flow.open", status="fail",
                       data_json=_json({"reason": "selector_not_found", "password": "never-send"})))
        db.commit()
    feedback = AssistantWorkflows(tools.manager, OWNER, tools.session_id, tools.target).feedback(run_id)
    assert feedback["ok"] and len(feedback["jobs"]) == 1
    assert "never-send" not in _json(feedback)
    assert feedback["steps"][0]["data"] == {"reason": "selector_not_found"}
    defs = [{"function": {"name": name}} for name in (
        "get_run_log", "submit_workflow_draft", "import_workflow_draft", "run_site", "browser_open")]
    assert [d["function"]["name"] for d in takeover_tool_definitions(
        tools.manager, OWNER, tools.session_id, defs)] == ["get_run_log", "submit_workflow_draft"]


@pytest.mark.parametrize("replacement_origin", ["https://replacement.example/", "https://example.com/"])
def test_live_dispatch_denies_all_reads_after_site_id_reuse(ctx, replacement_origin):
    from app.services.assistant_prompt import tool_definitions

    grant(ctx)
    _, run_id = failed_job(ctx)
    tools, action_id = tools_for(ctx)
    manager = tools.manager
    with manager.session_factory() as db:
        original_key = db.get(Site, ctx[2]).history_key
        service = WorkflowService(db, manager.settings, manager.crypto)
        service.delete_site(ctx[2])
        replacement = service.create_site(SiteWrite(
            name="replacement-private-name", base_url=replacement_origin,
            lane_policy="proxy_general", **DRAFT))
        assert replacement.id == ctx[2]
        assert db.get(Site, replacement.id).history_key != original_key
        assert db.get(AssistantTakeoverAction, action_id).site_history_key == original_key
    denied = {"ok": False, "code": "takeover_tool_denied"}
    for name, args in (
        ("get_site_config", {"site_id": ctx[2]}),
        ("get_run_results", {"run_id": run_id}),
        ("get_run_log", {"run_id": run_id}),
        ("get_feature_schema", {"feature_type": "flow.open"}),
        ("get_tool_result", {"message_id": 1}),
    ):
        result = asyncio.run(tools.dispatch(name, args))
        assert result == denied
        assert "replacement-private-name" not in _json(result)
    assert takeover_tool_definitions(manager, OWNER, tools.session_id, tool_definitions()) == []


@pytest.mark.parametrize("change", ["binding_history", "site_origin", "target_origin"])
def test_live_dispatch_revalidates_history_binding_and_frozen_origins(ctx, change):
    grant(ctx)
    failed_job(ctx)
    tools, _ = tools_for(ctx)
    with tools.manager.session_factory() as db:
        row = db.get(AssistantSession, tools.session_id)
        data = tools.manager._decrypt(row)
        if change == "binding_history":
            data["target"]["_workflow_binding"]["history_key"] = "different-history"
        elif change == "site_origin":
            db.get(Site, ctx[2]).base_url = "https://replacement.example/"
        else:
            data["target"]["base_url"] = "https://replacement.example/"
        tools.manager._encrypt(row, data)
        db.commit()
    assert asyncio.run(tools.dispatch("get_site_config", {"site_id": ctx[2]})) == {
        "ok": False, "code": "takeover_tool_denied"}


def test_live_dispatch_allows_stale_revision_analysis_on_same_site_identity(ctx):
    grant(ctx, mode="autofix")
    _, run_id = failed_job(ctx)
    tools, _ = tools_for(ctx)
    with tools.manager.session_factory() as db:
        db.get(Site, ctx[2]).config_revision += 1
        db.commit()
    assert asyncio.run(tools.dispatch("get_site_config", {"site_id": ctx[2]}))["ok"]
    assert asyncio.run(tools.dispatch("get_run_results", {"run_id": run_id}))["ok"]
    assert asyncio.run(tools.dispatch("import_workflow_draft", {"proposal_message_id": 1})) == {
        "ok": False, "code": "takeover_tool_denied"}


def test_live_dispatch_denies_multisite_summary_without_leaking_other_site(ctx):
    from app.services.assistant_prompt import tool_definitions

    grant(ctx)
    job_id, run_id = failed_job(ctx)
    with ctx[1].session_factory() as db:
        private_site = WorkflowService(db, ctx[1].settings, ctx[1].crypto).create_site(SiteWrite(
            name="unrelated-private-site-name", base_url="https://private.example/", **DRAFT))
        other = SiteJob(run_id=run_id, site_id=private_site.id, site_name=private_site.name,
                        lane="general", status="success", attempt=1,
                        result_json=_json({"private": "unrelated-private-result"}),
                        finished_at=datetime.now(timezone.utc))
        db.add(other)
        db.flush()
        db.add(JobStep(site_job_id=other.id, step_key="private", feature_type="flow.open",
                       status="success", data_json=_json({"reason": "unrelated-private-step"})))
        db.commit()
    tools, _ = tools_for(ctx)
    summary = asyncio.run(tools.dispatch("get_run_summary", {"run_id": run_id}))
    assert summary == {"ok": False, "code": "takeover_tool_denied"}
    definitions = takeover_tool_definitions(tools.manager, OWNER, tools.session_id, tool_definitions())
    assert "get_run_summary" not in {item["function"]["name"] for item in definitions}
    for name in ("get_run_results", "get_run_log"):
        result = asyncio.run(tools.dispatch(name, {"run_id": run_id}))
        assert result["ok"] and len(result["jobs"]) == 1
        assert "unrelated-private" not in _json(result)
    assert asyncio.run(tools.dispatch("list_run_artifacts", {
        "run_id": run_id, "job_id": job_id}))["ok"]


def test_ordinary_session_not_restricted(ctx):
    _, manager, _ = ctx
    sid = manager.create(OWNER, CreateSession())["id"]
    tools = AssistantTools(manager, OWNER, sid, None)
    assert takeover_tool_allowed(tools, "run_site", {}) is True
    defs = [{"function": {"name": "run_site"}}]
    assert takeover_tool_definitions(manager, OWNER, sid, defs) == defs


def test_busy_deferral_and_restart_unknown_are_bounded(ctx):
    grant(ctx)
    failed_job(ctx)
    action = queued(ctx)
    takeover, manager, _ = ctx

    async def scenario():
        manager.tasks = {"a": None, "b": None}
        await takeover.process_one()
        manager.tasks = {}
        with manager.session_factory() as db:
            row = db.get(AssistantTakeoverAction, action.id)
            assert row.status == "queued" and row.session_id is None and row.attempts == 0
            row.status = "dispatched"
            db.commit()
        await takeover.start()
        await takeover.stop()
        assert takeover._task is None
    asyncio.run(scenario())
    with manager.session_factory() as db:
        assert db.get(AssistantTakeoverAction, action.id).status == "unknown"


@pytest.mark.parametrize("busy_status", ["queued", "running", "admission"])
def test_busy_head_does_not_block_another_sites_takeover(ctx, monkeypatch, busy_status):
    takeover, manager, site_a = ctx
    with manager.session_factory() as db:
        site_b = WorkflowService(db, manager.settings, manager.crypto).create_site(SiteWrite(
            name="Site B", base_url="https://b.example/", **DRAFT)).id
        TakeoverSettingsService(db).update(TakeoverSettingsUpdate(
            expected_revision=0, confirm=True,
            values=TakeoverValues(enabled=True, live_access=False, site_ids=[site_a, site_b])), OWNER)
    failed_job(ctx)
    failed_job((takeover, manager, site_b))
    takeover.reconcile()
    if busy_status != "admission":
        with manager.session_factory() as db:
            run = Run(created_by=OWNER, trigger="manual", status=busy_status)
            db.add(run)
            db.flush()
            db.add(SiteJob(run_id=run.id, site_id=site_a, site_name="Busy A", lane="general",
                           status=busy_status, attempt=1))
            db.commit()
    monkeypatch.setattr(manager, "_runtime", Mock(return_value={}))
    attempted = []

    async def fake_send(owner, sid, payload, *, action_id):
        from app.services.assistant import AssistantError

        with manager.session_factory() as db:
            site_id = manager._decrypt(db.get(AssistantSession, sid))["target"]["workflow_site_id"]
            attempted.append(site_id)
            if site_id == site_a:
                raise AssistantError("session_running")
            manager._message(db, sid, "user", payload.content, request_id=str(payload.request_id))
            manager._message(db, sid, "assistant", "Site B analysis")
            db.commit()

    monkeypatch.setattr(manager, "send_takeover", fake_send)

    async def scenario():
        if busy_status == "admission":
            await takeover.process_one()
        await takeover.process_one()

    asyncio.run(scenario())
    assert attempted == ([site_a, site_b] if busy_status == "admission" else [site_b])
    assert manager._runtime.call_count == len(attempted)
    with manager.session_factory() as db:
        actions = {action.site_id: action for action in db.scalars(select(AssistantTakeoverAction))}
        assert actions[site_a].status == "queued" and actions[site_a].attempts == 0
        assert actions[site_b].status == "completed_analysis" and actions[site_b].attempts == 1


UNCERTAIN_SIGNALS = [
    "unknown", "interrupted", "waiting_human",
    {"outcome_uncertain": True}, {"reason": "outcome_uncertain"},
    {"code": "assistant_outcome_uncertain"},
]


@pytest.mark.parametrize("trigger", ["manual", "schedule"])
@pytest.mark.parametrize("signal", UNCERTAIN_SIGNALS)
def test_same_site_ordinary_uncertainty_does_not_prevent_enqueue(ctx, trigger, signal):
    grant(ctx, mode="autofix")
    job_id, _ = failed_job(ctx)
    unresolved_id = uncertain_job(ctx, trigger, signal)
    queued(ctx)
    with ctx[1].session_factory() as db:
        sources = set(db.scalars(select(AssistantTakeoverAction.source_job_id)))
        expected = {job_id} if signal in ("unknown", "interrupted") else {job_id, unresolved_id}
        assert sources == expected


@pytest.mark.parametrize("trigger", ["manual", "schedule"])
@pytest.mark.parametrize("signal", UNCERTAIN_SIGNALS)
def test_same_site_uncertainty_limits_writes_without_blocking_either_site(ctx, monkeypatch, trigger, signal):
    takeover, manager, site_a = ctx
    with manager.session_factory() as db:
        site_b = WorkflowService(db, manager.settings, manager.crypto).create_site(SiteWrite(
            name="Site B", base_url="https://b.example/", **DRAFT)).id
        TakeoverSettingsService(db).update(TakeoverSettingsUpdate(
            expected_revision=0, confirm=True,
            values=TakeoverValues(enabled=True, live_access=False, mode="autofix", site_ids=[site_a, site_b])), OWNER)
    failed_job(ctx)
    failed_job((takeover, manager, site_b))
    takeover.reconcile()
    uncertain_job(ctx, trigger, signal)
    runtime = Mock(return_value={})
    monkeypatch.setattr(manager, "_runtime", runtime)
    admitted = []

    async def fake_send(owner, sid, payload, *, action_id):
        fake_admit(manager, owner, sid, payload, action_id)
        with manager.session_factory() as db:
            target = manager._decrypt(db.get(AssistantSession, sid))["target"]
            admitted.append(target["workflow_site_id"])
            assert target["workflow_consent"] is (target["workflow_site_id"] == site_b)
            manager._message(db, sid, "user", payload.content, request_id=str(payload.request_id))
            manager._message(db, sid, "assistant", "Scoped analysis")
            db.commit()

    monkeypatch.setattr(manager, "send_takeover", fake_send)
    asyncio.run(takeover.process_one())
    asyncio.run(takeover.process_one())
    assert admitted == [site_a, site_b] and runtime.call_count == 2
    with manager.session_factory() as db:
        action = db.scalar(select(AssistantTakeoverAction).where(AssistantTakeoverAction.site_id == site_a))
        assert action.status == "completed_analysis" and action.attempts == 1
        assert action.mode == "analysis" and action.import_receipt_json is None


@pytest.mark.parametrize("trigger", ["manual", "schedule"])
@pytest.mark.parametrize("signal", UNCERTAIN_SIGNALS)
def test_live_import_rejects_ordinary_uncertainty_appearing_during_turn(ctx, trigger, signal):
    from app.services.assistant_prompt import tool_definitions

    grant(ctx, mode="autofix")
    _, run_id = failed_job(ctx)
    tools, action_id = tools_for(ctx)
    proposal_id = submit(tools)
    args = {"proposal_message_id": proposal_id}
    assert takeover_tool_allowed(tools, "import_workflow_draft", args) is True
    uncertain_job(ctx, trigger, signal)
    assert asyncio.run(tools.dispatch("import_workflow_draft", args)) == {
        "ok": False, "code": "takeover_tool_denied"}
    # The transaction-local write guard also rejects callers bypassing tool dispatch.
    workflows = AssistantWorkflows(tools.manager, OWNER, tools.session_id, tools.target)
    assert asyncio.run(workflows.dispatch("import_workflow_draft", args)) == {
        "ok": False, "code": "takeover_write_denied"}
    names = {item["function"]["name"] for item in takeover_tool_definitions(
        tools.manager, OWNER, tools.session_id, tool_definitions())}
    assert "import_workflow_draft" not in names
    assert asyncio.run(tools.dispatch("get_run_results", {"run_id": run_id}))["ok"]
    with tools.manager.session_factory() as db:
        assert db.get(Site, ctx[2]).config_revision == 1
        assert db.get(AssistantTakeoverAction, action_id).import_receipt_json is None


def test_uncertainty_during_runtime_lookup_downgrades_dispatch_claim(ctx, monkeypatch):
    grant(ctx, mode="autofix")
    failed_job(ctx)
    action = queued(ctx)
    takeover, manager, _ = ctx
    monkeypatch.setattr(manager, "_runtime", lambda: uncertain_job(ctx, "manual", "unknown"))
    send = AsyncMock()
    monkeypatch.setattr(manager, "send_takeover", send)
    asyncio.run(takeover.process_one())
    send.assert_awaited_once()
    with manager.session_factory() as db:
        saved = db.get(AssistantTakeoverAction, action.id)
        assert saved.attempts == 1 and saved.request_id is not None
        assert saved.mode == "analysis" and saved.import_receipt_json is None


def test_same_site_step_uncertainty_prevents_live_import(ctx):
    grant(ctx, mode="autofix")
    failed_job(ctx)
    tools, _ = tools_for(ctx)
    proposal_id = submit(tools)
    other_job = uncertain_job(ctx, "manual", {})
    with tools.manager.session_factory() as db:
        db.add(JobStep(site_job_id=other_job, step_key="unresolved", feature_type="flow.open",
                       status="fail", data_json=_json({"reason": "outcome_uncertain"})))
        db.commit()
    assert asyncio.run(tools.dispatch("import_workflow_draft", {"proposal_message_id": proposal_id})) == {
        "ok": False, "code": "takeover_tool_denied"}


def test_stale_source_downgrades_without_writes(ctx, monkeypatch):
    grant(ctx, mode="autofix")
    failed_job(ctx)
    queued(ctx)
    takeover, manager, _ = ctx
    with manager.session_factory() as db:
        db.get(Site, ctx[2]).config_revision += 1
        db.commit()
    monkeypatch.setattr(manager, "_runtime", lambda: {})

    async def fake_send(owner, sid, payload, *, action_id):
        fake_admit(manager, owner, sid, payload, action_id)
        with manager.session_factory() as db:
            target = manager._decrypt(db.get(AssistantSession, sid))["target"]
            assert not target["workflow_consent"] and not target["execution_consent"]
            manager._message(db, sid, "user", payload.content, request_id=str(payload.request_id))
            manager._message(db, sid, "assistant", "Source changed, analysis only.")
            db.commit()
    monkeypatch.setattr(manager, "send_takeover", fake_send)
    asyncio.run(takeover.process_one())
    with manager.session_factory() as db:
        action = db.scalar(select(AssistantTakeoverAction))
        assert action.mode == "analysis" and action.status == "completed_analysis"
        assert action.import_receipt_json is None


def test_unknown_step_reserved_action_and_artifact_scope(ctx):
    from app.models.assistant_workflow import AssistantWorkflowAction

    grant(ctx, mode="autofix")
    job_id, run_id = failed_job(ctx)
    _, manager, _ = ctx
    with manager.session_factory() as db:
        step = JobStep(site_job_id=job_id, step_key="open", feature_type="flow.open", status="unknown")
        db.add(step)
        db.commit()
    assert queued(ctx).source_job_id == job_id
    with manager.session_factory() as db:
        db.get(JobStep, step.id).status = "fail"
        db.commit()
    tools, _ = tools_for(ctx)
    with manager.session_factory() as db:
        artifact = JobArtifact(run_id=run_id, site_job_id=job_id, kind="log", relative_path="none",
                               content_type="text/plain")
        db.add(artifact)
        db.commit()
    assert takeover_tool_allowed(tools, "read_artifact_log", {"artifact_id": artifact.id}) is True
    assert takeover_tool_allowed(tools, "read_artifact_log", {"artifact_id": artifact.id + 1})["ok"] is False
    assert takeover_tool_allowed(tools, "list_run_artifacts", {"run_id": run_id})["ok"] is False
    assert takeover_tool_allowed(tools, "list_run_artifacts", {"run_id": run_id, "job_id": job_id}) is True
    with manager.session_factory() as db:
        reserved = AssistantWorkflowAction(id="reserved", session_id=tools.session_id, owner=OWNER,
            kind="run", state="reserved", site_id=ctx[2], request_fingerprint="unresolved",
            ciphertext=b"", nonce=b"", key_version=1)
        db.add(reserved)
        db.commit()
    assert takeover_tool_allowed(tools, "import_workflow_draft", {})["ok"] is False


def test_model_attempt_budget_and_busy_acknowledgement(ctx, monkeypatch):
    from app.services.assistant import AssistantError

    grant(ctx, max_attempts=2)
    failed_job(ctx)
    queued(ctx)
    takeover, manager, _ = ctx
    monkeypatch.setattr(manager, "_runtime", lambda: {})

    busy_requests = []

    async def busy(owner, sid, payload, *, action_id):
        busy_requests.append(str(payload.request_id))
        raise AssistantError("assistant_busy", 429)
    monkeypatch.setattr(manager, "send_takeover", busy)
    asyncio.run(takeover.process_one())
    with manager.session_factory() as db:
        action = db.scalar(select(AssistantTakeoverAction))
        assert action.status == "queued" and action.attempts == 0
        assert action.request_id is None
        first_request = busy_requests[0]

    async def failed(owner, sid, payload, *, action_id):
        with manager.session_factory() as db:
            manager._message(db, sid, "user", payload.content, request_id=str(payload.request_id))
            manager._message(db, sid, "system", "provider failed")
            db.commit()
    monkeypatch.setattr(manager, "send_takeover", failed)
    asyncio.run(takeover.process_one())
    with manager.session_factory() as db:
        action = db.scalar(select(AssistantTakeoverAction))
        assert action.status == "queued" and action.attempts == 1
        assert action.request_id == first_request
    asyncio.run(takeover.process_one())
    with manager.session_factory() as db:
        action = db.scalar(select(AssistantTakeoverAction))
        assert action.status == "failed" and action.attempts == 2
        assert action.request_id != first_request
        assert db.scalar(select(func.count()).select_from(Run)) == 1


def test_acknowledgement_loss_never_replays(ctx, monkeypatch):
    grant(ctx, max_attempts=3)
    failed_job(ctx)
    queued(ctx)
    takeover, manager, _ = ctx
    monkeypatch.setattr(manager, "_runtime", lambda: {})

    async def lost_ack(owner, sid, payload, *, action_id):
        with manager.session_factory() as db:
            manager._message(db, sid, "user", payload.content, request_id=str(payload.request_id))
            db.commit()
        raise RuntimeError("ack lost")
    send = AsyncMock(side_effect=lost_ack)
    monkeypatch.setattr(manager, "send_takeover", send)
    asyncio.run(takeover.process_one())
    asyncio.run(takeover.process_one())
    assert send.await_count == 1
    with manager.session_factory() as db:
        assert db.scalar(select(AssistantTakeoverAction)).status == "unknown"


def test_api_auth_confirmation_cas_and_owner_scope(client, anon_client):
    root = "/api/v1/assistant/takeover"
    assert anon_client.get(root + "/settings").status_code == 401
    assert anon_client.get(root + "/actions").status_code == 401
    default = client.get(root + "/settings")
    assert default.status_code == 200 and default.json()["revision"] == 0
    assert default.headers["cache-control"] == "no-store"
    assert client.put(root + "/settings", json={"expected_revision": 0, "values": {}}).status_code == 422
    saved = client.put(root + "/settings", json={
        "expected_revision": 0, "confirm": True,
        "values": {"enabled": True, "live_access": False, "mode": "analysis", "site_ids": None, "max_attempts": 1}})
    assert saved.status_code == 200, saved.text
    assert saved.json()["authorizing_owner"]
    assert client.put(root + "/settings", json={
        "expected_revision": 0, "confirm": True, "values": {}}).status_code == 409
    with client.app.state.session_factory() as db:
        db.add(AssistantTakeoverAction(id="foreign", source_job_id=9000, source_run_id=9000,
            site_id=9000, owner="another-owner", grant_revision=1, source_fingerprint="",
            site_history_key="foreign", site_origin="https://foreign.example",
            mode="analysis", status="completed_analysis"))
        db.commit()
    assert client.get(root + "/actions").json() == {"items": []}


def test_fake_model_turn_imports_without_rerun(ctx, monkeypatch):
    grant(ctx, mode="autofix")
    takeover, manager, _ = ctx
    with manager.session_factory() as db:
        run = ExecutionService(db, manager.settings, manager.crypto).create_run(
            RunCreate(site_ids=[ctx[2]]), OWNER, trigger="manual")
    manager.executor._post_execute = AsyncMock(return_value={
        "status": "fail", "steps": [{"step_key": "open", "feature_type": "flow.open",
                                      "status": "fail", "data": {"reason": "selector_not_found"}}]})
    manager.executor.schedule_run_notification = Mock()
    asyncio.run(manager.executor._execute_run(run.id))
    assert manager.executor._post_execute.await_count == 1
    queued(ctx)
    monkeypatch.setattr(manager, "_runtime", lambda: {
        "api_key": "provider-private-key", "model": "fake", "base_url": "https://provider.example/v1",
        "timeout": 10})

    async def fake_turn(owner, sid, config):
        with manager.session_factory() as db:
            target = manager._decrypt(db.get(AssistantSession, sid))["target"]
        tools = AssistantTools(manager, owner, sid, target, config=config)
        message_id = await asyncio.to_thread(submit, tools)
        assert takeover_tool_allowed(tools, "import_workflow_draft", {"proposal_message_id": message_id}) is True
        output = await AssistantWorkflows(manager, owner, sid, target).dispatch(
            "import_workflow_draft", {"proposal_message_id": message_id})
        assert output["ok"], output
        assert takeover_tool_allowed(tools, "run_site", {})["ok"] is False
        with manager.session_factory() as db:
            manager._message(db, sid, "assistant", "Validated fix saved, not executed.")
            db.get(AssistantSession, sid).status = "idle"
            db.commit()
        manager.tasks.pop(sid, None)

    monkeypatch.setattr(manager, "_run", fake_turn)
    asyncio.run(takeover.process_one())
    with manager.session_factory() as db:
        action = db.scalar(select(AssistantTakeoverAction))
        assert action.status == "updated" and action.attempts == 1 and action.request_id
        assert db.scalar(select(func.count()).select_from(Run)) == 1
        assert db.scalar(select(AssistantJobConfigSnapshot)).fingerprint
    assert manager.executor._post_execute.await_count == 1
