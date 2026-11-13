"""Target-independent scheduler reads and exact, user-reviewed global writes."""

from copy import deepcopy
import hashlib

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from app.models.assistant_workflow import AssistantWorkflowAction
from app.schemas.assistant_scheduler import UpdateScheduler
from app.schemas.scheduler import SchedulerSettingsUpdate
from app.services.assistant_workflows import AssistantWorkflows, WorkflowDenied
from app.services.config_ai_generation import _json
from app.services.scheduler_settings import (
    SchedulerConflictError, SchedulerLockedError, SchedulerSettingsService, SchedulerValidationError,
)


LABELS = {"enabled": "\u5168\u5c40\u8c03\u5ea6\u5f00\u5173", "mode": "\u8c03\u5ea6\u6a21\u5f0f",
          "cron": "cron \u8868\u8fbe\u5f0f", "interval_seconds": "\u95f4\u9694\u65f6\u95f4\uff08\u79d2\uff09",
          "ranges": "\u968f\u673a\u65f6\u95f4\u8303\u56f4"}


def canonical(request):
    return {"operation_id": str(request.operation_id),
            "patch": request.patch.model_dump(exclude_unset=True)}


def action_for(db, tools, operation_id):
    return db.scalar(select(AssistantWorkflowAction).where(
        AssistantWorkflowAction.session_id == tools.session_id,
        AssistantWorkflowAction.owner == tools.owner,
        AssistantWorkflowAction.kind == "scheduler_config",
        AssistantWorkflowAction.request_fingerprint == hashlib.sha256(
            _json({"operation_id": operation_id}).encode()).hexdigest()))


def replay(db, tools, request, data):
    action = action_for(db, tools, request["operation_id"])
    if action is not None:
        workflows = AssistantWorkflows(tools.manager, tools.owner, tools.session_id, None)
        saved = workflows._open(action)
        if saved.get("request") != request:
            raise WorkflowDenied("scheduler_operation_conflict")
        if action.state != "complete":
            raise WorkflowDenied("scheduler_cancelled")
        return {**saved["result"], "replayed": True}
    for item in data.get("_interaction_decisions", {}).values():
        previous = item.get("arguments", {}).get("request", {})
        if item.get("purpose") == "scheduler_config" and previous.get(
            "operation_id") == request["operation_id"]:
            if previous != request:
                raise WorkflowDenied("scheduler_operation_conflict")
            if item.get("result") is not None:
                return deepcopy(item["result"])
    return None


def check_context(db, tools):
    tools.manager._owned(db, tools.owner, tools.session_id)
    if tools.manager._stopping or not tools.manager.settings.config_ai_enabled:
        raise WorkflowDenied("workflow_execution_disabled")
    return SchedulerSettingsService(db, tools.manager.settings)


def prepare_scheduler_update(db, tools, request, data):
    """Return the host review, frozen private arguments and turn deduplication subject."""
    request = UpdateScheduler.model_validate(canonical(request), strict=True)
    args = canonical(request)
    service = check_context(db, tools)
    binding = service.binding()
    current = service.view()
    candidate = SchedulerSettingsUpdate(**{
        **{name: getattr(current, name) for name in LABELS}, **args["patch"]})
    candidate = service.preview(candidate)
    actual = {name: getattr(candidate, name) for name in args["patch"]
              if getattr(current, name) != getattr(candidate, name)}
    if not actual:
        raise WorkflowDenied("scheduler_no_changes")
    changes = [{"field": name, "label": LABELS[name], "before": getattr(current, name),
                "after": actual[name]} for name in LABELS if name in actual]
    review = {"scope": "global_scheduler", "timezone": current.timezone,
              "revision": current.revision, "changes": changes,
              "warnings": ["\u672c\u6b21\u53d8\u66f4\u4ec5\u5f71\u54cd\u5168\u5c40\u8c03\u5ea6\uff0c\u4e0d\u4f1a\u4fee\u6539\u4efb\u4f55\u7ad9\u70b9\u8bbe\u7f6e\u3002",
                           "\u4e0b\u6b21\u8fd0\u884c\u65f6\u95f4\u5c06\u91cd\u65b0\u8ba1\u7b97\u3002",
                           "\u65f6\u533a\u7531\u73af\u5883\u53d8\u91cf\u7ba1\u7406\uff0c\u65e0\u6cd5\u5728\u6b64\u4fee\u6539\u3002"]}
    public = {"kind": "authorization", "purpose": "scheduler_config", "options": [],
              "fields": [], "multiple": False, "title": "\u786e\u8ba4\u5168\u5c40\u8c03\u5ea6\u8bbe\u7f6e\u53d8\u66f4",
              "description": "\u4ec5\u6388\u6743\u4e0b\u5217\u7cbe\u786e\u7684\u5168\u5c40\u8c03\u5ea6\u53d8\u66f4\u3002\u5168\u5c40\u81ea\u52a8\u6388\u6743\u4e0d\u9002\u7528\uff0c\u62d2\u7edd\u6216\u8d85\u65f6\u4e0d\u4f1a\u4fee\u6539\u8bbe\u7f6e\u3002",
              "review": review}
    private = {"request": args, "owner": tools.owner, "session_id": tools.session_id,
               "binding": binding, "actual_patch": actual,
               "candidate": candidate.model_dump()}
    return public, private, "scheduler_config:" + hashlib.sha256(_json(actual).encode()).hexdigest()


