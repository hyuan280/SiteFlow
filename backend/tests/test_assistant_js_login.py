from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException

from tests.test_assistant_browser import Manager, TOKEN, URL, public_dns  # noqa: F401
from tests.test_assistant_step import request
from worker import executor
from worker.assistant_browser import AssistantBrowser
from worker.assistant_step import fingerprints, validate_secret_targets


@pytest.mark.parametrize("mode", ["js", "native", "get", "default_get", "cross_origin", "put", "delete"])
def test_real_chromium_generic_login_transport(public_dns, caplog, mode):
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
                        assert path == "/api/auth/login?redirect=%2F"
                        assert parse_qs(body.decode()) == {
                            "username": ["alice-private"], "password": ["password-private"],
                        }
                        response = (b"HTTP/1.1 200 OK\r\nSet-Cookie: logged_in=yes; Path=/\r\n"
                                    b"Content-Length: 0\r\nConnection: close\r\n\r\n" if mode == "js" else
                                    b"HTTP/1.1 303 See Other\r\nLocation: /home\r\nSet-Cookie: logged_in=yes; Path=/\r\n"
                                    b"Content-Length: 0\r\nConnection: close\r\n\r\n")
                    else:
                        if path in {"/home", "/next"}:
                            assert b"logged_in=yes" in headers
                            content = b'<h1 id="ok">Signed in</h1>'
                        else:
                            method_attr = {'native': ' method="post"', 'get': ' method="get"'}.get(mode, '')
                            content = (f'<form action="/api/auth/login?redirect=%2F"{method_attr}>'
                                       '<input id="u" name="username"><input id="p" name="password" type="password">'
                                       '<button id="s">Login</button></form>').encode()
                            if mode not in {"native", "get", "default_get"}:
                                target = ("https://other.example/api/auth/login?redirect=%2F" if mode == "cross_origin"
                                          else "/api/auth/login?redirect=%2F")
                                method = mode.upper() if mode in {"put", "delete"} else "POST"
                                # The omitted form method defaults to GET. JS chooses a different endpoint.
                                content = content.replace(b'action="/api/auth/login?redirect=%2F"', b'action="/login"')
                                content += ("<script>document.querySelector('form').onsubmit = async e => {"
                                            "e.preventDefault(); try { const r = await fetch('" + target + "', {method:'" + method + "',"
                                            "body:new URLSearchParams(new FormData(e.target))});"
                                            "if(r.ok) location.href='/home'; } catch {} };</script>").encode()
                        response = (b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\nContent-Length: "
                                    + str(len(content)).encode() + b"\r\n\r\n" + content)
                    writer.write(response)
                    await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()

            server = await asyncio.start_server(serve, "127.0.0.1", 0)
            origin = f"http://fixture.example:{server.sockets[0].getsockname()[1]}"
            browser = None
            try:
                manager = Manager()
                manager.lane.context = await chromium.new_context(service_workers="block")
                browser = AssistantBrowser(manager)
                session_id = (await browser.open(origin + "/login", "interaction", TOKEN,
                              trusted_origins=["https://other.example"]))["session_id"]
                session = browser.sessions[session_id]
                page = session.page
                payload = request("auth.form_login", {
                    "username_selector": ["#u"], "password_selector": ["#p"],
                    "submit_selector": ["#s"], "success_selector": ["#ok"],
                }, credentials={"username": "alice-private", "password": "password-private"})
                payload.site_origin = origin
                payload.step["timeout_seconds"] = 2
                # Even explicit/default GET is admitted by DOM validation, not allowed to leak on the wire.
                await validate_secret_targets(page, payload.step, origin)
                result = await browser.execute(session_id, payload)
                assert await browser.execute(session_id, payload) == result
                assert session.page is page and not session.closed and not session.executing
                posts = [row for row in seen if row[0] == "POST"]
                if mode in {"js", "native"}:
                    assert result["status"] == "success", result
                    assert result["result"] == {"submitted": True, "verified": True}
                    assert len(posts) == 1
                    assert result["after"]["url"] == origin + "/home"
                    assert any(node.get("text") == "Signed in" for node in result["after"]["elements"])
                    assert not result["diagnostics"].get("blocked_requests")
                    await browser.refresh(session_id, TOKEN)
                    assert len([row for row in seen if row[0] == "POST"]) == 1
                    navigate = request("action.navigate", {"url": origin + "/next"})
                    navigate.site_origin = origin
                    assert (await browser.execute(session_id, navigate))["status"] == "success"
                    assert session.page is page
                else:
                    assert result["status"] == "unknown", result
                    assert not posts and all(row[0] == "GET" for row in seen)
                    blocked = result["diagnostics"]["blocked_requests"]
                    assert 1 <= len(blocked) <= 5
                    assert blocked[0]["method"] == ({"get": "GET", "default_get": "GET", "cross_origin": "POST"}.get(mode, mode.upper()))
                    if mode in {"get", "default_get"}:
                        assert blocked[0]["url"] == "[redacted]"
                        assert blocked[0]["code"] == "browser_url_denied"
                    elif mode == "cross_origin":
                        assert blocked[0]["code"] == "browser_redirect_denied"
                        assert urlsplit(blocked[0]["url"]).hostname == "other.example"
                    if page.url.startswith(origin):
                        assert result["after"]["elements"]
                        await browser.observe(session_id, TOKEN)
                        await browser.screenshot(session_id, TOKEN)
                    assert session.uncertain
                    assert session.unresolved_operations == {str(payload.operation_id)}
                    # Retained page scripts cannot POST outside active execution.
                    assert await page.evaluate("""async () => {
                        try { await fetch('/late', {method:'POST', body:'private-body'}); return false; }
                        catch { return true; }
                    }""")
                    assert not any(row[1] == "/late" for row in seen)
                assert all("alice-private" not in row[1] and "password-private" not in row[1] for row in seen)
                for secret in ("alice-private", "password-private", TOKEN, "private-body"):
                    assert secret not in json.dumps(result)
                    assert secret not in caplog.text
                assert "fixture.example" not in caplog.text
            finally:
                if browser:
                    await browser.stop()
                server.close()
                await server.wait_closed()
                await chromium.close()
    asyncio.run(run())


