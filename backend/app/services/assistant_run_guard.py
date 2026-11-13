from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.crypto import CryptoError, CryptoService
from app.features.catalog import FEATURES
from app.models.assistant import AssistantSession
from app.models.assistant_run import AssistantRunGuard
from app.models.assistant_workflow import AssistantWorkflowAction
from app.models.execution import JobStep, Run, SiteJob
from app.models.workflow import Site
from app.services.workflows import WorkflowService, WorkflowValidationError


def _fingerprint(service: WorkflowService, site: Site) -> tuple[str, str]:
    # The workflow snapshot includes local config and feature policy, never stored credentials.
    snapshot = service._snapshot(site)
    canonical = json.dumps(
        snapshot, allow_nan=False, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    )
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return fingerprint, snapshot["resolved"]["lane"] or "general"


def create_guard(
    session: Session, run_id: int, site_id: int, service: WorkflowService
) -> None:
    # Do not reuse resolution/policy caches from earlier assistant tool calls.
    current = WorkflowService(session, service.settings, service.crypto)
    site = current._get_site(site_id)
    fingerprint, lane = _fingerprint(current, site)
    jobs = list(session.scalars(select(SiteJob).where(
        SiteJob.run_id == run_id, SiteJob.site_id == site_id,
    )))
    if not jobs or any(
        job.lane != lane or job.attempt != 1 or job.status != "queued" for job in jobs
    ):
        raise WorkflowValidationError("Assistant run lane does not match the confirmed workflow")
    session.add(AssistantRunGuard(
        run_id=run_id,
        site_id=site.id,
        site_history_key=site.history_key,
        site_revision=site.config_revision,
        fingerprint=fingerprint,
    ))


def validate_guard(
    session: Session, run_id: int, site: Site, settings: Settings, crypto: CryptoService,
    *, check_consumption: bool = True,
) -> bool:
    guard = session.get(AssistantRunGuard, run_id)
    if guard is None or (
        guard.site_id != site.id
        or guard.site_history_key != site.history_key
        or guard.site_revision != site.config_revision
    ):
        return False
    try:
        fingerprint, lane = _fingerprint(WorkflowService(session, settings, crypto), site)
        jobs = list(session.scalars(select(SiteJob).where(
            SiteJob.run_id == run_id, SiteJob.site_id == site.id,
        )))
        fallback = {"general": "interaction", "proxy_general": "proxy_interaction"}.get(lane)
        from app.services.assistant_consumption import build_plan

        service = WorkflowService(session, settings, crypto)
        consuming = any(step.enabled and FEATURES[step.feature_type].category == "post"
                        for step in service.resolve_site(site.id).steps)
        plan = consumption_plan(session, run_id, crypto) if consuming else None
        current_plan = build_plan(session, service, site) if check_consumption else plan
        if consuming and plan is None:
            return False
        if current_plan != plan or (plan is not None and any(job.attempt != 1 for job in jobs)):
            return False
        return bool(jobs) and all(
            job.lane == lane or (job.attempt > 1 and job.lane == fallback)
            for job in jobs
        ) and guard.fingerprint == fingerprint
    except (CryptoError, ValueError, TypeError, KeyError):
        return False


def consumption_plan(session: Session, run_id: int, crypto: CryptoService):
    action = session.scalar(select(AssistantWorkflowAction).where(
        AssistantWorkflowAction.run_id == run_id, AssistantWorkflowAction.kind == "run",
    ))
    if action is None:
        return None
    from app.services.config_ai_generation import _strict_json

    saved = _strict_json(crypto.decrypt(
        f"assistant-workflow:{action.id}", action.ciphertext, action.nonce, action.key_version))
    return saved.get("consumption_plan")


