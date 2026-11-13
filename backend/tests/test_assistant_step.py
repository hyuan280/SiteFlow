from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from tests.test_assistant_browser import Manager, TOKEN, URL, public_dns  # noqa: F401
from worker import assistant_browser as module, executor
from worker.app import create_app
from worker.assistant_browser import AssistantBrowser
from worker.assistant_step import ExecuteRequest, fingerprints, validate_step, validate_secret_targets
from worker.secret_client import AssistantSecretContext, resolve_secret, validate_assistant_submission


def request(feature="action.wait", config=None, **kwargs):
    return ExecuteRequest(
        token=TOKEN, operation_id=uuid.uuid4(), site_origin="https://example.com/",
        step={"step_key": "test", "feature_type": feature,
              "config": config if config is not None else {"seconds": 1}},
        execution={"feature_types": [feature], "allow_sign": feature.startswith("sign.") or feature in {"challenge.altcha", "challenge.slider"}},
        **kwargs,
    )


def test_existing_page_navigation_cache_and_redaction(public_dns, monkeypatch):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        session_id = (await browser.open(URL, "proxy_interaction", TOKEN))["session_id"]
        calls = []

        async def step(page, value, **kwargs):
            assert page is manager.page
            assert kwargs["credentials"] is None
            assert kwargs["artifacts"] is None and kwargs["capture_image"] is None
            kwargs["log"]("ERROR", "do-not-return-executor-log")
            assert browser.sessions[session_id].lock.locked()
            assert browser.sessions[session_id].executing
            calls.append(value)
            await page.goto(value["config"]["url"])
            return "success", "raw-secret-message", {"url": "secret", "verified": True, "count": 3, "raw": "secret"}

        monkeypatch.setattr(executor, "_run_step", step)
        payload = request("action.navigate", {"url": "https://example.com/next?token=hidden", "retry_count": 0})
        result = await browser.execute(session_id, payload)
        assert result == await browser.execute(session_id, payload)
        assert len(calls) == 1
        assert result["status"] == "success"
        assert result["before"]["url"] == URL
        assert result["after"]["url"] == "https://example.com/next?token=[REDACTED]"
        assert result["after"]["url_redacted"] is True
        assert result["result"] == {"verified": True, "count": 3}
        assert not any(secret in json.dumps(result) for secret in ["raw-secret", "do-not-return", "hidden"])
        assert (await browser.observe(session_id, TOKEN))["url"] == "https://example.com/next?token=[REDACTED]"
        assert manager.lane.execution_semaphore.active == 1
        manager.lease_lane.assert_awaited_once()
        manager.context.new_page.assert_awaited_once()
        payload.step["config"]["url"] = URL
        with pytest.raises(HTTPException) as error:
            await browser.execute(session_id, payload)
        assert error.value.status_code == 409
        await browser.stop()
    asyncio.run(run())


