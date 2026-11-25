from __future__ import annotations

import asyncio
import json
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Lock
from time import monotonic
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from app.core.config import Settings
from app.services import system_resources as resources

PRIVATE = "secret /private/docker.sock token=do-not-leak"


def container(index=1, *, project="siteflow", service="api", **overrides):
    return {
        "Id": f"{index:064x}", "State": "running", "Names": ["/siteflow-api-1"],
        "Labels": {resources.PROJECT_LABEL: project, resources.SERVICE_LABEL: service},
        **overrides,
    }


def stats(*, cpu=20, usage=200, cache=50, version=2):
    return {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 100 + cpu},
            "system_cpu_usage": 2000, "online_cpus": 16,
        },
        "precpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 1000},
        "memory_stats": {
            "usage": usage, "limit": 250,
            "stats": {"inactive_file" if version == 2 else "total_inactive_file": cache},
        },
    }


def one_shot_stats(total, system, *, usage=200):
    sample = stats(usage=usage)
    sample["precpu_stats"] = {}
    sample["cpu_stats"] = {
        "cpu_usage": {
            "total_usage": total, "usage_in_kernelmode": 50, "usage_in_usermode": 50,
        },
        "system_cpu_usage": system, "online_cpus": 16,
        "throttling_data": {"periods": 0, "throttled_periods": 0, "throttled_time": 0},
    }
    return sample


@pytest.fixture
def host(monkeypatch):
    module = SimpleNamespace(
        cpu_percent=Mock(return_value=40.0),
        virtual_memory=Mock(return_value=SimpleNamespace(
            total=1000, available=400, used=123, percent=12.3,
        )),
    )
    monkeypatch.setitem(sys.modules, "psutil", module)
    return module


@pytest.fixture
def docker(monkeypatch, tmp_path, host):
    # A real socket path exercises Path.is_socket without contacting a daemon.
    path = tmp_path / "docker.sock"
    local_socket = socket.socket(socket.AF_UNIX)
    local_socket.bind(str(path))
    settings = Settings(_env_file=None, docker_socket_path=str(path), docker_project="siteflow")
    requests = []
    transport_options = []

    def install(handler):
        def transport(**kwargs):
            transport_options.append(kwargs)

            async def handle(request):
                requests.append(request)
                assert request.method == "GET"
                assert request.url.host == "docker"
                assert request.url.path == "/containers/json" or (
                    request.url.path.startswith("/containers/") and request.url.path.endswith("/stats")
                )
                result = handler(request)
                return await result if asyncio.iscoroutine(result) else result

            return httpx.MockTransport(handle)

        monkeypatch.setattr(resources.httpx, "AsyncHTTPTransport", transport)
        return resources.SystemResourceSampler(settings)

    yield SimpleNamespace(
        install=install, settings=settings, requests=requests, options=transport_options,
        path=path,
    )
    local_socket.close()


def respond(containers, samples=None):
    def handler(request):
        if request.url.path == "/containers/json":
            return httpx.Response(200, json=containers)
        index = int(request.url.path.split("/")[2], 16)
        value = samples[index] if samples is not None else stats()
        if isinstance(value, list):
            value = value.pop(0)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, httpx.Response):
            return value
        assert dict(request.url.params) == {"stream": "false", "one-shot": "true"}
        return httpx.Response(200, json=value)

    return handler


def assert_no_totals(result):
    assert result.siteflow_cpu_percent is None
    assert result.siteflow_memory_percent is None
    assert result.siteflow_memory_used_bytes is None


def assert_host(result):
    assert result.cpu_percent == 40
    assert result.memory_total_bytes == 1000
    assert result.memory_used_bytes == 600
    assert result.memory_percent == 60
    assert result.error is None


