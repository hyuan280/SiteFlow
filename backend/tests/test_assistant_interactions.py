from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime
import json
import time
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models.assistant import AssistantMessage, AssistantSession
from app.schemas.assistant import InteractionResponse
from app.services.assistant import AssistantError, AssistantManager
from app.services.assistant_interactions import (
    ADAPTER, LEDGER, PENDING, apply_turn_defaults, respond_interaction, secret_input,
)
from app.services.assistant_execution import SUPPORTED_FEATURES
from app.services.assistant_tools import AssistantTools
from tests.test_assistant_api import (  # noqa: F401
    ROOT, KEY, assistant_client, provider, create, send, target, text, tool, wait,
    browser_open_response,
)
from tests.test_assistant_execution import OWNER, arguments, execution  # noqa: F401
from tests.test_assistant_retry import load, mutate
from tests.test_assistant_runtime_settings import save
from tests.test_assistant_workflows import context, configure, submit  # noqa: F401


TARGET = {"purpose": "target", "target": {
    "name": "Example", "base_url": "https://example.com/", "credential_mode": "none"}}


@pytest.mark.parametrize("decision", ["approve", "deny", "timeout"])
def test_global_scheduler_shared_review_without_target(assistant_client, provider, monkeypatch, decision):
    from app.services.scheduler_settings import SchedulerSettingsService

    client = assistant_client
    save(client, auto_title=False, auto_authorize_workflow=True)
    sid = create(client)
    manager = client.app.state.assistant_manager
    operation_id = str(uuid4())
    arguments_ = {"operation_id": operation_id, "patch": {"enabled": False}}
    if decision == "timeout":
        monkeypatch.setattr("app.services.assistant_interactions.INTERACTION_TIMEOUT", 0.2)
    provider[0].extend([
        tool("get_scheduler_settings", {}, "read"),
        tool("update_scheduler_settings", arguments_, "update"),
        tool("update_scheduler_settings", arguments_, "replay"), text("Reported actual result"),
    ])
    send(client, sid, content="Disable the global scheduler")
    item = pending(client, sid)
    assert item["purpose"] == "scheduler_config"
    assert item["review"]["scope"] == "global_scheduler"
    with manager.session_factory() as db:
        assert SchedulerSettingsService(db, manager.settings).view().enabled
        revision = manager._decrypt(manager._owned(db, "admin", sid)).get("_target_revision", 0)
    if decision != "timeout":
        assert client.post(url(sid, item["id"]), json={"decision": decision}).status_code == 200
    detail = wait(client, sid)
    results = [json.loads(m["content"]) for m in detail["messages"]
               if m.get("tool_name") == "update_scheduler_settings"]
    assert len(results) == 2
    assert all(result["ok"] == (decision == "approve") for result in results)
    assert all("target" not in result for result in results)
    if decision == "approve":
        assert results[0]["persisted"] and results[1]["replayed"]
        assert results[0]["operation_id"] == operation_id
    else:
        assert results[0] == results[1]
        assert results[0]["code"] == "interaction_" + ("denied" if decision == "deny" else "timed_out")
    with manager.session_factory() as db:
        assert SchedulerSettingsService(db, manager.settings).view().enabled == (decision != "approve")
        data = manager._decrypt(manager._owned(db, "admin", sid))
        assert data.get("_target_revision", 0) == revision and data.get("target") is None


@pytest.mark.parametrize("decision", ["approve", "deny"])
def test_cancel_shared_review_commits_before_executor(assistant_client, provider, monkeypatch, decision):
    from app.models.execution import Run

    client = assistant_client
    save(client, auto_title=False, auto_authorize_workflow=True)
    sid = create(client)
    manager = client.app.state.assistant_manager
    with manager.session_factory() as db:
        run = Run(created_by="another-admin", status="running")
        db.add(run)
        db.commit()
        run_id = run.id

    async def cancel(identifier):
        assert identifier == run_id
        with manager.session_factory() as db:
            assert db.get(Run, run_id).cancel_requested
            data = manager._decrypt(manager._owned(db, "admin", sid))
            assert next(iter(data[LEDGER].values()))["status"] == "completed"

    executor_cancel = AsyncMock(side_effect=cancel)
    monkeypatch.setattr(manager.executor, "cancel", executor_cancel)
    provider[0].extend([tool("cancel_run", {"run_id": run_id}), text("Cancellation reported")])
    send(client, sid, content=f"Cancel run {run_id}")
    item = pending(client, sid)
    assert item["purpose"] == "run_cancel" and item["review"]["run_id"] == run_id
    executor_cancel.assert_not_awaited()
    with manager.session_factory() as db:
        assert not db.get(Run, run_id).cancel_requested
    assert client.post(url(sid, item["id"]), json={"decision": decision}).status_code == 200
    detail = wait(client, sid)
    result = next(json.loads(m["content"]) for m in detail["messages"] if m.get("tool_name") == "cancel_run")
    assert result["ok"] == (decision == "approve") and "target" not in result
    if decision == "approve":
        executor_cancel.assert_awaited_once_with(run_id)
        assert result["cancel_requested"] and result["status"] == "running"
        assert result["worker_cancel_delivered"] and not result["finished"]
    else:
        executor_cancel.assert_not_awaited()
    with manager.session_factory() as db:
        data = manager._decrypt(manager._owned(db, "admin", sid))
        assert data.get("_target_revision", 0) == 0 and data.get("target") is None


def test_scheduler_completed_subject_does_not_succeed_for_new_operation(assistant_client, provider):
    from app.services.scheduler_settings import SchedulerSettingsService

    client = assistant_client
    save(client, auto_title=False)
    sid = create(client)
    provider[0].extend([
        tool("update_scheduler_settings", {"operation_id": str(uuid4()), "patch": {"enabled": enabled}},
             f"update-{index}") for index, enabled in enumerate((False, True, False))
    ] + [text("Repeated subject stopped")])
    send(client, sid, content="Disable then enable the global scheduler")
    for expected in (False, True):
        item = pending(client, sid)
        assert item["review"]["changes"][0]["after"] is expected
        assert client.post(url(sid, item["id"]), json={"decision": "approve"}).status_code == 200
    detail = wait(client, sid)
    results = [json.loads(m["content"]) for m in detail["messages"]
               if m.get("tool_name") == "update_scheduler_settings"]
    assert len(results) == 3 and results[0]["ok"] and results[1]["ok"]
    assert not results[2]["ok"] and results[2]["terminal"]
    assert results[2]["code"] == "interaction_already_requested"
    manager = client.app.state.assistant_manager
    with manager.session_factory() as db:
        assert SchedulerSettingsService(db, manager.settings).view().enabled


