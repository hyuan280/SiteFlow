from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from app.schemas.lane import BROWSER_BACKEND_UNAVAILABLE

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker.app import create_app as create_worker_app  # noqa: E402
from worker.browser_backend import ManagerBackend  # noqa: E402


PATH = "/api/v1/lanes/browser-settings"
WORKER_URL = "http://license-worker:6300"
MANAGER_URL = "https://license-manager.example/base"
TOKEN = "integration-private-worker-token"
SECRET = "integration-private-license-key"
VERSION = "156.0.8078.47.1"


class ProviderBody(httpx.AsyncByteStream):
    def __init__(self, body):
        self.body = body

    async def __aiter__(self):
        yield self.body


@pytest.fixture
def chain(client, tmp_path, monkeypatch):
    settings = client.app.state.settings
    settings.worker_url = WORKER_URL
    settings.browser_token = SecretStr(TOKEN)
    settings.cloakbrowser_license_key = None
    monkeypatch.setenv("SITEFLOW_BROWSER_TOKEN", TOKEN)
    monkeypatch.setenv("SITEFLOW_BROWSER_URL", MANAGER_URL)
    monkeypatch.delenv("CLOAKBROWSER_LICENSE_KEY", raising=False)

    state = SimpleNamespace(
        settings=settings, tier="none", version=VERSION, saved=False, failure=None,
        release_channel="stable",
        worker_requests=[], manager_requests=[], unexpected=[],
    )

    def provider(request):
        state.manager_requests.append(request)
        target, failure = state.failure or (None, None)
        if (target == request.method or target == request.url.path):
            if failure == "timeout":
                raise httpx.ReadTimeout(SECRET + TOKEN + MANAGER_URL, request=request)
            if failure == "http":
                return httpx.Response(403, stream=ProviderBody((SECRET + TOKEN).encode()))
            if failure == "malformed":
                return httpx.Response(200, stream=ProviderBody((SECRET + TOKEN).encode()))
        if request.url.path == "/base/api/status":
            payload = {
                "license_tier": state.tier, "binary_version": state.version,
                "license_key": SECRET, "token": TOKEN, "manager_url": MANAGER_URL,
            }
        elif request.method == "PUT":
            body = json.loads(request.content)
            state.saved = bool(body["license_key"])
            state.release_channel = body.get("release_channel", state.release_channel)
            payload = {"saved": True, "license_key": SECRET, "token": TOKEN}
        else:
            payload = {
                "license_key_set": state.saved, "license_key": SECRET,
                "masked_license_key": SECRET, "release_channel": state.release_channel,
                "manager_url": MANAGER_URL,
            }
        # Exercise the provider's raw streaming path, not a pre-buffered JSON response.
        return httpx.Response(200, stream=ProviderBody(json.dumps(payload).encode()))

    worker_app = create_worker_app()
    worker_transport = httpx.ASGITransport(app=worker_app)
    manager_transport = httpx.MockTransport(provider)

    class InProcessTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            url = str(request.url)
            if url == WORKER_URL + "/browser/settings":
                state.worker_requests.append(request)
                return await worker_transport.handle_async_request(request)
            if (url in {MANAGER_URL + "/api/status", MANAGER_URL + "/api/settings"}
                    and request.method in {"GET", "PUT"}):
                return await manager_transport.handle_async_request(request)
            state.unexpected.append((request.method, url))
            raise AssertionError("Unexpected outbound request")

    # httpx is shared by both modules. Capture the original before patching it so
    # constructing either hop's client cannot recursively call this factory.
    real_async_client = httpx.AsyncClient

    def async_client(*args, **kwargs):
        kwargs.setdefault("transport", InProcessTransport())
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    backend = ManagerBackend(tmp_path)
    # Do not enter the Worker lifespan: it starts browser lanes and other services.
    worker_app.state.manager = SimpleNamespace(backend=backend)
    state.backend = backend
    try:
        yield state
    finally:
        asyncio.run(backend.close())
        assert state.unexpected == []
        assert backend.profiles == {}
        assert not backend.state_path.exists()
        assert backend.playwright is None
        for request in state.worker_requests + state.manager_requests:
            assert request.headers["Authorization"] == f"Bearer {TOKEN}"
            assert request.headers["Accept-Encoding"] == "identity"
            assert not {"origin", "cookie", "x-csrf-token"}.intersection(request.headers)
        assert all(
            request.url.path == "/base/api/settings" or request.method == "GET"
            for request in state.manager_requests
        )


