import asyncio
import copy
import json
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker import app as worker_app
from worker import dispatch, executor
from worker.app import LaneCapacityError, LaneManager, create_app
from worker.browser_backend import BrowserSession


AUTH = {"Authorization": "Bearer dispatch-test-token"}
KEYS = list(dispatch.LANES)


def uid():
    return str(uuid.uuid4())


def batch(lanes=KEYS, run_id=7):
    return {"run_id": run_id, "tickets": [{"id": uid(), "lane": lane} for lane in lanes]}


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


@asynccontextmanager
async def worker(monkeypatch, tmp_path, capacity=1):
    monkeypatch.setenv("SITEFLOW_BROWSER_TOKEN", "dispatch-test-token")
    monkeypatch.setenv("SITEFLOW_BROWSER_BACKEND", "cloak-manager")
    monkeypatch.setenv("SITEFLOW_BROWSER_MAX_ACTIVE_PROFILES", str(capacity))
    monkeypatch.setattr(worker_app, "PROXY_URL", "http://proxy.invalid")
    monkeypatch.setattr(worker_app, "DATA_DIR", tmp_path)
    monkeypatch.setattr(worker_app, "SEAT_RETRY_SECONDS", 0.01)
    manager = LaneManager()
    launches, stops, active = [], [], set()

    async def start(key, proxy):
        assert manager._admission_lock.locked() and manager.get(key).lock.locked()
        assert len(active) < capacity
        active.add(key)
        launches.append(key)
        page = SimpleNamespace(goto=AsyncMock(), evaluate=AsyncMock(), wait_for_timeout=AsyncMock())
        context = SimpleNamespace(pages=[page], new_page=AsyncMock(side_effect=AssertionError("no task pages")))
        return BrowserSession(context, SimpleNamespace(is_connected=lambda: True), key, None, None)

    async def stop(session):
        stops.append(session.profile_id)
        active.remove(session.profile_id)

    monkeypatch.setattr(manager.backend, "capacity", AsyncMock(return_value=capacity))
    monkeypatch.setattr(manager.backend, "start", start)
    monkeypatch.setattr(manager.backend, "stop", stop)
    app = create_app()
    app.state.manager = manager
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker", headers=AUTH) as client:
            yield manager, client, launches, stops, active
    finally:
        await manager.dispatch.close()
        await manager.backend.close()


def test_capacity_two_grants_same_owner_lanes_concurrently(monkeypatch, tmp_path):
    async def run():
        async with worker(monkeypatch, tmp_path, 2) as (manager, client, starts, _, active):
            owner, payload = uid(), batch(KEYS[:2])
            response = await client.put(f"/browser/dispatch/{owner}", json=payload)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert {item["state"] for item in response.json()["tickets"]} == {"pending"}
            await until(lambda: all(t.state == "granted" for t in manager.dispatch.tickets.values()))
            assert starts == KEYS[:2] and active == set(KEYS[:2])
            assert all(manager.get(key).in_use == 1 for key in KEYS[:2])
            for key in KEYS[:2]:
                lane = manager.get(key)
                assert lane.execution_semaphore.active == 0 and not lane.task_pages
                lane.context.new_page.assert_not_called()
                assert lane.page.goto.call_args.args[0] == "about:blank"
    asyncio.run(run())


def test_free_all_four_priority_after_each_pin_release(monkeypatch, tmp_path):
    async def run():
        async with worker(monkeypatch, tmp_path) as (manager, client, starts, stops, active):
            owner, payload = uid(), batch(list(reversed(KEYS)))
            response = await client.put(f"/browser/dispatch/{owner}", json=payload)
            assert [row["lane"] for row in response.json()["tickets"]] == KEYS
            by_lane = {row["lane"]: row["id"] for row in payload["tickets"]}
            for index, key in enumerate(KEYS):
                ticket = manager.dispatch.tickets[by_lane[key]]
                await until(lambda: ticket.state == "granted")
                assert starts == KEYS[:index + 1] and active == {key}
                assert all(t.state == "pending" for t in manager.dispatch.tickets.values()
                           if KEYS.index(t.lane) > index)
                response = await client.delete(f"/browser/dispatch/{owner}/{ticket.id}")
                assert response.json()["tickets"][0]["state"] == "released"
                await until(lambda: ticket.pin is None)
            assert stops == KEYS[:-1]
            assert not manager._seat_waiters
    asyncio.run(run())


def test_idempotent_lost_ack_late_batch_and_cross_owner_pins(monkeypatch, tmp_path):
    async def run():
        async with worker(monkeypatch, tmp_path, 2) as (manager, client, starts, _, _):
            owner, payload = uid(), batch(["general"])
            url = f"/browser/dispatch/{owner}"
            await client.put(url, json=payload)
            ticket = manager.dispatch.tickets[payload["tickets"][0]["id"]]
            await until(lambda: ticket.state == "granted")
            pin, expiry = ticket.pin, ticket.expires
            for _ in range(3):
                response = await client.put(url, json=payload)
                assert response.json()["tickets"][0]["state"] == "granted"
            assert ticket.pin == pin and ticket.expires > expiry
            second_owner, second = uid(), batch(["general"])
            await client.put(f"/browser/dispatch/{second_owner}", json=second)
            other = manager.dispatch.tickets[second["tickets"][0]["id"]]
            await until(lambda: other.state == "granted")
            assert manager.get("general").leases == {pin, other.pin} and starts == ["general"]
            fallback = batch(["interaction"])
            await client.put(url, json=fallback)
            late = manager.dispatch.tickets[fallback["tickets"][0]["id"]]
            await until(lambda: late.state == "granted")
            assert ticket.pin == pin and starts == KEYS[:2]
            await client.delete(url)
            await until(lambda: ticket.pin is None and late.pin is None)
            assert manager.get("general").leases == {other.pin}
            assert (await client.put(url, json=payload)).status_code == 409
    asyncio.run(run())


