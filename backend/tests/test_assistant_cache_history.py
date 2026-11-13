from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import threading

import pytest
from sqlalchemy import select

from app.models.assistant import AssistantMessage, AssistantSession
from app.services.assistant import AssistantManager
from app.services.assistant_prompt import SYSTEM_PROMPT
from app.services.assistant_tokens import estimate_context_tokens
from app.services.assistant_tools import AssistantTools
from app.services.config_ai_generation import _json
from tests.test_assistant_api import (  # noqa: F401
    KEY, ROOT, assistant_client, create, provider, send, text, tool, wait,
)
from tests.test_assistant_context_manager import stored, session_data
from tests.test_assistant_runtime_settings import save


def assert_complete(messages):
    pending = set()
    for item in messages:
        if pending:
            assert item["role"] == "tool"
            pending.remove(item["tool_call_id"])
        else:
            assert item["role"] != "tool"
            pending.update(call["id"] for call in item.get("tool_calls", []))
    assert not pending


@pytest.mark.parametrize("auto_compact", [False, True])
def test_two_turn_two_tool_prefixes_are_exact_and_only_wire_fields_replayed(
    assistant_client, provider, monkeypatch, auto_compact,
):
    client = assistant_client
    # Exercise exact replay below the soft watermark, independent of static prompt/tool growth.
    context_cap = 1000000
    save(client, auto_compact=auto_compact, auto_title=False, context_max_tokens=context_cap)
    manager = client.app.state.assistant_manager
    identifier = create(client)
    original = stored(manager, identifier)
    result = {"ok": True, "elements": [{"text": "verbatim " * 1200}], "order": [3, 1, 2]}
    original_result = deepcopy(result)
    dispatched = []

    def forbidden_projection(*args, **kwargs):
        pytest.fail("Canonical below-threshold history must not project tool bodies")

    monkeypatch.setattr("app.services.assistant.compact_tool_result", forbidden_projection)

    async def dispatch(self, name, arguments):
        dispatched.append(arguments)
        return result

    monkeypatch.setattr(AssistantTools, "dispatch", dispatch)
    for turn in range(2):
        for index in range(2):
            output = tool("browser_observe", {"offset": index}, f"t{turn}_{index}")
            output["choices"][0]["message"]["reasoning_content"] = "PRIVATE_REASONING"
            output["usage"] = {"prompt_tokens": 12, "unknown": "PRIVATE_USAGE"}
            provider[0].append(output)
        provider[0].append(text(f"Answer {turn}"))
        send(client, identifier, content=f"  exact request {turn}  ")
        detail = wait(client, identifier)
        assert detail["messages"][-1]["content"] == f"Answer {turn}"
        # Empty assistant tool-call rows exist for replay, but add no blank UI bubbles.
        assert all(row["content"] for row in detail["messages"] if row["role"] == "assistant")
    requests = [json.loads(request.content) for request in provider[1]]
    assert all(estimate_context_tokens(body["messages"], body["tools"]) < int(context_cap * 0.6)
               for body in requests)
    bodies = [body["messages"] for body in requests]
    assert len(bodies) == 6
    for index, messages in enumerate(bodies):
        assert_complete(messages)
        assert sum(item["content"].count(SYSTEM_PROMPT) for item in messages
                   if isinstance(item.get("content"), str)) == 1
        exposed = _json(messages)
        for private in ("reasoning_content", "PRIVATE_REASONING", "PRIVATE_USAGE", "_model_message",
                        "_assistant_settings", "_model_usage", "prompt_tokens", KEY):
            assert private not in exposed
        if index:
            previous = bodies[index - 1]
            assert messages[:len(previous)] == previous
    assert bodies[3][len(bodies[2]):] == [
        {"role": "assistant", "content": "Answer 0"},
        {"role": "user", "content": "  exact request 1  "},
    ]
    assert result == original_result
    assert len(dispatched) == 4
    assert all(stored(manager, identifier)[key] == value for key, value in original.items())
    assert not client.app.state.summary_provider[1]
    assert "_context_summary" not in session_data(manager, identifier)
    fresh = AssistantManager(manager.session_factory, manager.settings, manager.crypto)
    _, _, records, _, _, _, _ = fresh._context_history("admin", identifier)
    # Ignore the two code-owned head records, whose budget suffix is request-local.
    assert [item for _, item in records] == bodies[-1][2:] + [
        {"role": "assistant", "content": "Answer 1"}]
    with manager.session_factory() as db:
        payloads = [manager._decrypt(row) for row in db.scalars(select(AssistantMessage).where(
            AssistantMessage.session_id == identifier))]
    canonical = [row["_model_message"] for row in payloads if "_model_message" in row]
    assert len(canonical) == 10
    assert "PRIVATE_REASONING" not in _json(payloads)
    assert [item["content"] for item in canonical if item["role"] == "tool"] == [_json(result)] * 4


