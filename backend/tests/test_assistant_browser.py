from __future__ import annotations

import asyncio
import json
import socket
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from worker import assistant_browser as module
from worker.app import LaneExecutionLimiter, create_app
from worker.assistant_browser import AssistantBrowser, PublicDNS, SNAPSHOT_SCRIPT, _snapshot, _url

TOKEN = "random-host-secret-not-a-model-credential"
URL = "https://example.com/start"


class Page:
    def __init__(self):
        self.url = "about:blank"
        self.main_frame = object()
        self.listeners = {}
        self.route = AsyncMock()
        self.route_web_socket = AsyncMock()
        self.bring_to_front = AsyncMock()
        self.close = AsyncMock()
        self.evaluate = AsyncMock(return_value={"elements": []})

    def on(self, event, callback):
        self.listeners[event] = callback

    async def goto(self, url, **kwargs):
        self.url = url


class Manager:
    def __init__(self):
        self.home, self.other, self.page = Page(), Page(), Page()
        self.cdp = SimpleNamespace(on=lambda *args: None, send=AsyncMock(), detach=AsyncMock())

        async def send(method, params=None):
            if method == "Page.getFrameTree":
                return {"frameTree": {"frame": {"id": "main", "url": self.page.url, "loaderId": "loader"}}}
            if method == "Page.createIsolatedWorld":
                return {"executionContextId": 42}
            if method == "Runtime.callFunctionOn":
                return {"result": {"value": {"elements": []}}}
            if method == "Target.getTargetInfo":
                return {"targetInfo": {"targetId": "owned-target"}}
            if method == "Target.closeTarget":
                return {"success": True}
            return {}

        self.cdp.send.side_effect = send
        self.context = SimpleNamespace(
            pages=[self.home, self.other], new_page=AsyncMock(return_value=self.page),
            new_cdp_session=AsyncMock(return_value=self.cdp),
        )
        self.lane = SimpleNamespace(context=self.context, execution_semaphore=LaneExecutionLimiter(1))
        self.get = {"interaction": self.lane, "proxy_interaction": self.lane}.__getitem__
        self.lease_lane = AsyncMock(return_value=("owned-lease", {}))
        self.release_lane = AsyncMock()


@pytest.fixture
def public_dns(monkeypatch):
    lookup = AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))])
    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", lookup)
    return lookup


def test_owned_page_proxy_lane_and_close(public_dns):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        view = await browser.open(URL, "proxy_interaction", TOKEN)
        assert str(uuid.UUID(view["session_id"])) == view["session_id"]
        assert view["lane_key"] == "proxy_interaction"
        assert "token" not in json.dumps(view)
        manager.lease_lane.assert_awaited_once_with("proxy_interaction")
        assert manager.lane.execution_semaphore.active == 1
        manager.context.new_page.assert_awaited_once()
        manager.page.bring_to_front.assert_awaited_once()
        manager.page.route.assert_awaited_once()
        manager.page.route_web_socket.assert_awaited_once()
        popup = Page()
        manager.page.listeners["popup"](popup)
        await browser.close(view["session_id"], TOKEN)
        popup.close.assert_awaited_once()
        manager.page.close.assert_awaited_once()
        manager.home.close.assert_not_awaited()
        manager.other.close.assert_not_awaited()
        manager.release_lane.assert_awaited_once_with("proxy_interaction", "owned-lease")
        assert manager.lane.execution_semaphore.active == 0
        assert not browser.sessions
    asyncio.run(run())


@pytest.mark.parametrize("lane_key", ["interaction", "proxy_interaction"])
def test_assistant_uses_exact_lane_not_stopped_general(public_dns, lane_key):
    async def run():
        manager = Manager()
        selected = manager.lane
        lanes = {"general": SimpleNamespace(context=None), lane_key: selected}
        looked_up = []

        def get(key):
            looked_up.append(key)
            assert key == lane_key
            lane = lanes[key]
            if lane.context is None:
                raise RuntimeError("general lane stopped")
            return lane

        manager.get = get
        browser = AssistantBrowser(manager)
        view = await browser.open(URL, lane_key, TOKEN)
        assert looked_up == [lane_key]
        assert browser.sessions[view["session_id"]].lane is selected
        manager.lease_lane.assert_awaited_once_with(lane_key)
        await browser.close(view["session_id"], TOKEN)
        manager.release_lane.assert_awaited_once_with(lane_key, "owned-lease")
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["observe", "navigate", "close"])
def test_all_actions_require_matching_token(public_dns, operation):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        view = await browser.open(URL, "interaction", TOKEN)
        args = (URL,) if operation == "navigate" else ()
        for session_id, token in [(view["session_id"], "wrong"), ("not-a-session", TOKEN)]:
            with pytest.raises(HTTPException) as error:
                await getattr(browser, operation)(session_id, token, *args)
            assert error.value.status_code == 404
        assert manager.lane.execution_semaphore.active == 1
        manager.page.evaluate.assert_not_awaited()
        manager.page.close.assert_not_awaited()
        await browser.stop()
    asyncio.run(run())


def test_busy_lane_fails_409_and_releases_lease(public_dns, monkeypatch):
    monkeypatch.setattr(module, "ACQUIRE_TIMEOUT", 0.01)

    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        async with manager.lane.execution_semaphore:
            with pytest.raises(HTTPException) as error:
                await browser.open(URL, "interaction", TOKEN)
            assert error.value.status_code == 409
            assert error.value.detail == {"code": "browser_lane_busy"}
            assert manager.lane.execution_semaphore.active == 1
        manager.release_lane.assert_awaited_once()
        manager.context.new_page.assert_not_awaited()
    asyncio.run(run())


