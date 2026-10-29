from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import ensure_aware
from app.core.config import Settings
from app.models.execution import JobArtifact, JobStep, SiteJob
from app.models.workflow import Site, SiteResignRecord
from app.schemas.analytics import (
    FailureItemView,
    FailuresView,
    FailureStepView,
    MatrixCellView,
    MatrixSiteView,
    MatrixView,
    TrendPointView,
    TrendsView,
)

FINAL_STATUSES = {
    "success",
    "fail",
    "error",
    "unknown",
    "waiting_human",
    "deferred",
    "canceled",
    "interrupted",
}
STATUS_PRIORITY = [
    "success",
    "waiting_human",
    "unknown",
    "deferred",
    "fail",
    "error",
    "canceled",
    "interrupted",
]
FAILURE_STEP_STATUSES = {"fail", "error", "unknown", "waiting_human", "deferred"}


def _aggregate_status(counts: dict[str, int]) -> str:
    for status in STATUS_PRIORITY:
        if counts.get(status, 0) > 0:
            return status
    return "unknown"


class AnalyticsService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        try:
            self.timezone = ZoneInfo(settings.scheduler_timezone)
        except ZoneInfoNotFoundError:
            self.timezone = ZoneInfo("Asia/Shanghai")

    def _range(self, days: int) -> tuple[date, date, list[str]]:
        days = max(1, min(days, 365))
        end = datetime.now(self.timezone).date()
        start = end - timedelta(days=days - 1)
        dates: list[str] = []
        current = start
        while current <= end:
            dates.append(current.isoformat())
            current += timedelta(days=1)
        return start, end, dates

    def _start_utc(self, value: date) -> datetime:
        return datetime.combine(value, time.min, tzinfo=self.timezone).astimezone(timezone.utc)

    def _end_utc(self, value: date) -> datetime:
        return datetime.combine(value + timedelta(days=1), time.min, tzinfo=self.timezone).astimezone(timezone.utc)

    def _local_date(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        return ensure_aware(value).astimezone(self.timezone).date().isoformat()

    def _jobs(
        self,
        start: date,
        end: date,
        *,
        site_id: int | None = None,
        statuses: Iterable[str] | None = None,
    ) -> list[SiteJob]:
        query = select(SiteJob).where(
            SiteJob.finished_at.is_not(None),
            SiteJob.finished_at >= self._start_utc(start),
            SiteJob.finished_at < self._end_utc(end),
            SiteJob.status.in_(statuses or FINAL_STATUSES),
        )
        if site_id is not None:
            query = query.where(SiteJob.site_id == site_id)
        return list(self.session.scalars(query.order_by(SiteJob.finished_at.asc(), SiteJob.id.asc())))

    def matrix(
        self,
        days: int,
        *,
        site_id: int | None = None,
        include_disabled: bool = False,
    ) -> MatrixView:
        start, end, dates = self._range(days)
        site_rows = list(
            self.session.scalars(
                select(Site).order_by(Site.name.asc())
            )
        )
        if not include_disabled:
            site_rows = [site for site in site_rows if site.enabled]
        if site_id is not None:
            site_rows = [site for site in site_rows if site.id == site_id]
        sites = {
            site.id: MatrixSiteView(
                site_id=site.id,
                name=site.name,
                enabled=site.enabled,
                cells={},
            )
            for site in site_rows
        }
        buckets: dict[tuple[int, str], dict[str, object]] = {}
        success_keys: set[tuple[int, str]] = set()
        for job in self._jobs(start, end, site_id=site_id):
            if job.site_id is None or job.site_id not in sites:
                continue
            local_date = self._local_date(job.finished_at)
            if local_date is None:
                continue
            key = (job.site_id, local_date)
            if job.status == "success":
                success_keys.add(key)
            bucket = buckets.setdefault(
                key,
                {"counts": {}, "attempts": 0, "latest": None},
            )
            counts = bucket["counts"]
            assert isinstance(counts, dict)
            counts[job.status] = counts.get(job.status, 0) + 1
            bucket["attempts"] = int(bucket["attempts"]) + 1
            latest = bucket["latest"]
            if latest is None or (
                job.finished_at is not None
                and ensure_aware(job.finished_at) >= ensure_aware(latest.finished_at)  # type: ignore[union-attr]
            ):
                bucket["latest"] = job
        records = self.session.scalars(
            select(SiteResignRecord).where(
                SiteResignRecord.resign_date >= start.isoformat(),
                SiteResignRecord.resign_date <= end.isoformat(),
                SiteResignRecord.status.in_(("success", "already_signed")),
            )
        )
        for record in records:
            if record.site_id not in sites:
                continue
            key = (record.site_id, record.resign_date)
            if key in success_keys:
                continue
            bucket = buckets.setdefault(
                key,
                {"counts": {}, "attempts": 0, "latest": None},
            )
            counts = bucket["counts"]
            assert isinstance(counts, dict)
            counts["success"] = int(counts.get("success", 0)) + 1
            counts["resign"] = int(counts.get("resign", 0)) + 1
            if bucket["latest"] is None:
                bucket["resign_message"] = record.message or "补签成功"
                bucket["resign_run_id"] = record.run_id
                bucket["resign_job_id"] = record.job_id
        for (row_site_id, local_date), bucket in buckets.items():
            latest = bucket["latest"]
            counts = bucket["counts"]
            assert isinstance(counts, dict)
            resign_message = str(bucket.get("resign_message") or "")
            sites[row_site_id].cells[local_date] = MatrixCellView(
                status=_aggregate_status({str(k): int(v) for k, v in counts.items()}),
                attempts=int(bucket["attempts"]),
                counts={str(k): int(v) for k, v in counts.items()},
                last_run_id=latest.run_id if isinstance(latest, SiteJob) else bucket.get("resign_run_id"),
                last_job_id=latest.id if isinstance(latest, SiteJob) else bucket.get("resign_job_id"),
                last_message=(latest.message if isinstance(latest, SiteJob) else resign_message)[:4000],
            )
        return MatrixView(
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            dates=dates,
            sites=list(sites.values()),
        )

    def trends(self, days: int, *, site_id: int | None = None) -> TrendsView:
        start, end, dates = self._range(days)
        points = {
            value: {
                "total": 0,
                "success": 0,
                "fail": 0,
                "error": 0,
                "waiting_human": 0,
                "unknown": 0,
                "deferred": 0,
                "canceled": 0,
                "interrupted": 0,
            }
            for value in dates
        }
        success_keys: set[tuple[int | None, str]] = set()
        for job in self._jobs(start, end, site_id=site_id):
            local_date = self._local_date(job.finished_at)
            if local_date is None or local_date not in points:
                continue
            if job.status == "success":
                success_keys.add((job.site_id, local_date))
            point = points[local_date]
            point["total"] += 1
            if job.status in point:
                point[job.status] += 1
        records = self.session.scalars(
            select(SiteResignRecord).where(
                SiteResignRecord.resign_date >= start.isoformat(),
                SiteResignRecord.resign_date <= end.isoformat(),
                SiteResignRecord.status.in_(("success", "already_signed")),
            )
        )
        for record in records:
            if site_id is not None and record.site_id != site_id:
                continue
            local_date = record.resign_date
            if local_date not in points or (record.site_id, local_date) in success_keys:
                continue
            point = points[local_date]
            point["total"] += 1
            point["success"] += 1
        result: list[TrendPointView] = []
        totals = {key: 0 for key in points[dates[0]]}
        for value in dates:
            point = points[value]
            total = int(point["total"])
            success = int(point["success"])
            rate = round(success * 100 / total, 2) if total else 0.0
            result.append(
                TrendPointView(
                    date=value,
                    **point,
                    success_rate=rate,
                )
            )
            for key in totals:
                totals[key] += int(point[key])
        total = totals["total"]
        success = totals["success"]
        return TrendsView(
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            points=result,
            total=total,
            success=success,
            fail=totals["fail"],
            error=totals["error"],
            waiting_human=totals["waiting_human"],
            unknown=totals["unknown"],
            deferred=totals["deferred"],
            canceled=totals["canceled"],
            interrupted=totals["interrupted"],
            success_rate=round(success * 100 / total, 2) if total else 0.0,
        )

    def failures(
        self,
        days: int,
        *,
        site_id: int | None = None,
        statuses: set[str] | None = None,
        lane: str | None = None,
        feature_type: str | None = None,
        query: str | None = None,
        date_value: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> FailuresView:
        start, end, _ = self._range(days)
        jobs = self._jobs(start, end, site_id=site_id)
        latest_by_site_day: dict[tuple[int | str, str], SiteJob] = {}
        for job in jobs:
            local_date = self._local_date(job.finished_at)
            if local_date is None:
                continue
            if date_value and local_date != date_value:
                continue
            key: tuple[int | str, str] = (
                job.site_id if job.site_id is not None else f"name:{job.site_name}",
                local_date,
            )
            current = latest_by_site_day.get(key)
            if current is None or (
                job.finished_at is not None
                and current.finished_at is not None
                and (
                    ensure_aware(job.finished_at) > ensure_aware(current.finished_at)
                    or (
                        ensure_aware(job.finished_at) == ensure_aware(current.finished_at)
                        and job.id > current.id
                    )
                )
            ):
                latest_by_site_day[key] = job
        latest = list(latest_by_site_day.values())
        if statuses:
            latest = [job for job in latest if job.status in statuses]
        latest = [job for job in latest if job.status != "success"]
        if lane:
            latest = [job for job in latest if job.lane == lane]
        latest.sort(
            key=lambda job: (
                ensure_aware(job.finished_at) if job.finished_at is not None else start,
                job.id,
            ),
            reverse=True,
        )
        jobs = latest
        job_ids = [job.id for job in jobs]
        step_map: dict[int, list[JobStep]] = {job_id: [] for job_id in job_ids}
        if job_ids:
            for step in self.session.scalars(
                select(JobStep).where(
                    JobStep.site_job_id.in_(job_ids),
                    JobStep.status.in_(FAILURE_STEP_STATUSES),
                )
            ):
                step_map.setdefault(step.site_job_id, []).append(step)
        artifacts = {}
        if job_ids:
            for artifact in self.session.scalars(
                select(JobArtifact).where(
                    JobArtifact.site_job_id.in_(job_ids),
                    JobArtifact.kind == "screenshot",
                )
            ):
                artifacts.setdefault(artifact.site_job_id, artifact)
        needle = (query or "").strip().casefold()
        items: list[FailureItemView] = []
        for job in jobs:
            failed_steps = step_map.get(job.id, [])
            if feature_type and not any(
                feature_type.casefold() in step.feature_type.casefold()
                for step in failed_steps
            ):
                continue
            if needle:
                haystack = " ".join(
                    [
                        job.site_name,
                        job.message,
                        *(step.label for step in failed_steps),
                        *(step.step_key for step in failed_steps),
                        *(step.message for step in failed_steps),
                    ]
                ).casefold()
                if needle not in haystack:
                    continue
            screenshot = artifacts.get(job.id)
            items.append(
                FailureItemView(
                    job_id=job.id,
                    run_id=job.run_id,
                    site_id=job.site_id,
                    site_name=job.site_name,
                    lane=job.lane,
                    attempt=job.attempt,
                    status=job.status,
                    message=job.message,
                    started_at=job.started_at,
                    finished_at=job.finished_at,
                    failed_steps=[
                        FailureStepView(
                            step_key=step.step_key,
                            label=step.label or step.step_key,
                            feature_type=step.feature_type,
                            status=step.status,
                            message=step.message,
                        )
                        for step in failed_steps
                    ],
                    screenshot_id=screenshot.id if screenshot else None,
                )
            )
        total = len(items)
        page = max(1, page)
        page_size = max(1, min(page_size, 100))
        start_index = (page - 1) * page_size
        return FailuresView(
            total=total,
            page=page,
            page_size=page_size,
            items=items[start_index : start_index + page_size],
        )
