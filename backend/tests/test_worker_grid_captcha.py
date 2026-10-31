from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker import grid_captcha as grid, vision  # noqa: E402


VISION = {"base_url": "https://private.invalid", "model": "test", "api_key": "secret",
          "supports_vision": True, "execution_settings": {"llm_prompt": "custom"}}


class Unclear(ValueError):
    pass


class Element:
    def __init__(self, page, kind):
        self.page = page
        self.kind = kind
        self.visible = True

    async def is_visible(self):
        return self.visible

    async def element_handle(self):
        return self

    async def dispose(self):
        pass

    async def scroll_into_view_if_needed(self, **kwargs):
        pass

    async def evaluate_handle(self, script, args):
        assert script == grid._SNAPSHOT
        assert args["cellSelector"] in (".geetest_item", ".custom")
        return State(self.page)

    def as_element(self):
        return self

    async def screenshot(self, **kwargs):
        assert kwargs["type"] == "png"
        return b"PNG:target-and-grid"


class State:
    def __init__(self, page):
        self.page = page
        self.identity = page.identity
        self.source = page.source
        self.disposed = False
        page.states.append(self)

    async def dispose(self):
        assert not self.disposed
        self.disposed = True

    async def evaluate_handle(self, script):
        assert script == "state => state.root"
        return self.page.root

    async def evaluate(self, script, args=None):
        page = self.page
        valid = (page.count == 9 and page.clickable and page.root.visible and
                 self.identity == page.identity)
        if script == "state => state.read().valid":
            return valid
        if script == "state => state.read().selected.length > 0":
            return bool(page.selected)
        if script == grid._ACT:
            if not valid or page.source != self.source or sorted(page.selected) != args["selected"]:
                return False
            if args["index"] is not None:
                index = args["index"]
                page.cell_clicks.append(index)
                if page.select_works:
                    page.selected.add(index)
                if page.on_cell:
                    page.on_cell()
            elif args["button"]:
                button = args["button"]
                if not button.visible:
                    return False
                if button.kind == "refresh":
                    page.refreshes += 1
                    if page.refresh_changes:
                        page.source += 1
                        page.identity += 1
                        page.selected.clear()
                else:
                    page.submits += 1
                    if page.on_submit:
                        page.on_submit()
            return True
        assert "old.root !== fresh.root" in script
        return self.identity != args.identity or self.source != args.source


class Locator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    async def all(self):
        if self.selector == "[invalid":
            raise ValueError("invalid selector")
        return self.page.elements.get(self.selector, [])

    async def inner_text(self, **kwargs):
        if self.page.text is None:
            raise RuntimeError("body unavailable")
        return self.page.text


class Page:
    def __init__(self):
        self.count = 9
        self.states = []
        self.clickable = True
        self.identity = 1
        self.source = 1
        self.selected = set()
        self.cell_clicks = []
        self.refreshes = self.submits = 0
        self.refresh_changes = self.select_works = True
        self.on_cell = self.on_submit = None
        self.text = ""
        self.root = Element(self, "root")
        self.elements = {
            ".geetest_panel_box": [self.root],
            ".geetest_refresh": [Element(self, "refresh")],
            ".geetest_commit": [Element(self, "submit")],
        }

    def locator(self, selector):
        return Locator(self, selector)


@pytest.fixture
def setup(monkeypatch):
    page = Page()
    logs, calls = [], []

    async def answer(image, config, custom_prompt=None):
        calls.append((image, config, custom_prompt))
        return [5, 1]

    monkeypatch.setattr(vision, "GridCaptchaUnclear", Unclear, raising=False)
    monkeypatch.setattr(vision, "answer_grid_captcha", answer, raising=False)
    # Poll once rather than waiting wall-clock seconds in negative tests.
    ticks = iter(range(0, 100000, 200))
    monkeypatch.setattr(grid, "time", type("Clock", (), {"monotonic": lambda: next(ticks)}))
    return page, logs, calls


@pytest.fixture(params=[(None, 4), (0, 1), (1, 2), (-8, 1), (50, 11), ("bad", 4)])
def retry_budget(request):
    limit, expected = request.param
    return ({} if limit is None else {"max_refresh": limit}), expected


def run(page, logs, config=None, vision_config=VISION, capture=None):
    return asyncio.run(grid.run_grid_captcha(
        page, config or {}, lambda level, message: logs.append((level, message)),
        vision_config, capture,
    ))


@pytest.mark.parametrize("vision_config", [None, {}, {**VISION, "api_key": ""},
    {**VISION, "base_url": ""}, {**VISION, "model": ""},
    {key: value for key, value in VISION.items() if key != "supports_vision"},
    {**VISION, "supports_vision": False}, {**VISION, "supports_vision": "true"}])
def test_vision_gate_never_touches_page(setup, vision_config):
    _, logs, calls = setup
    result = run(object(), logs, vision_config=vision_config)
    assert result[0] == "waiting_human"
    assert not calls


@pytest.mark.parametrize("count,clickable", [(0, True), (8, True), (10, True), (9, False)])
def test_requires_nine_visible_clickable_cells(setup, count, clickable):
    page, logs, calls = setup
    page.count, page.clickable = count, clickable
    result = run(page, logs)
    assert result[2]["reason"] == "invalid_grid"
    assert not calls and not page.cell_clicks and not page.submits


