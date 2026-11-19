from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
import unicodedata
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
import websockets
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from worker.executor import execute_job
from worker.dispatch import DispatchBoundary, DispatchRegistry, router as dispatch_router
from worker.assistant_browser import (
    AssistantBrowser, CheckSelectorRequest, ClearCookiesRequest, CookieGenerationRequest, InspectRequest, NavigateRequest,
    ObserveRequest, OpenRequest, TokenRequest,
)
from worker.assistant_step import ExecuteRequest
from worker.browser_backend import (
    BROWSER_SETTINGS_WRITE_UNKNOWN, BrowserCapacityError, BrowserSession, BrowserSettingsError,
    ManagerBackend, active_profile_budget, create_backend,
    await_browser_cleanup, observe_cleanup,
)
from worker.plugin_broker import (
    BrokerActionError,
    BrokerDenied,
    BrokerError,
    BrokerExpired,
    BrokerNotFound,
    PluginBroker,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("siteflow.worker")

DATA_DIR = Path(os.environ.get("SITEFLOW_WORKER_DATA", "/app/data"))
PROXY_URL = os.environ.get("SITEFLOW_PROXY_URL", "").strip()
PLUGIN_BROKER_URL = os.environ.get(
    "SITEFLOW_PLUGIN_BROKER_URL", "http://worker:6300/plugin-broker"
).strip()

LANE_DEFINITIONS: list[dict[str, Any]] = [
    {"key": "general", "label": "普通通用", "proxy": False},
    {"key": "interaction", "label": "普通交互", "proxy": False},
    {"key": "proxy_general", "label": "代理通用", "proxy": True},
    {"key": "proxy_interaction", "label": "代理交互", "proxy": True},
]

LANE_CONCURRENCY = {
    "general": max(1, int(os.environ.get("SITEFLOW_LANE_GENERAL_CONCURRENCY", "5"))),
    "interaction": 1,
    "proxy_general": max(
        1, int(os.environ.get("SITEFLOW_LANE_PROXY_GENERAL_CONCURRENCY", "3"))
    ),
    "proxy_interaction": 1,
}


class LaneBusyError(RuntimeError):
    pass


class LaneDisabledError(RuntimeError):
    pass


class LaneStartError(RuntimeError):
    pass


class LaneCapacityError(LaneStartError):
    pass


SEAT_WAIT_SECONDS = 120
SEAT_RETRY_SECONDS = 2
SEAT_WAIT_MESSAGE = "Browser profile capacity unavailable; seat wait expired"


class LaneNotRunningError(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LaneExecutionLimiter:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.active = 0
        self.changed = asyncio.Event()

    def resize(self, limit: int) -> None:
        self.limit = limit
        self.changed.set()

    async def __aenter__(self):
        while self.active >= self.limit:
            self.changed.clear()
            await self.changed.wait()
        self.active += 1
        return self

    async def __aexit__(self, *_args) -> None:
        self.active -= 1
        self.changed.set()


@dataclass
class LaneRuntime:
    key: str
    label: str
    proxy: bool
    enabled: bool = True
    status: str = "stopped"  # stopped / starting / running / stopping / error / disabled
    browser: str = "stopped"
    error: str | None = None
    started_at: str | None = None
    session: BrowserSession | None = None
    context: Any = None
    page: Any = None
    task_pages: dict[str, Any] = field(default_factory=dict)
    leases: set[str] = field(default_factory=set)
    viewers: set[str] = field(default_factory=set)
    last_activity_at: float = field(default_factory=time.monotonic)
    idle_stop_seconds: int = 300
    max_concurrency: int = 1
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    cleanup_task: asyncio.Task | None = field(default=None, repr=False)
    execution_semaphore: LaneExecutionLimiter = field(
        default_factory=lambda: LaneExecutionLimiter(1), repr=False
    )

    @property
    def display(self) -> str | None:
        return self.session.display if self.session else None

    @property
    def in_use(self) -> int:
        return len(self.leases)

    @property
    def viewer_count(self) -> int:
        return len(self.viewers)

    def idle_remaining(self) -> int | None:
        if self.status != "running" or self.in_use or self.viewers or self.idle_stop_seconds <= 0:
            return None
        return max(0, int(round(self.idle_stop_seconds - (time.monotonic() - self.last_activity_at))))

    def view(self) -> dict[str, Any]:
        active = self.status in ("starting", "running", "stopping")
        return {
            "key": self.key,
            "label": self.label,
            "enabled": self.enabled,
            "proxy": self.proxy,
            "status": self.status,
            "browser": self.browser,
            "display": self.display if active else None,
            "profile": f"profiles/{self.key}",
            "vnc_available": self.status == "running" and self.session is not None and not self.session.disconnected,
            "in_use": self.in_use,
            "task_pages": len(self.task_pages),
            "browser_pages": len(self.context.pages) if self.context is not None else 0,
            "max_concurrency": self.max_concurrency,
            "viewers": self.viewer_count,
            "idle_stop_seconds": self.idle_stop_seconds,
            "idle_remaining": self.idle_remaining(),
            "can_start": self.enabled and self.status in ("stopped", "error") and self.in_use == 0,
            "can_stop": self.status == "running" and not (self.in_use or self.viewers or self.task_pages),
            "error": self.error,
            "started_at": self.started_at,
        }


class LaneManager:
    def __init__(self) -> None:
        self._profile_budget = active_profile_budget()
        self.backend = create_backend(DATA_DIR)
        self.lanes: dict[str, LaneRuntime] = {}
        for definition in LANE_DEFINITIONS:
            lane = LaneRuntime(
                key=definition["key"],
                label=definition["label"],
                proxy=bool(definition["proxy"]),
                max_concurrency=LANE_CONCURRENCY.get(definition["key"], 1),
            )
            lane.execution_semaphore.resize(lane.max_concurrency)
            if lane.proxy and not PROXY_URL:
                lane.enabled = False
                lane.status = "disabled"
                lane.browser = "disabled"
                lane.error = "SITEFLOW_PROXY_URL 未配置"
            self.lanes[definition["key"]] = lane
        self.started_at = _now_iso()
        self.idle_stop_seconds = max(
            0, int(os.environ.get("SITEFLOW_LANE_IDLE_STOP_SECONDS", "300"))
        )
        for lane in self.lanes.values():
            lane.idle_stop_seconds = self.idle_stop_seconds
        self._idle_task: asyncio.Task | None = None
        self._admission_lock = asyncio.Lock()
        self._seat_changed = asyncio.Event()
        self._seat_waiters: dict[object, str] = {}
        self._stopping = False
        self._remote_retry_at = 0.0
        self._capacity_pressure = False
        self.dispatch = DispatchRegistry(self)

    async def start(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        await self.backend.prepare()
        self.dispatch.start()
        # Lanes are deliberately not started here. They are started by the UI
        # or leased by a task runner.
        self._idle_task = asyncio.create_task(self._idle_monitor(), name="lane-idle-monitor")

    async def stop(self) -> None:
        from worker.page_lifecycle import (
            SHUTDOWN_CANCEL_TIMEOUT_SECONDS, SHUTDOWN_TIMEOUT_SECONDS, shutdown_job_cleanup,
        )

        self._stopping = True
        self._seat_changed.set()

        async def shutdown():
            try:
                await self.dispatch.close()
                if self._idle_task is not None:
                    self._idle_task.cancel()
                    try:
                        await self._idle_task
                    except asyncio.CancelledError:
                        pass
                    self._idle_task = None
                await shutdown_job_cleanup(self)
            finally:
                try:
                    for lane in self.lanes.values():
                        async with self._admission_lock, lane.lock:
                            retained = {owner._cleanup_args[1] for owner in getattr(self, "_job_cleanup_owners", ())
                                        if owner.lane is lane and not owner._released}
                            try:
                                await self._stop_lane_locked(lane, force=True)
                            except LaneStartError:
                                logger.warning("shutdown stop unconfirmed for %s", lane.key)
                            lane.leases.update(retained)
                finally:
                    try:
                        await shutdown_job_cleanup(self, finalize=True)
                    finally:
                        await self.dispatch.close()
                        async with asyncio.timeout(20):
                            await self.backend.close()

        if getattr(self, "_stop_task", None) is None:
            self._stop_task = asyncio.create_task(shutdown())
        try:
            await asyncio.shield(self._stop_task)
        except asyncio.CancelledError:
            # Keep the same shutdown owner alive through repeated caller cancellation.
            deadline = asyncio.get_running_loop().time() + (
                20 * (len(self.lanes) + 1)
                + SHUTDOWN_TIMEOUT_SECONDS + 2 * SHUTDOWN_CANCEL_TIMEOUT_SECONDS
            )
            while not self._stop_task.done():
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait({self._stop_task}, timeout=remaining)
                except asyncio.CancelledError:
                    continue
            if self._stop_task.done() and not self._stop_task.cancelled():
                self._stop_task.exception()
            else:
                self._stop_task.add_done_callback(
                    lambda task: None if task.cancelled() else task.exception())
            raise

    def update_settings(
        self,
        *,
        idle_stop_seconds: int,
        general_concurrency: int,
        proxy_concurrency: int,
    ) -> dict[str, Any]:
        if idle_stop_seconds < 0 or idle_stop_seconds > 86400:
            raise HTTPException(status_code=422, detail="空闲关闭时间必须在 0 到 86400 秒之间")
        if not 1 <= general_concurrency <= 10 or not 1 <= proxy_concurrency <= 10:
            raise HTTPException(status_code=422, detail="车道并行任务数必须在 1 到 10 之间")
        changed = idle_stop_seconds != self.idle_stop_seconds
        self.idle_stop_seconds = idle_stop_seconds
        now = time.monotonic()
        for lane in self.lanes.values():
            lane.idle_stop_seconds = idle_stop_seconds
            if changed:
                lane.last_activity_at = now
        self._set_concurrency("general", general_concurrency)
        self._set_concurrency("proxy_general", proxy_concurrency)
        return self.status()

    def _set_concurrency(self, key: str, value: int) -> None:
        lane = self.lanes.get(key)
        if lane is None or lane.max_concurrency == value:
            return
        lane.max_concurrency = value
        lane.execution_semaphore.resize(value)

    async def _idle_monitor(self) -> None:
        while True:
            await asyncio.sleep(1)
            for lane in list(self.lanes.values()):
                if lane.session is not None and lane.session.disconnected:
                    async with self._admission_lock, lane.lock:
                        if lane.session is None or not lane.session.disconnected:
                            continue
                        lane.status = "error"
                        lane.browser = "error"
                        lane.error = "Browser backend disconnected"
                        if not (lane.in_use or lane.viewers or lane.task_pages):
                            try:
                                await self._stop_lane_locked(lane)
                            except Exception:
                                logger.warning("disconnected stop failed for %s; will retry", lane.key)
                    continue
                if self.idle_stop_seconds <= 0:
                    continue
                if lane.status != "running" or lane.in_use or lane.viewers or lane.task_pages:
                    continue
                if time.monotonic() - lane.last_activity_at < lane.idle_stop_seconds:
                    continue
                async with self._admission_lock, lane.lock:
                    if (
                        lane.status != "running"
                        or lane.in_use
                        or lane.viewers
                        or lane.task_pages
                        or time.monotonic() - lane.last_activity_at < lane.idle_stop_seconds
                    ):
                        continue
                    logger.info("auto-stopping idle lane %s after %ss", lane.key, lane.idle_stop_seconds)
                    try:
                        await self._stop_lane_locked(lane)
                    except Exception:
                        logger.warning("idle stop failed for %s; lane remains in error", lane.key)

    def get(self, key: str) -> LaneRuntime:
        lane = self.lanes.get(key)
        if lane is None:
            raise HTTPException(status_code=404, detail=f"未知车道: {key}")
        return lane

    async def start_lane(self, key: str) -> dict[str, Any]:
        _, view = await self._admit_lane(key, lease=False, wait=False)
        return view

    async def _admit_lane(
        self, key: str, *, lease: bool, wait: bool, verify: bool = False,
    ) -> tuple[str | None, dict[str, Any]]:
        lane = self.get(key)
        if not lane.enabled:
            raise LaneDisabledError(lane.error or "Lane disabled")
        ticket = object()
        self._seat_waiters[ticket] = key
        order = {definition["key"]: index for index, definition in enumerate(LANE_DEFINITIONS)}
        try:
            async with asyncio.timeout(SEAT_WAIT_SECONDS):
                capacity = None
                while True:
                    if self._stopping:
                        raise LaneCapacityError("Browser worker is stopping")
                    if capacity is None and (lane.status != "running" or (
                            lane.session is not None and lane.session.disconnected)):
                        capacity_method = getattr(self.backend, "capacity", None)
                        # A cold status read must not block other running-lane reuse.
                        capacity = self._profile_budget
                        if capacity_method is not None:
                            capacity = min(capacity, await capacity_method())
                    async with self._admission_lock:
                        if self._stopping:
                            raise LaneCapacityError("Browser worker is stopping")
                        async with lane.lock:
                            if lane.session is not None and lane.session.disconnected:
                                if lane.in_use or lane.viewers or lane.task_pages:
                                    raise LaneStartError("Browser backend disconnected; lane still in use")
                                lane.status = "error"
                            reusable = lane.status == "running"
                            if not reusable and (lane.in_use or lane.viewers or lane.task_pages):
                                raise LaneStartError("Browser lane still in use; cannot restart")
                        if not reusable and capacity is None:
                            continue  # Lane stopped while this request was acquiring admission.
                        first = min(self._seat_waiters.values(), key=order.__getitem__)
                        if reusable or (key == first and time.monotonic() >= self._remote_retry_at):
                            if not reusable:
                                # Count quarantined launches as well as live/transitioning sessions.
                                uncertain = getattr(self.backend, "uncertain_profiles", {})
                                for owned_key, session in uncertain.items():
                                    if owned_key in self.lanes and self.lanes[owned_key].session is None:
                                        self.lanes[owned_key].session = session
                                def occupied():
                                    return sum(
                                        value.session is not None or value.status in ("starting", "running", "stopping")
                                        for value in self.lanes.values()
                                    )
                                for other in self.lanes.values():
                                    if (other is lane or (not self._capacity_pressure
                                            and occupied() - int(lane.session is not None) < capacity)):
                                        continue
                                    async with other.lock:
                                        if (other.session is None or other.in_use or other.viewers
                                                or other.task_pages):
                                            continue
                                        try:
                                            await self._stop_lane_locked(other)
                                            self._capacity_pressure = False
                                        except LaneStartError:
                                            pass  # Unconfirmed closure continues to consume a seat.
                                # Replacing our own failed session does not require another seat.
                                available = occupied() - int(lane.session is not None) < capacity
                            else:
                                available = True
                            if available:
                                async with lane.lock:
                                    try:
                                        if not reusable:
                                            await self._start_lane_locked(lane)
                                            self._capacity_pressure = False
                                        if self._stopping:
                                            raise LaneCapacityError("Browser worker is stopping")
                                        if verify:
                                            if lane.session is None:
                                                raise LaneStartError("Browser session unavailable")
                                            browser = getattr(lane.session, "browser", None)
                                            connected = getattr(browser, "is_connected", None)
                                            if connected is not None and not connected():
                                                raise LaneStartError("Browser backend disconnected")
                                            if lane.context is None or not lane.context.pages:
                                                raise LaneStartError("Browser context unavailable")
                                            await lane.context.pages[0].evaluate("() => 1")
                                        if self._stopping:
                                            raise LaneCapacityError("Browser worker is stopping")
                                        lease_id = uuid.uuid4().hex if lease else None
                                        if lease_id is not None:
                                            lane.leases.add(lease_id)
                                        lane.last_activity_at = time.monotonic()
                                        return lease_id, lane.view()
                                    except LaneCapacityError:
                                        capacity = None
                                        self._capacity_pressure = True
                                        if not wait or self._stopping:
                                            raise
                                        self._remote_retry_at = time.monotonic() + SEAT_RETRY_SECONDS
                        if not wait:
                            raise LaneCapacityError("Browser profile capacity unavailable")
                        self._seat_changed.clear()
                    # Never hold admission or a lane lock while waiting for users to drain.
                    delay = max(SEAT_RETRY_SECONDS, self._remote_retry_at - time.monotonic())
                    try:
                        await asyncio.wait_for(self._seat_changed.wait(), delay)
                    except TimeoutError:
                        pass
        except TimeoutError:
            raise LaneCapacityError(SEAT_WAIT_MESSAGE) from None
        finally:
            self._seat_waiters.pop(ticket, None)
            self._seat_changed.set()

    async def preflight(self, lanes: list[str]) -> dict[str, Any]:
        required = [definition["key"] for definition in LANE_DEFINITIONS if definition["key"] in lanes]
        supported = isinstance(self.backend, ManagerBackend)
        flags = []
        for definition in LANE_DEFINITIONS:
            lane = self.lanes.get(definition["key"])
            eligible = bool(lane is not None and lane.enabled and (
                (lane.status == "running" and lane.session is not None and not lane.session.disconnected)
                or (lane.status in ("stopped", "error")
                    and not lane.in_use and not lane.viewers and not lane.task_pages)
            ))
            flags.append({"key": definition["key"], "ready": eligible and supported})
        ready = False
        reason = "unsupported_backend" if not supported else "browser_unavailable"
        if (supported and required and not self._stopping
                and all(item["ready"] for item in flags if item["key"] in required)):
            try:
                generation = self.backend.settings_generation
                if await self.backend.execution_ready():
                    await self._admit_lane(required[0], lease=False, wait=False, verify=True)
                    ready = (
                        self.backend.settings_generation == generation
                        and not self.backend.settings_lock.locked()
                        and not self.backend.settings_write_unknown
                    )
            except LaneCapacityError:
                reason = "browser_capacity_unavailable"
            except (LaneStartError, LaneDisabledError, TimeoutError):
                pass
            except Exception:
                logger.warning("browser preflight failed")
        if not ready:
            for item in flags:
                if item["key"] in required:
                    item["ready"] = False
        return {"backend": "cloak-manager" if supported else "unsupported", "ready": ready,
                "lanes": flags, "first_lane": required[0] if ready else None,
                "reason": None if ready else reason}

    async def stop_lane(self, key: str) -> dict[str, Any]:
        lane = self.get(key)
        async with self._admission_lock, lane.lock:
            if lane.in_use or lane.viewers or lane.task_pages:
                raise LaneBusyError(f"车道 {key} 正在执行任务，禁止关闭")
            if lane.status in ("stopped", "disabled") and lane.session is None:
                return lane.view()
            await self._stop_lane_locked(lane)
            return lane.view()

    async def lease_lane(self, key: str) -> tuple[str, dict[str, Any]]:
        """Task runners call this before using a lane. It auto-starts the lane."""
        return await self._admit_lane(key, lease=True, wait=True)

    async def release_lane(self, key: str, lease_id: str) -> dict[str, Any]:
        lane = self.get(key)
        async with self._admission_lock, lane.lock:
            lane.leases.discard(lease_id)
            lane.last_activity_at = time.monotonic()
            self._seat_changed.set()
            return lane.view()

    async def viewer_acquire(self, key: str, viewer_id: str) -> dict[str, Any]:
        lane = self.get(key)
        async with self._admission_lock, lane.lock:
            if self._stopping:
                raise LaneNotRunningError("Browser worker is stopping")
            if lane.status != "running":
                raise LaneNotRunningError(f"车道 {key} 未运行")
            lane.viewers.add(viewer_id)
            lane.last_activity_at = time.monotonic()
            return lane.view()

    async def viewer_release(self, key: str, viewer_id: str) -> dict[str, Any]:
        lane = self.get(key)
        async with self._admission_lock, lane.lock:
            lane.viewers.discard(viewer_id)
            lane.last_activity_at = time.monotonic()
            self._seat_changed.set()
            return lane.view()

    async def get_clipboard(self, key: str) -> str:
        lane = self.get(key)
        viewer_id = uuid.uuid4().hex
        await self.viewer_acquire(key, viewer_id)
        try:
            session = lane.session
            if session is None or session.disconnected:
                raise LaneNotRunningError("Lane is not running")
            # Manager may return a stale page cache before consulting X11.
            getter = getattr(session.desktop, "read_clipboard", None)
            text = await getter() if callable(getter) else await self.backend.get_clipboard(session)
            if not isinstance(text, str) or len(text) > 1_048_576:
                raise RuntimeError("Clipboard unavailable")
            text.encode("utf-8")
            return text
        finally:
            await self.viewer_release(key, viewer_id)

    async def set_clipboard(self, key: str, text: str) -> None:
        lane = self.get(key)
        viewer_id = uuid.uuid4().hex
        await self.viewer_acquire(key, viewer_id)
        try:
            session = lane.session
            if session is None or session.disconnected:
                raise LaneNotRunningError("Lane is not running")
            await self.backend.set_clipboard(session, text)
        finally:
            await self.viewer_release(key, viewer_id)

    async def focus_task_page(self, key: str, job_id: str) -> dict[str, Any]:
        lane = self.get(key)
        page = lane.task_pages.get(str(job_id))
        if page is None:
            raise LaneNotRunningError("任务标签页不存在或已关闭")
        try:
            await page.bring_to_front()
            await page.evaluate("() => window.focus()")
        except Exception as exc:
            raise LaneNotRunningError(
                f"任务标签页已失效: {type(exc).__name__}: {exc}"
            ) from exc
        lane.last_activity_at = time.monotonic()
        return lane.view()

    def status(self) -> dict[str, Any]:
        return {
            "broker_sessions": len(self.plugin_broker._sessions) if hasattr(self, "plugin_broker") else 0,
            "ok": all(
                lane.status in ("stopped", "disabled", "running")
                for lane in self.lanes.values()
            ),
            "proxy_configured": bool(PROXY_URL),
            "started_at": self.started_at,
            "lanes": [lane.view() for lane in self.lanes.values()],
        }

    async def _start_lane_locked(self, lane: LaneRuntime) -> None:
        if self._stopping:
            raise LaneCapacityError("Browser worker is stopping")
        if lane.status == "running":
            return
        try:
            await self._cleanup_locked(lane, strict=True)
        except Exception:
            raise LaneStartError("Browser backend stop failed") from None
        if self._stopping:
            raise LaneCapacityError("Browser worker is stopping")
        lane.status = "starting"
        lane.browser = "starting"
        lane.error = None
        lane.started_at = _now_iso()
        try:
            await self._start_browser(lane)
            lane.status = "running"
            lane.browser = "running"
            logger.info("lane %s connected display=%s", lane.key, lane.display)
        except BaseException as exc:
            uncertain = getattr(self.backend, "uncertain_profiles", {})
            if lane.session is None:
                lane.session = uncertain.get(lane.key)
            lane.error = ("Browser profile capacity unavailable" if isinstance(exc, BrowserCapacityError)
                          else f"{type(exc).__name__}: {exc}")
            cleanup = asyncio.create_task(self._cleanup_locked(lane))
            cancelled = isinstance(exc, asyncio.CancelledError)
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            lane.status = "error"
            lane.browser = "error"
            self._seat_changed.set()
            if cancelled:
                raise asyncio.CancelledError
            if isinstance(exc, BrowserCapacityError):
                raise LaneCapacityError(lane.error) from exc
            logger.exception("lane %s failed to start", lane.key)
            raise LaneStartError(lane.error) from exc

    async def _stop_lane_locked(self, lane: LaneRuntime, *, force: bool = False) -> None:
        if lane.status in ("stopped", "disabled") and lane.session is None:
            return
        if (lane.in_use or lane.viewers or lane.task_pages) and not force:
            raise LaneBusyError(f"车道 {lane.key} 正在执行任务，禁止关闭")
        lane.status = "stopping"
        lane.browser = "stopping"
        try:
            await self._cleanup_locked(lane, strict=True)
        except BaseException as exc:
            lane.status = "error"
            lane.browser = "error"
            lane.error = "Browser backend stop failed"
            self._seat_changed.set()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise LaneStartError(lane.error) from None
        lane.status = "stopped"
        lane.browser = "stopped"
        lane.started_at = None
        lane.error = None
        lane.leases.clear()
        lane.viewers.clear()
        self._seat_changed.set()
        logger.info("lane %s stopped", lane.key)

    async def _cleanup_locked(self, lane: LaneRuntime, *, strict: bool = False) -> None:
        if lane.session is not None:
            if lane.cleanup_task is None or lane.cleanup_task.done():
                lane.cleanup_task = asyncio.create_task(self.backend.stop(lane.session))
                lane.cleanup_task.add_done_callback(observe_cleanup)
            try:
                await await_browser_cleanup(lane.cleanup_task)
            except Exception:
                logger.warning("failed to stop browser backend for %s", lane.key)
                if strict:
                    raise
                return  # Never discard ownership after an unconfirmed stop.
            finally:
                if lane.cleanup_task.done():
                    lane.cleanup_task = None
            lane.session = None
        lane.context = None
        lane.page = None

    async def _start_browser(self, lane: LaneRuntime) -> None:
        lane.session = await self.backend.start(lane.key, PROXY_URL if lane.proxy else None)
        context = lane.session.context
        lane.context = context
        page = context.pages[0] if context.pages else await context.new_page()
        lane.page = page
        html = f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
html,body{{margin:0;height:100%;font-family:system-ui,sans-serif;background:#101820;color:#e8f1f8}}
body{{display:grid;place-items:center}}
.card{{text-align:center;padding:48px 64px;border:1px solid #2b3d4f;border-radius:20px;background:#17222d}}
h1{{margin:0 0 10px;font-size:30px}}p{{margin:0;color:#8fa6b8}}code{{color:#6cc7ff}}
</style></head><body><div class="card">
<h1>SiteFlow · {lane.label}</h1>
<p>lane <code>{lane.key}</code> · display <code>{lane.display}</code> · profile <code>{lane.key}</code></p>
</div></body></html>"""
        await page.goto("about:blank", wait_until="domcontentloaded", timeout=10000)
        await page.evaluate(
            "(html) => { document.open(); document.write(html); document.close(); }",
            html,
        )
        await page.wait_for_timeout(300)


class LeaseRequest(BaseModel):
    lease_id: str | None = None


class ViewerRequest(BaseModel):
    viewer_id: str


class JobFocusRequest(BaseModel):
    job_id: str


class IdleSettings(BaseModel):
    idle_stop_seconds: int
    general_concurrency: int = 5
    proxy_concurrency: int = 3


ACTIVE_EXECUTIONS: dict[str, asyncio.Event] = {}
PICKER_TTL_SECONDS = max(60, int(os.environ.get("SITEFLOW_PICKER_TTL_SECONDS", "900")))
PICKER_SESSIONS: dict[str, "PickerSession"] = {}


PICKER_SCRIPT = r"""
(() => {
  if (window.__siteflowPicker) return;
  const escape = (value) => (window.CSS && CSS.escape ? CSS.escape(String(value)) : String(value));
  const attr = (name, value) => `[${name}="${String(value).replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"]`;
  const unique = (selector) => {
    try { return document.querySelectorAll(selector).length === 1; } catch { return false; }
  };
  const selectorFor = (el) => {
    if (el.id && unique('#' + escape(el.id))) return '#' + escape(el.id);
    for (const name of ['data-testid', 'data-test', 'name', 'aria-label']) {
      const value = el.getAttribute(name);
      if (!value) continue;
      const selector = el.tagName.toLowerCase() + attr(name, value);
      if (unique(selector)) return selector;
    }
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && node !== document.documentElement) {
      if (node.id && unique('#' + escape(node.id))) {
        parts.unshift('#' + escape(node.id));
        break;
      }
      let part = node.tagName.toLowerCase();
      const classes = Array.from(node.classList)
        .filter((name) => name && name.length < 40 && !name.startsWith('__siteflow'))
        .slice(0, 2);
      if (classes.length) part += '.' + classes.map(escape).join('.');
      const parent = node.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter((item) => item.tagName === node.tagName);
        if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
      }
      parts.unshift(part);
      node = parent;
    }
    return parts.join(' > ');
  };
  let highlighted = null;
  const clear = () => {
    if (highlighted) highlighted.classList.remove('__siteflow_pick_hover');
    highlighted = null;
  };
  const onMove = (event) => {
    if (!event.altKey) { clear(); return; }
    const el = event.target;
    if (!(el instanceof Element)) return;
    if (el !== highlighted) {
      clear();
      highlighted = el;
      el.classList.add('__siteflow_pick_hover');
    }
  };
  const onClick = (event) => {
    if (!event.altKey) return;
    const el = event.target;
    if (!(el instanceof Element)) return;
    event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation();
    window.__siteflowPicker.picked = {
      selector: selectorFor(el),
      tag: el.tagName.toLowerCase(),
      text: String(el.innerText || el.value || '').trim().slice(0, 200),
      id: el.id || '',
      name: el.getAttribute('name') || '',
      url: location.href,
    };
    clear();
  };
  window.__siteflowPicker = { picked: null };
  document.addEventListener('mousemove', onMove, true);
  document.addEventListener('click', onClick, true);
  document.addEventListener('scroll', clear, true);
  const style = document.createElement('style');
  style.textContent = '.__siteflow_pick_hover{outline:2px solid #2f7ed8 !important;outline-offset:1px !important;}';
  document.documentElement.appendChild(style);
})();
"""


@dataclass
class PickerSession:
    session_id: str
    lane_key: str
    lease_id: str
    page: Any
    url: str
    created_at: float
    limiter: Any = None
    acquired_slot: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def expired(self) -> bool:
        return time.monotonic() - self.created_at > PICKER_TTL_SECONDS

    def view(self, picked: Any = None) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "lane_key": self.lane_key,
            "url": self.url,
            "current_url": self.page.url,
            "picked": picked,
            "expires_in": max(0, int(PICKER_TTL_SECONDS - (time.monotonic() - self.created_at))),
        }


async def _inject_picker(page: Any) -> None:
    try:
        await page.evaluate(PICKER_SCRIPT)
    except Exception:
        pass


async def _open_picker(manager: LaneManager, url: str, lane_key: str = "interaction") -> dict[str, Any]:
    lane = manager.get(lane_key)
    lease_id = None
    page = None
    acquired_slot = False
    provisioning = None
    try:
        async def lease() -> None:
            nonlocal lease_id
            lease_id, _ = await manager.lease_lane(lane_key)

        provisioning = asyncio.create_task(lease())
        await asyncio.shield(provisioning)

        async def acquire() -> None:
            nonlocal acquired_slot
            await lane.execution_semaphore.__aenter__()
            acquired_slot = True

        try:
            await asyncio.wait_for(acquire(), 2)
        except TimeoutError:
            raise HTTPException(409, "Browser lane busy") from None
        if lane.context is None:
            raise RuntimeError("车道没有可用的浏览器上下文")

        async def new_page() -> None:
            nonlocal page
            page = await lane.context.new_page()

        provisioning = asyncio.create_task(new_page())
        await asyncio.shield(provisioning)
        await page.bring_to_front()
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await _inject_picker(page)

        def reinject(_page: Any) -> None:
            asyncio.create_task(_inject_picker(page))

        page.on("load", reinject)
        session_id = uuid.uuid4().hex
        session = PickerSession(
            session_id=session_id,
            lane_key=lane_key,
            lease_id=lease_id,
            page=page,
            url=url,
            created_at=time.monotonic(),
            limiter=lane.execution_semaphore,
            acquired_slot=acquired_slot,
        )
        PICKER_SESSIONS[session_id] = session
        return session.view()
    except BaseException:
        async def cleanup() -> None:
            try:
                if provisioning is not None:
                    await asyncio.gather(provisioning, return_exceptions=True)
                if page is not None:
                    try:
                        await asyncio.wait_for(page.close(), 5)
                    except BaseException:
                        pass
            finally:
                try:
                    if lease_id is not None:
                        await manager.release_lane(lane_key, lease_id)
                finally:
                    if acquired_slot:
                        await lane.execution_semaphore.__aexit__(None, None, None)

        cleanup_task = asyncio.create_task(cleanup())
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            await cleanup_task
        raise


async def _close_picker(manager: LaneManager, session_id: str) -> None:
    session = PICKER_SESSIONS.pop(session_id, None)
    if session is None:
        return

    async def cleanup() -> None:
        async with session.lock:
            try:
                await asyncio.wait_for(session.page.close(), 5)
            except BaseException:
                pass
            try:
                await manager.release_lane(session.lane_key, session.lease_id)
            except Exception:
                pass
            finally:
                if session.acquired_slot:
                    await session.limiter.__aexit__(None, None, None)
                    session.acquired_slot = False

    cleanup_task = asyncio.create_task(cleanup())
    try:
        await asyncio.shield(cleanup_task)
    except asyncio.CancelledError:
        await cleanup_task
        raise


async def _picker_reaper(manager: LaneManager) -> None:
    while True:
        await asyncio.sleep(30)
        for session_id, session in list(PICKER_SESSIONS.items()):
            if session.expired():
                await _close_picker(manager, session_id)


class PickerOpenRequest(BaseModel):
    url: str
    lane_key: str = "interaction"


class AssistantRecoveryBodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        action = path.rsplit("/", 1)[-1]
        if (scope["type"] != "http" or not path.startswith("/assistant-browser/")
                or action not in {"execute", "screenshot", "refresh", "open-status", "open", "clear-cookies", "cookie-generation"}):
            return await self.app(scope, receive, send)
        limit = (352 * 1024 if action == "open" else
                 128 * 1024 if action == "execute" else 4 * 1024)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > limit:
                return await JSONResponse(status_code=413, content={"detail": "Browser request too large"})(scope, receive, send)
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        async def replay():
            nonlocal body
            if body is not None:
                message = {"type": "http.request", "body": bytes(body), "more_body": False}
                body = None
                return message
            return await receive()

        return await self.app(scope, replay, send)


@asynccontextmanager
async def lifespan(app: FastAPI):
    manager = LaneManager()
    manager.plugin_broker = PluginBroker(PLUGIN_BROKER_URL)
    app.state.manager = manager
    await manager.start()
    app.state.assistant_browser = AssistantBrowser(manager)
    await app.state.assistant_browser.start()
    picker_task = asyncio.create_task(_picker_reaper(manager), name="picker-reaper")
    try:
        yield
    finally:
        picker_task.cancel()
        try:
            try:
                await picker_task
            except asyncio.CancelledError:
                pass
            for session_id in list(PICKER_SESSIONS):
                await _close_picker(manager, session_id)
        finally:
            try:
                await app.state.assistant_browser.stop()
            finally:
                await manager.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="SiteFlow Worker", version="0.2.0", lifespan=lifespan)
    app.add_middleware(AssistantRecoveryBodyLimit)
    app.add_middleware(DispatchBoundary)
    app.include_router(dispatch_router)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        if request.url.path.startswith("/assistant-browser/"):
            return JSONResponse(status_code=422, content={"detail": "Invalid browser request"})
        return await request_validation_exception_handler(request, exc)

    @app.post("/assistant-browser/open")
    async def assistant_browser_open(payload: OpenRequest) -> dict[str, Any]:
        return await app.state.assistant_browser.open(
            payload.url, payload.lane_key, payload.token,
            session_id=str(payload.session_id) if payload.session_id is not None else None,
            trusted_origins=payload.trusted_origins,
            same_origin_only=payload.same_origin_only,
            wait_for_lane=payload.wait_for_lane,
            **{key: (str(payload.cookie_generation) if payload.cookie_generation is not None else None)
               if key == "cookie_generation" else payload.cookies
               for key in ("cookies", "cookie_generation") if key in payload.model_fields_set},
        )

    @app.post("/assistant-browser/cookie-generation")
    async def assistant_browser_cookie_generation(payload: CookieGenerationRequest) -> dict[str, str]:
        return await app.state.assistant_browser.cookie_generation(payload.url, payload.lane_key)

    @app.post("/assistant-browser/{session_id}/clear-cookies")
    async def assistant_browser_clear_cookies(session_id: str, payload: ClearCookiesRequest) -> dict[str, Any]:
        return await app.state.assistant_browser.clear_cookies(session_id, payload.token, str(payload.operation_id))

    @app.post("/assistant-browser/{session_id}/open-status")
    async def assistant_browser_open_status(session_id: str, payload: TokenRequest) -> dict[str, Any]:
        return await app.state.assistant_browser.open_status(session_id, payload.token)

    @app.post("/assistant-browser/{session_id}/observe")
    async def assistant_browser_observe(session_id: str, payload: ObserveRequest) -> dict[str, Any]:
        return await app.state.assistant_browser.observe(
            session_id, payload.token, payload.offset, payload.limit, payload.frame_id,
        )

    @app.post("/assistant-browser/{session_id}/inspect")
    async def assistant_browser_inspect(session_id: str, payload: InspectRequest) -> dict[str, Any]:
        return await app.state.assistant_browser.inspect(
            session_id, payload.token, str(payload.snapshot_id), payload.node_id,
            payload.offset, payload.limit,
        )

    @app.post("/assistant-browser/{session_id}/check-selector")
    async def assistant_browser_check_selector(session_id: str, payload: CheckSelectorRequest) -> dict[str, Any]:
        return await app.state.assistant_browser.check_selector(
            session_id, payload.token, payload.selector, payload.frame_id,
        )

    @app.post("/assistant-browser/{session_id}/navigate")
    async def assistant_browser_navigate(session_id: str, payload: NavigateRequest) -> dict[str, Any]:
        return await app.state.assistant_browser.navigate(session_id, payload.token, payload.url)

    @app.post("/assistant-browser/{session_id}/screenshot")
    async def assistant_browser_screenshot(session_id: str, payload: TokenRequest) -> dict[str, Any]:
        return await app.state.assistant_browser.screenshot(session_id, payload.token)

    @app.post("/assistant-browser/{session_id}/refresh")
    async def assistant_browser_refresh(session_id: str, payload: TokenRequest) -> dict[str, Any]:
        return await app.state.assistant_browser.refresh(session_id, payload.token)

    @app.post("/assistant-browser/{session_id}/close")
    async def assistant_browser_close(session_id: str, payload: TokenRequest) -> dict[str, Any]:
        return await app.state.assistant_browser.close(session_id, payload.token)

    @app.post("/assistant-browser/{session_id}/execute")
    async def assistant_browser_execute(session_id: str, payload: ExecuteRequest) -> dict[str, Any]:
        return await app.state.assistant_browser.execute(session_id, payload)

    @app.get("/health")
    def health() -> dict[str, Any]:
        status = app.state.manager.status()
        return {"ok": status["ok"], "lanes": len(status["lanes"])}

    @app.get("/status")
    def status() -> dict[str, Any]:
        return app.state.manager.status()

    @app.get("/browser/readiness")
    async def browser_readiness(request: Request) -> dict[str, Any]:
        token = os.environ.get("SITEFLOW_BROWSER_TOKEN", "")
        authorization = request.headers.get("authorization", "")
        if ("origin" in request.headers or not token
                or not secrets.compare_digest(authorization.encode(), f"Bearer {token}".encode())):
            raise HTTPException(403, "Forbidden")
        manager = app.state.manager
        backend = manager.backend
        supported = isinstance(backend, ManagerBackend)
        ready = False
        if supported:
            try:
                async with asyncio.timeout(10):
                    ready = await backend.execution_ready()
            except TimeoutError:
                pass
        lanes = []
        for definition in LANE_DEFINITIONS:
            usable = False
            try:
                lane = manager.get(definition["key"])
            except HTTPException:
                lane = None
            if lane is not None:
                usable = (lane.enabled and lane.status in ("stopped", "running")
                          and (lane.session is None or not lane.session.disconnected))
            lanes.append({"key": definition["key"], "ready": bool(ready and usable)})
        return {
            "backend": "cloak-manager" if supported else "unsupported", "ready": ready,
            "lanes": lanes,
            "reason": None if ready else "browser_unavailable" if supported else "unsupported_backend",
        }

    @app.post("/browser/preflight")
    async def browser_preflight(request: Request) -> dict[str, Any]:
        token = os.environ.get("SITEFLOW_BROWSER_TOKEN", "")
        authorization = request.headers.get("authorization", "")
        if ("origin" in request.headers or not token
                or not secrets.compare_digest(authorization.encode(), f"Bearer {token}".encode())):
            raise HTTPException(403, "Forbidden")
        try:
            body = bytearray()
            async with asyncio.timeout(10):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > 4096:
                        raise ValueError
                    body.extend(chunk)

            def unique_object(pairs):
                value = dict(pairs)
                if len(value) != len(pairs):
                    raise ValueError
                return value

            payload = json.loads(body.decode("utf-8"), object_pairs_hook=unique_object)
            if not isinstance(payload, dict) or set(payload) != {"lanes"}:
                raise ValueError
            lanes = payload["lanes"]
            keys = {definition["key"] for definition in LANE_DEFINITIONS}
            if (not isinstance(lanes, list) or not 1 <= len(lanes) <= len(keys)
                    or any(not isinstance(key, str) or key not in keys for key in lanes)
                    or len(set(lanes)) != len(lanes)):
                raise ValueError
        except TimeoutError:
            raise HTTPException(408, "Browser request timed out") from None
        except Exception:
            raise HTTPException(422, "Invalid browser request") from None
        try:
            async with asyncio.timeout(120):
                return await app.state.manager.preflight(lanes)
        except TimeoutError:
            return {"backend": "cloak-manager", "ready": False,
                    "lanes": [{"key": definition["key"], "ready": False} for definition in LANE_DEFINITIONS],
                    "first_lane": None, "reason": "browser_unavailable"}

    @app.api_route("/browser/settings", methods=["GET", "PUT"])
    async def browser_settings(request: Request) -> dict[str, Any]:
        token = os.environ.get("SITEFLOW_BROWSER_TOKEN", "")
        authorization = request.headers.get("authorization", "")
        if ("origin" in request.headers or not token
                or not secrets.compare_digest(authorization.encode(), f"Bearer {token}".encode())):
            raise HTTPException(403, "禁止访问浏览器授权设置。")
        backend = app.state.manager.backend
        if not isinstance(backend, ManagerBackend):
            return {
                "backend": "unsupported", "connected": False,
                "license": {"configured": False, "source": "unknown", "editable": False},
                "license_tier": "unknown", "binary_version": None,
                "error": "当前浏览器后端不支持授权设置。",
            }
        try:
            async with asyncio.timeout(25 if request.method == "GET" else 350):
                if request.method == "GET":
                    return await backend.browser_settings()
                if os.environ.get("CLOAKBROWSER_LICENSE_KEY", ""):
                    raise HTTPException(409, "授权密钥由部署环境配置，只能在部署环境中修改。")
                if backend.settings_write_unknown:
                    raise HTTPException(409, BROWSER_SETTINGS_WRITE_UNKNOWN)
                try:
                    body = bytearray()
                    async with asyncio.timeout(10):
                        async for chunk in request.stream():
                            if len(body) + len(chunk) > 8192:
                                raise ValueError
                            body.extend(chunk)

                    def unique_object(pairs):
                        value = dict(pairs)
                        if len(value) != len(pairs):
                            raise ValueError
                        return value

                    payload = json.loads(body.decode("utf-8"), object_pairs_hook=unique_object)
                    if not isinstance(payload, dict) or set(payload) != {"license_key"}:
                        raise ValueError
                    key = payload["license_key"]
                    if (not isinstance(key, str) or any(unicodedata.category(char).startswith("C") for char in key)
                            or (key != "" and not key.strip()) or len(key.strip()) > 4096):
                        raise ValueError
                    key = key.strip()
                except TimeoutError:
                    raise HTTPException(408, "读取授权设置请求超时。") from None
                except Exception:
                    raise HTTPException(422, "授权设置请求无效。") from None
                return await backend.update_browser_settings(key)
        except (TimeoutError, asyncio.CancelledError):
            detail = (BROWSER_SETTINGS_WRITE_UNKNOWN if backend.settings_write_unknown
                      else "浏览器授权设置请求超时，请稍后刷新。")
            raise HTTPException(504, detail) from None
        except BrowserSettingsError as exc:
            raise HTTPException(exc.status_code, str(exc)) from None

    @app.api_route("/lanes/{key}/clipboard", methods=["GET", "POST"])
    async def lane_clipboard(key: str, request: Request) -> JSONResponse:
        try:
            token = os.environ.get("SITEFLOW_BROWSER_TOKEN", "")
            authorization = request.headers.getlist("authorization")
            if ("origin" in request.headers or not token or len(authorization) != 1
                    or not secrets.compare_digest(authorization[0].encode(), f"Bearer {token}".encode())):
                raise HTTPException(403, "Forbidden")
            if request.method == "GET":
                try:
                    async with asyncio.timeout(12):
                        text = await app.state.manager.get_clipboard(key)
                except LaneNotRunningError:
                    raise HTTPException(409, "Lane is not running") from None
                except HTTPException as exc:
                    raise HTTPException(404 if exc.status_code == 404 else 502,
                                        "Unknown lane" if exc.status_code == 404 else "Clipboard unavailable") from None
                except TimeoutError:
                    raise HTTPException(504, "Clipboard request timed out") from None
                return JSONResponse({"text": text}, headers={"Cache-Control": "no-store"})
            try:
                body = bytearray()
                async with asyncio.timeout(10):
                    async for chunk in request.stream():
                        if len(body) + len(chunk) > 12 * 1_048_576 + 1024:
                            raise HTTPException(413, "Clipboard request too large")
                        body.extend(chunk)
                pairs = json.loads(body.decode("utf-8"), object_pairs_hook=list)
                if (not isinstance(pairs, list) or len(pairs) != 1
                        or not isinstance(pairs[0], tuple) or pairs[0][0] != "text"
                        or not isinstance(pairs[0][1], str)):
                    raise ValueError
                text = pairs[0][1]
                if len(text) > 1_048_576:
                    raise HTTPException(413, "Clipboard text too large")
                text.encode("utf-8")
            except TimeoutError:
                raise HTTPException(408, "Clipboard request timed out") from None
            except (ValueError, TypeError, RecursionError):
                raise HTTPException(422, "Invalid clipboard request") from None
            try:
                async with asyncio.timeout(12):
                    await app.state.manager.set_clipboard(key, text)
            except LaneNotRunningError:
                raise HTTPException(409, "Lane is not running") from None
            except HTTPException as exc:
                raise HTTPException(404 if exc.status_code == 404 else 502,
                                    "Unknown lane" if exc.status_code == 404 else "Clipboard unavailable") from None
            except TimeoutError:
                raise HTTPException(504, "Clipboard request timed out") from None
            return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
        except HTTPException as exc:
            # Lane lookup errors are static; never expose provider exception text.
            detail = "Unknown lane" if exc.status_code == 404 else exc.detail
            return JSONResponse({"detail": detail}, status_code=exc.status_code,
                                headers={"Cache-Control": "no-store"})
        except Exception:
            return JSONResponse({"detail": "Clipboard unavailable"}, status_code=502,
                                headers={"Cache-Control": "no-store"})

    @app.post("/lanes/{key}/start")
    async def start_lane(key: str) -> dict[str, Any]:
        try:
            return await app.state.manager.start_lane(key)
        except LaneDisabledError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except LaneCapacityError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except LaneStartError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/lanes/{key}/stop")
    async def stop_lane(key: str) -> dict[str, Any]:
        try:
            return await app.state.manager.stop_lane(key)
        except LaneBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except LaneStartError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/lanes/{key}/lease")
    async def lease_lane(key: str) -> dict[str, Any]:
        try:
            lease_id, lane = await app.state.manager.lease_lane(key)
            return {"lease_id": lease_id, "lane": lane}
        except LaneDisabledError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except LaneCapacityError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except LaneStartError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/lanes/{key}/release")
    async def release_lane(key: str, payload: LeaseRequest) -> dict[str, Any]:
        return {
            "lane": await app.state.manager.release_lane(key, payload.lease_id or "")
        }

    @app.post("/lanes/{key}/viewer")
    async def acquire_viewer(key: str, payload: ViewerRequest) -> dict[str, Any]:
        try:
            return {"lane": await app.state.manager.viewer_acquire(key, payload.viewer_id)}
        except LaneNotRunningError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/lanes/{key}/viewer/release")
    async def release_viewer(key: str, payload: ViewerRequest) -> dict[str, Any]:
        return {"lane": await app.state.manager.viewer_release(key, payload.viewer_id)}

    @app.post("/lanes/{key}/focus")
    async def focus_lane_page(key: str, payload: JobFocusRequest) -> dict[str, Any]:
        try:
            lane = await app.state.manager.focus_task_page(key, payload.job_id)
        except LaneNotRunningError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"focused": True, "lane": lane}

    @app.websocket("/lanes/{key}/vnc")
    async def vnc(websocket: WebSocket, key: str) -> None:
        token = os.environ.get("SITEFLOW_BROWSER_TOKEN", "")
        authorization = websocket.headers.get("authorization", "")
        if (websocket.headers.get("origin") is not None or not token
                or not secrets.compare_digest(authorization, f"Bearer {token}")):
            await websocket.close(code=4403, reason="Unauthorized internal viewer")
            return
        lane = app.state.manager.get(key)
        if lane.session is None or lane.session.disconnected:
            await websocket.close(code=4404)
            return
        await websocket.accept(subprotocol="binary" if "binary" in websocket.scope.get("subprotocols", []) else None)
        url, headers = app.state.manager.backend.vnc_connection(lane.session)
        try:
            async with websockets.connect(url, additional_headers=headers, subprotocols=["binary"],
                                          max_size=None, compression=None, ping_interval=None) as upstream:
                async def forward():
                    while True:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            return
                        data = message.get("bytes")
                        if data is not None:
                            await upstream.send(data)
                async def backward():
                    async for data in upstream:
                        if isinstance(data, bytes):
                            await websocket.send_bytes(data)
                tasks = [asyncio.create_task(forward()), asyncio.create_task(backward())]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except Exception:
            await websocket.close(code=1011, reason="Browser viewer unavailable")

    @app.put("/settings")
    async def update_settings(payload: IdleSettings) -> dict[str, Any]:
        return app.state.manager.update_settings(
            idle_stop_seconds=payload.idle_stop_seconds,
            general_concurrency=payload.general_concurrency,
            proxy_concurrency=payload.proxy_concurrency,
        )

    @app.post("/picker/open")
    async def open_picker(payload: PickerOpenRequest) -> dict[str, Any]:
        try:
            return await _open_picker(app.state.manager, payload.url, payload.lane_key)
        except HTTPException:
            raise
        except LaneDisabledError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except LaneStartError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except LaneNotRunningError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc

    @app.get("/picker/{session_id}")
    async def get_picker(session_id: str) -> dict[str, Any]:
        session = PICKER_SESSIONS.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="拾取会话不存在")
        if session.expired():
            await _close_picker(app.state.manager, session_id)
            raise HTTPException(status_code=410, detail="拾取会话已过期")
        try:
            picked = await session.page.evaluate(
                "() => window.__siteflowPicker?.picked ?? null"
            )
        except Exception:
            picked = None
        return session.view(picked)

    @app.delete("/picker/{session_id}")
    async def close_picker(session_id: str) -> dict[str, Any]:
        await _close_picker(app.state.manager, session_id)
        return {"closed": True}

    @app.post("/plugin-broker")
    async def plugin_broker(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return await app.state.manager.plugin_broker.execute(payload)
        except BrokerNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except BrokerExpired as exc:
            raise HTTPException(status_code=410, detail=str(exc)) from exc
        except BrokerDenied as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except BrokerActionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except BrokerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/execute")
    async def execute(payload: dict[str, Any]) -> dict[str, Any]:
        job_id = str(payload.get("job_id") or uuid.uuid4().hex)
        if job_id in ACTIVE_EXECUTIONS:
            raise HTTPException(409, "Job already executing")
        cancel = asyncio.Event()
        ACTIVE_EXECUTIONS[job_id] = cancel
        try:
            return await execute_job(app.state.manager, payload, cancel)
        finally:
            if ACTIVE_EXECUTIONS.get(job_id) is cancel:
                ACTIVE_EXECUTIONS.pop(job_id, None)

    @app.post("/execute/stream")
    async def execute_stream(payload: dict[str, Any]) -> StreamingResponse:
        job_id = str(payload.get("job_id") or uuid.uuid4().hex)
        if job_id in ACTIVE_EXECUTIONS:
            raise HTTPException(409, "Job already executing")
        cancel = asyncio.Event()
        ACTIVE_EXECUTIONS[job_id] = cancel
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def on_step(event: dict[str, Any]) -> None:
            await queue.put({"type": "progress", **event})

        async def run() -> None:
            try:
                result = await execute_job(
                    app.state.manager, payload, cancel, on_step=on_step
                )
                await queue.put({"type": "result", "result": result})
            except Exception as exc:
                await queue.put(
                    {
                        "type": "result",
                        "result": {
                            "status": "error",
                            "message": f"worker 执行异常: {type(exc).__name__}: {str(exc)[:500]}",
                            "steps": [],
                        },
                    }
                )
            finally:
                await queue.put(None)

        task = asyncio.create_task(run())

        async def stream():  # type: ignore[no-untyped-def]
            try:
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    yield json.dumps(item, ensure_ascii=False) + "\n"
            finally:
                try:
                    if not task.done():
                        cancel.set()
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                finally:
                    if ACTIVE_EXECUTIONS.get(job_id) is cancel:
                        ACTIVE_EXECUTIONS.pop(job_id, None)

        return StreamingResponse(
            stream(),
            media_type="application/x-ndjson",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/executions/{job_id}/cancel")
    async def cancel_execution(job_id: str) -> dict[str, Any]:
        event = ACTIVE_EXECUTIONS.get(job_id)
        if event is None:
            return {"canceled": False}
        event.set()
        return {"canceled": True}

    return app
