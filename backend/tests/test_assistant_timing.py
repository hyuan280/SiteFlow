"""Human waits pause the turn, not provider requests or approved execution."""

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from app.schemas.assistant import InteractionResponse
from app.services.assistant import AssistantManager
from app.services.assistant_interactions import (
    INTERACTION_TIMEOUT, LEDGER, PENDING as INTERACTION, respond_interaction,
)
from app.services.assistant_retry import (
    APPROVAL_TIMEOUT, DECISIONS, GRANTS, PENDING as APPROVAL, RECORDS, decide_retry_approval,
)
from app.services.assistant_tools import AssistantTools
from tests.test_assistant_execution import OWNER, arguments, execution, response  # noqa: F401
from tests.test_assistant_retry import load, mutate, unknown


@pytest.fixture(autouse=True)
def legacy_approval_timing(monkeypatch):
    # Exercise the retained dialog lifecycle explicitly, not ordinary non-post
    # routing. Actual post features remain unsupported by isolated execution.
    monkeypatch.setattr("app.services.assistant_retry.execution_category", lambda db, key: "post")


@pytest.mark.parametrize("kind", ["interaction", "retry"])
@pytest.mark.parametrize("outcome", ["approve", "deny", "timeout", "active_timeout", "stop", "shutdown"])
@pytest.mark.parametrize("startup_delay", [0, 0.35])
def test_real_turn_pauses_only_human_wait(execution, monkeypatch, kind, outcome, startup_delay):
    manager, sid = execution.manager, execution.session_id
    if kind == "retry":
        original = unknown(execution)["operation_id"]
        mutate(execution, lambda data: None, status="running")
        pending, ledger = APPROVAL, DECISIONS
        name, args = "retry_step", {"operation_id": original}
        assert APPROVAL_TIMEOUT == 180
    else:
        mutate(execution, lambda data: data["target"].update(execution_consent=False))
        pending, ledger = INTERACTION, LEDGER
        name, args = "execute_step", arguments()
        assert INTERACTION_TIMEOUT == 180
    manager._worker.reset_mock()
    if outcome == "timeout":
        monkeypatch.setattr(
            "app.services.assistant_retry.APPROVAL_TIMEOUT" if kind == "retry" else
            "app.services.assistant_interactions.INTERACTION_TIMEOUT", 0.6)

    async def scenario():
        loop = asyncio.get_running_loop()
        waiting = asyncio.Event()
        state = {"paused": 0.0}
        wait_for_user = manager._wait_for_user

        async def observed_wait(turn, event, timeout):
            if not waiting.is_set():
                # Setup is not the timing subject. Start the short measured
                # budget at the first human wait, retaining it for approval work.
                turn["deadline"] = loop.time() + 0.3
                turn["timeout"].reschedule(turn["deadline"])
                state["initial_deadline"] = turn["deadline"]
            before, deadline = loop.time(), turn["deadline"]
            waiting.set()
            try:
                await wait_for_user(turn, event, timeout)
            finally:
                elapsed = loop.time() - before
                state["paused"] += elapsed
                assert turn["deadline"] - deadline == pytest.approx(elapsed, abs=0.002)
                assert turn["timeout"].when() == turn["deadline"]

        async def worker(path, payload):
            turn = manager._active_turns[sid]
            assert not turn.get("_waiting_for_user")
            assert turn["timeout"].when() == turn["deadline"]
            state["remaining_at_execution"] = turn["deadline"] - loop.time()
            if outcome == "active_timeout":
                await asyncio.Event().wait()
            await asyncio.sleep(0.03)  # Execution time must not be credited back.
            return response(manager.browsers[sid], payload["operation_id"])

        async def conversation(owner, session_id, config):
            turn = manager._active_turns[sid]
            state["turn"] = turn
            state["initial_deadline"] = turn["deadline"]
            tools = AssistantTools(manager, owner, sid, None, turn=turn)
            await asyncio.sleep(startup_delay)
            state["result"] = await tools.dispatch(name, args)
            state["remaining_after"] = turn["deadline"] - loop.time()
            if outcome in {"deny", "timeout"}:
                assert await tools.dispatch(name, args) == state["result"]
                assert len(load(execution)[ledger]) == 1

        monkeypatch.setattr(manager, "_wait_for_user", observed_wait)
        monkeypatch.setattr(manager, "_conversation", conversation)
        monkeypatch.setattr(manager, "_auto_title", AsyncMock())
        manager._worker.side_effect = worker
        config = {"_assistant_settings": {"turn_timeout_seconds": 10}}
        task = asyncio.create_task(manager._run_turn(OWNER, sid, config))
        manager.tasks[sid] = task
        observer = asyncio.create_task(waiting.wait())
        try:
            done, _ = await asyncio.wait({task, observer}, timeout=2,
                                         return_when=asyncio.FIRST_COMPLETED)
            assert observer in done and not task.done(), manager.detail(OWNER, sid)["messages"]
        finally:
            observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)
        item = load(execution)[pending]
        # A prompt gets its own full cap, even with < 1 second of active time.
        if outcome != "timeout":
            assert 179 < item["expires_epoch"] - time.time() <= 180
        await asyncio.sleep(0.4)  # Longer than the entire active turn budget.
        assert not task.done()
        assert manager._worker.await_count == 0
        # An unrelated writer/decision task neither holds a wait lock nor credits time.
        async with manager._mutation(OWNER, sid):
            mutate(execution, lambda data: data.update(unrelated="preserved"))
        deadline = state["turn"]["deadline"]
        if outcome in {"stop", "shutdown"}:
            async with asyncio.timeout(0.2):
                if outcome == "stop":
                    await manager.cancel(OWNER, sid)
                else:
                    await manager.shutdown()
        elif outcome != "timeout":
            decision = "deny" if outcome == "deny" else "approve"
            if kind == "retry":
                await decide_retry_approval(manager, OWNER, sid, item["id"], decision)
            else:
                await respond_interaction(manager, OWNER, sid, item["id"], InteractionResponse(decision=decision))
            assert state["turn"]["deadline"] == deadline
        async with asyncio.timeout(2):
            await task
        saved = load(execution)
        assert saved["unrelated"] == "preserved"
        assert not saved.get(pending)
        assert not manager.tasks and not manager._active_turns
        assert not manager._approval_waiters and not manager._interaction_waiters
        assert "timeout" not in state["turn"]
        assert state["turn"]["deadline"] == pytest.approx(
            state["initial_deadline"] + state["paused"], abs=0.005)
        content = manager.detail(OWNER, sid)["messages"][-1]["content"]
        if outcome == "active_timeout":
            assert "[provider_timeout]" in content
            assert manager._worker.await_count == 1
            assert any(record["status"] == "unknown" for record in saved[RECORDS].values())
            manager._auto_title.assert_not_awaited()
        elif outcome in {"stop", "shutdown"}:
            assert "[cancelled]" in content
            assert next(iter(saved[ledger].values()))["status"] == "cancelled"
            manager._worker.assert_not_awaited()
            manager._auto_title.assert_not_awaited()
        else:
            manager._auto_title.assert_awaited_once_with(
                OWNER, sid, config, state["turn"]["deadline"])
            if outcome == "approve":
                assert state["result"]["ok"] and manager._worker.await_count == 1
                assert 0.15 < state["remaining_at_execution"] <= 0.3
                assert state["remaining_after"] < state["remaining_at_execution"] - 0.025
            else:
                assert state["result"]["code"] == {
                    ("interaction", "deny"): "interaction_denied",
                    ("interaction", "timeout"): "interaction_timed_out",
                    ("retry", "deny"): "retry_denied",
                    ("retry", "timeout"): "retry_approval_timeout",
                }[kind, outcome]
                manager._worker.assert_not_awaited()

    asyncio.run(scenario())


