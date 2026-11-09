from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import socket
import threading
from unittest.mock import AsyncMock, Mock

from fastapi import Request
import httpx
from httpx import AsyncClient as HTTPXAsyncClient
from pydantic import ValidationError
import pytest
from sqlalchemy import select

from app.api.v1 import config_ai
from app.core.auth import CSRF_COOKIE, SESSION_COOKIE
from app.core.config import Settings
from app.models.auth import AuthSession
from app.models.integration import IntegrationConfig
from app.schemas.config_ai import GenerateSiteDraftRequest, GenerationResult
from app.services import config_ai_generation as generation
from app.services.integrations import IntegrationService
from app.services.workflows import WorkflowService
from tests.helpers import make_settings
from tests.test_workflow_drafts import _read_only


URL = "/api/v1/config-ai/generate"
KEY = "fake-runtime-key-never-export"
PRIVATE = "private-provider-error-never-export"
CONFIG = {
    "base_url": "https://provider.example/v1", "model": "test-model",
    "api_key": KEY, "timeout": 30, "supports_vision": False,
}
EMPTY = {"steps": [], "groups": [], "signals": []}
PROPOSAL = {
    "draft": EMPTY, "explanation": "Review before saving.", "unresolved_questions": [],
    "status": "proposal", "requires_review": True,
}


def _payload(**changes):
    return {
        "site": {
            "name": "private-host-name",
            "base_url": "https://user:host-password@example.com/page?token=host-token#host-fragment",
        },
        "requirements": "Wait for the observed page.", "evidence": "Text-only observations.",
        "consent_to_send": True, "allowed_features": ["action.wait"], **changes,
    }


@pytest.fixture
def settings(tmp_path):
    return make_settings(
        tmp_path, config_ai_enabled=True, vision_base_url=None, vision_model=None,
        vision_api_key=None, vision_supports_vision=None,
    )


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    # TestClient has no real connection; disconnect and cancellation are tested explicitly below.
    monkeypatch.setattr(Request, "is_disconnected", AsyncMock(return_value=False))
    forbidden = Mock(side_effect=AssertionError("External network calls are forbidden"))
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(generation.httpx, "AsyncClient", forbidden)
    yield
    forbidden.assert_not_called()


@pytest.fixture
def mocked_generation(monkeypatch):
    runtime = Mock(return_value=deepcopy(CONFIG))
    generate = AsyncMock(return_value=deepcopy(PROPOSAL))
    monkeypatch.setattr(IntegrationService, "vision_runtime", runtime)
    monkeypatch.setattr(config_ai, "generate_site_draft", generate)
    return runtime, generate


def _assert_slot_free(client):
    slot = client.app.state.config_ai_slot
    assert slot.acquire(blocking=False)
    try:
        assert not slot.acquire(blocking=False)
    finally:
        slot.release()


def _assert_response(response, status, code=None, reason=None):
    assert response.status_code == status, response.text
    assert response.headers.get("cache-control") == "no-store"
    if code is not None:
        detail = {"code": code}
        if reason is not None:
            detail["reason"] = reason
        assert response.json() == {"detail": detail}
    for private in (KEY, PRIVATE):
        assert private not in response.text


def _post(client, payload=None, status=200, code=None, reason=None):
    with _read_only(client):
        response = client.post(URL, json=_payload() if payload is None else payload)
    _assert_response(response, status, code, reason)
    _assert_slot_free(client)
    return response


@pytest.mark.parametrize("auth_state", ["current", "older", "expired", "anonymous", "invalid"])
def test_auth_is_non_touching_and_required_before_runtime(client, mocked_generation, auth_state):
    if auth_state in {"older", "expired"}:
        with client.app.state.session_factory() as session:
            auth = session.scalar(select(AuthSession))
            auth.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=5)
            if auth_state == "expired":
                auth.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            session.commit()
    elif auth_state == "anonymous":
        client.cookies.clear()
    elif auth_state == "invalid":
        client.cookies.set(SESSION_COOKIE, "invalid-session", domain="testserver.local", path="/")
    authorized = auth_state in {"current", "older"}
    response = _post(client, status=200 if authorized else 401)
    runtime, generate = mocked_generation
    if authorized:
        assert response.json() == PROPOSAL
        runtime.assert_called_once_with()
        generate.assert_awaited_once()
    else:
        runtime.assert_not_called()
        generate.assert_not_called()


