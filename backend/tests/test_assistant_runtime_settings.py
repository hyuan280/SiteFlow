from __future__ import annotations

import asyncio
import json
import threading
from uuid import uuid4

import pytest

from app.models.setting import AppSetting
from app.schemas.assistant_settings import AssistantSettingsUpdate, AssistantSettingsValues
from app.services import assistant as service
from app.services.assistant_settings import AssistantSettingsService, KEY as SETTINGS_KEY
from app.services.assistant_tools import AssistantTools
from app.services.integrations import IntegrationService
from tests.test_assistant_api import (  # noqa: F401
    CONFIG, KEY, ROOT, assistant_client, create, provider, send, text, tool, wait,
)


def save(client, **changes):
    with client.app.state.session_factory() as db:
        settings = AssistantSettingsService(db)
        view = settings.view()
        return settings.update(AssistantSettingsUpdate(
            expected_revision=view.revision,
            values=AssistantSettingsValues.model_validate({**view.values.model_dump(), **changes}),
        )).values


@pytest.fixture
def runtime_client(assistant_client):
    save(assistant_client, auto_title=False)
    return assistant_client


def batch(size, prefix="batch"):
    output = tool("list_features", call_id=f"{prefix}_0")
    calls = output["choices"][0]["message"]["tool_calls"]
    for index in range(1, size):
        calls.extend(tool("list_features", call_id=f"{prefix}_{index}")
                     ["choices"][0]["message"]["tool_calls"])
    return output


@pytest.mark.parametrize("raw", [
    "not json", '{"revision":1,"values":{}}',
    json.dumps({"revision": 1, "values": {
        **AssistantSettingsValues().model_dump(), "max_tool_calls": 0,
    }}),
    json.dumps({"revision": 1, "values": {
        **AssistantSettingsValues().model_dump(), "max_auth_uses_per_turn": None,
    }}),
])
def test_invalid_settings_fail_before_message_or_task(runtime_client, provider, raw):
    client = runtime_client
    identifier = create(client)
    before = client.get(f"{ROOT}/{identifier}").json()
    with client.app.state.session_factory() as db:
        db.get(AppSetting, SETTINGS_KEY).value_json = raw
        db.commit()
    response = send(client, identifier, status=503)
    assert response.json() == {"detail": {"code": "assistant_settings_invalid"}}
    assert client.get(f"{ROOT}/{identifier}").json() == before
    assert not client.app.state.assistant_manager.tasks
    assert not provider[1]


def test_settings_loaded_once_before_task_and_never_on_replay(runtime_client, provider, monkeypatch):
    client = runtime_client
    identifier = create(client)
    manager = client.app.state.assistant_manager
    loads = []
    original = AssistantSettingsService.load
    started = threading.Event()
    release = threading.Event()

    def load(settings):
        assert identifier not in manager.tasks
        loads.append(settings.session)
        return original(settings)

    async def blocked(request):
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.005)
        return text("Completed")

    monkeypatch.setattr(AssistantSettingsService, "load", load)
    provider[0].append(blocked)
    request_id = uuid4()
    send(client, identifier, request_id=request_id)
    assert started.wait(2)
    assert len(loads) == 1
    # A replay remains read-only even if persisted settings are now corrupt.
    with manager.session_factory() as db:
        db.get(AppSetting, SETTINGS_KEY).value_json = "invalid settings"
        db.commit()

    def forbidden(*args):
        pytest.fail("A request-id replay must not read runtime or settings")

    monkeypatch.setattr(manager, "_runtime", forbidden)
    monkeypatch.setattr(AssistantSettingsService, "load", forbidden)
    assert send(client, identifier, request_id=request_id).json()["status"] == "running"
    release.set()
    detail = wait(client, identifier)
    assert send(client, identifier, request_id=request_id).json() == detail
    assert len(provider[1]) == 1


