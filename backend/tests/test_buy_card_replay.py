from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.models.execution import JobStep, Run, SiteJob
from app.schemas.execution import RunCreate
from app.services.execution import ExecutionService
from app.services.executor import ExecutionManager
from tests.test_worker_navigation import _JumpLane, _JumpManager, _JumpPage
from worker import executor as worker_executor


pytestmark = pytest.mark.usefixtures("fake_dispatch")

PURCHASES = [
    pytest.param({"bought": 1}, True, id="bought-without-attempt-flag"),
    pytest.param({"payment_attempted": False, "bought": 1}, True, id="bought"),
    pytest.param({"payment_attempted": True, "bought": 0}, True, id="payment-unconfirmed"),
    pytest.param(
        {"payment_attempted": False, "bought": 0, "reason": "insufficient_points"},
        False,
        id="insufficient-points",
    ),
    pytest.param({"bought": 0}, False, id="no-payment-evidence"),
]


def _step(key, feature="action.click", **extra):
    return {
        "step_key": key,
        "feature_type": feature,
        "enabled": True,
        "condition": "always",
        "config": {},
        **extra,
    }


@pytest.fixture
def run_worker(monkeypatch, tmp_path):
    monkeypatch.setattr(worker_executor, "ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(worker_executor.JobArtifacts, "capture_failure", AsyncMock())
    monkeypatch.setattr(worker_executor, "_export_site_cookies", AsyncMock(return_value=[]))

    def run(steps, run_step):
        monkeypatch.setattr(worker_executor, "_run_step", run_step)
        lane = _JumpLane(_JumpPage())
        manager = _JumpManager(lane)
        result = asyncio.run(worker_executor.execute_job(manager, {
            "job_id": 1,
            "run_id": 1,
            "lane_key": "general",
            "site_name": "buy-card-replay",
            "steps": steps,
        }))
        assert manager.released
        assert lane.task_pages == {}
        return result

    return run


@pytest.mark.parametrize("status", ["restart", "fallback_interaction"])
@pytest.mark.parametrize("purchase,paid", PURCHASES)
def test_worker_retry_after_purchase(run_worker, status, purchase, paid):
    calls = []

    async def run_step(_page, step, *_args, **_kwargs):
        key = step["step_key"]
        calls.append(key)
        if key == "buy":
            return "success", "purchase checked", {"buy_card": purchase}
        if key == "unpaid":
            return "success", "insufficient points", {
                "buy_card": {"payment_attempted": False, "bought": 0},
            }
        assert key == "after"
        return status, "retry requested", {}

    result = run_worker([
        _step("buy", "post.buy_card"),
        _step("unpaid", "post.buy_card"),
        _step("after", "challenge.turnstile", on_error="continue"),
        _step("tail"),
    ], run_step)

    expected = "waiting_human" if paid else status
    assert result["status"] == expected
    assert calls == ["buy", "unpaid", "after"]
    assert [row["step_key"] for row in result["steps"]] == calls
    assert result["steps"][0]["data"]["buy_card"] == purchase
    assert result["steps"][-1]["status"] == expected
    assert result["message"] == result["steps"][-1]["message"]


@pytest.mark.parametrize("status", ["restart", "fallback_interaction"])
@pytest.mark.parametrize("purchase,paid", PURCHASES[:3])
def test_worker_purchase_itself_cannot_request_replay(run_worker, status, purchase, paid):
    run_step = AsyncMock(return_value=(status, "retry requested", {"buy_card": purchase}))
    result = run_worker([_step("buy", "post.buy_card"), _step("tail")], run_step)

    run_step.assert_awaited_once()
    assert result["status"] == "waiting_human"
    assert result["steps"][0]["status"] == "waiting_human"
    assert result["steps"][0]["data"]["buy_card"] == purchase


@pytest.mark.parametrize("target", ["before", "buy", "after"])
@pytest.mark.parametrize("purchase,paid", PURCHASES)
def test_worker_backward_jump_only_blocks_replayed_payment(run_worker, target, purchase, paid):
    calls = Counter()

    async def run_step(_page, step, *_args, **_kwargs):
        key = step["step_key"]
        calls[key] += 1
        if key == "buy":
            return "success", "purchase checked", {"buy_card": purchase}
        if key == "jump":
            return "success", "jump checked", {
                "matched": calls[key] == 1,
                "action": "jump",
                "target_step_key": target,
            }
        return "success", "ok", {}

    result = run_worker([
        _step("before"),
        _step("buy", "post.buy_card"),
        _step("after"),
        _step("jump", "flow.jump", config={"max_jumps": 1}),
        _step("tail"),
    ], run_step)

    blocked = paid and target != "after"
    assert result["status"] == ("waiting_human" if blocked else "success")
    assert calls["buy"] == (2 if not blocked and target != "after" else 1)
    assert calls["before"] == (2 if not blocked and target == "before" else 1)
    assert calls["after"] == (1 if blocked else 2)
    assert calls["jump"] == (1 if blocked else 2)
    assert calls["tail"] == (0 if blocked else 1)
    assert [row["step_key"] for row in result["steps"]] == [
        "before", "buy", "after", "jump", *([] if blocked else ["tail"]),
    ]
    assert result["steps"][1]["data"]["buy_card"] == purchase
    assert result["steps"][3]["status"] == ("waiting_human" if blocked else "success")
    assert [row["step_key"] for row in result["outputs"]] == ["buy"]


@pytest.mark.parametrize("status", ["restart", "fallback_interaction"])
@pytest.mark.parametrize("purchase,paid", PURCHASES)
def test_backend_retry_checks_worker_payment_evidence(client, monkeypatch, status, purchase, paid):
    response = client.post("/api/v1/sites", json={
        "name": "buy-card-replay",
        "base_url": "https://pt.example.com",
        "lane_policy": "general",
        "steps": [
            _step("open", "flow.open"),
            _step("buy", "post.buy_card"),
            _step("unpaid", "post.buy_card"),
            _step("after", "challenge.turnstile"),
        ],
    })
    assert response.status_code == 201, response.text
    app = client.app
    manager = ExecutionManager(
        app.state.session_factory, app.state.settings, app.state.crypto,
    )
    monkeypatch.setattr(manager, "schedule_run_notification", lambda _run_id: None)
    with app.state.session_factory() as session:
        run = ExecutionService(session, app.state.settings, app.state.crypto).create_run(
            RunCreate(site_ids=[response.json()["id"]]), "admin",
        )
        run_id = run.id
        job_id = session.query(SiteJob).filter_by(run_id=run_id).one().id

    rows = [
        {**_step("open", "flow.open"), "status": "success", "data": {}},
        {**_step("buy", "post.buy_card"), "status": "success", "data": {"buy_card": purchase}},
        {
            **_step("unpaid", "post.buy_card"),
            "status": "success",
            "data": {"buy_card": {"payment_attempted": False, "bought": 0}},
        },
        {**_step("after", "challenge.turnstile"), "status": status, "data": {}},
    ]
    calls = []
    retry_attempts = 3 if status == "restart" else 2

    async def fake_execute(payload):
        calls.append((payload["attempt"], payload["lane_key"]))
        with app.state.session_factory() as session:
            # A legitimate retry clears the previous attempt's progress before dispatch.
            assert session.query(JobStep).filter_by(site_job_id=job_id).count() == 0
        if len(calls) == 1:
            manager._persist_progress(job_id, {"status": "running", "steps": rows})
        if len(calls) < retry_attempts:
            # Return the raw retry status even after payment, simulating an older worker.
            return {"status": status, "message": "retry requested", "steps": rows}
        return {
            "status": "success", "message": "done",
            "steps": [{**row, "status": "success"} for row in rows],
        }

    post_execute = AsyncMock(side_effect=fake_execute)
    monkeypatch.setattr(manager, "_post_execute", post_execute)
    asyncio.run(manager._execute_run(run_id))

    expected_attempts = 1 if paid else retry_attempts
    assert post_execute.await_count == expected_attempts
    assert calls == [
        (attempt, "interaction" if status == "fallback_interaction" and attempt > 1 else "general")
        for attempt in range(1, expected_attempts + 1)
    ]
    with app.state.session_factory() as session:
        job = session.get(SiteJob, job_id)
        assert job.attempt == expected_attempts
        assert job.lane == calls[-1][1]
        assert job.status == ("waiting_human" if paid else "success")
        assert session.get(Run, run_id).status == job.status
        assert job.finished_at is not None
        assert json.loads(job.result_json)["step_count"] == len(rows)
        saved = session.query(JobStep).filter_by(site_job_id=job_id).order_by(JobStep.id).all()
        assert [row.step_key for row in saved] == [row["step_key"] for row in rows]
        assert saved[1].status == "success"
        assert json.loads(saved[1].data_json)["buy_card"] == purchase
