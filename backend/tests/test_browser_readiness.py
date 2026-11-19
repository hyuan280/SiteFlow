import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker.app import LANE_DEFINITIONS, LaneManager, LaneRuntime, create_app  # noqa: E402
from worker.browser_backend import ManagerBackend  # noqa: E402


TOKEN = "internal-readiness-token"
SECRET = "private-license-provider-detail"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
KEYS = [definition["key"] for definition in LANE_DEFINITIONS]


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    monkeypatch.setenv("SITEFLOW_BROWSER_TOKEN", TOKEN)
    monkeypatch.setenv("SITEFLOW_BROWSER_URL", "https://browser.example/base")
    monkeypatch.setenv("SITEFLOW_BROWSER_BACKEND", "not-the-actual-backend")
    monkeypatch.delenv("CLOAKBROWSER_LICENSE_KEY", raising=False)


@asynccontextmanager
async def worker(tmp_path, monkeypatch, payload=None, respond=None):
    backend = ManagerBackend(tmp_path)
    await backend.settings_client.aclose()
    calls = []

    async def dispatch(request):
        calls.append(request)
        assert request.method == "GET"
        assert str(request.url) == "https://browser.example/base/api/status"
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert request.headers["accept-encoding"] == "identity"
        assert set(request.extensions["timeout"].values()) == {10}
        if respond is not None:
            return await respond(request)
        return httpx.Response(200, json={"license_tier": "free"} if payload is None else payload)

    backend.settings_client = httpx.AsyncClient(
        base_url=backend.url, headers={**backend.headers, "Accept-Encoding": "identity"},
        transport=httpx.MockTransport(dispatch), trust_env=False, follow_redirects=False,
    )
    manager = LaneManager.__new__(LaneManager)
    manager.backend = backend
    manager.lanes = {
        definition["key"]: LaneRuntime(**definition) for definition in LANE_DEFINITIONS
    }
    manager.get = Mock(wraps=manager.get)

    def forbidden(*args, **kwargs):
        raise AssertionError("readiness must not mutate or inspect browser state")

    for name in ("prepare", "start", "stop", "request", "browser_settings", "update_browser_settings"):
        monkeypatch.setattr(backend, name, forbidden)
    for name in ("start_lane", "stop_lane", "lease_lane", "release_lane", "viewer_acquire",
                 "viewer_release", "focus_task_page", "status"):
        monkeypatch.setattr(manager, name, forbidden)
    monkeypatch.setattr(LaneRuntime, "view", forbidden)
    app = create_app()
    app.state.manager = manager
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
            yield client, backend, manager, calls
        assert backend.profiles == {}
        assert not backend.state_path.exists()
    finally:
        await backend.close()


def assert_result(response, ready, lane_flags=None, backend="cloak-manager"):
    assert response.status_code == 200
    assert response.json() == {
        "backend": backend, "ready": ready,
        "lanes": [{"key": key, "ready": (lane_flags or {}).get(key, ready)} for key in KEYS],
        "reason": None if ready else "browser_unavailable" if backend == "cloak-manager" else "unsupported_backend",
    }
    assert SECRET not in response.text
    assert TOKEN not in response.text
    assert "browser.example" not in response.text


@pytest.mark.parametrize("tier", ["keyless", "free", "pro"])
@pytest.mark.parametrize("version", [None, "", "146.0.7680.177.5", "999.1"])
def test_recognized_tiers_need_neither_configured_key_nor_exact_binary(tmp_path, monkeypatch, tier, version):
    async def run():
        payload = {"license_tier": tier, "binary_version": version, "license_key_set": False,
                   "license_key": SECRET, "provider": SECRET, "error": SECRET}
        async with worker(tmp_path, monkeypatch, payload) as (client, backend, manager, calls):
            assert await backend.execution_ready() is True
            assert_result(await client.get("/browser/readiness", headers=AUTH), True)
            assert len(calls) == 2
            assert [call.args[0] for call in manager.get.call_args_list] == KEYS
    asyncio.run(run())


@pytest.mark.parametrize("payload", [
    {"license_tier": "none"}, {"license_tier": "unknown"}, {"license_tier": SECRET},
    {"license_tier": None}, {"license_tier": ["free"]}, {"license_tier": {"tier": "free"}},
    {}, {"binary_version": "146.0.7680.177.5", "license_key_set": True},
    {"license_tier": "free", "ok": False}, {"license_tier": "free", "healthy": False},
    {"license_tier": "free", "connected": False}, {"license_tier": "free", "disconnected": True},
    [SECRET], SECRET, False,
])
def test_unlicensed_unhealthy_and_malformed_status_fail_closed(tmp_path, monkeypatch, payload):
    monkeypatch.setenv("CLOAKBROWSER_LICENSE_KEY", SECRET)

    async def run():
        async with worker(tmp_path, monkeypatch, payload) as (client, backend, _, calls):
            assert await backend.execution_ready() is False
            assert_result(await client.get("/browser/readiness", headers=AUTH), False)
            assert len(calls) == 2
    asyncio.run(run())


