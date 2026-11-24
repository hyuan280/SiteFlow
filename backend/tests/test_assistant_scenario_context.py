from __future__ import annotations

import asyncio
from copy import deepcopy
import json

import pytest

from app.models.assistant import AssistantSession
from app.services import assistant_tokens, assistant_takeover
from app.services.assistant_prompt import SYSTEM_PROMPT, build_system_prompt, tool_definitions
from app.services.assistant_tokens import estimate_context_tokens
from app.services.config_ai_generation import GenerationError
from tests.test_assistant_api import CONFIG, assistant_client, provider, text, tool  # noqa: F401
from tests.test_assistant_takeover import (  # noqa: F401
    OWNER, ctx, failed_job, grant, tools_for,
)


def setup_turn(ctx, *, live=False):
    grant(ctx, live_access=live)
    failed_job(ctx)
    tools, _ = tools_for(ctx)
    manager, sid = tools.manager, tools.session_id
    with manager.session_factory() as db:
        manager._message(db, sid, "user", "Analyze the failed source job")
        db.commit()
    return manager, sid


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("compact", [False, True])
def test_preflight_and_provider_use_identical_scenario_context(
    ctx, provider, monkeypatch, live, compact,
):
    manager, sid = setup_turn(ctx, live=live)
    selected = assistant_takeover.takeover_tool_definitions(manager, OWNER, sid, tool_definitions())
    config = dict(CONFIG, _assistant_settings={"auto_compact": compact})
    prepared = asyncio.run(manager._prepare_context(OWNER, sid, config, definitions=selected))
    messages = prepared[1]
    # A tight cap admits the selected prompt/tools but not the ordinary catalog.
    cap = estimate_context_tokens(messages, selected)
    assert cap < estimate_context_tokens(messages, tool_definitions())
    ordinary = deepcopy(messages)
    ordinary[0]["content"] = SYSTEM_PROMPT
    assert cap < estimate_context_tokens(ordinary, tool_definitions())
    config["_assistant_settings"]["context_max_tokens"] = cap
    estimates = []
    original = assistant_tokens.estimate_context_tokens

    def estimate(messages, tools=None):
        if tools is not None:
            estimates.append((deepcopy(messages), tools))
        return original(messages, tools)

    selections = []
    select = assistant_takeover.takeover_tool_definitions

    def definitions(*args):
        result = select(*args)
        selections.append(result)
        return result

    monkeypatch.setattr(assistant_tokens, "estimate_context_tokens", estimate)
    monkeypatch.setattr(assistant_takeover, "takeover_tool_definitions", definitions)
    provider[0].append(text("Analysis complete"))
    asyncio.run(manager._conversation(OWNER, sid, config))
    body = json.loads(provider[1][0].content)
    assert selections == [selected]
    assert body["tools"] == selected
    assert all(tools is selections[0] for _, tools in estimates)
    assert estimates[0][0][0] == body["messages"][0]
    assert body["messages"][0] == messages[0]
    assert "Current host-enforced turn budgets" in messages[0]["content"]
    assert ("Current host takeover scope" in messages[0]["content"]) is live
    assert body["messages"][1]["content"].startswith("Host-confirmed target")


def test_dynamic_appendix_is_in_first_cap_check(ctx, monkeypatch):
    manager, sid = setup_turn(ctx, live=True)
    selected = assistant_takeover.takeover_tool_definitions(manager, OWNER, sid, tool_definitions())
    captured = []
    original = assistant_tokens.estimate_context_tokens

    def estimate(messages, tools=None):
        captured.append(deepcopy(messages))
        return original(messages, tools)

    monkeypatch.setattr(assistant_tokens, "estimate_context_tokens", estimate)
    config = dict(CONFIG, _assistant_settings={"context_max_tokens": 1})
    with pytest.raises(GenerationError) as raised:
        asyncio.run(manager._prepare_context(OWNER, sid, config, definitions=selected))
    assert raised.value.code == "context_limit"
    assert len(captured) == 1
    assert "Current host-enforced turn budgets" in captured[0][0]["content"]
    assert "Current host takeover scope" in captured[0][0]["content"]