def test_busy_disconnect_close_and_terminal_cancel(public_dns, monkeypatch):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        entered = asyncio.Event()

        async def hang(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        mock = AsyncMock(side_effect=hang)
        monkeypatch.setattr(executor, "_run_step", mock)
        payload = request()
        first = asyncio.create_task(browser.execute(session_id, payload))
        await entered.wait()
        for value in [payload, request()]:
            with pytest.raises(HTTPException) as error:
                await browser.execute(session_id, value)
            assert error.value.status_code == 409
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        session = browser.sessions[session_id]
        assert not session.execution_task.done()
        await asyncio.wait_for(browser.close(session_id, TOKEN), 1)
        response = session.operations[str(payload.operation_id)][1]
        assert response["status"] == "unknown" and response["code"] == "outcome_uncertain"
        assert not session.executing and not session.writes_authorized and not session.lock.locked()
        assert manager.lane.execution_semaphore.active == 0
        mock.assert_awaited_once()
    asyncio.run(run())


def test_timeout_cache_capacity_and_token(public_dns, monkeypatch):
    monkeypatch.setattr(module, "EXECUTE_TIMEOUT", 0.02)
    monkeypatch.setattr(module, "MAX_OPERATIONS", 1)

    async def run():
        browser = AssistantBrowser(Manager())
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        payload = request()
        wrong = payload.model_copy(update={"token": "wrong"})
        with pytest.raises(HTTPException) as error:
            await browser.execute(session_id, wrong)
        assert error.value.status_code == 404
        result = await browser.execute(session_id, payload)
        assert result["status"] == "unknown" and result["code"] == "outcome_uncertain"
        assert await browser.execute(session_id, payload) == result
        with pytest.raises(HTTPException) as error:
            await browser.execute(session_id, request())
        assert error.value.status_code == 409
        await browser.stop()
    asyncio.run(run())


@pytest.mark.parametrize("outcome,expected", [("fail", "failed"), ("unknown", "unknown"), ("raise", "unknown")])
def test_failure_responses_are_static_cached_and_observed(public_dns, monkeypatch, outcome, expected):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]

        async def step(*args, **kwargs):
            if outcome == "raise":
                raise RuntimeError("DO-NOT-RETURN-private-password")
            return outcome, "DO-NOT-RETURN-private-password", {"message": "DO-NOT-RETURN", "count": "DO-NOT-RETURN"}

        mock = AsyncMock(side_effect=step)
        monkeypatch.setattr(executor, "_run_step", mock)
        payload = request()
        result = await browser.execute(session_id, payload)
        assert result["status"] == expected
        if expected == "unknown":
            assert result["code"] == "outcome_uncertain"
        assert result["before"]["url"] == result["after"]["url"] == URL
        assert result["result"] == {}
        assert "DO-NOT-RETURN" not in json.dumps(result)
        assert await browser.execute(session_id, payload) == result
        mock.assert_awaited_once()
        assert not browser.sessions[session_id].executing
        await browser.stop()
    asyncio.run(run())


@pytest.mark.parametrize("feature,config", [
    ("flow.open", {"url": URL}), ("action.navigate", {"url": URL}),
    ("auth.form_login", {"username_selector": ["#u"], "password_selector": ["#p"], "submit_selector": ["#s"]}),
    ("auth.password_verify", {"password_selector": ["#p"], "submit_selector": ["#s"]}),
    ("action.wait", {"seconds": 1}), ("action.click", {"selector": ["#s"]}),
    ("sign.button", {"button_selector": ["#s"]}), ("sign.unit3d_claim", {"claim_selector": ["#s"]}),
    ("challenge.altcha", {"widget_selector": ["#w"], "checkbox_selector": ["#c"], "token_selector": ["#t"]}),
    ("challenge.slider", {"container_selector": ["#c"], "handle_selector": ["#h"]}),
])
def test_reviewed_builtin_configs(feature, config):
    kwargs = {"credentials": {"username": "user", "password": "pass"}} if feature.startswith("auth.") else {}
    assert validate_step(request(feature, config, **kwargs))["feature_type"] == feature


@pytest.mark.parametrize("change", [
    {"feature_type": "action.fill"}, {"feature_type": "plugin.bad"}, {"feature_type": "flow.jump"},
    {"feature_type": "post.buy_card"}, {"feature_type": "flow.result"},
    {"config": {"seconds": "1"}}, {"config": {"seconds": True}},
    {"config": {"seconds": 1, "javascript": "anything"}},
    {"condition": "arbitrary()"}, {"enabled": False}, {"timeout_seconds": True},
])
def test_denied_step_shapes(change):
    payload = request()
    payload.step.update(change)
    with pytest.raises(ValueError):
        validate_step(payload)


def test_sign_allowlist_and_json_limit():
    payload = request("sign.button", {"button_selector": ["#sign"]})
    payload.execution.allow_sign = False
    with pytest.raises(ValueError):
        validate_step(payload)
    payload.execution.allow_sign = True
    payload.execution.feature_types = ["action.wait"]
    with pytest.raises(ValueError):
        validate_step(payload)
    raw = request().model_dump(mode="json")
    raw["step"]["config"] = {"value": "x" * 65536}
    with pytest.raises(ValidationError):
        ExecuteRequest.model_validate(raw)


