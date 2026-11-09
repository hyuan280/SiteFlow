from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import httpx
from pydantic import ValidationError
from sqlalchemy import select

from app.core.crypto import CryptoService
from app.core.db import Base, create_db_engine, create_session_factory
from app.models.assistant_run import AssistantRunGuard
from app.models.execution import JobStep, Run, SiteJob
from app.models.setting import AppSetting
from app.models.workflow import Site, Template, WorkflowStep
from app.schemas.execution import RunCreate
from app.services.assistant_run_guard import create_guard, unresolved_assistant_runs, validate_guard
from app.services.credentials import CredentialService
from app.services.execution import ExecutionService, ExecutionValidationError
from app.services.executor import ExecutionManager
from app.services.feature_settings import FeatureSettingsService
from app.services.notifications import NotificationService
from app.services.scheduler import Scheduler
from app.services.workflows import WorkflowService, WorkflowValidationError


@pytest.fixture
def context(settings):
    engine = create_db_engine(settings.database_url)
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    crypto = CryptoService(b"x" * 32)
    with factory() as session:
        root = Template(name="root")
        session.add(root)
        session.flush()
        parent = Template(name="parent", parent_id=root.id)
        session.add(parent)
        session.flush()
        site = Site(name="guarded", base_url="https://example.test", template_id=parent.id)
        session.add(site)
        session.flush()
        session.add_all([
            WorkflowStep(owner_type="template", owner_id=root.id, step_key="open",
                         feature_type="flow.open", category="flow", position=1,
                         config_json='{"wait_until":"load"}'),
            WorkflowStep(owner_type="site", owner_id=site.id, step_key="result",
                         feature_type="flow.result", category="flow", position=2),
        ])
        session.commit()
        yield_context = SimpleNamespace(
            factory=factory, settings=settings, crypto=crypto,
            site_id=site.id, root_id=root.id, parent_id=parent.id,
        )
    yield yield_context
    engine.dispose()


def _create_run(context, *, trigger="assistant", guarded=True, allow_disabled=False):
    with context.factory() as session:
        run = ExecutionService(session, context.settings, context.crypto).create_run(
            RunCreate(site_ids=[context.site_id]), "human", trigger=trigger, _commit=False,
            _allow_disabled=allow_disabled,
        )
        if guarded:
            create_guard(session, run.id, context.site_id,
                         WorkflowService(session, context.settings, context.crypto))
        session.commit()
        return run


def _manager(context, monkeypatch):
    manager = ExecutionManager(context.factory, context.settings, context.crypto)
    manager._post_execute = AsyncMock(return_value={"status": "success", "steps": []})
    cookies = Mock(return_value=None)
    monkeypatch.setattr(CredentialService, "resolve_cookies", cookies)
    return manager, cookies


def _assert_changed(context, run):
    with context.factory() as session:
        job = session.get(SiteJob, run.jobs[0].id)
        assert job.status == "fail"
        assert job.message == "助手测试配置已变化，请重新确认后运行"
        assert json.loads(job.result_json) == {"reason": "assistant_configuration_changed"}
        assert job.finished_at is not None