def test_proxy_unavailable_never_falls_back_or_leaks_error(public_dns):
    async def run():
        manager = Manager()
        manager.lease_lane.side_effect = RuntimeError("proxy-password-secret")
        browser = AssistantBrowser(manager)
        with pytest.raises(HTTPException) as error:
            await browser.open(URL, "proxy_interaction", TOKEN)
        assert error.value.status_code == 409
        assert error.value.detail == {"code": "browser_lane_unavailable"}
        assert "secret" not in str(error.value)
        manager.lease_lane.assert_awaited_once_with("proxy_interaction")
        manager.context.new_page.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("failure", [RuntimeError("secret-url?password=secret"), asyncio.CancelledError()])
def test_open_baseexception_cleanup(public_dns, failure):
    async def run():
        manager = Manager()
        manager.page.bring_to_front.side_effect = failure
        browser = AssistantBrowser(manager)
        with pytest.raises((HTTPException, asyncio.CancelledError)) as error:
            await browser.open(URL, "interaction", TOKEN)
        assert "password" not in str(error.value)
        manager.page.close.assert_awaited_once()
        manager.release_lane.assert_awaited_once()
        assert manager.lane.execution_semaphore.active == 0
        assert not browser.sessions
    asyncio.run(run())


def test_stop_cancels_open_and_cleans_even_on_close_release_errors(public_dns):
    async def run():
        manager = Manager()
        entered = asyncio.Event()

        async def hang():
            entered.set()
            await asyncio.Event().wait()

        manager.page.bring_to_front.side_effect = hang
        manager.page.close.side_effect = RuntimeError("secret")
        manager.release_lane.side_effect = RuntimeError("secret")
        browser = AssistantBrowser(manager)
        await browser.start()
        opening = asyncio.create_task(browser.open(URL, "interaction", TOKEN))
        await entered.wait()
        await browser.stop()
        await asyncio.gather(opening, return_exceptions=True)
        assert opening.cancelled()
        assert manager.lane.execution_semaphore.active == 1
        assert next(iter(browser.sessions.values())).quarantined
        manager.release_lane.assert_awaited_once()
        assert browser._reaper is None
        manager.release_lane.side_effect = None
        manager.page.close.side_effect = None
        await browser.stop()
        assert manager.lane.execution_semaphore.active == 0
    asyncio.run(run())


def test_cancel_close_finishes_cleanup_and_release(public_dns):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        view = await browser.open(URL, "interaction", TOKEN)
        entered, finish = asyncio.Event(), asyncio.Event()

        async def slow_close():
            entered.set()
            await finish.wait()

        manager.page.close.side_effect = slow_close
        closing = asyncio.create_task(browser.close(view["session_id"], TOKEN))
        await entered.wait()
        closing.cancel()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert manager.lane.execution_semaphore.active == 0
        manager.release_lane.assert_awaited_once()
        assert not browser.sessions
    asyncio.run(run())


def test_host_id_idempotent_pending_open_and_authenticated_close(public_dns):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        session_id = str(uuid.uuid4())
        entered, finish = asyncio.Event(), asyncio.Event()

        async def lease(_key):
            entered.set()
            await finish.wait()
            return "owned-lease", {}

        manager.lease_lane.side_effect = lease
        first = asyncio.create_task(browser.open(URL, "interaction", TOKEN, session_id))
        await entered.wait()
        assert session_id in browser.sessions
        second = asyncio.create_task(browser.open(URL, "interaction", TOKEN, session_id))
        with pytest.raises(HTTPException) as error:
            await browser.close(session_id, "wrong")
        assert error.value.status_code == 404
        with pytest.raises(HTTPException) as error:
            await browser.open(URL, "interaction", "wrong", session_id)
        assert error.value.status_code == 404
        with pytest.raises(HTTPException) as error:
            await browser.open(URL + "/other", "interaction", TOKEN, session_id)
        assert error.value.status_code == 409
        finish.set()
        views = await asyncio.gather(first, second)
        assert views[0] == views[1] and views[0]["session_id"] == session_id
        assert await browser.open(URL, "interaction", TOKEN, session_id) == views[0]
        manager.lease_lane.assert_awaited_once()
        manager.context.new_page.assert_awaited_once()
        # Idempotent retries of an already-ready session outlive the open budget.
        browser.sessions[session_id].deadline -= module.OPEN_TIMEOUT + 1
        assert await browser.open(URL, "interaction", TOKEN, session_id) == views[0]
        await browser.close(session_id, TOKEN)
        assert await browser.close(session_id, TOKEN) == {"closed": True}
        with pytest.raises(HTTPException) as error:
            await browser.open(URL, "interaction", TOKEN, session_id)
        assert error.value.status_code == 410
        assert error.value.detail == {"code": "browser_closed"}
    asyncio.run(run())


def test_close_pending_open_cancels_and_retains_late_lease(public_dns):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        session_id = str(uuid.uuid4())
        entered, finish = asyncio.Event(), asyncio.Event()

        async def lease(_key):
            entered.set()
            await finish.wait()
            return "late-lease", {}

        manager.lease_lane.side_effect = lease
        opening = asyncio.create_task(browser.open(URL, "interaction", TOKEN, session_id))
        await entered.wait()
        closing = asyncio.create_task(browser.close(session_id, TOKEN))
        await asyncio.sleep(0)
        assert browser.sessions[session_id].closed
        finish.set()
        assert await closing == {"closed": True}
        await asyncio.gather(opening, return_exceptions=True)
        assert not browser.sessions
        manager.context.new_page.assert_not_awaited()
        manager.release_lane.assert_awaited_once_with("interaction", "late-lease")
        assert manager.lane.execution_semaphore.active == 0
    asyncio.run(run())