@pytest.mark.parametrize("feature,config", [
    ("challenge.altcha", {"widget_selector": ["#business"], "checkbox_selector": ["#business"], "token_selector": ["#token"]}),
    ("challenge.slider", {"container_selector": ["#business"], "handle_selector": ["#business"]}),
])
def test_challenge_arbitrary_targets_require_sign_permission(public_dns, monkeypatch, feature, config):
    async def run():
        browser = AssistantBrowser(Manager())
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        payload = request(feature, config)
        payload.execution.allow_sign = False
        mock = AsyncMock()
        monkeypatch.setattr(executor, "_run_step", mock)
        with pytest.raises(HTTPException) as error:
            await browser.execute(session_id, payload)
        assert error.value.status_code == 422
        mock.assert_not_awaited()
        payload.execution.allow_sign = True
        mock.return_value = ("success", "", {})
        assert (await browser.execute(session_id, payload))["status"] == "success"
        mock.assert_awaited_once()
        await browser.stop()
    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["unknown", "timeout", "raise"])
def test_uncertainty_preserves_history_but_allows_fresh_steps(public_dns, monkeypatch, outcome):
    monkeypatch.setattr(module, "EXECUTE_TIMEOUT", 0.02)

    async def run():
        browser = AssistantBrowser(Manager())
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]

        async def step(*args, **kwargs):
            if outcome == "timeout":
                await asyncio.Event().wait()
            if outcome == "raise":
                raise RuntimeError("private-error")
            return "unknown", "private-message", {"submitted": True, "verified": False}

        mock = AsyncMock(side_effect=step)
        monkeypatch.setattr(executor, "_run_step", mock)
        payload = request("sign.button", {"button_selector": ["#sign"]})
        result = await browser.execute(session_id, payload)
        assert result["status"] == "unknown" and result["code"] == "outcome_uncertain"
        if outcome == "unknown":
            assert result["result"] == {"submitted": True, "verified": False}
        assert await browser.execute(session_id, payload) == result
        mock.assert_awaited_once()
        mock.side_effect = None
        mock.return_value = ("success", "", {})
        for feature, config in [("sign.button", {"button_selector": ["#sign"]}),
                                ("action.click", {"selector": ["#sign"]}),
                                ("challenge.slider", {"container_selector": ["#c"], "handle_selector": ["#h"]})]:
            assert (await browser.execute(session_id, request(feature, config)))["status"] == "success"
        assert browser.sessions[session_id].unresolved_operations == {str(payload.operation_id)}
        assert await browser.execute(session_id, payload) == result
        await browser.observe(session_id, TOKEN)
        await browser.navigate(session_id, TOKEN, URL)
        for payload in [request(), request("action.navigate", {"url": URL})]:
            assert (await browser.execute(session_id, payload))["status"] == "success"
        assert browser.sessions[session_id].uncertain
        await browser.close(session_id, TOKEN)
        replacement = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        assert not browser.sessions[replacement].uncertain
        assert (await browser.execute(replacement, request("action.click", {"selector": ["#sign"]})))["status"] == "success"
        await browser.stop()
    asyncio.run(run())


def test_timeout_before_executor_does_not_mark_outcome_uncertain(public_dns, monkeypatch):
    monkeypatch.setattr(module, "EXECUTE_TIMEOUT", 0.02)

    async def run():
        browser = AssistantBrowser(Manager())
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]

        async def hang(*args):
            await asyncio.Event().wait()

        monkeypatch.setattr(browser, "_collect", hang)
        mock = AsyncMock()
        monkeypatch.setattr(executor, "_run_step", mock)
        payload = request()
        result = await browser.execute(session_id, payload)
        assert result["status"] == "error" and result["code"] == "execution_timeout"
        assert await browser.execute(session_id, payload) == result
        assert not browser.sessions[session_id].uncertain
        mock.assert_not_awaited()
        await browser.stop()
    asyncio.run(run())


