from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import socket
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, event, insert, select
from sqlalchemy.orm import sessionmaker

from app.core.crypto import CryptoService
from app.core.db import Base
from app.models.integration import IntegrationConfig
from app.services.assistant_data import AssistantData
from app.services.bitwarden import BitwardenService, clear_health_cache
from app.services.lanes import LaneService


PRIVATE = "never-emit-bitwarden-private"
ENDPOINT = f"http://{PRIVATE}.invalid:8090"


@pytest.fixture
def status(settings, monkeypatch):
    engine = create_engine(settings.database_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, autoflush=False)
    crypto = CryptoService(b"b" * 32)
    manager = SimpleNamespace(
        session_factory=factory, crypto=crypto,
        settings=settings.model_copy(update={"bitwarden_auto_detect": True}),
    )
    tool = AssistantData(manager)
    clear_health_cache()

    def respond(request, **kwargs):
        assert request.method == "GET" and request.url.path in {"/health", "/env/health"}
        assert "authorization" not in request.headers
        return httpx.Response(200, request=request, json={
            "status": "ok", "configured": True, "ready": True,
            "unlocked": True, "version": PRIVATE, "user_email": f"{PRIVATE}@example.com",
            "error": PRIVATE, "password": PRIVATE, "token": PRIVATE,
        })

    send = Mock(side_effect=respond)
    monkeypatch.setattr(httpx.Client, "send", send)
    monkeypatch.setattr(httpx.AsyncClient, "send", AsyncMock(side_effect=AssertionError("No HTTP")))
    monkeypatch.setattr(socket, "getaddrinfo", Mock(side_effect=AssertionError("No DNS")))
    monkeypatch.setattr(BitwardenService, "lookup", Mock(side_effect=AssertionError("No lookup")))
    monkeypatch.setattr(BitwardenService, "test", Mock(side_effect=AssertionError("No deep test")))
    monkeypatch.setattr(LaneService, "get_lanes", AsyncMock(return_value=SimpleNamespace(
        worker_connected=False, lanes=[],
    )))

    def seed(**values):
        with engine.begin() as db:
            for field, value in values.items():
                key = f"bitwarden.{field}"
                encrypted = crypto.encrypt(key, value)
                db.execute(insert(IntegrationConfig).values(
                    key=key, value_ciphertext=encrypted.ciphertext, nonce=encrypted.nonce,
                    key_version=encrypted.key_version, fingerprint=PRIVATE,
                ))

    statements = []

    def readonly(conn, cursor, statement, parameters, context, executemany):
        assert statement.lstrip().upper().startswith("SELECT")
        assert not (context.isinsert or context.isupdate or context.isdelete)
        assert "site_credentials" not in statement.lower()
        statements.append(statement)

    def call():
        event.listen(engine, "before_cursor_execute", readonly)
        try:
            return asyncio.run(tool.dispatch("get_system_status", {}))
        finally:
            event.remove(engine, "before_cursor_execute", readonly)

    def item():
        result = call()
        assert result["ok"], result
        assert result["health_checked"] is False
        assert result["health_scope"] == ["bitwarden"]
        return next(item for item in result["integrations"] if item["name"] == "bitwarden")

    yield SimpleNamespace(tool=tool, manager=manager, factory=factory, engine=engine,
                          seed=seed, call=call, item=item, send=send, statements=statements)
    clear_health_cache()
    engine.dispose()


def test_auto_detect_reports_real_readiness_without_optional_token(status, caplog):
    assert status.item() == {
        "name": "bitwarden", "enabled": True, "enabled_setting": True,
        "available": True, "configured": True, "auto_detected": True,
        "source": "auto", "enabled_source": "auto", "url": {"source": "auto"},
        "token": {"configured": False, "source": "unset"},
        "sources": {"url": "auto", "token": "unset", "enabled": "auto"},
        "health_checked": True,
    }
    assert status.send.call_count == 1
    output = json.dumps(status.call())
    for forbidden in (PRIVATE, "http://", "https://", "masked", "fingerprint",
                      "user_email", "server_version", "error", "password", "default_url"):
        assert forbidden not in output
    assert PRIVATE not in caplog.text


@pytest.mark.parametrize("source", ["env", "db", "env_over_db"])
def test_explicit_configuration_sources_and_precedence(status, source, caplog):
    if source != "env":
        status.seed(url=ENDPOINT, token=PRIVATE, enabled="true")
    if source != "db":
        status.manager.settings = status.manager.settings.model_copy(update={
            "bitwarden_url": ENDPOINT + "/env", "bitwarden_token": SecretStr(PRIVATE + "-env"),
            "bitwarden_enabled": True,
        })
    expected = "db" if source == "db" else "env"
    item = status.item()
    assert item["enabled"] and item["available"] and item["configured"]
    assert item["enabled_setting"] and not item["auto_detected"]
    assert item["source"] == item["enabled_source"] == expected
    assert item["url"] == {"source": expected}
    assert item["token"] == {"configured": True, "source": expected}
    assert item["sources"] == dict.fromkeys(("url", "token", "enabled"), expected)
    request = status.send.call_args.args[0]
    assert str(request.url) == ENDPOINT + ("/health" if source == "db" else "/env/health")
    assert PRIVATE not in json.dumps(item) and PRIVATE not in caplog.text