def test_close_before_dns_and_close_before_open_tombstones(public_dns):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        session_id = str(uuid.uuid4())
        assert await browser.close(session_id, TOKEN) == {"closed": True}
        with pytest.raises(HTTPException) as error:
            await browser.close(session_id, "wrong")
        assert error.value.status_code == 404
        with pytest.raises(HTTPException) as error:
            await browser.open(URL, "interaction", TOKEN, session_id)
        assert error.value.status_code == 410
        public_dns.assert_not_awaited()
        session_id = str(uuid.uuid4())
        entered = asyncio.Event()

        async def lookup(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        public_dns.side_effect = lookup
        opening = asyncio.create_task(browser.open(URL, "interaction", TOKEN, session_id))
        await entered.wait()
        await browser.close(session_id, TOKEN)
        await asyncio.gather(opening, return_exceptions=True)
        manager.lease_lane.assert_not_awaited()
        assert not browser.sessions
    asyncio.run(run())


def test_tombstones_expire_and_capacity_fails_closed(public_dns, monkeypatch):
    monkeypatch.setattr(module, "MAX_TOMBSTONES", 2)

    async def run():
        browser = AssistantBrowser(Manager())
        first, second, third = (str(uuid.uuid4()) for _ in range(3))
        await browser.close(first, TOKEN)
        await browser.close(second, TOKEN)
        with pytest.raises(HTTPException) as error:
            await browser.close(third, TOKEN)
        assert error.value.status_code == 503
        with pytest.raises(HTTPException):
            await browser.open(URL, "interaction", TOKEN, third)
        assert len(browser.tombstones) == 2
        digest, _ = browser.tombstones[first]
        browser.tombstones[first] = (digest, 0)
        await browser.close(third, TOKEN)
        assert first not in browser.tombstones and len(browser.tombstones) == 2
    asyncio.run(run())


def test_open_absolute_timeout_and_late_page_ownership(public_dns, monkeypatch):
    monkeypatch.setattr(module, "OPEN_TIMEOUT", 0.02)

    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        entered, finish = asyncio.Event(), asyncio.Event()

        async def create():
            entered.set()
            await finish.wait()
            return manager.page

        manager.context.new_page.side_effect = create
        session_id = str(uuid.uuid4())
        opening = asyncio.create_task(browser.open(URL, "interaction", TOKEN, session_id))
        await entered.wait()
        with pytest.raises(HTTPException) as error:
            await asyncio.wait_for(opening, 0.1)
        assert error.value.status_code == 504
        assert error.value.detail == {"code": "browser_open_timeout"}
        assert session_id in browser.sessions
        assert manager.lane.execution_semaphore.active == 1
        finish.set()
        await browser.close(session_id, TOKEN)
        manager.page.close.assert_awaited_once()
        assert not browser.sessions and manager.lane.execution_semaphore.active == 0
    asyncio.run(run())


@pytest.mark.parametrize("fallback_succeeds", [True, False])
def test_failed_target_close_fallback_and_quarantine(public_dns, fallback_succeeds):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        view = await browser.open(URL, "interaction", TOKEN)
        session_id = view["session_id"]
        manager.page.close.side_effect = RuntimeError("raw-secret")
        original = manager.cdp.send.side_effect

        async def send(method, params=None):
            if method == "Target.closeTarget":
                return {"success": fallback_succeeds}
            return await original(method, params)

        manager.cdp.send.side_effect = send
        if fallback_succeeds:
            await browser.close(session_id, TOKEN)
            assert session_id not in browser.sessions
            assert manager.lane.execution_semaphore.active == 0
            manager.release_lane.assert_awaited_once()
        else:
            with pytest.raises(HTTPException) as error:
                await browser.close(session_id, TOKEN)
            assert error.value.status_code == 503
            session = browser.sessions[session_id]
            assert session.closed and session.quarantined
            assert manager.lane.execution_semaphore.active == 1
            manager.release_lane.assert_not_awaited()
            manager.page.close.side_effect = None
            await browser._dispose(session)
            assert session_id not in browser.sessions
            assert manager.lane.execution_semaphore.active == 0
            manager.release_lane.assert_awaited_once()
        assert any(call.args[0] == "Target.closeTarget" for call in manager.cdp.send.call_args_list)
        manager.home.close.assert_not_awaited()
        manager.other.close.assert_not_awaited()
    asyncio.run(run())


def test_lifespan_attaches_starts_and_stops_assistant(monkeypatch):
    from worker import app as worker_app

    async def run():
        manager = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
        assistant = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
        factory = lambda value: assistant if value is manager else None
        monkeypatch.setattr(worker_app, "LaneManager", lambda: manager)
        monkeypatch.setattr(worker_app, "AssistantBrowser", factory)
        app = create_app()
        async with worker_app.lifespan(app):
            assert app.state.manager is manager
            assert app.state.assistant_browser is assistant
            assistant.start.assert_awaited_once()
        assistant.stop.assert_awaited_once()
        manager.stop.assert_awaited_once()
    asyncio.run(run())


def test_ttl_and_reaper(public_dns, monkeypatch):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        view = await browser.open(URL, "interaction", TOKEN)
        session = browser.sessions[view["session_id"]]
        assert 899 < session.deadline - time.monotonic() <= 900
        session.deadline = 0
        with pytest.raises(HTTPException) as error:
            await browser.observe(view["session_id"], TOKEN)
        assert error.value.status_code == 410
        assert error.value.detail == {"code": "browser_session_expired"}
        assert manager.lane.execution_semaphore.active == 0
        view = await browser.open(URL, "interaction", TOKEN)
        browser.sessions[view["session_id"]].deadline = 0
        real_sleep = asyncio.sleep

        async def fast_sleep(_seconds):
            await real_sleep(0.001)

        monkeypatch.setattr(module.asyncio, "sleep", fast_sleep)
        await browser.start()
        await real_sleep(0.02)
        assert not browser.sessions
        await browser.stop()
    asyncio.run(run())


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://[::1]/", "http://169.254.169.254/", "http://10.0.0.1/",
    "http://192.168.1.1/", "http://100.64.0.1/", "http://[fc00::1]/", "http://[fe80::1]/",
    "http://0.0.0.0/", "http://224.0.0.1/", "http://192.0.2.1/",
])
def test_nonpublic_literal_addresses_rejected(url):
    async def run():
        manager = Manager()
        with pytest.raises(HTTPException) as error:
            await AssistantBrowser(manager).open(url, "interaction", TOKEN)
        assert error.value.status_code == 403
        assert error.value.detail == {"code": "browser_dns_denied"}
        manager.lease_lane.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "https://u:p@example.com/",
    "https://example.com/\\x", "https://example.com/%5cx", "https://example.com/%0a",
    "https://example.com/\n", "https://example.com:bad/", "https://example.com./",
    "https://example.com:0/", "https://example.com/%7f", "https://example.com/\u0085",
])
def test_unsafe_urls_rejected_without_dns(url, public_dns):
    with pytest.raises(HTTPException) as error:
        _url(url)
    assert error.value.status_code == 403
    assert error.value.detail == {"code": "browser_url_denied"}
    public_dns.assert_not_awaited()


