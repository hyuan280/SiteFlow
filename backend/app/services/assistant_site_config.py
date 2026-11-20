"""Exact user-reviewed site patches, committed with their encrypted action receipt."""

from copy import deepcopy
import hashlib
import json
import re
from urllib.parse import unquote

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from app.models.assistant_workflow import AssistantWorkflowAction
from app.models.execution import SiteJob
from app.models.workflow import Site
from app.schemas.assistant_site_config import UpdateSiteConfig
from app.services.assistant_urls import contains_redaction, origin, project_url
from app.services.assistant_workflows import AssistantWorkflows, WorkflowDenied, bound_workflow, workflow_binding
from app.services.config_ai_generation import _json
from app.services.workflows import WorkflowError


LABELS = {
    "name": "\u7ad9\u70b9\u540d\u79f0", "trusted_urls": "\u51ed\u636e\u4fe1\u4efb URL \u6b63\u5219",
    "enabled": "\u542f\u7528\u5b9a\u65f6\u8c03\u5ea6", "lane_policy": "\u4fdd\u5b58\u8fd0\u884c\u8f66\u9053",
    "support_resign": "\u5141\u8bb8\u8865\u7b7e", "resign_max_days": "\u6700\u5927\u8865\u7b7e\u5929\u6570",
    "checkin_after": "\u7b7e\u5230\u8d77\u59cb\u65f6\u95f4", "timezone": "\u65f6\u533a",
    "tags": "\u6807\u7b7e", "notes": "\u5907\u6ce8",
    "credential_provider_id": "Credential provider",
    "observation.proxy": "\u89c2\u5bdf\u6d4f\u89c8\u5668\u4ee3\u7406",
    "observation.trusted_origins": "\u89c2\u5bdf\u4fe1\u4efb\u6765\u6e90",
}


def unsafe_site_config(arguments, key="", *, pattern=False):
    """Reject rather than alter sensitive authored values, including regex URL inputs."""
    from app.services.assistant_interactions import secret_input

    if secret_input(arguments, key) or contains_redaction(arguments):
        return True
    if isinstance(arguments, dict):
        return any(unsafe_site_config(value, key, pattern=name == "trusted_urls")
                   for name, value in arguments.items())
    if isinstance(arguments, list):
        return any(unsafe_site_config(value, key, pattern=pattern) for value in arguments)
    if isinstance(arguments, str):
        # Unescaping is for privacy checks only. Stored/reviewed patterns stay exact.
        inspected = re.sub(r"\\([./?&#:=@%-])", r"\1", arguments)
        if pattern or inspected.startswith(("^", r"\A")):
            for _ in range(10):
                # Optional scheme syntax is privacy evidence, not a literal URL.
                inspected = re.sub(r"http(?:s\?|\((?:\?:)?s\)\?)://", "https://", inspected, flags=re.I)
                if re.search(r"://[^/?#]*@", inspected):
                    return True
                # A regex quantifier/group marker must not hide a later delimiter.
                for delimiter in re.finditer(r"[?#]", inspected):
                    try:
                        if project_url("https://example.com/" + inspected[delimiter.start():])["url_redacted"]:
                            return True
                    except ValueError:
                        return True
                decoded = unquote(inspected)
                if decoded == inspected:
                    break
                inspected = decoded
        for match in re.finditer(r"https?://[^\s<>\"'`]+", inspected):
            try:
                if project_url(match.group())["url_redacted"]:
                    return True
            except ValueError:
                # Regex syntax may not be a URL. Still inspect query/fragment
                # parameters with the shared URL projection, without rewriting it.
                suffix = re.search(r"[?#].*", match.group())
                if "@" in match.group().split("/", 3)[2]:
                    return True
                if suffix:
                    try:
                        if project_url("https://example.com/" + suffix.group())["url_redacted"]:
                            return True
                    except ValueError:
                        return True
    return False


def canonical(request):
    return {"operation_id": str(request.operation_id),
            "patch": request.patch.model_dump(exclude_unset=True),
            "observation": request.observation.model_dump(exclude_unset=True),
            "successful_run_id": request.successful_run_id}