def test_guard_is_canonical_hash_only_and_never_decrypts_credentials(context, monkeypatch):
    decrypt = Mock(side_effect=AssertionError("must not decrypt credentials"))
    monkeypatch.setattr(context.crypto, "decrypt", decrypt)
    run = _create_run(context)
    with context.factory() as session:
        site = session.get(Site, context.site_id)
        snapshot = WorkflowService(session, context.settings, context.crypto)._snapshot(site)
        expected = hashlib.sha256(json.dumps(
            snapshot, allow_nan=False, sort_keys=True, ensure_ascii=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        guard = session.get(AssistantRunGuard, run.id)
        assert guard.fingerprint == expected
        assert guard.site_history_key == site.history_key
        assert guard.site_revision == site.config_revision
        assert set(guard.__table__.columns.keys()) == {
            "run_id", "site_id", "site_history_key", "site_revision", "fingerprint",
        }
        assert validate_guard(session, run.id, site, context.settings, context.crypto)
    decrypt.assert_not_called()


@pytest.mark.parametrize("change", [
    "base_url", "enabled", "lane_policy", "notes", "trusted_urls_json",
    "credential_provider_id", "config_revision", "history_key", "site_step",
    "ancestor_step", "ancestor_version", "ancestor_history_key", "ancestor_enabled",
    "feature_defaults", "feature_execution", "job_lane", "invalid_workflow",
])
def test_external_changes_fail_before_credentials_or_dispatch(context, monkeypatch, change):
    run = _create_run(context)
    with context.factory() as session:
        site = session.get(Site, context.site_id)
        if change in {"base_url", "enabled", "lane_policy", "notes", "trusted_urls_json",
                      "credential_provider_id", "config_revision", "history_key"}:
            setattr(site, change, {
                "base_url": "https://changed.test", "enabled": False,
                "lane_policy": "interaction", "notes": "external change",
                "trusted_urls_json": '["https://trusted.test"]',
                "credential_provider_id": "builtin.bitwarden", "config_revision": 2,
                "history_key": "replaced-site",
            }[change])
        elif change in {"site_step", "ancestor_step", "invalid_workflow"}:
            owner_type = "site" if change == "site_step" else "template"
            step = session.scalar(select(WorkflowStep).where(WorkflowStep.owner_type == owner_type))
            step.config_json = "broken" if change == "invalid_workflow" else '{"timeout":7}'
            if change == "ancestor_step":
                step.config_json = '{"url":"https://changed.test"}'
            if change == "site_step":
                step.label = "changed"
                step.config_json = "{}"
        elif change.startswith("ancestor_"):
            root = session.get(Template, context.root_id)
            field = change.removeprefix("ancestor_")
            setattr(root, field, {"version": 2, "history_key": "replaced-root", "enabled": False}[field])
        elif change.startswith("feature_"):
            key = "flow.open" if change == "feature_defaults" else "flow.result"
            policy = {"defaults": {}, "required_fields": []}
            if change == "feature_defaults":
                # Explicit step values shadow this default; policy changes must still invalidate.
                policy["defaults"] = {"wait_until": "networkidle"}
            else:
                policy["execution_settings"] = {"llm_enabled": True}
            session.add(AppSetting(key=f"feature.{key}", value_json=json.dumps(policy)))
        else:
            session.get(SiteJob, run.jobs[0].id).lane = "interaction"
        session.commit()
    manager, cookies = _manager(context, monkeypatch)
    asyncio.run(manager._execute_job(run.id, run.jobs[0].id))
    _assert_changed(context, run)
    cookies.assert_not_called()
    manager._post_execute.assert_not_awaited()
    assert not manager.secret_providers.jobs
    assert not manager.secret_providers.plugin_jobs


def test_missing_guard_fails_before_credentials(context, monkeypatch):
    run = _create_run(context, guarded=False)
    manager, cookies = _manager(context, monkeypatch)
    asyncio.run(manager._execute_job(run.id, run.jobs[0].id))
    _assert_changed(context, run)
    cookies.assert_not_called()
    manager._post_execute.assert_not_awaited()


@pytest.mark.parametrize("trigger", ["assistant", "manual", "schedule"])
def test_unchanged_dispatch_and_non_assistant_without_guard(context, monkeypatch, trigger):
    run = _create_run(context, trigger=trigger, guarded=trigger == "assistant")
    manager, cookies = _manager(context, monkeypatch)
    asyncio.run(manager._execute_job(run.id, run.jobs[0].id))
    cookies.assert_called_once_with(context.site_id)
    manager._post_execute.assert_awaited_once()
    payload = manager._post_execute.call_args.args[0]
    assert payload["lane_key"] == run.jobs[0].lane == "general"
    with context.factory() as session:
        assert session.get(SiteJob, run.jobs[0].id).status == "success"
        assert session.get(Run, run.id).created_by == "human"


def test_change_during_payload_build_is_rechecked(context, monkeypatch):
    run = _create_run(context)
    manager, cookies = _manager(context, monkeypatch)

    def edit_during_cookie_resolution(_site_id):
        with context.factory() as session:
            session.get(Site, context.site_id).base_url = "https://changed.test"
            session.commit()

    cookies.side_effect = edit_during_cookie_resolution
    asyncio.run(manager._execute_job(run.id, run.jobs[0].id))
    _assert_changed(context, run)
    manager._post_execute.assert_not_awaited()
    assert not manager.secret_providers.jobs


def test_run_guard_and_pending_caller_changes_roll_back_together(context, monkeypatch):
    with context.factory() as session:
        commit = Mock(wraps=session.commit)
        monkeypatch.setattr(session, "commit", commit)
        session.add(AppSetting(key="caller.pending", value_json="true"))
        run = ExecutionService(session, context.settings, context.crypto).create_run(
            RunCreate(site_ids=[context.site_id]), "human", trigger="assistant", _commit=False,
        )
        assert run.jobs[0].id is not None
        create_guard(session, run.id, context.site_id,
                     WorkflowService(session, context.settings, context.crypto))
        session.flush()
        assert session.get(AssistantRunGuard, run.id) is not None
        commit.assert_not_called()
        session.rollback()
    with context.factory() as session:
        assert session.get(Run, run.id) is None
        assert session.get(SiteJob, run.jobs[0].id) is None
        assert session.get(AssistantRunGuard, run.id) is None
        assert session.get(AppSetting, "caller.pending") is None


def test_default_create_run_still_commits(context):
    with context.factory() as session:
        run = ExecutionService(session, context.settings, context.crypto).create_run(
            RunCreate(site_ids=[context.site_id]), "human",
        )
    with context.factory() as session:
        assert session.get(Run, run.id) is not None
        assert session.get(SiteJob, run.jobs[0].id) is not None


def test_guard_cascades_with_run(context):
    run = _create_run(context)
    with context.factory() as session:
        session.delete(session.get(Run, run.id))
        session.commit()
        assert session.get(AssistantRunGuard, run.id) is None


def test_create_guard_rejects_queue_lane_mismatch(context):
    run = _create_run(context, guarded=False)
    with context.factory() as session:
        session.get(Site, context.site_id).lane_policy = "interaction"
        session.flush()
        with pytest.raises(WorkflowValidationError):
            create_guard(session, run.id, context.site_id,
                         WorkflowService(session, context.settings, context.crypto))


def test_nonfinite_configuration_fails_closed(context):
    run = _create_run(context)
    with context.factory() as session:
        step = session.scalar(select(WorkflowStep).where(WorkflowStep.owner_type == "site"))
        step.config_json = '{"not_a_field":NaN}'
        session.commit()
        assert not validate_guard(session, run.id, session.get(Site, context.site_id),
                                  context.settings, context.crypto)


def test_feature_schema_change_invalidates_guard(context, monkeypatch):
    run = _create_run(context)
    original = FeatureSettingsService.view

    def changed_view(self, feature):
        view = original(self, feature)
        view.config_schema["properties"]["new_field"] = {"type": "string"}
        return view

    monkeypatch.setattr(FeatureSettingsService, "view", changed_view)
    with context.factory() as session:
        assert not validate_guard(session, run.id, session.get(Site, context.site_id),
                                  context.settings, context.crypto)


def test_secret_feature_default_is_hashed_and_key_order_is_canonical(context):
    secret = "private-token-in-feature-default"
    policy = {"defaults": {"url": f"https://example.test/?token={secret}", "retry_count": 3},
              "required_fields": []}
    with context.factory() as session:
        session.add(AppSetting(key="feature.flow.open", value_json=json.dumps(policy)))
        session.commit()
    run = _create_run(context)
    with context.factory() as session:
        guard = session.get(AssistantRunGuard, run.id)
        assert secret not in str({column.name: getattr(guard, column.name)
                                  for column in guard.__table__.columns})
        row = session.get(AppSetting, "feature.flow.open")
        row.value_json = json.dumps({"required_fields": [], "defaults": {
            "retry_count": 3, "url": f"https://example.test/?token={secret}",
        }})
        session.commit()
        site = session.get(Site, context.site_id)
        assert validate_guard(session, run.id, site, context.settings, context.crypto)
        policy["defaults"]["url"] = "https://example.test/?token=changed"
        row.value_json = json.dumps(policy)
        session.commit()
        assert not validate_guard(session, run.id, site, context.settings, context.crypto)


def test_notification_displays_assistant_without_changing_human_audit(context):
    run = _create_run(context)
    with context.factory() as session:
        notifications = NotificationService(session, context.settings, context.crypto)
        event = notifications._run_event(run.id)
        assert event["trigger_label"] == "assistant"
        assert event["created_by"] == "human"
        assert "assistant" in notifications._render(event)["body"]


@pytest.mark.parametrize("trigger,allow_disabled,allowed", [
    ("assistant", True, True), ("assistant", False, False),
    ("manual", True, False), ("manual", False, False),
    ("schedule", True, False), ("schedule", False, False),
])
def test_disabled_site_override_is_private_and_assistant_only(
    context, monkeypatch, trigger, allow_disabled, allowed,
):
    with context.factory() as session:
        session.get(Site, context.site_id).enabled = False
        session.commit()
    if not allowed:
        with pytest.raises(ExecutionValidationError):
            _create_run(context, trigger=trigger, guarded=False, allow_disabled=allow_disabled)
        return
    run = _create_run(context, allow_disabled=True)
    manager, _ = _manager(context, monkeypatch)
    asyncio.run(manager._execute_job(run.id, run.jobs[0].id))
    manager._post_execute.assert_awaited_once()
    with context.factory() as session:
        assert not session.get(Site, context.site_id).enabled
        scheduler = Scheduler(context.factory, context.settings, context.crypto, Mock())
        assert scheduler._eligible_sites(session, datetime.now(timezone.utc)) == []


def test_public_run_schema_rejects_private_override():
    with pytest.raises(ValidationError):
        RunCreate.model_validate({"site_ids": [1], "_allow_disabled": True})


@pytest.mark.parametrize("original,fallback", [
    ("general", "interaction"), ("proxy_general", "proxy_interaction"),
])
def test_executor_fallback_retry_keeps_guard_valid(context, monkeypatch, original, fallback):
    with context.factory() as session:
        session.get(Site, context.site_id).lane_policy = original
        session.commit()
    run = _create_run(context)
    manager, _ = _manager(context, monkeypatch)
    manager._post_execute.side_effect = [
        {"status": "fallback_interaction", "steps": []}, {"status": "success", "steps": []},
    ]

    async def execute():
        assert await manager._execute_job(run.id, run.jobs[0].id) == "retry"
        with context.factory() as session:
            assert validate_guard(session, run.id, session.get(Site, context.site_id),
                                  context.settings, context.crypto)
        await manager._execute_job(run.id, run.jobs[0].id)

    asyncio.run(execute())
    assert [call.args[0]["lane_key"] for call in manager._post_execute.call_args_list] == [
        original, fallback,
    ]
    with context.factory() as session:
        assert session.get(SiteJob, run.jobs[0].id).status == "success"


@pytest.mark.parametrize("original,lane,attempt", [
    ("general", "interaction", 1), ("proxy_general", "proxy_interaction", 1),
    ("general", "proxy_interaction", 2), ("proxy_general", "interaction", 2),
])
def test_direct_or_cross_proxy_fallback_is_rejected(context, original, lane, attempt):
    with context.factory() as session:
        session.get(Site, context.site_id).lane_policy = original
        session.commit()
    run = _create_run(context)
    with context.factory() as session:
        job = session.get(SiteJob, run.jobs[0].id)
        job.lane, job.attempt = lane, attempt
        session.commit()
        assert not validate_guard(session, run.id, session.get(Site, context.site_id),
                                  context.settings, context.crypto)


@pytest.mark.parametrize("change", ["lane", "feature_policy"])
def test_configuration_change_still_blocks_legitimate_fallback(context, monkeypatch, change):
    run = _create_run(context)
    manager, _ = _manager(context, monkeypatch)
    manager._post_execute.return_value = {"status": "fallback_interaction", "steps": []}

    async def execute():
        assert await manager._execute_job(run.id, run.jobs[0].id) == "retry"
        with context.factory() as session:
            if change == "lane":
                session.get(Site, context.site_id).lane_policy = "interaction"
            else:
                session.add(AppSetting(key="feature.flow.open", value_json=json.dumps({
                    "defaults": {"retry_count": 7}, "required_fields": [],
                })))
            session.commit()
        await manager._execute_job(run.id, run.jobs[0].id)

    asyncio.run(execute())
    _assert_changed(context, run)
    manager._post_execute.assert_awaited_once()


@pytest.mark.parametrize("attempt,status", [(2, "queued"), (1, "running")])
def test_create_guard_requires_initial_queued_job(context, attempt, status):
    run = _create_run(context, guarded=False)
    with context.factory() as session:
        job = session.get(SiteJob, run.jobs[0].id)
        job.attempt, job.status = attempt, status
        session.flush()
        with pytest.raises(WorkflowValidationError):
            create_guard(session, run.id, context.site_id,
                         WorkflowService(session, context.settings, context.crypto))


@pytest.mark.parametrize("trigger", ["assistant", "manual", "schedule"])
@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectError, ValueError])
def test_worker_response_loss_is_uncertain_only_for_assistant(context, monkeypatch, trigger, error):
    run = _create_run(context, trigger=trigger, guarded=trigger == "assistant")
    manager, _ = _manager(context, monkeypatch)
    monkeypatch.setattr(manager, "schedule_run_notification", Mock())
    manager._post_execute.side_effect = error("private response details")
    asyncio.run(manager._execute_run(run.id))
    with context.factory() as session:
        job = session.get(SiteJob, run.jobs[0].id)
        if trigger == "assistant":
            assert job.status == session.get(Run, run.id).status == "unknown"
            assert json.loads(job.result_json) == {"reason": "assistant_outcome_uncertain"}
            assert "private response details" not in job.message
            assert unresolved_assistant_runs(session, context.site_id) == [run.id]
        else:
            assert job.status == "error"
            assert session.get(Run, run.id).status == "fail"
            assert unresolved_assistant_runs(session, context.site_id) == []
    manager._post_execute.assert_awaited_once()
    assert not manager.secret_providers.jobs
    assert not manager.secret_providers.plugin_jobs


