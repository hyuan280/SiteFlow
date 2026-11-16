from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import socket
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker import vision  # noqa: E402


@pytest.fixture(params=["result", "grid", "quiz", "captcha"])
def model_call(request, monkeypatch):
    kind = request.param
    config = {
        "api_key": "private-api-key", "model": "private-model-id",
        "base_url": "https://private-user:private-password@private-host.invalid/v1/"
                    "?token=private-query", "timeout": 17,
        "model_configs": [{"api_key": "private-nested-key", "llm_prompt": "private-nested-prompt"}],
        "_model_redaction_values": ("private-job-cookie",),
    }
    final = {
        "result": {"result": True, "reason": "Visible evidence."},
        "grid": {"is_clear": True, "need_refresh": False, "selected_indices": [1]},
        "quiz": {"is_clear": True, "need_refresh": False, "selected_index": 1,
                 "reason": "Visible evidence."},
        "captcha": "Ab12",
    }[kind]
    content = final if kind == "captcha" else json.dumps(final)
    case = SimpleNamespace(
        kind=kind, config=config, status=200, calls=0, raw=None, failure=None,
        read_failure=None, error_reads=[],
        prompt="private-custom-prompt", page_text="private-page-text", question="private-question",
        options=["private-option"], image=b"private-image",
        payload={"choices": [{"message": {"content": content}, "finish_reason": "stop"}]},
    )
    original_client = httpx.AsyncClient

    def observe():
        case.calls += 1
        assert vision._result_request_active.get()
        for name in ("httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2",
                     "httpcore.proxy", "httpcore.socks"):
            logging.getLogger(name).debug("private-log-trace private-api-key")
        if case.failure is not None:
            raise case.failure

    def handle(_request):
        observe()
        if case.raw is not None:
            return httpx.Response(case.status, content=case.raw)
        return httpx.Response(case.status, content=json.dumps(case.payload).encode())

    def client(**kwargs):
        assert kwargs == {"timeout": config.get("timeout", 30), "follow_redirects": False}
        return original_client(transport=httpx.MockTransport(handle), **kwargs)

    class ErrorBody(io.BytesIO):
        def read(self, size=-1):
            case.error_reads.append(size)
            if case.read_failure:
                raise case.read_failure
            return super().read(size)

    def urlopen(_request, **kwargs):
        observe()
        body = case.raw if case.raw is not None else json.dumps(case.payload).encode()
        if case.status >= 300:
            raise HTTPError(config["base_url"], case.status, "private-http-reason", {}, ErrorBody(body))
        if case.read_failure:
            class FailedRead(io.BytesIO):
                def read(self, size=-1):
                    raise case.read_failure
            return FailedRead()
        return io.BytesIO(body)

    monkeypatch.setattr(vision.httpx, "AsyncClient", client)
    monkeypatch.setattr(vision, "urlopen", urlopen)

    def invoke():
        if kind == "result":
            return asyncio.run(vision.judge_result(config, case.prompt, page_text=case.page_text))
        if kind == "grid":
            return asyncio.run(vision.answer_grid_captcha(case.image, config, case.prompt))
        if kind == "quiz":
            return vision.answer_image_quiz(case.image, case.question, case.options, config, case.prompt)
        return vision.recognize(case.image, config, case.prompt)

    case.invoke = invoke
    return case


