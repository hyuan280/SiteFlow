from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import time
from uuid import uuid4

import pytest

from app.features.catalog import FEATURES
from app.services.assistant import AssistantManager
from app.services.assistant_categories import Denied, builtin_category
from app.services.assistant_execution import AssistantExecution
from app.services.assistant_retry import GRANTS, PENDING, RECORDS
from app.services.assistant_tools import AssistantTools
from tests.test_assistant_execution import (  # noqa: F401
    OWNER, arguments, bind, execution, run, save_target,
)
from tests.test_assistant_retry import load, mutate, succeed, unknown


def host_turn(helper, limit=3, request=None):
    turn = {"id": str(uuid4()), "_request_id": request or str(uuid4()),
            "deadline": time.monotonic() + 180,
            "_assistant_settings": {"max_auth_uses_per_turn": limit}}
    helper.manager._active_turns[helper.session_id] = turn
    return turn


def test_registry_only_category_not_prefix_or_model_metadata(execution, monkeypatch):
    assert builtin_category("challenge.2fa") == "challenge"
    assert builtin_category("post.buy_card") == "post"
    for key in ("plugin.auth", "auth.unknown", None):
        with pytest.raises(Denied, match="execution_feature_denied"):
            builtin_category(key)
    monkeypatch.setitem(FEATURES, "plugin.auth", replace(FEATURES["auth.form_login"], key="plugin.auth"))
    with pytest.raises(Denied):
        builtin_category("plugin.auth")
    bind(execution)
    assert run(execution, arguments("auth.form_login", category="flow"))["code"] == "invalid_step"
    assert not load(execution).get(RECORDS)


def test_shared_default_cap_before_credentials_and_duplicate_does_not_charge(execution, monkeypatch):
    bind(execution)
    calls = []
    monkeypatch.setattr(execution, "_credentials", lambda: calls.append(True) or {
        "username": "account", "password": "private-password"})
    first = arguments("auth.form_login")
    assert run(execution, first)["ok"]
    assert run(execution, first)["code"] == "operation_already_used"
    assert run(execution, arguments("auth.password_verify"))["ok"]
    assert run(execution, arguments("auth.form_login"))["ok"]
    assert run(execution, arguments("auth.password_verify"))["code"] == "auth_budget_exhausted"
    assert len(calls) == 3 and execution.manager._worker.await_count == 3
    data = load(execution)
    assert len(data[RECORDS]) == 3
    assert {r["category"] for r in data[RECORDS].values()} == {"auth"}
    assert {r["turn_id"] for r in data[RECORDS].values()} == {data["_auth_budget_turn"]}


@pytest.mark.parametrize("limit", [1, 3, 20])
def test_config_snapshot_host_identity_and_real_new_turn(execution, limit):
    bind(execution)
    turn = host_turn(execution, limit)
    for _ in range(limit):
        helper = AssistantExecution(execution.manager, OWNER, execution.session_id, execution.target,
                                    turn=turn, auth_limit=20)
        assert run(helper, arguments("auth.password_verify"))["ok"]
    turn["_assistant_settings"]["max_auth_uses_per_turn"] = 20
    assert run(execution, arguments("auth.form_login"))["code"] == "auth_budget_exhausted"
    host_turn(execution, 20, request=turn["_request_id"])
    assert run(execution, arguments("auth.form_login"))["code"] == "auth_budget_exhausted"
    host_turn(execution, 1)
    assert run(execution, arguments("auth.form_login"))["ok"]
    assert run(execution, arguments("auth.form_login"))["code"] == "auth_budget_exhausted"


@pytest.mark.parametrize("limit", [True, "3", 0, 21, 3.0])
def test_invalid_host_limits_are_not_coerced(execution, limit):
    bind(execution)
    host_turn(execution, limit)
    assert run(execution, arguments("auth.form_login"))["code"] == "execution_authorization_changed"
    assert not load(execution).get(RECORDS)


def test_invalid_normalization_free_but_credentials_unavailable_counts(execution):
    assert run(execution, arguments("auth.form_login", {"username": "invalid"}))["code"] == "invalid_step"
    assert not load(execution).get(RECORDS)
    for _ in range(3):
        assert run(execution, arguments("auth.form_login"))["code"] == "credentials_unavailable"
    assert run(execution, arguments("auth.form_login"))["code"] == "auth_budget_exhausted"
    execution.manager._worker.assert_not_awaited()


