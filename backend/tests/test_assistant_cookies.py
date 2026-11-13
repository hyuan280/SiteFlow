from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import time
from unittest.mock import AsyncMock, Mock
from uuid import uuid1, uuid4

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker

from app.core.crypto import CryptoService
from app.core.db import Base
from app.models.assistant import AssistantMessage, AssistantSession
from app.models.assistant_workflow import AssistantWorkflowAction
from app.models.execution import Run, SiteJob
from app.models.workflow import Site, SiteCredential
from app.schemas.assistant import (
    CookieClearInteraction, CreateSession, InteractionResponse, PublicUserInteraction, SetTarget,
)
from app.services.assistant import AssistantError, AssistantManager, BrowserScope
from app.services.assistant_cookies import apply_cookie_clear, prepare_cookie_clear, private_cookie_seed
from app.services.assistant_interactions import respond_interaction
from app.services.assistant_prompt import tool_definitions
from app.services.assistant_tools import AssistantTools
from app.services.credentials import CredentialService
from tests.test_assistant_api import (  # noqa: F401
    ROOT, assistant_client, provider, create, send, target, text, tool, wait,
)


PRIVATE = "cookie-value-must-stay-private"
URL = "https://app.example.com/start"
NAME = "clear_bound_site_cookies"
GENERATION = str(uuid4())


@pytest.fixture
def tools(settings):
    engine = create_engine(settings.database_url)
    Base.metadata.create_all(engine)
    manager = AssistantManager(sessionmaker(engine, autoflush=False), settings, CryptoService(b"c" * 32))
    sid = manager.create("admin", CreateSession())["id"]
    with manager.session_factory() as db:
        db.add_all([Site(id=1, name="Credential source", base_url=URL),
                    Site(id=2, name="Workflow destination", base_url=URL)])
        db.commit()
        service = CredentialService(db, manager.crypto)
        for site_id in (1, 2):
            service.update(site_id, username="account", password="password-retained")
            service.update_cookies(site_id, json.dumps([
                {"name": "session", "value": PRIVATE + str(site_id), "domain": "app.example.com", "path": "/private"},
                {"name": "parent", "value": PRIVATE, "domain": ".example.com"},
                {"name": "unrelated", "value": PRIVATE, "url": "https://trusted.example.net/"},
                {"name": "dot", "value": PRIVATE, "domain": ".app.example.com"},
                {"name": "url", "value": PRIVATE, "url": "https://app.example.com/exact"},
                {"name": "wrong-port", "value": PRIVATE, "url": "https://app.example.com:444/"},
            ]))
    asyncio.run(manager.set_target("admin", sid, SetTarget(
        name="Target", base_url=URL, credential_mode="none", proxy=False,
        confirmed=True, browser_consent=True, site_id=1, workflow_site_id=2)))
    with manager.session_factory() as db:
        row = manager._owned(db, "admin", sid)
        target = manager._decrypt(row)["target"]
        row.status = "running"
        db.commit()
    tool = AssistantTools(manager, "admin", sid, target)
    manager.browsers[sid] = BrowserScope("admin", str(uuid4()), "private-browser-token", "interaction",
                                        "expires", time.monotonic() + 600,
                                        site_origin="https://app.example.com")
    async def worker(path, payload):
        if path == "cookie-generation":
            assert payload == {"url": URL, "lane_key": "interaction"}
            return {"generation": GENERATION}
        return {"session_id": path.split("/")[0], "lane_key": "interaction", "cleared": True}
    manager._worker = AsyncMock(side_effect=worker)
    yield tool
    engine.dispose()


def data_for(tools):
    with tools.manager.session_factory() as db:
        return tools.manager._decrypt(tools.manager._owned(db, tools.owner, tools.session_id))


def mutate(tools, change):
    with tools.manager.session_factory() as db:
        row = tools.manager._owned(db, tools.owner, tools.session_id)
        data = tools.manager._decrypt(row)
        change(data)
        tools.manager._encrypt(row, data)
        db.commit()


def assert_unchanged(tools):
    with tools.manager.session_factory() as db:
        assert db.get(SiteCredential, 2).cookies_ciphertext
        assert db.scalar(select(AssistantWorkflowAction.id).where(
            AssistantWorkflowAction.kind == "cookie_clear")) is None
    tools.manager._worker.assert_not_awaited()