def test_capture_prompt_selection_and_positive_text(setup):
    page, logs, calls = setup
    captured = []

    async def capture(image, label):
        captured.append((image, label))

    page.on_submit = lambda: setattr(page, "text", "今天已經簽到過啦")
    result = run(page, logs, capture=capture)
    assert result[0] == "success"
    assert result[2]["selected_indices"] == [1, 5]
    assert calls == [(b"PNG:target-and-grid", VISION, "custom")]
    assert captured == [(b"PNG:target-and-grid", "Grid captcha")]
    assert page.cell_clicks == [1, 5] and page.submits == 1


@pytest.mark.parametrize("answer", [[], [0], [10], [True], [1, 1], [1.0], "1", None, ["1"]])
def test_invalid_mocked_model_answer_never_clicks(setup, monkeypatch, answer):
    page, logs, _ = setup

    async def model(*args):
        return answer

    monkeypatch.setattr(vision, "answer_grid_captcha", model)
    result = run(page, logs)
    assert result[2]["reason"] == "invalid_answer"
    assert not page.cell_clicks and not page.submits


def test_preselected_grid_is_rejected_without_toggling(setup):
    page, logs, calls = setup
    page.selected.add(1)
    result = run(page, logs)
    assert result[2]["reason"] == "unsafe_state"
    assert not calls and not page.cell_clicks and not page.refreshes and not page.submits


@pytest.mark.parametrize("change", ["source", "identity", "selected"])
def test_stale_model_result_cannot_click_replacement(setup, monkeypatch, change):
    page, logs, calls = setup

    async def model(*args):
        calls.append(page.source)
        if len(calls) == 1:
            if change == "selected":
                page.selected.add(2)
            else:
                setattr(page, change, 2)
            return [1, 5]
        return [3]

    monkeypatch.setattr(vision, "answer_grid_captcha", model)
    page.on_submit = lambda: setattr(page, "text", "验证成功")
    result = run(page, logs)
    if change == "selected":
        assert result[2]["reason"] == "stale_challenge"
        assert not page.cell_clicks and not page.submits
    else:
        assert result[0] == "success"
        assert page.cell_clicks == [3] and page.submits == 1 and len(calls) == 2


@pytest.mark.parametrize("change", ["source", "identity", "extra_selection", "no_selection"])
def test_selection_or_challenge_change_prevents_submit(setup, change):
    page, logs, _ = setup
    if change == "no_selection":
        page.select_works = False
    elif change == "extra_selection":
        page.on_cell = lambda: page.selected.add(9)
    else:
        page.on_cell = lambda: setattr(page, change, 2)
    result = run(page, logs)
    assert result[0] == "waiting_human"
    assert page.cell_clicks == [1] and page.submits == 0


@pytest.mark.parametrize("change", ["source", "identity", "extra_selection", "no_selection"])
def test_final_selection_is_verified_before_submit(setup, change):
    page, logs, _ = setup

    def after_click():
        if len(page.cell_clicks) != 2:
            return
        if change == "extra_selection":
            page.selected.add(9)
        elif change == "no_selection":
            page.selected.discard(5)
        else:
            setattr(page, change, 2)

    page.on_cell = after_click
    result = run(page, logs)
    assert result[2]["reason"] == "unsafe_selection"
    assert page.cell_clicks == [1, 5] and page.submits == 0


def test_unclear_refresh_then_fresh_answer(setup, monkeypatch):
    page, logs, calls = setup

    async def model(*args):
        calls.append(page.source)
        if len(calls) == 1:
            raise Unclear("private provider response with URL and cookies")
        return [2]

    monkeypatch.setattr(vision, "answer_grid_captcha", model)
    page.on_submit = lambda: setattr(page, "text", "验证成功")
    result = run(page, logs)
    assert result[0] == "success"
    assert calls == [1, 2] and page.refreshes == 1 and page.cell_clicks == [2]
    assert result[2]["attempts"] == result[2]["verification_attempts"] == 2
    assert result[2]["retries"] == 1
    assert "private provider" not in str(logs)
    assert all(state.disposed for state in page.states)


@pytest.mark.parametrize("reason,status", [("http_error", 400), ("timeout", None),
                                          ("network_error", None), ("invalid_response", None)])
def test_provider_failure_logs_only_safe_diagnostics(setup, monkeypatch, reason, status):
    page, logs, _ = setup

    async def model(*args):
        error = ValueError("private API key, provider body and URL")
        error.reason = reason
        if status is not None:
            error.status_code = status
        raise error

    monkeypatch.setattr(vision, "answer_grid_captcha", model)
    result = run(page, logs)
    assert result[0] == "waiting_human"
    assert result[2]["provider_error"] == reason
    if status:
        assert result[2]["http_status"] == status
        assert "HTTP 400" in str(logs)
    assert reason in str(logs)
    assert "private" not in str(logs) + str(result)
    assert not page.cell_clicks and not page.submits


