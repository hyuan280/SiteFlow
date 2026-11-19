from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from app.core.crypto import CryptoService
from app.core.db import Base, create_db_engine, create_session_factory
from app.models.execution import JobStep, Run, SiteJob
from app.models.workflow import Site, WorkflowStep
from app.services.assistant_run_guard import create_guard
from app.services.executor import ExecutionManager
from app.services.browser_dispatch import (
    DISPATCH_ERROR, DISPATCH_GENERATION_ERROR, DispatchClient, DispatchError,
    DispatchTicket, DispatchTransportError,
)
from app.services.workflows import WorkflowService


LANES = ("general", "interaction", "proxy_general", "proxy_interaction")


class FakeDispatchClient:
    """Worker reservation simulator, with capacity measured in distinct lanes."""

    def __init__(self, capacity=1):
        self.capacity = capacity
        self.generation = str(uuid4())
        self.tickets = {}
        self.closed = set()
        self.released = set()
        self.requests = []
        self.blocked = set()
        self.failed = set()

    async def renew(self, owner, run_id, tickets):
        self.requests.append((owner, run_id, [(ticket.id, ticket.lane) for ticket in tickets]))
        assert 1 <= len(tickets) <= 4
        assert [ticket.lane for ticket in tickets] == sorted(
            [ticket.lane for ticket in tickets], key=LANES.index)
        for ticket in tickets:
            key = owner, ticket.id
            assert owner not in self.closed and key not in self.released
            self.tickets.setdefault(key, {"lane": ticket.lane, "state": "pending"})
        occupied = {row["lane"] for row in self.tickets.values() if row["state"] == "granted"}
        for row in self.tickets.values():
            if row["lane"] in self.failed:
                row["state"] = "failed"
            elif (row["state"] == "pending" and row["lane"] not in self.blocked
                  and (row["lane"] in occupied or len(occupied) < self.capacity)):
                row["state"] = "granted"
                occupied.add(row["lane"])
        return self.generation, {ticket.id: self.tickets[owner, ticket.id]["state"] for ticket in tickets}

    async def release(self, owner, ticket):
        self.released.add((owner, ticket))
        self.tickets.pop((owner, ticket), None)

    async def close(self, owner):
        self.closed.add(owner)
        for key in list(self.tickets):
            if key[0] == owner:
                self.released.add(key)
                del self.tickets[key]

    def validate(self, payload):
        cap = payload["dispatch_reservation"]
        assert cap["generation"] == self.generation
        row = self.tickets[cap["owner"], cap["id"]]
        assert (row["lane"], row["state"]) == (payload["lane_key"], "granted")


@pytest.fixture
def manager(settings, monkeypatch):
    engine = create_db_engine(settings.database_url)
    Base.metadata.create_all(engine)
    manager = ExecutionManager(create_session_factory(engine), settings, CryptoService(b"x" * 32))
    monkeypatch.setattr(manager, "schedule_run_notification", lambda _run_id: None)
    manager.dispatch_simulator = FakeDispatchClient()
    monkeypatch.setattr(manager, "_dispatch_client", lambda: manager.dispatch_simulator)
    yield manager
    engine.dispose()


def _create_run(manager, lanes, *, trigger="manual"):
    with manager.session_factory() as session:
        run = Run(created_by="admin", trigger=trigger)
        session.add(run)
        session.flush()
        job_ids = []
        for lane in lanes:
            site = Site(name=f"site-{run.id}-{len(job_ids)}", base_url="https://example.test",
                        lane_policy=lane)
            session.add(site)
            session.flush()
            session.add(WorkflowStep(owner_type="site", owner_id=site.id, step_key="open",
                                     feature_type="flow.open", category="flow", position=1))
            job = SiteJob(run_id=run.id, site_id=site.id, site_name=site.name, lane=lane)
            session.add(job)
            session.flush()
            job_ids.append(job.id)
        if trigger == "assistant":
            assert len(lanes) == 1
            create_guard(session, run.id, site.id,
                         WorkflowService(session, manager.settings, manager.crypto))
        session.commit()
        return run.id, job_ids


def _success():
    return {"status": "success", "message": "final result", "steps": [{
        "step_key": "open", "feature_type": "flow.open", "status": "success",
        "data": {"final": True},
    }]}