async def pending(tools):
    for _ in range(300):
        item = tools.manager.detail(tools.owner, tools.session_id)["pending_interaction"]
        if item:
            return item
        await asyncio.sleep(0.005)
    pytest.fail("No cookie-clear review")


async def reviewed(tools, *, decision="approve", during=None, operation_id=None):
    operation_id = operation_id or str(uuid4())
    task = asyncio.create_task(tools.dispatch(NAME, {"operation_id": operation_id}))
    item = await pending(tools)
    assert item["purpose"] == "cookie_clear"
    assert set(item["review"]) == {"site_id", "name", "base_url", "lane_key", "saved_cookie_count", "warnings"}
    assert item["review"]["site_id"] == 2 and item["review"]["saved_cookie_count"] == 6
    assert PRIVATE not in json.dumps(item)
    assert "\u786e\u8ba4" in item["title"] and "\u6e05\u9664" in item["description"]
    assert all(any("\u4e00" <= char <= "\u9fff" for char in warning) for warning in item["review"]["warnings"])
    assert_unchanged(tools)
    if during:
        during()
    await respond_interaction(tools.manager, tools.owner, tools.session_id, item["id"],
                              InteractionResponse(decision=decision))
    return await task


@pytest.mark.parametrize("mode", ["manual", "bitwarden", "cookie", "none"])
def test_private_open_destination_priority_filter_generation_order_and_no_reimport(tools, monkeypatch, mode):
    manager = tools.manager
    manager.browsers.clear()
    tools.target["credential_mode"] = mode
    mutate(tools, lambda data: data.update(target=tools.target))
    events = []
    resolve = CredentialService.resolve_cookies
    def read(service, site_id):
        assert events == ["generation"]
        events.append("read")
        assert site_id == 2
        return resolve(service, site_id)
    monkeypatch.setattr(CredentialService, "resolve_cookies", read)
    async def worker(path, payload):
        if path == "cookie-generation":
            assert payload == {"url": URL, "lane_key": "interaction"}
            assert not events
            events.append("generation")
            return {"generation": GENERATION}
        assert path == "open"
        assert events == ["generation", "read"]
        assert payload["cookie_generation"] == GENERATION
        assert [c["name"] for c in payload["cookies"]] == ["session", "dot", "url"]
        assert payload["cookies"][0]["value"] == PRIVATE + "2"
        return {"session_id": payload["session_id"], "lane_key": payload["lane_key"],
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()}
    manager._worker.side_effect = worker
    async def run():
        first = await tools.dispatch("browser_open", {})
        second = await tools.dispatch("browser_open", {})
        assert first["ok"] and second["ok"]
        assert PRIVATE not in json.dumps(first)
    asyncio.run(run())
    assert manager._worker.await_count == 2
    assert PRIVATE not in json.dumps(data_for(tools))
    assert PRIVATE not in json.dumps(manager.detail(tools.owner, tools.session_id))
    assert GENERATION not in json.dumps(data_for(tools))
    assert not tools.target.get("execution_consent") and not tools.target.get("credential_consent")


@pytest.mark.parametrize("binding", ["workflow", "credential", "unbound"])
def test_explicit_source_fallback_and_unbound_payload_unchanged(tools, binding):
    tools.manager.browsers.clear()
    if binding != "workflow":
        tools.target.pop("workflow_site_id", None)
        tools.target.pop("_workflow_binding", None)
    if binding == "unbound":
        tools.target.pop("site_id", None)
        tools.target.pop("_credential_binding", None)
    mutate(tools, lambda data: data.update(target=tools.target))
    async def worker(path, payload):
        if path == "cookie-generation":
            assert binding != "unbound"
            return {"generation": GENERATION}
        if binding == "unbound":
            assert set(payload) == {"url", "lane_key", "token", "session_id", "trusted_origins"}
        else:
            assert payload["cookies"][0]["value"] == PRIVATE + ("2" if binding == "workflow" else "1")
        return {"session_id": payload["session_id"], "lane_key": payload["lane_key"],
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()}
    tools.manager._worker.side_effect = worker
    assert asyncio.run(tools.dispatch("browser_open", {}))["ok"]


