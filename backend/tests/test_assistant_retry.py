from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
import time
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app.models.assistant import AssistantSession
from app.schemas.assistant import AuthorizeRetry
from app.services.assistant import AssistantError, AssistantManager, BrowserScope
from app.services.assistant_execution import AssistantExecution
from app.services.assistant_retry import (
    GRANTS, PENDING, RECORDS, authorize_retry,
    model_retry_status, retry_status, run_authorized_retry,
    valid_grant,
)
from app.services.assistant_tools import AssistantTools
from app.services.config_ai_generation import _json
from tests.helpers import login_client, make_settings
from tests.test_assistant_execution import (
    OWNER, TOKEN, arguments, bind, execution, response, run,  # noqa: F401
)


def load(helper):
    with helper.manager.session_factory() as db:
        return helper.manager._decrypt(db.get(AssistantSession, helper.session_id))


def mutate(helper, function, *, status=None):
    with helper.manager.session_factory() as db:
        row = db.get(AssistantSession, helper.session_id)
        data = helper.manager._decrypt(row)
        function(data)
        helper.manager._encrypt(row, data)
        if status is not None:
            row.status = status
        db.commit()


def unknown(helper, feature="sign.button", config=None):
    scope = helper.manager.browsers[helper.session_id]

    async def worker(path, payload):
        return response(scope, payload["operation_id"], status="unknown", code="outcome_uncertain")

    helper.manager._worker.side_effect = worker
    args = arguments(feature, config)
    result = run(helper, args)
    assert result["status"] == "unknown"
    helper.manager._idle(helper.session_id)
    return args


def approve(helper, operation, request=None):
    return asyncio.run(authorize_retry(helper.manager, helper.owner, helper.session_id, AuthorizeRetry(
        operation_id=operation, confirmed=True, request_id=request or uuid4(),
    )))


def retry(helper, operation):
    mutate(helper, lambda data: None, status="running")
    return asyncio.run(run_authorized_retry(helper.manager, helper.owner, helper.session_id, operation))


def succeed(helper):
    async def worker(path, payload):
        return response(helper.manager.browsers[helper.session_id], payload["operation_id"])

    helper.manager._worker.side_effect = worker


def test_grant_is_idle_local_private_idempotent_and_consumed_once(execution):
    args = unknown(execution)
    operation, request = args["operation_id"], uuid4()
    before = execution.manager._worker.await_count
    detail = approve(execution, operation, request)
    first = load(execution)
    grant = first[GRANTS][operation]
    assert detail["status"] == "idle" and grant["status"] == "approved"
    assert execution.manager._worker.await_count == before
    assert approve(execution, operation, request) == detail
    assert load(execution)[GRANTS][operation] == grant
    assert approve(execution, operation)["retry_authorizations"] == detail["retry_authorizations"]
    assert all(value not in json.dumps(detail) for value in [grant["id"], TOKEN, "step_fingerprint"])
    assert set(detail["retry_authorizations"][0]) == {
        "operation_id", "retry_operation_id", "expires_at", "status", "target_revision",
    }
    captured = []

    async def worker(path, payload):
        captured.append(deepcopy(payload))
        saved = load(execution)
        assert saved[GRANTS][operation]["status"] == "used"
        assert saved[RECORDS][payload["operation_id"]]["state"] == "dispatched"
        return response(execution.manager.browsers[execution.session_id], payload["operation_id"])

    execution.manager._worker.side_effect = worker
    result = retry(execution, operation)
    assert result["ok"] and result["retry_of"] == operation
    assert result["operation_id"] != operation
    assert captured[0]["step"] == grant["step"]
    assert captured[0]["retry_of"] == operation
    assert captured[0]["retry_authorization"] == grant["id"]
    assert grant["id"] not in json.dumps(result)
    assert retry(execution, operation)["code"] == "retry_authorization_required"
    saved = load(execution)
    assert saved[RECORDS][operation]["status"] == "unknown"
    assert saved[RECORDS][operation]["resolution"]["retry_operation_id"] == result["operation_id"]
    assert saved["_context_safety"][0]["operation_id"] == operation
    assert retry_status(saved)[0]["status"] == "used"
    assert model_retry_status(saved)[0]["resolution"]["status"] == "success"
    assert run(execution, arguments("sign.button"))["ok"]


