from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.features.catalog import FEATURES
from app.models.assistant import AssistantSession
from app.models.assistant_workflow import AssistantWorkflowAction
from app.models.execution import Run, SiteJob
from app.models.workflow import Site
from app.services.assistant import BrowserScope
from app.services.assistant_categories import Denied, reserve_run_auth_use, run_auth_binding
from app.services.assistant_execution import AssistantExecution
from app.services.assistant_retry import RECORDS
from app.services.credentials import CredentialService
from app.services.executor import ExecutionManager
from app.services.secret_providers import SecretDenied, SecretProviders
from app.services.workflows import WorkflowService
from tests.test_assistant_category_policy import host_turn
from tests.test_assistant_execution import arguments, response
from tests.test_assistant_retry import load, mutate
from tests.test_assistant_workflows import (  # noqa: F401
    OWNER, call, configure, context, make_site,
)


@pytest.fixture
def fullrun(context):
    site = make_site(context, steps=[
        {"step_key": "login", "feature_type": "auth.form_login", "config": {}},
        {"step_key": "verify", "feature_type": "auth.password_verify", "config": {}},
    ])
    manager, _ = context
    with manager.session_factory() as db:
        CredentialService(db, manager.crypto).update(site.id, username="account", password="private-password")
    tools = configure(context, credential_mode="manual", site_id=site.id, workflow_site_id=site.id)
    turn = host_turn(tools)
    tools.turn = turn
    executor = ExecutionManager(manager.session_factory, manager.settings, manager.crypto)
    executor.schedule_run_notification = Mock()
    manager.executor = executor
    executor.submit = Mock()
    return tools, executor


def queued(fullrun):
    tools, executor = fullrun
    result = call(tools, "run_site")
    assert result["ok"], result
    with tools.manager.session_factory() as db:
        job = db.scalar(select(SiteJob).where(SiteJob.run_id == result["run_id"]))
        return result, job.id


def registration(fullrun, job_id):
    tools, executor = fullrun
    manager = tools.manager
    with manager.session_factory() as db:
        job = db.get(SiteJob, job_id)
        site = db.get(Site, job.site_id)
        action_id, binding = run_auth_binding(db, job.run_id, manager.crypto)
        reserve = partial(reserve_run_auth_use, manager.session_factory, manager.settings,
                          manager.crypto, action_id, binding, job.id, job.attempt)
        steps = WorkflowService(db, manager.settings, manager.crypto).resolve_site(site.id).steps
        token = executor.secret_providers.register(job, site, steps, reserve_auth=reserve)
        return (token, job.id, site.id), binding


def lease(executor, scope, step="login", feature="auth.form_login", **kwargs):
    return asyncio.run(executor.secret_providers.request(
        *scope, step, feature, "builtin.manual", **kwargs))


def test_single_auth_exhaustion_denies_fullrun_before_creation_and_submit(fullrun):
    tools, executor = fullrun
    tools.turn = host_turn(tools, 1)
    scope = BrowserScope(OWNER, str(uuid4()), "private-token", "proxy_interaction", "",
                         tools.turn["deadline"] + 900)
    tools.manager.browsers[tools.session_id] = scope

    def worker(path, payload):
        if path == f"{scope.session_id}/close":
            assert payload == {"token": scope.token}
            return {"closed": True}
        assert path == f"{scope.session_id}/execute"
        return response(scope, payload["operation_id"])

    tools.manager._worker = AsyncMock(side_effect=worker)
    helper = AssistantExecution(tools.manager, OWNER, tools.session_id, tools.target)
    assert asyncio.run(helper.execute(arguments("auth.form_login")))["ok"]
    tools.manager._worker.assert_awaited_once()
    assert call(tools, "run_site") == {"ok": False, "code": "auth_budget_exhausted"}
    assert tools.manager._worker.await_count == 2
    assert tools.manager._worker.await_args.args == (f"{scope.session_id}/close", {"token": scope.token})
    assert tools.session_id not in tools.manager.browsers
    executor.submit.assert_not_called()
    with tools.manager.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Run)) == 0
        assert db.scalar(select(func.count()).select_from(AssistantWorkflowAction)) == 0


