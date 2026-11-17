from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError
from sqlalchemy import event, insert, select, update

from app.main import create_app
from app.models.assistant import AssistantMessage, AssistantSession
from app.models.auth import AuthSession
from app.models.integration import IntegrationConfig
from app.models.setting import AppSetting
from app.schemas.assistant_settings import (
    AssistantSettingsReset,
    AssistantSettingsUpdate,
    AssistantSettingsValues,
    AssistantSettingsView,
)
from app.services.assistant_settings import AssistantSettingsError, AssistantSettingsService, KEY
from app.services.integrations import IntegrationService
from tests.helpers import login_client, make_settings


ROOT = "/api/v1/assistant/settings"
PRIVATE = "private-api-key-never-echo-or-persist"
DEFAULTS = {
    "max_tool_calls": 20, "max_model_rounds": 12, "max_parallel_tool_calls": 4,
    "max_auth_uses_per_turn": 3,
    "turn_timeout_seconds": 180, "max_tokens": None, "context_max_tokens": 64000,
    "auto_compact": True, "auto_title": True,
    "auto_authorize_execution": False, "auto_authorize_screenshot": False,
    "auto_authorize_workflow": False,
}
PREVIOUS = {key: value for key, value in DEFAULTS.items() if key != "max_auth_uses_per_turn"}
LEGACY = {**{key: value for key, value in PREVIOUS.items()
             if key not in {"context_max_tokens", "auto_authorize_execution",
                            "auto_authorize_screenshot", "auto_authorize_workflow"}},
          "context_max_bytes": 200000}


@pytest.mark.parametrize("budget,expected", [(64000, 16000), (64001, 16001),
                                              (200000, 50000), (1000000, 250000)])
def test_exact_legacy_settings_migrate_readonly_then_persist_with_original_cas(client, budget, expected):
    raw = json.dumps({"revision": 7, "values": {**LEGACY, "context_max_bytes": budget}})
    with client.app.state.session_factory() as db:
        db.add(AppSetting(key=KEY, value_json=raw))
        db.commit()
    migrated = {**DEFAULTS, "context_max_tokens": expected}
    assert client.get(ROOT).json() == {"revision": 7, "source": "db", "values": migrated}
    with client.app.state.session_factory() as db:
        assert db.get(AppSetting, KEY).value_json == raw
    assert client.put(ROOT, json={"expected_revision": 6, "values": migrated}).status_code == 409
    response = client.put(ROOT, json={"expected_revision": 7, "values": migrated})
    assert response.json() == {"revision": 8, "source": "db", "values": migrated}
    with client.app.state.session_factory() as db:
        assert json.loads(db.get(AppSetting, KEY).value_json) == {"revision": 8, "values": migrated}


@pytest.mark.parametrize("old_values", [PREVIOUS, LEGACY])
@pytest.mark.parametrize("reset", [False, True])
def test_exact_old_settings_load_without_dml_then_update_or_reset(client, old_values, reset):
    raw = json.dumps({"revision": 7, "values": old_values}, indent=2)
    factory = client.app.state.session_factory
    with factory() as db:
        db.add(AppSetting(key=KEY, value_json=raw))
        db.commit()
        statements = []

        def capture(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement.lstrip().split()[0].upper())

        engine = db.get_bind()
        event.listen(engine, "before_cursor_execute", capture)
        try:
            view = AssistantSettingsService(db).view()
            loaded = AssistantSettingsService(db).load()
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert set(statements) == {"SELECT"}
        assert view.revision == 7 and view.source == "db"
        assert loaded.model_dump() == {
            **DEFAULTS, "context_max_tokens": 50000 if old_values == LEGACY else 64000,
        }
        assert db.get(AppSetting, KEY).value_json == raw
    if reset:
        response = client.post(ROOT + "/reset", json={"expected_revision": 7})
        expected = DEFAULTS
    else:
        expected = {**loaded.model_dump(), "max_auth_uses_per_turn": 20}
        response = client.put(ROOT, json={"expected_revision": 7, "values": expected})
    assert response.json() == {"revision": 8, "source": "db", "values": expected}
    with factory() as db:
        assert json.loads(db.get(AppSetting, KEY).value_json) == {"revision": 8, "values": expected}


