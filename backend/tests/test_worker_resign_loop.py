from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker import executor, resign_checkpoint
from worker.resign_loop import resign_loop_timeout, run_resign_loop


BASE = "http://127.0.0.1:18765"
CHALLENGE = "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/test"
TODAY = "2026-11-02"
DATE = "2026-10-01"
SECOND = "2026-10-02"
EXECUTABLE = os.environ.get("RESIGN_TEST_BROWSER", "/opt/opencode/.cache/ms-playwright/"
                            "chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell")


@asynccontextmanager
async def calendar(options=None):
    """Keep all browser traffic local, with server-side done state across visits."""
    from playwright.async_api import async_playwright

    if not Path(EXECUTABLE).is_file():
        pytest.skip("isolated Chromium executable unavailable")
    options = options or {}
    html = Path(__file__).with_name("resign_loop_fixture.html").read_text()
    html = html.replace("FIXTURE_OPTIONS", json.dumps(options))
    posts = []
    saved_signed = set(options.get("signed", []))
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=EXECUTABLE, headless=True)
        context = await browser.new_context(service_workers="block")

        async def route_local(route):
            if route.request.url == CHALLENGE:
                await route.fulfill(content_type="text/html", body="<p>verification loaded</p>")
                return
            assert route.request.url.startswith(BASE + "/"), "external request blocked by fixture"
            if route.request.url == BASE + "/fixture-state":
                await route.fulfill(content_type="application/json", body=json.dumps(sorted(saved_signed)))
            elif route.request.url == BASE + "/submit":
                posts.append(route.request.post_data)
                if not options.get("noResult"):
                    saved_signed.add(route.request.post_data)
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
    return await run_resign_loop(
        page, {"wait_seconds": 1, **(config or {})},
        [DATE] if dates is None else dates, lambda *_: None,
        today=kwargs.pop("today", TODAY), base_url=BASE,
        runtime=kwargs.pop("runtime", {}),
        durable_progress=kwargs.pop("durable_progress", AsyncMock()), **kwargs,
    )


def runtime_payload():
    return {"base_url": BASE, "resign_today": TODAY, "resign_unresolved_dates": [],
            "job_id": 17, "site_id": 23, "secret_access": {"token": "test-private-token"}}


async def runtime_run(page, dates=None, progress=None, payload=None):
    return await executor._run_step(
        page, {"step_key": "resign", "feature_type": "post.resign_loop",
               "config": {"wait_seconds": 1}},
        None, None, None, None, None, "general",
        resign_pending_dates=[DATE] if dates is None else dates,
        secret_payload=runtime_payload() if payload is None else payload,
        resign_progress=progress,
    )


def install_checkpoint_transport(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(resign_checkpoint.httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handler), **kwargs,
    ))


def test_executor_dispatch_retains_job_runtime_and_serializes_durable_ack(monkeypatch):
    requests = []

    def handler(request):
        assert request.headers["Authorization"] == "Bearer test-private-token"
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    install_checkpoint_transport(monkeypatch, handler)

    async def check():
        async with calendar({"persistent": True}) as (page, posts):
            payload = runtime_payload()
            payload["resign_unresolved_dates"] = [DATE]  # Not an argument to the loop runner.
            for phase in ["prepare", "confirm"]:
                status, message, data = await runtime_run(page, payload=payload)
                assert status == "success", message
                assert payload["_resign_runtime"]["resign"] == data["resign"]
                assert requests[-1]["phase"] == phase
                assert requests[-1]["date"] == DATE
                assert requests[-1]["step_key"] == "resign"
            assert posts == [DATE]
            assert data["resign"]["remaining"] == []
            assert [row["phase"] for row in requests] == ["prepare", "confirm"]

    asyncio.run(check())


def test_loop_sorts_dates_confirms_previous_then_hands_off_once():
    async def check():
        async with calendar({"persistent": True, "readyDelay": 100}) as (page, posts):
            runtime = {}
            durable = AsyncMock()
            for index, current in enumerate([DATE, SECOND, None]):
                status, message, data = await run(
                    page, [SECOND, DATE], runtime=runtime, durable_progress=durable,
                )
                assert status == "success", message
                assert runtime["current_date"] == current
                assert data["resign"] == runtime
                assert runtime["pending"] == [DATE, SECOND]
                assert runtime["remaining"] == [DATE, SECOND][index:]
                assert posts == [DATE, SECOND][:index + 1]
                assert "execution_mode" not in runtime
                assert not {"cards_before", "cards_after"} & runtime.keys()
            assert [item["date"] for item in runtime["success"]] == [DATE, SECOND]
            assert runtime["attempts"] == {DATE: 1, SECOND: 1}
            assert [(call.args[1], call.args[2]) for call in durable.call_args_list] == [
                ("prepare", DATE), ("confirm", DATE), ("prepare", SECOND), ("confirm", SECOND),
            ]

    asyncio.run(check())