def test_settings_defaults_and_environment(monkeypatch):
    monkeypatch.delenv("SITEFLOW_DOCKER_SOCKET", raising=False)
    monkeypatch.delenv("SITEFLOW_DOCKER_PROJECT", raising=False)
    settings = Settings(_env_file=None)
    assert settings.docker_socket_path == "/var/run/docker.sock"
    assert settings.docker_project == "siteflow"
    monkeypatch.setenv("SITEFLOW_DOCKER_SOCKET", "/run/user/1000/docker.sock")
    monkeypatch.setenv("SITEFLOW_DOCKER_PROJECT", "custom")
    settings = Settings(_env_file=None)
    assert settings.docker_socket_path == "/run/user/1000/docker.sock"
    assert settings.docker_project == "custom"


@pytest.mark.parametrize("kind", ["missing", "file", "directory", "remote"])
def test_without_local_socket_only_samples_host(docker, kind, tmp_path):
    path = tmp_path / "not-a-socket"
    if kind == "file":
        path.write_text("not a socket")
    elif kind == "directory":
        path.mkdir()
    docker.settings.docker_socket_path = "tcp://remote:2375" if kind == "remote" else str(path)
    sampler = docker.install(lambda _: pytest.fail("Must not connect without a local socket"))
    result = sampler.sample()
    assert_host(result)
    assert not result.docker_available
    assert result.docker_error is None
    assert result.siteflow_container_count == 0
    assert_no_totals(result)
    assert docker.options == []


@pytest.mark.parametrize("project", ["", " \t"])
def test_empty_project_disables_docker_without_broad_query(docker, project):
    docker.settings.docker_project = project
    result = docker.install(lambda _: pytest.fail("Must not query an empty project")).sample()
    assert_host(result)
    assert not result.docker_available
    assert result.docker_error is None
    assert_no_totals(result)


def test_multiservice_host_normalization_and_safe_filters(docker):
    result = docker.install(respond(
        [container(1), container(2, service="worker")],
        {1: stats(cpu=100, version=2), 2: stats(cpu=200, version=1)},
    )).sample()
    assert_host(result)
    assert result.docker_available
    assert result.siteflow_container_count == 2
    # Docker's online_cpus multiplier would report 480%; host share is 30%.
    assert result.siteflow_cpu_percent == 30
    assert result.siteflow_memory_used_bytes == 300
    assert result.siteflow_memory_percent == 30
    assert result.docker_error is None
    filters = json.loads(docker.requests[0].url.params["filters"])
    assert filters == {
        "label": [f"{resources.PROJECT_LABEL}=siteflow", resources.SERVICE_LABEL],
        "status": ["running"],
    }
    assert dict(docker.requests[0].url.params).keys() == {"filters"}
    assert docker.options[0]["uds"] == str(docker.path)
    assert docker.options[0]["retries"] == 0
    assert docker.options[0]["limits"].max_connections == 8


def test_project_and_nonempty_service_rechecked_not_container_names(docker):
    containers = [
        container(1), container(2, project="unrelated"), container(3, project="siteflow-extra"),
        container(4, service=""), container(5, service="  "),
        container(6, Labels={}), container(7, Labels=None),
        container(8, State="exited"), container(9, service=None), container(10, service=123),
    ]
    result = docker.install(respond(containers)).sample()
    assert result.docker_available
    assert result.siteflow_container_count == 1
    assert len(docker.requests) == 2
    assert docker.requests[1].url.path == f"/containers/{1:064x}/stats"


def test_custom_project_and_duplicate_ids(docker):
    docker.settings.docker_project = "my-deployment"
    matching = container(1, project="my-deployment")
    result = docker.install(respond([matching, matching, container(2)])).sample()
    assert result.siteflow_container_count == 1
    assert result.siteflow_cpu_percent == 2
    assert len(docker.requests) == 2
    assert json.loads(docker.requests[0].url.params["filters"])["label"][0].endswith("=my-deployment")


@pytest.mark.parametrize("containers", [
    [], [container(project="other")], [container(service="")], [container(State="exited")],
])
def test_no_matching_services_is_unavailable_not_zero_usage(docker, containers):
    result = docker.install(respond(containers)).sample()
    assert_host(result)
    assert not result.docker_available
    assert result.siteflow_container_count == 0
    assert_no_totals(result)
    assert result.docker_error == "未找到当前 Compose 项目的运行中服务"
    assert len(docker.requests) == 1


