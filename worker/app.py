from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from worker.executor import execute_job
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
PROFILE_ROOT = DATA_DIR / "cloak" / "profiles"
LOG_ROOT = DATA_DIR / "logs"
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

BASE_DISPLAY = int(os.environ.get("SITEFLOW_LANE_BASE_DISPLAY", "120"))
BASE_WS_PORT = int(os.environ.get("SITEFLOW_LANE_BASE_WS_PORT", "6310"))
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
    display_num: int
    ws_port: int
    profile: Path
    enabled: bool = True
    status: str = "stopped"  # stopped / starting / running / stopping / error / disabled
    browser: str = "stopped"
    error: str | None = None
    started_at: str | None = None
    xvnc: subprocess.Popen | None = None
    context: Any = None
    page: Any = None
    task_pages: dict[str, Any] = field(default_factory=dict)
    leases: set[str] = field(default_factory=set)
    viewers: set[str] = field(default_factory=set)
    last_activity_at: float = field(default_factory=time.monotonic)
    idle_stop_seconds: int = 300
    max_concurrency: int = 1
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    execution_semaphore: LaneExecutionLimiter = field(
        default_factory=lambda: LaneExecutionLimiter(1), repr=False
    )

    @property
    def display(self) -> str:
        return f":{self.display_num}"

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
            "ws_port": self.ws_port if self.status == "running" else None,
            "profile": f"profiles/{self.key}",
            "vnc_available": self.status == "running",
            "in_use": self.in_use,
            "task_pages": len(self.task_pages),
            "browser_pages": len(self.context.pages) if self.context is not None else 0,
            "max_concurrency": self.max_concurrency,
            "viewers": self.viewer_count,
            "idle_stop_seconds": self.idle_stop_seconds,
            "idle_remaining": self.idle_remaining(),
            "can_start": self.enabled and self.status in ("stopped", "error") and self.in_use == 0,
            "can_stop": self.status == "running" and self.in_use == 0,
            "error": self.error,
            "started_at": self.started_at,
        }