def failure(case, code, stage):
    with pytest.raises(vision.ModelCallError) as caught:
        case.invoke()
    error = caught.value
    assert type(error) is (vision.ResultJudgmentError if case.kind == "result" else vision.ModelCallError)
    assert (error.code, error.stage) == (code, stage)
    assert (error.reason, error.status_code) == (error.code, error.http_status)
    assert error.__context__ is error.__cause__ is None
    assert error.__suppress_context__
    assert not vision._result_request_active.get()
    assert case.calls <= 1
    assert len(vision.format_model_error(error)) <= 2000
    return error


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 503])
def test_http_details_are_safe_bounded_admin_only(model_call, status, caplog, capsys):
    case = model_call
    caplog.set_level(logging.DEBUG)
    case.status = status
    secrets = [case.config[key] for key in ("api_key", "model", "base_url")]
    secrets += ["private-user", "private-password", "private-host.invalid", "private-query",
                "private-nested-key", "private-nested-prompt", "private-job-cookie", case.prompt]
    if case.kind == "result":
        secrets.append(case.page_text)
    else:
        secrets.append(base64.b64encode(case.image).decode())
    if case.kind == "quiz":
        secrets.extend((case.question, *case.options))
    case.payload = {"error": {
        "message": "Quota exceeded; unsupported model: " + " ".join(secrets),
        "type": "rate_limit_error private-api-key", "code": 123,
        "headers": {"Authorization": "private-header"}, "request": "private-request",
    }}
    error = failure(case, "http_error", "request")
    data = vision.model_error_data(error, case.config)
    assert data["http_status"] == status
    assert data["provider_code"] == "123"
    assert "Quota exceeded; unsupported model:" in data["provider_message"]
    assert "rate_limit_error" in data["provider_type"]
    assert "Quota" not in str(error)
    assert set(data) == {"provider_error", "stage", "http_status", "provider_message",
                         "provider_type", "provider_code"}
    assert "HTTP " + str(status) in vision.format_model_error(error, case.config)
    output = (repr(vars(error)) + repr(data) + vision.format_model_error(error, case.config)
              + "".join(traceback.format_exception(error)) + caplog.text + repr(capsys.readouterr()))
    for secret in secrets + ["private-header", "private-request", "private-log-trace"]:
        assert secret not in output
    if case.kind in {"quiz", "captcha"}:
        assert case.error_reads == [16 * 1024 + 1]
    logging.getLogger("httpx").debug("unrelated-after-model")
    assert "unrelated-after-model" in caplog.text


@pytest.mark.parametrize("body", [
    b"<html>private-body</html>", b"not JSON private-body",
    json.dumps({"message": "private-body", "code": "not-an-error-envelope"}).encode(),
    json.dumps({"error": "private-body"}).encode(),
    json.dumps({"error": {"message": "private-body" + " " * (16 * 1024)}}).encode(),
])
def test_http_never_falls_back_to_raw_or_oversized_body(model_call, body):
    model_call.status, model_call.raw = 500, body
    error = failure(model_call, "http_error", "request")
    assert vision.model_error_data(error) == {
        "provider_error": "http_error", "stage": "request", "http_status": 500,
    }


@pytest.mark.parametrize("value", [
    "private-body data:image/png;base64,aGVsbG8=", "private-body BASE64 image",
    "private-body " + "aGVsbG8x" * 30,
    'private-body {"messages": [{"role":"user","content":"secret"}]}',
    "private-body messages=[secret request dump]", "private-body <html>secret</html>",
])
def test_provider_dump_reflection_is_rejected_in_all_detail_fields(model_call, value):
    model_call.status = 400
    model_call.payload = {"error": dict.fromkeys(("message", "type", "code"), value)}
    error = failure(model_call, "http_error", "request")
    assert set(vision.model_error_data(error)) == {"provider_error", "stage", "http_status"}


def test_error_envelope_on_http_success_is_still_failure(model_call):
    model_call.payload["error"] = {"message": "Quota exceeded", "type": "quota", "code": "no_credit"}
    error = failure(model_call, "invalid_response", "provider_response")
    assert vision.model_error_data(error)["provider_message"] == "Quota exceeded"


@pytest.mark.parametrize("timeout_cls,phase", [
    (httpx.ConnectTimeout, "connect"), (httpx.ReadTimeout, "read"),
    (httpx.WriteTimeout, "write"), (httpx.PoolTimeout, "pool"),
    (httpx.TimeoutException, "request"),
])
def test_httpx_timeout_subclasses(model_call, timeout_cls, phase):
    if model_call.kind not in {"result", "grid"}:
        pytest.skip("HTTPX transport only")
    model_call.failure = timeout_cls("private-hostname errno private-api-key")
    error = failure(model_call, "timeout", "request")
    assert vision.model_error_data(error) == {
        "provider_error": "timeout", "stage": "request", "timeout_phase": phase, "timeout_seconds": 17,
    }
    assert phase in vision.format_model_error(error)
    assert "private" not in repr(vars(error))


