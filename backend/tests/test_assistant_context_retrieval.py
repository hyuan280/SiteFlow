from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, Mock, call

from jsonschema import Draft202012Validator
import pytest
from sqlalchemy import select

from app.models.assistant import AssistantMessage, AssistantSession
from app.schemas.assistant import CreateSession
from app.services.assistant import AssistantError, AssistantManager
from app.services.assistant_execution import AssistantExecution
from app.services.assistant_prompt import SYSTEM_PROMPT, tool_definitions
from app.services.assistant_tools import AssistantTools
from app.services.config_ai_generation import _json
from tests.test_assistant_api import (  # noqa: F401
    assistant_client, create, provider, send, target, text, title_provider, tool, wait,
)


@pytest.fixture
def original_record(assistant_client):
    manager = assistant_client.app.state.assistant_manager
    identifier = create(assistant_client)
    result = {"ok": True, "text": "A\u6d4b\U0001f642\n\"\\" * 3000, "tail": "Original end"}
    content = json.dumps(result, ensure_ascii=False, indent=2)
    with manager.session_factory() as db:
        row = manager._message(
            db, identifier, "tool", content, status="success", title="Original observation",
            tool_name="browser_observe", arguments={"private": "PRIVATE_ARGUMENTS"},
            _credential_binding="PRIVATE_BINDING", token="PRIVATE_TOKEN",
        )
        db.commit()
        return identifier, row.id, content


def stored_messages(manager, identifier):
    with manager.session_factory() as db:
        return {row.id: (row.ciphertext, row.nonce, row.key_version, row.status)
                for row in db.scalars(select(AssistantMessage).where(
                    AssistantMessage.session_id == identifier))}


@pytest.fixture
def dispatch_context(client):
    manager = client.app.state.assistant_manager
    sid = manager.create("host-owner", CreateSession())["id"]
    return manager, sid


def test_retrieval_protocol_schema_and_no_replay_prompt():
    definitions = tool_definitions()
    names = [item["function"]["name"] for item in definitions]
    assert len(names) == len(set(names)) == 39
    for definition in definitions:
        assert definition["type"] == "function"
        Draft202012Validator.check_schema(definition["function"]["parameters"])
    definition = next(item["function"] for item in definitions
                      if item["function"]["name"] == "get_tool_result")
    schema = definition["parameters"]
    assert schema == {
        "type": "object", "additionalProperties": False,
        "required": ["message_id", "title"],
        "properties": {
            "message_id": {"type": "integer", "minimum": 1},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 10000},
            "title": {"type": "string", "minLength": 1, "maxLength": 120},
        },
    }
    validator = Draft202012Validator(schema)
    for args in ({"message_id": 1}, {"message_id": 1, "offset": 0, "limit": 1},
                 {"message_id": 1, "offset": 20000, "limit": 10000}):
        validator.validate({"title": "Recover original evidence", **args})
    for args in ({}, {"message_id": True}, {"message_id": 0},
                 {"message_id": 1, "offset": -1}, {"message_id": 1, "limit": 0},
                 {"message_id": 1, "limit": 10001}, {"message_id": 1, "owner": "other"}):
        assert not validator.is_valid({"title": "Read", **args})
    assert "character page" in definition["description"]
    assert "no browser action or replay" in definition["description"]
    section = SYSTEM_PROMPT.split("CONTEXT ECONOMY:\n", 1)[1].split("\n\n", 1)[0]
    assert "get_tool_result(message_id, offset, limit)" in section
    assert "without rerunning it" in section
    assert "character page of the original JSON" in section
    assert "joining across pages" in section
    assert "Never re-execute an action merely because its details were compacted" in section


@pytest.mark.parametrize("arguments", [
    None, [], "not an object", True, {},
    *[{"message_id": value} for value in (True, False, None, "1", 1.0, 0, -1, [], {})],
    *[{"message_id": 1, "offset": value}
      for value in (True, False, None, "0", 0.0, -1, [], {})],
    *[{"message_id": 1, "limit": value}
      for value in (True, False, None, "1", 1.0, 0, -1, 10001, [], {})],
    *[{"message_id": 1, key: "PRIVATE"}
      for key in ("owner", "session_id", "arguments", "_credential_binding", "title", "extra")],
])
def test_dispatch_rejects_malformed_arguments_before_manager_read(dispatch_context, arguments, monkeypatch):
    manager, sid = dispatch_context
    read = Mock()
    monkeypatch.setattr(manager, "get_tool_result", read)
    tools = AssistantTools(manager, "host-owner", sid, None)
    assert asyncio.run(tools.dispatch("get_tool_result", arguments)) == {
        "ok": False, "code": "invalid_arguments",
    }
    read.assert_not_called()