class LaneManager:
    def __init__(self) -> None:
        self.lanes: dict[str, LaneRuntime] = {}
        for index, definition in enumerate(LANE_DEFINITIONS):
            lane = LaneRuntime(
                key=definition["key"],
                label=definition["label"],
                proxy=bool(definition["proxy"]),
                display_num=BASE_DISPLAY + index,
                ws_port=BASE_WS_PORT + index,
                profile=PROFILE_ROOT / definition["key"],
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

    async def start(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
        LOG_ROOT.mkdir(parents=True, exist_ok=True)
        # Lanes are deliberately not started here. They are started by the UI
        # or leased by a task runner.
        self._idle_task = asyncio.create_task(self._idle_monitor(), name="lane-idle-monitor")

    async def stop(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
            try:
                await self._idle_task
            except asyncio.CancelledError:
                pass
            self._idle_task = None
        for lane in self.lanes.values():
            async with lane.lock:
                await self._stop_lane_locked(lane, force=True)

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
            if self.idle_stop_seconds <= 0:
                continue
            for lane in list(self.lanes.values()):
                if lane.status != "running" or lane.in_use or lane.viewers:
                    continue
                if time.monotonic() - lane.last_activity_at < lane.idle_stop_seconds:
                    continue
                async with lane.lock:
                    if (
                        lane.status != "running"
                        or lane.in_use
                        or lane.viewers
                        or time.monotonic() - lane.last_activity_at < lane.idle_stop_seconds
                    ):
                        continue
                    logger.info("auto-stopping idle lane %s after %ss", lane.key, lane.idle_stop_seconds)
                    await self._stop_lane_locked(lane)

    def get(self, key: str) -> LaneRuntime:
        lane = self.lanes.get(key)
        if lane is None:
            raise HTTPException(status_code=404, detail=f"未知车道: {key}")
        return lane

    async def start_lane(self, key: str) -> dict[str, Any]:
        lane = self.get(key)
        if not lane.enabled:
            raise LaneDisabledError(lane.error or "车道未启用")
        async with lane.lock:
            if lane.status == "running":
                return lane.view()
            await self._start_lane_locked(lane)
            lane.last_activity_at = time.monotonic()
            return lane.view()

    async def stop_lane(self, key: str) -> dict[str, Any]:
        lane = self.get(key)
        async with lane.lock:
            if lane.in_use:
                raise LaneBusyError(f"车道 {key} 正在执行任务，禁止关闭")
            if lane.status in ("stopped", "disabled"):
                return lane.view()
            await self._stop_lane_locked(lane)
            return lane.view()

    async def lease_lane(self, key: str) -> tuple[str, dict[str, Any]]:
        """Task runners call this before using a lane. It auto-starts the lane."""
        lane = self.get(key)
        if not lane.enabled:
            raise LaneDisabledError(lane.error or "车道未启用")
        async with lane.lock:
            if lane.status != "running":
                await self._start_lane_locked(lane)
            lease_id = uuid.uuid4().hex
            lane.leases.add(lease_id)
            lane.last_activity_at = time.monotonic()
            return lease_id, lane.view()

    async def release_lane(self, key: str, lease_id: str) -> dict[str, Any]:
        lane = self.get(key)
        async with lane.lock:
            lane.leases.discard(lease_id)
            lane.last_activity_at = time.monotonic()
            return lane.view()

    async def viewer_acquire(self, key: str, viewer_id: str) -> dict[str, Any]:
        lane = self.get(key)
        async with lane.lock:
            if lane.status != "running":
                raise LaneNotRunningError(f"车道 {key} 未运行")
            lane.viewers.add(viewer_id)
            lane.last_activity_at = time.monotonic()
            return lane.view()

    async def viewer_release(self, key: str, viewer_id: str) -> dict[str, Any]:
        lane = self.get(key)
        async with lane.lock:
            lane.viewers.discard(viewer_id)
            lane.last_activity_at = time.monotonic()
            return lane.view()

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
        if lane.status == "running":
            return
        await self._cleanup_locked(lane)
        lane.status = "starting"
        lane.browser = "starting"
        lane.error = None
        lane.started_at = _now_iso()
        try:
            self._clean_profile_locks(lane.profile)
            await self._start_xvnc(lane)
            await self._start_browser(lane)
            lane.status = "running"
            lane.browser = "running"
            logger.info("lane %s started display=%s ws=%s", lane.key, lane.display, lane.ws_port)
        except Exception as exc:
            lane.error = f"{type(exc).__name__}: {exc}"
            await self._cleanup_locked(lane)
            lane.status = "error"
            lane.browser = "error"
            logger.exception("lane %s failed to start", lane.key)
            raise LaneStartError(lane.error) from exc

    async def _stop_lane_locked(self, lane: LaneRuntime, *, force: bool = False) -> None:
        if lane.status in ("stopped", "disabled"):
            return
        if lane.in_use and not force:
            raise LaneBusyError(f"车道 {lane.key} 正在执行任务，禁止关闭")
        lane.status = "stopping"
        lane.browser = "stopping"
        await self._cleanup_locked(lane)
        lane.status = "stopped"
        lane.browser = "stopped"
        lane.started_at = None
        lane.error = None
        lane.leases.clear()
        lane.viewers.clear()
        logger.info("lane %s stopped", lane.key)

    async def _cleanup_locked(self, lane: LaneRuntime) -> None:
        if lane.context is not None:
            try:
                await asyncio.wait_for(lane.context.close(), timeout=10)
            except Exception:
                logger.warning("failed to close context for %s", lane.key, exc_info=True)
            lane.context = None
            lane.page = None
        if lane.xvnc is not None:
            try:
                lane.xvnc.terminate()
                lane.xvnc.wait(timeout=5)
            except Exception:
                try:
                    lane.xvnc.kill()
                except Exception:
                    pass
            lane.xvnc = None

    @staticmethod
    def _clean_profile_locks(profile: Path) -> None:
        profile.mkdir(parents=True, exist_ok=True)
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                (profile / name).unlink(missing_ok=True)
            except OSError:
                pass

    async def _start_xvnc(self, lane: LaneRuntime) -> None:
        log_path = LOG_ROOT / f"xvnc-{lane.key}.log"
        log_file = open(log_path, "w", encoding="utf-8")
        cmd = [
            "Xvnc",
            lane.display,
            "-websocketPort", str(lane.ws_port),
            "-rfbport", "-1",
            "-geometry", "1920x1080",
            "-depth", "24",
            "-SecurityTypes", "None",
            "-DisableBasicAuth",
            "-interface", "0.0.0.0",
            "-publicIP", "127.0.0.1",
            "-AlwaysShared",
            "-httpd", "/usr/share/kasmvnc/www",
        ]
        process = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT)
        log_file.close()
        lane.xvnc = process
        env = {**os.environ, "DISPLAY": lane.display}
        deadline = time.monotonic() + 20
        last = ""
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"Xvnc exited with {process.returncode}")
            probe = subprocess.run(
                ["xdotool", "getdisplaygeometry"],
                capture_output=True,
                text=True,
                env=env,
                timeout=3,
                check=False,
            )
            if probe.returncode == 0 and probe.stdout.strip():
                return
            last = (probe.stderr or probe.stdout or "").strip()
            await asyncio.sleep(0.25)
        raise RuntimeError(f"Xvnc display not ready: {last}")

    async def _start_browser(self, lane: LaneRuntime) -> None:
        from cloakbrowser import launch_persistent_context_async

        env = {**os.environ, "DISPLAY": lane.display}
        proxy = PROXY_URL if lane.proxy else None
        context = await launch_persistent_context_async(
            user_data_dir=str(lane.profile),
            headless=False,
            proxy=proxy or None,
            viewport={"width": 1920, "height": 947},
            args=[
                "--use-angle=swiftshader",
                "--fingerprint-platform=windows",
                "--fingerprint-gpu-vendor=Intel",
                "--fingerprint-screen-width=1920",
                "--fingerprint-screen-height=1080",
            ],
            env=env,
            timezone="Asia/Shanghai",
            locale="zh-CN",
            humanize=True,
            human_preset="default",
        )
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
    lease_id, _lane = await manager.lease_lane(lane_key)
    lane = manager.get(lane_key)
    page = None
    try:
        if lane.context is None:
            raise RuntimeError("车道没有可用的浏览器上下文")
        page = await lane.context.new_page()
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
        )
        PICKER_SESSIONS[session_id] = session
        return session.view()
    except Exception:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass
        await manager.release_lane(lane_key, lease_id)
        raise


