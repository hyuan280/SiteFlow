from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import time
from unittest.mock import Mock, AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.main import create_app
from app.models.assistant import AssistantSession
from app.models.assistant_run import AssistantRunGuard
from app.models.assistant_workflow import AssistantWorkflowAction
from app.models.execution import JobArtifact, JobStep, Run, SiteJob
from app.models.workflow import Site, SiteCredential, Template, WorkflowHistory, WorkflowStep
from app.schemas.assistant import CreateSession, SetTarget
from app.schemas.notification import NotificationLayoutUpdate
from app.schemas.workflow import SiteWrite, TemplateWrite
from app.services.assistant_lanes import saved_lane
from app.services.assistant_run_guard import validate_guard
from app.services.assistant_tools import AssistantTools
from app.services.config_ai_generation import _json
from app.services.notifications import NotificationService
from app.services.workflows import WorkflowService
from tests.helpers import make_settings
from tests.test_assistant_api import (  # noqa: F401
    ROOT, assistant_client, provider, create, target, send, tool, text, wait,
)


OWNER = "workflow-owner"
BASE = "https://example.com/"
DRAFT = {"steps": [{"step_key": "open", "feature_type": "flow.open", "config": {}}],
         "groups": [], "signals": []}


@pytest.fixture
def context(tmp_path):
    manager = create_app(make_settings(tmp_path, config_ai_enabled=True)).state.assistant_manager
    manager.executor = Mock()
    manager._close_browser = AsyncMock()
    identifier = manager.create(OWNER, CreateSession())["id"]
    return manager, identifier


def configure(context, **changes):
    manager, identifier = context
    payload = {"name": "Example", "base_url": BASE, "credential_mode": "none",
               "proxy": True, "authorized": True, "workflow_consent": True, **changes}
    asyncio.run(manager.set_target(OWNER, identifier, SetTarget(**payload)))
    with manager.session_factory() as db:
        row = db.get(AssistantSession, identifier)
        row.status = "running"
        db.commit()
        target = manager._decrypt(row)["target"]
    return AssistantTools(manager, OWNER, identifier, target)


def call(tools, name, arguments=None):
    return asyncio.run(tools.dispatch(name, arguments or {}))


def submit(tools, draft=None):
    draft = deepcopy(draft or DRAFT)
    for step in draft["steps"]:
        assert call(tools, "get_feature_schema", {"feature_type": step["feature_type"]})["ok"]
    result = call(tools, "submit_workflow_draft", {
        "draft": draft, "explanation": "Observed page workflow", "unresolved_questions": []})
    assert result.get("ok"), result
    manager = tools.manager
    with manager.session_factory() as db:
        data = manager._decrypt(db.get(AssistantSession, tools.session_id))
        row = manager._message(db, tools.session_id, "tool", _json(result),
                               tool_name="submit_workflow_draft", status="success",
                               _target_revision=data.get("_target_revision", 0))
        db.commit()
        return row.id


def import_draft(tools, draft=None):
    message_id = submit(tools, draft)
    result = call(tools, "import_workflow_draft", {"proposal_message_id": message_id})
    assert result.get("ok"), result
    return result, message_id


def make_site(context, **changes):
    manager, _ = context
    with manager.session_factory() as db:
        service = WorkflowService(db, manager.settings, manager.crypto)
        return service.create_site(SiteWrite(name="Saved", base_url=BASE, **changes))


@pytest.mark.parametrize("policy", ["auto", "general", "interaction", "proxy_general", "proxy_interaction"])
def test_import_lane_is_host_policy_not_browser_proxy(context, policy):
    tools = configure(context, run_lane_policy=policy)
    imported, proposal_id = import_draft(tools)
    assert imported["lane_policy"] == policy
    assert imported["lane"] == ("general" if policy == "auto" else policy)
    assert saved_lane({"proxy": True}) == "auto"
    repeated = call(tools, "import_workflow_draft", {"proposal_message_id": proposal_id})
    assert repeated == {**imported, "replayed": True}
    with tools.manager.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Site)) == 1
        site = db.get(Site, imported["site_id"])
        assert not site.enabled and not site.support_resign and site.checkin_after is None
        assert imported["operation"] == "created" and imported["name"] == site.name
        assert imported["history_key"] == site.history_key
        assert imported["config_revision"] == imported["site_revision"] == site.config_revision
        assert site.trusted_urls_json == "[]"
        assert db.get(SiteCredential, site.id) is None
        history = db.scalar(select(WorkflowHistory).where(WorkflowHistory.history_key == site.history_key))
        assert history.created_by == "assistant"
        assert db.get(AssistantSession, tools.session_id).owner == OWNER
        assert db.scalar(select(AssistantWorkflowAction)).owner == OWNER


def test_old_overall_grant_does_not_authorize_workflow_writes(context, monkeypatch):
    monkeypatch.setattr("app.services.assistant_interactions.INTERACTION_TIMEOUT", 0.01)
    tools = configure(context, workflow_consent=False)
    message_id = submit(tools)
    for name, args in [("import_workflow_draft", {"proposal_message_id": message_id}), ("run_site", {})]:
        result = call(tools, name, args)
        assert result["code"] == "interaction_timed_out" and result["terminal"]
    with tools.manager.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Site)) == 0
        assert db.scalar(select(func.count()).select_from(Run)) == 0


def test_destination_is_not_credential_source_and_manual_accounts_are_not_copied(context):
    source = make_site(context)
    tools = configure(context, credential_mode="manual", site_id=source.id)
    imported, _ = import_draft(tools)
    assert imported["site_id"] != source.id and imported["manual_credentials_needed"]
    assert call(tools, "run_site")["code"] == "manual_credentials_needed"
    with tools.manager.session_factory() as db:
        assert db.get(Site, source.id).config_revision == 1
        assert db.get(SiteCredential, imported["site_id"]) is None


