from __future__ import annotations

import asyncio
import base64
import json
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker import executor, vision  # noqa: E402


API_KEY = "private-provider-key"
PAGE_TEXT = "private-page-content"
RAW_OUTPUT = "private-provider-response"
JUDGMENT_ERROR = "Model judgement failed or returned no valid True/False conclusion"


@pytest.fixture
def model_config():
    return {
        "base_url": "https://provider.invalid/v1/",
        "model": "default-model",
        "api_key": API_KEY,
        "timeout": 17,
    }


@pytest.fixture
def mock_provider(monkeypatch):
    original_client = httpx.AsyncClient

    def install(handler):
        requests = []
        options = []

        def handle(request):
            requests.append(request)
            return handler(request)

        def client(**kwargs):
            options.append(kwargs)
            return original_client(transport=httpx.MockTransport(handle), **kwargs)

        monkeypatch.setattr(vision.httpx, "AsyncClient", client)
        return SimpleNamespace(requests=requests, options=options)

    return install


@pytest.fixture
def result_context(monkeypatch):
    judge = AsyncMock(return_value=True)
    monkeypatch.setattr(vision, "judge_result", judge)
    return SimpleNamespace(
        judge=judge,
        page=SimpleNamespace(
            screenshot=AsyncMock(return_value=b"private-screen-image"),
            evaluate=AsyncMock(return_value=PAGE_TEXT),
            content=AsyncMock(),
            inner_text=AsyncMock(),
        ),
        logs=[],
    )


def run_result(context, config, settings, *, value=True, message="original message",
               step_enabled=True, step_prompt=""):
    step_config = {"output_value": value, "message": message, "llm_prompt": step_prompt}
    if step_enabled is not None:
        step_config["llm_enabled"] = step_enabled
    return asyncio.run(executor._run_step(
        context.page,
        {"feature_type": "flow.result", "config": step_config},
        None, None, None, None, None, "general",
        log=lambda level, text: context.logs.append((level, text)),
        vision={**config, "execution_settings": settings},
    ))


def completion(message, *, finish_reason="stop"):
    return {"choices": [{"message": message, "finish_reason": finish_reason}]}


@pytest.mark.parametrize("value", [True, False])
@pytest.mark.parametrize("settings", [{}, {
    "llm_enabled": False, "judgment_scope": "all", "use_vision": True,
    "model": "ignored-model", "llm_prompt": "ignored prompt",
}])
def test_disabled_judgment_preserves_original_result(result_context, value, settings):
    result = run_result(result_context, {}, settings, value=value)
    assert result == ("success", "original message", {
        "value": value, "message": "original message",
    })
    result_context.judge.assert_not_awaited()
    result_context.page.screenshot.assert_not_awaited()
    result_context.page.evaluate.assert_not_awaited()
    assert result_context.logs == []


@pytest.mark.parametrize("step_enabled", [None, False])
def test_step_opt_in_required_even_with_global_enabled(result_context, step_enabled):
    result = run_result(result_context, {}, {"llm_enabled": True, "judgment_scope": "all"},
                        step_enabled=step_enabled)
    assert result == ("success", "original message", {"value": True, "message": "original message"})
    result_context.judge.assert_not_awaited()
    result_context.page.screenshot.assert_not_awaited()


@pytest.mark.parametrize("prompt,expected", [
    (" Site-specific prompt ", "Site-specific prompt"), ("", "Global prompt"), ("  ", "Global prompt"),
])
def test_step_prompt_overrides_or_falls_back_to_global(result_context, model_config, prompt, expected):
    result = run_result(result_context, model_config,
                        {"llm_enabled": True, "llm_prompt": "Global prompt"}, step_prompt=prompt)
    assert result[0] == "success"
    assert result_context.judge.call_args.args[1] == expected


@pytest.mark.parametrize("scope", [None, "success_only"])
def test_success_only_skips_original_false_even_without_model(result_context, scope):
    settings = {"llm_enabled": True}
    if scope is not None:
        settings["judgment_scope"] = scope
    assert run_result(result_context, {}, settings, value=False) == (
        "success", "original message", {"value": False, "message": "original message"},
    )
    result_context.judge.assert_not_awaited()
    result_context.page.screenshot.assert_not_awaited()
    result_context.page.evaluate.assert_not_awaited()