@pytest.mark.parametrize("run_status,job_status,marker,blocked", [
    ("unknown", "unknown", None, True), ("interrupted", "interrupted", None, True),
    ("waiting_human", "waiting_human", None, True), ("fail", "error", None, True),
    ("fail", "fail", "assistant_outcome_uncertain", True),
    ("fail", "fail", "outcome_uncertain", True),
    ("fail", "fail", "assistant_configuration_changed", False),
    ("success", "success", None, False), ("canceled", "canceled", None, False),
])
def test_unresolved_runs_block_scheduler_across_days_but_not_explicit_manual(
    context, run_status, job_status, marker, blocked,
):
    run = _create_run(context)
    now = datetime.now(timezone.utc) + timedelta(days=10)
    with context.factory() as session:
        row = session.get(Run, run.id)
        row.status = run_status
        job = session.get(SiteJob, run.jobs[0].id)
        job.status = job_status
        job.finished_at = now - timedelta(days=5)
        if marker:
            session.add(JobStep(site_job_id=job.id, step_key="action", feature_type="sign.button",
                                status="fail", data_json=json.dumps({"reason": marker})))
        session.commit()
        assert unresolved_assistant_runs(session, context.site_id) == ([run.id] if blocked else [])
        service = ExecutionService(session, context.settings, context.crypto)
        assert (context.site_id in service.today_excluded_site_ids(now)) == blocked
        scheduler = Scheduler(context.factory, context.settings, context.crypto, Mock())
        assert (context.site_id not in scheduler._eligible_sites(session, now)) == blocked
        # Explicit human execution is distinct from automatic scheduler selection.
        manual = service.create_run(RunCreate(site_ids=[context.site_id]), "human")
        assert manual.trigger == "manual"


def test_unresolved_helper_scopes_site_trigger_and_guard(context):
    guarded = _create_run(context)
    unguarded = _create_run(context, guarded=False)
    manual = _create_run(context, trigger="manual")
    with context.factory() as session:
        for run in (guarded, unguarded, manual):
            session.get(Run, run.id).status = "unknown"
            session.get(SiteJob, run.jobs[0].id).status = "unknown"
        session.commit()
        assert unresolved_assistant_runs(session, context.site_id) == [guarded.id]
        assert unresolved_assistant_runs(session, context.site_id + 1) == []
