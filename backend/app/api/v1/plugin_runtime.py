from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, Query
from sqlalchemy import select, func
from app.models.plugin import PluginAuditEvent
from sqlalchemy.orm import Session

from app.api.deps import get_current_session, get_db, get_settings, require_csrf
from app.core.config import Settings
from app.models.auth import AuthSession
from app.schemas.plugin_runtime import (
    PluginApproveRequest,
    PluginHeartbeatRequest,
    PluginRegisterRequest,
    PluginUnregisterRequest,
    PluginView,
)
from app.services.plugin_runtime import (
    PluginAuthenticationError,
    PluginConfigurationError,
    PluginGenerationConflict,
    PluginNotFoundError,
    PluginRuntimeService,
    PluginUnreachableError,
    PluginValidationError,
)

router = APIRouter(prefix="/api/v1/plugin-runtime", tags=["plugin-runtime"])


@router.get("/audit", dependencies=[Depends(get_current_session)])
def audit(session: Session = Depends(get_db), page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100), plugin_id: str | None = Query(None, max_length=64), event: str | None = Query(None, max_length=32), result: str | None = Query(None, max_length=16)):
    query = select(PluginAuditEvent)
    for name, value in (("plugin_id", plugin_id), ("event", event), ("result", result)):
        if value:
            query = query.where(getattr(PluginAuditEvent, name) == value)
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    rows = session.scalars(query.order_by(PluginAuditEvent.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return {"total": total, "page": page, "page_size": page_size, "items": [{column.name: getattr(row, column.name) for column in PluginAuditEvent.__table__.columns} for row in rows]}


@router.post("/plugins/{plugin_id}/revoke", response_model=PluginView)
def revoke_plugin(plugin_id: str, current: AuthSession = Depends(require_csrf), session: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    try:
        return PluginRuntimeService(session, settings).revoke(plugin_id, current.username)
    except Exception as exc:
        _raise_runtime_error(exc)


def get_service(
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> PluginRuntimeService:
    return PluginRuntimeService(session, settings)


def require_plugin_token(
    request: Request,
    x_plugin_token: str | None = Header(default=None),
) -> str:
    token = x_plugin_token or ""
    if not token:
        authorization = request.headers.get("authorization", "")
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            token = value.strip()
    if not token:
        raise HTTPException(status_code=401, detail="plugin token is required")
    return token


def _raise_runtime_error(exc: Exception) -> None:
    if isinstance(exc, PluginAuthenticationError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, PluginValidationError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if isinstance(exc, PluginNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, PluginGenerationConflict):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, PluginUnreachableError):
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if isinstance(exc, PluginConfigurationError):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    raise exc


@router.post("/register", response_model=PluginView, status_code=201)
def register(
    payload: PluginRegisterRequest,
    token: str = Depends(require_plugin_token),
    service: PluginRuntimeService = Depends(get_service),
) -> PluginView:
    try:
        return service.register(payload, token)
    except Exception as exc:
        _raise_runtime_error(exc)
    raise AssertionError("unreachable")


@router.post("/heartbeat", response_model=PluginView)
def heartbeat(
    payload: PluginHeartbeatRequest,
    token: str = Depends(require_plugin_token),
    service: PluginRuntimeService = Depends(get_service),
) -> PluginView:
    try:
        return service.heartbeat(payload.plugin_id, token, payload.generation)
    except Exception as exc:
        _raise_runtime_error(exc)
    raise AssertionError("unreachable")


@router.post("/unregister", status_code=204)
def unregister(
    payload: PluginUnregisterRequest,
    token: str = Depends(require_plugin_token),
    service: PluginRuntimeService = Depends(get_service),
) -> Response:
    try:
        service.unregister(payload.plugin_id, token, payload.generation)
    except Exception as exc:
        _raise_runtime_error(exc)
    return Response(status_code=204)


@router.get("/plugins", response_model=list[PluginView])
def list_plugins(
    _current: AuthSession = Depends(get_current_session),
    service: PluginRuntimeService = Depends(get_service),
) -> list[PluginView]:
    return service.list_plugins()


@router.post("/plugins/{plugin_id}/approve", response_model=PluginView)
def approve_plugin(
    plugin_id: str,
    payload: PluginApproveRequest,
    current: AuthSession = Depends(require_csrf),
    service: PluginRuntimeService = Depends(get_service),
) -> PluginView:
    try:
        return service.approve(plugin_id, payload.capabilities, current.username)
    except Exception as exc:
        _raise_runtime_error(exc)
    raise AssertionError("unreachable")


@router.post("/plugins/{plugin_id}/disable", response_model=PluginView)
def disable_plugin(
    plugin_id: str,
    _current: AuthSession = Depends(require_csrf),
    service: PluginRuntimeService = Depends(get_service),
) -> PluginView:
    try:
        return service.disable(plugin_id, _current.username)
    except Exception as exc:
        _raise_runtime_error(exc)
    raise AssertionError("unreachable")


@router.post("/plugins/{plugin_id}/enable", response_model=PluginView)
def enable_plugin(
    plugin_id: str,
    _current: AuthSession = Depends(require_csrf),
    service: PluginRuntimeService = Depends(get_service),
) -> PluginView:
    try:
        return service.enable(plugin_id, _current.username)
    except Exception as exc:
        _raise_runtime_error(exc)
    raise AssertionError("unreachable")


@router.post("/plugins/{plugin_id}/check", response_model=PluginView)
def check_plugin(
    plugin_id: str,
    _current: AuthSession = Depends(require_csrf),
    service: PluginRuntimeService = Depends(get_service),
) -> PluginView:
    try:
        return service.check(plugin_id)
    except Exception as exc:
        _raise_runtime_error(exc)
    raise AssertionError("unreachable")


@router.get("/plugins/{plugin_id}", response_model=PluginView)
def get_plugin(
    plugin_id: str,
    _current: AuthSession = Depends(get_current_session),
    service: PluginRuntimeService = Depends(get_service),
) -> PluginView:
    try:
        return service.get_plugin(plugin_id)
    except Exception as exc:
        _raise_runtime_error(exc)
    raise AssertionError("unreachable")


@router.get("/plugins/{plugin_id}/references", dependencies=[Depends(get_current_session)])
def references(plugin_id: str, service: PluginRuntimeService = Depends(get_service)):
    try:
        return service.references(plugin_id)
    except Exception as exc:
        _raise_runtime_error(exc)


@router.get("/plugins/{plugin_id}/change-impacts", dependencies=[Depends(get_current_session)])
def change_impacts(plugin_id: str, service: PluginRuntimeService = Depends(get_service)):
    try:
        return service.change_impacts(plugin_id)
    except Exception as exc:
        _raise_runtime_error(exc)


@router.post("/plugins/{plugin_id}/remove", response_model=PluginView)
def remove(plugin_id: str, current: AuthSession = Depends(require_csrf), service: PluginRuntimeService = Depends(get_service)):
    try:
        return service.remove(plugin_id, current.username)
    except Exception as exc:
        _raise_runtime_error(exc)


@router.post("/plugins/{plugin_id}/restore", response_model=PluginView)
def restore(plugin_id: str, current: AuthSession = Depends(require_csrf), service: PluginRuntimeService = Depends(get_service)):
    try:
        return service.restore(plugin_id, current.username)
    except Exception as exc:
        _raise_runtime_error(exc)
