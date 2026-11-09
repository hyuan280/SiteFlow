from __future__ import annotations

import asyncio
import base64
import json
import uuid
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import HTTPException
from PIL import Image
from pydantic import ValidationError

from tests.test_assistant_browser import Manager, TOKEN, URL, public_dns  # noqa: F401
from tests.test_assistant_step import request
from worker import executor
from worker.app import create_app
from worker.assistant_browser import AssistantBrowser, MAX_SCREENSHOT_BYTES, SCREENSHOT_MASK
from worker.assistant_step import ExecuteRequest, fingerprints


def retry(original, **updates):
    return original.model_copy(deep=True, update={
        "operation_id": uuid.uuid4(), "retry_of": original.operation_id,
        "retry_authorization": uuid.uuid4(), **updates,
    })


def screenshot_page(manager, width=800, height=600):
    buffer = BytesIO()
    Image.new("RGB", (min(width, 1600), min(height, 1200)), "white").save(buffer, "JPEG")
    manager.page.screenshot = AsyncMock(return_value=buffer.getvalue())
    manager.page.locator = Mock(return_value=object())
    original = manager.cdp.send.side_effect

    async def send(method, params=None):
        if method == "Runtime.callFunctionOn" and "innerWidth" in params["functionDeclaration"]:
            return {"result": {"value": {"width": width, "height": height, "x": 0, "y": 100}}}
        return await original(method, params)

    manager.cdp.send.side_effect = send


@pytest.mark.parametrize("terminal", ["success", "fail"])
def test_exact_host_retry_is_one_use_cached_and_resolves_only_its_unknown(public_dns, monkeypatch, terminal):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        step = AsyncMock(return_value=("unknown", "private", {}))
        monkeypatch.setattr(executor, "_run_step", step)
        original = request("action.click", {"selector": ["#approved"]})
        unknown = await browser.execute(session_id, original)
        session = browser.sessions[session_id]
        approved = retry(original)
        changed = retry(original)
        changed.step["config"]["selector"] = ["#different"]
        with pytest.raises(HTTPException, match="retry operation conflict"):
            await browser.execute(session_id, changed)
        assert not session.consumed_permits
        with pytest.raises(HTTPException, match="uncertain"):
            await browser.execute(session_id, original.model_copy(update={"operation_id": uuid.uuid4()}))
        step.return_value = (terminal, "", {})
        result = await browser.execute(session_id, approved)
        assert result["status"] == ("success" if terminal == "success" else "failed")
        assert result["retry_of"] == str(original.operation_id)
        assert "retry_authorization" not in json.dumps(result)
        assert str(approved.retry_authorization) not in json.dumps(result)
        assert await browser.execute(session_id, approved) == result
        with pytest.raises(HTTPException) as error:
            await browser.execute(session_id, approved.model_copy(update={"token": "wrong"}))
        assert error.value.status_code == 404
        assert await browser.execute(session_id, original) == unknown
        assert session.uncertain == (terminal == "fail")
        assert session.unresolved_operations == ({str(original.operation_id)} if terminal == "fail" else set())
        assert session.consumed_permits == {str(approved.retry_authorization)}
        with pytest.raises(HTTPException, match="authorization unavailable"):
            await browser.execute(session_id, approved.model_copy(update={"operation_id": uuid.uuid4()}))
        with pytest.raises(HTTPException, match="operation conflict"):
            await browser.execute(session_id, approved.model_copy(update={"retry_authorization": uuid.uuid4()}))
        assert step.await_count == 2
        step.return_value = ("success", "", {})
        if terminal == "fail":
            with pytest.raises(HTTPException, match="uncertain"):
                await browser.execute(session_id, request("action.click", {"selector": ["#next"]}))
            # The host, not the worker, decides whether to issue another grant.
            assert (await browser.execute(session_id, retry(original)))["status"] == "success"
            assert not session.uncertain and not session.unresolved_operations
        else:
            with pytest.raises(HTTPException, match="retry operation conflict"):
                await browser.execute(session_id, retry(original))
        assert (await browser.execute(session_id, request("action.click", {"selector": ["#next"]})))["status"] == "success"
        assert manager.context.new_page.await_count == manager.lease_lane.await_count == 1
        await browser.stop()
    asyncio.run(run())