@pytest.mark.parametrize("value,scope", [
    (True, "success_only"), (True, "all"), (False, "all"),
])
@pytest.mark.parametrize("judged", [True, False])
def test_judgment_replaces_value_without_turning_false_into_step_failure(
    result_context, model_config, value, scope, judged,
):
    result_context.judge.return_value = judged
    status, message, data = run_result(result_context, model_config, {
        "llm_enabled": True, "judgment_scope": scope,
    }, value=value)
    assert status == "success"
    assert data == {
        "value": judged, "message": message, "original_value": value, "judged_by": "llm",
    }
    assert message == ("original message" if judged == value else f"大模型判断结果: {str(judged).lower()}")
    assert ("INFO", f"大模型判断结果: {str(judged).lower()}") in result_context.logs
    result_context.judge.assert_awaited_once_with(
        {**model_config, "execution_settings": {
            "llm_enabled": True, "judgment_scope": scope,
        }},
        vision.DEFAULT_RESULT_PROMPT,
        image=b"private-screen-image",
    )
    result_context.page.screenshot.assert_awaited_once_with(
        type="png", timeout=executor.PAGE_TIMEOUT_MS,
    )
    result_context.page.evaluate.assert_not_awaited()
    result_context.page.content.assert_not_awaited()
    result_context.page.inner_text.assert_not_awaited()


@pytest.mark.parametrize("value", [True, False])
@pytest.mark.parametrize("configured_message", ["", "  ", "  Configured result  "])
def test_matching_judgment_uses_configured_message_or_normal_default(
    result_context, model_config, value, configured_message,
):
    result_context.judge.return_value = value
    status, message, data = run_result(result_context, model_config, {
        "llm_enabled": True, "judgment_scope": "all",
    }, value=value, message=configured_message)
    assert status == "success"
    assert message == (configured_message.strip() or f"输出结果: {str(value).lower()}")
    assert data["message"] == message
    assert data["value"] is value
    assert data["judged_by"] == "llm"


def test_text_judgment_uses_bounded_inner_text_and_model_prompt_overrides(
    result_context, model_config,
):
    model_config["supports_vision"] = False
    settings = {
        "llm_enabled": True, "use_vision": False, "judgment_scope": "all",
        "model": "text-only-override", "llm_prompt": "custom judgment instruction",
    }
    text = "x" * 24000
    result_context.page.evaluate.return_value = text
    status, _, data = run_result(result_context, model_config, settings, value=False)
    assert status == "success"
    assert data["value"] is True
    result_context.page.evaluate.assert_awaited_once()
    script = result_context.page.evaluate.await_args.args[0]
    assert "document.body.innerText.slice(0, 24000)" in script
    assert "document.body ?" in script
    assert "outerHTML" not in script
    assert ".value" not in script
    result_context.page.screenshot.assert_not_awaited()
    result_context.page.content.assert_not_awaited()
    result_context.page.inner_text.assert_not_awaited()
    result_context.judge.assert_awaited_once_with(
        {**model_config, "model": "text-only-override", "execution_settings": settings},
        "custom judgment instruction", page_text=text,
    )
    assert model_config["model"] == "default-model"
    assert PAGE_TEXT not in repr(result_context.logs)
    assert text not in repr(result_context.logs)


@pytest.mark.parametrize("missing", ["base_url", "model", "api_key"])
def test_missing_provider_is_explicit_failure_without_boolean(
    result_context, model_config, missing,
):
    model_config.pop(missing)
    status, message, data = run_result(result_context, model_config, {"llm_enabled": True})
    assert status == "fail"
    assert message
    assert data == {"reason": "model_unavailable"}
    result_context.judge.assert_not_awaited()
    result_context.page.screenshot.assert_not_awaited()
    result_context.page.evaluate.assert_not_awaited()
    assert API_KEY not in repr((message, data, result_context.logs))


def test_unsupported_vision_fails_before_collecting_evidence(result_context, model_config):
    model_config["supports_vision"] = False
    status, message, data = run_result(result_context, model_config, {
        "llm_enabled": True, "use_vision": True,
    })
    assert status == "fail"
    assert message
    assert data == {"reason": "vision_not_supported"}
    result_context.judge.assert_not_awaited()
    result_context.page.screenshot.assert_not_awaited()
    result_context.page.evaluate.assert_not_awaited()


