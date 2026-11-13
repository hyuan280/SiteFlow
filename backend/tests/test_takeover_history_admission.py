from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models.assistant import AssistantSession
from app.models.assistant_run import AssistantRunGuard
from app.models.assistant_takeover import AssistantTakeoverAction, AssistantTakeoverStepOutcome
from app.models.assistant_takeover_dedup import TakeoverAnalysisProvenance, TakeoverDedupLink
from app.models.assistant_workflow import AssistantWorkflowAction
from app.models.execution import JobStep, Run, SiteJob
from app.models.workflow import Site
from app.services.assistant_execution import AssistantExecution, SUPPORTED_FEATURES
from app.services.assistant_retry import READ_ONLY, RECORDS
from app.services.assistant_takeover import (
    _mutation_clear, activate_takeover, remove_takeover, takeover_site_uncertain, takeover_tool_allowed,
)
from app.services.assistant_tools import AssistantTools
from app.services.assistant_workflows import AssistantWorkflows
from app.services.run_notification_dispatch import plan_run_takeovers
from tests.test_assistant_execution import arguments, response
from tests.test_assistant_snapshot import snapshot
from tests.test_assistant_takeover import (  # noqa: F401
    OWNER, ctx, failed_job, grant, tools_for,
)
from tests.test_assistant_workflows import submit
from tests.test_takeover_daily import install_model, session_data
from tests.test_takeover_dedup import complete, failure, no_runtime
from tests.test_takeover_live import scope_for


def piggo_failure(ctx):
    job_id, run_id = failed_job(ctx)
    with ctx[1].session_factory() as db:
        db.add(JobStep(site_job_id=job_id, step_key="result", feature_type="flow.result",
                       status="success", data_json='{"value":false}'))
        db.commit()
    ctx[0].reconcile()
    with ctx[1].session_factory() as db:
        action_id = db.scalar(select(AssistantTakeoverAction.id).where(
            AssistantTakeoverAction.source_job_id == job_id))
    assert action_id is not None
    return action_id, job_id, run_id


