from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.main import create_app
from app.models.assistant import AssistantMessage, AssistantSession
from app.models.assistant_takeover import AssistantTakeoverAction
from app.models.assistant_takeover_conclusion import AssistantTakeoverConclusion
from app.models.assistant_takeover_dedup import (
    TakeoverAnalysisProvenance, TakeoverDedupLink, TakeoverFailureSignature,
)
from app.models.execution import JobStep, Run, SiteJob
from app.models.setting import AppSetting
from app.models.workflow import Site, Template
from app.schemas.assistant_takeover import TakeoverActionsView
from app.services.assistant_takeover import AssistantTakeover, _source_current, activate_takeover
from app.services.assistant_takeover_conclusions import (
    finalize_takeover_conclusion, notification_conclusions, public_conclusion,
)
from app.services.assistant_takeover_dedup import VERSION, failure_hash, mark_analysis_provenance
from app.services.config_ai_generation import _json
from app.services.feature_settings import FeatureSettingsService
from app.services.run_notification_dispatch import TERMINAL_ACTION_STATUSES, plan_run_takeovers
from tests.test_assistant_takeover import OWNER, ctx, failed_job, grant  # noqa: F401


MESSAGE = "\u9009\u62e9\u5668\u672a\u627e\u5230"
CONCLUSION = "The configured selector no longer matches. Review before retrying."


def failure(ctx, *, planner="reconcile", message=MESSAGE, data=None, step_key="login",
            feature="auth.login", status="fail", lane=None, snapshot=True, steps=True):
    takeover, manager, _ = ctx
    job_id, run_id = failed_job(ctx, snapshot=snapshot)
    with manager.session_factory() as db:
        job = db.get(SiteJob, job_id)
        if lane:
            # Capture is separately validated against the actual dispatch lane.
            job.lane = lane
        if steps:
            db.add(JobStep(site_job_id=job_id, step_key=step_key, feature_type=feature,
                           status=status, message=message, data_json=_json(
                               {"reason": "selector_not_found", "field": "username_selector"}
                               if data is None else data)))
        else:
            job.message, job.result_json = message, _json(data or {})
        db.commit()
    if planner == "reconcile":
        takeover.reconcile()
    else:
        with manager.session_factory() as db:
            plan_run_takeovers(db, run_id)
            db.commit()
    with manager.session_factory() as db:
        action = db.scalar(select(AssistantTakeoverAction).where(
            AssistantTakeoverAction.source_job_id == job_id))
        return action.id, job_id, run_id


def complete(ctx, identifier, *, status="completed_analysis", outcome=None):
    _, manager, _ = ctx
    sid = manager.create_takeover(identifier)
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, identifier)
        mark_analysis_provenance(db, manager, action, source_current=_source_current(db, manager, action))
        action.status, action.attempts, action.request_id = status, 1, str(uuid4())
        if status == "updated":
            action.import_receipt_json = _json({"type": "workflow_import", "result": {
                "ok": True, "site_id": action.site_id, "operation": "updated",
                "history_key": action.site_history_key, "config_revision": 2,
            }})
        manager._message(db, sid, "user", "Analyze", request_id=action.request_id)
        manager._message(db, sid, "assistant", CONCLUSION)
        db.flush()
        assert finalize_takeover_conclusion(manager, db, action, action.request_id,
                                            {"ok": True} if outcome is None else outcome)
        db.commit()
    return sid


def no_runtime(ctx, monkeypatch):
    _, manager, _ = ctx
    runtime = Mock(side_effect=AssertionError("runtime must not resolve"))
    monkeypatch.setattr(manager, "_runtime", runtime)
    monkeypatch.setattr(manager, "create_takeover", Mock(side_effect=AssertionError("no new chat")))
    monkeypatch.setattr(manager, "send_takeover", AsyncMock(side_effect=AssertionError("no model")))
    return runtime


@pytest.mark.parametrize("planner", ["reconcile", "notification"])
@pytest.mark.parametrize("source_status", ["completed_analysis", "updated"])
def test_repeat_is_durable_audit_not_model_analysis(ctx, monkeypatch, planner, source_status):
    grant(ctx)
    source, _, first_run = failure(ctx)
    sid = complete(ctx, source, status=source_status)
    current, job_id, run_id = failure(ctx, planner=planner)
    runtime = no_runtime(ctx, monkeypatch)
    asyncio.run(ctx[0].process_one())
    runtime.assert_not_called()
    ctx[1].send_takeover.assert_not_awaited()
    with ctx[1].session_factory() as db:
        action = db.get(AssistantTakeoverAction, current)
        assert action.status == "reused" and action.reason == "same_config_same_failure"
        assert action.attempts == 0 and action.request_id is None
        assert action.session_id == sid and action.import_receipt_json is None
        assert db.get(AssistantTakeoverConclusion, current) is None
        assert db.get(TakeoverDedupLink, current).source_action_id == source
        text = public_conclusion(ctx[1], db, action)
        assert text.startswith(f"\u590d\u7528\u6b64\u524d\u5206\u6790\uff08\u4efb\u52a1 #{first_run}\uff09\uff1a")
        assert "\u5f53\u524d\u672a\u91cd\u65b0\u5206\u6790/\u672a\u4fee\u590d" in text
        assert len(text) <= 300
        assert notification_conclusions(db, ctx[1].crypto, run_id)[job_id]["message"] == text
        assert db.get(SiteJob, job_id).status == db.get(Run, run_id).status == "fail"
        assert db.scalar(select(func.count()).select_from(AssistantMessage)) == 2
    assert "reused" in TERMINAL_ACTION_STATUSES
    TakeoverActionsView.model_validate(ctx[0].actions(OWNER))


