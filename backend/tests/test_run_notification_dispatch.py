from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.assistant_takeover import AssistantTakeoverAction
from app.models.assistant_takeover_notice import AssistantTakeoverNotice
from app.models.execution import Run, SiteJob
from app.models.run_notification import RunNotificationIntent
from app.services.execution import ExecutionService
from app.services.notifications import NotificationService
from app.services.run_notification_dispatch import RunNotificationDispatcher
from tests.test_assistant_takeover import ctx, failed_job, grant  # noqa: F401
from tests.test_notifications_api import _email_payload, _http_payload
from tests.test_assistant_takeover_conclusions import context, promote  # noqa: F401


@pytest.fixture
def deliveries(ctx, monkeypatch):
    _, manager, _ = ctx
    sent = []
    monkeypatch.setattr(NotificationService, "_send_email", lambda self, target, message:
                        sent.append(("email", target, message, None)) or "ok")
    monkeypatch.setattr(NotificationService, "_send_http", lambda self, target, message, event:
                        sent.append(("http", target, message, event)) or "ok")
    from app.schemas.notification import EmailNotificationUpdate, HTTPNotificationUpdate

    with manager.session_factory() as db:
        service = NotificationService(db, manager.settings, manager.crypto)
        service.update_email(EmailNotificationUpdate(**_email_payload()))
        service.update_http(HTTPNotificationUpdate(**_http_payload()))
    return sent


def intent(ctx, run_id):
    with ctx[1].session_factory() as db:
        return db.get(RunNotificationIntent, run_id)


def actions(ctx, run_id):
    with ctx[1].session_factory() as db:
        return list(db.scalars(select(AssistantTakeoverAction).where(
            AssistantTakeoverAction.source_run_id == run_id)))


def complete(ctx, run_id, status="completed_analysis"):
    with ctx[1].session_factory() as db:
        for row in db.scalars(select(AssistantTakeoverAction).where(
                AssistantTakeoverAction.source_run_id == run_id)):
            row.status = status
        db.commit()


def test_immediate_idempotent_finalize_and_wakes(ctx, deliveries):
    _, manager, _ = ctx
    job_id, run_id = failed_job(ctx)
    with manager.session_factory() as db:
        db.get(Run, run_id).status = "running"
        db.commit()
    executor = manager.executor
    executor._finalize_run(run_id)
    first = intent(ctx, run_id)
    for _ in range(3):
        executor._finalize_run(run_id)
        executor.schedule_run_notification(run_id)
        executor.notification_dispatch.dispatch_pending()
    assert len(deliveries) == 2
    assert intent(ctx, run_id).state == "completed"
    assert intent(ctx, run_id).deadline_at == first.deadline_at
    with manager.session_factory() as db:
        run = db.get(Run, run_id)
        assert run.status == "fail" and json.loads(run.summary_json) == {"fail": 1}
        assert db.get(SiteJob, job_id).status == "fail"


def test_all_jobs_planned_and_sent_before_fresh_analysis(ctx, deliveries, monkeypatch):
    grant(ctx)
    _, manager, site_id = ctx
    job_id, run_id = failed_job(ctx)
    with manager.session_factory() as db:
        job = db.get(SiteJob, job_id)
        job.message = "original failure"
        other = SiteJob(run_id=run_id, site_id=site_id, site_name="Other", lane="general",
                        status="fail", message="other failure", finished_at=datetime.now(timezone.utc))
        db.add(other)
        db.commit()
        other_id = other.id
    dispatcher = manager.executor.notification_dispatch
    runtime = Mock(side_effect=AssertionError("no model before notification"))
    monkeypatch.setattr(manager, "_runtime", runtime)
    monkeypatch.setattr(manager, "create_takeover", runtime)
    dispatcher.ensure(run_id)
    assert len(actions(ctx, run_id)) == 2
    assert intent(ctx, run_id).state == "ready"
    dispatcher.dispatch_pending()
    assert len(deliveries) == 2
    runtime.assert_not_called()
    assert all(action.status == "queued" for action in actions(ctx, run_id))
    event = deliveries[1][3]
    assert event["status"] == "fail" and event["fail_count"] == 2 and event["success_count"] == 0
    assert event["sites"][0]["message"] == "助手复查中，结果稍后发出。原任务结果：original failure"
    assert event["sites"][1]["message"] == "助手复查中，结果稍后发出。原任务结果：other failure"
    assert deliveries[0][2] == deliveries[1][2]
    with manager.session_factory() as db:
        assert db.get(SiteJob, job_id).message == "original failure"
        assert db.get(SiteJob, other_id).status == "fail"
    dispatcher.ensure(run_id)
    dispatcher.dispatch_pending()
    assert len(deliveries) == 2


@pytest.mark.parametrize("status", ["queued", "dispatched"])
def test_fresh_actions_never_delay_or_rewrite_task_notification(ctx, deliveries, status):
    grant(ctx)
    _, manager, _ = ctx
    job_id, run_id = failed_job(ctx)
    with manager.session_factory() as db:
        db.get(Run, run_id).finished_at = datetime.now(timezone.utc) - timedelta(seconds=301)
        db.get(SiteJob, job_id).message = "original failure"
        db.commit()
    dispatcher = manager.executor.notification_dispatch
    ctx[0].reconcile()
    complete(ctx, run_id, status)
    dispatcher.ensure(run_id)
    for _ in range(2):
        dispatcher.ensure(run_id)
    dispatcher.dispatch_pending()
    assert deliveries[1][3]["sites"][0]["message"] == "助手复查中，结果稍后发出。原任务结果：original failure"
    complete(ctx, run_id)
    dispatcher.dispatch_pending()
    assert len(deliveries) == 2


def test_startup_ambiguous_dispatch_unknown_no_resend_or_backfill(ctx, deliveries):
    _, manager, _ = ctx
    _, run_id = failed_job(ctx)
    failed_job(ctx)  # No explicit notification request: never backfilled.
    dispatcher = manager.executor.notification_dispatch
    dispatcher.ensure(run_id)
    with manager.session_factory() as db:
        db.get(RunNotificationIntent, run_id).state = "dispatched"
        db.commit()

    async def restart():
        await dispatcher.start()
        await asyncio.sleep(0)
        await dispatcher.stop()

    asyncio.run(restart())
    dispatcher.ensure(run_id)
    dispatcher.dispatch_pending()
    assert intent(ctx, run_id).state == "unknown" and deliveries == []
    with manager.session_factory() as db:
        assert len(list(db.scalars(select(RunNotificationIntent)))) == 1


