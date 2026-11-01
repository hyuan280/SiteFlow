from fastapi import APIRouter, Depends, Query

from app.api.v1.sites import _raise, get_service
from app.api.deps import get_current_session
from app.schemas.workflow import WorkflowVersionDetail, WorkflowVersionList
from app.services.workflows import WorkflowNotFoundError, WorkflowService, WorkflowValidationError


# SiteFlow currently has only authenticated administrators.
router = APIRouter(prefix="/api/v1/workflow-history", tags=["workflow"],
                   dependencies=[Depends(get_current_session)])


@router.get("/{history_key}/versions", response_model=WorkflowVersionList)
def list_archived_versions(
    history_key: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    service: WorkflowService = Depends(get_service),
) -> WorkflowVersionList:
    try:
        return service.list_archived_versions(history_key, limit=limit, offset=offset)
    except (WorkflowNotFoundError, WorkflowValidationError) as exc:
        _raise(exc)


@router.get("/{history_key}/versions/{revision}", response_model=WorkflowVersionDetail)
def get_archived_version(
    history_key: str, revision: int,
    service: WorkflowService = Depends(get_service),
) -> WorkflowVersionDetail:
    try:
        return service.get_archived_version(history_key, revision)
    except (WorkflowNotFoundError, WorkflowValidationError) as exc:
        _raise(exc)
