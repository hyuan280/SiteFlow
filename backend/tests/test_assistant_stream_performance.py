from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from fastapi import Request
import pytest
from sqlalchemy import select

from app.api.v1 import assistant as api
from app.core.auth import SESSION_COOKIE
from app.models.assistant import AssistantMessage, AssistantSession
from app.models.auth import AuthSession
from app.schemas.assistant import CreateSession
from app.services.assistant import BrowserScope
from app.services.config_ai_generation import _json


def stream_request(client, identifier):
    cookie = client.cookies.get(SESSION_COOKIE)
    request = Request({
        "type": "http", "method": "GET", "path": f"/api/v1/assistant/sessions/{identifier}/events",
        "headers": [(b"cookie", f"{SESSION_COOKIE}={cookie}".encode())], "app": client.app,
    })
    request.is_disconnected = AsyncMock(return_value=False)
    return request


def test_detail_decrypts_each_envelope_once_and_keeps_full_history_and_content(client, monkeypatch):
    manager = client.app.state.assistant_manager
    identifier = manager.create("admin", CreateSession())["id"]
    legacy, retry, resolved = (str(uuid4()) for _ in range(3))
    full_content = _json({"text": "observation " * 10000, "tail": "original end"})
    with manager.session_factory() as db:
        row = db.get(AssistantSession, identifier)
        data = manager._decrypt(row)
        data.update(target={"name": "Example", "base_url": "https://example.com/"}, _target_revision=7,
                    _execution_records={
                        "canonical": {"state": "dispatched", "step": {"feature_type": "action.wait"}},
                        resolved: {"status": "unknown", "resolution": {"retry_operation_id": "success"}},
                        "success": {"retry_of": resolved, "state": "terminal", "status": "success",
                                    "dispatched": True},
                    }, _context_safety=[{"operation_id": "safety", "status": "unknown"}])
        manager._encrypt(row, data)
        for index in range(1000):
            fields = {"tool_name": "browser_observe", "arguments": {"private": "PRIVATE_ARGUMENT"}}
            content = full_content if index >= 800 else _json({"ok": True})
            if index < 3:
                operation = [legacy, retry, resolved][index]
                fields.update(tool_name="retry_step" if index == 1 else "execute_step",
                              _target_revision=7,
                              arguments={"step": {"step_key": "wait", "feature_type": "action.wait",
                                                  "config": {}}})
                content = _json({"operation_id": operation, "status": "unknown",
                                 "code": "outcome_uncertain"})
            manager._message(db, identifier, "tool", content, status="success", title="Observation", **fields)
        manager._message(db, identifier, "system", "PRIVATE_IMAGE", context_image=True)
        manager._message(db, identifier, "assistant", "", tool_calls=[{"private": "PRIVATE_CALL"}])
        manager._message(db, identifier, "assistant", "Visible reply")
        db.commit()

    decrypt = manager._decrypt
    calls = Counter()

    def counted(row):
        calls[(type(row), row.id)] += 1
        return decrypt(row)

    monkeypatch.setattr(manager, "_decrypt", counted)
    detail = manager.detail("admin", identifier)
    assert len(calls) == 1004  # Session, full tool history, and three recent non-tool rows.
    assert set(calls.values()) == {1}
    assert len(detail["messages"]) == 198
    assert detail["messages"][-1]["content"] == "Visible reply"
    tools = detail["messages"][:-1]
    assert all(message["content"] == full_content for message in tools)
    assert all(set(message) == {"id", "role", "content", "title", "tool_name", "status", "created_at"}
               for message in tools)
    assert [message["id"] for message in tools] == sorted(message["id"] for message in tools)
    assert {item["operation_id"] for item in detail["unresolved_operations"]} == {
        legacy, retry, "canonical", "safety",
    }
    assert next(item for item in detail["unresolved_operations"]
                if item["operation_id"] == legacy)["feature_type"] == "action.wait"
    assert "PRIVATE" not in _json(detail)

    # Caches are scoped to one projection, never stale across status or envelope changes.
    with manager.session_factory() as db:
        message = db.get(AssistantMessage, tools[-1]["id"])
        value = decrypt(message)
        value["content"] = "Updated result"
        manager._encrypt(message, value)
        message.status = "error"
        db.commit()
    calls.clear()
    updated = manager.detail("admin", identifier)
    assert set(calls.values()) == {1}
    assert updated["messages"][-2]["content"] == "Updated result"
    assert updated["messages"][-2]["status"] == "error"


