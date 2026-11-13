from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models.assistant import AssistantMessage, AssistantSession
from app.models.assistant_takeover import AssistantTakeoverAction, DailyConversation
from app.models.execution import SiteJob
from app.models.workflow import Site
from app.schemas.assistant import SendMessage
from app.schemas.assistant_takeover import TakeoverSettingsUpdate, TakeoverValues
from app.services.assistant_takeover import (
    remove_takeover, takeover_source_job, takeover_tool_allowed,
    takeover_tool_definitions,
)
from app.services.assistant_takeover_settings import TakeoverSettingsService
from app.services.assistant_tools import AssistantTools
from app.services.assistant_workflows import AssistantWorkflows
from tests.test_assistant_takeover import (  # noqa: F401
    OWNER, ctx, failed_job, grant, queued, tools_for,
)
from tests.test_assistant_workflows import submit
from tests.test_assistant_api import assistant_client, provider, text, tool  # noqa: F401


RUNTIME = {"api_key": "test-private-provider-key", "model": "fake",
           "base_url": "https://provider.example/v1", "timeout": 10}


def action_for(ctx, *, finished_at=None):
    takeover, manager, _ = ctx
    job_id, _ = failed_job(ctx)
    if finished_at is not None:
        with manager.session_factory() as db:
            db.get(SiteJob, job_id).finished_at = finished_at
            db.commit()
    takeover.reconcile()
    with manager.session_factory() as db:
        return db.scalar(select(AssistantTakeoverAction).where(
            AssistantTakeoverAction.source_job_id == job_id))


def session_data(manager, sid):
    with manager.session_factory() as db:
        return manager._decrypt(db.get(AssistantSession, sid))


def install_model(monkeypatch, manager, handler=None):
    monkeypatch.setattr(manager, "_runtime", lambda: dict(RUNTIME))

    async def conversation(owner, sid, config):
        if handler:
            await handler(owner, sid, config)
        with manager.session_factory() as db:
            manager._message(db, sid, "assistant", "Failure diagnosed; no execution performed.")
            db.commit()

    monkeypatch.setattr(manager, "_conversation", conversation)


def test_daily_reuses_history_and_audits_source_completion(ctx):
    grant(ctx)
    _, manager, site_id = ctx
    anchor = datetime(2026, 11, 10, 15, 59, tzinfo=timezone.utc)
    with manager.session_factory() as db:
        db.get(Site, site_id).timezone = "Asia/Tokyo"
        db.commit()
    first = action_for(ctx, finished_at=anchor)
    sid = manager.create_takeover(first.id)
    with manager.session_factory() as db:
        manager._message(db, sid, "assistant", "Prior full diagnosis and rationale")
        db.commit()
    second = action_for(ctx, finished_at=anchor + timedelta(minutes=1))
    assert manager.create_takeover(second.id) == sid
    with manager.session_factory() as db:
        daily = db.scalar(select(DailyConversation))
        assert str(daily.local_date) == "2026-11-11"
        assert daily.timezone == "Asia/Tokyo"
        assert daily.anchor_action_id == first.id and daily.session_id == sid
        assert daily.anchor_finished_at.replace(tzinfo=timezone.utc) == anchor
        assert db.scalar(select(func.count()).select_from(DailyConversation)) == 1
    assert manager.detail(OWNER, sid)["messages"][0]["content"] == "Prior full diagnosis and rationale"
    data = session_data(manager, sid)
    assert data["_takeover_daily"]["anchor_action_id"] == first.id
    assert "_takeover" not in data and not data["target"]["workflow_consent"]


