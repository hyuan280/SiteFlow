from __future__ import annotations

import asyncio
import copy
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker import executor  # noqa: E402
from tests.assistant_consumption_helpers import approved_consumption_payload  # noqa: E402, F401
from tests.test_worker_navigation import _JumpLane, _JumpManager, _JumpPage  # noqa: E402


DATES = ["2026-10-01", "2026-10-02"]
PENDING = "signal:sys_resign_pending:true"


def step(key, feature="action.wait", **extra):
    return {"step_key": key, "feature_type": feature, "enabled": True,
            "condition": "always", "config": {}, **extra}


def loop_steps(target="resign"):
    return [
        step("resign", "post.resign_loop", condition=PENDING),
        step("verify", "challenge.turnstile", condition=PENDING),
        step("jump", "flow.jump", condition=PENDING,
             config={"match_mode": "always", "target_step_key": target, "max_jumps": 10}),
        step("tail"),
    ]


@pytest.fixture
def condition_checks(monkeypatch):
    checks = []
    original = executor._condition_allows

    async def record(page, condition, signals, log=None, runtime_signals=None):
        result = await original(page, condition, signals, log, runtime_signals)
        checks.append((condition, copy.deepcopy(runtime_signals), result))
        return result

    monkeypatch.setattr(executor, "_condition_allows", record)
    return checks


