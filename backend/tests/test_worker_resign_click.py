from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker import executor
from worker import resign_checkpoint
from worker.resign_click import resign_click_timeout, run_resign_click


BASE = "http://127.0.0.1:18765"
TODAY = "2026-11-02"
DATE = "2026-10-01"
EXECUTABLE = os.environ.get("RESIGN_TEST_BROWSER", "/opt/opencode/.cache/ms-playwright/"
                            "chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell")


@asynccontextmanager
async def calendar(options=None):
    from playwright.async_api import async_playwright

    if not Path(EXECUTABLE).is_file():
        pytest.skip("isolated Chromium executable unavailable")
    html = Path(__file__).with_name("resign_click_fixture.html").read_text()
    html = html.replace("FIXTURE_OPTIONS", json.dumps(options or {}))
    posts = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=EXECUTABLE, headless=True)
        context = await browser.new_context(service_workers="block")

        async def route_local(route):
            assert route.request.url.startswith(BASE + "/"), "external request blocked by fixture"
            if route.request.url == BASE + "/submit":
                posts.append(route.request.post_data)
                await route.fulfill(body="ok")
            else:
                await route.fulfill(content_type="text/html; charset=utf-8", body=html)

        await context.route("**/*", route_local)
        page = await context.new_page()
        try:
            yield page, posts
        finally:
            await context.close()
            await browser.close()


async def run(page, dates=None, config=None, **kwargs):
    return await run_resign_click(
        page, {"success_wait_seconds": 1, **(config or {})},
        dates if dates is not None else [DATE], lambda *_: None,
        today=kwargs.pop("today", TODAY), base_url=BASE,
        durable_progress=kwargs.pop("durable_progress", AsyncMock()), **kwargs,
    )


def test_exact_date_adjacent_month_and_checkpoint():
    async def check():
        async with calendar({"stockDelay": 200, "readyDelay": 200}) as (page, posts):
            snapshots = []

            async def progress(data):
                snapshots.append(copy.deepcopy(data["resign"]))
                if data["resign"]["unconfirmed"]:
                    assert not posts

            status, message, data = await run(page, progress=progress)
            state = data["resign"]
            assert status == "success", message
            assert posts == [DATE]
            assert [row["date"] for row in state["success"]] == [DATE]
            assert state["already_signed"] == []  # Adjacent month's day 1 is done.
            assert (state["cards_before"], state["cards_after"]) == (3, 2)
            assert state["remaining"] == state["unconfirmed"] == []
            assert state["submit_attempted"]
            assert any(row["unconfirmed"] and row["submit_attempted"] for row in snapshots)
            assert snapshots[-1]["success"] == state["success"]

    asyncio.run(check())


@pytest.mark.parametrize("dates,today", [
    ([TODAY], TODAY), (["2026-11-03"], TODAY), ([DATE, DATE], TODAY),
    (["2026-02-30"], TODAY), (["2026-1-01"], TODAY), ([DATE], None),
])
def test_invalid_dates_fail_closed_without_browser(dates, today):
    page = SimpleNamespace(goto=AsyncMock())
    result = asyncio.run(run(page, dates, today=today))
    assert result[0] == "waiting_human"
    assert result[2]["resign"]["submit_attempted"] is False
    page.goto.assert_not_awaited()


@pytest.mark.parametrize("options,config", [
    ({"ambiguous": True}, {}), ({"ambiguousTitle": True}, {}),
    ({"ambiguousButton": True}, {}), ({"cards": -1}, {}),
    ({"signed": [DATE], "duplicateDone": True}, {}),
    ({"contradiction": True}, {}), ({"ariaConflict": True}, {}),
    ({"wrongSelected": True}, {}), ({"changeMonth": True}, {}),
    ({"button": "今日签到"}, {}), ({"button": ""}, {}),
    ({"target": "_blank"}, {}), ({"action": "https://invalid.example/submit"}, {}),
    ({"stuckMonth": True, "month": "2026-11"}, {}),
    ({"month": "2026-11"}, {"max_month_jumps": 0}),
    ({}, {"attendance_url": "https://invalid.example/calendar"}),
    ({}, {"calendar_day_selector": ['[data-date="{unknown}"]']}),
    ({}, {"calendar_day_selector": ['.cal-grid > button.cal-cell:has(.cal-num:text-is("{day}"))']}),
])
def test_unsafe_observations_never_submit(options, config):
    async def check():
        async with calendar(options) as (page, posts):
            status, _, data = await run(page, config=config)
            assert status == "waiting_human", data
            assert not posts
            assert not data["resign"]["submit_attempted"]

    asyncio.run(check())