@pytest.mark.parametrize("csrf_state", ["missing_header", "missing_cookie", "mismatch", "forged_pair"])
def test_csrf_rejection_is_read_only_before_runtime(client, mocked_generation, csrf_state):
    if csrf_state == "missing_header":
        client.headers.pop("X-CSRF-Token")
    elif csrf_state == "missing_cookie":
        client.cookies.delete(CSRF_COOKIE, domain="testserver.local", path="/")
    else:
        client.headers["X-CSRF-Token"] = "forged-csrf"
        if csrf_state == "forged_pair":
            client.cookies.set(CSRF_COOKIE, "forged-csrf", domain="testserver.local", path="/")
    _post(client, status=403)
    for mock in mocked_generation:
        mock.assert_not_called()


def test_disabled_returns_503_without_resolving_runtime_or_generating(client, mocked_generation):
    client.app.state.settings.config_ai_enabled = False
    _post(client, status=503, code="generation_disabled")
    for mock in mocked_generation:
        mock.assert_not_called()


@pytest.mark.parametrize("features", [
    ["unknown-private-feature"], ["plugin.private.feature"], ["action.fill"],
    ["post.buy_card"], ["post.resign_dialog"], ["sign.iframe_form"], ["action.wait", "post.buy_card"],
])
def test_invalid_feature_selection_precedes_runtime(client, mocked_generation, features):
    _post(client, _payload(allowed_features=features), 422, "invalid_selection")
    for mock in mocked_generation:
        mock.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"consent_to_send": False}, {"consent_to_send": 1}, {"consent_to_send": "true"},
    {"requirements": ""}, {"requirements": "x" * 8001}, {"evidence": "x" * 16001},
    {"allowed_features": []}, {"allowed_features": ["action.wait"] * 2},
    {"allowed_features": ["action.wait"] * 23}, {"allowed_features": [1]},
    {"site": {"name": "", "base_url": "https://example.com"}},
    {"site": {"name": "new", "base_url": "file:///private"}},
    {"site": {"name": "new", "base_url": "https://example.com", "credentials": {}}},
    {"owner_id": 1}, {"template_id": 1}, {"credentials": {"password": "fake-password"}},
    {"expected_revision": 1}, {"trusted_urls": ["https://example.com"]},
])
def test_invalid_pydantic_arguments_are_uncacheable_and_do_not_reach_runtime(
    client, mocked_generation, changes,
):
    _post(client, _payload(**changes), 422, "invalid_request")
    for mock in mocked_generation:
        mock.assert_not_called()


@pytest.mark.parametrize("field", ["site", "requirements", "consent_to_send", "allowed_features"])
def test_required_arguments_cannot_be_omitted(client, mocked_generation, field):
    payload = _payload()
    del payload[field]
    _post(client, payload, 422, "invalid_request")
    for mock in mocked_generation:
        mock.assert_not_called()


def test_field_limits_remain_accepted(client, mocked_generation):
    payload = _payload(
        site={"name": "n" * 128, "base_url": "https://example.com"},
        requirements="r" * 8000, evidence="e" * 16000,
    )
    assert _post(client, payload).json() == PROPOSAL
    runtime, generate = mocked_generation
    runtime.assert_called_once_with()
    generate.assert_awaited_once()
    assert generate.await_args.args[1].model_dump(mode="json") == payload


@pytest.mark.parametrize("raw", [
    json.dumps(_payload(**{PRIVATE: {"password": KEY}})).encode(),
    json.dumps(_payload(requirements={PRIVATE: KEY})).encode(),
    ('{"' + PRIVATE + '":"' + KEY + '",').encode(),
    b"",
])
def test_validation_errors_never_echo_input(client, mocked_generation, raw):
    with _read_only(client):
        response = client.post(URL, content=raw, headers={"Content-Type": "application/json"})
    _assert_response(response, 422, "invalid_request")
    _assert_slot_free(client)
    for mock in mocked_generation:
        mock.assert_not_called()


@pytest.mark.parametrize("content_length", [None, "1", str(128 * 1024 + 1000)])
@pytest.mark.parametrize("oversized", [False, True])
def test_streamed_body_cap_uses_actual_bytes_and_caches_valid_body(
    client, mocked_generation, content_length, oversized,
):
    consumed = []
    raw = json.dumps(_payload()).encode()

    async def chunks():
        for chunk in (raw + b" " * (65536 - len(raw)), b" " * 65536):
            consumed.append(len(chunk))
            yield chunk
        if oversized:
            consumed.append(1)
            yield b" "
            pytest.fail("Oversized requests must not consume the rest of the stream")

    async def run():
        headers = {**client.headers, "Content-Type": "application/json"}
        if content_length is not None:
            headers["Content-Length"] = content_length
        async with HTTPXAsyncClient(
            transport=httpx.ASGITransport(app=client.app), base_url="http://testserver",
            cookies=client.cookies, headers=headers,
        ) as requester:
            return await requester.post(URL, content=chunks())

    with _read_only(client):
        response = asyncio.run(run())
    _assert_response(response, 413 if oversized else 200, "request_limit" if oversized else None)
    assert consumed == [65536, 65536] + ([1] if oversized else [])
    _assert_slot_free(client)
    runtime, generate = mocked_generation
    if oversized:
        runtime.assert_not_called()
        generate.assert_not_called()
    else:
        assert response.json() == PROPOSAL
        runtime.assert_called_once_with()
        generate.assert_awaited_once()
        assert generate.await_args.args[1].model_dump(mode="json") == _payload()