@pytest.mark.parametrize("wrapped", [True, False])
@pytest.mark.parametrize("read", [True, False])
def test_urllib_timeout_and_wrapped_reason(model_call, wrapped, read):
    if model_call.kind not in {"quiz", "captcha"}:
        pytest.skip("urllib transport only")
    error = socket.timeout("private-timeout-host")
    error = URLError(error) if wrapped else error
    if read:
        model_call.read_failure = error
    else:
        model_call.failure = error
    caught = failure(model_call, "timeout", "provider_response" if read else "request")
    data = vision.model_error_data(caught)
    assert (data["timeout_phase"], data["timeout_seconds"]) == ("read" if read else "request", 17)
    assert "private" not in repr(vars(caught))


def test_network_errors_never_copy_exception_text(model_call):
    model_call.failure = (httpx.ConnectError("private-host errno 111")
                          if model_call.kind in {"result", "grid"}
                          else URLError(OSError("private-host errno 111")))
    error = failure(model_call, "network_error", "request")
    assert vision.model_error_data(error) == {"provider_error": "network_error", "stage": "request"}
    assert "private" not in repr(vars(error))


@pytest.mark.parametrize("payload", [None, [], {}, {"choices": []}, {"choices": [None]},
                                   {"choices": [{"message": None}]}])
def test_invalid_envelopes(model_call, payload):
    model_call.payload = payload
    failure(model_call, "invalid_response", "provider_response")


def test_invalid_provider_json(model_call):
    model_call.raw = b"private-raw-body not json"
    error = failure(model_call, "invalid_json", "provider_response")
    assert "private" not in repr(vars(error))


@pytest.mark.parametrize("finish,code", [("length", "truncated"), ("content_filter", "content_filtered")])
def test_incomplete_output_is_typed(model_call, finish, code):
    model_call.payload["choices"][0]["finish_reason"] = finish
    failure(model_call, code, "provider_response")


def test_refusal_never_includes_raw_prose(model_call):
    model_call.payload["choices"][0]["message"]["refusal"] = "private-refusal"
    error = failure(model_call, "refused", "provider_response")
    assert "private" not in repr(vars(error))


def test_invalid_content(model_call):
    model_call.payload["choices"][0]["message"]["content"] = "not valid output"
    failure(model_call, "schema_error" if model_call.kind == "captcha" else "invalid_json", "content")


def test_invalid_selection_schema(model_call):
    if model_call.kind not in {"quiz", "grid"}:
        pytest.skip("selection clients only")
    model_call.payload["choices"][0]["message"]["content"] = json.dumps({
        "is_clear": True, "need_refresh": False, "selected_indices": [10], "selected_index": 10,
    })
    failure(model_call, "schema_error", "content")


@pytest.mark.parametrize("budget", [True, -1, 1.5, "private-invalid-budget"])
def test_invalid_budget_has_input_stage_without_transport(model_call, budget):
    model_call.config["max_tokens"] = budget
    failure(model_call, "invalid_input", "input")
    assert model_call.calls == 0


def test_error_body_read_failure_does_not_replace_http_error(model_call):
    if model_call.kind not in {"quiz", "captcha"}:
        pytest.skip("urllib transport only")
    model_call.status = 503
    model_call.read_failure = RuntimeError("private-read-error")
    error = failure(model_call, "http_error", "request")
    assert vision.model_error_data(error) == {
        "provider_error": "http_error", "stage": "request", "http_status": 503,
    }
    assert model_call.error_reads == [16 * 1024 + 1]


def test_cancellation_and_scoped_filter_cleanup(model_call):
    if model_call.kind not in {"result", "grid"}:
        pytest.skip("async transport only")
    model_call.failure = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        model_call.invoke()
    assert model_call.calls == 1
    assert not vision._result_request_active.get()


@pytest.mark.parametrize("unclear", [False, True])
def test_quiz_reason_is_sanitized_before_normalize_and_truncate(model_call, unclear):
    if model_call.kind != "quiz":
        pytest.skip("quiz reason contract")
    secret = "private-left\tprivate-middle\nprivate-right"
    model_call.config["_model_redaction_values"] = (secret, "private-left")
    model_call.payload["choices"][0]["message"]["content"] = json.dumps({
        "selected_index": 1, "need_refresh": unclear,
        "reason": "Visible evidence. " + secret + " remainder " * 100,
    })
    if unclear:
        with pytest.raises(vision.ImageQuizUnclear) as caught:
            model_call.invoke()
        assert caught.value.__context__ is caught.value.__cause__ is None
        reason = str(caught.value)
    else:
        _, reason = model_call.invoke()
    assert reason.startswith("Visible evidence. [REDACTED]")
    assert len(reason) <= 500
    assert "private" not in reason


