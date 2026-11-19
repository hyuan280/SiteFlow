import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker import app as worker_app
from worker import browser_backend
from worker.app import LaneBusyError, LaneCapacityError, LaneManager, LaneStartError, create_app
from worker.browser_backend import BrowserCapacityError, BrowserSession, ManagerBackend


KEYS = [item["key"] for item in worker_app.LANE_DEFINITIONS]
AUTH = {"Authorization": "Bearer seat-test-token"}


@asynccontextmanager
async def manager_fixture(monkeypatch, tmp_path, *, budget=1, tier="free"):
    monkeypatch.setenv("SITEFLOW_BROWSER_MAX_ACTIVE_PROFILES", str(budget))
    monkeypatch.setenv("SITEFLOW_BROWSER_TOKEN", "seat-test-token")
    monkeypatch.setattr(worker_app, "DATA_DIR", tmp_path)
    monkeypatch.setattr(worker_app, "PROXY_URL", "http://test-proxy")
    monkeypatch.setenv("SITEFLOW_BROWSER_BACKEND", "cloak-manager")
    manager = LaneManager()
    launches, stops, active = [], [], set()
    status = AsyncMock(return_value={"license_tier": tier})
    monkeypatch.setattr(manager.backend, "_settings_request", status)

    async def start(key, proxy):
        assert manager._admission_lock.locked()
        assert manager.get(key).lock.locked()
        assert len(active) < (1 if tier == "free" else budget)
        active.add(key)
        launches.append(key)
        page = SimpleNamespace(goto=AsyncMock(), evaluate=AsyncMock(), wait_for_timeout=AsyncMock())
        context = SimpleNamespace(pages=[page], new_page=AsyncMock(side_effect=AssertionError("no page")))
        browser = SimpleNamespace(is_connected=lambda: True)
        return BrowserSession(context, browser, key, None, None)

    async def stop(session):
        assert manager._admission_lock.locked()
        stops.append(session.profile_id)
        active.discard(session.profile_id)

    monkeypatch.setattr(manager.backend, "start", start)
    monkeypatch.setattr(manager.backend, "stop", stop)
    try:
        yield manager, launches, stops, active, status
    finally:
        await manager.backend.close()


async def queued(manager, count):
    async with asyncio.timeout(2):
        while len(manager._seat_waiters) != count:
            await asyncio.sleep(0)


@pytest.mark.parametrize("budget", [1, 4])
@pytest.mark.parametrize("idle", [0, 300])
def test_free_sequential_profiles_reclaim_without_idle_grace(monkeypatch, tmp_path, budget, idle):
    async def run():
        async with manager_fixture(monkeypatch, tmp_path, budget=budget) as (manager, starts, stops, active, _):
            manager.idle_stop_seconds = idle
            for key in KEYS:
                lease, _ = await manager.lease_lane(key)
                assert active == {key}
                await manager.release_lane(key, lease)
            assert starts == KEYS
            assert stops == KEYS[:-1]
    asyncio.run(run())


def test_same_lane_parallel_reuses_without_status_or_extra_seats(monkeypatch, tmp_path):
    async def run():
        async with manager_fixture(monkeypatch, tmp_path) as (manager, starts, _, _, status):
            leases = await asyncio.gather(*(manager.lease_lane("general") for _ in range(8)))
            status.reset_mock()
            await manager.start_lane("general")
            assert len({lease for lease, _ in leases}) == 8
            assert starts == ["general"]
            status.assert_not_called()
    asyncio.run(run())


def test_cold_status_read_does_not_block_running_reuse(monkeypatch, tmp_path):
    async def run():
        async with manager_fixture(monkeypatch, tmp_path) as (manager, starts, _, _, _):
            await manager.lease_lane("general")
            entered = asyncio.Event()

            async def capacity():
                entered.set()
                await asyncio.Event().wait()

            monkeypatch.setattr(manager.backend, "capacity", capacity)
            cold = asyncio.create_task(manager.lease_lane("interaction"))
            await entered.wait()
            await asyncio.wait_for(manager.lease_lane("general"), 0.2)
            assert starts == ["general"]
            cold.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cold
    asyncio.run(run())


def test_paid_capacity_is_configured_not_unlimited(monkeypatch, tmp_path):
    async def run():
        async with manager_fixture(monkeypatch, tmp_path, budget=2, tier="pro") as (manager, starts, stops, active, _):
            await manager.lease_lane("general")
            second, _ = await manager.lease_lane("interaction")
            with pytest.raises(LaneCapacityError):
                await manager.start_lane("proxy_general")
            assert active == {"general", "interaction"} and stops == []
            await manager.release_lane("interaction", second)
            await manager.start_lane("proxy_general")
            assert active == {"general", "proxy_general"}
            assert starts == KEYS[:3] and stops == ["interaction"]
    asyncio.run(run())


