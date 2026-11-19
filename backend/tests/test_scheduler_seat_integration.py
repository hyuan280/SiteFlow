from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr

from app.models.execution import JobStep, Run, SiteJob
from app.services.executor import ExecutionManager
from app.services.lanes import LANE_LABELS
from app.services.scheduler import Scheduler
from app.services.scheduler_settings import SchedulerSettingsService

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker import app as worker_app  # noqa: E402
from worker.app import LaneManager  # noqa: E402
from worker.browser_backend import ManagerBackend  # noqa: E402


LANES = list(LANE_LABELS)
WORKER_URL = "http://scheduler-worker:6300"
MANAGER_URL = "http://scheduler-manager:8080"
TOKEN = "scheduler-seat-private-token"
PROXY_URL = "http://scheduler-proxy:3128"
PROVIDER_SECRET = "private-provider-license-and-proxy-details"


class RawBody(httpx.AsyncByteStream):
    def __init__(self, payload):
        self.body = json.dumps(payload).encode()

    async def __aiter__(self):
        yield self.body[:5]
        yield self.body[5:]


class RecordedStream(httpx.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream
        self.body = bytearray()

    async def __aiter__(self):
        async for chunk in self.stream:
            self.body.extend(chunk)
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


@pytest_asyncio.fixture
async def chain(client, tmp_path, monkeypatch, request):
    tier, provider_capacity, configured_capacity = getattr(request, "param", ("free", 1, 4))
    settings = client.app.state.settings
    settings.worker_url = WORKER_URL
    settings.browser_token = SecretStr(TOKEN)
    settings.proxy_url = PROXY_URL
    monkeypatch.setenv("SITEFLOW_BROWSER_URL", MANAGER_URL)
    monkeypatch.setenv("SITEFLOW_BROWSER_TOKEN", TOKEN)
    monkeypatch.setenv("SITEFLOW_BROWSER_BACKEND", "cloak-manager")
    monkeypatch.setenv("SITEFLOW_BROWSER_MAX_ACTIVE_PROFILES", str(configured_capacity))
    monkeypatch.setattr(worker_app, "DATA_DIR", tmp_path / "worker")
    monkeypatch.setattr(worker_app, "PROXY_URL", PROXY_URL)

    for key in reversed(LANES):
        response = client.post("/api/v1/sites", json={
            "name": f"scheduled-{key}", "base_url": "https://seat.example.test",
            "lane_policy": key, "checkin_after": "00:00", "timezone": "UTC",
            "support_resign": False,
            "steps": [{"step_key": "open", "feature_type": "flow.open"}],
        })
        assert response.status_code == 201, response.text
    response = client.put("/api/v1/scheduler-settings", json={
        "enabled": True, "mode": "interval", "interval_seconds": 600,
        "cron": "", "ranges": [],
    })
    assert response.status_code == 200, response.text
    target = datetime.fromisoformat(response.json()["next_run_at"])
    executor = ExecutionManager(
        client.app.state.session_factory, settings, client.app.state.crypto,
    )
    state = SimpleNamespace(
        client=client, target=target, executor=executor, submitted=[], execute=False,
        denied=False, profiles={}, active=set(), launches=[], stops=[], maximum=0,
        manager_requests=[], worker_requests=[], worker_streams=[], unexpected=[],
        browsers=[], before_launch=None,
        tier=tier, provider_capacity=provider_capacity,
    )
    submit = executor.submit

    def record_submit(run_id):
        state.submitted.append(run_id)
        if state.execute:
            submit(run_id)

    monkeypatch.setattr(executor, "submit", record_submit)
    monkeypatch.setattr(executor, "schedule_run_notification", lambda _run_id: None)
    state.scheduler = Scheduler(
        client.app.state.session_factory, settings, client.app.state.crypto, executor,
    )

    def provider(request):
        state.manager_requests.append(request)
        method, path = request.method, request.url.path
        code = 200
        if (method, path) == ("GET", "/api/status"):
            payload = {"license_tier": state.tier, "max_profiles": state.provider_capacity,
                       "active_profiles": len(state.active)}
        elif (method, path) == ("POST", "/api/profiles"):
            options = json.loads(request.content)
            key = options["name"].removeprefix("SiteFlow ")
            assert key in LANES and key not in state.profiles.values()
            assert options["proxy"] == (PROXY_URL if key.startswith("proxy_") else None)
            profile_id = f"owned-{key}"
            state.profiles[profile_id] = key
            payload = {"id": profile_id}
        else:
            parts = path.strip("/").split("/")
            assert parts[:2] == ["api", "profiles"] and len(parts) in (3, 4)
            profile_id = parts[2]
            assert profile_id in state.profiles
            key = state.profiles[profile_id]
            action = parts[3] if len(parts) == 4 else None
            if (method, action) == ("GET", "status"):
                payload = {"status": "running" if profile_id in state.active else "stopped"}
            elif (method, action) == ("PUT", None):
                payload = {}
            elif (method, action) == ("POST", "launch"):
                if state.before_launch is not None:
                    state.before_launch()
                state.launches.append(key)
                if state.denied or len(state.active) >= state.provider_capacity:
                    code, payload = 402, {"detail": PROVIDER_SECRET}
                else:
                    state.active.add(profile_id)
                    state.maximum = max(state.maximum, len(state.active))
                    payload = {}
            elif (method, action) == ("POST", "stop"):
                state.stops.append(key)
                state.active.discard(profile_id)
                payload = {}
            else:
                raise AssertionError(f"Unexpected Manager operation: {method} {path}")
        return httpx.Response(code, stream=RawBody(payload))

    app = worker_app.create_app()
    worker_transport = httpx.ASGITransport(app=app)
    manager_transport = httpx.MockTransport(provider)

    class SelectiveTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            preflight = str(request.url) == WORKER_URL + "/browser/preflight"
            dispatch = str(request.url).startswith(WORKER_URL + "/browser/dispatch/")
            if preflight or dispatch:
                assert request.headers["Authorization"] == f"Bearer {TOKEN}"
                assert not request.url.query
                if preflight:
                    assert request.method == "POST"
                else:
                    parts = request.url.path.split("/")[3:]
                    assert len(parts) in (1, 2)
                    assert all(str(UUID(part)) == part for part in parts)
                    assert request.method in ({"PUT", "DELETE"} if len(parts) == 1 else {"DELETE"})
                state.worker_requests.append(request)
                response = await worker_transport.handle_async_request(request)
                assert not response.is_stream_consumed
                response.stream = RecordedStream(response.stream)
                state.worker_streams.append(response.stream)
                return response
            if str(request.url).startswith(MANAGER_URL + "/api/"):
                response = await manager_transport.handle_async_request(request)
                assert not response.is_stream_consumed
                return response
            state.unexpected.append((request.method, str(request.url)))
            raise AssertionError("Unexpected outbound request")

    # Both HTTP hops share httpx; retain the real constructor to avoid recursion.
    real_client = httpx.AsyncClient

    def async_client(*args, **kwargs):
        kwargs.setdefault("transport", SelectiveTransport())
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", async_client)

    async def connect(url, **kwargs):
        profile_id = url.removeprefix(MANAGER_URL + "/api/profiles/").removesuffix("/cdp")
        assert profile_id in state.active
        assert kwargs["headers"] == {"Authorization": f"Bearer {TOKEN}"}
        page = SimpleNamespace(
            goto=AsyncMock(), evaluate=AsyncMock(return_value=1), wait_for_timeout=AsyncMock(),
        )
        context = SimpleNamespace(pages=[page], new_page=AsyncMock())
        browser = SimpleNamespace(contexts=[context], connected=True, handlers={})
        browser.is_connected = lambda: browser.connected
        browser.on = lambda event, callback: browser.handlers.__setitem__(event, callback)

        async def close():
            browser.connected = False
            browser.handlers["disconnected"](browser)

        browser.close = AsyncMock(side_effect=close)
        state.browsers.append(browser)
        return browser

    fake_playwright = SimpleNamespace(
        chromium=SimpleNamespace(connect_over_cdp=AsyncMock(side_effect=connect)), stop=AsyncMock(),
    )
    import playwright.async_api

    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: SimpleNamespace(
        start=AsyncMock(return_value=fake_playwright),
    ))
    # Input humanization requires native Playwright objects, unlike lane startup.
    monkeypatch.setattr("worker.browser_input.humanize_actions", lambda _context: None)
    state.manager = LaneManager()
    assert isinstance(state.manager.backend, ManagerBackend)
    app.state.manager = state.manager
    state.before_launch = lambda: assert_no_admission(state)
    try:
        yield state
    finally:
        await executor.shutdown()
        await state.manager.stop()
        assert state.unexpected == []
        assert not state.active
        assert not state.manager.backend.uncertain_profiles
        assert not state.manager.backend._stop_tasks
        assert not state.manager._seat_waiters
        assert not state.manager.dispatch.cleanup_tasks
        assert all(owner.closed for owner in state.manager.dispatch.owners.values())
        assert all(ticket.state == "released" and ticket.pin is None
                   and (ticket.task is None or ticket.task.done())
                   for ticket in state.manager.dispatch.tickets.values())
        for lane in state.manager.lanes.values():
            assert lane.session is None and not lane.leases and not lane.task_pages
        for request in state.worker_requests + state.manager_requests:
            assert request.headers["Authorization"] == f"Bearer {TOKEN}"
            assert not {"origin", "cookie", "x-csrf-token"}.intersection(request.headers)
        assert all(request.headers["Accept-Encoding"] == "identity"
                   for request in state.worker_requests)


