"""Immediate run notifications and separate at-most-once investigation notices.

The database claim precedes network I/O. A crash after that claim is ambiguous,
so startup marks it unknown rather than retrying either SMTP or HTTP.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core.crypto import CryptoError
from app.models.assistant_takeover import AssistantJobConfigSnapshot, AssistantTakeoverAction
from app.models.assistant_takeover_notice import AssistantTakeoverNotice
from app.models.execution import Run, SiteJob
from app.models.run_notification import RunNotificationIntent
from app.models.workflow import Site
from app.services.execution import FINAL_RUN_STATUSES
from app.services.notifications import DeliveryTarget, NotificationService

logger = logging.getLogger("siteflow.notifications")
TERMINAL_RUN_STATUSES = FINAL_RUN_STATUSES | {"deferred"}
TERMINAL_ACTION_STATUSES = frozenset({
    "updated", "completed_analysis", "failed", "unknown", "revoked", "canceled", "stale", "reused",
})
COMPLETE_ACTION_STATUSES = frozenset({"completed_analysis", "updated"})


def _now():
    return datetime.now(timezone.utc)


def _decrypt(crypto, key, payload):
    return json.loads(crypto.decrypt(key, payload.ciphertext, payload.nonce, payload.key_version))


def _encrypt(crypto, key, payload):
    return crypto.encrypt(key, json.dumps(payload))


def _targets_for_run(service, event):
    return [
        asdict(target) for target in service._targets()
        if target.enabled and service._accepts_run(target.channel, event)
    ]


def plan_run_takeovers(db, run_id):
    """Plan the entire run before testing notification readiness, without a commit."""
    from app.services.assistant_takeover import _eligible
    from app.services.assistant_takeover_dedup import enqueue_takeover
    from app.services.assistant_takeover_performance import TakeoverPhaseCache
    from app.services.assistant_takeover_settings import TakeoverSettingsService

    policy = TakeoverSettingsService(db).view()
    run = db.get(Run, run_id)
    cache = TakeoverPhaseCache()
    planned = set(db.scalars(select(AssistantTakeoverAction.source_job_id).where(
        AssistantTakeoverAction.source_run_id == run_id)))
    jobs = list(db.scalars(select(SiteJob).where(SiteJob.run_id == run_id).order_by(SiteJob.id)))
    sites = {site.id: site for site in db.scalars(select(Site).where(
        Site.id.in_({job.site_id for job in jobs if job.site_id is not None})))}
    for job in jobs:
        if job.id in planned or not _eligible(db, job, run, policy, cache):
            continue
        binding = db.get(AssistantJobConfigSnapshot, job.id)
        site = sites.get(job.site_id)
        enqueue_takeover(db, job, run, policy, binding, site)


def register_run_notification(db, settings, crypto, run_id):
    """Register in the caller's terminal transaction; never commits or wakes tasks."""
    existing = db.get(RunNotificationIntent, run_id)
    if existing is not None:
        return existing
    run = db.get(Run, run_id)
    if run is None or run.status not in TERMINAL_RUN_STATUSES or run.finished_at is None:
        return None
    db.flush()
    now = _now()
    service = NotificationService(db, settings, crypto)
    try:
        with db.begin_nested():
            plan_run_takeovers(db, run_id)
            db.flush()
    except Exception:
        # A planning failure must not roll back the original run result.
        logger.error("run %s notification takeover planning failed; using original result", run_id)

    from app.services.assistant_takeover import (
        _eligible, _source_current, _waiting_source,
    )
    from app.services.assistant_takeover_dedup import reuse_completed
    from app.services.assistant_takeover_performance import TakeoverPhaseCache
    from app.services.assistant_takeover_settings import TakeoverSettingsService
    from app.services.assistant_urls import origin

    policy = TakeoverSettingsService(db).view()
    cache = TakeoverPhaseCache()
    actions = list(db.scalars(select(AssistantTakeoverAction).where(
        AssistantTakeoverAction.source_run_id == run_id,
        AssistantTakeoverAction.status.in_(("queued", "dispatched")),
    ).order_by(AssistantTakeoverAction.id)))
    pending_job_ids = []
    for action in actions:
        reused = False
        if action.status == "queued":
            job = db.get(SiteJob, action.source_job_id)
            site = db.get(Site, action.site_id)
            try:
                identity_current = (
                    site is not None and site.history_key == action.site_history_key
                    and origin(site.base_url) == action.site_origin
                )
            except (ValueError, TypeError):
                identity_current = False
            if (policy.revision != action.grant_revision
                    or policy.authorizing_owner != action.owner or not identity_current
                    or job is None or job.run_id != run_id or job.site_id != action.site_id
                    or not _eligible(db, job, run, policy, cache)):
                action.status, action.reason = "revoked", "policy_or_source_changed"
                action.updated_at = now
                continue
            busy = db.scalar(select(SiteJob.id).where(
                SiteJob.site_id == action.site_id, SiteJob.status.in_(("queued", "running")),
            ).limit(1))
            try:
                with db.begin_nested():
                    if (not busy and not _waiting_source(db, action, cache)
                            and _source_current(
                                db, SimpleNamespace(settings=settings, crypto=crypto), action,
                            )):
                        reused = reuse_completed(db, SimpleNamespace(crypto=crypto), action)
                    db.flush()
            except Exception:
                logger.warning("run %s notification dedup reuse failed", run_id)
                reused = False
        if not reused:
            pending_job_ids.append(action.source_job_id)

    event = service._run_event(run_id, include_conclusions=False)
    service.apply_conclusions(event)
    service.annotate_assistant_pending(event, pending_job_ids)
    payload = {
        "event": event,
        "message": service._render(event),
        "targets": _targets_for_run(service, event),
    }
    frozen = _encrypt(crypto, f"run-notification:{run_id}", payload)
    try:
        with db.begin_nested():
            row = RunNotificationIntent(
                run_id=run_id, state="ready", created_at=now, updated_at=now,
                deadline_at=now, ciphertext=frozen.ciphertext, nonce=frozen.nonce,
                key_version=frozen.key_version,
            )
            db.add(row)
            db.flush()
            return row
    except IntegrityError:
        return db.get(RunNotificationIntent, run_id)


