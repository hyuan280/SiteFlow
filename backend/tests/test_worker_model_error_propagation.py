from __future__ import annotations

import asyncio
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.error import HTTPError

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker import assistant_private, executor, grid_captcha, vision  # noqa: E402
from tests.test_worker_grid_captcha import Page as GridPage  # noqa: E402
from tests.test_assistant_browser import Manager, TOKEN, URL, public_dns  # noqa: E402, F401
from tests.test_assistant_private_steps import request  # noqa: E402
from worker.assistant_browser import AssistantBrowser  # noqa: E402


FEATURES = ["sign.image_quiz", "challenge.image_captcha", "sign.iframe.image_captcha",
            "challenge.grid_captcha", "flow.result"]
MODEL = {"base_url": "https://private-provider.invalid/v1", "api_key": "private-api-value",
         "model": "private-model-name", "supports_vision": True, "timeout": 30}


class StringBomb(ValueError):
    @property
    def reason(self):
        pytest.fail("Unknown exception attributes must not be inspected")

    def __str__(self):
        pytest.fail("Unknown exceptions must not be formatted")


@pytest.fixture
def caller(monkeypatch):
    page = GridPage()
    page.url = "https://site.invalid/"
    page.evaluate = AsyncMock(return_value="Page content")
    page.wait_for_load_state = AsyncMock()
    image = SimpleNamespace(screenshot=AsyncMock(return_value=b"captcha-image"))
    option = SimpleNamespace(click=AsyncMock())
    form = {"image": b"quiz-image", "question": "Choose an image", "options": ["Choice A"],
            "option_rows": [("Choice A", option, False)], "answer_selector": "#answer"}
    monkeypatch.setattr(executor, "_read_quiz_form", AsyncMock(return_value=(form, "")))
    monkeypatch.setattr(executor, "_find_locator", AsyncMock(return_value=image))
    monkeypatch.setattr(executor, "_find_frame", AsyncMock(return_value=page))
    refresh = AsyncMock(return_value=(image, b"fresh-image"))
    monkeypatch.setattr(executor, "_refresh_captcha_image", refresh)
    quiz_refresh = AsyncMock()
    monkeypatch.setattr(executor, "_refresh_quiz", quiz_refresh)
    monkeypatch.setattr(executor, "_wait_quiz_success", AsyncMock(return_value=(True, "text")))
    snapshot = AsyncMock(return_value=grid_captcha_state(page))
    monkeypatch.setattr(grid_captcha, "_wait_snapshot", snapshot)
    logs = []
    model = {**MODEL, "execution_settings": {"llm_enabled": True, "use_vision": False}}

    def run(feature, **kwargs):
        return asyncio.run(executor._run_step(
            page, {"feature_type": feature, "config": {
                "captcha_method": "vision", "image_selector": ["#image"],
                "iframe_selector": ["iframe"], "input_selector": ["#answer"],
                "max_refresh": 3, "llm_enabled": True, "length": 4,
            }}, kwargs.pop("credentials", None), kwargs.pop("twofauth", None),
            kwargs.pop("cfbox", None), None, None, "general", vision=model,
            log=kwargs.pop("log", lambda level, text: logs.append((level, text))), **kwargs,
        ))

    return SimpleNamespace(run=run, page=page, image=image, option=option, logs=logs,
                           model=model, refresh=refresh, quiz_refresh=quiz_refresh)


def grid_captcha_state(page):
    from tests.test_worker_grid_captcha import State

    return State(page)


@pytest.fixture
def provider(monkeypatch):
    original_client = httpx.AsyncClient

    def install(failure, message="Quota exhausted; contact administrator"):
        calls = []

        def sync(request, timeout):
            calls.append(request)
            assert timeout == 30
            if failure == "timeout":
                raise httpx.ReadTimeout("untrusted transport text")
            if failure == "http":
                raise HTTPError(request.full_url, 429, "untrusted HTTP text", {}, BytesIO(
                    json.dumps({"error": {"message": message, "type": "quota_error",
                                         "code": "quota_exceeded"}}).encode(),
                ))
            return BytesIO(b"not JSON")

        def async_handler(request):
            calls.append(request)
            if failure == "timeout":
                raise httpx.ReadTimeout("untrusted transport text", request=request)
            if failure == "http":
                return httpx.Response(429, json={"error": {
                    "message": message, "type": "quota_error", "code": "quota_exceeded",
                }})
            return httpx.Response(200, text="not JSON")

        monkeypatch.setattr(vision, "urlopen", sync)
        monkeypatch.setattr(vision.httpx, "AsyncClient", lambda **kwargs: original_client(
            transport=httpx.MockTransport(async_handler), **kwargs,
        ))
        return calls

    return install


