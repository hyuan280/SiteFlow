from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from tests.test_assistant_browser import Manager, Page, TOKEN, URL, public_dns  # noqa: F401
from worker import app as worker_app
from worker import assistant_browser as module
from worker.assistant_browser import AssistantBrowser


async def until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0.001)


async def queued(browser, lane="interaction", sid=None):
    return await browser.open(URL, lane, TOKEN, sid, wait_for_lane=True)


def test_human_blocks_queue_then_admission_starts_fresh_ttl(public_dns):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        human = await browser.open(URL, "interaction", TOKEN)
        started = time.monotonic()
        view = await queued(browser)
        assert time.monotonic() - started < 0.1
        sid = view["session_id"]
        session = browser.sessions[sid]
        assert set(view) == {"session_id", "lane_key", "status", "expires_at"}
        assert view["status"] == "queued"
        assert 1199 < session.deadline - time.monotonic() <= 1200
        assert 1199 < datetime.fromisoformat(view["expires_at"]).timestamp() - time.time() <= 1200
        await until(lambda: manager.lease_lane.await_count == 2)
        await asyncio.sleep(0.01)
        assert manager.context.new_page.await_count == 1
        assert manager.lane.execution_semaphore.active == 1
        assert (await browser.open_status(sid, TOKEN))["status"] == "queued"
        human_page = manager.page
        manager.page = Page()
        manager.context.new_page.return_value = manager.page
        released = time.monotonic()
        await browser.close(human["session_id"], TOKEN)
        await until(lambda: session.ready)
        ready = await browser.open_status(sid, TOKEN)
        assert ready["status"] == "ready" and ready["url"] == URL
        assert session.admitted_at >= released
        assert session.open_deadline == session.admitted_at + 110
        assert 899 < session.deadline - time.monotonic() <= 900
        assert 899 < datetime.fromisoformat(ready["expires_at"]).timestamp() - time.time() <= 900
        human_page.close.assert_awaited_once()
        manager.home.close.assert_not_awaited()
        manager.other.close.assert_not_awaited()
        await browser.stop()
        assert manager.lane.execution_semaphore.active == 0
    asyncio.run(run())


def test_fifo_even_when_first_lease_is_delayed_and_head_cancelled(public_dns):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        entered, finish = asyncio.Event(), asyncio.Event()
        calls = 0

        async def lease(key):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await finish.wait()
            return str(calls), {}

        manager.lease_lane.side_effect = lease
        first, second, third = [await queued(browser) for _ in range(3)]
        await entered.wait()
        await asyncio.sleep(0.01)
        assert calls == 1
        assert manager.lane.execution_semaphore.active == 0
        closing = asyncio.create_task(browser.close(first["session_id"], TOKEN))
        await until(lambda: browser.sessions[first["session_id"]].closed)
        finish.set()
        await closing
        await until(lambda: browser.sessions[second["session_id"]].ready)
        assert (await browser.open_status(third["session_id"], TOKEN))["status"] == "queued"
        assert manager.context.new_page.await_count == 1
        await browser.close(second["session_id"], TOKEN)
        await until(lambda: browser.sessions[third["session_id"]].ready)
        assert manager.context.new_page.await_count == 2
        await browser.stop()
        assert not browser._queues
        assert manager.lane.execution_semaphore.active == 0
        assert manager.release_lane.await_count == 3
    asyncio.run(run())


def test_fifo_admits_a_before_b_on_broadcast_limiter(public_dns):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        limiter = manager.lane.execution_semaphore
        await limiter.__aenter__()
        first, second = await queued(browser), await queued(browser)
        await asyncio.sleep(0.01)
        manager.context.new_page.assert_not_awaited()
        await limiter.__aexit__(None, None, None)
        await until(lambda: browser.sessions[first["session_id"]].ready)
        assert (await browser.open_status(second["session_id"], TOKEN))["status"] == "queued"
        await browser.close(first["session_id"], TOKEN)
        await until(lambda: browser.sessions[second["session_id"]].ready)
        await browser.stop()
        assert limiter.active == 0
    asyncio.run(run())