@pytest.mark.parametrize("failure", [
    PermissionError(PRIVATE), httpx.ConnectError(PRIVATE), httpx.ReadTimeout(PRIVATE),
    RuntimeError(PRIVATE), httpx.Response(403, text=PRIVATE),
    httpx.Response(500, text=PRIVATE), httpx.Response(200, text=PRIVATE),
    httpx.Response(302, headers={"Location": "http://remote/private"}),
    httpx.Response(200, json={"message": PRIVATE}),
])
def test_docker_list_failures_are_sanitized_and_cached(docker, host, failure):
    def fail(_):
        if isinstance(failure, Exception):
            raise failure
        return failure

    sampler = docker.install(fail)
    result = sampler.sample()
    assert_host(result)
    assert not result.docker_available
    assert result.docker_error == resources.DOCKER_ERROR
    assert PRIVATE not in result.model_dump_json()
    assert_no_totals(result)
    assert sampler.sample() is result
    host.cpu_percent.assert_called_once_with(interval=0.1)
    assert len(docker.requests) == 1


def test_socket_permission_failure_is_sanitized(docker, monkeypatch):
    def fail(_):
        raise PermissionError(PRIVATE)

    monkeypatch.setattr(Path, "is_socket", fail)
    result = docker.install(lambda _: pytest.fail("Unexpected request")).sample()
    assert_host(result)
    assert not result.docker_available
    assert result.docker_error == resources.DOCKER_ERROR
    assert_no_totals(result)


@pytest.mark.parametrize("failure", [
    httpx.Response(404, text=PRIVATE), httpx.ConnectError(PRIVATE),
    httpx.Response(200, json=[]),
])
def test_partial_failure_or_disappeared_container_never_returns_subtotal(docker, failure):
    result = docker.install(respond(
        [container(1), container(2)], {1: stats(), 2: failure},
    )).sample()
    assert_host(result)
    assert result.siteflow_container_count == 2
    assert not result.docker_available
    assert result.docker_error == resources.DOCKER_ERROR
    assert_no_totals(result)
    assert PRIVATE not in result.model_dump_json()


@pytest.mark.parametrize("previous", [{}, None, {"system_cpu_usage": 0, "cpu_usage": {"total_usage": 0}}])
def test_unchanged_second_sample_nulls_full_cpu_sum_but_keeps_memory(docker, previous):
    partial = stats()
    partial["precpu_stats"] = previous
    result = docker.install(respond(
        [container(1), container(2)], {1: stats(), 2: partial},
    )).sample()
    assert result.docker_available
    assert result.siteflow_container_count == 2
    assert result.siteflow_cpu_percent is None
    assert result.siteflow_memory_used_bytes == 300
    assert result.siteflow_memory_percent == 30
    assert result.docker_error == resources.DOCKER_ERROR
    assert len(docker.requests) == 4  # list, one valid sample, two baseline-less samples


@pytest.mark.parametrize("previous", [
    {}, None, {"system_cpu_usage": 0, "cpu_usage": {"total_usage": 0}},
    {"system_cpu_usage": 1000, "cpu_usage": {"total_usage": -1}},
])
def test_one_shot_cpu_uses_two_current_window_samples(docker, monkeypatch, previous):
    first = one_shot_stats(1_000_000_000, 100_000_000_000, usage=200)
    first["precpu_stats"] = previous
    second = one_shot_stats(1_160_000_000, 101_600_000_000, usage=350)
    # Neither response has a usable previous collector sample; host share is 10%, not 160%.
    sleeps = []
    real_sleep = asyncio.sleep

    async def sleep(delay):
        sleeps.append(delay)
        await real_sleep(delay)

    monkeypatch.setattr(resources.asyncio, "sleep", sleep)
    result = docker.install(respond([container()], {1: [first, second]})).sample()
    assert_host(result)
    assert result.docker_available
    assert result.siteflow_cpu_percent == 10
    assert result.siteflow_memory_used_bytes == 300  # use the second response's memory
    assert result.docker_error is None
    assert len(docker.requests) == 3
    assert sleeps == [0.1]


