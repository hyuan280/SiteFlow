from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from app.api.deps import (
    get_crypto, get_db, get_readonly_current_session, get_settings, require_csrf,
)
from app.api.v1.sites import _raise
from app.core.config import Settings
from app.core.crypto import CryptoService
from app.models.auth import AuthSession
from app.schemas.workflow import SiteWrite, TemplateWrite, WorkflowDraftValidation
from app.services.workflows import WorkflowError, WorkflowService


class _DraftRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def no_store(request: Request):
            try:
                response = await handler(request)
            except HTTPException as exc:
                exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
                raise
            except RequestValidationError as exc:
                response = await request_validation_exception_handler(request, exc)
            response.headers["Cache-Control"] = "no-store"
            return response

        return no_store


def get_service(
    request: Request,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    crypto: CryptoService = Depends(get_crypto),
    current: AuthSession = Depends(get_readonly_current_session),
) -> WorkflowService:
    require_csrf(request, current)
    return WorkflowService(session, settings, crypto, actor=current)


# Registered before the integer-ID routes; authentication does not update session activity.
router = APIRouter(prefix="/api/v1", tags=["workflow"], route_class=_DraftRoute)


def _validate(service, owner_type, payload, owner_id=None):
    try:
        return service.validate_draft(owner_type, payload, owner_id)
    except WorkflowError as exc:
        _raise(exc)


@router.post("/sites/validate", response_model=WorkflowDraftValidation)
def validate_site_draft(payload: SiteWrite, service: WorkflowService = Depends(get_service)):
    return _validate(service, "site", payload)


@router.post("/sites/{site_id}/validate", response_model=WorkflowDraftValidation)
def validate_site_update_draft(
    site_id: int, payload: SiteWrite, service: WorkflowService = Depends(get_service),
):
    return _validate(service, "site", payload, site_id)


@router.post("/templates/validate", response_model=WorkflowDraftValidation)
def validate_template_draft(payload: TemplateWrite, service: WorkflowService = Depends(get_service)):
    return _validate(service, "template", payload)


@router.post("/templates/{template_id}/validate", response_model=WorkflowDraftValidation)
def validate_template_update_draft(
    template_id: int, payload: TemplateWrite, service: WorkflowService = Depends(get_service),
):
    return _validate(service, "template", payload, template_id)