def test_survives_app_restart_and_deleted_chat_without_resurrection(ctx, monkeypatch):
    grant(ctx)
    source, _, _ = failure(ctx)
    sid = complete(ctx, source)
    asyncio.run(ctx[1].delete(OWNER, sid))
    current, _, _ = failure(ctx)
    manager = create_app(ctx[1].settings).state.assistant_manager
    restarted = AssistantTakeover(manager)
    runtime = no_runtime((restarted, manager, ctx[2]), monkeypatch)
    asyncio.run(restarted.process_one())
    runtime.assert_not_called()
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, current)
        assert action.status == "reused" and action.session_id is None
        assert public_conclusion(manager, db, action)
        assert db.scalar(select(func.count()).select_from(AssistantSession)) == 0


@pytest.mark.parametrize("days_later", [1, 7, 31, 366])
@pytest.mark.parametrize("planner", ["reconcile", "notification"])
def test_previous_days_analysis_reuses_only_within_default_ttl(ctx, monkeypatch, days_later, planner):
    from app.models.assistant_takeover import DailyConversation

    grant(ctx)
    source, _, _ = failure(ctx)
    sid = complete(ctx, source)
    with ctx[1].session_factory() as db:
        anchor = db.get(AssistantTakeoverConclusion, source).created_at.replace(tzinfo=timezone.utc)
        message_count = db.scalar(select(func.count()).select_from(AssistantMessage))
        daily_count = db.scalar(select(func.count()).select_from(DailyConversation))

    current, job_id, run_id = failure(ctx, planner=planner)
    with ctx[1].session_factory() as db:
        later = anchor + timedelta(days=days_later)
        run, job = db.get(Run, run_id), db.get(SiteJob, job_id)
        run.created_at = run.started_at = later
        run.finished_at = job.finished_at = later + timedelta(seconds=1)
        job.started_at = later
        db.get(AssistantTakeoverAction, current).created_at = later
        db.commit()
    monkeypatch.setattr("app.services.assistant_takeover._now", lambda: later)

    # A new day could route to a new chat, but reuse must happen before routing,
    # model configuration lookup, or any live-browser operation.
    runtime = no_runtime(ctx, monkeypatch)
    worker = AsyncMock(side_effect=AssertionError("no repeated browser execution"))
    monkeypatch.setattr(ctx[1], "_worker", worker)
    asyncio.run(ctx[0].process_one())
    if days_later >= 7:
        runtime.assert_called_once()
        with ctx[1].session_factory() as db:
            assert db.get(AssistantTakeoverAction, current).status == "queued"
            assert db.get(TakeoverDedupLink, current) is None
            assert notification_conclusions(db, ctx[1].crypto, run_id) == {}
        return
    runtime.assert_not_called()
    ctx[1].create_takeover.assert_not_called()
    ctx[1].send_takeover.assert_not_awaited()
    worker.assert_not_awaited()
    with ctx[1].session_factory() as db:
        action = db.get(AssistantTakeoverAction, current)
        assert action.status == "reused" and action.attempts == 0
        assert action.session_id == sid and action.request_id is None
        assert db.get(TakeoverDedupLink, current).source_action_id == source
        assert db.scalar(select(func.count()).select_from(AssistantMessage)) == message_count
        assert db.scalar(select(func.count()).select_from(DailyConversation)) == daily_count
        assert notification_conclusions(db, ctx[1].crypto, run_id)[job_id]["message"]


@pytest.mark.parametrize("ttl_days", [1, 7, 30])
@pytest.mark.parametrize("age", ["fresh", "before_boundary", "boundary", "expired", "future"])
def test_original_conclusion_rolling_ttl(ctx, monkeypatch, ttl_days, age):
    grant(ctx, conclusion_ttl_days=ttl_days)
    source, _, _ = failure(ctx)
    complete(ctx, source)
    current, _, _ = failure(ctx)
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    created = {
        "fresh": now,
        "before_boundary": now - timedelta(days=ttl_days) + timedelta(microseconds=1),
        "boundary": now - timedelta(days=ttl_days),
        "expired": now - timedelta(days=ttl_days, microseconds=1),
        "future": now + timedelta(microseconds=1),
    }[age]
    with ctx[1].session_factory() as db:
        db.get(AssistantTakeoverConclusion, source).created_at = created
        # Recent action activity cannot refresh an old conclusion.
        db.get(AssistantTakeoverAction, source).updated_at = now
        db.commit()
    monkeypatch.setattr("app.services.assistant_takeover._now", lambda: now)
    runtime = no_runtime(ctx, monkeypatch)
    asyncio.run(ctx[0].process_one())
    expected = age in {"fresh", "before_boundary"}
    assert runtime.call_count == (0 if expected else 1)
    with ctx[1].session_factory() as db:
        action = db.get(AssistantTakeoverAction, current)
        assert action.status == ("reused" if expected else "queued")
        assert (db.get(TakeoverDedupLink, current) is not None) is expected
        assert db.get(AssistantTakeoverConclusion, source).created_at.replace(tzinfo=timezone.utc) == created
        assert public_conclusion(ctx[1], db, db.get(AssistantTakeoverAction, source))