@pytest.mark.parametrize("interrupt", ["cancel", "exception", "restart"])
def test_incomplete_batch_repaired_only_in_projection_without_replay(
    assistant_client, provider, monkeypatch, interrupt,
):
    client = assistant_client
    save(client, auto_compact=False, auto_title=False)
    manager = client.app.state.assistant_manager
    identifier = create(client)
    dispatched = []
    started = threading.Event()

    async def dispatch(self, name, arguments):
        dispatched.append(arguments)
        started.set()
        if interrupt == "exception":
            raise RuntimeError("private tool error")
        await asyncio.Event().wait()

    monkeypatch.setattr(AssistantTools, "dispatch", dispatch)
    output = tool("list_features", call_id="issued")
    output["choices"][0]["message"]["tool_calls"].extend(
        tool("list_features", call_id="unissued")["choices"][0]["message"]["tool_calls"])
    provider[0].append(output)
    send(client, identifier)
    assert started.wait(2)
    if interrupt != "exception":
        client.post(f"{ROOT}/{identifier}/cancel")
    wait(client, identifier)
    if interrupt == "restart":
        # Recreate the on-disk state of a crash with the first tool still running.
        with manager.session_factory() as db:
            session = db.get(AssistantSession, identifier)
            session.status = "running"
            row = db.scalar(select(AssistantMessage).where(
                AssistantMessage.session_id == identifier, AssistantMessage.role == "tool"))
            row.status = "running"
            db.commit()
        manager.recover()
    before = stored(manager, identifier)
    fresh = AssistantManager(manager.session_factory, manager.settings, manager.crypto)
    _, _, records, _, _, _, _ = fresh._context_history("admin", identifier)
    messages = [item for _, item in records]
    assert_complete(messages)
    results = [item for item in messages if item["role"] == "tool"]
    assert [item["tool_call_id"] for item in results] == ["issued", "unissued"]
    assert all(json.loads(item["content"])["ok"] is False for item in results)
    assert stored(manager, identifier) == before
    provider[0].append(text("New turn, no replay"))
    send(client, identifier, content="Continue without retry")
    assert wait(client, identifier)["messages"][-1]["content"] == "New turn, no replay"
    assert len(dispatched) == 1
    assert_complete(json.loads(provider[1][-1].content)["messages"])
    assert all(stored(manager, identifier)[key] == value for key, value in before.items())