def register_investigation_notification(db, settings, crypto, action_id):
    """Register a separate notice only after a valid terminal investigation."""
    existing = db.get(AssistantTakeoverNotice, action_id)
    if existing is not None:
        return existing
    action = db.get(AssistantTakeoverAction, action_id)
    if action is None or action.status not in COMPLETE_ACTION_STATUSES:
        return None
    run = db.get(Run, action.source_run_id)
    job = db.get(SiteJob, action.source_job_id)
    site = db.get(Site, action.site_id)
    if (run is None or job is None or job.run_id != action.source_run_id
            or job.site_id != action.site_id
            or job.status not in {"fail", "error", "waiting_human"}
            or run.status not in {"fail", "error", "waiting_human"}):
        return None
    if site is not None and site.history_key != action.site_history_key:
        site = None
    from app.services.assistant_takeover_conclusions import public_conclusion

    try:
        conclusion = public_conclusion(
            SimpleNamespace(crypto=crypto, settings=settings), db, action,
        )
    except (CryptoError, ValueError, TypeError):
        return None
    if not conclusion:
        return None
    service = NotificationService(db, settings, crypto)
    event, message = service.investigation_notice(
        action=action, run=run, job=job, site=site, conclusion=conclusion,
    )
    targets = [asdict(target) for target in service.investigation_targets()]
    payload = {"event": event, "message": message, "targets": targets}
    frozen = _encrypt(crypto, f"assistant-takeover-notice:{action_id}", payload)
    now = _now()
    try:
        with db.begin_nested():
            row = AssistantTakeoverNotice(
                action_id=action_id, state="pending", created_at=now, updated_at=now,
                ciphertext=frozen.ciphertext, nonce=frozen.nonce,
                key_version=frozen.key_version,
            )
            db.add(row)
            db.flush()
            return row
    except IntegrityError:
        return db.get(AssistantTakeoverNotice, action_id)