def test_refresh_polls_until_the_challenge_actually_changes(setup, monkeypatch):
    page, logs, calls = setup
    ticks = iter(range(1000))
    monkeypatch.setattr(grid, "time", type("Clock", (), {"monotonic": lambda: next(ticks)}))
    page.refresh_changes = False
    polls = []

    async def sleep(seconds):
        if not page.refreshes:
            return
        polls.append(seconds)
        # The first poll is unchanged; only a later DOM update is accepted.
        if len(polls) == 2:
            page.source += 1

    async def model(*args):
        calls.append(page.source)
        if len(calls) == 1:
            raise Unclear()
        return [2]

    monkeypatch.setattr(grid.asyncio, "sleep", sleep)
    monkeypatch.setattr(vision, "answer_grid_captcha", model)
    page.on_submit = lambda: setattr(page, "text", "验证成功")
    assert run(page, logs)[0] == "success"
    assert polls == [0.1, 0.1] and calls == [1, 2]


def test_unclear_exhaustion_is_bounded(setup, monkeypatch, retry_budget):
    page, logs, calls = setup

    async def model(*args):
        calls.append(1)
        raise Unclear("do not log")

    monkeypatch.setattr(vision, "answer_grid_captcha", model)
    config, expected = retry_budget
    result = run(page, logs, config)
    assert result[2]["reason"] == "vision_unclear"
    assert len(calls) == expected and page.refreshes == expected - 1
    assert result[2]["attempts"] == result[2]["verification_attempts"] == expected
    assert result[2]["retries"] == expected - 1
    assert not page.cell_clicks and not page.submits
    assert all(state.disposed for state in page.states)


@pytest.mark.parametrize("failure", ["unchanged", "missing", "stale", "preselected_fresh"])
def test_refresh_requires_new_safe_challenge(setup, monkeypatch, failure):
    page, logs, calls = setup
    page.refresh_changes = failure != "unchanged"
    if failure == "missing":
        page.elements[".geetest_refresh"] = []

    async def model(*args):
        calls.append(1)
        if failure == "stale":
            page.source += 1
        raise Unclear()

    if failure == "preselected_fresh":
        original = grid._snapshot

        async def snapshot(*args):
            if page.refreshes:
                page.selected.add(1)
            return await original(*args)

        monkeypatch.setattr(grid, "_snapshot", snapshot)
    monkeypatch.setattr(vision, "answer_grid_captcha", model)
    result = run(page, logs)
    assert result[2]["reason"] == "refresh_unavailable"
    assert len(calls) == 1 and not page.cell_clicks and not page.submits


@pytest.mark.parametrize("failure", ["no_signal", "disappears", "hidden_success", "one_missing",
                                     "invalid_missing"])
def test_no_false_success_or_replay_after_submit(setup, failure):
    page, logs, calls = setup
    config = {"success_signals": []}
    if failure == "disappears":
        page.on_submit = lambda: setattr(page.root, "visible", False)
    if failure == "hidden_success":
        element = Element(page, "success")
        element.visible = False
        page.elements[".success"] = [element]
        config["success_selector"] = [".success"]
    if failure == "one_missing":
        config["success_when_missing"] = [".absent", ".geetest_panel_box"]
    if failure == "invalid_missing":
        config["success_when_missing"] = [".absent", "[invalid"]
    result = run(page, logs, config)
    assert result[0] == "unknown" and result[2]["reason"] == "success_timeout"
    assert len(calls) == 1 and page.submits == 1 and not page.refreshes


@pytest.mark.parametrize("mode", ["visible", "missing", "hidden"])
def test_explicit_success_selectors(setup, mode):
    page, logs, _ = setup
    config = {"success_signals": []}
    if mode == "visible":
        page.elements[".success"] = [Element(page, "success")]
        config["success_selector"] = [".success"]
    else:
        config["success_when_missing"] = [".absent", ".geetest_panel_box"]
        if mode == "hidden":
            page.on_submit = lambda: setattr(page.root, "visible", False)
        else:
            page.on_submit = lambda: page.elements.pop(".geetest_panel_box")
    assert run(page, logs, config)[0] == "success"


def test_success_checks_are_independent_or_conditions(setup):
    page, logs, _ = setup
    page.text = None
    page.elements[".success"] = [Element(page, "success")]
    result = run(page, logs, {"success_selector": ["[invalid", ".success"]})
    assert result[0] == "success" and result[2]["success_by"] == "selector"


def test_submit_missing_does_not_report_success(setup):
    page, logs, _ = setup
    page.elements[".geetest_commit"] = []
    assert run(page, logs)[2]["reason"] == "submit_unavailable"
    assert page.submits == 0 and page.refreshes == 0


@pytest.mark.parametrize("regenerated", [False, True])
def test_ambiguous_submit_failure_is_not_retried(setup, regenerated):
    page, logs, calls = setup

    def submit():
        if regenerated:
            page.source += 1
            page.selected.clear()
        raise RuntimeError("private URL and cookies")

    page.on_submit = submit
    result = run(page, logs)
    assert result[0] == "unknown"
    assert "private" not in str(result) + str(logs)
    assert page.submits == 1 and page.refreshes == 0 and len(calls) == 1
    assert all(state.disposed for state in page.states)


def test_capture_failure_is_sanitized_and_nonfatal(setup):
    page, logs, _ = setup

    async def capture(*args):
        raise RuntimeError("private provider cookie")

    page.on_submit = lambda: setattr(page, "text", "验证成功")
    assert run(page, logs, capture=capture)[0] == "success"
    assert "private" not in str(logs)