@pytest.mark.parametrize("change", ["target", "revision", "browser", "expired", "not_ready"])
def test_grants_invalidated_before_dispatch(execution, change):
    operation = unknown(execution)["operation_id"]
    approve(execution, operation)
    if change == "target":
        mutate(execution, lambda data: data["target"].update(name="Changed"))
    elif change == "revision":
        mutate(execution, lambda data: data.update(_target_revision=1))
    elif change == "browser":
        execution.manager.browsers[execution.session_id].session_id = str(uuid4())
    elif change == "expired":
        mutate(execution, lambda data: data[GRANTS][operation].update(expires_at=time.time() - 1))
    else:
        execution.manager.browsers[execution.session_id].ready = False
    before = execution.manager._worker.await_count
    assert not retry(execution, operation)["ok"]
    assert execution.manager._worker.await_count == before
    assert load(execution)[GRANTS][operation]["status"] == "approved"


def test_new_current_target_revision_can_be_explicitly_reauthorized(execution):
    operation = unknown(execution)["operation_id"]
    approve(execution, operation)
    old = load(execution)[GRANTS][operation]["id"]
    mutate(execution, lambda data: data.update(_target_revision=7))
    execution.manager.browsers[execution.session_id].session_id = str(uuid4())
    detail = approve(execution, operation)
    assert detail["retry_authorizations"][0]["target_revision"] == 7
    assert load(execution)[GRANTS][operation]["id"] != old
    succeed(execution)
    assert retry(execution, operation)["ok"]


@pytest.mark.parametrize("change", ["origin", "credentials", "proxy"])
def test_reauthorization_never_changes_original_origin_or_credentials(execution, change):
    operation = unknown(execution)["operation_id"]
    changes = {"origin": {"base_url": "https://other.example/"},
               "credentials": {"credential_mode": "bitwarden"}, "proxy": {"proxy": True}}
    mutate(execution, lambda data: data["target"].update(changes[change]))
    with pytest.raises(AssistantError, match="could not complete") as caught:
        approve(execution, operation)
    assert caught.value.code == "retry_binding_changed"
    assert not load(execution).get(GRANTS)
    assert execution.manager.detail(OWNER, execution.session_id)["status"] == "idle"


def test_request_replay_does_not_refresh_expiry_or_issue_new_grant(execution):
    operation, request = unknown(execution)["operation_id"], uuid4()
    approve(execution, operation, request)
    mutate(execution, lambda data: data[GRANTS][operation].update(expires_at=1))
    detail = approve(execution, operation, request)
    assert detail["retry_authorizations"][0]["status"] == "expired"
    assert load(execution)[GRANTS][operation]["expires_at"] == 1
    with pytest.raises(AssistantError) as caught:
        approve(execution, str(uuid4()), request)
    assert caught.value.code == "retry_request_conflict"


@pytest.mark.parametrize("source", ["record", "facts", "legacy_message"])
def test_durable_uncertainty_does_not_block_nonpost_in_new_browser(execution, source):
    args = unknown(execution)
    operation = args["operation_id"]
    if source == "facts":
        mutate(execution, lambda data: data.pop(RECORDS))
    elif source == "legacy_message":
        mutate(execution, lambda data: (data.pop(RECORDS), data.pop("_context_safety")))
        with execution.manager.session_factory() as db:
            execution.manager._message(db, execution.session_id, "tool", _json({
                "operation_id": operation, "status": "unknown", "code": "outcome_uncertain",
            }), tool_name="execute_step", arguments=args)
            db.commit()
    fresh = AssistantManager(execution.manager.session_factory, execution.manager.settings, execution.manager.crypto)
    fresh.browsers[execution.session_id] = BrowserScope(
        OWNER, str(uuid4()), TOKEN, "interaction", "", time.monotonic() + 900,
    )
    fresh._worker = execution.manager._worker
    helper = AssistantExecution(fresh, OWNER, execution.session_id, execution.target)
    mutate(execution, lambda data: None, status="running")
    succeed(helper)
    assert run(helper, arguments("sign.button"))["ok"]
    assert run(helper, arguments("auth.form_login"))["code"] == "credentials_unavailable"
    if source != "legacy_message":
        assert load(helper)["_context_safety"][0]["operation_id"] == operation
    assert run(helper, arguments("action.wait"))["ok"]