@pytest.mark.parametrize("site_zone,scheduler_zone,expected_zone,expected_day", [
    ("America/Los_Angeles", "Asia/Shanghai", "America/Los_Angeles", "2026-11-09"),
    ("invalid/site", "Asia/Tokyo", "Asia/Tokyo", "2026-11-10"),
    ("invalid/site", "invalid/scheduler", "UTC", "2026-11-10"),
])
def test_source_day_timezone_and_valid_fallback(ctx, monkeypatch, site_zone, scheduler_zone,
                                             expected_zone, expected_day):
    grant(ctx)
    _, manager, site_id = ctx
    monkeypatch.setattr(manager.settings, "scheduler_timezone", scheduler_zone)
    with manager.session_factory() as db:
        db.get(Site, site_id).timezone = site_zone
        db.commit()
    action = action_for(ctx, finished_at=datetime(2026, 11, 10, 1, tzinfo=timezone.utc))
    sid = manager.create_takeover(action.id)
    meta = session_data(manager, sid)["_takeover_daily"]
    assert meta["timezone"] == expected_zone and meta["date"] == expected_day


def test_daily_midnight_owner_and_history_identity_are_distinct(ctx):
    grant(ctx)
    _, manager, site_id = ctx
    with manager.session_factory() as db:
        db.get(Site, site_id).timezone = "UTC"
        db.commit()
    anchor = datetime(2026, 11, 10, 23, 59, tzinfo=timezone.utc)
    first = action_for(ctx, finished_at=anchor)
    sid = manager.create_takeover(first.id)
    tomorrow = action_for(ctx, finished_at=anchor + timedelta(minutes=1))
    assert manager.create_takeover(tomorrow.id) != sid
    other_owner = action_for(ctx, finished_at=anchor)
    with manager.session_factory() as db:
        db.get(AssistantTakeoverAction, other_owner.id).owner = "other-owner"
        db.commit()
    assert manager.create_takeover(other_owner.id) != sid
    with manager.session_factory() as db:
        db.get(Site, site_id).history_key = str(uuid4())
        db.commit()
    replacement = action_for(ctx, finished_at=anchor)
    assert manager.create_takeover(replacement.id) != sid


def test_deleted_daily_chat_replaces_mapping_without_replaying_old_action(ctx, monkeypatch):
    grant(ctx)
    takeover, manager, _ = ctx
    install_model(monkeypatch, manager)

    async def scenario():
        first = action_for(ctx)
        await takeover.process_one()
        with manager.session_factory() as db:
            old = db.get(AssistantTakeoverAction, first.id)
            sid, request_id = old.session_id, old.request_id
            assert old.status == "completed_analysis"
        await manager.delete(OWNER, sid)
        second = action_for(ctx)
        await takeover.process_one()
        with manager.session_factory() as db:
            old, new = db.get(AssistantTakeoverAction, first.id), db.get(AssistantTakeoverAction, second.id)
            assert old.session_id == sid and old.request_id == request_id and old.attempts == 1
            assert new.session_id != sid and new.status == "completed_analysis"
            assert db.scalar(select(DailyConversation)).session_id == new.session_id
            assert db.scalar(select(func.count()).select_from(DailyConversation)) == 1
            assert db.scalar(select(func.count()).select_from(AssistantTakeoverAction)) == 2

    asyncio.run(scenario())


