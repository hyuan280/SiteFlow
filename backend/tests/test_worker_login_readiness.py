from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.features.catalog import AuthFormLoginConfig, FEATURES
from worker import executor
from worker.assistant_step import ExecuteRequest, validate_step


def login_request(wait):
    return ExecuteRequest(
        token="test-token", operation_id=uuid.uuid4(), site_origin="https://site.test",
        step={"step_key": "login", "feature_type": "auth.form_login", "config": {
            "username_selector": ["#u"], "password_selector": ["#p"],
            "submit_selector": ["#s"], "success_wait_seconds": wait,
        }},
        execution={"feature_types": ["auth.form_login"]},
        credentials={"username": "fixture-user", "password": "fixture-password"},
    )


def test_login_readiness_default_and_schema():
    assert AuthFormLoginConfig().success_wait_seconds == 10
    assert FEATURES["auth.form_login"].validate_full({})["success_wait_seconds"] == 10
    field = FEATURES["auth.form_login"].config_schema()["properties"]["success_wait_seconds"]
    assert (field["default"], field["minimum"], field["maximum"]) == (10, 1, 120)


@pytest.mark.parametrize("wait", [1, 30, 120])
def test_login_readiness_accepts_bounded_integer_in_backend_and_worker(wait):
    assert AuthFormLoginConfig(success_wait_seconds=wait).success_wait_seconds == wait
    assert validate_step(login_request(wait))["config"]["success_wait_seconds"] == wait


@pytest.mark.parametrize("wait", [0, -1, 121, 1.5, 30.0, True, "30", None])
def test_login_readiness_rejects_invalid_wait_in_backend_and_worker(wait):
    with pytest.raises(ValidationError):
        AuthFormLoginConfig(success_wait_seconds=wait)
    with pytest.raises(ValueError):
        validate_step(login_request(wait))


@pytest.mark.parametrize("wait", [None, 1, 30, 120])
def test_login_uses_success_budget_without_changing_input_waits(monkeypatch, wait):
    username = SimpleNamespace(fill=AsyncMock(), input_value=AsyncMock(return_value="fixture-user"))
    password = SimpleNamespace(fill=AsyncMock(), input_value=AsyncMock(return_value="fixture-password"))
    submit = SimpleNamespace(click=AsyncMock())
    finder = AsyncMock(side_effect=[username, password, submit])
    success_finder = AsyncMock(return_value=object())
    monkeypatch.setattr(executor, "_find_locator", finder)
    monkeypatch.setattr(executor, "_find_success_locator", success_finder)
    config = {"success_selector": [".account"]}
    if wait is not None:
        config["success_wait_seconds"] = wait
    step = {"feature_type": "auth.form_login", "config": config, "timeout_seconds": 120}
    result = asyncio.run(executor._run_step(
        SimpleNamespace(url="https://site.test/"), step,
        {"username": "fixture-user", "password": "fixture-password"},
        None, None, None, None, "general",
    ))
    assert result[0] == "success" and result[2]["verified"] is True
    assert success_finder.await_args.kwargs["wait_ms"] == (10 if wait is None else wait) * 1000
    assert [call.kwargs["wait_ms"] for call in finder.await_args_list] == [executor.SELECTOR_WAIT_MS] * 3
    assert step["timeout_seconds"] == 120
    submit.click.assert_awaited_once()


def test_success_probe_has_one_deadline_even_when_frame_read_stalls():
    async def run():
        async def stall():
            await asyncio.Event().wait()

        locator = SimpleNamespace(count=AsyncMock(side_effect=stall))
        frame = SimpleNamespace(locator=Mock(return_value=locator))
        page = SimpleNamespace(frames=[frame] * 100, main_frame=frame)
        started = time.monotonic()
        assert await executor._find_success_locator(page, [".one", ".two"], wait_ms=60) is None
        assert time.monotonic() - started < 0.5
        assert locator.count.await_count == 1
    asyncio.run(run())


def test_success_probe_bounds_matches_and_does_not_accept_hidden_existence():
    hidden = SimpleNamespace(is_visible=AsyncMock(return_value=False))
    matches = SimpleNamespace(count=AsyncMock(return_value=1000), nth=Mock(return_value=hidden))
    page = SimpleNamespace(locator=Mock(return_value=matches))
    result = asyncio.run(executor._find_success_locator(page, [".account"], wait_ms=40))
    assert result is None
    assert matches.nth.call_count == 100
    assert matches.nth.call_args.args == (99,)


