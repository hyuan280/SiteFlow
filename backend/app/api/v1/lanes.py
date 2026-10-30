from __future__ import annotations

import asyncio
import uuid
from urllib.parse import urlparse

import websockets
from fastapi import APIRouter, Depends, HTTPException, WebSocket
from sqlalchemy.orm import Session

from app.api.deps import get_current_session, get_db, get_settings, require_csrf
from app.core.auth import SESSION_COOKIE
from app.core.config import Settings
from app.models.auth import AuthSession
from app.schemas.lane import LaneFocusRequest, LaneStatus, LanesResponse
from app.services.auth import AuthService
from app.services.lane_settings import LaneSettingsService
from app.services.lanes import LaneService, WorkerRequestError

router = APIRouter(prefix="/api/v1/lanes", tags=["lanes"])


@router.get("", response_model=LanesResponse)
async def list_lanes(
    settings: Settings = Depends(get_settings),
    session=Depends(get_db),
    _current: AuthSession = Depends(get_current_session),
) -> LanesResponse:
    lane_service = LaneService(settings)
    lane_settings = LaneSettingsService(session, settings).view()
    await lane_service.sync_settings(
        lane_settings.idle_stop_seconds.value,
        lane_settings.general_concurrency.value,
        lane_settings.proxy_concurrency.value,
    )
    return await lane_service.get_lanes()


async def _lane_action(
    settings: Settings,
    session,
    lane_key: str,
    action: str,
) -> LaneStatus:
    service = LaneService(settings)
    lane_settings = LaneSettingsService(session, settings).view()
    await service.sync_settings(
        lane_settings.idle_stop_seconds.value,
        lane_settings.general_concurrency.value,
        lane_settings.proxy_concurrency.value,
    )
    try:
        if action == "start":
            data = await service.start_lane(lane_key)
        else:
            data = await service.stop_lane(lane_key)
    except WorkerRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return LaneStatus.model_validate(data)


@router.post("/{lane_key}/start", response_model=LaneStatus)
async def start_lane(
    lane_key: str,
    settings: Settings = Depends(get_settings),
    session=Depends(get_db),
    _current: AuthSession = Depends(require_csrf),
) -> LaneStatus:
    return await _lane_action(settings, session, lane_key, "start")


@router.post("/{lane_key}/stop", response_model=LaneStatus)
async def stop_lane(
    lane_key: str,
    settings: Settings = Depends(get_settings),
    session=Depends(get_db),
    _current: AuthSession = Depends(require_csrf),
) -> LaneStatus:
    return await _lane_action(settings, session, lane_key, "stop")


@router.post("/{lane_key}/focus")
async def focus_lane_page(
    lane_key: str,
    payload: LaneFocusRequest,
    settings: Settings = Depends(get_settings),
    _current: AuthSession = Depends(require_csrf),
) -> dict:
    try:
        data = await LaneService(settings).focus_job(lane_key, payload.job_id)
    except WorkerRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"focused": True, "lane": data.get("lane", data)}


def _origin_allowed(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host")
    if not origin or not host:
        return True
    return urlparse(origin).netloc == host


def _authenticate_websocket(websocket: WebSocket, session: Session, settings: Settings) -> AuthSession | None:
    raw = websocket.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    app = websocket.scope["app"]
    auth = AuthService(session, settings, app.state.bootstrap)
    return auth.get_session(raw)


async def _client_to_worker(websocket: WebSocket, upstream) -> None:
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            data = message.get("bytes")
            if not data:
                continue
            payload = data
            if payload:
                await upstream.send(payload)
    except Exception:
        return


async def _worker_to_client(websocket: WebSocket, upstream) -> None:
    try:
        async for message in upstream:
            if isinstance(message, bytes):
                await websocket.send_bytes(message)
            else:
                await websocket.send_text(message)
    except Exception:
        return


@router.websocket("/{lane_key}/vnc")
async def lane_vnc(websocket: WebSocket, lane_key: str) -> None:
    if not _origin_allowed(websocket):
        await websocket.close(code=4403, reason="Origin not allowed")
        return

    requested = websocket.scope.get("subprotocols", [])
    subprotocol = "binary" if "binary" in requested else None
    await websocket.accept(subprotocol=subprotocol)

    app = websocket.scope["app"]
    settings: Settings = app.state.settings
    session_factory = app.state.session_factory
    with session_factory() as db:
        current = _authenticate_websocket(websocket, db, settings)
    if current is None:
        await websocket.close(code=4401, reason="Unauthorized")
        return

    service = LaneService(settings)
    status = await service.raw_status()
    if status is None:
        await websocket.close(code=4404, reason="Worker unavailable")
        return
    lane = next((item for item in status.get("lanes") or [] if item.get("key") == lane_key), None)
    if lane is None or not lane.get("vnc_available"):
        await websocket.close(code=4404, reason="Lane VNC unavailable")
        return

    viewer_id = uuid.uuid4().hex
    try:
        await service.viewer_acquire(lane_key, viewer_id)
    except WorkerRequestError as exc:
        await websocket.close(code=4404, reason=str(exc))
        return

    worker_url = service.ws_url(lane_key)
    try:
        token = settings.browser_token.get_secret_value() if settings.browser_token else ""
        if not token:
            await websocket.close(code=1011, reason="Viewer authentication not configured")
            return
        async with websockets.connect(
            worker_url,
            additional_headers={"Authorization": f"Bearer {token}"},
            subprotocols=["binary"],
            max_size=None,
            ping_interval=None,
            ping_timeout=None,
            compression=None,
        ) as upstream:
            client_task = asyncio.create_task(_client_to_worker(websocket, upstream))
            worker_task = asyncio.create_task(_worker_to_client(websocket, upstream))
            done, pending = await asyncio.wait(
                [client_task, worker_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    except Exception:
        try:
            await websocket.close(code=1011, reason="VNC upstream unavailable")
        except Exception:
            pass
        return
    finally:
        try:
            await service.viewer_release(lane_key, viewer_id)
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass
