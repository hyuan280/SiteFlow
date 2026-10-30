import asyncio
import io

import httpx
import pytest
from PIL import Image

from .test_worker_navigation import _CfStatePage
from worker import executor


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
        (False, ["error"], 100, "fail"),
        (False, ["unchecked", "unchecked", "absent"], 100, "success"),
        (True, ["unchecked", "unchecked", "absent"], 100, "success"),
        (True, ["unchecked", "verified"], 100, "success"),
    ],
)
def test_dual_confirmation(monkeypatch, marker, predictions, token_length, expected):
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
            if state == "error":
                raise httpx.ConnectError("unavailable")
            return httpx.Response(200, json={
                "state": state, "checkbox_center_pct": {"x": 50, "y": 50}
            })

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


def test_click_uses_screenshot_size_when_remote_viewport_is_absent(monkeypatch):
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
            return httpx.Response(200, json={
                "state": state,
                "checkbox_center_pct": {"x": 50, "y": 50},
            })

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


@pytest.mark.parametrize("visible,failing,expected", [(False, True, True), (True, True, False), (True, False, True)])
def test_cf_text_skips_hidden_frames_and_humanized_reads(visible, failing, expected):
    class Element:
        async def is_visible(self):
            return visible

    class Frame:
        url = "about:blank"
        async def frame_element(self):
            return Element()
        async def evaluate(self, script):
            if failing:
                raise RuntimeError("frame body unavailable")
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
    assert state["marker"] is False


def test_main_body_unreadable_is_not_absence():
    class Page(_CfStatePage):
        async def evaluate(self, script):
            if script == executor.CF_PAGE_TEXT_SCRIPT:
                raise RuntimeError("body missing")
            return await super().evaluate(script)

    assert asyncio.run(executor._cf_page_state(Page()))["text_readable"] is False


def test_challenge_title_without_widget():
    class Page(_CfStatePage):
        async def title(self):
            return "Just a moment..."

    state = asyncio.run(executor._cf_page_state(Page()))
    assert state["challenge_text"] is True
    assert state["marker"] is False


@pytest.mark.parametrize("prediction", ["unknown", "verified"])
@pytest.mark.parametrize("blocker", ["title", "url", "marker_error", "text_error", "loading"])
def test_inference_does_not_bypass_page_checks(monkeypatch, blocker, prediction):
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