@pytest.mark.parametrize("drift", ["origin", "history", "revision", "target", "missing_binding"])
def test_open_drift_fails_before_cookie_decrypt_without_fallback(tools, monkeypatch, drift):
    decrypt = Mock(side_effect=AssertionError("must not decrypt cookies"))
    monkeypatch.setattr(CredentialService, "resolve_cookies", decrypt)
    if drift == "target":
        mutate(tools, lambda data: data["target"].update(name="New target"))
    elif drift == "missing_binding":
        tools.target.pop("_workflow_binding")
        mutate(tools, lambda data: data.update(target=tools.target))
    else:
        with tools.manager.session_factory() as db:
            site = db.get(Site, 2)
            if drift == "origin":
                site.base_url = "https://other.example.com/"
            elif drift == "history":
                site.history_key = str(uuid4())
            else:
                site.config_revision += 1
            db.commit()
    result = asyncio.run(tools.dispatch("browser_open", {}))
    assert result == {"ok": False, "code": "cookie_binding_changed"}
    decrypt.assert_not_called()
    tools.manager._worker.assert_not_awaited()


def test_open_empty_cookies_omits_generation_and_decrypt_errors_are_static(tools, monkeypatch):
    tools.manager.browsers.clear()
    monkeypatch.setattr(CredentialService, "resolve_cookies", Mock(side_effect=ValueError(PRIVATE)))
    result = asyncio.run(tools.dispatch("browser_open", {}))
    assert result == {"ok": False, "code": "cookie_restore_unavailable"}
    tools.manager._worker.assert_awaited_once_with("cookie-generation", {"url": URL, "lane_key": "interaction"})
    monkeypatch.setattr(CredentialService, "resolve_cookies", lambda *args: [])
    async def worker(path, payload):
        if path == "cookie-generation":
            return {"generation": GENERATION}
        assert "cookies" not in payload and "cookie_generation" not in payload
        return {"session_id": payload["session_id"], "lane_key": payload["lane_key"],
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()}
    tools.manager._worker.side_effect = worker
    assert asyncio.run(tools.dispatch("browser_open", {}))["ok"]


def test_bound_without_saved_cookies_does_not_request_generation_or_resolve(tools, monkeypatch):
    tools.manager.browsers.clear()
    with tools.manager.session_factory() as db:
        CredentialService(db, tools.manager.crypto).delete_cookies(2)
    resolve = Mock(side_effect=AssertionError("no cookie read expected"))
    monkeypatch.setattr(CredentialService, "resolve_cookies", resolve)
    async def worker(path, payload):
        assert path == "open"
        assert "cookies" not in payload and "cookie_generation" not in payload
        return {"session_id": payload["session_id"], "lane_key": payload["lane_key"],
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()}
    tools.manager._worker.side_effect = worker
    assert asyncio.run(tools.dispatch("browser_open", {}))["ok"]
    tools.manager._worker.assert_awaited_once()
    resolve.assert_not_called()


@pytest.mark.parametrize("response", [RuntimeError(PRIVATE), {}, {"generation": "invalid"},
                                       {"generation": str(uuid1())}, {"generation": 42}])
def test_generation_failure_static_before_decrypt_or_open(tools, monkeypatch, response):
    tools.manager.browsers.clear()
    resolve = Mock(side_effect=AssertionError("no cookie decryption before valid generation"))
    monkeypatch.setattr(CredentialService, "resolve_cookies", resolve)
    async def worker(path, payload):
        assert path == "cookie-generation"
        if isinstance(response, Exception):
            raise response
        return response
    tools.manager._worker.side_effect = worker
    assert asyncio.run(tools.dispatch("browser_open", {})) == {
        "ok": False, "code": "cookie_restore_unavailable"}
    tools.manager._worker.assert_awaited_once()
    resolve.assert_not_called()
    assert not tools.manager.browsers


@pytest.mark.parametrize("drift", ["target", "binding", "removed"])
def test_generation_await_reloads_target_binding_and_cookie_snapshot(tools, monkeypatch, drift):
    tools.manager.browsers.clear()
    original = CredentialService.resolve_cookies
    resolve = Mock(side_effect=original)
    monkeypatch.setattr(CredentialService, "resolve_cookies", resolve)
    async def worker(path, payload):
        if path == "cookie-generation":
            if drift == "target":
                mutate(tools, lambda data: data["target"].update(name="Changed"))
            else:
                with tools.manager.session_factory() as db:
                    if drift == "binding":
                        db.get(Site, 2).config_revision += 1
                        db.commit()
                    else:
                        CredentialService(db, tools.manager.crypto).delete_cookies(2)
            return {"generation": GENERATION}
        assert drift == "removed" and path == "open"
        assert "cookies" not in payload and "cookie_generation" not in payload
        return {"session_id": payload["session_id"], "lane_key": payload["lane_key"],
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()}
    tools.manager._worker.side_effect = worker
    # Keep the descriptor binding when wrapping the real resolver.
    monkeypatch.setattr(CredentialService, "resolve_cookies", lambda service, site_id: resolve(service, site_id))
    result = asyncio.run(tools.dispatch("browser_open", {}))
    if drift == "removed":
        assert result["ok"]
    else:
        assert result == {"ok": False, "code": "cookie_binding_changed"}
        resolve.assert_not_called()
        tools.manager._worker.assert_awaited_once()


