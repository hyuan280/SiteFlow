from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import insert, select

from app.models.assistant import AssistantSession
from app.models.assistant_takeover import AssistantTakeoverAction
from app.models.assistant_takeover_conclusion import AssistantTakeoverConclusion
from app.models.assistant_takeover_notice import AssistantTakeoverNotice
from app.models.execution import Run, SiteJob
from app.services import assistant_takeover
from app.services.assistant_takeover import AssistantTakeover, _eligible
from app.services.assistant_takeover_settings import TakeoverSettingsService
from tests.test_assistant_takeover import OWNER, ctx, failed_job, grant  # noqa: F401
from tests.test_takeover_daily import install_model


@pytest.fixture
def startup(ctx, monkeypatch):
    policy = grant(ctx)
    boot = policy.effective_from + timedelta(hours=1)
    monkeypatch.setattr(assistant_takeover, "_now", lambda: boot)
    return AssistantTakeover(ctx[1]), ctx[1], ctx[2], boot


def finish_at(ctx, boot, finished_at, *, status="fail"):
    job_id, run_id = failed_job(ctx, status=status, run_status=status)
    with ctx[1].session_factory() as db:
        run = db.get(Run, run_id)
        run.created_at = boot - timedelta(minutes=30)
        run.finished_at = finished_at
        db.get(SiteJob, job_id).finished_at = finished_at
        db.commit()
    return job_id, run_id


@pytest.mark.parametrize("status", ["fail", "error", "waiting_human"])
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_scan_uses_run_completion_not_creation_or_job_completion(ctx, startup, status, offset):
    takeover, manager, _, boot = startup
    job_id, run_id = finish_at(ctx, boot, boot + timedelta(seconds=offset), status=status)
    # The run is the discovery boundary, even if its job finished on the other side.
    with manager.session_factory() as db:
        db.get(SiteJob, job_id).finished_at = boot - timedelta(seconds=offset)
        db.commit()
    takeover.reconcile()
    takeover.reconcile()
    with manager.session_factory() as db:
        actions = list(db.scalars(select(AssistantTakeoverAction)))
        assert [action.source_job_id for action in actions] == ([job_id] if offset >= 0 else [])
        assert db.get(Run, run_id).created_at.replace(tzinfo=boot.tzinfo) < boot


def test_boundary_is_fixed_at_construction_across_start_stop(startup, monkeypatch):
    takeover, manager, _, boot = startup
    later = boot + timedelta(hours=1)
    monkeypatch.setattr(assistant_takeover, "_now", lambda: later)
    monkeypatch.setattr(takeover, "_poll", AsyncMock())

    async def scenario():
        for _ in range(2):
            await takeover.start()
            await takeover.stop()
            assert takeover._scan_started_at == boot

    asyncio.run(scenario())
    assert AssistantTakeover(manager)._scan_started_at == later
    assert boot.utcoffset() == timedelta()


@pytest.mark.parametrize("status,receipt,expected", [
    ("queued", None, "queued"),
    ("dispatched", None, "unknown"),
    ("dispatched", "{}", "updated"),
    ("unknown", None, "unknown"),
    ("updated", "{}", "updated"),
])
def test_old_persisted_actions_recover_without_replay(ctx, startup, monkeypatch,
                                                    status, receipt, expected):
    takeover, manager, _, boot = startup
    job_id, _ = finish_at(ctx, boot, boot)
    takeover.reconcile()
    request_id = str(uuid4()) if status != "queued" else None
    attempts = int(status != "queued")
    with manager.session_factory() as db:
        action = db.scalar(select(AssistantTakeoverAction))
        action_id = action.id
        action.status, action.import_receipt_json = status, receipt
        action.request_id, action.attempts = request_id, attempts
        action.created_at = boot
        db.commit()

    monkeypatch.setattr(assistant_takeover, "_now", lambda: boot + timedelta(hours=1))
    restarted = AssistantTakeover(manager)
    restarted._recover()
    restarted.reconcile()
    with manager.session_factory() as db:
        actions = list(db.scalars(select(AssistantTakeoverAction)))
        assert len(actions) == 1
        assert actions[0].status == expected
        assert actions[0].source_job_id == job_id
        assert actions[0].attempts == attempts and actions[0].request_id == request_id

    send = AsyncMock(wraps=manager.send_takeover)
    monkeypatch.setattr(manager, "send_takeover", send)
    worker = AsyncMock(side_effect=AssertionError("no worker calls"))
    monkeypatch.setattr(manager, "_worker", worker)
    if status == "queued":
        install_model(monkeypatch, manager)
    else:
        monkeypatch.setattr(manager, "_runtime", Mock(side_effect=AssertionError("no replay")))
    asyncio.run(restarted.process_one())
    worker.assert_not_awaited()
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, action_id)
        if status == "queued":
            send.assert_awaited_once()
            assert action.status == "completed_analysis" and action.attempts == 1
        else:
            send.assert_not_awaited()
            manager._runtime.assert_not_called()
            assert action.status == expected
            assert action.attempts == attempts and action.request_id == request_id