@pytest.mark.parametrize("terminal", ["success", "fail"])
def test_unknown_retry_requires_new_permit_and_resolves_retry_chain(public_dns, monkeypatch, terminal):
    async def run():
        browser = AssistantBrowser(Manager())
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        step = AsyncMock(return_value=("unknown", "", {}))
        monkeypatch.setattr(executor, "_run_step", step)
        original = request("action.click", {"selector": ["#approved"]})
        await browser.execute(session_id, original)
        approved = retry(original)
        assert (await browser.execute(session_id, approved))["status"] == "unknown"
        session = browser.sessions[session_id]
        assert session.unresolved_operations == {str(original.operation_id), str(approved.operation_id)}
        assert (await browser.execute(session_id, approved))["status"] == "unknown"
        with pytest.raises(HTTPException):
            await browser.execute(session_id, approved.model_copy(update={"operation_id": uuid.uuid4()}))
        step.return_value = (terminal, "", {})
        assert (await browser.execute(session_id, retry(approved)))["status"] == ("success" if terminal == "success" else "failed")
        assert session.uncertain == (terminal == "fail")
        assert session.unresolved_operations == (
            {str(original.operation_id), str(approved.operation_id)} if terminal == "fail" else set())
        assert step.await_count == 3
        await browser.stop()
    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["pre_effect_error", "missing_target", "submitted_auth_failure"])
def test_retry_without_success_keeps_uncertainty(public_dns, monkeypatch, outcome):
    from worker import assistant_step

    async def run():
        browser = AssistantBrowser(Manager())
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        step = AsyncMock(return_value=("unknown", "", {}))
        monkeypatch.setattr(executor, "_run_step", step)
        monkeypatch.setattr(assistant_step, "validate_secret_targets", AsyncMock())
        original = request("auth.password_verify", {
            "password_selector": ["#p"], "submit_selector": ["#s"],
        }, credentials={"password": "password-private"})
        await browser.execute(session_id, original)

        async def attempt(*args, **kwargs):
            context = kwargs["secret_payload"]["_assistant_secret_context"]
            if outcome == "pre_effect_error":
                raise RuntimeError("private-error")
            if outcome == "missing_target":
                return "fail", "Missing submit button", {}
            await context.resolve(original.step)
            context.action_started = True
            return "fail", "private-error", {}

        step.side_effect = attempt
        approved = retry(original)
        result = await browser.execute(session_id, approved)
        assert result["status"] == {
            "pre_effect_error": "error", "missing_target": "failed", "submitted_auth_failure": "unknown",
        }[outcome]
        session = browser.sessions[session_id]
        assert session.uncertain and str(original.operation_id) in session.unresolved_operations
        assert (str(approved.operation_id) in session.unresolved_operations) == (outcome == "submitted_auth_failure")
        assert await browser.execute(session_id, approved) == result
        assert "private-error" not in json.dumps(result)
        with pytest.raises(HTTPException, match="uncertain"):
            await browser.execute(session_id, request("action.click", {"selector": ["#next"]}))
        await browser.stop()
    asyncio.run(run())


@pytest.mark.parametrize("terminal", ["success", "fail"])
def test_terminal_retry_does_not_resolve_unrelated_unknown_or_accept_missing_original(public_dns, monkeypatch, terminal):
    async def run():
        browser = AssistantBrowser(Manager())
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        step = AsyncMock(return_value=("unknown", "", {}))
        monkeypatch.setattr(executor, "_run_step", step)
        first, second = request(), request()
        await browser.execute(session_id, first)
        await browser.execute(session_id, second)
        with pytest.raises(HTTPException, match="operation unavailable"):
            await browser.execute(session_id, retry(request()))
        step.return_value = (terminal, "", {})
        await browser.execute(session_id, retry(second))
        session = browser.sessions[session_id]
        assert session.uncertain and session.unresolved_operations == (
            {str(first.operation_id), str(second.operation_id)} if terminal == "fail" else {str(first.operation_id)})
        with pytest.raises(HTTPException, match="uncertain"):
            await browser.execute(session_id, request("action.click", {"selector": ["#next"]}))
        await browser.stop()
    asyncio.run(run())