def test_piggo_old_unknown_then_ten_successes_diagnoses_once_and_reuses(ctx, monkeypatch):
    takeover, manager, site_id = ctx
    grant(ctx, live_access=True, mode="autofix")
    old_job, _ = failed_job(ctx, historical=True, status="unknown", run_status="unknown")
    with manager.session_factory() as db:
        db.add(JobStep(site_job_id=old_job, step_key="turnstile", feature_type="challenge.turnstile",
                       status="unknown", data_json='{"outcome_uncertain":true}'))
        db.commit()
    for _ in range(10):
        failed_job(ctx, historical=True, status="success", run_status="success", snapshot=False)
    action_id, job_id, run_id = piggo_failure(ctx)
    with manager.session_factory() as db:
        assert db.get(AssistantTakeoverAction, action_id).status == "queued"
    seen = []

    async def diagnose(owner, sid, config):
        seen.append(sid)
        target = session_data(manager, sid)["target"]
        tools = AssistantTools(manager, owner, sid, target, config=config,
                               turn=manager._active_turns[sid])
        with manager.session_factory() as db:
            action = db.get(AssistantTakeoverAction, action_id)
            assert action.mode == "analysis" and action.reason == "source_outcome_uncertain"
            assert not _mutation_clear(db, action)
        assert not target["workflow_consent"] and target["browser_consent"]
        assert (await tools.dispatch("get_run_results", {"run_id": run_id}))["ok"]
        assert (await tools.dispatch("browser_open", {}))["ok"]
        assert (await tools.dispatch("browser_observe", {}))["ok"]
        for feature in SUPPORTED_FEATURES - READ_ONLY:
            step = arguments(feature, {"selector": ["#submit"]})
            assert takeover_tool_allowed(tools, "execute_step", step) is True
        assert (await tools.dispatch("execute_step", arguments(
            "action.click", {"selector": ["#submit"]})))["ok"]
        for feature in READ_ONLY:
            assert (await tools.dispatch("execute_step", arguments(feature)))["ok"]
        proposal = await asyncio.to_thread(submit, tools)
        assert (await tools.dispatch("import_workflow_draft", {
            "proposal_message_id": proposal}))["code"] == "takeover_tool_denied"
        assert (await AssistantWorkflows(manager, owner, sid, target).dispatch(
            "import_workflow_draft", {"proposal_message_id": proposal}))[
                "code"] == "takeover_write_denied"

    async def worker(path, payload):
        if path == "open":
            return {"session_id": payload["session_id"], "lane_key": payload["lane_key"],
                    "status": "ready", "expires_at": (
                        datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()}
        if path.endswith("/close"):
            return {"closed": True}
        scope = next(iter(manager.browsers.values()))
        if path.endswith("/execute"):
            return response(scope, payload["operation_id"])
        assert path.endswith("/observe")
        return {**snapshot(), "session_id": scope.session_id, "lane_key": scope.lane_key}

    install_model(monkeypatch, manager, diagnose)
    monkeypatch.setattr(manager, "_worker", AsyncMock(side_effect=worker))
    send = AsyncMock(wraps=manager.send_takeover)
    monkeypatch.setattr(manager, "send_takeover", send)
    asyncio.run(takeover.process_one())
    send.assert_awaited_once()
    assert len(seen) == 1
    assert manager._worker.await_count == 4 + len(READ_ONLY)
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, action_id)
        assert action.status == "completed_analysis" and action.import_receipt_json is None
        proof = db.get(TakeoverAnalysisProvenance, action_id)
        assert proof.source_current and proof.source_fingerprint == proof.inspected_fingerprint
        assert db.get(Site, site_id).config_revision == 1
        assert db.get(SiteJob, job_id).status == db.get(Run, run_id).status == "fail"
        assert db.get(SiteJob, old_job).status == "unknown"
        assert db.scalar(select(func.count()).select_from(Run)) == 12
    repeated, _, _ = piggo_failure(ctx)
    runtime = no_runtime(ctx, monkeypatch)
    asyncio.run(takeover.process_one())
    runtime.assert_not_called()
    manager.send_takeover.assert_not_awaited()
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, repeated)
        assert action.status == "reused" and action.attempts == 0
        assert db.get(TakeoverDedupLink, repeated).source_action_id == action_id


@pytest.mark.parametrize("history", [
    "unknown", "interrupted", "waiting_human", "nested", "malformed", "unknown_action",
    "reserved_action", "assistant_run",
])
@pytest.mark.parametrize("planner", ["reconcile", "notification"])
def test_uncertain_history_allows_diagnosis_but_not_writes(ctx, monkeypatch, history, planner):
    takeover, manager, site_id = ctx
    grant(ctx, live_access=True, mode="autofix")
    old_job, old_run = failed_job(ctx, historical=True, snapshot=False,
                                status=history if history in {
                                    "unknown", "interrupted", "waiting_human"} else "success")
    with manager.session_factory() as db:
        if history in {"nested", "malformed"}:
            db.get(SiteJob, old_job).result_json = (
                '{"nested":[{"status":"unknown"}]}' if history == "nested" else '{"broken":')
        elif history in {"unknown_action", "reserved_action"}:
            conversation = AssistantSession(id="old-session", owner=OWNER, status="idle")
            manager._encrypt(conversation, {"target": {}})
            db.add(conversation)
            db.flush()
            db.add(AssistantWorkflowAction(id="old-action", session_id="old-session", owner=OWNER,
                kind="run", state="unknown" if history == "unknown_action" else "reserved",
                site_id=site_id, request_fingerprint="old", ciphertext=b"", nonce=b"", key_version=1))
        elif history == "assistant_run":
            site = db.get(Site, site_id)
            db.get(Run, old_run).trigger = "assistant"
            db.get(Run, old_run).status = "unknown"
            db.add(AssistantRunGuard(run_id=old_run, site_id=site_id,
                site_history_key=site.history_key, site_revision=site.config_revision, fingerprint="old"))
        db.commit()
    job_id, run_id = failed_job(ctx)
    if planner == "notification":
        with manager.session_factory() as db:
            plan_run_takeovers(db, run_id)
            db.commit()
    tools, action_id = tools_for(ctx)
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, action_id)
        assert action.source_job_id == job_id and action.mode == "analysis"
        assert action.reason == "source_outcome_uncertain" and not _mutation_clear(db, action)
    scope = scope_for(tools)
    worker = AsyncMock(side_effect=lambda path, payload: (
        response(scope, payload["operation_id"]) if path.endswith("/execute") else {
            **snapshot(), "session_id": scope.session_id, "lane_key": scope.lane_key}))
    monkeypatch.setattr(manager, "_worker", worker)
    assert asyncio.run(tools.dispatch("get_run_results", {"run_id": run_id}))["ok"]
    assert asyncio.run(tools.dispatch("browser_observe", {}))["ok"]
    assert takeover_tool_allowed(tools, "execute_step", arguments()) is True
    assert asyncio.run(AssistantExecution(manager, OWNER, tools.session_id, tools.target).execute(
        arguments("action.click", {"selector": ["#submit"]})))["ok"]
    assert takeover_tool_allowed(tools, "import_workflow_draft", {})["ok"] is False
    assert worker.await_count == 2