def test_active_setup_time_is_not_paused(execution, monkeypatch):
    manager, sid = execution.manager, execution.session_id
    mutate(execution, lambda data: data["target"].update(execution_consent=False))

    async def scenario():
        async def conversation(owner, session_id, config):
            tools = AssistantTools(manager, owner, sid, None, turn=manager._active_turns[sid])
            await asyncio.sleep(0.4)
            await tools.dispatch("execute_step", arguments())

        wait_for_user = AsyncMock(wraps=manager._wait_for_user)
        monkeypatch.setattr(manager, "_wait_for_user", wait_for_user)
        monkeypatch.setattr(manager, "_conversation", conversation)
        monkeypatch.setattr(manager, "_auto_title", AsyncMock())
        await asyncio.wait_for(manager._run_turn(OWNER, sid, {
            "_assistant_settings": {"turn_timeout_seconds": 0.3},
        }), timeout=2)
        wait_for_user.assert_not_awaited()
        manager._worker.assert_not_awaited()
        manager._auto_title.assert_not_awaited()
        assert not load(execution).get(INTERACTION)
        assert not load(execution).get(LEDGER)
        assert not manager._active_turns and not manager.tasks
        assert "[provider_timeout]" in manager.detail(OWNER, sid)["messages"][-1]["content"]

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["interaction", "retry"])
def test_approval_event_and_stop_same_loop_turn_cannot_swallow_cancellation(
    execution, monkeypatch, kind,
):
    manager, sid = execution.manager, execution.session_id
    if kind == "retry":
        operation = unknown(execution)["operation_id"]
        mutate(execution, lambda data: None, status="running")
        name, args, pending, ledger = "retry_step", {"operation_id": operation}, APPROVAL, DECISIONS
    else:
        mutate(execution, lambda data: data["target"].update(execution_consent=False))
        name, args, pending, ledger = "execute_step", arguments(), INTERACTION, LEDGER
    manager._worker.reset_mock()
    next_provider = AsyncMock()

    async def scenario():
        waiting = asyncio.Event()
        state = {}
        wait_for_user = manager._wait_for_user

        async def observed_wait(turn, event, timeout):
            state.update(turn=turn, event=event, outer=turn["timeout"])
            # Queue the observer before wait_for creates its event task. That
            # task then enters Event.wait before the observer resumes us.
            asyncio.get_running_loop().call_soon(waiting.set)
            await wait_for_user(turn, event, timeout)

        async def conversation(owner, session_id, config):
            tools = AssistantTools(manager, owner, session_id, None,
                                   turn=manager._active_turns[session_id])
            await tools.dispatch(name, args)
            await next_provider()

        monkeypatch.setattr(manager, "_wait_for_user", observed_wait)
        monkeypatch.setattr(manager, "_conversation", conversation)
        monkeypatch.setattr(manager, "_auto_title", AsyncMock())
        task = asyncio.create_task(manager._run_turn(OWNER, sid, {
            "_assistant_settings": {"turn_timeout_seconds": 10},
        }))
        manager.tasks[sid] = task
        async with asyncio.timeout(1):
            await waiting.wait()
        item = load(execution)[pending]
        assert state["outer"].when() is None
        assert not state["event"].is_set() and not task.done()
        # Both routes complete without yielding with an uncontended mutation
        # lock. Queue cancellation immediately after the event waiter wakes,
        # before wait_for's completion callback can resume the parent turn.
        if kind == "retry":
            await decide_retry_approval(manager, OWNER, sid, item["id"], "approve")
        else:
            await respond_interaction(manager, OWNER, sid, item["id"],
                                      InteractionResponse(decision="approve"))
        assert state["event"].is_set()
        assert next(iter(load(execution)[ledger].values()))["status"] == "approved"
        asyncio.get_running_loop().call_soon(task.cancel)
        async with asyncio.timeout(1):
            await task  # The real turn catches cancellation and persists its terminal outcome.

        manager._worker.assert_not_awaited()
        next_provider.assert_not_awaited()
        manager._auto_title.assert_not_awaited()
        manager._close_browser.assert_awaited_once_with(sid)
        saved = load(execution)
        assert next(iter(saved[ledger].values()))["status"] == "cancelled"
        assert not saved.get(pending)
        if kind == "retry":
            assert saved[GRANTS][operation]["status"] == "cancelled"
            assert set(saved[RECORDS]) == {operation}
        else:
            assert not saved["target"]["execution_consent"]
            assert not saved.get(RECORDS)
        detail = manager.detail(OWNER, sid)
        assert detail["status"] == "idle"
        assert "[cancelled]" in detail["messages"][-1]["content"]
        assert not manager.tasks and not manager._active_turns
        assert not manager._approval_waiters and not manager._interaction_waiters
        assert not manager._interaction_keys and not manager.browsers
        assert "timeout" not in state["turn"] and "_waiting_for_user" not in state["turn"]
        assert state["outer"].when() == state["turn"]["deadline"]
        assert not state["outer"].expired()

    asyncio.run(scenario())


