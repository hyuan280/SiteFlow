from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker import executor  # noqa: E402


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN1sAAAAASUVORK5CYII="
)
PRIVATE_TEXT = "https://private.invalid/font.woff?token=secret private-page-content"


@pytest.fixture
def screenshot_case(monkeypatch):
    for name in (
        "SCREENSHOT_TIMEOUT_SECONDS",
        "SCREENSHOT_DIAGNOSTIC_SECONDS",
        "SCREENSHOT_CLEANUP_SECONDS",
    ):
        monkeypatch.setattr(executor, name, 0.01)
    session = SimpleNamespace(
        send=AsyncMock(return_value={"data": base64.b64encode(PNG).decode("ascii")}),
        detach=AsyncMock(),
    )

    async def send(method, *_args):
        if method == "Page.getLayoutMetrics":
            return {"cssVisualViewport": {"pageX": 0, "pageY": 100, "clientWidth": 320,
                                       "clientHeight": 240, "scale": 1},
                    "contentSize": {"width": 320}, "cssContentSize": {"width": 320}}
        if method == "Runtime.evaluate":
            return {"result": {"value": 1}}
        return session.send.return_value

    session.send.side_effect = send
    page = SimpleNamespace(
        viewport_size=None,
        screenshot=AsyncMock(side_effect=executor.PlaywrightTimeoutError(PRIVATE_TEXT)),
        evaluate=AsyncMock(return_value={"loading": True, "pending": 2, "failed": 1}),
        context=SimpleNamespace(new_cdp_session=AsyncMock(return_value=session)),
    )
    return SimpleNamespace(page=page, session=session, log=Mock())


def test_page_screenshot_success_does_not_use_cdp(screenshot_case, monkeypatch):
    case = screenshot_case
    monkeypatch.setattr(executor, "SCREENSHOT_TIMEOUT_SECONDS", 3.0)
    timeout = Mock(wraps=asyncio.timeout)
    monkeypatch.setattr(executor.asyncio, "timeout", timeout)
    case.page.screenshot.side_effect = None
    case.page.screenshot.return_value = PNG
    private = SimpleNamespace(validate_origin=AsyncMock(), pin=AsyncMock())

    assert asyncio.run(executor._capture_page_screenshot(
        case.page, case.log, private=private,
    )) == PNG

    case.page.screenshot.assert_awaited_once_with(type="png", timeout=3000)
    timeout.assert_called_once_with(3.25)
    case.page.evaluate.assert_not_called()
    case.page.context.new_cdp_session.assert_not_called()
    case.session.detach.assert_not_called()
    private.validate_origin.assert_not_called()
    private.pin.assert_not_called()
    case.log.assert_not_called()


@pytest.mark.parametrize("error_type", [executor.PlaywrightTimeoutError, TimeoutError])
def test_page_screenshot_timeout_uses_page_bound_cdp(screenshot_case, error_type):
    case = screenshot_case
    case.page.screenshot.side_effect = error_type(PRIVATE_TEXT)

    assert asyncio.run(executor._capture_page_screenshot(case.page, case.log)) == PNG

    case.page.screenshot.assert_awaited_once_with(type="png", timeout=10)
    case.page.evaluate.assert_awaited_once()
    case.page.context.new_cdp_session.assert_awaited_once_with(case.page)
    case.session.send.assert_any_await("Page.captureScreenshot", {
        "format": "png", "fromSurface": True, "captureBeyondViewport": False,
        "clip": {"x": 0, "y": 100, "width": 320, "height": 240, "scale": 1},
    })
    case.session.detach.assert_awaited_once_with()
    case.log.assert_any_call("WARN", "screenshot fonts: loading=True, pending=2, failed=1")
    assert PRIVATE_TEXT not in repr(case.log.call_args_list)


@pytest.mark.parametrize("diagnostic_error", [False, True])
def test_screenshot_font_and_cleanup_logs_do_not_leak(screenshot_case, diagnostic_error):
    case = screenshot_case
    if diagnostic_error:
        case.page.evaluate.side_effect = RuntimeError(PRIVATE_TEXT)
    else:
        case.page.evaluate.return_value.update(url=PRIVATE_TEXT, content=PRIVATE_TEXT)
    case.session.detach.side_effect = RuntimeError(PRIVATE_TEXT)

    assert asyncio.run(executor._capture_page_screenshot(case.page, case.log)) == PNG

    if diagnostic_error:
        case.log.assert_any_call("WARN", "screenshot font diagnostics unavailable")
    else:
        case.log.assert_any_call("WARN", "screenshot fonts: loading=True, pending=2, failed=1")
    case.log.assert_any_call("WARN", "screenshot CDP cleanup failed")
    for secret in PRIVATE_TEXT.split():
        assert secret not in repr(case.log.call_args_list)


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
def test_page_screenshot_ordinary_error_does_not_fallback(screenshot_case, error_type):
    case = screenshot_case
    error = error_type(PRIVATE_TEXT)
    case.page.screenshot.side_effect = error

    with pytest.raises(error_type) as caught:
        asyncio.run(executor._capture_page_screenshot(case.page, case.log))

    assert caught.value is error
    case.page.evaluate.assert_not_called()
    case.page.context.new_cdp_session.assert_not_called()
    case.log.assert_not_called()