def test_provider_wait_does_not_cache_takeover_dispatch_authority(ctx, provider, monkeypatch):
    manager, sid = setup_turn(ctx, live=True)
    workers = []

    async def worker(*args):
        workers.append(args)
        raise AssertionError("Revoked takeover reached worker")

    async def revoke(request):
        assert "browser_open" in {
            item["function"]["name"] for item in json.loads(request.content)["tools"]}
        grant(ctx, live_access=False)
        return tool("browser_open")

    monkeypatch.setattr(manager, "_worker", worker)
    provider[0].extend([revoke, text("Authority changed; stopped")])
    asyncio.run(manager._conversation(OWNER, sid, dict(CONFIG)))
    body = json.loads(provider[1][1].content)
    results = [item for item in body["messages"] if item["role"] == "tool"]
    assert len(results) == 1
    assert json.loads(results[0]["content"])["code"] == "takeover_tool_denied"
    assert not workers


def test_human_followup_restores_ordinary_prompt_and_tools(ctx, provider):
    manager, sid = setup_turn(ctx, live=True)
    provider[0].append(text("Takeover finished"))
    asyncio.run(manager._conversation(OWNER, sid, dict(CONFIG)))
    autonomous = json.loads(provider[1][0].content)["messages"][0]["content"]
    with manager.session_factory() as db:
        row = db.get(AssistantSession, sid)
        data = manager._decrypt(row)
        assistant_takeover.remove_takeover(data)
        assert "_takeover_daily" in data
        assert data["target"]["_takeover_site_grant"]
        manager._encrypt(row, data)
        manager._message(db, sid, "user", "Help me with this site now")
        db.commit()
    manager._active_turns.pop(sid)
    provider[0].append(text("Human follow-up complete"))
    asyncio.run(manager._conversation(OWNER, sid, dict(CONFIG)))
    body = json.loads(provider[1][1].content)
    prompt = body["messages"][0]["content"]
    assert prompt.startswith(build_system_prompt())
    assert "ordinary human follow-up" in prompt
    assert "Current host takeover scope" not in prompt
    assert autonomous not in json.dumps(body["messages"])
    assert body["tools"] == assistant_takeover.takeover_tool_definitions(
        manager, OWNER, sid, tool_definitions())
    names = {item["function"]["name"] for item in body["tools"]}
    assert {"request_user_interaction", "run_site", "retry_step", "update_site_config"} <= names
    assert manager._history(OWNER, sid)[1][0]["content"].startswith(SYSTEM_PROMPT)
    with manager.session_factory() as db:
        assert "_context_summary" not in manager._decrypt(db.get(AssistantSession, sid))


@pytest.mark.parametrize("persisted", [False, True])
def test_revoked_takeover_never_falls_back_to_ordinary(ctx, provider, persisted):
    manager, sid = setup_turn(ctx, live=True)
    # Changing the grant invalidates current authority. Even missing per-turn
    # persisted metadata cannot turn an active autonomous request into a human.
    grant(ctx, live_access=False)
    if not persisted:
        with manager.session_factory() as db:
            row = db.get(AssistantSession, sid)
            data = manager._decrypt(row)
            data.pop("_takeover")
            manager._encrypt(row, data)
            db.commit()
    else:
        manager._active_turns.pop(sid)
    provider[0].append(text("Authority revoked"))
    asyncio.run(manager._conversation(OWNER, sid, dict(CONFIG)))
    body = json.loads(provider[1][0].content)
    assert body["tools"] == []
    prompt = body["messages"][0]["content"]
    assert prompt.startswith(build_system_prompt(takeover=True, tool_names=set()))
    assert "ordinary human follow-up" not in prompt
    assert "Current host takeover scope" not in prompt
