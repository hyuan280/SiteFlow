import asyncio

import httpx
import pytest

from worker.secret_client import require_site_origin, resolve_secret
from worker.executor import PAGE_TIMEOUT_MS, _run_step
from types import SimpleNamespace
from unittest.mock import AsyncMock


@pytest.mark.parametrize("current", ["https://other.test/login", "http://example.test", "https://example.test:444", "about:blank", "https://user:pass@example.test"])
def test_secret_origin_denied(current):
    with pytest.raises(RuntimeError, match="origin denied"):
        require_site_origin(current, "https://example.test")


def test_origin_normalization():
    require_site_origin("https://EXAMPLE.test:443/login", "https://example.test/")


@pytest.mark.parametrize("purpose", ["auth.form_login", "auth.password_verify"])
def test_worker_lease_exchange(monkeypatch, purpose):
    calls = []
    original = httpx.AsyncClient
    def handle(request):
        import json
        body = json.loads(request.content)
        calls.append(body)
        assert body["purpose"] == purpose
        assert request.headers["authorization"] == "Bearer worker-only"
        if len(calls) == 1:
            return httpx.Response(200, json={"lease": "one-use"})
        assert body["lease"] == "one-use"
        return httpx.Response(200, json={"username": "user", "password": "secret"})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    payload = {"job_id": 1, "site_id": 2, "secret_access": {"token": "worker-only", "providers": {"verify": "builtin.manual"}}}
    assert asyncio.run(resolve_secret(payload, {"step_key": "verify", "feature_type": purpose}))["password"] == "secret"
    assert len(calls) == 2
    assert "lease" not in calls[0]


def test_worker_provider_error_redacted(monkeypatch):
    original = httpx.AsyncClient
    def handle(request):
        return httpx.Response(403, text="private-token-and-password")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    payload = {"job_id": 1, "site_id": 2, "secret_access": {"token": "worker-only"}}
    with pytest.raises(RuntimeError) as error:
        asyncio.run(resolve_secret(payload, {"step_key": "login", "feature_type": "auth.form_login"}))
    assert "private" not in str(error.value)
    assert "worker-only" not in str(error.value)


@pytest.fixture
def password_verification(monkeypatch):
    import worker.executor as executor
    import worker.secret_client as client

    page = SimpleNamespace(url="https://example.test/verify")
    password = SimpleNamespace(fill=AsyncMock(), input_value=AsyncMock(return_value="private-password"))
    submit = SimpleNamespace(click=AsyncMock())
    success = object()
    locators = {"#password": password, "#submit": submit, "#success": success}
    finder = AsyncMock(side_effect=lambda page, selectors, **kwargs: locators[selectors[0]])
    resolver = AsyncMock(side_effect=lambda *args: {"password": "private-password"})
    logs = []
    monkeypatch.setattr(executor, "_find_locator", finder)
    monkeypatch.setattr(client, "resolve_secret", resolver)
    monkeypatch.setattr(executor.asyncio, "sleep", AsyncMock())
    config = {"password_selector": ["#password"], "submit_selector": ["#submit"], "success_selector": ["#success"]}
    payload = {"base_url": "https://example.test"}

    def run(secret_payload=payload):
        return asyncio.run(_run_step(
            page, {"step_key": "verify", "feature_type": "auth.password_verify", "config": config},
            {"username": "legacy-user", "password": "legacy-password"}, None, None, None, None, "general",
            log=lambda level, message: logs.append(message), secret_payload=secret_payload,
        ))

    return SimpleNamespace(run=run, page=page, password=password, submit=submit,
                           resolver=resolver, finder=finder, locators=locators, config=config, logs=logs)


def test_password_verify_fills_only_password_and_verifies_success(password_verification):
    h = password_verification
    result = h.run()
    assert result == ("success", "密码验证通过", {"url": h.page.url, "submitted": True, "verified": True})
    h.resolver.assert_awaited_once()
    h.password.fill.assert_awaited_once_with("private-password", timeout=PAGE_TIMEOUT_MS)
    h.password.input_value.assert_awaited_once()
    h.submit.click.assert_awaited_once()
    assert [call.args[1] for call in h.finder.await_args_list] == [["#password"], ["#submit"], ["#success"]]
    assert "private-password" not in str(h.logs) + str(result)


@pytest.mark.parametrize("failure", ["fill", "input_value", "mismatch", "submit"])
def test_password_verify_errors_redact_password_and_never_submit_failed_fill(password_verification, failure):
    h = password_verification
    if failure == "mismatch":
        h.password.input_value.return_value = "wrong-value"
    elif failure == "submit":
        h.submit.click.side_effect = RuntimeError("private-password browser call log")
    else:
        getattr(h.password, failure).side_effect = RuntimeError("private-password browser call log")
    with pytest.raises(RuntimeError) as error:
        h.run()
    assert "private" not in str(error.value) + str(h.logs)
    if failure != "submit":
        h.submit.click.assert_not_awaited()