def test_model_cannot_supply_grant_or_edit_step(execution):
    operation = unknown(execution)["operation_id"]
    approve(execution, operation)
    assert run(execution, {**arguments("sign.button"), "retry_authorization": str(uuid4())})["code"] == "invalid_arguments"
    assert retry(execution, {"operation_id": operation, "confirmed": True})["code"] == "invalid_arguments"
    data = load(execution)
    helper = AssistantExecution(execution.manager, OWNER, execution.session_id, execution.target,
                               retry_authorization={"operation_id": operation, "id": data[GRANTS][operation]["id"]})
    assert run(helper, arguments("sign.button", {"button_selector": ["#different"]}))["code"] == "retry_authorization_required"
    assert load(execution)[GRANTS][operation]["status"] == "approved"


def test_lost_response_persists_unknown_before_failure_and_has_no_default_replay(execution):
    args = arguments("sign.button")
    execution.manager._worker.side_effect = TimeoutError("private-exception")
    assert run(execution, args) == {"ok": False, "code": "execution_unavailable"}
    record = load(execution)[RECORDS][args["operation_id"]]
    assert record["state"] == "terminal" and record["status"] == "unknown"
    assert load(execution)["_context_safety"][0]["operation_id"] == args["operation_id"]
    execution.manager.browsers[execution.session_id] = BrowserScope(
        OWNER, str(uuid4()), TOKEN, "interaction", "", time.monotonic() + 900,
    )
    succeed(execution)
    assert run(execution, arguments("sign.button"))["ok"]
    assert "resolution" not in load(execution)[RECORDS][args["operation_id"]]
    execution.manager._idle(execution.session_id)
    approve(execution, args["operation_id"])
    succeed(execution)
    assert retry(execution, args["operation_id"])["ok"]
    assert "private-exception" not in json.dumps(load(execution))


def test_retry_grant_consumed_before_credentials_and_resolution_failure_never_replays(execution, monkeypatch):
    bind(execution)
    operation = unknown(execution, "auth.form_login")["operation_id"]
    approve(execution, operation)
    calls = []

    def credentials(helper):
        calls.append(True)
        assert load(helper)[GRANTS][operation]["status"] == "used"
        raise ValueError("secret")

    monkeypatch.setattr(AssistantExecution, "_credentials", credentials)
    before = execution.manager._worker.await_count
    assert retry(execution, operation)["code"] == "credentials_unavailable"
    assert retry(execution, operation)["code"] == "retry_authorization_required"
    assert calls == [True] and execution.manager._worker.await_count == before
    assert run(execution, arguments("sign.button"))["status"] == "unknown"
    execution.manager._idle(execution.session_id)
    assert execution.manager.detail(OWNER, execution.session_id)["unresolved_operations"][0]["can_authorize"]
    assert approve(execution, operation)["retry_authorizations"][0]["status"] == "approved"


def test_unknown_retry_keeps_gate_and_can_only_retry_new_operation(execution):
    operation = unknown(execution)["operation_id"]
    approve(execution, operation)
    result = retry(execution, operation)
    assert result["status"] == "unknown" and result["retry_of"] == operation
    assert run(execution, arguments("sign.button"))["status"] == "unknown"
    execution.manager._idle(execution.session_id)
    with pytest.raises(AssistantError) as caught:
        approve(execution, operation)
    assert caught.value.code == "retry_already_used"
    approve(execution, result["operation_id"])
    succeed(execution)
    assert retry(execution, result["operation_id"])["ok"]
    assert run(execution, arguments("sign.button"))["ok"]