@pytest.mark.parametrize("receipt,expected", [(None, "unknown"), ("{}", "updated")])
def test_claimed_dispatch_without_accepted_user_is_not_finalized(
    ctx, startup, monkeypatch, receipt, expected,
):
    takeover, manager, _, boot = startup
    finish_at(ctx, boot, boot)
    takeover.reconcile()
    with manager.session_factory() as db:
        action = db.scalar(select(AssistantTakeoverAction))
        action_id = action.id
    sid = manager.create_takeover(action_id)
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, action_id)
        action.status, action.attempts = "dispatched", 1
        action.session_id, action.request_id = sid, str(uuid4())
        action.import_receipt_json = receipt
        action.updated_at = boot
        db.commit()

    monkeypatch.setattr(assistant_takeover, "_now", lambda: boot + timedelta(hours=1))
    restarted = AssistantTakeover(manager)
    restarted._recover()
    restarted._recover_finalizations()
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, action_id)
        assert action.status == expected
        assert action.reason == "restart_after_dispatch"
        assert db.get(AssistantTakeoverConclusion, action_id) is None
        assert db.get(AssistantTakeoverNotice, action_id) is None


@pytest.fixture
def accepted_dispatch(ctx, startup):
    takeover, manager, _, boot = startup
    grant(ctx, max_attempts=3)
    finish_at(ctx, boot, boot)
    takeover.reconcile()
    with manager.session_factory() as db:
        action_id = db.scalar(select(AssistantTakeoverAction.id))
    sid = manager.create_takeover(action_id)
    request_id = str(uuid4())
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, action_id)
        action.status, action.attempts, action.request_id = "dispatched", 1, request_id
        action.updated_at = boot - timedelta(seconds=10)
        manager._message(db, sid, "user", "Analyze this failure", request_id=request_id)
        db.commit()
    return takeover, manager, action_id, sid, request_id, boot


@pytest.mark.parametrize("analysis", [False, True])
@pytest.mark.parametrize("outcome,expected", [
    (None, "unknown"), ({}, "unknown"), ([], "unknown"),
    ({"ok": 1}, "unknown"), ({"ok": "true"}, "unknown"),
    ({"ok": False, "code": "provider_timeout"}, "failed"),
    ({"ok": True}, "completed_analysis"),
])
def test_recovery_never_requeues_accepted_turns(
    accepted_dispatch, monkeypatch, outcome, expected, analysis,
):
    takeover, manager, action_id, sid, request_id, _ = accepted_dispatch
    with manager.session_factory() as db:
        row = db.get(AssistantSession, sid)
        data = manager._decrypt(row)
        data["_turn_outcomes"] = {} if outcome is None else {request_id: outcome}
        manager._encrypt(row, data)
        if analysis:
            manager._message(db, sid, "assistant", "The login selector needs review.")
        db.commit()
    monkeypatch.setattr(manager, "send_takeover", AsyncMock())
    monkeypatch.setattr(manager, "_runtime", Mock(side_effect=AssertionError("no replay")))
    takeover._recover()
    takeover._recover_finalizations()
    asyncio.run(takeover.process_one())
    manager.send_takeover.assert_not_awaited()
    manager._runtime.assert_not_called()
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, action_id)
        assert action.status == ("failed" if expected == "completed_analysis" and not analysis else expected)
        assert action.attempts == 1 and action.request_id == request_id


def test_recovery_preserves_import_proof_without_typed_outcome(accepted_dispatch):
    from tests.test_assistant_takeover_conclusions import update_receipt

    takeover, manager, action_id, _, _, _ = accepted_dispatch
    update_receipt((manager, action_id))
    with manager.session_factory() as db:
        db.get(AssistantTakeoverAction, action_id).status = "dispatched"
        db.commit()
    takeover._recover()
    takeover._recover_finalizations()
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, action_id)
        assert action.status == "updated" and action.reason == "validated_import"
        assert db.get(AssistantTakeoverNotice, action_id) is None


@pytest.mark.parametrize("busy", ["task", "running"])
@pytest.mark.parametrize("when", ["before_scan", "before_complete"])
def test_finalization_recovery_skips_live_sessions(accepted_dispatch, monkeypatch, busy, when):
    takeover, manager, action_id, sid, _, _ = accepted_dispatch
    original = takeover._complete

    def make_busy():
        if busy == "task":
            manager.tasks[sid] = Mock()
        else:
            with manager.session_factory() as db:
                db.get(AssistantSession, sid).status = "running"
                db.commit()

    def racing_complete(*args, **kwargs):
        make_busy()
        return original(*args, **kwargs)

    if when == "before_scan":
        make_busy()
    else:
        monkeypatch.setattr(takeover, "_complete", racing_complete)
    try:
        takeover._recover_finalizations()
    finally:
        manager.tasks.pop(sid, None)
    with manager.session_factory() as db:
        assert db.get(AssistantTakeoverAction, action_id).status == "dispatched"