def test_recent_conclusion_from_old_source_job_is_reusable(ctx, monkeypatch):
    grant(ctx)
    source, source_job, source_run = failure(ctx)
    complete(ctx, source)
    with ctx[1].session_factory() as db:
        old = datetime.now(timezone.utc) - timedelta(days=366)
        db.get(SiteJob, source_job).finished_at = old
        db.get(Run, source_run).finished_at = old
        db.get(AssistantTakeoverAction, source).created_at = old
        db.commit()
    current, _, _ = failure(ctx)
    runtime = no_runtime(ctx, monkeypatch)
    asyncio.run(ctx[0].process_one())
    runtime.assert_not_called()
    with ctx[1].session_factory() as db:
        assert db.get(AssistantTakeoverAction, current).status == "reused"


def test_reuse_does_not_reset_original_age_or_expire_history(ctx, monkeypatch):
    grant(ctx)
    source, source_job, source_run = failure(ctx)
    complete(ctx, source)
    with ctx[1].session_factory() as db:
        created = db.get(AssistantTakeoverConclusion, source).created_at.replace(tzinfo=timezone.utc)
    day6 = created + timedelta(days=6)
    monkeypatch.setattr("app.services.assistant_takeover._now", lambda: day6)
    first, first_job, first_run = failure(ctx)
    runtime = no_runtime(ctx, monkeypatch)
    asyncio.run(ctx[0].process_one())
    with ctx[1].session_factory() as db:
        first_text = public_conclusion(ctx[1], db, db.get(AssistantTakeoverAction, first))
        snapshot = notification_conclusions(db, ctx[1].crypto, first_run, completed_before=day6)
        assert first_text and snapshot[first_job]["message"] == first_text
        assert db.get(TakeoverDedupLink, first).created_at.replace(tzinfo=timezone.utc) == day6

    day8 = created + timedelta(days=8)
    monkeypatch.setattr("app.services.assistant_takeover._now", lambda: day8)
    current, _, _ = failure(ctx)
    asyncio.run(ctx[0].process_one())
    runtime.assert_called_once()
    ctx[0].reconcile()
    with ctx[1].session_factory() as db:
        assert db.get(AssistantTakeoverAction, current).status == "queued"
        assert db.get(TakeoverDedupLink, current) is None
        assert db.get(AssistantTakeoverAction, first).status == "reused"
        assert db.get(TakeoverDedupLink, first).source_action_id == source
        assert public_conclusion(ctx[1], db, db.get(AssistantTakeoverAction, first)) == first_text
        assert notification_conclusions(db, ctx[1].crypto, first_run, completed_before=day6) == snapshot
        assert notification_conclusions(db, ctx[1].crypto, source_run)[source_job]["message"]
        assert db.get(AssistantTakeoverConclusion, source).created_at.replace(tzinfo=timezone.utc) == created
        assert db.scalar(select(func.count()).select_from(AssistantTakeoverAction)) == 3


def test_current_policy_ttl_applies_only_to_next_reuse_decisions(ctx, monkeypatch):
    grant(ctx, conclusion_ttl_days=30)
    source, _, _ = failure(ctx)
    complete(ctx, source)
    with ctx[1].session_factory() as db:
        db.get(AssistantTakeoverConclusion, source).created_at = datetime.now(timezone.utc) - timedelta(days=8)
        db.commit()
    first, _, _ = failure(ctx)
    runtime = no_runtime(ctx, monkeypatch)
    asyncio.run(ctx[0].process_one())
    runtime.assert_not_called()
    grant(ctx, conclusion_ttl_days=7)
    current, _, _ = failure(ctx)
    asyncio.run(ctx[0].process_one())
    runtime.assert_called_once()
    with ctx[1].session_factory() as db:
        assert db.get(AssistantTakeoverAction, current).status == "queued"
        assert db.get(AssistantTakeoverAction, first).status == "reused"
        assert public_conclusion(ctx[1], db, db.get(AssistantTakeoverAction, first))


def test_new_failure_after_expiry_completes_fresh_analysis(ctx, monkeypatch):
    grant(ctx)
    source, _, _ = failure(ctx)
    complete(ctx, source)
    with ctx[1].session_factory() as db:
        db.get(AssistantTakeoverConclusion, source).created_at = datetime.now(timezone.utc) - timedelta(days=8)
        db.commit()
    current, _, _ = failure(ctx)
    monkeypatch.setattr(ctx[1], "_runtime", Mock(return_value={}))

    async def send(owner, sid, payload, *, action_id):
        with ctx[1].session_factory() as db:
            conversation = db.get(AssistantSession, sid)
            request_id = str(payload.request_id)
            activate_takeover(ctx[1], db, conversation, action_id, request_id)
            ctx[1]._message(db, sid, "user", payload.content, request_id=request_id)
            ctx[1]._message(db, sid, "assistant", "Fresh investigation confirms the selector still needs review.")
            data = ctx[1]._decrypt(conversation)
            data["_turn_outcomes"] = {request_id: {"ok": True}}
            ctx[1]._encrypt(conversation, data)
            db.commit()

    monkeypatch.setattr(ctx[1], "send_takeover", AsyncMock(side_effect=send))
    asyncio.run(ctx[0].process_one())
    ctx[1].send_takeover.assert_awaited_once()
    with ctx[1].session_factory() as db:
        action = db.get(AssistantTakeoverAction, current)
        assert action.status == "completed_analysis" and action.attempts == 1
        assert db.get(TakeoverDedupLink, current) is None
        assert db.get(AssistantTakeoverConclusion, current) is not None
        assert "Fresh investigation" in public_conclusion(ctx[1], db, action)
        assert public_conclusion(ctx[1], db, db.get(AssistantTakeoverAction, source))


