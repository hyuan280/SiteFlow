"""Host-built, exact, one-use approval for consuming saved workflow runs."""

from copy import deepcopy
from datetime import datetime
import hashlib
import time
from uuid import uuid4

from sqlalchemy import select

from app.features.catalog import FEATURES
from app.models.assistant_workflow import AssistantWorkflowAction
from app.models.workflow import SiteResignRecord
from app.services.assistant_run_guard import _fingerprint
from app.services.assistant_workflows import AssistantWorkflows, WorkflowDenied, bound_workflow, workflow_binding
from app.services.resign import ResignService


# Reviewed plan builders only. Whether approval is required comes from FEATURES.category.
CONSUMING_FEATURES = frozenset({
    "post.buy_card", "post.resign", "post.resign_click", "post.resign_dialog",
})


def build_plan(db, service, site):
    steps = []
    for step in service.resolve_site(site.id).steps:
        if step.feature_type not in FEATURES:
            raise WorkflowDenied("workflow_feature_denied")
        if step.enabled and FEATURES[step.feature_type].category == "post":
            # This set lists reviewed plan builders, not permission classification.
            if step.feature_type not in CONSUMING_FEATURES:
                raise WorkflowDenied("consumption_feature_denied")
            steps.append(step)
    if not steps:
        return None
    resign = ResignService(db, service.settings)
    today = datetime.now(resign.timezone).date().isoformat()
    days = site.resign_max_days if site.support_resign else 0
    dates = resign.pending_dates(site.id, days) if days else []
    if datetime.now(resign.timezone).date().isoformat() != today:
        raise WorkflowDenied("consumption_plan_changed")
    unresolved = list(db.scalars(select(SiteResignRecord.resign_date).where(
        SiteResignRecord.site_id == site.id, SiteResignRecord.status == "unknown",
    ).order_by(SiteResignRecord.resign_date)))
    if unresolved:
        raise WorkflowDenied("workflow_outcome_uncertain")
    review_steps = []
    for step in steps:
        item = {"feature_type": step.feature_type, "step_key": step.step_key,
                "label": {"post.buy_card": "\u8d2d\u4e70\u8865\u7b7e\u5361", "post.resign": "\u5386\u53f2\u65e5\u5386\u8865\u7b7e",
                          "post.resign_click": "\u70b9\u51fb\u8865\u7b7e", "post.resign_dialog": "\u5f39\u7a97\u8865\u7b7e"}[step.feature_type]}
        if step.feature_type == "post.buy_card":
            quantity, price = step.config.get("purchase_quantity", 0), step.config.get("max_price")
            if type(quantity) is not int or not 0 <= quantity <= 365:
                raise WorkflowDenied("consumption_quantity_required")
            if type(price) is not int or price <= 0:
                raise WorkflowDenied("consumption_max_price_required")
            maximum = quantity or len(dates)
            item.update(purchase_quantity=quantity, maximum_quantity=maximum,
                        max_price=price, maximum_spend=maximum * price)
        review_steps.append(item)
    fingerprint, _ = _fingerprint(service, site)
    return {"binding": workflow_binding(site), "fingerprint": fingerprint,
            "unresolved_dates": unresolved,
            "review": {"name": site.name, "base_url": site.base_url, "site_id": site.id,
                       "steps": review_steps, "dates": dates, "date_count": len(dates),
                       "timezone": str(resign.timezone),
                       "today": today,
                       "warnings": [
                            "\u6bcf\u4e2a post \u6b65\u9aa4\u5747\u9700\u5355\u72ec\u786e\u8ba4\uff0c\u5168\u90e8\u540c\u610f\u540e\u624d\u4f1a\u5b8c\u6574\u8fd0\u884c\u4e00\u6b21\uff1b\u4e0d\u4f1a\u81ea\u52a8\u91cd\u8bd5\u3002"
                            "\u672c\u6b65\u9aa4\u7684\u6388\u6743\u8986\u76d6\u6240\u5217\u5168\u90e8\u65e5\u671f\u548c\u6570\u91cf\u4e0a\u9650\uff0c\u4e0d\u4f1a\u4e3a\u6bcf\u4e2a\u65e5\u671f\u6216\u5185\u90e8\u70b9\u51fb\u518d\u6b21\u5f39\u7a97\u3002",
                           "\u6240\u5217\u4e3a\u914d\u7f6e\u4e0a\u9650\uff0c\u4e0d\u662f\u7f51\u7ad9\u5b9e\u65f6\u62a5\u4ef7\uff1b\u81ea\u52a8\u8d2d\u5361\u6570\u91cf\u6309\u5f85\u8865\u7b7e\u5929\u6570\u51cf\u5e93\u5b58\u8ba1\u7b97\u3002",
                           "\u6761\u4ef6\u6b65\u9aa4\u4e5f\u5217\u5165\u5ba1\u9605\uff1b\u666e\u901a\u70b9\u51fb\u7684\u7f51\u7ad9\u4e1a\u52a1\u6548\u679c\u65e0\u6cd5\u4ec5\u51ed\u9009\u62e9\u5668\u8bc6\u522b\u3002",
                       ]}}