def assert_no_admission(chain):
    with chain.client.app.state.session_factory() as session:
        for model in (Run, SiteJob, JobStep):
            assert session.query(model).count() == 0
        service = SchedulerSettingsService(session, chain.client.app.state.settings)
        assert service.view().next_run_at == chain.target
    assert chain.submitted == []


def assert_admitted(chain, ids):
    assert len(ids) == 1 and chain.submitted == ids
    preflights = [(request, stream) for request, stream in
                  zip(chain.worker_requests, chain.worker_streams)
                  if request.url.path == "/browser/preflight"]
    request, stream = preflights[-1]
    assert json.loads(request.content) == {"lanes": LANES}
    payload = json.loads(stream.body)
    assert payload["ready"] is True and payload["first_lane"] == "general"
    assert payload["lanes"] == [{"key": key, "ready": True} for key in LANES]
    with chain.client.app.state.session_factory() as session:
        run = session.query(Run).one()
        assert (run.id, run.trigger, run.mode) == (ids[0], "schedule", "scheduled")
        assert [job.lane for job in session.query(SiteJob).order_by(SiteJob.id)] == LANES
        service = SchedulerSettingsService(session, chain.client.app.state.settings)
        assert service.view().next_run_at > chain.target


@pytest.mark.asyncio
async def test_free_one_seat_admits_four_lanes_after_only_first_launch(chain):
    ids = await chain.scheduler.scan_once_async(chain.target)
    assert_admitted(chain, ids)
    assert chain.launches == ["general"]
    assert chain.stops == []
    assert chain.active == {"owned-general"}
    assert chain.manager.backend.profiles == {"general": "owned-general"}
    lane = chain.manager.get("general")
    assert lane.status == "running" and not lane.leases
    lane.page.goto.assert_awaited_once_with(
        "about:blank", wait_until="domcontentloaded", timeout=10000,
    )
    assert lane.page.evaluate.await_count == 2
    assert "document.write(html)" in lane.page.evaluate.await_args_list[0].args[0]
    assert lane.page.evaluate.await_args_list[1].args == ("() => 1",)
    assert all(chain.manager.get(key).session is None for key in LANES[1:])
    assert await chain.scheduler.scan_once_async(chain.target) == []
    assert len(chain.worker_requests) == 1 and chain.submitted == ids