def test_bindings_and_batch_validation_are_atomic(monkeypatch, tmp_path):
    async def run():
        async with worker(monkeypatch, tmp_path) as (manager, client, _, _, _):
            owner, payload = uid(), batch(["general"])
            url = f"/browser/dispatch/{owner}"
            await client.put(url, json=payload)
            for changed_owner, changed in [
                (uid(), payload), (owner, {**payload, "run_id": 8}),
                (owner, {**payload, "tickets": [{**payload["tickets"][0], "lane": "interaction"}]}),
                (owner, {**payload, "tickets": [*batch(["interaction"])["tickets"],
                                               {**payload["tickets"][0], "lane": "proxy_general"}]}),
            ]:
                response = await client.put(f"/browser/dispatch/{changed_owner}", json=changed)
                assert response.status_code == 409
                assert response.json() == {"detail": "Browser dispatch binding conflict"}
            assert len(manager.dispatch.tickets) == 1
    asyncio.run(run())


def test_delete_before_create_unknown_delete_and_cancel_pending(monkeypatch, tmp_path):
    async def run():
        async with worker(monkeypatch, tmp_path) as (manager, client, starts, _, _):
            owner, payload = uid(), batch(KEYS[:2])
            first = payload["tickets"][0]["id"]
            await client.delete(f"/browser/dispatch/{owner}/{first}")
            response = await client.put(f"/browser/dispatch/{owner}", json=payload)
            assert response.json()["tickets"][0]["state"] == "released"
            second = manager.dispatch.tickets[payload["tickets"][1]["id"]]
            await until(lambda: second.state == "granted")
            assert starts == ["interaction"]
            pending = batch(["proxy_general"])
            await client.put(f"/browser/dispatch/{owner}", json=pending)
            await client.delete(f"/browser/dispatch/{owner}")
            await until(lambda: not manager._seat_waiters and second.pin is None)
            assert starts == ["interaction"]
            unknown = uid()
            assert (await client.delete(f"/browser/dispatch/{unknown}")).status_code == 200
            assert (await client.put(f"/browser/dispatch/{unknown}", json=batch())).status_code == 409
            assert (await client.delete(f"/browser/dispatch/{owner}/{first}")).status_code == 200
    asyncio.run(run())


def test_cancel_grant_race_and_cancelled_delete_retain_cleanup(monkeypatch, tmp_path):
    async def run():
        async with worker(monkeypatch, tmp_path) as (manager, client, _, _, _):
            acquired, finish, releasing, release_finish = (asyncio.Event() for _ in range(4))
            original_lease, original_release = manager.lease_lane, manager.release_lane

            async def stubborn_lease(key):
                result = await original_lease(key)
                acquired.set()
                try:
                    await finish.wait()
                except asyncio.CancelledError:
                    await finish.wait()
                return result

            async def blocked_release(key, pin):
                releasing.set()
                await release_finish.wait()
                return await original_release(key, pin)

            monkeypatch.setattr(manager, "lease_lane", stubborn_lease)
            monkeypatch.setattr(manager, "release_lane", blocked_release)
            owner, payload = uid(), batch(["general"])
            await client.put(f"/browser/dispatch/{owner}", json=payload)
            await acquired.wait()
            request = asyncio.create_task(client.delete(f"/browser/dispatch/{owner}"))
            await request
            ticket = manager.dispatch.tickets[payload["tickets"][0]["id"]]
            assert ticket.state == "released"
            finish.set()
            await releasing.wait()
            closing = asyncio.create_task(manager.dispatch.close())
            await asyncio.sleep(0)
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing
            assert manager.get("general").in_use == 1
            release_finish.set()
            await manager.dispatch.close()
            assert ticket.pin is None and manager.get("general").in_use == 0
            assert not manager._seat_waiters and not manager.dispatch.cleanup_tasks
    asyncio.run(run())


def test_ttl_drops_only_pin_job_and_viewer_survive(monkeypatch, tmp_path):
    async def run():
        async with worker(monkeypatch, tmp_path) as (manager, client, _, _, _):
            owner, payload = uid(), batch(["general"])
            await client.put(f"/browser/dispatch/{owner}", json=payload)
            ticket = manager.dispatch.tickets[payload["tickets"][0]["id"]]
            await until(lambda: ticket.state == "granted")
            capability = {"owner": owner, "id": ticket.id, "generation": manager.dispatch.generation}
            job, _ = await manager.dispatch.lease_job("general", 7, capability, job_id=1, attempt=1)
            assert job != ticket.pin
            await manager.viewer_acquire("general", "viewer")
            ticket.expires = 0
            manager.dispatch.expire()
            await until(lambda: ticket.pin is None)
            lane = manager.get("general")
            assert ticket.state == "released" and lane.leases == {job} and lane.viewers == {"viewer"}
            assert (await client.put(f"/browser/dispatch/{owner}", json=payload)).json()["tickets"][0]["state"] == "released"
            with pytest.raises(RuntimeError, match=dispatch.INVALID_RESERVATION):
                await manager.dispatch.lease_job("general", 7, capability, job_id=1, attempt=1)
            await manager.release_lane("general", job)
    asyncio.run(run())


def test_keepalive_pending_retry_capacity_not_business_failure(monkeypatch, tmp_path):
    async def run():
        async with worker(monkeypatch, tmp_path) as (manager, client, _, _, _):
            monkeypatch.setattr(dispatch, "RETRY_SECONDS", 0.01)
            original = manager.lease_lane
            attempts = []

            async def lease(key):
                attempts.append(key)
                if len(attempts) < 3:
                    raise LaneCapacityError("private capacity")
                return await original(key)

            monkeypatch.setattr(manager, "lease_lane", lease)
            owner, payload = uid(), batch(["general"])
            url = f"/browser/dispatch/{owner}"
            await client.put(url, json=payload)
            ticket = manager.dispatch.tickets[payload["tickets"][0]["id"]]
            await until(lambda: len(attempts) >= 2)
            assert ticket.state == "pending" and not manager.get("general").leases
            assert (await client.put(url, json=payload)).status_code == 200
            await until(lambda: ticket.state == "granted")
            assert len(attempts) == 3
            monkeypatch.setattr(manager, "lease_lane", AsyncMock(side_effect=RuntimeError("private failure")))
            failed = batch(["interaction"])
            await client.put(url, json=failed)
            await until(lambda: manager.dispatch.tickets[failed["tickets"][0]["id"]].state == "failed")
            response = await client.put(url, json=failed)
            assert response.json()["tickets"][0]["state"] == "failed" and "private" not in response.text
            assert manager.lease_lane.await_count == 1
    asyncio.run(run())