@pytest.mark.parametrize("global_tokens,override,expected", [
    (None, None, None), (1777, None, 1777), (1777, 0, None),
    (None, 2345, 2345), (1777, 9007199254740991, 9007199254740991),
])
def test_token_snapshot_used_for_conversation_summary_and_title_without_mutating_runtime(
    runtime_client, provider, monkeypatch, global_tokens, override, expected,
):
    client = runtime_client
    config = dict(CONFIG)
    if global_tokens is not None:
        config["max_tokens"] = global_tokens
    original = dict(config)
    monkeypatch.setattr(IntegrationService, "model_runtime", lambda self, usage, *, requires_vision=False:
                        config if usage == "assistant"
                        and (not requires_vision or config["supports_vision"]) else None)
    save(client, max_tokens=override, auto_title=True)
    identifier = create(client)
    manager = client.app.state.assistant_manager
    # Many assistant records trigger a summary without excluding automatic titles.
    with manager.session_factory() as db:
        for index in range(48):
            manager._message(db, identifier, "assistant", f"Historical response {index}")
        db.commit()
    provider[0].append(text("Main reply"))
    client.app.state.title_provider[0].append(text('{"title":"Generated title"}'))
    send(client, identifier)
    detail = wait(client, identifier)
    assert detail["title"] == "Generated title"
    summary_requests = client.app.state.summary_provider[1]
    title_requests = client.app.state.title_provider[1]
    assert summary_requests and title_requests
    for request in provider[1] + summary_requests + title_requests:
        payload = json.loads(request.content)
        if expected is None:
            assert "max_tokens" not in payload
        else:
            assert payload["max_tokens"] == expected
        assert "_assistant_settings" not in request.content.decode()
        assert KEY not in request.content.decode()
    assert config == original


@pytest.mark.parametrize("parallel,size,success", [(1, 2, False), (4, 5, False), (8, 8, True), (8, 9, False)])
def test_configured_per_response_limit_is_atomic(runtime_client, provider, monkeypatch, parallel, size, success):
    client = runtime_client
    save(client, max_parallel_tool_calls=parallel)
    identifier = create(client)
    dispatched = []

    async def dispatch(self, name, args):
        dispatched.append(name)
        return {"ok": True}

    monkeypatch.setattr(AssistantTools, "dispatch", dispatch)
    response = batch(size)
    response["choices"][0]["message"]["content"] = "Batch accepted"
    provider[0].append(response)
    if success:
        provider[0].append(text("Complete"))
    send(client, identifier)
    detail = wait(client, identifier)
    if success:
        assert detail["messages"][-1]["content"] == "Complete"
        assert len(dispatched) == size
        assert f"max_parallel_tool_calls={parallel}" in json.loads(
            provider[1][0].content)["messages"][0]["content"]
    else:
        assert "too_many_parallel_tools" in detail["messages"][-1]["content"]
        assert not dispatched
        assert [item["role"] for item in detail["messages"]] == ["assistant", "user", "system"]


@pytest.mark.parametrize("setting,limit,batches,expected_calls,expected_requests,error", [
    ("max_tool_calls", 3, [2, 2], 2, 2, "tool_limit"),
    ("max_model_rounds", 2, [1, 1], 2, 2, "round_limit"),
    ("max_tool_calls", 24, [8, 8, 8, 1], 24, 4, "tool_limit"),
    ("max_model_rounds", 14, [1] * 14, 14, 14, "round_limit"),
])
def test_turn_limits_count_all_dispatches_without_replay(
    runtime_client, provider, monkeypatch, setting, limit, batches, expected_calls, expected_requests, error,
):
    client = runtime_client
    save(client, **{setting: limit}, max_parallel_tool_calls=8)
    identifier = create(client)
    dispatched = []

    async def dispatch(*args):
        dispatched.append(1)
        return {"ok": True}

    monkeypatch.setattr(AssistantTools, "dispatch", dispatch)
    for index, size in enumerate(batches):
        provider[0].append(batch(size, prefix=f"round{index}"))
    send(client, identifier)
    detail = wait(client, identifier)
    assert f"[{error}]" in detail["messages"][-1]["content"]
    assert len(dispatched) == expected_calls
    assert len(provider[1]) == expected_requests
    assert len([item for item in detail["messages"] if item["role"] == "tool"]) == expected_calls


def test_auto_title_disabled_leaves_manual_rename_available(runtime_client, provider):
    client = runtime_client
    identifier = create(client)
    provider[0].append(text("Reply"))
    send(client, identifier)
    detail = wait(client, identifier)
    assert not client.app.state.title_provider[1]
    assert detail["title_revision"] == 0
    renamed = client.put(f"{ROOT}/{identifier}/title", json={"title": "Manual title"}).json()
    assert renamed["title"] == "Manual title" and renamed["title_locked"] is True


@pytest.mark.parametrize("turn_timeout,provider_timeout,expected", [(30, 900, 30), (600, 900, 600), (600, 45, 45)])
def test_configured_turn_deadline_and_provider_timeout(
    runtime_client, provider, monkeypatch, turn_timeout, provider_timeout, expected,
):
    client = runtime_client
    save(client, turn_timeout_seconds=turn_timeout)
    monkeypatch.setattr(IntegrationService, "model_runtime", lambda self, usage, *, requires_vision=False:
                        {**CONFIG, "timeout": provider_timeout} if usage == "assistant"
                        and (not requires_vision or CONFIG["supports_vision"]) else None)
    deadlines = []
    original = asyncio.timeout_at

    def capture(deadline):
        deadlines.append(deadline - asyncio.get_running_loop().time())
        return original(deadline)

    monkeypatch.setattr(service.asyncio, "timeout_at", capture)
    provider[0].append(text("Complete"))
    identifier = create(client)
    send(client, identifier)
    assert wait(client, identifier)["messages"][-1]["content"] == "Complete"
    assert deadlines[0] == pytest.approx(turn_timeout, abs=0.1)
    assert provider[2][0]["timeout"].read == expected


