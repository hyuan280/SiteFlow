"""Read-only site-to-template candidates, shared by the editor and assistant."""

from pydantic import ValidationError
from sqlalchemy import select

from app.models.assistant_workflow import AssistantWorkflowAction
from app.models.execution import SiteJob
from app.schemas.template_site_import import TemplateSiteImport
from app.schemas.workflow import TemplateWrite
from app.services.workflows import WorkflowConflictError, WorkflowValidationError


def site_import_candidate(service, site_id: int) -> TemplateSiteImport:
    from app.services.assistant_run_guard import unresolved_assistant_runs
    from app.services.workflow_drafts import _DraftWorkflowService

    db = service.session
    with db.no_autoflush:
        # Detached reads avoid stale list caches and never flush caller changes.
        context = _DraftWorkflowService(db, service.settings, service.crypto, service.actor)
        context._preload_site_list()
        site = context._get_site(site_id)
        if db.scalar(select(SiteJob.id).where(
                SiteJob.site_id == site_id, SiteJob.status.in_(("queued", "running")))):
            raise WorkflowConflictError("Source site has an active job")
        if db.scalar(select(SiteJob.id).where(
                SiteJob.site_id == site_id,
                SiteJob.status.in_(("unknown", "interrupted", "waiting_human")))):
            raise WorkflowConflictError("Source site has an uncertain execution outcome")
        if (unresolved_assistant_runs(db, site_id=site_id)
                or db.scalar(select(AssistantWorkflowAction.id).where(
                    AssistantWorkflowAction.site_id == site_id,
                    AssistantWorkflowAction.kind == "run",
                    AssistantWorkflowAction.state.in_(("reserved", "unknown"))))):
            raise WorkflowConflictError("Source site has an uncertain execution outcome")
        context.resolve_site(site_id)
        local = context._local_snapshot(site)
        try:
            candidate = TemplateSiteImport.model_validate({
                "parent_id": site.template_id,
                **{key: local[key] for key in ("steps", "groups", "signals")},
            })
        except ValidationError:
            raise WorkflowValidationError("Source site local workflow is invalid") from None
        if candidate.parent_id is None and not candidate.steps:
            raise WorkflowValidationError("Source site has no workflow to import")
        return candidate


def template_import_write(service, candidate, *, name, description="", destination_id=None,
                          expected_revision=None, expected_history_key=None):
    values = candidate.model_dump()
    if destination_id is None:
        values.update(name=name, description=description)
    else:
        destination = service._get_template(destination_id)
        values.update(name=destination.name, description=destination.description,
                      enabled=destination.enabled, expected_revision=expected_revision,
                      expected_history_key=expected_history_key)
    write = TemplateWrite.model_validate(values)
    service.validate_draft("template", write, destination_id, preserve_local=True)
    return write