def test_proxy_queue_is_independent(public_dns):
    async def run():
        manager, proxy = Manager(), Manager()
        manager.get = {"interaction": manager.lane, "proxy_interaction": proxy.lane}.__getitem__
        browser = AssistantBrowser(manager)
        async with manager.lane.execution_semaphore:
            first = await queued(browser)
            second = await queued(browser, "proxy_interaction")
            await until(lambda: browser.sessions[second["session_id"]].ready)
            assert (await browser.open_status(first["session_id"], TOKEN))["status"] == "queued"
            manager.context.new_page.assert_not_awaited()
            proxy.context.new_page.assert_awaited_once()
            await browser.close(first["session_id"], TOKEN)
        await browser.stop()
        assert proxy.lane.execution_semaphore.active == 0
    asyncio.run(run())


@pytest.mark.parametrize("owner", ["job", "picker"])
def test_real_job_and_picker_limiter_blocks_queued_open(public_dns, owner):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        limiter = manager.lane.execution_semaphore
        if owner == "picker":
            picker = await worker_app._open_picker(manager, URL)
        else:
            await limiter.__aenter__()
        pages = manager.context.new_page.await_count
        view = await queued(browser)
        await until(lambda: manager.lease_lane.await_count == (2 if owner == "picker" else 1))
        await asyncio.sleep(0.01)
        assert manager.context.new_page.await_count == pages
        assert limiter.active == 1
        if owner == "picker":
            await worker_app._close_picker(manager, picker["session_id"])
        else:
            await limiter.__aexit__(None, None, None)
        await until(lambda: browser.sessions[view["session_id"]].ready)
        await browser.stop()
        assert limiter.active == 0
    asyncio.run(run())


def test_cancel_waiter_never_closes_human_and_remembers_id(public_dns):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        async with manager.lane.execution_semaphore:
            view = await queued(browser)
            sid = view["session_id"]
            await until(lambda: manager.lease_lane.await_count == 1)
            await browser.close(sid, TOKEN)
            assert await browser.close(sid, TOKEN) == {"closed": True}
            assert manager.lane.execution_semaphore.active == 1
            assert not browser.sessions and not browser._queues
            manager.release_lane.assert_awaited_once()
            manager.context.new_page.assert_not_awaited()
            manager.home.close.assert_not_awaited()
            manager.other.close.assert_not_awaited()
            for action in (lambda: queued(browser, sid=sid), lambda: browser.open_status(sid, TOKEN)):
                with pytest.raises(HTTPException) as error:
                    await action()
                assert error.value.status_code == 410
        await asyncio.sleep(0.01)
        manager.context.new_page.assert_not_awaited()
    asyncio.run(run())


def test_close_before_initializer_runs_removes_ticket(public_dns):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        view = await queued(browser)
        await browser.close(view["session_id"], TOKEN)
        assert not browser.sessions and not browser._queues
        manager.context.new_page.assert_not_awaited()
        next_view = await queued(browser)
        await until(lambda: browser.sessions[next_view["session_id"]].ready)
        await browser.stop()
        assert manager.lane.execution_semaphore.active == 0
    asyncio.run(run())


def test_viewer_only_and_new_viewer_during_wait_block_admission(public_dns):
    async def run():
        manager = Manager()
        manager.lane.viewers = {"human-vnc"}
        browser = AssistantBrowser(manager)
        view = await queued(browser)
        await until(lambda: manager.lease_lane.await_count == 1)
        await asyncio.sleep(0.01)
        assert manager.lane.execution_semaphore.active == 0
        manager.context.new_page.assert_not_awaited()
        # A viewer arriving while the semaphore is busy must also be noticed.
        await manager.lane.execution_semaphore.__aenter__()
        manager.lane.viewers.clear()
        await asyncio.sleep(0.21)
        manager.lane.viewers.add("new-viewer")
        await manager.lane.execution_semaphore.__aexit__(None, None, None)
        await asyncio.sleep(0.01)
        manager.context.new_page.assert_not_awaited()
        assert manager.lane.execution_semaphore.active == 0
        manager.lane.viewers.clear()
        await until(lambda: browser.sessions[view["session_id"]].ready)
        manager.lane.viewers.add("later-viewer")
        assert (await browser.open_status(view["session_id"], TOKEN))["status"] == "ready"
        await browser.stop()
        assert manager.lane.viewers == {"later-viewer"}
        manager.home.close.assert_not_awaited()
    asyncio.run(run())