def test_body_read_timeout_is_generic_and_does_not_reach_runtime(
    client, mocked_generation, monkeypatch,
):
    real_timeout = asyncio.timeout
    timeout = Mock(side_effect=lambda seconds: real_timeout(0))

    async def chunks():
        yield ('{"' + PRIVATE + '":"' + KEY).encode()
        await asyncio.Event().wait()

    async def run():
        async with HTTPXAsyncClient(
            transport=httpx.ASGITransport(app=client.app), base_url="http://testserver",
            cookies=client.cookies, headers={**client.headers, "Content-Type": "application/json"},
        ) as requester:
            return await requester.post(URL, content=chunks())

    with monkeypatch.context() as patch, _read_only(client):
        patch.setattr(config_ai.asyncio, "timeout", timeout)
        response = asyncio.run(run())
    _assert_response(response, 408, "request_timeout")
    timeout.assert_called_once_with(10)
    _assert_slot_free(client)
    for mock in mocked_generation:
        mock.assert_not_called()
    assert _post(client).json() == PROPOSAL


def test_get_validation_keeps_default_error_shape(client, mocked_generation):
    with _read_only(client):
        response = client.get("/api/v1/config-ai/features", params={"category": "not-a-category"})
    _assert_response(response, 422)
    errors = response.json()["detail"]
    assert isinstance(errors, list) and len(errors) == 1
    assert errors[0]["loc"] == ["query", "category"]
    assert errors[0]["type"] == "literal_error"
    assert errors[0]["input"] == "not-a-category"
    for mock in mocked_generation:
        mock.assert_not_called()


@pytest.mark.parametrize("state", ["absent", "partial", "invalid_url", "corrupt"])
def test_real_runtime_resolution_fails_safely_without_provider(client, monkeypatch, state):
    if state != "absent":
        with client.app.state.session_factory() as session:
            integrations = IntegrationService(
                session, client.app.state.settings, client.app.state.crypto,
            )
            integrations._upsert("vision.base_url", CONFIG["base_url"], "test")
            if state != "partial":
                integrations._upsert("vision.model", CONFIG["model"], "test")
                integrations._upsert("vision.api_key", KEY, "test")
            session.flush()
            if state == "invalid_url":
                integrations._upsert("vision.base_url", "file:///" + PRIVATE, "test")
            elif state == "corrupt":
                session.get(IntegrationConfig, "vision.api_key").value_ciphertext = PRIVATE.encode()
            session.commit()
    generate = AsyncMock(side_effect=AssertionError("No provider without a valid runtime"))
    monkeypatch.setattr(config_ai, "generate_site_draft", generate)
    _post(client, status=503, code="model_unavailable")
    generate.assert_not_called()


def test_runtime_exception_text_is_not_returned(client, mocked_generation):
    runtime, generate = mocked_generation
    runtime.side_effect = RuntimeError(f"{PRIVATE}: {KEY}")
    _post(client, status=503, code="model_unavailable")
    generate.assert_not_called()


def test_mocked_async_success_passes_host_service_and_returns_only_proposal(
    client, mocked_generation, monkeypatch,
):
    runtime, generate = mocked_generation
    monitor_stopped = threading.Event()

    async def disconnected(request):
        try:
            await asyncio.Event().wait()
        finally:
            monitor_stopped.set()

    async def success(service, payload, config):
        assert isinstance(service, WorkflowService)
        assert service.settings is client.app.state.settings
        assert service.crypto is client.app.state.crypto
        assert service.actor.id is not None
        assert payload.model_dump(mode="json") == _payload()
        assert config == CONFIG
        await asyncio.sleep(0)
        return deepcopy(PROPOSAL)

    monkeypatch.setattr(Request, "is_disconnected", disconnected)
    generate.side_effect = success
    response = _post(client)
    body = response.json()
    assert body == GenerationResult.model_validate(PROPOSAL).model_dump(mode="json")
    assert set(body) == {"draft", "explanation", "unresolved_questions", "status", "requires_review"}
    assert set(body["draft"]) == {"steps", "groups", "signals"}
    for private in ("private-host-name", "host-password", "host-token", "host-fragment"):
        assert private not in response.text
    assert monitor_stopped.wait(1)
    runtime.assert_called_once_with()
    generate.assert_awaited_once()