class RunNotificationDispatcher:
    def __init__(self, executor):
        self.executor = executor
        self.session_factory = executor.session_factory
        self.settings = executor.settings
        self.crypto = executor.crypto
        self._event = asyncio.Event()
        self._task = None
        self._stopping = False
        self._loop = None

    def register(self, db, run_id):
        return register_run_notification(db, self.settings, self.crypto, run_id)

    def ensure(self, run_id):
        with self.session_factory() as db:
            self.register(db, run_id)
            db.commit()
        self.wake()

    def ensure_investigation(self, action_id):
        with self.session_factory() as db:
            register_investigation_notification(
                db, self.settings, self.crypto, action_id,
            )
            db.commit()
        self.wake()

    def wake(self):
        if not self._stopping:
            if self._loop is not None and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._event.set)
            else:
                self._event.set()

    @staticmethod
    def _legacy_payload(db, service, crypto, row):
        key = f"run-notification:{row.run_id}"
        payload = _decrypt(crypto, key, row)
        event = payload.get("event")
        if not isinstance(event, dict):
            raise ValueError("notification event missing")
        if not isinstance(payload.get("message"), dict):
            payload["message"] = service._render(event)
        if not isinstance(payload.get("targets"), list):
            payload["targets"] = _targets_for_run(service, event)
        return payload

    async def start(self):
        if self._task is not None:
            return
        self._stopping = False
        self._loop = asyncio.get_running_loop()
        with self.session_factory() as db:
            db.execute(update(RunNotificationIntent).where(
                RunNotificationIntent.state == "dispatched",
            ).values(state="unknown", updated_at=_now()))
            db.execute(update(AssistantTakeoverNotice).where(
                AssistantTakeoverNotice.state == "dispatched",
            ).values(state="unknown", updated_at=_now()))
            service = NotificationService(db, self.settings, self.crypto)
            for row in list(db.scalars(select(RunNotificationIntent).where(
                    RunNotificationIntent.state == "waiting"))):
                # Old waiting events have no reliable fresh-analysis annotation.
                # Preserve their frozen messages and make them immediately ready.
                try:
                    payload = self._legacy_payload(db, service, self.crypto, row)
                    frozen = _encrypt(
                        self.crypto, f"run-notification:{row.run_id}", payload)
                except Exception:
                    logger.error("legacy run %s notification migration failed", row.run_id)
                    row.state = "unknown"
                else:
                    row.state = "ready"
                    row.ciphertext = frozen.ciphertext
                    row.nonce = frozen.nonce
                    row.key_version = frozen.key_version
                row.updated_at = _now()
            db.commit()
        self._task = asyncio.create_task(self._poll(), name="run-notifications")

    async def stop(self):
        self._stopping = True
        self._event.set()
        if self._task is not None:
            # Do not cancel a to_thread network attempt and leave its DB/engine
            # lifetime behind. Each transport already has a bounded timeout.
            await self._task
            self._task = None

    async def _poll(self):
        while not self._stopping:
            self._event.clear()
            try:
                await asyncio.to_thread(self.dispatch_pending)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("notification reconciliation failed")
            if self._stopping:
                break
            try:
                await asyncio.wait_for(self._event.wait(), timeout=2)
            except TimeoutError:
                pass

    def dispatch_pending(self):
        # Only intents/notices are scanned. Never backfill historical runs.
        with self.session_factory() as db:
            run_ids = list(db.scalars(select(RunNotificationIntent.run_id).where(
                RunNotificationIntent.state.in_(("ready", "waiting")),
            ).order_by(RunNotificationIntent.created_at, RunNotificationIntent.run_id)))
        for run_id in run_ids:
            if self._stopping:
                break
            try:
                self.dispatch_one(run_id)
            except Exception:
                logger.error("run %s notification dispatch failed", run_id)
        with self.session_factory() as db:
            action_ids = list(db.scalars(select(AssistantTakeoverNotice.action_id).where(
                AssistantTakeoverNotice.state == "pending",
            ).order_by(AssistantTakeoverNotice.created_at, AssistantTakeoverNotice.action_id)))
        for action_id in action_ids:
            if self._stopping:
                break
            try:
                self.dispatch_investigation_one(action_id)
            except Exception:
                logger.error("action %s investigation notification dispatch failed", action_id)

    def dispatch_one(self, run_id):
        payload = None
        with self.session_factory() as db:
            row = db.get(RunNotificationIntent, run_id)
            if row is None or row.state not in {"waiting", "ready"}:
                return
            key = f"run-notification:{run_id}"
            if row.state == "waiting":
                service = NotificationService(db, self.settings, self.crypto)
                try:
                    payload = self._legacy_payload(db, service, self.crypto, row)
                    frozen = _encrypt(self.crypto, key, payload)
                except Exception:
                    db.execute(update(RunNotificationIntent).where(
                        RunNotificationIntent.run_id == run_id,
                        RunNotificationIntent.state == "waiting",
                    ).values(state="unknown", updated_at=_now()))
                    db.commit()
                    return
                changed = db.execute(update(RunNotificationIntent).where(
                    RunNotificationIntent.run_id == run_id,
                    RunNotificationIntent.state == "waiting",
                ).values(state="ready", ciphertext=frozen.ciphertext, nonce=frozen.nonce,
                         key_version=frozen.key_version, updated_at=_now()))
                db.commit()
                if changed.rowcount != 1:
                    return
            else:
                try:
                    payload = _decrypt(self.crypto, key, row)
                except Exception:
                    db.execute(update(RunNotificationIntent).where(
                        RunNotificationIntent.run_id == run_id,
                        RunNotificationIntent.state == "ready",
                    ).values(state="unknown", updated_at=_now()))
                    db.commit()
                    return
            changed = db.execute(update(RunNotificationIntent).where(
                RunNotificationIntent.run_id == run_id,
                RunNotificationIntent.state == "ready",
            ).values(state="dispatched", updated_at=_now()))
            db.commit()
            if changed.rowcount != 1:
                return

        # Frozen targets include credentials and therefore live only in encrypted
        # storage. The DB claim is durable before either transport is invoked.
        with self.session_factory() as db:
            service = NotificationService(db, self.settings, self.crypto)
            results = service.send_event(
                payload["event"], payload["message"],
                targets=[DeliveryTarget(**target) for target in payload["targets"]],
            )
            for result in results:
                if result.ok:
                    logger.info("run %s %s notification: %s", run_id, result.channel, result.message)
                else:
                    logger.warning("run %s %s notification failed: %s",
                                   run_id, result.channel, result.message)
            db.execute(update(RunNotificationIntent).where(
                RunNotificationIntent.run_id == run_id,
                RunNotificationIntent.state == "dispatched",
            ).values(state="completed", updated_at=_now(), channel_results_json=json.dumps([
                {"channel": result.channel, "ok": result.ok} for result in results
            ])))
            db.commit()

    def dispatch_investigation_one(self, action_id):
        key = f"assistant-takeover-notice:{action_id}"
        with self.session_factory() as db:
            row = db.get(AssistantTakeoverNotice, action_id)
            if row is None or row.state != "pending":
                return
            try:
                payload = _decrypt(self.crypto, key, row)
            except Exception:
                db.execute(update(AssistantTakeoverNotice).where(
                    AssistantTakeoverNotice.action_id == action_id,
                    AssistantTakeoverNotice.state == "pending",
                ).values(state="unknown", updated_at=_now()))
                db.commit()
                return
            run_id = payload["event"]["run_id"]
            if db.get(RunNotificationIntent, run_id) is None:
                # A newly completed investigation may be the first explicit
                # notification request for its source. Send the source first.
                self.register(db, run_id)
                db.commit()
                self.wake()
                return
            changed = db.execute(update(AssistantTakeoverNotice).where(
                AssistantTakeoverNotice.action_id == action_id,
                AssistantTakeoverNotice.state == "pending",
                select(RunNotificationIntent.run_id).where(
                    RunNotificationIntent.run_id == run_id,
                    RunNotificationIntent.state.in_(("completed", "unknown")),
                ).exists(),
            ).values(state="dispatched", updated_at=_now()))
            db.commit()
            if changed.rowcount != 1:
                return

        with self.session_factory() as db:
            service = NotificationService(db, self.settings, self.crypto)
            results = service.send_event(
                payload["event"], payload["message"],
                targets=[DeliveryTarget(**target) for target in payload["targets"]],
            )
            for result in results:
                if result.ok:
                    logger.info("action %s %s investigation notification completed",
                                action_id, result.channel)
                else:
                    logger.warning("action %s %s investigation notification failed",
                                   action_id, result.channel)
            db.execute(update(AssistantTakeoverNotice).where(
                AssistantTakeoverNotice.action_id == action_id,
                AssistantTakeoverNotice.state == "dispatched",
            ).values(state="completed", updated_at=_now(), channel_results_json=json.dumps([
                {"channel": result.channel, "ok": result.ok} for result in results
            ])))
            db.commit()