@pytest.mark.parametrize("invalid", [None, "True", "False", "", 0, 1, {}, []])
def test_helper_requires_actual_boolean_from_judge(result_context, model_config, invalid):
    result_context.judge.return_value = invalid
    status, message, data = run_result(result_context, model_config, {"llm_enabled": True})
    assert status == "fail"
    assert message
    assert data == {"reason": "model_judgment_failed"}
    result_context.judge.assert_awaited_once()


@pytest.mark.parametrize("text", [None, "", " \n\t", 123, {}])
def test_unreadable_text_fails_without_calling_provider(result_context, model_config, text):
    result_context.page.evaluate.return_value = text
    status, _, data = run_result(result_context, model_config, {
        "llm_enabled": True, "use_vision": False,
    })
    assert status == "fail"
    assert data == {"reason": "model_judgment_failed"}
    result_context.judge.assert_not_awaited()
    result_context.page.screenshot.assert_not_awaited()


@pytest.mark.parametrize("source", ["judge", "screenshot", "evaluate"])
def test_helper_sanitizes_provider_and_browser_errors(
    result_context, model_config, source, caplog, capsys,
):
    target = result_context.judge if source == "judge" else getattr(result_context.page, source)
    target.side_effect = RuntimeError(f"{API_KEY} {PAGE_TEXT} {RAW_OUTPUT}")
    result = run_result(result_context, model_config, {
        "llm_enabled": True, "use_vision": source != "evaluate",
    })
    assert result[0] == "fail"
    assert result[2] == {"reason": "model_judgment_failed"}
    output = repr((result, result_context.logs, capsys.readouterr())) + caplog.text
    for secret in (API_KEY, PAGE_TEXT, RAW_OUTPUT):
        assert secret not in output
    if source != "judge":
        result_context.judge.assert_not_awaited()


@pytest.mark.parametrize("source", ["judge", "screenshot", "evaluate"])
def test_helper_propagates_cancellation(result_context, model_config, source):
    target = result_context.judge if source == "judge" else getattr(result_context.page, source)
    target.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        run_result(result_context, model_config, {
            "llm_enabled": True, "use_vision": source != "evaluate",
        })


@pytest.mark.parametrize("use_image", [True, False])
def test_async_provider_request_contains_only_selected_evidence(
    mock_provider, model_config, use_image,
):
    captured = mock_provider(lambda _: httpx.Response(
        200, json=completion({"content": "True"}),
    ))
    evidence = {"image": b"private-screen-image"} if use_image else {"page_text": PAGE_TEXT}
    assert asyncio.run(vision.judge_result(model_config, "system instruction", **evidence)) is True
    assert captured.options == [{"timeout": 17, "follow_redirects": False}]
    assert len(captured.requests) == 1
    request = captured.requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://provider.invalid/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    body = json.loads(request.content)
    assert body["model"] == "default-model"
    assert body["temperature"] == 0
    assert body["max_tokens"] == 1024
    system, user = body["messages"]
    assert system["role"] == "system"
    assert system["content"].startswith("system instruction")
    assert "True" in system["content"] and "False" in system["content"]
    assert user["role"] == "user"
    assert user["content"][0]["type"] == "text"
    assert PAGE_TEXT not in system["content"]
    if use_image:
        assert len(user["content"]) == 2
        assert user["content"][1] == {"type": "image_url", "image_url": {
            "url": "data:image/png;base64," + base64.b64encode(b"private-screen-image").decode(),
        }}
        assert PAGE_TEXT not in request.content.decode()
    else:
        assert len(user["content"]) == 1
        assert PAGE_TEXT in user["content"][0]["text"]
        assert "image_url" not in request.content.decode()
        assert "base64" not in request.content.decode()
    assert API_KEY not in request.content.decode()


@pytest.mark.parametrize("content,expected", [
    ("True", True), ("False", False), (" \nTrUe\t", True), (" false ", False),
    ("<think>False is only reasoning</think>True", True),
    ("<think>True is only reasoning</think> False", False),
    (" <think>first</think>\n<think>second</think>\nTRUE ", True),
    ([{"type": "text", "text": "Fa"}, {"type": "text", "text": "lse"}], False),
    ([{"type": "text", "text": "<think>analysis</think>"},
      {"type": "text", "text": "True"}], True),
])
def test_parser_accepts_only_explicit_boolean_after_valid_leading_think_blocks(
    mock_provider, model_config, content, expected,
):
    captured = mock_provider(lambda _: httpx.Response(200, json=completion({
        "content": content, "reasoning_content": str(not expected),
    })))
    assert asyncio.run(vision.judge_result(model_config, "judge", page_text=PAGE_TEXT)) is expected
    assert len(captured.requests) == 1