def test_human_followup_is_ordinary_without_global_or_old_delegation_grants(ctx, monkeypatch):
    grant(ctx, mode="autofix")
    takeover, manager, _ = ctx
    seen = []

    async def inspect(owner, sid, config):
        data = session_data(manager, sid)
        tools = AssistantTools(manager, owner, sid, data["target"], config=config,
                               turn=manager._active_turns[sid])
        seen.append(config)
        if config.get("_takeover_action_id"):
            assert data["target"]["workflow_consent"]
            assert takeover_tool_allowed(tools, "browser_open", {})["ok"] is False
        else:
            assert "_takeover" not in data
            assert not any(data["target"].get(key + "_consent") for key in (
                "workflow", "execution", "browser", "credential", "screenshot"))
            assert takeover_tool_allowed(tools, "request_user_interaction", {}) is True
            definitions = [{"function": {"name": "request_user_interaction"}}]
            assert takeover_tool_definitions(manager, owner, sid, definitions) == definitions
            output = await AssistantWorkflows(manager, owner, sid, tools.target).dispatch(
                "import_workflow_draft", {"proposal_message_id": 123})
            assert output == {"ok": False, "code": "workflow_consent_required"}

    install_model(monkeypatch, manager, inspect)
    # Deliberately turn all global defaults on: daily human followups must ask.
    from app.services.assistant_settings import AssistantSettingsService
    original = AssistantSettingsService.load

    def defaults(service):
        return original(service).model_copy(update={
            "auto_authorize_workflow": True, "auto_authorize_execution": True,
            "auto_authorize_screenshot": True})

    monkeypatch.setattr(AssistantSettingsService, "load", defaults)

    async def scenario():
        action = action_for(ctx)
        await takeover.process_one()
        with manager.session_factory() as db:
            action = db.get(AssistantTakeoverAction, action.id)
            sid, run_id, job_id = action.session_id, action.source_run_id, action.source_job_id
            service = TakeoverSettingsService(db)
            service.update(TakeoverSettingsUpdate(expected_revision=service.view().revision,
                confirm=True, values=TakeoverValues(enabled=False)), OWNER)
        data = session_data(manager, sid)
        assert "_takeover" not in data and data["_takeover_daily"]
        with manager.session_factory() as db:
            assert takeover_source_job(manager, db, OWNER, sid, run_id) == job_id
        assert AssistantWorkflows(manager, OWNER, sid, data["target"]).feedback(run_id)["ok"]
        await manager.send(OWNER, sid, SendMessage(request_id=uuid4(), content="Explain that fix",
                                                  consent_to_send=True))
        await manager.tasks[sid]
        assert len(seen) == 2 and "_takeover_action_id" not in seen[-1]
        assert len(manager.list_sessions(OWNER)["items"]) == 1

    asyncio.run(scenario())


def test_busy_human_turn_defers_without_overwriting_binding(ctx, monkeypatch):
    grant(ctx)
    takeover, manager, _ = ctx
    started, release = asyncio.Event(), asyncio.Event()

    async def wait_for_human(owner, sid, config):
        if not config.get("_takeover_action_id"):
            started.set()
            await release.wait()

    install_model(monkeypatch, manager, wait_for_human)

    async def scenario():
        first = action_for(ctx)
        await takeover.process_one()
        with manager.session_factory() as db:
            sid = db.get(AssistantTakeoverAction, first.id).session_id
        second = action_for(ctx)
        await manager.send(OWNER, sid, SendMessage(request_id=uuid4(), content="Discuss failure",
                                                  consent_to_send=True))
        task = manager.tasks[sid]
        await started.wait()
        binding = dict(manager._active_turns[sid])
        revision = session_data(manager, sid)["_target_revision"]
        await takeover.process_one()
        assert manager._active_turns[sid] == binding
        assert session_data(manager, sid)["_target_revision"] == revision
        with manager.session_factory() as db:
            action = db.get(AssistantTakeoverAction, second.id)
            assert action.status == "queued" and action.attempts == 0 and action.request_id is None
        release.set()
        await task
        await takeover.process_one()
        with manager.session_factory() as db:
            action = db.get(AssistantTakeoverAction, second.id)
            assert action.status == "completed_analysis" and action.session_id == sid

    asyncio.run(scenario())


def test_reactivation_rejects_old_proposal_and_stale_turn_tool_binding(ctx, monkeypatch):
    grant(ctx, mode="autofix")
    takeover, manager, _ = ctx
    proposals, old_tools = [], []

    async def inspect(owner, sid, config):
        tools = AssistantTools(manager, owner, sid, session_data(manager, sid)["target"],
                               config=config, turn=manager._active_turns[sid])
        if not proposals:
            old_tools.append(tools)
            proposals.append(await asyncio.to_thread(submit, tools))
        else:
            assert takeover_tool_allowed(old_tools[0], "get_site_config", {"site_id": ctx[2]}) == {
                "ok": False, "code": "takeover_tool_denied"}
            workflows = AssistantWorkflows(manager, owner, sid, tools.target)
            result = await workflows.dispatch("import_workflow_draft", {
                "proposal_message_id": proposals[0]})
            assert result == {"ok": False, "code": "proposal_target_changed"}
            fresh = await asyncio.to_thread(submit, tools)
            result = await workflows.dispatch("import_workflow_draft", {"proposal_message_id": fresh})
            assert result["ok"]

    install_model(monkeypatch, manager, inspect)

    async def scenario():
        first = action_for(ctx)
        await takeover.process_one()
        second = action_for(ctx)
        await takeover.process_one()
        with manager.session_factory() as db:
            a, b = db.get(AssistantTakeoverAction, first.id), db.get(AssistantTakeoverAction, second.id)
            assert a.session_id == b.session_id
            assert a.status == "completed_analysis" and b.status == "updated"
            assert not a.import_receipt_json and b.import_receipt_json

    asyncio.run(scenario())


