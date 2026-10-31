from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.crypto import CryptoService
from app.models.execution import JobArtifact, JobStep, Run, SiteJob
from app.models.workflow import Site
from app.services.credentials import CredentialError, CredentialService
from app.services.execution import ExecutionService, FINAL_JOB_STATUSES, FINAL_RUN_STATUSES
from app.services.integrations import IntegrationService
from app.services.notifications import NotificationService
from app.services.plugin_runtime import PluginRuntimeError, PluginRuntimeService
from app.services.resign import ResignService
from app.services.secret_providers import SecretProviders, step_provider
from app.services.workflows import WorkflowError, WorkflowService

logger = logging.getLogger("siteflow.executor")


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
        self._notification_tasks: set[asyncio.Task[None]] = set()

    def submit(self, run_id: int) -> None:
        existing = self._tasks.get(run_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._execute_run(run_id), name=f"run-{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda _task: self._tasks.pop(run_id, None))

    async def cancel(self, run_id: int) -> None:
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
            with self.session_factory() as session:
                run = session.get(Run, run_id)
                if run is None:
                    return
                if run.status in FINAL_RUN_STATUSES:
                    return
                if run.cancel_requested:
                    self._cancel_pending(session, run)
                    return
                run.status = "running"
                run.started_at = datetime.now(timezone.utc)
                session.commit()
                job_ids = [
                    job.id
                    for job in session.query(SiteJob)
                    .filter(SiteJob.run_id == run_id)
                    .order_by(SiteJob.id)
                    .all()
                ]

            tasks = [
                asyncio.create_task(
                    self._execute_job_guarded(run_id, job_id),
                    name=f"run-{run_id}-job-{job_id}",
                )
                for job_id in job_ids
            ]
            try:
                await asyncio.gather(*tasks)
            except asyncio.CancelledError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

            self._finalize_run(run_id)

    async def _execute_job_guarded(self, run_id: int, job_id: int) -> None:
        with self.session_factory() as session:
            job = session.get(SiteJob, job_id)
            if job is None or job.status != "queued":
                return
            lane = job.lane
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
            try:
                for _ in range(3):
                    if await self._execute_job(run_id, job_id) != "retry":
                        break
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
            self._save_steps(session, job, step_rows, allow_running=True)
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

    def _cancel_pending(self, session, run: Run) -> None:
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
        session.commit()
        self.schedule_run_notification(run.id)

    async def _execute_job(self, run_id: int, job_id: int) -> str | None:
        payload: dict[str, Any] | None = None
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
            resign_pending = (
                ResignService(session, self.settings).pending_dates(site.id, resign_days)
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
            needs_vision_runtime = "flow.result" in feature_execution_settings or any(
                step.enabled
                and (
                    "vision" in step.requires
                    or step.config.get("captcha_method") == "vision"
                )
                for step in resolved.steps
            )
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
                "vision": integrations.vision_runtime() if needs_vision_runtime else None,
                "resign_pending_dates": resign_pending,
                "resign_max_days": resign_days,
                "plugin_runtimes": plugin_runtimes,
            }
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
            "token": self.secret_providers.register(job, site, resolved.steps),
            "providers": {
                step.step_key: step_provider(site, step)
                for step in resolved.steps if step.feature_type in {"auth.form_login", "challenge.2fa"}
            },
        }
        self._active_jobs.setdefault(run_id, set()).add(job_id)
        try:
            result = await self._post_execute(payload)
        except Exception as exc:
            with self.session_factory() as session:
                job = session.get(SiteJob, job_id)
                if job is not None:
                    self._finish_job(
                        job,
                        "error",
                        f"worker 执行失败: {type(exc).__name__}: {str(exc)[:500]}",
                        {},
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

        if str(result.get("status") or "") == "fallback_interaction":
            with self.session_factory() as session:
                job = session.get(SiteJob, job_id)
                if job is None:
                    return None
                if job.attempt < 2:
                    job.lane = (
                        "proxy_interaction"
                        if job.lane.startswith("proxy")
                        else "interaction"
                    )
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
                status = "canceled"
            message = str(result.get("message") or "")
            steps = result.get("steps")
            step_rows = steps if isinstance(steps, list) else []
            self._save_steps(session, job, step_rows)
            resign_service = ResignService(session, self.settings)
            for row in step_rows:
                if not isinstance(row, dict) or row.get("feature_type") != "post.resign":
                    continue
                data = row.get("data")
                resign_result = data.get("resign") if isinstance(data, dict) else None
                if isinstance(resign_result, dict):
                    resign_service.record_results(
                        job.site_id, job.run_id, job.id, resign_result
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

    def _finalize_run(self, run_id: int) -> None:
        with self.session_factory() as session:
            run = session.get(Run, run_id)
            if run is None:
                return
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
            session.commit()
            self.schedule_run_notification(run.id)
