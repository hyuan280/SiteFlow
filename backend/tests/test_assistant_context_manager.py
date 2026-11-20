from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import threading
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.models.assistant import AssistantMessage, AssistantSession
from app.schemas.assistant_settings import AssistantSettingsUpdate, AssistantSettingsValues
from app.services import assistant as service
from app.services.assistant import AssistantError, AssistantManager
from app.services.assistant_settings import AssistantSettingsService
from app.services.assistant_tools import AssistantTools
from app.services.assistant_tokens import estimate_context_tokens
from app.services.config_ai_generation import _json
from tests.test_assistant_api import (  # noqa: F401
    CONFIG, KEY, ROOT, assistant_client, create, provider, send, target, text, tool, wait,
)


def stored(manager, identifier):
    with manager.session_factory() as db:
        return {row.id: (row.ciphertext, row.nonce, row.key_version, row.status)
                for row in db.scalars(select(AssistantMessage).where(
                    AssistantMessage.session_id == identifier).order_by(AssistantMessage.id))}


def seed(manager, identifier, *, rows=48, size=1000):
    with manager.session_factory() as db:
        session = db.get(AssistantSession, identifier)
        data = manager._decrypt(session)
        data.update(title="Retained title", title_locked=True, title_revision=7,
                    _execution_operations=["never-replay-operation"])
        manager._encrypt(session, data)
        ids = []
        for index in range(rows):
            row = manager._message(db, identifier, "user", f"Request {index}: " + "x" * size)
            db.flush()
            ids.append(row.id)
        db.commit()
        return ids


def session_data(manager, identifier):
    with manager.session_factory() as db:
        return manager._decrypt(db.get(AssistantSession, identifier))


@pytest.fixture
def context_settings(assistant_client):
    def save(**changes):
        with assistant_client.app.state.session_factory() as db:
            settings = AssistantSettingsService(db)
            view = settings.view()
            settings.update(AssistantSettingsUpdate(
                expected_revision=view.revision,
                values=AssistantSettingsValues.model_validate({
                    **view.values.model_dump(), "auto_title": False, **changes,
                }),
            ))
    return save


def assert_requests(requests, latest):
    for request in requests:
        body = json.loads(request.content)
        messages = body["messages"]
        assert estimate_context_tokens(messages, body["tools"]) <= 64000
        assert len(request.content) <= 1536 * 1024
        assert body["tools"] == [definition for definition in service.tool_definitions()
                                 if definition["function"]["name"] != "submit_takeover_conclusion"]
        assert messages[0]["role"] == messages[1]["role"] == "system"
        assert {"role": "user", "content": latest} in messages
        pending = set()
        for item in messages:
            if pending:
                assert item["role"] == "tool"
                assert item["tool_call_id"] in pending
                pending.remove(item["tool_call_id"])
            else:
                assert item["role"] != "tool"
                pending.update(call["id"] for call in item.get("tool_calls", []))
        assert not pending


def test_image_fitting_uses_tokens_not_base64_bytes_and_retains_latest(assistant_client):
    from app.services.assistant_images import image_message

    manager = assistant_client.app.state.assistant_manager
    image = {"mime_type": "image/jpeg", "image_base64": "A" * 100000,
             "width": 100, "height": 100, "masked": True}
    old = image_message(image, "image_id=old")
    latest = image_message(image, "image_id=latest")
    user = {"role": "user", "content": "actual host user"}
    messages = [{"role": "system", "content": "system"}] * 2 + [user, old, latest]
    estimate = estimate_context_tokens(messages)
    assert estimate < 1000
    assert manager._fit_context(messages, protected_users=[user], limit=1000) == messages
    fitted = manager._fit_context(messages, protected_users=[user], limit=estimate - 1)
    assert latest in fitted and user in fitted and old not in fitted
    assert any(isinstance(item.get("content"), str) and "image_id=old" in item["content"]
               for item in fitted)


@pytest.mark.parametrize("parallel", [1, 4])
def test_large_dom_rounds_fit_without_replay_or_history_mutation(
    assistant_client, provider, monkeypatch, parallel,
):
    client = assistant_client
    manager = client.app.state.assistant_manager
    identifier = create(client)
    client.put(f"{ROOT}/{identifier}/title", json={"title": "No title calls"})
    original = stored(manager, identifier)
    dispatched = []
    result = {"ok": True, "snapshot_id": "snapshot", "frame_id": "main",
              "elements": [{"node_id": f"n{i}", "text": "DOM" * 1900,
                            "selector": f"#n{i}"} for i in range(22)]}
    assert 120000 < len(_json(result)) < 128 * 1024

    async def dispatch(self, name, args):
        dispatched.append(args["offset"])
        return result

    monkeypatch.setattr(AssistantTools, "dispatch", dispatch)
    rounds = 4
    for index in range(rounds):
        response = tool("browser_observe", {"offset": index * parallel}, f"c{index}_0")
        calls = response["choices"][0]["message"]["tool_calls"]
        for offset in range(1, parallel):
            calls.extend(tool("browser_observe", {"offset": index * parallel + offset},
                              f"c{index}_{offset}")["choices"][0]["message"]["tool_calls"])
        provider[0].append(response)
    provider[0].append(text("Finished all DOM rounds"))
    latest = "Keep this exact latest request: " + "\u6d4b" * 11900
    send(client, identifier, content=latest)
    detail = wait(client, identifier)
    assert detail["messages"][-1]["content"] == "Finished all DOM rounds"
    assert dispatched == list(range(rounds * parallel))
    assert_requests(provider[1], latest)
    logs = [row for row in detail["messages"] if row["role"] == "tool"]
    assert len(logs) == rounds * parallel
    assert all(json.loads(row["content"]) == result for row in logs)
    assert all(stored(manager, identifier)[key] == value for key, value in original.items())
    assert "_context_summary" not in session_data(manager, identifier)
    if parallel == 4:
        outbound = json.loads(provider[1][1].content)["messages"]
        projected = [json.loads(row["content"]) for row in outbound if row["role"] == "tool"]
        assert {row["source_message_id"] for row in projected} == {row["id"] for row in logs[:4]}