def test_provider_failure_is_sanitized(setup, monkeypatch):
    page, logs, _ = setup

    async def model(*args):
        raise RuntimeError("https://secret.invalid cookie=secret provider raw text")

    monkeypatch.setattr(vision, "answer_grid_captcha", model)
    result = run(page, logs)
    assert result[2]["reason"] == "vision_failed"
    assert "secret" not in str(result) + str(logs)
    assert not page.cell_clicks and not page.submits


def test_cancellation_propagates(setup, monkeypatch):
    page, logs, _ = setup

    async def model(*args):
        raise asyncio.CancelledError()

    monkeypatch.setattr(vision, "answer_grid_captcha", model)
    with pytest.raises(asyncio.CancelledError):
        run(page, logs)
    assert not page.cell_clicks and not page.submits
    assert all(state.disposed for state in page.states)


def test_bounds():
    assert grid._bounded({}, "success_wait_seconds", 10, 1, 120) == 10
    assert grid._bounded({"x": 0}, "x", 10, 1, 120) == 1
    assert grid._bounded({"x": 999}, "x", 10, 1, 120) == 120


@pytest.mark.parametrize("change", ["source", "identity"])
def test_wrong_selection_new_challenge_is_recognized_again(setup, monkeypatch, change):
    page, logs, calls = setup

    async def model(*args):
        calls.append((page.identity, page.source))
        return [1, 5] if len(calls) == 1 else [3, 9]

    def submit():
        if page.submits == 1:
            setattr(page, change, getattr(page, change) + 1)
            page.selected.clear()
        else:
            page.text = "验证成功"

    monkeypatch.setattr(vision, "answer_grid_captcha", model)
    page.on_submit = submit
    result = run(page, logs)
    assert result[0] == "success"
    assert calls[0] != calls[1] and len(calls) == 2
    assert page.cell_clicks == [1, 5, 3, 9] and page.submits == 2
    assert result[2] == {"selected_indices": [3, 9], "success_by": "text",
                         "attempts": 2, "verification_attempts": 2, "retries": 1}
    assert any(level == "WARN" and "verification_attempts=1" in message
               and "retries=0/3" in message for level, message in logs)
    assert all(state.disposed for state in page.states)


def test_regenerated_failures_exhaust_verification_budget(setup, retry_budget):
    page, logs, calls = setup

    def submit():
        page.source += 1
        page.selected.clear()

    page.on_submit = submit
    config, expected = retry_budget
    result = run(page, logs, config)
    assert result[0] == "waiting_human" and result[2]["reason"] == "retry_exhausted"
    assert page.submits == len(calls) == expected
    assert result[2]["attempts"] == result[2]["verification_attempts"] == expected
    assert result[2]["retries"] == expected - 1
    assert not page.refreshes and all(state.disposed for state in page.states)


def test_unclear_and_wrong_answers_share_default_budget(setup, monkeypatch):
    page, logs, calls = setup

    async def model(*args):
        calls.append(page.source)
        if len(calls) % 3:
            raise Unclear()
        return [page.submits + 1]

    def submit():
        page.source += 1
        page.selected.clear()

    monkeypatch.setattr(vision, "answer_grid_captcha", model)
    page.on_submit = submit
    result = run(page, logs)
    assert result[2]["reason"] == "vision_unclear"
    assert page.submits == 1 and page.refreshes == 2 and len(calls) == 4
    assert result[2]["attempts"] == result[2]["verification_attempts"] == 4
    assert result[2]["retries"] == 3 and calls == [1, 2, 3, 4]
    assert page.cell_clicks == [1]
    assert all(state.disposed for state in page.states)


@pytest.mark.parametrize("outcomes,budget,reason,refreshes,submits", [
    (["unclear", "wrong", "unclear", "wrong"], None, "retry_exhausted", 2, 2),
    (["wrong", "unclear", "wrong", "unclear"], None, "vision_unclear", 1, 2),
    (["unclear", "wrong", "success"], 2, None, 1, 2),
    (["wrong", "unclear", "success"], 2, None, 1, 2),
    (["unclear", "wrong", "unclear"], 2, "vision_unclear", 1, 1),
    (["wrong", "unclear", "wrong"], 2, "retry_exhausted", 1, 2),
])
def test_mixed_alternation_uses_one_total_budget(
        setup, monkeypatch, outcomes, budget, reason, refreshes, submits):
    page, logs, calls = setup

    async def model(*args):
        calls.append(page.source)
        if outcomes[len(calls) - 1] == "unclear":
            raise Unclear("private provider response")
        return [len(calls)]

    def submit():
        if outcomes[len(calls) - 1] == "success":
            page.text = "验证成功"
        else:
            page.source += 1
            page.selected.clear()

    monkeypatch.setattr(vision, "answer_grid_captcha", model)
    page.on_submit = submit
    config = {} if budget is None else {"max_refresh": budget}
    result = run(page, logs, config)
    assert result[0] == ("waiting_human" if reason else "success")
    assert result[2].get("reason") == reason
    assert calls == list(range(1, len(outcomes) + 1))
    assert page.refreshes == refreshes and page.submits == submits
    assert result[2]["attempts"] == result[2]["verification_attempts"] == len(outcomes)
    assert result[2]["retries"] == len(outcomes) - 1
    assert "private" not in str(result) + str(logs)
    assert all(state.disposed for state in page.states)