def test_existing_destination_retains_authoritative_settings_and_template(context):
    manager, _ = context
    with manager.session_factory() as db:
        template = WorkflowService(db, manager.settings, manager.crypto).create_template(TemplateWrite(name="Base"))
    site = make_site(context, template_id=template.id, notes="private notes", checkin_after="14:30",
                     tags=["keep"], support_resign=True, trusted_urls=["^https://example.com/"])
    tools = configure(context, workflow_site_id=site.id, run_lane_policy="proxy_general")
    imported, _ = import_draft(tools)
    assert imported["site_id"] == site.id and imported["site_revision"] == 2
    assert imported["operation"] == "updated" and imported["enabled"] is True
    with manager.session_factory() as db:
        current = db.get(Site, site.id)
        assert current.name == "Saved" and current.template_id == template.id
        assert current.notes == "private notes" and current.checkin_after == "14:30"
        assert current.support_resign and json.loads(current.tags_json) == ["keep"]
        assert json.loads(current.trusted_urls_json) == ["^https://example.com/"]
        history = db.scalar(select(WorkflowHistory).where(
            WorkflowHistory.history_key == current.history_key, WorkflowHistory.revision == 2))
        assert history.created_by == "assistant"


def test_stale_destination_binding_rejects_import_without_overwrite(context):
    site = make_site(context)
    tools = configure(context, workflow_site_id=site.id)
    message_id = submit(tools)
    with tools.manager.session_factory() as db:
        db.get(Site, site.id).config_revision += 1
        db.commit()
    result = call(tools, "import_workflow_draft", {"proposal_message_id": message_id})
    assert not result["ok"]
    with tools.manager.session_factory() as db:
        assert db.get(Site, site.id).config_revision == 2
        assert db.scalar(select(func.count()).select_from(AssistantWorkflowAction)) == 0


def test_cross_session_proposals_and_results_are_not_authority(context):
    tools = configure(context)
    imported, message_id = import_draft(tools)
    run = call(tools, "run_site")
    assert run.get("ok"), run
    other = tools.manager.create(OWNER, CreateSession())["id"]
    outsider = configure((tools.manager, other))
    assert call(outsider, "import_workflow_draft", {"proposal_message_id": message_id})["code"] == "proposal_unavailable"
    assert call(outsider, "get_run_results", {"run_id": run["run_id"]})["code"] == "run_unavailable"
    assert call(outsider, "get_run_log", {"run_id": run["run_id"]})["code"] == "run_unavailable"


def test_run_is_atomic_guarded_idempotent_and_assistant_audited(context):
    tools = configure(context)
    imported, _ = import_draft(tools)
    first = call(tools, "run_site")
    assert first.get("ok"), first
    assert first["created_by"] == "assistant"
    assert OWNER not in _json(first)
    assert call(tools, "run_site") == {**first, "replayed": True}
    tools.manager.executor.submit.assert_called_once_with(first["run_id"])
    with tools.manager.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Run)) == 1
        run = db.get(Run, first["run_id"])
        assert run.trigger == "assistant" and run.created_by == "assistant" and not run.force
        assert db.get(AssistantSession, tools.session_id).owner == OWNER
        actions = list(db.scalars(select(AssistantWorkflowAction)))
        assert {action.kind for action in actions} == {"import", "run"}
        assert all(action.owner == OWNER and action.ciphertext for action in actions)
        assert db.get(AssistantRunGuard, run.id)
        site = db.get(Site, imported["site_id"])
        assert validate_guard(db, run.id, site, tools.manager.settings, tools.manager.crypto)
        site.config_revision += 1
        db.flush()
        assert not validate_guard(db, run.id, site, tools.manager.settings, tools.manager.crypto)


@pytest.mark.parametrize("policy", ["auto", "general", "interaction", "proxy_general", "proxy_interaction"])
@pytest.mark.parametrize("proxy", [False, True])
def test_run_releases_only_own_discovery_before_create_and_submit(context, monkeypatch, policy, proxy):
    from app.services.assistant import BrowserScope
    from app.services.execution import ExecutionService

    tools = configure(context, run_lane_policy=policy, proxy=proxy)
    import_draft(tools)
    manager = tools.manager
    other_id = manager.create("another-owner", CreateSession())["id"]
    lane = "proxy_interaction" if proxy else "interaction"
    own = BrowserScope(OWNER, str(uuid4()), "own-token", lane, "", time.monotonic() + 900)
    other = BrowserScope("another-owner", str(uuid4()), "other-token", lane, "", time.monotonic() + 900)
    manager.browsers.update({tools.session_id: own, other_id: other})
    events = []
    original_create = ExecutionService.create_run

    async def close(path, payload):
        assert path == f"{own.session_id}/close" and payload == {"token": own.token}
        with manager.session_factory() as db:
            assert db.scalar(select(func.count()).select_from(Run)) == 0
        manager.executor.submit.assert_not_called()
        await asyncio.sleep(0)
        events.append("closed")
        return {"closed": True}

    def create_run(service, *args, **kwargs):
        assert events == ["closed"]
        assert manager.browsers == {other_id: other}
        events.append("create")
        return original_create(service, *args, **kwargs)

    def submit_run(run_id):
        assert events == ["closed", "create"]
        assert manager.browsers == {other_id: other}
        events.append("submit")

    manager._worker = AsyncMock(side_effect=close)
    manager.executor.submit.side_effect = submit_run
    monkeypatch.setattr(ExecutionService, "create_run", create_run)
    first = call(tools, "run_site")
    assert first["ok"], first
    assert first["lane"] == ("general" if policy == "auto" else policy)
    assert call(tools, "run_site") == {**first, "replayed": True}
    assert events == ["closed", "create", "submit"]
    manager._worker.assert_awaited_once_with(f"{own.session_id}/close", {"token": own.token})
    manager.executor.submit.assert_called_once_with(first["run_id"])
    assert manager.browsers == {other_id: other}
    with manager.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Run)) == 1


@pytest.mark.parametrize("policy", ["auto", "general", "interaction", "proxy_general", "proxy_interaction"])
@pytest.mark.parametrize("failure", ["exception", "unconfirmed"])
def test_discovery_close_failure_creates_no_run_and_can_retry(context, policy, failure):
    from app.services.assistant import BrowserScope

    tools = configure(context, run_lane_policy=policy)
    import_draft(tools)
    manager = tools.manager
    scope = BrowserScope(OWNER, str(uuid4()), "own-token", "proxy_interaction", "", time.monotonic() + 900)
    manager.browsers[tools.session_id] = scope
    manager._worker = AsyncMock(side_effect=RuntimeError("close failed")) if failure == "exception" else AsyncMock(
        return_value={"closed": False})
    assert not call(tools, "run_site")["ok"]
    assert manager.browsers[tools.session_id] is scope
    manager.executor.submit.assert_not_called()
    with manager.session_factory() as db:
        for model in (Run, SiteJob, AssistantRunGuard):
            assert db.scalar(select(func.count()).select_from(model)) == 0
        assert db.scalar(select(func.count()).select_from(AssistantWorkflowAction).where(
            AssistantWorkflowAction.kind == "run")) == 0
    manager._worker = AsyncMock(return_value={"closed": True})
    first = call(tools, "run_site")
    assert first["ok"], first
    assert call(tools, "run_site") == {**first, "replayed": True}
    manager._worker.assert_awaited_once_with(f"{scope.session_id}/close", {"token": scope.token})
    manager.executor.submit.assert_called_once_with(first["run_id"])
    with manager.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Run)) == 1