@pytest.mark.parametrize("history_status", ["fail", "error", "unknown", "failed_analysis"])
def test_raw_repeat_without_valid_analysis_always_calls_model(ctx, monkeypatch, history_status):
    grant(ctx, live_access=True, mode="autofix")
    if history_status == "failed_analysis":
        old_action, _, _ = failure(ctx)
        complete(ctx, old_action)
        with ctx[1].session_factory() as db:
            db.get(AssistantTakeoverAction, old_action).status = "failed"
            db.commit()
    else:
        failed_job(ctx, historical=True, status=history_status)
    seen = []

    async def diagnose(owner, sid, config):
        seen.append(config["_takeover_action_id"])

    install_model(monkeypatch, ctx[1], diagnose)
    for _ in range(2):
        action_id, _, _ = failure(ctx)
        asyncio.run(ctx[0].process_one())
        with ctx[1].session_factory() as db:
            action = db.get(AssistantTakeoverAction, action_id)
            assert action.status == "completed_analysis"
            # No usable completed proof remains for the next occurrence.
            action.status = "failed"
            db.commit()
    assert len(seen) == 2


@pytest.mark.parametrize("storage", ["job", "step", "other_job", "other_step"])
def test_current_failed_run_uncertainty_does_not_prevent_analysis(ctx, monkeypatch, storage):
    grant(ctx, live_access=True, mode="autofix")
    job_id, run_id = failed_job(ctx)
    with ctx[1].session_factory() as db:
        source = db.get(SiteJob, job_id)
        if storage.startswith("other"):
            source = SiteJob(run_id=run_id, site_id=ctx[2], site_name="Other", lane="general",
                             status="unknown" if storage == "other_job" else "success")
            db.add(source)
            db.flush()
        if storage in {"job", "other_job"}:
            source.result_json = '{"outcome_uncertain":true}'
        else:
            db.add(JobStep(site_job_id=source.id, step_key="uncertain", feature_type="action.click",
                           status="unknown"))
        db.commit()
    seen = []

    async def diagnose(owner, sid, config):
        seen.append(config["_takeover_action_id"])
        assert not session_data(ctx[1], sid)["target"]["workflow_consent"]

    install_model(monkeypatch, ctx[1], diagnose)
    ctx[0].reconcile()
    asyncio.run(ctx[0].process_one())
    assert len(seen) == 1
    with ctx[1].session_factory() as db:
        action = db.get(AssistantTakeoverAction, seen[0])
        assert action.source_job_id == job_id and action.status == "completed_analysis"
        assert action.mode == "analysis" and action.import_receipt_json is None