def test_two_queued_failures_recheck_after_first_completes(ctx, monkeypatch):
    grant(ctx)
    source, _, _ = failure(ctx)
    current, _, _ = failure(ctx)
    complete(ctx, source)
    runtime = no_runtime(ctx, monkeypatch)
    asyncio.run(ctx[0].process_one())
    runtime.assert_not_called()
    with ctx[1].session_factory() as db:
        assert db.get(AssistantTakeoverAction, current).status == "reused"


@pytest.mark.parametrize("change", ["revision", "parent", "policy", "schema", "config"])
def test_configuration_version_or_dependency_changes_analyze_again(ctx, monkeypatch, change):
    grant(ctx)
    source, _, _ = failure(ctx)
    complete(ctx, source)
    if change == "schema":
        original = FeatureSettingsService.view

        def changed(self, feature):
            view = original(self, feature)
            view.config_schema["properties"]["new_field"] = {"type": "string"}
            return view

        monkeypatch.setattr(FeatureSettingsService, "view", changed)
    else:
        with ctx[1].session_factory() as db:
            site = db.get(Site, ctx[2])
            if change == "revision":
                site.config_revision += 1
            elif change == "parent":
                db.get(Template, site.template_id).version += 1
            elif change == "policy":
                db.add(AppSetting(key="feature.flow.open", value_json=_json({
                    "defaults": {}, "required_fields": ["url"]})))
            else:
                site.name = "Changed configuration"
            db.commit()
    current, _, _ = failure(ctx)
    runtime = no_runtime(ctx, monkeypatch)
    asyncio.run(ctx[0].process_one())
    runtime.assert_called_once()
    with ctx[1].session_factory() as db:
        assert db.get(AssistantTakeoverAction, current).status == "queued"


@pytest.mark.parametrize("changes", [
    {"step_key": "password"}, {"feature": "action.click"}, {"status": "error"},
    {"message": "A different selector was not found"},
    {"data": {"reason": "selector_not_found", "field": "password_selector"}},
    {"data": {"reason": "success_evidence_missing", "field": "username_selector"}},
    {"data": {"reason": "selector_not_found", "field": "username_selector", "stage": "login"}},
    {"data": {"reason": "selector_not_found", "field": "username_selector", "cause": "missing"}},
    {"data": {"reason": "selector_not_found", "field_path": "config.private_selector"}},
    {"snapshot": False}, {"lane": "proxy_interaction"},
])
def test_different_failure_or_missing_binding_never_reuses(ctx, monkeypatch, changes):
    grant(ctx)
    source, _, _ = failure(ctx)
    complete(ctx, source)
    current, _, _ = failure(ctx, **changes)
    runtime = no_runtime(ctx, monkeypatch)
    asyncio.run(ctx[0].process_one())
    runtime.assert_called_once()
    with ctx[1].session_factory() as db:
        assert db.get(AssistantTakeoverAction, current).status == "queued"


def test_volatile_metadata_ignored_and_only_hash_persisted(ctx, monkeypatch):
    grant(ctx)
    source, _, _ = failure(ctx, data={
        "reason": "selector_not_found", "field": "private-selector-value", "timestamp": "old",
        "duration_ms": 19, "url": "https://old.test/", "screenshot_hash": "old", "count": 1})
    complete(ctx, source)
    current, job_id, _ = failure(ctx, message="  " + MESSAGE + "  ", data={
        "reason": "selector_not_found", "field": "private-selector-value", "timestamp": "new",
        "duration_ms": 87, "url": "https://new.test/", "screenshot_hash": "new", "count": 8})
    runtime = no_runtime(ctx, monkeypatch)
    asyncio.run(ctx[0].process_one())
    runtime.assert_not_called()
    with ctx[1].session_factory() as db:
        row = db.get(TakeoverFailureSignature, job_id)
        assert len(row.failure_hash) == 64 and row.version == VERSION
        assert "private-selector-value" not in str(row.__dict__)
        assert db.get(AssistantTakeoverAction, current).status == "reused"


@pytest.mark.parametrize("data,message", [
    ({}, ""), ({"stage": "open"}, ""), ({"reason": "execution_error"}, ""),
    ({"reason": "network_error", "stage": "transport"}, ""),
])
def test_unidentified_or_generic_failure_has_no_signature(ctx, data, message):
    grant(ctx)
    _, job_id, _ = failure(ctx, steps=False, data=data, message=message)
    with ctx[1].session_factory() as db:
        assert failure_hash(db, db.get(SiteJob, job_id)) is None
        assert db.get(TakeoverFailureSignature, job_id) is None