@pytest.mark.parametrize("change", ["target", "status", "binding", "takeover", "browser"])
def test_run_revalidates_authority_after_discovery_close(context, change):
    from app.services.assistant import BrowserScope

    tools = configure(context, run_lane_policy="general")
    imported, _ = import_draft(tools)
    manager = tools.manager
    scope = BrowserScope(OWNER, str(uuid4()), "own-token", "proxy_interaction", "", time.monotonic() + 900)
    manager.browsers[tools.session_id] = scope

    async def close(path, payload):
        await asyncio.sleep(0)
        if change == "browser":
            manager.browsers[tools.session_id] = BrowserScope(
                OWNER, str(uuid4()), "replacement-token", "interaction", "", time.monotonic() + 900)
            return {"closed": True}
        with manager.session_factory() as db:
            row = db.get(AssistantSession, tools.session_id)
            if change == "status":
                row.status = "idle"
            elif change == "binding":
                db.get(Site, imported["site_id"]).config_revision += 1
            else:
                data = manager._decrypt(row)
                if change == "target":
                    data["target"]["workflow_consent"] = False
                else:
                    data["_takeover"] = {}
                manager._encrypt(row, data)
            db.commit()
        return {"closed": True}

    manager._worker = AsyncMock(side_effect=close)
    assert not call(tools, "run_site")["ok"]
    manager._worker.assert_awaited_once()
    if change == "browser":
        assert manager.browsers[tools.session_id].token == "replacement-token"
    manager.executor.submit.assert_not_called()
    with manager.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Run)) == 0


def test_run_checks_takeover_authority_before_closing_discovery(context):
    from app.services.assistant import BrowserScope
    from app.services.assistant_workflows import AssistantWorkflows

    tools = configure(context)
    import_draft(tools)
    manager = tools.manager
    scope = BrowserScope(OWNER, str(uuid4()), "own-token", "proxy_interaction", "", time.monotonic() + 900)
    manager.browsers[tools.session_id] = scope
    manager._worker = AsyncMock()
    with manager.session_factory() as db:
        row = db.get(AssistantSession, tools.session_id)
        data = manager._decrypt(row)
        data["_takeover"] = {}
        manager._encrypt(row, data)
        db.commit()
    workflows = AssistantWorkflows(manager, OWNER, tools.session_id, tools.target)
    assert asyncio.run(workflows.dispatch("run_site", {})) == {"ok": False, "code": "takeover_write_denied"}
    manager._worker.assert_not_awaited()
    assert manager.browsers[tools.session_id] is scope
    manager.executor.submit.assert_not_called()
    with manager.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Run)) == 0


def test_notification_actor_is_assistant_without_rewriting_manual_history(context):
    tools = configure(context)
    import_draft(tools)
    result = call(tools, "run_site")
    assert result["ok"], result
    with tools.manager.session_factory() as db:
        manual = Run(id=329, trigger="manual", created_by="admin")
        db.add(manual)
        db.commit()
        service = NotificationService(db, tools.manager.settings, tools.manager.crypto)
        event = service._run_event(result["run_id"])
        assert event["created_by"] == event["trigger"] == event["trigger_label"] == "assistant"
        assert "assistant" in service._render(event)["body"]
        service.update_layout(NotificationLayoutUpdate(
            title_template="{{created_by}}", body_template="{{trigger_label}}", format="text"))
        assert service._render(event) == {"title": "assistant", "body": "assistant", "format": "text"}
        manual_event = service._run_event(manual.id)
        assert manual_event["created_by"] == "admin" and manual_event["trigger"] == "manual"
        db.refresh(manual)
        assert manual.created_by == "admin" and manual.trigger == "manual"


def test_failed_dispatch_is_not_automatically_resubmitted(context):
    tools = configure(context)
    import_draft(tools)
    tools.manager.executor.submit.side_effect = RuntimeError("private failure")
    first = call(tools, "run_site")
    assert first["code"] == "run_submission_uncertain"
    assert first["ok"] is False and first["submitted"] is False
    replay = call(tools, "run_site")
    assert replay == {**first, "replayed": True}
    feedback = call(tools, "get_run_results", {"run_id": first["run_id"]})
    assert feedback["submitted"] is False and feedback["submission_code"] == "run_submission_uncertain"
    tools.manager.executor.submit.assert_called_once()


def test_guard_failure_rolls_back_run_and_action(context, monkeypatch):
    tools = configure(context)
    import_draft(tools)
    monkeypatch.setattr("app.services.assistant_run_guard.create_guard", Mock(side_effect=ValueError()))
    assert not call(tools, "run_site")["ok"]
    with tools.manager.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Run)) == 0
        assert db.scalar(select(func.count()).select_from(AssistantWorkflowAction).where(AssistantWorkflowAction.kind == "run")) == 0


def test_feedback_is_bounded_structural_and_does_not_leak_runtime_text(context):
    tools = configure(context)
    import_draft(tools)
    run = call(tools, "run_site")
    assert run.get("ok"), run
    secret = "password-token-cookie-never-export"
    with tools.manager.session_factory() as db:
        job = db.scalar(select(SiteJob).where(SiteJob.run_id == run["run_id"]))
        db.add_all([JobStep(site_job_id=job.id, step_key="open", feature_type="flow.open",
                           status="error", message=secret, label=secret,
                           data_json=json.dumps({"count": 2, "password": secret,
                                                 "reason": "execution_timeout", "url": BASE + secret})),
                    JobArtifact(run_id=run["run_id"], site_job_id=job.id, kind="log",
                                relative_path="../../private.log", content_type="text/plain", label=secret)])
        db.commit()
    results = call(tools, "get_run_results", {"run_id": run["run_id"], "limit": 1})
    assert results["steps"][0]["data"] == {"count": 2, "reason": "execution_timeout"}
    logs = call(tools, "get_run_log", {"run_id": run["run_id"], "limit": 20})
    assert logs["raw_log_available_in_ui"] and len(logs["content"].encode()) <= 20
    assert secret not in json.dumps([results, logs])