def test_legacy_waiting_becomes_ready_without_conclusion_backfill(ctx, deliveries, monkeypatch):
    grant(ctx)
    _, manager, _ = ctx
    _, run_id = failed_job(ctx)
    dispatcher = manager.executor.notification_dispatch
    dispatcher.ensure(run_id)
    with manager.session_factory() as db:
        row = db.get(RunNotificationIntent, run_id)
        event = NotificationService(db, manager.settings, manager.crypto)._run_event(
            run_id, include_conclusions=False)
        event["sites"][0]["message"] = "legacy frozen message"
        frozen = manager.crypto.encrypt(f"run-notification:{run_id}", json.dumps({"event": event}))
        row.state, row.ciphertext, row.nonce, row.key_version = (
            "waiting", frozen.ciphertext, frozen.nonce, frozen.key_version)
        row.deadline_at = datetime.now(timezone.utc) + timedelta(days=10)
        db.commit()
    failed_job(ctx)  # No historical notification or investigation backfill.
    monkeypatch.setattr(NotificationService, "apply_conclusions",
                        Mock(side_effect=AssertionError("no legacy backfill")))
    resumed = RunNotificationDispatcher(manager.executor)

    async def migrate():
        await resumed.start()
        assert intent(ctx, run_id).state == "ready"
        await resumed.stop()

    asyncio.run(migrate())
    resumed = RunNotificationDispatcher(manager.executor)
    resumed.dispatch_pending()
    assert len(deliveries) == 2
    assert deliveries[1][3]["sites"][0]["message"] == "legacy frozen message"
    with manager.session_factory() as db:
        assert len(list(db.scalars(select(RunNotificationIntent)))) == 1
        assert list(db.scalars(select(AssistantTakeoverNotice))) == []


def test_claim_before_network_and_frozen_between_channels(ctx, deliveries, monkeypatch):
    _, manager, _ = ctx
    job_id, run_id = failed_job(ctx)
    dispatcher = manager.executor.notification_dispatch
    dispatcher.ensure(run_id)
    frozen_send = NotificationService._send_email

    def mutate(self, target, message):
        assert intent(ctx, run_id).state == "dispatched"
        with manager.session_factory() as db:
            db.get(SiteJob, job_id).message = "late changed message"
            db.commit()
        from app.schemas.notification import HTTPNotificationUpdate, NotificationLayoutUpdate
        self.update_http(HTTPNotificationUpdate(enabled=False, url="https://changed.example.com"))
        self.update_layout(NotificationLayoutUpdate(
            title_template="changed", body_template="changed", format="text"))
        dispatcher.ensure(run_id)
        dispatcher.dispatch_pending()
        return frozen_send(self, target, message)

    monkeypatch.setattr(NotificationService, "_send_email", mutate)
    dispatcher.dispatch_pending()
    assert len(deliveries) == 2
    assert deliveries[1][1].values["url"] == _http_payload()["url"]
    assert deliveries[1][2] == deliveries[0][2]
    assert "late changed message" not in deliveries[1][2]["body"]
    row = intent(ctx, run_id)
    assert b"hook-secret" not in row.ciphertext and b"smtp-password" not in row.ciphertext
    assert json.loads(row.channel_results_json) == [
        {"channel": "email", "ok": True}, {"channel": "http", "ok": True}]


def test_cancel_registers_in_same_transaction_and_is_idempotent(ctx, deliveries):
    _, manager, _ = ctx
    with manager.session_factory() as db:
        run = Run(created_by="test", status="queued")
        db.add(run)
        db.flush()
        run_id = run.id
        db.add(SiteJob(run_id=run_id, site_name="Pending", lane="general", status="queued"))
        db.commit()
        ExecutionService(db, manager.settings, manager.crypto).cancel_run(run_id)
    assert intent(ctx, run_id).state == "ready"
    manager.executor._finalize_run(run_id)
    manager.executor.schedule_run_notification(run_id)
    manager.executor.notification_dispatch.dispatch_pending()
    manager.executor.notification_dispatch.dispatch_pending()
    assert len(deliveries) == 2 and deliveries[1][3]["status"] == "canceled"


def test_planning_failure_does_not_rollback_run(ctx, deliveries, monkeypatch):
    _, manager, _ = ctx
    _, run_id = failed_job(ctx)
    with manager.session_factory() as db:
        db.get(Run, run_id).status = "running"
        db.commit()
    monkeypatch.setattr("app.services.run_notification_dispatch.plan_run_takeovers",
                        Mock(side_effect=ValueError("unsafe secret text")))
    manager.executor._finalize_run(run_id)
    manager.executor.notification_dispatch.dispatch_pending()
    assert len(deliveries) == 2 and intent(ctx, run_id).state == "completed"
    with manager.session_factory() as db:
        assert db.get(Run, run_id).status == "fail"


def test_concurrent_dispatch_claims_once(ctx, deliveries):
    _, manager, _ = ctx
    _, run_id = failed_job(ctx)
    dispatcher = manager.executor.notification_dispatch
    dispatcher.ensure(run_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: dispatcher.dispatch_pending(), range(2)))
    assert len(deliveries) == 2 and intent(ctx, run_id).state == "completed"


def test_real_conclusion_http_body_and_structured_payload_agree(context, monkeypatch):
    manager, action_id = context
    summary = promote(context)
    captured = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, url, *, json, headers):
            captured.append(json)
            return Mock(status_code=200, raise_for_status=Mock())

    monkeypatch.setattr("app.services.notifications.httpx.Client", FakeClient)
    from app.schemas.notification import HTTPNotificationUpdate

    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, action_id)
        run_id, job_id = action.source_run_id, action.source_job_id
        NotificationService(db, manager.settings, manager.crypto).update_http(
            HTTPNotificationUpdate(**_http_payload()))
    dispatcher = manager.executor.notification_dispatch
    dispatcher.ensure(run_id)
    dispatcher.dispatch_pending()
    assert len(captured) == 1
    payload = captured[0]
    assert summary in payload["body"] and payload["body"] == payload["content"]
    assert payload["run"]["status"] == "fail" and payload["run"]["fail_count"] == 1
    assert payload["run"]["sites"][0]["job_id"] == job_id
    assert payload["run"]["sites"][0]["message"] == summary
    with manager.session_factory() as db:
        job = db.get(SiteJob, job_id)
        assert job.status == "fail" and job.message == "Original failure"


def test_source_event_frozen_while_ready_and_keys_sanitized(ctx, deliveries):
    grant(ctx)
    _, manager, _ = ctx
    job_id, run_id = failed_job(ctx)
    with manager.session_factory() as db:
        db.get(SiteJob, job_id).message = "original test-cfbox-key failure"
        db.commit()
    dispatcher = manager.executor.notification_dispatch
    dispatcher.ensure(run_id)
    with manager.session_factory() as db:
        db.get(SiteJob, job_id).message = "late mutation"
        db.commit()
    complete(ctx, run_id, "failed")
    dispatcher.dispatch_pending()
    message = deliveries[1][3]["sites"][0]["message"]
    assert "original" in message and "late mutation" not in message
    assert "test-cfbox-key" not in message