def test_replacement_host_retry_and_pre_effect_failure_consume_permit(public_dns, monkeypatch):
    async def run():
        browser = AssistantBrowser(Manager())
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        step = AsyncMock(return_value=("success", "", {}))
        monkeypatch.setattr(executor, "_run_step", step)
        approved = retry(request("action.click", {"selector": ["#approved"]}))
        collect = browser._collect
        monkeypatch.setattr(browser, "_collect", AsyncMock(side_effect=RuntimeError("private-password")))
        result = await browser.execute(session_id, approved)
        assert result["status"] == "error"
        assert "private-password" not in json.dumps(result)
        step.assert_not_awaited()
        assert await browser.execute(session_id, approved) == result
        with pytest.raises(HTTPException, match="authorization unavailable"):
            await browser.execute(session_id, approved.model_copy(update={"operation_id": uuid.uuid4()}))
        monkeypatch.setattr(browser, "_collect", collect)
        assert (await browser.execute(session_id, retry(request("action.click", {"selector": ["#approved"]}))))["status"] == "success"
        await browser.stop()
    asyncio.run(run())


@pytest.mark.parametrize("updates", [
    {"retry_of": str(uuid.uuid4())}, {"retry_authorization": str(uuid.uuid4())},
    {"retry_of": "private", "retry_authorization": "private"},
    {"retry_of": str(uuid.uuid4()), "retry_authorization": str(uuid.uuid4()), "retry_step_digest": "private"},
])
def test_strict_retry_metadata(updates):
    with pytest.raises(ValidationError):
        ExecuteRequest.model_validate({**request().model_dump(mode="json"), **updates})


def test_screenshot_is_bounded_masked_and_not_secret_redacted(public_dns):
    async def run():
        manager = Manager()
        screenshot_page(manager, 1900, 1400)
        browser = AssistantBrowser(manager)
        session_id = (await browser.open(URL, "proxy_interaction", TOKEN))["session_id"]
        session = browser.sessions[session_id]
        session.secret_fingerprints = fingerprints(["/9j/4AAQ"])
        result = await browser.screenshot(session_id, TOKEN)
        assert result["mime_type"] == "image/jpeg" and result["masked"] is True
        assert (result["width"], result["height"]) == (1600, 1200)
        assert result["session_id"] == session_id and result["lane_key"] == "proxy_interaction"
        assert base64.b64decode(result["image_base64"]) == manager.page.screenshot.return_value
        manager.page.locator.assert_called_once_with(SCREENSHOT_MASK)
        options = manager.page.screenshot.call_args.kwargs
        assert options["mask"] == [manager.page.locator.return_value]
        assert options["clip"] == {"x": 0, "y": 0, "width": 1600, "height": 1200}
        assert options["scale"] == "css" and options["full_page"] is False
        assert session.page is manager.page and not session.executing
        assert not session.operations and not session.snapshots
        assert manager.context.new_page.await_count == manager.lease_lane.await_count == 1
        assert session.secret_fingerprints == fingerprints(["/9j/4AAQ"])
        await browser.stop()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["oversize", "mask", "navigation", "loader", "denied", "dns", "unapproved"])
def test_screenshot_fails_closed_without_image_or_private_errors(public_dns, failure):
    async def run():
        manager = Manager()
        screenshot_page(manager)
        browser = AssistantBrowser(manager)
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        session = browser.sessions[session_id]
        if failure == "oversize":
            manager.page.screenshot.return_value = b"x" * (MAX_SCREENSHOT_BYTES + 1)
        elif failure == "mask":
            manager.page.locator.side_effect = RuntimeError("private-password")
        elif failure in {"navigation", "loader"}:
            async def capture(**kwargs):
                if failure == "navigation":
                    manager.page.url = "https://unapproved.example/"
                else:
                    original = manager.cdp.send.side_effect

                    async def changed(method, params=None):
                        value = await original(method, params)
                        if method == "Page.getFrameTree":
                            value["frameTree"]["frame"]["loaderId"] = "new-loader"
                        return value
                    manager.cdp.send.side_effect = changed
                return manager.page.screenshot.return_value
            manager.page.screenshot.side_effect = capture
        elif failure == "denied":
            session.failure = {"code": "browser_dns_denied"}
        elif failure == "dns":
            session.dns.check = AsyncMock(side_effect=HTTPException(403, {"code": "browser_dns_denied"}))
        elif failure == "unapproved":
            manager.page.url = "https://unapproved.example/"
        with pytest.raises(HTTPException) as error:
            await browser.screenshot(session_id, TOKEN)
        assert "private-password" not in str(error.value)
        assert not session.closed
        if failure in {"denied", "dns", "unapproved", "mask"}:
            manager.page.screenshot.assert_not_awaited()
        await browser.stop()
    asyncio.run(run())


