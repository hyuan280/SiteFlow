from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.api.deps import get_crypto, get_current_session, get_db, get_settings, require_csrf
from app.core.config import Settings
from app.core.crypto import CryptoService
from app.models.auth import AuthSession
from app.schemas.credential import (
    SiteCookieUpdate,
    SiteCredentialUpdate,
    SiteCredentialView,
)
from app.schemas.workflow import (
    ResolvedWorkflowView,
    SiteDetail,
    SiteOptionView,
    SiteSummary,
    SiteWrite,
    WorkflowRestore,
    WorkflowVersionDetail,
    WorkflowVersionList,
)
from app.services.credentials import (
    CredentialError,
    CredentialNotFoundError,
    CredentialService,
)
from app.services.workflows import (
    WorkflowConflictError,
    WorkflowNotFoundError,
    WorkflowService,
    WorkflowValidationError,
)

router = APIRouter(
    prefix="/api/v1/sites",
    tags=["workflow"],
    dependencies=[Depends(get_current_session)],
)


def get_service(
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    crypto: CryptoService = Depends(get_crypto),
    actor: AuthSession = Depends(get_current_session),
) -> WorkflowService:
    return WorkflowService(session, settings, crypto, actor=actor)


def get_credential_service(
    session: Session = Depends(get_db),
    crypto: CryptoService = Depends(get_crypto),
) -> CredentialService:
    return CredentialService(session, crypto)


def _raise(exc: Exception) -> None:
    if isinstance(exc, (WorkflowNotFoundError, CredentialNotFoundError)):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, WorkflowConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("", response_model=list[SiteSummary] | list[SiteOptionView])
def list_sites(
    view: Literal["summary", "options"] = "summary",
    include_valid: bool = False,
    service: WorkflowService = Depends(get_service),
) -> list[SiteSummary] | list[SiteOptionView]:
    if view == "options":
        return service.list_site_options(include_valid=include_valid)
    return service.list_sites()


@router.post("", response_model=SiteDetail, status_code=201)
def create_site(
    payload: SiteWrite,
    service: WorkflowService = Depends(get_service),
    _current: AuthSession = Depends(require_csrf),
) -> SiteDetail:
    try:
        return service.create_site(payload)
    except (WorkflowNotFoundError, WorkflowConflictError, WorkflowValidationError) as exc:
        _raise(exc)


@router.get("/{site_id}", response_model=SiteDetail)
def get_site(site_id: int, service: WorkflowService = Depends(get_service)) -> SiteDetail:
    try:
        return service.get_site(site_id)
    except WorkflowNotFoundError as exc:
        _raise(exc)


@router.put("/{site_id}", response_model=SiteDetail)
def update_site(
    site_id: int,
    payload: SiteWrite,
    service: WorkflowService = Depends(get_service),
    _current: AuthSession = Depends(require_csrf),
) -> SiteDetail:
    try:
        return service.update_site(site_id, payload)
    except (WorkflowNotFoundError, WorkflowConflictError, WorkflowValidationError) as exc:
        _raise(exc)


@router.post("/{site_id}/config/import", response_model=SiteDetail)
def import_site_config(
    site_id: int,
    payload: SiteWrite,
    service: WorkflowService = Depends(get_service),
    _current: AuthSession = Depends(require_csrf),
) -> SiteDetail:
    try:
        return service.import_site_config(site_id, payload)
    except (WorkflowNotFoundError, WorkflowConflictError, WorkflowValidationError) as exc:
        _raise(exc)


@router.delete("/{site_id}", status_code=204)
def delete_site(
    site_id: int,
    expected_revision: int | None = Query(default=None, ge=1),
    expected_history_key: UUID | None = Query(default=None),
    service: WorkflowService = Depends(get_service),
    _current: AuthSession = Depends(require_csrf),
) -> Response:
    try:
        service.delete_site(site_id, expected_revision=expected_revision,
                            expected_history_key=expected_history_key)
    except (WorkflowNotFoundError, WorkflowConflictError, WorkflowValidationError) as exc:
        _raise(exc)
    return Response(status_code=204)


@router.get("/{site_id}/versions", response_model=WorkflowVersionList)
def list_site_versions(
    site_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    service: WorkflowService = Depends(get_service),
) -> WorkflowVersionList:
    try:
        return service.list_versions("site", site_id, limit=limit, offset=offset)
    except (WorkflowNotFoundError, WorkflowValidationError) as exc:
        _raise(exc)


@router.get("/{site_id}/versions/{revision}", response_model=WorkflowVersionDetail)
def get_site_version(
    site_id: int, revision: int,
    service: WorkflowService = Depends(get_service),
) -> WorkflowVersionDetail:
    try:
        return service.get_version("site", site_id, revision)
    except (WorkflowNotFoundError, WorkflowValidationError) as exc:
        _raise(exc)


@router.post("/{site_id}/versions/{revision}/restore", response_model=SiteDetail)
def restore_site_version(
    site_id: int, revision: int, payload: WorkflowRestore,
    service: WorkflowService = Depends(get_service),
    _current: AuthSession = Depends(require_csrf),
) -> SiteDetail:
    try:
        return service.restore_version("site", site_id, revision, payload)
    except (WorkflowNotFoundError, WorkflowConflictError, WorkflowValidationError) as exc:
        _raise(exc)


@router.get("/{site_id}/resolved", response_model=ResolvedWorkflowView)
def resolve_site(
    site_id: int,
    service: WorkflowService = Depends(get_service),
) -> ResolvedWorkflowView:
    try:
        return service.resolve_site(site_id)
    except (WorkflowNotFoundError, WorkflowValidationError) as exc:
        _raise(exc)


@router.get("/{site_id}/credential", response_model=SiteCredentialView)
def get_site_credential(
    site_id: int,
    service: CredentialService = Depends(get_credential_service),
) -> SiteCredentialView:
    try:
        return service.view(site_id)
    except CredentialNotFoundError as exc:
        _raise(exc)


@router.put("/{site_id}/credential", response_model=SiteCredentialView)
def update_site_credential(
    site_id: int,
    payload: SiteCredentialUpdate,
    service: CredentialService = Depends(get_credential_service),
    _current: AuthSession = Depends(require_csrf),
) -> SiteCredentialView:
    try:
        return service.update(
            site_id,
            username=payload.username,
            password=payload.password,
        )
    except (CredentialNotFoundError, CredentialError) as exc:
        _raise(exc)


@router.delete("/{site_id}/credential", status_code=204)
def delete_site_credential(
    site_id: int,
    service: CredentialService = Depends(get_credential_service),
    _current: AuthSession = Depends(require_csrf),
) -> Response:
    try:
        service.delete(site_id)
    except CredentialNotFoundError as exc:
        _raise(exc)
    return Response(status_code=204)


@router.put("/{site_id}/cookies", response_model=SiteCredentialView)
def update_site_cookies(
    site_id: int,
    payload: SiteCookieUpdate,
    service: CredentialService = Depends(get_credential_service),
    _current: AuthSession = Depends(require_csrf),
) -> SiteCredentialView:
    try:
        return service.update_cookies(site_id, payload.cookies)
    except (CredentialNotFoundError, CredentialError) as exc:
        _raise(exc)


@router.delete("/{site_id}/cookies", status_code=204)
def delete_site_cookies(
    site_id: int,
    service: CredentialService = Depends(get_credential_service),
    _current: AuthSession = Depends(require_csrf),
) -> Response:
    try:
        service.delete_cookies(site_id)
    except CredentialNotFoundError as exc:
        _raise(exc)
    return Response(status_code=204)