def test_overlapping_secrets_are_removed_before_transforms():
    secret = "private-left\tprivate-right\nprivate-tail"
    error = vision.ModelCallError("http_error", "request", 429, details={
        "provider_message": "Quota exceeded. " + secret + " remainder " * 100,
    }, config={"api_key": secret[:20], "_model_redaction_values": (secret,)})
    assert error.provider_message.startswith("Quota exceeded. [REDACTED]")
    assert len(error.provider_message) <= 500
    assert "private" not in repr(vars(error))


def test_callers_can_resanitize_details_with_additional_job_secrets():
    error = vision.ModelCallError("http_error", "request", 429, details={
        "provider_message": "Quota exceeded for private-extra-cookie",
        "provider_type": "private-extra-cookie", "provider_code": "private-extra-cookie",
    })
    data = vision.model_error_data(error, redaction_values=("private-extra-cookie",))
    summary = vision.format_model_error(error, redaction_values=("private-extra-cookie",))
    assert "Quota exceeded" in summary
    assert "private-extra-cookie" not in repr(data) + summary
    assert "private-extra-cookie" in error.provider_message  # no mutation or hidden raw copy


@pytest.mark.parametrize("timeout", [True, "private-config", float("inf"), float("nan"), -1, None])
def test_timeout_metadata_is_finite_and_static(timeout):
    error = vision.ModelCallError("timeout", "request", details={
        "timeout_phase": "private-phase", "timeout_seconds": timeout,
    })
    assert vision.model_error_data(error) == {"provider_error": "timeout", "stage": "request"}
    assert "private" not in repr(vars(error))


def test_unknown_exception_is_not_stringified_or_inspected():
    class UnsafeError(ValueError):
        @property
        def reason(self):
            pytest.fail("do not inspect arbitrary exception properties")

        def __str__(self):
            pytest.fail("do not stringify arbitrary exceptions")

    assert vision.model_error_data(UnsafeError()) == {
        "provider_error": "unexpected_error", "stage": "request",
    }
    assert "unexpected_error" in vision.format_model_error(UnsafeError())
    old = ValueError("private-legacy-error")
    old.reason, old.status_code = "http_error", 401
    assert vision.model_error_data(old) == {
        "provider_error": "http_error", "stage": "request", "http_status": 401,
    }


def test_result_constructor_remains_compatible():
    error = vision.ResultJudgmentError("http_error", "request", 401)
    assert isinstance(error, vision.ModelCallError)
    assert str(error) == "Result judgment failed: http_error (request) [HTTP 401]"


@pytest.mark.parametrize("size,accepted", [(16 * 1024, True), (16 * 1024 + 1, False)])
def test_error_envelope_size_boundary(model_call, size, accepted):
    body = json.dumps({"error": {"message": "Quota exceeded"}}).encode()
    model_call.raw = body + b" " * (size - len(body))
    model_call.status = 429
    error = failure(model_call, "http_error", "request")
    assert ("provider_message" in vision.model_error_data(error)) is accepted


def test_only_scalar_envelope_fields_are_accepted(model_call):
    model_call.status = 400
    model_call.payload = {"error": {"message": {"private": "dump"}, "type": ["dump"], "code": True}}
    error = failure(model_call, "http_error", "request")
    assert set(vision.model_error_data(error)) == {"provider_error", "stage", "http_status"}


@pytest.mark.parametrize("source,code", [("tool_call", "invalid_tool_call"),
                                       ("tool_arguments", "invalid_json")])
def test_tool_stage_diagnostics_never_fall_back(model_call, source, code):
    if model_call.kind not in {"grid", "result"}:
        pytest.skip("tool clients only")
    model_call.payload["choices"][0]["message"]["tool_calls"] = [{
        "type": "function", "function": {
            "name": "wrong-tool" if source == "tool_call" else (
                "submit_geetest_result" if model_call.kind == "grid" else "submit_workflow_result"
            ),
            "arguments": "private-invalid-json",
        },
    }]
    error = failure(model_call, code, source)
    assert "private-invalid-json" not in repr(vars(error))