@pytest.mark.parametrize("flag", ["ok", "healthy", "connected", "disconnected"])
@pytest.mark.parametrize("value", [None, 0, 1, "", "false", "true", [], [True], {}, {"ok": True}])
def test_optional_health_flags_reject_non_booleans(tmp_path, monkeypatch, flag, value):
    async def run():
        async with worker(tmp_path, monkeypatch, {"license_tier": "free", flag: value}) as (client, backend, _, _):
            assert await backend.execution_ready() is False
            assert_result(await client.get("/browser/readiness", headers=AUTH), False)
    asyncio.run(run())


def test_explicit_healthy_flags_are_accepted(tmp_path, monkeypatch):
    async def run():
        payload = {"license_tier": "free", "ok": True, "healthy": True,
                   "connected": True, "disconnected": False}
        async with worker(tmp_path, monkeypatch, payload) as (client, backend, _, _):
            assert await backend.execution_ready() is True
            assert_result(await client.get("/browser/readiness", headers=AUTH), True)
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["timeout", "connect", "http", "malformed", "redirect"])
def test_transport_failures_are_static(tmp_path, monkeypatch, failure):
    async def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout(SECRET, request=request)
        if failure == "connect":
            raise httpx.ConnectError(SECRET, request=request)
        if failure == "redirect":
            return httpx.Response(302, headers={"Location": "https://other.example/" + SECRET})
        return httpx.Response(200 if failure == "malformed" else 503, content=SECRET)

    async def run():
        async with worker(tmp_path, monkeypatch, respond=respond) as (client, _, _, calls):
            assert_result(await client.get("/browser/readiness", headers=AUTH), False)
            assert len(calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize("headers,token", [
    ({}, TOKEN), ({"Authorization": "Bearer wrong"}, TOKEN),
    ({"Authorization": f"Basic {TOKEN}"}, TOKEN),
    ({**AUTH, "Origin": "https://siteflow.example"}, TOKEN), ({**AUTH, "Origin": ""}, TOKEN),
    (AUTH, ""),
])
def test_requires_internal_bearer_and_no_origin(tmp_path, monkeypatch, headers, token):
    monkeypatch.setenv("SITEFLOW_BROWSER_TOKEN", token)

    async def run():
        async with worker(tmp_path, monkeypatch) as (client, _, manager, calls):
            response = await client.get("/browser/readiness", headers=headers)
            assert response.status_code == 403
            assert calls == []
            manager.get.assert_not_called()
    asyncio.run(run())


def test_unsupported_identity_ignores_environment_and_duck_typing(tmp_path, monkeypatch):
    monkeypatch.setenv("SITEFLOW_BROWSER_BACKEND", "cloak-manager")

    async def run():
        async with worker(tmp_path, monkeypatch) as (client, _, manager, calls):
            manager.backend = SimpleNamespace(execution_ready=Mock(side_effect=AssertionError))
            assert_result(await client.get("/browser/readiness", headers=AUTH), False, backend="unsupported")
            assert calls == []
    asyncio.run(run())


@pytest.mark.parametrize("status,enabled,disconnected,expected", [
    ("stopped", True, None, True), ("running", True, False, True),
    ("running", True, None, True), ("stopped", False, None, False),
    ("disabled", False, None, False), ("error", True, None, False),
    ("starting", True, None, False), ("stopping", True, None, False),
    ("unrecognized", True, None, False), ("running", True, True, False),
    ("stopped", True, True, False),
])
def test_lane_flags_are_independent_and_readonly(tmp_path, monkeypatch, status, enabled, disconnected, expected):
    class UnreadableContext:
        @property
        def pages(self):
            raise AssertionError("do not inspect a running browser context")

    async def run():
        async with worker(tmp_path, monkeypatch) as (client, backend, manager, calls):
            lane = manager.lanes["general"]
            lane.status, lane.enabled = status, enabled
            lane.session = None if disconnected is None else SimpleNamespace(disconnected=disconnected)
            lane.context = UnreadableContext()
            lane.leases.add("busy-lease")
            lane.viewers.add("busy-viewer")
            lane.task_pages["job"] = object()
            manager.lanes["proxy_general"].enabled = False
            del manager.lanes["proxy_interaction"]
            snapshots = {key: (vars(value).copy(), set(value.leases), set(value.viewers), dict(value.task_pages))
                         for key, value in manager.lanes.items()}
            # Busy start/lane locks and lease/viewer counts are not readiness gates.
            async with backend.lock, lane.lock:
                response = await asyncio.wait_for(client.get("/browser/readiness", headers=AUTH), 1)
            assert_result(response, True, {"general": expected, "proxy_general": False, "proxy_interaction": False})
            for key, (fields, leases, viewers, pages) in snapshots.items():
                current = manager.lanes[key]
                assert vars(current) == fields
                assert current.leases == leases and current.viewers == viewers and current.task_pages == pages
            assert [call.args[0] for call in manager.get.call_args_list] == KEYS
            assert len(calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize("pending", ["unknown", "locked", "unknown-during-read", "locked-during-read"])
def test_pending_settings_fail_closed_without_clearing_state(tmp_path, monkeypatch, pending):
    async def run():
        async with worker(tmp_path, monkeypatch) as (client, backend, _, calls):
            if pending == "unknown":
                backend.settings_write_unknown = True
            elif pending == "locked":
                await backend.settings_lock.acquire()
            else:
                request = backend._settings_request

                async def concurrent_write(*args, **kwargs):
                    status = await request(*args, **kwargs)
                    if pending == "unknown-during-read":
                        backend.settings_write_unknown = True
                    else:
                        await backend.settings_lock.acquire()
                    return status

                monkeypatch.setattr(backend, "_settings_request", concurrent_write)
            try:
                assert_result(await client.get("/browser/readiness", headers=AUTH), False)
                assert backend.settings_write_unknown is pending.startswith("unknown")
                assert backend.settings_lock.locked() is pending.startswith("locked")
                assert len(calls) == int(pending.endswith("during-read"))
            finally:
                if backend.settings_lock.locked():
                    backend.settings_lock.release()
    asyncio.run(run())


@pytest.mark.parametrize("route", [False, True])
def test_completed_license_write_invalidates_inflight_read(tmp_path, monkeypatch, route):
    async def run():
        async with worker(tmp_path, monkeypatch) as (client, backend, _, _):
            entered = asyncio.Event()
            release = asyncio.Event()
            requests = []

            async def settings_request(method, path, **kwargs):
                requests.append((method, path))
                if len(requests) == 1:
                    assert (method, path) == ("GET", "/api/status")
                    entered.set()
                    await release.wait()
                if method == "PUT":
                    assert kwargs == {"json": {"license_key": SECRET}}
                    assert backend.settings_generation == 1
                    assert backend.settings_lock.locked()
                    assert backend.settings_write_unknown is True
                    return {}
                if path == "/api/status":
                    return {"license_tier": "free"}
                assert (method, path) == ("GET", "/api/settings")
                return {"license_key_set": False}

            monkeypatch.setattr(backend, "_settings_request", settings_request)
            monkeypatch.setattr(backend, "browser_settings", ManagerBackend.browser_settings.__get__(backend))
            assert backend.settings_generation == 0
            read = asyncio.create_task(client.get("/browser/readiness", headers=AUTH)
                                       if route else backend.execution_ready())
            try:
                await asyncio.wait_for(entered.wait(), 1)
                await ManagerBackend.update_browser_settings(backend, SECRET)
                assert backend.settings_generation == 1
                assert not backend.settings_lock.locked()
                assert backend.settings_write_unknown is False
                assert requests == [
                    ("GET", "/api/status"), ("GET", "/api/status"), ("GET", "/api/settings"),
                    ("PUT", "/api/settings"), ("GET", "/api/status"), ("GET", "/api/settings"),
                ]
            finally:
                release.set()
                result = await asyncio.wait_for(read, 1)
            if route:
                assert_result(result, False)
            else:
                assert result is False
            assert await backend.execution_ready() is True
            assert backend.settings_generation == 1
    asyncio.run(run())


def test_total_deadline_bounds_slow_status_stream(tmp_path, monkeypatch):
    real_timeout = asyncio.timeout
    deadlines = []
    closed = []

    def short_timeout(delay):
        deadlines.append(delay)
        return real_timeout(0.02)

    class SlowStatus(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"license_tier":'
            await asyncio.Event().wait()

        async def aclose(self):
            closed.append(True)

    async def respond(request):
        return httpx.Response(200, stream=SlowStatus())

    async def run():
        async with worker(tmp_path, monkeypatch, respond=respond) as (client, backend, _, calls):
            monkeypatch.setattr("worker.app.asyncio.timeout", short_timeout)
            response = await asyncio.wait_for(client.get("/browser/readiness", headers=AUTH), 1)
            assert_result(response, False)
            assert deadlines == [10]
            assert closed == [True]
            assert len(calls) == 1
            assert backend.settings_write_unknown is False
            assert not backend.settings_lock.locked()
    asyncio.run(run())