@pytest.mark.asyncio
async def test_first_launch_http_402_cleans_up_defers_and_recovers_next_scan(chain, caplog):
    chain.denied = True
    assert await chain.scheduler.scan_once_async(chain.target) == []
    assert_no_admission(chain)
    assert chain.launches == ["general"] and chain.stops == ["general"]
    assert not chain.active and not chain.browsers
    assert chain.manager.backend.playwright is None
    assert chain.manager.backend.profiles == {"general": "owned-general"}
    assert not chain.manager.backend.uncertain_profiles
    assert not chain.manager.backend._stop_tasks and not chain.manager._seat_waiters
    lane = chain.manager.get("general")
    assert lane.status == "error" and lane.error == "Browser profile capacity unavailable"
    assert lane.session is None and lane.context is None and not lane.leases
    assert not lane.lock.locked() and not chain.manager._admission_lock.locked()
    payload = json.loads(chain.worker_streams[0].body)
    assert payload["ready"] is False and payload["first_lane"] is None
    assert payload["reason"] == "browser_capacity_unavailable"
    assert all(item["ready"] is False for item in payload["lanes"])
    assert [(r.method, r.url.path) for r in chain.manager_requests[-3:]] == [
        ("POST", "/api/profiles/owned-general/launch"),
        ("POST", "/api/profiles/owned-general/stop"),
        ("GET", "/api/profiles/owned-general/status"),
    ]
    assert PROVIDER_SECRET not in caplog.text
    assert PROVIDER_SECRET.encode() not in chain.worker_streams[0].body

    chain.denied = False
    ids = await chain.scheduler.scan_once_async(chain.target)
    assert_admitted(chain, ids)
    assert chain.launches == ["general", "general"]
    assert sum(r.method == "POST" and r.url.path == "/api/profiles"
               for r in chain.manager_requests) == 1
    assert await chain.scheduler.scan_once_async(chain.target) == []
    assert len(chain.worker_requests) == 2 and chain.submitted == ids