@pytest.mark.parametrize("purpose", ["site_config", "consumption", "template_import", "scheduler_config", "run_cancel"])
def test_internal_interactions_are_not_model_callable(execution, purpose):
    from app.services.assistant_prompt import tool_definitions

    definition = next(d["function"] for d in tool_definitions()
                      if d["function"]["name"] == "request_user_interaction")
    assert purpose not in definition["parameters"]["properties"]["request"]["discriminator"]["mapping"]
    tools = AssistantTools(execution.manager, OWNER, execution.session_id, None)
    assert asyncio.run(tools.dispatch("request_user_interaction", {
        "request": {"purpose": purpose}})) == {"ok": False, "code": "invalid_arguments"}
    assert not load(execution).get(LEDGER)


@pytest.mark.parametrize("value", [KEY, "password=private-value", "Bearer private-value"])
def test_scheduler_secrets_rejected_before_history(assistant_client, provider, value):
    client = assistant_client
    save(client, auto_title=False)
    sid = create(client)
    provider[0].append(tool("update_scheduler_settings", {
        "operation_id": str(uuid4()), "patch": {"cron": value}}))
    send(client, sid)
    detail = wait(client, sid)
    assert "[secret_output]" in detail["messages"][-1]["content"]
    assert value not in json.dumps(detail)
    assert not any(m.get("tool_name") == "update_scheduler_settings" for m in detail["messages"])


def interaction(request, call_id="interaction"):
    return tool("request_user_interaction", {"request": request}, call_id)


def url(sid, identifier):
    return f"{ROOT}/{sid}/interactions/{identifier}/response"


def pending(client, sid):
    for _ in range(300):
        detail = client.get(f"{ROOT}/{sid}").json()
        if detail.get("pending_interaction"):
            return detail["pending_interaction"]
        if detail["status"] == "idle":
            pytest.fail(str(detail["messages"][-2:]))
        time.sleep(0.01)
    pytest.fail("No pending interaction")