def test_real_admission_timeout_keeps_ticket_pending_and_retries(monkeypatch, tmp_path):
    async def run():
        async with worker(monkeypatch, tmp_path) as (manager, client, starts, _, _):
            monkeypatch.setattr(worker_app, "SEAT_WAIT_SECONDS", 0.025)
            monkeypatch.setattr(dispatch, "RETRY_SECONDS", 0.01)
            blocker, _ = await manager.lease_lane("proxy_interaction")
            owner, payload = uid(), batch(KEYS[:2])
            url = f"/browser/dispatch/{owner}"
            await client.put(url, json=payload)
            tickets = list(manager.dispatch.tickets.values())
            for _ in range(4):
                await asyncio.sleep(0.02)
                response = await client.put(url, json=payload)
                assert {row["state"] for row in response.json()["tickets"]} == {"pending"}
            assert starts == ["proxy_interaction"]
            assert all(ticket.pin is None and not ticket.task.done() for ticket in tickets)
            await manager.release_lane("proxy_interaction", blocker)
            await until(lambda: tickets[0].state == "granted")
            assert starts == ["proxy_interaction", "general"]
            assert tickets[1].state == "pending"
    asyncio.run(run())


def test_job_lease_rechecks_expiry_after_waiting_for_admission_lock(monkeypatch, tmp_path):
    async def run():
        async with worker(monkeypatch, tmp_path) as (manager, client, _, _, _):
            owner, payload = uid(), batch(["general"])
            await client.put(f"/browser/dispatch/{owner}", json=payload)
            ticket = manager.dispatch.tickets[payload["tickets"][0]["id"]]
            await until(lambda: ticket.state == "granted")
            capability = {"owner": owner, "id": ticket.id, "generation": manager.dispatch.generation}
            async with manager._admission_lock:
                job = asyncio.create_task(manager.dispatch.lease_job("general", 7, capability, job_id=1, attempt=1))
                await asyncio.sleep(0)
                ticket.expires = 0
            with pytest.raises(RuntimeError, match=dispatch.INVALID_RESERVATION):
                await job
            assert manager.get("general").leases == {ticket.pin}
    asyncio.run(run())


@pytest.mark.parametrize("change", ["generation", "owner", "id", "lane", "run", "bool_run", "expired", "pin", "session", "disconnected", "pending", "null", "extra"])
def test_capability_validation_never_autostarts(monkeypatch, tmp_path, change):
    async def run():
        async with worker(monkeypatch, tmp_path) as (manager, client, starts, _, _):
            owner, payload = uid(), batch(["general"])
            await client.put(f"/browser/dispatch/{owner}", json=payload)
            ticket = manager.dispatch.tickets[payload["tickets"][0]["id"]]
            await until(lambda: ticket.state == "granted")
            capability = {"owner": owner, "id": ticket.id, "generation": manager.dispatch.generation}
            lane, run_id = "general", 7
            if change in {"generation", "owner", "id"}:
                capability[change] = uid()
            elif change == "lane":
                lane = "interaction"
            elif change in {"run", "bool_run"}:
                run_id = 8 if change == "run" else True
            elif change == "expired":
                ticket.expires = 0
            elif change == "pin":
                manager.get(lane).leases.clear()
            elif change == "session":
                ticket.session = object()
            elif change == "disconnected":
                manager.get(lane).session.disconnected = True
            elif change == "pending":
                ticket.state = "pending"
            elif change == "null":
                capability = None
            else:
                capability["extra"] = "private"
            before = set(manager.get(lane).leases)
            with pytest.raises(RuntimeError, match=f"^{dispatch.INVALID_RESERVATION}$"):
                await manager.dispatch.lease_job(lane, run_id, capability, job_id=1, attempt=1)
            assert manager.get(lane).leases == before and starts == ["general"]
    asyncio.run(run())


def test_registry_bounds_do_not_evict_live_and_tombstones_compact_permanently(monkeypatch, tmp_path):
    async def run():
        async with worker(monkeypatch, tmp_path) as (manager, client, _, _, _):
            monkeypatch.setattr(dispatch, "MAX_OWNERS", 1)
            monkeypatch.setattr(dispatch, "MAX_TICKETS", 1)
            owner, payload = uid(), batch(["general"])
            await client.put(f"/browser/dispatch/{owner}", json=payload)
            ticket = manager.dispatch.tickets[payload["tickets"][0]["id"]]
            await until(lambda: ticket.state == "granted")
            assert (await client.put(f"/browser/dispatch/{uid()}", json=batch(["general"]))).status_code == 503
            assert (await client.put(f"/browser/dispatch/{owner}", json=batch(["interaction"]))).status_code == 503
            assert ticket.state == "granted" and ticket.pin in manager.get("general").leases
            await client.delete(f"/browser/dispatch/{owner}")
            await until(lambda: ticket.pin is None)
            assert (await client.put(f"/browser/dispatch/{owner}", json=payload)).status_code == 409
            ticket.retired -= 601
            manager.dispatch.owners[owner].retired -= 601
            manager.dispatch.expire()
            assert not manager.dispatch.owners and not manager.dispatch.tickets
            assert owner in manager.dispatch.retired_owners
            assert ticket.id in manager.dispatch.retired_tickets
            assert (await client.put(f"/browser/dispatch/{owner}", json=payload)).status_code == 409
            assert (await client.put(f"/browser/dispatch/{uid()}", json=payload)).status_code == 409
            assert (await client.put(f"/browser/dispatch/{uid()}", json=batch(["general"]))).status_code == 200
    asyncio.run(run())