def test_legacy_message_only_and_exact_transport_messages(ctx, monkeypatch):
    grant(ctx)
    source, _, _ = failure(ctx, steps=False, message="Connection failed: private host A")
    complete(ctx, source)
    current, _, _ = failure(ctx, steps=False, message="Connection failed: private host A")
    runtime = no_runtime(ctx, monkeypatch)
    asyncio.run(ctx[0].process_one())
    runtime.assert_not_called()
    different, _, _ = failure(ctx, steps=False, message="Connection failed: private host B")
    asyncio.run(ctx[0].process_one())
    runtime.assert_called_once()
    with ctx[1].session_factory() as db:
        assert db.get(AssistantTakeoverAction, current).status == "reused"
        assert db.get(AssistantTakeoverAction, different).status == "queued"


@pytest.mark.parametrize("invalid", ["failed", "unknown", "revoked", "queued", "canceled",
                                     "missing", "corrupt", "false_outcome"])
def test_requires_positive_encrypted_completed_proof(ctx, monkeypatch, invalid):
    grant(ctx)
    source, _, _ = failure(ctx)
    complete(ctx, source, status="updated" if invalid == "false_outcome" else "completed_analysis",
             outcome={"ok": False, "code": "provider_error"} if invalid == "false_outcome" else None)
    with ctx[1].session_factory() as db:
        action = db.get(AssistantTakeoverAction, source)
        if invalid == "missing":
            db.delete(db.get(AssistantTakeoverConclusion, source))
        elif invalid == "corrupt":
            db.get(AssistantTakeoverConclusion, source).ciphertext = b"corrupt"
        elif invalid != "false_outcome":
            action.status = invalid
        db.commit()
    current, _, _ = failure(ctx)
    # A queued earlier action can be selected first, but neither may be reused.
    runtime = no_runtime(ctx, monkeypatch)
    asyncio.run(ctx[0].process_one())
    runtime.assert_called_once()
    with ctx[1].session_factory() as db:
        assert db.get(AssistantTakeoverAction, current).status == "queued"
        assert db.get(TakeoverDedupLink, current) is None


@pytest.mark.parametrize("change", ["owner", "history", "origin", "version"])
def test_scope_and_signature_version_boundaries(ctx, monkeypatch, change):
    grant(ctx)
    source, job_id, _ = failure(ctx)
    complete(ctx, source)
    with ctx[1].session_factory() as db:
        row = db.get(TakeoverFailureSignature, job_id)
        field = {"owner": "owner", "history": "site_history_key", "origin": "site_origin",
                 "version": "version"}[change]
        setattr(row, field, VERSION + 1 if change == "version" else "other")
        db.commit()
    failure(ctx)
    runtime = no_runtime(ctx, monkeypatch)
    asyncio.run(ctx[0].process_one())
    runtime.assert_called_once()


def test_mode_change_does_not_reanalyze_identical_version(ctx, monkeypatch):
    grant(ctx)
    source, _, _ = failure(ctx)
    complete(ctx, source)
    grant(ctx, mode="autofix")
    current, _, _ = failure(ctx)
    runtime = no_runtime(ctx, monkeypatch)
    asyncio.run(ctx[0].process_one())
    runtime.assert_not_called()
    with ctx[1].session_factory() as db:
        action = db.get(AssistantTakeoverAction, current)
        assert action.mode == "autofix" and action.status == "reused"


def test_current_uncertainty_does_not_block_harmless_proven_reuse(ctx, monkeypatch):
    grant(ctx)
    source, _, _ = failure(ctx)
    complete(ctx, source)
    current, job_id, _ = failure(ctx)
    with ctx[1].session_factory() as db:
        db.get(SiteJob, job_id).result_json = _json({"outcome_uncertain": True})
        db.commit()
    runtime = no_runtime(ctx, monkeypatch)
    asyncio.run(ctx[0].process_one())
    runtime.assert_not_called()
    with ctx[1].session_factory() as db:
        assert db.get(AssistantTakeoverAction, current).status == "reused"
        assert db.get(TakeoverDedupLink, current).source_action_id == source


def test_notification_cutoff_is_reuse_time_and_revalidates_source(ctx, monkeypatch):
    grant(ctx)
    source, _, _ = failure(ctx)
    complete(ctx, source)
    current, job_id, run_id = failure(ctx)
    before = datetime.now(timezone.utc) - timedelta(seconds=1)
    no_runtime(ctx, monkeypatch)
    asyncio.run(ctx[0].process_one())
    with ctx[1].session_factory() as db:
        link = db.get(TakeoverDedupLink, current)
        assert notification_conclusions(db, ctx[1].crypto, run_id, completed_before=before) == {}
        assert job_id in notification_conclusions(db, ctx[1].crypto, run_id,
                                                 completed_before=link.created_at)
        db.get(AssistantTakeoverConclusion, source).ciphertext = b"broken"
        db.flush()
        assert notification_conclusions(db, ctx[1].crypto, run_id) == {}
        assert public_conclusion(ctx[1], db, db.get(AssistantTakeoverAction, current)) is None