def test_dns_mixed_answers_cache_expiry_and_bounds(public_dns):
    async def run():
        dns = PublicDNS()
        with pytest.raises(HTTPException) as error:
            await dns.check("localhost")
        assert error.value.detail == {"code": "browser_dns_denied"}
        public_dns.assert_not_awaited()
        await dns.check("example.com")
        await dns.check("example.com")
        assert public_dns.await_count == 1
        dns.cache["example.com"] = 0
        public_dns.return_value += [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
        with pytest.raises(HTTPException) as error:
            await dns.check("example.com")
        assert error.value.detail == {"code": "browser_dns_denied"}
        public_dns.return_value = []
        with pytest.raises(HTTPException) as error:
            await dns.check("empty.example")
        assert error.value.detail == {"code": "browser_dns_denied"}
        public_dns.side_effect = OSError("secret DNS details")
        with pytest.raises(HTTPException) as error:
            await dns.check("proxy-only.example")
        assert error.value.detail == {"code": "browser_dns_denied"}
        public_dns.side_effect = None
        public_dns.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        for i in range(140):
            await dns.check(f"{i}.example.com")
        assert len(dns.cache) <= 128
    asyncio.run(run())


def test_route_revalidates_dns_after_short_cache(public_dns):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        view = await browser.open(URL, "interaction", TOKEN)
        session = browser.sessions[view["session_id"]]
        session.dns.cache.clear()
        public_dns.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 0))]
        route = SimpleNamespace(request=SimpleNamespace(
            url=URL, method="GET", resource_type="document", frame=manager.page.main_frame,
            is_navigation_request=lambda: True), abort=AsyncMock(), continue_=AsyncMock())
        await browser._route(session, route)
        route.abort.assert_awaited_once()
        route.continue_.assert_not_awaited()
        with pytest.raises(HTTPException):
            await browser.observe(view["session_id"], TOKEN)
        manager.page.evaluate.assert_not_awaited()
        await browser.stop()
    asyncio.run(run())


def test_observe_and_navigate_origin_defense(public_dns):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        view = await browser.open(URL, "interaction", TOKEN)
        session_id = view["session_id"]
        for url in ["https://other.example/", "http://example.com/", "https://example.com:444/"]:
            with pytest.raises(HTTPException):
                await browser.navigate(session_id, TOKEN, url)
        await browser.navigate(session_id, TOKEN, "https://example.com/next")
        result = await browser.observe(session_id, TOKEN)
        assert result["url"] == "https://example.com/next"
        manager.page.evaluate.assert_not_awaited()
        calls = [call for call in manager.cdp.send.call_args_list if call.args[0] == "Runtime.callFunctionOn"]
        assert len(calls) == 1
        assert calls[0].args[1]["functionDeclaration"] == SNAPSHOT_SCRIPT
        assert calls[0].args[1]["executionContextId"] == 42
        manager.page.url = "https://other.example/human-vnc"
        with pytest.raises(HTTPException):
            await browser.observe(session_id, TOKEN)
        assert manager.page.evaluate.await_count == 0
        await browser.stop()
    asyncio.run(run())