def _assert_finished(manager, run_id, status="success"):
    with manager.session_factory() as session:
        assert session.get(Run, run_id).status == status
        jobs = session.query(SiteJob).filter_by(run_id=run_id).all()
        assert all(job.finished_at is not None for job in jobs)
        assert all(job.status not in {"queued", "running"} for job in jobs)


def test_canonical_waves_ignore_reverse_job_ids_with_one_global_slot(manager):
    run_id, _ = _create_run(manager, list(reversed(LANES)))
    seen = []

    async def execute(payload):
        seen.append(payload["lane_key"])
        return _success()

    manager._job_semaphore = asyncio.Semaphore(1)
    manager._post_execute = execute
    asyncio.run(manager._execute_run(run_id))
    assert seen == list(LANES)
    _assert_finished(manager, run_id)


def test_same_lane_parallelism_joins_before_next_wave(manager):
    run_id, job_ids = _create_run(manager, ["interaction", "general", "general", "general"])

    async def run():
        manager._job_semaphore = asyncio.Semaphore(2)
        two_active = asyncio.Event()
        release = asyncio.Event()
        active = set()
        seen = []
        finished = []

        async def execute(payload):
            job_id = payload["job_id"]
            seen.append(job_id)
            active.add(job_id)
            if payload["lane_key"] == "general":
                if len(active) == 2:
                    two_active.set()
                await release.wait()
            else:
                assert set(finished) == set(job_ids[1:])
                assert active == {job_ids[0]}
            active.remove(job_id)
            finished.append(job_id)
            return _success()

        manager._post_execute = execute
        task = asyncio.create_task(manager._execute_run(run_id))
        try:
            await asyncio.wait_for(two_active.wait(), 2)
            assert seen == job_ids[1:3]
            release.set()
            await asyncio.wait_for(task, 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    _assert_finished(manager, run_id)


@pytest.mark.parametrize("slots", [1, 2])
@pytest.mark.parametrize("source,target", [("general", "interaction"),
                                          ("proxy_general", "proxy_interaction")])
def test_fallback_releases_slot_and_joins_source_wave(manager, slots, source, target):
    run_id, job_ids = _create_run(manager, [source] * (slots + 1))

    async def run():
        manager._job_semaphore = asyncio.Semaphore(slots)
        last_general = asyncio.Event()
        seen = []

        async def execute(payload):
            job_id = payload["job_id"]
            seen.append((job_id, payload["lane_key"]))
            if payload["lane_key"] == source:
                if job_id == job_ids[0]:
                    return {"status": "fallback_interaction", "steps": []}
                if job_id == job_ids[-1]:
                    last_general.set()
                # With inline fallback, every slot can be held waiting for this job.
                await last_general.wait()
            else:
                assert last_general.is_set()
                assert manager._active_jobs.get(run_id) == {job_id}
            return _success()

        manager._post_execute = execute
        await asyncio.wait_for(manager._execute_run(run_id), 2)
        assert seen == [(job_id, source) for job_id in job_ids] + [(job_ids[0], target)]
        assert manager._job_semaphore._value == slots
        assert not manager._active_jobs

    asyncio.run(run())
    _assert_finished(manager, run_id)
    with manager.session_factory() as session:
        job = session.get(SiteJob, job_ids[0])
        assert (job.lane, job.attempt, job.message) == (target, 2, "final result")
        assert json.loads(job.result_json)["status"] == "success"
        steps = session.query(JobStep).filter_by(site_job_id=job.id).all()
        assert len(steps) == 1 and json.loads(steps[0].data_json) == {"final": True}


@pytest.mark.parametrize("trigger", ["manual", "assistant"])
@pytest.mark.parametrize("source,target", [("general", "interaction"),
                                          ("proxy_general", "proxy_interaction")])
@pytest.mark.parametrize("retries", [("restart", "fallback_interaction", "restart"),
                                    ("fallback_interaction", "restart", "restart")])
def test_mixed_retries_preserve_fallback_and_restart_limits(manager, trigger, source, target, retries):
    run_id, job_ids = _create_run(manager, [source], trigger=trigger)
    seen = []

    async def execute(payload):
        seen.append((payload["attempt"], payload["lane_key"]))
        return {"status": retries[len(seen) - 1], "steps": []}

    manager._post_execute = execute
    asyncio.run(manager._execute_run(run_id))
    expected = ([(1, source), (2, source)] if retries[0] == "restart" else
                [(1, source), (2, target), (3, target)])
    assert seen == expected
    with manager.session_factory() as session:
        job = session.get(SiteJob, job_ids[0])
        assert job.status == "error" and job.attempt == len(expected)
        assert job.lane == expected[-1][1]
    _assert_finished(manager, run_id, "fail")


@pytest.mark.parametrize("lane", LANES)
def test_repeated_fallback_cannot_cycle_or_retry_forever(manager, lane):
    run_id, _ = _create_run(manager, [lane])
    seen = []

    async def execute(payload):
        seen.append((payload["attempt"], payload["lane_key"]))
        return {"status": "fallback_interaction", "steps": []}

    manager._post_execute = execute
    asyncio.run(manager._execute_run(run_id))
    expected = [(1, lane)]
    if lane in {"general", "proxy_general"}:
        expected.append((2, "interaction" if lane == "general" else "proxy_interaction"))
    assert seen == expected
    _assert_finished(manager, run_id, "fail")


def test_direct_guarded_fallback_leaves_retry_queued_without_slot(manager):
    run_id, job_ids = _create_run(manager, ["general"])

    async def execute(_payload):
        return {"status": "fallback_interaction", "steps": []}

    manager._job_semaphore = asyncio.Semaphore(1)
    manager._post_execute = execute
    asyncio.run(manager._execute_job_guarded(run_id, job_ids[0]))
    assert manager._job_semaphore._value == 1
    assert not manager._active_jobs
    with manager.session_factory() as session:
        job = session.get(SiteJob, job_ids[0])
        assert (job.status, job.lane, job.attempt) == ("queued", "interaction", 2)


def test_waves_do_not_replay_stored_terminal_jobs(manager):
    run_id, job_ids = _create_run(manager, ["general", "interaction", "proxy_general",
                                         "proxy_interaction", "general"])
    statuses = ["success", "unknown", "waiting_human", "canceled"]
    with manager.session_factory() as session:
        for job_id, status in zip(job_ids, statuses):
            job = session.get(SiteJob, job_id)
            manager._finish_job(job, status, "preserved", {"original": True})
        session.commit()
    seen = []

    async def execute(payload):
        seen.append(payload["job_id"])
        return _success()

    manager._post_execute = execute
    asyncio.run(manager._execute_run(run_id))
    assert seen == [job_ids[-1]]
    with manager.session_factory() as session:
        for job_id, status in zip(job_ids, statuses):
            job = session.get(SiteJob, job_id)
            assert (job.status, job.message) == (status, "preserved")
            assert json.loads(job.result_json) == {"original": True}


@pytest.mark.parametrize("evidence,expected", [("paid", "waiting_human"),
                                             ("resign", "waiting_human"),
                                             ("unknown", "unknown"),
                                             ("consumption", "unknown")])
def test_wave_does_not_replay_consumed_or_unknown_outcome(manager, monkeypatch, evidence, expected):
    trigger = "assistant" if evidence == "consumption" else "manual"
    run_id, job_ids = _create_run(manager, ["general"], trigger=trigger)
    calls = []
    if evidence == "consumption":
        # Exercise the executor's plan barrier without reproducing approval setup here.
        monkeypatch.setattr("app.services.assistant_run_guard.consumption_plan",
                            lambda *_: {"review": {"dates": [], "today": "2026-01-01"}})
        monkeypatch.setattr("app.services.executor.validate_guard", lambda *_: True)
        with manager.session_factory() as session:
            session.add(WorkflowStep(owner_type="site", owner_id=session.get(SiteJob, job_ids[0]).site_id,
                                     step_key="buy", feature_type="post.buy_card", category="post",
                                     position=2))
            session.commit()

    async def execute(payload):
        calls.append(payload)
        steps = []
        if evidence in {"paid", "resign"}:
            feature, data = (("post.buy_card", {"buy_card": {"payment_attempted": True}})
                             if evidence == "paid" else
                             ("post.resign_loop", {"resign": {"submit_attempted": True}}))
            steps.append({"step_key": "consumed", "feature_type": feature,
                          "status": "success", "data": data})
        return {"status": "unknown" if evidence == "unknown" else "fallback_interaction",
                "steps": steps}

    manager._post_execute = execute
    asyncio.run(manager._execute_run(run_id))
    assert len(calls) == 1
    with manager.session_factory() as session:
        job = session.get(SiteJob, job_ids[0])
        assert (job.status, job.lane, job.attempt) == (expected, "general", 1)
    _assert_finished(manager, run_id, expected)


@pytest.mark.parametrize("cancel_before", [True, False])
def test_run_cancellation_skips_pending_jobs_and_later_waves(manager, cancel_before):
    run_id, job_ids = _create_run(manager, ["general", "general", "interaction", "proxy_general"])
    seen = []
    if cancel_before:
        with manager.session_factory() as session:
            session.get(Run, run_id).cancel_requested = True
            session.commit()

    async def execute(payload):
        seen.append(payload["job_id"])
        with manager.session_factory() as session:
            session.get(Run, run_id).cancel_requested = True
            session.commit()
        return {"status": "fallback_interaction", "steps": []}

    manager._job_semaphore = asyncio.Semaphore(1)
    manager._post_execute = execute
    asyncio.run(manager._execute_run(run_id))
    assert seen == ([] if cancel_before else [job_ids[0]])
    _assert_finished(manager, run_id, "canceled")


def test_task_cancellation_joins_wave_and_releases_slots(manager):
    run_id, job_ids = _create_run(manager, ["general", "general", "interaction"])

    async def run():
        manager._job_semaphore = asyncio.Semaphore(1)
        entered = asyncio.Event()
        stopped = []

        async def execute(payload):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(payload["job_id"])

        manager._post_execute = execute
        task = asyncio.create_task(manager._execute_run(run_id))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped == [job_ids[0]]
        assert manager._job_semaphore._value == 1
        assert not manager._active_jobs
        assert not [task for task in asyncio.all_tasks()
                    if task.get_name().startswith(f"run-{run_id}-job-")]

    asyncio.run(run())


def test_two_runs_share_global_slots_without_holding_source_wave_for_fallback(manager):
    first, first_jobs = _create_run(manager, ["general", "general"])
    second, second_jobs = _create_run(manager, ["general"])

    async def run():
        manager._run_semaphore = asyncio.Semaphore(2)
        manager._job_semaphore = asyncio.Semaphore(2)
        first_entered = asyncio.Event()
        second_entered = asyncio.Event()
        other_general_finished = asyncio.Event()
        active = 0
        maximum = 0
        seen = []

        async def execute(payload):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            seen.append((payload["job_id"], payload["lane_key"]))
            try:
                if payload["job_id"] == first_jobs[0] and payload["lane_key"] == "general":
                    first_entered.set()
                    await second_entered.wait()
                    return {"status": "fallback_interaction", "steps": []}
                if payload["job_id"] == first_jobs[1]:
                    second_entered.set()
                    await other_general_finished.wait()
                elif payload["job_id"] == second_jobs[0]:
                    other_general_finished.set()
                else:
                    assert other_general_finished.is_set()
                return _success()
            finally:
                active -= 1

        manager._post_execute = execute
        first_task = asyncio.create_task(manager._execute_run(first))
        await asyncio.wait_for(first_entered.wait(), 2)
        await asyncio.wait_for(asyncio.gather(first_task, manager._execute_run(second)), 2)
        assert maximum == 2
        assert seen.index((second_jobs[0], "general")) < seen.index((first_jobs[0], "interaction"))
        assert manager._job_semaphore._value == 2

    asyncio.run(run())
    _assert_finished(manager, first)
    _assert_finished(manager, second)


def test_two_seats_dispatch_lanes_concurrently(manager):
    run_id, _ = _create_run(manager, list(reversed(LANES)))
    simulator = manager.dispatch_simulator
    simulator.capacity = 2

    async def run():
        both = asyncio.Event()
        release = asyncio.Event()
        seen = []

        async def execute(payload):
            simulator.validate(payload)
            seen.append(payload["lane_key"])
            if len(seen) == 2:
                both.set()
            if payload["lane_key"] in LANES[:2]:
                await release.wait()
            return _success()

        manager._post_execute = execute
        task = asyncio.create_task(manager._execute_run(run_id))
        try:
            await asyncio.wait_for(both.wait(), 2)
            assert seen == list(LANES[:2])
            assert [lane for _, lane in simulator.requests[0][2]] == list(LANES)
            release.set()
            await asyncio.wait_for(task, 3)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert not simulator.tickets

    asyncio.run(run())
    _assert_finished(manager, run_id)


def test_blocked_lane_does_not_occupy_global_job_slot(manager):
    run_id, _ = _create_run(manager, ["general", "interaction"])
    simulator = manager.dispatch_simulator
    simulator.blocked.add("general")
    manager._job_semaphore = asyncio.Semaphore(1)
    seen = []

    async def execute(payload):
        simulator.validate(payload)
        seen.append(payload["lane_key"])
        simulator.blocked.clear()
        return _success()

    manager._post_execute = execute
    asyncio.run(manager._execute_run(run_id))
    assert seen == ["interaction", "general"]
    _assert_finished(manager, run_id)


def test_fallback_into_active_lane_requires_new_batch_identity(manager):
    run_id, jobs = _create_run(manager, ["general", "interaction", "interaction"])
    simulator = manager.dispatch_simulator
    simulator.capacity = 2
    seen = []

    async def run():
        interaction_entered = asyncio.Event()
        source_returned = asyncio.Event()
        caps = {}

        async def execute(payload):
            simulator.validate(payload)
            job_id, lane = payload["job_id"], payload["lane_key"]
            seen.append((job_id, lane))
            caps[job_id, lane] = payload["dispatch_reservation"]["id"]
            if lane == "general":
                await interaction_entered.wait()
                source_returned.set()
                return {"status": "fallback_interaction", "steps": []}
            if job_id in jobs[1:]:
                interaction_entered.set()
                await source_returned.wait()
                await asyncio.sleep(0.03)
            else:
                old = caps[jobs[1], "interaction"]
                assert old != caps[job_id, lane]
                assert any(ticket == old for _, ticket in simulator.released)
            return _success()

        manager._post_execute = execute
        await asyncio.wait_for(manager._execute_run(run_id), 3)
        assert caps[jobs[1], "interaction"] == caps[jobs[2], "interaction"]

    asyncio.run(run())
    assert seen.count((jobs[0], "interaction")) == 1
    assert len(seen) == 4
    _assert_finished(manager, run_id)


@pytest.mark.parametrize("waiting_global", [False, True])
def test_renews_same_ticket_while_running_or_waiting_global(manager, waiting_global):
    run_id, _ = _create_run(manager, ["general"])
    simulator = manager.dispatch_simulator

    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()
        if waiting_global:
            manager._job_semaphore = asyncio.Semaphore(0)

        async def execute(payload):
            simulator.validate(payload)
            entered.set()
            await release.wait()
            return _success()

        manager._post_execute = execute
        task = asyncio.create_task(manager._execute_run(run_id))
        try:
            if not waiting_global:
                await asyncio.wait_for(entered.wait(), 2)
            await asyncio.sleep(1.15)
            assert len(simulator.requests) >= 2
            assert simulator.requests[0] == simulator.requests[-1]
            if waiting_global:
                assert not entered.is_set()
                manager._job_semaphore.release()
            release.set()
            await asyncio.wait_for(task, 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    _assert_finished(manager, run_id)


@pytest.mark.parametrize("change", ["generation", "downgrade"])
def test_lost_reservation_never_dispatches_semaphore_waiters_or_restarts(manager, monkeypatch, change):
    run_id, jobs = _create_run(manager, ["general", "general", "interaction"])
    simulator = manager.dispatch_simulator
    manager._job_semaphore = asyncio.Semaphore(1)
    monkeypatch.setattr("app.services.browser_dispatch.POLL_SECONDS", 0.01)
    seen = []

    async def execute(payload):
        seen.append(payload["job_id"])
        if change == "generation":
            simulator.generation = str(uuid4())
        else:
            simulator.tickets[next(iter(simulator.tickets))]["state"] = "pending"
            simulator.blocked.add("general")
        await asyncio.sleep(0.08)
        return {"status": "restart", "steps": []}

    manager._post_execute = execute
    asyncio.run(manager._execute_run(run_id))
    assert seen == [jobs[0]]
    with manager.session_factory() as session:
        message = session.get(SiteJob, jobs[1]).message
        assert message == (DISPATCH_GENERATION_ERROR if change == "generation" else DISPATCH_ERROR)
    assert not simulator.tickets
    _assert_finished(manager, run_id, "fail")


def test_failed_ticket_only_fails_queued_jobs_for_its_lane(manager):
    run_id, jobs = _create_run(manager, ["general", "interaction"])
    simulator = manager.dispatch_simulator
    simulator.failed.add("general")
    seen = []

    async def execute(payload):
        seen.append(payload["job_id"])
        return _success()

    manager._post_execute = execute
    asyncio.run(manager._execute_run(run_id))
    assert seen == [jobs[1]]
    with manager.session_factory() as session:
        assert session.get(SiteJob, jobs[0]).message == DISPATCH_ERROR
        assert session.get(SiteJob, jobs[1]).status == "success"
    assert len({ticket for _, _, request in simulator.requests for ticket, lane in request
                if lane == "general"}) == 1


@pytest.mark.parametrize("database_flag", [False, True])
def test_cancel_pending_only_run_closes_owner(manager, database_flag):
    run_id, _ = _create_run(manager, ["general"])
    simulator = manager.dispatch_simulator
    simulator.blocked.update(LANES)

    async def run():
        task = asyncio.create_task(manager._execute_run(run_id))
        try:
            while not simulator.requests:
                await asyncio.sleep(0.01)
            if database_flag:
                with manager.session_factory() as session:
                    session.get(Run, run_id).cancel_requested = True
                    session.commit()
            else:
                await manager.cancel(run_id)
            await asyncio.wait_for(task, 1.5)
            assert not simulator.tickets and simulator.closed
            assert not manager._run_cancel_events
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    _assert_finished(manager, run_id, "canceled")


def test_canceled_unknown_put_cleans_up_known_owner(manager, monkeypatch):
    run_id, _ = _create_run(manager, ["general"])
    simulator = manager.dispatch_simulator
    original = simulator.renew

    async def run():
        grant_unknown = asyncio.Event()

        async def renew(owner, run_id, tickets):
            await original(owner, run_id, tickets)
            grant_unknown.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(simulator, "renew", renew)
        task = asyncio.create_task(manager._execute_run(run_id))
        await asyncio.wait_for(grant_unknown.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not simulator.tickets and simulator.closed

    asyncio.run(run())


@pytest.mark.parametrize("resistant_join", [False, True])
def test_repeated_task_cancellation_preserves_teardown_and_owner_close(
    manager, monkeypatch, resistant_join,
):
    run_id, jobs = _create_run(manager, ["general", "interaction"])
    simulator = manager.dispatch_simulator
    monkeypatch.setattr("app.services.executor.DISPATCH_JOIN_TIMEOUT_SECONDS", 0.1)

    async def run():
        entered = asyncio.Event()
        joining = asyncio.Event()
        finish_join = asyncio.Event()
        owner_closed = asyncio.Event()
        calls = []
        cancellations = []
        original_close = simulator.close
        renewer = None

        async def execute(payload):
            simulator.validate(payload)
            calls.append(payload)
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellations.append(payload["job_id"])
                joining.set()
                try:
                    await finish_join.wait()
                except asyncio.CancelledError:
                    cancellations.append(payload["job_id"])
                    raise
                raise

        async def close(owner):
            assert renewer.done()
            assert all(task.done() for task in helpers)
            assert {row["lane"]: row["state"] for row in simulator.tickets.values()} == {
                "general": "granted", "interaction": "pending",
            }
            assert not simulator.released
            if resistant_join:
                assert not job_task.done()
                assert any(task.get_name().endswith("-dispatch-join")
                           for task in manager._dispatch_cleanup_tasks)
            else:
                assert job_task.done()
            await original_close(owner)
            owner_closed.set()

        monkeypatch.setattr(simulator, "close", close)
        manager._post_execute = execute
        task = asyncio.create_task(manager._execute_run(run_id))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            renewer = next(task for task in asyncio.all_tasks()
                           if task.get_name() == f"run-{run_id}-dispatch")
            helpers = [task for task in asyncio.all_tasks()
                       if getattr(task.get_coro(), "__qualname__", "") == "Event.wait"]
            job_task = next(task for task in asyncio.all_tasks()
                            if task.get_name() == f"run-{run_id}-job-{jobs[0]}")
            task.cancel()
            await asyncio.wait_for(joining.wait(), 2)
            assert manager._dispatch_cleanup_tasks
            task.cancel()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not job_task.done()
            assert manager._dispatch_cleanup_tasks
            assert simulator.tickets and not simulator.closed
            if not resistant_join:
                finish_join.set()
            await asyncio.wait_for(owner_closed.wait(), 2)
            assert not simulator.tickets
            assert not manager._run_cancel_events
            finish_join.set()
            await asyncio.wait_for(
                asyncio.gather(*manager._dispatch_cleanup_tasks), 2,
            )
            await asyncio.sleep(0)
            assert not manager._dispatch_cleanup_tasks
            assert not manager._active_jobs
            assert cancellations == [jobs[0]]
            assert len(calls) == 1
            assert calls[0]["job_id"] == jobs[0]
            assert job_task.done() and renewer.done()
        finally:
            finish_join.set()
            task.cancel()
            await asyncio.gather(task, *manager._dispatch_cleanup_tasks, return_exceptions=True)

    asyncio.run(run())


def test_unknown_transport_put_retries_only_same_identity(manager, monkeypatch):
    run_id, _ = _create_run(manager, ["general"])
    simulator = manager.dispatch_simulator
    original = simulator.renew
    monkeypatch.setattr("app.services.browser_dispatch.POLL_SECONDS", 0.01)
    seen = []

    async def renew(owner, run_id, tickets):
        result = await original(owner, run_id, tickets)
        if len(simulator.requests) == 1:
            raise DispatchTransportError()
        return result

    async def execute(payload):
        simulator.validate(payload)
        seen.append(payload)
        return _success()

    monkeypatch.setattr(simulator, "renew", renew)
    manager._post_execute = execute
    asyncio.run(manager._execute_run(run_id))
    assert len(seen) == 1
    assert simulator.requests[0] == simulator.requests[1]
    _assert_finished(manager, run_id)


@pytest.mark.parametrize("kind", ["unsupported", "transport"])
def test_unavailable_dispatch_fails_closed_without_business_retries(manager, monkeypatch, kind):
    run_id, _ = _create_run(manager, ["general", "interaction"])
    simulator = manager.dispatch_simulator
    original = simulator.renew
    monkeypatch.setattr("app.services.browser_dispatch.POLL_SECONDS", 0.01)
    monkeypatch.setattr("app.services.browser_dispatch.TRANSPORT_GRACE_SECONDS", 0.02)

    async def renew(owner, run_id, tickets):
        await original(owner, run_id, tickets)
        raise DispatchTransportError() if kind == "transport" else DispatchError()

    async def execute(_payload):
        pytest.fail("dispatch failure must not execute a job")

    monkeypatch.setattr(simulator, "renew", renew)
    manager._post_execute = execute
    asyncio.run(manager._execute_run(run_id))
    assert not simulator.tickets
    assert all(request == simulator.requests[0] for request in simulator.requests)
    _assert_finished(manager, run_id, "fail")


def test_unknown_release_stops_new_batches_and_owner_cleanup_releases_pin(manager, monkeypatch):
    run_id, jobs = _create_run(manager, ["general"])
    simulator = manager.dispatch_simulator
    calls = []

    async def release(owner, ticket):
        raise DispatchTransportError()

    async def execute(payload):
        calls.append(payload)
        return {"status": "fallback_interaction", "steps": []}

    monkeypatch.setattr(simulator, "release", release)
    manager._post_execute = execute
    asyncio.run(manager._execute_run(run_id))
    assert len(calls) == 1
    assert not simulator.tickets and simulator.closed
    with manager.session_factory() as session:
        assert session.get(SiteJob, jobs[0]).message == DISPATCH_ERROR
    _assert_finished(manager, run_id, "fail")


def test_cancel_active_batch_renews_pin_until_job_joins(manager, monkeypatch):
    run_id, _ = _create_run(manager, ["general", "interaction"])
    simulator = manager.dispatch_simulator
    monkeypatch.setattr("app.services.browser_dispatch.POLL_SECONDS", 0.01)

    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def execute(payload):
            entered.set()
            await release.wait()
            simulator.validate(payload)
            return _success()

        manager._post_execute = execute
        task = asyncio.create_task(manager._execute_run(run_id))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            with manager.session_factory() as session:
                session.get(Run, run_id).cancel_requested = True
                session.commit()
            manager._run_cancel_events[run_id].set()
            count = len(simulator.requests)
            await asyncio.sleep(0.08)
            assert len(simulator.requests) > count
            assert not task.done()
            assert {row["lane"] for row in simulator.tickets.values()} == {"general"}
            release.set()
            await asyncio.wait_for(task, 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert not simulator.tickets
    _assert_finished(manager, run_id, "canceled")


def test_real_execution_without_reservation_is_rejected(manager):
    async def run():
        with pytest.raises(DispatchError):
            await manager._post_execute({"job_id": 1})

    asyncio.run(run())


def test_same_lane_restart_keeps_pin_and_identity(manager):
    run_id, _ = _create_run(manager, ["general"])
    simulator = manager.dispatch_simulator
    capabilities = []

    async def execute(payload):
        simulator.validate(payload)
        capabilities.append(payload["dispatch_reservation"])
        assert not simulator.released
        return {"status": "restart", "steps": []} if len(capabilities) < 3 else _success()

    manager._post_execute = execute
    asyncio.run(manager._execute_run(run_id))
    assert len(capabilities) == 3
    assert capabilities[0] == capabilities[1] == capabilities[2]
    assert not simulator.tickets
    _assert_finished(manager, run_id)


@pytest.mark.parametrize("bad", [None, "generation", "duplicate", "lane", "state", "compressed", "large", "redirect"])
def test_dispatch_rpc_security_and_response_validation(settings, monkeypatch, bad):
    settings.browser_token = SecretStr("private-browser-token")
    settings.worker_url = "http://fixed-worker:9000"
    ticket = DispatchTicket("general")
    owner, generation = str(uuid4()), str(uuid4())
    real_client = httpx.AsyncClient
    requests = []

    async def handler(request):
        requests.append(request)
        assert str(request.url).startswith(f"http://fixed-worker:9000/browser/dispatch/{owner}")
        assert request.headers["authorization"] == "Bearer private-browser-token"
        assert request.headers["accept-encoding"] == "identity"
        assert "origin" not in request.headers
        if request.method == "DELETE":
            return httpx.Response(204, stream=httpx.ByteStream(b""))
        assert json.loads(request.content) == {"run_id": 42, "tickets": [{"id": ticket.id, "lane": "general"}]}
        data = {"generation": generation, "tickets": [{"id": ticket.id, "lane": "general", "state": "granted"}]}
        if bad == "generation":
            data["generation"] = "untrusted-secret"
        elif bad == "duplicate":
            data["tickets"] *= 2
        elif bad in {"lane", "state"}:
            data["tickets"][0][bad] = "untrusted-secret"
        if bad == "compressed":
            return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(b"secret"))
        if bad == "large":
            return httpx.Response(200, stream=httpx.ByteStream(b" " * 65537))
        if bad == "redirect":
            return httpx.Response(307, headers={"location": "http://attacker.invalid"})
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(data).encode()))

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        assert kwargs["timeout"] == 10
        return real_client(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr("app.services.browser_dispatch.httpx.AsyncClient", client)

    async def run():
        rpc = DispatchClient(settings)
        if bad:
            with pytest.raises(DispatchError, match=DISPATCH_ERROR):
                await rpc.renew(owner, 42, [ticket])
        else:
            assert await rpc.renew(owner, 42, [ticket]) == (generation, {ticket.id: "granted"})
        await rpc.release(owner, ticket.id)
        await rpc.close(owner)
        assert len(requests) == 3
        assert requests[1].url.path.endswith(ticket.id)
        assert requests[2].url.path.endswith(owner)

    asyncio.run(run())