@pytest.mark.parametrize("changes,removed", [
    ({"context_max_bytes": True}, None), ({"context_max_bytes": 63999}, None),
    ({"context_max_bytes": 1000001}, None), ({"auto_title": 1}, None),
    ({"auto_authorize_execution": False}, None), ({"extra": False}, None),
    ({"max_auth_uses_per_turn": 3}, None),
    ({}, "auto_title"), ({}, "max_tokens"),
])
def test_near_legacy_shape_fails_closed(client, changes, removed):
    values = {**LEGACY, **changes}
    if removed:
        values.pop(removed)
    raw = json.dumps({"revision": 7, "values": values})
    with client.app.state.session_factory() as db:
        db.add(AppSetting(key=KEY, value_json=raw))
        db.commit()
    assert client.get(ROOT).status_code == 503
    assert client.post(ROOT + "/reset", json={"expected_revision": 7}).status_code == 503
    with client.app.state.session_factory() as db:
        assert db.get(AppSetting, KEY).value_json == raw


def test_defaults_are_readonly_and_service_returns_snapshot(client):
    factory = client.app.state.session_factory
    with factory() as db:
        auth = db.scalar(select(AuthSession))
        auth.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        db.commit()
        last_seen = auth.last_seen_at.replace(tzinfo=None)
    expected = {"revision": 0, "source": "default", "values": DEFAULTS}
    for _ in range(2):
        response = client.get(ROOT)
        assert response.status_code == 200
        assert response.json() == expected
        assert response.headers["cache-control"] == "no-store"
    with factory() as db:
        service = AssistantSettingsService(db)
        assert isinstance(service.view(), AssistantSettingsView)
        assert service.view().model_dump() == expected
        snapshot = service.load()
        assert isinstance(snapshot, AssistantSettingsValues)
        snapshot.max_tool_calls = 99
        assert service.load().model_dump() == DEFAULTS
        assert db.get(AppSetting, KEY) is None
        assert db.scalar(select(AuthSession)).last_seen_at.replace(tzinfo=None) == last_seen


@pytest.mark.parametrize("method,path,payload", [
    ("get", ROOT, None),
    ("put", ROOT, {"expected_revision": 0, "values": DEFAULTS}),
    ("post", ROOT + "/reset", {"expected_revision": 0}),
])
def test_auth_required(anon_client, method, path, payload):
    response = getattr(anon_client, method)(path, **({"json": payload} if payload else {}))
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("method,path", [("put", ROOT), ("post", ROOT + "/reset")])
def test_csrf_required_without_auth_touch(client, method, path):
    factory = client.app.state.session_factory
    with factory() as db:
        auth = db.scalar(select(AuthSession))
        auth.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        db.commit()
        last_seen = auth.last_seen_at.replace(tzinfo=None)
    client.headers.pop("X-CSRF-Token")
    payload = {"expected_revision": 0}
    if method == "put":
        payload["values"] = DEFAULTS
    assert getattr(client, method)(path, json=payload).status_code == 403
    with factory() as db:
        assert db.get(AppSetting, KEY) is None
        assert db.scalar(select(AuthSession)).last_seen_at.replace(tzinfo=None) == last_seen


@pytest.mark.parametrize("field,low,high", [
    ("max_tool_calls", 1, 200), ("max_model_rounds", 1, 100),
    ("max_parallel_tool_calls", 1, 8), ("turn_timeout_seconds", 30, 1800),
    ("max_auth_uses_per_turn", 1, 20),
    ("max_tokens", 0, 9007199254740991), ("context_max_tokens", 16000, 1000000),
])
def test_integer_bounds_and_strict_types(field, low, high):
    for value in (low, high):
        assert getattr(AssistantSettingsValues(**{field: value}), field) == value
    for value in (low - 1, high + 1, True, False, str(low), float(low), [], {}):
        with pytest.raises(ValidationError):
            AssistantSettingsValues(**{field: value})
    if field != "max_tokens":
        with pytest.raises(ValidationError):
            AssistantSettingsValues(**{field: None})


