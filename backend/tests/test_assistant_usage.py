from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime
import json
import threading
from uuid import UUID, uuid4

import httpx
import pytest

from app.models.assistant import AssistantSession
from app.services.assistant import AssistantError, AssistantManager
from app.services.assistant_usage import (
    COUNTERS, MAX_SAFE_INTEGER, accumulate_usage, parse_usage, report_usage,
    usage_observer, usage_request,
)
from tests.test_assistant_api import (  # noqa: F401
    KEY, ROOT, assistant_client, create, provider, send, text, tool, wait,
)
from tests.test_assistant_context_manager import seed, session_data
from tests.test_assistant_runtime_settings import save


USAGE = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
         "prompt_tokens_details": {"cached_tokens": 80},
         "completion_tokens_details": {"reasoning_tokens": 5}}


def measured(output, usage=None):
    output["usage"] = deepcopy(USAGE if usage is None else usage)
    return output


@pytest.mark.parametrize("value", [None, True, False, -1, 1.0, "1", [], {},
                                  MAX_SAFE_INTEGER + 1])
def test_invalid_counters_remain_unknown(value):
    usage = {"prompt_tokens": value, "completion_tokens": value, "total_tokens": value,
             "prompt_tokens_details": {"cached_tokens": value},
             "completion_tokens_details": {"reasoning_tokens": value},
             "prompt_cache_hit_tokens": value, "prompt_cache_miss_tokens": value}
    assert parse_usage({"usage": usage}) == dict.fromkeys(COUNTERS)


@pytest.mark.parametrize("value", [0, 123, MAX_SAFE_INTEGER])
def test_valid_counters_are_exact_and_input_is_not_mutated(value):
    output = {"usage": {"prompt_tokens": value, "completion_tokens": value,
                        "total_tokens": value, "prompt_tokens_details": {"cached_tokens": value},
                        "completion_tokens_details": {"reasoning_tokens": value},
                        "provider_private": KEY}}
    original = deepcopy(output)
    assert parse_usage(output) == dict.fromkeys(COUNTERS, value)
    assert output == original


@pytest.mark.parametrize("output", [None, [], {}, {"usage": None}, {"usage": []},
                                    {"usage": {"input_tokens": 5, "output_tokens": 3}},
                                    {"tool": {"usage": USAGE}}])
def test_only_top_level_chat_usage_is_supported(output):
    assert parse_usage(output) == dict.fromkeys(COUNTERS)


def test_cache_precedence_and_no_inferred_totals():
    usage = {"prompt_tokens": 12, "completion_tokens": 4,
             "prompt_cache_hit_tokens": 9, "prompt_cache_miss_tokens": 3}
    assert parse_usage({"usage": usage}) == {
        "prompt_tokens": 12, "completion_tokens": 4, "total_tokens": None,
        "cached_tokens": 9, "reasoning_tokens": None,
    }
    usage["prompt_tokens_details"] = {"cached_tokens": 0}
    usage["total_tokens"] = 99
    assert parse_usage({"usage": usage})["cached_tokens"] == 0
    assert parse_usage({"usage": usage})["total_tokens"] == 99
    usage["prompt_tokens_details"]["cached_tokens"] = -1
    assert parse_usage({"usage": usage})["cached_tokens"] == 9


def test_observer_isolated_and_reports_once_on_all_exit_paths():
    async def run():
        first, second = [], []

        async def request(events, value):
            with usage_observer(events.append):
                with usage_request("conversation"):
                    await asyncio.sleep(0)
                    report_usage({"usage": {"total_tokens": value}}, "conversation")
                    report_usage(None, "conversation")
                with usage_request("title"):
                    pass

        await asyncio.gather(request(first, 11), request(second, 22))
        assert [event["total_tokens"] for event in first] == [11, None]
        assert [event["total_tokens"] for event in second] == [22, None]
        with usage_request("summary"):
            report_usage(measured({}), "summary")
        assert len(first) == len(second) == 2

    asyncio.run(run())


def test_bounded_ledger_keeps_lifetime_totals_and_saturates_safely():
    ledger = None
    for index in range(105):
        event = {"id": str(uuid4()), "kind": "conversation", "created_at": "now",
                 **dict.fromkeys(COUNTERS, MAX_SAFE_INTEGER if index == 0 else 1)}
        ledger = accumulate_usage(ledger, event)
    assert len(ledger["calls"]) == 100
    assert ledger["totals"] == {"request_count": 105, **dict.fromkeys(COUNTERS, MAX_SAFE_INTEGER)}
    assert ledger["revision"] == 105
    assert ledger["unreported_usage_count"] == 0