def test_stopped_lane_is_started_by_nonexclusive_lease(public_dns):
    async def run():
        manager = Manager()
        manager.lane.context = None

        async def lease(key):
            manager.lane.context = manager.context
            return "started", {}

        manager.lease_lane.side_effect = lease
        browser = AssistantBrowser(manager)
        view = await queued(browser)
        await until(lambda: browser.sessions[view["session_id"]].ready)
        manager.context.new_page.assert_awaited_once()
        await browser.stop()
        manager.release_lane.assert_awaited_once_with("interaction", "started")
    asyncio.run(run())


@pytest.mark.parametrize("blocked", ["limiter", "viewer", "dns", "lease", "ticket"])
def test_queue_timeout_is_static_and_cleans_without_poll(public_dns, monkeypatch, blocked):
    monkeypatch.setattr(module, "QUEUE_TIMEOUT", 0.03)

    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        finish = asyncio.Event()
        if blocked in {"limiter", "ticket"}:
            await manager.lane.execution_semaphore.__aenter__()
        elif blocked == "viewer":
            manager.lane.viewers = {"vnc"}
        elif blocked == "dns":
            async def dns(*args, **kwargs):
                await finish.wait()
                return []

            public_dns.side_effect = dns
        elif blocked == "lease":
            async def lease(key):
                await finish.wait()
                return "late-lease", {}

            manager.lease_lane.side_effect = lease
        if blocked == "ticket":
            await queued(browser)
        view = await queued(browser)
        sid = view["session_id"]
        session = browser.sessions[sid]
        await until(lambda: session.opening.done())
        # Background cleanup runs independently of polling or an HTTP connection.
        finish.set()
        await until(lambda: sid not in browser.sessions)
        with pytest.raises(HTTPException) as error:
            await browser.open_status(sid, TOKEN)
        assert error.value.status_code == 504
        assert error.value.detail == {"code": "browser_queue_timeout"}
        with pytest.raises(HTTPException) as error:
            await browser.open_status(sid, "wrong")
        assert error.value.status_code == 404
        manager.context.new_page.assert_not_awaited()
        if blocked in {"limiter", "ticket"}:
            assert manager.lane.execution_semaphore.active == 1
            await manager.lane.execution_semaphore.__aexit__(None, None, None)
        await browser.stop()
        assert not browser._queues
        assert manager.lane.execution_semaphore.active == 0
        manager.home.close.assert_not_awaited()
    asyncio.run(run())


def test_opening_status_is_fast_no_url_and_survives_cancelled_poll(public_dns):
    async def run():
        manager = Manager()
        entered, finish = asyncio.Event(), asyncio.Event()

        async def hang():
            entered.set()
            await finish.wait()

        manager.page.bring_to_front.side_effect = hang
        browser = AssistantBrowser(manager)
        view = await queued(browser)
        sid = view["session_id"]
        await entered.wait()
        poll = asyncio.create_task(browser.open_status(sid, TOKEN))
        poll.cancel()
        await asyncio.gather(poll, return_exceptions=True)
        started = time.monotonic()
        pending = await browser.open_status(sid, TOKEN)
        assert time.monotonic() - started < 0.1
        assert pending["status"] == "opening" and "url" not in pending
        assert not browser.sessions[sid].opening.done()
        finish.set()
        await until(lambda: browser.sessions[sid].ready)
        await browser.stop()
    asyncio.run(run())