def test_reverse_waiters_get_canonical_priority_and_reentrant_lane_can_share(monkeypatch, tmp_path):
    async def run():
        async with manager_fixture(monkeypatch, tmp_path) as (manager, starts, _, _, _):
            lease, _ = await manager.lease_lane("proxy_interaction")
            tasks = {key: asyncio.create_task(manager.lease_lane(key)) for key in reversed(KEYS[:-1])}
            await queued(manager, 3)
            shared, _ = await manager.lease_lane("proxy_interaction")
            await manager.release_lane("proxy_interaction", lease)
            assert not any(task.done() for task in tasks.values())
            await manager.release_lane("proxy_interaction", shared)
            for key in KEYS[:-1]:
                acquired, _ = await asyncio.wait_for(tasks[key], 2)
                assert starts[-1] == key
                await manager.release_lane(key, acquired)
            assert starts == ["proxy_interaction", *KEYS[:-1]]
            assert not manager._seat_waiters
    asyncio.run(run())


@pytest.mark.parametrize("protection", ["lease", "viewer", "task_page"])
def test_pressure_and_manual_stop_protect_users(monkeypatch, tmp_path, protection):
    async def run():
        async with manager_fixture(monkeypatch, tmp_path) as (manager, starts, stops, _, _):
            await manager.start_lane("general")
            lane = manager.get("general")
            if protection == "lease":
                owner, _ = await manager.lease_lane("general")
            elif protection == "viewer":
                await manager.viewer_acquire("general", "viewer")
            else:
                lane.task_pages["job"] = object()
            with pytest.raises(LaneBusyError):
                await manager.stop_lane("general")
            with pytest.raises(LaneCapacityError):
                await manager.start_lane("interaction")
            task = asyncio.create_task(manager.lease_lane("interaction"))
            await queued(manager, 1)
            assert starts == ["general"] and stops == []
            if protection == "lease":
                await manager.release_lane("general", owner)
            elif protection == "viewer":
                await manager.viewer_release("general", "viewer")
            else:
                lane.task_pages.clear()
                manager._seat_changed.set()
            await asyncio.wait_for(task, 2)
            assert stops == ["general"]
    asyncio.run(run())


def test_stop_failure_quarantines_seat_until_confirmed(monkeypatch, tmp_path):
    async def run():
        async with manager_fixture(monkeypatch, tmp_path) as (manager, starts, _, active, _):
            await manager.start_lane("general")
            session = manager.get("general").session
            stop = manager.backend.stop
            monkeypatch.setattr(manager.backend, "stop", AsyncMock(side_effect=RuntimeError("private")))
            with pytest.raises(LaneCapacityError):
                await manager.start_lane("interaction")
            assert starts == ["general"] and active == {"general"}
            assert manager.get("general").session is session
            assert manager.get("general").status == "error"
            monkeypatch.setattr(manager.backend, "stop", stop)
            await manager.start_lane("interaction")
            assert active == {"interaction"}
    asyncio.run(run())


@pytest.mark.parametrize("state", ["starting", "stopping", "quarantined"])
def test_transitions_and_backend_quarantine_count_as_occupied(monkeypatch, tmp_path, state):
    async def run():
        async with manager_fixture(monkeypatch, tmp_path) as (manager, starts, _, _, _):
            lane = manager.get("general")
            session = BrowserSession(None, None, "owned", None, None, disconnected=True)
            if state == "quarantined":
                lane.status = "error"
                manager.backend.uncertain_profiles["general"] = session
                monkeypatch.setattr(manager.backend, "stop", AsyncMock(side_effect=RuntimeError("unknown")))
            else:
                lane.status = state
            with pytest.raises(LaneCapacityError):
                await manager.start_lane("interaction")
            assert starts == []
            if state == "quarantined":
                assert lane.session is session
    asyncio.run(run())


def test_remote_capacity_retry_waits_instead_of_spinning(monkeypatch, tmp_path):
    async def run():
        async with manager_fixture(monkeypatch, tmp_path) as (manager, _, _, _, _):
            launch = AsyncMock(side_effect=BrowserCapacityError("402"))
            monkeypatch.setattr(manager.backend, "start", launch)
            monkeypatch.setattr(worker_app, "SEAT_WAIT_SECONDS", 0.06)
            with pytest.raises(LaneCapacityError, match="seat wait expired"):
                await manager.lease_lane("general")
            assert launch.await_count == 1
    asyncio.run(run())


