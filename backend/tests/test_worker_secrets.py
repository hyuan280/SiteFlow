import asyncio

import httpx
import pytest

from worker.secret_client import require_site_origin, resolve_secret
from worker.executor import _run_step


@pytest.mark.parametrize("current", ["https://other.test/login", "http://example.test", "https://example.test:444", "about:blank", "https://user:pass@example.test"])
def test_secret_origin_denied(current):
    with pytest.raises(RuntimeError, match="origin denied"):
        require_site_origin(current, "https://example.test")


def test_origin_normalization():
    require_site_origin("https://EXAMPLE.test:443/login", "https://example.test/")


def test_worker_lease_exchange(monkeypatch):
    calls = []
    original = httpx.AsyncClient
    def handle(request):
        import json
        body = json.loads(request.content)
        calls.append(body)
        assert request.headers["authorization"] == "Bearer worker-only"
        if len(calls) == 1:
            return httpx.Response(200, json={"lease": "one-use"})
        assert body["lease"] == "one-use"
        return httpx.Response(200, json={"username": "user", "password": "secret"})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    payload = {"job_id": 1, "site_id": 2, "secret_access": {"token": "worker-only", "providers": {"auth.form_login": "builtin.manual"}}}
    assert asyncio.run(resolve_secret(payload, {"step_key": "login", "feature_type": "auth.form_login"}))["password"] == "secret"
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


def test_login_fill_errors_do_not_echo_password(monkeypatch):
    import worker.executor as executor
    import worker.secret_client as client
    from types import SimpleNamespace
    class Locator:
        async def fill(self, value, **kwargs):
            raise RuntimeError(f"browser call log value={value}")
    async def locate(*args, **kwargs):
        return Locator()
    async def secret(*args):
        return {"username": "private-user", "password": "private-password"}
    monkeypatch.setattr(executor, "_find_locator", locate)
    monkeypatch.setattr(client, "resolve_secret", secret)
    with pytest.raises(RuntimeError, match="Sensitive login fill failed") as error:
        asyncio.run(_run_step(SimpleNamespace(url="https://example.test"), {"step_key": "login", "feature_type": "auth.form_login", "config": {}}, None, None, None, None, None, "general", secret_payload={"base_url": "https://example.test"}))
    assert "private" not in str(error.value)