def test_secret_capability_is_typed_scoped_and_one_use():
    async def run():
        step = {"feature_type": "auth.password_verify", "step_key": "verify"}
        validate = AsyncMock()
        context = AssistantSecretContext("auth.password_verify", "verify", {"password": "SECRET"}, validate)
        other = AssistantSecretContext("auth.password_verify", "verify", {}, validate)
        context.targets["test"] = object()
        assert not other.targets
        with pytest.raises(RuntimeError):
            await resolve_secret({"_assistant_secret_context": {"credentials": "SECRET"}}, step)
        with pytest.raises(RuntimeError):
            await resolve_secret({"_assistant_secret_context": context}, {**step, "step_key": "wrong"})
        assert await resolve_secret({"_assistant_secret_context": context}, step) == {"password": "SECRET"}
        with pytest.raises(RuntimeError):
            await resolve_secret({"_assistant_secret_context": context}, step)
        validate.assert_awaited_once()
        # Regular jobs never acquire the assistant validation behavior.
        await validate_assistant_submission(None, None, step)
        await validate_assistant_submission({"secret_access": {}}, None, step)
        validate.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize("url,method,resource,executing,allowed", [
    (URL + "?next=1", "POST", "document", True, True),
    (URL, "POST", "fetch", False, False), (URL, "PUT", "fetch", True, False),
    (URL, "DELETE", "fetch", True, False),
    ("https://other.example/", "GET", "image", True, False),
    ("https://other.example/", "POST", "fetch", True, False),
    ("https://other.example/", "GET", "image", False, False),
])
def test_execution_route_policy(public_dns, url, method, resource, executing, allowed):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        session = browser.sessions[session_id]
        session.executing, session.same_origin_only = executing, True
        session.writes_authorized = executing
        route = SimpleNamespace(request=SimpleNamespace(
            url=url, method=method, resource_type=resource, frame=manager.page.main_frame,
            is_navigation_request=lambda: resource == "document"), abort=AsyncMock(), continue_=AsyncMock())
        await browser._route(session, route)
        assert route.continue_.await_count == int(allowed)
        assert route.abort.await_count == int(not allowed)
        await browser.stop()
    asyncio.run(run())


@pytest.mark.parametrize("feature", ["flow.open", "action.navigate", "action.wait", "action.click"])
@pytest.mark.parametrize("uncertain", [False, True])
@pytest.mark.parametrize("transport", ["route", "redirect"])
def test_only_host_reviewed_write_steps_allow_post(public_dns, monkeypatch, feature, uncertain, transport):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        session = browser.sessions[session_id]
        assert not session.writes_authorized
        original = request(feature, {
            "flow.open": {"url": URL}, "action.navigate": {"url": URL},
            "action.wait": {"seconds": 1}, "action.click": {"selector": ["#approved"]},
        }[feature])
        if uncertain:
            monkeypatch.setattr(executor, "_run_step", AsyncMock(return_value=("unknown", "", {})))
            await browser.execute(session_id, original)
            assert not session.writes_authorized
        payload = original.model_copy(update={"operation_id": uuid.uuid4()})
        allowed = feature == "action.click"

        async def delayed_post(*args, **kwargs):
            assert session.executing and session.writes_authorized == allowed
            if transport == "route":
                route = SimpleNamespace(request=SimpleNamespace(
                    url=URL + "?public=1", method="POST", resource_type="fetch", frame=manager.page.main_frame,
                    is_navigation_request=lambda: False), abort=AsyncMock(), continue_=AsyncMock())
                await browser._route(session, route)
                assert route.continue_.await_count == int(allowed)
                assert route.abort.await_count == int(not allowed)
            else:
                await browser._response(session, {
                    "requestId": "delayed", "frameId": "main", "resourceType": "Fetch",
                    "request": {"url": URL + "?public=1", "method": "POST"}, "responseStatusCode": 307,
                    "responseHeaders": [{"name": "Location", "value": "/next?public=1"}],
                })
                assert manager.cdp.send.call_args.args[0] == (
                    "Fetch.continueResponse" if allowed else "Fetch.failRequest")
            return "success", "", {}

        monkeypatch.setattr(executor, "_run_step", delayed_post)
        result = await browser.execute(session_id, payload)
        assert result["status"] == ("success" if allowed else "unknown")
        assert not session.executing and not session.writes_authorized
        if not allowed:
            assert result["diagnostics"]["blocked_requests"][0]["method"] == "POST"
            assert result["diagnostics"]["blocked_requests"][0]["code"] == "browser_url_denied"
            assert result["after"]["url"] == URL
        if uncertain:
            assert str(original.operation_id) in session.unresolved_operations
        # Observation never re-enables writes, including after a granted step.
        await browser.observe(session_id, TOKEN)
        assert not session.writes_authorized
        await browser.stop()
    asyncio.run(run())