def test_executor_leases_count_actual_auth_loops_once_before_credentials(fullrun, monkeypatch):
    tools, executor = fullrun
    result, job_id = queued(fullrun)
    original = CredentialService.resolve
    credentials = []

    def resolve(service, site_id):
        credentials.append(site_id)
        assert len(load(tools)[RECORDS]) == len(credentials)
        return original(service, site_id)

    monkeypatch.setattr(CredentialService, "resolve", resolve)

    async def worker(payload):
        assert "auth_budget" not in payload and "reserve_auth" not in payload
        scope = (payload["secret_access"]["token"], job_id, payload["site_id"])
        for step, feature in [("login", "auth.form_login"), ("verify", "auth.password_verify"),
                              ("login", "auth.form_login")]:
            args = (*scope, step, feature, "builtin.manual")
            lease_value = (await executor.secret_providers.request(*args))["lease"]
            assert len(load(tools)[RECORDS]) == len(credentials) + 1
            assert (await executor.secret_providers.request(*args, lease=lease_value))["password"] == "private-password"
            with pytest.raises(SecretDenied):
                await executor.secret_providers.request(*args, lease=lease_value)
        with pytest.raises(SecretDenied, match="auth_budget_exhausted"):
            await executor.secret_providers.request(*scope, "login", "auth.form_login", "builtin.manual")
        return {"status": "success", "steps": []}

    executor._post_execute = AsyncMock(side_effect=worker)
    asyncio.run(executor._execute_job(result["run_id"], job_id))
    executor._post_execute.assert_awaited_once()
    assert len(credentials) == 3
    data = load(tools)
    assert len(data[RECORDS]) == 3
    assert {r["turn_id"] for r in data[RECORDS].values()} == {tools.turn["_request_id"]}
    before = dict(data[RECORDS])
    replay = call(tools, "run_site")
    assert replay["replayed"] and replay["run_id"] == result["run_id"]
    assert load(tools)[RECORDS] == before
    executor.submit.assert_called_once()


def test_background_and_fallback_attempts_use_frozen_origin_turn(fullrun):
    tools, executor = fullrun
    old = tools.turn = host_turn(tools, 2)
    _, job_id = queued(fullrun)
    scope, binding = registration(fullrun, job_id)
    assert not load(tools).get(RECORDS)  # Admission does not charge skipped/conditional auth.
    first = lease(executor, scope)["lease"]
    assert lease(executor, scope, lease=first)["password"]
    mutate(tools, lambda data: None, status="idle")
    tools.manager._active_turns.clear()
    new = host_turn(tools, 20)
    with tools.manager.session_factory() as db:
        job = db.get(SiteJob, job_id)
        job.attempt = 2
        job.lane = "interaction"
        db.commit()
    executor.secret_providers.revoke(job_id)
    scope, _ = registration(fullrun, job_id)
    second = lease(executor, scope, "verify", "auth.password_verify")["lease"]
    assert lease(executor, scope, "verify", "auth.password_verify", lease=second)["password"]
    with pytest.raises(SecretDenied, match="auth_budget_exhausted"):
        lease(executor, scope)
    records = load(tools)[RECORDS]
    assert len(records) == 2 and {r["attempt"] for r in records.values()} == {1, 2}
    assert {r["turn_id"] for r in records.values()} == {old["_request_id"]}
    assert binding["turn_id"] != new["_request_id"] and binding["limit"] == 2


def test_queued_run_and_later_single_step_compete_for_same_remaining_use(fullrun):
    tools, executor = fullrun
    tools.turn = host_turn(tools, 1)
    _, job_id = queued(fullrun)
    scope, _ = registration(fullrun, job_id)
    browser = BrowserScope(OWNER, str(uuid4()), "private-token", "proxy_interaction", "",
                           tools.turn["deadline"] + 900)
    tools.manager.browsers[tools.session_id] = browser
    tools.manager._worker = AsyncMock(side_effect=lambda path, payload: response(browser, payload["operation_id"]))
    helper = AssistantExecution(tools.manager, OWNER, tools.session_id, tools.target)
    assert asyncio.run(helper.execute(arguments("auth.password_verify")))["ok"]
    with pytest.raises(SecretDenied, match="auth_budget_exhausted"):
        lease(executor, scope)
    assert len(load(tools)[RECORDS]) == 1


def test_fullrun_only_snapshots_direct_tools_config(fullrun):
    tools, executor = fullrun
    tools.manager._active_turns.clear()
    tools.config = {"_assistant_settings": {"max_auth_uses_per_turn": 1}}
    _, job_id = queued(fullrun)
    scope, binding = registration(fullrun, job_id)
    assert binding["limit"] == 1
    lease(executor, scope)
    with pytest.raises(SecretDenied, match="auth_budget_exhausted"):
        lease(executor, scope, "verify", "auth.password_verify")


