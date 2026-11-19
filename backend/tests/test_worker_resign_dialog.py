from __future__ import annotations

import asyncio
import copy
import html
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.features.catalog import PostResignDialogConfig
from tests.test_worker_resign_loop import EXECUTABLE
from worker.resign_checkpoint import CHECKPOINT_ERROR
from worker.resign_dialog import DEFAULTS, run_resign_dialog


BASE = "http://127.0.0.1:18765"
TODAY = "2026-11-09"
DATE = "2026-11-08"
SECOND = "2026-11-07"


@asynccontextmanager
async def dialog_page(options=None, buttons=None):
    """All browser traffic is fulfilled locally; reloads render persistent server state."""
    from playwright.async_api import async_playwright

    if not Path(EXECUTABLE).is_file():
        pytest.skip("isolated Chromium executable unavailable")
    options = options or {}
    buttons = buttons if buttons is not None else [
        {"date": "2026-10-08"}, {"date": DATE}, {"date": SECOND},
    ]
    template = Path(__file__).with_name("resign_dialog_fixture.html").read_text()
    server = {"cards": options.get("cards", 3), "done": list(options.get("done", [])),
              "posts": [], "opens": 0, "loads": 0, "external": [],
              "submitted": asyncio.Event()}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=EXECUTABLE, headless=True)
        context = await browser.new_context(service_workers="block")

        async def route_local(route):
            request = route.request
            if not request.url.startswith(BASE + "/"):
                server["external"].append(request.url)
                await route.abort()
                return
            path = urlsplit(request.url).path
            if path == "/submit":
                assert request.method == "POST"
                server["posts"].append(request.post_data)
                if not options.get("noResult"):
                    server["cards"] -= options.get("delta", 1)
                    if not options.get("noDone"):
                        server["done"].append(request.post_data)
                await route.fulfill(body="ok")
                server["submitted"].set()
            elif path == "/opened":
                server["opens"] += 1
                await route.fulfill(body="ok")
            else:
                assert path == "/attendance", f"unexpected local request: {request.url}"
                server["loads"] += 1
                done = "".join(
                    '<span class="attendance-calendar__day is-done" '
                    f'title="{html.escape(stamp, quote=True)}">done</span>'
                    for stamp in server["done"]
                )
                available = [button for button in buttons if button["date"] not in server["done"]]
                body = (template.replace("FIXTURE_CARDS", str(server["cards"]))
                        .replace("FIXTURE_DONE", done)
                        .replace("FIXTURE_LOAD", str(server["loads"]))
                        .replace("FIXTURE_OPTIONS", json.dumps(options))
                        .replace("FIXTURE_BUTTONS", json.dumps(available)))
                await route.fulfill(content_type="text/html; charset=utf-8", body=body)

        await context.route("**/*", route_local)
        page = await context.new_page()
        try:
            await page.goto(BASE + "/attendance")
            yield page, server
        finally:
            await context.close()
            await browser.close()
            assert not server["external"], "fixture must never access external services"


async def run(page, dates=None, config=None, **kwargs):
    config = PostResignDialogConfig.model_validate({
        "dialog_wait_seconds": 1, "success_wait_seconds": 3, **(config or {}),
    }).model_dump()
    return await run_resign_dialog(
        page, config, [DATE] if dates is None else dates, lambda *_: None,
        today=kwargs.pop("today", TODAY), base_url=BASE,
        durable_progress=kwargs.pop("durable_progress", AsyncMock()), **kwargs,
    )


def test_delayed_dialog_and_buttons_exact_date_and_default_schema():
    assert all(PostResignDialogConfig().model_dump()[key] == value for key, value in DEFAULTS.items())

    async def check():
        async with dialog_page({"dialogDelay": 100, "buttonsDelay": 250}) as (page, server):
            checkpoints = []
            snapshots = []

            async def ack(state, phase, stamp):
                checkpoints.append((copy.deepcopy(state), phase, stamp))
                assert server["posts"] == ([] if phase == "prepare" else [DATE])

            async def progress(data):
                snapshots.append(copy.deepcopy(data))

            status, message, data = await run(page, durable_progress=ack, progress=progress)
            state = data["resign"]
            assert status == "success", message
            assert server["posts"] == [DATE]  # Same day in another month must not match.
            assert server["loads"] == 2
            assert [row[1:] for row in checkpoints] == [("prepare", DATE), ("confirm", DATE)]
            assert checkpoints[0][0]["unconfirmed"][0]["date"] == DATE
            assert checkpoints[1][0] == state
            assert [row["date"] for row in state["success"]] == [DATE]
            assert (state["cards_before"], state["cards_after"]) == (3, 2)
            assert state["remaining"] == state["unconfirmed"] == state["already_signed"] == []
            assert state["submit_attempted"] and snapshots[-1] == data

    asyncio.run(check())


