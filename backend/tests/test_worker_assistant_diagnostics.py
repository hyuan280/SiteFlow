from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker import executor  # noqa: E402
from tests.assistant_consumption_helpers import approved_consumption_payload  # noqa: F401, E402


PRIVATE = "private-password-and-selector-value"


@pytest.fixture
def job(monkeypatch, tmp_path, approved_consumption_payload):
    monkeypatch.setattr(executor, "ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(executor.JobArtifacts, "capture_failure", AsyncMock())
    page = SimpleNamespace(
        url="https://site.test/", on=Mock(), remove_listener=Mock(),
        bring_to_front=AsyncMock(), close=AsyncMock(),
    )
    lane = SimpleNamespace(
        execution_semaphore=asyncio.Semaphore(1), task_pages={}, page=None,
        context=SimpleNamespace(new_page=AsyncMock(return_value=page)),
    )
    manager = SimpleNamespace(
        get=Mock(return_value=lane),
        lease_lane=AsyncMock(return_value=("test-lease", {"browser": "fake"})),
        release_lane=AsyncMock(),
    )
    progress = AsyncMock()

    def run(feature="action.click", *, assistant=True, policy="stop", approved=True, **step_options):
        payload = {
            "job_id": 1, "run_id": 1, "lane_key": "general", "base_url": page.url,
            "assistant_run": assistant,
            "steps": [
                {"step_key": "active", "feature_type": feature, "on_error": policy,
                 "config": {}, **step_options},
                {"step_key": "must-not-run", "feature_type": "flow.result",
                 "config": {"output_value": True}},
            ],
        }
        if approved:
            payload = approved_consumption_payload(payload)
        result = asyncio.run(executor.execute_job(manager, payload, on_step=progress))
        if not approved:
            page.close.assert_not_awaited()
            lane.context.new_page.assert_not_awaited()
            manager.lease_lane.assert_not_awaited()
            return result
        page.close.assert_awaited()
        assert lane.task_pages == {}
        manager.release_lane.assert_awaited_with("general", "test-lease")
        return result

    return SimpleNamespace(run=run, progress=progress, tmp_path=tmp_path)


SIDE_EFFECTS = [
    "auth.form_login", "auth.password_verify", "sign.button", "sign.image_quiz",
    "sign.iframe_form", "sign.unit3d_claim", "action.click", "action.fill",
    "post.buy_card", "post.resign", "post.resign_click", "post.resign_dialog", "challenge.2fa",
    "challenge.altcha", "challenge.slider", "challenge.image_captcha", "challenge.grid_captcha",
]


@pytest.mark.parametrize("feature", SIDE_EFFECTS)
@pytest.mark.parametrize("error,cause", [
    (TimeoutError(PRIVATE), "execution_timeout"),
    (RuntimeError(PRIVATE), "execution_error"),
])
@pytest.mark.parametrize("policy", ["stop", "continue", "defer", "escalate_interaction"])
def test_assistant_side_effect_exception_is_terminal_unknown(monkeypatch, job, feature, error, cause, policy):
    actions = []

    async def run_step(_page, step, *_args, **_kwargs):
        actions.append(step["step_key"])
        raise error

    monkeypatch.setattr(executor, "_run_step", run_step)
    result = job.run(feature, policy=policy)
    assert actions == ["active"]
    assert result["status"] == "unknown"
    assert len(result["steps"]) == 1
    step = result["steps"][0]
    assert step["status"] == "unknown"
    assert {key: step["data"][key] for key in ("reason", "cause", "stage")} == {
        "reason": "outcome_uncertain", "cause": cause, "stage": "step_execution",
    }
    assert "field" not in step["data"]
    assert job.progress.await_args.args[0]["status"] == "unknown"
    assert job.progress.await_args.args[0]["steps"][0]["data"] == step["data"]
    log = (job.tmp_path / "run-1" / "job-1" / "job.log").read_text()
    assert PRIVATE not in json.dumps(result) + repr(job.progress.await_args_list) + log


@pytest.mark.parametrize("assistant", [False, None, 1, "true"])
@pytest.mark.parametrize("feature", ["action.click", "post.buy_card", "post.resign_click"])
@pytest.mark.parametrize("error", [RuntimeError(PRIVATE), TimeoutError(PRIVATE)])
def test_normal_jobs_keep_existing_exception_status(monkeypatch, job, assistant, feature, error):
    run_step = AsyncMock(side_effect=error)
    monkeypatch.setattr(executor, "_run_step", run_step)
    result = job.run(feature, assistant=assistant)
    expected = "waiting_human" if isinstance(error, TimeoutError) and feature.startswith("post.") else "fail"
    if feature == "post.resign_click":
        expected = "waiting_human"
    assert result["status"] == result["steps"][0]["status"] == expected
    run_step.assert_awaited_once()


@pytest.mark.parametrize("feature", ["flow.open", "action.navigate", "action.wait", "flow.result", "plugin.demo.test"])
@pytest.mark.parametrize("error,reason", [
    (RuntimeError(PRIVATE), "execution_error"), (TimeoutError(PRIVATE), "execution_timeout"),
])
def test_read_only_and_excluded_features_fail_not_unknown(monkeypatch, job, feature, error, reason):
    monkeypatch.setattr(executor, "_run_step", AsyncMock(side_effect=error))
    result = job.run(feature)
    assert result["status"] == result["steps"][0]["status"] == "fail"
    assert result["steps"][0]["data"] == {"reason": reason, "stage": "step_execution"}
    assert PRIVATE not in repr(result)


def test_assistant_exception_is_not_formatted(monkeypatch, job):
    class UnsafeError(RuntimeError):
        def __str__(self):
            pytest.fail("Assistant errors must not be formatted")

    monkeypatch.setattr(executor, "_run_step", AsyncMock(side_effect=UnsafeError()))
    assert job.run()["status"] == "unknown"


def test_actual_deadline_after_action_is_not_replayed(monkeypatch, job):
    actions = []

    async def run_step(*_args, **_kwargs):
        actions.append("clicked")
        await asyncio.Event().wait()

    wait_for = asyncio.wait_for

    async def short_deadline(awaitable, timeout):
        return await wait_for(awaitable, 0.01)

    monkeypatch.setattr(executor, "_run_step", run_step)
    monkeypatch.setattr(executor.asyncio, "wait_for", short_deadline)
    result = job.run(policy="continue")
    assert actions == ["clicked"]
    assert result["status"] == "unknown"
    assert result["steps"][0]["data"]["cause"] == "execution_timeout"


def test_resign_checkpoint_survives_uncertain_exception(monkeypatch, job):
    async def run_step(*_args, **kwargs):
        await kwargs["resign_progress"]({"resign": {"submit_attempted": True}})
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(executor, "_run_step", run_step)
    result = job.run("post.resign_click")
    assert result["status"] == "unknown"
    assert result["steps"][0]["data"]["resign"] == {"submit_attempted": True}
    assert result["steps"][0]["data"]["reason"] == "outcome_uncertain"


@pytest.mark.parametrize("feature", ["post.buy_card", "post.resign", "post.resign_click", "post.resign_dialog"])
def test_unapproved_consuming_full_run_rejects_before_page_creation(monkeypatch, job, feature):
    run_step = AsyncMock()
    monkeypatch.setattr(executor, "_run_step", run_step)
    result = job.run(feature, approved=False)
    assert result["status"] == "fail"
    assert result["reason"] == "assistant_configuration_changed"
    assert result["steps"] == []
    run_step.assert_not_awaited()


@pytest.mark.parametrize("condition", ["false", "unknown"])
def test_uninvoked_side_effect_is_not_uncertain(monkeypatch, job, condition):
    monkeypatch.setattr(executor, "_condition_allows", AsyncMock(return_value=condition))
    run_step = AsyncMock()
    monkeypatch.setattr(executor, "_run_step", run_step)
    result = job.run()
    assert result["status"] == ("success" if condition == "false" else "waiting_human")
    assert all("reason" not in step["data"] for step in result["steps"])
    run_step.assert_not_awaited()


def test_existing_waiting_human_remains_waiting_human(monkeypatch, job):
    monkeypatch.setattr(executor, "_run_step", AsyncMock(return_value=("waiting_human", "review", {})))
    result = job.run(policy="continue")
    assert result["status"] == result["steps"][0]["status"] == "waiting_human"
    assert result["steps"][0]["data"] == {}


def run_step(feature, config=None, credentials=None):
    return asyncio.run(executor._run_step(
        SimpleNamespace(url="https://site.test/"),
        {"feature_type": feature, "config": config or {}},
        credentials, None, None, None, None, "general",
    ))


@pytest.mark.parametrize("feature,field,position", [
    ("auth.form_login", "username_selector", 0),
    ("auth.form_login", "password_selector", 1),
    ("auth.form_login", "submit_selector", 2),
    ("action.click", "selector", 0),
    ("action.fill", "selector", 0),
    ("sign.button", "button_selector", 0),
    ("sign.unit3d_claim", "claim_selector", 0),
])
def test_missing_selector_has_static_field_without_selector_value(monkeypatch, feature, field, position):
    finder = AsyncMock(side_effect=[*[object() for _ in range(position)], None])
    monkeypatch.setattr(executor, "_find_locator", finder)
    status, message, data = run_step(feature, {field: [PRIVATE]}, {"username": PRIVATE, "password": PRIVATE})
    assert status == "fail" and message
    assert data == {"reason": "selector_not_found", "field": field}
    assert PRIVATE not in repr((message, data))
    assert all(type(value) is str and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) for value in data.values())


