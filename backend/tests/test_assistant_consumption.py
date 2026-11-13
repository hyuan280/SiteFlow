from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.features.catalog import FEATURES
from app.models.assistant import AssistantSession
from app.models.assistant_workflow import AssistantWorkflowAction
from app.models.execution import Run, SiteJob
from app.models.setting import AppSetting
from app.models.workflow import Site, SiteResignRecord, Template, WorkflowStep
from app.schemas.assistant import ConsumptionInteraction, InteractionResponse
from app.services.assistant_consumption import apply_consumption, build_plan, prepare_consumption
from app.services.assistant_interactions import respond_interaction
from app.services.assistant_run_guard import consumption_plan, validate_guard
from app.services.assistant_workflows import AssistantWorkflows, WorkflowDenied
from app.services.config_ai_generation import _json
from app.services.resign import ResignService
from tests.test_assistant_workflows import (  # noqa: F401
    OWNER, call, configure, context, make_site,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def setup(context, *, config=None, enabled=True, feature="post.buy_card"):
    site = make_site(context, support_resign=True, resign_max_days=365, steps=[{
        "step_key": "consume", "feature_type": feature, "enabled": enabled,
        "config": {"purchase_quantity": 2, "max_price": 10} if config is None and feature == "post.buy_card"
        else config or {},
    }])
    return configure(context, workflow_site_id=site.id), site.id


def session_data(tools):
    with tools.manager.session_factory() as db:
        return tools.manager._decrypt(db.get(AssistantSession, tools.session_id))


async def pending(tools, task):
    for _ in range(200):
        item = session_data(tools).get("_pending_interaction")
        if item:
            return item
        if task.done():
            pytest.fail(str(task.result()))
        await asyncio.sleep(.005)
    pytest.fail("No consumption review")


async def approve(tools, *, decision="approve", before=None):
    task = asyncio.create_task(tools.dispatch("run_site", {}))
    item = await pending(tools, task)
    assert item["purpose"] == "consumption"
    tools.manager.executor.submit.assert_not_called()
    if before:
        before()
    await respond_interaction(tools.manager, tools.owner, tools.session_id,
                              item["id"], InteractionResponse(decision=decision))
    return await task, item


def test_default_workflow_consent_never_bypasses_purchase_review_and_replay(context):
    tools, site_id = setup(context)
    before = deepcopy(session_data(tools)["target"])
    result, item = asyncio.run(approve(tools))
    assert result["ok"], result
    assert item["review"]["steps"] == [{
        "feature_type": "post.buy_card", "step_key": "consume", "label": "\u8d2d\u4e70\u8865\u7b7e\u5361",
        "purchase_quantity": 2, "maximum_quantity": 2, "max_price": 10, "maximum_spend": 20,
    }]
    assert session_data(tools)["target"] == before
    assert call(tools, "run_site")["replayed"]
    tools.manager.executor.submit.assert_called_once()
    assert "run_plan_id" not in _json(result) and "permit" not in _json(result)
    with tools.manager.session_factory() as db:
        plan = consumption_plan(db, result["run_id"], tools.manager.crypto)
        assert plan["review"] == item["review"]
        site = db.get(Site, site_id)
        assert validate_guard(db, result["run_id"], site, tools.manager.settings, tools.manager.crypto)
        job = db.scalar(select(SiteJob).where(SiteJob.run_id == result["run_id"]))
        job.attempt = 2
        db.flush()
        assert not validate_guard(db, result["run_id"], site, tools.manager.settings, tools.manager.crypto)
        grant = db.scalar(select(AssistantWorkflowAction).where(AssistantWorkflowAction.kind == "consumption"))
        assert grant.state == "used" and grant.ciphertext


@pytest.mark.parametrize("decision", ["deny", "timeout"])
def test_deny_timeout_never_calls_executor(context, monkeypatch, decision):
    tools, _ = setup(context)
    if decision == "timeout":
        monkeypatch.setattr("app.services.assistant_interactions.INTERACTION_TIMEOUT", .03)
        result = call(tools, "run_site")
    else:
        result, _ = asyncio.run(approve(tools, decision="deny"))
    assert result["code"] == "interaction_" + ("timed_out" if decision == "timeout" else "denied")
    assert not call(tools, "run_site")["ok"]
    tools.manager.executor.submit.assert_not_called()
    with tools.manager.session_factory() as db:
        assert db.scalar(select(Run)) is None


@pytest.mark.parametrize("feature", ["post.resign", "post.resign_click", "post.resign_dialog"])
def test_all_resign_features_require_review_even_with_no_pending_dates(context, feature):
    tools, site_id = setup(context, feature=feature)
    result, item = asyncio.run(approve(tools))
    assert result["ok"], result
    assert item["review"]["date_count"] == 0
    assert item["review"]["steps"][0]["feature_type"] == feature


def test_direct_run_and_model_requested_purpose_cannot_grant_authority(context):
    tools, _ = setup(context)
    workflows = AssistantWorkflows(tools.manager, tools.owner, tools.session_id, tools.target)
    with pytest.raises(WorkflowDenied, match="consumption_approval_required"):
        asyncio.run(workflows.run_site())
    assert call(tools, "request_user_interaction", {"request": {
        "purpose": "consumption", "run_plan_id": "x" * 36,
        "step_key": "consume"}})["code"] == "invalid_arguments"
    tools.manager.executor.submit.assert_not_called()


@pytest.mark.parametrize("change", ["config", "revision", "dates", "today", "unresolved"])
def test_changes_while_review_pending_invalidate_approval(context, monkeypatch, change):
    tools, site_id = setup(context)
    original = ResignService.pending_dates

    def drift():
        if change == "dates":
            monkeypatch.setattr(ResignService, "pending_dates", lambda *a: ["2020-01-01"])
        elif change == "today":
            import app.services.assistant_consumption as module

            clock = SimpleNamespace(now=lambda tz: datetime.now(tz) + timedelta(days=1))
            monkeypatch.setattr(module, "datetime", clock)
        else:
            with tools.manager.session_factory() as db:
                if change == "revision":
                    db.get(Site, site_id).config_revision += 1
                elif change == "config":
                    db.scalar(select(WorkflowStep).where(WorkflowStep.owner_id == site_id)).config_json = _json({
                        "purchase_quantity": 3, "max_price": 10})
                else:
                    db.add(SiteResignRecord(site_id=site_id, resign_date="2020-01-01", status="unknown"))
                db.commit()

    result, _ = asyncio.run(approve(tools, before=drift))
    assert not result["ok"], result
    tools.manager.executor.submit.assert_not_called()
    monkeypatch.setattr(ResignService, "pending_dates", original)


def test_auto_quantity_review_is_bounded_by_frozen_pending_not_unknown_stock(context, monkeypatch):
    tools, site_id = setup(context, config={"purchase_quantity": 0, "max_price": 7})
    dates = ["2020-01-01", "2020-01-02"]
    monkeypatch.setattr(ResignService, "pending_dates", lambda *a: dates)
    result, item = asyncio.run(approve(tools))
    assert result["ok"]
    review = item["review"]
    assert review["dates"] == dates and review["date_count"] == 2
    assert review["steps"][0]["maximum_quantity"] == 2
    assert review["steps"][0]["maximum_spend"] == 14
    with tools.manager.session_factory() as db:
        plan = build_plan(db, AssistantWorkflows(tools.manager, tools.owner, tools.session_id,
                                               tools.target)._service(db), db.get(Site, site_id))
        assert plan["review"] == review


def test_disabled_post_needs_no_consumption_review(context):
    tools, _ = setup(context, enabled=False)
    assert call(tools, "run_site")["ok"]
    assert not session_data(tools).get("_pending_interaction")


@pytest.mark.parametrize("feature", ["action.wait", "flow.open"])
def test_registry_category_not_feature_name_or_resolved_label_controls_policy(context, monkeypatch, feature):
    tools, _ = setup(context, feature=feature)
    monkeypatch.setitem(FEATURES, feature, replace(FEATURES[feature], category="post"))
    assert call(tools, "run_site") == {"ok": False, "code": "consumption_feature_denied"}
    workflows = AssistantWorkflows(tools.manager, tools.owner, tools.session_id, tools.target)
    with pytest.raises(WorkflowDenied, match="consumption_feature_denied"):
        asyncio.run(workflows.run_site())
    tools.manager.executor.submit.assert_not_called()
    with tools.manager.session_factory() as db:
        assert db.scalar(select(Run)) is None


def test_new_registered_post_without_reviewed_plan_fails_closed(context, monkeypatch):
    feature = "action.new_consumption"
    monkeypatch.setitem(FEATURES, feature, replace(FEATURES["action.wait"], key=feature, category="post"))
    tools, _ = setup(context, feature=feature)
    assert call(tools, "run_site") == {"ok": False, "code": "consumption_feature_denied"}
    tools.manager.executor.submit.assert_not_called()


def test_post_category_cannot_be_hidden_by_stored_label(context):
    tools, site_id = setup(context)
    with tools.manager.session_factory() as db:
        db.scalar(select(WorkflowStep).where(WorkflowStep.owner_id == site_id)).category = "action"
        db.commit()
    result, item = asyncio.run(approve(tools))
    assert result["ok"] and item["review"]["steps"][0]["feature_type"] == "post.buy_card"


def test_guard_requires_plan_when_current_registry_reclassifies_builtin_as_post(context, monkeypatch):
    tools, site_id = setup(context, feature="action.wait")
    result = call(tools, "run_site")
    assert result["ok"]
    monkeypatch.setitem(FEATURES, "action.wait", replace(FEATURES["action.wait"], category="post"))
    with tools.manager.session_factory() as db:
        for check in (True, False):
            assert not validate_guard(db, result["run_id"], db.get(Site, site_id),
                                      tools.manager.settings, tools.manager.crypto,
                                      check_consumption=check)


def test_nonbuiltin_feature_cannot_enter_consumption_plan():
    service = SimpleNamespace(resolve_site=lambda _: SimpleNamespace(steps=[SimpleNamespace(
        enabled=True, feature_type="plugin.custom.consume", category="post")]))
    with pytest.raises(WorkflowDenied, match="workflow_feature_denied"):
        build_plan(None, service, SimpleNamespace(id=1))


@pytest.mark.parametrize("second_decision", ["approve", "deny", "timeout"])
def test_each_post_step_needs_fresh_popup_before_any_execution(context, monkeypatch, second_decision):
    site = make_site(context, support_resign=True, steps=[{
        "step_key": key, "feature_type": "post.resign_click", "config": {},
    } for key in ("first", "second")])
    tools = configure(context, workflow_site_id=site.id)
    tools.config["_assistant_settings"] = {"auto_authorize_" + capability: True
                                         for capability in ("workflow", "execution", "screenshot")}
    dates = ["2020-01-01", "2020-01-02"]
    monkeypatch.setattr(ResignService, "pending_dates", lambda *a: dates)
    if second_decision == "timeout":
        monkeypatch.setattr("app.services.assistant_interactions.INTERACTION_TIMEOUT", .3)

    async def exercise():
        task = asyncio.create_task(tools.dispatch("run_site", {}))
        first = await pending(tools, task)
        assert [step["step_key"] for step in first["review"]["steps"]] == ["first"]
        assert first["review"]["dates"] == dates
        assert 1 <= len(first["review"]["warnings"]) <= 3
        await respond_interaction(tools.manager, tools.owner, tools.session_id,
                                  first["id"], InteractionResponse(decision="approve"))
        second = await pending(tools, task)
        assert second["id"] != first["id"]
        assert [step["step_key"] for step in second["review"]["steps"]] == ["second"]
        assert second["review"]["dates"] == dates
        tools.manager.executor.submit.assert_not_called()
        assert getattr(tools, "_consumption_permit", None) is None
        with tools.manager.session_factory() as db:
            action = db.scalar(select(AssistantWorkflowAction).where(
                AssistantWorkflowAction.kind == "consumption"))
            workflows = AssistantWorkflows(tools.manager, tools.owner, tools.session_id, tools.target)
            saved = workflows._open(action)
            assert action.state == "reserved" and saved["approved_steps"] == ["first"]
            assert "permit_digest" not in saved
            assert db.scalar(select(Run)) is None
            # A completed step, a substituted key and another tool invocation cannot grant authority.
            for key in ("first", "unreviewed"):
                tools._consumption_request = (action.id, key)
                with pytest.raises(WorkflowDenied):
                    apply_consumption(db, tools, session_data(tools), {
                        "run_plan_id": action.id, "step_key": key})
            tools._consumption_request = (action.id, "second")
            previous = tools.call_context
            tools.call_context = {"tool_call_id": "different-invocation"}
            with pytest.raises(WorkflowDenied, match="consumption_approval_required"):
                prepare_consumption(db, tools, ConsumptionInteraction(
                    purpose="consumption", run_plan_id=action.id, step_key="second"), {})
            tools.call_context = previous
        # Replaying the first approval cannot answer the second popup.
        await respond_interaction(tools.manager, tools.owner, tools.session_id,
                                  first["id"], InteractionResponse(decision="approve"))
        assert session_data(tools)["_pending_interaction"]["id"] == second["id"]
        if second_decision != "timeout":
            await respond_interaction(tools.manager, tools.owner, tools.session_id,
                                      second["id"], InteractionResponse(decision=second_decision))
        return await task, [first, second]

    result, dialogs = asyncio.run(exercise())
    if second_decision == "approve":
        assert result["ok"], result
        tools.manager.executor.submit.assert_called_once()
        assert call(tools, "run_site")["replayed"]
        tools.manager.executor.submit.assert_called_once()
        with tools.manager.session_factory() as db:
            plan = consumption_plan(db, result["run_id"], tools.manager.crypto)
            assert plan["review"]["steps"] == [dialog["review"]["steps"][0] for dialog in dialogs]
            action = db.scalar(select(AssistantWorkflowAction).where(
                AssistantWorkflowAction.kind == "consumption"))
            workflows = AssistantWorkflows(tools.manager, tools.owner, tools.session_id, tools.target)
            assert action.state == "used" and workflows._open(action)["approved_steps"] == ["first", "second"]
    else:
        assert result["code"] == "interaction_" + ("timed_out" if second_decision == "timeout" else "denied")
        tools.manager.executor.submit.assert_not_called()
        with tools.manager.session_factory() as db:
            assert db.scalar(select(Run)) is None


def test_each_step_review_gets_full_timeout_and_pauses_active_budget(context, monkeypatch):
    site = make_site(context, steps=[{
        "step_key": key, "feature_type": "post.resign", "config": {},
    } for key in ("first", "second")])
    tools = configure(context, workflow_site_id=site.id)
    real_time = time.time
    offset = [0]
    monkeypatch.setattr(time, "time", lambda: real_time() + offset[0])

    async def exercise():
        tools.turn["deadline"] = asyncio.get_running_loop().time() + .1
        task = asyncio.create_task(tools.dispatch("run_site", {}))
        first = await pending(tools, task)
        await asyncio.sleep(.15)
        offset[0] = 100
        await respond_interaction(tools.manager, tools.owner, tools.session_id,
                                  first["id"], InteractionResponse(decision="approve"))
        second = await pending(tools, task)
        assert second["expires_epoch"] >= first["expires_epoch"] + 99
        offset[0] = 200
        await respond_interaction(tools.manager, tools.owner, tools.session_id,
                                  second["id"], InteractionResponse(decision="approve"))
        return await task

    result = asyncio.run(exercise())
    assert result["ok"], result
    tools.manager.executor.submit.assert_called_once()


@pytest.mark.parametrize("transition", ["next_popup", "dispatch"])
@pytest.mark.parametrize("delay", [2, 60])
def test_timely_approval_has_bounded_transition_grace(context, monkeypatch, transition, delay):
    from app.services import assistant_interactions

    keys = ["first", "second"] if transition == "next_popup" else ["first"]
    site = make_site(context, steps=[{
        "step_key": key, "feature_type": "post.resign", "config": {},
    } for key in keys])
    tools = configure(context, workflow_site_id=site.id)
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    request = assistant_interactions.request_interaction

    async def delayed_transition(*args, **kwargs):
        result = await request(*args, **kwargs)
        clock[0] += delay
        return result

    monkeypatch.setattr(assistant_interactions, "request_interaction", delayed_transition)

    async def exercise():
        task = asyncio.create_task(tools.dispatch("run_site", {}))
        first = await pending(tools, task)
        # The approval is timely, but the next host operation crosses the old popup deadline.
        clock[0] = first["expires_epoch"] - 1
        await respond_interaction(tools.manager, tools.owner, tools.session_id,
                                  first["id"], InteractionResponse(decision="approve"))
        if transition == "next_popup" and delay < 60:
            second = await pending(tools, task)
            assert second["review"]["steps"][0]["step_key"] == "second"
            assert second["expires_epoch"] == clock[0] + assistant_interactions.INTERACTION_TIMEOUT
            clock[0] = second["expires_epoch"] - 1
            await respond_interaction(tools.manager, tools.owner, tools.session_id,
                                      second["id"], InteractionResponse(decision="approve"))
        return await task

    result = asyncio.run(exercise())
    if delay < 60:
        assert result["ok"], result
        tools.manager.executor.submit.assert_called_once()
    else:
        assert result == {"ok": False, "code": (
            "consumption_approval_expired" if transition == "next_popup" else "consumption_plan_changed")}
        tools.manager.executor.submit.assert_not_called()
        with tools.manager.session_factory() as db:
            assert db.scalar(select(Run)) is None


def test_inherited_purchase_is_reviewed_not_silently_denied(context):
    tools, site_id = setup(context)
    with tools.manager.session_factory() as db:
        template = Template(name="Purchase parent")
        db.add(template)
        db.flush()
        step = db.scalar(select(WorkflowStep).where(WorkflowStep.owner_id == site_id))
        step.owner_type, step.owner_id = "template", template.id
        db.get(Site, site_id).template_id = template.id
        db.commit()
    result, item = asyncio.run(approve(tools))
    assert result["ok"]
    assert item["review"]["steps"][0]["feature_type"] == "post.buy_card"


def test_full_365_dates_are_not_truncated(context, monkeypatch):
    tools, _ = setup(context, config={"purchase_quantity": 0, "max_price": 7})
    dates = [(datetime(2020, 1, 1) + timedelta(days=i)).date().isoformat() for i in range(365)]
    monkeypatch.setattr(ResignService, "pending_dates", lambda *a: dates)
    result, item = asyncio.run(approve(tools))
    assert result["ok"]
    assert item["review"]["dates"] == dates
    assert item["review"]["date_count"] == 365
    assert item["review"]["steps"][0]["maximum_spend"] == 365 * 7


@pytest.mark.parametrize("worker_status", ["restart", "fallback_interaction"])
def test_real_executor_disables_all_new_attempts_for_consumption(context, worker_status):
    from app.services.executor import ExecutionManager

    tools, site_id = setup(context)
    result, _ = asyncio.run(approve(tools))
    manager = ExecutionManager(tools.manager.session_factory, tools.manager.settings, tools.manager.crypto)
    manager._post_execute = AsyncMock(return_value={"status": worker_status, "steps": []})
    with tools.manager.session_factory() as db:
        job_id = db.scalar(select(SiteJob.id).where(SiteJob.run_id == result["run_id"]))
    assert asyncio.run(manager._execute_job(result["run_id"], job_id)) is None
    payload = manager._post_execute.call_args.args[0]
    assert payload["assistant_consumption_plan"]["review"]["steps"][0]["maximum_spend"] == 20
    assert payload["resign_pending_dates"] == []
    with tools.manager.session_factory() as db:
        job = db.get(SiteJob, job_id)
        assert job.status == "unknown" and job.attempt == 1
        assert job.lane == "general"
    manager._post_execute.assert_awaited_once()


@pytest.mark.parametrize("change", ["dates", "timezone", "window", "stored_policy", "shadowed_policy"])
def test_guard_rejects_drift_as_unknown_before_worker_dispatch(context, monkeypatch, change):
    from app.services.executor import ExecutionManager

    tools, site_id = setup(context)
    result, _ = asyncio.run(approve(tools))
    if change == "dates":
        monkeypatch.setattr(ResignService, "pending_dates", lambda *a: ["2020-01-01"])
    elif change == "timezone":
        tools.manager.settings.scheduler_timezone = "America/Los_Angeles"
    else:
        with tools.manager.session_factory() as db:
            if change == "window":
                db.get(Site, site_id).resign_max_days = 3
            else:
                defaults = {"purchase_interval_seconds": 1} if change == "stored_policy" else {"max_price": 999}
                db.add(AppSetting(key="feature.post.buy_card", value_json=_json({
                    "defaults": defaults, "required_fields": []})))
            db.commit()
    manager = ExecutionManager(tools.manager.session_factory, tools.manager.settings, tools.manager.crypto)
    manager._post_execute = AsyncMock()
    with tools.manager.session_factory() as db:
        job_id = db.scalar(select(SiteJob.id).where(SiteJob.run_id == result["run_id"]))
    asyncio.run(manager._execute_job(result["run_id"], job_id))
    manager._post_execute.assert_not_awaited()
    with tools.manager.session_factory() as db:
        job = db.get(SiteJob, job_id)
        assert job.status == "unknown"
        assert job.attempt == 1
        assert json.loads(job.result_json) == {
            "reason": "assistant_configuration_changed", "outcome_uncertain": True}


def test_policy_drift_during_payload_build_is_unknown_and_not_dispatched(context, monkeypatch):
    from app.services.credentials import CredentialService
    from app.services.executor import ExecutionManager

    tools, _ = setup(context)
    result, _ = asyncio.run(approve(tools))

    def drift(*_args):
        with tools.manager.session_factory() as db:
            db.add(AppSetting(key="feature.post.buy_card", value_json=_json({
                "defaults": {"max_price": 999}, "required_fields": []})))
            db.commit()
        return None

    monkeypatch.setattr(CredentialService, "resolve_cookies", drift)
    manager = ExecutionManager(tools.manager.session_factory, tools.manager.settings, tools.manager.crypto)
    manager._post_execute = AsyncMock()
    with tools.manager.session_factory() as db:
        job_id = db.scalar(select(SiteJob.id).where(SiteJob.run_id == result["run_id"]))
    asyncio.run(manager._execute_job(result["run_id"], job_id))
    manager._post_execute.assert_not_awaited()
    with tools.manager.session_factory() as db:
        assert db.get(SiteJob, job_id).status == "unknown"


@pytest.mark.parametrize("zone", ["America/Los_Angeles", "Asia/Shanghai", "invalid/timezone"])
@pytest.mark.parametrize("days", [1, 3, 30, 365])
def test_plan_pending_uses_real_history_timezone_and_maximum_window(context, monkeypatch, zone, days):
    import app.services.assistant_consumption as consumption
    import app.services.resign as resign_module

    fixed = datetime(2026, 11, 9, 0, 30, tzinfo=timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz)

    monkeypatch.setattr(consumption, "datetime", Clock)
    monkeypatch.setattr(resign_module, "datetime", Clock)
    tools, site_id = setup(context, config={"purchase_quantity": 0, "max_price": 7})
    tools.manager.settings.scheduler_timezone = zone
    with tools.manager.session_factory() as db:
        site = db.get(Site, site_id)
        site.resign_max_days = days
        resign = ResignService(db, tools.manager.settings)
        today = fixed.astimezone(resign.timezone).date()
        history = Run(created_by="fixture", status="fail")
        db.add(history)
        db.flush()
        for offset, status in [(400, "fail"), (2, "success")]:
            db.add(SiteJob(run_id=history.id, site_id=site_id, site_name=site.name, lane="general",
                           status=status, finished_at=resign._start_utc(today - timedelta(days=offset))))
        db.add(SiteResignRecord(site_id=site_id, resign_date=(today - timedelta(days=3)).isoformat(),
                               status="success"))
        db.flush()
        service = AssistantWorkflows(tools.manager, tools.owner, tools.session_id, tools.target)._service(db)
        plan = build_plan(db, service, site)
        expected = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, 0, -1)
                    if i not in {2, 3}]
        assert plan["review"]["dates"] == expected
        assert plan["review"]["date_count"] == len(expected)
        assert plan["review"]["today"] == today.isoformat()
        assert plan["review"]["timezone"] == ("Asia/Shanghai" if zone == "invalid/timezone" else zone)
        assert plan["review"]["steps"][0]["maximum_quantity"] == len(expected)
        assert plan["review"]["steps"][0]["maximum_spend"] == len(expected) * 7