@pytest.mark.parametrize("headers,token", [
    ({}, "dispatch-test-token"), ({"Authorization": "Bearer wrong"}, "dispatch-test-token"),
    ({**AUTH, "Origin": ""}, "dispatch-test-token"), ({**AUTH, "Origin": "null"}, "dispatch-test-token"),
    (AUTH, ""),
])
@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_operation_auth_no_origin_no_store(monkeypatch, tmp_path, headers, token, method):
    async def run():
        async with worker(monkeypatch, tmp_path) as (manager, client, starts, _, _):
            monkeypatch.setenv("SITEFLOW_BROWSER_TOKEN", token)
            client.headers.clear()
            response = await client.request(method, f"/browser/dispatch/{uid()}", headers=headers, json=batch())
            assert response.status_code == 403 and response.json() == {"detail": "Forbidden"}
            assert response.headers["cache-control"] == "no-store"
            assert not manager.dispatch.owners and starts == []
    asyncio.run(run())


@pytest.mark.parametrize("body", [
    b"{}", b"null", b"[]", b"private", b'{"run_id":7,"run_id":8,"tickets":[]}',
    json.dumps(batch([], 7)).encode(), json.dumps(batch(["unknown"])).encode(),
    json.dumps(batch(["general"], True)).encode(), json.dumps(batch(["general"], "7")).encode(),
    json.dumps(batch(["general"], 0)).encode(), json.dumps(batch(["general"], -1)).encode(),
    json.dumps(batch(["general", "general"])).encode(),
    b'{"run_id":7,"tickets":[{"id":"private","lane":"general"}]}',
    b'{"run_id":7,"tickets":[{"id":"private","lane":[]}]}',
    b'{"run_id":7,"tickets":[],"extra":"private"}',
])
def test_strict_body_static_errors(monkeypatch, tmp_path, body):
    async def run():
        async with worker(monkeypatch, tmp_path) as (manager, client, starts, _, _):
            response = await client.put(f"/browser/dispatch/{uid()}", content=body)
            assert response.status_code == 422 and response.json() == {"detail": "Invalid browser request"}
            assert response.headers["cache-control"] == "no-store"
            assert not manager.dispatch.tickets and starts == []
    asyncio.run(run())


def test_duplicate_ids_oversize_stream_unknown_path_and_query(monkeypatch, tmp_path):
    async def run():
        async with worker(monkeypatch, tmp_path) as (manager, client, _, _, _):
            payload = batch(KEYS[:2])
            payload["tickets"][1]["id"] = payload["tickets"][0]["id"]
            assert (await client.put(f"/browser/dispatch/{uid()}", json=payload)).status_code == 422

            async def chunks():
                yield b"x" * 8192
                yield b"x"

            response = await client.put(f"/browser/dispatch/{uid()}", content=chunks())
            assert response.status_code == 413 and response.headers["cache-control"] == "no-store"
            for path, status in [("private", 422), (f"{uid()}/private", 422),
                                 (f"{uid()}?ticket_id={uid()}", 422), (f"{uid()}/{uid()}/unknown", 404)]:
                response = await client.delete(f"/browser/dispatch/{path}")
                assert response.status_code == status and response.headers["cache-control"] == "no-store"
                assert "private" not in response.text
            assert not manager.dispatch.owners
    asyncio.run(run())


def test_executor_uses_independent_validated_job_lease_and_old_payload_compatibility(monkeypatch, tmp_path):
    async def run():
        async with worker(monkeypatch, tmp_path) as (manager, client, _, _, _):
            monkeypatch.setattr(executor, "ARTIFACT_ROOT", tmp_path / "artifacts")
            owner, payload = uid(), batch(["general"])
            await client.put(f"/browser/dispatch/{owner}", json=payload)
            ticket = manager.dispatch.tickets[payload["tickets"][0]["id"]]
            await until(lambda: ticket.state == "granted")
            lane = manager.get("general")
            capability = {"owner": owner, "id": ticket.id, "generation": manager.dispatch.generation}
            job_payload = {"run_id": 7, "job_id": 1, "attempt": 1, "lane_key": "general", "steps": [],
                           "dispatch_reservation": capability, "cookies": [{"name": "private"}]}
            lane.context.add_cookies = AsyncMock()
            observed = []

            async def no_page():
                observed.append(set(lane.leases))
                raise RuntimeError("test setup failure")

            lane.context.new_page = no_page
            normal_lease = manager.lease_lane
            monkeypatch.setattr(manager, "lease_lane", AsyncMock(side_effect=AssertionError("must not wait")))
            result = await executor._execute_admitted_job(manager, job_payload)
            assert result["reason"] == "browser_setup_failed"
            assert len(observed[0]) == 2 and ticket.pin in observed[0]
            assert lane.leases == {ticket.pin}
            invalid = copy.deepcopy(job_payload)
            invalid["dispatch_reservation"]["generation"] = uid()
            lane.context.add_cookies.reset_mock()
            with pytest.raises(RuntimeError, match=dispatch.INVALID_RESERVATION):
                await executor._execute_admitted_job(manager, invalid)
            lane.context.add_cookies.assert_not_called()
            assert len(observed) == 1
            del job_payload["dispatch_reservation"]
            fake = SimpleNamespace(get=manager.get, lease_lane=AsyncMock(wraps=normal_lease),
                                   release_lane=manager.release_lane)
            result = await executor._execute_admitted_job(fake, job_payload)
            assert result["reason"] == "browser_setup_failed"
            fake.lease_lane.assert_awaited_once_with("general")
            assert lane.leases == {ticket.pin}
    asyncio.run(run())