def test_valid_precpu_uses_one_request_without_waiting(docker, monkeypatch):
    async def forbidden_sleep(_):
        pytest.fail("Valid precpu must not trigger a second sample")

    monkeypatch.setattr(resources.asyncio, "sleep", forbidden_sleep)
    result = docker.install(respond([container()])).sample()
    assert result.siteflow_cpu_percent == 2
    assert result.docker_error is None
    assert len(docker.requests) == 2


@pytest.mark.parametrize("current", [
    None, {}, {"cpu_usage": {"total_usage": -1}, "system_cpu_usage": 1000},
    {"cpu_usage": {"total_usage": "100"}, "system_cpu_usage": 1000},
    {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 0},
])
def test_invalid_first_cpu_does_not_retry_and_preserves_memory(docker, current):
    first = one_shot_stats(100, 1000)
    first["cpu_stats"] = current
    result = docker.install(respond([container()], {1: first})).sample()
    assert result.docker_available
    assert result.siteflow_cpu_percent is None
    assert result.siteflow_memory_used_bytes == 150
    assert result.docker_error == resources.DOCKER_ERROR
    assert len(docker.requests) == 2


@pytest.mark.parametrize("total,system", [
    (99, 2000), (200, 999), (200, 1000), (-1, 2000), (200, None),
])
def test_second_sample_reset_or_invalid_counters_null_cpu_without_retry(docker, total, system):
    first = one_shot_stats(100, 1000)
    second = one_shot_stats(total, system)
    result = docker.install(respond([container()], {1: [first, second]})).sample()
    assert_host(result)
    assert result.docker_available
    assert result.siteflow_cpu_percent is None
    assert result.siteflow_memory_used_bytes == 150
    assert result.docker_error == resources.DOCKER_ERROR
    assert len(docker.requests) == 3