def test_direct_tool_turn_without_outer_timeout_preserves_active_budget():
    async def scenario():
        loop = asyncio.get_running_loop()
        turn = {"deadline": loop.time() + 0.02}
        deadline = turn["deadline"]
        event = asyncio.Event()
        loop.call_later(0.05, event.set)
        await AssistantManager._wait_for_user(turn, event, 1)
        assert turn["deadline"] - deadline >= 0.05
        assert 0 < turn["deadline"] - loop.time() <= 0.02
        # Sequential prompts credit only their own wait, not intervening active work.
        await asyncio.sleep(0.005)
        remaining = turn["deadline"] - loop.time()
        event.clear()
        loop.call_later(0.05, event.set)
        await AssistantManager._wait_for_user(turn, event, 1)
        assert turn["deadline"] - loop.time() == pytest.approx(remaining, abs=0.002)
        turn["deadline"] = loop.time() - 1
        with pytest.raises(TimeoutError):
            await AssistantManager._wait_for_user(turn, event, 1)

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["interaction", "retry"])
def test_direct_dispatch_without_outer_timeout_updates_shared_deadline(execution, kind):
    manager, sid = execution.manager, execution.session_id
    if kind == "retry":
        operation = unknown(execution)["operation_id"]
        mutate(execution, lambda data: None, status="running")
        name, args, pending = "retry_step", {"operation_id": operation}, APPROVAL
    else:
        name, args, pending = "request_user_interaction", {"request": {
            "purpose": "question", "kind": "authorization", "title": "Continue?",
        }}, INTERACTION

    async def scenario():
        loop = asyncio.get_running_loop()
        turn = {"id": "direct-turn", "deadline": loop.time() + 0.1}
        deadline = turn["deadline"]
        tools = AssistantTools(manager, OWNER, sid, None, turn=turn)
        task = asyncio.create_task(tools.dispatch(name, args))
        async with asyncio.timeout(1):
            while not load(execution).get(pending):
                await asyncio.sleep(0)
        await asyncio.sleep(0.15)
        item = load(execution)[pending]
        if kind == "retry":
            await decide_retry_approval(manager, OWNER, sid, item["id"], "deny")
        else:
            await respond_interaction(manager, OWNER, sid, item["id"], InteractionResponse(decision="deny"))
        async with asyncio.timeout(1):
            result = await task
        assert result["code"] == ("retry_denied" if kind == "retry" else "interaction_denied")
        assert turn["deadline"] - deadline >= 0.15
        assert 0 < turn["deadline"] - loop.time() < 0.1
        assert "timeout" not in turn and "_waiting_for_user" not in turn

    asyncio.run(scenario())
