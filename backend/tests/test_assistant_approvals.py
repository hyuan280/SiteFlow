from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime
import json
import time
from uuid import UUID, uuid4

import pytest
from app.models.assistant import AssistantSession
from app.services.assistant import AssistantError, AssistantManager
from app.services.assistant_retry import (
    DECISIONS, GRANTS, PENDING, RECORDS, decide_retry_approval, run_authorized_retry,
)
from app.services.assistant_tools import AssistantTools
from tests.test_assistant_api import (  # noqa: F401
    ROOT, assistant_client, create, provider, send, target, text, tool, wait,
)
from tests.test_assistant_execution import (  # noqa: F401
    OWNER, TOKEN, arguments, execution, response,
)
from tests.test_assistant_retry import load, mutate, succeed, unknown
from tests.test_assistant_runtime_settings import save


def pending(client, sid):
    for _ in range(300):
        detail = client.get(f"{ROOT}/{sid}").json()
        if detail["pending_approval"]:
            return detail
        time.sleep(0.01)
    pytest.fail("No pending approval appeared")


def decision_url(sid, approval):
    return f"{ROOT}/{sid}/retry-approvals/{approval}/decision"


def prepare(client, provider):
    from app.services.assistant import BrowserScope

    save(client, auto_title=False)
    sid = create(client)
    target(client, sid, authorized=True)
    manager = client.app.state.assistant_manager
    scope = BrowserScope("admin", str(uuid4()), TOKEN, "interaction", "", time.monotonic() + 900)
    manager.browsers[sid] = scope
    args = arguments("action.click", {"selector": ["#approved"]})
    provider[0].extend([
        tool("execute_step", args, "original"),
        response(scope, args["operation_id"], status="unknown", code="outcome_uncertain"),
        text("Inspect before retrying"),
    ])
    send(client, sid)
    assert wait(client, sid)["pending_approval"] is None
    return manager, sid, scope, args


@pytest.mark.parametrize("retry_tool,reuse_original", [
    ("retry_step", True), ("execute_step", True), ("execute_step", False),
])
def test_same_call_approval_exactly_one_worker_no_new_message(
    assistant_client, provider, retry_tool, reuse_original,
):
    client = assistant_client
    manager, sid, scope, args = prepare(client, provider)
    captured = []

    async def executed(request):
        payload = json.loads(request.content)
        captured.append(payload)
        return response(scope, payload["operation_id"])

    retry_args = ({"operation_id": args["operation_id"]} if retry_tool == "retry_step"
                  else {**args, "operation_id": args["operation_id"] if reuse_original else str(uuid4())})
    provider[0].extend([tool(retry_tool, retry_args, "waiting_call"), executed, text("Retried")])
    send(client, sid, content="Retry the uncertain action")
    detail = pending(client, sid)
    approval = detail["pending_approval"]
    assert detail["status"] == "running"
    assert set(approval) == {"id", "operation_id", "feature_type", "status", "created_at", "expires_at", "title"}
    assert UUID(approval["id"]).version == 4
    assert approval["operation_id"] == args["operation_id"] and approval["status"] == "pending"
    assert (datetime.fromisoformat(approval["expires_at"]) -
            datetime.fromisoformat(approval["created_at"])).total_seconds() == 180
    log = detail["messages"][-1]
    assert log["tool_name"] == retry_tool and log["status"] == "running" and not log["content"]
    assert len(captured) == 0
    # GET/SSE snapshots are pure reads. Reconnects cannot create another request.
    assert client.get(f"{ROOT}/{sid}").json()["pending_approval"] == approval
    assert client.get(f"{ROOT}/{sid}").json()["pending_approval"] == approval
    with manager.session_factory() as db:
        stored = manager._decrypt(db.get(AssistantSession, sid))
        assert not stored.get(GRANTS)
        frozen = deepcopy(stored[RECORDS][args["operation_id"]]["step"])
        assert stored[PENDING]["invocation"]["tool_message_id"] == log["id"]
        assert stored[PENDING]["invocation"]["tool_call_id"] == "waiting_call"
    url = decision_url(sid, approval["id"])
    accepted = client.post(url, json={"decision": "approve"})
    assert accepted.status_code == 200 and accepted.headers["cache-control"] == "no-store"
    assert accepted.json()["pending_approval"] is None
    detail = wait(client, sid)
    assert detail["messages"][-1]["content"] == "Retried"
    assert len(captured) == 1 and captured[0]["step"] == frozen
    assert captured[0]["retry_of"] == args["operation_id"]
    assert captured[0]["operation_id"] != args["operation_id"]
    assert len([row for row in detail["messages"] if row["role"] == "user"]) == 2
    terminal = next(row for row in detail["messages"] if row["id"] == log["id"])
    assert terminal["status"] == "success" and json.loads(terminal["content"])["ok"]
    assert client.post(url, json={"decision": "approve"}).status_code == 200
    assert client.post(url, json={"decision": "deny"}).status_code == 409
    assert len(captured) == 1 and not manager._approval_waiters and not manager._active_turns
    for private in (TOKEN, captured[0]["retry_authorization"], "step_fingerprint", "browser_binding"):
        assert private not in json.dumps(detail)