@pytest.mark.parametrize("missing", ["#password", "#submit", "#success"])
def test_password_verify_missing_elements_fail(password_verification, missing):
    h = password_verification
    h.locators[missing] = None
    result = h.run()
    assert result[0] == "fail"
    if missing == "#success":
        h.submit.click.assert_awaited_once()
        assert result[2]["verified"] is False
    else:
        h.password.fill.assert_not_awaited()
        h.submit.click.assert_not_awaited()


@pytest.mark.parametrize("secret", [{}, {"password": ""}, {"password": None}, {"password": 123}])
def test_password_verify_missing_password_never_submits(password_verification, secret):
    h = password_verification
    h.resolver.side_effect = lambda *args: dict(secret)
    assert h.run()[0] == "fail"
    h.finder.assert_not_awaited()
    h.submit.click.assert_not_awaited()


def test_password_verify_requires_secret_access_not_legacy_or_inline_password(password_verification):
    h = password_verification
    h.config["password"] = "inline-password"
    assert h.run(secret_payload=None)[0] == "fail"
    h.resolver.assert_not_awaited()
    h.finder.assert_not_awaited()
    h.password.fill.assert_not_awaited()


def test_password_verify_without_success_check_reports_only_submitted(password_verification):
    h = password_verification
    h.config["success_selector"] = []
    result = h.run()
    assert result[0] == "success"
    assert result[2]["submitted"] is True
    assert result[2]["verified"] is False
    assert "未配置成功元素检查" in result[1]


@pytest.mark.parametrize("redirect_on_focus", [1, 2])
def test_password_verify_rechecks_origin_after_input_gate_focus(password_verification, redirect_on_focus):
    from worker.browser_input import _InputGuard

    h = password_verification
    gate = _InputGuard()
    h.page.context = SimpleNamespace(_impl_obj=SimpleNamespace(_siteflow_input_guard=gate))
    focus_count = 0

    async def focus():
        nonlocal focus_count
        focus_count += 1
        if focus_count == redirect_on_focus:
            h.page.url = "https://evil.test/verify"

    h.page.bring_to_front = AsyncMock(side_effect=focus)
    with pytest.raises(RuntimeError, match="Secret origin denied"):
        h.run()
    if redirect_on_focus == 1:
        h.password.fill.assert_not_awaited()
    h.submit.click.assert_not_awaited()
    assert gate.owner is None and not gate.lock.locked()


def test_plugin_step_never_resolves_secret(monkeypatch):
    import worker.executor as executor
    import worker.secret_client as client
    async def forbidden(*args):
        raise AssertionError("plugin requested secret")
    async def plugin(*args):
        assert all(not isinstance(arg, dict) or "secret_access" not in arg for arg in args)
        return "success", "ok", {}
    monkeypatch.setattr(client, "resolve_secret", forbidden)
    monkeypatch.setattr(executor, "_run_plugin_step", plugin)
    result = asyncio.run(_run_step(None, {"step_key": "plugin", "feature_type": "plugin.demo.test", "config": {}}, None, None, None, None, None, "general", secret_payload={"secret_access": {"token": "private"}}))
    assert result == ("success", "ok", {})


@pytest.mark.parametrize("failure", ["username", "password", "mismatch"])
def test_login_fill_errors_do_not_echo_password(monkeypatch, failure):
    import worker.executor as executor
    import worker.secret_client as client
    from types import SimpleNamespace
    class Locator:
        def __init__(self, name):
            self.name = name

        async def fill(self, value, **kwargs):
            self.value = value
            if self.name == failure:
                raise RuntimeError(f"browser call log value={value}")

        async def input_value(self, **kwargs):
            return "" if failure == "mismatch" and self.name == "password" else self.value

        async def click(self, **kwargs):
            raise AssertionError("Must not submit a failed or unverified fill")

    locators = iter(Locator(name) for name in ("username", "password", "submit"))
    async def locate(*args, **kwargs):
        return next(locators)
    async def secret(*args):
        return {"username": "private-user", "password": "private-password"}
    monkeypatch.setattr(executor, "_find_locator", locate)
    monkeypatch.setattr(client, "resolve_secret", secret)
    with pytest.raises(RuntimeError, match="Sensitive login fill failed") as error:
        asyncio.run(_run_step(SimpleNamespace(url="https://example.test"), {"step_key": "login", "feature_type": "auth.form_login", "config": {}}, None, None, None, None, None, "general", secret_payload={"base_url": "https://example.test"}))
    assert "private" not in str(error.value)