def unresolved_assistant_runs(session: Session, site_id: int) -> list[int]:
    """Return guarded assistant run IDs that require explicit outcome resolution."""
    rows = session.execute(
        select(Run.id, Run.status, SiteJob.status, SiteJob.result_json, JobStep.data_json)
        .join(AssistantRunGuard, AssistantRunGuard.run_id == Run.id)
        .join(SiteJob, SiteJob.run_id == Run.id)
        .outerjoin(JobStep, JobStep.site_job_id == SiteJob.id)
        .where(
            Run.trigger == "assistant",
            AssistantRunGuard.site_id == site_id,
            SiteJob.site_id == site_id,
        )
    )
    unresolved = set()
    for run_id, run_status, job_status, result_json, step_json in rows:
        # A failed run may contain a dispatched job whose error did not prove no action.
        if run_status in {"unknown", "interrupted", "waiting_human"} or job_status in {
            "unknown", "interrupted", "waiting_human", "error",
        }:
            unresolved.add(run_id)
            continue
        for raw in (result_json, step_json):
            try:
                data = json.loads(raw or "{}")
            except (ValueError, TypeError):
                continue
            if isinstance(data, dict) and (
                data.get("reason") in ("assistant_outcome_uncertain", "outcome_uncertain")
                or data.get("code") == "outcome_uncertain"
                or data.get("outcome_uncertain") is True
            ):
                unresolved.add(run_id)
    return sorted(unresolved)


def require_enable_success(
    db: Session, owner: str, session_id: str, site: Site, service: WorkflowService,
    run_id: int | None = None,
) -> int:
    """Require current, conversation-owned full-run proof before changing enabled."""
    from app.services.assistant_retry import uncertain_operations
    from app.services.assistant_workflows import WorkflowDenied
    from app.services.config_ai_generation import _strict_json

    if run_id is not None and (type(run_id) is not int or run_id < 1):
        raise WorkflowDenied("enable_success_required")
    # Keep pending caller writes, but never accept proof against stale ORM rows.
    site_id = site.id
    db.flush()
    db.expire_all()
    site = db.get(Site, site_id)
    if site is None:
        raise WorkflowDenied("enable_success_required")
    conversation = db.scalar(select(AssistantSession).where(
        AssistantSession.id == session_id, AssistantSession.owner == owner,
    ))
    if conversation is None:
        raise WorkflowDenied("enable_success_required")
    if db.scalar(select(SiteJob.id).where(
        SiteJob.site_id == site.id, SiteJob.status.in_(("queued", "running")),
    )):
        raise WorkflowDenied("site_run_active")
    if (unresolved_assistant_runs(db, site.id)
            or db.scalar(select(AssistantWorkflowAction.id).where(
                AssistantWorkflowAction.site_id == site.id,
                AssistantWorkflowAction.site_history_key == site.history_key,
                AssistantWorkflowAction.state == "unknown",
            ))):
        raise WorkflowDenied("workflow_outcome_uncertain")

    def decrypt(row):
        kind = "session" if isinstance(row, AssistantSession) else "message"
        identifier = row.id if kind == "session" else row.uuid
        return _strict_json(service.crypto.decrypt(
            f"assistant:{kind}:{identifier}", row.ciphertext, row.nonce, row.key_version,
        ))

    try:
        data = decrypt(conversation)
        uncertain = uncertain_operations(SimpleNamespace(_decrypt=decrypt), db, session_id, data)
    except (CryptoError, ValueError, TypeError, KeyError, AttributeError):
        raise WorkflowDenied("workflow_outcome_uncertain") from None
    if uncertain:
        raise WorkflowDenied("retry_authorization_required")
    candidates = select(Run).join(
        AssistantWorkflowAction, AssistantWorkflowAction.run_id == Run.id,
    ).where(
        AssistantWorkflowAction.owner == owner,
        AssistantWorkflowAction.session_id == session_id,
        AssistantWorkflowAction.kind == "run",
        AssistantWorkflowAction.state == "complete",
        AssistantWorkflowAction.site_id == site.id,
        AssistantWorkflowAction.site_history_key == site.history_key,
        AssistantWorkflowAction.site_revision == site.config_revision,
        Run.trigger == "assistant", Run.status == "success",
        Run.finished_at.is_not(None), Run.cancel_requested.is_(False),
    ).order_by(Run.id.desc())
    if run_id is not None:
        candidates = candidates.where(Run.id == run_id)
    for run in db.scalars(candidates):
        jobs = list(db.scalars(select(SiteJob).where(SiteJob.run_id == run.id)))
        if (len(jobs) == 1 and jobs[0].site_id == site.id
                and jobs[0].status == "success" and jobs[0].finished_at is not None
                # Completed resign records legitimately change pending dates.
                and validate_guard(db, run.id, site, service.settings, service.crypto,
                                   check_consumption=False)):
            return run.id
    raise WorkflowDenied("enable_success_required")