@pytest.mark.parametrize("feature", FEATURES)
@pytest.mark.parametrize("failure,code", [("timeout", "timeout"), ("http", "http_error"),
                                          ("json", "invalid_json")])
def test_provider_diagnostics_reach_message_log_and_data(caller, provider, feature, failure, code):
    calls = provider(failure)
    status, message, data = caller.run(feature)
    assert status == ("waiting_human" if feature in {"sign.image_quiz", "challenge.grid_captcha"} else "fail")
    assert data["reason"] == ("model_judgment_failed" if feature == "flow.result" else
                              "provider_failed" if "image_captcha" in feature else "vision_failed")
    assert data["provider_error"] == code
    assert code in message
    assert any(message in text for _, text in caller.logs)
    if failure == "timeout":
        assert data["timeout_phase"] == "read"
        assert data["timeout_seconds"] == 30
        assert "read" in message and "30" in message
        assert "步骤超时" not in message
    if failure == "http":
        assert data["http_status"] == 429
        assert data["provider_message"] == "Quota exhausted; contact administrator"
        assert data["provider_type"] == "quota_error"
        assert data["provider_code"] == "quota_exceeded"
        assert "429" in message and data["provider_message"] in message
    assert len(calls) == 1
    caller.refresh.assert_not_awaited()
    caller.quiz_refresh.assert_not_awaited()
    caller.option.click.assert_not_awaited()
    assert not caller.page.cell_clicks and not caller.page.refreshes and not caller.page.submits
    assert all(value not in repr((message, data, caller.logs)) for value in MODEL.values()
               if isinstance(value, str))
    assert "_model_redaction_values" not in caller.model


@pytest.mark.parametrize("feature", FEATURES)
def test_unknown_errors_are_static_and_never_stringified(caller, monkeypatch, feature):
    name = {"sign.image_quiz": "answer_image_quiz", "challenge.grid_captcha": "answer_grid_captcha",
            "flow.result": "judge_result"}.get(feature, "recognize")
    helper = (AsyncMock(side_effect=StringBomb()) if name in {"answer_grid_captcha", "judge_result"}
              else Mock(side_effect=StringBomb()))
    monkeypatch.setattr(vision, name, helper)
    result = caller.run(feature)
    assert result[2]["provider_error"] == "invalid_response"
    assert helper.call_count == 1
    caller.refresh.assert_not_awaited()
    caller.quiz_refresh.assert_not_awaited()


@pytest.mark.parametrize("feature", FEATURES)
def test_all_job_secrets_are_available_before_provider_detail_redaction(caller, provider, feature):
    long_cookie = "opaque cookie start " + "private segment " * 40 + "opaque cookie tail"
    secrets = ["credential-user", "credential-password", "twofauth-value", "cfbox-value",
               "lease-value", "plugin-value", "983761", "cookie-value", long_cookie]
    calls = provider("http", "Quota exhausted " + " ; ".join([*secrets, *[
        value for value in MODEL.values() if isinstance(value, str)
    ]]))
    result = caller.run(feature, credentials={"username": secrets[0], "password": secrets[1]},
                        twofauth={"token": secrets[2]}, cfbox={"api_key": secrets[3]},
                        secret_payload={"secret_access": {"token": secrets[4]},
                                        "totp_code": secrets[6], "cookies": [
                                            {"value": secrets[7]}, {"value": long_cookie}]},
                        plugin_runtimes={"example": {"token": secrets[5]}})
    output = repr((result, caller.logs))
    assert "Quota exhausted" in result[1]
    assert "[REDACTED]" in result[1]
    for secret in [*secrets, "opaque cookie start", "private segment", "opaque cookie tail"]:
        assert secret not in output
    assert len(calls) == 1


