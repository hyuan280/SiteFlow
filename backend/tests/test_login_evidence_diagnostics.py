from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import Error as PlaywrightError

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker import executor


PRIVATE = "fixture-password-token-username"


def matches(*visibility, count=None):
    elements = [SimpleNamespace(is_visible=AsyncMock(return_value=value)) for value in visibility]
    return SimpleNamespace(
        count=AsyncMock(return_value=len(elements) if count is None else count),
        nth=Mock(side_effect=lambda index: elements[index % len(elements)]),
    )


def page_with(main, *children):
    return SimpleNamespace(
        url=f"https://site.test/account?token={PRIVATE}", main_frame=main,
        frames=[main, *children],
    )


def child_frame(parent, *, url="about:srcdoc", visible=True):
    handle = SimpleNamespace(is_visible=AsyncMock(return_value=visible), dispose=AsyncMock())
    return SimpleNamespace(
        url=url, parent_frame=parent, frame_element=AsyncMock(return_value=handle),
        locator=Mock(return_value=matches(True)), is_detached=Mock(return_value=False),
    )


@pytest.mark.parametrize("visibility,reason,count", [
    ([], "selector_missing", 0), ([False, False], "matches_hidden", 2),
    ([False, True], "visible_after_timeout", 2),
])
def test_snapshot_distinguishes_missing_hidden_and_late_visible(visibility, reason, count):
    locators = matches(*visibility)
    main = SimpleNamespace(locator=Mock(return_value=locators))
    log = Mock()
    evidence = asyncio.run(executor._login_success_evidence(
        page_with(main), [f'[data-token="{PRIVATE}"]'], waited_ms=1023, log=log,
    ))
    assert evidence["reason"] == reason
    assert evidence["waited_ms"] == 1023
    assert evidence["snapshot_consistency"] == "sampled_after_timeout"
    assert evidence["captured_after_timeout"] is True
    assert evidence["selector_mode"] == "visible"
    assert evidence["frame_scope"] == "main_and_visible_same_origin"
    assert evidence["visible_after_timeout"] is any(visibility)
    assert evidence["configured_count"] == 1
    assert evidence["evidence_truncated"] is False
    assert evidence["selectors"] == [{
        "selector_index": 0, "match_count": count, "visible_count": sum(visibility),
        "checked_count": count, "truncated": False, "read_errors": 0,
        "frames": [{"frame_index": 0, "scope": "main", "match_count": count,
                    "visible_count": sum(visibility), "checked_count": count, "truncated": False}],
    }]
    assert PRIVATE not in json.dumps(evidence) + repr(log.call_args_list)
    log.assert_called_once()
    assert f"#0 matches={count} visible={sum(visibility)}" in log.call_args.args[1]


def test_frame_scope_shared_and_excluded_frames_never_read_dom():
    main = SimpleNamespace(locator=Mock(return_value=matches()))
    allowed = child_frame(main)
    hidden = child_frame(main, visible=False)
    hidden_child = child_frame(hidden)
    cross = child_frame(main, url=f"https://other.test/?token={PRIVATE}")
    cross_child = child_frame(cross, url="https://site.test/frame")
    page = page_with(main, allowed, hidden, hidden_child, cross, cross_child)
    evidence = asyncio.run(executor._login_success_evidence(page, [PRIVATE], waited_ms=1000))
    row = evidence["selectors"][0]
    assert row["match_count"] == row["visible_count"] == row["checked_count"] == 1
    assert [frame["scope"] for frame in row["frames"]] == [
        "main", "same_origin", "hidden_frame", "hidden_frame", "cross_origin", "cross_origin",
    ]
    for frame, sample in zip(page.frames[2:], row["frames"][2:]):
        frame.locator.assert_not_called()
        assert sample["match_count"] is sample["visible_count"] is None
        assert sample["checked_count"] == 0
        assert sample["error"] == "frame_not_eligible"
    cross.frame_element.assert_not_awaited()
    cross_child.frame_element.assert_not_awaited()
    assert PRIVATE not in json.dumps(evidence)