def test_all_failed_steps_sorted_and_distinguished(ctx):
    grant(ctx)
    _, first, _ = failure(ctx)
    _, second, _ = failure(ctx)
    with ctx[1].session_factory() as db:
        for identifier in (second, first):
            db.add(JobStep(site_job_id=identifier, step_key="another", feature_type="action.click",
                           status="fail", message="Missing", data_json='{"reason":"selector_not_found"}'))
        db.flush()
        left = failure_hash(db, db.get(SiteJob, first))
        assert left == failure_hash(db, db.get(SiteJob, second))
        db.scalar(select(JobStep).where(JobStep.site_job_id == second,
                                       JobStep.step_key == "another")).message = "Different"
        db.flush()
        assert left != failure_hash(db, db.get(SiteJob, second))


def test_stale_current_config_cannot_reuse_old_dispatch_binding(ctx, monkeypatch):
    grant(ctx)
    source, _, _ = failure(ctx)
    complete(ctx, source)
    current, _, _ = failure(ctx)
    with ctx[1].session_factory() as db:
        db.get(Site, ctx[2]).config_revision += 1
        db.commit()
    runtime = no_runtime(ctx, monkeypatch)
    asyncio.run(ctx[0].process_one())
    runtime.assert_called_once()
    with ctx[1].session_factory() as db:
        action = db.get(AssistantTakeoverAction, current)
        assert action.status == "queued" and action.reason == "source_config_stale"


def test_failure_changed_after_planning_cannot_match_stale_signature(ctx, monkeypatch):
    grant(ctx)
    source, _, _ = failure(ctx)
    complete(ctx, source)
    current, job_id, _ = failure(ctx)
    with ctx[1].session_factory() as db:
        db.scalar(select(JobStep).where(JobStep.site_job_id == job_id)).message = "Different failure"
        db.commit()
    runtime = no_runtime(ctx, monkeypatch)
    asyncio.run(ctx[0].process_one())
    runtime.assert_called_once()
    with ctx[1].session_factory() as db:
        assert db.get(AssistantTakeoverAction, current).status == "queued"


def test_later_repeats_link_directly_to_positive_original(ctx, monkeypatch):
    grant(ctx)
    source, _, _ = failure(ctx)
    complete(ctx, source)
    first, _, _ = failure(ctx)
    no_runtime(ctx, monkeypatch)
    asyncio.run(ctx[0].process_one())
    second, _, _ = failure(ctx)
    asyncio.run(ctx[0].process_one())
    with ctx[1].session_factory() as db:
        assert db.get(TakeoverDedupLink, first).source_action_id == source
        assert db.get(TakeoverDedupLink, second).source_action_id == source
        assert db.scalar(select(func.count()).select_from(AssistantTakeoverConclusion)) == 1


def test_rechecks_after_runtime_before_claim(ctx, monkeypatch):
    grant(ctx)
    source, _, _ = failure(ctx)
    complete(ctx, source)
    with ctx[1].session_factory() as db:
        db.get(AssistantTakeoverAction, source).status = "dispatched"
        db.commit()
    current, _, _ = failure(ctx)

    def runtime():
        with ctx[1].session_factory() as db:
            db.get(AssistantTakeoverAction, source).status = "completed_analysis"
            db.commit()

    monkeypatch.setattr(ctx[1], "_runtime", Mock(side_effect=runtime))
    monkeypatch.setattr(ctx[1], "send_takeover", AsyncMock())
    asyncio.run(ctx[0].process_one())
    ctx[1].send_takeover.assert_not_awaited()
    with ctx[1].session_factory() as db:
        action = db.get(AssistantTakeoverAction, current)
        assert action.status == "reused" and action.attempts == 0 and action.request_id is None


def test_post_import_revision_is_not_previous_failure_version(ctx, monkeypatch):
    grant(ctx, mode="autofix")
    source, _, _ = failure(ctx)
    complete(ctx, source, status="updated")
    with ctx[1].session_factory() as db:
        db.get(Site, ctx[2]).config_revision = 2
        db.commit()
    failure(ctx)
    runtime = no_runtime(ctx, monkeypatch)
    asyncio.run(ctx[0].process_one())
    runtime.assert_called_once()


def test_newer_failed_analysis_does_not_hide_positive_original(ctx, monkeypatch):
    grant(ctx)
    source, _, _ = failure(ctx)
    complete(ctx, source)
    failed, _, _ = failure(ctx)
    with ctx[1].session_factory() as db:
        db.get(AssistantTakeoverAction, failed).status = "failed"
        db.commit()
    current, _, _ = failure(ctx)
    runtime = no_runtime(ctx, monkeypatch)
    asyncio.run(ctx[0].process_one())
    runtime.assert_not_called()
    with ctx[1].session_factory() as db:
        assert db.get(TakeoverDedupLink, current).source_action_id == source


def test_structured_step_reason_without_message_is_identifying(ctx, monkeypatch):
    grant(ctx)
    source, _, _ = failure(ctx, message="")
    complete(ctx, source)
    current, _, _ = failure(ctx, message="")
    runtime = no_runtime(ctx, monkeypatch)
    asyncio.run(ctx[0].process_one())
    runtime.assert_not_called()
    with ctx[1].session_factory() as db:
        assert db.get(AssistantTakeoverAction, current).status == "reused"