def test_missing_credentials_is_actionable_without_browser_access():
    assert run_step("auth.form_login")[2] == {"reason": "credentials_unavailable"}


def test_invalid_config_has_static_input_diagnostic():
    result = run_step("action.click", [PRIVATE])
    assert result[0] == "fail"
    assert result[2] == {"reason": "input_validation_failed", "field": "config"}
    assert PRIVATE not in repr(result)


@pytest.mark.parametrize("success,found,expected_status,verified", [
    ([], False, "success", False),
    ([PRIVATE], True, "success", True),
    ([PRIVATE], False, "fail", False),
])
def test_login_reports_submission_and_actual_success_evidence(monkeypatch, success, found, expected_status, verified):
    username = SimpleNamespace(fill=AsyncMock(), input_value=AsyncMock(return_value="user"))
    password = SimpleNamespace(fill=AsyncMock(), input_value=AsyncMock(return_value=PRIVATE))
    submit = SimpleNamespace(click=AsyncMock())
    monkeypatch.setattr(executor, "_find_locator", AsyncMock(side_effect=[
        username, password, submit,
    ]))
    monkeypatch.setattr(executor, "_find_success_locator", AsyncMock(return_value=object() if found else None))
    monkeypatch.setattr(executor.asyncio, "sleep", AsyncMock())
    status, message, data = run_step("auth.form_login", {"success_selector": success},
                                     {"username": "user", "password": PRIVATE})
    assert status == expected_status
    assert data["submitted"] is True
    assert data["verified"] is verified
    if expected_status == "fail":
        assert data["reason"] == "success_evidence_missing"
        assert data["field"] == "success_selector"
    assert PRIVATE not in repr((message, data))
    submit.click.assert_awaited_once()