@pytest.mark.parametrize("generation", ["invalid", str(uuid1()), None])
def test_private_seed_validates_injected_generation_before_resolution(tools, monkeypatch, generation):
    resolve = Mock(side_effect=AssertionError("must validate before resolve"))
    monkeypatch.setattr(CredentialService, "resolve_cookies", resolve)
    with tools.manager.session_factory() as db, pytest.raises(AssistantError) as error:
        private_cookie_seed(db, tools.manager, tools.target, generation)
    assert error.value.code == "cookie_restore_unavailable"
    resolve.assert_not_called()


def test_private_seed_uses_only_injected_generation_and_filtered_rows(tools):
    with tools.manager.session_factory() as db:
        payload = private_cookie_seed(db, tools.manager, tools.target, GENERATION)
    assert set(payload) == {"cookies", "cookie_generation"}
    assert payload["cookie_generation"] == GENERATION
    assert [row["name"] for row in payload["cookies"]] == ["session", "dot", "url"]


@pytest.mark.parametrize("decision", ["approve", "deny"])
def test_real_approval_exact_scope_commit_before_worker_and_cookie_only_deletion(tools, decision):
    scope = tools.manager.browsers[tools.session_id]
    async def worker(path, payload):
        assert path == f"{scope.session_id}/clear-cookies"
        assert payload["token"] == scope.token and set(payload) == {"token", "operation_id"}
        with tools.manager.session_factory() as db:
            credential = db.get(SiteCredential, 2)
            assert credential.cookies_ciphertext is None
            assert credential.cookie_count == 0 and credential.cookies_updated_at is None
            assert CredentialService(db, tools.manager.crypto).resolve(2).password == "password-retained"
            assert db.get(SiteCredential, 1).cookies_ciphertext
            action = db.scalar(select(AssistantWorkflowAction).where(AssistantWorkflowAction.kind == "cookie_clear"))
            assert action.state == "unknown"
        return {"session_id": scope.session_id, "lane_key": scope.lane_key, "cleared": True,
                "untrusted_extra": PRIVATE}
    tools.manager._worker.side_effect = worker
    operation_id = str(uuid4())
    result = asyncio.run(reviewed(tools, decision=decision, operation_id=operation_id))
    assert result["ok"] == (decision == "approve")
    if decision == "approve":
        assert result["saved_cookies_cleared"] and result["browser_cookies_cleared"]
        assert result["browser_outcome"] == "cleared"
        tools.manager._worker.assert_awaited_once()
        assert tools.manager.browsers[tools.session_id] is scope
    else:
        assert result["code"] == "interaction_denied"
        assert_unchanged(tools)
    replay = asyncio.run(tools.dispatch(NAME, {"operation_id": operation_id}))
    assert replay == result
    assert tools.manager._worker.await_count == (1 if decision == "approve" else 0)
    assert PRIVATE not in json.dumps(result) and PRIVATE not in json.dumps(data_for(tools))


@pytest.mark.parametrize("scope_change", ["absent", "expired", "not_ready", "origin", "lane", "owner"])
def test_no_ready_matching_scope_fails_before_confirmation_without_navigation(tools, scope_change):
    scope = tools.manager.browsers[tools.session_id]
    if scope_change == "absent":
        tools.manager.browsers.clear()
    elif scope_change == "expired":
        scope.deadline = 0
    elif scope_change == "not_ready":
        scope.ready = False
    elif scope_change == "origin":
        scope.site_origin = "https://other.example.com"
    elif scope_change == "lane":
        scope.lane_key = "proxy_interaction"
    else:
        scope.owner = "another-owner"
    assert asyncio.run(tools.dispatch(NAME, {"operation_id": str(uuid4())})) == {
        "ok": False, "code": "browser_reopen_required"}
    assert not data_for(tools).get("_interaction_decisions")
    assert_unchanged(tools)