def test_manager_stop_closes_registry_before_lane_teardown(monkeypatch, tmp_path):
    async def run():
        async with worker(monkeypatch, tmp_path) as (manager, client, _, _, _):
            owner, payload = uid(), batch()
            await client.put(f"/browser/dispatch/{owner}", json=payload)
            await until(lambda: any(t.state == "granted" for t in manager.dispatch.tickets.values()))
            stop = manager.backend.stop

            async def stopped(session):
                assert manager.dispatch.closed and not manager.dispatch.cleanup_tasks
                assert all(t.task.done() and t.pin is None for t in manager.dispatch.tickets.values())
                await stop(session)

            monkeypatch.setattr(manager.backend, "stop", stopped)
            await manager.stop()
            assert not manager._seat_waiters and manager.dispatch.reaper.done()
            assert all(not lane.leases for lane in manager.lanes.values())
    asyncio.run(run())


def test_backend_coordinator_real_worker_protocol_and_keepalive(monkeypatch, tmp_path):
    from pydantic import SecretStr

    from app.services import browser_dispatch as backend_dispatch

    async def run():
        async with worker(monkeypatch, tmp_path, 2) as (manager, client, starts, _, _):
            real_client = httpx.AsyncClient
            transport = client._transport

            def internal_client(*args, **kwargs):
                kwargs["transport"] = transport
                return real_client(*args, **kwargs)

            monkeypatch.setattr(httpx, "AsyncClient", internal_client)
            monkeypatch.setattr(backend_dispatch, "POLL_SECONDS", 0.01)
            coordinator = backend_dispatch.RunDispatch(backend_dispatch.DispatchClient(SimpleNamespace(
                browser_token=SecretStr("dispatch-test-token"), worker_url="http://worker",
            )), 7)
            for key in KEYS[:2]:
                coordinator.add(key)
            maintaining = asyncio.create_task(coordinator.maintain())
            try:
                await until(lambda: all(t.state == "granted" for t in coordinator.tickets.values()))
                assert coordinator.generation == manager.dispatch.generation and starts == KEYS[:2]
                ticket = coordinator.tickets["general"]
                pin = manager.dispatch.tickets[ticket.id]
                previous_expiry = pin.expires
                # Heartbeats continue even when no job has yet acquired a global slot.
                await until(lambda: pin.expires > previous_expiry)
                capability = await coordinator.reservation(ticket)
                job, _ = await manager.dispatch.lease_job("general", 7, capability, job_id=1, attempt=1)
                assert manager.get("general").leases == {pin.pin, job}
                await coordinator.release("general")
                await until(lambda: pin.pin is None)
                assert manager.get("general").leases == {job}
                await manager.release_lane("general", job)
            finally:
                maintaining.cancel()
                await asyncio.gather(maintaining, return_exceptions=True)
                await coordinator.client.close(coordinator.owner)

    asyncio.run(run())


@pytest.mark.parametrize("field,value", [
    (field, value) for field in ("job_id", "attempt")
    for value in (None, True, False, 0, -1, "1", 1.0, [], {})
] + [("attempt", 4)])
def test_executor_rejects_bad_dispatch_identity_before_business(monkeypatch, tmp_path, field, value):
    async def run():
        async with worker(monkeypatch, tmp_path) as (manager, client, _, _, _):
            monkeypatch.setattr(executor, "ARTIFACT_ROOT", tmp_path / "artifacts")
            owner, payload = uid(), batch(["general"])
            await client.put(f"/browser/dispatch/{owner}", json=payload)
            ticket = manager.dispatch.tickets[payload["tickets"][0]["id"]]
            await until(lambda: ticket.state == "granted")
            lane = manager.get("general")
            lane.context.add_cookies = AsyncMock()
            job = {"run_id": 7, "job_id": 1, "attempt": 1, "lane_key": "general",
                   "cookies": [{"name": "private"}], "dispatch_reservation": {
                       "owner": owner, "id": ticket.id, "generation": manager.dispatch.generation,
                   }}
            if value is None:
                del job[field]
            else:
                job[field] = value
            with pytest.raises(RuntimeError, match=f"^{dispatch.INVALID_RESERVATION}$"):
                await executor._execute_admitted_job(manager, job)
            assert lane.leases == {ticket.pin} and not manager.dispatch.claims
            lane.context.add_cookies.assert_not_called()
            lane.context.new_page.assert_not_called()
    asyncio.run(run())


def test_job_claim_atomic_across_tickets_and_completed_replays(monkeypatch, tmp_path):
    async def run():
        async with worker(monkeypatch, tmp_path, 2) as (manager, client, _, _, _):
            capabilities = []
            for lane in KEYS[:2]:
                owner, payload = uid(), batch([lane])
                await client.put(f"/browser/dispatch/{owner}", json=payload)
                ticket = manager.dispatch.tickets[payload["tickets"][0]["id"]]
                await until(lambda: ticket.state == "granted")
                capabilities.append({"owner": owner, "id": ticket.id, "generation": manager.dispatch.generation})
            results = await asyncio.gather(*(
                manager.dispatch.lease_job(lane, 7, capability, job_id=11, attempt=1)
                for lane, capability in zip(KEYS, capabilities)
            ), return_exceptions=True)
            assert sum(isinstance(result, RuntimeError) for result in results) == 1
            for lane, result in zip(KEYS, results):
                if not isinstance(result, Exception):
                    await manager.release_lane(lane, result[0])
            for lane, capability in zip(KEYS, capabilities):
                with pytest.raises(RuntimeError, match=dispatch.INVALID_RESERVATION):
                    await manager.dispatch.lease_job(lane, 7, capability, job_id=11, attempt=1)
            for job_id, attempt in [(11, 2), (11, 3), (12, 1)]:
                lease, _ = await manager.dispatch.lease_job("general", 7, capabilities[0], job_id=job_id, attempt=attempt)
                await manager.release_lane("general", lease)
            assert manager.dispatch.claims == {(7, 11, 1), (7, 11, 2), (7, 11, 3), (7, 12, 1)}
            assert all(ticket.state == "granted" for ticket in manager.dispatch.tickets.values())
    asyncio.run(run())


