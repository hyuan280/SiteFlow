from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.assistant_takeover import AssistantTakeoverAction
from app.models.assistant_takeover_dedup import TakeoverFailureSignature
from app.models.execution import JobStep, Run, SiteJob
from app.models.workflow import Site, SiteResignRecord
from app.services.assistant_execution import AssistantExecution, SUPPORTED_FEATURES
from app.services.assistant_retry import READ_ONLY
from app.services.assistant_takeover import _mutation_clear, _site_uncertain, takeover_tool_allowed
from app.services.assistant_takeover_dedup import record_signature
from app.services.assistant_workflows import AssistantWorkflows
from app.services.run_notification_dispatch import plan_run_takeovers
from tests.test_assistant_execution import arguments, response
from tests.test_assistant_takeover import (  # noqa: F401
    OWNER, ctx, failed_job, grant, queued, tools_for, uncertain_job,
)
from tests.test_assistant_private_execution import integrations  # noqa: F401
from tests.test_assistant_snapshot import snapshot
from tests.test_assistant_workflows import submit
from tests.test_takeover_daily import install_model, session_data
from tests.test_takeover_live import scope_for
from tests.test_worker_grid_captcha import setup as grid_setup  # noqa: F401


def waiting_job(ctx, feature="challenge.turnstile", data=None, **changes):
    job_id, run_id = failed_job(ctx, status="waiting_human", run_status="waiting_human", **changes)
    step = {"step_key": "challenge", "feature_type": feature, "status": "waiting_human",
            "data": data or {}}
    with ctx[1].session_factory() as db:
        db.get(SiteJob, job_id).result_json = json.dumps({
            "status": "waiting_human", "steps": [step], "outputs": [],
        })
        db.add(JobStep(site_job_id=job_id, step_key="challenge", feature_type=feature,
                       status="waiting_human", data_json=json.dumps(data or {})))
        db.commit()
    return job_id, run_id


@pytest.mark.parametrize("planner", [False, True])
@pytest.mark.parametrize("trigger", ["manual", "schedule"])
def test_waiting_and_mixed_sources_planned_once(ctx, planner, trigger):
    grant(ctx)
    waiting_id, run_id = waiting_job(ctx, trigger=trigger)
    with ctx[1].session_factory() as db:
        for status in ("fail", "error", "success", "deferred", "unknown", "interrupted", "canceled"):
            db.add(SiteJob(run_id=run_id, site_id=ctx[2], site_name=status, lane="general",
                           status=status, finished_at=datetime.now(timezone.utc)))
        db.commit()
        if planner:
            plan_run_takeovers(db, run_id)
            plan_run_takeovers(db, run_id)
            db.commit()
    ctx[0].reconcile()
    ctx[0].reconcile()
    with ctx[1].session_factory() as db:
        actions = list(db.scalars(select(AssistantTakeoverAction)))
        assert {db.get(SiteJob, action.source_job_id).status for action in actions} == {
            "fail", "error", "waiting_human"}
        assert len(actions) == 3 and waiting_id in {action.source_job_id for action in actions}


@pytest.mark.parametrize("changes", [
    {"trigger": "assistant"}, {"historical": True}, {"cancel": True},
    {"run_status": "unknown"}, {"run_status": "interrupted"}, {"run_status": "deferred"},
    {"run_status": "running"}, {"run_status": "canceled"}, {"run_status": "queued"},
    {"status": "deferred"}, {"status": "unknown"}, {"status": "interrupted"},
    {"status": "canceled"}, {"status": "queued"}, {"status": "running"},
])
def test_non_sources_never_planned(ctx, changes):
    grant(ctx)
    _, run_id = failed_job(ctx, **{"status": "waiting_human", "run_status": "waiting_human", **changes})
    with ctx[1].session_factory() as db:
        plan_run_takeovers(db, run_id)
        db.commit()
    assert queued(ctx) is None


@pytest.mark.parametrize("change", ["run_unfinished", "job_unfinished", "disabled", "unselected"])
def test_waiting_requires_finished_selected_enabled_source(ctx, change):
    grant(ctx)
    job_id, run_id = waiting_job(ctx)
    with ctx[1].session_factory() as db:
        if change == "run_unfinished":
            db.get(Run, run_id).finished_at = None
        elif change == "job_unfinished":
            db.get(SiteJob, job_id).finished_at = None
        elif change == "disabled":
            db.get(Site, ctx[2]).enabled = False
        db.commit()
    if change == "unselected":
        from app.schemas.assistant_takeover import TakeoverSettingsUpdate
        from app.services.assistant_takeover_settings import TakeoverSettingsService

        with ctx[1].session_factory() as db:
            service = TakeoverSettingsService(db)
            policy = service.view()
            service.update(TakeoverSettingsUpdate(expected_revision=policy.revision, confirm=True,
                values=policy.values.model_copy(update={"site_ids": []})), OWNER)
    assert queued(ctx) is None


