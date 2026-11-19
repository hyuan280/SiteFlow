from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, time, timedelta, timezone
from time import monotonic
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.crypto import CryptoService
from app.models.execution import Run
from app.models.workflow import Site
from app.schemas.execution import RunCreate
from app.services.execution import ExecutionService
from app.services.executor import ExecutionManager
from app.services.lanes import LANE_LABELS, LaneService
from app.services.scheduler_settings import (
    SchedulerSettingsService,
    next_random_run,
    random_range_target,
)
from app.services.workflows import WorkflowError, WorkflowService

logger = logging.getLogger("siteflow.scheduler")


class _ScheduledBrowserPreflight(Exception):
    def __init__(self, lanes: set[str]) -> None:
        self.lanes = frozenset(lanes)


class _ScheduledBrowserDeferred(Exception):
    pass


class Scheduler:
    def __init__(
        self,
        session_factory: sessionmaker,
        settings: Settings,
        crypto: CryptoService,
        executor: ExecutionManager,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.crypto = crypto
        self.executor = executor
        self._task: asyncio.Task[None] | None = None
        self._browser_admission_deferred = False
        try:
            self.timezone = ZoneInfo(settings.scheduler_timezone)
        except Exception:
            self.timezone = ZoneInfo("UTC")

    async def start(self) -> None:
        if self.settings.scheduler_enabled is False:
            return
        self._task = asyncio.create_task(self._loop(), name="siteflow-scheduler")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                run_ids = await self.scan_once_async()
                if run_ids:
                    logger.info("scheduled %s run(s): %s", len(run_ids), run_ids)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scheduler scan failed")
            await asyncio.sleep(max(5, self.settings.scheduler_poll_seconds))

    @staticmethod
    def _range_target(day, index: int, text: str, tz) -> tuple[datetime, datetime]:
        return random_range_target(day, index, text, tz)

    def _active_scheduled_run(self, session) -> bool:
        return bool(
            session.scalar(
                select(Run.id)
                .where(
                    Run.trigger == "schedule",
                    Run.status.in_(("queued", "running")),
                )
                .limit(1)
            )
        )

    def _eligible_sites(self, session, current: datetime) -> list[int]:
        workflow = WorkflowService(session, self.settings, self.crypto)
        excluded = ExecutionService(
            session, self.settings, self.crypto
        ).today_excluded_site_ids(current)
        due: list[int] = []
        sites = session.scalars(select(Site).where(Site.enabled.is_(True))).all()
        for site in sites:
            if site.id in excluded:
                continue
            try:
                tz = ZoneInfo(site.timezone)
                local_now = current.astimezone(tz)
            except Exception:
                logger.warning("site %s has invalid timezone", site.name)
                continue
            target_text = (site.checkin_after or "").strip()
            if target_text:
                try:
                    target = time.fromisoformat(target_text)
                except ValueError:
                    continue
                if local_now.time() < target:
                    continue
            try:
                resolved = workflow.resolve_site(site.id)
            except WorkflowError as exc:
                logger.warning("skip scheduled site %s: %s", site.name, exc)
                continue
            if resolved.steps:
                due.append(site.id)
        return due

    def _browser_ready(self, required_lanes: set[str]) -> bool:
        # Synchronous callers have no readiness proof; never perform blocking I/O.
        return False

    async def scan_once_async(self, now: datetime | None = None) -> list[int]:
        def preflight(lanes: set[str]) -> bool:
            raise _ScheduledBrowserPreflight(lanes)

        try:
            return self.scan_once(now, _readiness=preflight)
        except _ScheduledBrowserPreflight as pending:
            required = pending.lanes
        first_lane = next((key for key in LANE_LABELS if key in required), None)
        try:
            async with asyncio.timeout(135):
                ready = await LaneService(self.settings).scheduler_preflight(set(required))
        except Exception:
            # Readiness failures are expected deferrals, not provider text for logs.
            ready = False
        checked_at = monotonic()
        # No session or proof survives this scan. Re-resolve sites/config after the await;
        # A subset is safe only if its first lane is the one actually initialized.
        return self.scan_once(now, _readiness=lambda lanes: (
            ready is True and lanes <= required
            and next((key for key in LANE_LABELS if key in lanes), None) == first_lane
            and monotonic() - checked_at <= 10
        ))

    def _trigger(
        self, session, current: datetime, *,
        _readiness: Callable[[set[str]], bool] | None = None,
    ) -> list[int]:
        if self._active_scheduled_run(session):
            return []
        due = self._eligible_sites(session, current)
        if not due:
            return []
        workflow = WorkflowService(session, self.settings, self.crypto)
        site_lanes = {site_id: workflow.resolve_site(site_id).lane or "general" for site_id in due}
        required_lanes = set(site_lanes.values())
        if not (_readiness or self._browser_ready)(required_lanes):
            if not self._browser_admission_deferred:
                logger.warning(
                    "scheduled browser admission deferred reason=browser_or_required_lane_unavailable "
                    "required_lanes=%s",
                    ",".join(key for key in LANE_LABELS if key in required_lanes),
                )
            self._browser_admission_deferred = True
            raise _ScheduledBrowserDeferred
        if self._browser_admission_deferred:
            logger.info(
                "scheduled browser admission readiness check recovered required_lanes=%s",
                ",".join(key for key in LANE_LABELS if key in required_lanes),
            )
            self._browser_admission_deferred = False
        lane_order = {key: index for index, key in enumerate(LANE_LABELS)}
        due.sort(key=lambda site_id: lane_order[site_lanes[site_id]])
        run = ExecutionService(session, self.settings, self.crypto).create_run(
            RunCreate(site_ids=due),
            "scheduler",
            mode="scheduled",
            trigger="schedule",
        )
        self.executor.submit(run.id)
        return [run.id]

    def scan_once(
        self, now: datetime | None = None, *,
        _readiness: Callable[[set[str]], bool] | None = None,
    ) -> list[int]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        with self.session_factory() as session:
            settings_service = SchedulerSettingsService(session, self.settings)
            settings = settings_service.view()
            if not settings.enabled:
                if settings.next_run_at is not None:
                    settings_service.set_next_run(None)
                return []
            if settings.mode == "random":
                local = current.astimezone(self.timezone)
                next_run = next_random_run(
                    settings.ranges,
                    (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0),
                    tz=self.timezone,
                )
                due = False
                candidates: list[tuple[datetime, datetime, datetime]] = []
                for index, text in enumerate(settings.ranges):
                    try:
                        target, end_dt = self._range_target(
                            local.date(), index, text, local.tzinfo
                        )
                    except (ValueError, TypeError):
                        continue
                    if local > end_dt:
                        continue
                    start_dt = datetime.combine(
                        local.date(),
                        time.fromisoformat(text.split("-", 1)[0]),
                        tzinfo=local.tzinfo,
                    )
                    if local < target:
                        candidates.append((target, start_dt, end_dt))
                        continue
                    existing = session.scalar(
                        select(Run.id)
                        .where(
                            Run.trigger == "schedule",
                            Run.created_at >= start_dt.astimezone(timezone.utc),
                            Run.created_at <= end_dt.astimezone(timezone.utc),
                        )
                        .limit(1)
                    )
                    if existing is not None:
                        continue
                    due = True
                try:
                    run_ids = self._trigger(session, current, _readiness=_readiness) if due else []
                except _ScheduledBrowserDeferred:
                    run_ids = []
                # A run just created can also consume an overlapping future window.
                for target, start_dt, end_dt in candidates:
                    existing = session.scalar(
                        select(Run.id).where(
                            Run.trigger == "schedule",
                            Run.created_at >= start_dt.astimezone(timezone.utc),
                            Run.created_at <= end_dt.astimezone(timezone.utc),
                        ).limit(1)
                    )
                    if existing is None and (next_run is None or target < next_run):
                        next_run = target
                if due and not run_ids:
                    next_run = current
                # Missed or already consumed windows must not leave a stale timestamp.
                if settings.next_run_at != next_run:
                    settings_service.set_next_run(next_run)
                return run_ids

            next_run = settings.next_run_at
            if next_run is None:
                next_run = self._next_run(settings.mode, settings.cron, settings.interval_seconds, current)
                settings_service.set_next_run(next_run)
                return []
            if current < next_run:
                return []
            next_run = self._next_run(settings.mode, settings.cron, settings.interval_seconds, current)
            try:
                run_ids = self._trigger(session, current, _readiness=_readiness)
            except _ScheduledBrowserDeferred:
                return []
            settings_service.set_next_run(next_run)
            return run_ids

    def _next_run(
        self, mode: str, cron: str, interval_seconds: int, current: datetime
    ) -> datetime:
        if mode == "interval":
            return current + timedelta(seconds=max(60, interval_seconds))
        local = current.astimezone(self.timezone)
        next_value = croniter(cron, local).get_next(datetime)
        if next_value.tzinfo is None:
            next_value = next_value.replace(tzinfo=local.tzinfo or timezone.utc)
        return next_value
