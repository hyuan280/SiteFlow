from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from app.services.assistant import AssistantError, BrowserScope
from tests.test_assistant_browser import Manager, TOKEN, URL, public_dns  # noqa: F401
from tests.test_assistant_browser_queue import until
from tests.test_assistant_cookies import tools  # noqa: F401
from tests.test_assistant_snapshot import TARGET, dispatch_manager  # noqa: F401
from worker.assistant_browser import AssistantBrowser


PRIVATE = "private-CDP-error-url-cookie-token"
CODE = "browser_response_interception_failed"


def response_event(blocked=False):
    return {"requestId": "response", "frameId": "main", "resourceType": "Document",
            "request": {"url": URL, "method": "GET"},
            "responseStatusCode": 302 if blocked else 200,
            "responseHeaders": [{"name": "Location", "value": "http://127.0.0.1/private"}]}


def fail_fetch(manager, phase):
    original = manager.cdp.send.side_effect

    async def send(method, params=None):
        if method == {"continue_response": "Fetch.continueResponse",
                      "fail_request": "Fetch.failRequest"}[phase]:
            raise RuntimeError(PRIVATE)
        return await original(method, params)

    manager.cdp.send.side_effect = send


@pytest.mark.parametrize("phase", ["continue_response", "fail_request"])
@pytest.mark.parametrize("before_ready", [False, True])
@pytest.mark.parametrize("queued", [False, True])
def test_fetch_failure_retained_after_disposal_and_authenticated(
        public_dns, caplog, phase, before_ready, queued):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        sid = str(uuid4())
        session = None
        fail_fetch(manager, phase)

        async def goto(url, **kwargs):
            nonlocal session
            manager.page.url = url
            session = browser.sessions[sid]
            await browser._response(session, response_event(phase == "fail_request"))

        if before_ready:
            manager.page.goto = goto
        if queued:
            await browser.open(URL, "interaction", TOKEN, sid, wait_for_lane=True)
            await until(lambda: sid not in browser.sessions or browser.sessions[sid].ready)
        elif before_ready:
            with pytest.raises(HTTPException) as error:
                await asyncio.wait_for(browser.open(URL, "interaction", TOKEN, sid), 1)
            assert error.value.detail == {"code": CODE, "stage": phase}
        else:
            await browser.open(URL, "interaction", TOKEN, sid)
        if not before_ready:
            session = browser.sessions[sid]
            assert (await browser.open_status(sid, TOKEN))["status"] == "ready"
            await browser._response(session, response_event(phase == "fail_request"))
        await until(lambda: sid not in browser.sessions)
        assert session.closed and not session.ready
        for token in (TOKEN, "wrong"):
            for operation in (lambda: browser.observe(sid, token),
                              lambda: browser.navigate(sid, token, URL),
                              lambda: browser.open_status(sid, token),
                              lambda: browser.open(URL, "interaction", token, sid)):
                with pytest.raises(HTTPException) as error:
                    await operation()
                assert error.value.status_code == (502 if token == TOKEN else 404)
                assert error.value.detail == ({"code": CODE, "stage": phase} if token == TOKEN
                                              else "Browser session unavailable")
        # Stale Fetch callbacks during page.close cannot recursively dispose.
        calls = manager.cdp.send.await_count
        await browser._response(session, response_event())
        assert manager.cdp.send.await_count == calls
        assert manager.lane.execution_semaphore.active == 0
        manager.release_lane.assert_awaited_once()
        manager.page.close.assert_awaited_once()
        manager.cdp.detach.assert_awaited_once()
        assert not browser._queues
        digest, _ = browser.tombstones[sid]
        browser.tombstones[sid] = digest, 0
        with pytest.raises(HTTPException) as error:
            await browser.observe(sid, TOKEN)
        assert error.value.status_code == 404 and sid not in browser._open_errors

    asyncio.run(run())
    assert PRIVATE not in caplog.text and TOKEN not in caplog.text and URL not in caplog.text
    assert f"code={CODE}" in caplog.text and f"stage={phase}" in caplog.text


@pytest.mark.parametrize("expired", [False, True])
def test_closed_and_expired_tombstones_do_not_leak_to_wrong_token(public_dns, expired):
    async def run():
        browser = AssistantBrowser(Manager())
        sid = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        if expired:
            browser.sessions[sid].deadline = 0
        else:
            await browser.close(sid, TOKEN)
        for _ in range(2):
            for token in ("wrong", TOKEN):
                with pytest.raises(HTTPException) as error:
                    await browser.observe(sid, token)
                assert error.value.status_code == (410 if token == TOKEN else 404)
                assert error.value.detail == (
                    {"code": "browser_session_expired" if expired else "browser_closed"}
                    if token == TOKEN else "Browser session unavailable")
        assert not browser.sessions

    asyncio.run(run())


def test_closed_during_current_dns_cannot_publish_ready(public_dns):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        sid = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        session = browser.sessions[sid]

        async def check(host):
            await browser._response(session, response_event())

        fail_fetch(manager, "continue_response")
        session.dns.check = check
        with pytest.raises(HTTPException) as error:
            await browser.open_status(sid, TOKEN)
        assert error.value.detail == {"code": CODE, "stage": "continue_response"}
        await until(lambda: sid not in browser.sessions)
        assert not session.ready

    asyncio.run(run())


