import asyncio
import base64
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image

from .test_worker_navigation import _CfStatePage
from worker import executor


@pytest.fixture
def fake_clock(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    real_sleep = asyncio.sleep

    async def patch_sleep(seconds):
        clock.now += seconds
        await real_sleep(0)

    monkeypatch.setattr(executor, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(executor.asyncio, "sleep", patch_sleep)
    return clock


@pytest.mark.parametrize(
    "marker,predictions,token_length,expected",
    [
        (False, ["absent"], 100, "success"),
        (False, ["unchecked"], 100, "fallback_interaction"),
        (True, ["absent"], 100, "unknown"),
        (True, ["verified"], 100, "success"),
        (True, ["verified"], 0, "unknown"),
        (True, ["verified", "unknown"], 100, "unknown"),
        (False, ["unknown"], 100, "success"),
        (False, ["absent", "unknown"], 100, "success"),
        (True, ["unknown"], 100, "unknown"),
        (False, [None], 100, "fail"),
        (False, ["invalid"], 100, "fail"),
        (False, ["error"], 100, "unknown"),
        (False, ["connect_error"], 100, "fail"),
        (False, ["unchecked", "unchecked", "absent"], 100, "success"),
        (True, ["unchecked", "unchecked", "absent"], 100, "success"),
        (True, ["unchecked", "verified"], 100, "success"),
    ],
)
def test_dual_confirmation(monkeypatch, fake_clock, marker, predictions, token_length, expected):
    calls = []

    class Page(_CfStatePage):
        viewport_size = {"width": 100, "height": 100}

        @property
        def mouse(self):
            return self

        async def click(self, x, y):
            if "verified" not in predictions:
                self.marker = False
            self.token_length = token_length

        async def evaluate(self, script):
            if "readyState" in script:
                return True
            return await super().evaluate(script)

        async def screenshot(self, **kwargs):
            return b"fake-png"

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            state = predictions[min(len(calls), len(predictions) - 1)]
            calls.append(state)
            if state == "connect_error":
                raise httpx.ConnectError("unavailable")
            body = {"state": state}
            if state == "unchecked":
                body["checkbox_center_pct"] = {"x": 50, "y": 50}
            return httpx.Response(200, json=body)

    async def scroll(page):
        pass

    monkeypatch.setattr(executor.httpx, "AsyncClient", Client)
    monkeypatch.setattr(executor, "_scroll_cf_into_view", scroll)
    result = asyncio.run(executor._run_turnstile(
        Page(marker=marker, token_length=0 if predictions[0] == "unchecked" else token_length), {"wait_seconds": 7},
        {"url": "http://cfbox.invalid"}, None, None, "general",
    ))
    assert result[0] == expected
    assert calls
    if predictions[0] == "unchecked" and expected == "success":
        assert result[2]["state"] == "passed"


def test_click_uses_screenshot_size_when_remote_viewport_is_absent(monkeypatch, fake_clock):
    clicks = []
    calls = []
    image = io.BytesIO()
    Image.new("RGB", (200, 100), "white").save(image, format="PNG")
    png = image.getvalue()

    class Page(_CfStatePage):
        viewport_size = None

        @property
        def mouse(self):
            return self

        async def click(self, x, y):
            clicks.append((x, y))
            self.marker = False
            self.token_length = 100

        async def evaluate(self, script):
            if "readyState" in script:
                return True
            return await super().evaluate(script)

        async def screenshot(self, **kwargs):
            return png

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            state = "unchecked" if not calls else "absent"
            calls.append(state)
            body = {"state": state}
            if state == "unchecked":
                body["checkbox_center_pct"] = {"x": 50, "y": 50}
            return httpx.Response(200, json=body)

    async def scroll(page):
        pass

    monkeypatch.setattr(executor.httpx, "AsyncClient", Client)
    monkeypatch.setattr(executor, "_scroll_cf_into_view", scroll)
    result = asyncio.run(executor._run_turnstile(
        Page(marker=True), {"wait_seconds": 7},
        {"url": "http://cfbox.invalid"}, None, None, "general",
    ))
    assert result[0] == "success"
    assert clicks == [(100.0, 50.0)]


def test_image_dimensions_fall_back_only_for_invalid_images():
    image = io.BytesIO()
    Image.new("RGB", (37, 19), "white").save(image, format="PNG")
    assert executor._image_dimensions(image.getvalue(), (1920, 1080)) == (37, 19)
    assert executor._image_dimensions(b"not-an-image", (1920, 1080)) == (1920, 1080)


def test_missing_cfbox_requires_human():
    result = asyncio.run(executor._run_turnstile(
        _CfStatePage(marker=True), {}, None, None, None, "general"
    ))
    assert result[0] == "waiting_human"


def test_screenshot_respects_total_deadline():
    class Page(_CfStatePage):
        async def screenshot(self, **kwargs):
            await asyncio.sleep(10)

    async def run():
        async with asyncio.timeout(2):
            return await executor._run_turnstile(
                Page(), {"wait_seconds": 1}, {"url": "http://cfbox.invalid"},
                None, None, "general",
            )

    assert asyncio.run(run())[0] == "unknown"


def test_timeout_preserves_detection_phase():
    class Page(_CfStatePage):
        async def screenshot(self, **kwargs):
            await asyncio.sleep(10)

    result = asyncio.run(executor._run_turnstile(
        Page(), {"wait_seconds": 1}, {"url": "http://cfbox.invalid"}, None, None, "general",
    ))
    assert result[2]["phase"] == "screenshot"
    assert result[2]["page"]["text_readable"] is True


def test_second_screenshot_font_timeout_still_requires_fresh_confirmation(
    visual_error_scenario, monkeypatch,
):
    scenario = visual_error_scenario
    scenario.predictions = ["absent"]
    scenario.page.marker = False
    buffer = io.BytesIO()
    Image.new("RGB", (100, 100), "white").save(buffer, format="PNG")
    png = buffer.getvalue()
    original = scenario.page.screenshot
    attempts = []

    async def screenshot(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 2:
            raise executor.PlaywrightTimeoutError("waiting for fonts to load...")
        return await original(**kwargs)

    async def send(method, *_args):
        if method == "Page.getLayoutMetrics":
            return {"cssVisualViewport": {"pageX": 0, "pageY": 0, "scale": 1},
                    "contentSize": {"width": 100}, "cssContentSize": {"width": 100}}
        if method == "Runtime.evaluate":
            return {"result": {"value": 1}}
        return {"data": base64.b64encode(png).decode("ascii")}

    session = SimpleNamespace(send=AsyncMock(side_effect=send), detach=AsyncMock())
    scenario.page.context = SimpleNamespace(new_cdp_session=AsyncMock(return_value=session))
    monkeypatch.setattr(scenario.page, "screenshot", screenshot)

    result = scenario.run()

    assert result[0] == "success"
    assert result[2]["states"] == ["absent"] * 3
    assert len(attempts) == len(scenario.calls) == 3
    assert scenario.calls[-1][0] - scenario.calls[0][0] >= 2
    assert scenario.submitted_images[1] == png
    scenario.page.context.new_cdp_session.assert_awaited_once_with(scenario.page)
    session.detach.assert_awaited_once_with()


def test_screenshot_error_is_not_reported_as_cfbox_failure(visual_error_scenario, monkeypatch):
    scenario = visual_error_scenario
    monkeypatch.setattr(scenario.page, "screenshot", AsyncMock(side_effect=RuntimeError("private-url")))

    result = scenario.run()

    assert result[0] == "fail"
    assert result[1] == "CF 截图失败: RuntimeError"
    assert result[2]["phase"] == "screenshot"
    assert not scenario.calls
    assert "private-url" not in repr((result, scenario.logs))


def test_private_challenge_rejection_does_not_attempt_screenshot(monkeypatch):
    page = _CfStatePage(marker=False)
    screenshot = AsyncMock()
    monkeypatch.setattr(page, "screenshot", screenshot, raising=False)
    private = SimpleNamespace(pin_challenge=AsyncMock(side_effect=RuntimeError("denied")))

    result = asyncio.run(executor._run_turnstile(
        page, {"wait_seconds": 2}, {"url": "http://cfbox.invalid"},
        None, None, "general", private=private,
    ))

    assert result[0] == "fail"
    screenshot.assert_not_awaited()


@pytest.mark.parametrize("visible,failing,expected", [(False, True, True), (True, True, False), (True, False, True)])
def test_cf_text_skips_hidden_frames_and_humanized_reads(visible, failing, expected):
    class Element:
        async def evaluate(self, script):
            return visible
        async def dispose(self):
            pass

    class Frame:
        url = "about:blank"
        parent_frame = None
        async def frame_element(self):
            return Element()
        async def evaluate(self, script):
            if failing:
                raise RuntimeError("frame body unavailable")
            if script == executor.CF_VISIBLE_MARKER_SCRIPT:
                return False
            if script == executor.CF_TOKEN_LENGTH_SCRIPT:
                return 0
            return "Normal child frame"
        async def inner_text(self, selector):
            raise AssertionError("Must not use humanized text reads")

    class Page(_CfStatePage):
        @property
        def main_frame(self):
            return self
        @property
        def frames(self):
            return [self, Frame()]
        async def inner_text(self, selector):
            raise AssertionError("Must not use humanized text reads")

    state = asyncio.run(executor._cf_page_state(Page()))
    assert state["text_readable"] is expected
    assert state["marker"] is (None if visible and failing else False)


def test_main_body_unreadable_is_not_absence():
    class Page(_CfStatePage):
        async def evaluate(self, script):
            if script == executor.CF_PAGE_TEXT_SCRIPT:
                raise RuntimeError("body missing")
            return await super().evaluate(script)

    assert asyncio.run(executor._cf_page_state(Page()))["text_readable"] is False


@pytest.mark.parametrize("script,field", [
    (executor.CF_VISIBLE_MARKER_SCRIPT, "marker"),
    (executor.CF_TOKEN_LENGTH_SCRIPT, "token_readable"),
    (executor.CF_PAGE_TEXT_SCRIPT, "text_readable"),
])
def test_main_dom_null_result_is_unreadable(script, field):
    class Page(_CfStatePage):
        async def evaluate(self, expression):
            if expression == script:
                return None
            return await super().evaluate(expression)

    state = asyncio.run(executor._cf_page_state(Page()))
    assert state[field] is (None if field == "marker" else False)
    if field == "marker":
        assert state["dom_marker"] is None
    assert state["token_length"] == 0


@pytest.mark.parametrize("existing_marker", [False, True])
def test_unreadable_frame_list_is_not_clear_page(existing_marker):
    class Page(_CfStatePage):
        @property
        def frames(self):
            raise RuntimeError("frame list unavailable")

    state = asyncio.run(executor._cf_page_state(Page(marker=existing_marker)))
    assert state["marker"] is (True if existing_marker else None)
    assert state["text_readable"] is False
    assert state["token_readable"] is False


def test_challenge_title_without_widget():
    class Page(_CfStatePage):
        async def title(self):
            return "Just a moment..."

    state = asyncio.run(executor._cf_page_state(Page()))
    assert state["challenge_text"] is True
    assert state["marker"] is False


@pytest.mark.parametrize("prediction", ["unknown", "verified"])
@pytest.mark.parametrize("blocker", ["title", "url", "marker_error", "text_error", "loading"])
def test_inference_does_not_bypass_page_checks(monkeypatch, fake_clock, blocker, prediction):
    class Page(_CfStatePage):
        viewport_size = {"width": 100, "height": 100}

        async def title(self):
            if blocker == "text_error":
                raise RuntimeError("page detached")
            return "Just a moment..." if blocker == "title" else "Site"

        async def evaluate(self, script):
            if "readyState" in script:
                return blocker != "loading"
            if blocker == "marker_error" and script == executor.CF_VISIBLE_MARKER_SCRIPT:
                raise RuntimeError("page detached")
            return await super().evaluate(script)

        async def screenshot(self, **kwargs):
            return b"fake-png"

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            return httpx.Response(200, json={"state": prediction})

    async def scroll(page):
        pass

    monkeypatch.setattr(executor.httpx, "AsyncClient", Client)
    monkeypatch.setattr(executor, "_scroll_cf_into_view", scroll)
    page = Page(marker=True, token_length=100, url="https://site.test/?__cf_chl_rt_tk=x" if blocker == "url" else "https://site.test/")
    result = asyncio.run(executor._run_turnstile(
        page, {"wait_seconds": 3}, {"url": "http://cfbox.invalid"}, None, None, "general",
    ))
    assert result[0] == "unknown"


@pytest.fixture
def visual_error_scenario(monkeypatch, fake_clock):
    scenario = SimpleNamespace(
        clock=fake_clock,
        predictions=["processing", "unchecked", "error"],
        confidences=None,
        calls=[],
        screenshots=[],
        submitted_images=[],
        image=None,
        clicks=[],
        logs=[],
        diagnostics={},
        on_predict=None,
        blockers_before_click=False,
    )

    class Element:
        async def evaluate(self, script):
            return True

        async def dispose(self):
            pass

    class Frame:
        url = "https://challenges.cloudflare.com/turnstile/widget"
        parent_frame = None

        async def frame_element(self):
            if scenario.page.blocker == "frame_read":
                raise RuntimeError("frame host unavailable")
            return Element()

        async def evaluate(self, script):
            if script == executor.CF_PAGE_TEXT_SCRIPT:
                return "Normal frame"
            if script == executor.CF_TOKEN_LENGTH_SCRIPT:
                return 0
            return False

    class Page(_CfStatePage):
        viewport_size = {"width": 100, "height": 100}
        token_after_click = 100
        blocker = None

        @property
        def frames(self):
            return [Frame()] if self.blocker in {"frame", "frame_read"} else []

        @property
        def mouse(self):
            return self

        async def click(self, x, y):
            scenario.clicks.append((fake_clock.now, x, y))
            self.marker = False
            self.token_length = self.token_after_click
            if self.blocker == "url":
                self.url += "?__cf_chl_rt_tk=challenge"

        async def title(self):
            blocked = scenario.clicks or scenario.blockers_before_click
            if blocked and self.blocker == "title_read":
                raise RuntimeError("title unavailable")
            return "Just a moment..." if blocked and self.blocker == "text" else "Site"

        async def evaluate(self, script):
            if "readyState" in script:
                return self.blocker != "loading"
            if scenario.clicks or scenario.blockers_before_click:
                if script == executor.CF_VISIBLE_MARKER_SCRIPT:
                    if self.blocker == "marker_read":
                        raise RuntimeError("marker unavailable")
                    if self.blocker == "marker":
                        return True
                if script == executor.CF_PAGE_TEXT_SCRIPT:
                    if self.blocker == "text_read":
                        raise RuntimeError("body unavailable")
                    if self.blocker == "body_text":
                        return "Verify you are human"
                if "cf-turnstile-response" in script and self.blocker == "token_read":
                    raise RuntimeError("token unavailable")
            return await super().evaluate(script)

        async def screenshot(self, **kwargs):
            image = scenario.image or f"fake-png-{len(scenario.screenshots)}".encode()
            scenario.screenshots.append((fake_clock.now, image))
            return image

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            index = len(scenario.calls)
            prediction = scenario.predictions[min(index, len(scenario.predictions) - 1)]
            scenario.calls.append((fake_clock.now, prediction))
            scenario.submitted_images.append(kwargs["files"]["image"][1])
            if scenario.on_predict:
                scenario.on_predict(scenario.page, index)
            if prediction == "connect_error":
                raise httpx.ConnectError("unavailable")
            if prediction == "http_500":
                return httpx.Response(500, json={"state": "error"})
            if prediction == "malformed_json":
                return httpx.Response(200, content=b"not-json")
            if prediction == "malformed_body":
                return httpx.Response(200, json=[{"state": "error"}])
            if prediction == "invalid_state":
                return httpx.Response(200, json={"state": "invalid"})
            if isinstance(prediction, dict):
                return httpx.Response(200, content=json.dumps(prediction).encode())
            body = {"state": prediction}
            if prediction == "unchecked":
                body["checkbox_center_pct"] = {"x": 50, "y": 50}
            if scenario.confidences is not None:
                confidence = scenario.confidences[min(index, len(scenario.confidences) - 1)]
                if confidence is not ...:
                    body["confidence"] = confidence
            # Raw JSON also exercises non-finite values that httpx's encoder rejects.
            return httpx.Response(200, content=json.dumps(body).encode())

    async def scroll(page):
        pass

    detect = executor._run_turnstile_detection

    async def capture_diagnostics(*args):
        scenario.diagnostics = args[-1]
        return await detect(*args)

    def run(wait_seconds=8):
        return asyncio.run(executor._run_turnstile(
            scenario.page, {"wait_seconds": wait_seconds}, {"url": "http://cfbox.invalid"},
            None, None, "general",
            lambda level, message: scenario.logs.append((fake_clock.now, level, message)),
        ))

    scenario.page = Page(marker=True)
    scenario.run = run
    monkeypatch.setattr(executor.httpx, "AsyncClient", Client)
    monkeypatch.setattr(executor, "_scroll_cf_into_view", scroll)
    monkeypatch.setattr(executor, "_run_turnstile_detection", capture_diagnostics)
    return scenario


def test_persistent_visual_error_passes_with_fresh_token_and_clear_page(visual_error_scenario):
    scenario = visual_error_scenario
    result = scenario.run()

    assert result[0] == "success"
    assert result[2] == {
        "state": "passed",
        "cfbox_state": "error",
        "cfbox_summary_state": "error",
        "challenge_states": [],
        "clicked": True,
        "click_count": 1,
        "states": ["processing", "unchecked", "error", "error", "error"],
        "completion_reason": "fresh_token_and_clear_page",
        "confidence": None,
        "unresolved_challenge": False,
        "seen_page_challenge": True,
    }
    assert [(x, y) for _, x, y in scenario.clicks] == [(50.0, 50.0)]
    error_times = [at for at, prediction in scenario.calls if prediction == "error"]
    assert error_times[-1] - error_times[0] == 2


@pytest.mark.parametrize("failed_read", [4, 5], ids=["at_click", "after_click"])
def test_old_token_read_failure_does_not_become_fresh_token(
    visual_error_scenario, monkeypatch, failed_read,
):
    scenario = visual_error_scenario
    scenario.page.token_length = 100
    evaluate = scenario.page.evaluate
    token_reads = []

    async def flaky_evaluate(script):
        if "cf-turnstile-response" in script:
            if len(token_reads) + 1 == failed_read:
                token_reads.append(None)
                raise RuntimeError("token temporarily unavailable")
            token_reads.append(scenario.page.token_length)
        return await evaluate(script)

    monkeypatch.setattr(scenario.page, "evaluate", flaky_evaluate)
    result = scenario.run()

    assert token_reads.count(None) == 1
    assert all(value == 100 for value in token_reads if value is not None)
    assert token_reads[-1] == 100
    assert len(scenario.clicks) == 1
    assert result[0] == "unknown"
    assert result[2]["state"] == "error"
    assert result[2]["token_readable"] is True
    assert result[2]["token_present"] is True
    assert result[2]["fresh_token_after_click"] is False
    assert result[2]["blockers"] == ["cfbox_unresolved"]


def test_absent_confirmation_survives_continuous_token_read_failure(visual_error_scenario):
    scenario = visual_error_scenario
    scenario.predictions = ["processing", "unchecked", "absent"]
    scenario.page.blocker = "token_read"

    result = scenario.run()

    assert result[0] == "success"
    assert result[2]["cfbox_state"] == "absent"
    assert result[2]["completion_reason"] == "absent_and_clear_page"
    assert result[2]["click_count"] == 1
    assert result[2]["states"] == ["processing", "unchecked", "absent", "absent", "absent"]
    assert scenario.calls[-1][0] - scenario.calls[2][0] == 2


def test_token_read_failure_invalidates_click_before_token_expires_and_returns(
    visual_error_scenario,
):
    scenario = visual_error_scenario
    page_states = []

    def expire_token_after_read_failure(page, index):
        if index == 3:
            page.blocker = "token_read"
        elif index == 4:
            page.blocker = None
            page.token_length = 0
        elif index == 5:
            page.token_length = 100
        if 2 <= index <= 5:
            page_states.append((page.marker, page.blocker, page.token_length))

    scenario.on_predict = expire_token_after_read_failure
    result = scenario.run()

    assert page_states == [
        (False, None, 100),
        (False, "token_read", 100),
        (False, None, 0),
        (False, None, 100),
    ]
    assert len(scenario.clicks) == 1
    assert result[0] == "unknown"
    assert result[2]["state"] == "error"
    assert result[2]["token_readable"] is True
    assert result[2]["token_present"] is True
    assert result[2]["fresh_token_after_click"] is False
    assert result[2]["blockers"] == ["cfbox_unresolved"]


def test_second_click_with_readable_empty_token_can_establish_fresh_evidence(
    visual_error_scenario, monkeypatch,
):
    scenario = visual_error_scenario
    scenario.predictions = ["processing", "unchecked", "unchecked", "error"]
    evaluate = scenario.page.evaluate
    click = scenario.page.mouse.click
    token_reads = []
    click_page_states = []

    async def flaky_evaluate(script):
        if "cf-turnstile-response" in script:
            if len(token_reads) == 3:
                token_reads.append(None)
                raise RuntimeError("token temporarily unavailable at first click")
            token_reads.append(scenario.page.token_length)
        return await evaluate(script)

    async def click_on_second_attempt(x, y):
        click_page_states.append((scenario.page.marker, token_reads[-1]))
        if not scenario.clicks:
            scenario.clicks.append((scenario.clock.now, x, y))
            return
        await click(x, y)

    monkeypatch.setattr(scenario.page, "evaluate", flaky_evaluate)
    monkeypatch.setattr(scenario.page.mouse, "click", click_on_second_attempt)
    result = scenario.run()

    assert token_reads.count(None) == 1
    assert click_page_states == [(True, None), (True, 0)]
    assert len(scenario.clicks) == 2
    assert scenario.page.marker is False
    assert scenario.page.token_length == 100
    assert result[0] == "success"
    assert result[2]["cfbox_state"] == "error"
    assert result[2]["completion_reason"] == "fresh_token_and_clear_page"
    assert result[2]["click_count"] == 2
    assert result[2]["states"] == ["processing", "unchecked", "unchecked", "error", "error", "error"]
    assert scenario.calls[-1][0] - scenario.calls[3][0] == 2


def test_same_url_new_widget_cannot_reuse_old_click(visual_error_scenario):
    scenario = visual_error_scenario
    initial_url = scenario.page.url
    page_states = []

    def replace_challenge(page, index):
        if index == 3:
            page.marker = True
            page.token_length = 0
        elif index == 4:
            page.marker = False
            page.token_length = 100
        if 2 <= index <= 4:
            page_states.append((page.marker, page.token_length, page.url))

    scenario.on_predict = replace_challenge
    result = scenario.run()

    assert page_states == [
        (False, 100, initial_url),
        (True, 0, initial_url),
        (False, 100, initial_url),
    ]
    assert len(scenario.clicks) == 1
    assert result[0] == "unknown"
    assert result[2]["state"] == "error"
    assert result[2]["token_present"] is True
    assert result[2]["fresh_token_after_click"] is False
    assert result[2]["blockers"] == ["cfbox_unresolved"]


def test_token_growth_with_lingering_challenge_title_preserves_click_evidence(
    visual_error_scenario,
):
    scenario = visual_error_scenario
    scenario.page.blocker = "text"

    def clear_lingering_title(page, index):
        if index == 3:
            page.blocker = None

    scenario.on_predict = clear_lingering_title
    result = scenario.run()

    assert result[0] == "success"
    assert result[2]["cfbox_state"] == "error"
    assert result[2]["completion_reason"] == "fresh_token_and_clear_page"
    assert result[2]["click_count"] == 1
    assert result[2]["states"] == ["processing", "unchecked", "error", "error", "error", "error"]
    assert scenario.calls[-1][0] - scenario.calls[3][0] == 2


def test_extra_visual_unchecked_without_dom_preserves_click_evidence(visual_error_scenario):
    scenario = visual_error_scenario
    scenario.predictions = ["processing", "unchecked", "unchecked", "error"]
    click_page_states = []

    def record_click_page_state(page, index):
        if index in {1, 2}:
            click_page_states.append((page.marker, page.token_length))

    scenario.on_predict = record_click_page_state
    result = scenario.run()

    assert click_page_states == [(True, 0), (False, 100)]
    assert len(scenario.clicks) == 2
    assert result[0] == "success"
    assert result[2]["cfbox_state"] == "error"
    assert result[2]["completion_reason"] == "fresh_token_and_clear_page"
    assert result[2]["click_count"] == 2
    assert result[2]["states"] == ["processing", "unchecked", "unchecked", "error", "error", "error"]


@pytest.mark.parametrize("missing", ["fresh_token", "click", "dom_challenge", "token"])
def test_visual_error_requires_all_step_evidence(visual_error_scenario, missing):
    scenario = visual_error_scenario
    if missing == "fresh_token":
        scenario.page.token_length = 100
    elif missing == "click":
        scenario.predictions = ["processing", "error"]

        def clear_page(page, index):
            page.marker = False
            page.token_length = 100

        scenario.on_predict = clear_page
    elif missing == "dom_challenge":
        scenario.page.marker = False
    else:
        scenario.page.token_after_click = 0

    result = scenario.run()

    assert result[0] == "unknown"
    assert result[2]["state"] == "error"
    assert result[2]["fresh_token_after_click"] is False
    assert result[2]["token_present"] is (missing != "token")
    assert result[2]["clicked"] is (missing != "click")
    assert result[2]["page"] == {
        "marker": False, "challenge_text": False, "url_challenge": False, "text_readable": True,
        "dom_marker": False, "frame_challenge": False,
        "frames_checked": 0, "frames_hidden": 0, "frames_unreadable": 0,
    }
    assert "cfbox_unresolved" in result[2]["blockers"]


@pytest.mark.parametrize("blocker,expected_blocker", [
    ("marker", "visible_widget"),
    ("marker_read", "marker_unreadable"),
    ("text", "challenge_text"),
    ("url", "challenge_url"),
    ("loading", "page_not_ready"),
    ("title_read", "text_unreadable"),
    ("text_read", "text_unreadable"),
    ("token_read", "cfbox_unresolved"),
])
def test_visual_error_does_not_bypass_page_checks(
    visual_error_scenario, blocker, expected_blocker,
):
    scenario = visual_error_scenario
    scenario.page.blocker = blocker

    result = scenario.run()

    assert result[0] == "unknown"
    assert result[2]["state"] == "error"
    assert result[2]["clicked"] is True
    assert result[2]["click_count"] == 1
    assert expected_blocker in result[2]["blockers"]
    assert result[2]["predictions"] == len(scenario.calls)
    assert result[2]["phase"] == "poll_wait"


def test_visual_error_requires_two_continuous_ready_seconds(visual_error_scenario):
    scenario = visual_error_scenario

    def interrupt_ready(page, index):
        page.blocker = "loading" if index == 3 else None

    scenario.on_predict = interrupt_ready
    result = scenario.run()

    assert result[0] == "success"
    assert result[2]["completion_reason"] == "fresh_token_and_clear_page"
    assert len(scenario.calls) == 7
    assert scenario.calls[-1][0] - scenario.calls[4][0] == 2


def test_new_challenge_progress_resets_visual_error_evidence(visual_error_scenario):
    scenario = visual_error_scenario

    def new_challenge(page, index):
        if index == 2:
            page.url = "https://kamept.com/next"
            page.marker = True
        elif index == 3:
            page.marker = False

    scenario.on_predict = new_challenge
    result = scenario.run()

    assert result[0] == "unknown"
    assert result[2]["clicked"] is True
    assert result[2]["token_present"] is True
    assert result[2]["fresh_token_after_click"] is False
    assert result[2]["page_click_count"] == 0
    assert len(scenario.clicks) == 1
    assert "cfbox_unresolved" in result[2]["blockers"]


def test_visual_error_recovers_when_later_inference_is_absent(visual_error_scenario):
    scenario = visual_error_scenario
    scenario.predictions = ["processing", "unchecked", "error", "absent"]
    scenario.page.token_after_click = 0

    result = scenario.run()

    assert result[0] == "success"
    assert result[2]["cfbox_state"] == "absent"
    assert result[2]["completion_reason"] == "absent_and_clear_page"
    assert result[2]["states"] == ["processing", "unchecked", "error", "absent", "absent", "absent"]
    assert len(scenario.calls) == len(result[2]["states"])


@pytest.mark.parametrize("failure,detail", [
    ("connect_error", "ConnectError"),
    ("http_500", "HTTP 500"),
    ("malformed_json", "JSONDecodeError"),
    ("malformed_body", "\u54cd\u5e94\u683c\u5f0f\u65e0\u6548"),
    ("invalid_state", "\u54cd\u5e94\u72b6\u6001\u65e0\u6548"),
])
def test_service_failure_is_not_visual_error_success(visual_error_scenario, failure, detail):
    scenario = visual_error_scenario
    scenario.predictions = ["processing", "unchecked", "error", failure]

    result = scenario.run()

    assert result[0] == "fail"
    assert detail in result[1]
    assert scenario.page.marker is False
    assert scenario.page.token_length == 100
    assert len(scenario.clicks) == 1
    assert [prediction for _, prediction in scenario.calls] == scenario.predictions


def test_visual_error_heartbeat_keeps_rechecking_every_ten_seconds(visual_error_scenario):
    scenario = visual_error_scenario
    scenario.page.token_after_click = 0

    result = scenario.run(wait_seconds=25)

    assert result[0] == "unknown"
    heartbeats = [(at, message) for at, _, message in scenario.logs
                  if "state=error, confidence=" in message and "predictions=" in message]
    assert [at for at, _ in heartbeats] == [10.5, 20.5]
    assert all("confidence=None" in message and "low_confidence_count=0" in message
               and "fresh_token_after_click=False" in message
               for _, message in heartbeats)
    assert len(scenario.calls) == 25
    assert result[2]["predictions"] == 25
    assert result[2]["ready"] is True
    assert result[2]["token_present"] is False
    assert result[2]["blockers"] == ["cfbox_unresolved"]


@pytest.mark.parametrize("prediction", ["error", "unknown"])
@pytest.mark.parametrize("confidence", [0, 0.0, 0.9499])
def test_low_confidence_clear_page_needs_three_fresh_screenshots(
    visual_error_scenario, prediction, confidence,
):
    scenario = visual_error_scenario
    scenario.predictions = [prediction]
    scenario.confidences = [confidence]
    scenario.page.marker = False

    result = scenario.run()

    assert result[0] == "success"
    assert result[2]["completion_reason"] == "low_confidence_and_clear_page"
    assert result[2]["cfbox_state"] == prediction
    assert result[2]["confidence"] == confidence
    assert result[2]["low_confidence_count"] == 3
    assert result[2]["states"] == [prediction] * 3
    assert scenario.clicks == []
    assert result[2]["clicked"] is False
    assert scenario.page.token_length == 0
    assert len(scenario.calls) == len(scenario.screenshots) == 3
    assert scenario.submitted_images == [image for _, image in scenario.screenshots]
    assert len(set(scenario.submitted_images)) == 3
    assert scenario.calls[-1][0] - scenario.calls[0][0] == 2
    assert any(f"confidence={float(confidence)}" in message
               for _, _, message in scenario.logs)


@pytest.mark.parametrize("prediction", ["error", "unknown"])
@pytest.mark.parametrize("wait_seconds,expected_calls", [(1, 1), (2, 2)])
def test_low_confidence_does_not_complete_with_fewer_than_three_predictions(
    visual_error_scenario, prediction, wait_seconds, expected_calls,
):
    scenario = visual_error_scenario
    scenario.page.marker = False
    scenario.predictions = [prediction]
    scenario.confidences = [0.9499]

    result = scenario.run(wait_seconds=wait_seconds)

    assert result[0] == "unknown"
    assert result[2]["low_confidence_count"] == expected_calls
    assert len(scenario.calls) == len(scenario.screenshots) == expected_calls
    assert "completion_reason" not in result[2]


@pytest.mark.parametrize("prediction", ["error", "unknown"])
@pytest.mark.parametrize("poll_seconds,expected_calls", [(0.25, 9), (3, 3)])
def test_low_confidence_requires_both_three_predictions_and_two_seconds(
    visual_error_scenario, monkeypatch, prediction, poll_seconds, expected_calls,
):
    scenario = visual_error_scenario
    scenario.page.marker = False
    scenario.predictions = [prediction]
    scenario.confidences = [0.1]

    async def poll(seconds, cancel):
        await executor.asyncio.sleep(poll_seconds)
        return True

    monkeypatch.setattr(executor, "_sleep", poll)
    result = scenario.run(wait_seconds=10)

    assert result[0] == "success"
    assert result[2]["completion_reason"] == "low_confidence_and_clear_page"
    assert result[2]["low_confidence_count"] == expected_calls
    assert len(scenario.calls) == len(scenario.screenshots) == expected_calls
    assert scenario.calls[-1][0] - scenario.calls[0][0] >= 2
    assert all(at - scenario.calls[0][0] < 2 or index < 2
               for index, (at, _) in enumerate(scenario.calls[:-1]))


@pytest.mark.parametrize("prediction", ["error", "unknown"])
@pytest.mark.parametrize("confidence", [
    pytest.param(..., id="missing"),
    pytest.param(None, id="null"),
    pytest.param("0.1", id="string"),
    pytest.param(False, id="false"),
    pytest.param(True, id="true"),
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="inf"),
    pytest.param(float("-inf"), id="negative-inf"),
    pytest.param(-0.01, id="negative"),
    pytest.param(1.01, id="above-one"),
    pytest.param(10**400, id="huge-positive-int"),
    pytest.param(-(10**400), id="huge-negative-int"),
    pytest.param(0.95, id="threshold"),
    pytest.param(1, id="integer-one"),
])
def test_non_low_confidence_preserves_legacy_confirmation(
    visual_error_scenario, prediction, confidence,
):
    scenario = visual_error_scenario
    scenario.page.marker = False
    scenario.predictions = [prediction]
    scenario.confidences = [confidence]

    result = scenario.run(wait_seconds=4)

    expected_confidence = (
        float(confidence)
        if type(confidence) in (int, float) and confidence in (0.95, 1)
        else None
    )
    assert scenario.clicks == []
    assert scenario.page.token_length == 0
    assert result[2].get("completion_reason") != "low_confidence_and_clear_page"
    if prediction == "error":
        assert result[0] == "unknown"
        assert result[2]["low_confidence_count"] == 0
        assert result[2]["confidence"] == expected_confidence
        assert "cfbox_unresolved" in result[2]["blockers"]
    else:
        assert result[0] == "success"
        assert result[2]["completion_reason"] == "unknown_and_clear_page"
        assert result[2]["states"] == ["unknown"] * 3
    assert any(f"confidence={expected_confidence}" in message
               for _, _, message in scenario.logs)


@pytest.mark.parametrize("confidence", [0.1, 0.95])
def test_error_confidence_preserves_completion_with_fresh_token(
    visual_error_scenario, confidence,
):
    scenario = visual_error_scenario
    scenario.confidences = [confidence]

    result = scenario.run()

    assert result[0] == "success"
    assert result[2]["completion_reason"] == (
        "low_confidence_and_clear_page" if confidence < 0.95 else "fresh_token_and_clear_page"
    )
    assert result[2]["cfbox_state"] == "error"
    assert result[2]["click_count"] == 1


@pytest.mark.parametrize("error_confidences", [
    pytest.param([0.95, 0.1, 0.95], id="high-low-high"),
    pytest.param([0.1, 0.95, 0.1], id="low-high-low"),
])
def test_fresh_token_completion_timer_survives_alternating_confidence(
    visual_error_scenario, error_confidences,
):
    scenario = visual_error_scenario
    scenario.confidences = [None, None, *error_confidences]

    result = scenario.run()

    assert result[0] == "success"
    assert result[2]["completion_reason"] == "fresh_token_and_clear_page"
    assert result[2]["cfbox_state"] == "error"
    assert result[2]["states"] == ["processing", "unchecked", "error", "error", "error"]
    assert result[2]["clicked"] is True
    assert result[2]["click_count"] == len(scenario.clicks) == 1
    assert scenario.page.marker is False
    assert scenario.page.token_length == 100
    assert len(scenario.calls) == len(scenario.screenshots) == 5
    assert scenario.calls[-1][0] - scenario.calls[2][0] == 2


@pytest.mark.parametrize("prediction", ["error", "unknown", "multiple_errors"])
@pytest.mark.parametrize("blocker,expected_blocker", [
    ("marker", "visible_widget"),
    ("frame", "visible_widget"),
    ("frame_read", "marker_unreadable"),
    ("text", "challenge_text"),
    ("body_text", "challenge_text"),
    ("url", "challenge_url"),
    ("marker_read", "marker_unreadable"),
    ("title_read", "text_unreadable"),
    ("text_read", "text_unreadable"),
    ("loading", "page_not_ready"),
])
def test_low_confidence_cannot_bypass_blocked_page(
    visual_error_scenario, prediction, blocker, expected_blocker,
):
    scenario = visual_error_scenario
    scenario.page.marker = False
    scenario.predictions = [{
        "state": "error", "confidence": 0.7024, "ambiguous": True,
        "challenges": [{"state": "error", "confidence": 0.7024},
                       {"state": "error", "confidence": 0.92}],
    }] if prediction == "multiple_errors" else [prediction]
    scenario.confidences = [0.1]
    scenario.blockers_before_click = True
    scenario.page.blocker = blocker
    if blocker == "url":
        scenario.page.url += "?__cf_chl_rt_tk=challenge"

    result = scenario.run(wait_seconds=4)

    assert result[0] == "unknown"
    assert expected_blocker in result[2]["blockers"]
    assert result[2]["low_confidence_count"] == 0
    assert result[2]["low_confidence_eligible"] is True
    assert scenario.clicks == []
    assert len(scenario.calls) == len(scenario.screenshots) == 4


@pytest.mark.parametrize("interruption", [
    "unknown", "processing", "high", "missing", "null", "string", "false", "true",
    "nan", "inf", "negative", "above_one",
    "marker", "text", "body_text", "url", "marker_read", "text_read", "loading",
])
def test_low_confidence_streak_restarts_after_interruption(
    visual_error_scenario, interruption,
):
    scenario = visual_error_scenario
    scenario.page.marker = False
    scenario.predictions = ["error"] * 6
    scenario.confidences = [0.1] * 6
    scenario.blockers_before_click = True
    if interruption == "unknown":
        scenario.predictions[2:] = ["unknown"] * 4
        restart_index = 2
    else:
        restart_index = 3
        if interruption == "processing":
            scenario.predictions[2] = "processing"
        elif interruption in {
            "high", "missing", "null", "string", "false", "true",
            "nan", "inf", "negative", "above_one",
        }:
            scenario.confidences[2] = {
                "high": 0.95, "missing": ..., "null": None,
                "string": "0.1", "false": False, "true": True,
                "nan": float("nan"), "inf": float("inf"),
                "negative": -0.1, "above_one": 1.1,
            }[interruption]
        else:
            initial_url = scenario.page.url

            def interrupt_page(page, index):
                page.blocker = interruption if index == 2 else None
                if interruption == "url":
                    page.url = initial_url + ("?__cf_chl_rt_tk=x" if index == 2 else "")

            scenario.on_predict = interrupt_page
            if interruption == "url":
                # Returning from the challenge URL also navigates during inference.
                restart_index = 4

    result = scenario.run(wait_seconds=10)

    assert result[0] == "success"
    assert result[2]["completion_reason"] == "low_confidence_and_clear_page"
    assert result[2]["low_confidence_count"] == 3
    assert len(scenario.calls) == len(scenario.screenshots) == restart_index + 3
    assert scenario.calls[-1][0] - scenario.calls[restart_index][0] == 2
    assert scenario.clicks == []


def test_interrupted_low_confidence_timeout_reports_only_new_streak(visual_error_scenario):
    scenario = visual_error_scenario
    scenario.page.marker = False
    scenario.predictions = ["error"]
    scenario.confidences = [0.1, 0.1, 0.95, 0.1]

    result = scenario.run(wait_seconds=5)

    assert result[0] == "unknown"
    assert result[2]["confidence"] == 0.1
    assert result[2]["low_confidence_count"] == 2
    assert result[2]["predictions"] == len(scenario.calls) == 5
    assert result[2]["blockers"] == ["cfbox_unresolved"]
    assert result[2]["phase"] == "poll_wait"
    assert "completion_reason" not in result[2]


@pytest.mark.parametrize("timing", ["between_predictions", "during_inference"])
def test_low_confidence_url_change_requires_new_stable_screenshots(
    visual_error_scenario, monkeypatch, timing,
):
    scenario = visual_error_scenario
    scenario.page.marker = False
    scenario.predictions = ["error"]
    scenario.confidences = [0.1]
    next_url = scenario.page.url + "next"
    if timing == "during_inference":
        def navigate(page, index):
            if index == 2:
                page.url = next_url

        scenario.on_predict = navigate
        restart_index = 3
    else:
        sleep = executor._sleep

        async def navigate_between_predictions(seconds, cancel):
            result = await sleep(seconds, cancel)
            if len(scenario.calls) == 2:
                scenario.page.url = next_url
            return result

        monkeypatch.setattr(executor, "_sleep", navigate_between_predictions)
        restart_index = 2

    result = scenario.run()

    assert result[0] == "success"
    assert result[2]["completion_reason"] == "low_confidence_and_clear_page"
    assert result[2]["low_confidence_count"] == 3
    assert len(scenario.calls) == len(scenario.screenshots) == restart_index + 3
    assert scenario.calls[-1][0] - scenario.calls[restart_index][0] == 2
    assert scenario.page.url == next_url


@pytest.mark.parametrize("prediction", ["error", "unknown"])
@pytest.mark.parametrize("failure", [
    "connect_error", "http_500", "malformed_json", "malformed_body", "invalid_state",
])
def test_request_failure_cannot_complete_low_confidence_streak(
    visual_error_scenario, prediction, failure,
):
    scenario = visual_error_scenario
    scenario.page.marker = False
    scenario.predictions = [prediction, prediction, failure]
    scenario.confidences = [0.1]

    result = scenario.run()

    assert result[0] == "fail"
    assert "completion_reason" not in result[2]
    assert len(scenario.calls) == len(scenario.screenshots) == 3
    assert scenario.clicks == []


@pytest.mark.parametrize("evidence", ["error", "processing", "unchecked", "marker"])
@pytest.mark.parametrize("confidence", [None, ..., "0.1", False, float("nan"), 0.95, 1])
def test_unknown_cannot_clear_unresolved_challenge(visual_error_scenario, evidence, confidence):
    scenario = visual_error_scenario
    scenario.page.marker = evidence == "marker"
    scenario.predictions = ["unknown" if evidence == "marker" else evidence, "unknown"]
    scenario.confidences = [None, confidence]

    def clear_page(page, index):
        if index >= 1:
            page.marker = False

    scenario.on_predict = clear_page
    result = scenario.run(wait_seconds=5)

    assert result[0] == "unknown"
    assert result[2]["unresolved_challenge"] is True
    assert "cfbox_unresolved" in result[2]["blockers"]
    assert len(scenario.calls) == len(scenario.screenshots) == 5
    assert not any("挑战已消失" in message for _, _, message in scenario.logs)


def test_absent_clear_resolves_latch_for_later_unknown(visual_error_scenario):
    scenario = visual_error_scenario
    scenario.page.marker = False
    scenario.predictions = ["error", "unknown", "absent", "unknown"]
    result = scenario.run()
    assert result[0] == "success"
    assert result[2]["states"] == ["error", "unknown", "absent", "unknown", "unknown"]
    assert result[2]["unresolved_challenge"] is False
    assert result[2]["seen_page_challenge"] is False
    assert "挑战已消失" not in result[1]


def test_absent_with_visible_widget_does_not_resolve_latch(visual_error_scenario):
    scenario = visual_error_scenario
    scenario.predictions = ["error", "absent", "unknown"]

    def clear_page(page, index):
        if index >= 2:
            page.marker = False

    scenario.on_predict = clear_page
    result = scenario.run(wait_seconds=5)
    assert result[0] == "unknown"
    assert result[2]["unresolved_challenge"] is True
    assert "cfbox_unresolved" in result[2]["blockers"]


@pytest.fixture
def multiple_widgets_scenario(visual_error_scenario):
    scenario = visual_error_scenario
    image = io.BytesIO()
    Image.new("RGB", (1920, 947), "white").save(image, format="PNG")
    scenario.image = image.getvalue()
    # Deliberately differ from the screenshot to catch viewport-based clicks.
    scenario.page.viewport_size = {"width": 100, "height": 100}
    scenario.page.marker = False
    scenario.predictions = [{
        "state": "unknown",
        "confidence": None,
        "ambiguous": True,
        "reason": "multiple_widgets",
        "has_cf_challenge": True,
        "checkbox_center_pct": None,
        "challenges": [
            {"state": "unchecked", "confidence": 0.9259,
             "checkbox_center_pct": {"x": 42.77, "y": 92.99}},
            {"state": "unknown", "confidence": None, "checkbox_center_pct": None},
        ],
    }]
    return scenario


@pytest.mark.parametrize("others", [
    ["unknown"], ["error"], ["processing"], ["unknown", "error"],
])
@pytest.mark.parametrize("confidence", [None, 0, 0.3, 0.95, 1])
def test_multiple_widgets_clicks_exact_target_then_absent_can_succeed(
    multiple_widgets_scenario, others, confidence,
):
    scenario = multiple_widgets_scenario
    body = scenario.predictions[0]
    body["challenges"][1:] = [
        {"state": state, "confidence": confidence, "checkbox_center_pct": None}
        for state in others
    ]
    scenario.predictions.append({"state": "absent", "confidence": None})

    def check_click_before_absent(page, index):
        if index == 1:
            assert len(scenario.clicks) == 1
            assert scenario.clicks[0][1:] == pytest.approx((821.184, 880.6153))
            assert page.marker is False

    scenario.on_predict = check_click_before_absent
    result = scenario.run()

    assert result[0] == "success"
    assert len(scenario.clicks) == 1
    # 947 * 92.99 / 100 = 880.6153 (880.616 within 0.001 pixel).
    assert scenario.clicks[0][1:] == pytest.approx((821.184, 880.616), abs=0.001)
    assert [prediction for _, prediction in scenario.calls] == [body] + [
        {"state": "absent", "confidence": None},
    ] * 3
    assert result[2]["states"] == ["unchecked", "absent", "absent", "absent"]
    assert result[2]["completion_reason"] == "absent_and_clear_page"
    assert result[2]["cfbox_summary_state"] == "absent"
    assert result[2]["challenge_states"] == []
    assert result[2]["clicked"] is True
    assert result[2]["unresolved_challenge"] is False


@pytest.mark.parametrize("others", [
    ["unknown"], ["error"], ["processing"], ["unknown", "error"],
])
@pytest.mark.parametrize("confidence", [None, 0, 0.3, 0.95, 1])
@pytest.mark.parametrize("marker", [False, True], ids=["visual-only", "fresh-token"])
def test_persistent_multiple_widgets_never_passes(
    multiple_widgets_scenario, others, confidence, marker,
):
    scenario = multiple_widgets_scenario
    scenario.page.marker = marker
    scenario.predictions[0]["challenges"][1:] = [
        {"state": state, "confidence": confidence, "checkbox_center_pct": None}
        for state in others
    ]
    result = scenario.run(wait_seconds=3)

    assert result[0] == "unknown"
    assert scenario.clicks
    assert all((x, y) == pytest.approx((821.184, 880.6153)) for _, x, y in scenario.clicks)
    assert set(result[2]["states"]) == {"unchecked"}
    assert result[2]["state"] != "passed"
    assert result[2]["summary_state"] == "unknown"
    assert result[2]["challenge_states"] == ["unchecked", *others]
    assert result[2]["multiple_unresolved"] is True
    assert result[2]["unresolved_challenge"] is True
    assert result[2]["low_confidence_count"] == 0
    assert scenario.page.marker is False
    assert scenario.page.token_length == 100
    assert "cfbox_unresolved" in result[2]["blockers"]
    assert "completion_reason" not in result[2]


def test_persistent_unchecked_with_centerless_error_can_fallback(multiple_widgets_scenario):
    scenario = multiple_widgets_scenario
    scenario.predictions[0]["challenges"][1].update(state="error", confidence=1)
    result = scenario.run()

    assert result[0] == "fallback_interaction"
    assert scenario.clicks
    assert all((x, y) == pytest.approx((821.184, 880.6153)) for _, x, y in scenario.clicks)
    assert set(result[2]["states"]) == {"unchecked"}
    assert result[2]["state"] != "passed"
    assert "completion_reason" not in result[2]


@pytest.mark.parametrize("other,center", [
    *[(state, {"x": 10, "y": 20})
      for state in ("unchecked", "processing", "error", "unknown", "verified", "absent")],
    ("unchecked", {"x": 42.77, "y": 92.99}),
    ("unchecked", None),
])
def test_competing_targets_neither_click_nor_pass(multiple_widgets_scenario, other, center):
    scenario = multiple_widgets_scenario
    scenario.predictions[0]["challenges"][1] = {
        "state": other, "confidence": 0.99, "checkbox_center_pct": center,
    }
    result = scenario.run(wait_seconds=5)

    assert result[0] == "unknown"
    assert scenario.clicks == []
    assert result[2]["states"] == ["unchecked"] * 5
    assert result[2]["multiple_unresolved"] is (other not in {"verified", "absent"})
    assert result[2]["low_confidence_count"] == 0
    assert "cfbox_unresolved" in result[2]["blockers"]


@pytest.mark.parametrize("unknown_confidence", [None, 0.3])
def test_multiple_children_with_token_require_all_low_confidences(
    visual_error_scenario, unknown_confidence,
):
    scenario = visual_error_scenario
    scenario.predictions = [
        "unchecked",
        {"state": "unknown", "challenges": [
            {"state": "error", "confidence": 0.3}, {"state": "unknown"},
        ]},
        {"state": "verified", "confidence": 1, "challenges": [
            {"state": "verified", "confidence": 1},
            {"state": "unknown", "confidence": unknown_confidence},
            {"state": "error", "confidence": 0.3},
        ]},
    ]
    result = scenario.run(wait_seconds=6)

    assert len(scenario.clicks) == 1
    assert scenario.page.marker is False
    assert scenario.page.token_length == 100
    if unknown_confidence is not None:
        assert result[0] == "success"
        assert result[2]["completion_reason"] == "low_confidence_and_clear_page"
        assert result[2]["states"] == ["unchecked"] + ["error"] * 4
        assert result[2]["cfbox_summary_state"] == "verified"
        assert result[2]["challenge_states"] == ["verified", "unknown", "error"]
        assert result[2]["low_confidence_count"] == 3
        assert result[2]["unresolved_challenge"] is False
        assert scenario.calls[-1][0] - scenario.calls[2][0] == 2
        return
    assert result[0] == "unknown"
    assert result[2]["states"] == ["unchecked"] + ["error"] * 5
    assert result[2]["summary_state"] == "verified"
    assert result[2]["challenge_states"] == ["verified", "unknown", "error"]
    assert result[2]["multiple_unresolved"] is True
    assert result[2]["unresolved_challenge"] is True
    assert result[2]["low_confidence_count"] == 0
    assert result[2]["low_confidence_eligible"] is False
    assert "cfbox_unresolved" in result[2]["blockers"]
    assert "completion_reason" not in result[2]


def test_parent_absent_cannot_hide_unchecked_child(multiple_widgets_scenario):
    scenario = multiple_widgets_scenario
    scenario.predictions[0].update(state="absent", confidence=1, ambiguous=False)
    scenario.predictions[0]["challenges"] = scenario.predictions[0]["challenges"][:1]
    result = scenario.run(wait_seconds=3)

    assert result[0] == "unknown"
    assert len(scenario.clicks) == 3
    assert scenario.clicks[0][1:] == pytest.approx((821.184, 880.6153))
    assert result[2]["summary_state"] == "absent"
    assert result[2]["challenge_states"] == ["unchecked"]
    assert result[2]["states"] == ["unchecked"] * 3
    assert result[2]["unresolved_challenge"] is True


@pytest.mark.parametrize("summary", ["absent", "verified", "unknown"])
def test_processing_child_never_passes_clear_page(visual_error_scenario, summary):
    scenario = visual_error_scenario
    scenario.page.marker = False
    scenario.predictions = [{
        "state": summary, "confidence": 1,
        "challenges": [{"state": "processing", "confidence": 0.3}],
    }]
    result = scenario.run(wait_seconds=5)

    assert result[0] == "unknown"
    assert scenario.clicks == []
    assert result[2]["states"] == ["processing"] * 5
    assert result[2]["low_confidence_count"] == 0
    assert "cfbox_unresolved" in result[2]["blockers"]


@pytest.mark.parametrize("challenges", [[], [{"state": "unknown", "confidence": None}]])
def test_flagged_ambiguous_unknown_never_passes_clear_page(visual_error_scenario, challenges):
    scenario = visual_error_scenario
    scenario.page.marker = False
    scenario.predictions = [{
        "state": "unknown", "confidence": None, "ambiguous": True,
        "has_cf_challenge": True, "challenges": challenges,
    }]
    result = scenario.run(wait_seconds=5)

    assert result[0] == "unknown"
    assert scenario.clicks == []
    assert result[2]["multiple_unresolved"] is True
    assert result[2]["unresolved_challenge"] is True
    assert result[2]["low_confidence_count"] == 0


@pytest.mark.parametrize("states,confidences", [
    (["error", "error"], [0.7024, 0.92]),
    (["error", "unknown"], [0.8, 0.9]),
])
@pytest.mark.parametrize("summary", ["error", "unknown"])
@pytest.mark.parametrize("after_click", [False, True], ids=["no-token", "fresh-token"])
def test_all_low_children_pass_after_three_fresh_clear_screenshots(
    visual_error_scenario, states, confidences, summary, after_click,
):
    scenario = visual_error_scenario
    scenario.page.marker = after_click
    body = {
        "state": summary, "confidence": min(confidences), "ambiguous": True,
        "has_cf_challenge": True, "reason": "multiple_widgets",
        "challenges": [{"state": state, "confidence": confidence}
                       for state, confidence in zip(states, confidences)],
    }
    scenario.predictions = ["unchecked", body] if after_click else [body]

    result = scenario.run()

    assert result[0] == "success"
    assert result[2]["completion_reason"] == "low_confidence_and_clear_page"
    assert result[2]["cfbox_state"] == "error"
    assert result[2]["cfbox_summary_state"] == summary
    assert result[2]["challenge_states"] == states
    assert result[2]["confidence"] == min(confidences)
    assert result[2]["low_confidence_count"] == 3
    assert result[2]["states"] == (["unchecked"] if after_click else []) + ["error"] * 3
    assert result[2]["unresolved_challenge"] is False
    assert len(scenario.clicks) == int(after_click)
    assert scenario.page.token_length == (100 if after_click else 0)
    assert len(scenario.calls) == len(scenario.screenshots) == 3 + int(after_click)
    assert scenario.submitted_images == [image for _, image in scenario.screenshots]
    assert len(set(scenario.submitted_images)) == len(scenario.calls)
    assert scenario.calls[-1][0] - scenario.calls[int(after_click)][0] == 2
    assert scenario.diagnostics["challenge_confidences"] == confidences
    assert scenario.diagnostics["low_confidence_eligible"] is True
    assert scenario.diagnostics["multiple_unresolved"] is True
    if summary == "unknown":
        assert any(f"子置信度={confidences}" in message and "低置信度复核资格=True" in message
                   for _, _, message in scenario.logs)


@pytest.mark.parametrize("other", ["error", "unknown"])
@pytest.mark.parametrize("confidence", [0.95, 1, None, "0.8", False])
@pytest.mark.parametrize("after_click", [False, True], ids=["no-token", "fresh-token"])
def test_one_non_low_child_blocks_even_with_low_aggregated_confidence(
    visual_error_scenario, other, confidence, after_click,
):
    scenario = visual_error_scenario
    scenario.page.marker = after_click
    body = {
        "state": "unknown", "confidence": 0.7024, "ambiguous": True,
        "challenges": [{"state": "error", "confidence": 0.7024},
                       {"state": other, "confidence": confidence}],
    }
    scenario.predictions = ["unchecked", body] if after_click else [body]

    result = scenario.run(wait_seconds=5)

    assert result[0] == "unknown"
    assert result[2]["challenge_confidences"] == [
        0.7024, float(confidence) if type(confidence) in (int, float) else None,
    ]
    assert result[2]["low_confidence_eligible"] is False
    assert result[2]["low_confidence_count"] == 0
    assert result[2]["multiple_unresolved"] is True
    assert result[2]["unresolved_challenge"] is True
    assert result[2]["fresh_token_after_click"] is after_click
    assert result[2]["token_present"] is after_click
    assert "cfbox_unresolved" in result[2]["blockers"]
    assert "completion_reason" not in result[2]
    assert len(scenario.clicks) == int(after_click)
    assert len(scenario.calls) == len(scenario.screenshots) == 5
    assert any("子置信度=" in message and "低置信度复核资格=False" in message
               for _, _, message in scenario.logs)


@pytest.mark.parametrize("state", ["error", "unknown", "verified", "absent", "unchecked", "processing"])
def test_child_center_blocks_low_confidence_even_if_not_exposed_as_click_target(
    visual_error_scenario, state,
):
    scenario = visual_error_scenario
    scenario.page.marker = False
    scenario.predictions = [{
        "state": "error", "confidence": 0.7024, "ambiguous": True,
        "challenges": [{"state": "error", "confidence": 0.7024},
                       {"state": state, "confidence": 0.1,
                        "checkbox_center_pct": {"x": 50, "y": 50}}],
    }]

    result = scenario.run(wait_seconds=4)

    assert result[0] == "unknown"
    assert result[2]["low_confidence_eligible"] is False
    assert result[2]["low_confidence_count"] == 0
    assert "cfbox_unresolved" in result[2]["blockers"]
    assert "completion_reason" not in result[2]
    assert bool(scenario.clicks) is (state == "unchecked")


@pytest.mark.parametrize("state", ["error", "unknown"])
def test_summary_center_explicitly_blocks_low_confidence(visual_error_scenario, state):
    scenario = visual_error_scenario
    scenario.page.marker = False
    scenario.predictions = [{"state": state, "confidence": 0.7024, "ambiguous": True,
                             "checkbox_center_pct": {"x": 50, "y": 50}}]

    result = scenario.run(wait_seconds=4)

    assert result[0] == "unknown"
    assert result[2]["low_confidence_eligible"] is False
    assert result[2]["low_confidence_count"] == 0
    assert scenario.clicks == []
    assert "completion_reason" not in result[2]


def test_single_error_child_allows_three_low_confidence_rechecks(visual_error_scenario):
    scenario = visual_error_scenario
    scenario.page.marker = False
    scenario.predictions = [{
        "state": "unknown", "confidence": None, "has_cf_challenge": True,
        "challenges": [{"state": "error", "confidence": 0.3}],
    }]
    result = scenario.run()

    assert result[0] == "success"
    assert result[2]["completion_reason"] == "low_confidence_and_clear_page"
    assert result[2]["cfbox_state"] == "error"
    assert result[2]["cfbox_summary_state"] == "unknown"
    assert result[2]["challenge_states"] == ["error"]
    assert result[2]["confidence"] == 0.3
    assert result[2]["low_confidence_count"] == 3
    assert len(scenario.calls) == len(scenario.screenshots) == 3
    assert scenario.calls[-1][0] - scenario.calls[0][0] == 2
    assert len(set(scenario.submitted_images)) == 3
    assert scenario.clicks == []


@pytest.mark.parametrize("other", ["unknown", "error", "processing", "unchecked"])
@pytest.mark.parametrize("confidence", [None, 0.8])
@pytest.mark.parametrize("after_click", [False, True], ids=["initial-clear", "fresh-token"])
def test_mixed_children_only_pass_with_strict_low_confidence_evidence(
    visual_error_scenario, other, confidence, after_click,
):
    scenario = visual_error_scenario
    body = {
        "state": "unknown", "confidence": None,
        "challenges": [{"state": "error", "confidence": 0.3},
                       {"state": other, "confidence": confidence}],
    }
    scenario.page.marker = after_click
    scenario.predictions = ["unchecked", body] if after_click else [body]
    result = scenario.run(wait_seconds=6)

    if other in {"error", "unknown"} and confidence is not None:
        assert result[0] == "success"
        assert result[2]["completion_reason"] == "low_confidence_and_clear_page"
        assert result[2]["low_confidence_count"] == 3
        assert result[2]["unresolved_challenge"] is False
        assert scenario.diagnostics["low_confidence_eligible"] is True
        assert len(scenario.calls) == 3 + int(after_click)
        assert scenario.calls[-1][0] - scenario.calls[int(after_click)][0] == 2
        assert len(scenario.clicks) == int(after_click)
        return
    assert result[0] == "unknown"
    assert len(scenario.calls) == 6
    assert len(scenario.clicks) == int(after_click)
    assert scenario.page.marker is False
    assert scenario.page.token_length == (100 if after_click else 0)
    assert result[2]["multiple_unresolved"] is True
    assert result[2]["unresolved_challenge"] is True
    assert result[2]["low_confidence_count"] == 0
    assert "cfbox_unresolved" in result[2]["blockers"]
    assert "completion_reason" not in result[2]


@pytest.mark.parametrize("challenges", [
    {}, "invalid", False, [None], [{}], [{"state": "invalid"}],
    [{"state": "verified"}, {"state": "invalid"}],
    [{"state": "unchecked", "checkbox_center_pct": {"x": True, "y": 50}}],
    [{"state": "unchecked", "checkbox_center_pct": {"x": 101, "y": 50}}],
    [{"state": "unchecked", "checkbox_center_pct": {"x": float("nan"), "y": 50}}],
])
def test_malformed_children_fail_before_click_or_pass(visual_error_scenario, challenges):
    scenario = visual_error_scenario
    scenario.page.marker = False
    scenario.predictions = [{"state": "absent", "confidence": 1, "challenges": challenges}]
    result = scenario.run()

    assert result[0] == "fail"
    assert "ValueError" in result[1]
    assert len(scenario.calls) == 1
    assert scenario.clicks == []
    assert "completion_reason" not in result[2]


@pytest.mark.parametrize("challenges", [{}, {"challenges": None}, {"challenges": []}])
def test_plain_unknown_clear_page_keeps_legacy_success(visual_error_scenario, challenges):
    scenario = visual_error_scenario
    scenario.page.marker = False
    scenario.predictions = [{"state": "unknown", "confidence": None, **challenges}]
    result = scenario.run()

    assert result[0] == "success"
    assert result[2]["state"] == "absent"
    assert result[2]["completion_reason"] == "unknown_and_clear_page"
    assert result[2]["cfbox_summary_state"] == "unknown"
    assert result[2]["challenge_states"] == []
    assert result[2]["states"] == ["unknown"] * 3
    assert result[2]["unresolved_challenge"] is False
    assert scenario.clicks == []