def test_executor_completed_failure_cannot_replay_same_attempt(monkeypatch, tmp_path):
    async def run():
        async with worker(monkeypatch, tmp_path) as (manager, client, _, _, _):
            monkeypatch.setattr(executor, "ARTIFACT_ROOT", tmp_path / "artifacts")
            owner, payload = uid(), batch(["general"])
            await client.put(f"/browser/dispatch/{owner}", json=payload)
            ticket = manager.dispatch.tickets[payload["tickets"][0]["id"]]
            await until(lambda: ticket.state == "granted")
            lane = manager.get("general")
            lane.context.add_cookies = AsyncMock()
            lane.context.new_page = AsyncMock(side_effect=RuntimeError("setup failed"))
            job = {"run_id": 7, "job_id": 1, "attempt": 1, "cookies": [{"name": "private"}],
                   "dispatch_reservation": {"owner": owner, "id": ticket.id, "generation": manager.dispatch.generation}}
            assert (await executor._execute_admitted_job(manager, job))["reason"] == "browser_setup_failed"
            log_path = tmp_path / "artifacts" / "run-7" / "job-1" / "job.log"
            log_path.write_text("completed attempt evidence\n", encoding="utf-8")
            lane.context.add_cookies.reset_mock()
            lane.context.new_page.reset_mock()
            with pytest.raises(RuntimeError, match=dispatch.INVALID_RESERVATION):
                await executor._execute_admitted_job(manager, job)
            lane.context.add_cookies.assert_not_called()
            lane.context.new_page.assert_not_called()
            assert log_path.read_text(encoding="utf-8") == "completed attempt evidence\n"
            assert lane.leases == {ticket.pin}
    asyncio.run(run())


def test_claim_limit_is_fail_closed_and_retains_previous_claims(monkeypatch, tmp_path):
    async def run():
        async with worker(monkeypatch, tmp_path) as (manager, client, _, _, _):
            monkeypatch.setattr(dispatch, "MAX_CLAIMS", 1)
            owner, payload = uid(), batch(["general"])
            await client.put(f"/browser/dispatch/{owner}", json=payload)
            ticket = manager.dispatch.tickets[payload["tickets"][0]["id"]]
            await until(lambda: ticket.state == "granted")
            capability = {"owner": owner, "id": ticket.id, "generation": manager.dispatch.generation}
            job, _ = await manager.dispatch.lease_job("general", 7, capability, job_id=1, attempt=1)
            await manager.release_lane("general", job)
            for job_id, attempt in [(1, 1), (1, 2), (2, 1)]:
                with pytest.raises(RuntimeError, match=dispatch.INVALID_RESERVATION):
                    await manager.dispatch.lease_job("general", 7, capability, job_id=job_id, attempt=attempt)
            assert manager.dispatch.claims == {(7, 1, 1)} and manager.get("general").leases == {ticket.pin}
    asyncio.run(run())


def test_cancelled_job_cannot_replay_claim_with_fresh_ticket(monkeypatch, tmp_path):
    async def run():
        async with worker(monkeypatch, tmp_path) as (manager, client, _, _, _):
            owner, payload = uid(), batch(["general"])
            url = f"/browser/dispatch/{owner}"
            await client.put(url, json=payload)
            ticket = manager.dispatch.tickets[payload["tickets"][0]["id"]]
            await until(lambda: ticket.state == "granted")
            entered = asyncio.Event()

            async def job():
                lease, _ = await manager.dispatch.lease_job("general", 7, {
                    "owner": owner, "id": ticket.id, "generation": manager.dispatch.generation,
                }, job_id=1, attempt=1)
                try:
                    entered.set()
                    await asyncio.Event().wait()
                finally:
                    await manager.release_lane("general", lease)

            execution = asyncio.create_task(job())
            await entered.wait()
            execution.cancel()
            with pytest.raises(asyncio.CancelledError):
                await execution
            await client.delete(f"{url}/{ticket.id}")
            await until(lambda: ticket.pin is None)
            fresh = batch(["general"])
            await client.put(url, json=fresh)
            replacement = manager.dispatch.tickets[fresh["tickets"][0]["id"]]
            await until(lambda: replacement.state == "granted")
            capability = {"owner": owner, "id": replacement.id, "generation": manager.dispatch.generation}
            with pytest.raises(RuntimeError, match=dispatch.INVALID_RESERVATION):
                await manager.dispatch.lease_job("general", 7, capability, job_id=1, attempt=1)
            assert manager.get("general").leases == {replacement.pin}
            lease, _ = await manager.dispatch.lease_job("general", 7, capability, job_id=1, attempt=2)
            await manager.release_lane("general", lease)
    asyncio.run(run())


def test_replacement_registry_has_new_generation(monkeypatch, tmp_path):
    async def run():
        async with worker(monkeypatch, tmp_path) as (manager, client, _, _, _):
            owner, payload = uid(), batch(["general"])
            await client.put(f"/browser/dispatch/{owner}", json=payload)
            ticket = manager.dispatch.tickets[payload["tickets"][0]["id"]]
            await until(lambda: ticket.state == "granted")
            capability = {"owner": owner, "id": ticket.id, "generation": manager.dispatch.generation}
            await manager.dispatch.close()
            manager.dispatch = dispatch.DispatchRegistry(manager)
            assert manager.dispatch.generation != capability["generation"]
            await client.put(f"/browser/dispatch/{owner}", json=payload)
            await until(lambda: manager.dispatch.tickets[ticket.id].state == "granted")
            with pytest.raises(RuntimeError, match=dispatch.INVALID_RESERVATION):
                await manager.dispatch.lease_job("general", 7, capability, job_id=1, attempt=1)
            assert not manager.dispatch.claims
    asyncio.run(run())