@pytest.mark.parametrize("url,method,resource,allowed", [
    (URL, "GET", "document", True),
    ("https://other.example/", "GET", "document", False),
    ("https://cdn.example/asset?q=1", "GET", "script", True),
    ("https://cdn.example/asset", "HEAD", "image", True),
    (URL, "POST", "fetch", False),
    ("http://127.0.0.1/x", "GET", "image", False),
    ("ws://example.com/", "GET", "websocket", False),
    (URL + "?return_to=%2Fstart", "GET", "document", True),
    (URL + "#fragment", "GET", "document", True),
    ("https://other.example/?return_to=%2Fstart", "GET", "document", False),
    ("https://user:password@example.com/?q=1", "GET", "document", False),
    (URL + "?q=%0a", "GET", "document", False),
])
def test_fixed_route_policy(public_dns, url, method, resource, allowed):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        view = await browser.open(URL, "interaction", TOKEN)
        session = browser.sessions[view["session_id"]]
        request = SimpleNamespace(url=url, method=method, resource_type=resource,
                                  frame=manager.page.main_frame,
                                  is_navigation_request=lambda: resource == "document")
        route = SimpleNamespace(request=request, abort=AsyncMock(), continue_=AsyncMock())
        await browser._route(session, route)
        assert route.continue_.await_count == int(allowed)
        assert route.abort.await_count == int(not allowed)
        request.frame = object()
        request.resource_type = "document"
        request.url = "https://unapproved.example/frame"
        route.abort.reset_mock()
        await browser._route(session, route)
        route.abort.assert_awaited_once()
        await browser._response(session, {"requestId": "r", "responseStatusCode": 302})
        assert manager.cdp.send.call_args.args[0] == "Fetch.failRequest"
        await browser.stop()
    asyncio.run(run())


@pytest.mark.parametrize("location,resource,allowed", [
    ("/next", "Document", True), ("https://example.com/next", "Document", True),
    ("https://other.example/next", "Document", False),
    ("//other.example/next", "Document", False),
    ("/next?token=secret", "Document", True), ("/next#secret", "Document", True),
    ("/login?return_to=%2Fstart#fragment", "Document", True),
    ("https://example.com/login?return_to=%2Fstart", "Document", True),
    ("https://other.example/login?return_to=%2Fstart", "Document", False),
    ("https://user:password@example.com/login?q=1", "Document", False),
    ("file:///login?q=1", "Document", False), ("/login?q=%0a", "Document", False),
    ("http://127.0.0.1/private", "Image", False),
    ("https://cdn.example/image?q=1", "Image", True),
    ("/next\\secret", "Document", False), ("/next\nsecret", "Document", False),
])
@pytest.mark.parametrize("executing", [False, True])
def test_redirect_location_policy(public_dns, location, resource, allowed, executing):
    allowed = allowed and not (executing and location.startswith("https://cdn.example/"))

    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        view = await browser.open(URL, "interaction", TOKEN)
        session = browser.sessions[view["session_id"]]
        session.executing = executing
        event = {"requestId": "r", "networkId": "chain", "frameId": "main",
                 "request": {"url": URL + "?return_to=%2Fstart#fragment", "method": "GET"}, "resourceType": resource,
                 "responseStatusCode": 302, "responseHeaders": [{"name": "Location", "value": location}]}
        await browser._response(session, event)
        assert manager.cdp.send.call_args.args[0] == ("Fetch.continueResponse" if allowed else "Fetch.failRequest")
        if not allowed:
            code = "browser_dns_denied" if location.startswith("http://127.0.0.1/") and not executing else "browser_redirect_denied"
            if location.startswith(("https://user:password@", "file://")):
                code = "browser_url_denied"
            if not executing and (location.startswith("https://other.example/") or location.startswith("//other.example/")):
                code = "browser_trust_required"
                assert session.failure["target_url"] == ("https:" + location if location.startswith("//") else location)
            assert session.failure["code"] == code
        await browser.stop()
    asyncio.run(run())


@pytest.mark.parametrize("stage", ["route", "redirect_source", "redirect_target"])
@pytest.mark.parametrize("executing", [False, True])
@pytest.mark.parametrize("suffix", ["?token=known%2Dcredential", "#known-credential"])
def test_native_navigation_blocks_known_secrets(public_dns, caplog, stage, executing, suffix):
    from worker.assistant_step import fingerprints

    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        view = await browser.open(URL, "interaction", TOKEN)
        session = browser.sessions[view["session_id"]]
        session.executing = executing
        session.secret_fingerprints = fingerprints(["known-credential"])
        if stage == "route":
            route = SimpleNamespace(request=SimpleNamespace(
                url=URL + suffix, method="GET", resource_type="document", frame=manager.page.main_frame,
                is_navigation_request=lambda: True), abort=AsyncMock(), continue_=AsyncMock())
            await browser._route(session, route)
            route.abort.assert_awaited_once()
            route.continue_.assert_not_awaited()
            assert session.failure == {"code": "browser_url_denied"}
        else:
            await browser._response(session, {
                "requestId": "r", "frameId": "main", "resourceType": "Document",
                "request": {"url": URL + (suffix if stage == "redirect_source" else ""), "method": "GET"},
                "responseStatusCode": 307, "responseHeaders": [{"name": "Location", "value": (
                    "/login" + (suffix if stage == "redirect_target" else "")
                )}],
            })
            assert manager.cdp.send.call_args.args[0] == "Fetch.failRequest"
            assert session.failure == {"code": "browser_url_denied"}
        assert "known-credential" not in json.dumps(session.failure)
        assert "known-credential" not in caplog.text and "known%2Dcredential" not in caplog.text
        await browser.stop()
    asyncio.run(run())