@pytest.mark.parametrize("feature,data", [
    ("challenge.turnstile", {}), ("challenge.2fa", {}), ("sign.image_quiz", {}),
    ("sign.image_quiz", {"reason": "vision_unavailable"}),
    ("sign.image_quiz", {"reason": "vision_not_supported"}),
    ("challenge.grid_captcha", {"reason": "vision_unavailable", "retries": 0}),
    ("challenge.grid_captcha", {"reason": "vision_failed", "attempts": 1, "retries": 0}),
])
def test_known_pre_submit_waiting_retains_live_grant_and_import(ctx, feature, data):
    grant(ctx, live_access=True, mode="autofix")
    waiting_job(ctx, feature, data)
    tools, action_id = tools_for(ctx)
    with tools.manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, action_id)
        assert _site_uncertain(db, ctx[2])  # Strict history semantics are unchanged.
        assert _mutation_clear(db, action)
        assert record_signature(db, action) is None  # Fresh challenges are never reused.
        assert not list(db.scalars(select(TakeoverFailureSignature)))
    for live_feature in SUPPORTED_FEATURES:
        assert takeover_tool_allowed(tools, "execute_step", arguments(live_feature)) is True
    proposal = submit(tools)
    assert asyncio.run(AssistantWorkflows(tools.manager, OWNER, tools.session_id, tools.target).dispatch(
        "import_workflow_draft", {"proposal_message_id": proposal}))["ok"]


@pytest.mark.parametrize("damage", [
    "payment", "submit", "bought", "round", "unknown_step", "unknown_nested", "unknown_reservation",
    "other_unknown", "other_run_unknown", "malformed", "prior_purchase", "generic_click", "auth_wait",
    "post_wait", "unknown_quiz", "unsafe_grid",
])
def test_uncertain_waiting_allows_nonpost_steps_but_not_import(ctx, monkeypatch, damage):
    grant(ctx, live_access=True, mode="autofix")
    job_id, run_id = waiting_job(ctx)
    with ctx[1].session_factory() as db:
        job = db.get(SiteJob, job_id)
        step = db.scalar(select(JobStep).where(JobStep.site_job_id == job_id))
        if damage in {"payment", "submit", "bought", "round", "unknown_nested"}:
            payload = {
                "payment": {"payment_attempted": True}, "submit": {"submit_attempted": True},
                "bought": {"bought": 1}, "round": {"rounds": [{"dispatched": True}]},
                "unknown_nested": {"nested": [{"status": "unknown"}]},
            }[damage]
            step.data_json = json.dumps(payload)
        elif damage == "unknown_step":
            step.status = "unknown"
        elif damage == "unknown_reservation":
            # Lost checkpoint acknowledgement can leave all worker flags false.
            db.add(SiteResignRecord(site_id=ctx[2], resign_date="2020-01-01", status="unknown"))
        elif damage == "other_run_unknown":
            other = Run(created_by=OWNER, trigger="manual", status="unknown")
            db.add(other)
            db.flush()
            db.add(SiteJob(run_id=other.id, site_id=ctx[2], site_name="Old", lane="general", status="success"))
        elif damage == "malformed":
            job.result_json = '{"nested":'
        elif damage == "prior_purchase":
            db.add(JobStep(site_job_id=job_id, step_key="purchase", feature_type="post.buy_card",
                           status="success", data_json='{"buy_card":{"bought":1}}'))
        elif damage in {"generic_click", "auth_wait", "post_wait", "unknown_quiz", "unsafe_grid"}:
            step.feature_type = {"generic_click": "action.click", "auth_wait": "auth.form_login",
                "post_wait": "post.resign", "unknown_quiz": "sign.image_quiz",
                "unsafe_grid": "challenge.grid_captcha"}[damage]
            step.data_json = json.dumps({"reason": "unknown" if damage == "unknown_quiz" else "unsafe_state"})
        db.commit()
    if damage == "other_unknown":
        uncertain_job(ctx, "manual", "unknown")
    tools, action_id = tools_for(ctx)
    assert session_data(tools.manager, tools.session_id)["target"]["workflow_consent"] is False
    tools.turn["id"] = "waiting-diagnostics"
    with tools.manager.session_factory() as db:
        assert db.get(AssistantTakeoverAction, action_id).mode == "analysis"
    scope = scope_for(tools)

    async def worker(path, payload):
        if path.endswith("/execute"):
            return response(scope, payload["operation_id"])
        return {**snapshot(), "session_id": scope.session_id, "lane_key": scope.lane_key}

    monkeypatch.setattr(tools.manager, "_worker", AsyncMock(side_effect=worker))
    assert asyncio.run(tools.dispatch("get_run_results", {"run_id": run_id}))["ok"]
    assert asyncio.run(tools.dispatch("browser_observe", {}))["ok"]
    assert takeover_tool_allowed(tools, "browser_open", {}) is True
    for feature in SUPPORTED_FEATURES - READ_ONLY:
        assert takeover_tool_allowed(tools, "execute_step", arguments(feature)) is True
    assert asyncio.run(tools.dispatch("execute_step", arguments(
        "action.click", {"selector": ["#submit"]})))["ok"]
    assert takeover_tool_allowed(tools, "import_workflow_draft", {})["ok"] is False
    assert takeover_tool_allowed(tools, "retry_step", {})["ok"] is False
    assert takeover_tool_allowed(tools, "run_site", {})["ok"] is False
    for feature in READ_ONLY:
        assert asyncio.run(tools.dispatch("execute_step", arguments(feature)))["ok"]
    assert tools.manager._worker.await_count == 2 + len(READ_ONLY)