def test_skipped_auth_does_not_spend_budget_and_status_reads_are_free(fullrun):
    tools, executor = fullrun
    result, job_id = queued(fullrun)
    executor._post_execute = AsyncMock(return_value={"status": "success", "steps": [
        {"step_key": "login", "feature_type": "auth.form_login", "status": "skipped", "data": {}},
        {"step_key": "verify", "feature_type": "auth.password_verify", "status": "skipped", "data": {}},
    ]})
    asyncio.run(executor._execute_job(result["run_id"], job_id))
    assert call(tools, "get_run_results", {"run_id": result["run_id"]})["ok"]
    assert call(tools, "get_run_results", {"run_id": result["run_id"]})["ok"]
    assert call(tools, "run_site")["replayed"]
    assert not load(tools).get(RECORDS)


def test_executor_missing_binding_fails_before_worker_or_credentials(fullrun, monkeypatch):
    tools, executor = fullrun
    result, job_id = queued(fullrun)
    from app.services.assistant_workflows import AssistantWorkflows

    with tools.manager.session_factory() as db:
        action = db.scalar(select(AssistantWorkflowAction).where(AssistantWorkflowAction.run_id == result["run_id"]))
        workflows = AssistantWorkflows(tools.manager, OWNER, tools.session_id, tools.target)
        saved = workflows._open(action)
        saved.pop("auth_budget")
        workflows._seal(action, saved)
        db.commit()
    resolve = Mock(side_effect=AssertionError("No auth credential access"))
    monkeypatch.setattr(CredentialService, "resolve", resolve)
    executor._post_execute = AsyncMock(side_effect=AssertionError("No worker dispatch"))
    asyncio.run(executor._execute_job(result["run_id"], job_id))
    resolve.assert_not_called()
    executor._post_execute.assert_not_awaited()
    assert not load(tools).get(RECORDS)
    with tools.manager.session_factory() as db:
        assert db.get(SiteJob, job_id).status == "fail"


def test_failed_provider_still_consumes_reserved_auth_use(fullrun, monkeypatch):
    tools, executor = fullrun
    tools.turn = host_turn(tools, 1)
    _, job_id = queued(fullrun)
    scope, _ = registration(fullrun, job_id)
    monkeypatch.setattr(CredentialService, "resolve", Mock(side_effect=ValueError("private-provider-error")))
    value = lease(executor, scope)["lease"]
    with pytest.raises(SecretDenied) as error:
        lease(executor, scope, lease=value)
    assert "private-provider-error" not in str(error.value)
    with pytest.raises(SecretDenied, match="auth_budget_exhausted"):
        lease(executor, scope)
    assert len(load(tools)[RECORDS]) == 1


@pytest.mark.parametrize("damage", ["missing", "changed", "owner", "revision", "canceled"])
def test_runtime_binding_and_guard_fail_closed_before_lease(fullrun, damage):
    tools, executor = fullrun
    result, job_id = queued(fullrun)
    scope, _ = registration(fullrun, job_id)
    with tools.manager.session_factory() as db:
        action = db.scalar(select(AssistantWorkflowAction).where(AssistantWorkflowAction.run_id == result["run_id"]))
        if damage in {"missing", "changed"}:
            from app.services.assistant_workflows import AssistantWorkflows
            workflows = AssistantWorkflows(tools.manager, OWNER, tools.session_id, tools.target)
            saved = workflows._open(action)
            if damage == "missing":
                saved.pop("auth_budget")
            else:
                saved["auth_budget"]["turn_id"] = str(uuid4())
            workflows._seal(action, saved)
        elif damage == "owner":
            db.get(AssistantSession, tools.session_id).owner = "someone-else"
        elif damage == "revision":
            db.get(Site, scope[2]).config_revision += 1
        else:
            db.get(Run, result["run_id"]).cancel_requested = True
        db.commit()
    with pytest.raises(SecretDenied):
        lease(executor, scope)
    assert not executor.secret_providers.leases
    assert not load(tools).get(RECORDS)