@pytest.mark.parametrize("name,args", [
    ("import_workflow_draft", {"draft": DRAFT}), ("import_workflow_draft", {"proposal_message_id": True}),
    ("run_site", {"site_id": 1}), ("run_site", {"force": True}),
    ("get_run_results", {"run_id": 1, "limit": 51}),
    ("get_run_results", {"run_id": 1, "wait_seconds": 21}),
    ("get_run_log", {"run_id": 1, "limit": 16001}),
])
def test_tool_arguments_do_not_accept_authority_or_unbounded_reads(context, name, args):
    tools = configure(context)
    assert call(tools, name, args) == {"ok": False, "code": "invalid_arguments"}


@pytest.mark.parametrize("valid", [False, True])
def test_login_evidence_is_projected_in_results_and_structured_logs(context, valid):
    from tests.test_login_evidence_projection import evidence

    tools = configure(context)
    import_draft(tools)
    run_id = call(tools, "run_site")["run_id"]
    diagnostic = evidence()
    diagnostic.update(selector="private-selector-value", message="private-provider-message")
    diagnostic["selectors"][0]["frames"][0]["url"] = "https://private.example/"
    if not valid:
        diagnostic["selectors"][0]["frames"][0]["error"] = "private-provider-error"
    with tools.manager.session_factory() as db:
        job = db.scalar(select(SiteJob).where(SiteJob.run_id == run_id))
        db.add(JobStep(site_job_id=job.id, step_key="login", feature_type="auth.form_login",
                       status="unknown", message="private-runtime-message", data_json=_json({
                           "submitted": True, "verified": False, "reason": "success_evidence_missing",
                           "field": "success_selector", "cause": "execution_timeout",
                           "success_evidence": diagnostic,
                       })))
        db.commit()
    result = call(tools, "get_run_results", {"run_id": run_id})
    log = call(tools, "get_run_log", {"run_id": run_id})
    expected = {"submitted": True, "verified": False, "reason": "success_evidence_missing",
                "field": "success_selector", "cause": "execution_timeout",
                **({"success_evidence": evidence()} if valid else {})}
    assert result["steps"][0]["status"] == "unknown"
    assert result["steps"][0]["data"] == json.loads(log["content"])["data"] == expected
    assert "private-" not in _json([result, log])
    assert "private.example" not in _json([result, log])


def test_complete_conversation_submits_imports_runs_and_reads_feedback(assistant_client, provider):
    client = assistant_client
    manager = client.app.state.assistant_manager
    manager.executor = Mock()
    identifier = create(client)
    target(client, identifier, credential_mode="none", workflow_consent=True)

    async def import_response(request):
        messages = json.loads(request.content)["messages"]
        latest = next(json.loads(item["content"]) for item in reversed(messages)
                      if item["role"] == "tool")
        assert latest["submitted"]
        return tool("import_workflow_draft", {"proposal_message_id": latest["proposal_message_id"]}, "import")

    async def results_response(request):
        messages = json.loads(request.content)["messages"]
        latest = next(json.loads(item["content"]) for item in reversed(messages)
                      if item["role"] == "tool")
        assert latest["trigger"] == "assistant"
        assert latest["created_by"] == "assistant"
        return tool("get_run_results", {"run_id": latest["run_id"]}, "results")

    provider[0].extend([
        tool("get_feature_schema", {"feature_type": "flow.open"}, "schema"),
        tool("submit_workflow_draft", {"draft": DRAFT, "explanation": "Observed workflow",
                                       "unresolved_questions": []}, "submit"),
        import_response, tool("run_site", {}, "run"), results_response, text("Saved; full run is queued."),
    ])
    send(client, identifier, content="Import and test this workflow")
    detail = wait(client, identifier)
    tools = [item for item in detail["messages"] if item["role"] == "tool"]
    assert [item["tool_name"] for item in tools] == [
        "get_feature_schema", "submit_workflow_draft", "import_workflow_draft", "run_site", "get_run_results"]
    assert all(item["status"] == "success" for item in tools), tools
    assert detail["target"]["workflow_site_id"] is not None
    assert "_workflow_binding" not in json.dumps(detail)
    with manager.session_factory() as db:
        assert db.get(AssistantSession, identifier).owner == "admin"
        assert all(action.owner == "admin" for action in db.scalars(select(AssistantWorkflowAction)))
    manager.executor.submit.assert_called_once()


def test_protected_local_inline_values_require_manual_editor(context):
    manager, _ = context
    site = make_site(context)
    secret = "private-inline-field"
    with manager.session_factory() as db:
        db.add(WorkflowStep(owner_type="site", owner_id=site.id, step_key="inline",
                            feature_type="sign.iframe.image_captcha", category="sign",
                            config_json=json.dumps({"fields": {"hidden": secret}})))
        db.commit()
    tools = configure(context, workflow_site_id=site.id)
    result = call(tools, "validate_workflow_draft", {"draft": DRAFT})
    assert result == {"ok": False, "code": "protected_config_changed"}
    assert secret not in json.dumps(result)
    with manager.session_factory() as db:
        assert db.get(Site, site.id).config_revision == 1


def test_inherited_purchase_requires_bounded_consumption_independently_of_draft(context):
    manager, _ = context
    site = make_site(context)
    with manager.session_factory() as db:
        template = Template(name="Unreviewed parent")
        db.add(template)
        db.flush()
        db.add(WorkflowStep(owner_type="template", owner_id=template.id, step_key="post",
                            feature_type="post.buy_card", category="post", config_json="{}"))
        db.get(Site, site.id).template_id = template.id
        db.commit()
    tools = configure(context, workflow_site_id=site.id)
    assert call(tools, "run_site") == {"ok": False, "code": "consumption_max_price_required"}
    manager.executor.submit.assert_not_called()


def test_recovery_marks_reserved_actions_unknown_without_reexecution(context):
    tools = configure(context)
    _, proposal_id = import_draft(tools)
    with tools.manager.session_factory() as db:
        action = db.scalar(select(AssistantWorkflowAction))
        action.state = "reserved"
        db.commit()
    tools.manager.recover()
    tools.manager.executor.submit.assert_not_called()
    with tools.manager.session_factory() as db:
        action = db.scalar(select(AssistantWorkflowAction))
        assert action.state == "unknown"
        row = db.get(AssistantSession, tools.session_id)
        row.status = "running"
        db.commit()
    assert call(tools, "import_workflow_draft", {"proposal_message_id": proposal_id})["code"] == "workflow_outcome_uncertain"