def test_wait_timeout_and_cancellation_remove_tickets(monkeypatch, tmp_path):
    async def run():
        async with manager_fixture(monkeypatch, tmp_path) as (manager, starts, _, _, _):
            await manager.lease_lane("general")
            task = asyncio.create_task(manager.lease_lane("interaction"))
            await queued(manager, 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not manager._seat_waiters
            monkeypatch.setattr(worker_app, "SEAT_WAIT_SECONDS", 0.03)
            with pytest.raises(LaneCapacityError, match="seat wait expired"):
                await manager.lease_lane("interaction")
            assert not manager._seat_waiters and starts == ["general"]
            assert not manager._admission_lock.locked()
    asyncio.run(run())


@pytest.mark.parametrize("failed_stop", [False, True])
def test_cancellation_during_start_keeps_uncertain_ownership(monkeypatch, tmp_path, failed_stop):
    async def run():
        async with manager_fixture(monkeypatch, tmp_path) as (manager, _, _, active, _):
            entered = asyncio.Event()
            original = manager.backend.start

            async def launch(lane):
                lane.session = await original(lane.key, None)
                lane.context = lane.session.context
                entered.set()
                await asyncio.Event().wait()

            monkeypatch.setattr(manager, "_start_browser", launch)
            if failed_stop:
                monkeypatch.setattr(manager.backend, "stop", AsyncMock(side_effect=RuntimeError("unknown")))
            task = asyncio.create_task(manager.lease_lane("general"))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            lane = manager.get("general")
            assert bool(lane.session) is failed_stop
            assert bool(active) is failed_stop
            assert not lane.leases and not manager._seat_waiters
            if failed_stop:
                with pytest.raises(LaneCapacityError):
                    await manager.start_lane("interaction")
    asyncio.run(run())


def test_cancellation_during_error_cleanup_is_not_swallowed(monkeypatch, tmp_path):
    async def run():
        async with manager_fixture(monkeypatch, tmp_path) as (manager, _, _, _, _):
            entered, finish = asyncio.Event(), asyncio.Event()
            original = manager.backend.start

            async def launch(lane):
                lane.session = await original(lane.key, None)
                raise RuntimeError("startup failed")

            async def stop(session):
                entered.set()
                await finish.wait()

            monkeypatch.setattr(manager, "_start_browser", launch)
            monkeypatch.setattr(manager.backend, "stop", stop)
            task = asyncio.create_task(manager.lease_lane("general"))
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert manager.get("general").session is None
            assert not manager._seat_waiters and not manager._admission_lock.locked()
    asyncio.run(run())


def test_402_preflight_fails_fast_then_recovers_and_runtime_retries_only_launch(monkeypatch, tmp_path):
    async def run():
        async with manager_fixture(monkeypatch, tmp_path) as (manager, starts, _, _, _):
            original = manager.backend.start
            calls = []

            async def launch(key, proxy):
                calls.append(key)
                if len(calls) in (1, 3):
                    raise BrowserCapacityError("HTTP 402 private")
                return await original(key, proxy)

            monkeypatch.setattr(manager.backend, "start", launch)
            result = await manager.preflight(KEYS)
            assert not result["ready"] and result["first_lane"] is None
            assert calls == ["general"] and starts == []
            assert (await manager.preflight(KEYS))["ready"]
            monkeypatch.setattr(worker_app, "SEAT_RETRY_SECONDS", 0.02)
            lease, _ = await manager.lease_lane("interaction")
            assert calls == ["general", "general", "interaction", "interaction"]
            assert starts == ["general", "interaction"]
            assert manager.get("interaction").leases == {lease}
    asyncio.run(run())


def test_noncapacity_launch_failure_is_not_retried(monkeypatch, tmp_path):
    async def run():
        async with manager_fixture(monkeypatch, tmp_path) as (manager, _, _, _, _):
            launch = AsyncMock(side_effect=RuntimeError("HTTP 403"))
            monkeypatch.setattr(manager.backend, "start", launch)
            with pytest.raises(LaneStartError, match="403"):
                await manager.lease_lane("general")
            assert launch.await_count == 1
            assert not manager._capacity_pressure
    asyncio.run(run())


def test_multilane_preflight_only_starts_exact_first_canonical_lane(monkeypatch, tmp_path):
    async def run():
        async with manager_fixture(monkeypatch, tmp_path) as (manager, starts, _, active, _):
            result = await manager.preflight(list(reversed(KEYS)))
            assert result == {"backend": "cloak-manager", "ready": True,
                              "lanes": [{"key": key, "ready": True} for key in KEYS],
                              "first_lane": "general", "reason": None}
            assert starts == ["general"] and active == {"general"}
            lane = manager.get("general")
            assert not lane.leases
            lane.page.goto.reset_mock()
            lane.page.evaluate.reset_mock()
            assert (await manager.preflight(KEYS))["ready"]
            lane.page.goto.assert_not_called()
            lane.page.evaluate.assert_awaited_once_with("() => 1")
            lane.context.new_page.assert_not_called()
            lane.context = None
            assert not (await manager.preflight(KEYS))["ready"]
    asyncio.run(run())


def test_preflight_checks_all_disabled_required_lanes_before_any_launch(monkeypatch, tmp_path):
    async def run():
        async with manager_fixture(monkeypatch, tmp_path) as (manager, starts, _, _, _):
            manager.get("proxy_interaction").enabled = False
            assert not (await manager.preflight(KEYS))["ready"]
            assert starts == []
            result = await manager.preflight(["proxy_general", "interaction"])
            assert result["ready"] and result["first_lane"] == "interaction"
            assert starts == ["interaction"]
    asyncio.run(run())


@pytest.mark.parametrize("tier,expected", [("free", 1), ("pro", 4), ("keyless", 4), ("unknown", 4)])
def test_capacity_uses_finite_local_budget(monkeypatch, tmp_path, tier, expected):
    async def run():
        async with manager_fixture(monkeypatch, tmp_path, budget=4, tier=tier) as (manager, _, _, _, _):
            assert await manager.backend.capacity() == expected
    asyncio.run(run())


def test_local_budget_defaults_to_one_and_unknown_tier_is_not_unlimited(monkeypatch, tmp_path):
    async def run():
        async with manager_fixture(monkeypatch, tmp_path, tier="unknown") as (manager, _, _, _, _):
            monkeypatch.delenv("SITEFLOW_BROWSER_MAX_ACTIVE_PROFILES")
            assert worker_app.active_profile_budget() == 1
            assert await manager.backend.capacity() == 1
    asyncio.run(run())


def test_unconfirmed_successful_stop_still_retains_backend_quarantine(monkeypatch, tmp_path):
    async def run():
        backend = ManagerBackend(tmp_path)
        session = BrowserSession(None, None, "owned", None, None, disconnected=True)
        backend.uncertain_profiles["general"] = session
        request = AsyncMock(return_value={"status": "stopping"})
        monkeypatch.setattr(backend, "request", request)
        try:
            with pytest.raises(RuntimeError, match="unconfirmed"):
                await backend.stop(session)
            assert backend.uncertain_profiles == {"general": session}
            assert request.call_args_list[-1].args == ("GET", "/api/profiles/owned/status")
        finally:
            await backend.close()
    asyncio.run(run())


@pytest.mark.parametrize("value", ["0", "-1", "5", "unlimited", ""])
def test_invalid_local_budget_rejected(monkeypatch, tmp_path, value):
    monkeypatch.setenv("SITEFLOW_BROWSER_MAX_ACTIVE_PROFILES", value)
    monkeypatch.setenv("SITEFLOW_BROWSER_BACKEND", "cloak-manager")
    with pytest.raises(ValueError, match="between 1 and 4"):
        worker_app.active_profile_budget()


def test_shutdown_wakes_queue_and_forbids_new_starts(monkeypatch, tmp_path):
    async def run():
        async with manager_fixture(monkeypatch, tmp_path) as (manager, starts, _, _, _):
            await manager.lease_lane("general")
            task = asyncio.create_task(manager.lease_lane("interaction"))
            await queued(manager, 1)
            await manager.stop()
            with pytest.raises(LaneCapacityError, match="stopping"):
                await asyncio.wait_for(task, 2)
            with pytest.raises(LaneCapacityError, match="stopping"):
                await manager.start_lane("general")
            assert starts == ["general"] and not manager._seat_waiters
    asyncio.run(run())


@pytest.mark.parametrize("body", [b"{}", b'{"lanes":[]}', b'{"lanes":["general","general"]}',
                                  b'{"lanes":["unknown"]}', b'{"lanes":[1]}',
                                  b'{"lanes":["general"],"extra":"private"}',
                                  b'{"lanes":["general"],"lanes":["interaction"]}', b"x" * 4097])
def test_preflight_strict_body_static_errors(monkeypatch, tmp_path, body):
    async def run():
        async with manager_fixture(monkeypatch, tmp_path) as (manager, starts, _, _, _):
            app = create_app()
            app.state.manager = manager
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
                response = await client.post("/browser/preflight", content=body, headers=AUTH)
            assert response.status_code == 422
            assert response.json() == {"detail": "Invalid browser request"}
            assert starts == []
    asyncio.run(run())


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {**AUTH, "Origin": ""}])
def test_preflight_requires_bearer_without_origin(monkeypatch, tmp_path, headers):
    async def run():
        async with manager_fixture(monkeypatch, tmp_path) as (manager, starts, _, _, status):
            app = create_app()
            app.state.manager = manager
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
                response = await client.post("/browser/preflight", json={"lanes": KEYS}, headers=headers)
            assert response.status_code == 403 and starts == []
            status.assert_not_called()
    asyncio.run(run())