@pytest.mark.parametrize("scope", ["hidden_frame", "cross_origin"])
def test_exclusion_is_not_reported_as_missing(scope):
    main = SimpleNamespace(locator=Mock(return_value=matches()))
    child = child_frame(main, visible=scope != "hidden_frame",
                        url="https://other.test/" if scope == "cross_origin" else "about:blank")
    evidence = asyncio.run(executor._login_success_evidence(page_with(main, child), [".account"], waited_ms=1))
    assert evidence["reason"] == "frame_scope_excluded"
    assert evidence["evidence_truncated"] is False
    assert evidence["selectors"][0]["read_errors"] == 0


@pytest.mark.parametrize("error,detached,code", [
    (PlaywrightError(f'Unexpected token "{PRIVATE}" while parsing css selector'), False, "invalid_selector"),
    (RuntimeError(PRIVATE), True, "frame_detached"),
    (TimeoutError(PRIVATE), False, "read_timeout"),
    (RuntimeError(PRIVATE), False, "read_timeout"),
])
def test_read_errors_are_static_and_never_expose_provider_text(error, detached, code):
    main = SimpleNamespace(locator=Mock(side_effect=error), is_detached=Mock(return_value=detached))
    log = Mock()
    evidence = asyncio.run(executor._login_success_evidence(page_with(main), [PRIVATE], waited_ms=5, log=log))
    assert evidence["reason"] == "read_incomplete"
    assert evidence["selectors"][0]["frames"][0]["error"] == code
    assert evidence["selectors"][0]["read_errors"] == 1
    assert PRIVATE not in json.dumps(evidence) + repr(log.call_args_list)


def test_unknown_error_is_not_formatted():
    class UnsafeError(RuntimeError):
        def __str__(self):
            pytest.fail("Provider errors must not be formatted")

    main = SimpleNamespace(locator=Mock(side_effect=UnsafeError()))
    evidence = asyncio.run(executor._login_success_evidence(page_with(main), [PRIVATE], waited_ms=0))
    assert evidence["reason"] == "read_incomplete"


def test_current_origin_after_redirect_defines_scope():
    main = SimpleNamespace(locator=Mock(return_value=matches()))
    redirected = child_frame(main, url="https://account.test/frame")
    previous = child_frame(main, url="https://site.test/frame")
    page = page_with(main, redirected, previous)
    page.url = "https://account.test/welcome"
    evidence = asyncio.run(executor._login_success_evidence(page, [".account"], waited_ms=10))
    assert [sample["scope"] for sample in evidence["selectors"][0]["frames"]] == [
        "main", "same_origin", "cross_origin",
    ]
    previous.locator.assert_not_called()


def test_unreadable_frame_scope_is_incomplete_without_dom_read():
    main = SimpleNamespace(locator=Mock(return_value=matches()))
    child = child_frame(None)
    evidence = asyncio.run(executor._login_success_evidence(page_with(main, child), [".account"], waited_ms=10))
    assert evidence["reason"] == "read_incomplete"
    sample = evidence["selectors"][0]["frames"][1]
    assert sample["scope"] == "unreadable_frame"
    assert sample["error"] == "frame_not_eligible"
    assert sample["match_count"] is sample["visible_count"] is None
    child.locator.assert_not_called()


def test_normal_locator_still_uses_first_match_only():
    locators = matches(False, True)
    locators.first = locators.nth(0)
    locators.first.count = AsyncMock(return_value=1)
    locators.nth.reset_mock()
    page = SimpleNamespace(locator=Mock(return_value=locators))
    assert asyncio.run(executor._find_locator(page, [".account"])) is None
    locators.nth.assert_not_called()


def test_selector_frame_match_and_byte_caps():
    locators = matches(False, count=1000)
    main = SimpleNamespace(locator=Mock(return_value=locators))
    children = [child_frame(main) for _ in range(12)]
    for child in children:
        child.locator.return_value = matches(False, count=1000)
    selectors = [f".private-{PRIVATE}-{index}" for index in range(15)]
    log = Mock()
    evidence = asyncio.run(executor._login_success_evidence(page_with(main, *children), selectors, waited_ms=8, log=log))
    assert evidence["configured_count"] == 15
    assert [item["selector_index"] for item in evidence["selectors"]] == list(range(10))
    assert evidence["evidence_truncated"] is True
    assert evidence["reason"] == "read_incomplete"
    assert len(json.dumps(evidence).encode()) <= 16000
    assert log.call_count == 10
    for item in evidence["selectors"]:
        assert len(item["frames"]) <= 10
        assert item["checked_count"] == 100
        assert item["visible_count"] == 0
        assert item["truncated"] is True
        assert all(sample["checked_count"] <= 100 for sample in item["frames"])
    for child in children[9:]:
        child.locator.assert_not_called()
    assert PRIVATE not in json.dumps(evidence) + repr(log.call_args_list)


