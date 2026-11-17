from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from app.api.deps import (
    get_crypto,
    get_current_session,
    get_readonly_current_session,
    get_db,
    get_settings,
    require_csrf,
)
from app.core.config import Settings
from app.core.crypto import CryptoService
from app.models.auth import AuthSession
from app.schemas.integration import (
    BitwardenUpdate,
    BitwardenView,
    CFBoxUpdate,
    IntegrationsView,
    IntegrationTestResult,
    ProxyUpdate,
    TwoFAuthUpdate,
    VisionUpdate,
)
from app.services.bitwarden import (
    BitwardenConfigLockedError,
    BitwardenService,
    BitwardenValidationError,
)
from app.services.integrations import (
    ConfigLockedError,
    IntegrationService,
    IntegrationValidationError,
)
from app.schemas.model_registry import ModelTestRequest, RegistryUpdate, RegistryView
from app.services.model_registry import ModelRegistryError, ModelRegistryService, strict_json


class _ModelRegistryRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request: Request):
            try:
                if request.method in {"PUT", "POST"}:
                    limit = 16 * 1024 if request.method == "POST" else 256 * 1024
                    chunks, size = [], 0
                    try:
                        async with asyncio.timeout(10):
                            async for chunk in request.stream():
                                size += len(chunk)
                                if size > limit:
                                    raise ModelRegistryError("request_limit", 413)
                                chunks.append(chunk)
                    except TimeoutError:
                        raise ModelRegistryError("request_timeout", 408) from None
                    request._body = b"".join(chunks)
                    try:
                        if request._body or request.method == "PUT":
                            parsed = strict_json(request._body)
                            if not isinstance(parsed, dict):
                                raise ValueError("expected object")
                    except (ValueError, TypeError, RecursionError):
                        raise ModelRegistryError("invalid_model_registry", 422) from None
                response = await handler(request)
            except RequestValidationError:
                response = JSONResponse(status_code=422, content={"detail": {"code": "invalid_model_registry"}})
            except ModelRegistryError as exc:
                response = JSONResponse(status_code=exc.status, content={"detail": {"code": exc.code}})
            except HTTPException as exc:
                exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
                raise
            response.headers["Cache-Control"] = "no-store"
            return response

        return safe_handler


models_router = APIRouter(
    prefix="/models", tags=["integrations"],
    dependencies=[Depends(get_readonly_current_session)], route_class=_ModelRegistryRoute,
)


def get_model_registry_service(
    session: Session = Depends(get_db), settings: Settings = Depends(get_settings),
    crypto: CryptoService = Depends(get_crypto),
) -> ModelRegistryService:
    return ModelRegistryService(session, settings, crypto)


def model_mutation_owner(
    request: Request, current: AuthSession = Depends(get_readonly_current_session),
) -> str:
    require_csrf(request, current)
    return current.username


@models_router.get("", response_model=RegistryView)
def get_models(service: ModelRegistryService = Depends(get_model_registry_service)):
    return service.view()


@models_router.put("", response_model=RegistryView)
def update_models(
    payload: RegistryUpdate, owner: str = Depends(model_mutation_owner),
    service: ModelRegistryService = Depends(get_model_registry_service),
):
    return service.update(payload, owner)


@models_router.post("/{model_id}/test", response_model=IntegrationTestResult)
def test_model(
    model_id: str, _owner: str = Depends(model_mutation_owner),
    service: ModelRegistryService = Depends(get_model_registry_service),
    payload: ModelTestRequest | None = None,
):
    return service.test(model_id, payload)

router = APIRouter(
    prefix="/api/v1/integrations",
    tags=["integrations"],
)
router.include_router(models_router)
# Legacy routes retain their existing session-touching dependency.
router.dependencies.append(Depends(get_current_session))


def get_service(
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    crypto: CryptoService = Depends(get_crypto),
) -> IntegrationService:
    return IntegrationService(session, settings, crypto)


@router.put("/vision", response_model=IntegrationsView)
def update_vision(payload: VisionUpdate, service: IntegrationService = Depends(get_service),
                  _current: AuthSession = Depends(require_csrf)):
    try:
        return service.update_vision(**payload.model_dump(exclude_unset=True))
    except ConfigLockedError as exc:
        raise HTTPException(409, str(exc)) from exc
    except IntegrationValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ModelRegistryError as exc:
        raise HTTPException(exc.status, {"code": exc.code}) from None