@pytest.mark.parametrize("options,already", [({"signed": [DATE]}, True), ({"cards": 0}, False)])
def test_done_before_or_zero_stock_does_not_consume(options, already):
    async def check():
        async with calendar(options) as (page, posts):
            status, _, data = await run(page)
            state = data["resign"]
            assert status == "success" and not posts
            assert state["already_signed"] == ([DATE] if already else [])
            assert state["remaining"] == ([] if already else [DATE])
            assert state["submit_attempted"] is False

    asyncio.run(check())


def test_month_direction_recomputed_and_templates():
    async def check():
        async with calendar({"month": "2026-11"}) as (page, posts):
            dates = ["2026-09-01", DATE]
            status, message, data = await run(page, dates, config={
                "calendar_day_selector": ['[data-date="{date}"]:not(.mute)'],
                "calendar_selected_selector": ['[data-date="{year}-{month}-01"].on',
                                               '[data-date="{date}"].on'],
            })
            assert status == "success", message
            assert posts == dates
            assert await page.evaluate("flips") == [0, 0, 1]
            assert data["resign"]["cards_after"] == 1

    asyncio.run(check())


@pytest.mark.parametrize("is_done", [False, True])
def test_unresolved_targets_checked_read_only_before_any_submission(is_done):
    async def check():
        unknown = "2026-10-02"
        async with calendar({"signed": [unknown] if is_done else []}) as (page, posts):
            status, _, data = await run(page, [DATE, unknown], unresolved=[unknown])
            assert status == ("success" if is_done else "waiting_human")
            assert posts == ([DATE] if is_done else [])
            assert data["resign"]["already_signed"] == ([unknown] if is_done else [])

    asyncio.run(check())


@pytest.mark.parametrize("options", [
    {"noResult": True}, {"delta": 0}, {"delta": 2}, {"postBusy": True}, {"dialog": True},
])
def test_unknown_result_never_reclicks(options):
    async def check():
        async with calendar(options) as (page, posts):
            status, _, data = await run(page, [DATE, "2026-10-02"])
            state = data["resign"]
            assert status == "waiting_human"
            assert await page.evaluate("clicks") == [DATE]
            assert posts == ([] if options.get("dialog") else [DATE])
            assert state["success"] == []
            assert state["unconfirmed"][0]["date"] == DATE
            assert state["remaining"] == [DATE, "2026-10-02"]

    asyncio.run(check())


def test_click_error_after_dispatch_can_still_confirm(monkeypatch):
    from playwright.async_api import ElementHandle

    original = ElementHandle.click

    async def click(handle, **kwargs):
        submit = "补签" in await handle.inner_text()
        await original(handle, **kwargs)
        if submit:
            raise RuntimeError("transport lost after dispatch")

    monkeypatch.setattr(ElementHandle, "click", click)

    async def check():
        async with calendar() as (page, posts):
            status, message, data = await run(page)
            assert status == "success", message
            assert posts == [DATE]
            assert data["resign"]["success"][0]["date"] == DATE

    asyncio.run(check())


def test_numeric_day_fallback_and_playwright_done_selector():
    async def check():
        async with calendar({"noDates": True}) as (page, posts):
            status, message, _ = await run(page, config={
                "done_selector": ['.cal-badge.att:text-is("done")'], "card_count_selector": [],
            })
            assert status == "success", message
            assert posts == [DATE]

    asyncio.run(check())