def test_old_submission_after_target_change_is_not_importable(context):
    tools = configure(context)
    proposal_id = submit(tools)
    with tools.manager.session_factory() as db:
        row = db.get(AssistantSession, tools.session_id)
        row.status = "idle"
        db.commit()
    tools = configure(context, run_lane_policy="proxy_general")
    assert call(tools, "import_workflow_draft", {"proposal_message_id": proposal_id})["code"] == "proposal_target_changed"


def test_workflow_destination_requires_exact_origin_and_strict_host_fields(context):
    manager, identifier = context
    site = make_site(context)
    payload = {"name": "Example", "base_url": "https://other.example/", "credential_mode": "none",
               "proxy": False, "authorized": True, "workflow_site_id": site.id}
    from app.services.assistant import AssistantError

    with pytest.raises(AssistantError, match="Assistant request") as error:
        asyncio.run(manager.set_target(OWNER, identifier, SetTarget(**payload)))
    assert error.value.code == "workflow_site_mismatch"
    from pydantic import ValidationError

    for change in ({"workflow_consent": 1}, {"workflow_site_id": True}, {"run_lane_policy": "bad"},
                   {"_workflow_binding": {"id": site.id}}):
        with pytest.raises(ValidationError):
            SetTarget(**{**payload, **change})


def test_bitwarden_import_uses_runtime_provider_without_fetching_credentials(context, monkeypatch):
    from app.services.bitwarden import BitwardenService

    resolver = Mock(side_effect=AssertionError("no secret lookups during import or enqueue"))
    monkeypatch.setattr(BitwardenService, "resolve_for_site", resolver)
    tools = configure(context, credential_mode="bitwarden")
    imported, _ = import_draft(tools)
    result = call(tools, "run_site")
    assert result.get("ok"), result
    with tools.manager.session_factory() as db:
        site = db.get(Site, imported["site_id"])
        assert site.credential_provider_id == "builtin.bitwarden" and site.bitwarden_enabled
        assert db.get(SiteCredential, site.id) is None
    resolver.assert_not_called()


def test_cookie_run_requires_saved_destination_cookies(context):
    from app.services.credentials import CredentialService

    site = make_site(context, steps=DRAFT["steps"])
    manager, _ = context
    with manager.session_factory() as db:
        CredentialService(db, manager.crypto).update_cookies(site.id, "session=private-cookie-never-export")
    tools = configure(context, credential_mode="cookie", workflow_site_id=site.id)
    result = call(tools, "run_site")
    assert result.get("ok"), result
    assert "private-cookie" not in json.dumps(result)


def test_manual_existing_destination_refreshes_both_bindings_without_secret_reads(context, monkeypatch):
    from app.services.credentials import CredentialService

    site = make_site(context)
    manager, _ = context
    with manager.session_factory() as db:
        CredentialService(db, manager.crypto).update(site.id, username="private-user", password="private-password")
    tools = configure(context, credential_mode="manual", workflow_site_id=site.id, site_id=site.id)
    monkeypatch.setattr(CredentialService, "view", Mock(side_effect=AssertionError("no display decryption")))
    imported, _ = import_draft(tools)
    assert not imported["manual_credentials_needed"]
    assert tools.target["_workflow_binding"] == tools.target["_credential_binding"]
    assert call(tools, "run_site")["ok"]


def test_corrected_import_creates_new_run_only_after_prior_run_finishes(context):
    tools = configure(context)
    imported, _ = import_draft(tools)
    first = call(tools, "run_site")
    with tools.manager.session_factory() as db:
        db.get(Run, first["run_id"]).status = "fail"
        db.scalar(select(SiteJob).where(SiteJob.run_id == first["run_id"])).status = "fail"
        db.commit()
    second_import, _ = import_draft(tools)
    assert second_import["site_id"] == imported["site_id"]
    assert second_import["site_revision"] == imported["site_revision"] + 1
    second = call(tools, "run_site")
    assert second.get("ok") and second["run_id"] != first["run_id"]
    assert tools.manager.executor.submit.call_count == 2


def test_import_finalization_failure_rolls_back_site_history_and_action(context, monkeypatch):
    from app.services.assistant_workflows import AssistantWorkflows

    tools = configure(context)
    proposal_id = submit(tools)
    original = AssistantWorkflows._seal

    def fail_completion(self, action, value):
        if "result" in value:
            raise ValueError("simulated encrypted finalization failure")
        return original(self, action, value)

    monkeypatch.setattr(AssistantWorkflows, "_seal", fail_completion)
    assert not call(tools, "import_workflow_draft", {"proposal_message_id": proposal_id})["ok"]
    with tools.manager.session_factory() as db:
        for model in (Site, WorkflowHistory, AssistantWorkflowAction):
            assert db.scalar(select(func.count()).select_from(model)) == 0


def test_new_tool_definitions_are_unique_titled_bounded_and_valid():
    from jsonschema import Draft202012Validator
    from app.services.assistant_prompt import tool_definitions

    definitions = tool_definitions()
    names = [item["function"]["name"] for item in definitions]
    assert len(names) == len(set(names))
    assert "create_template_from_site" in names
    for item in definitions:
        schema = item["function"]["parameters"]
        Draft202012Validator.check_schema(schema)
        assert "title" in schema["required"]
        assert schema["additionalProperties"] is False


def test_other_active_run_blocks_new_assistant_run(context):
    tools = configure(context)
    imported, _ = import_draft(tools)
    with tools.manager.session_factory() as db:
        run = Run(created_by="another-user")
        db.add(run)
        db.flush()
        db.add(SiteJob(run_id=run.id, site_id=imported["site_id"], site_name="Example",
                       lane="general", status="running"))
        db.commit()
    assert call(tools, "run_site") == {"ok": False, "code": "site_run_active"}
    tools.manager.executor.submit.assert_not_called()


def test_changed_revision_does_not_bypass_uncertain_prior_full_run(context):
    tools = configure(context)
    import_draft(tools)
    first = call(tools, "run_site")
    with tools.manager.session_factory() as db:
        db.get(Run, first["run_id"]).status = "unknown"
        db.scalar(select(SiteJob).where(SiteJob.run_id == first["run_id"])).status = "unknown"
        db.commit()
    import_draft(tools)
    assert call(tools, "run_site") == {"ok": False, "code": "workflow_outcome_uncertain"}
    tools.manager.executor.submit.assert_called_once()