def test_refresh_uses_current_get_preserves_uncertainty_and_page(public_dns, monkeypatch):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        monkeypatch.setattr(executor, "_run_step", AsyncMock(return_value=("unknown", "", {})))
        original = request("action.click", {"selector": ["#approved"]})
        result = await browser.execute(session_id, original)
        session = browser.sessions[session_id]
        manager.page.url = URL + "?token=private-query"
        manager.page.goto = AsyncMock()
        manager.page.reload = AsyncMock()
        refreshed = await browser.refresh(session_id, TOKEN)
        assert refreshed["refreshed"] is True
        assert refreshed["url"] == URL + "?token=[REDACTED]" and refreshed["url_redacted"]
        manager.page.goto.assert_awaited_once_with(URL + "?token=private-query", wait_until="domcontentloaded", timeout=30000)
        manager.page.reload.assert_not_awaited()
        assert session.uncertain and session.unresolved_operations == {str(original.operation_id)}
        assert await browser.execute(session_id, original) == result
        assert session.page is manager.page and not session.executing
        assert not session.snapshots and not session.document_versions
        assert manager.context.new_page.await_count == manager.lease_lane.await_count == 1
        session.dns.check = AsyncMock(side_effect=HTTPException(403, {"code": "browser_dns_denied"}))
        with pytest.raises(HTTPException):
            await browser.refresh(session_id, TOKEN)
        assert manager.page.goto.await_count == 1 and not session.closed
        await browser.stop()
    asyncio.run(run())


@pytest.mark.parametrize("action", ["screenshot", "refresh"])
def test_recovery_actions_require_token_and_idle_session(public_dns, action):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        with pytest.raises(HTTPException) as error:
            await getattr(browser, action)(session_id, "wrong")
        assert error.value.status_code == 404
        async with browser.sessions[session_id].lock:
            with pytest.raises(HTTPException) as error:
                await getattr(browser, action)(session_id, TOKEN)
            assert error.value.status_code == 409
        await browser.stop()
    asyncio.run(run())


def test_recovery_endpoints_and_validation_do_not_echo_secrets():
    async def run():
        app = create_app()
        app.state.assistant_browser = Mock(screenshot=AsyncMock(return_value={"masked": True}),
                                           refresh=AsyncMock(return_value={"refreshed": True}))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
            for action in ("screenshot", "refresh"):
                path = f"/assistant-browser/id/{action}"
                assert (await client.post(path, json={"token": TOKEN})).status_code == 200
                response = await client.post(path, json={"token": TOKEN, "url": "private-password"})
                assert response.status_code == 422
                assert TOKEN not in response.text and "private-password" not in response.text
                getattr(app.state.assistant_browser, action).assert_awaited_once_with("id", TOKEN)
            payload = retry(request()).model_dump(mode="json")
            payload["retry_of"] = payload["operation_id"]
            response = await client.post("/assistant-browser/id/execute", json=payload)
            assert response.status_code == 422 and TOKEN not in response.text
    asyncio.run(run())


