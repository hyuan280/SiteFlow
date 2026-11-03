from fastapi import APIRouter, Depends, Header, HTTPException, Request
from app.api.deps import get_current_session, get_db, get_settings
from app.services.plugin_runtime import PluginRuntimeService
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Any, Literal
from sqlalchemy.exc import IntegrityError, OperationalError

from app.services.secret_providers import SecretDenied
from app.services.resign import ResignConflict, ResignService
import logging

router = APIRouter()


@router.get("/api/v1/secret-providers", dependencies=[Depends(get_current_session)])
def providers(session=Depends(get_db), settings=Depends(get_settings)):
    result = [
        {"id": "builtin.manual", "name": "Manual", "capability": "secret.credentials"},
        {"id": "builtin.bitwarden", "name": "Bitwarden", "capability": "secret.credentials"},
        {"id": "builtin.twofauth", "name": "2FAuth", "capability": "secret.totp"},
    ]
    for plugin in PluginRuntimeService(session, settings).list_plugins():
        if plugin.enabled and plugin.status == "available" and set(plugin.capabilities) == set(plugin.approved_capabilities):
            for capability in ("secret.credentials", "secret.totp"):
                if capability in plugin.approved_capabilities:
                    result.append({"id": f"plugin.{plugin.plugin_id}", "name": plugin.name, "capability": capability})
    return result


class SecretRequest(BaseModel):
    job_id: int
    site_id: int
    step_key: str = Field(max_length=128)
    purpose: str = Field(max_length=64)
    provider: str = Field(max_length=64)
    lease: str | None = Field(default=None, max_length=128)


class PluginAuthorizationRequest(BaseModel):
    job_id: int
    plugin_id: str = Field(max_length=64)
    generation: int
    capability: str | None = Field(default=None, pattern="^(features|browser_actions|captcha_methods)$")


class ResignCheckpointRequest(BaseModel):
    job_id: int = Field(strict=True, gt=0)
    site_id: int = Field(strict=True, gt=0)
    step_key: str = Field(min_length=1, max_length=128)
    phase: Literal["prepare", "confirm"]
    date: str = Field(min_length=10, max_length=10)
    result: dict[str, Any]


@router.post("/api/v1/internal/resign/checkpoint", include_in_schema=False)
def resign_checkpoint(
    payload: ResignCheckpointRequest, request: Request, authorization: str = Header(default=""),
):
    if not authorization.startswith("Bearer ") or len(authorization) > 200:
        raise HTTPException(403, "Resign request denied")
    executor = request.app.state.executor
    with executor.session_factory() as session:
        try:
            job = executor.secret_providers.authorize_resign(
                authorization[7:], payload.job_id, payload.site_id, payload.step_key, session,
            )
            result = payload.result.get("resign")
            if not isinstance(result, dict):
                raise ValueError("Invalid resign result")
            ResignService(session, executor.settings).checkpoint(job, payload.phase, payload.date, result)
            executor.secret_providers.authorize_resign(
                authorization[7:], payload.job_id, payload.site_id, payload.step_key, session,
            )
            session.commit()
        except SecretDenied:
            session.rollback()
            raise HTTPException(403, "Resign request denied") from None
        except (ResignConflict, IntegrityError):
            session.rollback()
            raise HTTPException(409, "Resign date already reserved or not owned") from None
        except OperationalError as exc:
            session.rollback()
            if session.bind.dialect.name == "sqlite" and any(
                value in str(exc.orig).lower() for value in ("locked", "busy")
            ):
                raise HTTPException(409, "Resign reservation conflicted") from None
            raise
        except ValueError:
            session.rollback()
            raise HTTPException(422, "Invalid resign checkpoint") from None
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})


@router.post("/api/v1/internal/plugins/authorize", include_in_schema=False)
def authorize_plugin(payload: PluginAuthorizationRequest, request: Request, authorization: str = Header(default="")):
    if not authorization.startswith("Bearer ") or len(authorization) > 200:
        raise HTTPException(403, "Plugin request denied")
    try:
        request.app.state.executor.secret_providers.authorize_plugin(authorization[7:], **payload.model_dump())
    except SecretDenied:
        raise HTTPException(403, "Plugin request denied") from None
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})


@router.post("/api/v1/internal/secrets/resolve", include_in_schema=False)
async def resolve(payload: SecretRequest, request: Request, authorization: str = Header(default="")):
    if not authorization.startswith("Bearer ") or len(authorization) > 200:
        raise HTTPException(403, "Secret request denied")
    try:
        result = await request.app.state.executor.secret_providers.request(authorization[7:], **payload.model_dump())
    except SecretDenied:
        logging.getLogger("siteflow.secrets").warning("secret request denied job=%s site=%s", payload.job_id, payload.site_id)
        raise HTTPException(403, "Secret request denied") from None
    return JSONResponse(result, headers={"Cache-Control": "no-store"})