@pytest.mark.parametrize("terminal", ["success", "failed", "error"])
def test_only_successful_terminal_retry_outcomes_resolve_gate(execution, terminal):
    operation = unknown(execution)["operation_id"]
    approve(execution, operation)

    async def worker(path, payload):
        return response(execution.manager.browsers[execution.session_id], payload["operation_id"],
                        status=terminal, code={"success": "step_succeeded", "failed": "step_failed",
                                               "error": "execution_error"}[terminal])

    execution.manager._worker.side_effect = worker
    assert retry(execution, operation)["status"] == terminal
    succeed(execution)
    result = run(execution, arguments("sign.button"))
    assert result["ok"]
    assert load(execution)[RECORDS][operation]["status"] == "unknown"


@pytest.mark.parametrize("terminal", ["failed", "error"])
def test_failed_retry_requires_fresh_explicit_approval_and_preserves_audit(execution, terminal):
    operation, first_request = unknown(execution)["operation_id"], uuid4()
    approve(execution, operation, first_request)

    async def worker(path, payload):
        return response(execution.manager.browsers[execution.session_id], payload["operation_id"],
                        status=terminal, code="step_failed" if terminal == "failed" else "execution_error")

    execution.manager._worker.side_effect = worker
    failed = retry(execution, operation)
    assert failed["status"] == terminal
    old = load(execution)[GRANTS][operation]
    assert run(execution, arguments("sign.button"))["status"] == terminal
    assert retry(execution, operation)["code"] == "retry_authorization_required"
    execution.manager._idle(execution.session_id)
    assert execution.manager.detail(OWNER, execution.session_id)["unresolved_operations"][0]["can_authorize"]
    assert approve(execution, operation, first_request)["retry_authorizations"][0]["status"] == "used"
    assert load(execution)[GRANTS][operation] == old

    second_request = uuid4()
    assert approve(execution, operation, second_request)["retry_authorizations"][0]["status"] == "approved"
    saved = load(execution)
    renewed = saved[GRANTS][operation]
    assert renewed["id"] != old["id"]
    assert saved[RECORDS][failed["operation_id"]]["retry_authorization"] == old
    assert saved["retry_requests"][str(first_request)]["grant_id"] == old["id"]
    assert saved["retry_requests"][str(second_request)]["grant_id"] == renewed["id"]
    approve(execution, operation, first_request)
    assert load(execution)[GRANTS][operation] == renewed

    succeed(execution)
    succeeded = retry(execution, operation)
    assert succeeded["ok"]
    assert run(execution, arguments("sign.button"))["ok"]
    saved = load(execution)
    assert saved[RECORDS][operation]["status"] == "unknown"
    assert saved[RECORDS][operation]["resolution"]["retry_operation_id"] == succeeded["operation_id"]
    execution.manager._idle(execution.session_id)
    for request in (first_request, second_request):
        assert approve(execution, operation, request)["retry_authorizations"][0]["status"] == "used"
    assert load(execution)[GRANTS][operation]["id"] == renewed["id"]


def test_near_expiry_admission_accepts_success_after_expiry_without_closing(execution, monkeypatch):
    operation = unknown(execution)["operation_id"]
    approve(execution, operation)
    grant = load(execution)[GRANTS][operation]
    clock = [grant["expires_at"] - 0.01]
    monkeypatch.setattr("app.services.assistant_retry.time.time", lambda: clock[0])

    async def worker(path, payload):
        assert load(execution)[GRANTS][operation]["status"] == "used"
        clock[0] = grant["expires_at"] + 60
        return response(execution.manager.browsers[execution.session_id], payload["operation_id"])

    execution.manager._worker.side_effect = worker
    result = retry(execution, operation)
    assert result["ok"] and result["status"] == "success"
    execution.manager._close_browser.assert_not_awaited()
    saved = load(execution)
    assert saved[RECORDS][result["operation_id"]]["status"] == "success"
    assert valid_grant(saved, execution.manager.browsers[execution.session_id], operation,
                       grant["id"], result["operation_id"])
    assert not valid_grant(saved, execution.manager.browsers[execution.session_id], operation,
                           grant["id"], str(uuid4()))
    assert not valid_grant(saved, execution.manager.browsers[execution.session_id], operation, grant["id"])
    assert retry(execution, operation)["code"] == "retry_authorization_required"