@pytest.mark.parametrize("action,limit", [("execute", 128 * 1024), ("screenshot", 4096), ("refresh", 4096)])
def test_recovery_request_limit_applies_to_chunked_body_before_json(action, limit):
    async def run():
        app = create_app()

        async def chunks():
            yield b'{"token":"private-password","padding":"'
            for _ in range(limit // 1024 + 1):
                yield b"x" * 1024
            yield b'"}'

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
            response = await client.post(f"/assistant-browser/id/{action}", content=chunks(),
                                         headers={"content-type": "application/json"})
            assert response.status_code == 413 and "private-password" not in response.text
    asyncio.run(run())


def test_real_browser_screenshot_masks_inputs_frames_and_refresh_never_reposts(public_dns, monkeypatch):
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
            browser = None
            server = None
            try:
                context = await chromium.new_context(viewport={"width": 800, "height": 600}, service_workers="block")
                seen = []

                async def serve(reader, writer):
                    headers = await reader.readuntil(b"\r\n\r\n")
                    seen.append(headers.split(b" ", 1)[0].decode())
                    length = next((int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                                   if line.lower().startswith(b"content-length:")), 0)
                    if length:
                        await reader.readexactly(length)
                    content = b'''
                        <style>input,textarea,select,[contenteditable],iframe {position:fixed;left:20px;width:200px;height:30px;margin:0;padding:0;border:0}</style>
                        <form action="/submitted" method="post">
                        <input id="password" type="password" value="private-password" style="top:20px">
                        <input id="revealed" value="private-password" style="top:70px">
                        <textarea style="top:120px">private-text</textarea>
                        <select style="top:170px"><option>private-option</option></select>
                        <div contenteditable style="top:220px">private-editable</div>
                        <iframe srcdoc="private-frame" style="top:270px"></iframe>
                        <button id="submit" type="submit">Submit</button></form>'''
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\nContent-Length: "
                                 + str(len(content)).encode() + b"\r\n\r\n" + content)
                    await writer.drain()
                    writer.close()
                    await writer.wait_closed()

                server = await asyncio.start_server(serve, "127.0.0.1", 0)
                origin = f"http://fixture.example:{server.sockets[0].getsockname()[1]}"
                manager = Manager()
                manager.lane.context = context
                browser = AssistantBrowser(manager)
                session_id = (await browser.open(origin + "/", "interaction", TOKEN))["session_id"]
                session = browser.sessions[session_id]
                page = session.page
                result = await browser.screenshot(session_id, TOKEN)
                with Image.open(BytesIO(base64.b64decode(result["image_base64"]))) as image:
                    assert image.size == (800, 600)
                    for y in (35, 85, 135, 185, 235, 285):
                        red, green, blue = image.getpixel((100, y))
                        assert red > 220 and green < 30 and blue > 220, (y, red, green, blue)
                assert await page.locator("#password").input_value() == "private-password"
                assert page.viewport_size == {"width": 800, "height": 600}
                assert await page.locator("#revealed").input_value() == "private-password"
                await page.evaluate("() => { document.body.style.height = '2000px'; scrollTo(0, 200); location.hash = 'section'; }")
                scrolled = await browser.screenshot(session_id, TOKEN)
                assert scrolled["url"] == origin + "/#section"
                assert await page.evaluate("() => scrollY") == 200
                with Image.open(BytesIO(base64.b64decode(scrolled["image_base64"]))) as image:
                    red, green, blue = image.getpixel((100, 85))
                    assert red > 220 and green < 30 and blue > 220
                gets = seen.count("GET")
                assert (await browser.refresh(session_id, TOKEN))["refreshed"]
                assert seen.count("GET") > gets
                await page.evaluate("() => scrollTo(0, 0)")

                async def submit(*args, **kwargs):
                    async with page.expect_navigation():
                        await page.locator("#submit").click()
                    return "unknown", "", {}

                monkeypatch.setattr(executor, "_run_step", submit)
                original = request("action.click", {"selector": ["#submit"]})
                original.site_origin = origin
                assert (await browser.execute(session_id, original))["status"] == "unknown"
                assert seen.count("POST") == 1
                assert (await browser.refresh(session_id, TOKEN))["refreshed"]
                assert seen[-1] == "GET" and seen.count("POST") == 1
                assert session.uncertain and session.page is page
                # Delayed page scripts must not gain POST permission from a
                # read-only recovery step while the original action is unknown.
                async def delayed_post(*args, **kwargs):
                    assert session.executing and not session.writes_authorized
                    assert await page.evaluate("""async () => {
                        await new Promise(resolve => setTimeout(resolve, 10));
                        try { await fetch('/delayed', {method:'POST', body:'private-body'}); return false; }
                        catch { return true; }
                    }""")
                    return "success", "", {}

                monkeypatch.setattr(executor, "_run_step", delayed_post)
                for feature in ["flow.open", "action.navigate", "action.wait"]:
                    readonly = request(feature, {"seconds": 1} if feature == "action.wait" else {"url": origin + "/"})
                    readonly.site_origin = origin
                    result = await browser.execute(session_id, readonly)
                    assert result["status"] == "unknown"
                    assert result["diagnostics"]["blocked_requests"][0]["method"] == "POST"
                    assert seen.count("POST") == 1
                    assert session.uncertain and str(original.operation_id) in session.unresolved_operations
                    assert not session.executing and not session.writes_authorized
            finally:
                if browser:
                    await browser.stop()
                if server:
                    server.close()
                    await server.wait_closed()
                await chromium.close()
    asyncio.run(run())