def assert_private_response(response, caplog):
    assert response.headers["Cache-Control"] == "no-store"
    for secret in (SECRET, TOKEN, MANAGER_URL):
        assert secret not in response.text
    for secret in (SECRET, TOKEN):
        assert secret not in caplog.text


@pytest.mark.parametrize("tier", ["free", "none"])
def test_get_unset_through_authenticated_api_worker_and_manager(client, chain, caplog, tier):
    chain.tier = tier
    response = client.get(PATH)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "backend": "cloak-manager", "connected": True,
        "license": {"configured": False, "source": "unset", "editable": True},
        "license_tier": tier, "binary_version": VERSION, "error": None,
    }
    assert_private_response(response, caplog)
    assert [r.method for r in chain.worker_requests] == ["GET"]
    assert [(r.method, r.url.path) for r in chain.manager_requests] == [
        ("GET", "/base/api/status"), ("GET", "/base/api/settings"),
    ]


@pytest.mark.parametrize("tier", ["free", "none"])
def test_put_saves_and_refreshes_manager_badge_without_inventing_tier(client, chain, caplog, tier):
    chain.tier = tier
    response = client.put(
        PATH + "?manager_url=https://attacker.invalid",
        json={"license_key": f"  {SECRET}  "},
    )
    assert response.status_code == 200, response.text
    expected = {
        "backend": "cloak-manager", "connected": True,
        "license": {"configured": True, "source": "manager", "editable": True},
        "license_tier": tier, "binary_version": VERSION, "error": None,
    }
    assert response.json() == expected
    assert_private_response(response, caplog)
    assert [r.method for r in chain.worker_requests] == ["PUT"]
    assert json.loads(chain.worker_requests[0].content) == {"license_key": SECRET}
    assert [(r.method, r.url.path) for r in chain.manager_requests] == [
        ("GET", "/base/api/status"), ("GET", "/base/api/settings"),
        ("PUT", "/base/api/settings"),
        ("GET", "/base/api/status"), ("GET", "/base/api/settings"),
    ]
    saved = chain.manager_requests[2]
    assert json.loads(saved.content) == {"license_key": SECRET}
    assert "release_channel" not in saved.content.decode()
    refreshed = client.get(PATH)
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json() == expected
    assert_private_response(refreshed, caplog)
    assert sum(r.method == "PUT" for r in chain.manager_requests) == 1


@pytest.mark.parametrize("tier,version", [
    ("keyless", "146.0.7680.177.5"), ("none", VERSION),
])
def test_clear_manager_saved_key_refreshes_unset_badge_and_reported_tier(
    client, chain, caplog, tier, version,
):
    chain.tier = "free"
    chain.release_channel = "beta"
    saved = client.put(PATH, json={"license_key": SECRET})
    assert saved.status_code == 200, saved.text
    assert saved.json()["license"] == {"configured": True, "source": "manager", "editable": True}
    assert chain.saved is True
    assert_private_response(saved, caplog)
    chain.worker_requests.clear()
    chain.manager_requests.clear()
    chain.tier = tier
    chain.version = version

    response = client.put(PATH, json={"license_key": ""})
    assert response.status_code == 200, response.text
    expected = {
        "backend": "cloak-manager", "connected": True,
        "license": {"configured": False, "source": "unset", "editable": True},
        "license_tier": tier, "binary_version": version, "error": None,
    }
    assert response.json() == expected
    assert_private_response(response, caplog)
    assert chain.saved is False
    assert chain.release_channel == "beta"
    assert [r.method for r in chain.worker_requests] == ["PUT"]
    assert json.loads(chain.worker_requests[0].content) == {"license_key": ""}
    assert [(r.method, r.url.path) for r in chain.manager_requests] == [
        ("GET", "/base/api/status"), ("GET", "/base/api/settings"),
        ("PUT", "/base/api/settings"),
        ("GET", "/base/api/status"), ("GET", "/base/api/settings"),
    ]
    assert json.loads(chain.manager_requests[2].content) == {"license_key": ""}
    refreshed = client.get(PATH)
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json() == expected
    assert_private_response(refreshed, caplog)
    assert sum(r.method == "PUT" for r in chain.manager_requests) == 1
    assert chain.backend.settings_write_unknown is False