def test_preflight_http_response_and_no_business_actions(monkeypatch, tmp_path):
    async def run():
        async with manager_fixture(monkeypatch, tmp_path) as (manager, starts, _, _, _):
            app = create_app()
            app.state.manager = manager
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
                response = await client.post("/browser/preflight", json={"lanes": KEYS}, headers=AUTH)
            assert response.status_code == 200
            assert response.json()["ready"] is True and response.json()["first_lane"] == "general"
            assert starts == ["general"]
    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_backend_intermediate_launch_quarantine_never_drops_ownership(monkeypatch, tmp_path, cancel):
    async def run():
        backend = ManagerBackend(tmp_path)
        calls = []
        confirmed = False

        async def request(method, path, **kwargs):
            calls.append((method, path))
            if path == "/api/status":
                return {"license_tier": "free"}
            if path == "/api/profiles":
                return {"id": "owned"}
            if path.endswith("/launch"):
                if cancel:
                    raise asyncio.CancelledError
                raise RuntimeError("connect uncertain")
            if path.endswith("/stop") and not confirmed:
                raise RuntimeError("stop unknown")
            return {"status": "stopped"}

        monkeypatch.setattr(backend, "request", request)
        monkeypatch.setitem(sys.modules, "playwright.async_api", SimpleNamespace(async_playwright=None))
        try:
            with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
                await backend.start("general", None)
            assert backend.uncertain_profiles["general"].profile_id == "owned"
            assert backend.profiles == {"general": "owned"}
            assert calls[-1] == ("POST", "/api/profiles/owned/stop")
            confirmed = True
            await backend.stop(backend.uncertain_profiles["general"])
            assert not backend.uncertain_profiles
            assert all(path in {"/api/status", "/api/profiles"} or path.startswith("/api/profiles/owned")
                       for _, path in calls)
        finally:
            await backend.close()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["http", "timeout", "malformed"])