def test_redirect_chain_bound_dns_revalidation_and_304(public_dns):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        view = await browser.open(URL, "interaction", TOKEN)
        session = browser.sessions[view["session_id"]]
        event = {"requestId": "r", "networkId": "chain", "frameId": "main",
                 "request": {"url": URL, "method": "GET"}, "resourceType": "Document",
                 "responseStatusCode": 302, "responseHeaders": [{"name": "Location", "value": "/next"}]}
        for i in range(8):
            event["requestId"] = f"r{i}"
            if i:
                event["redirectedRequestId"] = f"r{i-1}"
            await browser._response(session, event)
            assert manager.cdp.send.call_args.args[0] == "Fetch.continueResponse"
        await browser._response(session, event)
        assert manager.cdp.send.call_args.args[0] == "Fetch.failRequest"
        assert session.failure == {"code": "browser_redirect_denied"}
        event["responseStatusCode"] = 304
        await browser._response(session, event)
        assert manager.cdp.send.call_args.args[0] == "Fetch.continueResponse"
        assert not session.redirects
        session.dns.cache.clear()
        public_dns.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
        event["responseStatusCode"] = 307
        await browser._response(session, event)
        assert manager.cdp.send.call_args.args[0] == "Fetch.failRequest"
        assert session.failure == {"code": "browser_dns_denied"}
        await browser.stop()
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["open", "navigate"])
@pytest.mark.parametrize("failure,code,status", [
    ("url", "browser_url_denied", 403),
    ("dns", "browser_dns_denied", 403),
    ("redirect", "browser_trust_required", 403),
    ("redirect_no_error", "browser_trust_required", 403),
    ("redirect_dns", "browser_dns_denied", 403),
    ("route_dns", "browser_dns_denied", 403),
    ("navigation", "browser_navigation_failed", 502),
])
def test_http_errors_preserve_static_reason_without_sensitive_logs(public_dns, caplog, operation, failure, code, status):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        app = create_app()
        app.state.assistant_browser = browser
        endpoint = "/assistant-browser/open"
        if operation == "navigate":
            view = await browser.open(URL, "interaction", TOKEN)
            endpoint = f"/assistant-browser/{view['session_id']}/navigate"
            browser.sessions[view["session_id"]].dns.cache.clear()

        async def goto(url, **kwargs):
            session = next(iter(browser.sessions.values()))
            if failure in {"redirect", "redirect_no_error", "redirect_dns"}:
                if failure == "redirect_dns":
                    session.dns.cache.clear()
                    public_dns.side_effect = OSError(f"{url} {TOKEN} resolver-secret")
                await browser._response(session, {
                    "requestId": "r", "frameId": "main", "resourceType": "Document",
                    "request": {"url": url, "method": "GET"}, "responseStatusCode": 302,
                    "responseHeaders": [{"name": "Location", "value": (
                        "/next" if failure == "redirect_dns" else "https://other.example/next?token=redirect-secret"
                    )}],
                })
            elif failure == "route_dns":
                session.dns.cache.clear()
                public_dns.side_effect = OSError(f"{url} {TOKEN} resolver-secret")
                route = SimpleNamespace(request=SimpleNamespace(
                    url=url, method="GET", resource_type="document", frame=manager.page.main_frame,
                    is_navigation_request=lambda: True), abort=AsyncMock(), continue_=AsyncMock())
                await browser._route(session, route)
                route.abort.assert_awaited_once()
            if failure == "redirect_no_error":
                manager.page.url = url
                return
            raise RuntimeError(f"{url} {TOKEN} navigation-secret")

        manager.page.goto = goto
        if failure == "dns":
            public_dns.side_effect = OSError(f"{URL} {TOKEN} resolver-secret")
        url = URL + "/%0a" if failure == "url" else URL
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
            response = await client.post(endpoint, json={"url": url, "token": TOKEN})
        assert response.status_code == status
        if code == "browser_trust_required":
            assert response.json() == {"detail": {
                "code": code, "source_url": URL,
                "target_url": "https://other.example/next?token=[REDACTED]",
                "source_url_redacted": False, "target_url_redacted": True,
                "redacted_parameters": ["token"],
            }}
        else:
            assert response.json() == {"detail": {"code": code}}
        records = [record for record in caplog.records if record.name == module.__name__]
        assert records
        for record in records:
            logged_code, lane, stage = record.args
            assert logged_code in {code, "browser_url_denied"}
            assert lane in {"none", "interaction"}
            assert stage in {"policy", "route", "redirect", "open", "navigate"}
            assert record.exc_info is None
        assert not any(value in caplog.text for value in [URL, TOKEN, "secret", "example.com"])
        await browser.stop()
    asyncio.run(run())


def test_isolated_snapshot_errors_have_no_mainworld_fallback(public_dns):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        view = await browser.open(URL, "interaction", TOKEN)
        original = manager.cdp.send.side_effect

        async def send(method, params=None):
            if method == "Runtime.callFunctionOn":
                return {"exceptionDetails": {"text": "private-exception-secret"}}
            return await original(method, params)

        manager.cdp.send.side_effect = send
        with pytest.raises(HTTPException) as error:
            await browser.observe(view["session_id"], TOKEN)
        assert "secret" not in str(error.value)
        manager.page.evaluate.assert_not_awaited()
        await browser.stop()
    asyncio.run(run())