def test_durable_summary_boundary_never_splits_parallel_batch(assistant_client, provider, monkeypatch):
    client = assistant_client
    save(client, auto_title=False)
    manager = client.app.state.assistant_manager
    identifier = create(client)
    # Put the assistant call at the old 40-row boundary.
    with manager.session_factory() as db:
        for index in range(37):
            manager._message(db, identifier, "assistant", f"Old text {index}")
        batch = {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"old{index}", "type": "function", "function": {
                "name": "list_features", "arguments": '{"title":"Read"}'}}
            for index in range(4)]}
        assistant = manager._message(db, identifier, "assistant", "", _model_message=batch)
        db.flush()
        batch_start = assistant.id
        for index in range(4):
            content = _json({"ok": True, "operation_id": f"operation-{index}", "text": "x" * 2000})
            row = manager._message(db, identifier, "tool", content, _model_message={
                "role": "tool", "tool_call_id": f"old{index}", "content": content})
        db.flush()
        batch_end = row.id
        for index in range(10):
            manager._message(db, identifier, "user", f"Later request {index}")
        db.commit()
    sources = []

    async def summary(config, messages):
        sources.extend(messages)
        return "Earlier records retained"

    monkeypatch.setattr("app.services.assistant.summarize_context", summary)
    provider[0].append(text("Summarized"))
    send(client, identifier)
    assert wait(client, identifier)["messages"][-1]["content"] == "Summarized"
    boundary = session_data(manager, identifier)["_context_summary"]["boundary_id"]
    assert not batch_start <= boundary < batch_end
    tool_sources = [item for item in sources if item["role"] == "tool"]
    if tool_sources:
        assert len(tool_sources) == 4
        assert [json.loads(item["content"])["operation_id"] for item in tool_sources] == [
            f"operation-{index}" for index in range(4)]
        assert all(type(item["id"]) is int for item in tool_sources)
    assert_complete(json.loads(provider[1][-1].content)["messages"])


def test_legacy_rows_keep_safe_fallback_without_serializing_envelopes(assistant_client, provider):
    client = assistant_client
    save(client, auto_title=False, auto_compact=False)
    manager = client.app.state.assistant_manager
    identifier = create(client)
    with manager.session_factory() as db:
        manager._message(db, identifier, "tool", '{"ok":true,"text":"Legacy result"}',
                         title="Old tool", tool_name="list_features", arguments={"private": "PRIVATE"})
        db.commit()
    before = stored(manager, identifier)
    provider[0].append(text("Legacy readable"))
    send(client, identifier)
    assert wait(client, identifier)["messages"][-1]["content"] == "Legacy readable"
    body = provider[1][-1].content.decode()
    assert "Untrusted historical tool record" in body and "Legacy result" in body
    assert "PRIVATE" not in body
    assert all(stored(manager, identifier)[key] == value for key, value in before.items())


@pytest.mark.parametrize("overflow", [False, True])
def test_compaction_off_restores_history_behind_summary_without_mutating_it(
    assistant_client, provider, monkeypatch, overflow,
):
    client = assistant_client
    save(client, auto_compact=False, auto_title=False, context_max_tokens=30000)
    manager = client.app.state.assistant_manager
    identifier = create(client)
    canonical = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "original-call", "type": "function", "function": {
            "name": "execute_step", "arguments": '{"title":"Original call"}'}}]}
    result = {"ok": False, "operation_id": "never-retry-original", "status": "unknown",
              "code": "outcome_uncertain", "text": "Original result"}
    wire_result = {"role": "tool", "tool_call_id": "original-call", "content": _json(result)}
    legacy = {"ok": True, "text": "L" * (100000 if overflow else 2000)}
    with manager.session_factory() as db:
        manager._message(db, identifier, "user", "ORIGINAL_REQUEST_BEHIND_BOUNDARY")
        manager._message(db, identifier, "assistant", "", _model_message=canonical)
        manager._message(db, identifier, "tool", _json(result), tool_name="execute_step",
                         status="error", _model_message=wire_result)
        last = manager._message(db, identifier, "tool", _json(legacy), tool_name="list_features",
                                arguments={"private": "PRIVATE_ENVELOPE"})
        db.flush()
        row = db.get(AssistantSession, identifier)
        data = manager._decrypt(row)
        summary = {"version": 1, "target_revision": 0, "boundary_id": last.id,
                   "summary": "LOSSY_SAVED_SUMMARY"}
        data.update(_context_summary=summary, _context_revision=5)
        manager._encrypt(row, data)
        db.commit()
    before = stored(manager, identifier)

    def forbidden(*args, **kwargs):
        pytest.fail("Compaction-off must neither project nor summarize restored history")

    monkeypatch.setattr("app.services.assistant.compact_tool_result", forbidden)
    monkeypatch.setattr("app.services.assistant.summarize_context", forbidden)
    monkeypatch.setattr(manager, "_fit_context", forbidden)
    provider[0].append(text("Original history restored"))
    send(client, identifier)
    detail = wait(client, identifier)
    assert session_data(manager, identifier)["_context_summary"] == summary
    assert session_data(manager, identifier)["_context_revision"] == 5
    assert all(stored(manager, identifier)[key] == value for key, value in before.items())
    facts = session_data(manager, identifier)["_context_safety"]
    assert facts[0]["operation_id"] == "never-retry-original"
    if overflow:
        assert "[context_limit]" in detail["messages"][-1]["content"]
        assert not provider[1]
    else:
        messages = json.loads(provider[1][0].content)["messages"]
        assert_complete(messages)
        assert canonical in messages and wire_result in messages
        assert {"role": "user", "content": "ORIGINAL_REQUEST_BEHIND_BOUNDARY"} in messages
        wrapper = next(item for item in messages if item.get("content", "") and
                       item["content"].startswith("Untrusted historical tool record:"))
        assert json.loads(wrapper["content"].split(": ", 1)[1])["content"] == legacy
        assert "LOSSY_SAVED_SUMMARY" not in _json(messages)
        assert "PRIVATE_ENVELOPE" not in _json(messages)