@pytest.mark.parametrize("source", ["env", "db"])
def test_explicit_disable_respected_without_probe(status, source):
    if source == "db":
        status.seed(enabled="false")
    else:
        status.manager.settings = status.manager.settings.model_copy(update={
            "bitwarden_enabled": False,
        })
    item = status.item()
    assert item["enabled_source"] == source
    assert item["sources"] == {"url": "auto", "token": "unset", "enabled": source}
    assert item["enabled_setting"] is item["enabled"] is False
    assert item["available"] is item["configured"] is False
    assert item["health_checked"] is True
    status.send.assert_not_called()


@pytest.mark.parametrize("explicit", [False, True])
def test_wrong_url_unavailable_does_not_rewrite_explicit_enable(status, explicit, caplog):
    status.manager.settings = status.manager.settings.model_copy(update={
        "bitwarden_url": ENDPOINT, "bitwarden_enabled": True if explicit else None,
    })
    status.send.side_effect = RuntimeError(f"{ENDPOINT} {PRIVATE}")
    item = status.item()
    assert item["available"] is item["configured"] is item["enabled"] is False
    assert item["enabled_setting"] is explicit
    assert item["enabled_source"] == ("env" if explicit else "auto")
    assert PRIVATE not in json.dumps(item) and PRIVATE not in caplog.text


def test_connected_but_not_ready_is_not_configured_or_enabled(status, caplog):
    status.send.side_effect = lambda request, **kwargs: httpx.Response(200, request=request, json={
        "status": "ok", "configured": True, "ready": False,
        "error": f"{PRIVATE} {ENDPOINT}", "user_email": PRIVATE,
    })
    item = status.item()
    assert item["available"] is True
    assert item["configured"] is item["enabled"] is item["enabled_setting"] is False
    assert PRIVATE not in json.dumps(item) and PRIVATE not in caplog.text


@pytest.mark.parametrize("settings_first", [False, True])
def test_settings_and_assistant_share_shallow_health_cache(status, settings_first):
    def settings_view():
        with status.factory() as db:
            return BitwardenService(db, status.manager.settings, status.manager.crypto).view()

    first = settings_view() if settings_first else status.item()
    # A fresh probe would now fail; both consumers must retain the cached result.
    status.send.side_effect = AssertionError("Must use shared cache")
    second = status.item() if settings_first else settings_view()
    view, item = (first, second) if settings_first else (second, first)
    for field in ("enabled", "enabled_setting", "available", "configured", "auto_detected"):
        assert item[field] is getattr(view, field)
    assert item["source"] == view.source.value
    assert status.send.call_count == 1


def test_view_uses_worker_owned_session_and_manager_dependencies(status, monkeypatch):
    main_thread = threading.get_ident()
    sessions, exits = [], []

    @contextmanager
    def tracked_factory():
        with status.factory() as db:
            sessions.append((db, threading.get_ident()))
            yield db
        exits.append((db, threading.get_ident()))

    original = BitwardenService.view

    def view(service, *, force, deep):
        assert force is deep is False
        assert threading.get_ident() != main_thread
        assert service.session is sessions[1][0]
        assert service.settings is status.manager.settings
        assert service.crypto is status.manager.crypto
        assert exits == [sessions[0]]
        return original(service, force=force, deep=deep)

    monkeypatch.setattr(status.manager, "session_factory", tracked_factory)
    monkeypatch.setattr(BitwardenService, "view", view)
    assert status.item()["enabled"]
    assert len(sessions) == 2 and sessions[0][0] is not sessions[1][0]
    assert sessions[0][1] == main_thread and sessions[1][1] != main_thread
    assert exits == sessions


def test_status_only_reads_config_without_commits_or_credential_lookups(status, monkeypatch):
    status.seed(url=ENDPOINT, token=PRIVATE, enabled="true")
    with status.engine.connect() as db:
        before = db.execute(select(IntegrationConfig.__table__)).all()
    decrypt = Mock(wraps=status.manager.crypto.decrypt)
    monkeypatch.setattr(status.manager.crypto, "decrypt", decrypt)
    monkeypatch.setattr(status.factory.class_, "commit", Mock(side_effect=AssertionError("No commit")))
    assert status.item()["enabled"]
    assert {call.args[0] for call in decrypt.call_args_list} == {
        "bitwarden.url", "bitwarden.token", "bitwarden.enabled",
    }
    assert status.statements
    BitwardenService.lookup.assert_not_called()
    BitwardenService.test.assert_not_called()
    with status.engine.connect() as db:
        assert db.execute(select(IntegrationConfig.__table__)).all() == before


def test_view_failure_is_static_not_fake_disabled_health(status, monkeypatch, caplog):
    monkeypatch.setattr(BitwardenService, "view", Mock(side_effect=RuntimeError(
        f"{PRIVATE} {ENDPOINT}",
    )))
    assert status.call() == {"ok": False, "code": "data_unavailable"}
    assert PRIVATE not in caplog.text