@pytest.mark.parametrize("outcome,code", [("deny", "retry_denied"), ("timeout", "retry_approval_timeout")])
def test_denial_timeout_cached_for_turn_but_new_turn_can_ask(
    assistant_client, provider, monkeypatch, outcome, code,
):
    client = assistant_client
    manager, sid, scope, args = prepare(client, provider)
    if outcome == "timeout":
        monkeypatch.setattr("app.services.assistant_retry.APPROVAL_TIMEOUT", 0.15)
    original = args["operation_id"]
    provider[0].extend([
        tool("retry_step", {"operation_id": original}, "first"),
        tool("retry_step", {"operation_id": original}, "repeated"), text("Stopped"),
    ])
    before = len(provider[1])
    send(client, sid)
    approval = pending(client, sid)["pending_approval"]
    url = decision_url(sid, approval["id"])
    if outcome == "deny":
        assert client.post(url, json={"decision": "deny"}).status_code == 200
    detail = wait(client, sid)
    retries = [item for item in detail["messages"] if item.get("tool_name") == "retry_step"]
    assert [json.loads(item["content"])["code"] for item in retries] == [code, code]
    assert all(item["status"] == "error" for item in retries)
    assert detail["pending_approval"] is None and not manager._approval_waiters
    assert len(provider[1]) - before == 3  # model only, no worker
    if outcome == "deny":
        assert client.post(url, json={"decision": "deny"}).status_code == 200
    assert client.post(url, json={"decision": "approve"}).status_code == 409
    provider[0].extend([tool("execute_step", {**args, "operation_id": str(uuid4())}, "new_turn"), text("Stopped again")])
    send(client, sid)
    second = pending(client, sid)["pending_approval"]
    assert second["id"] != approval["id"]
    assert client.post(decision_url(sid, second["id"]), json={"decision": "deny"}).status_code == 200
    assert wait(client, sid)["pending_approval"] is None


def test_decision_auth_csrf_validation_and_owner(assistant_client, provider):
    client = assistant_client
    manager, sid, scope, args = prepare(client, provider)
    provider[0].extend([tool("retry_step", {"operation_id": args["operation_id"]}), text("Denied")])
    send(client, sid)
    approval = pending(client, sid)["pending_approval"]
    url = decision_url(sid, approval["id"])
    token = client.headers.pop("X-CSRF-Token")
    assert client.post(url, json={"decision": "approve"}).status_code == 403
    client.headers["X-CSRF-Token"] = token
    for body in ({}, {"decision": True}, {"decision": "approved"}, {"decision": "approve", "step": {}}):
        result = client.post(url, json=body)
        assert result.status_code == 422 and result.json() == {"detail": {"code": "invalid_request"}}
    assert client.post(decision_url(sid, str(uuid4())), json={"decision": "approve"}).status_code == 404
    with pytest.raises(AssistantError) as caught:
        asyncio.run(decide_retry_approval(manager, "other", sid, approval["id"], "approve"))
    assert caught.value.status == 404
    assert client.post(url, json={"decision": "deny"}).status_code == 200
    wait(client, sid)
    client.cookies.clear()
    assert client.post(url, json={"decision": "approve"}).status_code == 401