@pytest.mark.parametrize("release_before_create", [False, True])
def test_old_ticket_put_never_revives_after_compaction(monkeypatch, tmp_path, release_before_create):
    async def run():
        async with worker(monkeypatch, tmp_path) as (manager, client, starts, _, _):
            owner, payload = uid(), batch(["general"])
            ticket_id = payload["tickets"][0]["id"]
            url = f"/browser/dispatch/{owner}"
            if not release_before_create:
                await client.put(url, json=payload)
                await until(lambda: manager.dispatch.tickets[ticket_id].state == "granted")
            await client.delete(f"{url}/{ticket_id}")
            ticket = manager.dispatch.tickets[ticket_id]
            await until(lambda: ticket.pin is None and (ticket.task is None or ticket.task.done()))
            ticket.retired -= 601
            manager.dispatch.expire()
            assert ticket_id in manager.dispatch.retired_tickets
            assert (await client.put(url, json=payload)).status_code == 409
            assert (await client.put(f"/browser/dispatch/{uid()}", json=payload)).status_code == 409
            assert starts == ([] if release_before_create else ["general"])
            # A different ticket on the still-open owner remains legitimate.
            assert (await client.put(url, json=batch(["general"]))).status_code == 200
    asyncio.run(run())


def test_permanent_fence_bound_reserves_space_and_does_not_evict(monkeypatch, tmp_path):
    async def run():
        async with worker(monkeypatch, tmp_path) as (manager, client, _, _, _):
            monkeypatch.setattr(dispatch, "MAX_RETIRED", 2)
            owner, payload = uid(), batch(["general"])
            url = f"/browser/dispatch/{owner}"
            await client.put(url, json=payload)
            ticket = manager.dispatch.tickets[payload["tickets"][0]["id"]]
            await until(lambda: ticket.state == "granted")
            assert (await client.put(url, json=batch(["interaction"]))).status_code == 503
            assert (await client.delete(f"/browser/dispatch/{uid()}")).status_code == 503
            await client.delete(url)
            await until(lambda: ticket.pin is None)
            ticket.retired -= 601
            manager.dispatch.owners[owner].retired -= 601
            manager.dispatch.expire()
            assert manager.dispatch.retired_owners == {owner} and manager.dispatch.retired_tickets == {ticket.id}
            assert (await client.put(url, json=payload)).status_code == 409
            assert (await client.put(f"/browser/dispatch/{uid()}", json=batch(["general"]))).status_code == 503
            assert (await client.delete(url)).status_code == 200
    asyncio.run(run())


@pytest.mark.parametrize("cancel_close", [False, True])
def test_close_bounded_under_admission_lock_retains_pin_and_job(monkeypatch, tmp_path, cancel_close):
    async def run():
        async with worker(monkeypatch, tmp_path) as (manager, client, _, _, _):
            monkeypatch.setattr(dispatch, "CLOSE_WAIT_SECONDS", 0.02)
            owner, payload = uid(), batch(["general"])
            await client.put(f"/browser/dispatch/{owner}", json=payload)
            ticket = manager.dispatch.tickets[payload["tickets"][0]["id"]]
            await until(lambda: ticket.state == "granted")
            capability = {"owner": owner, "id": ticket.id, "generation": manager.dispatch.generation}
            job, _ = await manager.dispatch.lease_job("general", 7, capability, job_id=1, attempt=1)
            pin = ticket.pin
            async with manager._admission_lock:
                closing = asyncio.create_task(manager.dispatch.close())
                await until(lambda: manager.dispatch.closed)
                if cancel_close:
                    closing.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await closing
                else:
                    await asyncio.wait_for(closing, 0.2)
                assert ticket.state == "released" and ticket.pin == pin
                assert manager.get("general").leases == {pin, job}
                assert manager.dispatch.cleanup_tasks and not manager.dispatch.close_task.done()
            await manager.dispatch.close()
            assert ticket.pin is None and manager.get("general").leases == {job}
            assert manager.dispatch.close_task.done() and not manager.dispatch.cleanup_tasks
            await manager.release_lane("general", job)
    asyncio.run(run())


def test_manager_teardown_proceeds_with_cancel_resistant_dispatch_acquisition(monkeypatch, tmp_path):
    async def run():
        async with worker(monkeypatch, tmp_path) as (manager, client, _, _, _):
            monkeypatch.setattr(dispatch, "CLOSE_WAIT_SECONDS", 0.02)
            entered, finish = asyncio.Event(), asyncio.Event()
            lease_lane = manager.lease_lane

            async def stubborn_lease(key):
                lease = await lease_lane(key)
                entered.set()
                while not finish.is_set():
                    try:
                        await finish.wait()
                    except asyncio.CancelledError:
                        pass
                return lease

            monkeypatch.setattr(manager, "lease_lane", stubborn_lease)
            owner, payload = uid(), batch(["general"])
            await client.put(f"/browser/dispatch/{owner}", json=payload)
            await entered.wait()
            ticket = manager.dispatch.tickets[payload["tickets"][0]["id"]]
            try:
                await asyncio.wait_for(manager.stop(), 0.5)
                assert manager.dispatch.closed and not ticket.task.done()
                assert not manager.dispatch.close_task.done()
                assert manager.get("general").session is None
            finally:
                finish.set()
                await until(lambda: ticket.task.done())
                await manager.dispatch.close()
            assert manager.dispatch.close_task.done() and ticket.pin is None
            assert not manager.dispatch.cleanup_tasks and not manager.get("general").leases
    asyncio.run(run())