def unknown_takeover_step(ctx, monkeypatch):
    grant(ctx, live_access=True, mode="autofix")
    failed_job(ctx)
    tools, action_id = tools_for(ctx)
    tools.turn["id"] = "autonomous-step"
    scope = scope_for(tools)
    step = arguments("action.click", {"selector": ["#submit"]})

    async def worker(path, payload):
        with ctx[1].session_factory() as db:
            outcome = db.get(AssistantTakeoverStepOutcome, payload["operation_id"])
            assert outcome.state == "dispatched" and outcome.dispatched and not outcome.resolved
            data = ctx[1]._decrypt(db.get(AssistantSession, tools.session_id))
            assert data[RECORDS][payload["operation_id"]]["state"] == "dispatched"
        return response(scope, payload["operation_id"], status="unknown", code="outcome_uncertain")

    monkeypatch.setattr(ctx[1], "_worker", AsyncMock(side_effect=worker))
    result = asyncio.run(tools.dispatch("execute_step", step))
    assert result["status"] == "unknown"
    return tools, action_id, step


@pytest.mark.parametrize("deleted", [False, True])
def test_prior_daily_chat_unknown_step_blocks_only_future_writes(ctx, monkeypatch, deleted):
    tools, first_action, step = unknown_takeover_step(ctx, monkeypatch)
    takeover, manager, site_id = ctx
    with manager.session_factory() as db:
        first = db.get(AssistantTakeoverAction, first_action)
        anchor = db.get(SiteJob, first.source_job_id).finished_at
        row = db.get(AssistantSession, tools.session_id)
        data = manager._decrypt(row)
        remove_takeover(data)
        manager._encrypt(row, data)
        row.status = "idle"
        first.status = "completed_analysis"
        db.commit()
    manager._active_turns.pop(tools.session_id, None)
    if deleted:
        asyncio.run(manager.delete(OWNER, tools.session_id))
    second_job, run_id = failed_job(ctx)
    with manager.session_factory() as db:
        job = db.get(SiteJob, second_job)
        job.finished_at = anchor + timedelta(days=1)
        job.message = "Different failure requiring fresh analysis"
        db.commit()
    seen = []

    async def diagnose(owner, sid, config):
        seen.append(sid)
        assert sid != tools.session_id
        target = session_data(manager, sid)["target"]
        assert target["browser_consent"] and not target["workflow_consent"]
        current = AssistantTools(manager, owner, sid, target, config=config,
                                 turn=manager._active_turns[sid])
        assert (await current.dispatch("get_run_results", {"run_id": run_id}))["ok"]
        scope = scope_for(current)
        manager._worker = AsyncMock(side_effect=lambda path, payload: (
            response(scope, payload["operation_id"]) if path.endswith("/execute") else {
                **snapshot(), "session_id": scope.session_id, "lane_key": scope.lane_key}))
        assert (await current.dispatch("browser_observe", {}))["ok"]
        assert takeover_tool_allowed(current, "browser_open", {}) is True
        assert takeover_tool_allowed(current, "execute_step", arguments()) is True
        assert (await current.dispatch("execute_step", arguments(
            "action.click", {"selector": ["#submit"]})))["ok"]
        assert (await AssistantExecution(manager, owner, sid, target).execute(arguments(
            "action.click", {"selector": ["#submit"]})))["ok"]
        proposal = await asyncio.to_thread(submit, current)
        assert (await current.dispatch("import_workflow_draft", {
            "proposal_message_id": proposal}))["code"] == "takeover_tool_denied"
        assert (await AssistantWorkflows(manager, owner, sid, target).dispatch(
            "import_workflow_draft", {"proposal_message_id": proposal}))[
                "code"] == "takeover_write_denied"
        assert manager._worker.await_count == 3

    install_model(monkeypatch, manager, diagnose)
    takeover.reconcile()
    asyncio.run(takeover.process_one())
    assert len(seen) == 1
    with manager.session_factory() as db:
        action = db.scalar(select(AssistantTakeoverAction).where(
            AssistantTakeoverAction.source_job_id == second_job))
        assert action.status == "completed_analysis" and action.mode == "analysis"
        assert action.import_receipt_json is None and takeover_site_uncertain(db, action)
        original = db.get(AssistantTakeoverStepOutcome, step["operation_id"])
        assert original.status == "unknown" and not original.resolved
        assert db.get(Site, site_id).config_revision == 1