@pytest.mark.parametrize("failure", ["unclear", "wrong", "stale_model", "stale_capture"])
def test_zero_budget_never_refreshes_or_replays(setup, monkeypatch, failure):
    page, logs, calls = setup
    captures = []

    async def screenshot(self, **kwargs):
        captures.append(page.source)
        if failure == "stale_capture":
            page.source += 1
        return b"PNG"

    async def model(*args):
        calls.append(page.source)
        if failure == "unclear":
            raise Unclear()
        if failure == "stale_model":
            page.source += 1
        return [1]

    def submit():
        page.source += 1
        page.selected.clear()

    monkeypatch.setattr(Element, "screenshot", screenshot)
    monkeypatch.setattr(vision, "answer_grid_captcha", model)
    page.on_submit = submit
    result = run(page, logs, {"max_refresh": 0})
    assert result[0] == "waiting_human"
    assert result[2]["reason"] == ("vision_unclear" if failure == "unclear" else "retry_exhausted")
    assert captures == [1] and not page.refreshes
    assert len(calls) == result[2]["attempts"] == (0 if failure == "stale_capture" else 1)
    assert result[2]["verification_attempts"] == 1 and result[2]["retries"] == 0
    assert page.submits == (1 if failure == "wrong" else 0)
    assert page.cell_clicks == ([1] if failure == "wrong" else [])
    assert all(state.disposed for state in page.states)


@pytest.mark.parametrize("stale", ["model", "capture"])
def test_stale_rounds_share_budget_with_unclear_and_wrong(setup, monkeypatch, stale):
    page, logs, calls = setup
    captures = []

    async def screenshot(self, **kwargs):
        captures.append(page.source)
        if stale == "capture" and len(captures) == 3:
            page.source += 1
        return str(captures[-1]).encode()

    async def model(*args):
        calls.append(page.source)
        if page.source in {1, 4}:
            raise Unclear()
        if stale == "model" and page.source == 3:
            page.source += 1
        return [1]

    def submit():
        page.source += 1
        page.selected.clear()

    monkeypatch.setattr(Element, "screenshot", screenshot)
    monkeypatch.setattr(vision, "answer_grid_captcha", model)
    page.on_submit = submit
    result = run(page, logs)
    assert result[2]["reason"] == "vision_unclear"
    assert captures == [1, 2, 3, 4]
    assert calls == ([1, 2, 3, 4] if stale == "model" else [1, 2, 4])
    assert result[2]["attempts"] == len(calls)
    assert result[2]["verification_attempts"] == 4 and result[2]["retries"] == 3
    assert page.refreshes == page.submits == 1 and page.cell_clicks == [1]
    assert all(state.disposed for state in page.states)


@pytest.mark.parametrize("change", ["unchanged_reset", "preselected", "invalid_grid"])
def test_submit_reset_without_proven_empty_new_grid_never_replays(setup, change):
    page, logs, calls = setup

    def submit():
        page.selected.clear()
        if change != "unchanged_reset":
            page.source += 1
        if change == "preselected":
            page.selected.add(9)
        if change == "invalid_grid":
            page.count = 8
        page.text = "wrong selection; try again"

    page.on_submit = submit
    result = run(page, logs)
    assert result[2]["reason"] == "success_timeout"
    assert page.submits == len(calls) == 1 and page.cell_clicks == [1, 5]
    assert all(state.disposed for state in page.states)


def test_submit_waits_for_delayed_regenerated_grid(setup, monkeypatch):
    page, logs, calls = setup
    ticks = iter(range(1000))
    monkeypatch.setattr(grid, "time", type("Clock", (), {"monotonic": lambda: next(ticks)}))
    polls = []

    async def sleep(seconds):
        if not page.submits:
            assert not calls
            return
        polls.append(seconds)
        if len(polls) == 2:
            page.source += 1
            page.selected.clear()

    def submit():
        if page.submits == 2:
            page.text = "验证成功"

    monkeypatch.setattr(grid.asyncio, "sleep", sleep)
    page.on_submit = submit
    assert run(page, logs)[0] == "success"
    assert polls == [0.2, 0.2] and len(calls) == page.submits == 2


def test_initial_grid_changes_before_first_guard_waits_before_model(setup, monkeypatch):
    page, logs, calls = setup
    ticks = iter(range(1000))
    monkeypatch.setattr(grid, "time", type("Clock", (), {"monotonic": lambda: next(ticks)}))
    original = grid._act
    checks, polls = [], []

    async def act(state, selected, **kwargs):
        checks.append(True)
        if len(checks) == 1:
            page.source += 1
        return await original(state, selected, **kwargs)

    async def sleep(seconds):
        assert not calls and not page.cell_clicks
        polls.append(seconds)

    monkeypatch.setattr(grid, "_act", act)
    monkeypatch.setattr(grid.asyncio, "sleep", sleep)
    page.on_submit = lambda: setattr(page, "text", "验证成功")
    result = run(page, logs)
    assert result[0] == "success" and polls == [0.2, 0.1] and len(calls) == 1
    assert result[2]["verification_attempts"] == 1 and result[2]["retries"] == 0
    assert all(state.disposed for state in page.states)