def test_success_resets_scoped_log_filter(model_call, caplog):
    caplog.set_level(logging.DEBUG)
    model_call.invoke()
    assert not vision._result_request_active.get()
    assert "private-log-trace" not in caplog.text
    logging.getLogger("httpcore.http11").debug("unrelated-after-success")
    assert "unrelated-after-success" in caplog.text


def test_redaction_precedes_truncation_even_when_secret_straddles_limit():
    secret = "private-cookie-left\tprivate-cookie-right"
    prefix = "Quota exceeded. " + "x " * 235
    assert len(prefix) < 500 < len(prefix + secret)
    error = vision.ModelCallError("http_error", "request", 429, config={
        "_model_redaction_values": (secret,),
    }, details={"provider_message": prefix + secret})
    assert "private-cookie" not in error.provider_message
    assert "[REDACTED]" in error.provider_message


@pytest.mark.parametrize("key", ["api_key", "base_url", "model"])
def test_missing_connection_config_is_invalid_input(model_call, key):
    model_call.config.pop(key)
    failure(model_call, "invalid_input", "input")
    assert model_call.calls == 0


@pytest.mark.parametrize("unclear", [False, True])
def test_quiz_reflected_image_reason_is_not_returned(model_call, unclear):
    if model_call.kind != "quiz":
        pytest.skip("quiz reason contract")
    model_call.payload["choices"][0]["message"]["content"] = json.dumps({
        "selected_index": 1, "need_refresh": unclear,
        "reason": "private-image-dump data:image/png;base64,aGVsbG8=",
    })
    if unclear:
        with pytest.raises(vision.ImageQuizUnclear) as caught:
            model_call.invoke()
        assert str(caught.value) == "视觉模型无法确定答案"
        assert caught.value.__context__ is None
    else:
        assert model_call.invoke() == (1, "")


@pytest.mark.parametrize("unclear", [False, True])
@pytest.mark.parametrize("reason", [
    {"nested": {"value": "private-left\tprivate-middle\nprivate-right"}},
    ["private-left\tprivate-middle\nprivate-right"],
    {}, [], 0, 1234, 1.5, False, True,
])
def test_quiz_nonstring_reason_is_schema_error_without_repr_leak(model_call, unclear, reason):
    if model_call.kind != "quiz":
        pytest.skip("quiz reason contract")
    model_call.config["_model_redaction_values"] = ("private-left\tprivate-middle\nprivate-right",)
    model_call.payload["choices"][0]["message"]["content"] = json.dumps({
        "selected_index": 1, "need_refresh": unclear, "reason": reason,
    })
    error = failure(model_call, "schema_error", "content")
    output = repr(vars(error)) + vision.format_model_error(error) + str(error)
    assert "private-left" not in output
    assert "private-middle" not in output
    assert "private-right" not in output
    assert "\\t" not in output and "\\n" not in output


@pytest.mark.parametrize("unclear", [False, True])
@pytest.mark.parametrize("include_null", [False, True])
def test_quiz_missing_or_null_reason_remains_optional(model_call, unclear, include_null):
    if model_call.kind != "quiz":
        pytest.skip("quiz reason contract")
    result = {"selected_index": 1, "need_refresh": unclear}
    if include_null:
        result["reason"] = None
    model_call.payload["choices"][0]["message"]["content"] = json.dumps(result)
    if unclear:
        with pytest.raises(vision.ImageQuizUnclear) as caught:
            model_call.invoke()
        assert str(caught.value) == "视觉模型无法确定答案"
        assert caught.value.__context__ is caught.value.__cause__ is None
    else:
        assert model_call.invoke() == (1, "")


def test_http_details_redact_reconstructed_secret_before_clipping(model_call):
    model_call.status = 429
    model_call.payload = {"error": dict.fromkeys(
        ("message", "type", "code"), "Quota " + "x " * 243 + "private-\x1b[31mapi-key",
    )}
    error = failure(model_call, "http_error", "request")
    data = vision.model_error_data(error, model_call.config)
    assert data["http_status"] == 429
    for key in ("provider_message", "provider_type", "provider_code"):
        assert data[key].startswith("Quota ")
        assert len(data[key]) <= 500
        assert "private-" not in data[key]
        assert "\x1b" not in data[key]
        assert all(ord(char) >= 32 for char in data[key])
    assert "private-" not in vision.format_model_error(error)