@pytest.mark.parametrize("stamp,today,label,pattern", [
    ("2025-12-31", "2026-01-02", "Resign 12-31", None),
    ("2025-11-08", TODAY, "Resign 2025-11-08", r"(?P<date>\d{4}-\d{2}-\d{2})"),
    ("2025-11-08", TODAY, "Resign 2025-11-08",
     r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"),
])
def test_year_rollover_and_explicit_full_year(stamp, today, label, pattern):
    async def check():
        async with dialog_page(buttons=[{"date": stamp, "label": label}]) as (page, server):
            status, message, data = await run(
                page, [stamp], today=today, config={"date_regex": pattern} if pattern else {},
            )
            assert status == "success", message
            assert server["posts"] == [stamp]
            assert data["resign"]["success"][0]["date"] == stamp

    asyncio.run(check())


@pytest.mark.parametrize("dates", [
    ["2025-11-08"], ["2025-11-08", DATE], [DATE, "2025-11-08"],
])
def test_yearless_buttons_use_pending_year_without_guessing(dates):
    async def check():
        async with dialog_page(buttons=[{"date": dates[0], "dataDate": None}]) as (page, server):
            status, message, data = await run(page, dates)
            if len(dates) == 1:
                assert status == "success", message
                assert server["posts"] == dates
            else:
                assert status == "waiting_human", message
                assert not server["posts"]
                assert not data["resign"]["submit_attempted"]

    asyncio.run(check())


def test_default_regex_preserves_explicit_full_year_in_button_text():
    async def check():
        buttons = [{"date": "2025-11-08", "label": "Resign 2025-11-08", "dataDate": None}]
        async with dialog_page(buttons=buttons) as (page, server):
            ack = AsyncMock()
            status, message, data = await run(page, durable_progress=ack)
            state = data["resign"]
            assert status == "waiting_human", message
            assert not server["posts"] and await page.evaluate("clicks") == []
            assert not state["submit_attempted"] and state["remaining"] == [DATE]
            assert state["already_signed"] == state["success"] == state["unconfirmed"] == []
            ack.assert_not_awaited()

    asyncio.run(check())


@pytest.mark.parametrize("label,pattern", [
    ("Resign 11-08", None),
    ("Resign 2026-11-08", None),
    ("Resign 2026-11-08", r"(?P<date>\d{4}-\d{2}-\d{2})"),
    ("Resign 2026-11-08", r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"),
])
def test_data_date_contradicting_text_rejected_before_prepare(label, pattern):
    async def check():
        buttons = [{"date": DATE, "dataDate": "2025-11-08", "label": label}]
        async with dialog_page(buttons=buttons) as (page, server):
            ack = AsyncMock()
            status, message, data = await run(
                page, config={"date_regex": pattern} if pattern else {}, durable_progress=ack,
            )
            assert status == "waiting_human", message
            assert "矛盾" in message
            assert not server["posts"] and await page.evaluate("clicks") == []
            assert not data["resign"]["submit_attempted"]
            assert data["resign"]["remaining"] == [DATE]
            ack.assert_not_awaited()

    asyncio.run(check())


def test_date_or_month_day_regex_branches_in_same_dialog():
    async def check():
        buttons = [{"date": DATE, "label": "Resign 2026-11-08", "dataDate": None},
                   {"date": SECOND, "label": "Resign 11-07", "dataDate": None}]
        pattern = r"(?P<date>\d{4}-\d{2}-\d{2})|(?P<month>\d{2})-(?P<day>\d{2})"
        async with dialog_page(buttons=buttons) as (page, server):
            status, message, data = await run(page, [DATE, SECOND], config={"date_regex": pattern})
            assert status == "success", message
            assert server["posts"] == [DATE, SECOND]
            assert server["loads"] == 3 and server["opens"] == 2
            assert [row["date"] for row in data["resign"]["success"]] == [DATE, SECOND]
            assert data["resign"]["remaining"] == data["resign"]["unconfirmed"] == []

    asyncio.run(check())


@pytest.mark.parametrize("enabled", [False, True])
def test_missing_date_switch_only_for_ready_dialog(enabled):
    async def check():
        async with dialog_page(buttons=[]) as (page, server):
            ack = AsyncMock()
            status, message, data = await run(
                page, config={"missing_button_as_signed": enabled,
                              "dialog_ready_selector": ['.dates[data-ready="true"]']},
                durable_progress=ack,
            )
            state = data["resign"]
            assert await page.locator('.dates[data-ready="true"]').is_visible()
            assert status == ("success" if enabled else "waiting_human"), message
            assert state["already_signed"] == ([DATE] if enabled else [])
            assert state["inferred_signed"] == ([DATE] if enabled else [])
            assert state["remaining"] == ([] if enabled else [DATE])
            assert not server["posts"] and not state["submit_attempted"]
            ack.assert_not_awaited()

    asyncio.run(check())


@pytest.mark.parametrize("options", [{}, {"listError": True}])
def test_missing_switch_requires_evidence_for_empty_or_error_list(options):
    async def check():
        async with dialog_page(options, buttons=[]) as (page, server):
            ack = AsyncMock()
            status, message, data = await run(
                page, config={"missing_button_as_signed": True}, durable_progress=ack,
            )
            state = data["resign"]
            assert status == "waiting_human", message
            assert "缺少日期列表加载完成证据" in message
            assert state["already_signed"] == [] and state["remaining"] == [DATE]
            assert state["inferred_signed"] == []
            assert not state["submit_attempted"] and not server["posts"]
            if options.get("listError"):
                assert await page.locator(".dates").inner_text() == "Date list failed to load"
                assert await page.locator(".dates").get_attribute("data-ready") is None
            ack.assert_not_awaited()

    asyncio.run(check())


def test_missing_date_with_other_loaded_dates_is_recorded_as_inferred():
    async def check():
        async with dialog_page(buttons=[{"date": SECOND}]) as (page, server):
            ack = AsyncMock()
            status, message, data = await run(
                page, config={"missing_button_as_signed": True}, durable_progress=ack,
            )
            state = data["resign"]
            assert status == "success", message
            assert state["already_signed"] == state["inferred_signed"] == [DATE]
            assert state["remaining"] == state["success"] == state["unconfirmed"] == []
            assert not state["submit_attempted"] and not server["posts"]
            ack.assert_not_awaited()

    asyncio.run(check())


@pytest.mark.parametrize("options", [{"noDialog": True}, {"loadingForever": True}])
def test_missing_switch_never_swallows_dialog_loading_failure(options):
    async def check():
        async with dialog_page(options) as (page, server):
            status, message, data = await run(page, config={
                "missing_button_as_signed": True,
                "dialog_ready_selector": ['.dates[data-ready="true"]'],
            })
            assert status == "waiting_human", message
            assert data["resign"]["already_signed"] == []
            assert data["resign"]["remaining"] == [DATE]
            assert not data["resign"]["submit_attempted"] and not server["posts"]

    asyncio.run(check())


def test_ready_marker_only_appears_after_delayed_empty_list_finishes_loading():
    async def check():
        async with dialog_page({"buttonsDelay": 1500}, buttons=[]) as (page, server):
            status, message, data = await run(page, config={
                "missing_button_as_signed": True,
                "dialog_ready_selector": ['.dates[data-ready="true"]'],
            })
            assert status == "waiting_human", message
            assert await page.locator(".dates").get_attribute("data-ready") is None
            assert data["resign"]["already_signed"] == []
            assert data["resign"]["remaining"] == [DATE] and not server["posts"]

    asyncio.run(check())


@pytest.mark.parametrize("buttons", [
    [{"date": DATE}, {"date": DATE}],
    [{"date": DATE}, {"date": SECOND}, {"date": SECOND}],
    [{"date": DATE, "label": "11-08 / 11-07"}],
])
def test_duplicate_or_multiple_dates_fail_before_prepare(buttons):
    async def check():
        async with dialog_page(buttons=buttons) as (page, server):
            ack = AsyncMock()
            status, message, data = await run(page, durable_progress=ack)
            assert status == "waiting_human", message
            assert not server["posts"] and not data["resign"]["submit_attempted"]
            ack.assert_not_awaited()

    asyncio.run(check())


def test_zero_cards_preserves_all_pending_without_prepare():
    async def check():
        async with dialog_page({"cards": 0}) as (page, server):
            ack = AsyncMock()
            status, message, data = await run(page, [DATE, SECOND], durable_progress=ack)
            state = data["resign"]
            assert status == "success", message
            assert (state["cards_before"], state["cards_after"]) == (0, 0)
            assert state["remaining"] == [DATE, SECOND]
            assert state["success"] == state["unconfirmed"] == []
            assert not server["posts"] and not state["submit_attempted"]
            ack.assert_not_awaited()

    asyncio.run(check())


def test_existing_done_skips_dialog_and_durable_reservation():
    async def check():
        async with dialog_page({"done": [DATE]}) as (page, server):
            ack = AsyncMock()
            status, message, data = await run(page, durable_progress=ack)
            assert status == "success", message
            assert data["resign"]["already_signed"] == [DATE]
            assert data["resign"]["inferred_signed"] == []
            assert data["resign"]["remaining"] == []
            assert not data["resign"]["submit_attempted"]
            assert server["opens"] == 0 and not server["posts"]
            ack.assert_not_awaited()

    asyncio.run(check())


@pytest.mark.parametrize("toast_only", [False, True])
def test_multiple_dates_reopen_after_800ms_reload_and_verify_inventory(toast_only):
    async def check():
        options = {"toast": toast_only, "noDone": toast_only}
        config = {"done_selector": [], "success_selector": [".message"]} if toast_only else {}
        async with dialog_page(options) as (page, server):
            status, message, data = await run(page, [DATE, SECOND], config=config)
            state = data["resign"]
            assert status == "success", message
            assert server["posts"] == [DATE, SECOND]
            assert server["opens"] == 2 and server["loads"] == 3
            assert await page.locator('[role="dialog"]').count() == 0
            assert [row["date"] for row in state["success"]] == [DATE, SECOND]
            assert state["remaining"] == state["unconfirmed"] == []
            assert (state["cards_before"], state["cards_after"]) == (3, 1)

    asyncio.run(check())


@pytest.mark.parametrize("options", [
    {"noDone": True},
    {"noDone": True, "staleToast": True},
    {"noDone": True, "toast": True, "delta": 0},
    {"noDone": True, "toast": True, "delta": 2},
    {"delta": 0},
    {"noResult": True},
])
def test_unconfirmed_result_clicks_once_and_stops_remaining_dates(options):
    async def check():
        async with dialog_page(options) as (page, server):
            ack = AsyncMock()
            status, message, data = await run(
                page, [DATE, SECOND], config={"success_selector": [".message"],
                                            "success_wait_seconds": 2}, durable_progress=ack,
            )
            state = data["resign"]
            assert status == "waiting_human", message
            assert server["posts"] == [DATE]
            assert state["success"] == [] and state["submit_attempted"]
            assert [row["date"] for row in state["unconfirmed"]] == [DATE]
            assert state["remaining"] == [DATE, SECOND]
            assert ack.await_count == 1 and ack.await_args.args[1:] == ("prepare", DATE)

    asyncio.run(check())


def test_prepare_ack_blocks_stream_and_click_until_released():
    async def check():
        async with dialog_page() as (page, server):
            entered, release = asyncio.Event(), asyncio.Event()
            snapshots = []

            async def ack(state, phase, stamp):
                if phase == "prepare":
                    assert state["unconfirmed"][0]["date"] == stamp == DATE
                    entered.set()
                    await release.wait()

            async def progress(data):
                snapshots.append(copy.deepcopy(data))

            task = asyncio.create_task(run(page, durable_progress=ack, progress=progress))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                await asyncio.sleep(.1)
                assert server["posts"] == [] and await page.evaluate("clicks") == []
                assert all(not row["resign"]["submit_attempted"] for row in snapshots)
            finally:
                release.set()
            status, message, _ = await asyncio.wait_for(task, 5)
            assert status == "success", message
            assert server["posts"] == [DATE]

    asyncio.run(check())


def test_success_message_appearing_during_prepare_is_not_submission_evidence():
    async def check():
        async with dialog_page({"noDone": True}) as (page, server):
            phases = []

            async def ack(_state, phase, stamp):
                phases.append((phase, stamp))
                assert not server["posts"] and await page.evaluate("clicks") == []
                assert await page.locator(".message").inner_text() == ""
                await page.locator(".message").evaluate("el => el.textContent = '补签成功'")

            status, message, data = await run(
                page, [DATE, SECOND], durable_progress=ack,
                config={"success_selector": [".message"], "success_wait_seconds": 2},
            )
            state = data["resign"]
            assert status == "waiting_human", message
            assert server["posts"] == [DATE] and server["done"] == []
            assert server["loads"] == 2
            assert (state["cards_before"], state["cards_after"]) == (3, 2)
            assert phases == [("prepare", DATE)]
            assert state["success"] == [] and state["submit_attempted"]
            assert [row["date"] for row in state["unconfirmed"]] == [DATE]
            assert state["remaining"] == [DATE, SECOND]

    asyncio.run(check())


@pytest.mark.parametrize("failure", ["reject", "replace-dom"])
def test_prepare_rejection_or_dom_replacement_never_clicks(failure):
    async def check():
        async with dialog_page() as (page, server):
            async def ack(_state, phase, _stamp):
                assert phase == "prepare"
                if failure == "reject":
                    raise RuntimeError("private-token private-server-body")
                await page.locator(f'button[data-date="{DATE}"]').evaluate(
                    "el => el.replaceWith(el.cloneNode(true))",
                )

            status, message, data = await run(page, durable_progress=ack)
            state = data["resign"]
            assert status == "waiting_human", message
            assert not server["posts"] and await page.evaluate("clicks") == []
            assert state["remaining"] == [DATE]
            if failure == "reject":
                assert CHECKPOINT_ERROR in message
                assert "private-" not in str((message, data))
                assert state["unconfirmed"] == [] and not state["submit_attempted"]
            else:
                assert state["submit_attempted"]
                assert state["unconfirmed"][0]["date"] == DATE

    asyncio.run(check())


def test_missing_durable_callback_fails_before_opening_dialog():
    async def check():
        async with dialog_page() as (page, server):
            status, message, data = await run(page, durable_progress=None)
            assert status == "waiting_human" and CHECKPOINT_ERROR in message
            assert not data["resign"]["submit_attempted"]
            assert server["opens"] == 0 and not server["posts"]

    asyncio.run(check())


@pytest.mark.parametrize("done", [False, True])
def test_unknown_requires_date_bound_done_before_any_submission(done):
    async def check():
        async with dialog_page({"done": [SECOND] if done else [], "staleToast": True}) as (page, server):
            ack = AsyncMock()
            status, message, data = await run(
                page, [DATE, SECOND], unresolved=[SECOND],
                config={"missing_button_as_signed": True, "success_selector": [".message"]},
                durable_progress=ack,
            )
            state = data["resign"]
            assert status == ("success" if done else "waiting_human"), message
            assert state["already_signed"] == ([SECOND] if done else [])
            assert state["inferred_signed"] == []
            assert server["posts"] == ([DATE] if done else [])
            if not done:
                ack.assert_not_awaited()
                assert state["remaining"] == [DATE, SECOND]
                assert server["opens"] == 0

    asyncio.run(check())


def test_unknown_missing_button_and_stale_success_cannot_clear_unknown():
    async def check():
        async with dialog_page({"staleToast": True}, buttons=[]) as (page, server):
            await page.locator("header button").click()
            await page.locator(".message").wait_for()
            status, message, data = await run(
                page, unresolved=[DATE],
                config={"missing_button_as_signed": True, "success_selector": [".message"]},
            )
            assert status == "waiting_human", message
            assert data["resign"]["already_signed"] == []
            assert data["resign"]["inferred_signed"] == []
            assert data["resign"]["remaining"] == [DATE] and not server["posts"]

    asyncio.run(check())


@pytest.mark.parametrize("where", ["entry", "date"])
def test_cross_origin_action_rejected_before_prepare(where):
    async def check():
        options = {"entryAction": "https://invalid.example/submit"} if where == "entry" else {}
        buttons = [{"date": DATE, "action": "https://invalid.example/submit"}] if where == "date" else None
        async with dialog_page(options, buttons) as (page, server):
            ack = AsyncMock()
            status, message, data = await run(page, durable_progress=ack)
            assert status == "waiting_human", message
            assert not server["posts"] and not data["resign"]["submit_attempted"]
            ack.assert_not_awaited()

    asyncio.run(check())


def test_script_window_open_cannot_issue_external_request():
    async def check():
        async with dialog_page({"entryPopup": "https://invalid.example/popup"}) as (page, server):
            requests = []
            page.context.on("request", lambda request: requests.append(request.url))
            await page.evaluate("() => { window.openBeforeRun = window.open; }")
            ack = AsyncMock()
            status, message, data = await run(page, durable_progress=ack)
            assert status == "waiting_human", message
            assert await page.evaluate("popupAttempted && popupBlocked") is True
            assert requests == [BASE + "/opened"]
            assert not server["external"] and not server["posts"]
            assert len(page.context.pages) == 1
            assert not data["resign"]["submit_attempted"]
            assert await page.evaluate("window.open === window.openBeforeRun") is True
            ack.assert_not_awaited()

    asyncio.run(check())


def test_second_entry_after_reload_blocks_script_popup_and_continues_successfully():
    async def check():
        options = {"entryPopupOnReload": "https://invalid.example/popup"}
        async with dialog_page(options) as (page, server):
            new_pages, requests, popup_checks = [], [], []
            page.context.on("page", lambda opened: new_pages.append(opened))
            page.context.on("request", lambda request: requests.append(request.url))

            async def ack(_state, phase, stamp):
                if phase == "prepare" and stamp == SECOND:
                    popup_checks.append(await page.evaluate("popupAttempted && popupBlocked"))
                    assert server["loads"] == server["opens"] == 2
                    assert server["posts"] == [DATE]

            status, message, data = await run(page, [DATE, SECOND], durable_progress=ack)
            state = data["resign"]
            assert status == "success", message
            assert popup_checks == [True]
            assert new_pages == [] and len(page.context.pages) == 1
            assert all(url.startswith(BASE + "/") for url in requests)
            assert not server["external"]
            assert server["posts"] == [DATE, SECOND]
            assert server["loads"] == 3 and server["opens"] == 2
            assert [row["date"] for row in state["success"]] == [DATE, SECOND]
            assert state["remaining"] == state["unconfirmed"] == state["inferred_signed"] == []
            assert (state["cards_before"], state["cards_after"]) == (3, 1)

    asyncio.run(check())


def test_final_progress_closing_page_still_cleans_context_route(monkeypatch):
    async def check():
        async with dialog_page() as (page, server):
            context = page.context
            original_routes = list(context._impl_obj._routes)
            listeners = {event: page._impl_obj.listeners(event) for event in ("dialog", "popup")}
            install = AsyncMock(wraps=context.route)
            cleanup = AsyncMock(wraps=context.unroute)
            monkeypatch.setattr(context, "route", install)
            monkeypatch.setattr(context, "unroute", cleanup)
            snapshots = []

            async def progress(data):
                snapshots.append(copy.deepcopy(data))
                if data["resign"]["success"] and not data["resign"]["remaining"]:
                    await page.close()

            status, message, data = await run(page, progress=progress)
            assert status == "success", message
            assert page.is_closed() and server["posts"] == [DATE]
            assert snapshots[-1] == data
            install.assert_awaited_once()
            cleanup.assert_awaited_once_with(*install.await_args.args)
            assert context._impl_obj._routes == original_routes
            for event, before in listeners.items():
                assert page._impl_obj.listeners(event) == before
            # The fixture's context route must remain usable after runner cleanup.
            sibling = await context.new_page()
            await sibling.goto(BASE + "/attendance")
            assert await sibling.locator("header button").inner_text() == "补签 x 2"

    asyncio.run(check())


def test_context_route_install_failure_cleans_page_route_and_listeners(monkeypatch):
    async def check():
        async with dialog_page() as (page, server):
            context = page.context
            original_page_routes = list(page._impl_obj._routes)
            original_context_routes = list(context._impl_obj._routes)
            listeners = {event: page._impl_obj.listeners(event) for event in ("dialog", "popup")}
            install = AsyncMock(wraps=page.route)
            cleanup = AsyncMock(wraps=page.unroute)
            context_install = AsyncMock(side_effect=RuntimeError("context route installation failed"))
            context_cleanup = AsyncMock(wraps=context.unroute)
            monkeypatch.setattr(page, "route", install)
            monkeypatch.setattr(page, "unroute", cleanup)
            monkeypatch.setattr(context, "route", context_install)
            monkeypatch.setattr(context, "unroute", context_cleanup)
            ack = AsyncMock()

            status, message, data = await run(page, durable_progress=ack)
            assert status == "waiting_human", message
            assert "context route installation failed" in message
            assert not data["resign"]["submit_attempted"]
            assert not server["posts"] and server["opens"] == 0
            install.assert_awaited_once()
            context_install.assert_awaited_once_with(*install.await_args.args)
            cleanup.assert_awaited_once_with(*install.await_args.args)
            context_cleanup.assert_not_awaited()
            assert page._impl_obj._routes == original_page_routes
            assert context._impl_obj._routes == original_context_routes
            for event, before in listeners.items():
                assert page._impl_obj.listeners(event) == before
            ack.assert_not_awaited()
            await page.reload()
            assert server["loads"] == 2

    asyncio.run(check())


def test_native_confirm_dismissed_without_submission_or_retry():
    async def check():
        async with dialog_page({"nativeConfirm": True}) as (page, server):
            status, message, data = await run(page, [DATE, SECOND], config={"success_wait_seconds": 1})
            assert status == "waiting_human", message
            assert not server["posts"] and await page.evaluate("clicks") == [DATE]
            assert data["resign"]["unconfirmed"][0]["date"] == DATE
            assert data["resign"]["remaining"] == [DATE, SECOND]

    asyncio.run(check())


def test_cancellation_preserves_confirmed_and_authorized_progress():
    async def check():
        async with dialog_page() as (page, server):
            reached = asyncio.Event()
            snapshots = []

            async def progress(data):
                snapshots.append(copy.deepcopy(data))
                if data["resign"]["success"] and data["resign"]["unconfirmed"] and not reached.is_set():
                    reached.set()
                    await asyncio.Event().wait()

            task = asyncio.create_task(run(page, [DATE, SECOND], progress=progress))
            await asyncio.wait_for(reached.wait(), 8)
            task.cancel()
            status, message, data = await asyncio.wait_for(task, 2)
            state = data["resign"]
            assert status == "waiting_human", message
            assert server["posts"] == [DATE]
            assert [row["date"] for row in state["success"]] == [DATE]
            assert [row["date"] for row in state["unconfirmed"]] == [SECOND]
            assert state["remaining"] == [SECOND] and state["submit_attempted"]
            assert snapshots[-1] == data

    asyncio.run(check())


def test_cancellation_after_dispatch_preserves_unknown_and_last_snapshot():
    async def check():
        async with dialog_page({"noResult": True}) as (page, server):
            snapshots = []

            async def progress(data):
                snapshots.append(copy.deepcopy(data))

            task = asyncio.create_task(run(page, [DATE, SECOND], progress=progress))
            await asyncio.wait_for(server["submitted"].wait(), 5)
            task.cancel()
            status, message, data = await asyncio.wait_for(task, 2)
            state = data["resign"]
            assert status == "waiting_human", message
            assert server["posts"] == [DATE] and await page.evaluate("clicks") == [DATE]
            assert state["success"] == [] and state["submit_attempted"]
            assert [row["date"] for row in state["unconfirmed"]] == [DATE]
            assert state["remaining"] == [DATE, SECOND]
            assert snapshots[-1] == data

    asyncio.run(check())