@pytest.mark.parametrize("suffix,method,credential_execution,allowed", [
    ("?password=password-private", "GET", True, False),
    ("?password=%70%61%73%73%77%6f%72%64%2d%70%72%69%76%61%74%65", "GET", True, False),
    ("/%2570assword-private", "HEAD", True, False),
    ("?username=alice+private", "GET", True, False),
    ("/password-private", "POST", True, False),
    ("?safe=1", "POST", True, True), ("", "POST", True, True),
    ("?safe=2", "POST", True, True), ("/api/auth/login", "POST", True, True),
    ("?safe=%31", "POST", True, True),
    ("?safe=1", "GET", True, True),
    ("?safe=1", "POST", False, True),
    ("/password-private", "GET", False, False),
])
def test_credential_url_network_guard(public_dns, suffix, method, credential_execution, allowed):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        session = browser.sessions[session_id]
        session.executing = True
        session.writes_authorized = True
        session.credential_execution = credential_execution
        session.secret_fingerprints = fingerprints(["password-private", "alice private"])
        for resource in ["document", "image", "fetch"]:
            route = SimpleNamespace(request=SimpleNamespace(
                url=URL + suffix, method=method, resource_type=resource, frame=manager.page.main_frame,
                is_navigation_request=lambda: resource == "document"), abort=AsyncMock(), continue_=AsyncMock())
            await browser._route(session, route)
            assert route.continue_.await_count == int(allowed)
            assert route.abort.await_count == int(not allowed)
        await browser.stop()
    asyncio.run(run())


def test_redirect_cannot_bypass_secret_url_guard(public_dns):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        session = browser.sessions[session_id]
        session.executing = session.credential_execution = True
        session.writes_authorized = True
        session.secret_fingerprints = fingerprints(["password-private"])
        for resource in ["Document", "Image"]:
            await browser._response(session, {
                "requestId": "r", "frameId": "main", "request": {"url": URL, "method": "POST"},
                "resourceType": resource, "responseStatusCode": 303,
                "responseHeaders": [{"name": "Location", "value": "/home?password=%70assword-private"}],
            })
            assert manager.cdp.send.call_args.args[0] == "Fetch.failRequest"
        await browser.stop()
    asyncio.run(run())


@pytest.mark.parametrize("location,method,resource,allowed", [
    ("/home?session=private", "POST", "Document", True),
    ("https://other.example/home", "POST", "Document", False),
    ("https://other.example/image", "GET", "Image", False),
    ("/home", "DELETE", "Document", False),
])
def test_execution_redirect_scope(public_dns, location, method, resource, allowed):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        session = browser.sessions[session_id]
        session.executing = session.same_origin_only = True
        session.writes_authorized = True
        event = {"requestId": "r", "frameId": "main", "request": {"url": URL, "method": method},
                 "resourceType": resource, "responseStatusCode": 303,
                 "responseHeaders": [{"name": "Location", "value": location}]}
        await browser._response(session, event)
        assert manager.cdp.send.call_args.args[0] == ("Fetch.continueResponse" if allowed else "Fetch.failRequest")
        await browser.stop()
    asyncio.run(run())


@pytest.mark.parametrize("site_origin,target", [
    ("https://other.example/", URL), ("https://example.com/", "https://other.example/"),
    ("https://example.com/", "http://example.com/"),
    ("https://example.com/", "javascript:alert(1)"),
])
def test_execution_url_scope_before_executor(public_dns, monkeypatch, site_origin, target):
    async def run():
        browser = AssistantBrowser(Manager())
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        mock = AsyncMock()
        monkeypatch.setattr(executor, "_run_step", mock)
        payload = request("action.navigate", {"url": target})
        payload.site_origin = site_origin
        with pytest.raises(HTTPException) as error:
            await browser.execute(session_id, payload)
        assert error.value.status_code == 422
        mock.assert_not_awaited()
        assert not browser.sessions[session_id].operations
        await browser.stop()
    asyncio.run(run())