@pytest.mark.parametrize("safe", [False, True])
def test_coordinator_dispatches_waiting_without_resuming_source(ctx, monkeypatch, safe):
    grant(ctx, live_access=True, mode="autofix")
    job_id, run_id = waiting_job(ctx, "challenge.turnstile" if safe else "post.resign")
    seen = []

    async def inspect(owner, sid, config):
        seen.append(sid)
        assert session_data(ctx[1], sid)["target"]["workflow_consent"] is safe
        messages = ctx[1].detail(OWNER, sid)["messages"]
        prompt = next(message["content"] for message in messages if message["role"] == "user")
        assert "source status waiting_human" in prompt and "does not resume the source run" in prompt

    install_model(monkeypatch, ctx[1], inspect)
    ctx[0].reconcile()
    asyncio.run(ctx[0].process_one())
    asyncio.run(ctx[0].process_one())
    assert len(seen) == 1
    with ctx[1].session_factory() as db:
        action = db.scalar(select(AssistantTakeoverAction))
        assert action.status == "completed_analysis" and action.attempts == 1
        assert db.get(SiteJob, job_id).status == db.get(Run, run_id).status == "waiting_human"


@pytest.mark.parametrize("uncertain", [False, True])
def test_deferred_history_alone_is_not_uncertainty_or_a_source(ctx, uncertain):
    grant(ctx)
    failed_job(ctx, status="deferred", run_status="deferred", uncertain=uncertain)
    job_id, _ = failed_job(ctx)
    action = queued(ctx)
    assert action.source_job_id == job_id
    with ctx[1].session_factory() as db:
        assert _mutation_clear(db, db.get(AssistantTakeoverAction, action.id)) is not uncertain


@pytest.mark.parametrize("change", ["reservation", "unknown", "stale"])
def test_live_waiting_guards_recheck_after_activation(ctx, monkeypatch, change):
    grant(ctx, live_access=True, mode="autofix")
    waiting_job(ctx)
    tools, _ = tools_for(ctx)
    scope = scope_for(tools)
    if change == "unknown":
        uncertain_job(ctx, "manual", "unknown")
    else:
        with tools.manager.session_factory() as db:
            if change == "stale":
                db.get(Site, ctx[2]).config_revision += 1
            else:
                db.add(SiteResignRecord(site_id=ctx[2], resign_date="2020-01-01", status="unknown"))
            db.commit()
    worker = AsyncMock(side_effect=lambda path, payload: response(scope, payload["operation_id"]))
    monkeypatch.setattr(tools.manager, "_worker", worker)
    assert takeover_tool_allowed(tools, "browser_open", {}) is True
    assert takeover_tool_allowed(tools, "import_workflow_draft", {})["ok"] is False
    result = asyncio.run(AssistantExecution(tools.manager, OWNER, tools.session_id, tools.target).execute(
        arguments("action.click", {"selector": ["#submit"]})))
    if change == "stale":
        assert result["code"] == "takeover_tool_denied"
        worker.assert_not_awaited()
    else:
        assert result["ok"]
        worker.assert_awaited_once()


