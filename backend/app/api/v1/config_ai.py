from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_readonly_current_session
from app.api.v1.sites import _raise
from app.api.v1.workflow_drafts import _DraftRoute, get_service
from app.features.base import Category
from app.services.config_ai_catalog import ConfigAICatalog
from app.services.config_ai_protocol import generation_guide, tool_definitions
from app.schemas.config_ai import GenerateSiteDraftRequest, GenerationResult
from app.services.config_ai_generation import GenerationError, generate_site_draft
from app.services.config_ai_tools import ConfigAITools
from app.services.integrations import IntegrationService
from app.services.workflows import WorkflowError, WorkflowService


class _ConfigAIRoute(_DraftRoute):
    def get_route_handler(self):
        handler = APIRoute.get_route_handler(self)

        async def no_store(request: Request):
            is_generate = request.method == "POST" and self.path == "/api/v1/config-ai/generate"
            try:
                if is_generate:
                    chunks = []
                    size = 0
                    try:
                        async with asyncio.timeout(10):
                            async for chunk in request.stream():
                                size += len(chunk)
                                if size > 128 * 1024:
                                    raise HTTPException(413, detail={"code": "request_limit"})
                                chunks.append(chunk)
                    except TimeoutError:
                        raise HTTPException(408, detail={"code": "request_timeout"}) from None
                    # FastAPI must parse the body after the bounded stream has been consumed.
                    request._body = b"".join(chunks)
                response = await handler(request)
            except HTTPException as exc:
                exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
                raise
            except RequestValidationError as exc:
                if is_generate:
                    response = JSONResponse(status_code=422, content={"detail": {"code": "invalid_request"}})
                else:
                    response = await request_validation_exception_handler(request, exc)
            response.headers["Cache-Control"] = "no-store"
            return response

        return no_store


router = APIRouter(
    prefix="/api/v1/config-ai", tags=["config-ai"], route_class=_ConfigAIRoute,
    dependencies=[Depends(get_readonly_current_session)],
)


@router.get("/guide")
def get_guide() -> dict:
    return generation_guide()


@router.get("/tools")
def get_tools() -> dict:
    return {"version": 1, "execution_enabled": False, "tools": tool_definitions(),
            "generation_endpoint": "/api/v1/config-ai/generate",
            "execution_scope": "Tools execute only inside an enabled, authorized new-site generation request."}


@router.get("/features")
def list_features(category: Category | None = None, session: Session = Depends(get_db)) -> dict:
    return ConfigAICatalog(session).list_features(category)


@router.get("/features/{feature_type}")
def get_feature_schema(feature_type: str, session: Session = Depends(get_db)) -> dict:
    try:
        return ConfigAICatalog(session).get_feature_schema(feature_type)
    except WorkflowError as exc:
        _raise(exc)


@router.post("/generate", response_model=GenerationResult)
async def generate(
    payload: GenerateSiteDraftRequest,
    request: Request,
    service: WorkflowService = Depends(get_service),
) -> dict:
    if not service.settings.config_ai_enabled:
        raise HTTPException(503, detail={"code": "generation_disabled"})
    slot = request.app.state.config_ai_slot
    if not slot.acquire(blocking=False):
        raise HTTPException(429, detail={"code": "generation_busy"}, headers={"Retry-After": "5"})
    task = monitor = None
    try:
        try:
            ConfigAITools(service, payload.site, payload.allowed_features)
        except ValueError:
            raise HTTPException(422, detail={"code": "invalid_selection"}) from None
        try:
            config = IntegrationService(service.session, service.settings, service.crypto).model_runtime("assistant")
        except Exception:
            raise HTTPException(503, detail={"code": "model_unavailable"}) from None
        if config is None:
            raise HTTPException(503, detail={"code": "model_unavailable"})

        async def disconnected():
            while not await request.is_disconnected():
                await asyncio.sleep(0.25)

        task = asyncio.create_task(generate_site_draft(service, payload, config))
        monitor = asyncio.create_task(disconnected())
        completed, _ = await asyncio.wait({task, monitor}, return_when=asyncio.FIRST_COMPLETED)
        if monitor in completed:
            raise HTTPException(499, detail={"code": "generation_cancelled"})
        return await task
    except GenerationError as exc:
        detail = {"code": exc.code}
        if exc.reason is not None:
            detail["reason"] = exc.reason
        raise HTTPException(exc.http_status, detail=detail) from None
    finally:
        for pending in (task, monitor):
            if pending is not None and not pending.done():
                pending.cancel()
        await asyncio.gather(*(pending for pending in (task, monitor) if pending is not None),
                             return_exceptions=True)
        slot.release()