@pytest.mark.asyncio
@pytest.mark.parametrize("chain,capacity", [(("free", 1, 4), 1), (("pro", 4, 2), 2)],
                         indirect=["chain"], ids=["free-one-seat", "pro-configured-two-seats"])
async def test_scheduled_executor_leases_and_reclaims_manager_seats(chain, monkeypatch, capacity):
    seen = []
    active = set()
    maximum = 0
    first_batch = asyncio.Event()
    release = asyncio.Event()

    async def execute(payload):
        nonlocal maximum
        key = payload["lane_key"]
        reservation = payload["dispatch_reservation"]
        ticket = chain.manager.dispatch.tickets[reservation["id"]]
        lane = chain.manager.get(key)
        assert ticket.state == "granted" and lane.leases == {ticket.pin}
        lease_id, _ = await chain.manager.dispatch.lease_job(
            key, payload["run_id"], reservation,
            job_id=payload["job_id"], attempt=payload["attempt"],
        )
        try:
            assert f"owned-{key}" in chain.active
            assert lease_id != ticket.pin and lane.leases == {ticket.pin, lease_id}
            assert payload["run_id"] == chain.submitted[0]
            active.add(key)
            maximum = max(maximum, len(active))
            assert len(active) <= capacity and len(chain.active) <= capacity
            seen.append(key)
            if key in LANES[:capacity]:
                if len(active) == capacity:
                    first_batch.set()
                await release.wait()
            await asyncio.sleep(0)
            return {"status": "success", "message": "completed", "steps": [{
                "step_key": "open", "feature_type": "flow.open", "status": "success",
            }]}
        finally:
            active.discard(key)
            await chain.manager.release_lane(key, lease_id)
            # Job cleanup must leave the batch's dispatch pin in place.
            assert ticket.state == "granted" and lane.leases == {ticket.pin}

    monkeypatch.setattr(chain.executor, "_post_execute", execute)
    chain.execute = True
    ids = await chain.scheduler.scan_once_async(chain.target)
    assert_admitted(chain, ids)
    assert chain.launches == ["general"]
    chain.before_launch = None
    await asyncio.wait_for(first_batch.wait(), timeout=5)
    assert seen == LANES[:capacity] and active == set(LANES[:capacity])
    assert chain.launches == LANES[:capacity]
    assert chain.active == {f"owned-{key}" for key in LANES[:capacity]}
    assert [ticket.lane for ticket in chain.manager.dispatch.tickets.values()
            if ticket.state == "pending"] == LANES[capacity:]
    release.set()
    await asyncio.wait_for(asyncio.gather(*chain.executor._tasks.values()), timeout=5)
    assert seen == LANES and chain.launches == LANES
    assert maximum == capacity and chain.maximum == capacity and not active
    assert len(chain.stops) == len(LANES) - capacity
    assert len(chain.active) == capacity
    if capacity == 1:
        assert chain.stops == LANES[:-1]
        assert chain.active == {"owned-proxy_interaction"}
    assert not chain.executor._active_jobs
    assert all(not lane.leases for lane in chain.manager.lanes.values())
    assert all(owner.closed for owner in chain.manager.dispatch.owners.values())
    assert all(ticket.state == "released" and ticket.pin is None
               for ticket in chain.manager.dispatch.tickets.values())
    assert not chain.manager._seat_waiters and not chain.manager.dispatch.cleanup_tasks
    with chain.client.app.state.session_factory() as session:
        run = session.query(Run).one()
        assert run.status == "success" and run.finished_at is not None
        jobs = session.query(SiteJob).order_by(SiteJob.id).all()
        assert len(jobs) == 4
        assert all(job.status == "success" and job.finished_at is not None for job in jobs)
        assert session.query(JobStep).count() == 4
    assert await chain.scheduler.scan_once_async(chain.target) == []
    assert sum(request.url.path == "/browser/preflight"
               for request in chain.worker_requests) == 1
    puts = [request for request in chain.worker_requests if request.method == "PUT"]
    assert [row["lane"] for row in json.loads(puts[0].content)["tickets"]] == LANES
    assert json.loads(puts[0].content)["run_id"] == ids[0]
    owner_path = puts[0].url.path
    assert all(request.url.path == owner_path for request in puts)
    deletes = [request.url.path for request in chain.worker_requests if request.method == "DELETE"]
    assert set(deletes) == {owner_path, *(f"{owner_path}/{ticket.id}"
                                        for ticket in chain.manager.dispatch.tickets.values())}
    assert chain.submitted == ids