@pytest.mark.parametrize("arguments", [
    {"message_id": 1}, {"message_id": 3, "offset": 0, "limit": 1},
    {"message_id": 3, "offset": 20000, "limit": 10000},
])
def test_dispatch_passes_exact_arguments_and_host_scope_without_target(dispatch_context, arguments, monkeypatch):
    manager, sid = dispatch_context
    result = {"ok": True, "content": "original page"}
    read = Mock(return_value=result)
    monkeypatch.setattr(manager, "get_tool_result", read)
    tools = AssistantTools(manager, "host-owner", sid, None)
    assert asyncio.run(tools.dispatch("get_tool_result", arguments)) is result
    read.assert_called_once_with("host-owner", sid, **arguments)


@pytest.mark.parametrize("error", [
    AssistantError("tool_result_not_found", 404), RuntimeError("PRIVATE_INTERNAL_FAILURE"),
])
def test_dispatch_unavailable_result_does_not_expose_exception(dispatch_context, error, monkeypatch):
    manager, sid = dispatch_context
    read = Mock(side_effect=error)
    monkeypatch.setattr(manager, "get_tool_result", read)
    tools = AssistantTools(manager, "host-owner", sid, None)
    assert asyncio.run(tools.dispatch("get_tool_result", {"message_id": 1})) == {
        "ok": False, "code": "tool_result_unavailable",
    }
    read.assert_called_once_with("host-owner", sid, message_id=1)


@pytest.mark.parametrize("changes", [
    {"message_id": True}, {"message_id": "1"}, {"message_id": 1.0},
    *[{"offset": value} for value in (True, False, "0", None, 0.0, -1)],
    *[{"limit": value} for value in (True, False, "1", None, 1.0, 0, 10001)],
])
def test_manager_rejects_invalid_page_types_before_decryption(
    assistant_client, original_record, monkeypatch, changes,
):
    manager = assistant_client.app.state.assistant_manager
    identifier, message_id, _ = original_record
    decrypt = Mock(wraps=manager._decrypt)
    monkeypatch.setattr(manager, "_decrypt", decrypt)
    with pytest.raises(AssistantError) as error:
        manager.get_tool_result("admin", identifier, **{"message_id": message_id, **changes})
    assert error.value.code == "invalid_arguments" and error.value.status == 422
    decrypt.assert_not_called()


@pytest.mark.parametrize("scope", ["other_owner", "other_session", "foreign_session",
                                  "missing_session", "non_tool", "missing_message"])
def test_unavailable_records_are_never_decrypted(
    assistant_client, original_record, monkeypatch, scope,
):
    manager = assistant_client.app.state.assistant_manager
    identifier, message_id, _ = original_record
    owner = "admin"
    if scope == "other_owner":
        owner = "other-admin"
    elif scope in {"other_session", "foreign_session"}:
        identifier = create(assistant_client)
        if scope == "foreign_session":
            with manager.session_factory() as db:
                db.get(AssistantSession, identifier).owner = "other-admin"
                db.commit()
    elif scope == "missing_session":
        identifier = "missing-session"
    elif scope == "non_tool":
        with manager.session_factory() as db:
            message_id = db.scalar(select(AssistantMessage.id).where(
                AssistantMessage.session_id == identifier, AssistantMessage.role == "assistant"))
    else:
        message_id += 1000000
    before = stored_messages(manager, original_record[0])
    decrypt = Mock(wraps=manager._decrypt)
    monkeypatch.setattr(manager, "_decrypt", decrypt)
    with pytest.raises(AssistantError) as error:
        manager.get_tool_result(owner, identifier, message_id)
    assert error.value.status == 404
    decrypt.assert_not_called()
    tools = AssistantTools(manager, owner, identifier, None)
    assert asyncio.run(tools.dispatch("get_tool_result", {"message_id": message_id})) == {
        "ok": False, "code": "tool_result_unavailable",
    }
    # Dispatch may decrypt the caller's owned session for persisted takeover policy,
    # but must never decrypt an unavailable message or a foreign session.
    assert all(isinstance(args.args[0], AssistantSession) and args.args[0].id == identifier
               and args.args[0].owner == owner for args in decrypt.call_args_list)
    assert stored_messages(manager, original_record[0]) == before