def apply_scheduler_update(db, tools, data, args):
    """Apply inside the interaction's transaction; never commit the session or receipt here."""
    request = args["request"]
    previous = replay(db, tools, request, data)
    if previous is not None:
        return previous
    service = check_context(db, tools)
    if args["owner"] != tools.owner or args["session_id"] != tools.session_id:
        raise WorkflowDenied("scheduler_binding_changed")
    # Preview rechecks environment locks even if the effective values look unchanged.
    candidate = service.preview(SchedulerSettingsUpdate.model_validate(args["candidate"]))
    if service.binding() != args["binding"]:
        raise WorkflowDenied("scheduler_binding_changed")
    updated = service.update(candidate, expected_revision=args["binding"]["revision"], _commit=False,
                             fields=set(args["actual_patch"]))
    result = {"ok": True, "purpose": "scheduler_config", "scope": "global_scheduler",
              "operation_id": request["operation_id"], "persisted": True, "decision": "approve",
              "revision": updated.revision, "changed_fields": sorted(args["actual_patch"]),
              "settings": updated.model_dump(mode="json")}
    workflows = AssistantWorkflows(tools.manager, tools.owner, tools.session_id, None)
    action, existed = workflows._action(db, "scheduler_config", {"operation_id": request["operation_id"]})
    if existed:
        raise WorkflowDenied("scheduler_operation_conflict")
    action.state = "complete"
    workflows._seal(action, {"request": request, "binding": args["binding"],
                             "owner": tools.owner, "result": result})
    return result


async def dispatch(tools, name, args):
    from app.services.assistant import AssistantError
    from app.services.assistant_interactions import request_interaction

    try:
        if name in {"read_scheduler_settings", "get_scheduler_settings"}:
            if not isinstance(args, dict) or args:
                return {"ok": False, "code": "invalid_arguments"}
            with tools.manager.session_factory() as db:
                tools.manager._owned(db, tools.owner, tools.session_id)
                view = SchedulerSettingsService(db, tools.manager.settings).view()
                return {"ok": True, "scope": "global_scheduler",
                        "settings": view.model_dump(mode="json")}
        if name != "update_scheduler_settings":
            return {"ok": False, "code": "unknown_tool"}
        request = UpdateScheduler.model_validate(args, strict=True)
        with tools.manager.session_factory() as db:
            row = tools.manager._owned(db, tools.owner, tools.session_id)
            result = replay(db, tools, canonical(request), tools.manager._decrypt(row))
            if result is not None:
                return result
        return await request_interaction(tools, {"purpose": "scheduler_config", **canonical(request)})
    except ValidationError:
        return {"ok": False, "code": "invalid_arguments"}
    except (SchedulerConflictError, IntegrityError, OperationalError):
        return {"ok": False, "code": "scheduler_conflict"}
    except SchedulerLockedError:
        return {"ok": False, "code": "scheduler_locked"}
    except SchedulerValidationError:
        return {"ok": False, "code": "scheduler_invalid"}
    except (WorkflowDenied, AssistantError) as exc:
        return {"ok": False, "code": exc.code if isinstance(exc, AssistantError) else str(exc)}
    except Exception:
        return {"ok": False, "code": "scheduler_unavailable"}


def tool_definitions():
    return [{"type": "function", "function": {
        "name": "get_scheduler_settings", "description": "Read global scheduler settings, "
            "revision, environment lock and timezone without a site target or any writes.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
        {"type": "function", "function": {
            "name": "update_scheduler_settings", "description": "Only for explicitly requested "
                "global scheduler changes. Review the exact sparse patch and wait up to 180 seconds "
                "for authenticated user approval. Automatic authorization never bypasses review. "
                "No site settings or timezone changes. Reuse operation_id only for exact replays.",
            "parameters": UpdateScheduler.model_json_schema()}}]