@pytest.fixture
def worker_run(monkeypatch, tmp_path, approved_consumption_payload):
    monkeypatch.setattr(executor, "ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(executor.JobArtifacts, "capture_failure", AsyncMock())
    monkeypatch.setattr(executor, "_export_site_cookies", AsyncMock(return_value=[]))

    async def execute(steps, run_step, *, assistant=False):
        monkeypatch.setattr(executor, "_run_step", run_step)
        lane = _JumpLane(_JumpPage())
        lane.session = SimpleNamespace(disconnected=False, desktop=None)
        lane.in_use = 1
        manager = _JumpManager(lane)
        snapshots = []

        async def progress(data):
            snapshots.append(copy.deepcopy(data))

        payload = {"job_id": 1, "steps": steps, "assistant_run": assistant,
                   "base_url": "https://site.test/", "resign_today": "2026-11-02",
                   "resign_pending_dates": list(DATES),
                   "signals": [{"signal_key": "sys_resign_pending", "enabled": True}]}
        if assistant:
            payload = approved_consumption_payload(payload)
        result = await executor.execute_job(manager, payload, on_step=progress)
        assert manager.released
        assert lane.task_pages == {}
        return result, snapshots, payload

    return execute


@pytest.fixture
def cycle_runner():
    fake = SimpleNamespace(calls=[], pending=[], states=[], completion="success")

    async def run_step(page, current, *_args, **_kwargs):
        key = current["step_key"]
        fake.calls.append(key)
        if key == "resign":
            pending, payload = _args[7], _args[-1]
            fake.pending.append(list(pending))
            state = payload["_resign_runtime"].setdefault(key, {})
            if not state:
                state.update(pending=list(pending),
                             success=[], already_signed=[], remaining=list(pending),
                             unconfirmed=[], submit_attempted=False)
            # Each visit confirms the previous click, then dispatches at most one date.
            for item in state["unconfirmed"]:
                date = item["date"]
                if fake.completion == "success":
                    state["success"].append({"date": date, "message": "confirmed"})
                else:
                    state["already_signed"].append(date)
                state["remaining"].remove(date)
            state["unconfirmed"] = []
            if state["remaining"]:
                state["submit_attempted"] = True
                state["unconfirmed"] = [{"date": state["remaining"][0]}]
            fake.states.append(copy.deepcopy(state))
            return "success", "cycle advanced", {"resign": copy.deepcopy(state)}
        if current["feature_type"] == "flow.jump":
            return await executor._run_flow_jump(page, current["config"])
        return "success", "completed", {}

    fake.run_step = run_step
    return fake


@pytest.mark.parametrize("completion", ["success", "already_signed"])
def test_two_dates_loop_turns_pending_false_and_skips_verify_and_jump(
    worker_run, cycle_runner, condition_checks, completion,
):
    cycle_runner.completion = completion
    result, snapshots, payload = asyncio.run(worker_run(loop_steps(), cycle_runner.run_step))

    assert result["status"] == "success"
    assert cycle_runner.calls == ["resign", "verify", "jump", "resign", "verify", "jump",
                                  "resign", "tail"]
    assert cycle_runner.pending == [DATES, DATES, DATES]
    assert payload["resign_pending_dates"] == DATES
    assert [row["status"] for row in result["steps"]] == [
        "success", "skipped", "skipped", "success",
    ]
    state = result["steps"][0]["data"]["resign"]
    completed = ([row["date"] for row in state["success"]]
                 if completion == "success" else state["already_signed"])
    assert completed == DATES
    assert state["remaining"] == state["unconfirmed"] == []
    assert state == payload["_resign_runtime"]["resign"]
    assert [state["remaining"] for state in cycle_runner.states] == [DATES, DATES[1:], []]
    checks = [(signals["sys_resign_pending"][0], outcome)
              for condition, signals, outcome in condition_checks if condition == PENDING]
    assert checks == [(True, "true")] * 7 + [(False, "false")] * 2
    assert sum(row.get("status") == "success" and row.get("steps") == []
               for row in snapshots) == 2


def test_without_jump_remaining_dates_require_human(worker_run, cycle_runner, condition_checks):
    result, _, payload = asyncio.run(worker_run(
        [loop_steps()[0], step("tail", condition=PENDING)], cycle_runner.run_step,
    ))
    assert result["status"] == "waiting_human"
    assert cycle_runner.calls == ["resign", "tail"]
    assert result["steps"][0]["data"]["resign"]["remaining"] == DATES
    assert result["steps"][0]["status"] == "waiting_human"
    assert result["steps"][0]["data"]["value"] is False
    assert result["outputs"][0]["value"] is False
    assert payload["_resign_runtime"]["resign"]["unconfirmed"] == [{"date": DATES[0]}]
    assert [(signals["sys_resign_pending"][0], outcome)
            for condition, signals, outcome in condition_checks if condition == PENDING] == [
        (True, "true"), (True, "true"),
    ]


def test_jump_across_purchase_remains_forbidden(worker_run, cycle_runner):
    async def run_step(page, current, *args, **kwargs):
        if current["step_key"] == "buy":
            cycle_runner.calls.append("buy")
            return "success", "purchased", {"buy_card": {"payment_attempted": True, "bought": 1}}
        return await cycle_runner.run_step(page, current, *args, **kwargs)

    result, _, _ = asyncio.run(worker_run(
        [step("buy", "post.buy_card"), *loop_steps(target="buy")], run_step,
    ))
    assert result["status"] == "waiting_human"
    assert cycle_runner.calls == ["buy", "resign", "verify", "jump"]
    assert result["steps"][-1]["step_key"] == "jump"
    assert result["steps"][-1]["status"] == "waiting_human"
    assert result["steps"][0]["data"]["buy_card"]["payment_attempted"]


def test_finish_restores_accumulated_state_after_jump_prunes_results(worker_run, cycle_runner):
    gate_calls = 0

    async def run_step(page, current, *args, **kwargs):
        nonlocal gate_calls
        if current["step_key"] == "gate":
            gate_calls += 1
            if gate_calls == 3:
                return "waiting_human", "gate needs help", {}
        return await cycle_runner.run_step(page, current, *args, **kwargs)

    result, snapshots, payload = asyncio.run(worker_run(
        [step("gate"), *loop_steps(target="gate")], run_step,
    ))
    assert gate_calls == 3
    assert result["status"] == "waiting_human"
    assert [row["step_key"] for row in result["steps"]] == ["gate", "resign"]
    restored = result["steps"][-1]
    assert restored["status"] == "waiting_human"
    assert restored["data"]["resign"] == payload["_resign_runtime"]["resign"]
    assert restored["data"]["resign"]["success"] == [{"date": DATES[0], "message": "confirmed"}]
    assert restored["data"]["resign"]["remaining"] == DATES[1:]
    assert restored["data"]["resign"]["unconfirmed"] == [{"date": DATES[1]}]
    assert sum(row.get("status") == "success" and row.get("steps") == []
               for row in snapshots) == 2


def test_assistant_reenters_same_approved_step_without_expanding_dates(
    worker_run, cycle_runner, condition_checks,
):
    result, _, payload = asyncio.run(worker_run(
        loop_steps(), cycle_runner.run_step, assistant=True,
    ))
    approved_dates = payload["assistant_consumption_plan"]["review"]["dates"]
    assert approved_dates
    assert result["status"] == "success"
    assert cycle_runner.calls.count("resign") == len(approved_dates) + 1
    assert cycle_runner.calls.count("jump") == len(approved_dates)
    assert all(dates == approved_dates for dates in cycle_runner.pending)
    assert payload["resign_pending_dates"] == approved_dates
    state = result["steps"][0]["data"]["resign"]
    assert state["pending"] == approved_dates
    assert [item["date"] for item in state["success"]] == approved_dates
    assert state["remaining"] == []
    checks = [(signals["sys_resign_pending"][0], outcome)
              for condition, signals, outcome in condition_checks if condition == PENDING]
    assert checks == [(True, "true")] * (3 * len(approved_dates) + 1) + [(False, "false")] * 2
    assert [row["status"] for row in result["steps"][1:3]] == ["skipped", "skipped"]


def test_resign_runtime_is_independent_between_jobs(worker_run, cycle_runner):
    async def check():
        first, _, first_payload = await worker_run(loop_steps(), cycle_runner.run_step)
        second, _, second_payload = await worker_run(loop_steps(), cycle_runner.run_step)
        assert first["status"] == second["status"] == "success"
        assert first_payload["_resign_runtime"] is not second_payload["_resign_runtime"]
        assert first_payload["_resign_runtime"]["resign"] is not second_payload["_resign_runtime"]["resign"]
        assert cycle_runner.calls.count("resign") == 6
        assert cycle_runner.states[0] == cycle_runner.states[3]

    asyncio.run(check())


@pytest.mark.parametrize("action, status", [("jump", "success"), ("restart", "restart")])
def test_flow_jump_always_needs_no_page_evidence(monkeypatch, action, status):
    page_text = AsyncMock(side_effect=AssertionError("page text must not be read"))
    selector_exists = AsyncMock(side_effect=AssertionError("selectors must not be checked"))
    monkeypatch.setattr(executor, "_page_text", page_text)
    monkeypatch.setattr(executor, "_selector_exists", selector_exists)
    result = asyncio.run(executor._run_flow_jump(object(), {
        "match_mode": "always", "texts": ["stale page text"], "selectors": ["#stale-selector"],
        "action": action, "target_step_key": "resign",
    }))
    assert result[0] == status
    assert result[2] == {"matched": True, "matched_by": ["always"],
                         "action": action, "target_step_key": "resign"}
    page_text.assert_not_awaited()
    selector_exists.assert_not_awaited()


def test_run_step_passes_same_loop_runtime(monkeypatch):
    module = ModuleType("worker.resign_loop")
    runner = AsyncMock(return_value=("success", "runner result", {"resign": {}}))
    module.run_resign_loop = runner
    monkeypatch.setitem(sys.modules, module.__name__, module)
    config = {}
    payload = {"job_id": 17, "site_id": 23, "resign_today": "2026-11-02",
               "base_url": "https://site.test/", "resign_unresolved_dates": []}
    progress = AsyncMock()
    page = object()

    async def check():
        runtime = None
        for _ in range(2):
            result = await executor._run_step(
                page, step("resign", "post.resign_loop", config=config),
                None, None, None, None, None, "general", resign_pending_dates=DATES,
                secret_payload=payload, resign_progress=progress,
            )
            assert result == runner.return_value
            args, kwargs = runner.await_args
            assert args[:3] == (page, config, DATES)
            assert kwargs["progress"] is progress
            if runtime is None:
                runtime = kwargs["runtime"]
                runtime["sentinel"] = "retained"
            assert kwargs["runtime"] is runtime
            assert payload["_resign_runtime"]["resign"] is runtime
            assert runtime["sentinel"] == "retained"
            assert "unresolved" not in kwargs
        assert runner.await_count == 2

    asyncio.run(check())