@pytest.mark.parametrize("response", [
    {}, {"data": None}, {"data": ""}, {"data": "%%%"},
    {"data": base64.b64encode(b"not a PNG").decode("ascii")},
    {"data": base64.b64encode(PNG).decode("ascii") + "!"},
], ids=["missing", "null", "empty", "invalid-base64", "not-png", "base64-trailing-junk"])
def test_cdp_screenshot_rejects_invalid_data_and_detaches(screenshot_case, response):
    case = screenshot_case
    case.session.send.return_value = response

    with pytest.raises((KeyError, TypeError, ValueError)):
        asyncio.run(executor._capture_page_screenshot(case.page, case.log))

    case.session.detach.assert_awaited_once_with()
    assert not any("captured" in call.args[1] for call in case.log.call_args_list)


@pytest.mark.parametrize("stage", ["screenshot", "diagnostic", "session", "send", "detach"])
def test_screenshot_hung_operations_are_bounded(screenshot_case, stage):
    case = screenshot_case

    async def run():
        canceled = asyncio.Event()

        async def hang(*_args, **_kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                canceled.set()

        operation = {
            "screenshot": case.page.screenshot,
            "diagnostic": case.page.evaluate,
            "session": case.page.context.new_cdp_session,
            "send": case.session.send,
            "detach": case.session.detach,
        }[stage]
        operation.side_effect = hang
        # The watchdog must fail, not masquerade as the helper's own timeout.
        async with asyncio.timeout(0.75):
            if stage in {"session", "send"}:
                with pytest.raises(TimeoutError):
                    await executor._capture_page_screenshot(case.page, case.log)
            else:
                assert await executor._capture_page_screenshot(case.page, case.log) == PNG
        assert canceled.is_set()

    asyncio.run(run())
    if stage == "session":
        case.session.send.assert_not_called()
        case.session.detach.assert_not_called()
    else:
        case.session.detach.assert_awaited_once_with()
    if stage == "diagnostic":
        case.log.assert_any_call("WARN", "screenshot font diagnostics unavailable")
    if stage == "detach":
        case.log.assert_any_call("WARN", "screenshot CDP cleanup failed")


@pytest.mark.parametrize("stage", ["screenshot", "diagnostic", "session", "send", "detach"])
def test_screenshot_external_cancellation_propagates(screenshot_case, stage):
    case = screenshot_case

    async def run():
        entered = asyncio.Event()

        async def hang(*_args, **_kwargs):
            entered.set()
            await asyncio.Event().wait()

        operation = {
            "screenshot": case.page.screenshot,
            "diagnostic": case.page.evaluate,
            "session": case.page.context.new_cdp_session,
            "send": case.session.send,
            "detach": case.session.detach,
        }[stage]
        operation.side_effect = hang
        task = asyncio.create_task(executor._capture_page_screenshot(case.page, case.log))
        try:
            async with asyncio.timeout(0.75):
                await entered.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert task.cancelled()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    if stage in {"send", "detach"}:
        case.session.detach.assert_awaited_once_with()
    else:
        case.session.detach.assert_not_called()
    if stage in {"screenshot", "diagnostic"}:
        case.page.context.new_cdp_session.assert_not_called()
    if stage == "screenshot":
        case.page.evaluate.assert_not_called()
        case.log.assert_not_called()


@pytest.mark.parametrize("allowed", [False, True])
def test_private_screenshot_validates_before_cdp_without_pinning(screenshot_case, allowed):
    case = screenshot_case
    error = RuntimeError(PRIVATE_TEXT)

    async def validate():
        case.page.context.new_cdp_session.assert_not_called()
        if not allowed:
            raise error

    private = SimpleNamespace(validate_origin=AsyncMock(side_effect=validate), pin=AsyncMock())
    if allowed:
        assert asyncio.run(executor._capture_page_screenshot(
            case.page, case.log, private=private,
        )) == PNG
        case.page.context.new_cdp_session.assert_awaited_once_with(case.page)
        case.session.detach.assert_awaited_once_with()
    else:
        with pytest.raises(RuntimeError) as caught:
            asyncio.run(executor._capture_page_screenshot(case.page, case.log, private=private))
        assert caught.value is error
        case.page.context.new_cdp_session.assert_not_called()
        case.session.detach.assert_not_called()
    private.validate_origin.assert_awaited_once_with()
    private.pin.assert_not_called()


def test_failure_artifact_keeps_browser_errors_out_of_log(screenshot_case, monkeypatch, tmp_path):
    case = screenshot_case
    monkeypatch.setattr(executor, "ARTIFACT_ROOT", tmp_path)
    artifacts = executor.JobArtifacts({"run_id": 1, "job_id": 2})
    case.page.screenshot.side_effect = RuntimeError(PRIVATE_TEXT)

    assert asyncio.run(artifacts.capture_failure(case.page, "sign", "Sign")) is None

    log = artifacts.log_path.read_text(encoding="utf-8")
    assert "failure screenshot failed" in log
    for secret in PRIVATE_TEXT.split():
        assert secret not in log
    assert artifacts.screenshots == []
    case.page.context.new_cdp_session.assert_not_called()


def test_failure_artifact_cdp_save_preserves_failed_job(screenshot_case, monkeypatch, tmp_path):
    case = screenshot_case
    monkeypatch.setattr(executor, "ARTIFACT_ROOT", tmp_path)
    case.page.on = Mock()
    case.page.remove_listener = Mock()
    case.page.bring_to_front = AsyncMock()
    case.page.close = AsyncMock()
    case.page.url = "https://site.invalid/"
    lane = SimpleNamespace(
        execution_semaphore=asyncio.Semaphore(1), task_pages={}, page=None,
        context=SimpleNamespace(new_page=AsyncMock(return_value=case.page)),
    )
    manager = SimpleNamespace(
        get=Mock(return_value=lane),
        lease_lane=AsyncMock(return_value=("test-lease", {"browser": "fake"})),
        release_lane=AsyncMock(),
    )
    step_data = {"reason": "business_rejected", "value": False}
    run_step = AsyncMock(return_value=("fail", "business failure", step_data))
    monkeypatch.setattr(executor, "_run_step", run_step)
    result = asyncio.run(executor.execute_job(manager, {
        "run_id": 1, "job_id": 2,
        "steps": [{"step_key": "sign", "feature_type": "action.click", "label": "Sign"}],
    }))

    assert result["status"] == "fail"
    assert result["message"] == "business failure"
    assert len(result["steps"]) == 1
    assert result["steps"][0]["status"] == "fail"
    assert result["steps"][0]["message"] == "business failure"
    assert result["steps"][0]["data"] == step_data
    assert result["outputs"] == []
    images = [item for item in result["artifacts"] if item["kind"] == "screenshot"]
    assert len(images) == 1
    image = images[0]
    assert image["step_key"] == "sign"
    assert "Sign" in image["label"]
    assert image["content_type"] == "image/png"
    assert image["size_bytes"] == len(PNG)
    assert (tmp_path / image["relative_path"]).read_bytes() == PNG
    case.page.context.new_cdp_session.assert_awaited_once_with(case.page)
    case.session.detach.assert_awaited_once_with()
    log_item = next(item for item in result["artifacts"] if item["kind"] == "log")
    log = (tmp_path / log_item["relative_path"]).read_text(encoding="utf-8")
    assert "CDP viewport screenshot captured" in log
    assert PRIVATE_TEXT not in log


def test_job_artifacts_saves_captcha_image(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(executor, "ARTIFACT_ROOT", tmp_path)
    artifacts = executor.JobArtifacts({"run_id": 1, "job_id": 2})

    assert artifacts.save_image(
        b"captcha-png",
        prefix="captcha",
        label="验证码截图 · 登录",
        step_key="sign",
    )
    captcha = [item for item in artifacts.snapshot() if item["kind"] == "captcha"]
    assert len(captcha) == 1
    assert captcha[0]["step_key"] == "sign"
    assert captcha[0]["content_type"] == "image/png"
    assert (tmp_path / captcha[0]["relative_path"]).read_bytes() == b"captcha-png"


def test_read_captcha_saves_initial_and_refreshed_images_and_logs_text(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(executor, "ARTIFACT_ROOT", tmp_path)
    logs: list[str] = []
    captured: list[tuple[bytes, str]] = []
    results = iter([("bad", 0.1), ("ab12", 0.99)])
    monkeypatch.setattr(executor, "_classify_captcha", lambda _image: next(results))
    refreshed = AsyncMock()
    refreshed.screenshot.return_value = b"refreshed-png"

    async def fake_refresh(*_args):
        return refreshed, b"refreshed-png"

    monkeypatch.setattr(executor, "_refresh_captcha_image", fake_refresh)
    image = AsyncMock()
    image.screenshot.return_value = b"initial-png"

    async def capture(image_bytes: bytes, label: str) -> None:
        captured.append((image_bytes, label))

    status, text, message, data = asyncio.run(
        executor._read_captcha(
            None,
            image,
            {"charset": "alnum", "length": 4},
            lambda _level, value: logs.append(value),
            None,
            capture,
        )
    )

    assert (status, text, message) == ("ok", "ab12", "")
    assert data["refreshed"] is True
    assert captured == [
        (b"initial-png", "验证码截图"),
        (b"refreshed-png", "验证码截图（刷新后）"),
    ]
    assert any("bad" in line for line in logs)
    assert any("ab12" in line for line in logs)