def test_finalization_failure_backoff_and_private_logging(accepted_dispatch, monkeypatch, caplog):
    takeover, manager, action_id, _, _, boot = accepted_dispatch
    complete = Mock(side_effect=ValueError("private-delivery-secret"))
    monkeypatch.setattr(takeover, "_complete", complete)
    takeover._recover_finalizations()
    takeover._recover_finalizations()
    complete.assert_called_once()
    assert "finalization recovery failed" in caplog.text
    assert "private-delivery-secret" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, action_id)
        assert action.status == "dispatched"
        assert action.updated_at.replace(tzinfo=boot.tzinfo) == boot
    monkeypatch.setattr(assistant_takeover, "_now", lambda: boot + timedelta(seconds=6))
    takeover._recover_finalizations()
    assert complete.call_count == 2


@pytest.mark.parametrize("invalid", ["owner", "missing"])
@pytest.mark.parametrize("receipt,expected", [(None, "unknown"), ("{}", "updated")])
def test_invalid_session_skipped_then_startup_falls_back(accepted_dispatch, invalid, receipt, expected):
    takeover, manager, action_id, sid, request_id, _ = accepted_dispatch
    with manager.session_factory() as db:
        row = db.get(AssistantSession, sid)
        if invalid == "owner":
            row.owner = "another-owner"
        else:
            db.delete(row)
        db.get(AssistantTakeoverAction, action_id).import_receipt_json = receipt
        db.commit()
    takeover._recover_finalizations()
    if invalid == "owner":
        takeover._complete(action_id, sid, request_id, False)
    with manager.session_factory() as db:
        assert db.get(AssistantTakeoverAction, action_id).status == "dispatched"
    takeover._recover()
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, action_id)
        assert action.status == expected and action.reason == "restart_after_dispatch"
        assert db.get(AssistantTakeoverConclusion, action_id) is None
        assert db.get(AssistantTakeoverNotice, action_id) is None


def test_new_rules_do_not_backfill_old_waiting_jobs(ctx, startup, monkeypatch):
    takeover, manager, _, boot = startup
    job_id, run_id = finish_at(ctx, boot, boot - timedelta(seconds=1), status="waiting_human")
    monkeypatch.setattr(assistant_takeover, "DIAGNOSTIC_STATUSES", frozenset({"fail", "error"}))
    takeover.reconcile()
    monkeypatch.setattr(assistant_takeover, "DIAGNOSTIC_STATUSES",
                        frozenset({"fail", "error", "waiting_human"}))
    with manager.session_factory() as db:
        policy = TakeoverSettingsService(db).view()
        assert _eligible(db, db.get(SiteJob, job_id), db.get(Run, run_id), policy)
    takeover.reconcile()
    with manager.session_factory() as db:
        assert list(db.scalars(select(AssistantTakeoverAction))) == []
    fresh_job, _ = finish_at(ctx, boot, boot + timedelta(seconds=1), status="waiting_human")
    takeover.reconcile()
    with manager.session_factory() as db:
        assert list(db.scalars(select(AssistantTakeoverAction.source_job_id))) == [fresh_job]


def test_large_scan_pages_and_wraps_without_admitting_old_finalizations(startup):
    takeover, manager, site_id, boot = startup
    with manager.session_factory() as db:
        runs = [Run(created_by=OWNER, trigger="manual", status="fail",
                    created_at=boot - timedelta(minutes=30),
                    finished_at=boot + timedelta(seconds=offset)) for offset in (-1, 1)]
        db.add_all(runs)
        db.flush()
        # Interleave old/new IDs so every scan page must retain the boundary.
        db.execute(insert(SiteJob), [{
            "run_id": runs[index % 2].id, "site_id": site_id, "site_name": "Failure",
            "lane": "proxy_general", "status": "fail", "finished_at": boot,
        } for index in range(410)])
        fresh_ids = set(db.scalars(select(SiteJob.id).where(SiteJob.run_id == runs[1].id)))
        db.commit()
    for expected_count in (100, 200, 205, 205):
        takeover.reconcile()
        with manager.session_factory() as db:
            sources = set(db.scalars(select(AssistantTakeoverAction.source_job_id)))
            assert len(sources) == expected_count
            assert sources <= fresh_ids
    assert sources == fresh_ids and takeover._scan_after == 0