@pytest.mark.parametrize("feature", FEATURES)
def test_error_details_are_saved_in_artifact_log(caller, provider, monkeypatch, tmp_path, feature):
    monkeypatch.setattr(executor, "ARTIFACT_ROOT", tmp_path)
    artifacts = executor.JobArtifacts({"run_id": 1, "job_id": 1})
    provider("http", "Quota exhausted private-credential")
    result = caller.run(feature, credentials={"password": "private-credential"}, log=artifacts.log)
    log = artifacts.log_path.read_text()
    assert result[1] in log
    assert "Quota exhausted [REDACTED]" in log
    assert "private-credential" not in log
    assert all(value not in log for value in MODEL.values() if isinstance(value, str))


@pytest.mark.parametrize("feature", FEATURES)
def test_existing_redactions_and_resolved_secret_fingerprints_are_retained(caller, provider, feature):
    secret = "resolved-private-code"
    caller.model["_model_redaction_values"] = ("preexisting-private-value",)
    caller.model["model_configs"] = [{"api_key": "alternate-private-value"}]
    provider("http", "Quota exhausted " + secret + " preexisting-private-value alternate-private-value")
    result = caller.run(feature, secret_payload={"_result_secret_fingerprints": {
        (len(secret), hashlib.sha256(secret.encode()).hexdigest()),
    }})
    output = repr((result, caller.logs))
    assert "Quota exhausted" in output
    for value in (secret, "preexisting-private-value", "alternate-private-value"):
        assert value not in output
    assert caller.model["_model_redaction_values"] == ("preexisting-private-value",)


@pytest.mark.parametrize("feature", ["challenge.image_captcha", "sign.iframe.image_captcha"])
def test_vision_failure_after_refresh_has_same_safe_diagnostics(caller, monkeypatch, feature):
    helper = Mock(side_effect=["x", vision.ModelCallError(
        "timeout", "request", details={"timeout_phase": "read", "timeout_seconds": 30},
    )])
    monkeypatch.setattr(vision, "recognize", helper)
    result = caller.run(feature)
    assert result[0] == "fail"
    assert result[2]["reason"] == "provider_failed"
    assert result[2]["refreshed"] is True
    assert result[2]["timeout_seconds"] == 30
    assert "read" in result[1]
    assert helper.call_count == 2
    caller.refresh.assert_awaited_once()
    assert any(result[1] in text for _, text in caller.logs)


@pytest.mark.parametrize("method,helper", [("ddddocr", "_classify_captcha"),
                                          ("nexusphp_gd5", "_recognize_nexusphp_gd5")])
def test_non_model_recognizers_do_not_expose_model_details(caller, monkeypatch, method, helper):
    monkeypatch.setattr(executor, helper, Mock(side_effect=vision.ModelCallError(
        "http_error", "request", 429, details={"provider_message": "private provider prose"},
    )))
    result = asyncio.run(executor._read_captcha(
        caller.page, caller.image, {"captcha_method": method}, lambda *_: None, caller.model,
    ))
    assert result[0] == "fail" and result[3]["reason"] == "provider_failed"
    assert "provider_message" not in repr(result) and "private provider prose" not in repr(result)


def test_successful_quiz_reason_is_redacted_before_normalization(caller, monkeypatch):
    secret = "private-left   \t private-right"
    reason = "Visible evidence " + secret
    monkeypatch.setattr(vision, "urlopen", lambda *_args, **_kwargs: BytesIO(json.dumps({
        "choices": [{"message": {"content": json.dumps({"selected_index": 1, "reason": reason})}}],
    }).encode()))
    result = caller.run("sign.image_quiz", secret_payload={"credentials": {"cookie": secret}})
    assert result[0] == "success"
    assert any("Visible evidence [REDACTED]" in text for _, text in caller.logs)
    assert "private-left" not in repr(caller.logs) and "private-right" not in repr(caller.logs)