@pytest.mark.parametrize("timeout", [None, 7])
@pytest.mark.parametrize("phase", ["success_wait", "diagnostic"])
def test_login_readiness_does_not_extend_outer_deadline_or_replay(monkeypatch, job, timeout, phase):
    username = SimpleNamespace(fill=AsyncMock(), input_value=AsyncMock(return_value="user"))
    password = SimpleNamespace(fill=AsyncMock(), input_value=AsyncMock(return_value=PRIVATE))
    submit = SimpleNamespace(click=AsyncMock())
    monkeypatch.setattr(executor, "_find_locator", AsyncMock(side_effect=[username, password, submit]))
    monkeypatch.setattr("worker.secret_client.resolve_secret", AsyncMock(
        return_value={"username": "user", "password": PRIVATE},
    ))

    async def wait_for_evidence(*_args, **_kwargs):
        await asyncio.Event().wait()

    probe = AsyncMock(side_effect=wait_for_evidence) if phase == "success_wait" else AsyncMock(return_value=None)
    diagnostic = AsyncMock(side_effect=wait_for_evidence)
    monkeypatch.setattr(executor, "_find_success_locator", probe)
    monkeypatch.setattr(executor, "_login_success_evidence", diagnostic)
    wait_for = asyncio.wait_for
    deadlines = []

    async def short_deadline(awaitable, timeout):
        deadlines.append(timeout)
        return await wait_for(awaitable, 0.02)

    monkeypatch.setattr(executor.asyncio, "wait_for", short_deadline)
    result = job.run("auth.form_login", timeout_seconds=timeout, policy="continue", config={
        "success_selector": [".account"], "success_wait_seconds": 120,
    })
    assert deadlines == [30 if timeout is None else timeout]
    assert result["status"] == "unknown" and len(result["steps"]) == 1
    assert result["steps"][0]["data"]["cause"] == "execution_timeout"
    assert probe.await_args.kwargs["wait_ms"] == 120000
    if phase == "diagnostic":
        diagnostic.assert_awaited_once()
    else:
        diagnostic.assert_not_awaited()
    assert PRIVATE not in repr(result)
    submit.click.assert_awaited_once()


@pytest.mark.parametrize("field", ["username_selector", "password_selector"])
def test_login_input_validation_has_safe_metadata(monkeypatch, job, field):
    username = SimpleNamespace(fill=AsyncMock(), input_value=AsyncMock(return_value="" if field == "username_selector" else "user"))
    password = SimpleNamespace(fill=AsyncMock(), input_value=AsyncMock(return_value=""))
    submit = SimpleNamespace(click=AsyncMock())
    monkeypatch.setattr(executor, "_find_locator", AsyncMock(side_effect=[username, password, submit]))
    monkeypatch.setattr("worker.secret_client.resolve_secret", AsyncMock(return_value={"username": "user", "password": PRIVATE}))
    result = job.run("auth.form_login")
    assert result["status"] == "unknown"
    assert result["steps"][0]["data"] == {
        "reason": "outcome_uncertain", "cause": "execution_error",
        "stage": "step_execution", "field": field,
    }
    submit.click.assert_not_awaited()
    assert PRIVATE not in repr(result)


def test_unknown_return_cannot_become_success_via_continue(monkeypatch, job):
    run = AsyncMock(return_value=("unknown", "no evidence", {"reason": "success_evidence_missing"}))
    monkeypatch.setattr(executor, "_run_step", run)
    assert job.run("sign.button", policy="continue")["status"] == "unknown"
    run.assert_awaited_once()
