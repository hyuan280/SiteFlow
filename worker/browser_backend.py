"""SiteFlow controls browser/profile policy; providers execute transport operations."""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlsplit, urlunsplit

import httpx


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
        self.playwright = None

    async def request(self, method: str, path: str, **kwargs):
        response = await self.client.request(method, path, **kwargs)
        if response.status_code >= 400:
            # Provider responses may contain credentials or private proxy settings.
            raise RuntimeError(f"Browser backend returned HTTP {response.status_code}")
        return response.json()

    async def prepare(self) -> None:
        # A worker restart loses all in-memory leases. Reconcile only its recorded
        # profiles, never list/adopt user profiles or touch the Manager container.
        async with self.lock:
            for profile_id in self.profiles.values():
                path = f"/api/profiles/{quote(profile_id, safe='')}"
                current = await self.request("GET", path + "/status")
                if current.get("status") == "running":
                    await self.request("POST", path + "/stop")
                await self.request("PUT", path, json={
                    "auto_launch": False, "restore_session": False, "capture_preview": False,
                })

    async def start(self, key: str, proxy: str | None) -> BrowserSession:
        async with self.lock:
            status = await self.request("GET", "/api/status")
            if status.get("license_tier") != "keyless" or status.get("binary_version") != "146.0.7680.177.5":
                raise RuntimeError("Browser Manager must use keyless Chromium 146.0.7680.177.5")
            options = {
                "proxy": proxy, "timezone": "Asia/Shanghai", "locale": "zh-CN",
                "screen_width": 1920, "screen_height": 1080, "gpu_family": "intel",
                "humanize": True, "human_preset": "default", "geoip": False,
                "auto_launch": False, "restore_session": False, "set_google_default": False,
                "capture_preview": False,
            }
            profile_id = self.profiles.get(key)
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
            current = await self.request("GET", path + "/status")
            if current.get("status") == "running":
                await self.request("POST", path + "/stop")
            await self.request("PUT", path, json=options)
            browser = None
            try:
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
                return session
            except BaseException:
                if browser is not None:
                    try:
                        await browser.close()  # Disconnect CDP; SiteFlow requests process stop below.
                    except Exception:
                        pass
                try:
                    await self.request("POST", path + "/stop")
                except Exception:
                    pass
                raise

    async def stop(self, session: BrowserSession) -> None:
        try:
            await session.browser.close()
        finally:
            path = f"/api/profiles/{quote(session.profile_id, safe='')}"
            current = await self.request("GET", path + "/status")
            if current.get("status") != "stopped":
                await self.request("POST", path + "/stop")

    def vnc_connection(self, session: BrowserSession) -> tuple[str, dict[str, str]]:
        parsed = urlsplit(self.url)
        url = urlunsplit(("wss" if parsed.scheme == "https" else "ws", parsed.netloc,
                         parsed.path + f"/api/profiles/{quote(session.profile_id, safe='')}/vnc", "", ""))
        return url, self.headers

    async def close(self) -> None:
        if self.playwright is not None:
            await self.playwright.stop()
        await self.client.aclose()


def create_backend(data_dir: Path) -> BrowserBackend:
    kind = os.environ.get("SITEFLOW_BROWSER_BACKEND", "cloak-manager")
    if kind != "cloak-manager":
        raise ValueError(f"Unsupported browser backend: {kind}")
    return ManagerBackend(data_dir)
