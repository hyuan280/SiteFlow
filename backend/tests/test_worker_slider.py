from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker import executor
from worker.browser_input import _InputGuard


CONFIG = {
    "container_selector": ["#dragContainer"],
    "handle_selector": ["#dragHandler"],
    "success_selector": [".verified"],
}


@pytest.fixture
def slider(monkeypatch):
    gate = _InputGuard()
    container = SimpleNamespace(bounding_box=AsyncMock(return_value={
        "x": 808, "y": 400, "width": 304, "height": 37,
    }))
    handle = SimpleNamespace(bounding_box=AsyncMock(side_effect=[
        {"x": 810, "y": 402, "width": 40, "height": 33},
        {"x": 1070, "y": 402, "width": 40, "height": 33},
    ]))
    mouse = SimpleNamespace(move=AsyncMock(), down=AsyncMock(), up=AsyncMock())
    page = SimpleNamespace(
        mouse=mouse, bring_to_front=AsyncMock(),
        context=SimpleNamespace(_impl_obj=SimpleNamespace(_siteflow_input_guard=gate)),
    )
    finder = AsyncMock(side_effect=[container, handle])
    success = AsyncMock(return_value=object())
    monkeypatch.setattr(executor, "_find_locator", finder)
    monkeypatch.setattr(executor, "_find_success_locator", success)
    return SimpleNamespace(page=page, gate=gate, container=container, handle=handle,
                           finder=finder, success=success, mouse=mouse)


def test_slider_coordinates_and_actual_displacement_are_distinct(slider):
    log = Mock()
    status, message, data = asyncio.run(executor._run_slider(slider.page, CONFIG, None, log))
    assert (status, message) == ("success", "滑块验证通过")
    assert data == {"verified": True, "planned_distance": 262, "handle_displacement": 260}
    assert [call.args for call in slider.mouse.move.await_args_list] == [(830, 418.5), (1092, 418.5)]
    assert slider.mouse.move.await_args_list[1].kwargs == {"steps": 25}
    slider.mouse.down.assert_awaited_once()
    slider.mouse.up.assert_awaited_once()
    slider.success.assert_awaited_once()
    assert slider.success.await_args.args == (slider.page, [".verified"])
    assert slider.success.await_args.kwargs["wait_ms"] == executor.SELECTOR_WAIT_MS
    assert any("实际位移=260" in call.args[1] for call in log.call_args_list)


@pytest.mark.parametrize("verified", [True, False, None])
def test_slider_success_requires_configured_visible_evidence(slider, verified):
    config = {**CONFIG, "success_selector": [] if verified is None else [".verified"]}
    slider.success.return_value = object() if verified else None
    status, _, data = asyncio.run(executor._run_slider(slider.page, config, None))
    assert status == ("unknown" if verified is False else "success")
    assert data["verified"] is bool(verified)
    assert slider.success.await_count == (0 if verified is None else 1)


@pytest.mark.parametrize("failure", ["error", "cancel"])
def test_slider_always_releases_mouse_and_input_gate(slider, failure):
    async def move(*args, **kwargs):
        assert slider.gate.owner is asyncio.current_task()
        if kwargs.get("steps"):
            raise RuntimeError("drag interrupted") if failure == "error" else asyncio.CancelledError()

    slider.mouse.move.side_effect = move
    with pytest.raises(RuntimeError if failure == "error" else asyncio.CancelledError):
        asyncio.run(executor._run_slider(slider.page, CONFIG, None))
    slider.mouse.up.assert_awaited_once()
    assert slider.gate.owner is None and not slider.gate.lock.locked()
    slider.success.assert_not_awaited()


def test_slider_diagnostics_failure_does_not_mask_success(slider):
    slider.handle.bounding_box.side_effect = [
        {"x": 810, "y": 402, "width": 40, "height": 33}, RuntimeError("removed"),
    ]
    status, _, data = asyncio.run(executor._run_slider(slider.page, CONFIG, None))
    assert status == "success" and data["verified"]
    assert "handle_displacement" not in data
    slider.mouse.up.assert_awaited_once()