@pytest.mark.parametrize("stall", ["count", "is_visible", "frame_element", "frame_visibility", "dispose"])
def test_all_diagnostic_reads_share_single_three_second_deadline(monkeypatch, stall):
    async def run():
        async def hang():
            await asyncio.Event().wait()

        locators = matches(False)
        main = SimpleNamespace(locator=Mock(return_value=locators))
        child = child_frame(main)
        if stall == "count":
            locators.count.side_effect = hang
        elif stall == "is_visible":
            locators.nth(0).is_visible.side_effect = hang
        else:
            main.locator.return_value = matches()
            target = (child.frame_element if stall == "frame_element" else
                      child.frame_element.return_value.is_visible if stall == "frame_visibility" else
                      child.frame_element.return_value.dispose)
            target.side_effect = hang
        # One read can consume the whole budget; no selector/frame gets a fresh one.
        monkeypatch.setattr(executor, "FRAME_OP_TIMEOUT_SECONDS", 10)
        started = time.monotonic()
        evidence = await executor._login_success_evidence(page_with(main, child), [".one", ".two"], waited_ms=33)
        elapsed = time.monotonic() - started
        assert 2.9 <= elapsed < 3.5
        assert 2900 <= evidence["elapsed_ms"] < 3500
        assert evidence["reason"] == "read_incomplete"
        assert evidence["evidence_truncated"] is True
        assert evidence["selectors"][1]["frames"][0]["error"] == "read_timeout"
    asyncio.run(run())


def test_partial_visibility_counts_survive_timeout(monkeypatch):
    async def run():
        async def hang():
            await asyncio.Event().wait()

        locators = matches(False, False)
        locators.nth(1).is_visible.side_effect = hang
        main = SimpleNamespace(locator=Mock(return_value=locators))
        monkeypatch.setattr(executor, "FRAME_OP_TIMEOUT_SECONDS", 0.02)
        evidence = await executor._login_success_evidence(page_with(main), [".account"], waited_ms=1)
        row = evidence["selectors"][0]
        assert (row["match_count"], row["checked_count"], row["visible_count"], row["read_errors"]) == (2, 1, 0, 1)
        assert evidence["reason"] == "read_incomplete"
    asyncio.run(run())


def test_run_step_keeps_failure_for_late_evidence_and_measures_actual_wait(monkeypatch):
    async def run():
        username = SimpleNamespace(fill=AsyncMock(), input_value=AsyncMock(return_value="fixture-user"))
        password = SimpleNamespace(fill=AsyncMock(), input_value=AsyncMock(return_value=PRIVATE))
        submit = SimpleNamespace(click=AsyncMock())
        monkeypatch.setattr(executor, "_find_locator", AsyncMock(side_effect=[username, password, submit]))
        page = page_with(SimpleNamespace(locator=Mock(return_value=matches(False, True))))

        async def timed_out(*_args, **_kwargs):
            await asyncio.sleep(0.04)
            return None

        monkeypatch.setattr(executor, "_find_success_locator", timed_out)
        status, message, data = await executor._run_step(
            page, {"feature_type": "auth.form_login", "config": {"success_selector": [PRIVATE]}},
            {"username": "fixture-user", "password": PRIVATE}, None, None, None, None, "general",
        )
        assert status == "fail"
        assert data["reason"] == "success_evidence_missing" and data["field"] == "success_selector"
        assert data["submitted"] is True and data["verified"] is False
        assert data["success_evidence"]["reason"] == "visible_after_timeout"
        assert 30 <= data["success_evidence"]["waited_ms"] < 500
        assert PRIVATE not in json.dumps(data["success_evidence"]) + message
        submit.click.assert_awaited_once()
        username.fill.assert_awaited_once()
        password.fill.assert_awaited_once()
    asyncio.run(run())