@pytest.mark.parametrize("max_retries", [None, 0, 1])
def test_unfinished_oldest_date_retries_then_stops(max_retries):
    async def check():
        async with calendar({"noResult": True}) as (page, posts):
            runtime = {}
            config = {} if max_retries is None else {"max_retries": max_retries}
            limit = (3 if max_retries is None else max_retries) + 1
            durable = AsyncMock()
            for _ in range(limit):
                status, message, _ = await run(
                    page, [SECOND, DATE], config=config, runtime=runtime, durable_progress=durable,
                )
                assert status == "success", message
                assert runtime["current_date"] == DATE
            status, message, data = await run(page, [SECOND, DATE], config=config, runtime=runtime)
            assert status == "waiting_human" and "已用尽" in message
            assert posts == [DATE] * limit
            assert runtime["attempts"] == {DATE: limit}
            assert runtime["remaining"] == [DATE, SECOND]
            assert [item["date"] for item in runtime["unconfirmed"]] == [DATE]
            assert runtime["failed"] == runtime["success"] == []
            assert data["resign"] == runtime
            assert durable.await_count == limit

    asyncio.run(check())


def test_done_is_checked_even_after_retry_budget_exhausted():
    async def check():
        async with calendar({"persistent": True}) as (page, posts):
            runtime = {}
            await run(page, runtime=runtime, config={"max_retries": 0})
            status, message, data = await run(page, runtime=runtime, config={"max_retries": 0})
            assert status == "success", message
            assert posts == [DATE]
            assert data["resign"]["remaining"] == []

    asyncio.run(check())


def test_downstream_challenge_iframe_loads_and_completes_before_next_visit():
    async def check():
        async with calendar({"noResult": True, "persistent": True,
                             "challengeFrame": CHALLENGE}) as (page, posts):
            runtime = {}
            status, message, _ = await run(page, runtime=runtime)
            assert status == "success", message
            assert runtime["success"] == [] and runtime["remaining"] == [DATE]
            await page.frame_locator("iframe").get_by_text("verification loaded").wait_for(timeout=3000)
            await page.evaluate("date => sessionStorage.setItem('fixture-signed', JSON.stringify([date]))", DATE)
            status, message, data = await run(page, runtime=runtime)
            assert status == "success", message
            assert data["resign"]["remaining"] == []
            assert posts == [DATE]

    asyncio.run(check())


def test_already_done_dates_need_no_checkpoint_or_click():
    async def check():
        async with calendar({"signed": [DATE, SECOND]}) as (page, posts):
            durable = AsyncMock()
            status, message, data = await run(page, [SECOND, DATE], durable_progress=durable)
            assert status == "success", message
            assert data["resign"]["already_signed"] == [DATE, SECOND]
            assert data["resign"]["remaining"] == []
            assert not data["resign"]["submit_attempted"] and not posts
            durable.assert_not_awaited()

    asyncio.run(check())


@pytest.mark.parametrize("signed", [False, True])
def test_waits_for_date_cells_before_checking_done(signed):
    async def check():
        async with calendar({"dateDelay": 300, "signed": [DATE] if signed else []}) as (page, posts):
            status, message, data = await run(page, config={"wait_seconds": 2})
            assert status == "success", message
            assert posts == ([] if signed else [DATE])
            assert data["resign"]["already_signed"] == ([DATE] if signed else [])

    asyncio.run(check())


@pytest.mark.parametrize("button_state", ["missing", "hidden", "inactive", "disabled"])
@pytest.mark.parametrize("delayed", [False, True])
def test_button_readiness_after_selecting_exact_date(button_state, delayed):
    async def check():
        async with calendar({"buttonState": button_state, "buttonDelay": 250 if delayed else 0}) as (page, posts):
            status, message, data = await run(page)
            assert status == ("success" if delayed else "waiting_human"), message
            assert posts == ([DATE] if delayed else [])
            assert data["resign"]["submit_attempted"] is delayed
            assert data["resign"]["remaining"] == [DATE]
            if not delayed:
                assert "等待补签按钮超时" in message

    asyncio.run(check())