@pytest.mark.parametrize("field", ["auto_compact", "auto_title", "auto_authorize_execution",
                                    "auto_authorize_screenshot", "auto_authorize_workflow"])
def test_booleans_are_strict(field):
    for value in (True, False):
        assert getattr(AssistantSettingsValues(**{field: value}), field) is value
    for value in (0, 1, "true", "false", None):
        with pytest.raises(ValidationError):
            AssistantSettingsValues(**{field: value})


@pytest.mark.parametrize("revision", [-1, True, False, "0", 0.0, None])
def test_revision_is_required_strict_nonnegative(revision):
    for schema, extra in ((AssistantSettingsReset, {}),
                          (AssistantSettingsUpdate, {"values": DEFAULTS})):
        with pytest.raises(ValidationError):
            schema(expected_revision=revision, **extra)
        with pytest.raises(ValidationError):
            schema(**extra)


@pytest.mark.parametrize("payload", [
    {"expected_revision": 0},
    {"values": DEFAULTS},
    {"expected_revision": 0, "values": {**DEFAULTS, "api_key": PRIVATE}},
    {"expected_revision": 0, "values": DEFAULTS, "api_key": PRIVATE},
    {"expected_revision": 0, "values": {**DEFAULTS, "model": PRIVATE}},
    {"expected_revision": 0, "values": {**DEFAULTS, "max_tool_calls": PRIVATE}},
    {"expected_revision": 0, "values": {**DEFAULTS, "auto_title": 1}},
    {"expected_revision": True, "values": DEFAULTS},
    *({"expected_revision": 0, "values": {**DEFAULTS, "max_auth_uses_per_turn": value}}
      for value in (None, True, False, 1.0, 1.5, 0, 21, "3", "1.5")),
])
def test_invalid_api_inputs_are_static_and_not_persisted(client, payload):
    response = client.put(ROOT, json=payload)
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "invalid_request"}}
    assert response.headers["cache-control"] == "no-store"
    assert PRIVATE not in response.text
    with client.app.state.session_factory() as db:
        assert db.get(AppSetting, KEY) is None


@pytest.mark.parametrize("tokens", [None, 0, 1, 9007199254740991])
def test_update_preserves_token_semantics_and_reopen_persistence(client, tokens):
    values = {**DEFAULTS, "max_tokens": tokens, "max_tool_calls": 27, "auto_title": False,
              "max_auth_uses_per_turn": 20}
    response = client.put(ROOT, json={"expected_revision": 0, "values": values})
    assert response.status_code == 200, response.text
    expected = {"revision": 1, "source": "db", "values": values}
    assert response.json() == expected
    with client.app.state.session_factory() as db:
        assert json.loads(db.get(AppSetting, KEY).value_json) == {"revision": 1, "values": values}
        assert AssistantSettingsService(db).load().max_tokens == tokens
    reopened = TestClient(create_app(client.app.state.settings))
    login_client(reopened)
    assert reopened.get(ROOT).json() == expected


def test_full_replacement_global_view_and_monotonic_reset(client):
    values = {**DEFAULTS, "auto_compact": False, "max_tokens": 0, "max_auth_uses_per_turn": 1}
    assert client.put(ROOT, json={"expected_revision": 0, "values": values}).json() == {
        "revision": 1, "source": "db", "values": values,
    }
    with client.app.state.session_factory() as db:
        db.scalar(select(AuthSession)).username = "another-owner"
        db.commit()
    assert client.get(ROOT).json()["values"] == values
    # An update is replacement, not a patch: unspecified value fields get schema defaults.
    response = client.put(ROOT, json={"expected_revision": 1, "values": {"auto_title": False}})
    assert response.json() == {
        "revision": 2, "source": "db", "values": {**DEFAULTS, "auto_title": False},
    }
    for stale in (0, 1):
        response = client.post(ROOT + "/reset", json={"expected_revision": stale})
        assert response.status_code == 409
        assert response.json() == {"detail": {"code": "assistant_settings_conflict"}}
    for expected in (2, 3):
        response = client.post(ROOT + "/reset", json={"expected_revision": expected})
        assert response.status_code == 200
        assert response.json() == {"revision": expected + 1, "source": "db", "values": DEFAULTS}
        assert client.get(ROOT).json() == response.json()
    response = client.put(ROOT, json={"expected_revision": 0, "values": values})
    assert response.status_code == 409