@pytest.mark.parametrize("mutation", ["month", "selected", "button", "stock", "target", "action"])
def test_input_lock_wait_revalidates_before_submit(monkeypatch, mutation):
    from worker import resign_click

    async def check():
        async with calendar() as (page, posts):
            holds = 0

            @asynccontextmanager
            async def hold(_page):
                nonlocal holds
                holds += 1
                if holds == 2:  # Date selection completed; submission gate was delayed.
                    await page.evaluate("""kind => {
                        if (kind === 'month') document.querySelector('.cal-water').textContent = '2026年9月';
                        if (kind === 'selected') document.querySelector('.on').classList.remove('on');
                        if (kind === 'button') {
                            const button = document.querySelector('.cal-att button');
                            button.replaceWith(button.cloneNode(true));
                        }
                        if (kind === 'stock') document.querySelector('.cal-help').textContent = '补签卡（当前 2 张）';
                        if (kind === 'target') document.querySelector('.cal-att button').setAttribute('target', '_blank');
                        if (kind === 'action') document.querySelector('.cal-att button').setAttribute('formaction', 'https://invalid.example');
                    }""", mutation)
                yield

            monkeypatch.setattr(resign_click, "hold_page_input", hold)
            status, _, data = await run(page)
            assert status == "waiting_human"
            assert posts == []
            assert data["resign"]["submit_attempted"] is False

    asyncio.run(check())


def test_checkpoint_yield_revalidates_before_click():
    async def check():
        async with calendar() as (page, posts):
            async def progress(data):
                if data["resign"]["unconfirmed"]:
                    await page.locator('.cal-att button').evaluate("el => el.textContent = '今日签到'")

            status, _, data = await run(page, progress=progress)
            assert status == "waiting_human"
            assert posts == []
            assert data["resign"]["submit_attempted"] is True
            assert data["resign"]["unconfirmed"][0]["date"] == DATE

    asyncio.run(check())


def test_cancel_preserves_confirmed_and_unconfirmed_dates():
    async def check():
        async with calendar() as (page, posts):
            reached = asyncio.Event()
            snapshots = []

            async def progress(data):
                snapshots.append(copy.deepcopy(data))
                if data["resign"]["unconfirmed"] and data["resign"]["success"] and not reached.is_set():
                    reached.set()
                    await asyncio.Event().wait()

            task = asyncio.create_task(run(page, [DATE, "2026-10-02"], progress=progress))
            await asyncio.wait_for(reached.wait(), 5)
            task.cancel()
            status, _, data = await asyncio.wait_for(task, 2)
            assert posts == [DATE]
            assert status == "waiting_human"
            assert data["resign"]["success"][0]["date"] == DATE
            assert data["resign"]["unconfirmed"][0]["date"] == "2026-10-02"
            assert snapshots[-1] == data

    asyncio.run(check())


def test_real_runner_timeout_preserves_dispatch_without_reclick():
    async def check():
        async with calendar({"noResult": True}) as (page, posts):
            submitted = asyncio.Event()

            async def progress(data):
                if data["resign"]["unconfirmed"]:
                    submitted.set()

            task = asyncio.create_task(run(page, config={"success_wait_seconds": 10}, progress=progress))
            await asyncio.wait_for(submitted.wait(), 5)
            for _ in range(100):
                if posts:
                    break
                await asyncio.sleep(.01)
            assert posts == [DATE]
            status, _, data = await asyncio.wait_for(task, .05)
            assert status == "waiting_human"
            assert data["resign"]["unconfirmed"][0]["date"] == DATE
            assert data["resign"]["submit_attempted"] is True
            assert posts == [DATE]

    asyncio.run(check())


def step(key, feature="action.click", **extra):
    return {"step_key": key, "feature_type": feature, "enabled": True,
            "condition": "always", "config": {}, **extra}


