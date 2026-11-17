from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.models.execution import Run, SiteJob
from app.schemas.execution import RunCreate
from app.services.execution import ExecutionService
from app.services.executor import ExecutionManager
from app.services.integrations import IntegrationService

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from worker import executor, grid_captcha, vision

RESULT = "flow.result"
GRID = "challenge.grid_captcha"
TEXT = {"base_url": "https://text.example/v1", "api_key": "private-text-key",
        "model": "text-model", "supports_vision": False, "timeout": 11}
IMAGE = {"base_url": "https://image.example/v1", "api_key": "private-image-key",
         "model": "image-model", "supports_vision": True, "timeout": 73}
LEGACY = {"base_url": "https://legacy.example/v1", "api_key": "private-legacy-key",
          "model": "legacy-model", "supports_vision": True, "timeout": 999}
BEHAVIOR = {"llm_enabled": True, "use_vision": False, "judgment_scope": "all",
            "model": "provider-model-override"}


def backend_job(client, steps, behavior=None):
    if behavior is not None:
        response = client.put(f"/api/v1/features/{RESULT}/settings", json={
            "defaults": {}, "required_fields": [], "execution_settings": behavior,
        })
        assert response.status_code == 200, response.text
    response = client.post("/api/v1/sites", json={
        "name": "routing", "base_url": "https://site.example", "steps": steps,
    })
    assert response.status_code == 201, response.text
    app = client.app
    with app.state.session_factory() as session:
        run = ExecutionService(session, app.state.settings, app.state.crypto).create_run(
            RunCreate(site_ids=[response.json()["id"]]), "admin")
        run_id = run.id
    manager = ExecutionManager(app.state.session_factory, app.state.settings, app.state.crypto)
    manager._post_execute = AsyncMock(return_value={"status": "success", "steps": []})
    asyncio.run(manager._execute_run(run_id))
    return manager, run_id


def registry(client, *, enabled=True, usages=None):
    response = client.put("/api/v1/integrations/models", json={
        "expected_revision": 0, "items": [
            {"id": str(uuid4()), "name": name, "enabled": enabled,
             "usages": ["feature_cards"] if usages is None else usages,
             "config": {k: v for k, v in runtime.items() if k != "timeout"}}
            for name, runtime in (("Text", TEXT), ("Image", IMAGE))
        ] + [{"id": "primary", "name": "Primary", "enabled": False, "usages": []}],
    })
    assert response.status_code == 200, response.text


def test_backend_routes_text_and_image_in_same_job(client):
    registry(client)
    manager, _ = backend_job(client, [
        {"step_key": "result", "feature_type": RESULT, "config": {"llm_enabled": True}},
        {"step_key": "grid", "feature_type": GRID},
        {"step_key": "grid-again", "feature_type": GRID},
    ], BEHAVIOR)
    manager._post_execute.assert_awaited_once()
    payload = manager._post_execute.call_args.args[0]
    runtimes = payload["feature_model_runtimes"]
    assert set(runtimes) == {RESULT, GRID}
    assert runtimes[RESULT] == {**TEXT, "timeout": 30}
    assert runtimes[GRID] == {**IMAGE, "timeout": 30}
    assert payload["vision"] is None
    assert payload["feature_execution_settings"][RESULT]["model"] == BEHAVIOR["model"]
    assert "private-text-key" not in json.dumps(payload["steps"])
    assert "private-image-key" not in json.dumps(payload["feature_execution_settings"])


@pytest.mark.parametrize("feature,config,enabled,behavior,expected", [
    (GRID, {}, True, None, True),
    (GRID, {}, False, None, None),
    ("sign.image_quiz", {}, True, None, True),
    ("challenge.image_captcha", {"captcha_method": "vision"}, True, None, True),
    ("sign.iframe.image_captcha", {"captcha_method": "vision"}, True, None, True),
    ("challenge.image_captcha", {"captcha_method": "ddddocr"}, True, None, None),
    ("challenge.image_captcha", {"captcha_method": "nexusphp_gd5"}, True, None, None),
    ("challenge.image_captcha", {"captcha_method": "vision"}, False, None, None),
    (RESULT, {"llm_enabled": True}, True, BEHAVIOR, False),
    (RESULT, {"llm_enabled": True}, True, {**BEHAVIOR, "use_vision": True}, True),
    (RESULT, {"llm_enabled": True}, True, {**BEHAVIOR, "llm_enabled": False}, None),
    (RESULT, {"llm_enabled": False}, True, BEHAVIOR, None),
    (RESULT, {"llm_enabled": True, "output_value": False}, True,
     {**BEHAVIOR, "judgment_scope": "success_only"}, None),
])
def test_backend_capabilities_and_gates(client, monkeypatch, feature, config, enabled,
                                       behavior, expected):
    select = Mock(side_effect=lambda _self, usage, **kw: IMAGE if kw["requires_vision"] else TEXT)
    monkeypatch.setattr(IntegrationService, "model_runtime", lambda self, *a, **kw: select(self, *a, **kw))
    manager, _ = backend_job(client, [
        {"step_key": "step", "feature_type": feature, "config": config, "enabled": enabled},
    ], behavior)
    payload = manager._post_execute.call_args.args[0]
    if expected is None:
        select.assert_not_called()
        assert payload["feature_model_runtimes"] == {}
    else:
        assert select.call_count == 1
        assert select.call_args.args[1] == "feature_cards"
        assert select.call_args.kwargs == {"requires_vision": expected}
        assert payload["feature_model_runtimes"] == {feature: IMAGE if expected else TEXT}


