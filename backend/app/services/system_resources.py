from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from time import monotonic

import httpx

from app.core.config import Settings
from app.schemas.system import SystemResources

RESOURCE_SCOPE = (
    "API 所在系统的操作系统可见 CPU 和内存（Linux 为 OS 可见的系统视图）；"
    "不是 worker 或浏览器进程用量；容器部署时也不代表容器 cgroup 配额或配额使用率。"
    "host 内存已用按 total-available 计算。Docker 仅通过本机 Unix socket 读取同主机的"
    "指定 Compose 项目服务，SiteFlow 占比以 host 总资源为分母，是 host 已用资源的子集；"
    "采样时间窗不同，SiteFlow 展示值限幅至 host 已用值，不扩大 host 用量。"
)
RESOURCE_ERROR = "系统资源采集暂不可用"
DOCKER_ERROR = "Docker 资源采集暂不可用"
DOCKER_NO_SERVICES_ERROR = "未找到当前 Compose 项目的运行中服务"
CACHE_SECONDS = 5.0
SAMPLE_BUDGET_SECONDS = 5.0
DOCKER_REQUEST_SECONDS = 1.5
DOCKER_CONCURRENCY = 8
DOCKER_CONTAINER_LIMIT = 64
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"


def _counter(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("Invalid resource counter")
    return value


def _cpu_counters(stats: object) -> tuple[int, int] | None:
    try:
        total = _counter(stats["cpu_usage"]["total_usage"])
        system = _counter(stats["system_cpu_usage"])
        if system > 0:
            return total, system
    except (KeyError, TypeError, ValueError):
        pass
    return None


def _container_resources(stats: dict) -> tuple[float | None, int | None]:
    cpu = memory = None
    current = _cpu_counters(stats.get("cpu_stats"))
    previous = _cpu_counters(stats.get("precpu_stats"))
    try:
        if current is not None and previous is not None:
            cpu_delta = current[0] - previous[0]
            system_delta = current[1] - previous[1]
            if system_delta > 0 and cpu_delta >= 0:
                # system_cpu_usage spans all host cores; do not multiply by online_cpus.
                cpu = cpu_delta / system_delta * 100
    except OverflowError:
        pass
    try:
        values = stats["memory_stats"]
        usage = _counter(values["usage"])
        cache_stats = values.get("stats", {})
        if not isinstance(cache_stats, dict):
            raise ValueError("Invalid memory cache stats")
        # v1's hierarchical counter includes child cgroups; v2 only exposes inactive_file.
        cache = _counter(cache_stats.get("total_inactive_file", cache_stats.get("inactive_file", 0)))
        memory = max(0, usage - cache)
    except (KeyError, TypeError, ValueError):
        pass
    return cpu, memory


class SystemResourceSampler:
    def __init__(self, settings: Settings | None = None) -> None:
        self._lock = Lock()
        self._cached: SystemResources | None = None
        self._expires_at = 0.0
        self._docker_socket = settings.docker_socket_path if settings else "/var/run/docker.sock"
        self._docker_project = settings.docker_project if settings else "siteflow"

    async def _sample_docker(
        self, result: SystemResources, docker: dict, deadline: float,
    ) -> None:
        # This private event loop runs inside the sync sampler's worker thread. Cancellation
        # bounds the entire operation, including queued requests and slow response bodies.
        async with asyncio.timeout(max(0, deadline - monotonic())):
            transport = httpx.AsyncHTTPTransport(
                uds=self._docker_socket, retries=0,
                limits=httpx.Limits(max_connections=DOCKER_CONCURRENCY),
            )
            async with httpx.AsyncClient(
                transport=transport, base_url="http://docker", trust_env=False,
                follow_redirects=False, timeout=DOCKER_REQUEST_SECONDS,
            ) as client:
                async def get(path: str, params: dict) -> object:
                    async with asyncio.timeout(DOCKER_REQUEST_SECONDS):
                        response = await client.get(path, params=params)
                        response.raise_for_status()
                        return response.json()

                containers = await get("/containers/json", {"filters": json.dumps({
                    "label": [f"{PROJECT_LABEL}={self._docker_project}", SERVICE_LABEL],
                    "status": ["running"],
                })})
                if not isinstance(containers, list):
                    raise ValueError("Invalid container list")
                ids = set()
                for container in containers:
                    labels = container.get("Labels") or {}
                    service = labels.get(SERVICE_LABEL)
                    # Recheck labels: a daemon/proxy may ignore server-side filters.
                    if (labels.get(PROJECT_LABEL) != self._docker_project
                            or not isinstance(service, str) or not service.strip()):
                        continue
                    if container.get("State") != "running":
                        continue
                    container_id = container["Id"]
                    if (not isinstance(container_id, str)
                            or not re.fullmatch(r"[0-9a-f]{64}", container_id)):
                        raise ValueError("Invalid container ID")
                    ids.add(container_id)
                docker["siteflow_container_count"] = len(ids)
                if not ids:
                    # An empty match may mean the project is misconfigured, not zero usage.
                    docker["docker_error"] = DOCKER_NO_SERVICES_ERROR
                    return
                if len(ids) > DOCKER_CONTAINER_LIMIT:
                    raise ValueError("Too many containers for a complete sample")

                semaphore = asyncio.Semaphore(DOCKER_CONCURRENCY)

                async def sample_container(container_id: str) -> tuple[float | None, int | None]:
                    async with semaphore:
                        stats = await get(f"/containers/{container_id}/stats", {
                            "stream": "false", "one-shot": "true",
                        })
                        if not isinstance(stats, dict):
                            raise ValueError("Invalid container stats")
                        if (_cpu_counters(stats.get("precpu_stats")) is None
                                and _cpu_counters(stats.get("cpu_stats")) is not None):
                            # A fresh one-shot collector often has no precpu_stats. Use only
                            # this sample's counters, never the previous cached 5s window.
                            first_cpu = stats["cpu_stats"]
                            await asyncio.sleep(0.1)
                            stats = await get(f"/containers/{container_id}/stats", {
                                "stream": "false", "one-shot": "true",
                            })
                            if not isinstance(stats, dict):
                                raise ValueError("Invalid container stats")
                            stats["precpu_stats"] = first_cpu
                        return _container_resources(stats)

                # TaskGroup cancels siblings on failure: never return a successful subtotal.
                async with asyncio.TaskGroup() as group:
                    tasks = [group.create_task(sample_container(container_id)) for container_id in ids]
                values = [task.result() for task in tasks]

        docker["docker_available"] = True
        cpu_complete = all(cpu is not None for cpu, _ in values)
        memory_complete = all(memory is not None for _, memory in values)
        if not cpu_complete or not memory_complete:
            docker["docker_error"] = DOCKER_ERROR
        if cpu_complete and result.cpu_percent is not None:
            docker["siteflow_cpu_percent"] = min(result.cpu_percent, sum(cpu for cpu, _ in values))
        if (memory_complete and result.memory_used_bytes is not None
                and result.memory_total_bytes is not None):
            docker["siteflow_memory_used_bytes"] = min(
                result.memory_used_bytes, sum(memory for _, memory in values),
            )
            docker["siteflow_memory_percent"] = (
                docker["siteflow_memory_used_bytes"] / result.memory_total_bytes * 100
            )

    def sample(self) -> SystemResources:
        # Hold the lock through sampling so concurrent cache misses share one sample.
        with self._lock:
            if self._cached is not None and monotonic() < self._expires_at:
                return self._cached
            deadline = monotonic() + SAMPLE_BUDGET_SECONDS
            cpu = memory_percent = memory_used = memory_total = None
            error = None
            try:
                # Lazy import keeps a missing/broken dependency from breaking API startup.
                import psutil
            except Exception:
                error = RESOURCE_ERROR
            else:
                try:
                    cpu = psutil.cpu_percent(interval=0.1)
                except Exception:
                    error = RESOURCE_ERROR
                try:
                    memory = psutil.virtual_memory()
                    memory_total = _counter(memory.total)
                    if memory_total == 0:
                        raise ValueError("Invalid host memory total")
                    memory_used = max(0, min(memory_total, memory_total - _counter(memory.available)))
                    memory_percent = memory_used / memory_total * 100
                except Exception:
                    memory_percent = memory_used = memory_total = None
                    error = RESOURCE_ERROR
            result = SystemResources(
                cpu_percent=cpu,
                memory_percent=memory_percent,
                memory_used_bytes=memory_used,
                memory_total_bytes=memory_total,
                scope=RESOURCE_SCOPE,
                sampled_at=datetime.now(timezone.utc).isoformat(),
                error=error,
            )
            docker = {}
            try:
                if self._docker_project.strip() and Path(self._docker_socket).is_socket():
                    asyncio.run(self._sample_docker(result, docker, deadline))
            except Exception:
                docker.update({
                    "docker_available": False, "siteflow_cpu_percent": None,
                    "siteflow_memory_percent": None, "siteflow_memory_used_bytes": None,
                    "docker_error": DOCKER_ERROR,
                })
            self._cached = result.model_copy(update={
                **docker,
                "sampled_at": datetime.now(timezone.utc).isoformat(),
            })
            self._expires_at = monotonic() + CACHE_SECONDS
            return self._cached