def test_failed_channel_is_recorded_and_not_retried(ctx, deliveries, monkeypatch, caplog):
    _, manager, _ = ctx
    _, run_id = failed_job(ctx)

    def fail(*args):
        raise TimeoutError("transport timed out")

    monkeypatch.setattr(NotificationService, "_send_email", fail)
    dispatcher = manager.executor.notification_dispatch
    dispatcher.ensure(run_id)
    dispatcher.dispatch_pending()
    dispatcher.ensure(run_id)
    dispatcher.dispatch_pending()
    assert len(deliveries) == 1 and deliveries[0][0] == "http"
    assert "email notification failed" in caplog.text
    row = intent(ctx, run_id)
    assert row.state == "completed"
    assert json.loads(row.channel_results_json) == [
        {"channel": "email", "ok": False}, {"channel": "http", "ok": True}]


def test_ready_snapshot_survives_restart_without_rebuilding(ctx, deliveries, monkeypatch):
    _, manager, _ = ctx
    job_id, run_id = failed_job(ctx)
    dispatcher = manager.executor.notification_dispatch
    dispatcher.ensure(run_id)
    assert intent(ctx, run_id).state == "ready" and deliveries == []
    with manager.session_factory() as db:
        db.get(SiteJob, job_id).message = "later message"
        db.commit()
    from app.schemas.notification import HTTPNotificationUpdate

    with manager.session_factory() as db:
        NotificationService(db, manager.settings, manager.crypto).update_http(
            HTTPNotificationUpdate(enabled=False))
    resumed = RunNotificationDispatcher(manager.executor)
    resumed.dispatch_pending()
    assert len(deliveries) == 2
    assert deliveries[1][3]["sites"][0]["message"] != "later message"


def test_channel_filters_use_failed_source_even_after_conclusion(ctx, deliveries, monkeypatch):
    _, manager, _ = ctx
    job_id, run_id = failed_job(ctx)
    from app.schemas.notification import EmailNotificationUpdate, HTTPNotificationUpdate

    with manager.session_factory() as db:
        service = NotificationService(db, manager.settings, manager.crypto)
        service.update_email(EmailNotificationUpdate(notify_success=False))
        service.update_http(HTTPNotificationUpdate(notify_manual=False))
    monkeypatch.setattr("app.services.assistant_takeover_conclusions.notification_conclusions",
                        lambda db, crypto, run_id, **kwargs:
                        {job_id: {"message": "Configuration updated, not rerun"}})
    dispatcher = manager.executor.notification_dispatch
    dispatcher.ensure(run_id)
    dispatcher.dispatch_pending()
    assert len(deliveries) == 1 and deliveries[0][0] == "email"
    assert "Configuration updated, not rerun" in deliveries[0][2]["body"]


def test_unreadable_secret_redacts_messages_without_rolling_back_finalization(ctx, deliveries):
    _, manager, _ = ctx
    _, run_id = failed_job(ctx)
    from app.models.integration import IntegrationConfig

    with manager.session_factory() as db:
        db.get(Run, run_id).status = "running"
        db.add(IntegrationConfig(key="vision.api_key", value_ciphertext=b"corrupt",
                                 nonce=b"0" * 12, key_version=1, is_secret=True,
                                 fingerprint="00000000"))
        db.commit()
    manager.executor._finalize_run(run_id)
    manager.executor.notification_dispatch.dispatch_pending()
    assert len(deliveries) == 2 and deliveries[1][3]["sites"][0]["message"] == "[redacted]"
    with manager.session_factory() as db:
        assert db.get(Run, run_id).status == "fail"


@pytest.mark.parametrize("notify_manual", [False, True])
def test_executor_finalizes_deferred_run_and_notifies_with_source_filters(ctx, deliveries, notify_manual):
    _, manager, _ = ctx
    job_id, run_id = failed_job(ctx)
    from app.schemas.notification import EmailNotificationUpdate, HTTPNotificationUpdate

    with manager.session_factory() as db:
        run = db.get(Run, run_id)
        run.status, run.finished_at = "running", None
        job = db.get(SiteJob, job_id)
        job.status, job.message = "deferred", "retry later"
        service = NotificationService(db, manager.settings, manager.crypto)
        service.update_email(EmailNotificationUpdate(notify_success=False))
        service.update_http(HTTPNotificationUpdate(notify_success=False, notify_manual=notify_manual))
    manager.executor._finalize_run(run_id)
    dispatcher = manager.executor.notification_dispatch
    assert intent(ctx, run_id) is not None
    dispatcher.dispatch_pending()
    assert [entry[0] for entry in deliveries] == (["email", "http"] if notify_manual else ["email"])
    assert "retry later" in deliveries[0][2]["body"]
    if notify_manual:
        event = deliveries[1][3]
        assert event["status"] == "deferred" and event["deferred_count"] == 1
        assert event["success_count"] == 0 and event["fail_count"] == 0
        assert event["sites"][0]["status"] == "deferred"
    with manager.session_factory() as db:
        run, job = db.get(Run, run_id), db.get(SiteJob, job_id)
        assert run.status == "deferred" and run.finished_at is not None
        assert json.loads(run.summary_json) == {"deferred": 1}
        assert job.status == "deferred" and job.message == "retry later"


@pytest.mark.parametrize("status", ["fail", "deferred"])
def test_intent_requires_finished_at(ctx, status):
    _, manager, _ = ctx
    _, run_id = failed_job(ctx)
    with manager.session_factory() as db:
        run = db.get(Run, run_id)
        run.status, run.finished_at = status, None
        db.commit()
    manager.executor.notification_dispatch.ensure(run_id)
    assert intent(ctx, run_id) is None


def test_later_conclusion_cannot_rewrite_frozen_task_notification(context, monkeypatch):
    manager, action_id = context
    from app.schemas.notification import HTTPNotificationUpdate

    sent = []
    monkeypatch.setattr(NotificationService, "_send_http", lambda self, target, message, event:
                        sent.append((message, event)) or "ok")
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, action_id)
        run_id, job_id = action.source_run_id, action.source_job_id
        NotificationService(db, manager.settings, manager.crypto).update_http(
            HTTPNotificationUpdate(**_http_payload()))
    dispatcher = manager.executor.notification_dispatch
    dispatcher.ensure(run_id)
    summary = promote(context)
    resumed = RunNotificationDispatcher(manager.executor)
    resumed.dispatch_pending()
    expected = "Original failure"
    assert len(sent) == 1 and sent[0][1]["sites"][0]["message"] == expected
    assert summary not in sent[0][0]["body"]
    assert expected in sent[0][0]["body"]
    assert sent[0][1]["status"] == "fail" and sent[0][1]["fail_count"] == 1
    assert sent[0][1]["success_count"] == 0 and sent[0][1]["sites"][0]["status"] == "fail"
    with manager.session_factory() as db:
        frozen = db.get(RunNotificationIntent, run_id).ciphertext
        job = db.get(SiteJob, job_id)
        assert job.status == "fail" and job.message == "Original failure"
    dispatcher.ensure(run_id)
    dispatcher.dispatch_pending()
    with manager.session_factory() as db:
        assert db.get(RunNotificationIntent, run_id).ciphertext == frozen
    assert len(sent) == 1