@pytest.mark.parametrize("enabled,usages", [(False, ["feature_cards"]), (True, ["assistant"])])
def test_backend_no_eligible_model_dispatches_unavailable(client, enabled, usages):
    registry(client, enabled=enabled, usages=usages)
    manager, _ = backend_job(client, [{"step_key": "grid", "feature_type": GRID}])
    assert manager._post_execute.call_args.args[0]["feature_model_runtimes"] == {GRID: None}


def test_backend_corrupt_registry_fails_closed(client):
    from app.services.model_registry import REGISTRY_KEY

    with client.app.state.session_factory() as session:
        IntegrationService(session, client.app.state.settings, client.app.state.crypto)._upsert(
            REGISTRY_KEY, '{"private-corrupt-field": true}', "test")
        session.commit()
    manager, run_id = backend_job(client, [{"step_key": "grid", "feature_type": GRID}])
    manager._post_execute.assert_not_awaited()
    with client.app.state.session_factory() as session:
        job = session.query(SiteJob).filter_by(run_id=run_id).one()
        assert job.status == "fail"
        assert json.loads(job.result_json) == {"reason": "model_registry_unavailable"}
        assert "private-corrupt-field" not in job.message
        assert session.get(Run, run_id).status == "fail"


@pytest.fixture
def worker_job(monkeypatch, tmp_path):
    monkeypatch.setattr(executor, "ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(executor.JobArtifacts, "capture_failure", AsyncMock())
    page = SimpleNamespace(
        url="https://site.example/", on=Mock(), remove_listener=Mock(),
        bring_to_front=AsyncMock(), close=AsyncMock(),
        evaluate=AsyncMock(return_value="visible evidence"), screenshot=AsyncMock(return_value=b"image"),
    )
    lane = SimpleNamespace(execution_semaphore=asyncio.Semaphore(1), task_pages={}, page=None,
                           context=SimpleNamespace(new_page=AsyncMock(return_value=page)))
    manager = SimpleNamespace(get=Mock(return_value=lane), release_lane=AsyncMock(),
                              lease_lane=AsyncMock(return_value=("test-lease", {})))
    progress = []

    async def record(event):
        progress.append(copy.deepcopy(event))

    def run(**payload):
        result = asyncio.run(executor.execute_job(manager, {
            "job_id": 1, "run_id": 1, "vision": LEGACY,
            "feature_execution_settings": {RESULT: BEHAVIOR, GRID: {"llm_prompt": "grid prompt"}},
            "steps": [{"step_key": "result", "feature_type": RESULT,
                       "config": {"llm_enabled": True}}], **payload,
        }, on_step=record))
        log_path = tmp_path / "run-1" / "job-1" / "job.log"
        return result, progress, log_path.read_text() if log_path.exists() else ""

    return SimpleNamespace(run=run, page=page, manager=manager)


@pytest.mark.parametrize("routing", [None, {}, {RESULT: None}, {GRID: IMAGE}])
def test_worker_legacy_fallback_only_when_map_absent(worker_job, monkeypatch, routing):
    judge = AsyncMock(return_value={"result": True, "reason": "visible evidence"})
    monkeypatch.setattr(vision, "judge_result", judge)
    payload = {} if routing is None else {"feature_model_runtimes": routing}
    result, progress, log = worker_job.run(**payload)
    if routing is None:
        assert result["status"] == "success"
        assert judge.await_args.args[0]["api_key"] == LEGACY["api_key"]
        assert judge.await_args.args[0]["model"] == BEHAVIOR["model"]
    else:
        assert result["status"] == "fail"
        assert result["steps"][0]["data"] == {"reason": "model_unavailable"}
        judge.assert_not_awaited()
        worker_job.page.evaluate.assert_not_awaited()
    for secret in (LEGACY["api_key"], LEGACY["base_url"], LEGACY["model"]):
        assert secret not in repr((result, progress)) + log


@pytest.mark.parametrize("routing", [None, [], "private-map", {RESULT: "private-entry"}])
def test_worker_rejects_malformed_map_without_logging_values(worker_job, routing):
    result, progress, log = worker_job.run(feature_model_runtimes=routing)
    assert result == {"status": "fail", "message": "Invalid feature model runtimes",
                      "steps": [], "reason": "model_runtime_invalid"}
    worker_job.manager.lease_lane.assert_not_awaited()
    assert "private-map" not in repr((result, progress)) + log
    assert "private-entry" not in repr((result, progress)) + log


@pytest.mark.parametrize("explicit_timeout", [None, 9])
def test_worker_routes_each_step_timeout_settings_and_redacts_entire_map(
    worker_job, monkeypatch, explicit_timeout,
):
    judge = AsyncMock(return_value={
        "result": True, "reason": "visible evidence " + " ".join(
            runtime[key] for runtime in (TEXT, IMAGE, LEGACY)
            for key in ("api_key", "base_url", "model")),
    })
    grid = AsyncMock(return_value=("success", "grid complete", {}))
    monkeypatch.setattr(vision, "judge_result", judge)
    monkeypatch.setattr(grid_captcha, "run_grid_captcha", grid)
    original = executor.asyncio.wait_for
    budgets = []

    async def wait_for(coro, timeout):
        if getattr(coro, "cr_code", None) is executor._run_step.__code__:
            budgets.append(timeout)
        return await original(coro, timeout=timeout)

    monkeypatch.setattr(executor.asyncio, "wait_for", wait_for)
    runtimes = {RESULT: dict(TEXT), GRID: dict(IMAGE)}
    result, progress, log = worker_job.run(feature_model_runtimes=runtimes, steps=[
        {"step_key": "grid", "feature_type": GRID,
         "timeout_seconds": explicit_timeout,
         "config": {"max_refresh": 2, "success_wait_seconds": 7}},
        {"step_key": "result", "feature_type": RESULT, "timeout_seconds": explicit_timeout,
         "config": {"llm_enabled": True}},
    ])
    assert result["status"] == "success"
    assert budgets == ([330, 46] if explicit_timeout is None else [9, 9])
    selected_grid = grid.await_args.args[3]
    assert selected_grid["api_key"] == IMAGE["api_key"]
    assert selected_grid["model"] == IMAGE["model"]
    assert selected_grid["execution_settings"] == {"llm_prompt": "grid prompt"}
    selected_result = judge.await_args.args[0]
    assert selected_result["api_key"] == TEXT["api_key"]
    assert selected_result["model"] == BEHAVIOR["model"]
    assert selected_result["execution_settings"] == BEHAVIOR
    assert runtimes == {RESULT: TEXT, GRID: IMAGE}
    assert judge.await_count == grid.await_count == 1
    for runtime in (TEXT, IMAGE, LEGACY):
        for key in ("api_key", "base_url", "model"):
            assert runtime[key] not in repr((result, progress)) + log


def test_worker_freezes_runtime_map_for_entire_job(worker_job, monkeypatch):
    runtimes = {RESULT: dict(TEXT), GRID: dict(IMAGE)}
    selected = []

    async def run_step(*args, **kwargs):
        selected.append(copy.deepcopy(args[10]))
        runtimes[RESULT]["api_key"] = "changed-after-dispatch"
        return "success", "complete", {}

    monkeypatch.setattr(executor, "_run_step", run_step)
    result, _, _ = worker_job.run(feature_model_runtimes=runtimes, steps=[
        {"step_key": "grid", "feature_type": GRID, "config": {}},
        {"step_key": "result", "feature_type": RESULT, "config": {"llm_enabled": True}},
    ])
    assert result["status"] == "success"
    assert selected[0]["api_key"] == IMAGE["api_key"]
    assert selected[1]["api_key"] == TEXT["api_key"]


@pytest.mark.parametrize("routing", [{}, {GRID: None}, {RESULT: TEXT}])
def test_worker_missing_grid_runtime_waits_without_legacy_fallback(worker_job, routing):
    result, progress, log = worker_job.run(feature_model_runtimes=routing, steps=[
        {"step_key": "grid", "feature_type": GRID, "config": {}},
    ])
    assert result["status"] == "waiting_human"
    assert result["steps"][0]["data"]["reason"] == "vision_unavailable"
    assert LEGACY["api_key"] not in repr((result, progress)) + log


def test_worker_selected_provider_error_never_falls_back(worker_job, monkeypatch):
    judge = AsyncMock(side_effect=vision.ResultJudgmentError("http_error", "request", 503))
    monkeypatch.setattr(vision, "judge_result", judge)
    result, _, log = worker_job.run(feature_model_runtimes={RESULT: TEXT, GRID: IMAGE})
    assert result["status"] == "fail"
    assert result["steps"][0]["data"]["provider_error"] == "http_error"
    judge.assert_awaited_once()
    assert judge.await_args.args[0]["api_key"] == TEXT["api_key"]
    assert LEGACY["api_key"] not in log
