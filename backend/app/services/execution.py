from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.crypto import CryptoService
from app.models.execution import JobArtifact, JobStep, Run, SiteJob
from app.models.workflow import Site
from app.schemas.execution import (
    ArtifactView,
    JobStepView,
    RunCreate,
    RunDetail,
    RunSummary,
    SiteJobView,
)
from app.services.workflows import WorkflowError, WorkflowService

FINAL_RUN_STATUSES = {
    "success",
    "fail",
    "error",
    "unknown",
    "waiting_human",
    "canceled",
    "interrupted",
}
FINAL_JOB_STATUSES = FINAL_RUN_STATUSES | {"skipped"}
RUN_STATUS_LABELS = {
    "queued": "排队中",
    "running": "运行中",
    "success": "成功",
    "fail": "失败",
    "error": "错误",
    "unknown": "未知",
    "waiting_human": "等待人工",
    "canceled": "已取消",
    "interrupted": "已中断",
}


class ExecutionError(ValueError):
    pass


class ExecutionValidationError(ExecutionError):
    pass


class ExecutionNotFoundError(ExecutionError):
    pass


class ExecutionConflictError(ExecutionError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return fallback


class ExecutionService:
    def __init__(self, session: Session, settings: Settings, crypto: CryptoService) -> None:
        self.session = session
        self.settings = settings
        self.crypto = crypto

    def _run(self, run_id: int) -> Run:
        row = self.session.get(Run, run_id)
        if row is None:
            raise ExecutionNotFoundError(f"运行 {run_id} 不存在")
        return row

    def _jobs(self, run_id: int) -> list[SiteJob]:
        return list(
            self.session.scalars(
                select(SiteJob).where(SiteJob.run_id == run_id).order_by(SiteJob.id)
            )
        )

    def _steps(self, job_id: int) -> list[JobStep]:
        return list(
            self.session.scalars(
                select(JobStep).where(JobStep.site_job_id == job_id).order_by(JobStep.id)
            )
        )

    def _artifacts(self, job_id: int) -> list[JobArtifact]:
        return list(
            self.session.scalars(
                select(JobArtifact)
                .where(JobArtifact.site_job_id == job_id)
                .order_by(JobArtifact.id)
            )
        )

    @staticmethod
    def _counts(jobs: list[SiteJob]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for job in jobs:
            counts[job.status] = counts.get(job.status, 0) + 1
        return counts

    def _summary(self, run: Run) -> RunSummary:
        jobs = self._jobs(run.id)
        return RunSummary(
            id=run.id,
            mode=run.mode,
            trigger=run.trigger,
            status=run.status,
            force=run.force,
            cancel_requested=run.cancel_requested,
            requested_sites=[int(item) for item in _json(run.requested_sites_json, [])],
            summary=_json(run.summary_json, {}) or self._counts(jobs),
            site_count=len(jobs),
            site_names=[job.site_name for job in jobs],
            created_by=run.created_by,
            created_at=run.created_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
        )

    def _job_view(self, job: SiteJob) -> SiteJobView:
        return SiteJobView(
            id=job.id,
            site_id=job.site_id,
            site_name=job.site_name,
            lane=job.lane,
            attempt=job.attempt,
            status=job.status,
            message=job.message,
            result=_json(job.result_json, {}),
            started_at=job.started_at,
            finished_at=job.finished_at,
            steps=[
                JobStepView(
                    step_key=step.step_key,
                    feature_type=step.feature_type,
                    label=step.label,
                    status=step.status,
                    message=step.message,
                    data=_json(step.data_json, {}),
                    started_at=step.started_at,
                    finished_at=step.finished_at,
                )
                for step in self._steps(job.id)
            ],
            artifacts=[
                ArtifactView(
                    id=artifact.id,
                    step_key=artifact.step_key,
                    kind=artifact.kind,
                    label=artifact.label,
                    content_type=artifact.content_type,
                    size_bytes=artifact.size_bytes,
                    sha256=artifact.sha256,
                    created_at=artifact.created_at,
                )
                for artifact in self._artifacts(job.id)
            ],
        )

    def get_run(self, run_id: int) -> RunDetail:
        run = self._run(run_id)
        summary = self._summary(run)
        return RunDetail(
            **summary.model_dump(),
            jobs=[self._job_view(job) for job in self._jobs(run_id)],
        )

    def list_runs(self, limit: int = 50) -> list[RunSummary]:
        rows = self.session.scalars(select(Run).order_by(Run.id.desc()).limit(limit)).all()
        return [self._summary(row) for row in rows]

    def _today_bounds_utc(self, now: datetime | None = None) -> tuple[datetime, datetime]:
        try:
            tz = ZoneInfo(self.settings.scheduler_timezone)
        except ZoneInfoNotFoundError:
            tz = ZoneInfo("Asia/Shanghai")
        current = now or _utcnow()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        today = current.astimezone(tz).date()
        start = datetime.combine(today, time.min, tzinfo=tz).astimezone(timezone.utc)
        end = datetime.combine(today + timedelta(days=1), time.min, tzinfo=tz).astimezone(
            timezone.utc
        )
        return start, end

    def today_excluded_site_ids(self, now: datetime | None = None) -> set[int]:
        """Sites successful in the scheduler's calendar day, or currently active."""
        start, end = self._today_bounds_utc(now)
        successful = set(
            self.session.scalars(
                select(SiteJob.site_id).where(
                    SiteJob.site_id.is_not(None),
                    SiteJob.status == "success",
                    SiteJob.finished_at.is_not(None),
                    SiteJob.finished_at >= start,
                    SiteJob.finished_at < end,
                )
            ).all()
        )
        active = set(
            self.session.scalars(
                select(SiteJob.site_id).where(
                    SiteJob.site_id.is_not(None),
                    SiteJob.status.in_(("queued", "running")),
                )
            ).all()
        )
        return {site_id for site_id in successful | active if site_id is not None}

    def create_run(
        self,
        payload: RunCreate,
        created_by: str,
        *,
        mode: str = "manual",
        trigger: str = "manual",
    ) -> RunDetail:
        if payload.today_unsuccessful and payload.site_ids is not None:
            raise ExecutionValidationError("不能同时指定站点和今日未成功模式")
        explicit_sites = payload.site_ids is not None
        if payload.site_ids is not None:
            site_ids = list(dict.fromkeys(payload.site_ids))
            if not site_ids:
                raise ExecutionValidationError("请选择至少一个站点")
            rows = list(
                self.session.scalars(select(Site).where(Site.id.in_(site_ids))).all()
            )
            by_id = {row.id: row for row in rows}
            missing = [site_id for site_id in site_ids if site_id not in by_id]
            if missing:
                raise ExecutionNotFoundError(
                    f"站点不存在: {', '.join(str(item) for item in missing)}"
                )
            sites = [by_id[site_id] for site_id in site_ids]
        else:
            sites = list(
                self.session.scalars(
                    select(Site).where(Site.enabled.is_(True)).order_by(Site.id)
                ).all()
            )
            if not sites:
                raise ExecutionValidationError("没有启用的站点")
            if payload.today_unsuccessful:
                completed = self.today_excluded_site_ids()
                sites = [site for site in sites if site.id not in completed]
                if not sites:
                    raise ExecutionValidationError("今日没有未成功的启用站点")

        workflow = WorkflowService(self.session, self.settings, self.crypto)
        resolved_by_site: dict[int, Any] = {}
        for site in sites:
            if not site.enabled:
                raise ExecutionValidationError(f"站点未启用: {site.name}")
            try:
                resolved = workflow.resolve_site(site.id)
            except WorkflowError as exc:
                if explicit_sites:
                    raise ExecutionValidationError(f"{site.name}: {exc}") from exc
                continue
            if not resolved.steps:
                if explicit_sites:
                    raise ExecutionValidationError(f"{site.name}: 没有可执行步骤")
                continue
            resolved_by_site[site.id] = resolved
        if not resolved_by_site:
            raise ExecutionValidationError("没有可执行的启用站点")
        sites = [site for site in sites if site.id in resolved_by_site]

        run = Run(
            mode=mode,
            trigger=trigger,
            status="queued",
            requested_sites_json=json.dumps([site.id for site in sites]),
            force=payload.force,
            created_by=created_by,
            summary_json="{}",
        )
        self.session.add(run)
        self.session.flush()
        for site in sites:
            resolved = resolved_by_site[site.id]
            self.session.add(
                SiteJob(
                    run_id=run.id,
                    site_id=site.id,
                    site_name=site.name,
                    lane=resolved.lane or "general",
                    status="queued",
                    message="等待执行",
                )
            )
        self.session.commit()
        return self.get_run(run.id)

    def cancel_run(self, run_id: int) -> RunDetail:
        run = self._run(run_id)
        if run.status in FINAL_RUN_STATUSES:
            label = RUN_STATUS_LABELS.get(run.status, run.status)
            raise ExecutionConflictError(f"运行已结束，不能取消: {label}")
        run.cancel_requested = True
        now = _utcnow()
        if run.status == "queued":
            run.status = "canceled"
            run.finished_at = now
            for job in self._jobs(run_id):
                if job.status == "queued":
                    job.status = "canceled"
                    job.message = "运行已取消"
                    job.finished_at = now
            run.summary_json = json.dumps(self._counts(self._jobs(run_id)))
        self.session.commit()
        return self.get_run(run_id)

    def recover_interrupted(self) -> None:
        now = _utcnow()
        runs = self.session.scalars(
            select(Run).where(Run.status.in_(("queued", "running")))
        ).all()
        for run in runs:
            run.status = "interrupted"
            run.cancel_requested = True
            run.finished_at = now
            jobs = self._jobs(run.id)
            for job in jobs:
                if job.status not in FINAL_JOB_STATUSES:
                    job.status = "interrupted"
                    job.message = "API 重启，执行已中断"
                    job.finished_at = now
                    for step in self._steps(job.id):
                        if step.status == "running":
                            step.status = "interrupted"
                            step.message = "API 重启，执行已中断"
                            step.finished_at = now
            run.summary_json = json.dumps(self._counts(jobs))
        if runs:
            self.session.commit()