def test_only_dispatched_successful_linked_retry_resolves_durable_ancestry(ctx, monkeypatch):
    from tests.test_assistant_retry import approve, retry

    tools, action_id, step = unknown_takeover_step(ctx, monkeypatch)
    manager = ctx[1]
    with manager.session_factory() as db:
        row = db.get(AssistantSession, tools.session_id)
        data = manager._decrypt(row)
        remove_takeover(data)
        manager._encrypt(row, data)
        row.status = "idle"
        db.commit()
    manager._active_turns[tools.session_id] = {"id": "human"}
    target = session_data(manager, tools.session_id)["target"]
    helper = AssistantExecution(manager, OWNER, tools.session_id, target)
    original = step["operation_id"]
    approve(helper, original)
    with manager.session_factory() as db:
        assert takeover_site_uncertain(db, db.get(AssistantTakeoverAction, action_id))
    scope = manager.browsers[tools.session_id]
    manager._worker = AsyncMock(side_effect=lambda path, payload: response(
        scope, payload["operation_id"], status="unknown", code="outcome_uncertain"))
    child = retry(helper, original)
    assert child["status"] == "unknown"
    manager._idle(tools.session_id)
    approve(helper, child["operation_id"])
    with manager.session_factory() as db:
        assert not db.get(AssistantTakeoverStepOutcome, original).resolved
        assert not db.get(AssistantTakeoverStepOutcome, child["operation_id"]).resolved
    manager._worker = AsyncMock(side_effect=lambda path, payload: response(scope, payload["operation_id"]))
    success = retry(helper, child["operation_id"])
    assert success["ok"]
    with manager.session_factory() as db:
        for operation in (original, child["operation_id"], success["operation_id"]):
            assert db.get(AssistantTakeoverStepOutcome, operation).resolved
        assert db.get(AssistantTakeoverStepOutcome, original).status == "unknown"
        assert _mutation_clear(db, db.get(AssistantTakeoverAction, action_id))
        # Upgrade recovery must reconstruct successful retry ancestry too.
        for outcome in list(db.scalars(select(AssistantTakeoverStepOutcome))):
            db.delete(outcome)
        db.commit()
    manager.recover()
    with manager.session_factory() as db:
        assert _mutation_clear(db, db.get(AssistantTakeoverAction, action_id))
        for operation in (original, child["operation_id"], success["operation_id"]):
            assert db.get(AssistantTakeoverStepOutcome, operation).resolved
    # A different future failure can again use the explicitly granted live writes.
    future_job, _ = failed_job(ctx)
    with manager.session_factory() as db:
        db.get(SiteJob, future_job).finished_at += timedelta(days=1)
        db.commit()
    ctx[0].reconcile()
    selected, _ = ctx[0]._select()
    sid = manager.create_takeover(selected)
    claim = ctx[0]._claim(selected, sid, False)
    assert claim is not None and claim[2] == "autofix"
    with manager.session_factory() as db:
        row = db.get(AssistantSession, sid)
        row.status = "running"
        activate_takeover(manager, db, row, selected, claim[0])
        db.commit()
    manager._active_turns[sid] = {"id": "future-takeover", "_takeover_action_id": selected,
        "_takeover_request_id": claim[0], "_request_id": claim[0]}
    current = AssistantTools(manager, OWNER, sid, session_data(manager, sid)["target"],
                             turn=manager._active_turns[sid])
    scope = scope_for(current)
    manager._worker = AsyncMock(side_effect=lambda path, payload: response(scope, payload["operation_id"]))
    assert asyncio.run(current.dispatch("execute_step", arguments(
        "action.click", {"selector": ["#submit"]})))["ok"]
    proposal = submit(current)
    assert asyncio.run(current.dispatch("import_workflow_draft", {"proposal_message_id": proposal}))["ok"]