def action_for(db, workflows, operation_id):
    return db.scalar(select(AssistantWorkflowAction).where(
        AssistantWorkflowAction.session_id == workflows.session_id,
        AssistantWorkflowAction.owner == workflows.owner,
        AssistantWorkflowAction.kind == "site_config",
        AssistantWorkflowAction.request_fingerprint == hashlib.sha256(
            _json({"operation_id": operation_id}).encode()).hexdigest()))


def destination_identity(db, target):
    identity = {k: target.get(k) for k in ("base_url", "workflow_site_id")}
    identity["history_key"] = None
    if target.get("workflow_site_id") is not None:
        binding = target.get("_workflow_binding") or {}
        site = db.get(Site, target["workflow_site_id"])
        if (site is None or binding.get("id") != site.id or not binding.get("history_key")
                or binding["history_key"] != site.history_key
                or origin(site.base_url) != origin(target["base_url"])):
            raise WorkflowDenied("workflow_binding_changed")
        identity["history_key"] = site.history_key
    return identity


def replay(db, tools, request, data):
    workflows = AssistantWorkflows(tools.manager, tools.owner, tools.session_id, data.get("target"))
    action = action_for(db, workflows, request["operation_id"])
    if action is None:
        for item in data.get("_interaction_decisions", {}).values():
            previous = item.get("arguments", {})
            if item.get("purpose") == "site_config" and previous.get("request", {}).get(
                    "operation_id") == request["operation_id"]:
                if previous["request"] != request:
                    raise WorkflowDenied("site_config_operation_conflict")
                if item.get("result") is not None:
                    return deepcopy(item["result"])
        return None
    saved = workflows._open(action)
    if saved.get("request") != request:
        raise WorkflowDenied("site_config_operation_conflict")
    if action.state != "complete":
        raise WorkflowDenied("site_config_cancelled")
    target = data.get("target") or {}
    if saved.get("destination") != destination_identity(db, target):
        raise WorkflowDenied("workflow_binding_changed")
    return {**saved["result"], "replayed": True}


def check_context(db, tools, data, patch):
    manager = tools.manager
    target = data.get("target") or {}
    if manager._stopping or not manager.settings.config_ai_enabled:
        raise WorkflowDenied("workflow_execution_disabled")
    if target.get("confirmed") is not True:
        raise WorkflowDenied("target_confirmation_required")
    site = bound_workflow(db, target) if patch else None
    if site is not None:
        from app.services.assistant_run_guard import unresolved_assistant_runs
        from app.services.assistant_retry import uncertain_operations

        if db.scalar(select(SiteJob.id).where(
                SiteJob.site_id == site.id, SiteJob.status.in_(("queued", "running")))):
            raise WorkflowDenied("site_run_active")
        if (unresolved_assistant_runs(db, site_id=site.id)
                or db.scalar(select(AssistantWorkflowAction.id).where(
                    AssistantWorkflowAction.kind == "run", AssistantWorkflowAction.site_id == site.id,
                    AssistantWorkflowAction.state.in_(("reserved", "unknown"))))):
            raise WorkflowDenied("workflow_outcome_uncertain")
        if uncertain_operations(manager, db, tools.session_id, data):
            raise WorkflowDenied("retry_authorization_required")
    return site


def credential_provider_binding(db, provider_id):
    if provider_id in {"builtin.manual", "builtin.bitwarden"}:
        return {"id": provider_id}
    from app.services.assistant_categories import Denied
    from app.services.assistant_provider_policy import provider_binding

    try:
        return provider_binding(db, provider_id, "secret.credentials")
    except Denied as exc:
        raise WorkflowDenied(str(exc)) from None