@pytest.mark.parametrize("layer", ["api", "worker"])
@pytest.mark.parametrize("env_key", [SECRET, " "])
@pytest.mark.parametrize("key", [SECRET, ""])
def test_env_lock_at_either_layer_blocks_put_and_reports_readonly_badge(
    client, chain, monkeypatch, caplog, layer, env_key, key,
):
    if layer == "api":
        chain.settings.cloakbrowser_license_key = SecretStr(env_key)
    else:
        monkeypatch.setenv("CLOAKBROWSER_LICENSE_KEY", env_key)
    response = client.put(PATH, json={"license_key": key})
    assert response.status_code == 409, response.text
    assert_private_response(response, caplog)
    assert [r.method for r in chain.worker_requests] == ([] if layer == "api" else ["PUT"])
    assert chain.manager_requests == []
    response = client.get(PATH)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "backend": "cloak-manager", "connected": True,
        "license": {"configured": True, "source": "env", "editable": False},
        "license_tier": "none", "binary_version": VERSION, "error": None,
    }
    assert_private_response(response, caplog)
    assert [r.method for r in chain.manager_requests] == ["GET", "GET"]
    assert not chain.saved


@pytest.mark.parametrize("target", ["/base/api/status", "/base/api/settings"])
@pytest.mark.parametrize("failure", ["http", "timeout", "malformed"])
def test_get_provider_failure_is_redacted_across_both_hops(client, chain, caplog, target, failure):
    chain.failure = (target, failure)
    response = client.get(PATH)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "backend": "cloak-manager", "connected": False,
        "license": {"configured": False, "source": "unknown", "editable": False},
        "license_tier": "unknown" if target.endswith("status") else "none",
        "binary_version": None if target.endswith("status") else VERSION,
        "error": BROWSER_BACKEND_UNAVAILABLE,
    }
    assert_private_response(response, caplog)
    assert len(chain.worker_requests) == 1
    assert len(chain.manager_requests) == (1 if target.endswith("status") else 2)
    assert all(r.method == "GET" for r in chain.manager_requests)


@pytest.mark.parametrize("failure,code", [("http", 502), ("timeout", 504), ("malformed", 502)])
@pytest.mark.parametrize("key", [SECRET, ""])
def test_put_provider_failure_is_static_redacted_and_not_retried(client, chain, caplog, failure, code, key):
    chain.failure = ("PUT", failure)
    response = client.put(PATH, json={"license_key": key})
    assert response.status_code == code, response.text
    assert response.json() == {
        "detail": "Browser settings request timed out" if code == 504 else BROWSER_BACKEND_UNAVAILABLE,
    }
    assert_private_response(response, caplog)
    assert len(chain.worker_requests) == 1
    assert [(r.method, r.url.path) for r in chain.manager_requests] == [
        ("GET", "/base/api/status"), ("GET", "/base/api/settings"),
        ("PUT", "/base/api/settings"),
    ]
    assert not chain.backend.settings_lock.locked()


@pytest.mark.parametrize("method,authenticated,code", [
    ("GET", False, 401), ("PUT", False, 401), ("PUT", True, 403),
])
@pytest.mark.parametrize("key", [SECRET, ""])
def test_public_auth_and_csrf_reject_before_internal_hops(
    client, chain, caplog, method, authenticated, code, key,
):
    if not authenticated:
        client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    response = client.request(method, PATH, json={"license_key": key})
    assert response.status_code == code, response.text
    assert_private_response(response, caplog)
    assert chain.worker_requests == []
    assert chain.manager_requests == []