@pytest.fixture
def worker_run(monkeypatch, tmp_path):
    from tests.test_worker_navigation import _JumpLane, _JumpManager, _JumpPage

    monkeypatch.setattr(executor, "ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(executor.JobArtifacts, "capture_failure", AsyncMock())
    monkeypatch.setattr(executor, "_export_site_cookies", AsyncMock(return_value=[]))

    async def execute(steps, run_step, cancel=None, disconnect=False, task_cancel=False, assistant=False):
        monkeypatch.setattr(executor, "_run_step", run_step)
        lane = _JumpLane(_JumpPage())
        lane.session = SimpleNamespace(disconnected=False, desktop=None)
        lane.in_use = 1
        manager = _JumpManager(lane)
        snapshots = []
        canceled = False

        async def progress(data):
            nonlocal canceled
            snapshots.append(copy.deepcopy(data))
            if disconnect and data.get("steps") and data["steps"][-1].get("data", {}).get("resign"):
                lane.session.disconnected = True
            if task_cancel and not canceled and data.get("steps") and data["steps"][-1].get("data", {}).get("resign"):
                canceled = True
                task.cancel()

        task = asyncio.create_task(executor.execute_job(
            manager, {"job_id": 1, "steps": steps, "assistant_run": assistant}, cancel, progress,
        ))
        result = await task
        assert manager.released
        return result, snapshots

    return execute


@pytest.mark.parametrize("reason", ["cancel", "disconnect", "timeout", "task"])
@pytest.mark.parametrize("feature", ["post.resign_click", "post.resign_dialog"])
@pytest.mark.parametrize("confirmed", [False, True])
def test_executor_preserves_partial_checkpoint(worker_run, reason, feature, confirmed):
    async def check():
        cancel = asyncio.Event()
        state = {"pending": [DATE],
                 "success": [{"date": DATE, "message": "confirmed"}] if confirmed else [],
                 "submit_attempted": True, "unconfirmed": [] if confirmed else [{"date": DATE}]}

        async def run_step(_page, _step, *_args, resign_progress=None, **_kwargs):
            await resign_progress({"resign": state})
            if reason == "cancel":
                cancel.set()
            await asyncio.Event().wait()

        result, snapshots = await worker_run(
            [step("resign", feature, timeout_seconds=1, on_error="continue"), step("tail")],
            run_step, cancel, reason == "disconnect", reason == "task",
        )
        assert result["status"] == "waiting_human"
        assert len(result["steps"]) == 1
        assert result["steps"][0]["data"]["resign"] == state
        assert any(row.get("steps") and row["steps"][-1].get("data", {}).get("resign") == state
                   for row in snapshots)

    asyncio.run(check())


@pytest.mark.parametrize("retry_request", ["restart", "fallback_interaction", "jump"])
@pytest.mark.parametrize("evidence", [{"submit_attempted": True}, {"success": [{"date": DATE}]}])
@pytest.mark.parametrize("feature", ["post.resign_click", "post.resign_dialog"])
def test_replay_guard(worker_run, retry_request, evidence, feature):
    async def run_step(_page, current, *_args, **_kwargs):
        if current["step_key"] == "resign":
            return "success", "confirmed", {"resign": evidence}
        if retry_request == "jump":
            return "success", "jump", {"matched": True, "action": "jump", "target_step_key": "resign"}
        return retry_request, "retry requested", {}

    async def check():
        result, _ = await worker_run([step("resign", feature, timeout_seconds=30),
                                      step("after", "flow.jump", on_error="continue"), step("tail")], run_step)
        assert result["status"] == "waiting_human"
        assert len(result["steps"]) == 2
        assert result["steps"][0]["data"]["resign"] == evidence

    asyncio.run(check())


@pytest.mark.parametrize("feature", ["post.resign_click", "post.resign_dialog"])
@pytest.mark.parametrize("assistant", [False, True])
def test_executor_exception_keeps_partial_checkpoint(worker_run, feature, assistant):
    state = {"pending": [DATE], "submit_attempted": True, "unconfirmed": [{"date": DATE}]}

    async def run_step(_page, _step, *_args, resign_progress=None, **_kwargs):
        await resign_progress({"resign": state})
        raise RuntimeError("runner failed after prepare")

    result, _ = asyncio.run(worker_run([
        step("resign", feature, on_error="continue", timeout_seconds=30), step("tail"),
    ], run_step, assistant=assistant))
    assert result["status"] == ("unknown" if assistant else "waiting_human")
    assert len(result["steps"]) == 1
    assert result["steps"][0]["data"]["resign"] == state


@pytest.mark.parametrize("feature", ["post.resign_click", "post.resign_dialog"])
@pytest.mark.parametrize("authorized", [False, True])
def test_checkpointed_runner_dispatch_contract(monkeypatch, checkpoint_http, feature, authorized):
    suffix = feature.removeprefix("post.")
    module = ModuleType(f"worker.{suffix}")
    runner = AsyncMock(return_value=("success", "runner result", {"resign": {}}))
    setattr(module, f"run_{suffix}", runner)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    page = object()
    config = {"success_wait_seconds": 1}
    payload = runtime_payload()
    payload["resign_unresolved_dates"] = ["2026-10-02"]
    if not authorized:
        payload.pop("secret_access")
    progress = AsyncMock()

    async def check():
        result = await executor._run_step(
            page, {"step_key": "dialog-step", "feature_type": feature, "config": config},
            None, None, None, None, None, "general", resign_pending_dates=[DATE],
            secret_payload=payload, resign_progress=progress,
        )
        assert result == runner.return_value
        runner.assert_awaited_once()
        args, kwargs = runner.call_args
        assert args[:3] == (page, config, [DATE])
        assert callable(args[3])
        assert kwargs["today"] == TODAY
        assert kwargs["unresolved"] == ["2026-10-02"]
        assert kwargs["base_url"] == BASE
        assert kwargs["progress"] is progress
        if authorized:
            state = {"pending": [DATE], "submit_attempted": True, "unconfirmed": [{"date": DATE}]}
            await kwargs["durable_progress"](state, "prepare", DATE)
            assert len(checkpoint_http) == 1
            request = checkpoint_http[0]
            assert request.headers["Authorization"] == "Bearer test-private-token"
            body = json.loads(request.content)
            assert body == {"job_id": 17, "site_id": 23, "step_key": "dialog-step",
                            "phase": "prepare", "date": DATE, "result": {"resign": state}}
        else:
            assert kwargs["durable_progress"] is None
            assert not checkpoint_http

    asyncio.run(check())


@pytest.mark.parametrize("feature", ["post.resign_click", "post.resign_dialog"])
@pytest.mark.parametrize("override", [None, 7])
def test_executor_uses_feature_timeout(worker_run, monkeypatch, feature, override):
    suffix = feature.removeprefix("post.")
    module = ModuleType(f"worker.{suffix}")
    timeout = Mock(return_value=73)
    setattr(module, f"{suffix}_timeout", timeout)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    original = asyncio.wait_for
    budgets = []

    async def wait_for(awaitable, timeout):
        budgets.append(timeout)
        return await original(awaitable, timeout=timeout)

    monkeypatch.setattr(executor.asyncio, "wait_for", wait_for)
    config = {"success_wait_seconds": 2}
    result, _ = asyncio.run(worker_run([
        step("resign", feature, config=config, timeout_seconds=override),
    ], AsyncMock(return_value=("success", "done", {}))))
    assert result["status"] == "success"
    assert budgets == [73 if override is None else override]
    if override is None:
        timeout.assert_called_once_with(config, [])
    else:
        timeout.assert_not_called()


def test_timeout_budget_scales_with_dates_and_months():
    assert resign_click_timeout({}, [DATE] * 2) > resign_click_timeout({}, [DATE])
    assert resign_click_timeout({"max_month_jumps": 0}, [DATE]) < resign_click_timeout({}, [DATE])
    assert resign_click_timeout({"max_month_jumps": 120, "success_wait_seconds": 60}, [DATE] * 1000) == 7200


def test_catalog_defaults_dispatch_to_independent_runner(checkpoint_http):
    from app.features.catalog import PostResignClickConfig
    from worker.resign_click import DEFAULTS

    config = PostResignClickConfig().model_dump()
    assert all(config[key] == value for key, value in DEFAULTS.items())

    async def check():
        async with calendar() as (page, posts):
            checkpoints = []

            async def progress(data):
                checkpoints.append(copy.deepcopy(data))

            status, message, data = await executor._run_step(
                page, {"step_key": "resign", "feature_type": "post.resign_click", "config": config},
                None, None, None, None, None, "general", resign_pending_dates=[DATE],
                secret_payload=runtime_payload(),
                resign_progress=progress,
            )
            assert status == "success", message
            assert posts == [DATE]
            assert checkpoints[-1] == data
            assert [json.loads(request.content)["phase"] for request in checkpoint_http] == ["prepare", "confirm"]

    asyncio.run(check())


def runtime_payload():
    return {"base_url": BASE, "resign_today": TODAY, "resign_unresolved_dates": [],
            "job_id": 17, "site_id": 23, "secret_access": {"token": "test-private-token"}}


async def runtime_run(page, dates=None, progress=None, payload=None):
    return await executor._run_step(
        page, {"step_key": "resign", "feature_type": "post.resign_click",
               "config": {"success_wait_seconds": 1}},
        None, None, None, None, None, "general", resign_pending_dates=dates or [DATE],
        secret_payload=runtime_payload() if payload is None else payload,
        resign_progress=progress,
    )


@pytest.fixture
def checkpoint_http(monkeypatch):
    """Real HTTP serialization and ACK validation; never contact the backend."""
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    original = httpx.AsyncClient
    monkeypatch.setattr(resign_checkpoint.httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    return requests


def install_checkpoint_transport(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(resign_checkpoint.httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs,
    ))


def test_http_ack_contract_and_prepare_precedes_stream_and_click(checkpoint_http, monkeypatch):
    monkeypatch.setenv("SITEFLOW_RESIGN_CHECKPOINT_URL", BASE + "/internal/checkpoint")

    async def check():
        async with calendar() as (page, posts):
            prepare_seen = False
            confirm_seen = False

            async def progress(data):
                nonlocal prepare_seen, confirm_seen
                if data["resign"]["unconfirmed"]:
                    prepare_seen = True
                    assert len(checkpoint_http) == 1 and not posts
                if data["resign"]["success"]:
                    confirm_seen = True
                    assert len(checkpoint_http) == 2

            status, message, data = await runtime_run(page, progress=progress)
            assert status == "success", message
            assert prepare_seen and confirm_seen
            assert posts == [DATE]
            bodies = [json.loads(request.content) for request in checkpoint_http]
            for request, body, phase in zip(checkpoint_http, bodies, ("prepare", "confirm")):
                assert str(request.url) == BASE + "/internal/checkpoint"
                assert request.method == "POST"
                assert request.headers["Authorization"] == "Bearer test-private-token"
                assert set(body) == {"job_id", "site_id", "step_key", "phase", "date", "result"}
                assert (body["job_id"], body["site_id"], body["step_key"], body["date"]) == (17, 23, "resign", DATE)
                assert body["phase"] == phase
                assert body["result"]["resign"]["submit_attempted"] is True
                assert request.extensions["timeout"]["read"] == 20
            assert bodies[0]["result"]["resign"]["unconfirmed"][0]["date"] == DATE
            assert bodies[1]["result"] == data

    asyncio.run(check())


@pytest.mark.parametrize("failure", [403, 409, 500, 201, 302, "timeout", "invalid-json", "false", "one", "list", "missing"])
def test_prepare_http_failure_never_clicks_or_stream_claims(monkeypatch, failure):
    requests = []

    def handler(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("test-private-token server-private-body", request=request)
        if isinstance(failure, int):
            return httpx.Response(failure, text="test-private-token server-private-body")
        if failure == "invalid-json":
            return httpx.Response(200, text="test-private-token server-private-body")
        return httpx.Response(200, json={"false": {"ok": False}, "one": {"ok": 1},
                                         "list": [True], "missing": {}}[failure])

    install_checkpoint_transport(monkeypatch, handler)

    async def check():
        async with calendar() as (page, posts):
            snapshots = []

            async def progress(data):
                snapshots.append(copy.deepcopy(data))

            status, message, data = await runtime_run(page, [DATE, "2026-10-02"], progress)
            state = data["resign"]
            assert status == "waiting_human"
            assert resign_checkpoint.CHECKPOINT_ERROR in message
            assert "test-private-token" not in str((message, data, snapshots))
            assert "server-private-body" not in str((message, data, snapshots))
            assert not posts and await page.evaluate("clicks") == []
            assert len(requests) == 1
            assert json.loads(requests[0].content)["phase"] == "prepare"
            assert str(requests[0].url) == "http://api:8000/api/v1/internal/resign/checkpoint"
            assert state["remaining"] == [DATE, "2026-10-02"]
            assert state["submit_attempted"] is False and state["unconfirmed"] == []
            assert all(not row["resign"]["unconfirmed"] for row in snapshots)

    asyncio.run(check())


@pytest.mark.parametrize("failure", [409, 500, "timeout", "invalid-json"])
def test_confirm_http_failure_keeps_known_success_and_stops(monkeypatch, failure):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if body["phase"] == "prepare":
            return httpx.Response(200, json={"ok": True})
        if failure == "timeout":
            raise httpx.ReadTimeout("test-private-token server-private-body", request=request)
        if failure == "invalid-json":
            return httpx.Response(200, text="server-private-body")
        return httpx.Response(failure, text="server-private-body")

    install_checkpoint_transport(monkeypatch, handler)

    async def check():
        async with calendar() as (page, posts):
            snapshots = []

            async def progress(data):
                snapshots.append(copy.deepcopy(data))

            status, message, data = await runtime_run(page, [DATE, "2026-10-02"], progress)
            state = data["resign"]
            assert status == "waiting_human"
            assert posts == [DATE]
            assert [row["phase"] for row in requests] == ["prepare", "confirm"]
            assert [row["date"] for row in state["success"]] == [DATE]
            assert state["failed"] == state["unconfirmed"] == []
            assert state["remaining"] == ["2026-10-02"] and state["submit_attempted"]
            assert snapshots[-1] == data
            assert "test-private-token" not in str((message, data))
            assert "server-private-body" not in str((message, data))

    asyncio.run(check())


def test_stream_failure_does_not_replace_or_skip_http_ack(checkpoint_http):
    async def check():
        async with calendar() as (page, posts):
            progress = AsyncMock(side_effect=RuntimeError("queue disconnected"))
            status, message, data = await runtime_run(page, progress=progress)
            assert status == "success", message
            assert posts == [DATE]
            assert progress.await_count >= 3
            assert [json.loads(row.content)["phase"] for row in checkpoint_http] == ["prepare", "confirm"]
            assert data["resign"]["success"][0]["date"] == DATE

    asyncio.run(check())


@pytest.mark.parametrize("missing", ["token", "job_id", "site_id"])
def test_runtime_missing_authorization_fails_before_navigation(checkpoint_http, missing):
    async def check():
        async with calendar() as (page, posts):
            payload = runtime_payload()
            if missing == "token":
                payload.pop("secret_access")
            else:
                payload.pop(missing)
            status, message, data = await runtime_run(page, payload=payload)
            assert status == "waiting_human"
            assert page.url == "about:blank"
            assert not checkpoint_http and not posts
            assert data["resign"]["submit_attempted"] is False
            assert resign_checkpoint.CHECKPOINT_ERROR in message

    asyncio.run(check())


def test_direct_runner_without_explicit_ack_fails_closed():
    page = SimpleNamespace(goto=AsyncMock())
    status, _, data = asyncio.run(run(page, durable_progress=None))
    assert status == "waiting_human"
    assert data["resign"]["submit_attempted"] is False
    page.goto.assert_not_awaited()


def test_direct_durable_exception_is_sanitized_and_never_clicks():
    async def check():
        async with calendar() as (page, posts):
            ack = AsyncMock(side_effect=RuntimeError("test-private-token server-private-body"))
            status, message, data = await run(page, durable_progress=ack)
            assert status == "waiting_human" and not posts
            ack.assert_awaited_once()
            assert ack.await_args.args[1:] == ("prepare", DATE)
            assert data["resign"]["unconfirmed"] == []
            assert "test-private-token" not in str((message, data))
            assert "server-private-body" not in str((message, data))

    asyncio.run(check())


def test_initial_already_signed_never_reserves_or_confirms(checkpoint_http):
    async def check():
        async with calendar({"signed": [DATE]}) as (page, posts):
            status, _, data = await runtime_run(page)
            assert status == "success"
            assert data["resign"]["already_signed"] == [DATE]
            assert not posts and not checkpoint_http

    asyncio.run(check())


def test_stale_queued_job_and_same_job_reprepare_rejected(monkeypatch):
    requests = []
    claimed = set()

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        key = (body["site_id"], body["date"])
        if body["phase"] == "prepare":
            if key in claimed:
                return httpx.Response(409, json={"detail": "existing unknown or terminal"})
            claimed.add(key)
        return httpx.Response(200, json={"ok": True})

    install_checkpoint_transport(monkeypatch, handler)

    async def check():
        async with calendar({"noResult": True}) as (page, posts):
            first = await runtime_run(page)
            assert first[0] == "waiting_human" and posts == [DATE]
            # A fresh DOM and stale payload do not reveal the first job's unknown.
            for job_id in (18, 17):
                payload = {**runtime_payload(), "job_id": job_id}
                status, _, data = await runtime_run(page, payload=payload)
                assert status == "waiting_human"
                assert posts == [DATE] and await page.evaluate("clicks") == []
                assert data["resign"]["unconfirmed"] == []
            assert [row["job_id"] for row in requests] == [17, 18, 17]

    asyncio.run(check())


def test_http_checkpoint_total_deadline_is_bounded(monkeypatch):
    entered = []

    async def handler(request):
        entered.append(request)
        await asyncio.Event().wait()

    install_checkpoint_transport(monkeypatch, handler)
    monkeypatch.setattr(resign_checkpoint, "CHECKPOINT_TIMEOUT_SECONDS", .03)

    async def check():
        callback = resign_checkpoint.make_resign_checkpoint(runtime_payload(), "resign")
        with pytest.raises(resign_checkpoint.ResignCheckpointError) as error:
            await asyncio.wait_for(callback({"pending": [DATE]}, "prepare", DATE), .5)
        assert str(error.value) == resign_checkpoint.CHECKPOINT_ERROR
        assert len(entered) == 1

    asyncio.run(check())


@pytest.mark.parametrize("phase", ["prepare", "confirm"])
def test_cancel_during_http_ack_preserves_safe_partial(monkeypatch, phase):
    async def check():
        reached = asyncio.Event()
        requests = []

        async def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            if body["phase"] == phase:
                reached.set()
                await asyncio.Event().wait()  # The server may have committed before ACK loss.
            return httpx.Response(200, json={"ok": True})

        install_checkpoint_transport(monkeypatch, handler)
        async with calendar() as (page, posts):
            snapshots = []

            async def progress(data):
                snapshots.append(copy.deepcopy(data))

            task = asyncio.create_task(runtime_run(page, [DATE, "2026-10-02"], progress))
            await asyncio.wait_for(reached.wait(), 5)
            task.cancel()
            status, _, data = await asyncio.wait_for(task, 2)
            state = data["resign"]
            assert status == "waiting_human"
            assert state["unconfirmed"] == []
            assert snapshots[-1] == data
            if phase == "prepare":
                assert not posts and await page.evaluate("clicks") == []
                assert state["success"] == [] and not state["submit_attempted"]
                assert state["remaining"] == [DATE, "2026-10-02"]
                assert [row["phase"] for row in requests] == ["prepare"]
            else:
                assert posts == [DATE]
                assert state["success"][0]["date"] == DATE
                assert state["remaining"] == ["2026-10-02"] and state["submit_attempted"]
                assert [row["phase"] for row in requests] == ["prepare", "confirm"]

    asyncio.run(check())


def test_prepare_ack_yield_reauthorizes_dom_before_click(monkeypatch):
    async def check():
        async with calendar() as (page, posts):
            requests = []

            async def handler(request):
                requests.append(json.loads(request.content))
                await page.locator('.cal-att button').evaluate("el => el.textContent = '今日签到'")
                return httpx.Response(200, json={"ok": True})

            install_checkpoint_transport(monkeypatch, handler)
            status, _, data = await runtime_run(page)
            assert status == "waiting_human" and not posts
            assert [row["phase"] for row in requests] == ["prepare"]
            assert data["resign"]["unconfirmed"][0]["date"] == DATE
            assert data["resign"]["submit_attempted"] is True

    asyncio.run(check())


def test_stream_exception_and_durable_rejection_never_submit(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(409, json={"ok": False})

    install_checkpoint_transport(monkeypatch, handler)

    async def check():
        async with calendar() as (page, posts):
            progress = AsyncMock(side_effect=RuntimeError("queue disconnected"))
            status, _, data = await runtime_run(page, progress=progress)
            assert status == "waiting_human" and not posts
            assert len(requests) == 1
            assert progress.await_count >= 1
            assert data["resign"]["unconfirmed"] == []

    asyncio.run(check())


def test_prepare_commit_ack_loss_never_clicks_and_later_reservation_conflicts(monkeypatch):
    claims = set()
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        key = body["site_id"], body["date"]
        if key in claims:
            return httpx.Response(409, json={"ok": False})
        claims.add(key)  # Simulate commit, followed by losing the ACK on the wire.
        raise httpx.ReadTimeout("private response", request=request)

    install_checkpoint_transport(monkeypatch, handler)

    async def check():
        async with calendar() as (page, posts):
            for job_id in (17, 18):
                payload = {**runtime_payload(), "job_id": job_id}
                status, message, data = await runtime_run(page, payload=payload)
                assert status == "waiting_human" and not posts
                assert data["resign"]["remaining"] == [DATE]
                assert data["resign"]["unconfirmed"] == []
                assert data["resign"]["submit_attempted"] is False
                assert resign_checkpoint.CHECKPOINT_ERROR in message
            assert claims == {(23, DATE)}
            assert [row["phase"] for row in requests] == ["prepare", "prepare"]

    asyncio.run(check())