def prepare_site_config(db, tools, request, data):
    args = canonical(request)
    if unsafe_site_config(args, tools.config.get("api_key", "")):
        raise WorkflowDenied("secret_input")
    patch, observation = args["patch"], args["observation"]
    site = check_context(db, tools, data, patch)
    provider = None
    if "credential_provider_id" in patch:
        provider = credential_provider_binding(db, patch["credential_provider_id"])
    target = data["target"]
    workflows = AssistantWorkflows(tools.manager, tools.owner, tools.session_id, target)
    service = workflows._service(db)
    current = {name: getattr(site, name) for name in LABELS if "." not in name and name not in {
        "trusted_urls", "tags"}} if site else {}
    if site:
        current.update(trusted_urls=json.loads(site.trusted_urls_json), tags=json.loads(site.tags_json))
    actual = {name: value for name, value in patch.items() if current[name] != value}
    successful_run_id = None
    if patch.get("enabled") is True:
        from app.services.assistant_run_guard import require_enable_success

        successful_run_id = require_enable_success(
            db, tools.owner, tools.session_id, site, service, args["successful_run_id"])
    changes, warnings = [], []
    for name, after in actual.items():
        before = current[name]
        if name == "notes":
            before = "\u5df2\u6709\u5907\u6ce8\uff08\u5185\u5bb9\u4e0d\u5c55\u793a\uff09" if before else ""
        elif name in {"trusted_urls", "tags"}:
            before = f"{len(before)} \u9879\uff08\u65e7\u5185\u5bb9\u4e0d\u5c55\u793a\uff09"
        elif unsafe_site_config(before, tools.config.get("api_key", "")):
            before = "\u5df2\u9690\u85cf"
        changes.append({"field": name, "label": LABELS[name], "before": before, "after": after})
    for name, after in observation.items():
        before = target.get(name, [] if name == "trusted_origins" else False)
        if before != after:
            field = "observation." + name
            changes.append({"field": field, "label": LABELS[field], "before": before, "after": after})
    if not changes:
        raise WorkflowDenied("site_config_no_changes")
    if "trusted_urls" in actual:
        warnings.append("\u51ed\u636e\u4fe1\u4efb\u6b63\u5219\u53ef\u80fd\u5141\u8bb8\u5411\u5339\u914d\u5730\u5740\u53d1\u9001\u51ed\u636e\uff0c\u8bf7\u6838\u5bf9\u5b8c\u6574\u6b63\u5219\u3002")
    if "trusted_origins" in observation:
        warnings.append("\u89c2\u5bdf\u4fe1\u4efb\u6765\u6e90\u4ec5\u7528\u4e8e\u6d4f\u89c8\uff0c\u4e0d\u6388\u6743\u8f6c\u53d1\u51ed\u636e\u6216\u5b8c\u6574\u8fd0\u884c\u8bbf\u95ee\u3002")
    if "proxy" in observation:
        warnings.append("\u89c2\u5bdf\u4ee3\u7406\u4e0d\u6539\u53d8\u4fdd\u5b58\u8fd0\u884c\u8f66\u9053\u3002")
    if patch.get("enabled") is True:
        warnings.append("\u542f\u7528\u540e\u7ad9\u70b9\u53ef\u88ab\u5b9a\u65f6\u8c03\u5ea6\uff1b\u4ec5\u63a5\u53d7\u5f53\u524d\u914d\u7f6e\u7684\u540c\u4f1a\u8bdd\u5b8c\u6574\u6210\u529f\u6d4b\u8bd5\u3002")
    review = {"name": target["name"], "base_url": target["base_url"],
              "changes": changes, "warnings": warnings}
    if site:
        review["site_id"] = site.id
    if successful_run_id is not None:
        review["successful_run_id"] = successful_run_id
    public = {"kind": "authorization", "purpose": "site_config", "options": [], "fields": [],
              "multiple": False, "title": "\u786e\u8ba4\u7ad9\u70b9\u8bbe\u7f6e\u53d8\u66f4",
              "description": "\u4ec5\u6388\u6743\u4e0b\u5217\u7cbe\u786e\u53d8\u66f4\u3002\u5168\u5c40\u81ea\u52a8\u6388\u6743\u4e0d\u9002\u7528\uff0c\u62d2\u7edd\u6216\u8d85\u65f6\u4e0d\u4f1a\u4fee\u6539\u8bbe\u7f6e\u3002",
              "review": review}
    private = {"request": args, "binding": workflow_binding(site) if site else None,
               "current": current, "actual_patch": actual, "successful_run_id": successful_run_id,
               "provider_binding": provider}
    from app.services.assistant_retry import fingerprint

    return public, private, "site_config:" + fingerprint({"patch": patch, "observation": observation})