@pytest.mark.parametrize("change", ["cookies", "revision", "history", "origin", "scope", "queued", "running"])
def test_review_drift_and_active_job_revalidation_write_nothing(tools, change):
    def drift():
        if change == "scope":
            tools.manager.browsers[tools.session_id].token = "different-token"
            return
        with tools.manager.session_factory() as db:
            if change == "cookies":
                CredentialService(db, tools.manager.crypto).update_cookies(2, "session=changed")
            elif change in {"queued", "running"}:
                run = Run(created_by="admin")
                db.add(run)
                db.flush()
                db.add(SiteJob(run_id=run.id, site_id=2, site_name="Bound", lane="general", status=change))
            else:
                site = db.get(Site, 2)
                if change == "revision":
                    site.config_revision += 1
                elif change == "history":
                    site.history_key = str(uuid4())
                else:
                    site.base_url = "https://other.example.com/"
            db.commit()
    result = asyncio.run(reviewed(tools, during=drift))
    assert not result["ok"]
    assert result["code"] in {"cookie_clear_binding_changed", "cookie_binding_changed", "site_run_active"}
    assert_unchanged(tools)


@pytest.mark.parametrize("outcome", ["exception", "bad_response", "cancel"])
def test_unknown_is_durable_and_replay_never_retries_or_reapproves(tools, outcome):
    operation_id = str(uuid4())
    async def worker(path, payload):
        if outcome == "cancel":
            raise asyncio.CancelledError
        if outcome == "exception":
            raise RuntimeError(PRIVATE)
        return {"session_id": str(uuid4()), "lane_key": "interaction", "cleared": True, "cookie": PRIVATE}
    tools.manager._worker.side_effect = worker
    if outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(reviewed(tools, operation_id=operation_id))
    else:
        result = asyncio.run(reviewed(tools, operation_id=operation_id))
        assert result["saved_cookies_cleared"] and not result["browser_cookies_cleared"]
        assert result["browser_outcome"] == "unknown" and PRIVATE not in json.dumps(result)
    replay = asyncio.run(tools.dispatch(NAME, {"operation_id": operation_id}))
    assert replay["saved_cookies_cleared"] and replay["browser_outcome"] == "unknown"
    assert tools.manager.detail(tools.owner, tools.session_id)["pending_interaction"] is None
    tools.manager._worker.assert_awaited_once()


def test_scope_changed_after_approval_preserves_saved_clear_without_mutating_new_browser(tools, monkeypatch):
    from app.services import assistant_interactions

    interaction = assistant_interactions.request_interaction
    async def change_after_approval(current, arguments):
        result = await interaction(current, arguments)
        if result.get("ok"):
            tools.manager.browsers[tools.session_id].session_id = str(uuid4())
        return result
    monkeypatch.setattr(assistant_interactions, "request_interaction", change_after_approval)
    result = asyncio.run(reviewed(tools))
    assert result["saved_cookies_cleared"] and not result["browser_cookies_cleared"]
    assert result["code"] == "browser_scope_changed"
    tools.manager._worker.assert_not_awaited()
    with tools.manager.session_factory() as db:
        assert db.get(SiteCredential, 2).cookies_ciphertext is None


def test_restart_after_approved_delete_preserves_unknown_without_worker_replay(tools):
    operation_id = str(uuid4())
    tools._cookie_clear_request = operation_id
    with tools.manager.session_factory() as db:
        data = data_for(tools)
        _, args, _ = prepare_cookie_clear(db, tools, CookieClearInteraction(
            purpose="cookie_clear", operation_id=operation_id), data)
        apply_cookie_clear(db, tools, data, args)
        db.commit()
    fresh = AssistantManager(tools.manager.session_factory, tools.manager.settings, tools.manager.crypto)
    fresh._worker = AsyncMock()
    fresh.recover()
    replay_tools = AssistantTools(fresh, tools.owner, tools.session_id, None)
    result = asyncio.run(replay_tools.dispatch(NAME, {"operation_id": operation_id}))
    assert result["saved_cookies_cleared"] and result["browser_outcome"] == "unknown"
    fresh._worker.assert_not_awaited()
    assert fresh.detail(tools.owner, tools.session_id)["pending_interaction"] is None


