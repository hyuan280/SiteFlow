"""Conservative versioned failure identities; no runtime or provider access."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta, timezone
from uuid import uuid4

from sqlalchemy import select, update

from app.core.crypto import CryptoError
from app.models.assistant import AssistantSession
from app.models.assistant_takeover import AssistantJobConfigSnapshot, AssistantTakeoverAction
from app.models.assistant_takeover_conclusion import AssistantTakeoverConclusion
from app.models.assistant_takeover_dedup import (
    TakeoverAnalysisProvenance, TakeoverDedupLink, TakeoverFailureSignature,
)
from app.models.execution import JobStep, SiteJob
from app.models.workflow import Site
from app.services.assistant_urls import origin
from app.services.config_ai_generation import _strict_json


VERSION = 2
MATCH_FIELDS = ("owner", "site_history_key", "site_origin", "config_revision",
                "config_fingerprint", "version", "failure_hash")
FACT_FIELDS = ("reason", "code", "errorcode", "provider_error", "stage", "cause",
               "field", "field_path")
GENERIC_REASONS = {"error", "fail", "unknown", "timeout", "execution_error", "execution_timeout",
                   "transport_error", "network_error", "connection_error", "http_error",
                   "worker_error", "worker_unavailable"}


def failure_hash(db, job):
    """Hash only failure facts, never persist/export raw messages or field paths.

    Exact whitespace-normalized messages deliberately distinguish even failures
    sharing a machine reason. Volatile messages may miss reuse, never overmatch.
    """
    def facts(raw, message):
        data = _strict_json(raw)
        if not isinstance(data, dict):
            raise ValueError("invalid_failure_data")
        result = {key: data[key] for key in FACT_FIELDS
                  if isinstance(data.get(key), str) and data[key].strip()}
        text = " ".join(message.split()) if isinstance(message, str) else ""
        if isinstance(data.get("message"), str) and data["message"].strip():
            result["result_message"] = " ".join(data["message"].split())
        if text:
            result["message"] = text
        return result

    def identifying(result):
        reasons = [result[key] for key in ("reason", "code", "errorcode", "provider_error", "cause")
                   if key in result]
        return (bool(result.get("message") or result.get("result_message"))
                or any(value not in GENERIC_REASONS for value in reasons))

    try:
        steps = db.scalars(select(JobStep).where(
            JobStep.site_job_id == job.id,
        ).order_by(JobStep.step_key)).all()
        failures = []
        for step in steps:
            false_result = (step.feature_type in {"flow.result", "action.result"}
                            and step.status not in {"skipped", "queued"}
                            and _strict_json(step.data_json).get("value") is False)
            if step.status not in {"fail", "error"} and not false_result:
                continue
            detail = facts(step.data_json, step.message)
            if (not (false_result or identifying(detail))
                    or not step.step_key or not step.feature_type):
                return None
            if false_result:
                detail["value"] = False
            failures.append({"step": step.step_key, "feature": step.feature_type,
                             "status": step.status, "facts": detail})
        terminal = facts(job.result_json, job.message)
        if not failures and not identifying(terminal):
            return None
        canonical = json.dumps({"version": VERSION, "lane": job.lane,
                                "status": job.status, "terminal": terminal, "failures": failures},
                               sort_keys=True, ensure_ascii=True, allow_nan=False,
                               separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()
    except (ValueError, TypeError, AttributeError, RecursionError):
        return None


def mark_analysis_provenance(db, manager, action, *, source_current, create=True):
    """Record phase observations, never promote a previously stale observation."""
    from app.services.assistant_takeover import _current_fingerprint

    row = db.get(TakeoverAnalysisProvenance, action.id)
    if row is None and not create:
        return
    if row is not None and not row.source_current:
        return
    inspected = action.source_fingerprint if source_current else ""
    if not source_current:
        site = db.get(Site, action.site_id)
        try:
            inspected = _current_fingerprint(db, manager, site) if site else ""
        except (CryptoError, ValueError, TypeError, KeyError):
            pass
    if row is None:
        db.add(TakeoverAnalysisProvenance(
            action_id=action.id, source_fingerprint=action.source_fingerprint,
            inspected_fingerprint=inspected, source_current=source_current,
        ))
    else:
        row.source_current = bool(source_current and row.source_fingerprint == action.source_fingerprint)
        row.inspected_fingerprint = inspected
    db.flush()


def record_signature(db, action):
    existing = db.get(TakeoverFailureSignature, action.source_job_id)
    if existing is not None:
        return existing if existing.action_id == action.id else None
    binding = db.get(AssistantJobConfigSnapshot, action.source_job_id)
    job = db.get(SiteJob, action.source_job_id)
    if (binding is None or job is None or not action.source_fingerprint
            or job.status not in {"fail", "error"} or job.site_id != action.site_id
            or job.run_id != action.source_run_id or binding.site_id != action.site_id
            or binding.history_key != action.site_history_key or binding.attempt != job.attempt
            or binding.fingerprint != action.source_fingerprint):
        return None
    digest = failure_hash(db, job)
    if digest is None:
        return None
    row = TakeoverFailureSignature(
        source_job_id=job.id, action_id=action.id, owner=action.owner,
        site_history_key=action.site_history_key, site_origin=action.site_origin,
        config_revision=binding.revision, config_fingerprint=binding.fingerprint,
        version=VERSION, failure_hash=digest,
    )
    db.add(row)
    db.flush()
    return row


def enqueue_takeover(db, job, run, policy, binding, site):
    """Shared planning only; encrypted proof/current configuration are checked at consumption."""
    action = AssistantTakeoverAction(
        id=str(uuid4()), source_job_id=job.id, source_run_id=run.id,
        site_id=job.site_id, owner=policy.authorizing_owner,
        site_history_key=binding.history_key if binding else site.history_key,
        site_origin=origin(site.base_url), grant_revision=policy.revision,
        source_fingerprint=binding.fingerprint if binding else "",
        mode=policy.values.mode if binding else "analysis", status="queued",
        reason="" if binding else "source_snapshot_missing",
    )
    db.add(action)
    db.flush()
    record_signature(db, action)
    return action


def reuse_completed(db, manager, action):
    """Reuse original conclusions within the current rolling TTL, across chat dates.

    Caller must validate eligibility and _source_current before calling.
    """
    from app.services.assistant_takeover import _now
    from app.services.assistant_takeover_conclusions import _read
    from app.services.assistant_takeover_settings import TakeoverSettingsService

    if (action.status != "queued" or action.attempts != 0
            or action.request_id or action.import_receipt_json):
        return False
    signature = record_signature(db, action)
    if signature is None:
        return False
    binding = db.get(AssistantJobConfigSnapshot, action.source_job_id)
    job = db.get(SiteJob, action.source_job_id)
    if (binding is None or job is None or signature.version != VERSION
            or signature.config_revision != binding.revision
            or signature.config_fingerprint != action.source_fingerprint
            or signature.owner != action.owner or signature.site_history_key != action.site_history_key
            or signature.site_origin != action.site_origin
            or signature.failure_hash != failure_hash(db, job)):
        return False
    now = _now().astimezone(timezone.utc)
    ttl_days = TakeoverSettingsService(db).view().values.conclusion_ttl_days
    rows = db.execute(select(AssistantTakeoverAction, AssistantTakeoverConclusion).join(
        TakeoverFailureSignature, TakeoverFailureSignature.action_id == AssistantTakeoverAction.id,
    ).join(AssistantTakeoverConclusion,
           AssistantTakeoverConclusion.action_id == AssistantTakeoverAction.id).join(
        TakeoverAnalysisProvenance,
        TakeoverAnalysisProvenance.action_id == AssistantTakeoverAction.id,
    ).where(
        AssistantTakeoverConclusion.created_at > now - timedelta(days=ttl_days),
        AssistantTakeoverConclusion.created_at <= now,
        TakeoverAnalysisProvenance.source_current.is_(True),
        TakeoverAnalysisProvenance.source_fingerprint == signature.config_fingerprint,
        TakeoverAnalysisProvenance.inspected_fingerprint == signature.config_fingerprint,
        *(getattr(TakeoverFailureSignature, key) == getattr(signature, key) for key in MATCH_FIELDS),
        AssistantTakeoverAction.id != action.id,
        AssistantTakeoverAction.site_id == action.site_id,
        AssistantTakeoverAction.owner == action.owner,
        AssistantTakeoverAction.site_history_key == action.site_history_key,
        AssistantTakeoverAction.site_origin == action.site_origin,
        AssistantTakeoverAction.source_fingerprint == action.source_fingerprint,
        AssistantTakeoverAction.status.in_(("completed_analysis", "updated")),
    ).order_by(AssistantTakeoverConclusion.created_at.desc(), AssistantTakeoverAction.id))
    for source, conclusion in rows:
        data = _read(manager.crypto, conclusion, source)
        if (data is None or data.get("turn_outcome", {}).get("ok") is not True):
            continue
        source_binding = db.get(AssistantJobConfigSnapshot, source.source_job_id)
        if (source_binding is None or source_binding.revision != signature.config_revision
                or source_binding.fingerprint != signature.config_fingerprint
                or source_binding.history_key != action.site_history_key):
            continue
        session = db.get(AssistantSession, source.session_id) if source.session_id else None
        sid = session.id if session is not None and session.owner == action.owner else None
        changed = db.execute(update(AssistantTakeoverAction).where(
            AssistantTakeoverAction.id == action.id, AssistantTakeoverAction.status == "queued",
            AssistantTakeoverAction.attempts == 0, AssistantTakeoverAction.request_id.is_(None),
        ).values(status="reused", reason="same_config_same_failure", session_id=sid, updated_at=now))
        if changed.rowcount != 1:
            return False
        db.add(TakeoverDedupLink(action_id=action.id, source_action_id=source.id, created_at=now))
        db.flush()
        return True
    return False


def reused_conclusion(db, crypto, action, *, keys=(), completed_before=None):
    """One hop to original encrypted proof, never a chain or a fresh model claim."""
    from app.services.assistant_takeover_conclusions import _read

    if action.status != "reused" or action.reason != "same_config_same_failure":
        return None
    link = db.get(TakeoverDedupLink, action.id)
    if link is None:
        return None
    if completed_before is not None:
        created = link.created_at.replace(tzinfo=link.created_at.tzinfo or timezone.utc)
        if created > completed_before.replace(tzinfo=completed_before.tzinfo or timezone.utc):
            return None
    source = db.get(AssistantTakeoverAction, link.source_action_id)
    left = db.get(TakeoverFailureSignature, action.source_job_id)
    right = db.get(TakeoverFailureSignature, source.source_job_id) if source else None
    if (source is None or source.id == action.id or left is None or right is None
            or left.action_id != action.id or right.action_id != source.id
            or source.site_id != action.site_id
            or any(getattr(source, key) != getattr(action, key) for key in (
                "owner", "site_history_key", "site_origin", "source_fingerprint"))
            or any(getattr(left, key) != getattr(right, key) for key in MATCH_FIELDS)):
        return None
    row = db.get(AssistantTakeoverConclusion, source.id)
    provenance = db.get(TakeoverAnalysisProvenance, source.id)
    if (provenance is None or not provenance.source_current
            or provenance.source_fingerprint != source.source_fingerprint
            or provenance.inspected_fingerprint != source.source_fingerprint):
        return None
    data = _read(crypto, row, source, keys) if row else None
    if data is None or data.get("turn_outcome", {}).get("ok") is not True:
        return None
    prefix = (f"\u590d\u7528\u6b64\u524d\u5206\u6790\uff08\u4efb\u52a1 #{source.source_run_id}\uff09\uff1a"
              "\u5f53\u524d\u672a\u91cd\u65b0\u5206\u6790/\u672a\u4fee\u590d\u3002")
    return (prefix + data["conclusion"])[:300]