def test_message_only_step_supports_common_legacy_selector_failure(ctx, monkeypatch):
    grant(ctx)
    source, _, _ = failure(ctx, data={})
    complete(ctx, source)
    current, _, _ = failure(ctx, data={})
    runtime = no_runtime(ctx, monkeypatch)
    asyncio.run(ctx[0].process_one())
    runtime.assert_not_called()
    with ctx[1].session_factory() as db:
        assert db.get(AssistantTakeoverAction, current).status == "reused"


@pytest.mark.parametrize("difference", ["terminal_message", "terminal_reason", "result_key",
                                        "result_message", "result_feature"])
def test_continued_step_failure_does_not_hide_actual_terminal_cause(ctx, monkeypatch, difference):
    grant(ctx)

    def terminal_failure(second):
        job_id, run_id = failed_job(ctx)
        with ctx[1].session_factory() as db:
            job = db.get(SiteJob, job_id)
            job.message = ("insufficient balance" if second and difference == "terminal_message"
                           else "execution failed")
            job.result_json = _json({"reason": (
                "insufficient_balance" if second and difference == "terminal_reason" else "account_disabled")})
            db.add(JobStep(site_job_id=job_id, step_key="optional", feature_type="action.click",
                           status="fail", message="Selector missing",
                           data_json='{"reason":"selector_not_found"}'))
            db.add(JobStep(
                site_job_id=job_id,
                step_key="insufficient_balance" if second and difference == "result_key" else "account_disabled",
                feature_type="action.result" if second and difference == "result_feature" else "flow.result",
                status="success", message="execution failed", data_json=_json({
                    "value": False, "message": (
                        "insufficient balance" if second and difference == "result_message" else "account disabled"),
                })))
            db.commit()
        ctx[0].reconcile()
        with ctx[1].session_factory() as db:
            return db.scalar(select(AssistantTakeoverAction.id).where(
                AssistantTakeoverAction.source_job_id == job_id)), job_id

    source, first_job = terminal_failure(False)
    complete(ctx, source)
    current, second_job = terminal_failure(True)
    with ctx[1].session_factory() as db:
        assert failure_hash(db, db.get(SiteJob, first_job)) != failure_hash(db, db.get(SiteJob, second_job))
    runtime = no_runtime(ctx, monkeypatch)
    asyncio.run(ctx[0].process_one())
    runtime.assert_called_once()
    with ctx[1].session_factory() as db:
        assert db.get(AssistantTakeoverAction, current).status == "queued"


def test_successful_false_result_is_identifying_without_failed_steps(ctx):
    grant(ctx)
    job_id, _ = failed_job(ctx)
    with ctx[1].session_factory() as db:
        step = JobStep(site_job_id=job_id, step_key="disabled", feature_type="flow.result",
                       status="success", data_json='{"value":false}')
        db.add(step)
        db.flush()
        digest = failure_hash(db, db.get(SiteJob, job_id))
        assert digest is not None
        step.step_key = "insufficient_balance"
        db.flush()
        assert digest != failure_hash(db, db.get(SiteJob, job_id))
        step.status = "skipped"
        db.flush()
        assert failure_hash(db, db.get(SiteJob, job_id)) is None


@pytest.mark.parametrize("phase", ["select", "claim", "activation", "completion"])
def test_stale_analysis_provenance_survives_policy_restore_and_reason_overwrite(ctx, monkeypatch, phase):
    grant(ctx)
    source, _, _ = failure(ctx)
    takeover, manager, site_id = ctx
    sid = manager.create_takeover(source)
    if phase in {"activation", "completion"}:
        assert takeover._claim(source, sid, False)
    with manager.session_factory() as db:
        # Same site revision, but a different effective feature policy during analysis.
        db.add(AppSetting(key="feature.flow.open", value_json=_json({
            "defaults": {}, "required_fields": ["url"]})))
        db.commit()
        original_fingerprint = db.get(AssistantTakeoverAction, source).source_fingerprint
    if phase == "select":
        assert takeover._select() == (source, OWNER)
    elif phase == "claim":
        assert takeover._claim(source, sid, False)
    elif phase == "activation":
        with manager.session_factory() as db:
            action = db.get(AssistantTakeoverAction, source)
            activate_takeover(manager, db, db.get(AssistantSession, sid), source, action.request_id)
            db.commit()
    else:
        with manager.session_factory() as db:
            action = db.get(AssistantTakeoverAction, source)
            request_id = action.request_id
            manager._message(db, sid, "user", "Analyze", request_id=request_id)
            manager._message(db, sid, "assistant", CONCLUSION)
            conversation = db.get(AssistantSession, sid)
            data = manager._decrypt(conversation)
            data["_turn_outcomes"] = {request_id: {"ok": True}}
            manager._encrypt(conversation, data)
            db.commit()
        takeover._complete(source, sid, request_id, False)
    with manager.session_factory() as db:
        proof = db.get(TakeoverAnalysisProvenance, source)
        assert proof.source_current is False
        assert proof.source_fingerprint == original_fingerprint
        assert proof.inspected_fingerprint != original_fingerprint
        db.delete(db.get(AppSetting, "feature.flow.open"))
        action = db.get(AssistantTakeoverAction, source)
        # Completion overwrites the reason, but must never erase the stale proof.
        action.reason = "analysis_available"
        action.status = "queued" if phase != "completion" else action.status
        db.commit()
    if phase != "completion":
        complete(ctx, source)
    current, _, _ = failure(ctx)
    runtime = no_runtime(ctx, monkeypatch)
    asyncio.run(takeover.process_one())
    runtime.assert_called_once()
    with manager.session_factory() as db:
        assert db.get(TakeoverAnalysisProvenance, source).source_current is False
        assert db.get(AssistantTakeoverAction, current).status == "queued"
        assert db.get(TakeoverDedupLink, current) is None