@pytest.mark.parametrize("status", ["fail", "error"])
def test_failure_in_waiting_run_is_diagnostic_not_mutation_proof(ctx, status):
    grant(ctx, live_access=True, mode="autofix")
    job_id, _ = failed_job(ctx, status=status, run_status="waiting_human")
    with ctx[1].session_factory() as db:
        db.add(JobStep(site_job_id=job_id, step_key="purchase", feature_type="post.buy_card",
                       status="success", data_json='{"buy_card":{"bought":1}}'))
        db.commit()
    tools, _ = tools_for(ctx)
    assert takeover_tool_allowed(tools, "browser_open", {}) is True
    assert takeover_tool_allowed(tools, "execute_step", arguments("action.click")) is True
    assert takeover_tool_allowed(tools, "import_workflow_draft", {})["ok"] is False


def assert_analysis_only(ctx, monkeypatch):
    tools, action_id = tools_for(ctx)
    with tools.manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, action_id)
        assert action.mode == "analysis"
        run_id = action.source_run_id
        assert not _mutation_clear(db, action)
    scope = scope_for(tools)
    worker = AsyncMock(side_effect=lambda path, payload: (
        response(scope, payload["operation_id"]) if path.endswith("/execute") else {
            **snapshot(), "session_id": scope.session_id, "lane_key": scope.lane_key}))
    monkeypatch.setattr(tools.manager, "_worker", worker)
    assert asyncio.run(tools.dispatch("get_run_results", {"run_id": run_id}))["ok"]
    assert asyncio.run(tools.dispatch("browser_observe", {}))["ok"]
    assert takeover_tool_allowed(tools, "browser_open", {}) is True
    click = arguments("action.click", {"selector": ["#submit"]})
    assert takeover_tool_allowed(tools, "execute_step", click) is True
    assert asyncio.run(AssistantExecution(tools.manager, OWNER, tools.session_id, tools.target).execute(
        click))["ok"]
    proposal = submit(tools)
    assert takeover_tool_allowed(tools, "import_workflow_draft", {
        "proposal_message_id": proposal})["code"] == "takeover_tool_denied"
    assert asyncio.run(AssistantWorkflows(tools.manager, OWNER, tools.session_id, tools.target).dispatch(
        "import_workflow_draft", {"proposal_message_id": proposal}))["ok"] is False
    assert worker.await_count == 2


@pytest.mark.parametrize("reason", ["vision_failed", "vision_unclear"])
@pytest.mark.parametrize("retry_data", [
    {}, {"retries": 1}, {"retries": False}, {"retries": "0"}, {"retries": 0.0},
    {"retries": -1}, {"retries": 0, "phase": "retrying"},
    {"retries": 0, "phase": "submit"},
])
def test_grid_final_recognition_reason_requires_whole_step_pre_submit_proof(
    ctx, monkeypatch, reason, retry_data,
):
    grant(ctx, live_access=True, mode="autofix")
    waiting_job(ctx, "challenge.grid_captcha", {"reason": reason, **retry_data})
    assert_analysis_only(ctx, monkeypatch)


@pytest.mark.parametrize("prior_submission", [False, True])
@pytest.mark.parametrize("failure", ["failed", "unclear"])
def test_actual_grid_recognition_failure_after_submission_is_analysis_only(
    ctx, monkeypatch, grid_setup, prior_submission, failure,
):
    from tests.test_worker_grid_captcha import Unclear, run
    from worker import vision

    page, logs, _ = grid_setup
    error = Unclear() if failure == "unclear" else ValueError("recognition failed")
    model = AsyncMock(side_effect=[[1], error] if prior_submission else [error])
    monkeypatch.setattr(vision, "answer_grid_captcha", model)

    def regenerate():
        page.source += 1
        page.selected.clear()

    page.on_submit = regenerate
    status, _, data = run(page, logs, {"max_refresh": int(prior_submission)})
    assert status == "waiting_human" and data["reason"] == "vision_" + failure
    assert data["retries"] == page.submits == int(prior_submission)
    assert "submitted" not in data and "selected_indices" not in data
    grant(ctx, live_access=True, mode="autofix")
    waiting_job(ctx, "challenge.grid_captcha", data)
    if prior_submission:
        assert_analysis_only(ctx, monkeypatch)
    else:
        tools, action_id = tools_for(ctx)
        with tools.manager.session_factory() as db:
            assert _mutation_clear(db, db.get(AssistantTakeoverAction, action_id))
        assert takeover_tool_allowed(tools, "execute_step", arguments("action.click")) is True
        proposal = submit(tools)
        assert asyncio.run(AssistantWorkflows(tools.manager, OWNER, tools.session_id, tools.target).dispatch(
            "import_workflow_draft", {"proposal_message_id": proposal}))["ok"]