def test_endpoint_validation_does_not_echo_secrets():
    async def run():
        app = create_app()
        app.state.assistant_browser = SimpleNamespace(execute=AsyncMock(return_value={"status": "success"}))
        payload = request().model_dump(mode="json")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
            assert (await client.post("/assistant-browser/id/execute", json=payload)).status_code == 200
            for update in [{"operation_id": "SECRET"}, {"credentials": {"password": ["SECRET"]}},
                           {"vision": {"api_key": "SECRET"}}, {"execution": {"feature_types": ["plugin.SECRET"]}}]:
                response = await client.post("/assistant-browser/id/execute", json={**payload, **update})
                assert response.status_code == 422
                assert "SECRET" not in response.text and TOKEN not in response.text
    asyncio.run(run())


def test_real_browser_login_and_secret_target_defenses(public_dns):
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
            seen = []

            async def serve(reader, writer):
                headers = await reader.readuntil(b"\r\n\r\n")
                method, path, _ = headers.split(b"\r\n")[0].decode().split(" ")
                length = next((int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                               if line.lower().startswith(b"content-length:")), 0)
                body = await reader.readexactly(length) if length else b""
                seen.append((method, path, body))
                if method == "POST":
                    response = b"HTTP/1.1 303 See Other\r\nLocation: /home?session=private-query\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                else:
                    content = (b'<h1 id="ok">Welcome alice-private password-private</h1>' if path.startswith("/home") else
                               b'<form action="/login" method="post"><input id="u" name="username"><input id="p" name="password" type="password"><button id="s" type="submit">Login</button></form>')
                    response = b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\nContent-Length: " + str(len(content)).encode() + b"\r\n\r\n" + content
                writer.write(response)
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_server(serve, "127.0.0.1", 0)
            origin = f"http://fixture.example:{server.sockets[0].getsockname()[1]}"
            browser = None
            try:
                context = await chromium.new_context(service_workers="block")
                manager = Manager()
                manager.lane.context = context
                browser = AssistantBrowser(manager)
                session_id = (await browser.open(origin + "/", "interaction", TOKEN))["session_id"]
                page = browser.sessions[session_id].page
                payload = request("auth.form_login", {
                    "username_selector": ["#u"], "password_selector": ["#p"],
                    "submit_selector": ["#s"], "success_selector": ["#ok"],
                }, credentials={"username": "alice-private", "password": "password-private"})
                payload.site_origin = origin
                # Each unsafe target must fail before either input is filled.
                for script in [
                    "document.querySelector('#p').type = 'text'",
                    "document.querySelector('#u').type = 'hidden'",
                    "document.querySelector('#p').readOnly = true",
                    "document.querySelector('#u').after(document.querySelector('#u').cloneNode())",
                    "document.body.appendChild(document.createElement('form')).append(document.querySelector('#p'))",
                    "document.querySelector('form').replaceWith(...document.querySelector('form').children)",
                    "document.querySelector('form').action = 'https://other.example/'",
                    "document.querySelector('form').action = 'http://user:password@fixture.example/'",
                    "document.querySelector('#s').setAttribute('formaction', 'javascript:void(0)')",
                    "document.querySelector('form').target = '_blank'",
                    "document.querySelector('#s').formTarget = '_blank'",
                ]:
                    await page.evaluate(script)
                    with pytest.raises((RuntimeError, HTTPException)):
                        await validate_secret_targets(page, payload.step, origin)
                    denied = await browser.execute(session_id, payload.model_copy(update={"operation_id": uuid.uuid4()}))
                    assert denied["status"] == "error"
                    assert await page.locator("#p").input_value() == ""
                    assert not any(method == "POST" for method, _, _ in seen)
                    await browser.navigate(session_id, TOKEN, origin + "/")
                result = await browser.execute(session_id, payload)
                assert result["status"] == "success", result
                assert result["result"] == {"submitted": True, "verified": True}
                assert result["after"]["url"] == origin + "/home?session=[REDACTED]"
                assert result["after"]["url_redacted"] is True
                assert await browser.execute(session_id, payload) == result
                assert len([row for row in seen if row[0] == "POST"]) == 1
                assert b"password-private" in next(row[2] for row in seen if row[0] == "POST")
                assert "?session=" in page.url
                assert browser.sessions[session_id].page is page
                observed = await browser.observe(session_id, TOKEN)
                for secret in ["alice-private", "password-private", "private-query", TOKEN]:
                    assert secret not in json.dumps(result)
                    assert secret not in json.dumps(observed)
                await browser.navigate(session_id, TOKEN, origin + "/")
                verify = request("auth.password_verify", {
                    "password_selector": ["#p"], "submit_selector": ["#s"], "success_selector": ["#ok"],
                }, credentials={"password": "password-private"})
                verify.site_origin = origin
                verified = await browser.execute(session_id, verify)
                assert verified["status"] == "success", verified
                assert verified["result"] == {"submitted": True, "verified": True}
                assert "password-private" not in json.dumps(verified)
                # A failed success-element check is not proof the submitted auth
                # action failed; preserve evidence and the exact-ID replay result.
                await browser.navigate(session_id, TOKEN, origin + "/")
                unconfirmed = verify.model_copy(deep=True)
                unconfirmed.operation_id = uuid.uuid4()
                unconfirmed.step["config"]["success_selector"] = ["#missing-success"]
                unconfirmed.step["timeout_seconds"] = 2
                uncertain = await browser.execute(session_id, unconfirmed)
                assert uncertain["status"] == "unknown" and uncertain["code"] == "outcome_uncertain"
                assert await browser.execute(session_id, unconfirmed) == uncertain
                # The new authenticated page is still available to the next step.
                next_step = request("action.navigate", {"url": origin + "/next", "retry_count": 0})
                next_step.site_origin = origin
                assert (await browser.execute(session_id, next_step))["status"] == "success"
                assert browser.sessions[session_id].page is page
                # Input handlers can invalidate a form after the initial secret
                # check; click handlers can race even the final DOM check.
                for feature, script, password_empty in [
                    ("auth.form_login", "document.querySelector('#u').oninput = () => document.querySelector('form').method = 'get'", False),
                    ("auth.form_login", "document.querySelector('#u').oninput = () => document.querySelector('#p').replaceWith(document.querySelector('#p').cloneNode())", True),
                    ("auth.form_login", "document.querySelector('#u').oninput = () => { const old = document.querySelector('form'); const replacement = document.createElement('form'); replacement.method = 'post'; old.replaceWith(replacement); replacement.append(...old.children); }", True),
                    ("auth.form_login", "document.querySelector('#p').oninput = () => document.querySelector('form').method = 'get'", False),
                    ("auth.password_verify", "document.querySelector('#p').oninput = () => document.querySelector('form').method = 'get'", False),
                    ("auth.form_login", "document.querySelector('#s').onclick = () => document.querySelector('form').method = 'get'", False),
                    ("auth.password_verify", "document.querySelector('#s').onclick = () => document.querySelector('form').method = 'get'", False),
                ]:
                    await browser.close(session_id, TOKEN)
                    session_id = (await browser.open(origin + "/", "interaction", TOKEN))["session_id"]
                    page = browser.sessions[session_id].page
                    await page.evaluate("() => { " + script + "; }")
                    config = {"password_selector": ["#p"], "submit_selector": ["#s"], "success_selector": ["#ok"]}
                    if feature == "auth.form_login":
                        config["username_selector"] = ["#u"]
                    unsafe = request(feature, config, credentials={"username": "alice-private", "password": "password-private"})
                    unsafe.site_origin = origin
                    # Avoid waiting for a success element that must never appear.
                    unsafe.step["timeout_seconds"] = 2
                    seen_before = len(seen)
                    denied = await browser.execute(session_id, unsafe)
                    assert denied["status"] == "unknown", (feature, script, denied, page.url)
                    assert denied["code"] == "outcome_uncertain"
                    assert await browser.execute(session_id, unsafe) == denied
                    assert not any(method == "POST" for method, _, _ in seen[seen_before:])
                    assert all("password-private" not in path and "alice-private" not in path
                               for _, path, _ in seen)
                    if password_empty:
                        assert await page.locator("#p").input_value() == ""
                    assert "password-private" not in json.dumps(denied)
                    assert "alice-private" not in json.dumps(denied)
                    assert str(unsafe.operation_id) in browser.sessions[session_id].unresolved_operations
            finally:
                if browser:
                    await browser.stop()
                server.close()
                await server.wait_closed()
                await chromium.close()
    asyncio.run(run())