@pytest.mark.parametrize("prompt,completion,budget,expected", [
    (100, 20, 64000, 120), (0, 0, 16000, 0), (100, None, 64000, None),
    (None, 20, 64000, None), (100, 20, None, None),
    (MAX_SAFE_INTEGER, 20, 64000, None),
])
def test_current_is_latest_prompt_plus_completion_not_total_or_cumulative(prompt, completion, budget, expected):
    event = {"id": "one", "kind": "conversation", "created_at": "now",
             **dict.fromkeys(COUNTERS), "prompt_tokens": prompt, "completion_tokens": completion,
             "total_tokens": 999, "context_budget_tokens": budget}
    usage = accumulate_usage(None, event)
    assert usage["last_context_tokens"] == expected
    assert usage["last_context_budget_tokens"] == (budget if expected is not None else None)
    assert usage["last_context_prompt_tokens"] == (prompt if expected is not None else None)
    assert usage["last_context_completion_tokens"] == (completion if expected is not None else None)
    assert usage["last_context_usage_missing"] is (expected is None)
    assert usage["totals"]["total_tokens"] == 999
    for kind in ("summary", "title"):
        later = accumulate_usage(usage, {**event, "kind": kind, "prompt_tokens": 9999})
        assert {key: value for key, value in later.items() if key.startswith("last_context_")} == {
            key: value for key, value in usage.items() if key.startswith("last_context_")}


def test_legacy_usage_without_budget_and_context_changes_have_no_stale_current():
    from app.services.assistant_usage import pending_context_usage
    from app.services.assistant import AssistantManager

    old = {"revision": 5, "last_context_tokens": 100, "last_context_cached_tokens": 80,
           "totals": {"request_count": 1, **dict.fromkeys(COUNTERS, 100)}, "calls": []}
    payload = {"_model_usage": old}
    AssistantManager._context_fields(payload)
    assert payload["_model_usage"]["last_context_tokens"] is None
    assert payload["_model_usage"]["last_context_budget_tokens"] is None
    assert payload["_model_usage"]["revision"] == 5
    assert payload["_model_usage"]["totals"] == old["totals"]
    pending = pending_context_usage(old)
    assert pending["revision"] == 6 and pending["last_context_tokens"] is None
    assert pending["calls"] == old["calls"] and pending["totals"] == old["totals"]


@pytest.mark.parametrize("missing", ["prompt_tokens", "completion_tokens", "context_budget_tokens"])
def test_incomplete_measurement_retains_entire_last_known_snapshot_until_next_complete(missing):
    event = {"id": "one", "kind": "conversation", "created_at": "now",
             **parse_usage(measured({})), "context_budget_tokens": 64000,
             "context_revision": 2, "target_revision": 3}
    known = accumulate_usage(None, event)
    assert known["last_context_usage_missing"] is False
    incomplete = {**event, "id": "two", "prompt_tokens": 200, "completion_tokens": 40,
                  "cached_tokens": None, "context_budget_tokens": 100000, missing: None}
    stale = accumulate_usage(known, incomplete)
    assert stale["last_context_usage_missing"] is True
    for key in known:
        if key.startswith("last_context_") and key != "last_context_usage_missing":
            assert stale[key] == known[key], key
    assert stale["totals"]["request_count"] == 2
    assert stale["calls"][-1] == incomplete
    fresh = accumulate_usage(stale, {**event, "id": "three", "prompt_tokens": 300,
                                     "completion_tokens": 60, "context_budget_tokens": 100000})
    assert fresh["last_context_usage_missing"] is False
    assert fresh["last_context_tokens"] == 360
    assert fresh["last_context_budget_tokens"] == 100000
    from app.services.assistant_usage import pending_context_usage
    pending = pending_context_usage(stale)
    assert pending["last_context_tokens"] is pending["last_context_budget_tokens"] is None
    assert pending["last_context_usage_missing"] is False


def test_current_denominator_is_turn_snapshot_not_new_global_settings(assistant_client, provider):
    client = assistant_client
    save(client, auto_title=False, context_max_tokens=64000)
    identifier = create(client)
    started, release = threading.Event(), threading.Event()

    async def blocked(request):
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.005)
        return measured(text("Measured with snapshot"))

    provider[0].append(blocked)
    send(client, identifier)
    assert started.wait(2)
    save(client, context_max_tokens=100000)
    release.set()
    usage = wait(client, identifier)["usage"]
    assert usage["last_context_tokens"] == 120
    assert usage["last_context_budget_tokens"] == 64000
    provider[0].append(measured(text("New snapshot")))
    send(client, identifier)
    usage = wait(client, identifier)["usage"]
    assert usage["last_context_tokens"] == 120
    assert usage["last_context_budget_tokens"] == 100000
    assert usage["totals"]["total_tokens"] == 240


