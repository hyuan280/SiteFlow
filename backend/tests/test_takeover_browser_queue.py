"""Queued admission is one tool call, not model retries or human authorization."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from app.models.assistant import AssistantSession
from app.models.assistant_takeover import AssistantTakeoverAction
from app.models.execution import Run, SiteJob
from app.models.workflow import Site
from app.schemas.assistant_takeover import TakeoverSettingsUpdate
from app.schemas.assistant import SendMessage
from app.services.assistant import AssistantError, AssistantManager
from app.services.assistant_takeover_settings import TakeoverSettingsService
from app.services.assistant_tools import AssistantTools
from tests.test_assistant_takeover import OWNER, ctx, failed_job, grant, tools_for  # noqa: F401
from tests.test_takeover_daily import action_for, install_model, session_data
from tests.test_takeover_live import scope_for
from tests.test_assistant_api import assistant_client, provider, text, tool  # noqa: F401


def view(scope, status):
    return {"session_id": scope.session_id, "lane_key": scope.lane_key, "status": status,
            "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=900)).isoformat()}


@pytest.mark.parametrize("terminal", ["success", "active_timeout", "open_timeout"])
def test_queue_status_io_pauses_budget_but_opening_counts(ctx, monkeypatch, terminal):
    grant(ctx, live_access=True)
    failed_job(ctx)
    tools, _ = tools_for(ctx)
    manager, sid = tools.manager, tools.session_id
    monkeypatch.setattr("app.services.assistant.BROWSER_QUEUE_POLL_INTERVAL", 0.01)
    monkeypatch.setattr("app.services.assistant.BROWSER_OPEN_TIMEOUT", 0.04 if terminal == "open_timeout" else 110)
    state, calls = {}, []

    async def worker(path, payload):
        calls.append((path, payload))
        scope = manager.browsers.get(sid)
        if path == "open":
            assert payload["wait_for_lane"] is True and payload["same_origin_only"] is True
            state["scope"] = scope
            return view(scope, "queued")
        if path.endswith("/close"):
            assert manager.detail(OWNER, sid)["status"] == "running"
            return {"closed": True}
        assert path == f"{scope.session_id}/open-status" and payload == {"token": scope.token}
        turn = manager._active_turns[sid]
        if len(calls) <= 4:
            assert turn["timeout"].when() is None
            assert manager.detail(OWNER, sid)["browser"] is None
            await asyncio.sleep(0.06)  # Network alone exceeds the whole active budget.
            return view(scope, "queued" if len(calls) < 4 else "opening")
        assert turn["timeout"].when() == turn["deadline"]
        if terminal != "success":
            await asyncio.Event().wait()
        return view(scope, "ready")

    async def conversation(owner, session_id, config):
        turn = manager._active_turns[sid]
        turn["deadline"] = asyncio.get_running_loop().time() + 0.12
        turn["timeout"].reschedule(turn["deadline"])
        state["initial"] = turn["deadline"]
        state["turn"] = turn
        state["result"] = await manager.browser_tool(owner, sid, tools.target, "browser_open", None)

    monkeypatch.setattr(manager, "_worker", AsyncMock(side_effect=worker))
    monkeypatch.setattr(manager, "_conversation", conversation)
    monkeypatch.setattr(manager, "_auto_title", AsyncMock())

    async def scenario():
        await asyncio.wait_for(manager._run_turn(OWNER, sid, {
            **tools.turn, "_assistant_settings": {"turn_timeout_seconds": 10},
        }), 2)

    asyncio.run(scenario())
    assert state["turn"]["deadline"] - state["initial"] >= 0.17
    assert sum(path == "open" for path, _ in calls) == 1
    assert sum(path.endswith("/close") for path, _ in calls) == 1
    assert sid not in manager.browsers and not manager._active_turns
    assert manager.detail(OWNER, sid)["status"] == "idle"
    if terminal == "success":
        assert state["result"]["ok"]
    elif terminal == "active_timeout":
        assert "[provider_timeout]" in manager.detail(OWNER, sid)["messages"][-1]["content"]
    else:
        assert not state["result"]["ok"]
        assert state["result"]["reason"] == "browser_open_timeout"


@pytest.mark.parametrize("change", ["policy", "origin", "request", "run_unfinished", "job_unfinished"])
def test_queue_revalidates_fresh_authority_after_each_poll(ctx, monkeypatch, change):
    grant(ctx, live_access=True)
    job_id, run_id = failed_job(ctx)
    tools, _ = tools_for(ctx)
    manager, sid = tools.manager, tools.session_id
    calls = []

    async def worker(path, payload):
        calls.append(path)
        if path.endswith("/close"):
            return {"closed": True}
        scope = manager.browsers[sid]
        if path == "open":
            return view(scope, "queued")
        with manager.session_factory() as db:
            if change == "policy":
                service = TakeoverSettingsService(db)
                policy = service.view()
                service.update(TakeoverSettingsUpdate(expected_revision=policy.revision, confirm=True,
                    values=policy.values.model_copy(update={"enabled": False})), OWNER)
            elif change == "origin":
                db.get(Site, ctx[2]).base_url = "https://changed.example/"
            elif change == "run_unfinished":
                db.get(Run, run_id).finished_at = None
            elif change == "job_unfinished":
                db.get(SiteJob, job_id).finished_at = None
            else:
                manager._active_turns[sid]["_takeover_request_id"] = str(uuid4())
            db.commit()
        return view(scope, "ready")

    monkeypatch.setattr(manager, "_worker", AsyncMock(side_effect=worker))

    async def scenario():
        tools.turn["deadline"] = asyncio.get_running_loop().time() + 180
        assert await tools.dispatch("browser_open", {}) == {"ok": False, "code": "takeover_tool_denied"}

    asyncio.run(scenario())
    assert len(calls) == 3 and calls[-1].endswith("/close")
    assert sid not in manager.browsers
    assert not session_data(manager, sid).get("_execution_records")


@pytest.mark.parametrize("ending", ["queue_timeout", "worker_timeout", "cancel", "shutdown"])
def test_queue_terminal_paths_close_only_owned_scope(ctx, monkeypatch, ending):
    grant(ctx, live_access=True)
    failed_job(ctx)
    tools, _ = tools_for(ctx)
    manager, sid = tools.manager, tools.session_id
    monkeypatch.setattr("app.services.assistant.BROWSER_QUEUE_TIMEOUT", 0.04)
    entered = asyncio.Event()
    calls = []

    async def worker(path, payload):
        calls.append(path)
        if path.endswith("/close"):
            return {"closed": True}
        scope = manager.browsers[sid]
        if path == "open":
            return view(scope, "queued")
        entered.set()
        if ending == "worker_timeout":
            raise AssistantError("browser_unavailable", reason="browser_queue_timeout")
        await asyncio.Event().wait()

    monkeypatch.setattr(manager, "_worker", AsyncMock(side_effect=worker))

    async def scenario():
        tools.turn["deadline"] = asyncio.get_running_loop().time() + 180
        other_sid = str(uuid4())
        other = scope_for(AssistantTools(manager, OWNER, other_sid, tools.target))
        task = asyncio.create_task(tools.dispatch("browser_open", {}))
        manager.tasks[sid] = task
        await entered.wait()
        if ending == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif ending == "shutdown":
            # Shutdown intentionally closes all scopes; individual cancellation does not.
            manager.browsers.pop(other_sid)
            await manager.shutdown()
        else:
            result = await task
            assert result["code"] == "browser_unavailable" and result["reason"] == "browser_queue_timeout"
        if ending != "shutdown":
            assert manager.browsers[other_sid] is other
        assert sid not in manager.browsers

    asyncio.run(scenario())
    assert sum(path == "open" for path in calls) == 1
    assert sum(path.endswith("/close") for path in calls) == 1


def test_manual_browser_defers_daily_action_without_model_or_preemption(ctx, monkeypatch):
    grant(ctx, live_access=True)
    takeover, manager, _ = ctx
    install_model(monkeypatch, manager)
    action = action_for(ctx)
    sid = manager.create_takeover(action.id)
    manual = scope_for(AssistantTools(manager, OWNER, sid, session_data(manager, sid)["target"]))
    worker, conversation = AsyncMock(), AsyncMock()
    monkeypatch.setattr(manager, "_worker", worker)
    monkeypatch.setattr(manager, "_conversation", conversation)
    asyncio.run(takeover.process_one())
    with manager.session_factory() as db:
        saved = db.get(AssistantTakeoverAction, action.id)
        assert saved.status == "queued" and saved.attempts == 0 and saved.request_id is None
    assert manager.browsers[sid] is manual
    worker.assert_not_awaited()
    conversation.assert_not_awaited()


def test_old_automation_cleanup_never_closes_human_replacement(ctx, monkeypatch):
    manager = ctx[1]
    sid = str(uuid4())
    manual = scope_for(AssistantTools(manager, OWNER, sid, None))
    worker = AsyncMock()
    monkeypatch.setattr(manager, "_worker", worker)
    asyncio.run(manager._close_browser(sid, automation_request_id=str(uuid4())))
    assert manager.browsers[sid] is manual
    worker.assert_not_awaited()


@pytest.mark.parametrize("before_entry", [False, True])
def test_close_completes_before_idle_even_when_cancelled_before_entry(ctx, monkeypatch, before_entry):
    grant(ctx, live_access=True)
    takeover, manager, _ = ctx
    install_model(monkeypatch, manager)
    action = action_for(ctx)
    sid = manager.create_takeover(action.id)
    request_id = str(uuid4())
    with manager.session_factory() as db:
        saved = db.get(AssistantTakeoverAction, action.id)
        saved.status, saved.request_id = "dispatched", request_id
        db.commit()
    entered, release = asyncio.Event(), asyncio.Event()

    async def conversation(owner, session_id, config):
        scope_for(AssistantTools(manager, owner, sid, session_data(manager, sid)["target"]))

    async def worker(path, payload):
        assert path.endswith("/close")
        assert manager.detail(OWNER, sid)["status"] == "running"
        entered.set()
        await release.wait()
        return {"closed": True}

    monkeypatch.setattr(manager, "_worker", AsyncMock(side_effect=worker))
    monkeypatch.setattr(manager, "_conversation", conversation)

    async def scenario():
        await manager.send_takeover(OWNER, sid, SendMessage(request_id=request_id,
            content="Inspect source", consent_to_send=True), action_id=action.id)
        task = manager.tasks[sid]
        if before_entry:
            # A synthetic already-owned scope exercises the callback backstop.
            scope_for(AssistantTools(manager, OWNER, sid, session_data(manager, sid)["target"]))
            task.cancel()
        await entered.wait()
        assert manager.detail(OWNER, sid)["status"] == "running"
        assert sid in manager.tasks
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        async with asyncio.timeout(1):
            while sid in manager.tasks:
                await asyncio.sleep(0)
        assert manager.detail(OWNER, sid)["status"] == "idle"
        assert sid not in manager.browsers and sid not in manager._active_turns
        data = session_data(manager, sid)
        assert "_takeover" not in data and data["target"]["browser_consent"]
        assert data["_turn_outcomes"][request_id]["ok"] is (not before_entry)

    asyncio.run(scenario())
    manager._worker.assert_awaited_once()


@pytest.mark.parametrize("ending", ["cancel", "cancel_twice", "shutdown", "worker_error"])
def test_cancellation_during_terminal_close_finalizes_before_human_followup(ctx, monkeypatch, ending):
    grant(ctx, live_access=True)
    takeover, manager, _ = ctx
    entered, release = asyncio.Event(), asyncio.Event()
    opened, closed, auto_scope = [], [], {}

    async def inspect(owner, sid, config):
        tools = AssistantTools(manager, owner, sid, session_data(manager, sid)["target"],
                               config=config, turn=manager._active_turns[sid])
        assert (await tools.dispatch("browser_open", {}))["ok"]

    install_model(monkeypatch, manager, inspect)

    async def worker(path, payload):
        if path == "open":
            opened.append(payload["session_id"])
            assert payload.get("wait_for_lane", False) is (len(opened) == 1)
            if len(opened) == 1:
                auto_scope.update(token=payload["token"], sid=next(
                    sid for sid, scope in manager.browsers.items()
                    if scope.session_id == payload["session_id"]))
            return {"session_id": payload["session_id"], "lane_key": payload["lane_key"],
                    "status": "ready",
                    "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=900)).isoformat()}
        assert path == f"{opened[0]}/close"
        assert payload == {"token": auto_scope["token"]}
        closed.append(path)
        assert manager.detail(OWNER, auto_scope["sid"])["status"] == "running"
        entered.set()
        await release.wait()
        assert manager.detail(OWNER, auto_scope["sid"])["status"] == "running"
        if ending == "worker_error":
            raise httpx.ReadError("private close error")
        return {"closed": True}

    monkeypatch.setattr(manager, "_worker", AsyncMock(side_effect=worker))

    async def scenario():
        action = action_for(ctx)
        coordinator = asyncio.create_task(takeover.process_one())
        async with asyncio.timeout(2):
            await entered.wait()
        sid = auto_scope["sid"]
        task = manager.tasks[sid]
        shutdown = None
        if ending == "shutdown":
            shutdown = asyncio.create_task(manager.shutdown())
        elif ending != "worker_error":
            task.cancel()
        if ending != "worker_error":
            async with asyncio.timeout(1):
                while not task.cancelling():
                    await asyncio.sleep(0)
            await asyncio.sleep(0)
            if ending == "cancel_twice":
                task.cancel()
                await asyncio.sleep(0)
        assert not task.done()
        assert manager.detail(OWNER, sid)["status"] == "running"
        assert sid in manager._active_turns
        release.set()
        async with asyncio.timeout(2):
            await coordinator
            if shutdown:
                await shutdown
        assert sid not in manager.tasks and sid not in manager._active_turns
        assert sid not in manager.browsers
        data = session_data(manager, sid)
        assert "_takeover" not in data
        assert all(data["target"][key + "_consent"] for key in (
            "browser", "execution", "credential", "screenshot", "workflow"))
        with manager.session_factory() as db:
            saved = db.get(AssistantTakeoverAction, action.id)
            outcome = data["_turn_outcomes"][saved.request_id]
            if ending == "worker_error":
                assert outcome["ok"] and outcome["code"] is None
                assert saved.status == "completed_analysis"
            else:
                assert outcome["ok"] is False
                assert outcome["code"] == outcome["errorcode"] == "cancelled"
                assert saved.status == "failed"
                messages = manager.detail(OWNER, sid)["messages"]
                assert messages[-1]["role"] == "system" and "[cancelled]" in messages[-1]["content"]
        assert manager.detail(OWNER, sid)["status"] == "idle"

        followup = manager
        if ending == "shutdown":
            # A stopped manager resumes service only through a fresh runtime.
            followup = AssistantManager(manager.session_factory, manager.settings, manager.crypto)
            followup.recover()
            install_model(monkeypatch, followup)
            monkeypatch.setattr(followup, "_worker", manager._worker)

            async def human_inspect(owner, session_id, config):
                tools = AssistantTools(followup, owner, session_id,
                    session_data(followup, session_id)["target"], config=config,
                    turn=followup._active_turns[session_id])
                assert (await tools.dispatch("browser_open", {}))["ok"]

            install_model(monkeypatch, followup, human_inspect)
        await followup.send(OWNER, sid, SendMessage(request_id=uuid4(),
            content="Inspect the site again", consent_to_send=True))
        await followup.tasks[sid]
        await asyncio.sleep(0)
        assert followup.detail(OWNER, sid)["status"] == "idle"
        assert followup.browsers[sid].session_id == opened[1]
        assert followup.browsers[sid].automation_request_id is None
        assert closed == [f"{opened[0]}/close"]

    asyncio.run(scenario())


@pytest.mark.parametrize("code,expected", [
    ("browser_queue_timeout", "browser_queue_timeout"),
    ("private-queue-error?token=SECRET", "browser_open_timeout"),
])
def test_open_status_http_error_is_static(ctx, monkeypatch, code, expected):
    manager = ctx[1]
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(504, json={
        "detail": {"code": code, "private": "SECRET"}}))
    monkeypatch.setattr("app.services.assistant.httpx.AsyncClient",
                        lambda **kwargs: real_client(transport=transport, **kwargs))
    with pytest.raises(AssistantError) as caught:
        asyncio.run(manager._worker(f"{uuid4()}/open-status", {"token": "host-private-token"}))
    assert caught.value.code == "browser_unavailable" and caught.value.reason == expected
    assert "SECRET" not in str(caught.value)


def test_real_model_queue_uses_one_open_and_no_extra_model_round(ctx, monkeypatch, provider):
    grant(ctx, live_access=True)
    takeover, manager, _ = ctx
    install_model(monkeypatch, manager)
    # Use the real conversation loop rather than the model-free lifecycle helper.
    monkeypatch.setattr(manager, "_conversation", AssistantManager._conversation.__get__(manager))
    monkeypatch.setattr("app.services.assistant.BROWSER_QUEUE_POLL_INTERVAL", 0.001)
    paths = []

    async def worker(path, payload):
        paths.append(path)
        if path.endswith("/close"):
            return {"closed": True}
        scope = next(iter(manager.browsers.values()))
        return view(scope, "queued" if len(paths) < 4 else "ready")

    monkeypatch.setattr(manager, "_worker", AsyncMock(side_effect=worker))
    action = action_for(ctx)
    provider[0].extend([tool("browser_open", {}, "open"), text("Browser inspected; no fix saved.")])
    asyncio.run(takeover.process_one())
    assert paths[0] == "open" and paths[-1].endswith("/close")
    assert sum(path.endswith("/open-status") for path in paths) == 3
    assert len(provider[1]) == 2
    with manager.session_factory() as db:
        saved = db.get(AssistantTakeoverAction, action.id)
        assert saved.status == "completed_analysis" and saved.attempts == 1
        assert saved.session_id not in manager.browsers
    serialized = json.dumps([json.loads(request.content) for request in provider[1]])
    assert "do not retry" in serialized and "open-status" not in serialized