def apply_site_config(db, tools, data, args):
    request = args["request"]
    site = check_context(db, tools, data, request["patch"])
    if site and workflow_binding(site) != args["binding"]:
        raise WorkflowDenied("workflow_binding_changed")
    if "credential_provider_id" in request["patch"]:
        if credential_provider_binding(db, request["patch"]["credential_provider_id"]) != args.get("provider_binding"):
            raise WorkflowDenied("provider_binding_changed")
    if site:
        for name, value in args["current"].items():
            current = (json.loads(getattr(site, name + "_json")) if name in {"tags", "trusted_urls"}
                       else getattr(site, name))
            if current != value:
                raise WorkflowDenied("workflow_binding_changed")
    workflows = AssistantWorkflows(tools.manager, tools.owner, tools.session_id, data["target"])
    if action_for(db, workflows, request["operation_id"]) is not None:
        raise WorkflowDenied("site_config_operation_conflict")
    service = workflows._service(db)
    if request["patch"].get("enabled") is True:
        from app.services.assistant_run_guard import require_enable_success

        require_enable_success(db, tools.owner, tools.session_id, site, service, args["successful_run_id"])
    if args["actual_patch"]:
        service.patch_site_settings(site.id, args["actual_patch"],
                                    expected_revision=args["binding"]["revision"],
                                    expected_history_key=args["binding"]["history_key"], _commit=False)
        db.refresh(site)
        target = data["target"]
        target.update(name=site.name, run_lane_policy=site.lane_policy,
                      _workflow_binding=workflow_binding(site))
        if "credential_provider_id" in args["actual_patch"]:
            target.update(credential_mode="site", site_id=site.id)
        if target.get("site_id") == site.id:
            target["_credential_binding"] = workflow_binding(site)
    data["target"].update(deepcopy(request["observation"]))
    result = {"ok": True, "purpose": "site_config", "operation_id": request["operation_id"],
              "persisted": bool(args["actual_patch"]), "decision": "approve",
              "name": site.name if site else data["target"]["name"],
              "changed_fields": sorted(args["actual_patch"]),
              "target": tools.manager._public_target(data["target"])}
    if site:
        result.update(site_id=site.id, config_revision=site.config_revision, history_key=site.history_key)
    if args["successful_run_id"] is not None:
        result["successful_run_id"] = args["successful_run_id"]
    action, _ = workflows._action(db, "site_config", {"operation_id": request["operation_id"]})
    if site:
        workflows._identity(action, site)
    action.state = "complete"
    workflows._seal(action, {"request": request, "result": result,
                             "destination": destination_identity(db, data["target"])})
    return result


async def request_site_config(tools, arguments):
    from app.services.assistant import AssistantError
    from app.services.assistant_interactions import request_interaction

    try:
        request = UpdateSiteConfig.model_validate(arguments, strict=True)
        if unsafe_site_config(arguments, tools.config.get("api_key", "")):
            return {"ok": False, "code": "secret_input"}
        with tools.manager.session_factory() as db:
            row = tools.manager._owned(db, tools.owner, tools.session_id)
            data = tools.manager._decrypt(row)
            result = replay(db, tools, canonical(request), data)
            if result is not None:
                tools.target = deepcopy(data.get("target"))
                return result
        return await request_interaction(tools, {"purpose": "site_config", **canonical(request)})
    except ValidationError:
        return {"ok": False, "code": "invalid_arguments"}
    except (WorkflowDenied, AssistantError) as exc:
        return {"ok": False, "code": exc.code if isinstance(exc, AssistantError) else str(exc)}
    except (IntegrityError, OperationalError):
        return {"ok": False, "code": "site_config_conflict"}
    except WorkflowError:
        return {"ok": False, "code": "site_config_invalid"}
    except Exception:
        return {"ok": False, "code": "site_config_unavailable"}