@pytest.mark.parametrize("change", ["target", "revision", "browser", "step"])
def test_binding_changes_block_approval_and_worker(execution, change):
    original = unknown(execution)["operation_id"]
    mutate(execution, lambda data: None, status="running")
    before = execution.manager._worker.await_count

    async def scenario():
        tools = AssistantTools(execution.manager, OWNER, execution.session_id, execution.target)
        task = asyncio.create_task(tools.dispatch("retry_step", {"operation_id": original}))
        while not load(execution).get(PENDING):
            await asyncio.sleep(0)
        approval = load(execution)[PENDING]
        if change == "browser":
            execution.manager.browsers[execution.session_id].token = "replacement"
        elif change == "target":
            mutate(execution, lambda data: data["target"].update(name="Changed"))
        elif change == "revision":
            mutate(execution, lambda data: data.update(_target_revision=9))
        else:
            mutate(execution, lambda data: data[RECORDS][original]["step"]["config"].update(button_selector=["#changed"]))
        with pytest.raises(AssistantError) as caught:
            await decide_retry_approval(execution.manager, OWNER, execution.session_id, approval["id"], "approve")
        assert caught.value.code == "retry_approval_invalidated"
        result = await task
        assert result["code"] == "retry_approval_invalidated" and result["reason"] == "retry_binding_changed"

    asyncio.run(scenario())
    assert execution.manager._worker.await_count == before
    assert not load(execution).get(PENDING) and not load(execution).get(GRANTS)


def test_no_locks_held_while_waiting_and_grant_bound_to_call(execution):
    original = unknown(execution)["operation_id"]
    mutate(execution, lambda data: None, status="running")
    succeed(execution)

    async def scenario():
        manager, sid = execution.manager, execution.session_id
        tools = AssistantTools(manager, OWNER, sid, execution.target)
        tools.call_context = {"tool_message_id": 12, "tool_call_id": "host-call"}
        task = asyncio.create_task(tools.dispatch("retry_step", {"operation_id": original}))
        while not load(execution).get(PENDING):
            await asyncio.sleep(0)
        async with asyncio.timeout(1):
            async with manager._mutation(OWNER, sid):
                # This independent writer proves no database transaction spans the wait.
                mutate(execution, lambda data: data.update(unrelated="preserved"))
        approval = load(execution)[PENDING]
        # Deliberately lose the event; bounded polling still resumes the call.
        event = manager._approval_waiters.pop(approval["id"])
        await decide_retry_approval(manager, OWNER, sid, approval["id"], "approve")
        assert not event.is_set()
        assert (await run_authorized_retry(manager, OWNER, sid, original))["code"] == "retry_authorization_required"
        assert (await task)["ok"]
        assert load(execution)["unrelated"] == "preserved"

    asyncio.run(scenario())
    assert not execution.manager._approval_waiters


def test_remaining_turn_budget_returns_structured_timeout_without_dialog(execution):
    original = unknown(execution)["operation_id"]
    mutate(execution, lambda data: None, status="running")

    async def scenario():
        tools = AssistantTools(execution.manager, OWNER, execution.session_id, execution.target,
                               turn={"id": str(uuid4()), "deadline": asyncio.get_running_loop().time() - 1})
        result = await tools.dispatch("retry_step", {"operation_id": original})
        assert result["code"] == "retry_approval_timeout"
        assert await tools.dispatch("retry_step", {"operation_id": original}) == result
        assert len(load(execution)[DECISIONS]) == 1
        assert not load(execution).get(PENDING)

    asyncio.run(scenario())


def test_approval_ledger_is_bounded_without_issuing_authority(execution):
    original = unknown(execution)["operation_id"]
    mutate(execution, lambda data: data.update(**{DECISIONS: {
        str(index): {"id": str(uuid4()), "status": "denied"} for index in range(256)
    }}), status="running")

    async def scenario():
        tools = AssistantTools(execution.manager, OWNER, execution.session_id, execution.target)
        result = await tools.dispatch("retry_step", {"operation_id": original})
        assert result == {"ok": False, "code": "retry_approval_limit"}

    asyncio.run(scenario())
    assert not load(execution).get(PENDING) and not load(execution).get(GRANTS)
    assert len(load(execution)[DECISIONS]) == 256