def test_capacity_status_failure_uses_one_seat(monkeypatch, tmp_path, failure):
    async def run():
        async with manager_fixture(monkeypatch, tmp_path, budget=4, tier="pro") as (manager, _, _, _, _):
            backend = manager.backend
            await backend.settings_client.aclose()

            def respond(request):
                if failure == "timeout":
                    raise httpx.ReadTimeout("private", request=request)
                return httpx.Response(503 if failure == "http" else 200, content="private")

            backend.settings_client = httpx.AsyncClient(
                base_url=backend.url, transport=httpx.MockTransport(respond),
            )
            monkeypatch.setattr(backend, "_settings_request", ManagerBackend._settings_request.__get__(backend))
            assert await backend.capacity() == 1
    asyncio.run(run())


def test_402_invalidates_estimate_and_reclaims_idle_owned_below_local_budget(monkeypatch, tmp_path):
    async def run():
        async with manager_fixture(monkeypatch, tmp_path, budget=4, tier="pro") as (manager, starts, stops, active, status):
            await manager.start_lane("general")
            owner, _ = await manager.lease_lane("interaction")
            original = manager.backend.start
            calls = []

            async def launch(key, proxy):
                calls.append(key)
                if "general" in active:
                    raise BrowserCapacityError("402")
                return await original(key, proxy)

            monkeypatch.setattr(manager.backend, "start", launch)
            monkeypatch.setattr(worker_app, "SEAT_RETRY_SECONDS", 0.02)
            status.reset_mock()
            lease, _ = await manager.lease_lane("proxy_general")
            assert status.await_count == 2
            assert calls == ["proxy_general", "proxy_general"]
            assert stops == ["general"]
            assert active == {"interaction", "proxy_general"}
            assert manager.get("interaction").leases == {owner}
            assert manager.get("proxy_general").leases == {lease}
            assert starts == KEYS[:3]
    asyncio.run(run())


def test_replacing_target_at_capacity_does_not_evict_other_idle_lane(monkeypatch, tmp_path):
    async def run():
        async with manager_fixture(monkeypatch, tmp_path, budget=2, tier="pro") as (manager, starts, stops, active, _):
            await manager.start_lane("general")
            await manager.start_lane("interaction")
            manager.get("interaction").status = "error"
            other = manager.get("general").session
            await manager.start_lane("interaction")
            assert stops == ["interaction"]
            assert starts == ["general", "interaction", "interaction"]
            assert active == {"general", "interaction"}
            assert manager.get("general").session is other
    asyncio.run(run())