def test_target_from_null_resumes_same_call_and_next_tool(assistant_client, provider):
    from datetime import timedelta, timezone

    client = assistant_client
    save(client, auto_title=False)
    sid = create(client)
    worker_ids = []
    provider[0].extend([
        interaction(TARGET), tool("browser_open", {}, "open"),
        browser_open_response(worker_ids, "interaction", (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()),
        text("Confirmed and observed"),
    ])
    send(client, sid)
    review = pending(client, sid)
    assert set(review) == {"id", "kind", "purpose", "title", "description", "expires_at",
                           "options", "multiple", "fields", "review"}
    assert review["kind"] == "authorization" and review["purpose"] == "target"
    assert review["review"] == {"name": "Example", "base_url": "https://example.com/"}
    assert 175 < (datetime.fromisoformat(review["expires_at"]).timestamp() - time.time()) <= 180
    result = client.post(url(sid, review["id"]), json={"decision": "approve"})
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    detail = wait(client, sid)
    assert detail["messages"][-1]["content"] == "Confirmed and observed"
    assert len(worker_ids) == 1 and detail["browser"]["lane_key"] == "interaction"
    assert detail["target"]["confirmed"] and not detail["target"]["proxy"]
    assert not detail["target"]["execution_consent"]
    assert not detail["target"]["screenshot_consent"] and not detail["target"]["workflow_consent"]
    assert not detail["pending_interaction"]
    model_requests = [json.loads(r.content) for r in provider[1] if "chat/completions" in str(r.url)]
    assert "Example" in model_requests[-1]["messages"][1]["content"]
    assert client.post(url(sid, review["id"]), json={"decision": "approve"}).status_code == 200
    assert client.post(url(sid, review["id"]), json={"decision": "deny"}).status_code == 409


def test_endpoint_auth_owner_csrf_and_shape(assistant_client, provider):
    client = assistant_client
    sid = create(client)
    provider[0].extend([interaction(TARGET), text("Denied")])
    send(client, sid)
    item = pending(client, sid)
    endpoint = url(sid, item["id"])
    csrf = client.headers.pop("X-CSRF-Token")
    assert client.post(endpoint, json={"decision": "approve"}).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    for body in ({}, {"decision": True}, {"decision": "approved"},
                 {"decision": "approve", "patch": {}}, {"decision": "deny", "values": {}},
                 {"decision": "approve", "selections": ["unknown"]}):
        assert client.post(endpoint, json=body).status_code == 422
    manager = client.app.state.assistant_manager
    with pytest.raises(AssistantError) as caught:
        asyncio.run(respond_interaction(manager, "other", sid, item["id"], InteractionResponse(decision="approve")))
    assert caught.value.status == 404
    assert client.post(url(sid, str(uuid4())), json={"decision": "deny"}).status_code == 404
    assert client.post(endpoint, json={"decision": "deny"}).status_code == 200
    assert wait(client, sid)["target"] is None
    client.cookies.clear()
    assert client.post(endpoint, json={"decision": "approve"}).status_code == 401


@pytest.mark.parametrize("mode", ["deny", "timeout"])
def test_terminal_subject_dedup_whole_turn(assistant_client, provider, monkeypatch, mode):
    if mode == "timeout":
        monkeypatch.setattr("app.services.assistant_interactions.INTERACTION_TIMEOUT", 0.2)
    client = assistant_client
    sid = create(client)
    changed = deepcopy(TARGET)
    changed["target"]["name"] = "Rephrased"
    provider[0].extend([interaction(TARGET), interaction(changed, "repeat"), text("Stopped")])
    send(client, sid)
    item = pending(client, sid)
    if mode == "deny":
        assert client.post(url(sid, item["id"]), json={"decision": "deny"}).status_code == 200
    detail = wait(client, sid)
    results = [json.loads(m["content"]) for m in detail["messages"] if m.get("tool_name") == "request_user_interaction"]
    assert len(results) == 2 and results[0] == results[1]
    assert results[0]["code"] == ("interaction_denied" if mode == "deny" else "interaction_timed_out")
    assert results[0]["terminal"] and detail["target"] is None


@pytest.mark.parametrize("question,response", [
    ({"kind": "authorization", "title": "Continue planning?"}, {"decision": "approve"}),
    ({"kind": "choice", "title": "Account source", "options": [
        {"id": "manual", "label": "Saved account"}, {"id": "bitwarden", "label": "Bitwarden"}]},
     {"decision": "approve", "selections": ["manual"]}),
    ({"kind": "choice", "title": "Goals", "multiple": True, "options": [
        {"id": "a", "label": "Observe"}, {"id": "b", "label": "Propose"}]},
     {"decision": "approve", "selections": ["a", "b"]}),
    ({"kind": "input", "title": "Website details", "fields": [
        {"id": "name", "label": "Site name", "required": True}]},
     {"decision": "approve", "values": {"name": "Example"}}),
])
def test_generic_questions_have_no_authority(assistant_client, provider, question, response):
    client = assistant_client
    sid = create(client)
    provider[0].extend([interaction({"purpose": "question", **question}), text("Collected")])
    send(client, sid)
    item = pending(client, sid)
    assert item["review"] == {"required": True} and item["kind"] == question["kind"]
    assert client.post(url(sid, item["id"]), json=response).status_code == 200
    detail = wait(client, sid)
    assert detail["target"] is None
    result = json.loads(next(m["content"] for m in detail["messages"] if m.get("tool_name") == "request_user_interaction"))
    assert all(result[k] == v for k, v in response.items())


def test_input_secret_and_selection_validation(assistant_client, provider):
    client = assistant_client
    sid = create(client)
    provider[0].extend([interaction({"purpose": "question", "kind": "input", "title": "Site name",
                                   "fields": [{"id": "name", "label": "Name"}]}), text("Answered")])
    send(client, sid)
    item = pending(client, sid)
    endpoint = url(sid, item["id"])
    for values in ({"name": "password=private-value"}, {"name": KEY}, {"name": "密码：秘密"},
                   {"name": "x" * 2001}, {"other": "Example"}, {}):
        response = client.post(endpoint, json={"decision": "approve", "values": values})
        assert response.status_code == 422
        assert "private-value" not in response.text and KEY not in response.text
    assert pending(client, sid)["id"] == item["id"]
    assert client.post(endpoint, json={"decision": "approve", "values": {"name": "Example"}}).status_code == 200
    assert wait(client, sid)["target"] is None


@pytest.mark.parametrize("interaction_request,code", [
    ({"purpose": "question", "kind": "input", "title": "Enter your password", "fields": [{"id": "value", "label": "Value"}]}, "secret_input"),
    ({"purpose": "question", "kind": "input", "title": "验证码", "fields": [{"id": "value", "label": "Value"}]}, "secret_input"),
    ({"purpose": "target", "target": {**TARGET["target"], "proxy": True}}, "invalid_arguments"),
    *[({"purpose": "authorization", "capability": "execution", "feature_type": feature},
       "execution_feature_denied") for feature in ("flow.jump", "post.buy_card", "plugin.example")],
])
def test_model_cannot_request_secrets_or_arbitrary_patches(execution, interaction_request, code):
    async def scenario():
        tools = AssistantTools(execution.manager, OWNER, execution.session_id, execution.target)
        result = await asyncio.wait_for(tools.dispatch(
            "request_user_interaction", {"request": interaction_request}), timeout=0.5)
        assert result == {"ok": False, "code": code}
        assert not load(execution).get(PENDING) and not load(execution).get(LEDGER)
        assert not execution.manager._interaction_waiters
        execution.manager._worker.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize("feature", ["action.fill", "challenge.2fa"])
def test_supported_private_features_request_host_approval(execution, feature):
    mutate(execution, lambda data: data["target"].update(credential_mode="bitwarden"))

    async def scenario():
        manager, sid = execution.manager, execution.session_id
        tools = AssistantTools(manager, OWNER, sid, None)
        task = asyncio.create_task(tools.dispatch("request_user_interaction", {"request": {
            "purpose": "authorization", "capability": "execution", "feature_type": feature}}))
        async with asyncio.timeout(0.5):
            while not load(execution).get(PENDING):
                assert not task.done(), task.result()
                await asyncio.sleep(0)
        item = load(execution)[PENDING]
        assert item["review"]["feature_type"] == feature
        assert feature in item["review"]["execution_features"]
        assert item["review"]["credential_consent"]
        await respond_interaction(manager, OWNER, sid, item["id"], InteractionResponse(decision="deny"))
        assert (await asyncio.wait_for(task, timeout=0.5))["code"] == "interaction_denied"
        assert not load(execution).get(PENDING)
        manager._worker.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize("purpose,interaction_request", [
    ("trusted_origins", {"purpose": "trusted_origins", "origins": ["https://other.example"]}),
    ("proxy", {"purpose": "proxy", "enabled": True}),
])
def test_target_mutation_disposes_browser_invalidates_proposal(execution, purpose, interaction_request):
    mutate(execution, lambda d: d.update(_workflow_proposal_id=9, _context_summary={"summary": "old", "boundary_id": 1}))
    before = load(execution)

    async def scenario():
        manager, sid = execution.manager, execution.session_id
        tools = AssistantTools(manager, OWNER, sid, execution.target)
        task = asyncio.create_task(tools.dispatch("request_user_interaction", {"request": interaction_request}))
        while not load(execution).get(PENDING):
            await asyncio.sleep(0)
        item = load(execution)[PENDING]
        # A concurrent independent writer and missing notification do not lose data or block progress.
        async with manager._mutation(OWNER, sid):
            mutate(execution, lambda d: d.update(unrelated="preserved"))
        manager._interaction_waiters.pop(item["id"])
        await respond_interaction(manager, OWNER, sid, item["id"], InteractionResponse(
            decision="approve", **({"selections": ["proxy_general"]} if purpose == "proxy" else {})))
        assert (await task)["ok"]
        assert tools.target == load(execution)["target"]

    asyncio.run(scenario())
    after = load(execution)
    assert after["_target_revision"] == before.get("_target_revision", 0) + 1
    assert "_workflow_proposal_id" not in after and "_context_summary" not in after
    assert after["unrelated"] == "preserved"
    assert not execution.manager.browsers and execution.manager._close_browser.await_count == 1
    if purpose == "proxy":
        assert after["target"]["proxy"] is True
        assert after["target"]["run_lane_policy"] == "proxy_general"
    else:
        assert after["target"]["trusted_origins"] == ["https://other.example"]


@pytest.mark.parametrize("scope", [None, "browser", "workflow", "both"])
@pytest.mark.parametrize("lane", ["general", "proxy_general"])
def test_proxy_user_selection_overrides_model_suggestion_and_scope(execution, scope, lane):
    initial_proxy = lane == "general"
    initial_policy = "proxy_general" if lane == "general" else "general"
    mutate(execution, lambda d: d["target"].update(proxy=initial_proxy, run_lane_policy=initial_policy))

    async def scenario():
        manager, sid = execution.manager, execution.session_id
        tools = AssistantTools(manager, OWNER, sid, None)
        request_ = {"purpose": "proxy", "enabled": lane == "general"}
        if scope is not None:
            request_["scope"] = scope
        task = asyncio.create_task(tools.dispatch("request_user_interaction", {"request": request_}))
        while not load(execution).get(PENDING):
            await asyncio.sleep(0)
        item = load(execution)[PENDING]
        assert item["kind"] == "choice" and not item["multiple"]
        assert item["title"] == "\u9009\u62e9\u8f66\u9053\u4e0e\u4ee3\u7406"
        assert item["options"] == [
            {"id": "general", "label": "\u901a\u7528\u8f66\u9053\uff08\u4e0d\u4f7f\u7528\u4ee3\u7406\uff09"},
            {"id": "proxy_general", "label": "\u4ee3\u7406\u901a\u7528\u8f66\u9053\uff08\u4f7f\u7528\u5df2\u914d\u7f6e\u4ee3\u7406\uff09"},
        ]
        assert item["review"]["scope"] == (scope or "both")
        assert item["review"]["suggested_lane"] != lane
        assert item["review"]["browser_implementation"] == {
            "general": "interaction", "proxy_general": "proxy_interaction"}
        assert item["review"]["run_lane_policy"] == {
            "general": "general", "proxy_general": "proxy_general"}
        for selections in (None, [], ["general", "proxy_general"], ["interaction"]):
            with pytest.raises(AssistantError) as caught:
                await respond_interaction(manager, OWNER, sid, item["id"], InteractionResponse(
                    decision="approve", **({"selections": selections} if selections is not None else {})))
            assert caught.value.code == "invalid_interaction_response"
            assert load(execution)["target"]["proxy"] == initial_proxy
            assert load(execution)["target"]["run_lane_policy"] == initial_policy
        await respond_interaction(manager, OWNER, sid, item["id"], InteractionResponse(
            decision="approve", selections=[lane]))
        result = await task
        assert result["ok"] and result["selections"] == [lane]
        target_ = load(execution)["target"]
        assert target_["proxy"] == (lane == "proxy_general" if scope != "workflow" else initial_proxy)
        assert target_["run_lane_policy"] == (lane if scope != "browser" else initial_policy)
        assert load(execution)["_target_revision"] == 1
        manager._close_browser.assert_awaited_once_with(sid)
    asyncio.run(scenario())


def test_proxy_choice_endpoint_and_default_both_scope(assistant_client, provider):
    client = assistant_client
    sid = create(client)
    target(client, sid, run_lane_policy="general")
    provider[0].extend([interaction({"purpose": "proxy", "enabled": True}), text("User selected general")])
    send(client, sid)
    item = pending(client, sid)
    endpoint = url(sid, item["id"])
    assert item["kind"] == "choice" and item["review"]["scope"] == "both"
    assert client.post(endpoint, json={"decision": "approve"}).status_code == 422
    assert client.post(endpoint, json={"decision": "approve", "selections": ["general"]}).status_code == 200
    detail = wait(client, sid)
    assert not detail["target"]["proxy"]
    assert detail["target"]["run_lane_policy"] == "general"
    assert detail["messages"][-1]["content"] == "User selected general"


def test_unrelated_generic_confirmations_prompt_independently(assistant_client, provider):
    client = assistant_client
    sid = create(client)
    first = {"purpose": "question", "kind": "authorization", "title": "Continue planning?"}
    second = {**first, "title": "Include notification planning?", "description": "A separate optional topic"}
    provider[0].extend([interaction(first), interaction(second, "different_question"), text("Both answered")])
    send(client, sid)
    item = pending(client, sid)
    assert client.post(url(sid, item["id"]), json={"decision": "approve"}).status_code == 200
    other = pending(client, sid)
    # Wait for the distinct second prompt if the response snapshot races the waiter.
    for _ in range(300):
        if other["id"] != item["id"]:
            break
        time.sleep(0.01)
        other = pending(client, sid)
    assert other["id"] != item["id"] and other["title"] == second["title"]
    assert client.post(url(sid, other["id"]), json={"decision": "approve"}).status_code == 200
    assert wait(client, sid)["messages"][-1]["content"] == "Both answered"


@pytest.mark.parametrize("field,value", [
    ("title", "Another title"), ("description", "Another description"),
    ("multiple", True), ("required", False),
    ("options", [{"id": "b", "label": "B"}]),
])
def test_question_subject_covers_semantic_content(execution, field, value):
    from app.services.assistant_interactions import prepare

    question = {"purpose": "question", "kind": "choice", "title": "Choose", "description": "Topic",
                "options": [{"id": "a", "label": "A"}]}
    with execution.manager.session_factory() as db:
        _, _, first = prepare(db, ADAPTER.validate_python(question), load(execution), {})
        _, _, second = prepare(db, ADAPTER.validate_python({**question, field: value}), load(execution), {})
    assert first != second


@pytest.mark.parametrize("outcome", ["approve", "deny", "timeout"])
def test_proxy_success_cache_scoped_but_denial_turn_wide(execution, monkeypatch, outcome):
    if outcome == "timeout":
        monkeypatch.setattr("app.services.assistant_interactions.INTERACTION_TIMEOUT", 0.01)

    async def scenario():
        manager, sid = execution.manager, execution.session_id
        tools = AssistantTools(manager, OWNER, sid, None)
        browser = {"purpose": "proxy", "enabled": True, "scope": "browser"}
        workflow = {**browser, "scope": "workflow"}
        task = asyncio.create_task(tools.dispatch("request_user_interaction", {"request": browser}))
        while not load(execution).get(PENDING):
            await asyncio.sleep(0)
        first = load(execution)[PENDING]
        if outcome != "timeout":
            await respond_interaction(manager, OWNER, sid, first["id"], InteractionResponse(
                decision=outcome, **({"selections": ["general"]} if outcome == "approve" else {})))
        result = await task
        if outcome != "approve":
            assert await tools.dispatch("request_user_interaction", {"request": workflow}) == result
            assert len(load(execution)[LEDGER]) == 1
            return
        task = asyncio.create_task(tools.dispatch("request_user_interaction", {"request": workflow}))
        while not load(execution).get(PENDING):
            await asyncio.sleep(0)
        second = load(execution)[PENDING]
        assert second["id"] != first["id"] and second["review"]["scope"] == "workflow"
        await respond_interaction(manager, OWNER, sid, second["id"], InteractionResponse(
            decision="approve", selections=["proxy_general"]))
        assert (await task)["ok"]
        assert not load(execution)["target"]["proxy"]
        assert load(execution)["target"]["run_lane_policy"] == "proxy_general"
        assert len(load(execution)[LEDGER]) == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("required", [True, False])
@pytest.mark.parametrize("outcome", ["deny", "timeout"])
def test_required_question_blocks_same_batch_and_later_round_tools(
    assistant_client, provider, monkeypatch, required, outcome,
):
    from app.services.assistant_execution import AssistantExecution

    if outcome == "timeout":
        monkeypatch.setattr("app.services.assistant_interactions.INTERACTION_TIMEOUT", 0.05)
    client = assistant_client
    save(client, auto_title=False)
    sid = create(client)
    target(client, sid, authorized=True)
    execute = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(AssistantExecution, "execute", execute)
    question = {"purpose": "question", "kind": "authorization", "title": "Proceed with this plan?", "required": required}
    batch = interaction(question)
    batch["choices"][0]["message"]["tool_calls"].extend(
        tool("execute_step", arguments(), "same_batch")["choices"][0]["message"]["tool_calls"])
    provider[0].extend([batch, tool("execute_step", arguments(), "later_round"), text("Stopped safely")])
    send(client, sid)
    item = pending(client, sid)
    if outcome == "deny":
        assert client.post(url(sid, item["id"]), json={"decision": "deny"}).status_code == 200
    detail = wait(client, sid)
    assert detail["messages"][-1]["content"] == "Stopped safely"
    results = [json.loads(row["content"]) for row in detail["messages"] if row.get("tool_name") == "execute_step"]
    question_result = json.loads(next(row["content"] for row in detail["messages"]
                                     if row.get("tool_name") == "request_user_interaction"))
    if required:
        assert execute.await_count == 0
        assert question_result["blocking"]
        assert [result["code"] for result in results] == ["prerequisite_missing", "prerequisite_missing"]
        assert "All further tools are blocked" in provider[1][-1].content.decode()
    else:
        assert execute.await_count == 2 and all(result["ok"] for result in results)
        assert not question_result.get("blocking")


def test_required_question_blocks_rephrased_question_and_pure_reads(execution):
    async def scenario():
        tools = AssistantTools(execution.manager, OWNER, execution.session_id, None,
                               turn={"id": "blocked-turn", "deadline": asyncio.get_running_loop().time() - 1})
        question = {"purpose": "question", "kind": "authorization", "title": "Required prerequisite"}
        result = await tools.dispatch("request_user_interaction", {"request": question})
        assert result["blocking"] and tools.interaction_blocked
        for name, args in (("request_user_interaction", {"request": {**question, "title": "Rephrased"}}),
                           ("list_features", {}), ("get_tool_result", {"message_id": 1})):
            assert (await tools.dispatch(name, args))["code"] == "prerequisite_missing"
        assert len(load(execution)[LEDGER]) == 1
    asyncio.run(scenario())


def test_execution_gate_continues_same_call_persists_overall_permission(execution):
    mutate(execution, lambda d: d["target"].update(execution_consent=False, execution_features=[], allow_sign=False))

    async def scenario():
        tools = AssistantTools(execution.manager, OWNER, execution.session_id, None)
        task = asyncio.create_task(tools.dispatch("execute_step", arguments()))
        while not load(execution).get(PENDING):
            await asyncio.sleep(0)
        item = load(execution)[PENDING]
        assert execution.manager._worker.await_count == 0
        assert item["review"]["feature_type"] == "action.wait"
        assert item["review"]["execution_features"] == sorted(
            key for key in SUPPORTED_FEATURES if not key.startswith("auth.") and key != "challenge.2fa")
        assert not item["review"]["credential_consent"]
        await respond_interaction(execution.manager, OWNER, execution.session_id, item["id"], InteractionResponse(decision="approve"))
        assert (await task)["ok"]
        other = AssistantTools(execution.manager, OWNER, execution.session_id, None)
        assert (await other.dispatch("execute_step", arguments("action.click", {"selector": ["#approved"]})))["ok"]
        assert not load(execution).get(PENDING)
    asyncio.run(scenario())
    assert execution.manager._worker.await_count == 2
    assert load(execution)["target"]["execution_features"] == sorted(
        key for key in SUPPORTED_FEATURES if not key.startswith("auth.") and key != "challenge.2fa")
    assert load(execution)["target"]["allow_sign"]


@pytest.mark.parametrize("name,args", [
    ("list_features", {}), ("get_feature_schema", {"feature_type": "action.wait"}),
])
def test_catalog_checks_session_without_requiring_target(execution, monkeypatch, name, args):
    owned = execution.manager._owned
    checked = []

    def authorized_session(*args, **kwargs):
        checked.append(True)
        return owned(*args, **kwargs)

    monkeypatch.setattr(execution.manager, "_owned", authorized_session)
    tools = AssistantTools(execution.manager, OWNER, execution.session_id, None)
    assert asyncio.run(tools.dispatch(name, args))["ok"]
    assert checked


@pytest.mark.parametrize("outcome", ["deny", "timeout"])
def test_execution_denial_dedup_across_features(execution, monkeypatch, outcome):
    if outcome == "timeout":
        monkeypatch.setattr("app.services.assistant_interactions.INTERACTION_TIMEOUT", 0.01)
    mutate(execution, lambda d: d["target"].update(execution_consent=False, execution_features=[]))

    async def scenario():
        tools = AssistantTools(execution.manager, OWNER, execution.session_id, None)
        task = asyncio.create_task(tools.dispatch("execute_step", arguments()))
        while not load(execution).get(PENDING):
            await asyncio.sleep(0)
        if outcome == "deny":
            await respond_interaction(execution.manager, OWNER, execution.session_id,
                                      load(execution)[PENDING]["id"], InteractionResponse(decision="deny"))
        result = await task
        assert not result["ok"] and result["terminal"]
        assert await tools.dispatch("execute_step", arguments("action.click", {"selector": ["#approved"]})) == result
        assert len(load(execution)[LEDGER]) == 1
        assert not load(execution).get(PENDING)
    asyncio.run(scenario())
    assert execution.manager._worker.await_count == 0


def test_execution_review_freezes_supported_features_and_credential_scope(execution, monkeypatch):
    mutate(execution, lambda d: d["target"].update(
        credential_mode="bitwarden", execution_consent=False, execution_features=[]))
    reviewed = set(SUPPORTED_FEATURES)

    async def scenario():
        tools = AssistantTools(execution.manager, OWNER, execution.session_id, None)
        task = asyncio.create_task(tools.dispatch("request_user_interaction", {
            "request": {"purpose": "authorization", "capability": "execution"}}))
        while not load(execution).get(PENDING):
            await asyncio.sleep(0)
        item = load(execution)[PENDING]
        assert set(item["review"]["execution_features"]) == reviewed
        assert item["review"]["credential_consent"]
        monkeypatch.setattr("app.services.assistant_execution.SUPPORTED_FEATURES", reviewed | {"future.feature"})
        await respond_interaction(execution.manager, OWNER, execution.session_id, item["id"], InteractionResponse(decision="approve"))
        assert (await task)["ok"]
        assert set(load(execution)["target"]["execution_features"]) == reviewed
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["choice", "input"])
@pytest.mark.parametrize("identifier", ["__proto__", "constructor", "prototype"])
def test_reserved_interaction_ids_rejected(kind, identifier):
    from jsonschema import Draft202012Validator

    key = "options" if kind == "choice" else "fields"
    request_ = {"purpose": "question", "kind": kind, "title": "Choose",
                key: [{"id": identifier, "label": "Example"}]}
    with pytest.raises(ValidationError):
        ADAPTER.validate_python(request_)
    assert not Draft202012Validator(ADAPTER.json_schema()).is_valid(request_)
    with pytest.raises(ValidationError):
        InteractionResponse(decision="approve", **(
            {"selections": [identifier]} if kind == "choice" else {"values": {identifier: "Example"}}))