def test_mixed_fresh_and_locally_reused_actions_notify_without_runtime(ctx, deliveries, monkeypatch):
    from tests.test_takeover_dedup import complete as complete_analysis, failure, no_runtime

    grant(ctx)
    source_id, _, prior_run = failure(ctx)
    complete_analysis(ctx, source_id)
    _, manager, site_id = ctx
    job_id, run_id = failed_job(ctx)
    with manager.session_factory() as db:
        from app.models.execution import JobStep

        db.add(JobStep(site_job_id=job_id, step_key="login", feature_type="auth.login",
                       status="fail", message="选择器未找到", data_json=json.dumps({
                           "reason": "selector_not_found", "field": "username_selector"})))
        fresh = SiteJob(run_id=run_id, site_id=site_id, site_name="Fresh", lane="general",
                        status="fail", message="different failure", finished_at=datetime.now(timezone.utc))
        db.add(fresh)
        db.commit()
        fresh_id = fresh.id
    runtime = no_runtime(ctx, monkeypatch)
    dispatcher = manager.executor.notification_dispatch
    dispatcher.ensure(run_id)
    assert {a.source_job_id: a.status for a in actions(ctx, run_id)} == {
        job_id: "reused", fresh_id: "queued"}
    dispatcher.dispatch_pending()
    runtime.assert_not_called()
    assert len(deliveries) == 2
    rows = {row["job_id"]: row for row in deliveries[1][3]["sites"]}
    assert f"复用此前分析（任务 #{prior_run}）" in rows[job_id]["message"]
    assert "当前未重新分析/未修复" in rows[job_id]["message"]
    assert rows[fresh_id]["message"] == "助手复查中，结果稍后发出。原任务结果：different failure"
    for action in actions(ctx, run_id):
        dispatcher.ensure_investigation(action.id)
    dispatcher.dispatch_pending()
    assert len(deliveries) == 2
    with manager.session_factory() as db:
        assert list(db.scalars(select(AssistantTakeoverNotice))) == []


@pytest.fixture
def investigation_deliveries(context, monkeypatch):
    manager, _ = context
    sent = []
    monkeypatch.setattr(NotificationService, "_send_email", lambda self, target, message:
                        sent.append(("email", target, message, None)) or "ok")
    monkeypatch.setattr(NotificationService, "_send_http", lambda self, target, message, event:
                        sent.append(("http", target, message, event)) or "ok")
    from app.schemas.notification import EmailNotificationUpdate, HTTPNotificationUpdate

    with manager.session_factory() as db:
        service = NotificationService(db, manager.settings, manager.crypto)
        service.update_email(EmailNotificationUpdate(**_email_payload()))
        service.update_http(HTTPNotificationUpdate(**_http_payload()))
        run_id = db.get(AssistantTakeoverAction, context[1]).source_run_id
    dispatcher = manager.executor.notification_dispatch
    dispatcher.ensure(run_id)
    dispatcher.dispatch_one(run_id)
    sent.clear()
    return sent


def notice(context):
    with context[0].session_factory() as db:
        return db.get(AssistantTakeoverNotice, context[1])


@pytest.mark.parametrize("updated", [False, True])
@pytest.mark.parametrize("source_status", ["fail", "waiting_human"])
@pytest.mark.parametrize("fault", [None, "registration", "missing_notice", "wake"])
def test_complete_atomically_queues_distinct_investigation_once(
    context, investigation_deliveries, monkeypatch, updated, source_status, fault,
):
    from app.models.assistant import AssistantSession
    from app.services.assistant_takeover import AssistantTakeover
    from tests.test_assistant_takeover_conclusions import GOOD, update_receipt

    manager, action_id = context
    if updated:
        update_receipt(context)
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, action_id)
        sid, request_id = action.session_id, action.request_id
        run_id, job_id = action.source_run_id, action.source_job_id
        action.status = "dispatched"
        run, job = db.get(Run, run_id), db.get(SiteJob, job_id)
        run.status = job.status = source_status
        run.summary_json = json.dumps({source_status: 1})
        manager._message(db, sid, "assistant", GOOD, reasoning_content="private raw reasoning")
        conversation = db.get(AssistantSession, sid)
        data = manager._decrypt(conversation)
        data["_turn_outcomes"] = {request_id: {"ok": True}}
        manager._encrypt(conversation, data)
        db.commit()
    dispatcher = manager.executor.notification_dispatch
    monkeypatch.setattr(dispatcher, "ensure_investigation",
                        Mock(side_effect=AssertionError("no separate registration")))
    from app.models.assistant_takeover_conclusion import AssistantTakeoverConclusion
    from app.services import run_notification_dispatch as module

    original = module.register_investigation_notification

    def inside_transaction(db, settings, crypto, identifier):
        assert db.get(AssistantTakeoverAction, identifier).status == (
            "updated" if updated else "completed_analysis")
        assert db.get(AssistantTakeoverConclusion, identifier) is not None
        result = original(db, settings, crypto, identifier)
        if fault == "registration":
            raise RuntimeError("registration failed before completion commit")
        if fault == "missing_notice":
            return None
        return result

    def after_commit():
        with manager.session_factory() as db:
            saved = db.get(AssistantTakeoverAction, action_id)
            assert saved.status == ("updated" if updated else "completed_analysis")
            assert db.get(AssistantTakeoverConclusion, action_id) is not None
            assert db.get(AssistantTakeoverNotice, action_id) is not None
        if fault == "wake":
            raise RuntimeError("process exit after completion commit")

    monkeypatch.setattr(module, "register_investigation_notification", inside_transaction)
    monkeypatch.setattr(dispatcher, "wake", after_commit)
    takeover = AssistantTakeover(manager)
    if fault:
        with pytest.raises(ValueError if fault == "missing_notice" else RuntimeError):
            asyncio.run(takeover._phase(takeover._complete, action_id, sid, request_id, False))
        if fault in {"registration", "missing_notice"}:
            with manager.session_factory() as db:
                assert db.get(AssistantTakeoverAction, action_id).status == "dispatched"
                assert db.get(AssistantTakeoverConclusion, action_id) is None
                assert db.get(AssistantTakeoverNotice, action_id) is None
        monkeypatch.setattr(module, "register_investigation_notification", original)
        monkeypatch.setattr(dispatcher, "wake", lambda: None)
        if fault in {"registration", "missing_notice"}:
            takeover._complete(action_id, sid, request_id, False)
    else:
        asyncio.run(takeover._phase(takeover._complete, action_id, sid, request_id, False))
    assert notice(context).state == "pending" and investigation_deliveries == []
    RunNotificationDispatcher(manager.executor).dispatch_pending()
    takeover._complete(action_id, sid, request_id, False)
    dispatcher.wake()
    dispatcher.dispatch_pending()
    assert len(investigation_deliveries) == 2
    email, http = investigation_deliveries
    assert email[2] == http[2] and email[2]["format"] == "text"
    assert email[2]["title"] == f"[SiteFlow] 助手调查结果 #{run_id}"
    assert ("已更新配置但未重新验证" if updated else "仅调查") in email[2]["body"]
    assert f"站点任务 #{job_id}" in email[2]["body"]
    assert source_status in email[2]["body"] and "private raw reasoning" not in email[2]["body"]
    assert http[3]["event"] == "assistant.investigation"
    assert http[3]["job_status"] == http[3]["run_status"] == source_status
    with manager.session_factory() as db:
        assert db.get(Run, run_id).status == source_status
        assert json.loads(db.get(Run, run_id).summary_json) == {source_status: 1}
        assert db.get(SiteJob, job_id).status == source_status
        assert db.get(SiteJob, job_id).message == "Original failure"
    row = notice(context)
    assert row.state == "completed"
    assert json.loads(row.channel_results_json) == [
        {"channel": "email", "ok": True}, {"channel": "http", "ok": True}]
    for secret in (b"smtp-password", b"hook-secret", b"private raw reasoning"):
        assert secret not in row.ciphertext