@pytest.mark.parametrize("error", [None, "provider_timeout"])
def test_request_completion_ignores_context_image_and_later_human_turn(ctx, monkeypatch, error):
    grant(ctx)
    takeover, manager, _ = ctx
    install_model(monkeypatch, manager)

    async def fake_send(owner, sid, payload, *, action_id):
        with manager.session_factory() as db:
            row = db.get(AssistantSession, sid)
            manager._message(db, sid, "user", payload.content, request_id=str(payload.request_id))
            db.flush()
            user_id = db.scalar(select(AssistantMessage.id).where(
                AssistantMessage.request_id == str(payload.request_id)))
            manager._message(db, sid, "system", "image context", context_image=True)
            manager._message(db, sid, "assistant", "Exact source diagnosis")
            manager._message(db, sid, "user", "Later human question", request_id=str(uuid4()))
            manager._message(db, sid, "system", "Later human provider error")
            data = manager._decrypt(row)
            data["_turn_outcomes"] = {str(payload.request_id): {
                "ok": error is None, "code": error, "user_message_id": user_id}}
            manager._encrypt(row, data)
            row.status = "running"  # A later human turn need not be idle to collect the old result.
            db.commit()

    monkeypatch.setattr(manager, "send_takeover", fake_send)
    action_for(ctx)
    asyncio.run(takeover.process_one())
    with manager.session_factory() as db:
        action = db.scalar(select(AssistantTakeoverAction))
        assert action.status == ("failed" if error else "completed_analysis")


def test_typed_outcomes_are_bounded_and_persisted_before_idle(ctx, monkeypatch):
    grant(ctx)
    takeover, manager, _ = ctx
    install_model(monkeypatch, manager)

    async def scenario():
        action = action_for(ctx)
        await takeover.process_one()
        with manager.session_factory() as db:
            action = db.get(AssistantTakeoverAction, action.id)
            sid, first_request = action.session_id, action.request_id
            data = manager._decrypt(db.get(AssistantSession, sid))
            outcome = data["_turn_outcomes"][first_request]
            assert outcome["ok"] is True and outcome["action_id"] == action.id
            assert outcome["user_message_id"]
            data["_turn_outcomes"] = {str(i): {"ok": True} for i in range(32)}
            manager._encrypt(db.get(AssistantSession, sid), data)
            db.commit()
        request_id = str(uuid4())
        await manager.send(OWNER, sid, SendMessage(request_id=request_id, content="Explain",
                                                  consent_to_send=True))
        await manager.tasks[sid]
        outcomes = session_data(manager, sid)["_turn_outcomes"]
        assert len(outcomes) == 32 and "0" not in outcomes
        assert outcomes[request_id]["action_id"] is None

    asyncio.run(scenario())


def test_turn_error_clears_autonomous_permissions(ctx, monkeypatch):
    grant(ctx, mode="autofix")
    takeover, manager, _ = ctx
    install_model(monkeypatch, manager)
    monkeypatch.setattr(manager, "_conversation", AsyncMock(side_effect=TimeoutError))
    action_for(ctx)
    asyncio.run(takeover.process_one())
    with manager.session_factory() as db:
        action = db.scalar(select(AssistantTakeoverAction))
        data = manager._decrypt(db.get(AssistantSession, action.session_id))
        assert action.status == "failed"
        assert data["_turn_outcomes"][action.request_id]["code"] == "provider_timeout"
        assert "_takeover" not in data and not data["target"]["workflow_consent"]