def test_private_grid_model_failure_is_determinate_without_prose(caller, monkeypatch):
    state = grid_captcha_state(caller.page)
    evaluate = state.evaluate

    async def read(script, args=None):
        if "ownerDocument.location.href" in script:
            return caller.page.url
        return await evaluate(script, args)

    state.evaluate = read
    monkeypatch.setattr(grid_captcha, "_wait_snapshot", AsyncMock(return_value=state))
    model = AsyncMock(side_effect=vision.ModelCallError(
        "http_error", "request", 429, details={"provider_message": "private provider prose"},
    ))
    monkeypatch.setattr(vision, "answer_grid_captcha", model)
    context = SimpleNamespace(page=caller.page, origin=caller.page.url,
                              validate_origin=AsyncMock(), action_started=False)
    result = asyncio.run(assistant_private._run_grid(context, {"max_refresh": 3}, caller.model))
    assert result == ("fail", "", {})
    assert not context.action_started
    model.assert_awaited_once()
    assert not caller.page.cell_clicks and not caller.page.refreshes and not caller.page.submits
    assert state.disposed


@pytest.mark.parametrize("feature", ["sign.image_quiz", "challenge.image_captcha", "challenge.grid_captcha"])
def test_assistant_boundary_discards_provider_prose_and_model_choice_reasons(public_dns, monkeypatch, feature):
    private_prose = "Provider instructions that must not reach the assistant"
    monkeypatch.setattr(executor, "_run_step", AsyncMock(return_value=("fail", private_prose, {
        "reason": "vision_failed", "provider_error": "http_error", "stage": "request",
        "http_status": 429, "provider_message": private_prose, "provider_code": "quota_exceeded",
        "judgment_reason": "private model choice reasoning", "selected_index": 2,
    })))

    async def run():
        browser = AssistantBrowser(Manager())
        try:
            sid = (await browser.open(URL, "interaction", TOKEN))["session_id"]
            config = {
                "sign.image_quiz": {"image_selector": ["#image"], "answer_selector": [".answer"]},
                "challenge.image_captcha": {"image_selector": ["#image"], "input_selector": ["#code"],
                                            "captcha_method": "vision"},
            }.get(feature, {})
            response = await browser.execute(sid, request(feature, config, runtime={"vision": MODEL}))
            assert response["status"] == "failed"
            assert response["result"] == {}
            output = json.dumps(response)
            for value in (private_prose, "private model choice reasoning", "provider_message",
                          "selected_index", "quota_exceeded"):
                assert value not in output
        finally:
            await browser.stop()

    asyncio.run(run())


def test_outer_step_timeout_is_not_a_provider_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(executor, "ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(executor.JobArtifacts, "capture_failure", AsyncMock())
    original_wait_for = asyncio.wait_for

    async def wait_for(awaitable, timeout):
        if getattr(awaitable, "cr_code", None) is executor._run_step.__code__:
            awaitable.close()
            raise asyncio.TimeoutError()
        return await original_wait_for(awaitable, timeout)

    monkeypatch.setattr(executor.asyncio, "wait_for", wait_for)
    page = SimpleNamespace(url="https://site.invalid/", on=Mock(), remove_listener=Mock(),
                           bring_to_front=AsyncMock(), close=AsyncMock())
    lane = SimpleNamespace(execution_semaphore=asyncio.Semaphore(1), task_pages={}, page=None,
                           context=SimpleNamespace(new_page=AsyncMock(return_value=page)))
    manager = SimpleNamespace(get=Mock(return_value=lane),
                              lease_lane=AsyncMock(return_value=("lease", {"browser": "fake"})),
                              release_lane=AsyncMock())
    result = asyncio.run(executor.execute_job(manager, {
        "job_id": 1, "run_id": 1, "lane_key": "general", "vision": MODEL,
        "steps": [{"step_key": "result", "feature_type": "flow.result", "timeout_seconds": 1,
                   "config": {"output_value": True, "llm_enabled": True}}],
    }))
    step = result["steps"][0]
    assert step["status"] == "fail"
    assert step["data"] == {"reason": "execution_timeout", "stage": "step_execution"}
    assert "步骤超时" in step["message"]
    assert "provider_error" not in repr(step)