def test_legacy_conclusion_without_analysis_provenance_fails_closed(ctx, monkeypatch):
    grant(ctx)
    source, _, _ = failure(ctx)
    complete(ctx, source)
    with ctx[1].session_factory() as db:
        db.delete(db.get(TakeoverAnalysisProvenance, source))
        db.commit()
    failure(ctx)
    runtime = no_runtime(ctx, monkeypatch)
    asyncio.run(ctx[0].process_one())
    runtime.assert_called_once()


@pytest.mark.parametrize("code", ["assistant_busy", "session_running", "model_unavailable"])
def test_proven_unaccepted_send_can_reuse_later_completed_analysis(ctx, monkeypatch, code):
    grant(ctx)
    source, _, _ = failure(ctx)
    complete(ctx, source)
    with ctx[1].session_factory() as db:
        db.get(AssistantTakeoverAction, source).status = "dispatched"
        db.commit()
    current, _, _ = failure(ctx)
    sid = ctx[1].create_takeover(current)
    request_id = ctx[0]._claim(current, sid, False)[0]
    ctx[0]._send_failed(current, sid, request_id, code)
    with ctx[1].session_factory() as db:
        action = db.get(AssistantTakeoverAction, current)
        assert action.status == "queued" and action.attempts == 0 and action.request_id is None
        db.get(AssistantTakeoverAction, source).status = "completed_analysis"
        db.commit()
    runtime = no_runtime(ctx, monkeypatch)
    asyncio.run(ctx[0].process_one())
    runtime.assert_not_called()
    with ctx[1].session_factory() as db:
        assert db.get(AssistantTakeoverAction, current).status == "reused"
        assert db.get(TakeoverDedupLink, current).source_action_id == source


@pytest.mark.parametrize("accepted,code", [(True, "assistant_busy"), (True, "session_running"),
                                          (True, "model_unavailable"), (False, None)])
def test_accepted_or_ambiguous_send_preserves_identity_and_never_replays(ctx, monkeypatch, accepted, code):
    grant(ctx)
    current, _, _ = failure(ctx)
    sid = ctx[1].create_takeover(current)
    request_id = ctx[0]._claim(current, sid, False)[0]
    if accepted:
        with ctx[1].session_factory() as db:
            ctx[1]._message(db, sid, "user", "Analyze", request_id=request_id)
            db.commit()
    ctx[0]._send_failed(current, sid, request_id, code)
    runtime = no_runtime(ctx, monkeypatch)
    asyncio.run(ctx[0].process_one())
    runtime.assert_not_called()
    with ctx[1].session_factory() as db:
        action = db.get(AssistantTakeoverAction, current)
        assert action.status == "unknown" and action.attempts == 1 and action.request_id == request_id
        assert db.get(TakeoverDedupLink, current) is None


def test_completed_turn_records_positive_provenance_and_projects_run_not_job(ctx, monkeypatch):
    grant(ctx)
    # Ensure the user-facing run identity is not accidentally the per-site job ID.
    with ctx[1].session_factory() as db:
        db.add(Run(created_by=OWNER, status="success", finished_at=datetime.now(timezone.utc)))
        db.commit()
    source, first_job, first_run = failure(ctx)
    assert first_job != first_run
    monkeypatch.setattr(ctx[1], "_runtime", lambda: {})

    async def send(owner, sid, payload, *, action_id):
        def accept():
            with ctx[1].session_factory() as db:
                conversation = db.get(AssistantSession, sid)
                request_id = str(payload.request_id)
                activate_takeover(ctx[1], db, conversation, action_id, request_id)
                ctx[1]._message(db, sid, "user", payload.content, request_id=request_id)
                ctx[1]._message(db, sid, "assistant", CONCLUSION)
                data = ctx[1]._decrypt(conversation)
                data["_turn_outcomes"] = {request_id: {"ok": True}}
                ctx[1]._encrypt(conversation, data)
                db.commit()
        await asyncio.to_thread(accept)

    monkeypatch.setattr(ctx[1], "send_takeover", AsyncMock(side_effect=send))
    asyncio.run(ctx[0].process_one())
    with ctx[1].session_factory() as db:
        proof = db.get(TakeoverAnalysisProvenance, source)
        assert proof.source_current is True
        assert proof.source_fingerprint == proof.inspected_fingerprint
        assert db.get(AssistantTakeoverAction, source).status == "completed_analysis"
    current, _, _ = failure(ctx)
    runtime = no_runtime(ctx, monkeypatch)
    asyncio.run(ctx[0].process_one())
    runtime.assert_not_called()
    with ctx[1].session_factory() as db:
        text = public_conclusion(ctx[1], db, db.get(AssistantTakeoverAction, current))
        assert text.startswith(f"\u590d\u7528\u6b64\u524d\u5206\u6790\uff08\u4efb\u52a1 #{first_run}\uff09\uff1a")