def test_replay_while_worker_in_flight_cannot_dispatch_a_second_clear(tools):
    operation_id = str(uuid4())
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        scope = tools.manager.browsers[tools.session_id]
        async def worker(path, payload):
            started.set()
            await release.wait()
            return {"session_id": scope.session_id, "lane_key": scope.lane_key, "cleared": True}
        tools.manager._worker.side_effect = worker
        first = asyncio.create_task(reviewed(tools, operation_id=operation_id))
        await started.wait()
        replay = await tools.dispatch(NAME, {"operation_id": operation_id})
        assert replay["saved_cookies_cleared"] and replay["browser_outcome"] == "unknown"
        tools.manager._worker.assert_awaited_once()
        release.set()
        assert (await first)["browser_outcome"] == "cleared"
        assert (await tools.dispatch(NAME, {"operation_id": operation_id}))["browser_outcome"] == "cleared"
        tools.manager._worker.assert_awaited_once()
    asyncio.run(run())


def test_cancel_during_review_has_no_effects(tools):
    async def run():
        task = asyncio.create_task(tools.dispatch(NAME, {"operation_id": str(uuid4())}))
        await pending(tools)
        assert_unchanged(tools)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert_unchanged(tools)


def test_timeout_has_no_effects(tools, monkeypatch):
    monkeypatch.setattr("app.services.assistant_interactions.INTERACTION_TIMEOUT", 0.01)
    result = asyncio.run(tools.dispatch(NAME, {"operation_id": str(uuid4())}))
    assert result["code"] == "interaction_timed_out"
    assert_unchanged(tools)


def test_internal_only_schema_and_dispatch_reject_arbitrary_destinations(tools):
    definitions = {d["function"]["name"]: d["function"] for d in tool_definitions()}
    schema = definitions[NAME]["parameters"]
    Draft202012Validator.check_schema(schema)
    assert set(schema["properties"]) == {"operation_id", "title"}
    assert set(schema["required"]) == {"operation_id", "title"}
    for extra in ({"site_id": 1}, {"domain": "example.com"}, {"url": URL}, {"force": True}):
        assert asyncio.run(tools.dispatch(NAME, {"operation_id": str(uuid4()), **extra}))["code"] == "invalid_arguments"
    for operation_id in (str(uuid1()), "bad", 42, True):
        assert asyncio.run(tools.dispatch(NAME, {"operation_id": operation_id}))["code"] == "invalid_arguments"
    request = {"purpose": "cookie_clear", "operation_id": str(uuid4())}
    with pytest.raises(ValidationError):
        TypeAdapter(PublicUserInteraction).validate_python(request)
    assert asyncio.run(tools.dispatch("request_user_interaction", {"request": request}))["code"] == "invalid_arguments"
    with tools.manager.session_factory() as db:
        with pytest.raises(AssistantError):
            prepare_cookie_clear(db, tools, CookieClearInteraction(**request), data_for(tools))
        with pytest.raises(AssistantError):
            apply_cookie_clear(db, tools, data_for(tools), {"operation_id": request["operation_id"]})
    assert_unchanged(tools)


def test_autonomous_takeover_denied_before_confirmation(tools):
    mutate(tools, lambda data: data.update(_takeover={"untrusted": True}))
    assert asyncio.run(tools.dispatch(NAME, {"operation_id": str(uuid4())}))["code"] == "takeover_tool_denied"
    assert_unchanged(tools)


def test_human_daily_followup_can_clear_only_after_fresh_approval(tools):
    mutate(tools, lambda data: data.update(_takeover_daily={"id": str(uuid4()), "owner": tools.owner}))
    result = asyncio.run(reviewed(tools))
    assert result["ok"] and result["browser_outcome"] == "cleared"
    tools.manager._worker.assert_awaited_once()


def test_active_automated_turn_denied_even_with_daily_metadata(tools):
    mutate(tools, lambda data: data.update(_takeover_daily={"id": str(uuid4())}))
    tools.manager._active_turns[tools.session_id] = {"_takeover_action_id": str(uuid4())}
    assert asyncio.run(tools.dispatch(NAME, {"operation_id": str(uuid4())}))["code"] == "takeover_tool_denied"
    assert_unchanged(tools)