@pytest.mark.parametrize("original_path", ["/execute", "/execute/stream"])
@pytest.mark.parametrize("duplicate_path", ["/execute", "/execute/stream"])
@pytest.mark.parametrize("duplicate_attempt", [1, 2])
def test_endpoint_concurrent_replay_preserves_original_cancel(
    monkeypatch, tmp_path, original_path, duplicate_path, duplicate_attempt,
):
    async def run():
        async with worker(monkeypatch, tmp_path) as (manager, client, _, _, _):
            monkeypatch.setattr(executor, "ARTIFACT_ROOT", tmp_path / "artifacts")
            owner, payload = uid(), batch(["general"])
            await client.put(f"/browser/dispatch/{owner}", json=payload)
            ticket = manager.dispatch.tickets[payload["tickets"][0]["id"]]
            await until(lambda: ticket.state == "granted")
            lane = manager.get("general")
            entered = asyncio.Event()

            async def cookies(_rows):
                entered.set()
                await asyncio.Event().wait()

            lane.context.add_cookies = AsyncMock(side_effect=cookies)
            job = {"run_id": 7, "job_id": 1, "attempt": 1, "lane_key": "general",
                   "cookies": [{"name": "private"}], "dispatch_reservation": {
                       "owner": owner, "id": ticket.id, "generation": manager.dispatch.generation,
                   }}
            original = asyncio.create_task(client.post(original_path, json=job))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                cancel = worker_app.ACTIVE_EXECUTIONS["1"]
                leases = set(lane.leases)
                artifact = tmp_path / "artifacts" / "run-7" / "job-1" / "job.log"
                evidence = artifact.read_bytes()
                duplicate = await asyncio.wait_for(client.post(
                    duplicate_path, json={**job, "attempt": duplicate_attempt},
                ), 0.2)
                assert duplicate.status_code == 409
                assert duplicate.json() == {"detail": "Job already executing"}
                assert worker_app.ACTIVE_EXECUTIONS["1"] is cancel and not cancel.is_set()
                assert not original.done() and lane.leases == leases
                assert manager.dispatch.claims == {(7, 1, 1)}
                assert artifact.read_bytes() == evidence
                lane.context.add_cookies.assert_awaited_once()
                lane.context.new_page.assert_not_called()
                response = await client.post("/executions/1/cancel")
                assert response.json() == {"canceled": True} and cancel.is_set()
                response = await asyncio.wait_for(original, 1)
                if original_path == "/execute/stream":
                    rows = [json.loads(line) for line in response.text.splitlines()]
                    assert rows[-1]["result"]["status"] == "canceled"
                else:
                    assert response.json()["status"] == "canceled"
                assert "1" not in worker_app.ACTIVE_EXECUTIONS
                await until(lambda: lane.leases == {ticket.pin})
            finally:
                if not original.done():
                    worker_app.ACTIVE_EXECUTIONS["1"].set()
                    await original
    asyncio.run(run())


@pytest.mark.parametrize("path", ["/execute", "/execute/stream"])
def test_endpoint_cleanup_only_removes_own_cancel_event(monkeypatch, tmp_path, path):
    async def run():
        async with worker(monkeypatch, tmp_path) as (_, client, _, _, _):
            replacement = asyncio.Event()

            async def execute(*_args, **_kwargs):
                worker_app.ACTIVE_EXECUTIONS["1"] = replacement
                return {"status": "success", "steps": []}

            monkeypatch.setattr(worker_app, "execute_job", execute)
            try:
                response = await client.post(path, json={"job_id": 1})
                assert response.status_code == 200
                assert worker_app.ACTIVE_EXECUTIONS["1"] is replacement
            finally:
                if worker_app.ACTIVE_EXECUTIONS.get("1") is replacement:
                    worker_app.ACTIVE_EXECUTIONS.pop("1")
    asyncio.run(run())


@pytest.mark.parametrize("uncertain_release", [False, True])
@pytest.mark.parametrize("reserved", [False, True])
def test_artifact_initialization_failure_releases_or_retains_owned_lease(
    monkeypatch, tmp_path, uncertain_release, reserved,
):
    from worker import page_lifecycle

    async def run():
        async with worker(monkeypatch, tmp_path) as (manager, client, _, _, _):
            monkeypatch.setattr(page_lifecycle, "LEASE_RELEASE_TIMEOUT_SECONDS", 0.01)
            monkeypatch.setattr(page_lifecycle, "CLEANUP_RETRY_SECONDS", 0.01)
            owner, payload = uid(), batch(["general"])
            await client.put(f"/browser/dispatch/{owner}", json=payload)
            ticket = manager.dispatch.tickets[payload["tickets"][0]["id"]]
            await until(lambda: ticket.state == "granted")
            lane = manager.get("general")
            lane.context.add_cookies = AsyncMock()
            job = {"run_id": 7, "job_id": 1, "attempt": 1, "lane_key": "general",
                   "cookies": [{"name": "private"}]}
            if reserved:
                job["dispatch_reservation"] = {"owner": owner, "id": ticket.id, "generation": manager.dispatch.generation}

            def artifacts(_payload):
                assert len(lane.leases) == 2
                raise RuntimeError("artifact initialization failed")

            monkeypatch.setattr(executor, "JobArtifacts", artifacts)
            finish = asyncio.Event()
            original_release = manager.release_lane

            async def release(key, lease_id):
                await finish.wait()
                return await original_release(key, lease_id)

            if uncertain_release:
                monkeypatch.setattr(manager, "release_lane", release)
            try:
                with pytest.raises(RuntimeError, match="^artifact initialization failed$"):
                    await executor._execute_admitted_job(manager, job)
                lane.context.add_cookies.assert_not_called()
                lane.context.new_page.assert_not_called()
                if uncertain_release:
                    assert len(lane.leases) == 2 and ticket.pin in lane.leases
                    cleanup_owner, = manager._job_cleanup_owners
                    assert not cleanup_owner._released
                    assert cleanup_owner._release_task is not None
                    assert not cleanup_owner._release_task.done()
                else:
                    assert lane.leases == {ticket.pin}
                if reserved:
                    assert manager.dispatch.claims == {(7, 1, 1)}
            finally:
                finish.set()
                await until(lambda: lane.leases == {ticket.pin})
                await until(lambda: not manager._job_cleanup_owners)
    asyncio.run(run())