def test_guard_completed_proof_allows_expected_date_changes(context, monkeypatch):
    tools, site_id = setup(context, feature="post.resign")
    monkeypatch.setattr(ResignService, "pending_dates", lambda *a: ["2020-01-01"])
    result, _ = asyncio.run(approve(tools))
    monkeypatch.setattr(ResignService, "pending_dates", lambda *a: [])
    with tools.manager.session_factory() as db:
        site = db.get(Site, site_id)
        assert not validate_guard(db, result["run_id"], site, tools.manager.settings, tools.manager.crypto)
        assert validate_guard(db, result["run_id"], site, tools.manager.settings, tools.manager.crypto,
                              check_consumption=False)


@pytest.mark.parametrize("feature", ["post.resign_click", "post.resign_dialog"])
def test_approved_worker_keeps_real_partial_checkpoint_on_exception(context, monkeypatch, tmp_path, feature):
    from worker import executor
    from tests.test_worker_navigation import _JumpLane, _JumpManager, _JumpPage

    tools, site_id = setup(context, feature=feature)
    with tools.manager.session_factory() as db:
        workflows = AssistantWorkflows(tools.manager, tools.owner, tools.session_id, tools.target)
        service = workflows._service(db)
        plan = build_plan(db, service, db.get(Site, site_id))
        steps = [step.model_dump(mode="json") for step in service.resolve_site(site_id).steps]
    state = {"pending": [], "submit_attempted": True, "unconfirmed": [{"date": "2020-01-01"}]}

    async def run_step(_page, _step, *_args, resign_progress=None, **_kwargs):
        await resign_progress({"resign": state})
        raise RuntimeError("after durable prepare")

    monkeypatch.setattr(executor, "_run_step", run_step)
    monkeypatch.setattr(executor, "ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(executor.JobArtifacts, "capture_failure", AsyncMock())
    lane = _JumpLane(_JumpPage())
    lane.session = SimpleNamespace(disconnected=False, desktop=None)
    lane.in_use = 1
    manager = _JumpManager(lane)
    payload = {"job_id": 1, "assistant_run": True, "assistant_consumption_plan": plan,
               "steps": steps, "attempt": 1, "resign_pending_dates": [],
               "resign_today": plan["review"]["today"]}
    result = asyncio.run(executor.execute_job(manager, payload))
    assert result["status"] == "unknown"
    assert result["steps"][0]["data"]["resign"] == state
    assert manager.released


def test_legacy_resign_exception_stops_and_retains_unknown_date(context, monkeypatch):
    from worker import executor
    from tests.test_worker_resign import FakePage

    page = FakePage()
    page.missed = {"2026-10-01", "2026-10-02"}
    original = page.evaluate

    async def evaluate(script, arg=None):
        if "dateParam" in script:
            raise RuntimeError("response lost after dispatch")
        return await original(script, arg)

    page.evaluate = evaluate
    monkeypatch.setattr(executor.asyncio, "sleep", AsyncMock())
    status, _, data = asyncio.run(executor._run_resign(
        page, {"attendance_url": "https://pt.example.com/attendance.php"},
        sorted(page.missed), lambda *a: None))
    assert status == "waiting_human"
    assert data["outcome_uncertain"]
    assert [item["date"] for item in data["resign"]["unconfirmed"]] == ["2026-10-01"]


def test_auto_purchase_rejects_negative_inventory_before_exceeding_approved_count(monkeypatch):
    from worker import executor
    from tests.test_buy_card_clicks import BuyPage, SHOP

    page = BuyPage()
    page.cards = -1
    monkeypatch.setattr(executor, "_regex_number", lambda *a, **kw: -1)
    status, _, data = asyncio.run(executor._run_buy_card(
        page, {"shop_url": SHOP, "purchase_quantity": 0, "max_price": 10000},
        ["2020-01-01"], lambda *a: None))
    assert status == "fail"
    assert data["buy_card"]["bought"] == 0
    assert not data["buy_card"]["payment_attempted"]


def test_fresh_execution_after_corrected_revision_gets_fresh_popup(context):
    tools, site_id = setup(context)
    first, review1 = asyncio.run(approve(tools))
    with tools.manager.session_factory() as db:
        db.get(Run, first["run_id"]).status = "fail"
        db.scalar(select(SiteJob).where(SiteJob.run_id == first["run_id"])).status = "fail"
        site = db.get(Site, site_id)
        site.config_revision += 1
        row = db.get(AssistantSession, tools.session_id)
        data = tools.manager._decrypt(row)
        data["target"]["_workflow_binding"]["revision"] = site.config_revision
        tools.target = deepcopy(data["target"])
        tools.manager._encrypt(row, data)
        db.commit()
    tools.call_context = {"tool_message_id": 222, "tool_call_id": "fresh"}
    # The helper asserts no submit before approval for each independent execution.
    tools.manager.executor.submit.reset_mock()
    second, review2 = asyncio.run(approve(tools))
    assert second["ok"] and second["run_id"] != first["run_id"]
    assert review2["id"] != review1["id"]


@pytest.mark.parametrize("change", [
    "missing", "quantity", "price", "spend", "dates", "attempt", "midnight",
    "hidden_category", "unknown_post", "reclassified_post", "extra_approval", "duplicate_approval",
    "different_key", "different_feature", "duplicate_step",
])
def test_worker_rejects_missing_or_changed_frozen_limits_before_browser(context, change):
    from worker.executor import _execute_admitted_job

    tools, site_id = setup(context)
    with tools.manager.session_factory() as db:
        workflows = AssistantWorkflows(tools.manager, tools.owner, tools.session_id, tools.target)
        plan = build_plan(db, workflows._service(db), db.get(Site, site_id))
        steps = [s.model_dump(mode="json") for s in workflows._service(db).resolve_site(site_id).steps]
    payload = {"assistant_run": True, "assistant_consumption_plan": plan,
               "steps": steps, "attempt": 1, "resign_pending_dates": [],
               "resign_today": plan["review"]["today"]}
    if change == "missing":
        del payload["assistant_consumption_plan"]
    elif change == "quantity":
        steps[0]["config"]["purchase_quantity"] = 3
    elif change == "price":
        steps[0]["config"]["max_price"] = 11
    elif change == "spend":
        plan["review"]["steps"][0]["maximum_spend"] = 19
    elif change == "dates":
        payload["resign_pending_dates"] = ["2020-01-01"]
    elif change == "midnight":
        plan["review"]["today"] = payload["resign_today"] = "2020-01-01"
    elif change == "hidden_category":
        steps[0]["category"] = "action"
        del payload["assistant_consumption_plan"]
    elif change in {"unknown_post", "reclassified_post"}:
        feature = "action.new_consumption" if change == "unknown_post" else "action.wait"
        steps[0].update(feature_type=feature, category="post")
        plan["review"]["steps"][0]["feature_type"] = feature
    elif change in {"extra_approval", "duplicate_approval"}:
        item = deepcopy(plan["review"]["steps"][0])
        if change == "extra_approval":
            item["step_key"] = "unreviewed"
        plan["review"]["steps"].append(item)
    elif change == "different_key":
        steps[0]["step_key"] = "unreviewed"
    elif change == "different_feature":
        steps[0]["feature_type"] = "post.resign"
    elif change == "duplicate_step":
        steps.append(deepcopy(steps[0]))
    else:
        payload["attempt"] = 2
    manager = SimpleNamespace(lease_lane=AsyncMock(side_effect=AssertionError("must not lease")))
    result = asyncio.run(_execute_admitted_job(manager, payload))
    assert result["status"] == "fail"
    manager.lease_lane.assert_not_awaited()
