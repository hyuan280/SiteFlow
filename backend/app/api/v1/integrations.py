from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import (
    get_crypto,
    get_current_session,
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

router = APIRouter(
    prefix="/api/v1/integrations",
    tags=["integrations"],
    dependencies=[Depends(get_current_session)],
)


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
        return service.update_vision(**payload.model_dump())
    except ConfigLockedError as exc:
        raise HTTPException(409, str(exc)) from exc
    except IntegrationValidationError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.delete("/vision", response_model=IntegrationsView)
def delete_vision(service: IntegrationService = Depends(get_service),
                  _current: AuthSession = Depends(require_csrf)):
    try:
        return service.delete_vision()
    except ConfigLockedError as exc:
        raise HTTPException(409, str(exc)) from exc


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
