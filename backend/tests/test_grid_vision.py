from __future__ import annotations

import asyncio
import base64
import json
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker import vision  # noqa: E402


API_KEY = "private-grid-api-key"
IMAGE = b"private-grid-image"
RAW_OUTPUT = "private-grid-provider-response"
GRID_ERROR = "Grid vision provider request failed or returned invalid output"
VALID_RESULT = {"is_clear": True, "selected_indices": [9, 1, 5], "need_refresh": False}


@pytest.fixture
def model_config():
    return {
        "base_url": "https://grid-provider.invalid/v1/",
        "model": "grid-model",
        "api_key": API_KEY,
        "timeout": 17,
    }


@pytest.fixture
def mock_provider(monkeypatch):
    original_client = httpx.AsyncClient

    def install(handler):
        requests = []
        options = []

        async def handle(request):
            requests.append(request)
            response = handler(request)
            if asyncio.iscoroutine(response):
                response = await response
            return response

        def client(**kwargs):
            options.append(kwargs)
            return original_client(transport=httpx.MockTransport(handle), **kwargs)

        monkeypatch.setattr(vision.httpx, "AsyncClient", client)
        return SimpleNamespace(requests=requests, options=options)

    return install


@pytest.fixture(params=["tool", "content"])
def source(request):
    return request.param


def completion(message, *, finish_reason="stop"):
    return {"choices": [{"message": message, "finish_reason": finish_reason}]}


def grid_message(text, source):
    if source == "content":
        return {"content": text}
    return {"tool_calls": [{
        "id": "grid-result",
        "type": "function",
        "function": {"name": "submit_geetest_result", "arguments": text},
    }]}


@pytest.mark.parametrize("prompt", [None, "Use the custom target-matching instruction."])
def test_request_uses_auto_grid_tool_and_selected_indices_schema(
    mock_provider, model_config, prompt,
):
    captured = mock_provider(lambda _: httpx.Response(200, json=completion(
        grid_message(json.dumps(VALID_RESULT), "tool"), finish_reason="tool_calls",
    )))

    assert asyncio.run(vision.answer_grid_captcha(IMAGE, model_config, prompt)) == [1, 5, 9]
    assert captured.options == [{"timeout": 17, "follow_redirects": False}]
    assert len(captured.requests) == 1
    request = captured.requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://grid-provider.invalid/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert request.headers["content-type"] == "application/json"
    assert request.extensions["timeout"] == dict.fromkeys(
        ("connect", "read", "write", "pool"), 17,
    )
    body = json.loads(request.content)
    assert body["model"] == "grid-model"
    assert body["temperature"] == 0.1
    assert body["max_tokens"] == 2048
    assert body["tool_choice"] == "auto"
    assert len(body["tools"]) == 1
    tool = body["tools"][0]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "submit_geetest_result"
    assert tool["function"]["parameters"] == {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "is_clear": {"type": "boolean"},
            "selected_indices": {
                "type": "array",
                "items": {"type": "integer", "minimum": 1, "maximum": 9},
                "maxItems": 9,
                "uniqueItems": True,
            },
            "need_refresh": {"type": "boolean"},
            "reason": {"type": "string", "maxLength": 500},
        },
        "required": ["is_clear", "selected_indices", "need_refresh"],
    }
    system, user = body["messages"]
    assert system["role"] == "system"
    assert "submit_geetest_result" in system["content"]
    if prompt is not None:
        assert system["content"].startswith(prompt)
    else:
        assert "1-9" in system["content"]
    assert user["role"] == "user"
    assert len(user["content"]) == 2
    assert user["content"][0]["type"] == "text"
    assert user["content"][1] == {"type": "image_url", "image_url": {
        "url": "data:image/png;base64," + base64.b64encode(IMAGE).decode("ascii"),
    }}
    assert API_KEY not in request.content.decode()


@pytest.mark.parametrize("indices", [[9], [9, 1, 5], list(range(9, 0, -1))])
def test_valid_indices_are_sorted(mock_provider, model_config, source, indices):
    result = {**VALID_RESULT, "selected_indices": indices}
    mock_provider(lambda _: httpx.Response(200, json=completion(
        grid_message(json.dumps(result), source),
    )))
    assert asyncio.run(vision.answer_grid_captcha(IMAGE, model_config)) == sorted(indices)


@pytest.mark.parametrize("content", [
    " \n" + json.dumps(VALID_RESULT) + "\t",
    "<think>Not a final selection: 2, 3.</think>" + json.dumps(VALID_RESULT),
    "<think>first</think>\n<think>second</think>" + json.dumps(VALID_RESULT),
    [{"type": "text", "text": '{"is_clear": true, "selected_indices":'},
     {"type": "text", "text": '[9, 1, 5], "need_refresh": false}'}],
])
def test_content_json_fallback(mock_provider, model_config, content):
    mock_provider(lambda _: httpx.Response(200, json=completion({
        "content": content, "reasoning_content": json.dumps({
            **VALID_RESULT, "selected_indices": [2, 3],
        }),
    })))
    assert asyncio.run(vision.answer_grid_captcha(IMAGE, model_config)) == [1, 5, 9]