def test_one_shot_baselines_are_not_reused_after_cache_expiry(docker, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(resources, "monotonic", lambda: clock[0])
    sampler = docker.install(respond([container()], {1: [
        one_shot_stats(100, 1000), one_shot_stats(200, 2000),
        one_shot_stats(8000, 10000), one_shot_stats(8020, 11000),
    ]}))
    first = sampler.sample()
    assert first.siteflow_cpu_percent == 10
    assert sampler.sample() is first
    assert len(docker.requests) == 3
    clock[0] += 5.1
    assert sampler.sample().siteflow_cpu_percent == 2
    assert len(docker.requests) == 6


@pytest.mark.parametrize("system,total", [(1000, 200), (900, 200), (2000, 99), (None, 200)])
def test_invalid_cpu_delta_does_not_fabricate_zero(docker, system, total):
    sample = stats()
    sample["cpu_stats"]["system_cpu_usage"] = system
    sample["cpu_stats"]["cpu_usage"]["total_usage"] = total
    result = docker.install(respond([container()], {1: sample})).sample()
    assert result.siteflow_cpu_percent is None
    assert result.siteflow_memory_used_bytes == 150
    assert result.docker_error == resources.DOCKER_ERROR


def test_valid_zero_cpu_delta_is_zero(docker):
    result = docker.install(respond([container()], {1: stats(cpu=0)})).sample()
    assert result.siteflow_cpu_percent == 0
    assert result.docker_error is None


@pytest.mark.parametrize("memory", [
    {}, None, {"usage": -1}, {"usage": 200, "stats": {"inactive_file": "bad"}},
    {"usage": 200, "stats": None}, {"usage": 200, "stats": []},
])
def test_missing_or_invalid_memory_does_not_return_memory_subtotal(docker, memory):
    partial = stats()
    partial["memory_stats"] = memory
    result = docker.install(respond(
        [container(1), container(2)], {1: stats(), 2: partial},
    )).sample()
    assert result.docker_available
    assert result.siteflow_cpu_percent == 4
    assert result.siteflow_memory_used_bytes is None
    assert result.siteflow_memory_percent is None
    assert result.docker_error == resources.DOCKER_ERROR


@pytest.mark.parametrize("cache_stats,expected", [
    ({"inactive_file": 40, "total_inactive_file": 90}, 110),
    ({"total_inactive_file": 90}, 110), ({"inactive_file": 40}, 160),
    ({}, 200), ({"inactive_file": 300}, 0),
])
def test_memory_cache_and_host_denominator(docker, cache_stats, expected):
    sample = stats()
    sample["memory_stats"]["stats"] = cache_stats
    result = docker.install(respond([container()], {1: sample})).sample()
    assert result.siteflow_memory_used_bytes == expected
    assert result.siteflow_memory_percent == expected / 1000 * 100


def test_sampling_window_clamp_preserves_host_values_and_explains_scope(docker):
    result = docker.install(respond(
        [container(1), container(2)],
        {1: stats(cpu=900, usage=900), 2: stats(cpu=900, usage=900)},
    )).sample()
    assert_host(result)
    assert result.siteflow_cpu_percent == result.cpu_percent
    assert result.siteflow_memory_used_bytes == result.memory_used_bytes
    assert result.siteflow_memory_percent == result.memory_percent
    assert "采样时间窗不同" in result.scope
    assert "不扩大 host 用量" in result.scope


@pytest.mark.parametrize("failed", ["cpu", "memory", "both"])
def test_host_failure_nulls_only_corresponding_docker_metric(docker, host, failed):
    if failed in {"cpu", "both"}:
        host.cpu_percent.side_effect = RuntimeError(PRIVATE)
    if failed in {"memory", "both"}:
        host.virtual_memory.side_effect = RuntimeError(PRIVATE)
    result = docker.install(respond([container()])).sample()
    assert result.error == resources.RESOURCE_ERROR
    assert result.docker_available
    assert result.docker_error is None
    assert result.siteflow_container_count == 1
    assert result.siteflow_cpu_percent == (2 if failed == "memory" else None)
    assert result.siteflow_memory_used_bytes == (150 if failed == "cpu" else None)
    assert result.siteflow_memory_percent == (15 if failed == "cpu" else None)


def test_container_limit_rejects_incomplete_totals_before_stats(docker):
    count = resources.DOCKER_CONTAINER_LIMIT + 1
    result = docker.install(respond([container(index + 1) for index in range(count)])).sample()
    assert_host(result)
    assert result.siteflow_container_count == count
    assert not result.docker_available
    assert result.docker_error == resources.DOCKER_ERROR
    assert_no_totals(result)
    assert len(docker.requests) == 1


@pytest.mark.parametrize("bad_id", ["../../other/inspect", "", "a" * 63, "g" * 64, None])
def test_invalid_ids_never_construct_additional_endpoints(docker, bad_id):
    result = docker.install(respond([container(Id=bad_id)])).sample()
    assert not result.docker_available
    assert result.docker_error == resources.DOCKER_ERROR
    assert len(docker.requests) == 1


def test_docker_cache_expiry_and_concurrent_cache_misses(docker, host, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(resources, "monotonic", lambda: clock[0])
    sampler = docker.install(respond([container()]))
    barrier = Barrier(8)

    def sample():
        barrier.wait(timeout=5)
        return sampler.sample()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: sample(), range(8)))
    assert all(result is results[0] for result in results)
    assert len(docker.requests) == 2
    clock[0] += 4.9
    assert sampler.sample() is results[0]
    clock[0] += 0.2
    assert sampler.sample() is not results[0]
    assert len(docker.requests) == 4
    assert host.cpu_percent.call_count == 2