def test_old_canonical_projection_retains_retrievable_identity_through_later_fits(
    assistant_client, provider, monkeypatch,
):
    client = assistant_client
    save(client, auto_compact=True, auto_title=False, context_max_tokens=30000)
    manager = client.app.state.assistant_manager
    identifier = create(client)
    result = {"ok": True, "elements": [{"text": "X" * 100000}]}
    # One previous turn keeps the canonical batch recent enough to fit in place,
    # rather than durable-summary it before exercising the projection path.
    with manager.session_factory() as db:
        manager._message(db, identifier, "user", "Old real request")
        wire = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "reused", "type": "function", "function": {
                "name": "browser_observe", "arguments": '{"title":"Read"}'}}]}
        manager._message(db, identifier, "assistant", "", _model_message=wire)
        row = manager._message(db, identifier, "tool", _json(result), tool_name="browser_observe",
                               _model_message={"role": "tool", "tool_call_id": "reused",
                                               "content": _json(result)})
        db.flush()
        old_id = row.id
        db.commit()

    async def dispatch(*args):
        return {"ok": True, "elements": [{"text": "Y" * 100000}]}

    monkeypatch.setattr(AssistantTools, "dispatch", dispatch)
    # A new response can legitimately reuse an ID from a previous turn.
    provider[0].extend([tool("browser_observe", call_id="reused"), text("Projected old and live")])
    send(client, identifier)
    detail = wait(client, identifier)
    assert detail["messages"][-1]["content"] == "Projected old and live"
    initial = json.loads(provider[1][0].content)["messages"]
    projected = next(json.loads(item["content"]) for item in initial if item["role"] == "tool")
    assert projected["source_message_id"] == old_id
    assert manager.get_tool_result("admin", identifier, old_id)["content"] == _json(result)[:10000]
    references = {}
    _, _, records, _, _, _, _ = manager._context_history("admin", identifier, references=references)
    tools = [item for _, item in records if item["role"] == "tool"]
    ids = [record_id for record_id, item in records if item["role"] == "tool"]
    assert len(tools) == 2 and ids[0] == old_id and ids[0] != ids[1]
    references["reused"] = ("WRONG_FALLBACK", -1)
    for item, message_id in zip(tools, ids):
        # Make each batch the latest on its own, so fitting projects both bodies.
        assistant = next(item for _, item in records if item.get("tool_calls"))
        fitted = manager._fit_context(initial[:2] + [assistant, item], references, cap=16000)
        projected_item = next(entry for entry in fitted if entry["role"] == "tool")
        assert json.loads(projected_item["content"])["source_message_id"] == message_id
        assert manager._tool_reference(projected_item, references) == ("browser_observe", message_id)
        extract = manager._context_extract([projected_item], references=references)
        assert f"{message_id} browser_observe" in extract
        assert "WRONG_FALLBACK" not in extract