@pytest.mark.parametrize("opted_out", ["email", "http"])
def test_assistant_opt_out_does_not_disable_task_notifications(
    context, investigation_deliveries, opted_out,
):
    from app.schemas.notification import EmailNotificationUpdate, HTTPNotificationUpdate

    manager, action_id = context
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, action_id)
        run_id = action.source_run_id
        db.delete(db.get(RunNotificationIntent, run_id))
        service = NotificationService(db, manager.settings, manager.crypto)
        if opted_out == "email":
            service.update_email(EmailNotificationUpdate(notify_assistant=False))
        else:
            service.update_http(HTTPNotificationUpdate(notify_assistant=False))
    dispatcher = manager.executor.notification_dispatch
    dispatcher.ensure(run_id)
    dispatcher.dispatch_pending()
    assert [entry[0] for entry in investigation_deliveries] == ["email", "http"]
    promote(context)
    dispatcher.ensure_investigation(action_id)
    dispatcher.dispatch_pending()
    assert [entry[0] for entry in investigation_deliveries[2:]] == [
        "http" if opted_out == "email" else "email"]


@pytest.mark.parametrize("status", ["failed", "unknown", "revoked", "reused", "queued", "dispatched"])
def test_nonterminal_success_or_missing_conclusion_never_notifies(
    context, investigation_deliveries, status,
):
    manager, action_id = context
    promote(context)
    with manager.session_factory() as db:
        db.get(AssistantTakeoverAction, action_id).status = status
        db.commit()
    dispatcher = manager.executor.notification_dispatch
    dispatcher.ensure_investigation(action_id)
    dispatcher.dispatch_pending()
    assert notice(context) is None and investigation_deliveries == []


def test_no_conclusion_never_registers_investigation(context, investigation_deliveries):
    manager, action_id = context
    dispatcher = manager.executor.notification_dispatch
    dispatcher.ensure_investigation(action_id)
    dispatcher.dispatch_pending()
    assert notice(context) is None and investigation_deliveries == []


def test_concurrent_investigation_claim_and_failed_channel_no_retry(
    context, investigation_deliveries, monkeypatch,
):
    manager, action_id = context
    promote(context)
    dispatcher = manager.executor.notification_dispatch
    dispatcher.ensure_investigation(action_id)

    def fail(*args):
        assert notice(context).state == "dispatched"
        raise TimeoutError("smtp-password hook-secret")

    monkeypatch.setattr(NotificationService, "_send_email", fail)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: dispatcher.dispatch_pending(), range(2)))
    dispatcher.ensure_investigation(action_id)
    dispatcher.dispatch_pending()
    assert len(investigation_deliveries) == 1 and investigation_deliveries[0][0] == "http"
    row = notice(context)
    assert row.state == "completed"
    assert json.loads(row.channel_results_json) == [
        {"channel": "email", "ok": False}, {"channel": "http", "ok": True}]
    assert "secret" not in row.channel_results_json


def test_investigation_restart_dispatched_unknown_no_resend(context, investigation_deliveries):
    manager, action_id = context
    promote(context)
    dispatcher = manager.executor.notification_dispatch
    dispatcher.ensure_investigation(action_id)
    with manager.session_factory() as db:
        db.get(AssistantTakeoverNotice, action_id).state = "dispatched"
        db.commit()

    async def restart():
        await dispatcher.start()
        await dispatcher.stop()

    asyncio.run(restart())
    resumed = RunNotificationDispatcher(manager.executor)
    resumed.ensure_investigation(action_id)
    resumed.dispatch_pending()
    assert notice(context).state == "unknown" and investigation_deliveries == []


def test_investigation_real_http_and_mail_agree_without_layout_or_secrets(context, monkeypatch):
    from app.schemas.notification import (
        EmailNotificationUpdate, HTTPNotificationUpdate, NotificationLayoutUpdate,
    )

    manager, action_id = context
    summary = promote(context)
    mail, requests = [], []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, url, *, json, headers):
            requests.append(json)
            return Mock(status_code=200, raise_for_status=Mock())

    monkeypatch.setattr("app.services.notifications.httpx.Client", FakeClient)
    monkeypatch.setattr(NotificationService, "_send_email", lambda self, target, message:
                        mail.append(message) or "ok")
    with manager.session_factory() as db:
        service = NotificationService(db, manager.settings, manager.crypto)
        service.update_email(EmailNotificationUpdate(**_email_payload(), notify_manual=False))
        service.update_http(HTTPNotificationUpdate(**_http_payload(), notify_manual=False))
        service.update_layout(NotificationLayoutUpdate(
            title_template="Wrong {{run_id}}", body_template="<b>{{sites}}</b>", format="html"))
    dispatcher = manager.executor.notification_dispatch
    with manager.session_factory() as db:
        run_id = db.get(AssistantTakeoverAction, action_id).source_run_id
    dispatcher.ensure(run_id)
    dispatcher.dispatch_one(run_id)
    dispatcher.ensure_investigation(action_id)
    dispatcher.dispatch_pending()
    assert len(requests) == len(mail) == 1
    payload = requests[0]
    assert payload["event"] == "assistant.investigation"
    assert payload["body"] == payload["content"] == mail[0]["body"]
    assert payload["title"] == mail[0]["title"]
    assert payload["format"] == mail[0]["format"] == "text"
    assert payload["conclusion"] == summary and summary in payload["body"]
    assert payload["action_status"] == "completed_analysis"
    assert payload["run_status"] == payload["job_status"] == "fail"
    assert "run" not in payload and "Wrong" not in payload["title"]
    assert "{{" not in payload["body"] and "<b>" not in payload["body"]
    assert "smtp-password" not in json.dumps(payload) and "hook-secret" not in json.dumps(payload)