@pytest.mark.parametrize("code,status,reason", [
    ("provider_http", 502, None), ("provider_timeout", 504, None), ("secret_input", 422, None),
    ("invalid_response", 502, None),
    ("invalid_response", 502, "no_tool_calls"), ("invalid_response", 502, "truncated"),
    ("invalid_response", 502, "too_many_parallel_tools"),
    ("invalid_response", 502, "submission_not_alone"),
    ("invalid_response", 502, "invalid_tool_function"),
    ("invalid_arguments", 502, "invalid_argument_json"),
    ("invalid_arguments", 502, "arguments_not_object"),
])
def test_generation_failure_releases_slot_for_next_request(
    client, mocked_generation, code, status, reason,
):
    runtime, generate = mocked_generation
    error = (generation.GenerationError(code, status) if reason is None else
             generation.GenerationError(code, status, reason=reason))
    assert error.reason == reason
    generate.side_effect = [error, deepcopy(PROPOSAL)]
    _post(client, status=status, code=code, reason=reason)
    assert _post(client).json() == PROPOSAL
    assert runtime.call_count == generate.await_count == 2


def test_concurrent_request_is_busy_without_provider_and_slot_reopens(client, mocked_generation):
    runtime, generate = mocked_generation
    started = threading.Event()
    release = threading.Event()

    async def blocked(*args):
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return deepcopy(PROPOSAL)

    generate.side_effect = blocked
    with _read_only(client), ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.post, URL, json=_payload())
        try:
            assert started.wait(5), "First generation did not start"
            busy = client.post(URL, json=_payload())
            _assert_response(busy, 429, "generation_busy")
            assert busy.headers["retry-after"] == "5"
            assert runtime.call_count == generate.await_count == 1
        finally:
            release.set()
        _assert_response(first.result(timeout=5), 200)
    _assert_slot_free(client)
    assert _post(client).json() == PROPOSAL
    assert runtime.call_count == generate.await_count == 2


def test_disconnect_cancels_generation_and_releases_slot(client, mocked_generation, monkeypatch):
    _, generate = mocked_generation
    started = threading.Event()
    cancelled = threading.Event()

    async def blocked(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def disconnected(request):
        while not started.is_set():
            await asyncio.sleep(0)
        return True

    generate.side_effect = blocked
    with monkeypatch.context() as patch:
        patch.setattr(Request, "is_disconnected", disconnected)
        _post(client, status=499, code="generation_cancelled")
    assert cancelled.wait(1)
    generate.side_effect = None
    assert _post(client).json() == PROPOSAL


def test_parent_task_cancellation_cleans_up_both_children_and_slot(
    client, mocked_generation,
):
    _, generate = mocked_generation

    async def run():
        started = asyncio.Event()
        watching = asyncio.Event()
        stopped = set()

        async def blocked(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.add("generation")

        async def disconnected():
            watching.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.add("watcher")

        generate.side_effect = blocked
        request = Mock(app=client.app, is_disconnected=disconnected)
        with client.app.state.session_factory() as session:
            service = WorkflowService(session, client.app.state.settings, client.app.state.crypto)
            task = asyncio.create_task(config_ai.generate(
                GenerateSiteDraftRequest.model_validate(_payload()), request, service,
            ))
            await asyncio.wait_for(started.wait(), 2)
            await asyncio.wait_for(watching.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert stopped == {"generation", "watcher"}

    with _read_only(client):
        asyncio.run(run())
    _assert_slot_free(client)
    generate.side_effect = None
    assert _post(client).json() == PROPOSAL


@pytest.fixture
def provider(client, monkeypatch):
    requests = []
    responses = []
    options = []

    async def handler(request):
        requests.append(request)
        assert responses, "Unexpected extra provider request"
        response = responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response if isinstance(response, httpx.Response) else httpx.Response(200, json=response)

    def factory(**kwargs):
        options.append(kwargs)
        return HTTPXAsyncClient(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(generation.httpx, "AsyncClient", factory)
    with client.app.state.session_factory() as session:
        integrations = IntegrationService(session, client.app.state.settings, client.app.state.crypto)
        for field in ("base_url", "model", "api_key"):
            integrations._upsert("vision." + field, CONFIG[field], "test")
        integrations._upsert("vision.supports_vision", "false", "test")
        session.commit()
    return responses, requests, options


def _tool_response(name, arguments, call_id="call"):
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "tool_calls": [{"id": call_id, "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments),
        }}],
    }}]}