def test_budget_survives_new_services_manager_target_and_browser(execution):
    bind(execution)
    for _ in range(3):
        assert run(execution, arguments("auth.form_login"))["ok"]
    scope = deepcopy(execution.manager.browsers[execution.session_id])
    scope.session_id = str(uuid4())
    fresh = AssistantManager(execution.manager.session_factory, execution.manager.settings, execution.manager.crypto)
    fresh.browsers[execution.session_id] = scope
    helper = AssistantExecution(fresh, OWNER, execution.session_id, execution.target,
                                turn={"id": str(uuid4())}, auth_limit=20)
    save_target(helper, {**helper.target, "name": "New display name"})
    assert run(helper, arguments("auth.form_login"))["code"] == "auth_budget_exhausted"


def test_latest_user_request_fallback_is_durable(execution):
    bind(execution)
    for _ in range(3):
        assert run(execution, arguments("auth.form_login"))["ok"]
    request = str(uuid4())
    with execution.manager.session_factory() as db:
        execution.manager._message(db, execution.session_id, "user", "Continue", request_id=request)
        db.commit()
    for _ in range(3):
        assert run(execution, arguments("auth.form_login"))["ok"]
    assert run(execution, arguments("auth.form_login"))["code"] == "auth_budget_exhausted"
    assert load(execution)["_execution_turn_id"] == request


def test_host_turn_fallback_cannot_be_reset_by_recreating_manager(execution):
    bind(execution)
    turn = host_turn(execution, 1)
    assert run(execution, arguments("auth.form_login"))["ok"]
    execution.manager._active_turns.clear()
    assert run(execution, arguments("auth.form_login"))["code"] == "auth_budget_exhausted"
    assert load(execution)["_execution_turn_id"] == turn["_request_id"]


def test_non_auth_categories_do_not_spend_auth_budget(execution, monkeypatch):
    bind(execution)
    host_turn(execution, 1)
    for _ in range(5):
        assert run(execution, arguments("action.wait"))["ok"]
    assert run(execution, arguments("auth.form_login"))["ok"]
    assert run(execution, arguments("auth.password_verify"))["code"] == "auth_budget_exhausted"
    assert run(execution, arguments("sign.button"))["ok"]
    monkeypatch.setitem(FEATURES, "action.wait", replace(FEATURES["action.wait"], category="auth"))
    assert run(execution, arguments("action.wait"))["code"] == "auth_budget_exhausted"


def test_turn_revoked_during_credentials_never_dispatches(execution, monkeypatch):
    bind(execution)
    old = host_turn(execution)

    def credentials():
        host_turn(execution)
        return {"username": "account", "password": "private-password"}

    monkeypatch.setattr(execution, "_credentials", credentials)
    assert run(execution, arguments("auth.form_login"))["code"] == "execution_authorization_changed"
    execution.manager._worker.assert_not_awaited()
    record = next(iter(load(execution)[RECORDS].values()))
    assert record["turn_id"] == old["_request_id"] and record["status"] == "failed"


def test_stale_service_turn_cannot_spend_new_host_budget(execution):
    bind(execution)
    old = host_turn(execution)
    host_turn(execution)
    helper = AssistantExecution(execution.manager, OWNER, execution.session_id, execution.target, turn=old)
    assert run(helper, arguments("auth.form_login"))["code"] == "execution_authorization_changed"
    assert not load(execution).get(RECORDS)


def test_atomic_reservation_across_managers(execution):
    step = execution._normalize(arguments("auth.form_login")["step"])

    def reserve(_):
        manager = AssistantManager(execution.manager.session_factory, execution.manager.settings,
                                   execution.manager.crypto)
        manager.browsers[execution.session_id] = execution.manager.browsers[execution.session_id]
        helper = AssistantExecution(manager, OWNER, execution.session_id, execution.target)
        helper._normalize(step)
        try:
            helper._reserve(str(uuid4()), step)
            return "reserved"
        except Denied as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(8)))
    assert results.count("reserved") == 3
    assert results.count("auth_budget_exhausted") == 5
    records = load(execution)[RECORDS]
    assert len(records) == 3 and len({r["turn_id"] for r in records.values()}) == 1


