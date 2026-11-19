"""SiteFlow controls browser/profile policy; providers execute transport operations."""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlsplit, urlunsplit

import httpx


BROWSER_SETTINGS_WRITE_UNKNOWN = (
    "授权保存状态未知，已禁止继续编辑。刷新仅查看状态；"
    "请由运维确认 Manager 已完成处理，再重启 worker 后编辑。不要自动重试。"
)


class Desktop(Protocol):
    async def screenshot(self) -> bytes: ...
    async def click(self, x: float, y: float) -> None: ...


@dataclass
class BrowserSession:
    context: Any
    browser: Any
    profile_id: str
    display: str | None
    desktop: Desktop | None
    disconnected: bool = False


class BrowserBackend(Protocol):
    async def prepare(self) -> None: ...
    async def start(self, key: str, proxy: str | None) -> BrowserSession: ...
    async def stop(self, session: BrowserSession) -> None: ...
    def vnc_connection(self, session: BrowserSession) -> tuple[str, dict[str, str]]: ...
    async def close(self) -> None: ...


class BrowserSettingsError(RuntimeError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code


class BrowserCapacityError(RuntimeError):
    """Manager rejected a launch before any business action ran."""


BROWSER_STOP_TIMEOUT_SECONDS = 20


async def await_browser_cleanup(task: asyncio.Task) -> None:
    """Bound waiting, not cancellation acknowledgement; the owner retains the task."""
    try:
        done, _ = await asyncio.wait({task}, timeout=BROWSER_STOP_TIMEOUT_SECONDS)
        if not done:
            raise TimeoutError("Browser backend stop deadline exceeded")
        task.result()
    except BaseException:
        if not task.done():
            task.cancel()
        raise


def observe_cleanup(task: asyncio.Task) -> None:
    if not task.cancelled():
        task.exception()


def active_profile_budget() -> int:
    try:
        value = int(os.environ.get("SITEFLOW_BROWSER_MAX_ACTIVE_PROFILES", "1"))
    except ValueError:
        raise ValueError("SITEFLOW_BROWSER_MAX_ACTIVE_PROFILES must be between 1 and 4") from None
    if not 1 <= value <= 4:
        raise ValueError("SITEFLOW_BROWSER_MAX_ACTIVE_PROFILES must be between 1 and 4")
    return value


class ManagerBackend:
    """SiteFlow-controlled profiles via an unmodified Manager transport."""

    def __init__(self, data_dir: Path):
        self.url = os.environ.get("SITEFLOW_BROWSER_URL", "http://browser:8080").rstrip("/")
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("Invalid browser backend URL")
        token = os.environ.get("SITEFLOW_BROWSER_TOKEN", "")
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.state_path = data_dir / "browser-profiles.json"
        self.profiles = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        if not isinstance(self.profiles, dict) or not all(isinstance(v, str) for v in self.profiles.values()):
            raise ValueError("Invalid browser profile mapping")
        self.client = httpx.AsyncClient(base_url=self.url, headers=self.headers, timeout=90)
        self.lock = asyncio.Lock()
        self.settings_lock = asyncio.Lock()
        self.settings_write_unknown = False
        self.settings_generation = 0
        self.settings_client = httpx.AsyncClient(
            base_url=self.url, headers={**self.headers, "Accept-Encoding": "identity"},
            trust_env=False, follow_redirects=False, timeout=10,
        )
        self.playwright = None
        # A failed/cancelled launch still owns a seat until stop is verified.
        self.uncertain_profiles: dict[str, BrowserSession] = {}
        self._stop_tasks: dict[str, asyncio.Task] = {}

    async def capacity(self) -> int:
        """Refresh on cold admission only; running-lane reuse needs no status RPC."""
        budget = active_profile_budget()
        try:
            status = await self._settings_request("GET", "/api/status")
        except BrowserSettingsError:
            return min(budget, 1)
        return min(budget, 1) if status.get("license_tier") == "free" else budget

    async def _settings_request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        try:
            async with self.settings_client.stream(
                method, path, timeout=300 if method == "PUT" else 10, **kwargs,
            ) as response:
                if path == "/api/settings" and response.status_code in (404, 405, 501):
                    raise BrowserSettingsError(409, "浏览器管理器不支持授权设置，请升级管理器后重试。")
                if (not 200 <= response.status_code < 300
                        or response.headers.get("content-encoding", "identity").lower() != "identity"):
                    raise ValueError
                body = bytearray()
                if response.is_stream_consumed:
                    # In-memory transports may supply an already buffered response.
                    if len(response.content) > 65536:
                        raise ValueError
                    body.extend(response.content)
                else:
                    async for chunk in response.aiter_raw():
                        if len(body) + len(chunk) > 65536:
                            raise ValueError
                        body.extend(chunk)
                result = json.loads(body.decode("utf-8"))
                if not isinstance(result, dict):
                    raise ValueError
                return result
        except BrowserSettingsError:
            raise
        except httpx.TimeoutException:
            if method == "PUT":
                raise BrowserSettingsError(504, BROWSER_SETTINGS_WRITE_UNKNOWN) from None
            raise BrowserSettingsError(502, "无法读取浏览器管理器授权设置，请稍后刷新。") from None
        except Exception:
            detail = (BROWSER_SETTINGS_WRITE_UNKNOWN if method == "PUT"
                      else "无法读取浏览器管理器授权设置，请稍后刷新。")
            raise BrowserSettingsError(502, detail) from None

    async def execution_ready(self) -> bool:
        """Read-only preflight, not a guarantee that a subsequent launch succeeds."""
        if self.settings_write_unknown or self.settings_lock.locked():
            return False
        generation = self.settings_generation
        try:
            status = await self._settings_request("GET", "/api/status")
        except BrowserSettingsError:
            return False
        # Older keyless Managers need not support /api/settings. Binary metadata
        # and configured keys alone do not establish execution readiness.
        return (
            not self.settings_write_unknown and not self.settings_lock.locked()
            and self.settings_generation == generation
            and isinstance(status, dict)
            and status.get("license_tier") in ("keyless", "free", "pro")
            and all(flag not in status or status[flag] is True for flag in ("ok", "healthy", "connected"))
            and ("disconnected" not in status or status["disconnected"] is False)
        )

    async def browser_settings(self) -> dict[str, Any]:
        env_configured = bool(os.environ.get("CLOAKBROWSER_LICENSE_KEY", ""))
        result = {
            "backend": "cloak-manager", "connected": False,
            "license": {"configured": env_configured, "source": "env" if env_configured else "unknown",
                        "editable": False},
            "license_tier": "unknown", "binary_version": None, "error": None,
        }
        try:
            status = await self._settings_request("GET", "/api/status")
            tier = status.get("license_tier")
            if isinstance(tier, str) and tier in ("keyless", "free", "pro", "none", "unknown"):
                result["license_tier"] = tier
            version = status.get("binary_version")
            if isinstance(version, str) and len(version) <= 64 and re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", version):
                result["binary_version"] = version
            settings = await self._settings_request("GET", "/api/settings")
            configured = settings.get("license_key_set")
            if type(configured) is not bool:
                raise BrowserSettingsError(502, "浏览器管理器授权设置格式无效，请升级管理器后重试。")
            result["connected"] = True
            if not env_configured:
                result["license"] = {"configured": configured, "source": "manager" if configured else "unset",
                                     "editable": True}
        except BrowserSettingsError as exc:
            result["connected"] = exc.status_code == 409
            result["error"] = str(exc)
        if self.settings_write_unknown:
            result["license"]["editable"] = False
            result["error"] = BROWSER_SETTINGS_WRITE_UNKNOWN
        return result

    async def update_browser_settings(self, key: str) -> dict[str, Any]:
        if os.environ.get("CLOAKBROWSER_LICENSE_KEY", ""):
            raise BrowserSettingsError(409, "授权密钥由部署环境配置，只能在部署环境中修改。")
        if self.settings_write_unknown:
            raise BrowserSettingsError(409, BROWSER_SETTINGS_WRITE_UNKNOWN)
        if self.settings_lock.locked():
            raise BrowserSettingsError(409, "授权设置正在保存，请稍后刷新。")
        async with self.settings_lock:
            self.settings_generation += 1
            settings = await self.browser_settings()
            if not settings["license"]["editable"]:
                raise BrowserSettingsError(409 if settings["connected"] else 502,
                                           "浏览器授权设置当前不可编辑，请刷新或升级管理器。")
            if os.environ.get("CLOAKBROWSER_LICENSE_KEY", ""):
                raise BrowserSettingsError(409, "授权密钥由部署环境配置，只能在部署环境中修改。")
            # Do not send Manager's other settings (notably release_channel).
            # Manager may continue after cancellation. Only a successful PUT clears
            # this guard; otherwise an operator must verify completion and restart worker.
            self.settings_write_unknown = True
            await self._settings_request("PUT", "/api/settings", json={"license_key": key})
            self.settings_write_unknown = False
            return await self.browser_settings()

    async def request(self, method: str, path: str, **kwargs):
        response = await self.client.request(method, path, **kwargs)
        if response.status_code >= 400:
            # Provider responses may contain credentials or private proxy settings.
            if method == "POST" and path.startswith("/api/profiles/") and path.endswith("/launch"):
                if response.status_code == 402:
                    raise BrowserCapacityError("Browser Manager concurrent profile limit reached (HTTP 402)")
                if response.status_code == 403:
                    raise RuntimeError(
                        "Browser Manager license rejected (HTTP 403). "
                        "Set a valid CLOAKBROWSER_LICENSE_KEY in the Compose .env and recreate the browser service, "
                        "or update the key in CloakBrowser Manager Settings. "
                        "SITEFLOW_BROWSER_TOKEN is not a license key."
                    )
            raise RuntimeError(f"Browser backend returned HTTP {response.status_code}")
        return response.json()

    async def prepare(self) -> None:
        # A worker restart loses all in-memory leases. Reconcile only its recorded
        # profiles, never list/adopt user profiles or touch the Manager container.
        async with self.lock:
            for key, profile_id in self.profiles.items():
                path = f"/api/profiles/{quote(profile_id, safe='')}"
                current = await self.request("GET", path + "/status")
                if current.get("status") != "stopped":
                    uncertain = BrowserSession(None, None, profile_id, None, None, disconnected=True)
                    self.uncertain_profiles[key] = uncertain
                    await self.stop(uncertain, force=True)
                await self.request("PUT", path, json={
                    "auto_launch": False, "restore_session": False, "capture_preview": False,
                })

    async def start(self, key: str, proxy: str | None) -> BrowserSession:
        async with self.lock:
            profile_id = self.profiles.get(key)
            pending_stop = self._stop_tasks.get(profile_id)
            if pending_stop is not None and not pending_stop.done():
                raise RuntimeError("Browser backend stop is still pending")
            status = await self.request("GET", "/api/status")
            if not isinstance(status, dict):
                raise RuntimeError("Browser Manager returned an invalid status")
            tier = status.get("license_tier")
            if tier == "none":
                raise RuntimeError(
                    "Browser Manager requires a valid CloakBrowser license key (a free key is supported). "
                    "Get a free key at https://cloakbrowser.dev/free, set CLOAKBROWSER_LICENSE_KEY in the Compose .env, "
                    "then recreate the browser service with docker compose up -d --force-recreate browser "
                    "(use your deployment's Compose file). Alternatively, save the key in CloakBrowser Manager Settings "
                    "without restarting Manager, or use the CloakBrowser settings card on SiteFlow's lanes page. "
                    "SITEFLOW_BROWSER_TOKEN is not a license key."
                )
            # Older images are keyless; Manager 0.2+ requires a free or paid key.
            # Its reported binary version is metadata, not proof of CDP readiness.
            if tier not in ("keyless", "free", "pro"):
                raise RuntimeError("Browser Manager returned an unsupported license tier")
            options = {
                "proxy": proxy, "timezone": "Asia/Shanghai", "locale": "zh-CN",
                "screen_width": 1920, "screen_height": 1080, "gpu_family": "intel",
                "humanize": True, "human_preset": "default", "geoip": False,
                "auto_launch": False, "restore_session": False, "set_google_default": False,
                "capture_preview": False,
            }
            if not profile_id:
                created = await self.request("POST", "/api/profiles", json={"name": f"SiteFlow {key}", **options})
                profile_id = str(created["id"])
                self.profiles[key] = profile_id
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.state_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(self.profiles))
                temporary.replace(self.state_path)
            path = f"/api/profiles/{quote(profile_id, safe='')}"
            # IDs are persisted by SiteFlow; never adopt or stop arbitrary user profiles.
            browser = None
            uncertain = BrowserSession(None, None, profile_id, None, None, disconnected=True)
            self.uncertain_profiles[key] = uncertain
            try:
                current = await self.request("GET", path + "/status")
                if current.get("status") != "stopped":
                    await self.stop(uncertain, force=True)
                    self.uncertain_profiles[key] = uncertain
                await self.request("PUT", path, json=options)
                from playwright.async_api import async_playwright
                launched = await self.request("POST", path + "/launch")
                if self.playwright is None:
                    self.playwright = await async_playwright().start()
                browser = await self.playwright.chromium.connect_over_cdp(
                    self.url + path + "/cdp", headers=self.headers, timeout=30000,
                )
                if not browser.contexts:
                    raise RuntimeError("Browser backend did not expose a persistent context")
                context = browser.contexts[0]
                # Remote connections do not inherit Manager's Python input wrappers.
                from worker.browser_input import humanize_actions
                humanize_actions(context)
                display = launched.get("display")
                desktop = None
                if os.environ.get("SITEFLOW_BROWSER_DESKTOP", "x11") == "x11" and display:
                    from worker.desktop import X11Desktop
                    desktop = X11Desktop(display)
                session = BrowserSession(context, browser, profile_id, display, desktop)
                browser.on("disconnected", lambda _: setattr(session, "disconnected", True))
                self.uncertain_profiles.pop(key, None)
                return session
            except BaseException:
                uncertain.browser = browser
                try:
                    await self.stop(uncertain, force=True)
                except Exception:
                    pass  # Keep ownership quarantined for LaneManager.
                raise

    async def _stop_profile(self, profile_id: str, *, deadline: float | None = None) -> None:
        path = f"/api/profiles/{quote(profile_id, safe='')}"
        if deadline is None:
            deadline = asyncio.get_running_loop().time() + BROWSER_STOP_TIMEOUT_SECONDS
        async with asyncio.timeout_at(deadline):
            await self.request("POST", path + "/stop")
            if (asyncio.current_task().cancelling()
                    or asyncio.get_running_loop().time() >= deadline):
                raise TimeoutError("Browser backend stop deadline exceeded")
            current = await self.request("GET", path + "/status")
            if (asyncio.current_task().cancelling()
                    or asyncio.get_running_loop().time() >= deadline):
                raise TimeoutError("Browser backend stop deadline exceeded")
            if current.get("status") != "stopped":
                raise RuntimeError("Browser backend stop is unconfirmed")

    async def stop(self, session: BrowserSession, *, force: bool = False) -> None:
        task = self._stop_tasks.get(session.profile_id)
        if task is None or task.done():
            deadline = asyncio.get_running_loop().time() + BROWSER_STOP_TIMEOUT_SECONDS

            async def cleanup():
                async with asyncio.timeout_at(deadline):
                    try:
                        if session.browser is not None:
                            await session.browser.close()
                    finally:
                        # Cancellation during CDP close must not start a fresh HTTP wait.
                        if (asyncio.current_task().cancelling()
                                or asyncio.get_running_loop().time() >= deadline):
                            raise TimeoutError("Browser backend stop deadline exceeded")
                        path = f"/api/profiles/{quote(session.profile_id, safe='')}"
                        current = None if force else await self.request("GET", path + "/status")
                        if current is None or current.get("status") != "stopped":
                            if (asyncio.current_task().cancelling()
                                    or asyncio.get_running_loop().time() >= deadline):
                                raise TimeoutError("Browser backend stop deadline exceeded")
                            await self._stop_profile(session.profile_id, deadline=deadline)
                        if (asyncio.current_task().cancelling()
                                or asyncio.get_running_loop().time() >= deadline):
                            raise TimeoutError("Browser backend stop deadline exceeded")
                        for key, uncertain in list(self.uncertain_profiles.items()):
                            if uncertain.profile_id == session.profile_id:
                                self.uncertain_profiles.pop(key, None)

            task = asyncio.create_task(cleanup())
            task.add_done_callback(observe_cleanup)
            self._stop_tasks[session.profile_id] = task
        try:
            await await_browser_cleanup(task)
        finally:
            if task.done() and self._stop_tasks.get(session.profile_id) is task:
                self._stop_tasks.pop(session.profile_id, None)

    def vnc_connection(self, session: BrowserSession) -> tuple[str, dict[str, str]]:
        parsed = urlsplit(self.url)
        url = urlunsplit(("wss" if parsed.scheme == "https" else "ws", parsed.netloc,
                         parsed.path + f"/api/profiles/{quote(session.profile_id, safe='')}/vnc", "", ""))
        return url, self.headers

    async def close(self) -> None:
        if self.playwright is not None:
            await self.playwright.stop()
        await self.client.aclose()
        await self.settings_client.aclose()


def create_backend(data_dir: Path) -> BrowserBackend:
    kind = os.environ.get("SITEFLOW_BROWSER_BACKEND", "cloak-manager")
    if kind != "cloak-manager":
        raise ValueError(f"Unsupported browser backend: {kind}")
    return ManagerBackend(data_dir)