def test_observe_warns_once_for_blocked_request_but_revalidates_current_url(public_dns):
    from worker.assistant_step import fingerprints

    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        session = browser.sessions[session_id]
        session.failure = {"code": "browser_url_denied"}
        observed = await browser.observe(session_id, TOKEN)
        assert observed["url"] == URL
        assert observed["warnings"] == [{"code": "browser_url_denied", "source": "blocked_request"}]
        assert "warnings" not in await browser.observe(session_id, TOKEN)
        session.secret_fingerprints = fingerprints(["private-credential"])
        for unsafe in [URL + "?q=private%2Dcredential", "https://unapproved.example/", "chrome-error://chromewebdata/"]:
            manager.page.url = unsafe
            with pytest.raises(HTTPException):
                await browser.observe(session_id, TOKEN)
        await browser.stop()
    asyncio.run(run())


def test_observe_does_not_consume_new_failure_during_snapshot(public_dns, monkeypatch):
    async def run():
        browser = AssistantBrowser(Manager())
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        session = browser.sessions[session_id]
        session.failure = {"code": "browser_url_denied"}
        newer = {"code": "browser_redirect_denied"}

        async def snapshot(*args):
            session.failure = newer
            return {"elements": []}

        monkeypatch.setattr(browser, "_isolated_snapshot", snapshot)
        observed = await browser.observe(session_id, TOKEN)
        assert observed["warnings"][0]["code"] == "browser_url_denied"
        assert session.failure is newer
        await browser.stop()
    asyncio.run(run())


def test_picker_and_assistant_share_admission(public_dns, monkeypatch):
    from worker import app as worker_app

    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        picker = await worker_app._open_picker(manager, URL)
        assert manager.lane.execution_semaphore.active == 1
        monkeypatch.setattr(module, "ACQUIRE_TIMEOUT", 0.01)
        with pytest.raises(HTTPException) as error:
            await browser.open(URL, "interaction", TOKEN)
        assert error.value.status_code == 409
        await worker_app._close_picker(manager, picker["session_id"])
        assert manager.lane.execution_semaphore.active == 0
        # Once a picker has acquired admission, cancellation closes its page
        # and releases both ownership resources.
        entered = asyncio.Event()

        async def hang():
            entered.set()
            await asyncio.Event().wait()

        manager.page.bring_to_front.side_effect = hang
        opening = asyncio.create_task(worker_app._open_picker(manager, URL))
        await entered.wait()
        opening.cancel()
        with pytest.raises(asyncio.CancelledError):
            await opening
        assert manager.lane.execution_semaphore.active == 0
    asyncio.run(run())


def test_snapshot_defense_and_byte_bound():
    raw = {"cookies": "secret", "elements": [{
        "node_id": "n0", "parent_id": None, "tag": "input",
        "attributes": {"type": "password", "id": "safe.id-1", "name": "unsafe name", "value": "secret", "data-secret": "secret"},
        "value": "secret", "defaultValue": "secret", "text": "secret", "selector": "injected",
        "label": "Password",
    }, {"node_id": "n1", "tag": "a", "attributes": {"href": "https://example.com/path?token=PRIVATE_VALUE#/login?tab=foo"}, "text": "Link"},
        {"node_id": "n2", "tag": "textarea", "text": "secret"},
        {"node_id": "n3", "tag": "input", "attributes": {"type": "hidden"}, "label": "secret"}]}
    result = _snapshot(raw)
    assert "secret" not in json.dumps(result)
    assert "PRIVATE_VALUE" not in json.dumps(result)
    assert "selector" not in result["elements"][0]
    assert result["elements"][1]["attributes"]["href"] == "https://example.com/path?token=[REDACTED]#/login?tab=foo"
    assert result["elements"][0]["attributes"]["name"] == "unsafe name"
    assert result["elements"][3]["attributes"]["type"] == "hidden"
    assert len(result["elements"]) == 4


def test_routes_use_app_state_and_validation_never_echoes_tokens():
    async def run():
        app = create_app()
        browser = SimpleNamespace(open=AsyncMock(return_value={"session_id": "opaque"}),
                                  observe=AsyncMock(return_value={"elements": []}),
                                  navigate=AsyncMock(return_value={}), close=AsyncMock(return_value={"closed": True}))
        app.state.assistant_browser = browser
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
            assert (await client.post("/assistant-browser/open", json={"url": URL, "token": TOKEN})).status_code == 200
            browser.open.assert_awaited_once_with(URL, "interaction", TOKEN, session_id=None, trusted_origins=[])
            host_id = str(uuid.uuid4())
            response = await client.post("/assistant-browser/open", json={"url": URL, "token": TOKEN, "session_id": host_id})
            assert response.status_code == 200
            browser.open.assert_awaited_with(URL, "interaction", TOKEN, session_id=host_id, trusted_origins=[])
            await client.post("/assistant-browser/open", json={"url": URL, "token": TOKEN,
                "trusted_origins": ["https://sso.example.com"]})
            browser.open.assert_awaited_with(URL, "interaction", TOKEN, session_id=None,
                                           trusted_origins=["https://sso.example.com"])
            for op in ["observe", "navigate", "close"]:
                payload = {"token": TOKEN, **({"url": URL} if op == "navigate" else {})}
                assert (await client.post(f"/assistant-browser/opaque/{op}", json=payload)).status_code == 200
            for payload in [{"token": TOKEN, "url": URL, "lane_key": "general"},
                            {"token": {"secret": TOKEN}, "url": URL},
                            {"token": TOKEN, "url": URL, "evaluate": "secret"}]:
                response = await client.post("/assistant-browser/open", json=payload)
                assert response.status_code == 422
                assert TOKEN not in response.text
            response = await client.post("/assistant-browser/opaque/observe", json={})
            assert response.status_code == 422
    asyncio.run(run())