@pytest.mark.parametrize("message", [
    {}, {"reasoning_content": "True"}, {"content": None, "reasoning_content": "False"},
    {"content": "<think>True</think>"}, {"content": "<think>False"},
    {"content": "<think>outer<think>inner</think></think>True"},
    {"content": "True<think>reason</think>"}, {"content": "</think>True"},
    {"content": "<think>reason</think>True</think>"},
    {"content": "<think>reason</think>True False"},
    {"content": '{"value": true}'}, {"content": '"True"'},
    {"content": "The result is True"}, {"content": "True, signed in"},
    {"content": "True."}, {"content": "True\nFalse"}, {"content": "```\nTrue\n```"},
    {"content": "1"}, {"content": True}, {"content": 0}, {"content": {}},
    {"content": ""}, {"content": " \n"}, {"content": []},
    {"content": [{"type": "image_url", "text": "True"}]},
    {"content": [{"type": "text", "text": True}]}, {"content": ["True"]},
    {"content": "<think>" + "x" * 32000 + "</think>True"},
])
def test_parser_rejects_reasoning_only_json_prose_and_malformed_content(
    mock_provider, model_config, message,
):
    mock_provider(lambda _: httpx.Response(200, json=completion(message)))
    with pytest.raises(ValueError, match=f"^{JUDGMENT_ERROR}$"):
        asyncio.run(vision.judge_result(model_config, "judge", page_text=PAGE_TEXT))


@pytest.mark.parametrize("finish_reason", ["length", "content_filter"])
@pytest.mark.parametrize("content", ["True", "False", "<think>analysis</think>True"])
def test_parser_rejects_truncated_or_filtered_boolean(
    mock_provider, model_config, finish_reason, content,
):
    mock_provider(lambda _: httpx.Response(200, json=completion(
        {"content": content}, finish_reason=finish_reason,
    )))
    with pytest.raises(ValueError, match=f"^{JUDGMENT_ERROR}$"):
        asyncio.run(vision.judge_result(model_config, "judge", page_text=PAGE_TEXT))


@pytest.mark.parametrize("payload", [None, [], {}, {"choices": []}, {
    "choices": [{"message": None}],
}])
def test_parser_rejects_invalid_response_envelopes(mock_provider, model_config, payload):
    mock_provider(lambda _: httpx.Response(200, content=json.dumps(payload)))
    with pytest.raises(ValueError, match=f"^{JUDGMENT_ERROR}$"):
        asyncio.run(vision.judge_result(model_config, "judge", page_text=PAGE_TEXT))


@pytest.mark.parametrize("failure", ["http", "network", "timeout", "json", "output"])
def test_provider_errors_are_sanitized(mock_provider, model_config, failure, caplog, capsys):
    private = f"{API_KEY} {PAGE_TEXT} {RAW_OUTPUT}"

    def handle(request):
        if failure == "network":
            raise httpx.ConnectError(private, request=request)
        if failure == "timeout":
            raise httpx.ReadTimeout(private, request=request)
        if failure == "http":
            return httpx.Response(401, text=private)
        if failure == "json":
            return httpx.Response(200, text=private)
        return httpx.Response(200, json=completion({"content": private}))

    captured = mock_provider(handle)
    with pytest.raises(ValueError) as error:
        asyncio.run(vision.judge_result(model_config, "judge", page_text=PAGE_TEXT))
    assert str(error.value) == JUDGMENT_ERROR
    assert len(captured.requests) == 1
    output = "".join(traceback.format_exception(error.value))
    output += caplog.text + repr(capsys.readouterr())
    for secret in (API_KEY, PAGE_TEXT, RAW_OUTPUT):
        assert secret not in output


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("destination", [
    "https://provider.invalid/redirected", "https://untrusted.invalid/collect",
])
def test_provider_redirect_is_rejected_without_forwarding_credentials_or_content(
    mock_provider, model_config, status, destination,
):
    def handle(request):
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(status, headers={
                "Location": f"{destination}?private={API_KEY}",
            }, text=RAW_OUTPUT)
        return httpx.Response(200, json=completion({"content": "True"}))

    captured = mock_provider(handle)
    with pytest.raises(ValueError) as error:
        asyncio.run(vision.judge_result(model_config, "judge", page_text=PAGE_TEXT))
    assert str(error.value) == JUDGMENT_ERROR
    assert len(captured.requests) == 1
    assert captured.options[0]["follow_redirects"] is False
    assert str(captured.requests[0].url) == "https://provider.invalid/v1/chat/completions"
    output = "".join(traceback.format_exception(error.value))
    for secret in (API_KEY, PAGE_TEXT, RAW_OUTPUT, destination):
        assert secret not in output