def test_slider_does_not_allow_other_tab_input_between_mouse_calls(slider):
    async def run():
        events = []
        entered, queued = asyncio.Event(), asyncio.Event()

        async def move(*args, **kwargs):
            assert slider.gate.owner is asyncio.current_task()
            events.append("drag" if kwargs.get("steps") else "approach")
            if not kwargs:
                entered.set()
                await queued.wait()
                await asyncio.sleep(0)
                assert events == ["approach"]

        async def other_input():
            await entered.wait()
            queued.set()
            async with slider.gate.hold():
                events.append("other")

        async def down():
            events.append("down")

        async def up():
            events.append("up")

        slider.mouse.move.side_effect = move
        slider.mouse.down.side_effect = down
        slider.mouse.up.side_effect = up
        await asyncio.wait_for(asyncio.gather(
            executor._run_slider(slider.page, CONFIG, None), other_input(),
        ), 1)
        assert events == ["approach", "down", "drag", "up", "other"]

    asyncio.run(run())


@pytest.mark.parametrize("humanized", [False, True])
@pytest.mark.parametrize("mode", ["hidden_first", "reload", "rejected"])
def test_real_chromium_slider_geometry_and_success(monkeypatch, humanized, mode):
    if humanized:
        pytest.importorskip("cloakbrowser.human")
    monkeypatch.setattr(executor, "SELECTOR_WAIT_MS", 500)

    async def run():
        from playwright.async_api import async_playwright
        from worker.browser_input import humanize_actions

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
                context = await browser.new_context(viewport={"width": 1920, "height": 1080})
                if humanized:
                    humanize_actions(context)
                page = await context.new_page()
                html = """<style>
                    #dragContainer {position:absolute;top:50%;left:50%;
                        transform:translate(-50%,-50%);width:300px;height:33px;
                        border:2px solid #e8e8e8;}
                    #dragHandler {position:absolute;width:40px;height:100%;background:#aaa;}
                    </style><div id="dragContainer"><div id="dragHandler"></div></div>
                    <div class="verified" hidden>Old hidden success</div>
                    <script>
                    const container = document.querySelector('#dragContainer');
                    const handle = document.querySelector('#dragHandler');
                    const max = container.clientWidth - handle.clientWidth;
                    window.trace = [];
                    let dragging = false;
                    handle.onmousedown = e => {e.preventDefault(); dragging = true;
                        trace.push({type:'down', x:e.clientX});};
                    document.onmousemove = e => {
                        if (!dragging) return;
                        const left = Math.max(0, Math.min(max,
                            e.clientX - container.getBoundingClientRect().left - handle.clientWidth/2));
                        handle.style.left = left + 'px';
                    };
                    document.onmouseup = e => {
                        if (!dragging) return;
                        dragging = false;
                        trace.push({type:'up', x:e.clientX, left:handle.offsetLeft});
                        if (handle.offsetLeft < max || MODE === 'rejected') {
                            handle.style.left = '0px'; return;
                        }
                        if (MODE === 'reload') {
                            location.href = '/attendance'; return;
                        }
                        const success = document.createElement('div');
                        success.className = 'verified'; success.textContent = 'Verified';
                        document.body.append(success);
                    };
                    </script>""".replace("MODE", repr(mode))
                # All requests stay local, including the simulated post-verification navigation.
                await context.route("**/*", lambda route: route.fulfill(
                    content_type="text/html", body=(
                        '<div class="verified">Attendance</div>'
                        if route.request.url.endswith("/attendance") else html
                    ),
                ))
                await page.goto("https://slider.test/challenge")
                status, _, data = await executor._run_slider(page, CONFIG, None)
                assert status == ("unknown" if mode == "rejected" else "success")
                assert data["verified"] is (mode != "rejected")
                assert data["planned_distance"] == 262
                assert data["handle_displacement"] == 260
                if mode != "reload":
                    trace = await page.evaluate("trace")
                    assert [item["type"] for item in trace] == ["down", "up"]
                    assert trace[1]["x"] - trace[0]["x"] == 262
                    assert trace[1]["left"] == 260
                else:
                    assert page.url == "https://slider.test/attendance"
            finally:
                await browser.close()

    asyncio.run(run())