@pytest.mark.parametrize("channel", ["email", "http"])
def test_investigation_skips_disabled_or_invalid_channel(context, investigation_deliveries, channel):
    from app.schemas.notification import EmailNotificationUpdate

    manager, action_id = context
    promote(context)
    with manager.session_factory() as db:
        service = NotificationService(db, manager.settings, manager.crypto)
        if channel == "email":
            service.update_email(EmailNotificationUpdate(enabled=False))
        else:
            service.delete_http()  # Still enabled, but no valid configuration.
    dispatcher = manager.executor.notification_dispatch
    dispatcher.ensure_investigation(action_id)
    dispatcher.dispatch_pending()
    assert [entry[0] for entry in investigation_deliveries] == [
        "http" if channel == "email" else "email"]


def test_pending_annotation_is_bounded_and_never_changes_job_results(ctx):
    _, manager, _ = ctx
    job_id, run_id = failed_job(ctx)
    with manager.session_factory() as db:
        job = db.get(SiteJob, job_id)
        job.message = "original " * 100
        db.commit()
        service = NotificationService(db, manager.settings, manager.crypto)
        event = service._run_event(run_id, include_conclusions=False)
        service.annotate_assistant_pending(event, [job_id])
        message = event["sites"][0]["message"]
        assert message.startswith("助手复查中，结果稍后发出。原任务结果：original")
        assert len(message) == 500
        assert job.message == "original " * 100 and job.status == "fail"


def test_pending_investigation_survives_restart_with_frozen_channels(context, investigation_deliveries):
    from app.schemas.notification import HTTPNotificationUpdate

    manager, action_id = context
    promote(context)
    dispatcher = manager.executor.notification_dispatch
    dispatcher.ensure_investigation(action_id)
    frozen = notice(context).ciphertext
    with manager.session_factory() as db:
        NotificationService(db, manager.settings, manager.crypto).update_http(
            HTTPNotificationUpdate(enabled=False, notify_assistant=False))
    resumed = RunNotificationDispatcher(manager.executor)

    async def restart():
        await resumed.start()
        try:
            async with asyncio.timeout(5):
                while notice(context).state != "completed":
                    await asyncio.sleep(0.01)
        finally:
            await resumed.stop()

    asyncio.run(restart())
    assert [entry[0] for entry in investigation_deliveries] == ["email", "http"]
    assert notice(context).ciphertext == frozen
    assert notice(context).state == "completed"


def test_investigation_revalidates_known_secrets_before_registration(context, investigation_deliveries):
    from app.models.integration import IntegrationConfig

    manager, action_id = context
    secret = "newly-configured-opaque-key"
    assert promote(context, "The page showed " + secret)
    with manager.session_factory() as db:
        encrypted = manager.crypto.encrypt("vision.api_key", secret)
        db.add(IntegrationConfig(key="vision.api_key", value_ciphertext=encrypted.ciphertext,
                                 nonce=encrypted.nonce, key_version=encrypted.key_version,
                                 fingerprint=manager.crypto.fingerprint(secret)))
        db.commit()
    dispatcher = manager.executor.notification_dispatch
    dispatcher.ensure_investigation(action_id)
    dispatcher.dispatch_pending()
    assert notice(context) is None and investigation_deliveries == []


@pytest.mark.parametrize("state", ["waiting", "ready", "dispatched", "completed", "unknown", None])
def test_investigation_requires_original_notification_attempt(
    context, investigation_deliveries, state,
):
    manager, action_id = context
    promote(context)
    dispatcher = manager.executor.notification_dispatch
    dispatcher.ensure_investigation(action_id)
    with manager.session_factory() as db:
        run_id = db.get(AssistantTakeoverAction, action_id).source_run_id
        original = db.get(RunNotificationIntent, run_id)
        if state is None:
            db.delete(original)
        else:
            original.state = state
        db.commit()
    dispatcher.dispatch_investigation_one(action_id)
    if state in {"completed", "unknown"}:
        assert notice(context).state == "completed"
        assert len(investigation_deliveries) == 2
    else:
        assert notice(context).state == "pending" and investigation_deliveries == []
        with manager.session_factory() as db:
            original = db.get(RunNotificationIntent, run_id)
            assert original.state == ("ready" if state is None else state)
            if state == "dispatched":
                original.state = "completed"
                db.commit()
        if state != "dispatched":
            dispatcher.dispatch_one(run_id)
            assert len(investigation_deliveries) == 2
            assert investigation_deliveries[1][3].get("event") != "assistant.investigation"
            investigation_deliveries.clear()
        dispatcher.dispatch_investigation_one(action_id)
        assert notice(context).state == "completed"
        assert len(investigation_deliveries) == 2
        assert investigation_deliveries[1][3]["event"] == "assistant.investigation"