def test_reset_absent_settings_creates_revision_one(client):
    response = client.post(ROOT + "/reset", json={"expected_revision": 0})
    assert response.json() == {"revision": 1, "source": "db", "values": DEFAULTS}


@pytest.mark.parametrize("raw", [
    PRIVATE, "null", "[]", "{}", '{"revision":0}',
    json.dumps({"revision": 1, "values": {}}),
    json.dumps({"revision": True, "values": DEFAULTS}),
    json.dumps({"revision": -1, "values": DEFAULTS}),
    json.dumps({"revision": "1", "values": DEFAULTS}),
    json.dumps({"revision": 1, "values": {**DEFAULTS, "max_tool_calls": 201}}),
    json.dumps({"revision": 1, "values": {**DEFAULTS, "api_key": PRIVATE}}),
    json.dumps({"revision": 1, "values": {key: value for key, value in DEFAULTS.items()
                                          if key != "auto_authorize_execution"}}),
    *(json.dumps({"revision": 1, "values": {key: value for key, value in PREVIOUS.items()
                                            if key != missing}}) for missing in PREVIOUS),
    json.dumps({"revision": 1, "values": {**PREVIOUS, "unknown": False}}),
    json.dumps({"revision": 1, "values": {**PREVIOUS, "auto_title": 1}}),
    *(json.dumps({"revision": 1, "values": {**DEFAULTS, "max_auth_uses_per_turn": value}})
      for value in (None, True, False, 1.0, 1.5, 0, 21, "3")),
    json.dumps({"revision": 1, "values": DEFAULTS, "source": "default"}),
    '{"revision":1,"revision":2,"values":' + json.dumps(LEGACY) + '}',
])
def test_corrupt_settings_fail_closed_without_echo_or_overwrite(client, raw):
    factory = client.app.state.session_factory
    with factory() as db:
        db.add(AppSetting(key=KEY, value_json=raw))
        db.commit()
    for method, path, payload in (
        ("get", ROOT, None),
        ("put", ROOT, {"expected_revision": 0, "values": DEFAULTS}),
        ("post", ROOT + "/reset", {"expected_revision": 0}),
    ):
        response = getattr(client, method)(path, **({"json": payload} if payload else {}))
        assert response.status_code == 503
        assert response.json() == {"detail": {"code": "assistant_settings_invalid"}}
        assert response.headers["cache-control"] == "no-store"
        assert PRIVATE not in response.text
    with factory() as db:
        assert db.get(AppSetting, KEY).value_json == raw
        with pytest.raises(AssistantSettingsError) as error:
            AssistantSettingsService(db).load()
        assert (error.value.code, error.value.status) == ("assistant_settings_invalid", 503)


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("values", [DEFAULTS, PREVIOUS, LEGACY])
def test_atomic_cas_detects_interleaved_insert_or_raw_change(client, monkeypatch, existing, values):
    factory = client.app.state.session_factory
    initial = json.dumps({"revision": 1, "values": values})
    winning_values = {**DEFAULTS, "max_tool_calls": 77}
    # Same revision intentionally proves the UPDATE compares the entire previous raw value.
    winning_raw = json.dumps({"revision": 1, "values": winning_values})
    with factory() as db:
        if existing:
            db.add(AppSetting(key=KEY, value_json=initial))
            db.commit()
        service = AssistantSettingsService(db)
        read = service._read

        def interleaved():
            snapshot = read()
            with factory() as writer:
                if existing:
                    writer.execute(update(AppSetting).where(AppSetting.key == KEY).values(
                        value_json=winning_raw,
                    ))
                else:
                    writer.execute(insert(AppSetting).values(key=KEY, value_json=winning_raw))
                writer.commit()
            return snapshot

        monkeypatch.setattr(service, "_read", interleaved)
        with pytest.raises(AssistantSettingsError) as error:
            service.update(AssistantSettingsUpdate(
                expected_revision=int(existing), values=AssistantSettingsValues(max_tool_calls=99),
            ))
        assert (error.value.code, error.value.status) == ("assistant_settings_conflict", 409)
        assert not db.in_transaction()
        assert db.scalar(select(AppSetting.value_json).where(AppSetting.key == KEY)) == winning_raw