@pytest.mark.parametrize("is_clear,need_refresh", [(False, False), (True, True), (False, True)])
def test_unclear_result_raises_distinct_exception(
    mock_provider, model_config, source, is_clear, need_refresh,
):
    result = {"is_clear": is_clear, "need_refresh": need_refresh, "selected_indices": []}
    mock_provider(lambda _: httpx.Response(200, json=completion(
        grid_message(json.dumps(result), source),
    )))
    with pytest.raises(vision.GridCaptchaUnclear) as error:
        asyncio.run(vision.answer_grid_captcha(IMAGE, model_config))
    assert str(error.value)
    assert str(error.value) != GRID_ERROR


@pytest.mark.parametrize("indices", [
    None, [], True, 1, "1", {}, [True], [False], [1, True], [1.0], ["1"], [None],
    [[1]], [{}], [1, 1], [0], [-1], [10], [1, 10], list(range(1, 11)),
])
def test_invalid_indices_are_rejected(mock_provider, model_config, source, indices):
    result = {**VALID_RESULT, "selected_indices": indices}
    mock_provider(lambda _: httpx.Response(200, json=completion(
        grid_message(json.dumps(result), source),
    )))
    with pytest.raises(ValueError) as error:
        asyncio.run(vision.answer_grid_captcha(IMAGE, model_config))
    assert type(error.value) is ValueError
    assert str(error.value) == GRID_ERROR


@pytest.mark.parametrize("result", [
    None, [], [VALID_RESULT], True, 1, "result", {},
    {key: value for key, value in VALID_RESULT.items() if key != "selected_indices"},
    {key: value for key, value in VALID_RESULT.items() if key != "is_clear"},
    {key: value for key, value in VALID_RESULT.items() if key != "need_refresh"},
    {**VALID_RESULT, "is_clear": 1}, {**VALID_RESULT, "is_clear": "true"},
    {**VALID_RESULT, "is_clear": None}, {**VALID_RESULT, "need_refresh": 0},
    {**VALID_RESULT, "need_refresh": "false"}, {**VALID_RESULT, "need_refresh": None},
    {"is_clear": True, "selected_index": 1, "need_refresh": False},
    pytest.param({**VALID_RESULT, "unexpected": "not in schema"}, id="extra-property"),
])
def test_invalid_result_shape_is_rejected(mock_provider, model_config, source, result):
    mock_provider(lambda _: httpx.Response(200, json=completion(
        grid_message(json.dumps(result), source),
    )))
    with pytest.raises(ValueError) as error:
        asyncio.run(vision.answer_grid_captcha(IMAGE, model_config))
    assert type(error.value) is ValueError
    assert str(error.value) == GRID_ERROR


@pytest.mark.parametrize("tools", [
    [grid_message(json.dumps(VALID_RESULT), "tool")["tool_calls"][0]] * 2,
    [{"type": "function", "function": {
        "name": "other_tool", "arguments": json.dumps(VALID_RESULT),
    }}],
    [{"type": "other", "function": {
        "name": "submit_geetest_result", "arguments": json.dumps(VALID_RESULT),
    }}],
    [{}], [None], [{"type": "function"}],
    [{"type": "function", "function": {"name": "submit_geetest_result"}}],
    [{"type": "function", "function": {
        "name": "submit_geetest_result", "arguments": VALID_RESULT,
    }}],
    "not-a-tool-list", {"function": "not-a-tool-list"},
])
def test_invalid_tool_calls_do_not_fall_back_to_valid_content(
    mock_provider, model_config, tools,
):
    mock_provider(lambda _: httpx.Response(200, json=completion({
        "tool_calls": tools, "content": json.dumps(VALID_RESULT),
    })))
    with pytest.raises(ValueError, match=f"^{GRID_ERROR}$"):
        asyncio.run(vision.answer_grid_captcha(IMAGE, model_config))


def test_tool_arguments_take_precedence_over_content(mock_provider, model_config):
    message = grid_message(json.dumps(VALID_RESULT), "tool")
    message["content"] = json.dumps({**VALID_RESULT, "selected_indices": [2]})
    mock_provider(lambda _: httpx.Response(200, json=completion(message)))
    assert asyncio.run(vision.answer_grid_captcha(IMAGE, model_config)) == [1, 5, 9]


@pytest.mark.parametrize("finish_reason", ["length", "content_filter"])
def test_truncated_or_filtered_result_is_rejected(
    mock_provider, model_config, source, finish_reason,
):
    mock_provider(lambda _: httpx.Response(200, json=completion(
        grid_message(json.dumps(VALID_RESULT), source), finish_reason=finish_reason,
    )))
    with pytest.raises(ValueError, match=f"^{GRID_ERROR}$"):
        asyncio.run(vision.answer_grid_captcha(IMAGE, model_config))