def test_investigation_cannot_overtake_inflight_original(context, investigation_deliveries, monkeypatch):
    from threading import Event

    manager, action_id = context
    promote(context)
    dispatcher = manager.executor.notification_dispatch
    dispatcher.ensure_investigation(action_id)
    with manager.session_factory() as db:
        run_id = db.get(AssistantTakeoverAction, action_id).source_run_id
        db.get(RunNotificationIntent, run_id).state = "ready"
        db.commit()
    entered, release = Event(), Event()
    original = NotificationService._send_email

    def blocked(self, target, message):
        if "运行任务通知" in message["title"]:
            entered.set()
            assert release.wait(5)
        return original(self, target, message)

    monkeypatch.setattr(NotificationService, "_send_email", blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        task = pool.submit(dispatcher.dispatch_one, run_id)
        try:
            assert entered.wait(5)
            with manager.session_factory() as db:
                assert db.get(RunNotificationIntent, run_id).state == "dispatched"
            dispatcher.dispatch_investigation_one(action_id)
            assert notice(context).state == "pending" and investigation_deliveries == []
        finally:
            release.set()
        task.result()
    dispatcher.dispatch_investigation_one(action_id)
    assert len(investigation_deliveries) == 4
    assert investigation_deliveries[1][3].get("event") != "assistant.investigation"
    assert investigation_deliveries[3][3]["event"] == "assistant.investigation"


def test_filtered_original_completes_before_independently_opted_in_investigation(
    context, investigation_deliveries,
):
    from app.schemas.notification import EmailNotificationUpdate, HTTPNotificationUpdate

    manager, action_id = context
    promote(context)
    with manager.session_factory() as db:
        run_id = db.get(AssistantTakeoverAction, action_id).source_run_id
        db.delete(db.get(RunNotificationIntent, run_id))
        service = NotificationService(db, manager.settings, manager.crypto)
        service.update_email(EmailNotificationUpdate(notify_manual=False))
        service.update_http(HTTPNotificationUpdate(notify_manual=False))
    dispatcher = manager.executor.notification_dispatch
    dispatcher.ensure_investigation(action_id)
    dispatcher.dispatch_investigation_one(action_id)
    assert notice(context).state == "pending" and investigation_deliveries == []
    dispatcher.dispatch_one(run_id)
    with manager.session_factory() as db:
        original = db.get(RunNotificationIntent, run_id)
        assert original.state == "completed" and json.loads(original.channel_results_json) == []
    dispatcher.dispatch_investigation_one(action_id)
    assert len(investigation_deliveries) == 2
    assert investigation_deliveries[1][3]["event"] == "assistant.investigation"


@pytest.mark.parametrize("change", [
    "disabled", "revision", "owner", "scope", "grant", "url", "url_aba", "identity",
    "source_status", "source_cancelled",
])
def test_queued_notification_reuse_revalidates_authority(ctx, deliveries, change):
    from app.models.setting import AppSetting
    from app.models.workflow import Site
    from app.services.assistant_takeover_settings import KEY, SITE_GRANTS_KEY
    from tests.test_takeover_dedup import complete as complete_analysis
    from tests.test_takeover_dedup import failure

    grant(ctx, live_access=True)
    source_id, _, _ = failure(ctx)
    complete_analysis(ctx, source_id)
    _, manager, site_id = ctx
    if change == "url_aba":
        with manager.session_factory() as db:
            db.get(Site, site_id).base_url = "https://elsewhere.example/"
            db.commit()
        # An already queued action captured while the selected URL was changed.
        from app.services.assistant_takeover_dedup import enqueue_takeover
        from app.services.assistant_takeover_settings import TakeoverSettingsService
        from app.models.assistant_takeover import AssistantJobConfigSnapshot

        job_id, run_id = failed_job(ctx)
        with manager.session_factory() as db:
            action = enqueue_takeover(
                db, db.get(SiteJob, job_id), db.get(Run, run_id),
                TakeoverSettingsService(db).view(), db.get(AssistantJobConfigSnapshot, job_id),
                db.get(Site, site_id),
            )
            action_id = action.id
            db.get(Site, site_id).base_url = "https://example.com/"
            db.commit()
    else:
        action_id, job_id, run_id = failure(ctx)
    with manager.session_factory() as db:
        row = db.get(AppSetting, KEY)
        policy = json.loads(row.value_json)
        if change == "disabled":
            policy["values"]["enabled"] = False
        elif change == "revision":
            policy["revision"] += 1
        elif change == "owner":
            policy["authorizing_owner"] = "different-owner"
        elif change == "scope":
            policy["values"]["site_ids"] = []
        elif change == "grant":
            saved = db.get(AppSetting, SITE_GRANTS_KEY)
            grants = json.loads(saved.value_json)
            grants["bindings"][str(site_id)]["origin"] = "https://elsewhere.example"
            saved.value_json = json.dumps(grants)
        elif change == "url":
            db.get(Site, site_id).base_url = "https://elsewhere.example/"
        elif change == "identity":
            db.get(Site, site_id).history_key = "different-history"
        elif change == "source_status":
            db.get(SiteJob, job_id).status = "success"
        elif change == "source_cancelled":
            db.get(Run, run_id).cancel_requested = True
        row.value_json = json.dumps(policy)
        original_message = db.get(SiteJob, job_id).message
        db.commit()
    dispatcher = manager.executor.notification_dispatch
    dispatcher.ensure(run_id)
    dispatcher.dispatch_pending()
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, action_id)
        assert action.status == "revoked" and action.reason == "policy_or_source_changed"
    message = deliveries[1][3]["sites"][0]["message"]
    assert message == original_message
    assert "复用此前分析" not in message and "复查中" not in message


def test_busy_site_blocks_local_reuse_but_retains_pending_annotation(ctx, deliveries):
    from tests.test_takeover_dedup import complete as complete_analysis
    from tests.test_takeover_dedup import failure

    grant(ctx)
    source_id, _, _ = failure(ctx)
    complete_analysis(ctx, source_id)
    action_id, _, run_id = failure(ctx)
    _, manager, site_id = ctx
    with manager.session_factory() as db:
        active = Run(created_by="test", status="running")
        db.add(active)
        db.flush()
        db.add(SiteJob(run_id=active.id, site_id=site_id, site_name="Active", lane="general",
                       status="running"))
        db.commit()
    dispatcher = manager.executor.notification_dispatch
    dispatcher.ensure(run_id)
    dispatcher.dispatch_pending()
    with manager.session_factory() as db:
        assert db.get(AssistantTakeoverAction, action_id).status == "queued"
    message = deliveries[1][3]["sites"][0]["message"]
    assert message.startswith("助手复查中") and "复用此前分析" not in message


@pytest.mark.parametrize("valid_prior", [False, True])
@pytest.mark.parametrize("history", ["unknown", "waiting_human", "malformed"])
@pytest.mark.parametrize("preplanned", [False, True])
def test_uncertain_history_never_revokes_notification_diagnostics_or_proven_reuse(
    ctx, deliveries, monkeypatch, valid_prior, history, preplanned,
):
    from tests.test_takeover_dedup import complete as complete_analysis, failure, no_runtime

    grant(ctx, live_access=True)
    if valid_prior:
        source_id, _, prior_run = failure(ctx)
        complete_analysis(ctx, source_id)
    old_job, _ = failed_job(ctx, historical=True, snapshot=False,
                            status="success" if history == "malformed" else history)
    if history == "malformed":
        with ctx[1].session_factory() as db:
            db.get(SiteJob, old_job).result_json = '{"broken":'
            db.commit()
    action_id, job_id, run_id = failure(ctx)
    if not preplanned:
        from app.models.assistant_takeover_dedup import TakeoverFailureSignature

        with ctx[1].session_factory() as db:
            db.delete(db.get(TakeoverFailureSignature, job_id))
            db.delete(db.get(AssistantTakeoverAction, action_id))
            db.commit()
    runtime = no_runtime(ctx, monkeypatch)
    dispatcher = ctx[1].executor.notification_dispatch
    dispatcher.ensure(run_id)
    dispatcher.dispatch_pending()
    runtime.assert_not_called()
    saved = actions(ctx, run_id)
    assert len(saved) == 1 and saved[0].source_job_id == job_id
    assert saved[0].status == ("reused" if valid_prior else "queued")
    assert saved[0].attempts == 0 and saved[0].import_receipt_json is None
    message = deliveries[1][3]["sites"][0]["message"]
    if valid_prior:
        assert f"复用此前分析（任务 #{prior_run}）" in message
        assert "当前未重新分析/未修复" in message and "复查中" not in message
    else:
        assert message.startswith("助手复查中") and "复用此前分析" not in message


