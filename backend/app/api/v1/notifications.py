from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_crypto, get_current_session, get_db, get_settings, require_csrf
from app.core.config import Settings
from app.core.crypto import CryptoService
from app.models.auth import AuthSession
from app.schemas.notification import (
    EmailNotificationUpdate,
    HTTPNotificationUpdate,
    NotificationLayoutUpdate,
    NotificationSettingsView,
    NotificationTestResult,
)
from app.services.notifications import (
    NotificationConfigLockedError,
    NotificationService,
    NotificationValidationError,
)

router = APIRouter(
    prefix="/api/v1/notifications",
    tags=["notifications"],
    dependencies=[Depends(get_current_session)],
)


def get_service(
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    crypto: CryptoService = Depends(get_crypto),
) -> NotificationService:
    return NotificationService(session, settings, crypto)


@router.get("", response_model=NotificationSettingsView)
def get_notification_settings(
    service: NotificationService = Depends(get_service),
) -> NotificationSettingsView:
    return service.view()


@router.put("/layout", response_model=NotificationSettingsView)
def update_notification_layout(
    payload: NotificationLayoutUpdate,
    service: NotificationService = Depends(get_service),
    _current: AuthSession = Depends(require_csrf),
) -> NotificationSettingsView:
    try:
        return service.update_layout(payload)
    except NotificationValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/email", response_model=NotificationSettingsView)
def update_email_notification(
    payload: EmailNotificationUpdate,
    service: NotificationService = Depends(get_service),
    _current: AuthSession = Depends(require_csrf),
) -> NotificationSettingsView:
    try:
        return service.update_email(payload)
    except NotificationConfigLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except NotificationValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/email", response_model=NotificationSettingsView)
def delete_email_notification(
    service: NotificationService = Depends(get_service),
    _current: AuthSession = Depends(require_csrf),
) -> NotificationSettingsView:
    return service.delete_email()


@router.put("/http", response_model=NotificationSettingsView)
def update_http_notification(
    payload: HTTPNotificationUpdate,
    service: NotificationService = Depends(get_service),
    _current: AuthSession = Depends(require_csrf),
) -> NotificationSettingsView:
    try:
        return service.update_http(payload)
    except NotificationConfigLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except NotificationValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/http", response_model=NotificationSettingsView)
def delete_http_notification(
    service: NotificationService = Depends(get_service),
    _current: AuthSession = Depends(require_csrf),
) -> NotificationSettingsView:
    return service.delete_http()


@router.post("/email/test", response_model=NotificationTestResult)
def test_email_notification(
    service: NotificationService = Depends(get_service),
    _current: AuthSession = Depends(require_csrf),
) -> NotificationTestResult:
    return service.test_channel("email")


@router.post("/http/test", response_model=NotificationTestResult)
def test_http_notification(
    service: NotificationService = Depends(get_service),
    _current: AuthSession = Depends(require_csrf),
) -> NotificationTestResult:
    return service.test_channel("http")