@pytest.mark.parametrize("text", [
    "", " \n", "<think>" + json.dumps(VALID_RESULT) + "</think>",
    "<think>" + json.dumps(VALID_RESULT),
    "<think>outer<think>inner</think></think>" + json.dumps(VALID_RESULT),
    "</think>" + json.dumps(VALID_RESULT),
    json.dumps(VALID_RESULT) + "<think>trailing</think>",
    "The selected cells are: " + json.dumps(VALID_RESULT),
    json.dumps(VALID_RESULT) + " These match the target.",
    "```json\n" + json.dumps(VALID_RESULT) + "\n```",
    json.dumps(VALID_RESULT) + json.dumps(VALID_RESULT),
    '{"is_clear": true, "selected_indices": [1], "need_refresh": false,}',
    "<think>" + "x" * 32000 + "</think>" + json.dumps(VALID_RESULT),
])
def test_parser_requires_strict_json_not_prose_or_reasoning_only(
    mock_provider, model_config, source, text,
):
    mock_provider(lambda _: httpx.Response(200, json=completion(grid_message(text, source))))
    with pytest.raises(ValueError, match=f"^{GRID_ERROR}$"):
        asyncio.run(vision.answer_grid_captcha(IMAGE, model_config))


@pytest.mark.parametrize("message", [
    {}, {"reasoning_content": json.dumps(VALID_RESULT)},
    {"content": None, "reasoning_content": json.dumps(VALID_RESULT)},
    {"content": True}, {"content": 1}, {"content": VALID_RESULT}, {"content": []},
    {"content": [json.dumps(VALID_RESULT)]},
    {"content": [{"type": "image_url", "text": json.dumps(VALID_RESULT)}]},
    {"content": [{"type": "text", "text": VALID_RESULT}]},
    {"content": [{"type": "text", "text": json.dumps(VALID_RESULT)}, None]},
])
def test_reasoning_field_and_invalid_content_are_not_answers(mock_provider, model_config, message):
    mock_provider(lambda _: httpx.Response(200, json=completion(message)))
    with pytest.raises(ValueError, match=f"^{GRID_ERROR}$"):
        asyncio.run(vision.answer_grid_captcha(IMAGE, model_config))


@pytest.mark.parametrize("payload", [
    None, [], {}, {"choices": []}, {"choices": None}, {"choices": [None]},
    {"choices": [{}]}, {"choices": [{"message": None}]},
    {"choices": [{"message": []}]},
])
def test_invalid_response_envelope_is_sanitized(mock_provider, model_config, payload):
    mock_provider(lambda _: httpx.Response(200, content=json.dumps(payload)))
    with pytest.raises(ValueError, match=f"^{GRID_ERROR}$"):
        asyncio.run(vision.answer_grid_captcha(IMAGE, model_config))


@pytest.mark.parametrize("failure", ["http", "network", "timeout", "json", "output"])
def test_provider_errors_are_sanitized(mock_provider, model_config, failure, caplog, capsys):
    private = f"{API_KEY} {IMAGE.decode()} {RAW_OUTPUT} {model_config['base_url']}"

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
        asyncio.run(vision.answer_grid_captcha(IMAGE, model_config))
    assert type(error.value) is ValueError
    assert str(error.value) == GRID_ERROR
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True
    assert error.value.reason == {"http": "http_error", "network": "network_error",
                                  "timeout": "timeout", "json": "invalid_response",
                                  "output": "invalid_response"}[failure]
    if failure == "http":
        assert error.value.status_code == 401
    assert len(captured.requests) == 1
    output = "".join(traceback.format_exception(error.value))
    output += caplog.text + repr(capsys.readouterr())
    for secret in (API_KEY, IMAGE.decode(), RAW_OUTPUT, model_config["base_url"]):
        assert secret not in output


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("destination", [
    "https://grid-provider.invalid/redirected", "https://untrusted.invalid/collect",
])
def test_redirects_are_not_followed_or_exposed(
    mock_provider, model_config, status, destination,
):
    def handle(request):
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(status, headers={
                "Location": f"{destination}?private={API_KEY}",
            }, text=RAW_OUTPUT)
        return httpx.Response(200, json=completion({"content": json.dumps(VALID_RESULT)}))

    captured = mock_provider(handle)
    with pytest.raises(ValueError) as error:
        asyncio.run(vision.answer_grid_captcha(IMAGE, model_config))
    assert str(error.value) == GRID_ERROR
    assert len(captured.requests) == 1
    assert captured.options[0]["follow_redirects"] is False
    assert str(captured.requests[0].url) == "https://grid-provider.invalid/v1/chat/completions"
    output = "".join(traceback.format_exception(error.value))
    for secret in (API_KEY, IMAGE.decode(), RAW_OUTPUT, destination):
        assert secret not in output


def test_cancellation_propagates_from_in_flight_request(mock_provider, model_config):
    async def scenario():
        started = asyncio.Event()

        async def handle(_request):
            started.set()
            await asyncio.Future()

        captured = mock_provider(handle)
        task = asyncio.create_task(vision.answer_grid_captcha(IMAGE, model_config))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert task.cancelled()
        assert len(captured.requests) == 1

    asyncio.run(scenario())
