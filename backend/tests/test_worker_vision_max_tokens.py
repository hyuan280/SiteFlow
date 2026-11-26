from __future__ import annotations

import asyncio
import io
import json
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker import vision  # noqa: E402


@pytest.fixture(params=["result", "grid", "quiz", "captcha"])
def vision_path(request, monkeypatch):
    path = request.param
    config = {
        "model": "private-model",
        "base_url": "https://private-provider.invalid/v1/",
        "api_key": "private-api-key",
        "timeout": 17,
    }
    final = {
        "result": {"result": True, "reason": "Visible evidence."},
        "grid": {"is_clear": True, "selected_indices": [1], "need_refresh": False},
        "quiz": {"is_clear": True, "selected_index": 1, "need_refresh": False,
                 "reason": "Visible evidence."},
        "captcha": "Ab12",
    }[path]
    text = final if path == "captcha" else json.dumps(final)
    payload = {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}
    bodies = []
    client_options = []
    original_client = httpx.AsyncClient

    def handle(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=payload)

    def client(**kwargs):
        client_options.append(kwargs)
        return original_client(transport=httpx.MockTransport(handle), **kwargs)

    def urlopen(request, **kwargs):
        client_options.append(kwargs)
        bodies.append(json.loads(request.data))
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(vision.httpx, "AsyncClient", client)
    monkeypatch.setattr(vision, "urlopen", urlopen)

    def invoke():
        if path == "result":
            return asyncio.run(vision.judge_result(config, "Judge", page_text="Page data"))
        if path == "grid":
            return asyncio.run(vision.answer_grid_captcha(b"image", config))
        if path == "quiz":
            return vision.answer_image_quiz(b"image", "Question", ["One", "Two"], config)
        return vision.recognize(b"image", config, "Read captcha")

    expected = {"result": final, "grid": [1], "quiz": (1, "Visible evidence."),
                "captcha": "Ab12"}[path]
    return SimpleNamespace(
        path=path, config=config, payload=payload, bodies=bodies, client_options=client_options,
        invoke=invoke, expected=expected, text=text,
    )


@pytest.mark.parametrize("options", [
    pytest.param({}, id="missing"),
    pytest.param({"max_tokens": None}, id="null"),
    pytest.param({"max_tokens": 0}, id="zero"),
    pytest.param({"max_tokens": 1}, id="one"),
    pytest.param({"max_tokens": 4097}, id="custom"),
    pytest.param({"max_tokens": 131072}, id="large"),
])
def test_request_uses_only_exact_positive_configured_budget(vision_path, options):
    case = vision_path
    case.config.update(options)
    original = case.config.copy()
    assert case.invoke() == case.expected
    assert case.config == original
    assert len(case.bodies) == 1
    body = case.bodies[0]
    expected_keys = {"model", "messages"}
    if case.path != "captcha":
        expected_keys.add("temperature")
    if case.path == "grid":
        expected_keys.update(("tools", "tool_choice"))
        assert body["tool_choice"] == "auto"
    if options.get("max_tokens"):
        expected_keys.add("max_tokens")
        assert type(body["max_tokens"]) is int
        assert body["max_tokens"] == options["max_tokens"]
    assert set(body) == expected_keys


@pytest.mark.parametrize("max_tokens", [
    True, False, 0.0, 1.0, -1, -1.5, float("nan"), float("inf"),
    "2048", "0", "", "private-invalid-budget", [], {},
])
def test_invalid_budget_fails_safely_before_request(vision_path, max_tokens):
    case = vision_path
    case.config["max_tokens"] = max_tokens
    with pytest.raises(ValueError) as caught:
        case.invoke()
    assert case.bodies == case.client_options == []
    error = caught.value
    if case.path == "result":
        assert type(error) is vision.ResultJudgmentError
        assert (error.code, error.stage, error.http_status) == ("invalid_input", "input", None)
        assert error.__context__ is None
        assert not vision._result_request_active.get()
    else:
        assert type(error) is vision.ModelCallError
        assert (error.code, error.stage) == ("invalid_input", "input")
        assert error.__context__ is None
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    output = "".join(traceback.format_exception(error))
    for private in (case.config["model"], case.config["base_url"], case.config["api_key"],
                    "private-invalid-budget"):
        assert private not in output


@pytest.mark.parametrize("options", [
    pytest.param({}, id="missing"),
    pytest.param({"max_tokens": None}, id="null"),
    pytest.param({"max_tokens": 0}, id="zero"),
    pytest.param({"max_tokens": 8193}, id="positive"),
])
@pytest.mark.parametrize("finish_reason", ["stop", "length", "content_filter"])
def test_reasoning_without_final_content_remains_failure(vision_path, options, finish_reason):
    case = vision_path
    case.config.update(options)
    case.payload["choices"][0] = {
        "message": {"content": None, "reasoning_content": case.text},
        "finish_reason": finish_reason,
    }
    with pytest.raises(ValueError) as caught:
        case.invoke()
    assert len(case.bodies) == 1
    body = case.bodies[0]
    if options.get("max_tokens"):
        assert body["max_tokens"] == options["max_tokens"]
    else:
        assert "max_tokens" not in body
    if case.path == "result":
        code, stage = {
            "stop": ("no_result", "content"),
            "length": ("truncated", "provider_response"),
            "content_filter": ("content_filtered", "provider_response"),
        }[finish_reason]
        assert (caught.value.code, caught.value.stage, caught.value.http_status) == (code, stage, None)
        assert caught.value.__context__ is None
        assert not vision._result_request_active.get()
    else:
        assert type(caught.value) is vision.ModelCallError
        code, stage = {
            "stop": ("no_result", "content"),
            "length": ("truncated", "provider_response"),
            "content_filter": ("content_filtered", "provider_response"),
        }[finish_reason]
        assert (caught.value.code, caught.value.stage) == (code, stage)
        assert caught.value.__context__ is None