def test_small_live_context_is_verbatim(assistant_client, provider, monkeypatch):
    client = assistant_client
    identifier = create(client)
    result = {"ok": True, "elements": [{"node_id": "n1", "text": "Small DOM"}]}

    async def dispatch(*args):
        return result

    monkeypatch.setattr(AssistantTools, "dispatch", dispatch)
    provider[0].extend([tool("browser_observe", call_id="one"),
                        tool("browser_observe", call_id="two"), text("Complete")])
    send(client, identifier)
    assert wait(client, identifier)["messages"][-1]["content"] == "Complete"
    messages = json.loads(provider[1][-1].content)["messages"]
    assert [item["content"] for item in messages if item["role"] == "tool"] == [_json(result)] * 2


def test_live_projection_rechecks_token_budget_without_resampling(assistant_client, monkeypatch):
    manager = assistant_client.app.state.assistant_manager
    user = {"role": "user", "content": "Keep this original request"}
    calls = [{"id": f"call-{index}", "type": "function", "function": {
        "name": "browser_observe", "arguments": "{}",
    }} for index in range(4)]
    head = [{"role": "system", "content": "System"}] * 2 + [
        user, {"role": "assistant", "content": None, "tool_calls": calls},
    ]
    result = {"ok": True, "snapshot_id": "snapshot", "frame_id": "main",
              "elements": [{"node_id": f"n{i}", "text": "DOM" * 1900} for i in range(22)]}
    messages = head + [{"role": "tool", "tool_call_id": call["id"], "content": _json(result)}
                       for call in calls]
    original = deepcopy(messages)
    references = {id(item): ("browser_observe", index + 1)
                  for index, item in enumerate(messages[len(head):])}
    project = manager._context_tool_result
    budgets = []

    def inspect(name, source, message_id, budget, **kwargs):
        assert source == result, "each attempt must project the original full result"
        budgets.append(budget)
        return project(name, source, message_id, budget, **kwargs)

    monkeypatch.setattr(manager, "_context_tool_result", inspect)
    cap = estimate_context_tokens(head) + 800
    fitted = manager._fit_context(messages, references, protected_users=[user], cap=cap)
    assert estimate_context_tokens(fitted) <= cap
    assert min(budgets) < 2000, "the old per-result floor cannot block further fitting"
    assert messages == original
    assert user in fitted
    results = [item for item in fitted if item["role"] == "tool"]
    assert [item["tool_call_id"] for item in results] == [call["id"] for call in calls]
    for index, item in enumerate(results, 1):
        assert references[id(item)] == ("browser_observe", index)
        assert json.loads(item["content"])["source_message_id"] == index


def test_live_projection_keeps_required_unknown_state_when_cap_cannot_fit(assistant_client):
    manager = assistant_client.app.state.assistant_manager
    user = {"role": "user", "content": "Do not lose this request"}
    head = [{"role": "system", "content": "System"}] * 2 + [user, {
        "role": "assistant", "content": None, "tool_calls": [{
            "id": "call", "type": "function", "function": {
                "name": "execute_step", "arguments": "{}",
            },
        }],
    }]
    result = {"ok": False, "operation_id": "uncertain-operation", "status": "unknown",
              "code": "outcome_uncertain", "after": {
                  "url": "https://example.com/", "elements": [{"text": "x" * 100000}],
              }}
    item = {"role": "tool", "tool_call_id": "call", "content": _json(result)}
    messages = head + [item]
    original = deepcopy(messages)
    cap = estimate_context_tokens(head) + 1
    fitted = manager._fit_context(messages, {id(item): ("execute_step", 7)},
                                  protected_users=[user], cap=cap)
    # The caller must still reject an impossible context, rather than lose safety facts.
    assert estimate_context_tokens(fitted) > cap
    projected = json.loads(fitted[-1]["content"])
    assert projected["operation_id"] == "uncertain-operation"
    assert projected["status"] == "unknown" and projected["code"] == "outcome_uncertain"
    assert projected["no_retry"] is True
    assert projected["after"]["url"] == result["after"]["url"]
    assert user in fitted and messages == original


