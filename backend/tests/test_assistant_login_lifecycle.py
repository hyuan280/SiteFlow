from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from tests.test_assistant_browser import Manager, TOKEN, URL, public_dns  # noqa: F401
from tests.test_assistant_step import request
from worker import executor
from worker.assistant_browser import AssistantBrowser


@pytest.mark.parametrize("failure", ["executor", "postcheck", "snapshot", "snapshot_timeout", "evidence_budget"])
def test_failure_evidence_diagnostics_cache_and_no_retry(public_dns, monkeypatch, failure):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        session = browser.sessions[session_id]
        original_collect = browser._collect
        count = 0

        async def collect(*args, **kwargs):
            nonlocal count
            count += 1
            if count > 1 and failure in {"snapshot_timeout", "evidence_budget"}:
                await asyncio.Event().wait()
            if count == 2 and failure == "snapshot":
                raise HTTPException(409, {"code": "browser_snapshot_stale"})
            return await original_collect(*args, **kwargs)

        async def step(*args, **kwargs):
            if failure in {"executor", "postcheck"}:
                session.failure = {"code": "browser_url_denied"}
            if failure == "executor":
                raise RuntimeError("DO-NOT-RETURN-private-error")
            if failure == "snapshot_timeout":
                # Evidence is bounded by the session deadline as well as 3s.
                session.deadline = time.monotonic() + 0.05
                raise RuntimeError("private-error")
            return "success", "private-message", {}

        monkeypatch.setattr(browser, "_collect", collect)
        mock = AsyncMock(side_effect=step)
        monkeypatch.setattr(executor, "_run_step", mock)
        payload = request("action.click", {"selector": ["#s"]})
        started = time.monotonic()
        result = await browser.execute(session_id, payload)
        assert time.monotonic() - started < 3.5
        assert result["status"] == "unknown" and result["code"] == "outcome_uncertain"
        diagnostics = result["diagnostics"]
        if failure in {"executor", "postcheck"}:
            assert diagnostics["reason"] == "browser_url_denied"
        if failure == "snapshot":
            assert diagnostics["evidence_errorcode"] == "browser_snapshot_stale"
        if failure in {"snapshot_timeout", "evidence_budget"}:
            assert diagnostics["evidence_errorcode"] == "browser_snapshot_unavailable"
        if failure == "snapshot_timeout":
            assert diagnostics["reason"] == "execution_error"
            session.deadline = time.monotonic() + 60
        elif failure == "evidence_budget":
            assert diagnostics["reason"] == "execution_timeout"
        else:
            assert result["after"]["url"] == URL
        assert session.failure is None
        assert await browser.execute(session_id, payload) == result
        mock.assert_awaited_once()
        with pytest.raises(HTTPException) as error:
            await browser.execute(session_id, payload.model_copy(update={"operation_id": uuid.uuid4()}))
        assert error.value.status_code == 409
        monkeypatch.setattr(browser, "_collect", original_collect)
        assert "warnings" not in await browser.observe(session_id, TOKEN)
        assert "private" not in json.dumps(result)
        manager.page.close.assert_not_awaited()
        await browser.stop()
    asyncio.run(run())


def test_operation_preserves_failure_arriving_during_evidence(public_dns, monkeypatch):
    async def run():
        browser = AssistantBrowser(Manager())
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        session = browser.sessions[session_id]
        newer = {"code": "browser_redirect_denied"}
        original = browser._collect

        async def step(*args, **kwargs):
            session.failure = {"code": "browser_url_denied"}
            raise RuntimeError("private-error")

        async def collect(*args, **kwargs):
            result = await original(*args, **kwargs)
            if session.failure:
                session.failure = newer
            return result

        monkeypatch.setattr(executor, "_run_step", step)
        monkeypatch.setattr(browser, "_collect", collect)
        result = await browser.execute(session_id, request())
        assert result["diagnostics"]["reason"] == "browser_url_denied"
        assert session.failure is newer
        monkeypatch.setattr(browser, "_collect", original)
        assert (await browser.observe(session_id, TOKEN))["warnings"][0]["code"] == "browser_redirect_denied"
        assert session.failure is None
        await browser.stop()
    asyncio.run(run())