@pytest.mark.parametrize("protection", [None, "lease", "viewer", "task_page", "stop_failure"])
def test_preflight_reconciles_retained_error_session_strictly(monkeypatch, tmp_path, protection):
    async def run():
        async with manager_fixture(monkeypatch, tmp_path) as (manager, starts, stops, _, _):
            await manager.start_lane("general")
            lane = manager.get("general")
            retained = lane.session
            lane.status = "error"
            if protection == "lease":
                lane.leases.add("owner")
            elif protection == "viewer":
                lane.viewers.add("owner")
            elif protection == "task_page":
                lane.task_pages["owner"] = object()
            elif protection == "stop_failure":
                monkeypatch.setattr(manager.backend, "stop", AsyncMock(side_effect=RuntimeError("unknown")))
            result = await manager.preflight(["general"])
            assert result["ready"] is (protection is None)
            if protection is None:
                assert stops == ["general"] and starts == ["general", "general"]
                assert lane.session is not retained
            else:
                assert starts == ["general"] and lane.session is retained
    asyncio.run(run())


@pytest.mark.parametrize("phantom_key", ["general", "interaction"])
def test_preflight_rejects_running_phantom_even_with_live_context(monkeypatch, tmp_path, phantom_key):
    async def run():
        async with manager_fixture(monkeypatch, tmp_path) as (manager, starts, _, _, _):
            lane = manager.get(phantom_key)
            lane.status = "running"
            lane.context = SimpleNamespace(pages=[SimpleNamespace(evaluate=AsyncMock())])
            result = await manager.preflight(["general", "interaction"])
            assert not result["ready"] and result["first_lane"] is None
            assert starts == []
            lane.context.pages[0].evaluate.assert_not_called()
    asyncio.run(run())


def test_real_manager_http_402_cleans_owned_profile_and_fails_preflight(monkeypatch, tmp_path):
    async def run():
        async with manager_fixture(monkeypatch, tmp_path) as (manager, _, _, _, _):
            backend = manager.backend
            for name in ("start", "stop", "_settings_request"):
                monkeypatch.setattr(backend, name, getattr(ManagerBackend, name).__get__(backend))
            await backend.client.aclose()
            await backend.settings_client.aclose()
            calls = []

            def respond(request):
                path = request.url.path
                calls.append((request.method, path))
                if path == "/api/status":
                    return httpx.Response(200, json={"license_tier": "free"})
                if path == "/api/profiles":
                    return httpx.Response(200, json={"id": "owned"})
                if path.endswith("/launch"):
                    return httpx.Response(402, json={"detail": "private-license-proxy"})
                if path.endswith("/status"):
                    return httpx.Response(200, json={"status": "stopped"})
                return httpx.Response(200, json={})

            backend.client = httpx.AsyncClient(base_url=backend.url, transport=httpx.MockTransport(respond))
            backend.settings_client = httpx.AsyncClient(base_url=backend.url, transport=httpx.MockTransport(respond))
            monkeypatch.setitem(sys.modules, "playwright.async_api", SimpleNamespace(async_playwright=None))
            app = create_app()
            app.state.manager = manager
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
                response = await client.post("/browser/preflight", json={"lanes": KEYS}, headers=AUTH)
            assert response.status_code == 200
            assert response.json()["ready"] is False
            assert response.json()["first_lane"] is None
            assert response.json()["reason"] == "browser_capacity_unavailable"
            assert "private-license-proxy" not in response.text
            assert calls[-3:] == [("POST", "/api/profiles/owned/launch"),
                                  ("POST", "/api/profiles/owned/stop"),
                                  ("GET", "/api/profiles/owned/status")]
            assert sum(path.endswith("/launch") for _, path in calls) == 1
            assert not backend.uncertain_profiles
            assert backend.profiles == {"general": "owned"}
            assert manager.get("general").session is None
            assert manager.get("general").status == "error"
    asyncio.run(run())