@pytest.mark.parametrize("fingerprint_only", [False, True])
@pytest.mark.parametrize("secret,reflected", [
    ("private-api-key", "private-\x1b[31mapi-key"),
    ("private-left private-right", "private-left\t\nprivate-right"),
    ("private-left\t\nprivate-right", "private-left private-right"),
])
def test_sanitizer_rescans_normalized_secrets_and_fingerprints(fingerprint_only, secret, reflected):
    if fingerprint_only and "\t" in secret:
        pytest.skip("a digest cannot recover the normalization of an unknown secret")
    config = ({"_result_secret_fingerprints": {
        (len(secret), hashlib.sha256(secret.encode()).hexdigest()),
    }} if fingerprint_only else {"api_key": secret})
    reason = "Quota " + "x " * 243 + reflected
    sanitized = vision.sanitize_judgment_reason(reason, config)
    assert sanitized.startswith("Quota ")
    assert len(sanitized) <= 500
    assert "private-" not in sanitized
    assert "\x1b" not in sanitized


@pytest.mark.parametrize("unclear", [False, True])
def test_quiz_reason_redacts_reconstructed_secret_before_clipping(model_call, unclear):
    if model_call.kind != "quiz":
        pytest.skip("quiz reason contract")
    model_call.payload["choices"][0]["message"]["content"] = json.dumps({
        "selected_index": 1, "need_refresh": unclear,
        "reason": "Quota " + "x " * 243 + "private-\x1b[31mapi-key",
    })
    if unclear:
        with pytest.raises(vision.ImageQuizUnclear) as caught:
            model_call.invoke()
        reason = str(caught.value)
        assert caught.value.__context__ is caught.value.__cause__ is None
    else:
        _, reason = model_call.invoke()
    assert reason.startswith("Quota ")
    assert "private-" not in reason
    assert "\x1b" not in reason
    assert len(reason) <= 500


@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"])
def test_http_surrogate_details_preserve_safe_status_and_no_context(model_call, surrogate):
    secret = "private-resolved-secret"
    model_call.config["_result_secret_fingerprints"] = {
        (len(secret), hashlib.sha256(secret.encode()).hexdigest()),
    }
    model_call.status = 429
    model_call.payload = {"error": dict.fromkeys(
        ("message", "type", "code"), "Quota private-provider-raw " + surrogate + secret,
    )}
    error = failure(model_call, "http_error", "request")
    assert vision.model_error_data(error, model_call.config) == {
        "provider_error": "http_error", "stage": "request", "http_status": 429,
    }
    output = repr(vars(error)) + vision.format_model_error(error, model_call.config)
    output += "".join(traceback.format_exception(error))
    assert "HTTP 429" in output
    assert "private-provider-raw" not in output
    assert secret not in output
    assert "\\ud800" not in output and "\\udfff" not in output
    output.encode("utf-8")
    if model_call.kind in {"quiz", "captcha"}:
        assert model_call.error_reads == [16 * 1024 + 1]


@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"])
def test_shared_sanitizer_rejects_invalid_unicode_without_hashing_failure(surrogate):
    secret = "private-resolved-secret"
    config = {"_result_secret_fingerprints": {
        (len(secret), hashlib.sha256(secret.encode()).hexdigest()),
    }}
    assert vision.sanitize_judgment_reason("Visible " + surrogate + secret, config) == ""


@pytest.mark.parametrize("fingerprint_only", [False, True])
def test_result_success_reason_redacts_secret_reconstructed_by_ansi_removal(model_call, fingerprint_only):
    if model_call.kind != "result":
        pytest.skip("result reason contract")
    secret = "private-resolved-secret" if fingerprint_only else model_call.config["api_key"]
    if fingerprint_only:
        model_call.config["_result_secret_fingerprints"] = {
            (len(secret), hashlib.sha256(secret.encode()).hexdigest()),
        }
    reflected = secret.replace("private-", "private-\x1b[31m", 1)
    model_call.payload["choices"][0]["message"]["content"] = json.dumps({
        "result": True, "reason": "Visible evidence. " + reflected,
    })
    assert model_call.invoke() == {"result": True, "reason": "Visible evidence. [REDACTED]"}