@pytest.mark.parametrize("field,value", [
    ("cookies_ciphertext", b"replaced-encrypted-cookies"), ("cookies_nonce", b"replaced-nonce"),
    ("cookie_count", 99), ("key_version", 999),
])
def test_cookie_cas_rejects_replacement_after_fingerprint_check(tools, monkeypatch, field, value):
    from sqlalchemy.sql.dml import Update

    operation_id = str(uuid4())
    tools._cookie_clear_request = operation_id
    with tools.manager.session_factory() as db:
        data = data_for(tools)
        _, args, _ = prepare_cookie_clear(db, tools, CookieClearInteraction(
            purpose="cookie_clear", operation_id=operation_id), data)
        execute = db.execute
        injected = False
        def race(statement, *positional, **kwargs):
            nonlocal injected
            if isinstance(statement, Update) and statement.table.name == "site_credentials" and not injected:
                injected = True
                execute(update(SiteCredential).where(SiteCredential.site_id == 2).values(
                    **{field: value}).execution_options(synchronize_session=False))
            return execute(statement, *positional, **kwargs)
        monkeypatch.setattr(db, "execute", race)
        with pytest.raises(AssistantError) as error:
            apply_cookie_clear(db, tools, data, args)
        assert error.value.code == "cookie_clear_binding_changed" and injected
        # Commit the injected replacement to prove the failed CAS did not erase it.
        db.commit()
    with tools.manager.session_factory() as db:
        credential = db.get(SiteCredential, 2)
        assert getattr(credential, field) == value
        assert credential.cookies_ciphertext and credential.cookie_count > 0
        assert db.scalar(select(AssistantWorkflowAction.id)) is None
    tools.manager._worker.assert_not_awaited()


@pytest.mark.parametrize("field,value", [("config_revision", 999), ("history_key", str(uuid4())),
                                         ("base_url", "https://other.example.com/")])
def test_site_identity_conditional_lock_rejects_post_check_drift(tools, monkeypatch, field, value):
    from sqlalchemy.sql.dml import Update

    operation_id = str(uuid4())
    tools._cookie_clear_request = operation_id
    with tools.manager.session_factory() as db:
        data = data_for(tools)
        _, args, _ = prepare_cookie_clear(db, tools, CookieClearInteraction(
            purpose="cookie_clear", operation_id=operation_id), data)
        execute = db.execute
        def race(statement, *positional, **kwargs):
            if isinstance(statement, Update) and statement.table.name == "sites":
                execute(update(Site).where(Site.id == 2).values(**{field: value})
                        .execution_options(synchronize_session=False))
            return execute(statement, *positional, **kwargs)
        monkeypatch.setattr(db, "execute", race)
        with pytest.raises(AssistantError) as error:
            apply_cookie_clear(db, tools, data, args)
        assert error.value.code == "cookie_clear_binding_changed"
        db.commit()
    assert_unchanged(tools)


def test_repopulated_cookie_snapshot_after_approval_blocks_browser_clear_and_replay(tools, monkeypatch):
    from app.services import assistant_interactions

    interaction = assistant_interactions.request_interaction
    async def repopulate(current, arguments):
        result = await interaction(current, arguments)
        if result.get("ok"):
            with tools.manager.session_factory() as db:
                CredentialService(db, tools.manager.crypto).update_cookies(2, "session=repopulated")
        return result
    monkeypatch.setattr(assistant_interactions, "request_interaction", repopulate)
    operation_id = str(uuid4())
    result = asyncio.run(reviewed(tools, operation_id=operation_id))
    assert result["saved_cookies_cleared"] and not result["browser_cookies_cleared"]
    assert result["code"] == "saved_cookies_changed"
    assert asyncio.run(tools.dispatch(NAME, {"operation_id": operation_id})) == result
    tools.manager._worker.assert_not_awaited()
    with tools.manager.session_factory() as db:
        assert CredentialService(db, tools.manager.crypto).resolve_cookies(2)[0]["value"] == "repopulated"


def test_apply_rollback_preserves_password_and_cookies(tools):
    operation_id = str(uuid4())
    tools._cookie_clear_request = operation_id
    with tools.manager.session_factory() as db:
        _, args, _ = prepare_cookie_clear(db, tools, CookieClearInteraction(
            purpose="cookie_clear", operation_id=operation_id), data_for(tools))
        result = apply_cookie_clear(db, tools, data_for(tools), args)
        assert result["saved_cookies_cleared"]
        db.rollback()
    assert_unchanged(tools)