def test_concurrent_leases_across_providers_share_atomic_budget(fullrun):
    tools, executor = fullrun
    _, job_id = queued(fullrun)
    scope, binding = registration(fullrun, job_id)
    with tools.manager.session_factory() as db:
        action_id, _ = run_auth_binding(db, db.get(SiteJob, job_id).run_id, tools.manager.crypto)

    def reserve(_):
        try:
            reserve_run_auth_use(tools.manager.session_factory, tools.manager.settings, tools.manager.crypto,
                                 action_id, binding, job_id, 1, "login", "auth.form_login", str(uuid4()))
            return True
        except Denied as exc:
            assert str(exc) == "auth_budget_exhausted"
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(8))) == 3
    assert len(load(tools)[RECORDS]) == 3


def test_lease_category_comes_from_catalog(fullrun, monkeypatch):
    tools, executor = fullrun
    monkeypatch.setitem(FEATURES, "auth.form_login", replace(FEATURES["auth.form_login"], category="challenge"))
    _, job_id = queued(fullrun)
    scope, _ = registration(fullrun, job_id)
    for _ in range(5):
        lease(executor, scope)
    assert not load(tools).get(RECORDS)


def test_lease_rejects_category_changed_after_dispatch_binding(fullrun, monkeypatch):
    tools, executor = fullrun
    _, job_id = queued(fullrun)
    scope, _ = registration(fullrun, job_id)
    monkeypatch.setitem(FEATURES, "auth.form_login", replace(FEATURES["auth.form_login"], category="challenge"))
    with pytest.raises(SecretDenied):
        lease(executor, scope)
    assert not executor.secret_providers.leases
    assert not load(tools).get(RECORDS)


@pytest.mark.parametrize("outcome", ["restart", "fallback_interaction"])
def test_executor_retries_cannot_get_another_auth_lease_after_cap(fullrun, outcome):
    tools, executor = fullrun
    tools.turn = host_turn(tools, 1)
    result, job_id = queued(fullrun)
    attempts = []

    async def worker(payload):
        attempts.append(payload["attempt"])
        args = (payload["secret_access"]["token"], job_id, payload["site_id"],
                "login", "auth.form_login", "builtin.manual")
        if payload["attempt"] == 1:
            value = (await executor.secret_providers.request(*args))["lease"]
            assert (await executor.secret_providers.request(*args, lease=value))["password"]
            return {"status": outcome, "steps": []}
        with pytest.raises(SecretDenied, match="auth_budget_exhausted"):
            await executor.secret_providers.request(*args)
        return {"status": "fail", "steps": []}

    executor._post_execute = AsyncMock(side_effect=worker)

    async def execute():
        assert await executor._execute_job(result["run_id"], job_id) == "retry"
        mutate(tools, lambda data: None, status="idle")
        tools.manager._active_turns.clear()
        host_turn(tools, 20)
        await executor._execute_job(result["run_id"], job_id)

    asyncio.run(execute())
    assert attempts == [1, 2] and len(load(tools)[RECORDS]) == 1


def test_assistant_registration_without_budget_callback_fails_closed(fullrun):
    tools, executor = fullrun
    _, job_id = queued(fullrun)
    with tools.manager.session_factory() as db:
        job = db.get(SiteJob, job_id)
        site = db.get(Site, job.site_id)
        steps = WorkflowService(db, tools.manager.settings, tools.manager.crypto).resolve_site(site.id).steps
        token = executor.secret_providers.register(job, site, steps)
    with pytest.raises(SecretDenied, match="execution_authorization_changed"):
        lease(executor, (token, job_id, site.id))
    assert not load(tools).get(RECORDS) and not executor.secret_providers.leases


def test_ordinary_auth_leases_are_not_budgeted(fullrun):
    tools, executor = fullrun
    _, job_id = queued(fullrun)
    # No callback on ordinary capabilities, even for an auth category.
    with tools.manager.session_factory() as db:
        job = db.get(SiteJob, job_id)
        db.get(Run, job.run_id).trigger = "manual"
        db.commit()
        site = db.get(Site, job.site_id)
        providers = SecretProviders(tools.manager.session_factory, tools.manager.settings, tools.manager.crypto)
        steps = WorkflowService(db, tools.manager.settings, tools.manager.crypto).resolve_site(site.id).steps
        token = providers.register(job, site, steps)
    for _ in range(5):
        asyncio.run(providers.request(token, job_id, site.id, "verify", "auth.password_verify", "builtin.manual"))
    assert not load(tools).get(RECORDS)