@pytest.mark.parametrize("kind", ["choice", "input", "authorization"])
@pytest.mark.parametrize("blank", [" ", "\t\n"])
def test_whitespace_interaction_titles_and_labels_rejected(kind, blank):
    request_ = {"purpose": "question", "kind": kind, "title": "Choose"}
    if kind != "authorization":
        request_["options" if kind == "choice" else "fields"] = [{"id": "example", "label": blank}]
        with pytest.raises(ValidationError):
            ADAPTER.validate_python(request_)
        request_["options" if kind == "choice" else "fields"][0]["label"] = "Example"
    request_["title"] = blank
    with pytest.raises(ValidationError):
        ADAPTER.validate_python(request_)


@pytest.mark.parametrize("capability,tool_name,args", [
    ("screenshot", "browser_screenshot", {}),
    ("workflow", "import_workflow_draft", {"proposal_message_id": 1}),
    ("workflow", "run_site", {}),
])
def test_business_tools_gate_before_dispatch(execution, capability, tool_name, args):
    mutate(execution, lambda d: d["target"].update({capability + "_consent": False}))
    async def scenario():
        tools = AssistantTools(execution.manager, OWNER, execution.session_id, None, config={"supports_vision": True})
        task = asyncio.create_task(tools.dispatch(tool_name, args))
        while not load(execution).get(PENDING):
            await asyncio.sleep(0)
        item = load(execution)[PENDING]
        assert item["review"]["capability"] == capability
        await respond_interaction(execution.manager, OWNER, execution.session_id, item["id"], InteractionResponse(decision="deny"))
        assert (await task)["code"] == "interaction_denied"
        assert (await tools.dispatch(tool_name, args))["code"] == "interaction_denied"
    asyncio.run(scenario())
    assert execution.manager._worker.await_count == 0