async def _close_picker(manager: LaneManager, session_id: str) -> None:
    session = PICKER_SESSIONS.pop(session_id, None)
    if session is None:
        return
    async with session.lock:
        try:
            await session.page.close()
        except Exception:
            pass
        try:
            await manager.release_lane(session.lane_key, session.lease_id)
        except Exception:
            pass


async def _picker_reaper(manager: LaneManager) -> None:
    while True:
        await asyncio.sleep(30)
        for session_id, session in list(PICKER_SESSIONS.items()):
            if session.expired():
                await _close_picker(manager, session_id)


class PickerOpenRequest(BaseModel):
    url: str
    lane_key: str = "interaction"


@asynccontextmanager
async def lifespan(app: FastAPI):
    manager = LaneManager()
    manager.plugin_broker = PluginBroker(PLUGIN_BROKER_URL)
    app.state.manager = manager
    await manager.start()
    picker_task = asyncio.create_task(_picker_reaper(manager), name="picker-reaper")
    try:
        yield
    finally:
        picker_task.cancel()
        try:
            await picker_task
        except asyncio.CancelledError:
            pass
        for session_id in list(PICKER_SESSIONS):
            await _close_picker(manager, session_id)
        await manager.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="SiteFlow Worker", version="0.2.0", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, Any]:
        status = app.state.manager.status()
        return {"ok": status["ok"], "lanes": len(status["lanes"])}

    @app.get("/status")
    def status() -> dict[str, Any]:
        return app.state.manager.status()

    @app.post("/lanes/{key}/start")
    async def start_lane(key: str) -> dict[str, Any]:
        try:
            return await app.state.manager.start_lane(key)
        except LaneDisabledError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except LaneStartError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/lanes/{key}/stop")
    async def stop_lane(key: str) -> dict[str, Any]:
        try:
            return await app.state.manager.stop_lane(key)
        except LaneBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/lanes/{key}/lease")
    async def lease_lane(key: str) -> dict[str, Any]:
        try:
            lease_id, lane = await app.state.manager.lease_lane(key)
            return {"lease_id": lease_id, "lane": lane}
        except LaneDisabledError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
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
        cancel = asyncio.Event()
        ACTIVE_EXECUTIONS[job_id] = cancel
        try:
            return await execute_job(app.state.manager, payload, cancel)
        finally:
            ACTIVE_EXECUTIONS.pop(job_id, None)

    @app.post("/execute/stream")
    async def execute_stream(payload: dict[str, Any]) -> StreamingResponse:
        job_id = str(payload.get("job_id") or uuid.uuid4().hex)
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
                ACTIVE_EXECUTIONS.pop(job_id, None)
                if not task.done():
                    cancel.set()
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

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