def test_live_projection_fits_required_metadata_between_sampling_budgets(assistant_client):
    manager = assistant_client.app.state.assistant_manager
    user = {"role": "user", "content": "Keep original user text"}
    head = [{"role": "system", "content": "System"}] * 2 + [user, {
        "role": "assistant", "content": None, "tool_calls": [{
            "id": "call", "type": "function", "function": {
                "name": "browser_observe", "arguments": "{}",
            },
        }],
    }]
    result = {"ok": True, "snapshot_id": "snapshot", "url": "https://example.com/" + "a" * 900,
              "elements": [{"node_id": f"n{i}", "text": "DOM" * 1900} for i in range(22)]}
    item = {"role": "tool", "tool_call_id": "call", "content": _json(result)}
    messages = head + [item]
    required = manager._context_tool_result("browser_observe", result, 7, 2000, required_only=True)
    assert 1000 < len(_json(required)) < 2000
    cap = estimate_context_tokens(head + [{**item, "content": _json(required)}]) + 10
    fitted = manager._fit_context(messages, {id(item): ("browser_observe", 7)},
                                  protected_users=[user], cap=cap)
    assert estimate_context_tokens(fitted) <= cap
    projected = json.loads(fitted[-1]["content"])
    assert projected["url"] == result["url"] and projected["source_message_id"] == 7
    assert "elements" not in projected
    assert item["content"] == _json(result) and user in fitted


def test_summary_reopens_reuses_and_target_invalidation_preserves_ledger(assistant_client, provider):
    client = assistant_client
    manager = client.app.state.assistant_manager
    identifier = create(client)
    target(client, identifier)
    ids = seed(manager, identifier)
    before = stored(manager, identifier)
    summaries, summary_requests = client.app.state.summary_provider
    summaries.extend([text('{"summary":"Earlier goal retained"}')] * 2)
    # Summary responses must not contain the normal provider's private reasoning field.
    for response in summaries:
        response["choices"][0]["message"].pop("reasoning_content", None)
    provider[0].append(text("First reply"))
    send(client, identifier, content="Current request")
    first = wait(client, identifier)
    data = session_data(manager, identifier)
    summary = data["_context_summary"]
    assert summary["version"] == 1 and summary["target_revision"] == data["_target_revision"]
    assert summary["boundary_id"] < ids[-1]
    assert first["context"]["summary_through_message_id"] == summary["boundary_id"]
    assert first["context"]["compressed"] is True
    assert len(summary_requests) <= 2
    assert all(stored(manager, identifier)[key] == value for key, value in before.items())
    fresh = AssistantManager(manager.session_factory, manager.settings, manager.crypto)
    assert fresh.detail("admin", identifier) == first
    assert fresh.list_sessions("admin")["items"][0]["context"] == first["context"]
    # Exercise the reopened manager directly, rather than replacing API dependency state.
    async def run():
        from app.schemas.assistant import SendMessage
        await fresh.send("admin", identifier, SendMessage(
            content="Next request", consent_to_send=True, request_id=uuid4()))
        await fresh.tasks[identifier]
    previous_calls = len(summary_requests)
    provider[0].append(text("Second reply"))
    asyncio.run(run())
    assert len(summary_requests) == previous_calls
    assert "Earlier goal retained" in provider[1][-1].content.decode()
    assert fresh.detail("admin", identifier)["title_revision"] == 7
    result = target(client, identifier, name="Changed target")
    after = session_data(manager, identifier)
    assert after["_target_revision"] == data["_target_revision"] + 1
    assert "_context_summary" not in after
    assert after["_execution_operations"] == ["never-replay-operation"]
    assert (after["title"], after["title_revision"], after["title_locked"]) == (
        "Retained title", 7, True)
    assert result["context"]["compressed"] is False
    assert result["context"]["summary_through_message_id"] is None


@pytest.mark.parametrize("failure", ["exception", "timeout", "secret", "empty"])
def test_summary_failure_falls_back_and_covers_exact_bounded_chunk(
    assistant_client, provider, monkeypatch, failure,
):
    client = assistant_client
    manager = client.app.state.assistant_manager
    identifier = create(client)
    ids = seed(manager, identifier, rows=130, size=4000)
    before = stored(manager, identifier)
    sources = []

    async def summarize(config, messages):
        sources.append(messages)
        assert estimate_context_tokens(messages) <= 20000
        if failure == "exception":
            raise httpx.ConnectError("private exception")
        if failure == "timeout":
            raise TimeoutError()
        return KEY if failure == "secret" else None

    monkeypatch.setattr(service, "summarize_context", summarize)
    provider[0].append(text("Completed despite summary failure"))
    send(client, identifier, content="Latest must survive")
    detail = wait(client, identifier)
    assert detail["messages"][-1]["content"] == "Completed despite summary failure"
    assert len(sources) == 2
    summary = session_data(manager, identifier)["_context_summary"]
    covered = [int(item["content"].split(":", 1)[0].removeprefix("Message ID "))
               for source in sources for item in source if item["content"].startswith("Message ID ")]
    assert covered == sorted(covered)
    assert covered == list(before)[:len(covered)]
    assert summary["boundary_id"] == covered[-1]
    assert summary["boundary_id"] < ids[-2]
    assert "Not enough context" in summary["summary"]
    assert len(_json(summary["summary"])) <= 12000
    assert KEY not in _json(detail)
    assert_requests(provider[1], "Latest must survive")
    assert all(stored(manager, identifier)[key] == value for key, value in before.items())
    _, _, records, _, _, _, _ = manager._context_history("admin", identifier)
    assert records[0][0] == next(key for key in before if key > summary["boundary_id"])