def test_success_probe_refreshes_frames_and_skips_bad_selectors():
    visible = SimpleNamespace(is_visible=AsyncMock(return_value=True))
    matches = SimpleNamespace(count=AsyncMock(return_value=1), nth=Mock(return_value=visible))
    good = SimpleNamespace(locator=Mock(side_effect=[ValueError("bad selector"), matches]))
    detached = SimpleNamespace(locator=Mock(side_effect=RuntimeError("detached frame")))

    class Page:
        reads = 0

        @property
        def frames(self):
            self.reads += 1
            return [detached] if self.reads == 1 else [detached, good]

    page = Page()
    page.url = "https://site.test/login"
    page.main_frame = good
    detached.url = "https://site.test/frame"
    detached.parent_frame = good
    detached.frame_element = AsyncMock(side_effect=RuntimeError("detached frame"))
    matched = Mock()
    assert asyncio.run(executor._find_success_locator(
        page, ["[", ".account"], wait_ms=1000, on_match=matched,
    )) is visible
    assert page.reads == 2
    matched.assert_called_once_with(".account")


@pytest.mark.parametrize("url,allowed", [
    ("https://SITE.test:443/account", True),
    ("about:blank", True),
    ("about:srcdoc", True),
    ("https://other.test/account", False),
    ("http://site.test/account", False),
    ("https://site.test:444/account", False),
    ("https://site.test:0/account", False),
    ("https://site.test:invalid/account", False),
    ("data:text/html,avatar", False),
])
def test_success_probe_restricts_frame_origins_and_disposes_handles(url, allowed):
    visible = SimpleNamespace(is_visible=AsyncMock(return_value=True))
    matches = SimpleNamespace(count=AsyncMock(return_value=1), nth=Mock(return_value=visible))
    main = SimpleNamespace(locator=Mock(return_value=SimpleNamespace(count=AsyncMock(return_value=0))))
    handle = SimpleNamespace(is_visible=AsyncMock(return_value=True), dispose=AsyncMock())
    child = SimpleNamespace(url=url, parent_frame=main, frame_element=AsyncMock(return_value=handle),
                            locator=Mock(return_value=matches))
    page = SimpleNamespace(url="https://site.test/login", main_frame=main, frames=[main, child])
    result = asyncio.run(executor._find_success_locator(page, [".account"], wait_ms=30))
    assert (result is visible) is allowed
    if allowed:
        assert handle.dispose.await_count == child.frame_element.await_count == 2
    else:
        child.locator.assert_not_called()


@pytest.mark.parametrize("stall", ["frame_element", "is_visible", "dispose"])
def test_success_probe_frame_visibility_and_cleanup_share_deadline(stall):
    async def run():
        async def hang():
            await asyncio.Event().wait()

        main = SimpleNamespace()
        handle = SimpleNamespace(is_visible=AsyncMock(return_value=True), dispose=AsyncMock())
        child = SimpleNamespace(url="about:srcdoc", parent_frame=main,
                                frame_element=AsyncMock(return_value=handle), locator=Mock())
        target = child if stall == "frame_element" else handle
        getattr(target, stall).side_effect = hang
        page = SimpleNamespace(url="https://site.test/login", main_frame=main, frames=[child])
        started = time.monotonic()
        assert await executor._find_success_locator(page, [".account"], wait_ms=50) is None
        assert time.monotonic() - started < 0.5
        child.locator.assert_not_called()
        if stall == "is_visible":
            handle.dispose.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize("blocked", ["hidden_parent", "cross_origin_parent", "hidden_after_match"])