@pytest.mark.parametrize("state,status,dispatched,blocked", [
    ("dispatched", None, True, True), ("terminal", "unknown", True, True),
    ("reserved", None, False, False), ("terminal", "failed", False, False),
    ("terminal", "success", False, False),
])
def test_recovery_backfills_existing_takeover_records_once(ctx, monkeypatch, state, status, dispatched, blocked):
    tools, action_id, step = unknown_takeover_step(ctx, monkeypatch)
    with ctx[1].session_factory() as db:
        db.delete(db.get(AssistantTakeoverStepOutcome, step["operation_id"]))
        row = db.get(AssistantSession, tools.session_id)
        data = ctx[1]._decrypt(row)
        data[RECORDS][step["operation_id"]].update(state=state, status=status, dispatched=dispatched)
        ctx[1]._encrypt(row, data)
        db.commit()
    ctx[1].recover()
    ctx[1].recover()
    with ctx[1].session_factory() as db:
        action = db.get(AssistantTakeoverAction, action_id)
        assert takeover_site_uncertain(db, action) is blocked
        assert db.scalar(select(func.count()).select_from(AssistantTakeoverStepOutcome)) == 1
        outcome = db.get(AssistantTakeoverStepOutcome, step["operation_id"])
        assert outcome.action_id == action_id
        assert "#submit" not in str(outcome.__dict__)
        action.site_history_key = "new-site-identity"
        assert not takeover_site_uncertain(db, action)


@pytest.mark.parametrize("status,dispatched", [("success", False), ("failed", True), ("error", True)])
def test_unsuccessful_or_undispatched_retry_cannot_clear_original(ctx, monkeypatch, status, dispatched):
    from app.services.assistant_takeover import record_takeover_step

    tools, action_id, step = unknown_takeover_step(ctx, monkeypatch)
    with ctx[1].session_factory() as db:
        row = db.get(AssistantSession, tools.session_id)
        data = ctx[1]._decrypt(row)
        records = data[RECORDS]
        retry_id = str(uuid4())
        records[retry_id] = {"step": records[step["operation_id"]]["step"],
            "state": "terminal", "status": status, "dispatched": dispatched,
            "retry_of": step["operation_id"]}
        record_takeover_step(db, row, retry_id, records)
        assert not db.get(AssistantTakeoverStepOutcome, step["operation_id"]).resolved
        assert takeover_site_uncertain(db, db.get(AssistantTakeoverAction, action_id))
        db.rollback()


def test_linked_success_does_not_resolve_unrelated_takeover_step(ctx, monkeypatch):
    from app.services.assistant_takeover import record_takeover_step

    tools, action_id, step = unknown_takeover_step(ctx, monkeypatch)
    with ctx[1].session_factory() as db:
        row = db.get(AssistantSession, tools.session_id)
        records = ctx[1]._decrypt(row)[RECORDS]
        unrelated, retry_id = str(uuid4()), str(uuid4())
        records[unrelated] = dict(records[step["operation_id"]])
        record_takeover_step(db, row, unrelated, records)
        records[retry_id] = {"step": records[step["operation_id"]]["step"],
            "state": "terminal", "status": "success", "dispatched": True,
            "retry_of": step["operation_id"]}
        record_takeover_step(db, row, retry_id, records)
        assert db.get(AssistantTakeoverStepOutcome, step["operation_id"]).resolved
        assert not db.get(AssistantTakeoverStepOutcome, unrelated).resolved
        assert takeover_site_uncertain(db, db.get(AssistantTakeoverAction, action_id))


def test_step_outcome_and_encrypted_ledger_roll_back_together(ctx, monkeypatch):
    tools, _, step = unknown_takeover_step(ctx, monkeypatch)
    helper = AssistantExecution(ctx[1], OWNER, tools.session_id, tools.target)
    helper.operation_id = step["operation_id"]

    def fail_encrypt(*args):
        raise RuntimeError("encrypted ledger write failed")

    monkeypatch.setattr(ctx[1], "_encrypt", fail_encrypt)
    with pytest.raises(RuntimeError, match="encrypted ledger write failed"):
        helper._execution_state("terminal", "success", "step_succeeded")
    with ctx[1].session_factory() as db:
        outcome = db.get(AssistantTakeoverStepOutcome, step["operation_id"])
        assert outcome.status == "unknown" and not outcome.resolved
        data = ctx[1]._decrypt(db.get(AssistantSession, tools.session_id))
        assert data[RECORDS][step["operation_id"]]["status"] == "unknown"
