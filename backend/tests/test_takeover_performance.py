from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import event, insert, select
from starlette.requests import Request

from app.api.v1.assistant_takeover import put_settings
from app.models.execution import JobStep, Run, SiteJob
from app.models.assistant import AssistantMessage
from app.models.assistant_takeover import AssistantTakeoverAction
from app.models.workflow import Site
from app.schemas.assistant_takeover import TakeoverSettingsUpdate, TakeoverValues
from app.schemas.workflow import SiteWrite
from app.services.assistant_takeover import _eligible, _mutation_clear, _site_uncertain
from app.services.assistant_takeover_performance import TakeoverPhaseCache
from app.services.assistant_takeover_settings import TakeoverSettingsService
from app.services.workflows import WorkflowService
from app.services.run_notification_dispatch import plan_run_takeovers
from tests.test_assistant_takeover import ctx, failed_job, grant  # noqa: F401
from tests.test_assistant_workflows import DRAFT


@pytest.mark.parametrize("row_count", [500, 2000])
@pytest.mark.parametrize("run_count,site_count", [(1, 1), (4, 2)])
@pytest.mark.parametrize("status", ["fail", "waiting_human"])
def test_phase_sql_scales_with_unique_runs_and_sites(ctx, row_count, run_count, site_count, status):
    _, manager, site_id = ctx
    grant(ctx)
    with manager.session_factory() as db:
        site_ids = [site_id]
        if site_count == 2:
            site = WorkflowService(db, manager.settings, manager.crypto).create_site(SiteWrite(
                name="Second", base_url="https://second.example/", **DRAFT))
            site_ids.append(site.id)
        policy = TakeoverSettingsService(db).view()
        TakeoverSettingsService(db).update(TakeoverSettingsUpdate(
            expected_revision=policy.revision, confirm=True,
            values=policy.values.model_copy(update={"site_ids": None})), "takeover-owner")
    with manager.session_factory() as db:
        now = datetime.now(timezone.utc)
        runs = [Run(created_by="test", status=status, finished_at=now) for _ in range(run_count)]
        db.add_all(runs)
        db.flush()
        db.execute(insert(SiteJob), [{
            "run_id": runs[index % run_count].id,
            "site_id": site_ids[(index // run_count) % site_count],
            "site_name": "Failure", "lane": "general", "status": status, "finished_at": now,
        } for index in range(row_count)])
        db.commit()

    with manager.session_factory() as db:
        policy = TakeoverSettingsService(db).view()
        runs = {run.id: run for run in db.scalars(select(Run))}
        jobs = list(db.scalars(select(SiteJob)))
        statements = []

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        engine = db.get_bind()
        event.listen(engine, "before_cursor_execute", record)
        try:
            cache = TakeoverPhaseCache()
            assert all(_eligible(db, job, runs[job.run_id], policy, cache) for job in jobs)
            first_count = len(statements)
            assert all(_eligible(db, job, runs[job.run_id], policy, cache) for job in jobs)
            assert len(statements) == first_count
        finally:
            event.remove(engine, "before_cursor_execute", record)
        assert first_count == site_count
        assert not cache.run_sources and not cache.site_uncertain and not cache.site_clear


@pytest.mark.parametrize("raw", [
    '{"nested":[{"deeper":{"outcome_uncertain":true}}]}',
    '{"nested":',
    '{"duplicate":1,"duplicate":2}',
])
def test_site_uncertainty_checks_oldest_history_and_fails_closed(ctx, raw):
    _, manager, site_id = ctx
    grant(ctx)
    with manager.session_factory() as db:
        run = Run(created_by="test", status="success", finished_at=datetime.now(timezone.utc))
        db.add(run)
        db.flush()
        db.execute(insert(SiteJob), [{
            "run_id": run.id, "site_id": site_id, "site_name": "History", "lane": "general",
            "status": "success", "result_json": "{}",
        } for _ in range(2000)])
        oldest = db.scalar(select(SiteJob.id).order_by(SiteJob.id))
        step = JobStep(site_job_id=oldest, step_key="old", feature_type="flow.open",
                       label="Old", status="success", data_json=raw)
        db.add(step)
        db.commit()
        cache = TakeoverPhaseCache()
        assert _site_uncertain(db, site_id, cache)
        step.data_json = "{}"
        db.commit()
        assert not _site_uncertain(db, site_id, TakeoverPhaseCache())
    job_id, run_id = failed_job(ctx, snapshot=False)
    with manager.session_factory() as db:
        policy = TakeoverSettingsService(db).view()
        job, run = db.get(SiteJob, job_id), db.get(Run, run_id)
        assert _eligible(db, job, run, policy, TakeoverPhaseCache())
        db.get(SiteJob, oldest).result_json = raw
        db.commit()
        assert _eligible(db, job, run, policy, TakeoverPhaseCache())
        assert _eligible(db, job, run, policy)
        assert not _mutation_clear(db, SimpleNamespace(site_id=site_id, source_job_id=job_id,
            source_run_id=run_id, site_history_key=db.get(Site, site_id).history_key,
            site_origin="https://example.com"))


def test_phase_cache_does_not_bypass_cheap_policy_checks(ctx):
    _, manager, site_id = ctx
    grant(ctx)
    job_id, run_id = failed_job(ctx, snapshot=False)
    with manager.session_factory() as db:
        policy = TakeoverSettingsService(db).view()
        job, run = db.get(SiteJob, job_id), db.get(Run, run_id)
        cache = TakeoverPhaseCache()
        assert _eligible(db, job, run, policy, cache)
        policy.values.enabled = False
        assert not _eligible(db, job, run, policy, cache)
        policy.values.enabled = True
        policy.values.site_ids = []
        assert not _eligible(db, job, run, policy, cache)
        policy.values.site_ids = [site_id]
        run.cancel_requested = True
        assert not _eligible(db, job, run, policy, cache)


def test_new_phase_rechecks_source_and_site_mutations(ctx):
    _, manager, site_id = ctx
    grant(ctx)
    job_id, run_id = failed_job(ctx, snapshot=False)
    with manager.session_factory() as db:
        policy = TakeoverSettingsService(db).view()
        job, run = db.get(SiteJob, job_id), db.get(Run, run_id)
        assert _eligible(db, job, run, policy, TakeoverPhaseCache())
        job.result_json = '{"nested":[{"status":"unknown"}]}'
        db.commit()
        assert _eligible(db, job, run, policy, TakeoverPhaseCache())
        assert _eligible(db, job, run, policy)
        assert not _mutation_clear(db, SimpleNamespace(site_id=site_id, source_job_id=job_id,
            source_run_id=run_id, site_history_key=db.get(Site, site_id).history_key,
            site_origin="https://example.com"))
        job.result_json = "{}"
        db.get(Site, site_id).enabled = False
        db.commit()
        assert not _eligible(db, job, run, policy, TakeoverPhaseCache())
        db.get(Site, site_id).enabled = True
        db.commit()
        assert _eligible(db, job, run, policy, TakeoverPhaseCache())


def test_settings_update_owns_worker_session_and_wakes_on_loop(ctx, monkeypatch):
    takeover, manager, _ = ctx
    loop_thread = threading.get_ident()
    session_threads = []

    @contextmanager
    def session_factory():
        session_threads.append(threading.get_ident())
        with manager.session_factory() as db:
            yield db

    blocking = threading.Event()
    original = TakeoverSettingsService.update

    def slow_update(self, payload, owner):
        assert threading.get_ident() != loop_thread
        blocking.set()
        time.sleep(0.15)
        try:
            return original(self, payload, owner)
        finally:
            blocking.clear()

    def wake():
        assert threading.get_ident() == loop_thread
        asyncio.get_running_loop()

    monkeypatch.setattr(TakeoverSettingsService, "update", slow_update)
    monkeypatch.setattr(takeover, "wake", Mock(side_effect=wake))
    request = Request({"type": "http", "app": SimpleNamespace(state=SimpleNamespace(
        session_factory=session_factory, assistant_takeover=takeover))})
    payload = TakeoverSettingsUpdate(expected_revision=0, confirm=True, values=TakeoverValues())

    async def exercise():
        heartbeat = 0
        task = asyncio.create_task(put_settings(payload, request, "test-owner"))
        while not task.done():
            await asyncio.sleep(0.005)
            if blocking.is_set():
                heartbeat += 1
        return heartbeat, await task

    heartbeat, result = asyncio.run(exercise())
    assert heartbeat >= 5 and result.revision == 1
    assert session_threads and all(identifier != loop_thread for identifier in session_threads)
    takeover.wake.assert_called_once_with()


@pytest.mark.parametrize("canceled", [False, True])
@pytest.mark.parametrize("durable", [False, True])
def test_execute_run_blocking_terminal_phase_keeps_loop_responsive(ctx, monkeypatch, canceled, durable):
    _, manager, _ = ctx
    executor = manager.executor
    with manager.session_factory() as db:
        run = Run(created_by="test", cancel_requested=canceled)
        db.add(run)
        db.commit()
        run_id = run.id

    loop_thread = threading.get_ident()
    session_factory = executor.session_factory
    session_threads = []

    @contextmanager
    def tracked_session():
        owner = threading.get_ident()
        session_threads.append(owner)
        with session_factory() as db:
            yield db
            assert threading.get_ident() == owner

    monkeypatch.setattr(executor, "session_factory", tracked_session)
    blocking = threading.Event()
    worker_threads = []
    if durable:
        dispatcher = executor.notification_dispatch
        original = dispatcher.register

        def slow_register(db, identifier):
            worker_threads.append(threading.get_ident())
            blocking.set()
            time.sleep(0.15)
            try:
                return original(db, identifier)
            finally:
                blocking.clear()

        monkeypatch.setattr(dispatcher, "register", slow_register)
        monkeypatch.setattr(dispatcher, "ensure", Mock(side_effect=AssertionError("already registered")))

        def wake():
            assert threading.get_ident() == loop_thread
            asyncio.get_running_loop()

        monkeypatch.setattr(dispatcher, "wake", Mock(side_effect=wake))
    else:
        monkeypatch.delattr(executor, "notification_dispatch")
        original = executor._prepare_run if canceled else executor._finalize_run

        def slow_phase(*args, **kwargs):
            worker_threads.append(threading.get_ident())
            blocking.set()
            time.sleep(0.15)
            try:
                return original(*args, **kwargs)
            finally:
                blocking.clear()

        monkeypatch.setattr(executor, "_prepare_run" if canceled else "_finalize_run", slow_phase)
        monkeypatch.setattr(executor, "_notify_run", AsyncMock())

    wakes = []

    def takeover_wake():
        wakes.append(threading.get_ident())

    monkeypatch.setattr(executor.takeover, "wake", takeover_wake)

    async def exercise():
        heartbeat = 0
        task = asyncio.create_task(executor._execute_run(run_id))
        while not task.done():
            await asyncio.sleep(0.005)
            if blocking.is_set():
                heartbeat += 1
        await task
        await asyncio.sleep(0)
        return heartbeat

    assert asyncio.run(exercise()) >= 5
    assert worker_threads and all(identifier != loop_thread for identifier in worker_threads)
    assert session_threads and all(identifier != loop_thread for identifier in session_threads)
    assert wakes == ([] if canceled else [loop_thread])
    if durable:
        dispatcher.wake.assert_called_once_with()
        dispatcher.ensure.assert_not_called()
    else:
        executor._notify_run.assert_awaited_once_with(run_id)
    with session_factory() as db:
        run = db.get(Run, run_id)
        assert run.status == ("canceled" if canceled else "fail")
        assert json.loads(run.summary_json) == {}


@pytest.mark.parametrize("phase", ["_select", "_claim", "_complete", "create_takeover", "_send_failed"])
def test_takeover_process_phases_do_not_block_loop(ctx, monkeypatch, phase):
    takeover, manager, _ = ctx
    grant(ctx)
    failed_job(ctx)
    takeover.reconcile()
    loop_thread = threading.get_ident()
    monkeypatch.setattr(manager, "_runtime", lambda: {})
    original_sessions = manager.session_factory
    session_threads = []

    @contextmanager
    def tracked_session():
        owner = threading.get_ident()
        session_threads.append(owner)
        with original_sessions() as db:
            yield db
            assert threading.get_ident() == owner

    monkeypatch.setattr(manager, "session_factory", tracked_session)

    async def send(owner, sid, payload, *, action_id):
        assert threading.get_ident() == loop_thread
        if phase == "_send_failed":
            raise RuntimeError("lost acknowledgement")
        # Simulate an accepted completed model turn without DB work on the loop.
        def accept():
            with manager.session_factory() as db:
                manager._message(db, sid, "user", payload.content, request_id=str(payload.request_id))
                manager._message(db, sid, "assistant", "Analysis complete.")
                db.commit()
        await asyncio.to_thread(accept)

    monkeypatch.setattr(manager, "send_takeover", AsyncMock(side_effect=send))
    target = manager if phase == "create_takeover" else takeover
    original = getattr(target, phase)
    blocking = threading.Event()
    results = []

    def slow(*args):
        assert threading.get_ident() != loop_thread
        blocking.set()
        time.sleep(0.1)
        try:
            result = original(*args)
            # Cross-thread phase results contain no live ORM state.
            json.dumps(result)
            results.append(result)
            return result
        finally:
            blocking.clear()

    monkeypatch.setattr(target, phase, slow)

    async def exercise():
        ticks = 0
        task = asyncio.create_task(takeover.process_one())
        while not task.done():
            await asyncio.sleep(0.005)
            ticks += blocking.is_set()
        await task
        return ticks

    assert asyncio.run(exercise()) >= 5
    assert results and session_threads
    assert all(identifier != loop_thread for identifier in session_threads)
    manager.send_takeover.assert_awaited_once()
    with original_sessions() as db:
        action = db.scalar(select(AssistantTakeoverAction))
        assert action.status == ("unknown" if phase == "_send_failed" else "completed_analysis")


def test_poll_offloads_reconcile_and_stop_drains_worker(ctx, monkeypatch):
    takeover, _, _ = ctx
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    loop_thread = threading.get_ident()

    def reconcile():
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(3)
        finished.set()

    monkeypatch.setattr(takeover, "reconcile", reconcile)
    monkeypatch.setattr(takeover, "process_one", AsyncMock())

    async def exercise():
        takeover._task = asyncio.create_task(takeover._poll())
        async with asyncio.timeout(3):
            while not entered.is_set():
                await asyncio.sleep(0.005)
            stop = asyncio.create_task(takeover.stop())
            await asyncio.sleep(0.03)
            assert not stop.done()
            release.set()
            await stop
        assert finished.is_set()

    asyncio.run(exercise())
    takeover.process_one.assert_not_awaited()


@pytest.mark.parametrize("change", ["uncertainty", "active_job", "policy", "stop"])
def test_claim_rechecks_after_runtime_await(ctx, monkeypatch, change):
    takeover, manager, site_id = ctx
    grant(ctx)
    job_id, run_id = failed_job(ctx)
    takeover.reconcile()

    def runtime():
        if change == "stop":
            takeover._stopping = True
        elif change == "policy":
            grant(ctx)
        else:
            with manager.session_factory() as db:
                if change == "uncertainty":
                    db.get(SiteJob, job_id).result_json = '{"nested":{"outcome_uncertain":true}}'
                else:
                    active = Run(created_by="test")
                    db.add(active)
                    db.flush()
                    db.add(SiteJob(run_id=active.id, site_id=site_id, site_name="New",
                                   lane="general", status="queued"))
                db.commit()
        return {}

    monkeypatch.setattr(manager, "_runtime", runtime)
    monkeypatch.setattr(manager, "send_takeover", AsyncMock())
    asyncio.run(takeover.process_one())
    if change == "uncertainty":
        manager.send_takeover.assert_awaited_once()
        with manager.session_factory() as db:
            action = db.scalar(select(AssistantTakeoverAction))
            assert action.attempts == 1 and action.mode == "analysis"
            assert action.import_receipt_json is None
        return
    manager.send_takeover.assert_not_awaited()
    with manager.session_factory() as db:
        action = db.scalar(select(AssistantTakeoverAction))
        assert action.attempts == 0 and action.request_id is None
        assert action.status == ("revoked" if change == "policy" else "queued")


@pytest.mark.parametrize("after_commit", [False, True])
def test_cancel_claim_drains_worker_without_leaving_unsent_dispatch(ctx, monkeypatch, after_commit):
    takeover, manager, _ = ctx
    grant(ctx)
    failed_job(ctx)
    takeover.reconcile()
    monkeypatch.setattr(manager, "_runtime", lambda: {})
    monkeypatch.setattr(manager, "send_takeover", AsyncMock())
    original = takeover._claim
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def claim(*args):
        result = original(*args) if after_commit else None
        entered.set()
        assert release.wait(3)
        if not after_commit:
            result = original(*args)
        finished.set()
        return result

    monkeypatch.setattr(takeover, "_claim", claim)

    async def exercise():
        takeover._task = asyncio.create_task(takeover.process_one())
        async with asyncio.timeout(3):
            while not entered.is_set():
                await asyncio.sleep(0.005)
            stop = asyncio.create_task(takeover.stop())
            await asyncio.sleep(0.02)
            assert not stop.done()
            release.set()
            await stop
        assert finished.is_set()

    asyncio.run(exercise())
    manager.send_takeover.assert_not_awaited()
    with manager.session_factory() as db:
        action = db.scalar(select(AssistantTakeoverAction))
        assert action.status == "queued" and action.attempts == 0
        assert not db.scalar(select(AssistantMessage.id))


@pytest.mark.parametrize("row_count", [500, 2000])
def test_notification_planner_does_not_scan_safety_history(ctx, monkeypatch, row_count):
    import app.services.assistant_takeover as module

    _, manager, site_id = ctx
    grant(ctx)
    now = datetime.now(timezone.utc)
    with manager.session_factory() as db:
        run = Run(created_by="test", status="fail", finished_at=now)
        db.add(run)
        db.flush()
        run_id = run.id
        db.execute(insert(SiteJob), [{
            "run_id": run_id, "site_id": site_id, "site_name": "Failure", "lane": "general",
            "status": "fail", "finished_at": now,
        } for _ in range(row_count)])
        db.commit()
    original = module._site_uncertain
    scans = []

    def uncertain(db, identifier, cache=None):
        if cache is None or identifier not in cache.site_uncertain:
            scans.append(identifier)
        return original(db, identifier, cache)

    monkeypatch.setattr(module, "_site_uncertain", uncertain)
    with manager.session_factory() as db:
        statements = []

        def record(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith("SELECT"):
                statements.append(statement)

        engine = db.get_bind()
        event.listen(engine, "before_cursor_execute", record)
        try:
            plan_run_takeovers(db, run_id)
            db.commit()
        finally:
            event.remove(engine, "before_cursor_execute", record)
        assert scans == []
        # Per-job binding/signature lookups remain linear, not full-history scans.
        assert len(statements) <= 3 * row_count + 20
        source_scans = [sql for sql in statements
                        if "FROM site_jobs" in sql and "WHERE site_jobs.run_id =" in sql]
        assert len(source_scans) == 1
        assert len(list(db.scalars(select(AssistantTakeoverAction)))) == row_count


def test_concurrent_consumers_claim_only_one_model_send(ctx, monkeypatch):
    takeover, manager, _ = ctx
    grant(ctx)
    failed_job(ctx)
    takeover.reconcile()
    with manager.session_factory() as db:
        action_id = db.scalar(select(AssistantTakeoverAction.id))
    # Both consumers resume from runtime with the same queued action and chat.
    manager.create_takeover(action_id)
    barrier = threading.Barrier(2)

    def runtime():
        barrier.wait(timeout=3)
        return {}

    monkeypatch.setattr(manager, "_runtime", runtime)
    monkeypatch.setattr(manager, "send_takeover", AsyncMock())

    async def exercise():
        await asyncio.gather(takeover.process_one(), takeover.process_one())

    asyncio.run(exercise())
    manager.send_takeover.assert_awaited_once()
    with manager.session_factory() as db:
        assert db.get(AssistantTakeoverAction, action_id).attempts == 1


def test_stop_during_claim_evidence_rolls_back_before_dispatch(ctx, monkeypatch):
    takeover, manager, _ = ctx
    grant(ctx)
    failed_job(ctx)
    takeover.reconcile()
    monkeypatch.setattr(manager, "_runtime", lambda: {})
    monkeypatch.setattr(manager, "send_takeover", AsyncMock())
    entered, release = threading.Event(), threading.Event()

    def prior(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return "old evidence"

    monkeypatch.setattr("app.services.assistant_takeover_conclusions.prior_takeover_context", prior)

    async def exercise():
        takeover._task = asyncio.create_task(takeover.process_one())
        async with asyncio.timeout(3):
            while not entered.is_set():
                await asyncio.sleep(0.005)
            stop = asyncio.create_task(takeover.stop())
            await asyncio.sleep(0.02)
            assert not stop.done()
            release.set()
            await stop

    asyncio.run(exercise())
    manager.send_takeover.assert_not_awaited()
    with manager.session_factory() as db:
        action = db.scalar(select(AssistantTakeoverAction))
        assert action.status == "queued" and action.request_id is None and action.attempts == 0


def test_selection_admits_uncertain_history_without_decoding_it(ctx, monkeypatch):
    import app.services.assistant_takeover as module

    takeover, manager, site_id = ctx
    grant(ctx)
    with manager.session_factory() as db:
        now = datetime.now(timezone.utc)
        run = Run(created_by="test", status="fail", finished_at=now)
        db.add(run)
        db.flush()
        db.execute(insert(SiteJob), [{
            "run_id": run.id, "site_id": site_id, "site_name": "Failure", "lane": "general",
            "status": "fail", "finished_at": now,
        } for _ in range(100)])
        db.flush()
        plan_run_takeovers(db, run.id)
        db.scalar(select(SiteJob).order_by(SiteJob.id)).result_json = '{"nested":{"status":"unknown"}}'
        db.commit()
    original = module._strict_json
    decoded = []

    def decode(raw):
        decoded.append(raw)
        return original(raw)

    monkeypatch.setattr(module, "_strict_json", decode)
    monkeypatch.setattr(module, "_mutation_clear", Mock(return_value=False))
    monkeypatch.setattr(module, "_source_current", Mock(return_value=True))
    selected = takeover._select()
    assert selected is not None
    assert decoded == []
    module._mutation_clear.assert_called_once()
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, selected[0])
        assert action.mode == "analysis" and action.reason == "source_outcome_uncertain"