@pytest.mark.parametrize("change", ["target", "revision", "browser", "token", "credentials"])
def test_used_grant_still_enforces_bindings_after_expiry(execution, change):
    operation = unknown(execution)["operation_id"]
    approve(execution, operation)
    succeed(execution)
    result = retry(execution, operation)
    saved = load(execution)
    scope = deepcopy(execution.manager.browsers[execution.session_id])
    grant = saved[GRANTS][operation]
    grant["expires_at"] = 1
    assert valid_grant(saved, scope, operation, grant["id"], result["operation_id"])
    if change == "target":
        saved["target"]["name"] = "Changed"
    elif change == "revision":
        saved["_target_revision"] = 99
    elif change == "credentials":
        saved["target"]["_credential_binding"] = {"id": 1, "revision": 2, "history_key": "changed"}
    elif change == "token":
        scope.token = "replacement"
    else:
        scope.session_id = str(uuid4())
    assert not valid_grant(saved, scope, operation, grant["id"], result["operation_id"])


@pytest.mark.parametrize("change", ["owner", "target", "browser", "credentials"])
def test_expired_used_grant_does_not_accept_result_after_authority_revocation(execution, change):
    operation = unknown(execution)["operation_id"]
    approve(execution, operation)

    async def worker(path, payload):
        raw = response(execution.manager.browsers[execution.session_id], payload["operation_id"])
        mutate(execution, lambda data: data[GRANTS][operation].update(expires_at=1))
        if change == "owner":
            execution.manager.browsers[execution.session_id].owner = "other"
        elif change == "browser":
            execution.manager.browsers[execution.session_id].session_id = str(uuid4())
        elif change == "credentials":
            mutate(execution, lambda data: data["target"].update(credential_mode="bitwarden"))
        else:
            mutate(execution, lambda data: data["target"].update(name="Changed"))
        return raw

    execution.manager._worker.side_effect = worker
    assert not retry(execution, operation)["ok"]
    execution.manager._close_browser.assert_awaited_once_with(execution.session_id)
    saved = load(execution)
    retry_operation = saved[GRANTS][operation]["retry_operation_id"]
    assert saved[RECORDS][retry_operation]["status"] == "unknown"
    assert saved[RECORDS][operation]["status"] == "unknown"


def test_retry_status_projects_current_browser_without_expiring_used_history(execution):
    operation = unknown(execution)["operation_id"]
    approve(execution, operation)
    saved = load(execution)
    scope = execution.manager.browsers[execution.session_id]
    assert retry_status(saved, scope)[0]["status"] == "approved"
    assert model_retry_status(saved, scope)[0]["status"] == "approved"
    assert retry_status(saved)[0]["status"] == "expired"
    replacement = deepcopy(scope)
    replacement.session_id = str(uuid4())
    assert retry_status(saved, replacement)[0]["status"] == "expired"
    succeed(execution)
    assert retry(execution, operation)["ok"]
    saved = load(execution)
    for current in (scope, replacement, None):
        assert retry_status(saved, current)[0]["status"] == "used"


def test_legacy_unknown_requires_original_revision_and_stored_arguments(execution):
    args = unknown(execution)
    mutate(execution, lambda data: data.pop(RECORDS))
    with execution.manager.session_factory() as db:
        execution.manager._message(db, execution.session_id, "tool", _json({
            "operation_id": args["operation_id"], "status": "unknown", "code": "outcome_uncertain",
        }), tool_name="execute_step", arguments=args, _target_revision=0)
        db.commit()
    assert approve(execution, args["operation_id"])["retry_authorizations"][0]["status"] == "approved"
    assert load(execution)[RECORDS][args["operation_id"]]["step"]["feature_type"] == "sign.button"


def test_running_session_cannot_approve(execution):
    operation = unknown(execution)["operation_id"]
    mutate(execution, lambda data: None, status="running")
    with pytest.raises(AssistantError) as caught:
        approve(execution, operation)
    assert caught.value.code == "session_running"