def test_credential_post_guard_checks_original_origin_without_route(public_dns):
    async def run():
        browser = AssistantBrowser(Manager())
        session_id = (await browser.open(URL, "interaction", TOKEN,
                      trusted_origins=["https://other.example"]))["session_id"]
        session = browser.sessions[session_id]
        session.credential_execution = True
        for url in [URL + "/api/auth?redirect=%2F", URL + "?public=1"]:
            browser._guard_secret_url(session, url, "POST")
        with pytest.raises(HTTPException) as error:
            browser._guard_secret_url(session, "https://other.example/api/auth", "POST")
        assert error.value.detail == {"code": "browser_redirect_denied"}
        await browser.stop()
    asyncio.run(run())


def test_blocked_request_diagnostics_are_bounded_projected_and_body_free(public_dns, monkeypatch):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        session = browser.sessions[session_id]
        session.secret_fingerprints = fingerprints(["password-private", "alice private"])

        async def step(*args, **kwargs):
            for suffix in ["?redirect=%2F&token=private-token", "/password%252Dprivate",
                           "?q=alice+private", *[f"?public={i}" for i in range(6)]]:
                route = SimpleNamespace(request=SimpleNamespace(
                    url=URL + suffix, method="DELETE", resource_type="fetch", frame=manager.page.main_frame,
                    post_data="private-body", is_navigation_request=lambda: False),
                    abort=AsyncMock(), continue_=AsyncMock())
                await browser._route(session, route)
                route.abort.assert_awaited_once()
            return "success", "", {}

        monkeypatch.setattr(executor, "_run_step", step)
        payload = request("action.click", {"selector": ["#s"]})
        result = await browser.execute(session_id, payload)
        assert result["status"] == "unknown"
        blocked = result["diagnostics"]["blocked_requests"]
        assert len(blocked) == 5
        assert blocked[0]["url"] == URL + "?redirect=%2F&token=[REDACTED]"
        assert blocked[1]["url"] == blocked[2]["url"] == "[redacted]"
        assert all(item["method"] == "DELETE" for item in blocked)
        assert all(item["code"] == "browser_url_denied" for item in blocked)
        assert result["after"]["url"] == URL
        for secret in ["password-private", "alice private", "private-token", "private-body", TOKEN]:
            assert secret not in json.dumps(result)
        assert await browser.execute(session_id, payload) == result
        await browser.stop()
    asyncio.run(run())