@pytest.mark.parametrize("stage", ["close", "status", "stop"])
@pytest.mark.parametrize("resist_cancel", [False, True])
def test_backend_stop_deadline_retains_ownership_without_holding_admission(
    monkeypatch, tmp_path, stage, resist_cancel,
):
    async def run():
        async with manager_fixture(monkeypatch, tmp_path) as (manager, _, _, _, _):
            backend = manager.backend
            monkeypatch.setattr(backend, "stop", ManagerBackend.stop.__get__(backend))
            monkeypatch.setattr(browser_backend, "BROWSER_STOP_TIMEOUT_SECONDS", 0.03)
            entered, cancelled, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
            calls = []

            async def blocked():
                entered.set()
                try:
                    await finish.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    if not resist_cancel:
                        raise
                    while not finish.is_set():
                        try:
                            await finish.wait()
                        except asyncio.CancelledError:
                            continue

            async def close():
                calls.append("close")
                if stage == "close":
                    await blocked()

            async def respond(request):
                calls.append(request.method)
                if ((stage == "status" and request.method == "GET")
                        or (stage == "stop" and request.method == "POST")):
                    await blocked()
                return httpx.Response(200, json={"status": "running"})

            await backend.client.aclose()
            backend.client = httpx.AsyncClient(base_url=backend.url, transport=httpx.MockTransport(respond))
            session = BrowserSession(None, SimpleNamespace(close=close), "owned", None, None)
            backend.profiles["general"] = "owned"
            backend.uncertain_profiles["general"] = session
            lane = manager.get("general")
            lane.status, lane.session = "running", session
            task = asyncio.create_task(manager.stop_lane("general"))
            await entered.wait()
            try:
                with pytest.raises(LaneStartError):
                    await asyncio.wait_for(task, 0.3)
                assert lane.session is session and lane.status == "error"
                assert backend.uncertain_profiles == {"general": session}
                assert not manager._admission_lock.locked() and not lane.lock.locked()
                await asyncio.wait_for(cancelled.wait(), 0.2)
                if resist_cancel:
                    retained = backend._stop_tasks["owned"]
                    assert not retained.done()
                    if stage == "close":
                            with pytest.raises(RuntimeError, match="still pending"):
                                await ManagerBackend.start(backend, "general", None)
                before = list(calls)
            finally:
                finish.set()
                pending = list(backend._stop_tasks.values())
                if lane.cleanup_task is not None:
                    pending.append(lane.cleanup_task)
                await asyncio.gather(*pending, return_exceptions=True)
            assert calls == before  # No new status/stop RPC gets a fresh timeout.
            assert backend.uncertain_profiles == {"general": session}
    asyncio.run(run())


def test_cancel_during_cdp_close_never_starts_fresh_status_wait(monkeypatch, tmp_path):
    async def run():
        backend = ManagerBackend(tmp_path)
        entered, finish = asyncio.Event(), asyncio.Event()

        async def close():
            entered.set()
            try:
                await finish.wait()
            except asyncio.CancelledError:
                await finish.wait()

        request = AsyncMock(side_effect=AssertionError("no fresh status request"))
        monkeypatch.setattr(backend, "request", request)
        session = BrowserSession(None, SimpleNamespace(close=close), "owned", None, None)
        backend.uncertain_profiles["general"] = session
        try:
            task = asyncio.create_task(backend.stop(session))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 0.2)
            assert backend.uncertain_profiles["general"] is session
            assert "owned" in backend._stop_tasks
        finally:
            finish.set()
            await asyncio.gather(*backend._stop_tasks.values(), return_exceptions=True)
            await backend.close()
        request.assert_not_called()
        assert backend.uncertain_profiles["general"] is session
    asyncio.run(run())


def test_lane_retains_cancellation_resistant_stop_task_and_seat(monkeypatch, tmp_path):
    async def run():
        async with manager_fixture(monkeypatch, tmp_path) as (manager, _, _, _, _):
            await manager.start_lane("general")
            lane = manager.get("general")
            session = lane.session
            finish = asyncio.Event()

            async def stop(value):
                while not finish.is_set():
                    try:
                        await finish.wait()
                    except asyncio.CancelledError:
                        continue

            monkeypatch.setattr(manager.backend, "stop", stop)
            monkeypatch.setattr(browser_backend, "BROWSER_STOP_TIMEOUT_SECONDS", 0.02)
            try:
                with pytest.raises(LaneStartError):
                    await asyncio.wait_for(manager.stop_lane("general"), 0.2)
                retained = lane.cleanup_task
                assert retained is not None and not retained.done()
                assert lane.session is session
                assert not manager._admission_lock.locked() and not lane.lock.locked()
                with pytest.raises(LaneCapacityError):
                    await asyncio.wait_for(manager.start_lane("interaction"), 0.2)
                assert lane.cleanup_task is retained
            finally:
                finish.set()
                await asyncio.gather(lane.cleanup_task, return_exceptions=True)
            # Completion alone never discards ownership; a later strict reconcile does.
            assert lane.session is session
    asyncio.run(run())