def test_character_pages_reconstruct_exact_stored_json_without_envelope_or_side_effects(
    assistant_client, original_record, provider, title_provider, monkeypatch,
):
    manager = assistant_client.app.state.assistant_manager
    identifier, message_id, content = original_record
    before = stored_messages(manager, identifier)
    decrypt = Mock(wraps=manager._decrypt)
    browser, execution = AsyncMock(), AsyncMock()
    monkeypatch.setattr(manager, "_decrypt", decrypt)
    monkeypatch.setattr(manager, "browser_tool", browser)
    monkeypatch.setattr(AssistantExecution, "execute", execution)
    tools = AssistantTools(manager, "admin", identifier, None)
    assert len(content.encode()) > len(content)
    first = manager.get_tool_result("admin", identifier, message_id)
    assert first == {
        "ok": True, "message_id": message_id, "tool_name": "browser_observe",
        "title": "Original observation", "status": "success", "offset": 0,
        "limit": 10000, "total": len(content), "next_offset": 10000,
        "content": content[:10000],
    }

    async def pages():
        parts, offset = [], 0
        while offset is not None:
            page = await tools.dispatch("get_tool_result", {
                "message_id": message_id, "offset": offset, "limit": 997,
            })
            assert set(page) == set(first)
            assert page["message_id"] == message_id and page["total"] == len(content)
            assert page["offset"] == offset and page["limit"] == 997
            assert page["content"] == content[offset:offset + 997]
            end = min(offset + 997, len(content))
            assert page["next_offset"] == (end if end < len(content) else None)
            parts.append(page["content"])
            assert "PRIVATE" not in _json(page)
            offset = page["next_offset"]
        return "".join(parts)

    recovered = asyncio.run(pages())
    assert recovered == content and json.loads(recovered) == json.loads(content)
    one = manager.get_tool_result("admin", identifier, message_id, 1, 1)
    assert one["content"] == content[1:2] and one["next_offset"] == 2
    for offset in (len(content), len(content) + 100):
        page = manager.get_tool_result("admin", identifier, message_id, offset, 10000)
        assert page["content"] == "" and page["next_offset"] is None
        assert page["offset"] == offset and page["total"] == len(content)
    assert decrypt.call_count > 1
    assert all((isinstance(args.args[0], AssistantMessage) and args.args[0].id == message_id)
               or (isinstance(args.args[0], AssistantSession) and args.args[0].id == identifier)
               for args in decrypt.call_args_list)
    browser.assert_not_awaited()
    execution.assert_not_awaited()
    assert not provider[1] and not title_provider[1]
    assert not assistant_client.app.state.summary_provider[1]
    assert stored_messages(manager, identifier) == before