def test_docker_failure_recovers_after_cache_expiry(docker, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(resources, "monotonic", lambda: clock[0])
    handler = respond([container()])
    failed = [True]

    def sometimes_fails(request):
        if failed[0]:
            raise httpx.ConnectError(PRIVATE)
        return handler(request)

    sampler = docker.install(sometimes_fails)
    failure = sampler.sample()
    failed[0] = False
    assert sampler.sample() is failure
    assert failure.docker_error == resources.DOCKER_ERROR
    clock[0] += 5.1
    recovered = sampler.sample()
    assert recovered.docker_available
    assert recovered.docker_error is None
    assert recovered.siteflow_cpu_percent == 2
    assert recovered.siteflow_memory_used_bytes == 150


@pytest.mark.parametrize("one_shot", [False, True])
def test_concurrency_is_bounded_and_all_containers_are_included(docker, one_shot):
    active = peak = 0
    count = resources.DOCKER_CONTAINER_LIMIT
    calls = {}

    async def handler(request):
        nonlocal active, peak
        if request.url.path == "/containers/json":
            return httpx.Response(200, json=[container(index + 1) for index in range(count)])
        active += 1
        peak = max(peak, active)
        path = request.url.path
        calls[path] = calls.get(path, 0) + 1
        try:
            await asyncio.sleep(0.005)
            if one_shot:
                sample = one_shot_stats(100 + calls[path], 1000 * calls[path], usage=1)
                sample["memory_stats"]["stats"] = {}
                return httpx.Response(200, json=sample)
            return httpx.Response(200, json=stats(cpu=1, usage=1, cache=0))
        finally:
            active -= 1

    result = docker.install(handler).sample()
    assert result.docker_available
    assert result.siteflow_container_count == count
    assert result.siteflow_cpu_percent == pytest.approx(count * 0.1)
    assert result.siteflow_memory_used_bytes == count
    assert peak == resources.DOCKER_CONCURRENCY == 8
    assert active == 0
    assert len(docker.requests) == 1 + count * (2 if one_shot else 1)


@pytest.mark.parametrize("stage", ["list", "stats"])
def test_total_deadline_cancels_all_pending_work_and_keeps_host(docker, monkeypatch, stage):
    monkeypatch.setattr(resources, "SAMPLE_BUDGET_SECONDS", 0.05)
    cancelled = 0

    async def handler(request):
        nonlocal cancelled
        if stage == "stats" and request.url.path == "/containers/json":
            return httpx.Response(200, json=[container(index + 1) for index in range(16)])
        try:
            await asyncio.sleep(10)
        finally:
            cancelled += 1

    sampler = docker.install(handler)
    start = monotonic()
    result = sampler.sample()
    assert monotonic() - start < 0.5
    assert_host(result)
    assert not result.docker_available
    assert_no_totals(result)
    assert result.docker_error == resources.DOCKER_ERROR
    assert result.siteflow_container_count == (16 if stage == "stats" else 0)
    assert cancelled == (8 if stage == "stats" else 1)


def test_host_sampling_counts_against_total_deadline(docker, monkeypatch):
    clock = iter([100.0, 105.1])
    monkeypatch.setattr(resources, "monotonic", lambda: next(clock, 106.0))

    async def handler(_):
        await asyncio.sleep(10)

    result = docker.install(handler).sample()
    assert_host(result)
    assert not result.docker_available
    assert result.docker_error == resources.DOCKER_ERROR
    assert_no_totals(result)


def test_total_deadline_applies_across_successful_request_waves(docker, monkeypatch):
    monkeypatch.setattr(resources, "SAMPLE_BUDGET_SECONDS", 0.08)
    monkeypatch.setattr(resources, "DOCKER_REQUEST_SECONDS", 0.06)

    async def handler(request):
        await asyncio.sleep(0.03)
        if request.url.path == "/containers/json":
            return httpx.Response(200, json=[container(index + 1) for index in range(16)])
        return httpx.Response(200, json=stats())

    start = monotonic()
    result = docker.install(handler).sample()
    assert monotonic() - start < 0.5
    assert_host(result)
    assert result.siteflow_container_count == 16
    assert not result.docker_available
    assert result.docker_error == resources.DOCKER_ERROR
    assert_no_totals(result)


def test_total_deadline_includes_one_shot_baseline_wait(docker, monkeypatch):
    monkeypatch.setattr(resources, "SAMPLE_BUDGET_SECONDS", 0.05)
    start = monotonic()
    result = docker.install(respond([container()], {1: one_shot_stats(100, 1000)})).sample()
    assert monotonic() - start < 0.5
    assert_host(result)
    assert not result.docker_available
    assert result.siteflow_container_count == 1
    assert result.docker_error == resources.DOCKER_ERROR
    assert_no_totals(result)
    assert len(docker.requests) == 2  # cancelled during the wait, before the second request


@pytest.mark.parametrize("failure", ["timeout", "disappeared"])
def test_second_one_shot_request_failure_keeps_host_without_subtotal(docker, monkeypatch, failure):
    monkeypatch.setattr(resources, "DOCKER_REQUEST_SECONDS", 0.03)
    calls = 0

    async def handler(request):
        nonlocal calls
        if request.url.path == "/containers/json":
            return httpx.Response(200, json=[container()])
        calls += 1
        if calls == 1:
            return httpx.Response(200, json=one_shot_stats(100, 1000))
        if failure == "timeout":
            await asyncio.sleep(10)
        return httpx.Response(404, text=PRIVATE)

    start = monotonic()
    result = docker.install(handler).sample()
    assert monotonic() - start < 0.5
    assert_host(result)
    assert not result.docker_available
    assert result.siteflow_container_count == 1
    assert result.docker_error == resources.DOCKER_ERROR
    assert_no_totals(result)
    assert calls == 2


@pytest.mark.parametrize("stage", ["list", "stats"])
def test_single_request_timeout_includes_slow_response_body(docker, monkeypatch, stage):
    assert resources.DOCKER_REQUEST_SECONDS < 2
    assert resources.SAMPLE_BUDGET_SECONDS == 5
    monkeypatch.setattr(resources, "DOCKER_REQUEST_SECONDS", 0.03)
    closed = []

    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"["
            await asyncio.sleep(10)
            yield b"]"

        async def aclose(self):
            closed.append(True)

    def handler(request):
        if stage == "stats" and request.url.path == "/containers/json":
            return httpx.Response(200, json=[container()])
        return httpx.Response(200, stream=SlowBody())

    sampler = docker.install(handler)
    start = monotonic()
    result = sampler.sample()
    assert monotonic() - start < 0.5
    assert closed
    assert_host(result)
    assert not result.docker_available
    assert result.docker_error == resources.DOCKER_ERROR
    assert_no_totals(result)


@pytest.mark.asyncio
async def test_docker_sampling_does_not_block_api_event_loop(client, docker):
    started = asyncio.Event()
    release_lock = Lock()
    release_lock.acquire()
    loop = asyncio.get_running_loop()

    async def handler(request):
        if request.url.path == "/containers/json":
            return httpx.Response(200, json=[container()])
        loop.call_soon_threadsafe(started.set)
        while release_lock.locked():
            await asyncio.sleep(0.005)
        return httpx.Response(200, json=stats())

    client.app.state.system_resource_sampler = docker.install(handler)
    client.put("/api/v1/system-settings", json={"show_system_resources": True})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client.app), base_url="http://testserver",
        cookies=client.cookies,
    ) as api:
        task = asyncio.create_task(api.get("/api/v1/system-resources"))
        try:
            await asyncio.wait_for(started.wait(), 2)
            health = await asyncio.wait_for(api.get("/api/v1/health"), 1)
            assert health.status_code == 200
            assert not task.done()
        finally:
            release_lock.release()
            response = await task
    assert response.status_code == 200
    assert response.json()["docker_available"]


def test_application_passes_docker_settings_to_sampler(client, settings):
    sampler = client.app.state.system_resource_sampler
    assert sampler._docker_socket == settings.docker_socket_path
    assert sampler._docker_project == settings.docker_project