def test_success_probe_rejects_ineligible_ancestors_even_if_child_reports_visible(blocked):
    visible = SimpleNamespace(is_visible=AsyncMock(return_value=True))
    matches = SimpleNamespace(count=AsyncMock(return_value=1), nth=Mock(return_value=visible))
    main = SimpleNamespace()
    parent_handle = SimpleNamespace(
        is_visible=AsyncMock(return_value=blocked != "hidden_parent"), dispose=AsyncMock(),
    )
    parent = SimpleNamespace(url="https://other.test/" if blocked == "cross_origin_parent" else "about:blank",
                             parent_frame=main, frame_element=AsyncMock(return_value=parent_handle))
    child_handle = SimpleNamespace(is_visible=AsyncMock(return_value=True), dispose=AsyncMock())
    if blocked == "hidden_after_match":
        child_handle.is_visible.side_effect = [True, False]
    child = SimpleNamespace(url="about:srcdoc", parent_frame=parent,
                            frame_element=AsyncMock(return_value=child_handle), locator=Mock(return_value=matches))
    page = SimpleNamespace(url="https://site.test/login", main_frame=main, frames=[child])
    assert asyncio.run(executor._find_success_locator(page, [".account"], wait_ms=40)) is None
    assert child_handle.dispose.await_count == child.frame_element.await_count
    assert parent_handle.dispose.await_count == parent.frame_element.await_count
    if blocked == "hidden_after_match":
        visible.is_visible.assert_awaited_once()
        assert child_handle.dispose.await_count == 2
    else:
        child.locator.assert_not_called()