@pytest.mark.parametrize("failure,code,status", [
    ("navigation", "browser_navigation_failed", 502),
    ("policy", "browser_trust_required", 403),
    ("unexpected", "browser_lane_unavailable", 502),
    ("timeout", "browser_open_timeout", 504),
])
def test_initialization_errors_are_retrieved_static_and_cleaned(public_dns, monkeypatch, failure, code, status):
    monkeypatch.setattr(module, "OPEN_TIMEOUT", 0.03)

    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        if failure == "navigation":
            manager.page.goto = AsyncMock(side_effect=RuntimeError("password-secret"))
        elif failure == "policy":
            manager.page.goto = AsyncMock(side_effect=RuntimeError("password-secret"))
            # Even structured trust errors must not expose arbitrary exception fields.
            original = browser._provision

            async def provision(session):
                session.failure = {"code": "browser_trust_required", "target_url": "password-secret"}
                await original(session)

            monkeypatch.setattr(browser, "_provision", provision)
        elif failure == "unexpected":
            manager.page.bring_to_front.side_effect = RuntimeError("password-secret")
        else:
            manager.page.bring_to_front.side_effect = asyncio.Event().wait
        view = await queued(browser)
        sid = view["session_id"]
        session = browser.sessions[sid]
        await until(lambda: sid not in browser.sessions)
        assert session.opening.done()
        with pytest.raises(HTTPException) as error:
            await browser.open_status(sid, TOKEN)
        assert error.value.status_code == status
        assert error.value.detail == {"code": code}
        assert "secret" not in str(error.value)
        assert manager.lane.execution_semaphore.active == 0
        manager.page.close.assert_awaited_once()
        manager.release_lane.assert_awaited_once()
        await browser.close(sid, TOKEN)
    asyncio.run(run())


def test_close_blocked_page_creation_owns_late_page_and_releases_once(public_dns):
    async def run():
        manager = Manager()
        entered, finish = asyncio.Event(), asyncio.Event()

        async def create():
            entered.set()
            await finish.wait()
            return manager.page

        manager.context.new_page.side_effect = create
        browser = AssistantBrowser(manager)
        view = await queued(browser)
        sid = view["session_id"]
        await entered.wait()
        closing = asyncio.create_task(browser.close(sid, TOKEN))
        await until(lambda: browser.sessions[sid].closed)
        finish.set()
        await closing
        assert not browser.sessions
        manager.page.close.assert_awaited_once()
        manager.page.bring_to_front.assert_not_awaited()
        manager.release_lane.assert_awaited_once()
        assert manager.lane.execution_semaphore.active == 0
        with pytest.raises(HTTPException) as error:
            await queued(browser, sid=sid)
        assert error.value.status_code == 410
    asyncio.run(run())


def test_open_timeout_does_not_wait_for_late_page_cleanup(public_dns, monkeypatch):
    monkeypatch.setattr(module, "OPEN_TIMEOUT", 0.03)

    async def run():
        manager = Manager()
        entered, finish = asyncio.Event(), asyncio.Event()

        async def create():
            entered.set()
            await finish.wait()
            return manager.page

        manager.context.new_page.side_effect = create
        browser = AssistantBrowser(manager)
        view = await queued(browser)
        sid = view["session_id"]
        session = browser.sessions[sid]
        await entered.wait()
        await until(lambda: session.closed)
        started = time.monotonic()
        with pytest.raises(HTTPException) as error:
            await browser.open_status(sid, TOKEN)
        assert time.monotonic() - started < 0.1
        assert error.value.detail == {"code": "browser_open_timeout"}
        assert manager.lane.execution_semaphore.active == 1
        manager.release_lane.assert_not_awaited()
        finish.set()
        await until(lambda: sid not in browser.sessions)
        manager.page.close.assert_awaited_once()
        manager.page.bring_to_front.assert_not_awaited()
        manager.release_lane.assert_awaited_once()
        assert manager.lane.execution_semaphore.active == 0
        await browser.close(sid, TOKEN)
        manager.release_lane.assert_awaited_once()
    asyncio.run(run())


def test_quarantine_keeps_slot_until_owned_target_closes(public_dns):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        view = await queued(browser)
        sid = view["session_id"]
        await until(lambda: browser.sessions[sid].ready)
        session = browser.sessions[sid]
        manager.page.close.side_effect = RuntimeError("private")
        original = manager.cdp.send.side_effect

        async def send(method, params=None):
            if method == "Target.closeTarget":
                return {"success": False}
            return await original(method, params)

        manager.cdp.send.side_effect = send
        with pytest.raises(HTTPException) as error:
            await browser.close(sid, TOKEN)
        assert error.value.status_code == 503
        assert session.quarantined and session.closed
        next_view = await queued(browser)
        await asyncio.sleep(0.01)
        assert manager.context.new_page.await_count == 1
        assert manager.lane.execution_semaphore.active == 1
        manager.page.close.side_effect = None
        await browser._dispose(session)
        await until(lambda: browser.sessions[next_view["session_id"]].ready)
        await browser.stop()
        assert manager.lane.execution_semaphore.active == 0
        manager.home.close.assert_not_awaited()
    asyncio.run(run())