def test_active_pointer_without_server_turn_is_not_authority(ctx):
    grant(ctx, mode="autofix")
    failed_job(ctx)
    tools, _ = tools_for(ctx)
    tools.manager._active_turns.clear()
    assert takeover_tool_allowed(tools, "get_site_config", {"site_id": ctx[2]}) == {
        "ok": False, "code": "takeover_tool_denied"}
    data = session_data(tools.manager, tools.session_id)
    remove_takeover(data)
    assert data["_takeover_daily"] and "_takeover" not in data


def test_human_wins_admission_race_without_autonomous_binding_overwrite(ctx, monkeypatch):
    grant(ctx)
    takeover, manager, _ = ctx
    install_model(monkeypatch, manager)
    first = action_for(ctx)
    sid = manager.create_takeover(first.id)
    entered, release = asyncio.Event(), asyncio.Event()

    async def human_turn(owner, session_id, config):
        entered.set()
        await release.wait()

    monkeypatch.setattr(manager, "_conversation", human_turn)
    original = manager.send_takeover

    async def racing_send(owner, session_id, payload, *, action_id):
        await manager.send(owner, session_id, SendMessage(request_id=uuid4(),
                           content="A human discussion wins admission", consent_to_send=True))
        await entered.wait()
        binding = dict(manager._active_turns[session_id])
        try:
            return await original(owner, session_id, payload, action_id=action_id)
        finally:
            assert manager._active_turns[session_id] == binding
            assert "_takeover_action_id" not in binding
            assert "_takeover" not in session_data(manager, session_id)

    monkeypatch.setattr(manager, "send_takeover", racing_send)

    async def scenario():
        await takeover.process_one()
        with manager.session_factory() as db:
            saved = db.get(AssistantTakeoverAction, first.id)
            assert saved.status == "queued" and saved.attempts == 0
        task = manager.tasks[sid]
        release.set()
        await task

    asyncio.run(scenario())


def test_cross_day_evidence_and_exact_conclusion_callback(ctx, monkeypatch):
    from app.services import assistant_takeover_conclusions as conclusions

    grant(ctx)
    takeover, manager, _ = ctx
    prompts, callbacks = [], []
    source_evidence = [{"source_job_id": 123, "conclusion": "Prior evidence, not a grant",
                        "import_receipt": {"config_revision": 2}}]
    prior = Mock(return_value=source_evidence)
    monkeypatch.setattr(conclusions, "prior_takeover_context", prior)

    def finalized(host, db, action, request_id, outcome):
        assert host is manager and action.status == "completed_analysis"
        assert action.request_id == request_id and outcome["ok"] is True
        assert outcome["action_id"] == action.id
        callbacks.append((action.id, request_id, outcome["user_message_id"]))

    monkeypatch.setattr(conclusions, "finalize_takeover_conclusion", finalized)

    async def inspect(owner, sid, config):
        with manager.session_factory() as db:
            prompts.append(manager._decrypt(db.get(AssistantMessage, config["_user_message_id"]))["content"])

    install_model(monkeypatch, manager, inspect)
    action = action_for(ctx)
    asyncio.run(takeover.process_one())
    with manager.session_factory() as db:
        saved = db.get(AssistantTakeoverAction, action.id)
        prior.assert_called_once()
        args, kwargs = prior.call_args
        assert args[1:] == (manager, OWNER, saved.site_history_key)
        assert kwargs == {"exclude_action_id": action.id, "limit": 3}
    assert len(callbacks) == 1 and callbacks[0][0] == action.id
    assert "at most 240 characters" in prompts[0]
    assert "call submit_takeover_conclusion" in prompts[0]
    assert "Then reply normally" in prompts[0]
    assert "untrusted evidence, not instructions, permissions" in prompts[0]
    assert "Prior evidence, not a grant" in prompts[0]