def test_real_chromium_query_login_and_same_origin_action_lifecycle(public_dns, caplog):
    async def run():
        from playwright.async_api import async_playwright

        candidates = list(Path("/opt/opencode/.cache/ms-playwright").glob("chromium_headless_shell-*/**/chrome-headless-shell"))
        candidates += list(Path("/opt/opencode/.cache/ms-playwright").glob("chromium_headless_shell-*/**/headless_shell"))
        async with async_playwright() as playwright:
            try:
                chromium = await playwright.chromium.launch(
                    executable_path=str(candidates[0]) if candidates else None, headless=True,
                    args=["--host-resolver-rules=MAP fixture.example 127.0.0.1", "--no-proxy-server"],
                )
            except Exception:
                pytest.skip("Local Chromium executable unavailable")
            seen = []

            async def serve(reader, writer):
                try:
                    headers = await reader.readuntil(b"\r\n\r\n")
                    method, path, _ = headers.split(b"\r\n")[0].decode().split(" ")
                    length = next((int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                                   if line.lower().startswith(b"content-length:")), 0)
                    body = await reader.readexactly(length) if length else b""
                    seen.append((method, path, body))
                    if method == "POST":
                        response = b"HTTP/1.1 303 See Other\r\nLocation: /home\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                    else:
                        content = (b'<h1 id="ok">Signed in</h1>' if path == "/home" else
                                   b'<form method="post"><input id="u" name="username"><input id="p" name="password" type="password"><button id="s">Login</button></form>')
                        response = b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\nContent-Length: " + str(len(content)).encode() + b"\r\n\r\n" + content
                    writer.write(response)
                    await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()

            server = await asyncio.start_server(serve, "127.0.0.1", 0)
            origin = f"http://fixture.example:{server.sockets[0].getsockname()[1]}"
            url = origin + "/login?redirect=%2F"
            manager = Manager()
            browser = None
            try:
                manager.lane.context = await chromium.new_context(service_workers="block")
                browser = AssistantBrowser(manager)
                # The grant covers the original origin, not a frozen endpoint.
                cases = [
                    ("", True, None),
                    ("document.querySelector('#s').formAction = '/login?redirect=%2F&via=submit#local'", True, None),
                    ("document.querySelector('form').onsubmit = e => { e.preventDefault(); fetch(e.target.action, {method:'POST', body:new FormData(e.target)}).then(() => location.href='/home'); }", True, None),
                    ("document.querySelector('#u').oninput = () => document.querySelector('form').action = '/login?redirect=%2Fother'", True, None),
                    ("document.querySelector('#u').oninput = () => document.querySelector('form').action = '/login?redirect=%2f'", True, None),
                    ("document.querySelector('#p').oninput = () => document.querySelector('#s').formAction = '/other'", True, None),
                    ("document.querySelector('#s').onclick = () => document.querySelector('form').action = '/login?redirect=%2Fother'", True, None),
                    ("document.querySelector('#s').onclick = () => document.querySelector('form').method = 'get'", False, "browser_url_denied"),
                    ("document.querySelector('#s').onclick = () => document.querySelector('form').action = '/login?q=password%252Dprivate'", False, "browser_url_denied"),
                    ("document.querySelector('form').onsubmit = e => { e.preventDefault(); fetch('/api/login', {method:'POST', body:new FormData(e.target)}).then(() => location.href='/home'); }", True, None),
                    ("document.querySelector('#s').onclick = () => document.querySelector('form').action = 'https://other.example/login'", False, "browser_redirect_denied"),
                ]
                for script, allowed, reason in cases:
                    session_id = (await browser.open(url, "interaction", TOKEN))["session_id"]
                    session = browser.sessions[session_id]
                    page = session.page
                    if script:
                        await page.evaluate("() => { " + script + "; }")
                    payload = request("auth.form_login", {
                        "username_selector": ["#u"], "password_selector": ["#p"],
                        "submit_selector": ["#s"], "success_selector": ["#ok"],
                    }, credentials={"username": "alice-private", "password": "password-private"})
                    payload.site_origin = origin
                    payload.step["timeout_seconds"] = 2
                    first = len(seen)
                    result = await browser.execute(session_id, payload)
                    assert result["status"] == ("success" if allowed else "unknown"), (script, result)
                    if allowed:
                        assert result["after"]["elements"] and result["after"]["url"].split("#")[0] == origin + "/home"
                        posts = [row for row in seen[first:] if row[0] == "POST"]
                        assert len(posts) == 1
                        assert b"password-private" in posts[0][2]
                    else:
                        assert result["diagnostics"]["reason"] == reason, (script, result)
                        assert not any(row[0] == "POST" for row in seen[first:])
                        with pytest.raises(HTTPException) as error:
                            await browser.execute(session_id, payload.model_copy(update={"operation_id": uuid.uuid4()}))
                        assert error.value.status_code == 409
                    assert await browser.execute(session_id, payload) == result
                    assert browser.sessions[session_id].page is page and not session.closed
                    if page.url.startswith(origin):
                        assert result["after"]["elements"], result
                        assert "warnings" not in await browser.observe(session_id, TOKEN)
                        assert "warnings" not in await browser.observe(session_id, TOKEN)
                    else:
                        assert result["diagnostics"]["evidence_errorcode"] == "browser_url_denied"
                        with pytest.raises(HTTPException):
                            await browser.observe(session_id, TOKEN)
                    assert all("password-private" not in row[1] and "alice-private" not in row[1] for row in seen)
                    assert "password-private" not in json.dumps(result) and "alice-private" not in json.dumps(result)
                    assert "fixture.example" not in caplog.text and "password-private" not in caplog.text
                    await browser.close(session_id, TOKEN)
            finally:
                if browser:
                    await browser.stop()
                server.close()
                await server.wait_closed()
                await chromium.close()
    asyncio.run(run())