def test_direct_operation_reservation_retains_existing_contract(execution):
    operation = str(uuid4())
    execution._reserve(operation)
    assert operation in load(execution)["_execution_operations"]
    assert run(execution, {**arguments(), "operation_id": operation}) == {
        "ok": False, "code": "operation_already_used",
    }
    assert run(execution)["ok"]


def test_request_and_execution_records_are_bounded(execution):
    operation = unknown(execution)["operation_id"]
    mutate(execution, lambda data: data.update(retry_requests={str(uuid4()): {} for _ in range(256)}))
    with pytest.raises(AssistantError) as caught:
        approve(execution, operation)
    assert caught.value.code == "retry_request_limit"
    mutate(execution, lambda data: data.update(**{RECORDS: {
        str(uuid4()): {"state": "terminal", "status": "success"} for _ in range(256)
    }}), status="running")
    assert run(execution)["code"] == "execution_operation_limit"


def test_concurrent_retry_consumes_grant_only_once(execution, monkeypatch):
    bind(execution)
    operation = unknown(execution, "auth.form_login")["operation_id"]
    approve(execution, operation)
    succeed(execution)
    mutate(execution, lambda data: None, status="running")

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def credentials(function):
            entered.set()
            await release.wait()
            return {"username": "account", "password": "private-password"}

        monkeypatch.setattr(asyncio, "to_thread", credentials)
        task = asyncio.create_task(run_authorized_retry(
            execution.manager, OWNER, execution.session_id, operation,
        ))
        await entered.wait()
        other = await run_authorized_retry(execution.manager, OWNER, execution.session_id, operation)
        assert other["code"] == "retry_authorization_required"
        release.set()
        assert (await task)["ok"]

    asyncio.run(scenario())
    assert execution.manager._worker.await_count == 2


def test_cancellation_after_dispatch_is_durable_unknown(execution):
    args = arguments("sign.button")

    async def scenario():
        entered = asyncio.Event()

        async def worker(path, payload):
            assert load(execution)[RECORDS][args["operation_id"]]["state"] == "dispatched"
            entered.set()
            await asyncio.Event().wait()

        execution.manager._worker.side_effect = worker
        task = asyncio.create_task(execution.execute(args))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert load(execution)[RECORDS][args["operation_id"]]["status"] == "unknown"
    assert load(execution)["_context_safety"][0]["operation_id"] == args["operation_id"]


@pytest.mark.parametrize("change", ["revision", "browser"])
def test_retry_rechecks_binding_after_async_credentials(execution, monkeypatch, change):
    bind(execution)
    operation = unknown(execution, "auth.form_login")["operation_id"]
    approve(execution, operation)

    async def credentials(function):
        if change == "revision":
            mutate(execution, lambda data: data.update(_target_revision=1))
        else:
            execution.manager.browsers[execution.session_id].session_id = str(uuid4())
        return {"username": "account", "password": "private-password"}

    monkeypatch.setattr(asyncio, "to_thread", credentials)
    assert not retry(execution, operation)["ok"]
    execution.manager._worker.assert_awaited_once()
    assert load(execution)[GRANTS][operation]["status"] == "used"


def test_admitted_retry_survives_expiry_during_credentials(execution, monkeypatch):
    bind(execution)
    operation = unknown(execution, "auth.form_login")["operation_id"]
    approve(execution, operation)
    succeed(execution)

    async def credentials(function):
        mutate(execution, lambda data: data[GRANTS][operation].update(expires_at=1))
        return {"username": "account", "password": "private-password"}

    monkeypatch.setattr(asyncio, "to_thread", credentials)
    assert retry(execution, operation)["ok"]
    execution.manager._close_browser.assert_not_awaited()
    assert execution.manager._worker.await_count == 2