def test_overlapping_turns_keep_independent_snapshots(runtime_client, provider, monkeypatch):
    client = runtime_client
    save(client, max_tool_calls=2, max_parallel_tool_calls=2, max_model_rounds=3,
         turn_timeout_seconds=40, context_max_tokens=32000, max_tokens=111, auto_compact=False)
    manager = client.app.state.assistant_manager
    first, second = create(client), create(client)
    started, release = threading.Event(), threading.Event()
    snapshots, dispatched = {}, []
    original = manager._conversation

    async def conversation(owner, identifier, config):
        snapshots[identifier] = dict(config["_assistant_settings"])
        await original(owner, identifier, config)

    async def blocked(request):
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.005)
        return batch(2, "first")

    async def dispatch(*args):
        dispatched.append(1)
        return {"ok": True}

    monkeypatch.setattr(manager, "_conversation", conversation)
    monkeypatch.setattr(AssistantTools, "dispatch", dispatch)
    provider[0].append(blocked)
    send(client, first)
    assert started.wait(2)
    updated = save(client, max_tool_calls=1, max_parallel_tool_calls=1, max_model_rounds=1,
                   turn_timeout_seconds=90, context_max_tokens=100000, max_tokens=222, auto_compact=True,
                   max_auth_uses_per_turn=7)
    provider[0].append(text("Second complete"))
    send(client, second)
    assert wait(client, second)["messages"][-1]["content"] == "Second complete"
    provider[0].append(text("First complete"))
    release.set()
    assert wait(client, first)["messages"][-1]["content"] == "First complete"
    assert snapshots[first]["max_tool_calls"] == 2 and snapshots[first]["auto_compact"] is False
    assert snapshots[first]["context_max_tokens"] == 32000
    assert snapshots[first]["max_auth_uses_per_turn"] == 3
    assert snapshots[second] == updated.model_dump()
    assert snapshots[second]["max_auth_uses_per_turn"] == 7
    assert len(dispatched) == 2
    assert [json.loads(request.content)["max_tokens"] for request in provider[1]] == [111, 222, 111]
    # The next turn in the original session picks up the newly saved settings.
    provider[0].append(text("Next complete"))
    send(client, first)
    assert wait(client, first)["messages"][-1]["content"] == "Next complete"
    assert snapshots[first] == updated.model_dump()


def test_turn_timeout_exhaustion_releases_task_and_persists_safe_error(
    runtime_client, provider, monkeypatch,
):
    client = runtime_client
    save(client, turn_timeout_seconds=30)
    original = asyncio.timeout_at
    cancelled = threading.Event()

    def shortened(deadline):
        assert 29 < deadline - asyncio.get_running_loop().time() <= 30
        return original(asyncio.get_running_loop().time() + 0.05)

    async def blocked(request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(service.asyncio, "timeout_at", shortened)
    provider[0].append(blocked)
    identifier = create(client)
    send(client, identifier)
    detail = wait(client, identifier)
    assert "[provider_timeout]" in detail["messages"][-1]["content"]
    assert cancelled.is_set() and not client.app.state.assistant_manager.tasks
    assert len(provider[1]) == 1


def test_large_token_context_reaches_provider_above_previous_request_cap(
    runtime_client, provider, monkeypatch,
):
    client = runtime_client
    save(client, context_max_tokens=1000000, auto_compact=False)
    manager = client.app.state.assistant_manager
    identifier = create(client)
    original = manager._prepare_context

    async def large_context(*args):
        target, messages, users, safety, revision, references = await original(*args)
        messages.insert(2, {"role": "assistant", "content": "x" * 900000})
        return target, messages, users, safety, revision, references

    monkeypatch.setattr(manager, "_prepare_context", large_context)
    provider[0].append(text("Large context accepted"))
    send(client, identifier)
    assert wait(client, identifier)["messages"][-1]["content"] == "Large context accepted"
    assert len(provider[1]) == 1
    request = provider[1][0]
    assert 512 * 1024 < len(request.content) < 1536 * 1024
    assert len(json.dumps(json.loads(request.content)["messages"])) <= 1000000