def test_dispatch_retrieves_original_after_live_and_durable_compaction_and_reopen(
    assistant_client, provider, title_provider, monkeypatch,
):
    client = assistant_client
    manager = client.app.state.assistant_manager
    identifier = create(client)
    target(client, identifier)
    result = {"ok": True, "snapshot_id": "retained-snapshot", "frame_id": "main",
              "elements": [{"node_id": "n1", "text": "original evidence " * 7000}]}
    browser = AsyncMock(return_value=result)
    execution = AsyncMock()
    dispatch_tool = AsyncMock(side_effect=AssistantTools.dispatch)

    async def dispatch(self, name, arguments):
        return await dispatch_tool(self, name, arguments)

    monkeypatch.setattr(manager, "browser_tool", browser)
    monkeypatch.setattr(AssistantExecution, "execute", execution)
    monkeypatch.setattr(AssistantTools, "dispatch", dispatch)
    batch = tool("browser_observe", call_id="original")
    batch["choices"][0]["message"]["tool_calls"].extend(
        tool("browser_observe", call_id="second")["choices"][0]["message"]["tool_calls"])
    provider[0].extend([batch, text("Observations retained")])
    title_provider[0].append(text('{"title":"Retained evidence"}'))
    send(client, identifier, content="Inspect the approved page")
    first = wait(client, identifier)
    originals = [row for row in first["messages"] if row["role"] == "tool"]
    assert len(originals) == 2
    original = originals[0]
    message_id = original["id"]
    assert original["content"] == _json(result) and original["status"] == "success"
    live = json.loads(provider[1][-1].content)["messages"]
    projected = json.loads(next(row["content"] for row in live
                                if row.get("tool_call_id") == "original"))
    assert projected["context_compacted"] is True
    assert projected["source_message_id"] == message_id
    assert "get_tool_result" in projected["notice"]
    before = stored_messages(manager, identifier)
    with manager.session_factory() as db:
        for index in range(80):
            manager._message(db, identifier, "user", f"Later request {index}: " + "z" * 2500)
        db.commit()
    summaries, summary_requests = client.app.state.summary_provider
    for _ in range(2):
        response = text('{"summary":"Original observations remain available in stored tool records."}')
        response["choices"][0]["message"].pop("reasoning_content")
        summaries.append(response)
    arguments = {"message_id": message_id, "offset": 57, "limit": 999}
    provider[0].extend([
        tool("get_tool_result", {"title": "Recover original observation", **arguments}, "retrieve"),
        text("Original evidence recovered without another browser action"),
    ])
    send(client, identifier, content="Recover the earlier observation, do not repeat it")
    detail = wait(client, identifier)
    assert detail["messages"][-1]["content"] == "Original evidence recovered without another browser action"
    assert detail["context"]["compressed"] is True
    assert detail["context"]["summary_through_message_id"] >= message_id
    assert len(summary_requests) == 2 and not summaries
    assert len(title_provider[1]) == 1 and not title_provider[0]
    assert detail["title"] == "Retained evidence"
    assert not provider[0]
    assert all("chat/completions" in str(request.url) for request in provider[1])
    assert [args.args[1:] for args in dispatch_tool.await_args_list] == [
        ("browser_observe", {}), ("browser_observe", {}), ("get_tool_result", arguments),
    ]
    assert browser.await_args_list == [
        call("admin", identifier, first["target"], "browser_observe", None, {}),
        call("admin", identifier, first["target"], "browser_observe", None, {}),
    ]
    execution.assert_not_awaited()
    assert next(row for row in detail["messages"] if row["id"] == message_id) == original
    page_row = next(row for row in detail["messages"] if row.get("tool_name") == "get_tool_result")
    assert page_row["id"] != message_id and page_row["status"] == "success"
    page = json.loads(page_row["content"])
    assert page["message_id"] == message_id
    assert page["tool_name"] == "browser_observe" and page["title"] == original["title"]
    assert page["content"] == original["content"][57:1056]
    model = json.loads(provider[1][-1].content)["messages"]
    assert next(row["content"] for row in model
                if row.get("tool_call_id") == "retrieve") == page_row["content"]
    fresh = AssistantManager(manager.session_factory, manager.settings, manager.crypto)
    assert fresh.detail("admin", identifier) == detail
    with fresh.session_factory() as db:
        summary = fresh._decrypt(db.get(AssistantSession, identifier))["_context_summary"]
        assert summary["boundary_id"] >= message_id
        assert summary["summary"] == "Original observations remain available in stored tool records."
    _, _, records, _, _, _, _ = fresh._context_history("admin", identifier)
    assert message_id not in {record_id for record_id, _ in records}
    request_counts = (len(provider[1]), len(title_provider[1]), len(summary_requests))
    parts, offset = [], 0
    while offset is not None:
        page = fresh.get_tool_result("admin", identifier, message_id, offset, 10000)
        assert page["message_id"] == message_id
        parts.append(page["content"])
        offset = page["next_offset"]
    assert "".join(parts) == original["content"]
    assert json.loads("".join(parts)) == result
    assert request_counts == (len(provider[1]), len(title_provider[1]), len(summary_requests))
    assert all(stored_messages(manager, identifier)[key] == value for key, value in before.items())