@pytest.mark.parametrize("evidence", [{}, {"image": b"image", "page_text": PAGE_TEXT}])
def test_provider_requires_exactly_one_evidence_type(mock_provider, model_config, evidence):
    captured = mock_provider(lambda _: httpx.Response(200, json=completion({"content": "True"})))
    with pytest.raises(ValueError, match=f"^{JUDGMENT_ERROR}$"):
        asyncio.run(vision.judge_result(model_config, "judge", **evidence))
    assert captured.requests == []
    assert captured.options == []


def test_async_provider_propagates_cancellation(mock_provider, model_config):
    def handle(_request):
        raise asyncio.CancelledError()

    captured = mock_provider(handle)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(vision.judge_result(model_config, "judge", page_text=PAGE_TEXT))
    assert len(captured.requests) == 1


@pytest.mark.parametrize("on_error", ["stop", "continue"])
def test_execute_job_negative_judgment_is_terminal_failure(
    monkeypatch, tmp_path, mock_provider, model_config, on_error,
):
    monkeypatch.setattr(executor, "ARTIFACT_ROOT", tmp_path)
    capture_failure = AsyncMock()
    monkeypatch.setattr(executor.JobArtifacts, "capture_failure", capture_failure)
    captured = mock_provider(lambda _: httpx.Response(
        200, json=completion({"content": "<think>check completed</think>False"}),
    ))
    page = SimpleNamespace(
        url="https://site.invalid/", on=Mock(), remove_listener=Mock(),
        bring_to_front=AsyncMock(), close=AsyncMock(),
        evaluate=AsyncMock(return_value=PAGE_TEXT), screenshot=AsyncMock(),
    )
    lane = SimpleNamespace(
        execution_semaphore=asyncio.Semaphore(1), task_pages={}, page=None,
        context=SimpleNamespace(new_page=AsyncMock(return_value=page)),
    )
    manager = SimpleNamespace(
        get=Mock(return_value=lane),
        lease_lane=AsyncMock(return_value=("safe-test-lease", {"browser": "fake"})),
        release_lane=AsyncMock(),
    )
    progress = AsyncMock()
    result = asyncio.run(executor.execute_job(manager, {
        "job_id": 1, "run_id": 1, "lane_key": "general", "vision": model_config,
        "feature_execution_settings": {"flow.result": {
            "llm_enabled": True, "use_vision": False, "judgment_scope": "success_only",
        }},
        "steps": [
            {"step_key": "result", "feature_type": "flow.result", "on_error": on_error,
             "config": {"output_value": True, "message": "original message", "llm_enabled": True}},
            {"step_key": "must-not-run", "feature_type": "flow.result",
             "config": {"output_value": True}},
        ],
    }, on_step=progress))
    assert result["status"] == "fail"
    assert len(result["steps"]) == 1
    step = result["steps"][0]
    assert step["step_key"] == "result"
    assert step["status"] == "success"
    assert step["data"]["value"] is False
    assert step["data"]["original_value"] is True
    assert step["data"]["judged_by"] == "llm"
    assert result["outputs"] == [{
        "step_key": "result", "value": False, "message": step["message"],
    }]
    assert result["message"] == step["message"]
    assert progress.await_args.args[0]["status"] == "fail"
    assert len(captured.requests) == 1
    page.evaluate.assert_awaited_once()
    page.screenshot.assert_not_awaited()
    page.close.assert_awaited_once()
    assert lane.task_pages == {}
    manager.release_lane.assert_awaited_once_with("general", "safe-test-lease")
    log = (tmp_path / "run-1" / "job-1" / "job.log").read_text()
    for secret in (API_KEY, PAGE_TEXT, RAW_OUTPUT):
        assert secret not in repr(result) + log