@pytest.mark.asyncio
@pytest.mark.parametrize("chain", [("pro", 4, 2)], indirect=True)
async def test_fallback_after_target_batch_finished_gets_fresh_dispatch_pin(chain, monkeypatch):
    seen = []
    reservations = {}

    async def execute(payload):
        key, job_id = payload["lane_key"], payload["job_id"]
        reservation = payload["dispatch_reservation"]
        ticket = chain.manager.dispatch.tickets[reservation["id"]]
        lane = chain.manager.get(key)
        assert lane.leases == {ticket.pin}
        lease_id, _ = await chain.manager.dispatch.lease_job(
            key, payload["run_id"], reservation,
            job_id=payload["job_id"], attempt=payload["attempt"],
        )
        seen.append((job_id, key, payload["attempt"]))
        reservations[job_id, key] = reservation
        try:
            assert lane.leases == {ticket.pin, lease_id} and lease_id != ticket.pin
            assert len(chain.active) <= 2
            if key == "general":
                while True:
                    targets = [item for item in chain.manager.dispatch.tickets.values()
                               if item.lane == "interaction"]
                    if targets and targets[0].state == "released" and targets[0].pin is None:
                        break
                    await asyncio.sleep(0.01)
                with chain.client.app.state.session_factory() as session:
                    target_job = session.query(SiteJob).filter_by(lane="interaction").one()
                    assert target_job.status == "success" and target_job.finished_at is not None
                return {"status": "fallback_interaction", "steps": []}
            if key == "interaction" and payload["attempt"] == 2:
                old = next(cap for (other_id, other_key), cap in reservations.items()
                           if other_key == key and other_id != job_id)
                assert reservation["owner"] == old["owner"]
                assert reservation["id"] != old["id"]
                old_ticket = chain.manager.dispatch.tickets[old["id"]]
                assert old_ticket.state == "released" and old_ticket.pin is None
            return {"status": "success", "message": "completed", "steps": [{
                "step_key": "open", "feature_type": "flow.open", "status": "success",
            }]}
        finally:
            await chain.manager.release_lane(key, lease_id)
            assert ticket.state == "granted" and lane.leases == {ticket.pin}

    monkeypatch.setattr(chain.executor, "_post_execute", execute)
    chain.execute = True
    ids = await chain.scheduler.scan_once_async(chain.target)
    assert_admitted(chain, ids)
    chain.before_launch = None
    await asyncio.wait_for(asyncio.gather(*chain.executor._tasks.values()), timeout=8)
    with chain.client.app.state.session_factory() as session:
        run = session.query(Run).one()
        assert run.status == "success" and run.finished_at is not None
        jobs = session.query(SiteJob).order_by(SiteJob.id).all()
        assert [job.lane for job in jobs] == ["interaction", *LANES[1:]]
        assert [job.attempt for job in jobs] == [2, 1, 1, 1]
        assert all(job.status == "success" and job.finished_at is not None for job in jobs)
        assert session.query(JobStep).count() == 4
        assert seen.count((jobs[0].id, "general", 1)) == 1
        assert seen.count((jobs[0].id, "interaction", 2)) == 1
        assert seen.count((jobs[1].id, "interaction", 1)) == 1
    assert len(seen) == 5 and chain.maximum == 2
    assert not chain.executor._active_jobs and not chain.manager._seat_waiters
    assert all(not lane.leases for lane in chain.manager.lanes.values())
    assert all(owner.closed for owner in chain.manager.dispatch.owners.values())
    assert all(ticket.state == "released" and ticket.pin is None
               for ticket in chain.manager.dispatch.tickets.values())
    assert not chain.manager.dispatch.cleanup_tasks
    assert await chain.scheduler.scan_once_async(chain.target) == []
    assert sum(request.url.path == "/browser/preflight"
               for request in chain.worker_requests) == 1
    assert chain.submitted == ids