def test_compression_clears_current_until_next_measured_conversation(assistant_client, provider, monkeypatch):
    client = assistant_client
    save(client, auto_title=False)
    manager = client.app.state.assistant_manager
    identifier = create(client)
    provider[0].append(measured(text("First measured reply")))
    send(client, identifier)
    before = wait(client, identifier)["usage"]
    assert before["last_context_tokens"] == 120
    seed(manager, identifier, rows=48, size=100)
    started, release = threading.Event(), threading.Event()

    async def summarize(*args):
        return "Earlier requests retained"

    async def blocked(request):
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.005)
        return measured(text("Measured compressed reply"))

    monkeypatch.setattr("app.services.assistant.summarize_context", summarize)
    provider[0].append(blocked)
    send(client, identifier)
    assert started.wait(2)
    pending = client.get(f"{ROOT}/{identifier}").json()["usage"]
    assert pending["revision"] > before["revision"]
    assert pending["last_context_tokens"] is pending["last_context_budget_tokens"] is None
    assert pending["totals"] == before["totals"] and pending["calls"] == before["calls"]
    release.set()
    usage = wait(client, identifier)["usage"]
    assert usage["revision"] == pending["revision"] + 1
    assert usage["last_context_tokens"] == 120
    assert usage["last_context_revision"] == session_data(manager, identifier)["_context_revision"]