def test_global_defaults_apply_to_existing_and_new_targets_but_never_identity(execution):
    values = {"auto_authorize_" + k: True for k in ("execution", "screenshot", "workflow")}
    mutate(execution, lambda d: d["target"].update(execution_consent=False, execution_features=[]))
    apply_turn_defaults(execution.manager, OWNER, execution.session_id, values)
    target_ = load(execution)["target"]
    assert all(target_[k + "_consent"] for k in ("execution", "screenshot", "workflow"))
    mutate(execution, lambda d: d.update(target=None))
    apply_turn_defaults(execution.manager, OWNER, execution.session_id, values)
    assert load(execution)["target"] is None
    async def scenario():
        tools = AssistantTools(execution.manager, OWNER, execution.session_id, None, config={"_assistant_settings": values})
        task = asyncio.create_task(tools.dispatch("request_user_interaction", {"request": TARGET}))
        while not load(execution).get(PENDING):
            await asyncio.sleep(0)
        item = load(execution)[PENDING]
        assert item["purpose"] == "target" and load(execution)["target"] is None
        assert item["review"] == {"name": "Example", "base_url": "https://example.com/"}
        assert all(item["arguments"]["target"][k + "_consent"] for k in ("execution", "screenshot", "workflow"))
        await respond_interaction(execution.manager, OWNER, execution.session_id, item["id"], InteractionResponse(decision="approve"))
        assert (await task)["ok"]
    asyncio.run(scenario())