def test_stale_revision_rolls_back_and_leaves_encrypted_chat_unchanged(client):
    identifier = client.post("/api/v1/assistant/sessions", json={}).json()["id"]
    factory = client.app.state.session_factory
    with factory() as db:
        before_session = db.execute(select(AssistantSession.__table__)).all()
        before_messages = db.execute(select(AssistantMessage.__table__)).all()
        service = AssistantSettingsService(db)
        service.update(AssistantSettingsUpdate(expected_revision=0, values=AssistantSettingsValues()))
        chat = db.get(AssistantSession, identifier)
        chat.ciphertext = b"uncommitted-chat-change"
        db.add(AppSetting(key="unrelated-pending", value_json=PRIVATE))
        with pytest.raises(AssistantSettingsError) as error:
            service.update(AssistantSettingsUpdate(
                expected_revision=0, values=AssistantSettingsValues(auto_title=False),
            ))
        assert error.value.code == "assistant_settings_conflict"
        assert not db.in_transaction()
        assert service.view().revision == 1
        assert db.get(AppSetting, "unrelated-pending") is None
        assert db.execute(select(AssistantSession.__table__)).all() == before_session
        assert db.execute(select(AssistantMessage.__table__)).all() == before_messages
    response = client.put(ROOT, json={"expected_revision": 0, "values": DEFAULTS})
    assert response.status_code == 409
    assert response.json() == {"detail": {"code": "assistant_settings_conflict"}}
    assert response.headers["cache-control"] == "no-store"


def test_settings_never_probe_or_modify_global_model_configuration(tmp_path, monkeypatch):
    settings = make_settings(
        tmp_path, vision_base_url="https://provider.example/v1", vision_model="global-model",
        vision_api_key=PRIVATE, vision_max_tokens=1024, config_ai_enabled=False,
    )
    client = TestClient(create_app(settings))
    login_client(client)
    before = settings.model_dump()

    def forbidden(*args, **kwargs):
        pytest.fail("Assistant settings must not probe model/runtime/providers")

    monkeypatch.setattr(IntegrationService, "vision_runtime", forbidden)
    monkeypatch.setattr(IntegrationService, "model_runtime", forbidden)
    monkeypatch.setattr(client.app.state.assistant_manager, "_runtime", forbidden)
    with client.app.state.session_factory() as db:
        integrations = db.execute(select(IntegrationConfig.__table__)).all()
    assert client.get(ROOT).json()["values"] == DEFAULTS
    for revision, tokens in enumerate((None, 0, 123)):
        response = client.put(ROOT, json={
            "expected_revision": revision,
            "values": {**DEFAULTS, "max_tokens": tokens, "max_auth_uses_per_turn": 20},
        })
        assert response.status_code == 200
        assert response.json()["values"]["max_tokens"] == tokens
        assert response.json()["values"]["max_auth_uses_per_turn"] == 20
    assert client.post(ROOT + "/reset", json={"expected_revision": 3}).status_code == 200
    assert settings.model_dump() == before
    with client.app.state.session_factory() as db:
        assert db.execute(select(IntegrationConfig.__table__)).all() == integrations
        assert PRIVATE not in db.get(AppSetting, KEY).value_json