def test_real_browser_snapshot_and_redirect_guard(public_dns):
    """Optional local Chromium only; no external HTTP requests."""
    async def run():
        from playwright.async_api import async_playwright

        candidates = list(Path("/opt/opencode/.cache/ms-playwright").glob("chromium_headless_shell-*/chrome-linux/headless_shell"))
        candidates += list(Path("/opt/opencode/.cache/ms-playwright").glob("chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"))
        async with async_playwright() as playwright:
            try:
                chromium = await playwright.chromium.launch(
                    headless=True, executable_path=str(candidates[0]) if candidates else None,
                    args=["--host-resolver-rules=MAP fixture.example 127.0.0.1", "--no-proxy-server"],
                )
            except Exception:
                pytest.skip("Local Chromium executable unavailable")
            try:
                context = await chromium.new_context(service_workers="block")
                manager = Manager()
                manager.lane.context = context
                browser = AssistantBrowser(manager)
                # Serve HTTP locally without relaxing the public DNS validator.
                seen = []

                async def serve(reader, writer):
                    request = await reader.readuntil(b"\r\n\r\n")
                    path = request.split(b" ")[1].decode()
                    seen.append(path)
                    if path == "/redirect":
                        response = (f"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:{port}/private\r\n"
                                    "Content-Length: 0\r\nConnection: close\r\n\r\n").encode()
                    elif path == "/redirect-login":
                        response = ("HTTP/1.1 307 Temporary Redirect\r\n"
                                    "Location: /login?return_to=%2Fstart&token=REDIRECT_ONLY_TOKEN#login-fragment\r\n"
                                    "Content-Length: 0\r\nConnection: close\r\n\r\n").encode()
                    elif path in {"/same", "/loop"}:
                        location = "/" if path == "/same" else "/loop"
                        response = (f"HTTP/1.1 302 Found\r\nLocation: {location}\r\n"
                                    "Content-Length: 0\r\nConnection: close\r\n\r\n").encode()
                    else:
                        body = b'''<h1>Welcome</h1><label for="password">Password <input id="password" type="password" value="SECRET"></label>
<label for="user">Username</label><input id="user" value="SECRET"><textarea>SECRET</textarea>
<select id="choice"><option>SECRET</option></select><div contenteditable="true"><button>SECRET</button></div>
<button>Continue <span hidden>Hidden helper</span><input value="SECRET"></button><h2 style="display:none">Hidden heading</h2>
<a href="https://example.com/path?token=SECRET#/login?tab=foo&access_token=SECRET">Safe link</a><input type="hidden" value="SECRET">
<div role="status">Ready</div><p>Generic body text</p>
<label for="editable" contenteditable="true">SECRET editable label</label><input id="editable">
<div contenteditable="true"><label for="nested">SECRET nested label</label></div><input id="nested">
<button contenteditable="true">SECRET root editable</button>
<script>
Document.prototype.createTreeWalker = () => { throw new Error('SECRET patched main world'); };
window.getComputedStyle = () => { throw new Error('SECRET patched main world'); };
</script>'''
                        response = b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
                    writer.write(response)
                    await writer.drain()
                    writer.close()
                    await writer.wait_closed()

                server = await asyncio.start_server(serve, "127.0.0.1", 0)
                port = server.sockets[0].getsockname()[1]
                try:
                    view = await browser.open(f"http://fixture.example:{port}/redirect-login", "interaction", TOKEN)
                    page = browser.sessions[view["session_id"]].page
                    assert page.url == f"http://fixture.example:{port}/login?return_to=%2Fstart&token=REDIRECT_ONLY_TOKEN#login-fragment"
                    assert "/login?return_to=%2Fstart&token=REDIRECT_ONLY_TOKEN" in seen
                    result = await browser.observe(view["session_id"], TOKEN)
                    assert result["url"] == f"http://fixture.example:{port}/login?return_to=%2Fstart&token=[REDACTED]#login-fragment"
                    assert result["url_redacted"] is True
                    assert result["redacted_parameters"] == ["token"]
                    encoded = json.dumps(result)
                    assert "SECRET" not in encoded
                    assert "REDIRECT_ONLY_TOKEN" not in encoded and "return_to=%2Fstart" in encoded
                    assert "login-fragment" in encoded and TOKEN not in encoded
                    assert "Welcome" in encoded and "Password" in encoded and "Continue" in encoded
                    assert '"tag": "textarea"' in encoded and '"tag": "select"' in encoded
                    link = next(item for item in result["elements"] if item["tag"] == "a")
                    assert link["attributes"]["href"] == "https://example.com/path?token=[REDACTED]#/login?tab=foo&access_token=[REDACTED]"
                    await browser.navigate(view["session_id"], TOKEN, f"http://fixture.example:{port}/same")
                    assert browser.sessions[view["session_id"]].page.url == f"http://fixture.example:{port}/"
                    with pytest.raises(HTTPException):
                        await browser.navigate(view["session_id"], TOKEN, f"http://fixture.example:{port}/redirect")
                    assert "/redirect" in seen and "/private" not in seen
                    with pytest.raises(HTTPException):
                        await browser.navigate(view["session_id"], TOKEN, f"http://fixture.example:{port}/loop")
                    assert seen.count("/loop") == 9
                    await browser.stop()
                finally:
                    server.close()
                    await server.wait_closed()
            finally:
                await chromium.close()
    asyncio.run(run())