@pytest.mark.parametrize("credential_row", [True, False])
def test_clear_without_saved_cookies_still_clears_ready_browser(tools, credential_row):
    with tools.manager.session_factory() as db:
        row = db.get(SiteCredential, 2)
        if credential_row:
            CredentialService(db, tools.manager.crypto).delete_cookies(2)
        else:
            db.delete(row)
            db.commit()
    async def run():
        task = asyncio.create_task(tools.dispatch(NAME, {"operation_id": str(uuid4())}))
        item = await pending(tools)
        assert item["review"]["saved_cookie_count"] == 0
        await respond_interaction(tools.manager, tools.owner, tools.session_id, item["id"],
                                  InteractionResponse(decision="approve"))
        return await task
    assert asyncio.run(run())["browser_outcome"] == "cleared"
    tools.manager._worker.assert_awaited_once()


@pytest.mark.parametrize("status", ["queued", "running"])
def test_active_site_job_denied_before_review(tools, status):
    with tools.manager.session_factory() as db:
        run = Run(created_by="admin")
        db.add(run)
        db.flush()
        db.add(SiteJob(run_id=run.id, site_id=2, site_name="Bound", lane="general", status=status))
        db.commit()
    result = asyncio.run(tools.dispatch(NAME, {"operation_id": str(uuid4())}))
    assert result == {"ok": False, "code": "site_run_active"}
    assert not data_for(tools).get("_interaction_decisions")
    assert_unchanged(tools)


def test_real_model_open_and_clear_keep_cookie_values_and_browser_identity_private(assistant_client, provider):
    client = assistant_client
    manager = client.app.state.assistant_manager
    with manager.session_factory() as db:
        db.add(Site(id=1, name="Bound", base_url=URL))
        db.commit()
        service = CredentialService(db, manager.crypto)
        service.update(1, username="account", password="retained")
        service.update_cookies(1, "session=" + PRIVATE)
    sid = create(client)
    target(client, sid, base_url=URL, credential_mode="none", workflow_site_id=1)
    worker_private = {}
    async def generation(request):
        assert str(request.url).endswith("/assistant-browser/cookie-generation")
        assert json.loads(request.content) == {"url": URL, "lane_key": "interaction"}
        return {"generation": GENERATION}
    async def opened(request):
        payload = json.loads(request.content)
        assert payload["cookies"][0]["value"] == PRIVATE and payload["cookie_generation"] == GENERATION
        worker_private.update(session_id=payload["session_id"], token=payload["token"])
        return {"session_id": payload["session_id"], "lane_key": "interaction",
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()}
    provider[0].extend([tool("browser_open"), generation, opened, text("Opened the bound site")])
    send(client, sid)
    assert wait(client, sid)["browser"]["status"] == "ready"
    operation_id = str(uuid4())
    async def cleared(request):
        payload = json.loads(request.content)
        assert payload == {"token": worker_private["token"], "operation_id": operation_id}
        with manager.session_factory() as db:
            assert db.get(SiteCredential, 1).cookies_ciphertext is None
        return {"session_id": worker_private["session_id"], "lane_key": "interaction", "cleared": True}
    provider[0].extend([tool(NAME, {"operation_id": operation_id}), cleared,
                        tool(NAME, {"operation_id": operation_id}, "replay"), text("Cookies cleared without reload")])
    send(client, sid, content="Clear the bound site's saved and browser cookies")
    for _ in range(300):
        review = client.get(f"{ROOT}/{sid}").json()["pending_interaction"]
        if review:
            break
        time.sleep(0.01)
    assert review and review["purpose"] == "cookie_clear"
    assert client.post(f"{ROOT}/{sid}/interactions/{review['id']}/response", json={"decision": "approve"}).status_code == 200
    detail = wait(client, sid)
    results = [json.loads(message["content"]) for message in detail["messages"] if message.get("tool_name") == NAME]
    assert len(results) == 2 and results[0] == results[1] and results[0]["browser_outcome"] == "cleared"
    _, history = manager._history("admin", sid)
    with manager.session_factory() as db:
        session_data = manager._decrypt(db.get(AssistantSession, sid))
        messages = [manager._decrypt(message) for message in db.scalars(select(AssistantMessage).where(
            AssistantMessage.session_id == sid))]
    assert PRIVATE not in json.dumps([detail, history, session_data, messages])
    for secret in (PRIVATE, GENERATION, worker_private["session_id"], worker_private["token"]):
        assert secret not in json.dumps([detail, history, messages])
        assert all(secret not in request.content.decode() for request in provider[1]
                   if "chat/completions" in str(request.url))
    provider[0].append({"closed": True})
    assert client.post(f"{ROOT}/{sid}/cancel").status_code == 200
    assert not provider[0]
