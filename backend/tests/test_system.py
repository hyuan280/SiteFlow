from __future__ import annotations

import asyncio
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier, Event
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models.setting import AppSetting
from app.services import system_resources
from app.services.system_settings import SHOW_SYSTEM_RESOURCES_KEY
from tests.helpers import login_client

SETTINGS_PATH = "/api/v1/system-settings"
RESOURCES_PATH = "/api/v1/system-resources"
METRICS = ("cpu_percent", "memory_percent", "memory_used_bytes", "memory_total_bytes")


@pytest.fixture
def psutil_mock(monkeypatch):
    module = SimpleNamespace(
        cpu_percent=Mock(return_value=12.5),
        virtual_memory=Mock(return_value=SimpleNamespace(
            percent=40, used=4 * 1024**3, available=5 * 1024**3, total=8 * 1024**3,
        )),
    )
    monkeypatch.setitem(sys.modules, "psutil", module)
    monkeypatch.setattr(system_resources.Path, "is_socket", lambda self: False)
    return module


def test_default_and_persistence_across_app_restart(client, settings):
    assert client.get(SETTINGS_PATH).json() == {"show_system_resources": False}
    with client.app.state.session_factory() as db:
        assert db.get(AppSetting, SHOW_SYSTEM_RESOURCES_KEY) is None

    for enabled in (True, False):
        response = client.put(SETTINGS_PATH, json={"show_system_resources": enabled})
        assert response.status_code == 200, response.text
        assert response.json() == {"show_system_resources": enabled}
        with client.app.state.session_factory() as db:
            assert json.loads(db.get(AppSetting, SHOW_SYSTEM_RESOURCES_KEY).value_json) is enabled
        restarted = TestClient(create_app(settings))
        login_client(restarted)
        assert restarted.get(SETTINGS_PATH).json() == {"show_system_resources": enabled}


@pytest.mark.parametrize("raw", ["broken-json", "1", '"true"', "null"])
def test_invalid_stored_setting_defaults_to_disabled(client, raw):
    with client.app.state.session_factory() as db:
        db.add(AppSetting(key=SHOW_SYSTEM_RESOURCES_KEY, value_json=raw))
        db.commit()
    assert client.get(SETTINGS_PATH).json() == {"show_system_resources": False}


@pytest.mark.parametrize("method,path", [
    ("GET", SETTINGS_PATH), ("PUT", SETTINGS_PATH), ("GET", RESOURCES_PATH),
])
def test_authentication_precedes_sampling(anon_client, psutil_mock, method, path):
    response = anon_client.request(method, path, json={"show_system_resources": True})
    assert response.status_code == 401
    psutil_mock.cpu_percent.assert_not_called()
    psutil_mock.virtual_memory.assert_not_called()


@pytest.mark.parametrize("csrf", [None, "wrong"])
def test_update_requires_csrf(client, csrf):
    client.headers.pop("X-CSRF-Token")
    if csrf is not None:
        client.headers["X-CSRF-Token"] = csrf
    assert client.put(SETTINGS_PATH, json={"show_system_resources": True}).status_code == 403
    assert client.get(SETTINGS_PATH).json() == {"show_system_resources": False}


@pytest.mark.parametrize("payload", [{}, {"show_system_resources": "true"},
                                     {"show_system_resources": 1},
                                     {"show_system_resources": None}])
def test_update_requires_boolean(client, payload):
    assert client.put(SETTINGS_PATH, json=payload).status_code == 422


def test_disabled_never_samples_including_warm_cache(client, psutil_mock):
    assert client.get(RESOURCES_PATH).status_code == 403
    psutil_mock.cpu_percent.assert_not_called()
    psutil_mock.virtual_memory.assert_not_called()
    assert client.put(SETTINGS_PATH, json={"show_system_resources": True}).status_code == 200
    assert client.get(RESOURCES_PATH).status_code == 200
    assert client.put(SETTINGS_PATH, json={"show_system_resources": False}).status_code == 200
    assert client.get(RESOURCES_PATH).status_code == 403
    psutil_mock.cpu_percent.assert_called_once_with(interval=0.1)
    psutil_mock.virtual_memory.assert_called_once_with()