def test_idempotency_freezes_wait_policy_and_normal_contract(public_dns, monkeypatch):
    monkeypatch.setattr(module, "ACQUIRE_TIMEOUT", 0.01)

    async def run():
        manager = Manager()
        manager.lane.viewers = {"vnc"}
        browser = AssistantBrowser(manager)
        # Normal explicit human opens still ignore viewers and have no status field.
        normal = await browser.open(URL, "interaction", TOKEN)
        assert "status" not in normal
        sid = normal["session_id"]
        with pytest.raises(HTTPException) as error:
            await queued(browser, sid=sid)
        assert error.value.status_code == 409
        with pytest.raises(HTTPException) as error:
            await browser.open(URL, "interaction", TOKEN)
        assert error.value.detail == {"code": "browser_lane_busy"}
        view = await queued(browser)
        queued_id = view["session_id"]
        assert await queued(browser, sid=queued_id) == view
        with pytest.raises(HTTPException) as error:
            await browser.open(URL, "interaction", TOKEN, queued_id)
        assert error.value.status_code == 409
        await browser.close(sid, TOKEN)
        manager.lane.viewers.clear()
        await until(lambda: browser.sessions[queued_id].ready)
        ready = await queued(browser, sid=queued_id)
        assert ready["status"] == "ready" and ready["url"] == URL
        await browser.stop()
    asyncio.run(run())


def test_reaper_backstop_for_expired_queue_and_tombstone_pruning(public_dns, monkeypatch):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        async with manager.lane.execution_semaphore:
            view = await queued(browser)
            sid = view["session_id"]
            browser.sessions[sid].deadline = 0
            real_sleep = asyncio.sleep

            async def fast_sleep(seconds):
                await real_sleep(0.001)

            monkeypatch.setattr(module.asyncio, "sleep", fast_sleep)
            await browser.start()
            await until(lambda: sid not in browser.sessions)
            with pytest.raises(HTTPException) as error:
                await browser.open_status(sid, TOKEN)
            assert error.value.detail == {"code": "browser_queue_timeout"}
            assert manager.lane.execution_semaphore.active == 1
            digest, _ = browser.tombstones[sid]
            browser.tombstones[sid] = digest, 0
            browser._prune_tombstones()
            assert sid not in browser._open_errors
        await browser.stop()
    asyncio.run(run())


def test_status_endpoint_token_validation_and_body_cap(public_dns):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        app = worker_app.create_app()
        app.state.assistant_browser = browser
        sid = str(uuid.uuid4())
        async with manager.lane.execution_semaphore:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
                payload = {"url": URL, "lane_key": "interaction", "token": TOKEN,
                           "session_id": sid, "same_origin_only": True, "wait_for_lane": True}
                response = await client.post("/assistant-browser/open", json=payload)
                assert response.status_code == 200 and response.json()["status"] == "queued"
                endpoint = f"/assistant-browser/{sid}/open-status"
                response = await client.post(endpoint, json={"token": TOKEN})
                assert response.status_code == 200 and response.json()["status"] == "queued"
                assert "url" not in response.json() and TOKEN not in response.text
                assert (await client.post(endpoint, json={"token": "wrong"})).status_code == 404
                for bad in [{}, {"token": TOKEN, "url": "private-secret"}, {"token": {"secret": TOKEN}}]:
                    response = await client.post(endpoint, json=bad)
                    assert response.status_code == 422
                    assert response.json() == {"detail": "Invalid browser request"}
                for bad in [0, 1, "true", "false", None, [], {}]:
                    response = await client.post("/assistant-browser/open", json={**payload, "wait_for_lane": bad})
                    assert response.status_code == 422 and TOKEN not in response.text

                async def chunks():
                    yield json.dumps({"token": TOKEN, "padding": "x" * 5000}).encode()

                response = await client.post(endpoint, content=chunks(), headers={"content-type": "application/json"})
                assert response.status_code == 413 and TOKEN not in response.text
                response = await client.post(endpoint, content=b"{private-secret", headers={"content-type": "application/json"})
                assert response.status_code == 422 and "secret" not in response.text
                await client.post(f"/assistant-browser/{sid}/close", json={"token": TOKEN})
        await browser.stop()
    asyncio.run(run())