@pytest.mark.parametrize("interactive", [False, True])
def test_permission_changes_expire_unused_retry_grants_without_replay(execution, interactive):
    from app.services.assistant_retry import GRANTS, RECORDS, issue_grant, valid_grant

    manager, sid = execution.manager, execution.session_id
    original = deepcopy(load(execution)["target"])
    original["screenshot_consent"] = False
    mutate(execution, lambda d: d.update(target=deepcopy(original)))
    scope = manager.browsers[sid]
    unused, used = str(uuid4()), str(uuid4())

    def seed(data):
        issue_grant(data, scope, unused, {"feature_type": "action.click"})
        issue_grant(data, scope, used, {"feature_type": "action.click"})
        data[GRANTS][used].update(status="used", retry_operation_id="already-dispatched")
        data[RECORDS] = {"uncertain": {"state": "terminal", "status": "unknown"}}

    mutate(execution, seed)
    before = load(execution)
    capability = before[GRANTS][unused]["id"]
    assert valid_grant(before, scope, unused, capability)
    if interactive:
        async def scenario():
            tools = AssistantTools(manager, OWNER, sid, None)
            task = asyncio.create_task(tools.dispatch("request_user_interaction", {
                "request": {"purpose": "authorization", "capability": "screenshot"}}))
            while not load(execution).get(PENDING):
                await asyncio.sleep(0)
            await respond_interaction(manager, OWNER, sid, load(execution)[PENDING]["id"], InteractionResponse(decision="approve"))
            assert (await task)["ok"]
        asyncio.run(scenario())
    else:
        apply_turn_defaults(manager, OWNER, sid, {"auto_authorize_screenshot": True})
    after = load(execution)
    assert after[GRANTS][unused]["status"] == "expired"
    assert after[GRANTS][used] == before[GRANTS][used]
    assert after[RECORDS] == before[RECORDS]
    assert after.get("_target_revision", 0) == before.get("_target_revision", 0)
    # Recreating the exact fingerprint cannot revive old authority.
    mutate(execution, lambda d: d.update(target=original))
    assert not valid_grant(load(execution), scope, unused, capability)


def test_unchanged_defaults_leave_existing_retry_grant_intact(execution):
    from app.services.assistant_retry import GRANTS, issue_grant

    manager, sid = execution.manager, execution.session_id
    apply_turn_defaults(manager, OWNER, sid, {"auto_authorize_execution": True})
    mutate(execution, lambda d: issue_grant(d, manager.browsers[sid], "unused", {"feature_type": "action.click"}))
    before = deepcopy(load(execution)[GRANTS])
    apply_turn_defaults(manager, OWNER, sid, {"auto_authorize_execution": True})
    assert load(execution)[GRANTS] == before


