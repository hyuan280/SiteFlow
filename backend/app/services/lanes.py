from __future__ import annotations

import asyncio
import json
from urllib.parse import urlparse

import httpx

from app.core.config import Settings
from app.schemas.lane import (
    BROWSER_BACKEND_UNAVAILABLE, BrowserLicenseView, BrowserSettingsView, LaneStatus, LanesResponse,
)

LANE_DEFINITIONS = [
    {"key": "general", "label": "普通通用"},
    {"key": "interaction", "label": "普通交互"},
    {"key": "proxy_general", "label": "代理通用"},
    {"key": "proxy_interaction", "label": "代理交互"},
]
LANE_LABELS = {item["key"]: item["label"] for item in LANE_DEFINITIONS}


class WorkerRequestError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code


class LaneService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def scheduler_preflight(self, lanes: set[str]) -> bool:
        """Start/initialize the first required lane; others need only be queue-eligible."""
        token = self.settings.browser_token
        if not lanes or not lanes <= LANE_LABELS.keys() or token is None or not token.get_secret_value():
            return False
        required = [key for key in LANE_LABELS if key in lanes]
        try:
            async with asyncio.timeout(130):
                async with httpx.AsyncClient(
                    timeout=130.0, follow_redirects=False, trust_env=False,
                ) as client:
                    async with client.stream(
                        "POST", f"{self.settings.worker_url.rstrip('/')}/browser/preflight",
                        headers={
                            "Authorization": f"Bearer {token.get_secret_value()}",
                            "Accept-Encoding": "identity",
                        },
                        json={"lanes": required},
                    ) as response:
                        if response.status_code != 200:
                            return False
                        if response.headers.get("content-encoding", "identity").lower() != "identity":
                            return False
                        body = bytearray()
                        async for chunk in response.aiter_raw(chunk_size=8192):
                            if len(body) + len(chunk) > 65536:
                                return False
                            body.extend(chunk)
            data = json.loads(body)
            if not isinstance(data, dict) or data.get("backend") != "cloak-manager":
                return False
            if data.get("ready") is not True or data.get("first_lane") != required[0]:
                return False
            if not isinstance(data.get("lanes"), list):
                return False
            # Ignore provider extras/reasons and license seat counts, never log them.
            available: dict[str, bool] = {}
            for lane in data["lanes"]:
                if not isinstance(lane, dict):
                    return False
                key = lane.get("key")
                if not isinstance(key, str) or key not in LANE_LABELS or key in available:
                    return False
                if type(lane.get("ready")) is not bool:
                    return False
                available[key] = lane["ready"]
            return all(available.get(key) is True for key in required)
        except Exception:
            # Startup may have completed even on timeout. Do not retry this request.
            return False

    async def scheduler_readiness(self, lanes: set[str]) -> bool:
        """Read-only admission check; never start browsers or consult license settings."""
        token = self.settings.browser_token
        if not lanes or not lanes <= LANE_LABELS.keys() or token is None or not token.get_secret_value():
            return False
        try:
            async with asyncio.timeout(10):
                async with httpx.AsyncClient(
                    timeout=10.0, follow_redirects=False, trust_env=False,
                ) as client:
                    async with client.stream(
                        "GET", f"{self.settings.worker_url.rstrip('/')}/browser/readiness",
                        headers={
                            "Authorization": f"Bearer {token.get_secret_value()}",
                            "Accept-Encoding": "identity",
                        },
                    ) as response:
                        if response.status_code != 200:
                            return False
                        if response.headers.get("content-encoding", "identity").lower() != "identity":
                            return False
                        body = bytearray()
                        async for chunk in response.aiter_raw(chunk_size=8192):
                            if len(body) + len(chunk) > 65536:
                                return False
                            body.extend(chunk)
            data = json.loads(body)
            if not isinstance(data, dict) or data.get("backend") != "cloak-manager":
                return False
            if data.get("ready") is not True or not isinstance(data.get("lanes"), list):
                return False
            # Only these flags are admission evidence; ignore untrusted reason text.
            available: dict[str, bool] = {}
            for lane in data["lanes"]:
                if not isinstance(lane, dict):
                    return False
                key = lane.get("key")
                if not isinstance(key, str) or key not in LANE_LABELS or key in available:
                    return False
                if type(lane.get("ready")) is not bool:
                    return False
                available[key] = lane["ready"]
            return all(available.get(key) is True for key in lanes)
        except Exception:
            return False

    def browser_license_env_locked(self) -> bool:
        key = self.settings.cloakbrowser_license_key
        return bool(key.get_secret_value()) if key is not None else False

    async def browser_settings(self, license_key: str | None = None) -> BrowserSettingsView:
        if license_key is not None and self.browser_license_env_locked():
            raise WorkerRequestError(409, "Browser license is managed by environment")
        token = self.settings.browser_token
        if token is None or not token.get_secret_value():
            raise WorkerRequestError(503, BROWSER_BACKEND_UNAVAILABLE)
        deadline = 360.0 if license_key is not None else 30.0
        try:
            async with asyncio.timeout(deadline):
                async with httpx.AsyncClient(
                    timeout=deadline, follow_redirects=False, trust_env=False,
                ) as client:
                    async with client.stream(
                        "PUT" if license_key is not None else "GET",
                        f"{self.settings.worker_url.rstrip('/')}/browser/settings",
                        headers={
                            "Authorization": f"Bearer {token.get_secret_value()}",
                            "Accept-Encoding": "identity",
                        },
                        **({"json": {"license_key": license_key}} if license_key is not None else {}),
                    ) as response:
                        if response.status_code != 200:
                            status = response.status_code
                            errors = {
                                409: "Browser license settings are locked",
                                422: "Browser license key was rejected",
                                502: BROWSER_BACKEND_UNAVAILABLE,
                                503: BROWSER_BACKEND_UNAVAILABLE,
                                504: "Browser settings request timed out",
                            }
                            raise WorkerRequestError(
                                status if status in errors else 502,
                                errors.get(status, BROWSER_BACKEND_UNAVAILABLE),
                            )
                        # Never decode compressed provider data, even if identity was ignored.
                        if response.headers.get("content-encoding", "identity").lower() != "identity":
                            raise WorkerRequestError(502, BROWSER_BACKEND_UNAVAILABLE)
                        body = bytearray()
                        async for chunk in response.aiter_raw(chunk_size=8192):
                            if len(body) + len(chunk) > 65536:
                                raise WorkerRequestError(502, BROWSER_BACKEND_UNAVAILABLE)
                            body.extend(chunk)
            data = json.loads(body)
            if not isinstance(data, dict) or not isinstance(data.get("license"), dict):
                raise ValueError("invalid browser settings")
            # Project only public fields; provider errors and masks are never public data.
            license = BrowserLicenseView.model_validate({
                key: data["license"][key] for key in ("configured", "source", "editable")
            })
            if data["error"] is not None:
                license.editable = False
            if data["backend"] == "cloak-manager" and self.browser_license_env_locked():
                license.configured = True
                license.source = "env"
                license.editable = False
            view = BrowserSettingsView.model_validate({
                "backend": data["backend"],
                "connected": data["connected"],
                "license": license,
                "license_tier": data["license_tier"],
                "binary_version": data["binary_version"],
                "error": BROWSER_BACKEND_UNAVAILABLE if data["error"] is not None else None,
            })
        except WorkerRequestError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise WorkerRequestError(504, "Browser settings request timed out") from None
        except (ValueError, KeyError, TypeError, RecursionError):
            raise WorkerRequestError(502, BROWSER_BACKEND_UNAVAILABLE) from None
        except Exception:
            raise WorkerRequestError(503, BROWSER_BACKEND_UNAVAILABLE) from None
        return view

    async def _post(self, path: str, payload: dict | None = None, *, timeout: float | None = None) -> dict:
        try:
            async with httpx.AsyncClient(timeout=timeout or self.settings.worker_timeout) as client:
                response = await client.post(
                    f"{self.settings.worker_url.rstrip('/')}{path}",
                    json=payload,
                )
        except Exception as exc:
            raise WorkerRequestError(503, f"Worker 不可达: {exc}") from exc
        if response.status_code != 200:
            try:
                detail = response.json().get("detail") or response.text
            except Exception:
                detail = response.text
            raise WorkerRequestError(response.status_code, str(detail))
        data = response.json()
        if not isinstance(data, dict):
            raise WorkerRequestError(502, "Worker 返回了无效响应")
        return data

    async def sync_settings(
        self,
        idle_stop_seconds: int,
        general_concurrency: int,
        proxy_concurrency: int,
    ) -> bool:
        try:
            async with httpx.AsyncClient(timeout=self.settings.worker_timeout) as client:
                response = await client.put(
                    f"{self.settings.worker_url.rstrip('/')}/settings",
                    json={
                        "idle_stop_seconds": idle_stop_seconds,
                        "general_concurrency": general_concurrency,
                        "proxy_concurrency": proxy_concurrency,
                    },
                )
                return response.status_code == 200
        except Exception:
            return False

    async def start_lane(self, key: str) -> dict:
        return await self._post(f"/lanes/{key}/start", timeout=max(120, self.settings.worker_timeout))

    async def stop_lane(self, key: str) -> dict:
        return await self._post(f"/lanes/{key}/stop", timeout=max(30, self.settings.worker_timeout))

    async def lease_lane(self, key: str) -> dict:
        return await self._post(f"/lanes/{key}/lease", timeout=max(120, self.settings.worker_timeout))

    async def release_lane(self, key: str, lease_id: str) -> dict:
        return await self._post(f"/lanes/{key}/release", {"lease_id": lease_id})

    async def open_picker(self, url: str, lane_key: str = "interaction") -> dict:
        try:
            async with httpx.AsyncClient(timeout=180.0) as client:
                response = await client.post(
                    f"{self.settings.worker_url.rstrip('/')}/picker/open",
                    json={"url": url, "lane_key": lane_key},
                )
        except Exception as exc:
            raise WorkerRequestError(503, f"Worker 不可达: {exc}") from exc
        if response.status_code != 200:
            try:
                detail = response.json().get("detail") or response.text
            except Exception:
                detail = response.text
            raise WorkerRequestError(response.status_code, str(detail))
        data = response.json()
        if not isinstance(data, dict):
            raise WorkerRequestError(502, "Worker 返回了无效响应")
        return data

    async def get_picker(self, session_id: str) -> dict:
        try:
            async with httpx.AsyncClient(timeout=self.settings.worker_timeout) as client:
                response = await client.get(
                    f"{self.settings.worker_url.rstrip('/')}/picker/{session_id}"
                )
        except Exception as exc:
            raise WorkerRequestError(503, f"Worker 不可达: {exc}") from exc
        if response.status_code != 200:
            try:
                detail = response.json().get("detail") or response.text
            except Exception:
                detail = response.text
            raise WorkerRequestError(response.status_code, str(detail))
        data = response.json()
        if not isinstance(data, dict):
            raise WorkerRequestError(502, "Worker 返回了无效响应")
        return data

    async def close_picker(self, session_id: str) -> dict:
        try:
            async with httpx.AsyncClient(timeout=self.settings.worker_timeout) as client:
                response = await client.delete(
                    f"{self.settings.worker_url.rstrip('/')}/picker/{session_id}"
                )
        except Exception as exc:
            raise WorkerRequestError(503, f"Worker 不可达: {exc}") from exc
        if response.status_code != 200:
            try:
                detail = response.json().get("detail") or response.text
            except Exception:
                detail = response.text
            raise WorkerRequestError(response.status_code, str(detail))
        data = response.json()
        return data if isinstance(data, dict) else {"closed": True}

    async def focus_job(self, key: str, job_id: str) -> dict:
        return await self._post(f"/lanes/{key}/focus", {"job_id": job_id})

    async def viewer_acquire(self, key: str, viewer_id: str) -> dict:
        data = await self._post(f"/lanes/{key}/viewer", {"viewer_id": viewer_id})
        return data.get("lane", data)

    async def viewer_release(self, key: str, viewer_id: str) -> dict:
        data = await self._post(f"/lanes/{key}/viewer/release", {"viewer_id": viewer_id})
        return data.get("lane", data)

    async def raw_status(self) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=self.settings.worker_timeout) as client:
                response = await client.get(f"{self.settings.worker_url.rstrip('/')}/status")
                response.raise_for_status()
                data = response.json()
                return data if isinstance(data, dict) else None
        except Exception:
            return None

    async def get_lanes(self) -> LanesResponse:
        data = await self.raw_status()
        if data is None:
            return LanesResponse(
                worker_connected=False,
                error="Worker 不可达",
                lanes=[
                    LaneStatus(
                        key=item["key"],
                        label=item["label"],
                        enabled=False,
                        status="unavailable",
                        browser="unavailable",
                        error="Worker 不可达",
                    )
                    for item in LANE_DEFINITIONS
                ],
            )

        lanes = []
        for item in data.get("lanes") or []:
            key = str(item.get("key"))
            lanes.append(
                LaneStatus(
                    key=key,
                    label=LANE_LABELS.get(key, str(item.get("label") or key)),
                    enabled=bool(item.get("enabled")),
                    status=str(item.get("status") or "unknown"),
                    browser=str(item.get("browser") or "unknown"),
                    display=item.get("display"),
                    profile=item.get("profile"),
                    vnc_available=bool(item.get("vnc_available")),
                    in_use=int(item.get("in_use") or 0),
                    max_concurrency=int(item.get("max_concurrency") or 1),
                    viewers=int(item.get("viewers") or 0),
                    idle_stop_seconds=int(item.get("idle_stop_seconds") or 300),
                    idle_remaining=(
                        int(item["idle_remaining"])
                        if item.get("idle_remaining") is not None
                        else None
                    ),
                    can_start=bool(item.get("can_start")),
                    can_stop=bool(item.get("can_stop")),
                    error=item.get("error"),
                    started_at=item.get("started_at"),
                )
            )
        return LanesResponse(worker_connected=True, lanes=lanes)

    def ws_url(self, lane_key: str) -> str:
        parsed = urlparse(self.settings.worker_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return f"{scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/lanes/{lane_key}/vnc"