@pytest.mark.parametrize("mode", [
    "delayed", "hidden_first", "iframe", "blank_iframe", "same_origin_iframe", "nested_iframe",
    "hidden_iframe", "hidden_ancestor", "cross_origin_iframe", "cross_origin_ancestor",
    "cross_origin_srcdoc_ancestor", "hidden", "missing",
])
def test_real_chromium_login_waits_for_visible_evidence_without_replay(mode):
    async def run():
        from playwright.async_api import async_playwright

        candidates = list(Path("/opt/opencode/.cache/ms-playwright").glob(
            "chromium_headless_shell-*/**/chrome-headless-shell"))
        candidates += list(Path("/opt/opencode/.cache/ms-playwright").glob(
            "chromium_headless_shell-*/**/headless_shell"))
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(
                    executable_path=str(candidates[0]) if candidates else None, headless=True,
                )
            except Exception:
                pytest.skip("Local Chromium executable unavailable")
            try:
                page = await browser.new_page()
                seen = []
                html = """<form method="post" action="/api/login">
                    <input id="u" name="username"><input id="p" name="password" type="password">
                    <button id="s">Login</button></form>
                    <script>document.querySelector('form').onsubmit = async e => {
                        e.preventDefault();
                        await fetch('/api/login', {method:'POST', body:new URLSearchParams(new FormData(e.target))});
                        document.body.innerHTML = '<h1>Account ready</h1>';
                        const mode = MODE;
                        if (mode === 'delayed') setTimeout(() => {
                            document.body.insertAdjacentHTML('beforeend', '<header><span data-slot="avatar">Account</span></header>');
                        }, 2000);
                        if (mode === 'hidden_first') document.body.insertAdjacentHTML('beforeend',
                            '<span data-slot="avatar" hidden>Hidden template</span><header><span data-slot="avatar">Account</span></header>');
                        if (mode === 'hidden') document.body.insertAdjacentHTML('beforeend',
                            '<span data-slot="avatar" hidden>Hidden template</span>');
                        if (['iframe', 'blank_iframe', 'same_origin_iframe', 'nested_iframe', 'hidden_iframe',
                             'hidden_ancestor', 'cross_origin_iframe', 'cross_origin_ancestor',
                             'cross_origin_srcdoc_ancestor'].includes(mode)) {
                            const frame = document.createElement('iframe');
                            const avatar = '<header><span data-slot="avatar">Account</span></header>';
                            if (mode.startsWith('cross_origin_'))
                                frame.src = 'https://other.test/' + mode;
                            else if (mode === 'same_origin_iframe') frame.src = '/frame';
                            else if (mode === 'hidden_ancestor' || mode === 'nested_iframe') frame.srcdoc =
                                '<iframe srcdoc="&lt;span data-slot=avatar&gt;Account&lt;/span&gt;"></iframe>';
                            else if (mode !== 'blank_iframe') frame.srcdoc = avatar;
                            if (mode === 'hidden_iframe' || mode === 'hidden_ancestor') frame.hidden = true;
                            document.body.append(frame);
                            if (mode === 'blank_iframe') frame.contentDocument.body.innerHTML = avatar;
                        }
                    };</script>""".replace("MODE", json.dumps(mode))

                async def route(request_route):
                    request = request_route.request
                    seen.append((request.method, request.url))
                    content = html if request.method == "GET" else ""
                    if request.url == 'https://other.test/cross_origin_ancestor':
                        content = '<iframe src="https://site.test/frame"></iframe>'
                    elif request.url == 'https://other.test/cross_origin_srcdoc_ancestor':
                        content = '<iframe srcdoc="&lt;span data-slot=avatar&gt;Account&lt;/span&gt;"></iframe>'
                    elif request.url.endswith('/frame') or request.url == 'https://other.test/cross_origin_iframe':
                        content = '<header><span data-slot="avatar">Account</span></header>'
                    await request_route.fulfill(
                        content_type="text/html", body=content,
                    )

                await page.route("https://**/*", route)
                await page.goto("https://site.test/login")
                selector = '[data-slot="avatar"]'
                signals = {"logged_in": {"include_texts": ["Account ready"]}}
                assert await executor._condition_allows(page, "signal:logged_in:true", signals) == "false"
                request = login_request(4 if mode == "delayed" else 1)
                request.step["config"]["success_selector"] = ["#missing", selector]
                started = time.monotonic()
                status, _, data = await executor._run_step(
                    page, request.step, request.credentials.model_dump(),
                    None, None, None, None, "general",
                )
                elapsed = time.monotonic() - started
                expected = mode in {"delayed", "hidden_first", "iframe", "blank_iframe", "same_origin_iframe", "nested_iframe"}
                assert status == ("success" if expected else "fail")
                assert data["submitted"] is True and data["verified"] is expected
                if not expected:
                    assert data["reason"] == "success_evidence_missing"
                    assert data["field"] == "success_selector"
                    evidence = data["success_evidence"]
                    assert evidence["captured_after_timeout"] is True
                    assert evidence["snapshot_consistency"] == "sampled_after_timeout"
                    assert 900 <= evidence["waited_ms"] < 2000
                    assert [row["selector_index"] for row in evidence["selectors"]] == [0, 1]
                    if mode in {"hidden", "missing"}:
                        assert evidence["reason"] == ("matches_hidden" if mode == "hidden" else "selector_missing")
                        assert evidence["selectors"][1]["match_count"] == (1 if mode == "hidden" else 0)
                        assert evidence["selectors"][1]["visible_count"] == 0
                    else:
                        assert evidence["reason"] == "frame_scope_excluded"
                        excluded = evidence["selectors"][1]["frames"][1:]
                        assert excluded and all(row["error"] == "frame_not_eligible" for row in excluded)
                        assert all(row["match_count"] is row["visible_count"] is None for row in excluded)
                    assert selector not in json.dumps(evidence)
                else:
                    assert "success_evidence" not in data
                if mode == "delayed":
                    assert 2 <= elapsed < 5
                assert [row for row in seen if row[0] == "POST"] == [("POST", "https://site.test/api/login")]
                assert seen.count(("GET", "https://site.test/login")) == 1
                assert all(method == "GET" for method, url in seen if url.endswith('/frame') or 'other.test' in url)
                assert page.url == "https://site.test/login"
                assert await executor._condition_allows(page, "signal:logged_in:true", signals) == "true"
                if mode == "hidden":
                    assert await executor._selector_exists(page, selector) is True
                if mode == "missing":
                    invalid = await executor._login_success_evidence(page, ["["], waited_ms=0)
                    assert invalid["reason"] == "read_incomplete"
                    assert invalid["selectors"][0]["frames"][0]["error"] == "invalid_selector"
                if mode in {"hidden_iframe", "hidden_ancestor", "cross_origin_iframe",
                            "cross_origin_ancestor", "cross_origin_srcdoc_ancestor"}:
                    assert await executor._selector_exists(page, selector) is True
                if mode.startswith("cross_origin_"):
                    assert any([await frame.locator(selector).first.is_visible() for frame in page.frames
                                if await frame.locator(selector).count()])
                assert "fixture-password" not in repr(data)
            finally:
                await browser.close()
    asyncio.run(run())