@pytest.mark.parametrize("change", ["revision", "turn", "target"])
def test_stale_binding_cannot_grant_permission(execution, change):
    async def scenario():
        tools = AssistantTools(execution.manager, OWNER, execution.session_id, None)
        task = asyncio.create_task(tools.dispatch("request_user_interaction", {"request": {"purpose": "proxy", "enabled": True}}))
        while not load(execution).get(PENDING):
            await asyncio.sleep(0)
        item = load(execution)[PENDING]
        if change == "revision":
            mutate(execution, lambda d: d.update(_target_revision=99))
        elif change == "turn":
            mutate(execution, lambda d: d.update(_interaction_turn_id="another-turn"))
        else:
            mutate(execution, lambda d: d["target"].update(name="Changed"))
        with pytest.raises(AssistantError):
            await respond_interaction(execution.manager, OWNER, execution.session_id, item["id"], InteractionResponse(
                decision="approve", selections=["proxy_general"]))
        assert (await task)["code"] == "interaction_invalidated"
    asyncio.run(scenario())
    assert not load(execution)["target"]["proxy"]


@pytest.mark.parametrize("restart", [False, True])
def test_cancel_restart_leave_no_pending_authority(execution, restart):
    async def scenario():
        manager, sid = execution.manager, execution.session_id
        task = asyncio.create_task(AssistantTools(manager, OWNER, sid, None).dispatch(
            "request_user_interaction", {"request": {"purpose": "proxy", "enabled": True}}))
        while not load(execution).get(PENDING):
            await asyncio.sleep(0)
        if restart:
            fresh = AssistantManager(manager.session_factory, manager.settings, manager.crypto)
            fresh.recover()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert not manager._interaction_waiters and not manager._interaction_keys
    asyncio.run(scenario())
    data = load(execution)
    assert not data.get(PENDING) and not data["target"]["proxy"]
    assert next(iter(data[LEDGER].values()))["status"] == "cancelled"


def test_remaining_deadline_terminal_and_ledger_bound(execution):
    async def scenario():
        tools = AssistantTools(execution.manager, OWNER, execution.session_id, None,
                               turn={"id": str(uuid4()), "deadline": asyncio.get_running_loop().time() - 1})
        result = await tools.dispatch("request_user_interaction", {"request": TARGET})
        assert result["code"] == "interaction_timed_out"
        assert await tools.dispatch("request_user_interaction", {"request": TARGET}) == result
        assert not load(execution).get(PENDING)
    asyncio.run(scenario())
    assert len(load(execution)[LEDGER]) == 1


def test_import_approval_uses_original_proposal_same_call(context):
    tools = configure(context, workflow_consent=False)
    proposal_id = submit(tools)
    manager, sid = context

    async def scenario():
        task = asyncio.create_task(tools.dispatch("import_workflow_draft", {"proposal_message_id": proposal_id}))
        while True:
            with manager.session_factory() as db:
                data = manager._decrypt(db.get(AssistantSession, sid))
            if data.get(PENDING):
                break
            await asyncio.sleep(0)
        await respond_interaction(manager, tools.owner, sid, data[PENDING]["id"], InteractionResponse(decision="approve"))
        imported = await task
        assert imported["ok"] and imported["proposal_message_id"] == proposal_id
        assert imported["lane"] == "general"  # Browser proxy remains independent.
        replay = await tools.dispatch("import_workflow_draft", {"proposal_message_id": proposal_id})
        assert replay["replayed"] and replay["site_id"] == imported["site_id"]

    asyncio.run(scenario())


def test_screenshot_approval_continues_and_persists(execution):
    manager, sid = execution.manager, execution.session_id
    manager.browser_tool = AsyncMock(return_value={"ok": True, "image": "safe-metadata"})

    async def scenario():
        tools = AssistantTools(manager, OWNER, sid, None, config={"supports_vision": True})
        task = asyncio.create_task(tools.dispatch("browser_screenshot", {}))
        while not load(execution).get(PENDING):
            await asyncio.sleep(0)
        await respond_interaction(manager, OWNER, sid, load(execution)[PENDING]["id"], InteractionResponse(decision="approve"))
        assert (await task)["ok"]
        assert (await tools.dispatch("browser_screenshot", {}))["ok"]
        assert not load(execution).get(PENDING)
    asyncio.run(scenario())
    assert manager.browser_tool.await_count == 2


def test_ledger_prunes_previous_turns_but_never_current_turn(execution):
    async def scenario():
        tools = AssistantTools(execution.manager, OWNER, execution.session_id, None,
                               turn={"id": "current", "deadline": asyncio.get_running_loop().time() - 1})
        mutate(execution, lambda d: d.update(**{LEDGER: {
            str(i): {"id": str(uuid4()), "status": "denied", "turn_id": "previous"} for i in range(256)}}))
        assert (await tools.dispatch("request_user_interaction", {"request": TARGET}))["code"] == "interaction_timed_out"
        assert len(load(execution)[LEDGER]) == 256
        mutate(execution, lambda d: d.update(**{LEDGER: {
            str(i): {"id": str(uuid4()), "status": "denied", "turn_id": "current"} for i in range(256)}}))
        assert (await tools.dispatch("request_user_interaction", {"request": TARGET}))["code"] == "interaction_limit"
        assert not load(execution).get(PENDING)
    asyncio.run(scenario())


@pytest.mark.parametrize("purpose", ["proxy", "authorization"])
def test_restart_cancels_approved_but_unconsumed_interaction(execution, purpose):
    mutate(execution, lambda d: d["target"].update(screenshot_consent=False))
    async def scenario():
        manager, sid = execution.manager, execution.session_id
        request_ = ({"purpose": "proxy", "enabled": True} if purpose == "proxy" else
                    {"purpose": "authorization", "capability": "screenshot"})
        task = asyncio.create_task(AssistantTools(manager, OWNER, sid, None).dispatch(
            "request_user_interaction", {"request": request_}))
        while not load(execution).get(PENDING):
            await asyncio.sleep(0)
        item = load(execution)[PENDING]
        response = InteractionResponse(decision="approve", **(
            {"selections": ["proxy_general"]} if purpose == "proxy" else {}))
        await respond_interaction(manager, OWNER, sid, item["id"], response)
        fresh = AssistantManager(manager.session_factory, manager.settings, manager.crypto)
        fresh.recover()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        with pytest.raises(AssistantError):
            await respond_interaction(manager, OWNER, sid, item["id"], response)
    asyncio.run(scenario())
    assert not load(execution)["target"]["proxy"]
    assert not load(execution)["target"]["screenshot_consent"]
    assert next(iter(load(execution)[LEDGER].values()))["status"] == "cancelled"