@pytest.mark.parametrize("during_dns", [False, True])
def test_closed_page_during_provision_cannot_become_ready(public_dns, during_dns):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        sid = str(uuid4())
        closed = False
        manager.page.is_closed = lambda: closed

        async def goto(url, **kwargs):
            nonlocal closed
            manager.page.url = url
            if not during_dns:
                closed = True
            else:
                async def check(host):
                    nonlocal closed
                    closed = True

                browser.sessions[sid].dns.check = check

        manager.page.goto = goto
        with pytest.raises(HTTPException) as error:
            await asyncio.wait_for(browser.open(URL, "interaction", TOKEN, sid), 1)
        assert error.value.detail == {"code": "browser_closed"}
        assert not browser.sessions
        manager.release_lane.assert_awaited_once()
        assert manager.lane.execution_semaphore.active == 0

    asyncio.run(run())


def test_cancelled_response_fails_closed_without_recursive_cleanup(public_dns):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        sid = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        session = browser.sessions[sid]
        entered = asyncio.Event()

        async def send(method, params=None):
            entered.set()
            await asyncio.Event().wait()

        manager.cdp.send.side_effect = send
        task = asyncio.create_task(browser._response(session, response_event()))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async def close():
            await browser._response(session, response_event())

        manager.page.close.side_effect = close
        await asyncio.wait_for(session.disposal, 1)
        assert not browser.sessions and not session.ready
        manager.page.close.assert_awaited_once()
        manager.release_lane.assert_awaited_once()
        assert manager.lane.execution_semaphore.active == 0

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["navigate", "refresh"])
def test_fetch_failure_while_navigation_holds_lock_retains_cause(public_dns, operation):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        sid = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        session = browser.sessions[sid]
        fail_fetch(manager, "continue_response")

        async def goto(url, **kwargs):
            await browser._response(session, response_event())
            await asyncio.sleep(0)
            raise RuntimeError(PRIVATE)

        manager.page.goto = goto
        args = (URL,) if operation == "navigate" else ()
        with pytest.raises(HTTPException) as error:
            await asyncio.wait_for(getattr(browser, operation)(sid, TOKEN, *args), 1)
        assert error.value.detail == {"code": CODE, "stage": "continue_response"}
        await asyncio.wait_for(session.disposal, 1)
        assert not browser.sessions and not session.lock.locked()
        manager.release_lane.assert_awaited_once()
        assert manager.lane.execution_semaphore.active == 0

    asyncio.run(run())


@pytest.mark.parametrize("status,detail,reason", [
    (502, {"code": CODE, "stage": "continue_response", "message": PRIVATE}, CODE),
    (502, {"code": CODE, "stage": PRIVATE}, CODE),
    (422, {"code": "browser_cookies_invalid", "cookies": PRIVATE}, "browser_cookies_invalid"),
    (502, {"code": "browser_cookie_import_failed", "message": PRIVATE}, "browser_cookie_import_failed"),
    (502, {"code": "browser_observation_unavailable"}, "browser_observation_unavailable"),
    (502, {"code": "browser_navigation_failed"}, "browser_navigation_failed"),
    (404, PRIVATE, "browser_session_unavailable"),
    (502, {"code": PRIVATE, "stage": PRIVATE}, "browser_worker_error"),
    (502, {"code": [PRIVATE]}, "browser_worker_error"),
    (502, {"code": CODE, "stage": [PRIVATE]}, CODE),
])
def test_backend_worker_errors_project_only_static_diagnostics(
        dispatch_manager, monkeypatch, status, detail, reason):
    manager, _ = dispatch_manager
    client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json={"detail": detail}))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(transport=transport, **kwargs))
    with pytest.raises(AssistantError) as error:
        asyncio.run(manager._worker("session/observe", {"token": TOKEN}))
    assert error.value.reason == reason
    expected = {"stage": "continue_response"} if isinstance(detail, dict) and detail.get("stage") == "continue_response" else {}
    assert error.value.details == expected
    assert PRIVATE not in repr(error.value.details) + str(error.value)


def test_backend_tool_retains_phase_without_claiming_url_or_proxy_cause(dispatch_manager):
    manager, sid = dispatch_manager
    manager.browsers[sid] = BrowserScope("owner", str(uuid4()), TOKEN, "interaction", "",
                                        time.monotonic() + 600)
    manager._worker = AsyncMock(side_effect=AssistantError(
        "browser_unavailable", reason=CODE, details={"stage": "fail_request", "message": PRIVATE}))
    result = asyncio.run(manager.browser_tool("owner", sid, TARGET, "browser_observe", None))
    assert result["reason"] == CODE and result["stage"] == "fail_request"
    assert result["lane_key"] == "interaction" and not result["ok"]
    assert sid not in manager.browsers and PRIVATE not in json.dumps(result)
    assert "does not establish" in result["notice"]


@pytest.mark.parametrize("reason", ["browser_lane_unavailable", "browser_dns_denied", PRIVATE])
def test_cookie_generation_preserves_only_static_failure_before_decryption(tools, monkeypatch, reason):
    from app.services.credentials import CredentialService

    tools.manager.browsers.clear()
    resolve = AsyncMock(side_effect=AssertionError("must not decrypt"))
    monkeypatch.setattr(CredentialService, "resolve_cookies", resolve)
    tools.manager._worker.side_effect = AssistantError(
        "browser_unavailable", reason=reason, details={"stage": PRIVATE, "payload": PRIVATE})
    result = asyncio.run(tools.dispatch("browser_open", {}))
    assert result == {"ok": False, "code": "cookie_restore_unavailable",
                      "reason": reason if reason != PRIVATE else "browser_connection_failed",
                      "lane_key": "interaction", "stage": "cookie_restore"}
    assert PRIVATE not in json.dumps(result)
    resolve.assert_not_called()
    tools.manager._worker.assert_awaited_once()