@pytest.mark.parametrize("settings_change", ["completed", "locked", "unknown"])
def test_post_preflight_rechecks_settings_after_running_browser_verification(
    monkeypatch, tmp_path, settings_change,
):
    async def run():
        async with manager_fixture(monkeypatch, tmp_path) as (manager, starts, _, _, _):
            await manager.start_lane("general")
            backend = manager.backend
            lane = manager.get("general")
            original_session = lane.session
            entered, finish = asyncio.Event(), asyncio.Event()

            async def verify(script):
                assert script == "() => 1"
                entered.set()
                await finish.wait()

            monkeypatch.setattr(lane.page, "evaluate", verify)
            app = create_app()
            app.state.manager = manager
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
                request = asyncio.create_task(client.post(
                    "/browser/preflight", json={"lanes": KEYS}, headers=AUTH,
                ))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    if settings_change == "completed":
                        monkeypatch.delenv("CLOAKBROWSER_LICENSE_KEY", raising=False)
                        monkeypatch.setattr(backend, "browser_settings", AsyncMock(return_value={
                            "connected": True, "license": {"editable": True},
                        }))
                        await backend.update_browser_settings("test-key")
                        assert backend.settings_generation == 1
                        assert not backend.settings_lock.locked() and not backend.settings_write_unknown
                    elif settings_change == "locked":
                        await backend.settings_lock.acquire()
                        assert backend.settings_generation == 0
                    else:
                        backend.settings_write_unknown = True
                        assert backend.settings_generation == 0
                    finish.set()
                    response = await asyncio.wait_for(request, 1)
                    assert response.status_code == 200
                    data = response.json()
                    assert data["ready"] is False and data["first_lane"] is None
                    assert data["reason"] == "browser_unavailable"
                    assert all(not lane_flag["ready"] for lane_flag in data["lanes"])
                    assert starts == ["general"] and lane.session is original_session
                    assert not lane.leases
                finally:
                    finish.set()
                    if backend.settings_lock.locked():
                        backend.settings_lock.release()
                    await asyncio.gather(request, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["start", "prepare"])
def test_profile_reconciliation_retains_cancel_resistant_http_stop_without_lock_leak(
    monkeypatch, tmp_path, operation,
):
    async def run():
        async with manager_fixture(monkeypatch, tmp_path) as (manager, _, _, _, _):
            backend = manager.backend
            for name in ("start", "stop"):
                monkeypatch.setattr(backend, name, getattr(ManagerBackend, name).__get__(backend))
            backend.profiles["general"] = "owned"
            monkeypatch.setattr(browser_backend, "BROWSER_STOP_TIMEOUT_SECONDS", 0.02)
            entered, cancelled, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
            calls = []

            async def respond(request):
                path = request.url.path
                calls.append((request.method, path))
                if path == "/api/status":
                    return httpx.Response(200, json={"license_tier": "free"})
                if request.method == "POST" and path == "/api/profiles/owned/stop":
                    entered.set()
                    while not finish.is_set():
                        try:
                            await finish.wait()
                        except asyncio.CancelledError:
                            cancelled.set()
                    return httpx.Response(200, json={})
                assert path == "/api/profiles/owned/status" and request.method == "GET"
                return httpx.Response(200, json={"status": "running"})

            await backend.client.aclose()
            backend.client = httpx.AsyncClient(base_url=backend.url, transport=httpx.MockTransport(respond))
            task = asyncio.create_task(
                manager.start_lane("general") if operation == "start" else backend.prepare()
            )
            try:
                await asyncio.wait_for(entered.wait(), 1)
                with pytest.raises(LaneStartError if operation == "start" else TimeoutError):
                    await asyncio.wait_for(task, 0.3)
                await asyncio.wait_for(cancelled.wait(), 0.2)
                assert not backend.lock.locked()
                assert not manager._admission_lock.locked() and not manager.get("general").lock.locked()
                retained = backend._stop_tasks["owned"]
                assert not retained.done()
                uncertain = backend.uncertain_profiles["general"]
                assert uncertain.profile_id == "owned"
                assert backend.profiles == {"general": "owned"}
                if operation == "start":
                    assert manager.get("general").status == "error"
                    assert manager.get("general").session is uncertain
                with pytest.raises(RuntimeError, match="still pending"):
                    await backend.start("general", None)
                assert sum(method == "POST" for method, _ in calls) == 1
                assert not any(path.endswith("/launch") or method == "PUT" for method, path in calls)
                snapshot = list(calls)
            finally:
                finish.set()
                await asyncio.gather(task, *backend._stop_tasks.values(), return_exceptions=True)
            assert calls == snapshot
            assert backend.uncertain_profiles["general"] is uncertain
    asyncio.run(run())
