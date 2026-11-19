from __future__ import annotations

import asyncio
import json
import logging
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.crypto import CryptoService
from app.models.execution import JobArtifact, JobStep, Run, SiteJob
from app.models.workflow import Site, SiteResignRecord
from app.services.assistant_run_guard import validate_guard
from app.services.browser_dispatch import (
    DISPATCH_ERROR, DispatchClient, DispatchError, DispatchTicket, RunDispatch,
)
from app.services.credentials import CredentialError, CredentialService
from app.services.execution import ExecutionService, FINAL_JOB_STATUSES, FINAL_RUN_STATUSES
from app.services.integrations import IntegrationService
from app.services.notifications import NotificationService
from app.services.plugin_runtime import PluginRuntimeError, PluginRuntimeService
from app.services.resign import CHECKPOINT_RESIGN_FEATURES, ResignService
from app.services.secret_providers import SECRET_PURPOSES, SecretProviders, step_provider
from app.services.workflows import WorkflowError, WorkflowService

logger = logging.getLogger("siteflow.executor")
DISPATCH_JOIN_TIMEOUT_SECONDS = 10.0
_dispatch_context: ContextVar[tuple[RunDispatch, DispatchTicket] | None] = ContextVar(
    "dispatch_reservation", default=None,
)


class ExecutionManager:
    def __init__(
        self,
        session_factory: sessionmaker,
        settings: Settings,
        crypto: CryptoService,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.crypto = crypto
        self.secret_providers = SecretProviders(session_factory, settings, crypto)
        self._run_semaphore = asyncio.Semaphore(max(1, settings.execution_max_runs))
        self._job_semaphore = asyncio.Semaphore(max(1, settings.execution_max_jobs))
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._active_jobs: dict[int, set[int]] = {}
        self._run_cancel_events: dict[int, asyncio.Event] = {}
        self._dispatch_cleanup_tasks: set[asyncio.Task[None]] = set()
        self._notification_tasks: set[asyncio.Task[None]] = set()

    def submit(self, run_id: int) -> None:
        existing = self._tasks.get(run_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._execute_run(run_id), name=f"run-{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda _task: self._tasks.pop(run_id, None))

    async def cancel(self, run_id: int) -> None:
        event = self._run_cancel_events.get(run_id)
        if event is not None:
            event.set()
        job_ids = sorted(self._active_jobs.get(run_id, set()))
        for job_id in job_ids:
            self.secret_providers.revoke(job_id)
        if not job_ids:
            return
        async with httpx.AsyncClient(timeout=5) as client:
            for job_id in job_ids:
                try:
                    await client.post(
                        f"{self.settings.worker_url.rstrip('/')}/executions/{job_id}/cancel"
                    )
                except Exception:
                    pass

    async def shutdown(self) -> None:
        self.secret_providers.clear()
        tasks = list(self._tasks.values())
        notification_tasks = list(self._notification_tasks)
        for task in tasks + notification_tasks:
            task.cancel()
        if tasks or notification_tasks:
            await asyncio.gather(*(tasks + notification_tasks), return_exceptions=True)

    def schedule_run_notification(self, run_id: int) -> None:
        dispatcher = getattr(self, "notification_dispatch", None)
        if dispatcher is not None:
            dispatcher.ensure(run_id)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(
            self._notify_run(run_id), name=f"run-{run_id}-notification"
        )
        self._notification_tasks.add(task)
        task.add_done_callback(self._notification_tasks.discard)

    async def _notify_run(self, run_id: int) -> None:
        try:
            results = await asyncio.to_thread(self._notify_run_sync, run_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("run %s notification dispatch failed", run_id)
            return
        for result in results:
            if result.ok:
                logger.info("run %s %s notification: %s", run_id, result.channel, result.message)
            else:
                logger.warning(
                    "run %s %s notification failed: %s",
                    run_id,
                    result.channel,
                    result.message,
                )

    def _notify_run_sync(self, run_id: int):
        with self.session_factory() as session:
            return NotificationService(session, self.settings, self.crypto).send_run(run_id)

    async def _execute_run(self, run_id: int) -> None:
        async with self._run_semaphore:
            prepared = await asyncio.to_thread(self._prepare_run, run_id)
            if prepared is None:
                return
            _, canceled = prepared
            if canceled:
                self._wake_run_notification(run_id)
                return

            await self._dispatch_run(run_id)

            finalized = await asyncio.to_thread(self._finalize_run, run_id, _notify=False)
            if finalized is not None:
                self._wake_run_notification(run_id)
            if finalized:
                takeover = getattr(self, "takeover", None)
                if takeover is not None:
                    takeover.wake()

    def _dispatch_client(self) -> DispatchClient:
        return DispatchClient(self.settings)

    def _dispatch_jobs(self, run_id: int) -> tuple[bool, dict[str, list[int]]]:
        with self.session_factory() as session:
            run = session.get(Run, run_id)
            if run is None or run.cancel_requested or run.status in FINAL_RUN_STATUSES:
                return True, {}
            lanes: dict[str, list[int]] = {}
            for job in session.scalars(select(SiteJob).where(
                SiteJob.run_id == run_id, SiteJob.status == "queued",
            ).order_by(SiteJob.id)):
                lanes.setdefault(job.lane, []).append(job.id)
            return False, lanes

    def _fail_dispatch_jobs(self, run_id: int, message: str, lane: str | None = None,
                            exclude: set[int] | None = None) -> None:
        with self.session_factory() as session:
            jobs = select(SiteJob).where(SiteJob.run_id == run_id, SiteJob.status == "queued")
            if lane is not None:
                jobs = jobs.where(SiteJob.lane == lane)
            for job in session.scalars(jobs):
                if job.id not in (exclude or set()):
                    self._finish_job(job, "error", message, {})
            session.commit()

    async def _dispatch_run(self, run_id: int) -> None:
        from app.services.lanes import LANE_LABELS

        dispatch = RunDispatch(self._dispatch_client(), run_id)
        canceled = asyncio.Event()
        self._run_cancel_events[run_id] = canceled
        # Assigned IDs remain excluded until the entire batch joins, even if a
        # job has already queued a cross-lane fallback in the database.
        batches: dict[str, dict[int, asyncio.Task[None]]] = {}
        failed_lanes: set[str] = set()
        renewer: asyncio.Task | None = None
        cancel_waiter = asyncio.create_task(canceled.wait())
        changed_waiter: asyncio.Task | None = None
        try:
            while True:
                dispatch.changed.clear()
                for lane, tasks in list(batches.items()):
                    if all(task.done() for task in tasks.values()):
                        await asyncio.gather(*tasks.values())
                        del batches[lane]
                        try:
                            await dispatch.release(lane)
                        except DispatchError:
                            dispatch.abort()

                db_canceled, queued = await asyncio.to_thread(self._dispatch_jobs, run_id)
                if db_canceled or canceled.is_set():
                    dispatch.stopping = True
                    for lane in list(dispatch.tickets):
                        if lane not in batches:
                            try:
                                await dispatch.release(lane)
                            except DispatchError:
                                dispatch.abort()
                    if not batches:
                        break
                    queued = {}
                assigned = {job_id for tasks in batches.values() for job_id in tasks}
                if dispatch.error or dispatch.stopping:
                    if not db_canceled and not canceled.is_set():
                        await asyncio.to_thread(self._fail_dispatch_jobs, run_id, dispatch.error,
                                                exclude=assigned)
                    if not batches:
                        break
                else:
                    for lane in queued.keys() - LANE_LABELS.keys():
                        await asyncio.to_thread(self._fail_dispatch_jobs, run_id, DISPATCH_ERROR, lane)
                    for lane, ticket in list(dispatch.tickets.items()):
                        if ticket.state == "failed":
                            failed_lanes.add(lane)
                            await asyncio.to_thread(self._fail_dispatch_jobs, run_id, DISPATCH_ERROR,
                                                    lane, assigned)
                            if lane not in batches:
                                try:
                                    await dispatch.release(lane)
                                except DispatchError:
                                    dispatch.abort()
                    for lane in failed_lanes:
                        await asyncio.to_thread(self._fail_dispatch_jobs, run_id, DISPATCH_ERROR,
                                                lane, assigned)
                    # Include every initially required lane in the first renewal.
                    # Later rescans create a fresh identity only after confirmed release.
                    if not dispatch.error and (renewer is None or dispatch.healthy.is_set()):
                        for lane in LANE_LABELS:
                            if (lane not in failed_lanes and lane not in dispatch.tickets
                                    and any(job_id not in assigned for job_id in queued.get(lane, []))):
                                dispatch.add(lane)
                    if renewer is None:
                        renewer = asyncio.create_task(dispatch.maintain(), name=f"run-{run_id}-dispatch")
                    for lane in LANE_LABELS:
                        ticket = dispatch.tickets.get(lane)
                        if (dispatch.error or not dispatch.healthy.is_set() or ticket is None
                                or ticket.state != "granted" or lane in batches):
                            continue
                        job_ids = [job_id for job_id in queued.get(lane, []) if job_id not in assigned]
                        token = _dispatch_context.set((dispatch, ticket))
                        try:
                            batches[lane] = {job_id: asyncio.create_task(
                                self._execute_job_guarded(run_id, job_id),
                                name=f"run-{run_id}-job-{job_id}",
                            ) for job_id in job_ids}
                        finally:
                            _dispatch_context.reset(token)
                if not queued and not batches and not dispatch.tickets:
                    break
                changed_waiter = asyncio.create_task(dispatch.changed.wait())
                await asyncio.wait(
                    [changed_waiter, *([] if cancel_waiter.done() else [cancel_waiter]),
                     *(task for tasks in batches.values() for task in tasks.values() if not task.done())],
                    timeout=1.0, return_when=asyncio.FIRST_COMPLETED,
                )
                changed_waiter.cancel()
                await asyncio.gather(changed_waiter, return_exceptions=True)
                changed_waiter = None
        finally:
            dispatch.abort()
            tasks = [task for batch in batches.values() for task in batch.values()]
            helpers = [task for task in (renewer, cancel_waiter, changed_waiter) if task is not None]

            async def teardown() -> None:
                for task in tasks + helpers:
                    task.cancel()

                async def join() -> None:
                    await asyncio.gather(*(tasks + helpers), return_exceptions=True)

                joiner = asyncio.create_task(join(), name=f"run-{run_id}-dispatch-join")
                self._dispatch_cleanup_tasks.add(joiner)
                joiner.add_done_callback(self._dispatch_cleanup_tasks.discard)
                try:
                    # Do not cancel resistant business tasks again when the join
                    # budget expires. Retain their joiner to collect late outcomes.
                    await asyncio.wait({joiner}, timeout=DISPATCH_JOIN_TIMEOUT_SECONDS)
                finally:
                    self._run_cancel_events.pop(run_id, None)
                    # Known even on an unknown PUT grant; DELETE also tombstones
                    # an owner whose creation has not reached the worker yet.
                    try:
                        async with asyncio.timeout(10):
                            await dispatch.client.close(dispatch.owner)
                    except Exception:
                        pass  # Unconfirmed releases expire under the worker's ticket TTL.

            cleanup = asyncio.create_task(teardown(), name=f"run-{run_id}-dispatch-cleanup")
            self._dispatch_cleanup_tasks.add(cleanup)
            cleanup.add_done_callback(self._dispatch_cleanup_tasks.discard)
            await asyncio.shield(cleanup)

    def _prepare_run(self, run_id: int) -> tuple[list[int], bool] | None:
        with self.session_factory() as session:
            run = session.get(Run, run_id)
            if run is None or run.status in FINAL_RUN_STATUSES:
                return None
            if run.cancel_requested:
                self._cancel_pending(session, run, _notify=False)
                return [], True
            run.status = "running"
            run.started_at = datetime.now(timezone.utc)
            session.commit()
            return list(session.scalars(select(SiteJob.id).where(
                SiteJob.run_id == run_id).order_by(SiteJob.id))), False

    def _wake_run_notification(self, run_id: int) -> None:
        # The worker has already registered the intent in its terminal transaction.
        dispatcher = getattr(self, "notification_dispatch", None)
        if dispatcher is not None:
            dispatcher.wake()
        else:
            self.schedule_run_notification(run_id)

    async def _execute_job_guarded(self, run_id: int, job_id: int) -> None:
        async with self._job_semaphore:
            with self.session_factory() as session:
                job = session.get(SiteJob, job_id)
                run = session.get(Run, run_id)
                if job is None or job.status != "queued":
                    return
                if run is None or run.cancel_requested:
                    now = datetime.now(timezone.utc)
                    job.status = "canceled"
                    job.message = "运行已取消"
                    job.finished_at = now
                    session.commit()
                    return
                lane = job.lane
            try:
                for _ in range(3):
                    if await self._execute_job(run_id, job_id) != "retry":
                        break
                    with self.session_factory() as session:
                        job = session.get(SiteJob, job_id)
                        if job is None or job.status != "queued" or job.lane != lane:
                            # Cross-lane retries need a new batch reservation.
                            return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                with self.session_factory() as session:
                    job = session.get(SiteJob, job_id)
                    if job is not None:
                        job.status = "error"
                        job.message = f"执行器异常: {type(exc).__name__}: {str(exc)[:500]}"
                        job.finished_at = datetime.now(timezone.utc)
                        session.commit()

    @staticmethod
    def _canonical_status(value: str) -> str:
        allowed = {
            "success",
            "fail",
            "error",
            "deferred",
            "unknown",
            "skipped",
            "waiting_human",
            "canceled",
            "interrupted",
        }
        return value if value in allowed else "error"

    @staticmethod
    def _step_status(value: str) -> str:
        allowed = {
            "running",
            "success",
            "fail",
            "error",
            "deferred",
            "unknown",
            "skipped",
            "waiting_human",
            "canceled",
            "interrupted",
        }
        return value if value in allowed else "error"

    @staticmethod
    def _merge_resign_checkpoints(session, job: SiteJob, step_rows: list[Any]) -> list[Any]:
        # Only checkpointed features preserve evidence across incomplete snapshots.
        rows = list(step_rows)
        for saved in session.scalars(
            select(JobStep).where(
                JobStep.site_job_id == job.id,
                JobStep.feature_type.in_(CHECKPOINT_RESIGN_FEATURES),
            ).order_by(JobStep.id)
        ):
            data = json.loads(saved.data_json)
            previous = data.get("resign") if isinstance(data, dict) else None
            if not isinstance(previous, dict) or not previous:
                continue
            index = next((
                i for i, row in enumerate(rows)
                if isinstance(row, dict) and row.get("step_key") == saved.step_key
                and row.get("feature_type") == saved.feature_type
            ), None)
            if index is None:
                rows.append({
                    "step_key": saved.step_key,
                    "feature_type": saved.feature_type,
                    "label": saved.label,
                    "status": saved.status,
                    "message": saved.message,
                    "data": data,
                    "started_at": saved.started_at.isoformat() if saved.started_at else None,
                    "finished_at": saved.finished_at.isoformat() if saved.finished_at else None,
                })
                continue
            current_data = rows[index].get("data")
            current_data = current_data if isinstance(current_data, dict) else {}
            current = current_data.get("resign")
            current = current if isinstance(current, dict) else {}
            merged = {**previous, **current}
            merged["submit_attempted"] = bool(
                previous.get("submit_attempted") or current.get("submit_attempted")
            )
            for key in ("success", "already_signed", "unconfirmed", "inferred_signed"):
                entries = []
                for item in (previous.get(key) or []) + (current.get(key) or []):
                    if item not in entries:
                        entries.append(item)
                if entries:
                    merged[key] = entries
            rows[index] = {**rows[index], "data": {**data, **current_data, "resign": merged}}
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or row.get("feature_type") not in CHECKPOINT_RESIGN_FEATURES:
                continue
            data = row.get("data")
            merged = data.get("resign") if isinstance(data, dict) else None
            if not isinstance(merged, dict):
                continue
            confirmed = {
                item.get("date") if isinstance(item, dict) else item
                for key in ("success", "already_signed") for item in merged.get(key) or []
            }
            merged = dict(merged)
            for key in ("unconfirmed", "remaining"):
                if isinstance(merged.get(key), list):
                    merged[key] = [
                        item for item in merged[key]
                        if (item.get("date") if isinstance(item, dict) else item) not in confirmed
                    ]
            rows[index] = {**row, "data": {**data, "resign": merged}}
        return rows

    def _save_steps(
        self,
        session,
        job: SiteJob,
        step_rows: list[Any],
        *,
        allow_running: bool = False,
    ) -> None:
        session.query(JobStep).filter(JobStep.site_job_id == job.id).delete(
            synchronize_session=False
        )
        for index, item in enumerate(step_rows):
            if not isinstance(item, dict):
                continue
            step_key = str(item.get("step_key") or f"step-{index + 1}")
            raw_status = str(item.get("status") or "error")
            status = (
                self._step_status(raw_status)
                if allow_running
                else self._canonical_status(raw_status)
            )
            if (
                not allow_running and raw_status == "running"
                and item.get("feature_type") in CHECKPOINT_RESIGN_FEATURES
            ):
                status = "waiting_human"
            session.add(
                JobStep(
                    site_job_id=job.id,
                    step_key=step_key,
                    feature_type=str(item.get("feature_type") or ""),
                    label=str(item.get("label") or ""),
                    status=status,
                    message=str(item.get("message") or "")[:4000],
                    data_json=json.dumps(item.get("data") or {}, ensure_ascii=False),
                    started_at=self._parse_time(item.get("started_at")),
                    finished_at=self._parse_time(item.get("finished_at")),
                )
            )

    @staticmethod
    def _save_artifacts(session, job: SiteJob, items: Any) -> None:
        existing = {
            row.relative_path: row
            for row in session.scalars(
                select(JobArtifact).where(JobArtifact.site_job_id == job.id)
            )
        }
        seen: set[str] = set()
        rows = items if isinstance(items, list) else []
        for item in rows:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "")
            relative_path = str(item.get("relative_path") or "").strip().lstrip("/")
            content_type = str(item.get("content_type") or "").strip()
            if kind not in {"log", "screenshot", "captcha"} or not relative_path:
                continue
            if Path(relative_path).is_absolute() or ".." in Path(relative_path).parts:
                continue
            if not content_type or relative_path in seen:
                continue
            seen.add(relative_path)
            try:
                size_bytes = max(0, int(item.get("size_bytes") or 0))
            except (TypeError, ValueError):
                size_bytes = 0
            row = existing.get(relative_path)
            if row is None:
                row = JobArtifact(
                    run_id=job.run_id,
                    site_job_id=job.id,
                    relative_path=relative_path[:512],
                    created_at=datetime.now(timezone.utc),
                )
                session.add(row)
            row.step_key = str(item.get("step_key"))[:64] if item.get("step_key") else None
            row.kind = kind
            row.label = str(item.get("label") or "")[:200]
            row.content_type = content_type[:100]
            row.size_bytes = size_bytes
            row.sha256 = str(item.get("sha256") or "")[:64]
        for relative_path, row in existing.items():
            if relative_path not in seen:
                session.delete(row)

    def _persist_progress(self, job_id: int, event: dict[str, Any]) -> None:
        with self.session_factory() as session:
            job = session.get(SiteJob, job_id)
            if job is None or job.status in FINAL_JOB_STATUSES:
                return
            if event.get("status") == "running" and job.status == "queued":
                job.status = "running"
                job.started_at = datetime.now(timezone.utc)
            steps = event.get("steps")
            step_rows = steps if isinstance(steps, list) else []
            step_rows = self._merge_resign_checkpoints(session, job, step_rows)
            self._save_steps(session, job, step_rows, allow_running=True)
            resign_service = ResignService(session, self.settings)
            for row in step_rows:
                if not isinstance(row, dict) or row.get("feature_type") not in CHECKPOINT_RESIGN_FEATURES:
                    continue
                data = row.get("data")
                resign_result = data.get("resign") if isinstance(data, dict) else None
                if isinstance(resign_result, dict):
                    resign_service.record_results(
                        job.site_id, job.run_id, job.id, resign_result,
                        feature_type=row["feature_type"],
                    )
            if "artifacts" in event:
                self._save_artifacts(session, job, event.get("artifacts"))
            message = str(event.get("message") or "").strip()
            if message:
                job.message = message[:4000]
            job.heartbeat_at = datetime.now(timezone.utc)
            session.commit()

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    def _cancel_pending(self, session, run: Run, *, _notify: bool = True) -> None:
        now = datetime.now(timezone.utc)
        run.status = "canceled"
        run.finished_at = now
        jobs = session.query(SiteJob).filter(SiteJob.run_id == run.id).all()
        for job in jobs:
            if job.status == "queued":
                job.status = "canceled"
                job.message = "运行已取消"
                job.finished_at = now
        run.summary_json = json.dumps(ExecutionService._counts(jobs))
        dispatcher = getattr(self, "notification_dispatch", None)
        if dispatcher is not None:
            dispatcher.register(session, run.id)
        session.commit()
        if _notify:
            self.schedule_run_notification(run.id)

    async def _execute_job(self, run_id: int, job_id: int) -> str | None:
        context = _dispatch_context.get()
        reservation = None
        if context is not None:
            try:
                reservation = await context[0].reservation(context[1])
            except DispatchError as exc:
                with self.session_factory() as session:
                    job = session.get(SiteJob, job_id)
                    run = session.get(Run, run_id)
                    if (job is not None and job.status == "queued" and run is not None
                            and not run.cancel_requested and not context[0].stopping):
                        self._finish_job(job, "error", str(exc), {})
                        session.commit()
                return
        payload: dict[str, Any] | None = None
        reserve_auth = None
        with self.session_factory() as session:
            run = session.get(Run, run_id)
            job = session.get(SiteJob, job_id)
            if job is None:
                return
            if job.status != "queued":
                return
            if run is None or run.cancel_requested:
                now = datetime.now(timezone.utc)
                job.status = "canceled"
                job.message = "运行已取消"
                job.finished_at = now
                session.commit()
                return
            site = session.get(Site, job.site_id) if job.site_id is not None else None
            if run.trigger == "assistant" and (
                site is None
                or not validate_guard(session, run_id, site, self.settings, self.crypto)
            ):
                from app.services.assistant_run_guard import consumption_plan

                stored_plan = consumption_plan(session, run_id, self.crypto)
                self._finish_job(
                    job, "unknown" if stored_plan is not None else "fail",
                    "助手测试配置已变化，请重新确认后运行",
                    {"reason": "assistant_configuration_changed",
                     **({"outcome_uncertain": True} if stored_plan is not None else {})},
                )
                session.commit()
                return
            if site is None:
                self._finish_job(job, "fail", "站点已不存在", {})
                session.commit()
                return
            try:
                resolved = WorkflowService(session, self.settings, self.crypto).resolve_site(
                    site.id
                )
            except WorkflowError as exc:
                self._finish_job(job, "fail", f"工作流解析失败: {exc}", {})
                session.commit()
                return

            if run.trigger == "assistant":
                from app.services.assistant_categories import (
                    Denied, builtin_category, reserve_run_auth_use, run_auth_binding,
                )

                try:
                    if any(step.enabled and builtin_category(step.feature_type) == "auth"
                           for step in resolved.steps):
                        action_id, binding = run_auth_binding(session, run_id, self.crypto)
                        reserve_auth = partial(
                            reserve_run_auth_use, self.session_factory, self.settings, self.crypto,
                            action_id, binding, job.id, job.attempt)
                except Denied:
                    self._finish_job(job, "fail", "Assistant auth budget binding unavailable",
                                     {"reason": "execution_authorization_changed"})
                    session.commit()
                    return

            cookies = None
            try:
                cookies = CredentialService(session, self.crypto).resolve_cookies(site.id)
            except CredentialError as exc:
                self._finish_job(job, "fail", str(exc), {})
                session.commit()
                return

            integrations = IntegrationService(session, self.settings, self.crypto)
            cfbox = None
            if any(
                step.enabled and step.feature_type == "challenge.turnstile"
                for step in resolved.steps
            ):
                url = integrations.resolve_cfbox_url().value
                key = integrations.resolve_cfbox_api_key().value
                if url:
                    cfbox = {"url": url, "api_key": key or ""}

            plugin_ids: set[str] = set()
            for step in resolved.steps:
                if not step.enabled:
                    continue
                if step.feature_type.startswith("plugin."):
                    parts = step.feature_type.split(".", 2)
                    if len(parts) >= 3:
                        plugin_ids.add(parts[1])
                method = step.config.get("captcha_method")
                if isinstance(method, str) and method.startswith("plugin."):
                    parts = method.split(".", 2)
                    if len(parts) >= 3:
                        plugin_ids.add(parts[1])
            plugin_runtimes: dict[str, dict[str, str]] = {}
            if plugin_ids:
                plugin_service = PluginRuntimeService(session, self.settings)
                try:
                    plugin_runtimes = {
                        plugin_id: plugin_service.runtime_for_plugin(plugin_id)
                        for plugin_id in sorted(plugin_ids)
                    }
                except PluginRuntimeError as exc:
                    self._finish_job(job, "fail", str(exc), {})
                    session.commit()
                    return

            resign_days = site.resign_max_days if site.support_resign else 0
            resign_service = ResignService(session, self.settings)
            resign_pending = (
                resign_service.pending_dates(site.id, resign_days)
                if resign_days
                else []
            )
            from app.features.base import EXECUTION_SETTINGS_FEATURES
            from app.features.catalog import get_feature
            from app.services.feature_settings import FeatureSettingsService

            feature_settings = FeatureSettingsService(session)
            feature_execution_settings = {}
            for key in {step.feature_type for step in resolved.steps if step.enabled}:
                if key not in EXECUTION_SETTINGS_FEATURES:
                    continue
                behavior = feature_settings.execution_settings(get_feature(key))
                if key == "flow.result" and not (
                    behavior.llm_enabled
                    and any(
                        step.enabled
                        and step.feature_type == key
                        and step.config.get("llm_enabled") is True
                        and (
                            behavior.judgment_scope == "all"
                            or step.config.get("output_value") is True
                        )
                        for step in resolved.steps
                    )
                ):
                    continue
                feature_execution_settings[key] = behavior.model_dump(mode="json")
            model_requirements = {}
            for step in resolved.steps:
                if not step.enabled:
                    continue
                key = step.feature_type
                if key == "flow.result":
                    if key in feature_execution_settings:
                        model_requirements[key] = feature_execution_settings[key]["use_vision"]
                elif "vision" in step.requires or step.config.get("captcha_method") == "vision":
                    model_requirements[key] = True
            from app.services.model_registry import ModelRegistryError

            try:
                feature_model_runtimes = {
                    key: integrations.model_runtime("feature_cards", requires_vision=requires_vision)
                    for key, requires_vision in sorted(model_requirements.items())
                }
            except ModelRegistryError:
                self._finish_job(job, "fail", "Model runtime configuration unavailable",
                                 {"reason": "model_registry_unavailable"})
                session.commit()
                return
            payload = {
                "job_id": job.id,
                "run_id": run.id,
                "site_name": job.site_name,
                "base_url": site.base_url,
                "trusted_urls": json.loads(site.trusted_urls_json),
                "attempt": job.attempt,
                "lane_key": job.lane,
                "steps": [step.model_dump(mode="json") for step in resolved.steps],
                "signals": [signal.model_dump(mode="json") for signal in resolved.signals],
                "site_id": site.id,
                "cookies": cookies,
                "cfbox": cfbox,
                "feature_execution_settings": feature_execution_settings,
                "feature_model_runtimes": feature_model_runtimes,
                "vision": None,
                "resign_pending_dates": resign_pending,
                "resign_today": datetime.now(resign_service.timezone).date().isoformat(),
                "resign_unresolved_dates": list(session.scalars(
                    select(SiteResignRecord.resign_date).where(
                        SiteResignRecord.site_id == site.id,
                        SiteResignRecord.status == "unknown",
                    ).order_by(SiteResignRecord.resign_date)
                )),
                "resign_max_days": resign_days,
                "plugin_runtimes": plugin_runtimes,
            }
            if reservation is not None:
                payload["dispatch_reservation"] = reservation
            if run.trigger == "assistant":
                payload["assistant_run"] = True
                from app.services.assistant_run_guard import consumption_plan
                from app.services.assistant_consumption import CONSUMING_FEATURES

                plan = consumption_plan(session, run_id, self.crypto) if any(
                    step.enabled and step.feature_type in CONSUMING_FEATURES for step in resolved.steps
                ) else None
                if plan is not None:
                    payload["assistant_consumption_plan"] = plan
                    payload["resign_pending_dates"] = list(plan["review"]["dates"])
                    payload["resign_today"] = plan["review"]["today"]
                # A separate session avoids cached ORM/policy values from payload construction.
                with self.session_factory() as guard_session:
                    current_site = guard_session.get(Site, site.id)
                    unchanged = current_site is not None and validate_guard(
                        guard_session, run_id, current_site, self.settings, self.crypto
                    )
                if not unchanged:
                    self._finish_job(
                        job, "unknown" if plan is not None else "fail",
                        "助手测试配置已变化，请重新确认后运行",
                        {"reason": "assistant_configuration_changed",
                         **({"outcome_uncertain": True} if plan is not None else {})},
                    )
                    session.commit()
                    return
            from app.services.assistant_takeover import capture_job_config

            capture_job_config(session, self.settings, self.crypto, site, job, payload)
            job.message = "等待浏览器车道空位"
            job.heartbeat_at = datetime.now(timezone.utc)
            job.worker_id = "worker"
            session.commit()

        plugin_authorization = self.secret_providers.register_plugins(job, payload["plugin_runtimes"])
        for runtime in payload["plugin_runtimes"].values():
            runtime["authorization"] = {
                "token": plugin_authorization,
                "job_id": job.id,
            }
        payload["secret_access"] = {
            "token": self.secret_providers.register(job, site, resolved.steps, reserve_auth=reserve_auth),
            "providers": {
                step.step_key: step_provider(site, step)
                for step in resolved.steps if step.feature_type in SECRET_PURPOSES
            },
        }
        self._active_jobs.setdefault(run_id, set()).add(job_id)
        try:
            result = await self._post_execute(payload)
        except Exception as exc:
            with self.session_factory() as session:
                job = session.get(SiteJob, job_id)
                if job is not None:
                    assistant = run.trigger == "assistant"
                    self._finish_job(
                        job,
                        "unknown" if assistant else "error",
                        "助手运行结果无法确认，请人工核验后再运行" if assistant else
                        f"worker 执行失败: {type(exc).__name__}: {str(exc)[:500]}",
                        {"reason": "assistant_outcome_uncertain"} if assistant else {},
                    )
                    session.commit()
            return
        finally:
            self.secret_providers.revoke(job_id)
            active = self._active_jobs.get(run_id)
            if active is not None:
                active.discard(job_id)
                if not active:
                    self._active_jobs.pop(run_id, None)

        with self.session_factory() as session:
            job = session.get(SiteJob, job_id)
            if job is None:
                return
            steps = result.get("steps")
            result = {**result, "steps": self._merge_resign_checkpoints(
                session, job, steps if isinstance(steps, list) else []
            )}
            resign_reserved = session.scalar(select(SiteResignRecord.id).where(
                SiteResignRecord.job_id == job.id,
                SiteResignRecord.run_id == job.run_id,
                SiteResignRecord.status.in_({"unknown", "success", "already_signed"}),
            ).limit(1)) is not None

        if str(result.get("status") or "") in {"restart", "fallback_interaction"}:
            if payload.get("assistant_consumption_plan") is not None:
                result = {**result, "status": "unknown",
                          "message": "\u6d88\u8d39\u8fd0\u884c\u7981\u6b62\u81ea\u52a8\u91cd\u8bd5\u6216\u5207\u6362\u8f66\u9053\uff0c\u8bf7\u4eba\u5de5\u6838\u9a8c\u5e76\u53d1\u8d77\u65b0\u8bf7\u6c42\u91cd\u65b0\u786e\u8ba4"}
            paid = any(
                isinstance(row, dict) and row.get("feature_type") == "post.buy_card"
                and isinstance(row.get("data"), dict)
                and isinstance(row["data"].get("buy_card"), dict)
                and (row["data"]["buy_card"].get("payment_attempted")
                     or row["data"]["buy_card"].get("bought", 0))
                for row in result.get("steps", [])
            )
            if paid:
                result = {**result, "status": "waiting_human",
                          "message": "本任务已发起购卡点击，禁止自动重跑以避免重复购买，请人工核验"}
            elif resign_reserved or any(
                isinstance(row, dict) and row.get("feature_type") in CHECKPOINT_RESIGN_FEATURES
                and isinstance(row.get("data"), dict)
                and isinstance(row["data"].get("resign"), dict)
                and (row["data"]["resign"].get("submit_attempted")
                     or row["data"]["resign"].get("success"))
                for row in result["steps"]
            ):
                result = {**result, "status": "waiting_human",
                          "message": "本任务已发起补签点击，禁止自动重跑以避免重复补签，请人工核验"}

        if str(result.get("status") or "") == "fallback_interaction":
            with self.session_factory() as session:
                job = session.get(SiteJob, job_id)
                if job is None:
                    return None
                fallback = {"general": "interaction", "proxy_general": "proxy_interaction"}.get(
                    job.lane
                )
                if job.attempt < 2 and fallback is not None:
                    job.lane = fallback
                    job.status = "queued"
                    job.message = "Playwright 点击未生效，转交互车道重试"
                    job.attempt += 1
                    job.started_at = None
                    job.finished_at = None
                    job.heartbeat_at = None
                    job.worker_id = None
                    job.result_json = "{}"
                    session.query(JobStep).filter(
                        JobStep.site_job_id == job.id
                    ).delete(synchronize_session=False)
                    session.query(JobArtifact).filter(
                        JobArtifact.site_job_id == job.id
                    ).delete(synchronize_session=False)
                    session.commit()
                    return "retry"
                self._finish_job(
                    job,
                    "error",
                    str(result.get("message") or "交互车道重试未完成"),
                    {},
                )
                session.commit()
                return None

        if str(result.get("status") or "") == "restart":
            with self.session_factory() as session:
                job = session.get(SiteJob, job_id)
                if job is None:
                    return None
                message = str(result.get("message") or "验证码置信度不足")
                if job.attempt < 3:
                    job.status = "queued"
                    job.message = (
                        f"{message}，重新开始任务（{job.attempt}/2）"
                    )
                    job.attempt += 1
                    job.started_at = None
                    job.finished_at = None
                    job.heartbeat_at = None
                    job.worker_id = None
                    job.result_json = "{}"
                    session.query(JobStep).filter(
                        JobStep.site_job_id == job.id
                    ).delete(synchronize_session=False)
                    session.query(JobArtifact).filter(
                        JobArtifact.site_job_id == job.id
                    ).delete(synchronize_session=False)
                    session.commit()
                    return "retry"
                self._finish_job(
                    job,
                    "error",
                    f"{message}（已重新开始 2 次）",
                    {},
                )
                session.commit()
                return None

        with self.session_factory() as session:
            job = session.get(SiteJob, job_id)
            if job is None:
                return
            run = session.get(Run, run_id)
            status = self._canonical_status(str(result.get("status") or "error"))
            if run is not None and run.cancel_requested:
                status = "unknown" if payload.get("assistant_consumption_plan") is not None else "canceled"
            message = str(result.get("message") or "")
            steps = result.get("steps")
            step_rows = steps if isinstance(steps, list) else []
            self._save_steps(session, job, step_rows)
            resign_service = ResignService(session, self.settings)
            for row in step_rows:
                if not isinstance(row, dict) or row.get("feature_type") not in {
                    "post.resign", *CHECKPOINT_RESIGN_FEATURES,
                }:
                    continue
                data = row.get("data")
                resign_result = data.get("resign") if isinstance(data, dict) else None
                if isinstance(resign_result, dict):
                    resign_service.record_results(
                        job.site_id, job.run_id, job.id, resign_result,
                        feature_type=row["feature_type"],
                    )
            if isinstance(result.get("artifacts"), list):
                self._save_artifacts(session, job, result.get("artifacts"))
            job.status = status
            job.message = message[:4000]
            outputs = result.get("outputs")
            job.result_json = json.dumps(
                {
                    "status": status,
                    "message": message,
                    "step_count": len(step_rows),
                    "outputs": outputs if isinstance(outputs, list) else [],
                },
                ensure_ascii=False,
            )
            job.finished_at = datetime.now(timezone.utc)
            job.heartbeat_at = job.finished_at
            session.commit()
            if status == "success" and job.site_id is not None:
                exported_cookies = result.get("cookies")
                if isinstance(exported_cookies, list):
                    try:
                        CredentialService(session, self.crypto).store_exported_cookies(
                            job.site_id, exported_cookies
                        )
                    except Exception:
                        pass

    async def _post_execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        context = _dispatch_context.get()
        if context is None:
            raise DispatchError()
        if payload.get("dispatch_reservation") != await context[0].reservation(context[1]):
            raise DispatchError()
        job_id = int(payload["job_id"])
        result: dict[str, Any] | None = None
        async with httpx.AsyncClient(timeout=self.settings.execution_timeout) as client:
            async with client.stream(
                "POST",
                f"{self.settings.worker_url.rstrip('/')}/execute/stream",
                json=payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    if event.get("type") == "progress":
                        try:
                            self._persist_progress(job_id, event)
                        except Exception:
                            pass
                    elif event.get("type") == "result":
                        candidate = event.get("result")
                        if isinstance(candidate, dict):
                            result = candidate
        if not isinstance(result, dict):
            raise ValueError("worker 返回了无效响应")
        return result

    @staticmethod
    def _finish_job(job: SiteJob, status: str, message: str, result: dict[str, Any]) -> None:
        job.status = status
        job.message = message[:4000]
        job.result_json = json.dumps(result, ensure_ascii=False)
        job.finished_at = datetime.now(timezone.utc)
        job.heartbeat_at = job.finished_at

    def _finalize_run(self, run_id: int, *, _notify: bool = True) -> bool | None:
        with self.session_factory() as session:
            run = session.get(Run, run_id)
            if run is None:
                return
            if run.status in FINAL_RUN_STATUSES:
                if _notify:
                    self.schedule_run_notification(run_id)
                else:
                    dispatcher = getattr(self, "notification_dispatch", None)
                    if dispatcher is not None:
                        dispatcher.register(session, run_id)
                        session.commit()
                return False
            jobs = session.query(SiteJob).filter(SiteJob.run_id == run.id).all()
            for job in jobs:
                if job.status == "queued":
                    job.status = "canceled"
                    job.message = "运行已结束，未执行"
                    job.finished_at = datetime.now(timezone.utc)
            counts = ExecutionService._counts(jobs)
            if run.cancel_requested:
                run.status = "canceled"
            elif any(job.status == "waiting_human" for job in jobs):
                run.status = "waiting_human"
            elif any(job.status == "deferred" for job in jobs):
                run.status = "deferred"
            elif any(job.status == "unknown" for job in jobs):
                run.status = "unknown"
            elif any(job.status == "canceled" for job in jobs):
                run.status = "canceled"
            elif any(job.status in {"fail", "error", "interrupted"} for job in jobs):
                run.status = "fail"
            elif jobs and all(job.status in {"success", "skipped"} for job in jobs):
                run.status = "success"
            else:
                run.status = "fail"
            run.summary_json = json.dumps(counts)
            run.finished_at = datetime.now(timezone.utc)
            dispatcher = getattr(self, "notification_dispatch", None)
            if dispatcher is not None:
                dispatcher.register(session, run.id)
            session.commit()
            if _notify:
                self.schedule_run_notification(run.id)
                takeover = getattr(self, "takeover", None)
                if takeover is not None:
                    takeover.wake()
            return True