@pytest.mark.parametrize("phase", ["reserved", "dispatched"])
def test_restart_terminalizes_crashed_retry_and_fresh_browser_can_resume(execution, monkeypatch, phase):
    bind(execution)
    original = unknown(execution, "auth.form_login")["operation_id"]
    approve(execution, original)
    mutate(execution, lambda data: data.update(_context_summary={
        "version": 1, "target_revision": 0, "boundary_id": 0, "text": "No action is known to have run.",
    }), status="running")
    before = load(execution)
    original_state = AssistantExecution._execution_state

    async def scenario():
        entered = asyncio.Event()

        async def credentials(function):
            if phase == "reserved":
                entered.set()
                await asyncio.Event().wait()
            return {"username": "account", "password": "private-password"}

        async def worker(path, payload):
            entered.set()
            await asyncio.Event().wait()  # The dispatched response never reaches the host.

        def state(helper, state, status=None, code=None):
            # Simulate process loss: execute's Python finally cannot persist a result.
            if state != "terminal":
                original_state(helper, state, status, code)

        with monkeypatch.context() as patch:
            patch.setattr(asyncio, "to_thread", credentials)
            patch.setattr(AssistantExecution, "_execution_state", state)
            if phase == "dispatched":
                patch.setattr(execution.manager, "_worker", AsyncMock(side_effect=worker))
            task = asyncio.create_task(run_authorized_retry(
                execution.manager, OWNER, execution.session_id, original))
            async with asyncio.timeout(2):
                await entered.wait()
            crashed = load(execution)
            child = crashed[GRANTS][original]["retry_operation_id"]
            assert crashed[RECORDS][child]["state"] == phase
            assert "resolution" not in crashed[RECORDS][original]
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        fresh = AssistantManager(execution.manager.session_factory, execution.manager.settings, execution.manager.crypto)
        execution.manager = fresh
        fresh.recover()
        saved = load(execution)
        record = saved[RECORDS][child]
        assert record["state"] == "terminal" and record["step"] == before[RECORDS][original]["step"]
        assert record["dispatched"] is (phase == "dispatched")
        assert record["status"] == ("unknown" if phase == "dispatched" else "error")
        assert record["code"] == ("outcome_uncertain" if phase == "dispatched" else "execution_interrupted")
        assert saved[RECORDS][original]["status"] == "unknown"
        assert saved[GRANTS][original]["status"] == "used"
        assert saved["_context_summary"] == before["_context_summary"]
        facts = {fact["operation_id"] for fact in saved["_context_safety"]}
        assert original in facts and (child in facts) is (phase == "dispatched")
        if phase == "dispatched":
            assert saved[RECORDS][original]["resolution"] == {
                "retry_operation_id": child, "status": "unknown", "code": "outcome_uncertain",
            }
        else:
            assert "resolution" not in saved[RECORDS][original]
        fresh.recover()
        assert load(execution) == saved  # Recovery is idempotent and never invents success.
        fresh.browsers[execution.session_id] = BrowserScope(
            OWNER, str(uuid4()), "new-browser-token", "interaction", "", time.monotonic() + 900,
        )
        choices = {item["operation_id"]: item for item in fresh.detail(OWNER, execution.session_id)["unresolved_operations"]}
        candidate = child if phase == "dispatched" else original
        assert choices[candidate]["can_authorize"]
        if phase == "dispatched":
            assert not choices[original]["can_authorize"]
        assert {fact["operation_id"] for fact in fresh._context_history(OWNER, execution.session_id)[-1]} == facts
        captured = []

        async def resumed(path, payload):
            captured.append(deepcopy(payload))
            return response(fresh.browsers[execution.session_id], payload["operation_id"])

        fresh._worker = AsyncMock(side_effect=resumed)
        monkeypatch.setattr(AssistantExecution, "_credentials", lambda helper: {
            "username": "account", "password": "private-password",
        })
        mutate(execution, lambda data: None, status="running")
        tools = AssistantTools(fresh, OWNER, execution.session_id, execution.target)
        result = await tools.dispatch("retry_step", {"operation_id": candidate})
        assert not load(execution).get(PENDING)
        assert result["ok"] and result["retry_of"] == candidate
        assert len(captured) == 1 and captured[0]["step"] == record["step"]
        final = load(execution)
        assert final[RECORDS][original]["status"] == "unknown"
        assert final[RECORDS][child]["status"] == record["status"]
        assert fresh.detail(OWNER, execution.session_id)["unresolved_operations"] == []
        assert not fresh._approval_waiters

    asyncio.run(scenario())