def test_notice_build_failure_rolls_back_completion_but_preserves_candidate_and_import(
    context, investigation_deliveries, monkeypatch,
):
    from app.models.assistant import AssistantSession
    from app.models.assistant_takeover_conclusion import (
        AssistantTakeoverConclusion, AssistantTakeoverConclusionCandidate,
    )
    from app.services.assistant_takeover import AssistantTakeover
    from app.services.assistant_takeover_conclusions import submit_conclusion
    from tests.test_assistant_takeover_conclusions import candidate_tools, update_receipt

    manager, action_id = context
    tools, args = candidate_tools(context)
    assert asyncio.run(submit_conclusion(tools, args))["ok"]
    update_receipt(context)
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, action_id)
        sid, request_id, receipt = action.session_id, action.request_id, action.import_receipt_json
        action.status = "dispatched"
        manager._message(db, sid, "assistant", "Detailed final\n" * 100)
        conversation = db.get(AssistantSession, sid)
        data = manager._decrypt(conversation)
        data["_turn_outcomes"] = {request_id: {"ok": True}}
        manager._encrypt(conversation, data)
        db.commit()
        frozen = db.get(AssistantTakeoverConclusionCandidate, (action_id, request_id)).ciphertext
    takeover = AssistantTakeover(manager)
    with monkeypatch.context() as patch:
        patch.setattr(NotificationService, "investigation_targets",
                      Mock(side_effect=ValueError("invalid delivery configuration")))
        with pytest.raises(ValueError, match="invalid delivery configuration"):
            takeover._complete(action_id, sid, request_id, False)
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, action_id)
        assert action.status == "dispatched" and action.import_receipt_json == receipt
        assert db.get(AssistantTakeoverConclusion, action_id) is None
        assert db.get(AssistantTakeoverNotice, action_id) is None
        assert db.get(AssistantTakeoverConclusionCandidate, (action_id, request_id)).ciphertext == frozen
    with manager.session_factory() as db:
        # assistant_manager.recover() makes sessions idle before takeover startup.
        db.get(AssistantSession, sid).status = "idle"
        db.get(AssistantTakeoverAction, action_id).updated_at = (
            datetime.now(timezone.utc) - timedelta(seconds=10))
        db.commit()
    restarted = AssistantTakeover(manager)
    restarted._recover()
    with manager.session_factory() as db:
        assert db.get(AssistantTakeoverAction, action_id).status == "dispatched"
    restarted._recover_finalizations()
    with manager.session_factory() as db:
        assert db.get(AssistantTakeoverAction, action_id).status == "updated"
        assert db.get(AssistantTakeoverConclusion, action_id) is not None
        assert db.get(AssistantTakeoverNotice, action_id).state == "pending"
    assert investigation_deliveries == []


def test_accepted_dispatched_turn_recovers_once_without_duplicate_notice(context):
    from app.models.assistant import AssistantSession
    from app.models.assistant_takeover_conclusion import AssistantTakeoverConclusion
    from app.services.assistant_takeover import AssistantTakeover
    from tests.test_assistant_takeover_conclusions import GOOD

    manager, action_id = context
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, action_id)
        sid, request_id = action.session_id, action.request_id
        action.status, action.reason = "dispatched", "awaiting_finalization"
        action.updated_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        manager._message(db, sid, "assistant", GOOD)
        conversation = db.get(AssistantSession, sid)
        data = manager._decrypt(conversation)
        data["_turn_outcomes"] = {request_id: {"ok": True}}
        manager._encrypt(conversation, data)
        db.commit()

    takeover = AssistantTakeover(manager)
    takeover._recover()
    with manager.session_factory() as db:
        assert db.get(AssistantTakeoverAction, action_id).status == "dispatched"
    takeover._recover_finalizations()
    takeover._recover_finalizations()

    with manager.session_factory() as db:
        assert db.get(AssistantTakeoverAction, action_id).status == "completed_analysis"
        assert db.get(AssistantTakeoverConclusion, action_id) is not None
        notices = list(db.scalars(select(AssistantTakeoverNotice).where(
            AssistantTakeoverNotice.action_id == action_id)))
        assert len(notices) == 1 and notices[0].state == "pending"


@pytest.mark.parametrize("mismatch", ["request", "session"])
def test_completion_rejects_binding_mismatch_without_mutation(context, mismatch):
    from app.models.assistant_takeover_conclusion import AssistantTakeoverConclusion
    from app.services.assistant_takeover import AssistantTakeover

    manager, action_id = context
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, action_id)
        sid, request_id = action.session_id, action.request_id
        action.status, action.reason = "dispatched", "claimed"
        action.updated_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    with manager.session_factory() as db:
        before = db.get(AssistantTakeoverAction, action_id).updated_at

    AssistantTakeover(manager)._complete(
        action_id, str(uuid4()) if mismatch == "session" else sid,
        str(uuid4()) if mismatch == "request" else request_id, False,
    )

    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, action_id)
        assert action.status == "dispatched" and action.reason == "claimed"
        assert action.updated_at == before
        assert db.get(AssistantTakeoverConclusion, action_id) is None
        assert db.get(AssistantTakeoverNotice, action_id) is None


def test_completed_action_replay_after_chat_deletion_preserves_conclusion(context):
    from app.models.assistant import AssistantMessage, AssistantSession
    from app.models.assistant_takeover_conclusion import AssistantTakeoverConclusion
    from app.services.assistant_takeover import AssistantTakeover
    from app.services.assistant_takeover_conclusions import public_conclusion
    from tests.test_assistant_takeover_conclusions import GOOD

    manager, action_id = context
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, action_id)
        sid, request_id = action.session_id, action.request_id
        action.status = "dispatched"
        manager._message(db, sid, "assistant", GOOD)
        session = db.get(AssistantSession, sid)
        data = manager._decrypt(session)
        data["_turn_outcomes"] = {request_id: {"ok": True}}
        manager._encrypt(session, data)
        db.commit()
    takeover = AssistantTakeover(manager)
    takeover._complete(action_id, sid, request_id, False)
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, action_id)
        original = (action.status, action.reason, action.updated_at,
                    public_conclusion(manager, db, action))
        for message in db.scalars(select(AssistantMessage).where(AssistantMessage.session_id == sid)):
            db.delete(message)
        db.delete(db.get(AssistantSession, sid))
        db.commit()
    takeover._complete(action_id, sid, request_id, False)
    takeover._complete(action_id, sid, request_id, True, recovery=True)
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, action_id)
        assert (action.status, action.reason, action.updated_at,
                public_conclusion(manager, db, action)) == original
        assert db.get(AssistantTakeoverConclusion, action_id) is not None
        assert db.get(AssistantTakeoverNotice, action_id) is not None