def test_list_sessions_decrypts_once_per_owned_row(client, monkeypatch):
    manager = client.app.state.assistant_manager
    identifiers = {manager.create("admin", CreateSession(title=f"Session {index}"))["id"]
                   for index in range(3)}
    manager.create("other", CreateSession(title="Private session"))
    decrypt = manager._decrypt
    calls = []

    def counted(row):
        calls.append(row.id)
        return decrypt(row)

    monkeypatch.setattr(manager, "_decrypt", counted)
    result = manager.list_sessions("admin")
    assert {item["id"] for item in result["items"]} == identifiers
    assert Counter(calls) == Counter({identifier: 1 for identifier in identifiers})


@pytest.mark.parametrize("phase", ["auth", "detail", "encode"])
def test_stream_snapshot_work_does_not_block_event_loop(client, monkeypatch, phase):
    manager = client.app.state.assistant_manager
    identifier = manager.create("admin", CreateSession())["id"]
    request = stream_request(client, identifier)
    original = {"auth": api.get_readonly_current_session, "detail": manager.detail, "encode": api._json}[phase]
    target, name = (manager, "detail") if phase == "detail" else (
        api, "get_readonly_current_session" if phase == "auth" else "_json")

    async def run():
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        entered, release = asyncio.Event(), threading.Event()
        calls = []

        def blocking(*args, **kwargs):
            assert threading.get_ident() != loop_thread
            calls.append(threading.get_ident())
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(3), "The event loop must run while snapshot work is blocked"
            return original(*args, **kwargs)

        monkeypatch.setattr(target, name, blocking)
        pending = asyncio.create_task(api.events(UUID(identifier), request, "admin", manager))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert not pending.done()
            release.set()
            response = await asyncio.wait_for(pending, 2)
            assert (await anext(response.body_iterator)).startswith("event: update\n")
            entered.clear()
            release.clear()
            poll = asyncio.create_task(anext(response.body_iterator))
            await asyncio.wait_for(entered.wait(), 2)
            assert not poll.done()
            release.set()
            assert await asyncio.wait_for(poll, 2) == ": heartbeat\n\n"
            assert len(calls) == 2  # No duplicate initial snapshot.
            await response.body_iterator.aclose()
        finally:
            release.set()

    asyncio.run(run())