def test_main_summary_title_persist_known_usage_without_affecting_last_context(
    assistant_client, provider,
):
    client = assistant_client
    identifier = create(client)
    manager = client.app.state.assistant_manager
    seed(manager, identifier, rows=48, size=100)
    # seed locks the title; unlock it and use assistant rows so title generation is eligible.
    from app.models.assistant import AssistantMessage
    from sqlalchemy import select
    with manager.session_factory() as db:
        row = db.get(AssistantSession, identifier)
        data = manager._decrypt(row)
        data["title_locked"] = False
        manager._encrypt(row, data)
        for message in db.scalars(select(AssistantMessage).where(
                AssistantMessage.session_id == identifier, AssistantMessage.role == "user")):
            message.role = "assistant"
        db.commit()
    summary = measured(text('{"summary":"Earlier requests"}'), {
        "prompt_tokens": 300, "completion_tokens": 30, "total_tokens": 330})
    summary["choices"][0]["message"].pop("reasoning_content")
    client.app.state.summary_provider[0].append(summary)
    client.app.state.title_provider[0].append(measured(text('{"title":"Measured title"}'), {
        "prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}))
    provider[0].append(measured(text("Answer")))
    send(client, identifier)
    detail = wait(client, identifier)
    usage = detail["usage"]
    assert [call["kind"] for call in usage["calls"]] == ["summary", "conversation", "title"]
    assert usage["revision"] == 4  # Three calls plus context invalidation after compression.
    assert usage["last_context_tokens"] == 120
    assert usage["last_context_budget_tokens"] == 64000
    assert usage["last_context_prompt_tokens"] == 100
    assert usage["last_context_completion_tokens"] == 20
    assert usage["last_context_cached_tokens"] == 80
    assert usage["totals"] == {"request_count": 3, "prompt_tokens": 410,
                               "completion_tokens": 52, "total_tokens": 462,
                               "cached_tokens": 80, "reasoning_tokens": 5}
    assert usage["unreported_usage_count"] == 0
    for call in usage["calls"]:
        metadata = ({"context_budget_tokens", "context_revision", "target_revision"}
                    if call["kind"] == "conversation" else set())
        assert set(call) == {"id", "kind", "created_at", *COUNTERS, *metadata}
        UUID(call["id"])
        assert datetime.fromisoformat(call["created_at"]).tzinfo is not None
    assert "usage" not in client.get(ROOT).json()["items"][0]
    assert session_data(manager, identifier)["_execution_operations"] == ["never-replay-operation"]
    assert session_data(manager, identifier)["_model_usage"] == usage
    fresh = AssistantManager(manager.session_factory, manager.settings, manager.crypto)
    assert fresh.detail("admin", identifier)["usage"] == usage
    with pytest.raises(AssistantError):
        fresh.detail("other-owner", identifier)
    with manager.session_factory() as db:
        assert b"prompt_tokens" not in db.get(AssistantSession, identifier).ciphertext
    for request in provider[1] + client.app.state.summary_provider[1] + client.app.state.title_provider[1]:
        assert "_model_usage" not in request.content.decode()
        assert "prompt_tokens" not in request.content.decode()


@pytest.mark.parametrize("kind", ["conversation", "summary", "title"])
@pytest.mark.parametrize("failure", ["length", "invalid_choice", "invalid_json", "http", "secret",
                                   "embedded_secret", "invalid_embedded_json"])
def test_every_attempt_recorded_before_protocol_parse_without_retry(
    assistant_client, provider, kind, failure,
):
    client = assistant_client
    save(client, auto_title=False)
    manager = client.app.state.assistant_manager
    identifier = create(client)
    output = measured(text("malformed answer"))
    if failure == "length":
        output["choices"][0]["finish_reason"] = "length"
    elif failure == "invalid_choice":
        output["choices"] = []
    elif failure == "secret":
        output["unknown"] = KEY
    elif failure == "invalid_json":
        output = httpx.Response(200, text='{"usage":{},"usage":{}}')
    elif failure == "http":
        output = httpx.Response(500, json={"usage": USAGE})
    elif failure in {"embedded_secret", "invalid_embedded_json"}:
        escaped_key = "".join(f"\\u{ord(character):04x}" for character in KEY)
        if kind == "conversation":
            output = measured(tool("list_features"))
            output["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = (
                '{"title":"Read","nested":{"' + escaped_key + '":"value"}}'
                if failure == "embedded_secret" else '{"title":')
        else:
            output["choices"][0]["message"]["content"] = (
                '{"' + kind + '":"' + escaped_key + '"}'
                if failure == "embedded_secret" else '{"invalid":')
    if kind == "conversation":
        queue, requests = provider[:2]
        queue.append(output)
        send(client, identifier)
        detail = wait(client, identifier)
        usage = detail["usage"]
    else:
        from app.services.assistant_context import summarize_context
        from app.services.assistant_titles import generate_title
        from tests.test_assistant_api import CONFIG
        queue, requests = getattr(client.app.state, f"{kind}_provider")
        queue.append(output)

        async def run():
            with manager.session_factory() as db:
                db.get(AssistantSession, identifier).status = "running"
                db.commit()
            with usage_observer(lambda event: manager._record_usage("admin", identifier, event)):
                try:
                    if kind == "title":
                        await generate_title(CONFIG, ["Title"])
                    else:
                        await summarize_context(CONFIG, [{"role": "user", "content": "Summary"}])
                except Exception:
                    pass
        asyncio.run(run())
        usage = manager.detail("admin", identifier)["usage"]
    assert len(requests) == 1
    assert usage["totals"]["request_count"] == 1
    call = usage["calls"][0]
    assert call["kind"] == kind
    known = failure in {"length", "invalid_choice", "invalid_embedded_json"}
    assert call["prompt_tokens"] == (100 if known else None)
    assert usage["unreported_usage_count"] == (0 if known else 1)
    assert KEY not in json.dumps(usage)
    assert KEY not in json.dumps(session_data(manager, identifier))


def test_tool_usage_ignored_missing_retains_last_known_and_replay_is_read_only(
    assistant_client, provider, monkeypatch,
):
    from app.services.assistant_tools import AssistantTools
    client = assistant_client
    save(client, auto_title=False)
    identifier = create(client)

    async def dispatch(*args):
        return {"ok": True, "usage": USAGE}

    monkeypatch.setattr(AssistantTools, "dispatch", dispatch)
    provider[0].extend([measured(tool("list_features")), text("Answer")])
    request_id = uuid4()
    send(client, identifier, request_id=request_id)
    detail = wait(client, identifier)
    usage = detail["usage"]
    assert usage["totals"]["request_count"] == 2
    assert usage["totals"]["total_tokens"] == 120
    assert usage["last_context_tokens"] == 120
    assert usage["last_context_prompt_tokens"] == 100
    assert usage["last_context_completion_tokens"] == 20
    assert usage["last_context_cached_tokens"] == 80
    assert usage["last_context_budget_tokens"] == 64000
    assert usage["last_context_usage_missing"] is True
    assert usage["unreported_usage_count"] == 1
    assert send(client, identifier, request_id=request_id).json() == detail
    assert len(provider[1]) == 2


def test_cancelled_request_records_unknown_once(assistant_client, provider):
    client = assistant_client
    save(client, auto_title=False)
    started = threading.Event()

    async def blocked(request):
        started.set()
        await asyncio.Event().wait()

    identifier = create(client)
    provider[0].append(blocked)
    send(client, identifier)
    assert started.wait(2)
    detail = client.post(f"{ROOT}/{identifier}/cancel").json()
    assert detail["usage"]["totals"]["request_count"] == 1
    assert detail["usage"]["unreported_usage_count"] == 1
    assert len(provider[1]) == 1


def test_telemetry_failure_does_not_discard_answer(assistant_client, provider, monkeypatch, caplog):
    client = assistant_client
    save(client, auto_title=False)
    manager = client.app.state.assistant_manager
    identifier = create(client)

    def broken(*args):
        raise RuntimeError(KEY)

    monkeypatch.setattr(manager, "_record_usage", broken)
    provider[0].append(measured(text("Answer survives")))
    send(client, identifier)
    assert wait(client, identifier)["messages"][-1]["content"] == "Answer survives"
    assert KEY not in caplog.text
    assert "usage could not be persisted" in caplog.text


def test_overlapping_sessions_persist_only_their_own_call(assistant_client, provider):
    client = assistant_client
    save(client, auto_title=False)
    first, second = create(client), create(client)
    started, release = threading.Event(), threading.Event()

    async def blocked(request):
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.005)
        return measured(text("First"), {"prompt_tokens": 11, "total_tokens": 15})

    provider[0].append(blocked)
    send(client, first)
    assert started.wait(2)
    provider[0].append(measured(text("Second"), {"completion_tokens": 7, "total_tokens": 27}))
    send(client, second)
    second_usage = wait(client, second)["usage"]
    release.set()
    first_usage = wait(client, first)["usage"]
    assert first_usage["totals"]["request_count"] == second_usage["totals"]["request_count"] == 1
    assert first_usage["totals"]["total_tokens"] == 15
    assert first_usage["totals"]["prompt_tokens"] == 11
    assert first_usage["totals"]["completion_tokens"] == 0
    assert second_usage["totals"]["total_tokens"] == 27
    assert second_usage["totals"]["prompt_tokens"] == 0
    assert second_usage["totals"]["completion_tokens"] == 7
    assert first_usage["unreported_usage_count"] == second_usage["unreported_usage_count"] == 1
    assert first_usage["calls"][0]["id"] != second_usage["calls"][0]["id"]


@pytest.mark.parametrize("kind", ["conversation", "title", "summary"])
@pytest.mark.parametrize("failure", ["header", "serialization", "build", "network"])
def test_only_sends_count_not_local_request_construction(
    assistant_client, provider, monkeypatch, kind, failure,
):
    from app.services.assistant_context import summarize_context
    from app.services.assistant_titles import generate_title
    from tests.test_assistant_api import CONFIG

    client = assistant_client
    manager = client.app.state.assistant_manager
    identifier = create(client)
    config = {**CONFIG, "_assistant_settings": {"auto_title": False}}
    if failure == "header":
        config["api_key"] = "non-ascii-\u6d4b-key"
    elif failure == "serialization":
        config["model"] = object() if kind != "summary" else CONFIG["model"]
        if kind == "summary":
            def broken_json(value):
                raise TypeError("local JSON failure")
            monkeypatch.setattr("app.services.assistant_context._json", broken_json)
    elif failure == "build":
        def broken_build(*args, **kwargs):
            raise ValueError("local request build failure")
        from tests.test_assistant_api import RealAsyncClient
        monkeypatch.setattr(RealAsyncClient, "build_request", broken_build)
    else:
        queue = provider[0] if kind == "conversation" else getattr(client.app.state, f"{kind}_provider")[0]
        queue.append(httpx.ConnectError("private connection failure"))
    events = []

    async def run():
        with usage_observer(events.append):
            try:
                if kind == "conversation":
                    await manager._conversation("admin", identifier, config)
                elif kind == "title":
                    await generate_title(config, ["Local request"])
                else:
                    await summarize_context(config, [{"role": "user", "content": "Local request"}])
            except (TypeError, ValueError, httpx.HTTPError):
                pass

    asyncio.run(run())
    requests = provider[1] + client.app.state.title_provider[1] + client.app.state.summary_provider[1]
    assert len(requests) == len(events) == (1 if failure == "network" else 0)
    if events:
        assert events[0]["kind"] == kind
        assert all(events[0][key] is None for key in COUNTERS)
    assert "usage" not in manager.detail("admin", identifier)