def test_legacy_reconstruction_uses_same_gate_and_original_revision(execution):
    from app.services.config_ai_generation import _json

    args = unknown(execution)
    mutate(execution, lambda data: data.pop(RECORDS), status="running")
    with execution.manager.session_factory() as db:
        execution.manager._message(db, execution.session_id, "tool", _json({
            "operation_id": args["operation_id"], "status": "unknown", "code": "outcome_uncertain",
        }), tool_name="execute_step", arguments=args, _target_revision=0)
        db.commit()

    async def scenario():
        manager, sid = execution.manager, execution.session_id
        tools = AssistantTools(manager, OWNER, sid, execution.target)
        task = asyncio.create_task(tools.dispatch("execute_step", {**args, "operation_id": str(uuid4())}))
        while not load(execution).get(PENDING):
            await asyncio.sleep(0)
        assert args["operation_id"] in load(execution)[RECORDS]
        approval = load(execution)[PENDING]
        await decide_retry_approval(manager, OWNER, sid, approval["id"], "deny")
        assert (await task)["code"] == "retry_denied"
        mutate(execution, lambda data: (data.pop(RECORDS), data.update(_target_revision=1)))
        other = AssistantTools(manager, OWNER, sid, execution.target)
        result = await other.dispatch("retry_step", {"operation_id": args["operation_id"]})
        assert result == {"ok": False, "code": "retry_step_unavailable"}
        assert not load(execution).get(PENDING)

    asyncio.run(scenario())


def test_execute_mismatch_cannot_bypass_uncertainty(execution):
    original = unknown(execution)["operation_id"]
    mutate(execution, lambda data: None, status="running")

    async def scenario():
        tools = AssistantTools(execution.manager, OWNER, execution.session_id, execution.target)
        result = await tools.dispatch("execute_step", arguments("sign.button", {"button_selector": ["#different"]}))
        assert result["code"] == "retry_step_mismatch" and result["operation_ids"] == [original]
        assert not load(execution).get(PENDING)

    asyncio.run(scenario())
    assert execution.manager._worker.await_count == 1


def test_cancel_pending_call_and_restart_leave_no_authority(assistant_client, provider, monkeypatch):
    client = assistant_client
    manager, sid, scope, args = prepare(client, provider)
    provider[0].append(tool("retry_step", {"operation_id": args["operation_id"]}))
    send(client, sid)
    approval = pending(client, sid)["pending_approval"]
    # Avoid a real close request through the provider fixture.
    async def close(identifier):
        manager.browsers.pop(identifier, None)
    monkeypatch.setattr(manager, "_close_browser", close)
    result = client.post(f"{ROOT}/{sid}/cancel")
    assert result.status_code == 200 and result.json()["status"] == "idle"
    assert result.json()["pending_approval"] is None
    assert json.loads(result.json()["messages"][-2]["content"])["code"] == "interrupted"
    assert not manager._approval_waiters and not manager._active_turns
    assert client.post(decision_url(sid, approval["id"]), json={"decision": "approve"}).status_code == 409
    with manager.session_factory() as db:
        data = manager._decrypt(db.get(AssistantSession, sid))
        assert next(iter(data[DECISIONS].values()))["status"] == "cancelled"
        assert not data.get(PENDING) and not data.get(GRANTS)


@pytest.mark.parametrize("approved", [False, True])
def test_restart_cancels_pending_or_approved_unconsumed_grant(execution, approved):
    original = unknown(execution)["operation_id"]
    mutate(execution, lambda data: None, status="running")

    async def scenario():
        manager, sid = execution.manager, execution.session_id
        tools = AssistantTools(manager, OWNER, sid, execution.target)
        task = asyncio.create_task(tools.dispatch("retry_step", {"operation_id": original}))
        while not load(execution).get(PENDING):
            await asyncio.sleep(0)
        if approved:
            await decide_retry_approval(manager, OWNER, sid, load(execution)[PENDING]["id"], "approve")
        fresh = AssistantManager(manager.session_factory, manager.settings, manager.crypto)
        fresh.recover()
        saved = load(execution)
        assert not saved.get(PENDING)
        assert next(iter(saved[DECISIONS].values()))["status"] == "cancelled"
        if approved:
            assert saved[GRANTS][original]["status"] == "cancelled"
        assert fresh.detail(OWNER, sid)["pending_approval"] is None
        task.cancel()
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        assert isinstance(result, asyncio.CancelledError) or result["code"] == "retry_approval_not_running"
        assert not manager._approval_waiters

    asyncio.run(scenario())