def test_resources_values_scope_timestamp_and_cache(client, psutil_mock, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(system_resources, "monotonic", lambda: clock[0])
    client.put(SETTINGS_PATH, json={"show_system_resources": True})
    response = client.get(RESOURCES_PATH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "cpu_percent": 12.5, "memory_percent": 37.5,
        "memory_used_bytes": 3 * 1024**3, "memory_total_bytes": 8 * 1024**3,
        "scope": system_resources.RESOURCE_SCOPE, "sampled_at": body["sampled_at"],
        "error": None,
        "docker_available": False, "siteflow_cpu_percent": None,
        "siteflow_memory_percent": None, "siteflow_memory_used_bytes": None,
        "siteflow_container_count": 0, "docker_error": None,
    }
    assert datetime.fromisoformat(body["sampled_at"]).utcoffset().total_seconds() == 0
    assert "API 所在系统" in body["scope"]
    assert "不是 worker 或浏览器进程" in body["scope"]
    assert "不代表容器 cgroup 配额" in body["scope"]
    clock[0] += 4.9
    assert client.get(RESOURCES_PATH).json() == body
    psutil_mock.cpu_percent.assert_called_once_with(interval=0.1)
    clock[0] += 0.2
    psutil_mock.cpu_percent.return_value = 25.0
    assert client.get(RESOURCES_PATH).json()["cpu_percent"] == 25.0
    assert psutil_mock.cpu_percent.call_count == 2
    assert psutil_mock.virtual_memory.call_count == 2


@pytest.mark.parametrize("failed", ["cpu", "memory", "both", "import"])
def test_sampling_failures_are_nullable_sanitized_and_cached(
    client, psutil_mock, monkeypatch, failed,
):
    clock = [100.0]
    monkeypatch.setattr(system_resources, "monotonic", lambda: clock[0])
    private = "secret /private/path internal failure"
    if failed in {"cpu", "both"}:
        psutil_mock.cpu_percent.side_effect = RuntimeError(private)
    if failed in {"memory", "both"}:
        psutil_mock.virtual_memory.side_effect = RuntimeError(private)
    if failed == "import":
        monkeypatch.setitem(sys.modules, "psutil", None)
    client.put(SETTINGS_PATH, json={"show_system_resources": True})
    response = client.get(RESOURCES_PATH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["error"] == system_resources.RESOURCE_ERROR
    assert private not in response.text
    assert body["cpu_percent"] == (12.5 if failed == "memory" else None)
    for field in METRICS[1:]:
        assert (body[field] is None) == (failed != "cpu")
    assert client.get(RESOURCES_PATH).json() == body
    assert psutil_mock.cpu_percent.call_count == (0 if failed == "import" else 1)
    assert psutil_mock.virtual_memory.call_count == (0 if failed == "import" else 1)
    clock[0] += 5.1
    monkeypatch.setitem(sys.modules, "psutil", psutil_mock)
    psutil_mock.cpu_percent.side_effect = None
    psutil_mock.virtual_memory.side_effect = None
    recovered = client.get(RESOURCES_PATH).json()
    assert recovered["error"] is None
    assert all(recovered[field] is not None for field in METRICS)


def test_concurrent_cache_misses_share_one_sample(psutil_mock):
    sampler = system_resources.SystemResourceSampler()
    barrier = Barrier(8)
    started, release = Event(), Event()

    def blocking_cpu(*, interval):
        assert interval == 0.1
        started.set()
        assert release.wait(timeout=5)
        return 12.5

    psutil_mock.cpu_percent.side_effect = blocking_cpu

    def sample():
        barrier.wait(timeout=5)
        return sampler.sample()

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(sample) for _ in range(8)]
        try:
            assert started.wait(timeout=5)
            assert not any(future.done() for future in futures)
        finally:
            release.set()
        results = [future.result(timeout=5) for future in futures]
    assert all(result == results[0] for result in results)
    psutil_mock.cpu_percent.assert_called_once_with(interval=0.1)
    psutil_mock.virtual_memory.assert_called_once_with()


@pytest.mark.asyncio
async def test_sampling_does_not_block_event_loop(client, psutil_mock):
    started, release = Event(), Event()

    def blocking_cpu(*, interval):
        assert interval == 0.1
        started.set()
        assert release.wait(timeout=5)
        return 12.5

    psutil_mock.cpu_percent.side_effect = blocking_cpu
    client.put(SETTINGS_PATH, json={"show_system_resources": True})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client.app), base_url="http://testserver",
        cookies=client.cookies,
    ) as api:
        task = asyncio.create_task(api.get(RESOURCES_PATH))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            health = await asyncio.wait_for(api.get("/api/v1/health"), timeout=2)
            assert health.status_code == 200
            assert not task.done()
        finally:
            release.set()
            response = await task
        assert response.status_code == 200