def test_running_stream_suppresses_unchanged_updates_but_emits_tool_status_changes(client):
    manager = client.app.state.assistant_manager
    identifier = manager.create("admin", CreateSession())["id"]
    with manager.session_factory() as db:
        db.get(AssistantSession, identifier).status = "running"
        tool = manager._message(db, identifier, "tool", "Full result", tool_name="browser_read",
                                status="running", title="Read page")
        db.commit()
        message_id = tool.id

    async def run():
        response = await api.events(UUID(identifier), stream_request(client, identifier), "admin", manager)
        iterator = response.body_iterator
        first = await anext(iterator)
        assert json.loads(first.split("data: ", 1)[1])["status"] == "running"
        assert await anext(iterator) == ": heartbeat\n\n"
        with manager.session_factory() as db:
            db.get(AssistantMessage, message_id).status = "success"
            db.commit()
        changed = await anext(iterator)
        detail = json.loads(changed.split("data: ", 1)[1])
        assert detail["status"] == "running"
        assert detail["messages"][-1]["status"] == "success"
        assert await anext(iterator) == ": heartbeat\n\n"
        await iterator.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("field,value", [
    ("title", "Renamed while streaming"),
    ("target", {"base_url": "https://example.com/new"}),
    ("_pending_retry_approval", {
        "id": "approval", "operation_id": "operation", "feature_type": "action.click",
        "status": "pending", "created_at": "2026-01-01T00:00:00+00:00",
        "expires_at": "2026-01-01T00:03:00+00:00", "title": "Review retry",
    }),
])
def test_stream_emits_encrypted_session_changes_without_updated_at_change(client, field, value):
    manager = client.app.state.assistant_manager
    identifier = manager.create("admin", CreateSession())["id"]

    async def run():
        response = await api.events(UUID(identifier), stream_request(client, identifier), "admin", manager)
        first = await anext(response.body_iterator)
        with manager.session_factory() as db:
            row = db.get(AssistantSession, identifier)
            updated_at = row.updated_at
            data = manager._decrypt(row)
            data[field] = value
            manager._encrypt(row, data)
            db.commit()
            assert row.updated_at == updated_at
        changed = await anext(response.body_iterator)
        assert changed.startswith("event: update\n") and changed != first
        public_field = "pending_approval" if field == "_pending_retry_approval" else field
        assert json.loads(changed.split("data: ", 1)[1])[public_field] == value
        if field == "_pending_retry_approval":
            with manager.session_factory() as db:
                row = db.get(AssistantSession, identifier)
                data = manager._decrypt(row)
                data.pop(field)
                manager._encrypt(row, data)
                db.commit()
            cleared = await anext(response.body_iterator)
            assert json.loads(cleared.split("data: ", 1)[1])[public_field] is None
        await response.body_iterator.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["revoked", "expired", "owner", "auth_owner"])
def test_stream_rechecks_auth_and_ownership_without_holding_db_between_polls(client, monkeypatch, change):
    manager = client.app.state.assistant_manager
    identifier = manager.create("admin", CreateSession())["id"]
    request = stream_request(client, identifier)
    factory = manager.session_factory
    active = []

    @contextmanager
    def tracked_factory():
        thread = threading.get_ident()
        with factory() as db:
            active.append(thread)
            try:
                yield db
                assert threading.get_ident() == thread
            finally:
                active.remove(thread)

    monkeypatch.setattr(manager, "session_factory", tracked_factory)

    async def run():
        response = await api.events(UUID(identifier), request, "admin", manager)
        assert not active and client.app.state.engine.pool.checkedout() == 0
        assert (await anext(response.body_iterator)).startswith("event: update\n")
        assert not active and client.app.state.engine.pool.checkedout() == 0
        with factory() as db:
            if change == "owner":
                db.get(AssistantSession, identifier).owner = "other"
            else:
                auth = db.scalar(select(AuthSession))
                if change == "revoked":
                    db.delete(auth)
                elif change == "expired":
                    auth.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                else:
                    auth.username = "other"
            db.commit()
        with pytest.raises(StopAsyncIteration):
            await anext(response.body_iterator)
        assert not active and client.app.state.engine.pool.checkedout() == 0

    asyncio.run(run())


def test_stream_emits_browser_expiry_without_database_changes(client, monkeypatch):
    manager = client.app.state.assistant_manager
    identifier = manager.create("admin", CreateSession())["id"]
    from app.services import assistant as service

    now = service.time.monotonic()
    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=lambda: now))
    manager.browsers[identifier] = BrowserScope("admin", identifier, "private-token", "interaction",
                                               "2030-01-01T00:00:00+00:00", now + 1)

    async def run():
        nonlocal now
        response = await api.events(UUID(identifier), stream_request(client, identifier), "admin", manager)
        first = json.loads((await anext(response.body_iterator)).split("data: ", 1)[1])
        assert first["browser"]["status"] == "ready"
        now += 2
        expired = json.loads((await anext(response.body_iterator)).split("data: ", 1)[1])
        assert expired["browser"]["status"] == "expired"
        await response.body_iterator.aclose()

    asyncio.run(run())
