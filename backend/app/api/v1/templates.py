from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.api.deps import get_crypto, get_current_session, get_db, get_settings, require_csrf
from app.core.config import Settings
from app.core.crypto import CryptoService
from app.models.auth import AuthSession
from app.schemas.workflow import (
    ResolvedWorkflowView,
    TemplateDetail,
    TemplateSummary,
    TemplateWrite,
)
from app.services.workflows import (
    WorkflowConflictError,
    WorkflowNotFoundError,
    WorkflowService,
    WorkflowValidationError,
)

router = APIRouter(
    prefix="/api/v1/templates",
    tags=["workflow"],
    dependencies=[Depends(get_current_session)],
)


def get_service(
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    crypto: CryptoService = Depends(get_crypto),
) -> WorkflowService:
    return WorkflowService(session, settings, crypto)


def _raise(exc: Exception) -> None:
    if isinstance(exc, WorkflowNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, WorkflowConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("", response_model=list[TemplateSummary])
def list_templates(service: WorkflowService = Depends(get_service)) -> list[TemplateSummary]:
    return service.list_templates()


@router.post("", response_model=TemplateDetail, status_code=201)
def create_template(
    payload: TemplateWrite,
    service: WorkflowService = Depends(get_service),
    _current: AuthSession = Depends(require_csrf),
) -> TemplateDetail:
    try:
        return service.create_template(payload)
    except (WorkflowNotFoundError, WorkflowConflictError, WorkflowValidationError) as exc:
        _raise(exc)


@router.get("/{template_id}", response_model=TemplateDetail)
def get_template(
    template_id: int,
    service: WorkflowService = Depends(get_service),
) -> TemplateDetail:
    try:
        return service.get_template(template_id)
    except WorkflowNotFoundError as exc:
        _raise(exc)


@router.put("/{template_id}", response_model=TemplateDetail)
def update_template(
    template_id: int,
    payload: TemplateWrite,
    service: WorkflowService = Depends(get_service),
    _current: AuthSession = Depends(require_csrf),
) -> TemplateDetail:
    try:
        return service.update_template(template_id, payload)
    except (WorkflowNotFoundError, WorkflowConflictError, WorkflowValidationError) as exc:
        _raise(exc)


@router.delete("/{template_id}", status_code=204)
def delete_template(
    template_id: int,
    service: WorkflowService = Depends(get_service),
    _current: AuthSession = Depends(require_csrf),
) -> Response:
    try:
        service.delete_template(template_id)
    except (WorkflowNotFoundError, WorkflowConflictError) as exc:
        _raise(exc)
    return Response(status_code=204)


@router.get("/{template_id}/resolved", response_model=ResolvedWorkflowView)
def resolve_template(
    template_id: int,
    service: WorkflowService = Depends(get_service),
) -> ResolvedWorkflowView:
    try:
        return service.resolve_template(template_id)
    except (WorkflowNotFoundError, WorkflowValidationError) as exc:
        _raise(exc)