def _pending_plan(db, tools, plan_id, step_key):
    if not tools or getattr(tools, "_consumption_request", None) != (plan_id, step_key):
        raise WorkflowDenied("consumption_internal_only")
    workflows = AssistantWorkflows(tools.manager, tools.owner, tools.session_id, tools.target)
    workflows._context(db, write=True)
    action = db.get(AssistantWorkflowAction, plan_id)
    if (action is None or action.kind != "consumption" or action.owner != tools.owner
            or action.session_id != tools.session_id or action.state != "reserved"):
        raise WorkflowDenied("consumption_approval_required")
    saved = workflows._open(action)
    if time.time() >= saved["expires_epoch"]:
        raise WorkflowDenied("consumption_approval_expired")
    invocation = {"turn_id": tools.turn["id"], **tools.call_context}
    if saved["invocation"] != invocation:
        raise WorkflowDenied("consumption_approval_required")
    site = bound_workflow(db, tools.target)
    if saved["plan"] != build_plan(db, workflows._service(db), site):
        raise WorkflowDenied("consumption_plan_changed")
    keys = [step["step_key"] for step in saved["plan"]["review"]["steps"]]
    approved = saved["approved_steps"]
    if (approved != keys[:len(approved)] or len(approved) >= len(keys)
            or keys[len(approved)] != step_key):
        raise WorkflowDenied("consumption_approval_required")
    return workflows, action, saved


def prepare_consumption(db, tools, request, data):
    workflows, action, saved = _pending_plan(db, tools, request.run_plan_id, request.step_key)
    from app.services.assistant_interactions import INTERACTION_TIMEOUT

    # Each dialog receives its own full review window, not the first dialog's remainder.
    saved["expires_epoch"] = time.time() + INTERACTION_TIMEOUT
    workflows._seal(action, saved)
    review = deepcopy(saved["plan"]["review"])
    index = len(saved["approved_steps"])
    count = len(review["steps"])
    review["steps"] = [review["steps"][index]]
    return ({"kind": "authorization", "purpose": "consumption", "options": [],
             "fields": [], "multiple": False, "review": review,
             "title": f"\u786e\u8ba4\u672c\u6b21\u6d88\u8d39\u6b65\u9aa4\uff08{index + 1}/{count}\uff09",
             "description": "\u8bf7\u6838\u5bf9\u5168\u90e8\u65e5\u671f\u3001\u6570\u91cf\u548c\u8d39\u7528\u4e0a\u9650\u3002\u62d2\u7edd\u6216\u8d85\u65f6\u4e0d\u4f1a\u8fd0\u884c\uff0c\u4e0d\u4f1a\u4fee\u6539\u5168\u5c40\u6388\u6743\u3002"},
            {"run_plan_id": action.id, "step_key": request.step_key},
            "consumption:" + action.request_fingerprint + ":" + request.step_key)


def apply_consumption(db, tools, data, args):
    workflows, action, saved = _pending_plan(db, tools, args["run_plan_id"], args["step_key"])
    saved["approved_steps"].append(args["step_key"])
    # A timely approval gets a bounded next-dialog/dispatch window, independent of its popup deadline.
    saved["expires_epoch"] = time.time() + 60
    if len(saved["approved_steps"]) == len(saved["plan"]["review"]["steps"]):
        permit = str(uuid4())
        saved["permit_digest"] = hashlib.sha256(permit.encode()).hexdigest()
        action.state = "approved"
        # This capability never enters interaction results or public session data.
        tools._consumption_permit = (action.id, permit)
    workflows._seal(action, saved)


def consume_permit(db, workflows, site, permit):
    plan = build_plan(db, workflows._service(db), site)
    if plan is None:
        return None
    if not isinstance(permit, tuple) or len(permit) != 2:
        raise WorkflowDenied("consumption_approval_required")
    action = db.get(AssistantWorkflowAction, permit[0])
    if (action is None or action.kind != "consumption" or action.state != "approved"
            or action.owner != workflows.owner or action.session_id != workflows.session_id):
        raise WorkflowDenied("consumption_approval_required")
    saved = workflows._open(action)
    if (saved["permit_digest"] != hashlib.sha256(permit[1].encode()).hexdigest()
            or saved["plan"] != plan or time.time() >= saved["expires_epoch"]
            or saved["approved_steps"] != [step["step_key"] for step in plan["review"]["steps"]]):
        raise WorkflowDenied("consumption_plan_changed")
    action.state = "used"
    return plan


async def run_with_consumption_approval(tools, workflows):
    from app.services.assistant_interactions import INTERACTION_TIMEOUT, request_interaction

    try:
        with tools.manager.session_factory() as db:
            _, data, target = workflows._context(db, write=True)
            site = bound_workflow(db, target)
            # Match run_site's durable identity before requesting any new approval.
            _, replay = workflows._action(db, "run", {
                "binding": workflow_binding(site), "proposal": data.get("_workflow_proposal_id")},
                data.get("_workflow_proposal_id"), reserve=False)
            plan = None if replay else build_plan(db, workflows._service(db), site)
            identity = workflow_binding(site)
        if plan is not None:
            with tools.manager.session_factory() as db:
                workflows._context(db, write=True)
                invocation = {"turn_id": tools.turn["id"], **tools.call_context}
                action, replay = workflows._action(db, "consumption", {"invocation": invocation})
                if replay:
                    return {"ok": False, "code": "consumption_already_requested"}
                action.site_id, action.site_history_key, action.site_revision = (
                    identity["id"], identity["history_key"], identity["revision"])
                workflows._seal(action, {"plan": plan, "invocation": invocation, "approved_steps": [],
                                          "expires_epoch": time.time() + INTERACTION_TIMEOUT})
                db.commit()
            for step in plan["review"]["steps"]:
                tools._consumption_request = (action.id, step["step_key"])
                result = await request_interaction(tools, {
                    "purpose": "consumption", "run_plan_id": action.id, "step_key": step["step_key"]})
                if not result.get("ok"):
                    return result
        return await workflows.run_site(consumption_permit=getattr(tools, "_consumption_permit", None))
    except WorkflowDenied as exc:
        return {"ok": False, "code": str(exc)}
    except Exception:
        return {"ok": False, "code": "workflow_unavailable"}
    finally:
        tools._consumption_request = None
        tools._consumption_permit = None