@pytest.mark.parametrize("change", ["source", "identity", "selected"])
def test_initial_grid_must_remain_empty_and_unchanged_while_settling(
        setup, monkeypatch, change):
    page, logs, calls = setup
    ticks = iter(range(1000))
    monkeypatch.setattr(grid, "time", type("Clock", (), {"monotonic": lambda: next(ticks)}))
    polls = []

    async def sleep(seconds):
        assert not calls and not page.cell_clicks and not page.submits
        polls.append(seconds)
        if len(polls) == 1:
            if change == "selected":
                page.selected.add(2)
            else:
                setattr(page, change, 2)

    monkeypatch.setattr(grid.asyncio, "sleep", sleep)
    page.on_submit = lambda: setattr(page, "text", "验证成功")
    result = run(page, logs)
    if change == "selected":
        assert result[2]["reason"] == "unsafe_state"
        assert not calls and not page.cell_clicks and not page.submits
    else:
        assert result[0] == "success" and polls == [0.1, 0.2, 0.1]
        assert len(calls) == 1 and result[2]["retries"] == 0
    assert all(state.disposed for state in page.states)


def test_cancellation_during_initial_settle_disposes_state(setup, monkeypatch):
    page, logs, calls = setup

    async def sleep(seconds):
        raise asyncio.CancelledError()

    monkeypatch.setattr(grid.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        run(page, logs)
    assert not calls and all(state.disposed for state in page.states)


def test_never_stable_initial_grid_times_out_without_model(setup, monkeypatch):
    page, logs, calls = setup
    ticks = iter(range(1000))
    monkeypatch.setattr(grid, "time", type("Clock", (), {"monotonic": lambda: next(ticks)}))

    async def sleep(seconds):
        page.source += 1

    monkeypatch.setattr(grid.asyncio, "sleep", sleep)
    result = run(page, logs)
    assert result[2]["reason"] == "invalid_grid"
    assert result[2]["attempts"] == 0 and result[2]["retries"] == 0
    assert not calls and not page.cell_clicks and not page.submits
    assert all(state.disposed for state in page.states)


@pytest.mark.parametrize("raises", [False, True])
def test_capture_changed_challenge_discards_old_image(setup, monkeypatch, raises):
    page, logs, calls = setup
    captures = []

    async def screenshot(self, **kwargs):
        captures.append(page.source)
        if len(captures) == 1:
            page.source += 1
            if raises:
                raise RuntimeError("private detached image")
        return str(captures[-1]).encode()

    monkeypatch.setattr(Element, "screenshot", screenshot)
    page.on_submit = lambda: setattr(page, "text", "验证成功")
    result = run(page, logs)
    assert result[0] == "success"
    assert captures == [1, 2] and len(calls) == 1 and calls[0][0] == b"2"
    assert page.submits == 1 and result[2]["retries"] == 1
    assert all(state.disposed for state in page.states)


def test_cancellation_while_waiting_for_regeneration_disposes_handles(setup, monkeypatch):
    page, logs, _ = setup
    original = grid._changed

    async def changed(*args):
        if page.submits:
            raise asyncio.CancelledError()
        return await original(*args)

    monkeypatch.setattr(grid, "_changed", changed)
    with pytest.raises(asyncio.CancelledError):
        run(page, logs)
    assert page.submits == 1 and all(state.disposed for state in page.states)


@pytest.mark.parametrize("when", ["select", "submit_guard"])
def test_regeneration_during_selection_requires_new_answer(setup, monkeypatch, when):
    page, logs, calls = setup

    async def model(*args):
        calls.append(page.source)
        return [1, 5] if len(calls) == 1 else [7]

    def cell():
        if len(page.cell_clicks) == (1 if when == "select" else 2):
            page.source += 1
            page.selected.clear()

    monkeypatch.setattr(vision, "answer_grid_captcha", model)
    page.on_cell = cell
    page.on_submit = lambda: setattr(page, "text", "验证成功")
    assert run(page, logs)[0] == "success"
    assert calls == [1, 2] and page.submits == 1
    assert page.cell_clicks == ([1, 7] if when == "select" else [1, 5, 7])
    assert all(state.disposed for state in page.states)


def test_snapshot_does_not_use_sdk_handle_scrolling(setup, monkeypatch):
    page, logs, _ = setup

    async def forbidden(*args, **kwargs):
        raise AssertionError("Ambiguous ElementHandle humanized scrolling must not run")

    monkeypatch.setattr(Element, "scroll_into_view_if_needed", forbidden)
    page.on_submit = lambda: setattr(page, "text", "验证成功")
    assert run(page, logs)[0] == "success"


def test_loading_panel_read_error_retried_before_model(setup, monkeypatch):
    page, logs, calls = setup
    ticks = iter(range(1000))
    monkeypatch.setattr(grid, "time", type("Clock", (), {"monotonic": lambda: next(ticks)}))
    original = grid._snapshot
    reads = []

    async def snapshot(*args):
        reads.append(True)
        if len(reads) == 1:
            raise RuntimeError("private detached loading panel")
        return await original(*args)

    monkeypatch.setattr(grid, "_snapshot", snapshot)
    page.on_submit = lambda: setattr(page, "text", "验证成功")
    assert run(page, logs)[0] == "success"
    assert len(reads) == 2 and len(calls) == 1
    assert "private" not in str(logs)


def test_permanent_read_failure_has_safe_stage_diagnostic(setup, monkeypatch):
    page, logs, calls = setup

    async def snapshot(*args):
        raise RuntimeError("private URL cookie=value")

    monkeypatch.setattr(grid, "_snapshot", snapshot)
    result = run(page, logs)
    assert result[0] == "unknown"
    assert result[2]["phase"] == "read_grid"
    assert result[2]["error_type"] == "RuntimeError"
    assert result[2]["attempts"] == 0
    assert not calls and not page.submits
    assert "private" not in str(result) + str(logs)


def test_browser_scripts_identity_sources_background_and_selection():
    """Exercise the production JS, not just Python mocks, with a minimal DOM."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to exercise browser JavaScript")
    script = r"""
const assert = require('node:assert/strict');
class Element {
    constructor(tag = 'DIV') {
        this.tagName = tag; this.isConnected = true; this.attrs = {};
        this.children = []; this.childNodes = []; this.selected = false;
        this.background = 'none'; this.clicks = 0; this.ownerDocument = document;
    }
    getClientRects() { return this.isConnected ? [1] : []; }
    getBoundingClientRect() { return {left: 0, top: 0, width: 10, height: 10}; }
    getAttribute(key) { return this.attrs[key] || null; }
    querySelectorAll(selector) { return selector === '*' ? this.children : cells; }
    matches(selector) { return selector === ':disabled' ? false : this.selected; }
    closest() { return null; }
    contains() { return false; }
    scrollIntoView() { document.top = this; }
    click() { this.selected = !this.selected; this.clicks++; }
}
const document = {top: null, elementFromPoint() { return this.top; }};
global.getComputedStyle = el => ({visibility: 'visible', pointerEvents: 'auto',
    backgroundImage: el.background, backgroundPosition: '0 0', backgroundSize: 'auto'});
const root = new Element();
const target = new Element('IMG'); target.attrs.src = 'target-1';
const cells = Array.from({length: 9}, () => new Element());
root.children = [target, ...cells];
const snapshot = SNAPSHOT;
const act = ACT;
const changed = CHANGED;
const options = {cellSelector: '.cell', selectedSelector: '.selected'};
let state = snapshot(root, options);
assert.equal(state.read().valid, true);
assert.equal(act(state, {selected: [], index: 1}), true);
assert.equal(act(state, {selected: [1], index: 2}), true);
assert.deepEqual(state.read().selected, [1, 2]);
assert.equal(act(state, {selected: [], index: 3}), false);
target.attrs.src = 'target-2';
assert.equal(act(state, {selected: [1, 2], index: 3}), false);
state = snapshot(root, options);
target.background = 'url(changed)';
assert.equal(act(state, {selected: [1, 2], index: 3}), false);
state = snapshot(root, options);
root.children[0] = new Element('IMG');
assert.equal(act(state, {selected: [1, 2], index: 3}), false);
root.children[0] = target;
state = snapshot(root, options);
cells[0] = new Element();
assert.equal(act(state, {selected: [1, 2], index: 3}), false);
cells.pop();
assert.equal(snapshot(root, options).read().valid, false);
cells.push(new Element());
root.children = [target, ...cells];
cells.forEach(cell => cell.selected = false);
state = snapshot(root, options);
cells[0].selected = true;
cells[0].attrs.class = 'selected';
target.childNodes.push({nodeType: 3, textContent: 'Try again'});
root.children.push(new Element('SPAN'));
assert.equal(changed(state, snapshot(root, options)), false);
assert.equal(act(state, {selected: [], index: 3}), false);
cells[0].selected = false;
state = snapshot(root, options);
target.attrs.src = 'target-3';
assert.equal(changed(state, snapshot(root, options)), true);
state = snapshot(root, options);
target.currentSrc = 'loaded-responsive-image';
assert.equal(changed(state, snapshot(root, options)), true);
assert.equal(act(state, {selected: [], index: 3}), false);
state = snapshot(root, options);
cells[0] = new Element();
root.children = [target, ...cells];
assert.equal(changed(state, snapshot(root, options)), true);
state = snapshot(root, options);
const replacement = new Element(); replacement.children = root.children;
assert.equal(changed(state, snapshot(replacement, options)), true);
state = snapshot(root, options);
document.elementFromPoint = () => new Element();
assert.equal(act(state, {selected: [], index: 3}), false);
assert.equal(cells[2].clicks, 0);
"""
    script = script.replace("SNAPSHOT", "(" + grid._SNAPSHOT + ")").replace(
        "ACT", "(" + grid._ACT + ")"
    ).replace("CHANGED", "(" + grid._CHANGED + ")")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("mode", ["src", "cells", "root", "text_only", "preselected",
                                  "budget_default", "budget_zero", "settle"])
def test_real_browser_grid_rounds(monkeypatch, mode):
    playwright = pytest.importorskip("playwright.async_api")

    async def exercise():
        async with playwright.async_playwright() as driver:
            executable = os.environ.get("SITEFLOW_TEST_CHROMIUM", driver.chromium.executable_path)
            if not Path(executable).is_file():
                pytest.skip("Set SITEFLOW_TEST_CHROMIUM to run real-browser grid regressions")
            browser = await driver.chromium.launch(executable_path=executable, headless=True,
                                                  args=["--no-sandbox"])
            try:
                page = await browser.new_page()
                await page.set_content("""
                    <style>
                      .geetest_panel_box { width: 300px; }
                      .geetest_item { width: 80px; height: 60px; margin: 2px; }
                      .selected { outline: 2px solid red; }
                    </style>
                    <div class="geetest_panel_box">
                      <img id="target" width="40" height="40"><span id="caption">Target</span>
                      <div id="cells"></div><button class="geetest_commit">Submit</button>
                    </div><p id="status"></p>
                    <script>
                    window.clicks = []; window.submissions = []; window.generation = 1;
                    window.image = n => 'data:image/svg+xml,' + encodeURIComponent(
                        `<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40">
                         <rect width="40" height="40" fill="${n === 1 ? 'red' : 'blue'}"/>
                         <text x="5" y="25">${n}</text></svg>`);
                    document.querySelector('#target').src = image(1);
                    document.querySelector('#cells').innerHTML = Array.from({length: 9},
                        (_, i) => `<button class="geetest_item" data-index="${i+1}">${i+1}</button>`
                    ).join('');
                    document.addEventListener('click', event => {
                        if (event.target.matches('.geetest_item')) {
                            clicks.push([generation, Number(event.target.dataset.index)]);
                            event.target.classList.toggle('selected');
                        }
                        if (!event.target.matches('.geetest_commit')) return;
                        submissions.push(Array.from(document.querySelectorAll('.selected'),
                            el => Number(el.dataset.index)));
                        if ((mode === 'settle') || (submissions.length > 1 &&
                            !mode.startsWith('budget'))) {
                            document.querySelector('#status').textContent = 'Verified';
                            return;
                        }
                        document.querySelectorAll('.selected').forEach(el =>
                            el.classList.remove('selected'));
                        generation++;
                        if (mode === 'text_only') {
                            document.querySelector('#caption').textContent = 'Wrong, try again';
                        } else if (mode === 'cells') {
                            document.querySelectorAll('.geetest_item').forEach(el =>
                                el.replaceWith(el.cloneNode(true)));
                        } else if (mode === 'root') {
                            const root = document.querySelector('.geetest_panel_box');
                            root.replaceWith(root.cloneNode(true));
                        } else {
                            document.querySelector('#target').src = image(generation);
                        }
                        if (mode === 'preselected')
                            document.querySelector('.geetest_item').classList.add('selected');
                    });
                    </script>
                """.replace("window.clicks =", f"window.mode = '{mode}'; window.clicks ="))
                calls, logs = [], []

                async def model(image_bytes, *args):
                    assert image_bytes.startswith(b"\x89PNG")
                    assert await page.locator('.selected').count() == 0
                    calls.append(await page.locator('#target').get_attribute('src'))
                    if mode == 'settle':
                        assert calls[-1] == await page.evaluate('image(2)')
                    return [1, 5] if len(calls) == 1 else [9]

                monkeypatch.setattr(vision, "answer_grid_captcha", model)
                if mode == "settle":
                    original = grid._snapshot
                    reads = []

                    async def snapshot(*args):
                        state = await original(*args)
                        reads.append(True)
                        if len(reads) == 1:
                            await page.evaluate("""() => setTimeout(() => {
                                document.querySelector('#target').src = image(2);
                            }, 30)""")
                        return state

                    monkeypatch.setattr(grid, "_snapshot", snapshot)
                config = {"success_signals": ["Verified"], "success_wait_seconds": 1}
                if mode == 'budget_zero':
                    config['max_refresh'] = 0
                result = await grid.run_grid_captcha(
                    page, config, lambda *entry: logs.append(entry), VISION,
                )
                submissions = await page.evaluate('submissions')
                clicks = await page.evaluate('clicks')
                if mode in {'text_only', 'preselected'}:
                    assert result[0] == 'unknown' and result[2]['reason'] == 'success_timeout'
                    assert len(calls) == len(submissions) == 1
                    assert clicks == [[1, 1], [1, 5]]
                elif mode.startswith('budget'):
                    expected = 1 if mode == 'budget_zero' else 4
                    assert result[2]['reason'] == 'retry_exhausted'
                    assert len(calls) == len(submissions) == expected
                    assert result[2]['verification_attempts'] == expected
                    assert result[2]['retries'] == expected - 1
                elif mode == 'settle':
                    assert result[0] == 'success' and len(calls) == 1
                    assert result[2]['retries'] == 0 and len(reads) >= 2
                else:
                    assert result[0] == 'success' and len(calls) == 2
                    assert submissions == [[1, 5], [9]]
                    assert clicks == [[1, 1], [1, 5], [2, 9]]
                    assert result[2]['verification_attempts'] == 2
                    assert result[2]['retries'] == 1
                    if mode == 'src':
                        assert calls[0] != calls[1]
            finally:
                await browser.close()

    asyncio.run(exercise())