@pytest.mark.parametrize("unrelated", [False, True])
def test_child_grant_covers_durable_ancestors_without_resolution_not_unrelated(execution, unrelated):
    original = unknown(execution)["operation_id"]
    approve(execution, original)
    child = retry(execution, original)["operation_id"]
    execution.manager._idle(execution.session_id)
    approve(execution, child)
    unrelated_id = str(uuid4())

    def change(data):
        data[RECORDS][original].pop("resolution")
        if unrelated:
            data[RECORDS][unrelated_id] = deepcopy(data[RECORDS][original])

    mutate(execution, change, status="running")
    saved = load(execution)
    helper = AssistantExecution(execution.manager, OWNER, execution.session_id, execution.target,
                                retry_authorization={"operation_id": child, "id": saved[GRANTS][child]["id"]})
    succeed(execution)
    before = execution.manager._worker.await_count
    result = run(helper, arguments("sign.button"))
    assert result["ok"] and result["retry_of"] == child
    assert execution.manager._worker.await_count == before + 1
    if unrelated:
        assert load(execution)[RECORDS][unrelated_id]["status"] == "unknown"
    assert load(execution)[RECORDS][original]["status"] == "unknown"


@pytest.fixture
def client(tmp_path):
    app = create_app(make_settings(tmp_path, config_ai_enabled=True))

    @asynccontextmanager
    async def lifespan(app):
        yield

    app.router.lifespan_context = lifespan
    with TestClient(app) as client:
        login_client(client)
        yield client


def test_api_owner_csrf_and_strict_confirmation(client):
    sid = client.post("/api/v1/assistant/sessions", json={}).json()["id"]
    url = f"/api/v1/assistant/sessions/{sid}/retry-authorizations"
    body = {"operation_id": str(uuid4()), "confirmed": True, "request_id": str(uuid4())}
    token = client.headers.pop("X-CSRF-Token")
    assert client.post(url, json=body).status_code == 403
    client.headers["X-CSRF-Token"] = token
    for patch in ({"confirmed": 1}, {"confirmed": False}, {"confirmed": "true"},
                  {"operation_id": "invalid"}, {"request_id": "invalid"}, {"step": {}}):
        result = client.post(url, json={**body, **patch})
        assert result.status_code == 422 and result.json() == {"detail": {"code": "invalid_request"}}
    assert client.post(url, json=body).json() == {"detail": {"code": "retry_not_uncertain"}}
    manager = client.app.state.assistant_manager
    with manager.session_factory() as db:
        db.get(AssistantSession, sid).owner = "other"
        db.commit()
    result = client.post(url, json=body)
    assert result.status_code == 404 and result.headers["cache-control"] == "no-store"
    client.cookies.clear()
    assert client.post(url, json=body).status_code == 401


def test_successful_api_authorization_is_local_and_returns_only_public_grant(client):
    sid = client.post("/api/v1/assistant/sessions", json={}).json()["id"]
    manager = client.app.state.assistant_manager
    target = {"name": "Example", "base_url": "https://example.com/", "credential_mode": "none",
              "confirmed": True, "proxy": False, "execution_consent": True,
              "allow_sign": True, "execution_features": ["sign.button"]}
    manager.browsers[sid] = BrowserScope("admin", str(uuid4()), TOKEN, "interaction", "", time.monotonic() + 900)
    helper = AssistantExecution(manager, "admin", sid, target)
    mutate(helper, lambda data: data.update(target=target), status="running")
    manager._worker = AsyncMock()
    operation = unknown(helper)["operation_id"]
    body = {"operation_id": operation, "confirmed": True, "request_id": str(uuid4())}
    result = client.post(f"/api/v1/assistant/sessions/{sid}/retry-authorizations", json=body)
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    assert result.json()["status"] == "idle"
    assert result.json()["retry_authorizations"][0]["status"] == "approved"
    manager._worker.assert_awaited_once()
    assert load(helper)[GRANTS][operation]["id"] not in result.text
    assert TOKEN not in result.text
