from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.auth import ensure_aware
from app.core.config import Settings
from app.models.execution import SiteJob
from app.models.workflow import Site, SiteResignRecord, utcnow

TERMINAL_RESIGN_STATUSES = {"success", "already_signed"}


class ResignService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        try:
            self.timezone = ZoneInfo(settings.scheduler_timezone)
        except ZoneInfoNotFoundError:
            self.timezone = ZoneInfo("Asia/Shanghai")

    def _start_utc(self, value: date) -> datetime:
        return datetime.combine(value, time.min, tzinfo=self.timezone).astimezone(timezone.utc)

    def _end_utc(self, value: date) -> datetime:
        return datetime.combine(value + timedelta(days=1), time.min, tzinfo=self.timezone).astimezone(timezone.utc)

    def pending_dates(self, site_id: int, days: int = 30) -> list[str]:
        site = self.session.get(Site, site_id)
        if site is None:
            return []
        days = max(1, min(days, 365))
        today = datetime.now(self.timezone).date()
        window_start = today - timedelta(days=days - 1)
        first_finished = self.session.scalar(
            select(func.min(SiteJob.finished_at)).where(
                SiteJob.site_id == site_id,
                SiteJob.finished_at.is_not(None),
            )
        )
        if first_finished is None:
            return []
        first_record = ensure_aware(first_finished).astimezone(self.timezone).date()
        start = max(window_start, first_record)
        if start >= today:
            return []
        jobs = self.session.scalars(
            select(SiteJob).where(
                SiteJob.site_id == site_id,
                SiteJob.finished_at.is_not(None),
                SiteJob.finished_at >= self._start_utc(start),
                SiteJob.finished_at < self._end_utc(today),
            )
        )
        successes: set[str] = set()
        for job in jobs:
            if job.finished_at is None or job.status != "success":
                continue
            value = ensure_aware(job.finished_at).astimezone(self.timezone).date()
            if start <= value < today:
                successes.add(value.isoformat())
        records = self.session.scalars(
            select(SiteResignRecord).where(
                SiteResignRecord.site_id == site_id,
                SiteResignRecord.resign_date >= start.isoformat(),
                SiteResignRecord.resign_date < today.isoformat(),
            )
        )
        handled = {
            row.resign_date
            for row in records
            if row.status in TERMINAL_RESIGN_STATUSES
        }
        missing = {
            (start + timedelta(days=offset)).isoformat()
            for offset in range((today - start).days)
        }
        return sorted(missing - successes - handled)

    def record_results(
        self,
        site_id: int | None,
        run_id: int | None,
        job_id: int | None,
        result: dict,
    ) -> None:
        if site_id is None:
            return
        now = utcnow()
        entries: list[tuple[str, str, str]] = []
        for item in result.get("success") or []:
            if isinstance(item, dict) and item.get("date"):
                entries.append((str(item["date"]), "success", str(item.get("message") or "补签成功")))
        for item in result.get("already_signed") or []:
            value = item.get("date") if isinstance(item, dict) else item
            if value:
                message = (
                    str(item.get("message") or "已经签到")
                    if isinstance(item, dict)
                    else "已经签到"
                )
                entries.append((str(value), "already_signed", message))
        for item in result.get("failed") or []:
            if isinstance(item, dict) and item.get("date"):
                entries.append((str(item["date"]), "failed", str(item.get("message") or "")))
        for value, status, message in entries:
            row = self.session.scalar(
                select(SiteResignRecord).where(
                    SiteResignRecord.site_id == site_id,
                    SiteResignRecord.resign_date == value,
                )
            )
            if row is None:
                row = SiteResignRecord(
                    site_id=site_id,
                    resign_date=value,
                    status=status,
                    message=message[:4000],
                    run_id=run_id,
                    job_id=job_id,
                    created_at=now,
                    updated_at=now,
                )
                self.session.add(row)
            else:
                row.status = status
                row.message = message[:4000]
                row.run_id = run_id
                row.job_id = job_id
                row.updated_at = now
