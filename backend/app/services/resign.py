from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.auth import ensure_aware
from app.core.config import Settings
from app.models.execution import SiteJob
from app.models.workflow import Site, SiteResignRecord, utcnow

TERMINAL_RESIGN_STATUSES = {"success", "already_signed"}


class ResignConflict(ValueError):
    pass


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

    def checkpoint(self, job: SiteJob, phase: str, value: str, result: dict) -> None:
        today = datetime.now(self.timezone).date()

        def valid_date(item):
            if not isinstance(item, str):
                return False
            try:
                parsed = date.fromisoformat(item)
            except ValueError:
                return False
            return parsed.isoformat() == item and parsed < today

        pending = result.get("pending")
        if (
            not valid_date(value) or not isinstance(pending, list)
            or any(not valid_date(item) for item in pending) or value not in pending
        ):
            raise ValueError("Invalid resign dates")
        dates = {}
        for key in ("success", "already_signed", "unconfirmed", "failed"):
            items = result.get(key, [])
            if not isinstance(items, list):
                raise ValueError("Invalid resign entries")
            dates[key] = []
            for item in items:
                if key != "already_signed" and not isinstance(item, dict):
                    raise ValueError("Invalid resign entries")
                day = item.get("date") if isinstance(item, dict) else item
                if not valid_date(day) or day not in pending:
                    raise ValueError("Invalid resign entries")
                dates[key].append(day)
        if phase == "prepare":
            if result.get("submit_attempted") is not True or value not in dates["unconfirmed"]:
                raise ValueError("Invalid resign preparation")
            if value in dates["success"] or value in dates["already_signed"]:
                raise ValueError("Invalid resign preparation")
        elif phase == "confirm":
            if value not in dates["success"] + dates["already_signed"]:
                raise ValueError("Invalid resign confirmation")
        else:
            raise ValueError("Invalid resign phase")

        row = self.session.scalar(select(SiteResignRecord).where(
            SiteResignRecord.site_id == job.site_id,
            SiteResignRecord.resign_date == value,
        ))
        now = utcnow()
        if phase == "prepare":
            if row is not None and row.status in TERMINAL_RESIGN_STATUSES | {"unknown"}:
                raise ResignConflict()
            site = self.session.get(Site, job.site_id)
            if (
                site is None or not site.support_resign or not site.resign_max_days
                or value not in self.pending_dates(site.id, site.resign_max_days)
            ):
                raise ValueError("Resign date outside current window")
            if row is None:
                self.session.add(SiteResignRecord(
                    site_id=job.site_id, resign_date=value, status="unknown",
                    message="Resign submission reserved; confirmation pending",
                    run_id=job.run_id, job_id=job.id, created_at=now, updated_at=now,
                ))
                self.session.flush()
            else:
                changed = self.session.execute(update(SiteResignRecord).where(
                    SiteResignRecord.id == row.id,
                    SiteResignRecord.status.not_in(TERMINAL_RESIGN_STATUSES | {"unknown"}),
                ).values(
                    status="unknown", message="Resign submission reserved; confirmation pending",
                    run_id=job.run_id, job_id=job.id, updated_at=now,
                ))
                if changed.rowcount != 1:
                    raise ResignConflict()
        else:
            # The cumulative state can contain read-only observations without reservations.
            # Only the explicit target is authorized and changed by this HTTP confirmation.
            if (
                row is None or row.job_id != job.id or row.run_id != job.run_id
                or row.status not in TERMINAL_RESIGN_STATUSES | {"unknown"}
            ):
                raise ResignConflict()
            if row.status in TERMINAL_RESIGN_STATUSES:
                return
            changed = self.session.execute(update(SiteResignRecord).where(
                SiteResignRecord.id == row.id, SiteResignRecord.status == "unknown",
                SiteResignRecord.job_id == job.id, SiteResignRecord.run_id == job.run_id,
            ).values(
                status="success" if value in dates["success"] else "already_signed",
                message="Resign completion confirmed", updated_at=now,
            ))
            if changed.rowcount != 1:
                raise ResignConflict()

    def record_results(
        self,
        site_id: int | None,
        run_id: int | None,
        job_id: int | None,
        result: dict,
        *,
        feature_type: str = "post.resign",
    ) -> None:
        if site_id is None:
            return
        now = utcnow()
        pending = result.get("pending")
        scoped = feature_type == "post.resign_click" and isinstance(pending, list)
        today = datetime.now(self.timezone).date()
        entries: list[tuple[str, str, str]] = []
        for item in result.get("unconfirmed") or []:
            if isinstance(item, dict) and item.get("date"):
                entries.append((str(item["date"]), "unknown", str(item.get("message") or "")))
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
            if feature_type == "post.resign_click" and status == "unknown":
                message = "Resign submission reserved; confirmation pending"
            if scoped:
                try:
                    parsed = date.fromisoformat(value)
                except ValueError:
                    continue
                if parsed.isoformat() != value or value not in pending or parsed >= today:
                    continue
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
                self.session.flush()
            else:
                if row.status in TERMINAL_RESIGN_STATUSES:
                    continue
                owned = row.job_id == job_id and row.run_id == run_id
                readonly_done = scoped and status == "already_signed"
                if not readonly_done and (
                    (row.status == "unknown" and (status == "failed" or not owned))
                    or (feature_type == "post.resign_click" and not owned)
                ):
                    continue
                values = {"status": status, "message": message[:4000], "updated_at": now}
                if feature_type != "post.resign_click" and row.status != "unknown":
                    values.update(run_id=run_id, job_id=job_id)
                # All writers compare the observed status and owner, including legacy results.
                # Read-only done observations may close a reservation but never acquire it.
                self.session.execute(update(SiteResignRecord).where(
                    SiteResignRecord.id == row.id, SiteResignRecord.status == row.status,
                    SiteResignRecord.job_id == row.job_id, SiteResignRecord.run_id == row.run_id,
                ).values(**values))
