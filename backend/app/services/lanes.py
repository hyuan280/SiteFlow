from __future__ import annotations

from urllib.parse import urlparse

import httpx

from app.core.config import Settings
from app.schemas.lane import LaneStatus, LanesResponse

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

    async def _post(self, path: str, payload: dict | None = None) -> dict:
        try:
            async with httpx.AsyncClient(timeout=self.settings.worker_timeout) as client:
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
        return await self._post(f"/lanes/{key}/start")

    async def stop_lane(self, key: str) -> dict:
        return await self._post(f"/lanes/{key}/stop")

    async def lease_lane(self, key: str) -> dict:
        return await self._post(f"/lanes/{key}/lease")

    async def release_lane(self, key: str, lease_id: str) -> dict:
        return await self._post(f"/lanes/{key}/release", {"lease_id": lease_id})

    async def open_picker(self, url: str, lane_key: str = "interaction") -> dict:
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
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

    def ws_url(self, ws_port: int) -> str:
        parsed = urlparse(self.settings.worker_url)
        host = parsed.hostname or "worker"
        return f"ws://{host}:{ws_port}/websockify"