def store_proposal(tools, draft):
    """Persist a previously successful proposal to test revalidation after changes."""
    manager = tools.manager
    with manager.session_factory() as db:
        data = manager._decrypt(db.get(AssistantSession, tools.session_id))
        message = manager._message(db, tools.session_id, "tool", _json({
            "ok": True, "submitted": True, "proposal": {"draft": draft, "explanation": "Prior validation",
                                                         "unresolved_questions": []}}),
            tool_name="submit_workflow_draft", status="success",
            _target_revision=data.get("_target_revision", 0))
        db.commit()
        return message.id


@pytest.mark.parametrize("url", [
    "https://unapproved.example/path", "https://sso.example/path", "http://example.com/path",
    "https://example.com:444/path", "http://127.0.0.1/", "http://169.254.169.254/latest/",
    "javascript:alert(1)", "file:///etc/passwd", "https://user:secret@example.com/",
    "https://example.com/?token=private", "https://example.com/?t%6fken=private",
    "https://example.com/?next=https%3A%2F%2Fexample.com%2F%3Ftoken%3Dprivate",
    "https://example.com/?x=%255BREDACTED%255D",
])
def test_import_revalidates_navigation_before_any_write(context, url):
    tools = configure(context, trusted_origins=["https://sso.example"])
    draft = deepcopy(DRAFT)
    draft["steps"][0]["config"] = {"url": url}
    message_id = store_proposal(tools, draft)
    result = call(tools, "import_workflow_draft", {"proposal_message_id": message_id})
    assert result == {"ok": False, "code": "workflow_url_not_authorized"}
    with tools.manager.session_factory() as db:
        for model in (Site, WorkflowHistory, AssistantWorkflowAction):
            assert db.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize("feature", ["flow.open", "action.navigate"])
def test_inherited_navigation_revalidated_for_import_and_actual_run(context, feature):
    manager, _ = context
    with manager.session_factory() as db:
        template = WorkflowService(db, manager.settings, manager.crypto).create_template(TemplateWrite(
            name="Inherited navigation", steps=[{"step_key": "ancestor", "feature_type": feature,
                                                  "config": {"url": "https://sso.example/"}}]))
    site = make_site(context, template_id=template.id)
    tools = configure(context, workflow_site_id=site.id, trusted_origins=["https://sso.example"])
    message_id = store_proposal(tools, {"steps": [{"step_key": "wait", "feature_type": "action.wait",
                                                 "config": {}}], "groups": [], "signals": []})
    assert call(tools, "import_workflow_draft", {"proposal_message_id": message_id})["code"] == "workflow_url_not_authorized"
    assert call(tools, "run_site")["code"] == "workflow_url_not_authorized"
    manager.executor.submit.assert_not_called()
    with manager.session_factory() as db:
        assert db.get(Site, site.id).config_revision == 1
        assert db.scalar(select(func.count()).select_from(Run)) == 0


@pytest.mark.parametrize("config", [
    {"iframe_url": "http://127.0.0.1/"},
    {"nested": {"attendance_url": "https://sso.example/"}},
    {"nested": [{"shop_url": "javascript:alert(1)"}]},
    {"u%72l": "https://sso.example/"},
])
def test_nested_and_encoded_url_field_checks(config):
    from app.services.assistant_workflows import validate_workflow_urls, WorkflowDenied

    with pytest.raises(WorkflowDenied, match="workflow_url_not_authorized"):
        validate_workflow_urls({"steps": [{"enabled": True, "config": config}]}, {"base_url": BASE})


def test_public_queries_and_empty_hidden_defaults_allow_repeated_corrections(context):
    tools = configure(context)
    draft = deepcopy(DRAFT)
    draft["steps"][0]["config"] = {"url": BASE + "?tab=foo#/login?tab=foo/"}
    first, _ = import_draft(tools, draft)
    second, _ = import_draft(tools, draft)
    assert second["site_id"] == first["site_id"] and second["config_revision"] == 2
    with tools.manager.session_factory() as db:
        step = db.scalar(select(WorkflowStep).where(WorkflowStep.owner_id == first["site_id"]))
        assert json.loads(step.config_json)["url"] == draft["steps"][0]["config"]["url"]
        db.add(WorkflowStep(owner_type="site", owner_id=first["site_id"], step_key="result",
                            feature_type="flow.result", category="flow",
                            config_json='{"llm_enabled":false,"llm_prompt":""}'))
        db.commit()
    third, _ = import_draft(tools, draft)
    assert third["config_revision"] == 3


def test_live_results_and_log_pagination_detect_mutable_step_updates(context):
    tools = configure(context)
    import_draft(tools)
    run_id = call(tools, "run_site")["run_id"]
    with tools.manager.session_factory() as db:
        job = db.scalar(select(SiteJob).where(SiteJob.run_id == run_id))
        db.add_all([JobStep(site_job_id=job.id, step_key=key, feature_type="action.wait",
                           status="running", data_json="{}") for key in ("first", "second")])
        db.commit()
    first = call(tools, "get_run_results", {"run_id": run_id, "limit": 1})
    assert first["next_cursor"] == 1
    next_args = {"run_id": run_id, "cursor": 1, "limit": 1, "snapshot_id": first["snapshot_id"]}
    second = call(tools, "get_run_results", next_args)
    assert second["steps"][0]["step_key"] == "second"
    logs = call(tools, "get_run_log", {"run_id": run_id, "limit": 10})
    assert logs["next_offset"] == 10
    with tools.manager.session_factory() as db:
        step = db.scalar(select(JobStep).where(JobStep.step_key == "first"))
        step.status = "success"
        db.commit()
    changed = call(tools, "get_run_results", next_args)
    assert changed["code"] == "run_snapshot_changed" and changed["restart_cursor"] == 0
    changed_log = call(tools, "get_run_log", {"run_id": run_id, "offset": 10,
                                             "snapshot_id": logs["snapshot_id"]})
    assert changed_log["code"] == "run_snapshot_changed" and changed_log["restart_offset"] == 0
    assert call(tools, "get_run_results", {"run_id": run_id, "cursor": 1})["code"] == "run_snapshot_changed"
    refreshed = call(tools, "get_run_results", {"run_id": run_id})
    assert refreshed["steps"][0]["status"] == "success"