@pytest.mark.parametrize("secret_title", [
    "Enter password", "Enter your 2FA", "Provide MFA", "one-time code", "one_time_code",
    "one time code", "authentication-code", "authentication_code", "auth code",
    "recovery-code", "recovery_code", "passcode", "backup code",
])
def test_secret_question_never_persisted_in_model_history(assistant_client, provider, secret_title):
    client = assistant_client
    sid = create(client)
    provider[0].append(interaction({"purpose": "question", "kind": "input",
                                    "title": secret_title, "fields": [{"id": "value", "label": "Value"}]}))
    send(client, sid)
    detail = wait(client, sid)
    assert not detail["pending_interaction"]
    assert not any(m.get("tool_name") == "request_user_interaction" for m in detail["messages"])
    assert "secret_output" in detail["messages"][-1]["content"]


@pytest.mark.parametrize("label", [
    "2FA", "2_FA", "MFA", "one-time code", "one_time_code", "one time code",
    "authentication-code", "authentication_code", "auth code", "recovery-code",
    "recovery_code", "passcode", "backup code",
])
def test_secret_code_response_guard(label):
    from app.services.assistant_interactions import unsafe_model_interaction, validate_response

    assert unsafe_model_interaction({"request": {"purpose": "question", "kind": "input",
        "title": "Provide details", "fields": [{"id": "value", "label": label}]}}, "")

    response = InteractionResponse(decision="approve", values={"value": label + ": 123456"})
    item = {"kind": "input", "fields": [{"id": "value", "required": True}]}
    with pytest.raises(AssistantError) as caught:
        validate_response(item, response, "")
    assert caught.value.code == "secret_input"
    assert secret_input(label + "=123456")


@pytest.mark.parametrize("auto_compact", [False, True])
@pytest.mark.parametrize("purpose", ["target", "trusted_origins", "proxy"])
def test_target_revision_removes_live_and_queued_pixels_and_prevents_later_replay(
    assistant_client, provider, monkeypatch, auto_compact, purpose,
):
    from sqlalchemy import select
    from tests.test_assistant_recovery_integration import jpeg, setup_browser, vision

    client = assistant_client
    vision(monkeypatch)
    save(client, auto_title=False, auto_compact=auto_compact)
    manager, sid, scope = setup_browser(client)
    image = jpeg()
    monkeypatch.setattr(manager, "browser_tool", AsyncMock(return_value={
        "ok": True, "url": "https://example.com/", "_image": image}))
    monkeypatch.setattr(manager, "_close_browser", AsyncMock(side_effect=lambda sid: manager.browsers.pop(sid, None)))
    request_ = {
        "target": {"purpose": "target", "target": {"name": "New", "base_url": "https://new.example/"}},
        "trusted_origins": {"purpose": "trusted_origins", "origins": ["https://other.example"]},
        "proxy": {"purpose": "proxy", "enabled": True},
    }[purpose]
    batch = tool("browser_screenshot", call_id="queued_capture")
    batch["choices"][0]["message"]["tool_calls"].extend(
        interaction(request_, "mutate_target")["choices"][0]["message"]["tool_calls"])
    provider[0].extend([tool("browser_screenshot", call_id="live_capture"), batch, text("Changed safely")])
    send(client, sid)
    item = pending(client, sid)
    # The second request already contains the first screenshot, while the second
    # screenshot waits in the current batch ahead of the target mutation.
    assert "data:image" in provider[1][1].content.decode()
    assert client.post(url(sid, item["id"]), json={"decision": "approve", **(
        {"selections": ["proxy_general"]} if purpose == "proxy" else {})}).status_code == 200
    detail = wait(client, sid)
    assert detail["messages"][-1]["content"] == "Changed safely"
    assert "data:image" not in provider[1][-1].content.decode()
    with manager.session_factory() as db:
        data = manager._decrypt(db.get(AssistantSession, sid))
        images = [manager._decrypt(row) for row in db.scalars(select(AssistantMessage).where(
            AssistantMessage.session_id == sid)) if manager._decrypt(row).get("context_image")]
    assert len(images) == 2
    assert all(value["_target_revision"] == 1 for value in images)
    assert data["_target_revision"] == 2
    if purpose == "target":
        assert not detail["target"]["screenshot_consent"]
        provider[0].extend([interaction({"purpose": "authorization", "capability": "screenshot"}, "new_consent"),
                            text("New consent does not restore old pixels")])
        send(client, sid)
        item = pending(client, sid)
        assert client.post(url(sid, item["id"]), json={"decision": "approve"}).status_code == 200
        assert wait(client, sid)["target"]["screenshot_consent"]
        assert "data:image" not in provider[1][-1].content.decode()
    provider[0].append(text("History remains pixel-free"))
    send(client, sid)
    assert wait(client, sid)["messages"][-1]["content"] == "History remains pixel-free"
    assert "data:image" not in provider[1][-1].content.decode()


@pytest.mark.parametrize("capability,title", [
    ("execution", "\u6388\u6743\u771f\u5b9e\u6267\u884c"),
    ("screenshot", "\u6388\u6743\u622a\u56fe\u53d1\u9001"),
    ("workflow", "\u6388\u6743\u4fdd\u5b58\u5e76\u8fd0\u884c"),
])
def test_host_authorization_localized_and_combined_workflow_scope(execution, capability, title):
    from app.services.assistant_interactions import prepare

    request_ = ADAPTER.validate_python({"purpose": "authorization", "capability": capability})
    with execution.manager.session_factory() as db:
        public, _, _ = prepare(db, request_, load(execution), {})
    assert public["title"] == title
    assert public["review"]["run_lane_policy"] == "auto"
    assert "\u8d39\u7528" in public["description"]
    if capability == "workflow":
        assert all(term in public["description"] for term in ("\u4fdd\u5b58", "\u5b8c\u6574", "\u8fd0\u884c", "\u771f\u5b9e", "\u51ed\u636e"))


def test_endpoint_cancel_cleans_interaction_and_waiter(assistant_client, provider):
    client = assistant_client
    sid = create(client)
    provider[0].append(interaction(TARGET))
    send(client, sid)
    item = pending(client, sid)
    response = client.post(f"{ROOT}/{sid}/cancel")
    assert response.status_code == 200
    detail = response.json()
    assert detail["status"] == "idle" and detail["pending_interaction"] is None
    manager = client.app.state.assistant_manager
    assert not manager._interaction_waiters and not manager._interaction_keys
    assert client.post(url(sid, item["id"]), json={"decision": "approve"}).status_code == 409
    with manager.session_factory() as db:
        data = manager._decrypt(db.get(AssistantSession, sid))
    assert not data.get(PENDING) and data["target"] is None
    assert next(iter(data[LEDGER].values()))["status"] == "cancelled"


@pytest.mark.parametrize("value", ["password=abc", "Cookie: abc", "密码：秘密", "Bearer abc",
                                  '{"password":"secret-value"}'])
def test_shared_secret_guard(value):
    assert secret_input(value)
