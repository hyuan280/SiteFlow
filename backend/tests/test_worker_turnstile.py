import asyncio

import httpx
import pytest

from .test_worker_navigation import _CfStatePage
from worker import executor


@pytest.mark.parametrize(
    "marker,predictions,token_length,expected",
    [
        (False, ["absent"], 100, "success"),
        (False, ["unchecked"], 100, "unknown"),
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