def test_end_to_end_mock_httpx_schema_then_submit_without_persistence(client, provider):
    responses, requests, options = provider
    draft = {**EMPTY, "steps": [{
        "step_key": "wait", "feature_type": "action.wait", "config": {"seconds": 2},
    }]}
    responses.extend([
        _tool_response("get_feature_schema", {"feature_type": "action.wait"}, "schema"),
        _tool_response("submit_workflow_draft", {
            "draft": draft, "explanation": "Review before saving.", "unresolved_questions": [],
        }, "submit"),
    ])
    response = _post(client)
    result = GenerationResult.model_validate(response.json())
    assert result.draft.steps[0].config == {"seconds": 2}
    assert result.requires_review is True and result.status == "proposal"
    assert set(response.json()) == set(PROPOSAL)
    assert not responses and len(requests) == 2 and len(options) == 1
    assert options[0]["trust_env"] is False and options[0]["follow_redirects"] is False
    for request in requests:
        assert str(request.url) == CONFIG["base_url"] + "/chat/completions"
        assert request.headers["authorization"] == "Bearer " + KEY
        data = json.loads(request.content)
        assert data["model"] == CONFIG["model"]
        assert data["tool_choice"] == "auto" and data["stream"] is False
        for private in (KEY, "private-host-name", "host-password", "host-token", "host-fragment"):
            assert private not in request.content.decode()
            assert private not in response.text
    messages = json.loads(requests[-1].content)["messages"]
    schema = json.loads(next(message["content"] for message in messages if message["role"] == "tool"))
    assert schema["feature_type"] == "action.wait"
    assert "seconds" in schema["config_schema"]["properties"]


@pytest.mark.parametrize("location", ["requirements", "evidence", "provider_content", "provider_arguments"])
def test_known_runtime_key_is_rejected_without_echo(client, provider, location):
    responses, requests, _ = provider
    payload = _payload()
    if location in {"requirements", "evidence"}:
        payload[location] = "Accidental credential: " + KEY
        status, code = 422, "secret_input"
    else:
        output = _tool_response("submit_workflow_draft", {
            "draft": EMPTY, "explanation": KEY if location == "provider_arguments" else "Review.",
            "unresolved_questions": [],
        })
        if location == "provider_content":
            output["choices"][0]["message"]["reasoning_content"] = KEY
        responses.append(output)
        status, code = 502, "secret_output"
    _post(client, payload, status, code)
    assert len(requests) == (0 if status == 422 else 1)


@pytest.mark.parametrize("failure,code,status,reason", [
    (httpx.Response(500, text=PRIVATE + KEY), "provider_http", 502, None),
    (httpx.Response(200, text=PRIVATE), "invalid_response", 502, "invalid_json"),
    ({"choices": [{"finish_reason": "stop", "message": {"content": PRIVATE}}]},
     "invalid_response", 502, "no_tool_calls"),
    ({"choices": [{"finish_reason": "length", "message": {"content": PRIVATE}}]},
     "invalid_response", 502, "truncated"),
    (_tool_response(PRIVATE, {}), "invalid_response", 502, "invalid_tool_function"),
    (httpx.ConnectError(PRIVATE + KEY), "provider_network", 502, None),
    (httpx.ReadTimeout(PRIVATE + KEY), "provider_timeout", 504, None),
])
def test_provider_errors_are_generic_uncacheable_and_release_slot(
    client, provider, failure, code, status, reason,
):
    responses, requests, _ = provider
    responses.append(failure)
    _post(client, status=status, code=code, reason=reason)
    assert len(requests) == 1


@pytest.mark.parametrize("value,expected", [
    (None, False), ("true", True), ("1", True), ("false", False), ("0", False),
])
def test_config_ai_enabled_environment_parsing(monkeypatch, value, expected):
    monkeypatch.delenv("SITEFLOW_CONFIG_AI_ENABLED", raising=False)
    if value is not None:
        monkeypatch.setenv("SITEFLOW_CONFIG_AI_ENABLED", value)
    assert Settings(_env_file=None).config_ai_enabled is expected


def test_config_ai_enabled_rejects_invalid_environment_value(monkeypatch):
    monkeypatch.setenv("SITEFLOW_CONFIG_AI_ENABLED", "not-a-boolean")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