def test_cancel_during_summary_does_not_write_summary(assistant_client, provider, monkeypatch):
    client = assistant_client
    manager = client.app.state.assistant_manager
    identifier = create(client)
    seed(manager, identifier)
    before = stored(manager, identifier)
    started = threading.Event()
    cancelled = threading.Event()

    async def blocked(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(service, "summarize_context", blocked)
    send(client, identifier)
    assert started.wait(2)
    response = client.post(f"{ROOT}/{identifier}/cancel")
    assert response.status_code == 200
    assert cancelled.is_set()
    assert "_context_summary" not in session_data(manager, identifier)
    assert all(stored(manager, identifier)[key] == value for key, value in before.items())
    assert not provider[1]


@pytest.mark.parametrize("change", ["target", "status"])
def test_summary_write_checks_target_revision_and_running_status(
    assistant_client, provider, monkeypatch, change,
):
    client = assistant_client
    manager = client.app.state.assistant_manager
    identifier = create(client)
    seed(manager, identifier)

    async def race(*args):
        with manager.session_factory() as db:
            row = db.get(AssistantSession, identifier)
            data = manager._decrypt(row)
            if change == "target":
                data["_target_revision"] = data.get("_target_revision", 0) + 1
            else:
                row.status = "idle"
            manager._encrypt(row, data)
            db.commit()
        return "Stale summary"

    monkeypatch.setattr(service, "summarize_context", race)
    send(client, identifier)
    detail = wait(client, identifier)
    assert "context_changed" in detail["messages"][-1]["content"]
    assert "_context_summary" not in session_data(manager, identifier)
    assert session_data(manager, identifier)["_execution_operations"] == ["never-replay-operation"]
    assert not provider[1]


def test_repeated_call_id_still_rejected_after_live_compaction(assistant_client, provider, monkeypatch):
    client = assistant_client
    identifier = create(client)
    count = []

    async def dispatch(*args):
        count.append(1)
        return {"ok": True, "elements": [{"text": "x" * 120000}]}

    monkeypatch.setattr(AssistantTools, "dispatch", dispatch)
    provider[0].extend([tool("browser_observe", call_id="first"),
                        tool("browser_observe", call_id="second"),
                        tool("browser_observe", call_id="first")])
    send(client, identifier)
    detail = wait(client, identifier)
    assert "invalid_tool_call" in detail["messages"][-1]["content"]
    assert count == [1, 1]
    assert_requests(provider[1], "Help me configure this site")


def test_large_parallel_arguments_and_assistant_content_fit(assistant_client, provider, monkeypatch):
    client = assistant_client
    identifier = create(client)
    result = {"ok": True, "elements": [{"text": "x" * 120000}]}
    arguments = {"title": "Inspect", "explanation": "a" * 60000}
    dispatched = []

    async def dispatch(self, name, args):
        dispatched.append(args)
        return result

    monkeypatch.setattr(AssistantTools, "dispatch", dispatch)
    response = tool("browser_observe", arguments, "arg0")
    # Four argument payloads alone exceed the model-message cap.
    calls = response["choices"][0]["message"]["tool_calls"]
    for index in range(1, 4):
        calls.extend(tool("browser_observe", arguments, f"arg{index}")
                     ["choices"][0]["message"]["tool_calls"])
    # The provider response cap still applies independently of model context.
    response["choices"][0]["message"]["content"] = "\u6d4b" * 1000
    provider[0].extend([response, text("Arguments handled once")])
    latest = "\u6d4b" * 12000
    send(client, identifier, content=latest)
    detail = wait(client, identifier)
    assert detail["messages"][-1]["content"] == "Arguments handled once"
    assert len(dispatched) == 4 and all(args["explanation"] == "a" * 60000 for args in dispatched)
    assert_requests(provider[1], latest)
    assert len([row for row in detail["messages"] if row["role"] == "tool"]) == 4


def test_summary_timeout_has_twelve_second_outer_bound(assistant_client, provider, monkeypatch):
    client = assistant_client
    manager = client.app.state.assistant_manager
    identifier = create(client)
    seed(manager, identifier)
    delays = []
    real_timeout = asyncio.timeout

    async def blocked(*args):
        await asyncio.Event().wait()

    def shortened(delay):
        if delay == 12:
            delays.append(delay)
            return real_timeout(0.01)
        return real_timeout(delay)

    monkeypatch.setattr(service, "summarize_context", blocked)
    monkeypatch.setattr(service.asyncio, "timeout", shortened)
    provider[0].append(text("Finished after bounded timeout"))
    send(client, identifier)
    detail = wait(client, identifier)
    assert detail["messages"][-1]["content"] == "Finished after bounded timeout"
    assert 1 <= len(delays) <= 2 and all(delay == 12 for delay in delays)
    assert "Not enough context" in session_data(manager, identifier)["_context_summary"]["summary"]


def test_historical_projection_excludes_private_envelopes_and_notices(
    assistant_client, provider, monkeypatch,
):
    client = assistant_client
    manager = client.app.state.assistant_manager
    identifier = create(client)
    with manager.session_factory() as db:
        row = db.get(AssistantSession, identifier)
        data = manager._decrypt(row)
        data.update(title_locked=True, target={"name": "Private-bound target",
                    "base_url": "https://example.com/", "_credential_binding": {"id": "PRIVATE"}})
        manager._encrypt(row, data)
        manager._message(db, identifier, "user", "Earlier request")
        outcome = {"ok": False, "operation_id": "retained-operation", "status": "unknown",
                   "code": "outcome_uncertain", "after": {"elements": [{"text": "x" * 110000}]}}
        for index in range(45):
            manager._message(db, identifier, "tool", _json(outcome), status="error",
                             title=f"Retained title {index}", tool_name="execute_step",
                             arguments={"_credential_binding": "PRIVATE_ARGUMENTS"},
                             private_binding="PRIVATE_ENVELOPE")
        manager._message(db, identifier, "system", "NOTICE_MUST_NOT_BE_SUMMARIZED", context_notice=True)
        manager._message(db, identifier, "user", "Previous request")
        db.commit()
    sources = []

    async def summarize(config, messages):
        sources.extend(messages)
        return None

    monkeypatch.setattr(service, "summarize_context", summarize)
    provider[0].append(text("Projected safely"))
    send(client, identifier)
    detail = wait(client, identifier)
    assert detail["messages"][-1]["content"] == "Projected safely"
    exposed = _json(sources) + b"".join(request.content for request in provider[1]).decode()
    for private in ("PRIVATE", "NOTICE_MUST_NOT_BE_SUMMARIZED", "_credential_binding"):
        assert private not in exposed
    assert "arguments" not in _json(sources)
    assert "Retained title" in exposed
    assert "retained-operation" in exposed and "outcome_uncertain" in exposed
    assert "retained-operation" in session_data(manager, identifier)["_context_summary"]["summary"]


def test_tool_counter_survives_multiple_live_compactions(assistant_client, provider, monkeypatch):
    client = assistant_client
    identifier = create(client)
    dispatched = []

    async def dispatch(*args):
        dispatched.append(1)
        return {"ok": True, "elements": [{"text": "x" * 120000}]}

    monkeypatch.setattr(AssistantTools, "dispatch", dispatch)
    for index in range(6):
        response = tool("browser_observe", call_id=f"batch{index}_0")
        calls = response["choices"][0]["message"]["tool_calls"]
        for offset in range(1, 4):
            calls.extend(tool("browser_observe", call_id=f"batch{index}_{offset}")
                         ["choices"][0]["message"]["tool_calls"])
        provider[0].append(response)
    send(client, identifier)
    detail = wait(client, identifier)
    assert "tool_limit" in detail["messages"][-1]["content"]
    assert len(dispatched) == 20
    assert_requests(provider[1], "Help me configure this site")


def test_original_tool_result_pages_are_owner_scoped_and_omit_envelope(assistant_client):
    manager = assistant_client.app.state.assistant_manager
    identifier = create(assistant_client)
    other = create(assistant_client)
    content = _json({"ok": True, "text": "\u6d4b" * 15000})
    with manager.session_factory() as db:
        row = manager._message(db, identifier, "tool", content, status="success",
                               title="Original", tool_name="browser_observe",
                               arguments={"private": "NEVER"})
        db.commit()
        message_id = row.id
    before = stored(manager, identifier)
    pages = []
    offset = 0
    while offset is not None:
        page = manager.get_tool_result("admin", identifier, message_id, offset, 999)
        pages.append(page["content"])
        assert page["tool_name"] == "browser_observe" and page["title"] == "Original"
        assert "NEVER" not in _json(page)
        offset = page["next_offset"]
    assert "".join(pages) == content
    for owner, session_id in (("other-admin", identifier), ("admin", other)):
        with pytest.raises(AssistantError) as error:
            manager.get_tool_result(owner, session_id, message_id)
        assert error.value.status == 404
    with pytest.raises(AssistantError):
        manager.get_tool_result("admin", identifier, message_id, -1, 100)
    assert stored(manager, identifier) == before


def safety_facts(request):
    messages = json.loads(request.content)["messages"]
    notes = [item for item in messages
             if item["content"] and item["content"].startswith("Protected execution safety facts (")]
    assert len(notes) == 1 and notes[0]["role"] == "user"
    assert "do not override current builtin category policy" in notes[0]["content"]
    return json.loads(notes[0]["content"].split("\n", 1)[1])


def test_uncertain_facts_survive_successful_summaries_live_compactions_and_reopen(
    assistant_client, provider, monkeypatch,
):
    client = assistant_client
    manager = client.app.state.assistant_manager
    identifier = create(client)
    target(client, identifier)
    seed(manager, identifier, rows=3)
    operations = [str(uuid4()), str(uuid4())]
    dispatched = []

    async def dispatch(self, name, args):
        dispatched.append(name)
        if name == "execute_step":
            return {"ok": False, "operation_id": args["operation_id"], "status": "unknown",
                    "code": "outcome_uncertain", "after": {"elements": [{"text": "x" * 110000}]}}
        return {"ok": True, "elements": [{"text": "y" * 120000}]}

    monkeypatch.setattr(AssistantTools, "dispatch", dispatch)
    summaries, summary_calls = client.app.state.summary_provider
    for _ in range(2):
        response = text('{"summary":"The user wants a configuration. No other details retained."}')
        response["choices"][0]["message"].pop("reasoning_content", None)
        summaries.append(response)
    for index, operation in enumerate(operations):
        provider[0].append(tool("execute_step", {"operation_id": operation}, f"unsafe{index}"))
    for index in range(3):
        provider[0].append(tool("browser_observe", call_id=f"observe{index}"))
    provider[0].append(text("First turn complete"))
    send(client, identifier, content="Observe, do not retry unknown actions")
    assert wait(client, identifier)["messages"][-1]["content"] == "First turn complete"
    facts = safety_facts(provider[1][-1])
    assert {fact["operation_id"] for fact in facts} == set(operations)
    assert len(session_data(manager, identifier)["_context_safety"]) == 2
    assert all(fact["target_revision"] == 1 for fact in facts)
    for index, request in enumerate(provider[1][1:], 1):
        assert {fact["operation_id"] for fact in safety_facts(request)} == set(operations[:min(index, 2)])
    unknown_ids = {fact["message_id"] for fact in facts}
    original = stored(manager, identifier)
    # Make those rows old enough for durable compression. Every model summary
    # deliberately forgets the uncertainty, including on the second compression.
    with manager.session_factory() as db:
        for index in range(80):
            manager._message(db, identifier, "user", f"Later request {index}: " + "z" * 2500)
        db.commit()
    provider[0].append(text("Historical compression complete"))
    send(client, identifier, content="Compress older records")
    assert wait(client, identifier)["messages"][-1]["content"] == "Historical compression complete"
    data = session_data(manager, identifier)
    assert data["_context_summary"]["boundary_id"] >= max(unknown_ids)
    assert all(operation not in data["_context_summary"]["summary"] for operation in operations)
    assert len(summary_calls) == 2
    assert not summaries
    assert {fact["operation_id"] for fact in safety_facts(provider[1][-1])} == set(operations)
    fresh = AssistantManager(manager.session_factory, manager.settings, manager.crypto)
    _, _, records, _, _, _, recovered = fresh._context_history("admin", identifier)
    assert unknown_ids.isdisjoint({identifier for identifier, _ in records})
    assert {fact["operation_id"] for fact in recovered} == set(operations)

    async def reopen_turn():
        from app.schemas.assistant import SendMessage
        await fresh.send("admin", identifier, SendMessage(
            content="Still do not retry", consent_to_send=True, request_id=uuid4()))
        await fresh.tasks[identifier]

    provider[0].append(text("Reopened safely"))
    asyncio.run(reopen_turn())
    assert fresh.detail("admin", identifier)["messages"][-1]["content"] == "Reopened safely"
    assert {fact["operation_id"] for fact in safety_facts(provider[1][-1])} == set(operations)
    target(client, identifier, name="New confirmation does not resolve an old outcome")
    assert session_data(manager, identifier)["_context_safety"] == data["_context_safety"]
    assert all(stored(manager, identifier)[key] == value for key, value in original.items())
    assert dispatched == ["execute_step"] * 2 + ["browser_observe"] * 3


def test_legacy_unknown_behind_boundary_is_recovered_without_rewriting_tool(
    assistant_client, provider, monkeypatch,
):
    client = assistant_client
    manager = client.app.state.assistant_manager
    identifier = create(client)
    operation = str(uuid4())
    with manager.session_factory() as db:
        tool_row = manager._message(db, identifier, "tool", _json({
            "ok": False, "operation_id": operation, "status": "unknown", "code": "outcome_uncertain",
        }), tool_name="execute_step", status="error")
        db.flush()
        message_id = tool_row.id
        row = db.get(AssistantSession, identifier)
        data = manager._decrypt(row)
        data.update(title_locked=True, _target_revision=3, _context_revision=1,
                    _context_summary={"version": 1, "boundary_id": message_id,
                                      "target_revision": 3, "summary": "All uncertainty forgotten"})
        manager._encrypt(row, data)
        manager._message(db, identifier, "user", "Previous real request")
        db.commit()
    before = stored(manager, identifier)
    _, history = manager._history("admin", identifier)
    assert operation in _json(history)
    provider[0].append(text("Legacy uncertainty retained"))
    send(client, identifier)
    assert wait(client, identifier)["messages"][-1]["content"] == "Legacy uncertainty retained"
    assert safety_facts(provider[1][-1]) == [{"operation_id": operation, "status": "unknown",
        "code": "outcome_uncertain", "message_id": message_id, "target_revision": None}]
    assert session_data(manager, identifier)["_context_safety"][0]["operation_id"] == operation
    assert all(stored(manager, identifier)[key] == value for key, value in before.items())


def test_both_recent_genuine_users_survive_large_unicode_live_batches(
    assistant_client, provider, monkeypatch,
):
    client = assistant_client
    from tests.test_assistant_runtime_settings import save
    save(client, context_max_tokens=100000)
    manager = client.app.state.assistant_manager
    identifier = create(client)
    previous = "\u6d4b" * 11930 + " DISTINCT_CONSTRAINT_TAIL_DO_NOT_PURCHASE"
    latest = "\u8bd5" * 11950 + " LATEST_TAIL"
    with manager.session_factory() as db:
        manager._message(db, identifier, "user", "Old removable request")
        manager._message(db, identifier, "user", previous)
        # Converted historical tool and system rows are not genuine user turns.
        manager._message(db, identifier, "tool", _json({"ok": True, "text": "x" * 100000}),
                         tool_name="browser_observe", status="success")
        manager._message(db, identifier, "system", "Not a user request")
        data = manager._decrypt(db.get(AssistantSession, identifier))
        data["title_locked"] = True
        manager._encrypt(db.get(AssistantSession, identifier), data)
        db.commit()

    async def dispatch(*args):
        return {"ok": True, "elements": [{"text": "x" * 120000}]}

    monkeypatch.setattr(AssistantTools, "dispatch", dispatch)
    for index in range(3):
        response = tool("browser_observe", call_id=f"pair{index}_0")
        for offset in range(1, 4):
            response["choices"][0]["message"]["tool_calls"].extend(
                tool("browser_observe", call_id=f"pair{index}_{offset}")
                ["choices"][0]["message"]["tool_calls"])
        provider[0].append(response)
    provider[0].append(text("Both constraints retained"))
    send(client, identifier, content=latest)
    assert wait(client, identifier)["messages"][-1]["content"] == "Both constraints retained"
    for request in provider[1]:
        body = json.loads(request.content)
        assert estimate_context_tokens(body["messages"], body["tools"]) <= 100000
        assert {"role": "user", "content": latest} in body["messages"]
    for request in provider[1]:
        assert {"role": "user", "content": previous} in json.loads(request.content)["messages"]
    _, history = manager._history("admin", identifier)
    assert {"role": "user", "content": previous} in history
    assert {"role": "user", "content": latest} in history


@pytest.mark.parametrize("parallel", [1, 2])
def test_large_unicode_provenance_result_can_be_loaded_next_turn(
    assistant_client, provider, monkeypatch, parallel,
):
    client = assistant_client
    manager = client.app.state.assistant_manager
    identifier = create(client)
    client.put(f"{ROOT}/{identifier}/title", json={"title": "Fixed"})
    url = "https://example.com/" + "\u6d4b" * 3500
    result = {"ok": True, "url": url, "snapshot_id": "large-url", "frame_id": "main",
              "elements": [{"text": "x" * 95000}]}
    assert len(_json(result)) < 128 * 1024
    with pytest.raises(ValueError):
        service.compact_tool_result("browser_observe", result, message_id=1, budget=10000)

    async def dispatch(*args):
        return result

    monkeypatch.setattr(AssistantTools, "dispatch", dispatch)
    response = tool("browser_observe", call_id="url")
    if parallel == 2:
        response["choices"][0]["message"]["tool_calls"].extend(
            tool("browser_observe", call_id="url2")["choices"][0]["message"]["tool_calls"])
    provider[0].extend([response, text("First URL observation")])
    send(client, identifier, content="Read the page")
    assert wait(client, identifier)["messages"][-1]["content"] == "First URL observation"
    assert_requests(provider[1], "Read the page")
    live = [json.loads(item["content"]) for item in json.loads(provider[1][-1].content)["messages"]
            if item["role"] == "tool"]
    assert len(live) == parallel and all(item["url"] == url for item in live)
    before = stored(manager, identifier)
    provider[0].append(text("Next turn with provenance"))
    send(client, identifier, content="Continue with the same URL")
    assert wait(client, identifier)["messages"][-1]["content"] == "Next turn with provenance"
    historical = [json.loads(item["content"])
                  for item in json.loads(provider[1][-1].content)["messages"]
                  if item["role"] == "tool"]
    assert len(historical) == parallel and all(item["url"] == url for item in historical)
    assert_requests(provider[1][-1:], "Continue with the same URL")
    assert all(stored(manager, identifier)[key] == value for key, value in before.items())


def test_projection_budget_failure_is_a_controlled_context_error(assistant_client, provider, monkeypatch):
    client = assistant_client
    manager = client.app.state.assistant_manager
    identifier = create(client)
    with manager.session_factory() as db:
        manager._message(db, identifier, "tool", '{"ok":true}', tool_name="browser_observe")
        db.commit()
    budgets = []

    def cannot_project(name, result, message_id=None, budget=10000):
        budgets.append(budget)
        raise ValueError("private projection failure")

    monkeypatch.setattr(service, "compact_tool_result", cannot_project)
    send(client, identifier)
    detail = wait(client, identifier)
    assert "[context_limit]" in detail["messages"][-1]["content"]
    assert budgets == [10000, 32000, 128 * 1024]
    assert "assistant_failed" not in _json(detail) and not provider[1]


def test_disabled_compaction_skips_model_summary_and_live_fitting(
    assistant_client, provider, context_settings, monkeypatch,
):
    client = assistant_client
    context_settings(auto_compact=False)
    identifier = create(client)
    manager = client.app.state.assistant_manager
    seed(manager, identifier)

    def forbidden(*args, **kwargs):
        pytest.fail("Automatic fitting and summarization must be disabled")

    monkeypatch.setattr(manager, "_fit_context", forbidden)
    monkeypatch.setattr(service, "summarize_context", forbidden)
    provider[0].append(text("Uncompacted history retained"))
    send(client, identifier)
    assert wait(client, identifier)["messages"][-1]["content"] == "Uncompacted history retained"
    assert "_context_summary" not in session_data(manager, identifier)
    assert "Request 0:" in provider[1][0].content.decode()


@pytest.mark.parametrize("cap,success", [(16000, False), (100000, True)])
def test_disabled_compaction_still_enforces_configured_context_cap(
    assistant_client, provider, context_settings, cap, success,
):
    client = assistant_client
    context_settings(auto_compact=False, context_max_tokens=cap)
    identifier = create(client)
    seed(client.app.state.assistant_manager, identifier, rows=10, size=20000)
    if success:
        provider[0].append(text("Large configured context accepted"))
    send(client, identifier)
    detail = wait(client, identifier)
    if success:
        assert detail["messages"][-1]["content"] == "Large configured context accepted"
        messages = json.loads(provider[1][0].content)["messages"]
        assert 16000 < estimate_context_tokens(messages, service.tool_definitions()) <= cap
    else:
        assert "[context_limit]" in detail["messages"][-1]["content"]
        assert not provider[1]
    assert not client.app.state.summary_provider[1]


@pytest.mark.parametrize("auto_compact", [False, True])
def test_minimum_context_cap_preserves_latest_unicode_or_fails_controlled(
    assistant_client, provider, context_settings, auto_compact,
):
    client = assistant_client
    context_settings(context_max_tokens=16000, auto_compact=auto_compact)
    identifier = create(client)
    latest = "\u6d4b" * 12000
    send(client, identifier, content=latest)
    detail = wait(client, identifier)
    assert detail["messages"][-2]["content"] == latest
    assert "[context_limit]" in detail["messages"][-1]["content"]
    assert not provider[1] and not client.app.state.summary_provider[1]


def test_small_context_cap_scales_historical_summary_chunks(
    assistant_client, provider, context_settings, monkeypatch,
):
    client = assistant_client
    context_settings(context_max_tokens=30000)
    manager = client.app.state.assistant_manager
    identifier = create(client)
    seed(manager, identifier, rows=48, size=1000)
    chunks = []

    async def summarize(config, messages):
        chunks.append(messages)
        assert estimate_context_tokens(messages) <= 18000
        return "Earlier requests retained"

    monkeypatch.setattr(service, "summarize_context", summarize)
    provider[0].append(text("Small context complete"))
    send(client, identifier)
    assert wait(client, identifier)["messages"][-1]["content"] == "Small context complete"
    assert chunks
    assert estimate_context_tokens(json.loads(provider[1][0].content)["messages"], service.tool_definitions()) <= 30000


def test_disabled_compaction_retains_full_legacy_tool_and_uncertainty(
    assistant_client, provider, context_settings,
):
    client = assistant_client
    context_settings(auto_compact=False, context_max_tokens=100000)
    manager = client.app.state.assistant_manager
    identifier = create(client)
    operation = "do-not-retry-with-compaction-off"
    with manager.session_factory() as db:
        manager._message(db, identifier, "tool", _json({
            "ok": False, "operation_id": operation, "status": "unknown",
            "code": "outcome_uncertain", "after": {"elements": [{"text": "x" * 110000}]},
        }), tool_name="execute_step", status="error", arguments={"private": "PRIVATE_ARGUMENT"})
        db.commit()
    original = stored(manager, identifier)
    provider[0].append(text("Uncertainty retained"))
    send(client, identifier)
    assert wait(client, identifier)["messages"][-1]["content"] == "Uncertainty retained"
    assert safety_facts(provider[1][0])[0]["operation_id"] == operation
    body = provider[1][0].content.decode()
    assert "context_compacted" not in body and "PRIVATE_ARGUMENT" not in body
    assert "x" * 110000 in body
    assert all(stored(manager, identifier)[key] == value for key, value in original.items())


def test_disabled_compaction_live_overflow_fails_without_replaying_tools(
    assistant_client, provider, context_settings, monkeypatch,
):
    client = assistant_client
    context_settings(auto_compact=False, context_max_tokens=24000)
    identifier = create(client)
    dispatched = []

    async def dispatch(*args):
        dispatched.append(1)
        return {"ok": True, "elements": [{"text": "x" * 100000}]}

    monkeypatch.setattr(AssistantTools, "dispatch", dispatch)
    provider[0].append(tool("browser_observe"))
    send(client, identifier)
    detail = wait(client, identifier)
    assert "[context_limit]" in detail["messages"][-1]["content"]
    assert dispatched == [1] and len(provider[1]) == 1
    result = next(item for item in detail["messages"] if item["role"] == "tool")
    assert len(json.loads(result["content"])["elements"][0]["text"]) == 100000


def test_fixed_complete_request_cap_is_independent_of_context_setting(
    assistant_client, provider, context_settings, monkeypatch,
):
    client = assistant_client
    context_settings(context_max_tokens=1000000)
    identifier = create(client)
    definitions = service.tool_definitions()
    definitions[0]["function"]["description"] = "x" * (1536 * 1024)
    monkeypatch.setattr(service, "tool_definitions", lambda: definitions)
    send(client, identifier)
    detail = wait(client, identifier)
    assert "[request_limit]" in detail["messages"][-1]["content"]
    assert not provider[1]


def test_small_context_cap_fits_live_results_without_losing_protected_users(
    assistant_client, provider, context_settings, monkeypatch,
):
    client = assistant_client
    context_settings(context_max_tokens=30000)
    manager = client.app.state.assistant_manager
    identifier = create(client)
    previous = "Earlier exact constraint " + "\u6d4b" * 200
    latest = "Current exact constraint " + "\u8bd5" * 200
    with manager.session_factory() as db:
        manager._message(db, identifier, "user", previous)
        db.commit()
    dispatched = []

    async def dispatch(*args):
        dispatched.append(1)
        return {"ok": True, "elements": [{"text": "x" * 120000}]}

    monkeypatch.setattr(AssistantTools, "dispatch", dispatch)
    for index in range(3):
        provider[0].append(tool("browser_observe", call_id=f"smallcap{index}"))
    provider[0].append(text("Small-cap live fitting complete"))
    send(client, identifier, content=latest)
    assert wait(client, identifier)["messages"][-1]["content"] == "Small-cap live fitting complete"
    assert dispatched == [1, 1, 1]
    for request in provider[1]:
        messages = json.loads(request.content)["messages"]
        assert estimate_context_tokens(messages, service.tool_definitions()) <= 30000
        assert {"role": "user", "content": previous} in messages
        assert {"role": "user", "content": latest} in messages