def test_disabled_import_runs_explicitly_but_stays_out_of_scheduler_selection(context):
    from app.services.execution import ExecutionService, ExecutionValidationError
    from app.schemas.execution import RunCreate

    tools = configure(context)
    imported, _ = import_draft(tools)
    result = call(tools, "run_site")
    assert result["ok"]
    with tools.manager.session_factory() as db:
        assert db.get(Site, imported["site_id"]).enabled is False
        with pytest.raises(ExecutionValidationError):
            ExecutionService(db, tools.manager.settings, tools.manager.crypto).create_run(RunCreate(), OWNER)


@pytest.mark.parametrize("job_status,data", [
    ("error", {}), ("fail", {"outcome_uncertain": True}),
    ("fail", {"reason": "assistant_outcome_uncertain"}),
])
def test_failed_run_uncertainty_is_shared_across_sessions(context, job_status, data):
    tools = configure(context)
    imported, _ = import_draft(tools)
    run_id = call(tools, "run_site")["run_id"]
    with tools.manager.session_factory() as db:
        db.get(Run, run_id).status = "fail"
        job = db.scalar(select(SiteJob).where(SiteJob.run_id == run_id))
        job.status = job_status
        job.result_json = _json(data)
        db.commit()
    other = tools.manager.create(OWNER, CreateSession())["id"]
    outsider = configure((tools.manager, other), workflow_site_id=imported["site_id"])
    assert call(outsider, "run_site") == {"ok": False, "code": "workflow_outcome_uncertain"}
    tools.manager.executor.submit.assert_called_once()


def test_submission_uncertainty_survives_new_revision_even_if_run_is_marked_failed(context):
    tools = configure(context)
    import_draft(tools)
    tools.manager.executor.submit.side_effect = RuntimeError("lost acknowledgement")
    failed = call(tools, "run_site")
    with tools.manager.session_factory() as db:
        db.get(Run, failed["run_id"]).status = "fail"
        db.scalar(select(SiteJob).where(SiteJob.run_id == failed["run_id"])).status = "fail"
        db.commit()
    import_draft(tools)
    assert call(tools, "run_site") == {"ok": False, "code": "workflow_outcome_uncertain"}
    tools.manager.executor.submit.assert_called_once()


def test_navigation_validation_preserves_relative_public_queries_and_ignores_disabled_steps():
    from app.services.assistant_workflows import validate_workflow_urls

    resolved = {"steps": [{"enabled": True, "config": {"url": "/login?tab=foo#/path?tab=foo/"}},
                          {"enabled": False, "config": {"url": "http://127.0.0.1/"}}]}
    before = deepcopy(resolved)
    validate_workflow_urls(resolved, {"base_url": BASE})
    assert resolved == before


@pytest.mark.parametrize("config", [{"llm_enabled": True}, {"llm_prompt": "private server prompt"},
                                    {"fields": {"hidden": "private value"}},
                                    {"nested": {"api_key": "private key"}}])
def test_nonempty_protected_values_still_require_manual_editor(config):
    from app.services.assistant_workflows import _contains_protected

    assert _contains_protected(config, {"llm_enabled", "llm_prompt"}, base_url=BASE)


def test_failed_submission_marks_conversation_tool_error(assistant_client, provider):
    client = assistant_client
    manager = client.app.state.assistant_manager
    manager.executor = Mock()
    manager.executor.submit.side_effect = RuntimeError("private dispatch failure")
    identifier = create(client)
    target(client, identifier, credential_mode="none", workflow_consent=True)

    async def import_response(request):
        messages = json.loads(request.content)["messages"]
        latest = next(json.loads(item["content"]) for item in reversed(messages) if item["role"] == "tool")
        return tool("import_workflow_draft", {"proposal_message_id": latest["proposal_message_id"]}, "import")

    provider[0].extend([
        tool("get_feature_schema", {"feature_type": "flow.open"}, "schema"),
        tool("submit_workflow_draft", {"draft": DRAFT, "explanation": "Observed workflow",
                                      "unresolved_questions": []}, "submit"),
        import_response, tool("run_site", {}, "run"), text("Submission could not be confirmed."),
    ])
    send(client, identifier, content="Import and test")
    detail = wait(client, identifier)
    run_message = next(item for item in detail["messages"] if item.get("tool_name") == "run_site")
    assert run_message["status"] == "error"
    result = json.loads(run_message["content"])
    assert result["ok"] is False and result["run_id"] > 0 and result["submitted"] is False
    assert "private dispatch" not in json.dumps(detail)


@pytest.mark.parametrize("diagnostics,expected", [
    ({"reason": "selector_not_found", "field": "submit_selector", "stage": "fill",
      "cause": "selector_not_found"},
     {"reason": "selector_not_found", "field": "submit_selector", "stage": "fill",
      "cause": "selector_not_found"}),
    ({"reason": "input_validation_failed", "field": "config", "stage": "input_validation",
      "cause": "execution_error"},
     {"reason": "input_validation_failed", "field": "config", "stage": "input_validation",
      "cause": "execution_error"}),
    ({"reason": "selector_not_found", "field": "private-password", "stage": "private-stage",
      "cause": "private-cookie", "value": "private-value", "message": "private-message"},
     {"reason": "selector_not_found"}),
    ({"field": ["submit_selector"], "stage": {"name": "fill"}, "cause": True}, {}),
])
def test_repair_diagnostics_use_only_code_owned_values(context, diagnostics, expected):
    tools = configure(context)
    import_draft(tools)
    run_id = call(tools, "run_site")["run_id"]
    with tools.manager.session_factory() as db:
        job = db.scalar(select(SiteJob).where(SiteJob.run_id == run_id))
        db.add(JobStep(site_job_id=job.id, step_key="sign", feature_type="sign.button",
                       status="fail", message="private runtime message", data_json=_json(diagnostics)))
        db.commit()
    result = call(tools, "get_run_results", {"run_id": run_id})
    assert result["steps"][0]["data"] == expected
    log = call(tools, "get_run_log", {"run_id": run_id})
    assert json.loads(log["content"])["data"] == expected
    exposed = _json([result, log])
    assert all(value not in exposed for value in (
        "private runtime message", "private-password", "private-stage", "private-cookie",
        "private-value", "private-message"))


