from __future__ import annotations

import asyncio
from copy import deepcopy
import json

import httpx
import pytest

from app.services import assistant_context as context
from app.services.assistant_tokens import estimate_context_tokens, estimate_tokens


KEY = "private-provider-key-not-for-context"
CONFIG = {"base_url": "https://provider.example/v1", "model": "same-model",
          "api_key": KEY, "timeout": 30}


def test_token_estimate_unicode_json_tools_and_image_transport_are_separate():
    assert estimate_tokens("\u6d4b" * 100) > estimate_tokens("x" * 100)
    assert estimate_tokens(context._json({"text": "\u6d4b" * 100})) == estimate_tokens(
        json.dumps({"text": "\u6d4b" * 100}, ensure_ascii=False, separators=(",", ":")))
    messages = [{"role": "system", "content": "Safety policy"}]
    assert estimate_context_tokens(messages, [{"name": "tool", "schema": "x" * 1000}]) > estimate_context_tokens(messages)
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 100000}}
    assert estimate_tokens(image) == 97
    assert estimate_context_tokens([{"role": "user", "content": [image]}]) > 85


def response(content=None, **fields):
    return {"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": context._json({"summary": "Confirmed goal; question open."})
        if content is None else content, **fields,
    }}]}


@pytest.fixture
def provider(monkeypatch):
    original = httpx.AsyncClient
    requests, options, replies = [], [], []

    async def handler(request):
        requests.append(request)
        assert replies, "unexpected retry"
        reply = replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            return await reply(request)
        return reply if isinstance(reply, httpx.Response) else httpx.Response(200, json=reply)

    def factory(**kwargs):
        options.append(kwargs)
        return original(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(context.httpx, "AsyncClient", factory)

    def run(reply, messages=None, config=None):
        replies[:] = [reply]
        return asyncio.run(context.summarize_context(
            CONFIG if config is None else config,
            [{"role": "user", "content": "Goal"}] if messages is None else messages,
        ))

    return run, requests, options


@pytest.mark.parametrize("tokens", [None, 0, 12345])
def test_summary_uses_same_provider_and_token_policy(provider, tokens):
    run, requests, options = provider
    assert run(response(), config={**CONFIG, "max_tokens": tokens}) == "Confirmed goal; question open."
    request = requests[0]
    payload = json.loads(request.content)
    assert str(request.url) == CONFIG["base_url"] + "/chat/completions"
    assert request.headers["authorization"] == "Bearer " + KEY
    assert request.headers["accept-encoding"] == "identity"
    assert payload["model"] == CONFIG["model"] and payload["stream"] is False
    if tokens:
        assert payload["max_tokens"] == tokens
    else:
        assert "max_tokens" not in payload
    assert "tools" not in payload and "tool_choice" not in payload
    assert "SiteFlow context compaction" in payload["messages"][0]["content"]
    assert "untrusted" in payload["messages"][0]["content"]
    assert options[0]["trust_env"] is False and options[0]["follow_redirects"] is False
    assert options[0]["timeout"].read == 12


@pytest.mark.parametrize("body", [
    b"not json", b"\xff", b'{"choices":[],"choices":[]}', b'{"x":NaN}',
    b'{"x":Infinity}', b'{"x":1e999}', b"[" * 1100,
])
def test_strict_envelope_json(provider, body):
    assert provider[0](httpx.Response(200, content=body)) is None


@pytest.mark.parametrize("content", [
    "not JSON", "[]", "null", '{"summary":"a","summary":"b"}',
    '{"summary":"a","extra":true}', '{"summary":NaN}', '{"summary":1e999}',
    '{"summary":42}', '{"summary":" "}', '{"summary":""}',
    json.dumps({"summary": "x" * 6001}), json.dumps({"summary": "\u4e00" * 2000}),
])
def test_strict_summary_json_and_limits(provider, content):
    assert provider[0](response(content)) is None


@pytest.mark.parametrize("fields", [
    {"role": "user"}, {"content": []}, {"refusal": "no"}, {"tool_calls": []},
    {"tool_calls": [{}]}, {"function_call": {}}, {"reasoning_content": "private reasoning"},
    {"reasoning": "private reasoning"}, {"reasoning_details": [{"text": "private reasoning"}]},
])
def test_no_tools_refusal_or_reasoning(provider, fields):
    output = response()
    output["choices"][0]["message"].update(fields)
    assert provider[0](output) is None


@pytest.mark.parametrize("output", [
    [], {}, {"choices": []}, {"choices": [None]}, {"choices": [{}, {}]},
    {"choices": [{"finish_reason": "length", "message": {}}]},
    {"choices": [{"finish_reason": "tool_calls", "message": {}}]},
    {"choices": [{"finish_reason": "stop", "message": None}]},
])
def test_malformed_protocol(provider, output):
    assert provider[0](output) is None


def test_secret_checks_all_inputs_and_provider_fields(provider, caplog):
    run, requests, _ = provider
    assert run(response(), [{"role": "user", "content": KEY}] + [
        {"role": "user", "content": "later"} for _ in range(100)
    ]) is None
    assert not requests
    assert run(response(), config={**CONFIG, "model": KEY}) is None
    assert not requests
    output = response()
    output["unused"] = {"reasoning": KEY}
    assert run(output) is None
    escaped = '{"summary":"' + "".join(f"\\u{ord(c):04x}" for c in KEY) + '"}'
    assert run(response(escaped)) is None
    assert KEY not in caplog.text


def test_unrelated_config_fields_are_not_exported_and_oversized_request_is_not_sent(provider):
    config = {**CONFIG, "credentials": {"password": "private-configuration-value"}}
    assert provider[0](response(), config=config)
    assert "private-configuration-value" not in provider[1][0].content.decode()
    count = len(provider[1])
    assert provider[0](response(), config={**CONFIG, "model": "x" * 65536}) is None
    assert len(provider[1]) == count


def test_input_is_sampled_as_valid_structured_units(provider):
    messages = [{"role": "user", "content": "\u4e00\\\"" * 10000,
                 "reasoning_content": "never send reasoning"} for _ in range(100)]
    original = deepcopy(messages)
    assert provider[0](response(), messages)
    request = provider[1][0]
    assert len(request.content) <= 64 * 1024
    payload = json.loads(request.content)
    projection = json.loads(payload["messages"][1]["content"])
    assert projection["omitted_records"] > 0
    assert "omitted_chars" in payload["messages"][1]["content"]
    assert "never send reasoning" not in request.content.decode()
    assert messages == original


def test_structured_tool_evidence_and_early_goal_survive_sampling(provider):
    messages = [{"role": "user", "content": "earliest requested goal"}, *[
        {"role": "assistant", "content": "intermediate" * 1000} for _ in range(100)
    ], {"role": "tool", "content": context._json({
        "operation_id": "execution-123", "status": "unknown", "code": "outcome_uncertain",
        "snapshot_id": "snapshot-123", "nodes": ["evidence" * 10000],
    })}]
    assert provider[0](response(), messages)
    projection = json.loads(json.loads(provider[1][0].content)["messages"][1]["content"])
    records = projection["untrusted_history"]
    assert records[0]["content"] == "earliest requested goal"
    assert records[-1]["content"]["operation_id"] == "execution-123"
    assert records[-1]["content"]["status"] == "unknown"
    assert records[-1]["content"]["snapshot_id"] == "snapshot-123"


class GuardedStream(httpx.AsyncByteStream):
    def __init__(self, chunks, forbidden=False):
        self.chunks = chunks
        self.forbidden = forbidden
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        assert not self.forbidden, "error or encoded body must not be read"
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("status,headers", [(500, {}), (302, {"location": "https://other.example"}),
                                          (200, {"content-encoding": "gzip"})])
def test_error_bodies_not_read_and_redirects_not_followed(provider, status, headers):
    stream = GuardedStream([], forbidden=True)
    assert provider[0](httpx.Response(status, headers=headers, stream=stream)) is None
    assert stream.closed and len(provider[1]) == 1


def test_stream_response_byte_limit(provider):
    stream = GuardedStream([b" " * (32 * 1024), b"x", b"must not read"])
    assert provider[0](httpx.Response(200, stream=stream)) is None
    assert stream.reads == 2 and stream.closed


def test_maximum_valid_summary_and_json_size(provider):
    summary = "x" * 6000
    assert provider[0](response(context._json({"summary": summary}))) == summary
    assert provider[0](response(context._json({"summary": "\u4e00" * 1998}))) is None


@pytest.mark.parametrize("error", [httpx.ConnectError("private details"),
                                   httpx.ReadTimeout("private details"), TimeoutError()])
def test_expected_network_failures_are_none_no_retry(provider, caplog, error):
    assert provider[0](error) is None
    assert len(provider[1]) == 1 and "private details" not in caplog.text


def test_total_timeout_and_cancellation(provider, monkeypatch):
    original_timeout = asyncio.timeout
    durations = []

    def short_timeout(seconds):
        durations.append(seconds)
        return original_timeout(0.01)

    monkeypatch.setattr(context.asyncio, "timeout", short_timeout)

    async def stall(request):
        await asyncio.sleep(1)

    assert provider[0](stall) is None
    assert durations == [12]
    with pytest.raises(asyncio.CancelledError):
        provider[0](asyncio.CancelledError())


def test_total_timeout_covers_stream_reads(provider, monkeypatch):
    original_timeout = asyncio.timeout
    monkeypatch.setattr(context.asyncio, "timeout", lambda _: original_timeout(0.01))

    class SlowStream(GuardedStream):
        async def __aiter__(self):
            for _ in range(100):
                yield b" "
                await asyncio.sleep(0.005)

    stream = SlowStream([])
    assert provider[0](httpx.Response(200, stream=stream)) is None
    assert stream.closed


def test_external_cancellation_during_stream_propagates_and_closes(provider):
    class CancelingStream(GuardedStream):
        async def __aiter__(self):
            yield b" "
            asyncio.current_task().cancel()
            await asyncio.sleep(0)

    stream = CancelingStream([])
    with pytest.raises(asyncio.CancelledError):
        provider[0](httpx.Response(200, stream=stream))
    assert stream.closed and len(provider[1]) == 1


@pytest.mark.parametrize("change", [{"max_tokens": -1}, {"max_tokens": True},
                                    {"timeout": float("nan")}, {"timeout": 0},
                                    {"api_key": ""}, {"base_url": "invalid"}])
def test_invalid_runtime_does_not_call_provider(provider, change):
    assert provider[0](response(), config={**CONFIG, **change}) is None
    assert not provider[1]


def snapshot():
    return {"url": "https://example.com/page", "snapshot_id": "snapshot-1", "frame_id": "main",
            "offset": 20, "next_offset": 120, "total": 500, "truncated": True,
            "warnings": [{"code": "browser_trust_required"}], "diagnostics": {"code": "observed"},
            "elements": [{"node_id": f"n{i}", "text": "Observed text", "selector": f"#node-{i}",
                          "selector_source": "id", "selector_matches": 1,
                          "attributes": {"id": f"node-{i}", "aria-label": "Submit"}}
                         for i in range(100)]}


def test_modest_results_are_deep_copied_unchanged():
    result = {"ok": True, "config_schema": {"properties": {"seconds": {"default": 2}}}}
    compacted = context.compact_tool_result("get_feature_schema", result)
    assert compacted == result and compacted is not result
    compacted["config_schema"].clear()
    assert result["config_schema"]


def test_snapshot_compaction_keeps_provenance_and_sampled_evidence():
    result = snapshot()
    original = deepcopy(result)
    compacted = context.compact_tool_result("browser_observe", result, 42)
    assert len(context._json(compacted)) <= 10000
    for key in ("url", "snapshot_id", "frame_id", "offset", "next_offset", "total", "truncated"):
        assert compacted[key] == result[key]
    assert len(compacted["elements"]) == 8 and compacted["elements_omitted_count"] == 92
    assert compacted["elements"][0]["selector"] == "#node-0"
    assert compacted["elements"][0]["attributes"]["aria-label"] == "Submit"
    assert compacted["elements"][0]["text"] == "Observed text"
    assert compacted["warnings"] and compacted["diagnostics"]
    assert compacted["source_message_id"] == 42 and compacted["omitted"] is True
    assert compacted["notice"] == "full result remains stored; retrieve via get_tool_result"
    assert result == original


def test_execution_unknown_never_loses_operation_or_no_retry():
    result = {"ok": False, "operation_id": "operation-123", "status": "unknown",
              "code": "outcome_uncertain", "before": snapshot(), "after": snapshot(),
              "result": {"submitted": True}, "logs": ["Step execution started", "Step execution finished"],
              "diagnostics": {"reason": "execution_timeout"}}
    for budget in (10000, 2500, 1600):
        compacted = context.compact_tool_result("execute_step", result, 9, budget)
        assert len(context._json(compacted)) <= budget
        assert compacted["operation_id"] == "operation-123"
        assert compacted["status"] == "unknown" and compacted["code"] == "outcome_uncertain"
        assert compacted["no_retry"] is True and "category policy" in compacted["retry_notice"]
        assert compacted["before"]["snapshot_id"] == "snapshot-1"
        assert compacted["after"]["url"] == "https://example.com/page"
        assert compacted["result"] == result["result"]
        assert compacted["diagnostics"] == result["diagnostics"]
        assert compacted["logs"] == result["logs"]
        if budget == 10000:
            assert len(compacted["before"]["elements"]) <= 4
            assert compacted["before"]["snapshot_id"] == "snapshot-1"
            assert compacted["logs"] == result["logs"]
            assert compacted["diagnostics"] == result["diagnostics"]
    with pytest.raises(ValueError, match="protected"):
        context.compact_tool_result("execute_step", result, budget=100)


def test_large_schema_is_reference_not_config_or_expanded_definitions():
    result = {"feature_type": "action.click", "version": 3,
              "config_schema": {"properties": {"selector": {"description": "x" * 20000,
                                                           "default": "private-config"}},
                                "$defs": {"expanded": "x" * 20000}, "required": ["selector"]},
              "config": {"actual": "must-not-copy"}}
    compacted = context.compact_tool_result("get_feature_schema", result, 17)
    assert compacted["feature_type"] == "action.click" and compacted["version"] == 3
    assert compacted["config_schema"]["field_names"] == ["selector"]
    assert compacted["retrieval_reference"]["tool"] == "get_feature_schema"
    assert "must-not-copy" not in context._json(compacted)
    assert "private-config" not in context._json(compacted) and "$defs" not in context._json(compacted)


def test_generic_large_results_are_bounded_and_explicit():
    result = {"items": [{"text": "\u4e00" * 10000} for _ in range(100)], "ok": True}
    for budget in (10000, 1500, 300):
        compacted = context.compact_tool_result("get_tool_result", result, 5, budget)
        assert len(context._json(compacted)) <= budget
        assert compacted["context_compacted"] is True and compacted["omitted"] is True
        assert compacted["source_message_id"] == 5
    compacted = context.compact_tool_result("get_tool_result", result)
    assert len(compacted["items"]["items"]) <= 8
    assert compacted["items"]["omitted_count"] >= 92


def batch():
    return [{"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]},
            {"role": "tool", "tool_call_id": "a", "content": "result-a"},
            {"role": "tool", "tool_call_id": "b", "content": "result-b"}]


def test_fit_drops_old_whole_turns_keeps_recent_users_and_system():
    system = {"role": "system", "content": "host policy"}
    recent = [{"role": "user", "content": "recent full user"}, *batch(),
              {"role": "user", "content": "latest full user"}]
    messages = [system, {"role": "user", "content": "old" * 10000},
                {"role": "assistant", "content": "old answer"}, *recent]
    original = deepcopy(messages)
    fitted = context.fit_context(messages, max_tokens=estimate_context_tokens([system, *recent]))
    assert fitted == [system, *recent]
    assert messages == original and fitted[0] is not system
    with pytest.raises(ValueError, match="protected"):
        context.fit_context(messages, max_tokens=10)


def test_fit_preserves_live_result_and_valid_tool_batches():
    messages = [{"role": "user", "content": "latest"}, *batch()]
    assert context.fit_context(messages) == messages
    messages[-1]["content"] = "x" * 300000
    with pytest.raises(ValueError, match="protected"):
        context.fit_context(messages)
    for malformed in (batch()[1:], batch()[:-1], [batch()[0], {"role": "user", "content": "break"}]):
        with pytest.raises(ValueError, match="tool context"):
            context.fit_context(malformed)


def test_fit_drops_complete_old_tool_turn_not_partial_batch():
    recent = [{"role": "user", "content": "recent"}, {"role": "user", "content": "latest"}]
    messages = [{"role": "user", "content": "old"}, *batch(), *recent]
    assert context.fit_context(messages, max_tokens=estimate_context_tokens(recent)) == recent


def test_fit_noop_and_invalid_limits():
    messages = [{"role": "system", "content": "policy"}, {"role": "user", "content": "full"}]
    assert context.fit_context(messages) == messages
    assert context.fit_context([]) == []
    for kwargs in ({"max_tokens": 0}, {"keep_recent_users": -1}, {"keep_recent_users": True}):
        with pytest.raises(ValueError):
            context.fit_context(messages, **kwargs)