@router.delete("/vision", response_model=IntegrationsView)
def delete_vision(service: IntegrationService = Depends(get_service),
                  _current: AuthSession = Depends(require_csrf)):
    try:
        return service.delete_vision()
    except ConfigLockedError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ModelRegistryError as exc:
        raise HTTPException(exc.status, {"code": exc.code}) from None


@router.post("/vision/test", response_model=IntegrationTestResult)
def test_vision(service: IntegrationService = Depends(get_service),
                _current: AuthSession = Depends(require_csrf)):
    return service.test_vision()


def get_bitwarden_service(
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    crypto: CryptoService = Depends(get_crypto),
) -> BitwardenService:
    return BitwardenService(session, settings, crypto)


@router.get("", response_model=IntegrationsView)
def get_integrations(service: IntegrationService = Depends(get_service)) -> IntegrationsView:
    return service.get_integrations()


@router.put("/proxy", response_model=IntegrationsView)
def update_proxy(
    payload: ProxyUpdate,
    service: IntegrationService = Depends(get_service),
    _current: AuthSession = Depends(require_csrf),
) -> IntegrationsView:
    try:
        return service.update_proxy(payload.url)
    except ConfigLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrationValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/proxy", response_model=IntegrationsView)
def delete_proxy(
    service: IntegrationService = Depends(get_service),
    _current: AuthSession = Depends(require_csrf),
) -> IntegrationsView:
    try:
        return service.delete_proxy()
    except ConfigLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.put("/twofauth", response_model=IntegrationsView)
def update_twofauth(
    payload: TwoFAuthUpdate,
    service: IntegrationService = Depends(get_service),
    _current: AuthSession = Depends(require_csrf),
) -> IntegrationsView:
    try:
        return service.update_twofauth(url=payload.url, token=payload.token)
    except ConfigLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrationValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/cfbox", response_model=IntegrationsView)
def update_cfbox(
    payload: CFBoxUpdate,
    service: IntegrationService = Depends(get_service),
    _current: AuthSession = Depends(require_csrf),
) -> IntegrationsView:
    try:
        return service.update_cfbox(url=payload.url, api_key=payload.api_key)
    except ConfigLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrationValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/cfbox", response_model=IntegrationsView)
def delete_cfbox(
    service: IntegrationService = Depends(get_service),
    _current: AuthSession = Depends(require_csrf),
) -> IntegrationsView:
    try:
        return service.delete_cfbox()
    except ConfigLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/twofauth", response_model=IntegrationsView)
def delete_twofauth(
    service: IntegrationService = Depends(get_service),
    _current: AuthSession = Depends(require_csrf),
) -> IntegrationsView:
    try:
        return service.delete_twofauth()
    except ConfigLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/proxy/test", response_model=IntegrationTestResult)
def test_proxy(
    service: IntegrationService = Depends(get_service),
    _current: AuthSession = Depends(require_csrf),
) -> IntegrationTestResult:
    return service.test_proxy()


@router.post("/twofauth/test", response_model=IntegrationTestResult)
def test_twofauth(
    service: IntegrationService = Depends(get_service),
    _current: AuthSession = Depends(require_csrf),
) -> IntegrationTestResult:
    return service.test_twofauth()


@router.post("/cfbox/test", response_model=IntegrationTestResult)
def test_cfbox(
    service: IntegrationService = Depends(get_service),
    _current: AuthSession = Depends(require_csrf),
) -> IntegrationTestResult:
    return service.test_cfbox()


@router.put("/bitwarden", response_model=BitwardenView)
def update_bitwarden(
    payload: BitwardenUpdate,
    service: BitwardenService = Depends(get_bitwarden_service),
    _current: AuthSession = Depends(require_csrf),
) -> BitwardenView:
    try:
        return service.update(url=payload.url, token=payload.token, enabled=payload.enabled)
    except BitwardenConfigLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BitwardenValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/bitwarden", response_model=BitwardenView)
def delete_bitwarden(
    service: BitwardenService = Depends(get_bitwarden_service),
    _current: AuthSession = Depends(require_csrf),
) -> BitwardenView:
    try:
        return service.delete()
    except BitwardenConfigLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/bitwarden/test", response_model=IntegrationTestResult)
def test_bitwarden(
    service: BitwardenService = Depends(get_bitwarden_service),
    _current: AuthSession = Depends(require_csrf),
) -> IntegrationTestResult:
    return service.test()