@pytest.mark.parametrize("feature", [
    "auth.form_login", "auth.password_verify", "challenge.2fa", "challenge.turnstile",
])
@pytest.mark.parametrize("status", ["fail", "error"])
@pytest.mark.parametrize("storage", ["db", "nested"])
def test_failed_mutating_step_before_challenge_is_not_pre_submit_proof(
    ctx, monkeypatch, feature, status, storage,
):
    grant(ctx, live_access=True, mode="autofix")
    job_id, _ = waiting_job(ctx)
    with ctx[1].session_factory() as db:
        data = {"reason": "execution_error"}
        if storage == "db":
            db.add(JobStep(site_job_id=job_id, step_key="login", feature_type=feature,
                           status=status, data_json=json.dumps(data)))
        else:
            job = db.get(SiteJob, job_id)
            result = json.loads(job.result_json)
            result["steps"].insert(0, {"step_key": "login", "feature_type": feature,
                "status": status, "on_error": "continue", "data": data})
            job.result_json = json.dumps(result)
        db.commit()
    assert_analysis_only(ctx, monkeypatch)


@pytest.mark.parametrize("feature", ["auth.form_login", "auth.password_verify", "challenge.2fa"])
@pytest.mark.parametrize("storage", ["db", "nested", "both"])
def test_confirmed_successful_authentication_before_challenge_allows_mutation(ctx, feature, storage):
    grant(ctx, live_access=True, mode="autofix")
    job_id, _ = waiting_job(ctx)
    with ctx[1].session_factory() as db:
        data = {"submitted": True, "verified": True, "url": "https://example.com/"}
        if storage in {"db", "both"}:
            db.add(JobStep(site_job_id=job_id, step_key="login", feature_type=feature,
                           status="success", data_json=json.dumps(data)))
        db.add(JobStep(site_job_id=job_id, step_key="navigation", feature_type="action.navigate",
                       status="fail", data_json='{"reason":"execution_error"}'))
        if storage in {"nested", "both"}:
            job = db.get(SiteJob, job_id)
            result = json.loads(job.result_json)
            result["steps"].insert(0, {"step_key": "login", "feature_type": feature,
                                      "status": "success", "data": data})
            job.result_json = json.dumps(result)
        db.commit()
    tools, action_id = tools_for(ctx)
    with tools.manager.session_factory() as db:
        assert _mutation_clear(db, db.get(AssistantTakeoverAction, action_id))
    assert takeover_tool_allowed(tools, "execute_step", arguments("action.click")) is True
    proposal = submit(tools)
    assert asyncio.run(AssistantWorkflows(tools.manager, OWNER, tools.session_id, tools.target).dispatch(
        "import_workflow_draft", {"proposal_message_id": proposal}))["ok"]


@pytest.mark.parametrize("feature,data,top_level", [
    ("auth.form_login", {"submitted": True, "verified": False}, False),
    ("auth.form_login", {"submitted": True, "verified": "true"}, False),
    ("auth.form_login", {"submitted": "true", "verified": True}, False),
    ("auth.form_login", {}, False),
    ("auth.form_login", {"submitted": True, "verified": True, "outcome_uncertain": True}, False),
    ("auth.form_login", {"submitted": True, "verified": True, "payment_attempted": True}, False),
    ("auth.form_login", {"submitted": True, "verified": True, "resign": {"submit_attempted": True}}, False),
    ("auth.form_login", {"submitted": True, "verified": True, "nested": {"submitted": True}}, False),
    ("auth.form_login", {"submitted": True, "verified": True}, True),
    ("sign.button", {"submitted": True, "verified": True}, False),
])
def test_auth_exception_never_masks_other_effects(ctx, monkeypatch, feature, data, top_level):
    grant(ctx, live_access=True, mode="autofix")
    job_id, _ = waiting_job(ctx)
    with ctx[1].session_factory() as db:
        job = db.get(SiteJob, job_id)
        result = json.loads(job.result_json)
        result["steps"].insert(0, {"step_key": "login", "feature_type": feature,
                                  "status": "success", "data": data})
        if top_level:
            result["submitted"] = True
        job.result_json = json.dumps(result)
        db.commit()
    assert_analysis_only(ctx, monkeypatch)