@pytest.mark.parametrize("uncertain", [False, True])
def test_real_executor_fake_worker_feedback_correction_and_uncertainty(
    context, monkeypatch, uncertain, fake_dispatch,
):
    from app.services import executor as executor_module
    from app.services.assistant import BrowserScope
    from app.services.assistant_execution import AssistantExecution
    from app.services.executor import ExecutionManager

    tools = configure(context)
    manager = tools.manager
    executor = ExecutionManager(manager.session_factory, manager.settings, manager.crypto)
    executor.schedule_run_notification = Mock()
    manager.executor = executor
    real_client = httpx.AsyncClient
    payloads = []
    private = "private-worker-message-password-cookie"
    draft = deepcopy(DRAFT)
    public_url = BASE + "?tab=foo#/login?tab=foo/"
    draft["steps"][0]["config"] = {"url": public_url}
    draft["steps"].append({"step_key": "sign", "feature_type": "sign.iframe.image_captcha",
                           "config": {"submit_selector": ["#missing"]}})

    async def worker(request):
        assert request.method == "POST" and request.url.path == "/execute/stream"
        payload = json.loads(request.content)
        fake_dispatch.validate(payload)
        payloads.append(payload)
        assert payload["assistant_run"] is True and payload["lane_key"] == "general"
        assert payload["steps"][0]["config"]["url"] == public_url
        status = "unknown" if uncertain else "fail" if len(payloads) == 1 else "success"
        diagnostics = ({"reason": "outcome_uncertain", "outcome_uncertain": True,
                        "stage": "step_execution", "cause": "execution_timeout"} if uncertain else
                       {"reason": "selector_not_found", "field": "submit_selector",
                        "stage": "fill", "cause": "selector_not_found"} if status == "fail" else
                       {"verified": True})
        diagnostics.update(password=private, message=private, url=BASE + "?token=" + private)
        rows = [{"step_key": step["step_key"], "feature_type": step["feature_type"],
                 "status": "success" if index == 0 else status, "label": private, "message": private,
                 "data": {} if index == 0 else diagnostics} for index, step in enumerate(payload["steps"])]
        events = [{"type": "progress", "status": "running", "steps": rows},
                  {"type": "result", "result": {"status": status, "message": private, "steps": rows}}]
        return httpx.Response(200, content="\n".join(_json(event) for event in events),
                              headers={"content-type": "application/x-ndjson"})

    monkeypatch.setattr(executor_module.httpx, "AsyncClient",
                        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(worker)))

    async def scenario():
        # Tool dispatch is async, but import validation remains synchronous and
        # uses only the temporary test database, just like a real assistant turn.
        for step in draft["steps"]:
            assert (await tools.dispatch("get_feature_schema", {"feature_type": step["feature_type"]}))["ok"]
        # Store a successful proposal through the same validation/submission path.
        result = await tools.dispatch("submit_workflow_draft", {
            "draft": draft, "explanation": "Observed signing controls", "unresolved_questions": []})
        assert result["ok"], result

        def persist(result):
            with manager.session_factory() as db:
                data = manager._decrypt(db.get(AssistantSession, tools.session_id))
                row = manager._message(db, tools.session_id, "tool", _json(result),
                                       tool_name="submit_workflow_draft", status="success",
                                       _target_revision=data["_target_revision"])
                db.commit()
                return row.id

        imported = await tools.dispatch("import_workflow_draft", {"proposal_message_id": persist(result)})
        assert imported["ok"] and imported["enabled"] is False
        first = await tools.dispatch("run_site", {})
        assert first["ok"] and first["status"] == "queued"
        with manager.session_factory() as db:
            run = db.get(Run, first["run_id"])
            assert run.status == "queued" and run.trigger == "assistant" and run.created_by == "assistant"
        await executor._tasks[first["run_id"]]
        feedback = await tools.dispatch("get_run_results", {"run_id": first["run_id"]})
        assert feedback["status"] == ("unknown" if uncertain else "fail")
        sign = feedback["steps"][1]
        logs = await tools.dispatch("get_run_log", {"run_id": first["run_id"]})
        assert private not in _json([feedback, logs])
        if uncertain:
            assert sign["data"]["cause"] == "execution_timeout"
            replay = await tools.dispatch("run_site", {})
            assert replay["replayed"] and replay["run_id"] == first["run_id"]
            assert len(payloads) == 1
            scope = BrowserScope(OWNER, str(uuid4()), "private-browser-token", "proxy_interaction", "",
                                 time.monotonic() + 900)
            manager.browsers[tools.session_id] = scope
            from tests.test_assistant_execution import response

            manager._worker = AsyncMock(side_effect=lambda path, payload: (
                {"closed": True} if path == f"{scope.session_id}/close"
                else response(scope, payload["operation_id"])))
            step_result = await AssistantExecution(manager, OWNER, tools.session_id, tools.target).execute({
                "operation_id": str(uuid4()), "step": {"step_key": "click", "feature_type": "action.click",
                                                        "config": {"selector": ["#sign"]}}})
            assert step_result["ok"]
            manager._worker.assert_awaited_once()
        else:
            assert sign["data"] == {"reason": "selector_not_found", "field": "submit_selector",
                                    "stage": "fill", "cause": "selector_not_found"}
        draft["steps"][1]["config"]["submit_selector"] = ["#observed-submit"]
        proposal = await tools.dispatch("submit_workflow_draft", {
            "draft": draft, "explanation": "Correct observed submit selector", "unresolved_questions": []})
        assert proposal["ok"], proposal
        corrected = await tools.dispatch("import_workflow_draft", {"proposal_message_id": persist(proposal)})
        assert corrected["ok"] and corrected["config_revision"] == imported["config_revision"] + 1
        assert corrected["enabled"] is False
        second = await tools.dispatch("run_site", {})
        if uncertain:
            assert second == {"ok": False, "code": "workflow_outcome_uncertain"}
            assert len(payloads) == 1
        else:
            assert second["ok"] and second["run_id"] != first["run_id"]
            await executor._tasks[second["run_id"]]
            success = await tools.dispatch("get_run_results", {"run_id": second["run_id"]})
            assert success["status"] == "success" and success["steps"][1]["data"] == {"verified": True}
            assert payloads[-1]["steps"][1]["config"]["submit_selector"] == ["#observed-submit"]
        with manager.session_factory() as db:
            assert db.get(Site, imported["site_id"]).enabled is False
            assert db.scalar(select(func.count()).select_from(Run)) == (1 if uncertain else 2)
        await executor.shutdown()

    asyncio.run(scenario())