@pytest.mark.asyncio
async def test_duplicate_executor_payload_cannot_replay_job_attempt(chain, monkeypatch):
    executed = []

    async def deliver(payload):
        key = payload["lane_key"]
        ticket = chain.manager.dispatch.tickets[payload["dispatch_reservation"]["id"]]
        lease_id, _ = await chain.manager.dispatch.lease_job(
            key, payload["run_id"], payload["dispatch_reservation"],
            job_id=payload["job_id"], attempt=payload["attempt"],
        )
        try:
            assert lease_id != ticket.pin
            assert chain.manager.get(key).leases == {ticket.pin, lease_id}
            executed.append((payload["run_id"], payload["job_id"], payload["attempt"]))
            # Keep the winning job lease alive while the competing delivery claims it.
            await asyncio.sleep(0)
            return {"status": "success", "message": "completed", "steps": [{
                "step_key": "open", "feature_type": "flow.open", "status": "success",
            }]}
        finally:
            await chain.manager.release_lane(key, lease_id)

    async def execute(payload):
        ticket = chain.manager.dispatch.tickets[payload["dispatch_reservation"]["id"]]
        results = await asyncio.gather(deliver(payload), deliver(payload), return_exceptions=True)
        failures = [result for result in results if isinstance(result, Exception)]
        successes = [result for result in results if isinstance(result, dict)]
        assert len(failures) == len(successes) == 1
        assert isinstance(failures[0], RuntimeError)
        assert str(failures[0]) == "Invalid browser dispatch reservation"
        assert ticket.state == "granted" and ticket.pin is not None
        assert chain.manager.get(payload["lane_key"]).leases == {ticket.pin}
        # Completion must not make the same attempt claimable again, even on a live ticket.
        with pytest.raises(RuntimeError, match="^Invalid browser dispatch reservation$"):
            await deliver(payload)
        assert chain.manager.get(payload["lane_key"]).leases == {ticket.pin}
        return successes[0]

    monkeypatch.setattr(chain.executor, "_post_execute", execute)
    chain.execute = True
    ids = await chain.scheduler.scan_once_async(chain.target)
    assert_admitted(chain, ids)
    chain.before_launch = None
    await asyncio.wait_for(asyncio.gather(*chain.executor._tasks.values()), timeout=8)
    with chain.client.app.state.session_factory() as session:
        run = session.query(Run).one()
        assert run.status == "success" and run.finished_at is not None
        jobs = session.query(SiteJob).order_by(SiteJob.id).all()
        assert executed == [(ids[0], job.id, 1) for job in jobs]
        assert len(jobs) == 4 and [job.lane for job in jobs] == LANES
        assert all(job.status == "success" and job.attempt == 1 for job in jobs)
        assert session.query(JobStep).count() == 4
    assert chain.manager.dispatch.claims == set(executed)
    assert chain.maximum == 1 and chain.launches == LANES
    assert not chain.executor._active_jobs and not chain.manager._seat_waiters
    assert all(not lane.leases for lane in chain.manager.lanes.values())
    assert all(owner.closed for owner in chain.manager.dispatch.owners.values())
    assert all(ticket.state == "released" and ticket.pin is None
               for ticket in chain.manager.dispatch.tickets.values())
    assert not chain.manager.dispatch.cleanup_tasks
    assert await chain.scheduler.scan_once_async(chain.target) == []
    assert chain.submitted == ids