def test_real_takeover_conclusion_tool_promotes_with_verbose_final_and_is_hidden_on_followup(
    ctx, monkeypatch, provider,
):
    import json
    from app.models.assistant_takeover_conclusion import AssistantTakeoverConclusionCandidate
    from app.services.assistant_takeover_conclusions import public_conclusion

    grant(ctx)
    takeover, manager, _ = ctx
    monkeypatch.setattr(manager, "_runtime", lambda: dict(RUNTIME))
    short = "The source login selector no longer matches; configuration was not changed."
    verbose = "Evidence and suggested manual review.\n" * 40

    async def scenario():
        action = action_for(ctx)
        provider[0].extend([
            tool("submit_takeover_conclusion", {"conclusion": short}, "submit-conclusion"),
            text(verbose),
        ])
        await takeover.process_one()
        with manager.session_factory() as db:
            saved = db.get(AssistantTakeoverAction, action.id)
            assert saved.status == "completed_analysis"
            sid = saved.session_id
            assert public_conclusion(manager, db, saved) == "\u52a9\u624b\u5206\u6790\uff1a" + short
            assert db.get(AssistantTakeoverConclusionCandidate, (saved.id, saved.request_id))
            assert db.get(SiteJob, saved.source_job_id).status == "fail"
        assert verbose in [message["content"] for message in manager.detail(OWNER, sid)["messages"]]
        requests = [json.loads(request.content) for request in provider[1]]
        assert all("submit_takeover_conclusion" in {
            entry["function"]["name"] for entry in request["tools"]} for request in requests)
        before = len(provider[1])
        provider[0].append(text("The previous conclusion is evidence, not permission to execute."))
        await manager.send(OWNER, sid, SendMessage(request_id=uuid4(), content="Explain the diagnosis",
                                                  consent_to_send=True))
        await manager.tasks[sid]
        followups = [json.loads(request.content) for request in provider[1][before:]]
        assert followups
        assert all(len(request["tools"]) == 38 and "submit_takeover_conclusion" not in {
            entry["function"]["name"] for entry in request["tools"]} for request in followups)

    asyncio.run(scenario())


def test_cancel_before_task_entry_removes_grants_and_records_outcome(ctx, monkeypatch):
    grant(ctx, mode="autofix")
    _, manager, _ = ctx
    install_model(monkeypatch, manager)
    action = action_for(ctx)
    sid = manager.create_takeover(action.id)
    request_id = str(uuid4())
    with manager.session_factory() as db:
        saved = db.get(AssistantTakeoverAction, action.id)
        saved.request_id, saved.status = request_id, "dispatched"
        db.commit()

    async def scenario():
        await manager.send_takeover(OWNER, sid, SendMessage(request_id=request_id,
            content="Analyze the failed job", consent_to_send=True), action_id=action.id)
        task = manager.tasks[sid]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    data = session_data(manager, sid)
    assert "_takeover" not in data and not data["target"]["workflow_consent"]
    assert data["_turn_outcomes"][request_id]["code"] == "cancelled"
    assert manager.detail(OWNER, sid)["status"] == "idle"


@pytest.mark.parametrize("consent", [False, None, "true", 1])
@pytest.mark.parametrize("name", [
    "browser_open", "browser_observe", "browser_inspect", "browser_check_selector",
    "browser_navigate", "browser_refresh", "browser_screenshot",
])
def test_browser_host_requires_explicit_consent(ctx, monkeypatch, consent, name):
    from app.services.assistant import BrowserScope
    import time

    grant(ctx)
    _, manager, _ = ctx
    action = action_for(ctx)
    sid = manager.create_takeover(action.id)
    target = session_data(manager, sid)["target"]
    if consent is None:
        target.pop("browser_consent")
    else:
        target["browser_consent"] = consent
    target["screenshot_consent"] = True
    scope = BrowserScope(OWNER, str(uuid4()), "private-token", "interaction", "",
                         time.monotonic() + 900)
    manager.browsers[sid] = scope
    worker = AsyncMock()
    monkeypatch.setattr(manager, "_worker", worker)
    result = asyncio.run(manager.browser_tool(OWNER, sid, target, name, None))
    assert result["code"] == "browser_consent_required"
    assert "request_user_interaction" in result["notice"]
    assert manager.browsers[sid] is scope
    worker.assert_not_awaited()