def test_auth_retry_is_automatic_exact_and_shares_budget(execution):
    bind(execution)
    turn = host_turn(execution)
    original = unknown(execution, "auth.form_login")["operation_id"]
    mutate(execution, lambda data: None, status="running")
    succeed(execution)
    tools = AssistantTools(execution.manager, OWNER, execution.session_id, execution.target, turn=turn)
    tools.call_context = {"tool_call_id": "retry", "tool_message_id": 123}
    result = asyncio.run(tools.dispatch("retry_step", {"operation_id": original}))
    assert result["ok"] and result["retry_of"] == original and result["operation_id"] != original
    data = load(execution)
    assert not data.get(PENDING)
    grant = data[GRANTS][original]
    assert grant["automatic"] and grant["status"] == "used"
    assert grant["turn_id"] == turn["_request_id"]
    assert data[RECORDS][result["operation_id"]]["step"] == data[RECORDS][original]["step"]
    assert run(execution, arguments("auth.password_verify"))["ok"]
    assert run(execution, arguments("auth.form_login"))["code"] == "auth_budget_exhausted"


def test_direct_tools_snapshot_propagates_to_retry_and_cannot_reset_budget(execution):
    bind(execution)
    config = {"_assistant_settings": {"max_auth_uses_per_turn": 1}}
    original = unknown(execution, "sign.button")["operation_id"]
    mutate(execution, lambda data: None, status="running")
    # Start a genuine request after the standalone unknown operation.
    with execution.manager.session_factory() as db:
        execution.manager._message(db, execution.session_id, "user", "Continue", request_id=str(uuid4()))
        db.commit()
    succeed(execution)
    tools = AssistantTools(execution.manager, OWNER, execution.session_id, execution.target, config=config)
    assert asyncio.run(tools.dispatch("execute_step", arguments("auth.form_login")))["ok"]
    tools = AssistantTools(execution.manager, OWNER, execution.session_id, execution.target,
                           config={"_assistant_settings": {"max_auth_uses_per_turn": 20}})
    assert asyncio.run(tools.dispatch("execute_step", arguments("auth.password_verify")))["code"] == "auth_budget_exhausted"
    assert asyncio.run(tools.dispatch("retry_step", {"operation_id": original}))["ok"]


def test_auth_retry_at_cap_opens_no_dialog_and_leaves_no_authority(execution):
    bind(execution)
    turn = host_turn(execution, 1)
    original = unknown(execution, "auth.form_login")["operation_id"]
    mutate(execution, lambda data: None, status="running")
    tools = AssistantTools(execution.manager, OWNER, execution.session_id, execution.target, turn=turn)
    assert asyncio.run(tools.dispatch("retry_step", {"operation_id": original}))["code"] == "auth_budget_exhausted"
    data = load(execution)
    assert not data.get(PENDING) and data[GRANTS][original]["status"] == "expired"
    assert len(data[RECORDS]) == 1 and "resolution" not in data[RECORDS][original]
    execution.manager._worker.assert_awaited_once()


def test_automatic_grant_cannot_be_stolen_or_rebound(execution, monkeypatch):
    original = unknown(execution)["operation_id"]
    mutate(execution, lambda data: None, status="running")
    turn = host_turn(execution)
    from app.services import assistant_retry
    execute_retry = assistant_retry.run_authorized_retry

    async def inspect(manager, owner, sid, operation, **kwargs):
        grant = load(execution)[GRANTS][original]
        assert (await execute_retry(manager, owner, sid, operation))["code"] == "retry_authorization_required"
        forged = AssistantExecution(manager, owner, sid, execution.target,
            retry_authorization={"operation_id": original, "id": grant["id"]},
            turn=turn, invocation={"wrong": "invocation"})
        assert (await forged.execute(arguments("sign.button")))["code"] == "retry_authorization_required"
        host_turn(execution)
        return await execute_retry(manager, owner, sid, operation, **kwargs)

    monkeypatch.setattr(assistant_retry, "run_authorized_retry", inspect)
    tools = AssistantTools(execution.manager, OWNER, execution.session_id, execution.target, turn=turn)
    assert asyncio.run(tools.dispatch("retry_step", {"operation_id": original}))["code"] == "execution_authorization_changed"
    assert execution.manager._worker.await_count == 1


def test_post_and_plugin_never_auto_execute(execution):
    host_turn(execution)
    for feature in ("post.buy_card", "post.resign", "post.resign_loop", "post.resign_dialog", "plugin.foo"):
        from app.services.assistant_feature_policy import set_assistant_allowed
        if feature.startswith("post."):
            with execution.manager.session_factory() as db:
                set_assistant_allowed(db, feature, False)
                db.commit()
        assert run(execution, arguments(feature))["code"] == "execution_feature_denied"
    execution.manager._worker.assert_not_awaited()