@pytest.mark.parametrize("options,config", [
    ({"ambiguous": True}, {}), ({"ambiguousTitle": True}, {}),
    ({"ambiguousButton": True}, {}), ({"signed": [DATE], "duplicateDone": True}, {}),
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
def test_unsafe_date_dom_and_actions_never_submit(options, config):
    async def check():
        async with calendar(options) as (page, posts):
            status, _, data = await run(page, config=config)
            assert status == "waiting_human", data
            assert not posts and not data["resign"]["submit_attempted"]

    asyncio.run(check())


@pytest.mark.parametrize("dates,today", [
    ([TODAY], TODAY), (["2026-11-03"], TODAY), ([DATE, DATE], TODAY),
    (["2026-02-30"], TODAY), (["2026-1-01"], TODAY), ([DATE], None),
])
def test_invalid_dates_fail_before_navigation(dates, today):
    page = SimpleNamespace(goto=AsyncMock())
    status, _, data = asyncio.run(run(page, dates, today=today))
    assert status == "waiting_human" and not data["resign"]["submit_attempted"]
    page.goto.assert_not_awaited()


def test_month_navigation_templates_and_numeric_day_fallback():
    async def check():
        async with calendar({"month": "2026-11", "noDates": True}) as (page, posts):
            status, message, _ = await run(page, config={"done_selector": ['.cal-badge.att:text-is("done")']})
            assert status == "success", message
            assert posts == [DATE]  # Adjacent-month day 1 is done, but never selected.
            assert await page.evaluate("flips") == [0]
        async with calendar({"month": "2026-09"}) as (page, posts):
            status, message, _ = await run(page, config={
                "calendar_day_selector": ['[data-date="{date}"]:not(.mute)'],
                "calendar_selected_selector": ['[data-date="{year}-{month}-01"].on', '[data-date="{date}"].on'],
            })
            assert status == "success", message
            assert posts == [DATE] and await page.evaluate("flips") == [1]

    asyncio.run(check())


@pytest.mark.parametrize("mutation", ["selected", "button", "month", "action", "popup"])
def test_prepare_ack_yield_revalidates_dom_and_popup(mutation):
    async def check():
        async with calendar() as (page, posts):
            async def durable(_state, phase, _date):
                assert phase == "prepare" and not posts
                await page.evaluate("""kind => {
                    if (kind === 'selected') document.querySelector('.on').classList.remove('on');
                    if (kind === 'button') {
                        const button = document.querySelector('.cal-att button');
                        button.replaceWith(button.cloneNode(true));
                    }
                    if (kind === 'month') document.querySelector('.cal-water').textContent = '2026年9月';
                    if (kind === 'action') document.querySelector('.cal-att button').setAttribute('formaction', 'https://invalid.example');
                }""", mutation)
                if mutation == "popup":
                    async with page.expect_popup():
                        await page.evaluate("window.__siteflowResignOpen('about:blank')")
                    await asyncio.sleep(.05)

            status, _, data = await run(page, durable_progress=durable)
            assert status == "waiting_human" and not posts
            assert data["resign"]["submit_attempted"]
            assert data["resign"]["unconfirmed"][0]["date"] == DATE

    asyncio.run(check())


@pytest.mark.parametrize("failure", [409, "ack-lost", "invalid-json", "false"])
def test_prepare_http_failure_never_clicks_or_claims_reservation(monkeypatch, failure):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if failure == "ack-lost":
            raise httpx.ReadTimeout("test-private-token private-response", request=request)
        if failure == "invalid-json":
            return httpx.Response(200, text="private-response")
        return httpx.Response(409 if failure == 409 else 200, json={"ok": False})

    install_checkpoint_transport(monkeypatch, handler)

    async def check():
        async with calendar() as (page, posts):
            snapshots = []

            async def progress(data):
                snapshots.append(copy.deepcopy(data))

            status, message, data = await run(
                page, progress=progress,
                durable_progress=resign_checkpoint.make_resign_checkpoint(runtime_payload(), "resign"),
            )
            assert status == "waiting_human" and resign_checkpoint.CHECKPOINT_ERROR in message
            assert not posts and len(requests) == 1
            assert requests[0]["phase"] == "prepare"
            assert not data["resign"]["submit_attempted"] and data["resign"]["unconfirmed"] == []
            assert all(not row["resign"]["unconfirmed"] for row in snapshots)
            assert "test-private-token" not in str((message, data))
            assert "private-response" not in str((message, data))

    asyncio.run(check())


@pytest.mark.parametrize("phase", ["prepare", "confirm"])
@pytest.mark.parametrize("cancel", [False, True])
def test_ack_failure_or_cancel_keeps_safe_partial_state(phase, cancel):
    async def check():
        async with calendar({"persistent": True}) as (page, posts):
            runtime = {}
            snapshots = []
            reached = asyncio.Event()

            async def durable(_state, current_phase, _date):
                if current_phase == phase:
                    if cancel:
                        reached.set()
                        await asyncio.Event().wait()
                    raise RuntimeError("private-response")

            async def progress(data):
                snapshots.append(copy.deepcopy(data))

            if phase == "confirm":
                assert (await run(page, [DATE, SECOND], runtime=runtime))[0] == "success"
            task = asyncio.create_task(run(page, [DATE, SECOND], runtime=runtime,
                                           durable_progress=durable, progress=progress))
            if cancel:
                await asyncio.wait_for(reached.wait(), 5)
                task.cancel()
            status, message, data = await asyncio.wait_for(task, 5)
            assert status == "waiting_human" and "private-response" not in message
            assert snapshots[-1] == data
            assert runtime["unconfirmed"] == []
            if phase == "prepare":
                assert posts == [] and not runtime["submit_attempted"]
                assert runtime["remaining"] == [DATE, SECOND]
            else:
                assert posts == [DATE] and runtime["submit_attempted"]
                assert runtime["success"][0]["date"] == DATE
                assert runtime["remaining"] == [SECOND]

    asyncio.run(check())


@pytest.mark.parametrize("cancel", [False, True])
def test_click_transport_error_or_cancel_preserves_attempt(monkeypatch, cancel):
    from playwright.async_api import ElementHandle

    original = ElementHandle.click
    reached = None

    async def click(handle, **kwargs):
        submit = "补签" in await handle.inner_text()
        await original(handle, **kwargs)
        if submit:
            if cancel:
                reached.set()
                await asyncio.Event().wait()
            raise RuntimeError("transport lost after dispatch")

    monkeypatch.setattr(ElementHandle, "click", click)

    async def check():
        nonlocal reached
        reached = asyncio.Event()
        async with calendar({"persistent": True}) as (page, posts):
            runtime = {}
            task = asyncio.create_task(run(page, runtime=runtime))
            if cancel:
                await asyncio.wait_for(reached.wait(), 5)
                task.cancel()
            status, _, data = await task
            assert status == ("waiting_human" if cancel else "success")
            assert posts == [DATE] and data["resign"]["attempts"] == {DATE: 1}
            assert data["resign"]["unconfirmed"][0]["date"] == DATE
            assert (await run(page, runtime=runtime))[0] == "success"
            assert posts == [DATE] and runtime["remaining"] == []

    asyncio.run(check())


def test_stream_failure_is_best_effort_and_does_not_skip_ack():
    async def check():
        async with calendar({"persistent": True}) as (page, posts):
            runtime = {}
            durable = AsyncMock()
            for _ in range(2):
                status, message, _ = await run(
                    page, runtime=runtime, durable_progress=durable,
                    progress=AsyncMock(side_effect=RuntimeError("queue disconnected")),
                )
                assert status == "success", message
            assert posts == [DATE]
            assert [call.args[1] for call in durable.call_args_list] == ["prepare", "confirm"]

    asyncio.run(check())


def test_requires_job_runtime_and_durable_ack():
    page = SimpleNamespace(goto=AsyncMock())
    with pytest.raises(ValueError, match="运行上下文"):
        asyncio.run(run(page, runtime=None))
    status, _, data = asyncio.run(run(page, durable_progress=None))
    assert status == "waiting_human" and not data["resign"]["submit_attempted"]
    page.goto.assert_not_awaited()


def test_timeout_budget_scales_with_dates_and_months_and_has_cap():
    assert resign_loop_timeout({}, [DATE, SECOND]) > resign_loop_timeout({}, [DATE])
    assert resign_loop_timeout({"max_month_jumps": 0}, [DATE]) < resign_loop_timeout({}, [DATE])
    assert resign_loop_timeout({"max_month_jumps": 120, "wait_seconds": 60}, [DATE] * 1000) == 7200
