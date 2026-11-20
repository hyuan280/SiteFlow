"""Encrypted, host-issued one-use authority to repeat an uncertain execution.

Non-post builtin retries receive exact host-issued authority within the category
policy. Explicit user approval APIs remain available for persisted grants.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import time
from uuid import UUID, uuid4

from sqlalchemy import select, update

from app.models.assistant import AssistantMessage, AssistantSession
from app.services.assistant_categories import Denied, auth_budget_context, execution_category
from app.services.assistant_tools import failure
from app.services.config_ai_generation import _strict_json


RECORDS = "_execution_records"
GRANTS = "_retry_authorizations"
PENDING = "_pending_retry_approval"
DECISIONS = "_retry_approval_decisions"
APPROVAL_TIMEOUT = 180
READ_ONLY = frozenset({"flow.open", "action.navigate", "action.wait", "flow.result"})


def fingerprint(value):
    # Canonicalize independently of JSON insertion order after encrypted reloads.
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def browser_binding(scope):
    return fingerprint([scope.session_id, scope.token, scope.lane_key])


def credential_binding(target):
    return {key: deepcopy(target.get(key)) for key in (
        "credential_mode", "site_id", "_credential_binding", "proxy",
    )}


def uncertain_operations(manager, db, session_id, data, *, decode=None):
    """Use the full durable history, not the current context window or browser."""
    decode = decode if decode is not None else manager._decrypt
    records = data.get(RECORDS, {})
    uncertain = {operation for operation, record in records.items()
                 if record.get("state") == "dispatched" or record.get("status") == "unknown"}
    uncertain.update(fact["operation_id"] for fact in data.get("_context_safety", [])
                     if fact.get("status") == "unknown" or fact.get("code") == "outcome_uncertain")
    for message in db.scalars(select(AssistantMessage).where(
        AssistantMessage.session_id == session_id, AssistantMessage.role == "tool",
    )):
        value = decode(message)
        if value.get("tool_name") not in {"execute_step", "retry_step"}:
            continue
        try:
            result = _strict_json(value["content"])
        except (ValueError, TypeError, KeyError, RecursionError):
            continue
        if (isinstance(result, dict) and isinstance(result.get("operation_id"), str)
                and (result.get("status") == "unknown" or result.get("code") == "outcome_uncertain")):
            uncertain.add(result["operation_id"])
    # Keep historical uncertainty intact. Only a linked, successful terminal retry
    # resolves the gate, not approval, target changes, summaries or page evidence.
    for operation in list(uncertain):
        current, seen = operation, set()
        while current not in seen:
            seen.add(current)
            resolution = records.get(current, {}).get("resolution", {})
            next_operation = resolution.get("retry_operation_id")
            retry = records.get(next_operation, {})
            if retry.get("retry_of") != current or retry.get("state") != "terminal":
                break
            if retry.get("status") == "success" and retry.get("dispatched"):
                uncertain.discard(operation)
                break
            current = next_operation
    return uncertain


def valid_grant(data, scope, operation, capability, retry_operation=None):
    grant = data.get(GRANTS, {}).get(operation)
    if (not isinstance(grant, dict) or grant.get("id") != capability
            or grant.get("target_revision") != data.get("_target_revision", 0)
            or grant.get("target_fingerprint") != fingerprint(data.get("target"))
            or scope is None or grant.get("browser_binding") != browser_binding(scope)
            or fingerprint(grant.get("step")) != grant.get("step_fingerprint")):
        return False
    # Expiry bounds admission, not acceptance of an already consumed operation.
    return (grant.get("status") == "approved" and grant.get("expires_at", 0) > time.time()) or (
        grant.get("status") == "used" and retry_operation is not None
        and grant.get("retry_operation_id") == retry_operation
    )


def retry_status(data, scope=None) -> list[dict]:
    """Public projection: no step, capability, fingerprint or browser identity."""
    result = []
    for operation, grant in data.get(GRANTS, {}).items():
        status = grant["status"]
        if status == "approved" and (
            grant["expires_at"] <= time.time()
            or grant["target_revision"] != data.get("_target_revision", 0)
            or grant["target_fingerprint"] != fingerprint(data.get("target"))
            or scope is None or grant["browser_binding"] != browser_binding(scope)
        ):
            status = "expired"
        result.append({
            "operation_id": operation, "retry_operation_id": grant.get("retry_operation_id"),
            "expires_at": datetime.fromtimestamp(grant["expires_at"], timezone.utc).isoformat(),
            "status": status, "target_revision": grant["target_revision"],
        })
    return result


def model_retry_status(data, scope=None) -> list[dict]:
    """Safe context evidence; this projection itself confers no authority."""
    records = data.get(RECORDS, {})
    return [{**item, **({"resolution": deepcopy(records[item["operation_id"]]["resolution"])}
                       if records.get(item["operation_id"], {}).get("resolution") else {})}
            for item in retry_status(data, scope)]


def can_reauthorize(data, operation):
    grant = data.get(GRANTS, {}).get(operation, {})
    if grant.get("status") != "used":
        return True
    previous = data.get(RECORDS, {}).get(grant.get("retry_operation_id"), {})
    return (previous.get("retry_of") == operation and previous.get("state") == "terminal"
            and previous.get("status") in {"failed", "error"})


def eligible_record(manager, db, owner, sid, data, operation):
    """Shared authority checks for idle grants and live approval admission/decision."""
    from app.services.assistant import AssistantError
    from app.services.assistant_execution import AssistantExecution, _Denied, _origin

    if operation not in uncertain_operations(manager, db, sid, data):
        raise AssistantError("retry_not_uncertain")
    target = data.get("target")
    if not target:
        raise AssistantError("target_confirmation_required")
    scope = manager.browsers.get(sid)
    record = data.get(RECORDS, {}).get(operation)
    helper = AssistantExecution(manager, owner, sid, target)
    if record is None:
        # Legacy reconstruction requires the original explicitly stored revision.
        for message in db.scalars(select(AssistantMessage).where(
            AssistantMessage.session_id == sid, AssistantMessage.role == "tool",
        ).order_by(AssistantMessage.id)):
            value = manager._decrypt(message)
            if value.get("tool_name") != "execute_step":
                continue
            try:
                result = _strict_json(value["content"])
            except (ValueError, TypeError, KeyError, RecursionError):
                continue
            if (not isinstance(result, dict) or result.get("operation_id") != operation
                    or result.get("status") != "unknown"):
                continue
            if value.get("_target_revision") != data.get("_target_revision", 0):
                raise AssistantError("retry_step_unavailable")
            try:
                step = helper._normalize(value["arguments"]["step"])
            except Exception:
                raise AssistantError("retry_step_unavailable") from None
            record = {"step": step, "origin": list(_origin(target["base_url"])),
                      "step_fingerprint": fingerprint(step),
                      "target_fingerprint": fingerprint(target),
                      "credential_binding": credential_binding(target),
                      "target_revision": data.get("_target_revision", 0),
                      "state": "terminal", "status": "unknown", "code": "outcome_uncertain"}
            records = data.setdefault(RECORDS, {})
            if len(records) >= 256:
                raise AssistantError("execution_operation_limit")
            records[operation] = record
            break
    if not record or not isinstance(record.get("step"), dict):
        raise AssistantError("retry_step_unavailable")
    step = record["step"]
    if record.get("step_fingerprint") != fingerprint(step):
        raise AssistantError("retry_step_unavailable")
    if (record.get("origin") != list(_origin(target["base_url"]))
            or record.get("credential_binding") != credential_binding(target)):
        raise AssistantError("retry_binding_changed")
    try:
        if helper._normalize(step) != step:
            raise ValueError()
        helper._authorize(scope, step["feature_type"], check_uncertain=False, db=db)
    except _Denied as exc:
        raise AssistantError(str(exc)) from None
    except Exception:
        raise AssistantError("retry_step_unavailable") from None
    old = data.get(GRANTS, {}).get(operation)
    if old and old.get("status") == "used":
        if not can_reauthorize(data, operation):
            raise AssistantError("retry_already_used")
        previous = data[RECORDS][old["retry_operation_id"]]
        previous.setdefault("retry_authorization", deepcopy(old))
    return record


def issue_grant(data, scope, operation, step, **binding):
    grant = {"id": str(uuid4()), "status": "approved", "expires_at": time.time() + 300,
             "step": deepcopy(step), "step_fingerprint": fingerprint(step),
             "target_revision": data.get("_target_revision", 0),
             "target_fingerprint": fingerprint(data.get("target")),
             "browser_binding": browser_binding(scope), **binding}
    data.setdefault(GRANTS, {})[operation] = grant
    return grant


def pending_approval(data):
    """The public snapshot deliberately excludes all execution authority."""
    pending = data.get(PENDING)
    if not pending or pending.get("status") != "pending":
        return None
    return {key: pending[key] for key in (
        "id", "operation_id", "feature_type", "status", "created_at", "expires_at", "title",
    )}


def approval_result(status, operation, *, reason=None):
    code, message = {
        "denied": ("retry_denied", "The user denied this retry. No action was executed."),
        "timed_out": ("retry_approval_timeout", "Retry approval timed out. No action was executed."),
        "cancelled": ("cancelled", "Retry approval was cancelled. No action was executed."),
        "invalidated": ("retry_approval_invalidated", "Retry authority changed. No action was executed."),
    }[status]
    return {**failure(code), "operation_id": operation, "message": message,
            **({"reason": reason} if reason else {})}


def finish_approval(data, approval, status, *, reason=None):
    approval["status"] = status
    if status != "approved":
        approval["result"] = approval_result(status, approval["operation_id"], reason=reason)
        grant = data.get(GRANTS, {}).get(approval["operation_id"], {})
        if grant.get("approval_id") == approval["id"] and grant.get("status") == "approved":
            grant["status"] = "cancelled" if status == "cancelled" else "expired"
    if data.get(PENDING, {}).get("id") == approval["id"]:
        data.pop(PENDING, None)


def cancel_approvals(data):
    """Finalization/restart must never leave a resumable approval capability."""
    for approval in data.get(DECISIONS, {}).values():
        if approval["status"] == "pending" or (
                approval["status"] == "approved" and "result" not in approval):
            finish_approval(data, approval, "cancelled")
    data.pop(PENDING, None)


def approval_binding(data, scope, approval):
    return (scope is not None
            and approval["target_revision"] == data.get("_target_revision", 0)
            and approval["target_fingerprint"] == fingerprint(data.get("target"))
            and approval["browser_binding"] == browser_binding(scope)
            and approval["step_fingerprint"] == fingerprint(
                data.get(RECORDS, {}).get(approval["operation_id"], {}).get("step")))


def running_row(manager, db, owner, sid):
    from app.services.assistant import AssistantError

    changed = db.execute(update(AssistantSession).where(
        AssistantSession.id == sid, AssistantSession.owner == owner,
        AssistantSession.status == "running",
    ).values(status="running"))
    if changed.rowcount != 1:
        raise AssistantError("retry_approval_not_running")
    db.expire_all()
    return manager._owned(db, owner, sid)


async def decide_retry_approval(manager, owner, sid, approval_id, decision):
    from app.services.assistant import AssistantError

    if decision not in {"approve", "deny"}:
        raise AssistantError("invalid_arguments", 422)
    async with manager._mutation(owner, sid):
        with manager.session_factory() as db:
            # Terminal replays are read-only, including after the turn becomes idle.
            row = manager._owned(db, owner, sid)
            data = manager._decrypt(row)
            approval = next((item for item in data.get(DECISIONS, {}).values()
                             if item["id"] == approval_id), None)
            if approval is None:
                raise AssistantError("retry_approval_not_found", 404)
            if approval["status"] != "pending":
                if approval.get("decision") == decision and approval["status"] in {"approved", "denied"}:
                    return manager._detail(db, row)
                raise AssistantError("retry_approval_conflict")
            row = running_row(manager, db, owner, sid)
            data = manager._decrypt(row)
            approval = next((item for item in data.get(DECISIONS, {}).values()
                             if item["id"] == approval_id), None)
            if approval and approval["status"] != "pending":
                if approval.get("decision") == decision and approval["status"] in {"approved", "denied"}:
                    return manager._detail(db, row)
                raise AssistantError("retry_approval_conflict")
            if not approval:
                raise AssistantError("retry_approval_conflict")
            if approval["expires_epoch"] <= time.time():
                finish_approval(data, approval, "timed_out")
            else:
                try:
                    scope = manager.browsers.get(sid)
                    if not approval_binding(data, scope, approval):
                        raise AssistantError("retry_binding_changed")
                    record = eligible_record(manager, db, owner, sid, data, approval["operation_id"])
                except AssistantError as exc:
                    finish_approval(data, approval, "invalidated", reason=exc.code)
                else:
                    approval["decision"] = decision
                    if decision == "deny":
                        finish_approval(data, approval, "denied")
                    else:
                        issue_grant(data, scope, approval["operation_id"], record["step"],
                                    approval_id=approval_id, invocation=approval["invocation"])
                        finish_approval(data, approval, "approved")
            manager._encrypt(row, data)
            db.commit()
            detail = manager._detail(db, row)
    # Events are hints only. Durable state is authoritative and polling repairs loss.
    event = manager._approval_waiters.get(approval_id)
    if event:
        event.set()
    if approval["status"] not in {"approved", "denied"}:
        raise AssistantError("retry_approval_" + approval["status"])
    return detail


async def run_with_retry_approval(manager, owner, sid, operation_id, *, turn, invocation, auth_limit=None):
    from app.services.assistant import AssistantError

    try:
        if not isinstance(operation_id, str):
            raise ValueError()
        operation = str(UUID(operation_id))
    except ValueError:
        return failure("invalid_arguments")
    key = turn["id"] + ":" + operation
    approval_id = None
    try:
        with manager.session_factory() as db:
            row = running_row(manager, db, owner, sid)
            data = manager._decrypt(row)
            ledger = data.setdefault(DECISIONS, {})
            previous = ledger.get(key)
            if previous:
                return deepcopy(previous.get("result") or failure("retry_approval_already_requested"))
            scope = manager.browsers.get(sid)
            grant = data.get(GRANTS, {}).get(operation)
            preapproved = bool(grant and not grant.get("invocation")
                               and valid_grant(data, scope, operation, grant["id"]))
            record = eligible_record(manager, db, owner, sid, data, operation)
            automatic = execution_category(db, record["step"]["feature_type"]) != "post"
            if automatic:
                turn_id, _ = auth_budget_context(
                    manager, db, sid, data, turn=turn, auth_limit=auth_limit)
                if grant and grant.get("automatic") and valid_grant(data, scope, operation, grant["id"]):
                    return failure("retry_authorization_required")
                grant = issue_grant(data, scope, operation, record["step"], automatic=True,
                                    turn_id=turn_id, invocation=deepcopy(invocation))
                manager._encrypt(row, data)
            elif not preapproved:
                remaining = turn["deadline"] - asyncio.get_running_loop().time()
                if data.get(PENDING):
                    return failure("retry_approval_already_pending")
                if len(ledger) >= 256:
                    return failure("retry_approval_limit")
                now = time.time()
                expires = now + APPROVAL_TIMEOUT if remaining > 0 else now
                approval_id = str(uuid4())
                approval = {"id": approval_id, "operation_id": operation, "status": "pending",
                            "feature_type": record["step"]["feature_type"],
                            "title": "Retry " + record["step"]["feature_type"],
                            "created_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                            "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat(),
                            "expires_epoch": expires, "invocation": deepcopy(invocation),
                            "target_revision": data.get("_target_revision", 0),
                            "target_fingerprint": fingerprint(data.get("target")),
                            "browser_binding": browser_binding(scope),
                            "step_fingerprint": record["step_fingerprint"]}
                ledger[key] = approval
                if remaining <= 0:
                    finish_approval(data, approval, "timed_out")
                else:
                    data[PENDING] = approval
                manager._encrypt(row, data)
            db.commit()
        if automatic:
            try:
                return await run_authorized_retry(
                    manager, owner, sid, operation, invocation=invocation, turn=turn, auth_limit=auth_limit)
            finally:
                # A denied/cancelled admission must not leave reusable authority.
                with manager.session_factory() as db:
                    db.execute(update(AssistantSession).where(
                        AssistantSession.id == sid, AssistantSession.owner == owner,
                    ).values(updated_at=AssistantSession.updated_at))
                    db.expire_all()
                    row = manager._owned(db, owner, sid)
                    data = manager._decrypt(row)
                    current = data.get(GRANTS, {}).get(operation, {})
                    if current.get("id") == grant["id"] and current.get("status") == "approved":
                        current["status"] = "expired"
                        manager._encrypt(row, data)
                        db.commit()
        if preapproved:
            return await run_authorized_retry(manager, owner, sid, operation, turn=turn, auth_limit=auth_limit)
        event = asyncio.Event()
        manager._approval_waiters[approval_id] = event
        while True:
            with manager.session_factory() as db:
                row = running_row(manager, db, owner, sid)
                data = manager._decrypt(row)
                approval = data[DECISIONS][key]
                previous_status = approval["status"]
                if approval["status"] in {"pending", "approved"}:
                    if not approval_binding(data, manager.browsers.get(sid), approval):
                        finish_approval(data, approval, "invalidated", reason="retry_binding_changed")
                    elif approval["status"] == "pending" and approval["expires_epoch"] <= time.time():
                        finish_approval(data, approval, "timed_out")
                    elif approval["status"] == "approved":
                        try:
                            eligible_record(manager, db, owner, sid, data, operation)
                        except AssistantError as exc:
                            finish_approval(data, approval, "invalidated", reason=exc.code)
                status = approval["status"]
                if approval["status"] != previous_status:
                    manager._encrypt(row, data)
                db.commit()
                if status not in {"pending", "approved"}:
                    return deepcopy(approval["result"])
            if status == "approved":
                result = await run_authorized_retry(manager, owner, sid, operation, invocation=invocation,
                                                    turn=turn, auth_limit=auth_limit)
                with manager.session_factory() as db:
                    row = running_row(manager, db, owner, sid)
                    data = manager._decrypt(row)
                    data[DECISIONS][key]["result"] = result
                    grant = data.get(GRANTS, {}).get(operation, {})
                    if grant.get("approval_id") == approval_id and grant.get("status") == "approved":
                        grant["status"] = "expired"
                    manager._encrypt(row, data)
                    db.commit()
                return result
            await manager._wait_for_user(turn, event, min(0.25, max(
                0.001, approval["expires_epoch"] - time.time())))
            event.clear()
    except (AssistantError, Denied) as exc:
        return failure(exc.code if isinstance(exc, AssistantError) else str(exc))
    finally:
        if approval_id:
            manager._approval_waiters.pop(approval_id, None)
            with manager.session_factory() as db:
                # Also clean up direct dispatch callers without a manager finalizer.
                db.execute(update(AssistantSession).where(
                    AssistantSession.id == sid, AssistantSession.owner == owner,
                ).values(updated_at=AssistantSession.updated_at))
                row = manager._owned(db, owner, sid)
                data = manager._decrypt(row)
                approval = data.get(DECISIONS, {}).get(key)
                if approval and "result" not in approval:
                    finish_approval(data, approval, "cancelled")
                    manager._encrypt(row, data)
                    db.commit()


async def authorize_retry(manager, owner, sid, payload) -> dict:
    from app.services.assistant import AssistantError

    async with manager._mutation(owner, sid):
        with manager.session_factory() as db:
            manager._reserve_idle(db, owner, sid)
            db.expire_all()
            row = manager._owned(db, owner, sid)
            data = manager._decrypt(row)
            operation, request = str(payload.operation_id), str(payload.request_id)
            requests = data.setdefault("retry_requests", {})
            if request in requests:
                if requests[request]["operation_id"] != operation:
                    raise AssistantError("retry_request_conflict")
            else:
                if len(requests) >= 256:
                    raise AssistantError("retry_request_limit")
                record = eligible_record(manager, db, owner, sid, data, operation)
                scope = manager.browsers.get(sid)
                step = record["step"]
                old = data.setdefault(GRANTS, {}).get(operation)
                if old and valid_grant(data, scope, operation, old["id"]):
                    grant = old
                else:
                    grant = issue_grant(data, scope, operation, step)
                requests[request] = {"operation_id": operation, "grant_id": grant["id"]}
            manager._encrypt(row, data)
            row.status = "idle"
            db.commit()
            detail = manager._detail(db, row)
            detail["retry_authorizations"] = retry_status(data, manager.browsers.get(sid))
            return detail


async def run_authorized_retry(manager, owner, sid, operation_id, *, invocation=None, turn=None,
                               auth_limit=None) -> dict:
    from app.services.assistant_execution import AssistantExecution

    try:
        if not isinstance(operation_id, str):
            raise ValueError()
        operation = str(UUID(operation_id))
    except ValueError:
        return failure("invalid_arguments")
    with manager.session_factory() as db:
        row = manager._owned(db, owner, sid)
        data = manager._decrypt(row)
        grant = data.get(GRANTS, {}).get(operation)
        scope = manager.browsers.get(sid)
        if not grant or grant.get("status") != "approved" or not valid_grant(data, scope, operation, grant["id"]):
            return {**failure("retry_authorization_required"),
                    "notice": "Use retry_step to request host approval and wait in the same call."}
        if grant.get("invocation") is not None and grant["invocation"] != invocation:
            return failure("retry_authorization_required")
        target, step, capability = deepcopy(data.get("target")), deepcopy(grant["step"]), grant["id"]
    return await AssistantExecution(
        manager, owner, sid, target,
        retry_authorization={"operation_id": operation, "id": capability},
        turn=turn, invocation=invocation, auth_limit=auth_limit,
    ).execute({"operation_id": str(uuid4()), "step": step})