@pytest.mark.parametrize("operation", ["refresh", "screenshot"])
@pytest.mark.parametrize("consent", [False, None])
def test_browser_recovery_requires_explicit_consent(ctx, monkeypatch, operation, consent):
    from app.services.assistant import AssistantError

    grant(ctx)
    _, manager, _ = ctx
    action = action_for(ctx)
    sid = manager.create_takeover(action.id)
    with manager.session_factory() as db:
        row = db.get(AssistantSession, sid)
        data = manager._decrypt(row)
        if consent is None:
            data["target"].pop("browser_consent")
        data["target"]["screenshot_consent"] = True
        manager._encrypt(row, data)
        db.commit()
    worker = AsyncMock()
    monkeypatch.setattr(manager, "_worker", worker)
    with pytest.raises(AssistantError) as exc:
        asyncio.run(manager.browser_recovery(OWNER, sid, operation))
    assert exc.value.code == "browser_consent_required"
    worker.assert_not_awaited()
    assert manager.detail(OWNER, sid)["status"] == "idle"


def test_real_daily_followup_browser_waits_for_explicit_target_confirmation(ctx, monkeypatch, provider):
    import json
    from app.schemas.assistant import InteractionResponse
    from app.services.assistant_interactions import respond_interaction

    grant(ctx)
    takeover, manager, _ = ctx
    monkeypatch.setattr(manager, "_runtime", lambda: dict(RUNTIME))

    async def opened(path, payload):
        assert path == "open"
        assert session_data(manager, sid)["target"]["browser_consent"] is True
        return {"session_id": payload["session_id"], "lane_key": "interaction",
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}

    worker = AsyncMock(side_effect=opened)
    monkeypatch.setattr(manager, "_worker", worker)
    sid = None

    async def scenario():
        nonlocal sid
        action = action_for(ctx)
        provider[0].append(text("The failed source was analyzed; browsing is not enabled."))
        await takeover.process_one()
        with manager.session_factory() as db:
            sid = db.get(AssistantTakeoverAction, action.id).session_id
        provider[0].extend([tool("browser_open", {}, "denied-open"),
                            text("Browsing is not enabled; target confirmation is required.")])
        await manager.send(OWNER, sid, SendMessage(request_id=uuid4(),
            content="Can you use the browser now?", consent_to_send=True))
        await manager.tasks[sid]
        detail = manager.detail(OWNER, sid)
        result = next(json.loads(m["content"]) for m in detail["messages"]
                      if m.get("tool_name") == "browser_open")
        assert result["code"] == "browser_consent_required"
        assert detail["target"]["browser_consent"] is False
        assert detail["pending_interaction"] is None and not detail["browser"]
        worker.assert_not_awaited()
        provider[0].extend([
            tool("request_user_interaction", {"request": {"purpose": "target", "target": {
                "name": "Example", "base_url": "https://example.com/", "credential_mode": "none",
            }}}, "confirm-target"),
            tool("browser_open", {}, "approved-open"), text("Browser opened with your confirmation."),
        ])
        await manager.send(OWNER, sid, SendMessage(request_id=uuid4(),
            content="Ask me to confirm the target before browsing", consent_to_send=True))
        task = manager.tasks[sid]
        for _ in range(300):
            pending = manager.detail(OWNER, sid)["pending_interaction"]
            if pending or task.done():
                break
            await asyncio.sleep(0.01)
        assert pending and pending["purpose"] == "target"
        worker.assert_not_awaited()
        assert session_data(manager, sid)["target"]["browser_consent"] is False
        await respond_interaction(manager, OWNER, sid, pending["id"],
                                  InteractionResponse(decision="approve"))
        await task
        worker.assert_awaited_once()
        assert manager.detail(OWNER, sid)["target"]["browser_consent"] is True
        assert manager.detail(OWNER, sid)["browser"]["status"] == "ready"

    asyncio.run(scenario())
