import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.core.crypto import CryptoError
from app.models.auth import AuthSession
from app.models.integration import IntegrationConfig
from app.services.integrations import IntegrationService
from app.services.model_registry import ModelRegistryError, ModelRegistryService, REGISTRY_KEY
from tests.test_assistant_operations import tools  # noqa: F401

PATH = "/api/v1/integrations/models"
CONNECTION = {"base_url": "https://example.com/v1", "model": "secondary", "api_key": "private-secondary-key"}


def primary(**fields):
    return {"id": "primary", "name": "Primary", "enabled": True,
            "usages": ["feature_cards", "assistant"], **fields}


def extra(**fields):
    return {"id": str(uuid4()), "name": "Secondary", "enabled": True,
            "usages": ["feature_cards", "assistant"], "config": dict(CONNECTION), **fields}


def save(client, items, revision=0):
    return client.put(PATH, json={"expected_revision": revision, "items": items})


def registry(client, session):
    return ModelRegistryService(session, client.app.state.settings, client.app.state.crypto)


def test_absent_read_is_pure_and_preserves_partial_primary(client, monkeypatch):
    with client.app.state.session_factory() as session:
        registry(client, session).primary._upsert("vision.model", "partial", "test")
        session.commit()
        before = session.scalar(select(AuthSession)).last_seen_at
    def unexpected(*args, **kwargs):
        pytest.fail("no network on registry read or metadata save")
    monkeypatch.setattr(httpx.Client, "get", unexpected)
    # TestClient.get itself uses httpx.Client.get, so use request.
    response = client.request("GET", PATH)
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["revision"] == 0
    item, = view["items"]
    assert item["id"] == "primary" and item["name"] == "\u9ed8\u8ba4\u6a21\u578b"
    assert item["config"]["model"]["value"] == "partial"
    assert not item["config"]["available"]
    with client.app.state.session_factory() as session:
        assert session.get(IntegrationConfig, REGISTRY_KEY) is None
        assert session.scalar(select(AuthSession)).last_seen_at == before
    assert save(client, [primary()]).status_code == 200


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_first_legacy_mutation_invalidates_revision_zero(client, method):
    assert client.get(PATH).json()["revision"] == 0
    with client.app.state.session_factory() as session:
        assert session.get(IntegrationConfig, REGISTRY_KEY) is None
    response = client.request(method, "/api/v1/integrations/vision",
                              **({"json": {"model": "legacy"}} if method == "PUT" else {}))
    assert response.status_code == 200, response.text
    assert client.get(PATH).json()["revision"] == 1
    assert save(client, [primary()]).status_code == 409


def test_encrypted_document_aad_masking_and_partial_edits(client):
    second = extra()
    response = save(client, [second, primary(config={"api_key": "private-primary-key"})])
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == 1
    assert CONNECTION["api_key"] not in response.text
    config = response.json()["items"][0]["config"]
    assert config["api_key"]["masked"].startswith("****")
    assert config["max_tokens"]["source"] == "unset"
    assert config["available"]
    with client.app.state.session_factory() as session:
        service = registry(client, session)
        row = session.get(IntegrationConfig, REGISTRY_KEY)
        assert row.is_secret and row.updated_by == "admin"
        assert CONNECTION["api_key"].encode() not in row.value_ciphertext
        raw = service.crypto.decrypt(REGISTRY_KEY, row.value_ciphertext, row.nonce, row.key_version)
        doc = json.loads(raw)
        assert doc["version"] == 1 and doc["revision"] == 1
        assert not doc["items"][1].get("config")
        with pytest.raises(CryptoError):
            service.crypto.decrypt("vision.api_key", row.value_ciphertext, row.nonce, row.key_version)
        assert set(service.known_api_keys()) == {CONNECTION["api_key"], "private-primary-key"}
    second["config"] = {"max_tokens": None, "supports_vision": False}
    response = save(client, [primary(), second], 1)
    assert response.status_code == 200, response.text
    config = response.json()["items"][1]["config"]
    assert config["api_key"]["configured"] and config["available"]
    assert config["max_tokens"]["value"] == 0
    assert config["supports_vision"]["value"] is False
    assert save(client, [primary()], 2).json()["revision"] == 3


def test_order_scope_disabled_vision_and_no_provider_calls(client, monkeypatch):
    disabled = extra(enabled=False)
    reserved = extra(usages=["mcp"])
    text = extra(config={**CONNECTION, "model": "text", "supports_vision": False, "max_tokens": 0})
    image = extra(config={**CONNECTION, "model": "image", "max_tokens": 123})
    assert save(client, [disabled, reserved, text, image, primary()]).status_code == 200
    monkeypatch.setattr(httpx.Client, "post", lambda *a, **k: pytest.fail("selection must not call provider"))
    with client.app.state.session_factory() as session:
        service = registry(client, session)
        assert service.select_runtime("assistant")["model"] == "text"
        expected = {**CONNECTION, "model": "image", "supports_vision": True, "timeout": 30, "max_tokens": 123}
        assert service.select_runtime("feature_cards", requires_vision=True) == expected
        assert service.primary.model_runtime("assistant")["model"] == "text"
        assert service.primary.vision_runtime() is None
        assert service.select_runtime("mcp")["model"] == CONNECTION["model"]


def test_absent_selector_honors_mocked_primary_runtime(client, monkeypatch):
    expected = {**CONNECTION, "supports_vision": False, "timeout": 30}
    client.app.state.settings.vision_model = "configured"
    monkeypatch.setattr(IntegrationService, "vision_runtime", lambda self: expected)
    with client.app.state.session_factory() as session:
        service = registry(client, session)
        assert service.select_runtime("assistant") == expected
        assert service.select_runtime("feature_cards", requires_vision=True) is None


def test_primary_env_locks_whole_config_false_zero_metadata_editable(client):
    settings = client.app.state.settings
    settings.vision_supports_vision = False
    settings.vision_max_tokens = 0
    settings.vision_model = "env-model"
    response = save(client, [extra(), primary(enabled=False, usages=["mcp"])])
    assert response.status_code == 200, response.text
    config = response.json()["items"][1]["config"]
    assert config["model"]["source"] == "env" and not config["model"]["editable"]
    assert config["supports_vision"] == {"value": False, "source": "env", "editable": False}
    assert config["max_tokens"]["value"] == 0 and not config["max_tokens"]["editable"]
    assert all(not config[field]["editable"] for field in CONNECTION)
    for field, value in (("model", "new"), ("supports_vision", False), ("max_tokens", 0)):
        response = save(client, [primary(config={field: value})], 1)
        assert response.status_code == 409, response.text
    assert client.get(PATH).json()["revision"] == 1


def test_revision_cas_legacy_touch_and_legacy_exact_primary(client):
    second = extra()
    assert save(client, [second, primary(config={**CONNECTION, "model": "primary-model"})]).status_code == 200
    assert save(client, [primary()], 0).status_code == 409
    response = client.put("/api/v1/integrations/vision", json={"model": "legacy-model"})
    assert response.status_code == 200
    assert client.get(PATH).json()["revision"] == 2
    with client.app.state.session_factory() as session:
        service = registry(client, session)
        assert service.primary.vision_runtime()["model"] == "legacy-model"
        assert service.select_runtime("assistant")["model"] == "secondary"
    assert save(client, [primary()], 1).status_code == 409
    assert client.delete("/api/v1/integrations/vision").status_code == 200
    assert client.get(PATH).json()["revision"] == 3
    assert client.get(PATH).json()["items"][1]["config"]["available"] is False


def test_ciphertext_cas_snapshot_rejects_stale_write(client):
    assert save(client, [primary()]).status_code == 200
    with client.app.state.session_factory() as a, client.app.state.session_factory() as b:
        first, second = registry(client, a), registry(client, b)
        doc, snapshot = first._load()
        assert second.update({"expected_revision": 1, "items": [primary(name="changed")]}, "other").revision == 2
        with pytest.raises(ModelRegistryError) as error:
            first._save(doc, snapshot, "stale")
        assert error.value.status == 409
        a.rollback()
    assert client.get(PATH).json()["items"][0]["name"] == "changed"


@pytest.mark.parametrize("existing", [False, True])
def test_simultaneous_writers_only_one_commits(client, existing):
    revision = 0
    if existing:
        assert save(client, [primary()]).status_code == 200
        revision = 1
    barrier = Barrier(2)

    def write(name):
        with client.app.state.session_factory() as session:
            service = registry(client, session)
            barrier.wait(timeout=5)
            try:
                service.update({"expected_revision": revision, "items": [primary(
                    name=name, config={"model": name},
                )]}, name)
                return 200, name
            except ModelRegistryError as exc:
                return exc.status, name

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ["first", "second"]))
    assert sorted(status for status, _ in results) == [200, 409]
    winner = next(name for status, name in results if status == 200)
    view = client.get(PATH).json()
    assert view["revision"] == revision + 1
    assert view["items"][0]["name"] == winner
    assert view["items"][0]["config"]["model"]["value"] == winner


def test_valid_incomplete_saved_entries_skip_but_malformed_entries_fail_closed(client, monkeypatch):
    partial = extra(config={"model": "partial"})
    complete = extra()
    with client.app.state.session_factory() as session:
        service = registry(client, session)
        service.primary._upsert(REGISTRY_KEY, json.dumps({
            "version": 1, "revision": 1, "items": [partial, primary(), complete],
        }), "test")
        session.commit()
        assert service.select_runtime("assistant")["model"] == "secondary"
        assert not service.view().items[0].config.available
    monkeypatch.setattr(httpx.Client, "post", lambda *a, **k: pytest.fail("must not call provider"))
    response = client.request("POST", PATH + f'/{partial["id"]}/test')
    assert response.status_code == 200 and not response.json()["ok"]
    with client.app.state.session_factory() as session:
        service = registry(client, session)
        partial["config"] = {**CONNECTION, "supports_vision": "private-bad-field"}
        service.primary._upsert(REGISTRY_KEY, json.dumps({
            "version": 1, "revision": 1, "items": [partial, primary(), complete],
        }), "test")
        session.commit()
    response = client.request("POST", PATH + f'/{partial["id"]}/test')
    assert response.status_code == 503 and "private-bad-field" not in response.text


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(expected_revision=True),
    lambda p: p.update(extra="private-invalid-key"),
    lambda p: p.update(redaction_keys=["private-invalid-key"]),
    lambda p: p.update(items=[primary(), primary()]),
    lambda p: p.update(items=[primary()] + [extra() for _ in range(20)]),
    lambda p: p["items"][0].update(enabled=1),
    lambda p: p["items"][0].update(name="  "),
    lambda p: p["items"][0].update(usages=["unknown"]),
    lambda p: p["items"][0].update(usages=["assistant", "assistant"]),
    lambda p: p["items"][0].update(config={"api_key": " "}),
    lambda p: p["items"][0].update(config={"api_key": None}),
    lambda p: p["items"][0].update(config={"api_key": 123456789}),
    lambda p: p["items"][0].update(config={"api_key": "private-invalid-key", "max_tokens": True}),
    lambda p: p["items"][0].update(config={"supports_vision": "false"}),
    lambda p: p["items"][0].update(config={"timeout": 1}),
    lambda p: p["items"][0].update(config={"base_url": "https://user:private-invalid-key@example.com/v1"}),
    lambda p: p.update(items=[primary(), extra(id=str(uuid4()).upper())]),
    lambda p: p.update(items=[primary(), extra(config={"model": "incomplete"})]),
])
def test_invalid_request_is_static_and_atomic(client, mutation):
    payload = {"expected_revision": 0, "items": [primary()]}
    mutation(payload)
    response = client.put(PATH, json=payload)
    assert response.status_code == 422, response.text
    assert "private-invalid-key" not in response.text
    assert "123456789" not in response.text
    assert client.get(PATH).json()["revision"] == 0


def test_duplicate_request_keys_rejected(client):
    response = client.put(PATH, content='{"expected_revision":0,"expected_revision":0,"items":[]}',
                          headers={"Content-Type": "application/json"})
    assert response.status_code == 422


@pytest.mark.parametrize("raw", [
    '{"version":1,"version":1,"revision":0,"items":[]}',
    '{"version":2,"revision":0,"items":[]}',
    '{"version":true,"revision":0,"items":[]}',
    '{"version":1,"revision":false,"items":[]}',
    '{"version":1,"revision":0,"items":[],"future":"private-corrupt-key"}',
    'not-json-private-corrupt-key',
    json.dumps({"version": 1, "revision": 1, "items": [primary(), extra(config={"max_tokens": "private-corrupt-key"})]}),
    json.dumps({"version": 1, "revision": 1, "items": [primary(config=CONNECTION)]}),
    "bad-ciphertext",
])
def test_corrupt_registry_fails_closed(client, raw):
    with client.app.state.session_factory() as session:
        service = registry(client, session)
        service.primary._upsert(REGISTRY_KEY, raw, "test")
        session.commit()
        if raw == "bad-ciphertext":
            session.get(IntegrationConfig, REGISTRY_KEY).value_ciphertext = b"bad"
            session.commit()
        with pytest.raises(ModelRegistryError) as error:
            service.select_runtime("assistant")
        assert error.value.status == 503
    response = client.get(PATH)
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "model_registry_unavailable"}}
    assert save(client, [primary()]).status_code == 503
    assert client.put("/api/v1/integrations/vision", json={"model": "new"}).status_code == 503


def test_saved_entry_selftest_is_exact_even_disabled_and_provider_failure_no_fallback(client, monkeypatch):
    chosen = extra(enabled=False, usages=[], config={**CONNECTION, "supports_vision": False})
    assert save(client, [extra(), primary(), chosen]).status_code == 200
    calls = []
    async def send(self, request, **kwargs):
        calls.append(json.loads(request.content)["model"])
        raise RuntimeError("private-secondary-key provider body")
    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    response = client.request("POST", PATH + f'/{chosen["id"]}/test')
    assert response.status_code == 200 and response.json()["ok"] is False
    assert calls == ["secondary"]
    assert "private-secondary-key" not in response.text
    calls.clear()
    assert client.request("POST", PATH + "/primary/test").json()["ok"] is False
    assert calls == []
    assert client.request("POST", PATH + "/missing/test").status_code == 404


@pytest.mark.parametrize("field,value", [
    ("base_url", "https://user:private-corrupt-key@example.com/v1"),
    ("supports_vision", "not-a-bool"),
    ("max_tokens", "not-an-int"),
    ("model", "   "),
    ("api_key", "corrupt-ciphertext"),
])
def test_corrupt_legacy_primary_fields_are_static_registry_errors(client, field, value):
    with client.app.state.session_factory() as session:
        service = registry(client, session)
        service.primary._upsert(f"vision.{field}", value, "test")
        session.commit()
        if value == "corrupt-ciphertext":
            session.get(IntegrationConfig, f"vision.{field}").value_ciphertext = b"bad"
            session.commit()
        with pytest.raises(ModelRegistryError) as error:
            service.select_runtime("assistant")
        assert error.value.status == 503
    response = client.get(PATH)
    if field == "api_key":
        assert response.status_code == 503
        assert response.json() == {"detail": {"code": "model_registry_unavailable"}}
    else:
        assert response.status_code == 200, response.text
        config = response.json()["items"][0]["config"]
        assert not config["available"]
        assert config[field]["editable"]
        if field in {"max_tokens", "base_url"}:
            assert config[field]["error"] and config[field]["value"] is None
    assert "private-corrupt-key" not in response.text


def test_selftest_primary_and_secondary_use_exact_saved_configuration(client, monkeypatch):
    chosen = extra(enabled=False, config={**CONNECTION, "model": "chosen", "max_tokens": 77})
    assert save(client, [chosen, primary(config={**CONNECTION, "model": "primary-model"})]).status_code == 200
    calls = []

    def post(self, url, **kwargs):
        calls.append(kwargs["json"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.Client, "post", post)
    async def send(self, request, **kwargs):
        response = post(self, str(request.url), json=json.loads(request.content))
        return httpx.Response(response.status_code, stream=httpx.ByteStream(response.content), request=request)
    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    assert client.request("POST", PATH + f'/{chosen["id"]}/test').json()["ok"]
    assert client.request("POST", "/api/v1/integrations/vision/test").json()["ok"]
    assert [body["model"] for body in calls] == ["chosen", "primary-model"]
    assert calls[0]["max_tokens"] == 77
    assert "max_tokens" not in calls[1]


def test_login_csrf_and_no_store(client, anon_client):
    assert anon_client.get(PATH).status_code == 401
    assert anon_client.put(PATH, json={"expected_revision": 0, "items": [primary()]}).status_code == 401
    assert client.get(PATH).headers["Cache-Control"] == "no-store"
    client.headers.pop("X-CSRF-Token")
    assert save(client, [primary()]).status_code == 403
    assert client.request("POST", PATH + "/primary/test").status_code == 403


@pytest.mark.parametrize("field,bad,repair", [
    ("max_tokens", "invalid-private-cap", None),
    ("base_url", "https://user:private-invalid-url@example.com/v1", CONNECTION["base_url"]),
])
def test_management_repairs_primary_and_edits_secondary_while_primary_disabled(client, field, bad, repair):
    with client.app.state.session_factory() as session:
        service = registry(client, session)
        for name, value in CONNECTION.items():
            service.primary._upsert(f"vision.{name}", value, "test")
        session.flush()
        service.primary._upsert(f"vision.{field}", bad, "test")
        session.commit()
    response = client.get(PATH)
    assert response.status_code == 200
    assert bad not in response.text
    config = response.json()["items"][0]["config"]
    assert config[field]["error"] and config[field]["editable"]
    assert config[field]["source"] == "db" and not config["available"]
    second = extra()
    assert save(client, [second, primary(enabled=False)]).status_code == 200
    second["name"] = "Edited secondary"
    assert save(client, [second, primary(enabled=False)], 1).status_code == 200
    with client.app.state.session_factory() as session:
        assert registry(client, session).select_runtime("assistant")["model"] == CONNECTION["model"]
    response = save(client, [primary(config={field: repair}), second], 2)
    assert response.status_code == 200, response.text
    config = response.json()["items"][0]["config"]
    assert config["available"] and not config[field]["error"]
    with client.app.state.session_factory() as session:
        assert registry(client, session).select_runtime("assistant")["api_key"] == CONNECTION["api_key"]


@pytest.mark.parametrize("field,bad", [
    ("max_tokens", "invalid-private-cap"),
    ("base_url", "https://user:private-invalid-url@example.com/v1"),
])
@pytest.mark.parametrize("state", ["disabled", "wrong_usage", "eligible"])
@pytest.mark.parametrize("requires_vision", [False, True])
def test_primary_validation_only_after_enabled_and_usage_filters(client, field, bad, state, requires_vision):
    first = primary(enabled=state != "disabled",
                    usages=["mcp"] if state == "wrong_usage" else ["assistant"])
    assert save(client, [first, extra()]).status_code == 200
    with client.app.state.session_factory() as session:
        service = registry(client, session)
        service.primary._upsert(f"vision.{field}", bad, "test")
        session.commit()
        if state == "eligible":
            with pytest.raises(ModelRegistryError) as error:
                service.select_runtime("assistant", requires_vision=requires_vision)
            assert error.value.status == 503
        else:
            assert service.select_runtime("assistant", requires_vision=requires_vision) == {
                **CONNECTION, "supports_vision": True, "timeout": 30,
            }
        assert service.known_api_keys() == [CONNECTION["api_key"]]
    response = client.get(PATH)
    assert response.status_code == 200 and bad not in response.text
    config = response.json()["items"][0]["config"]
    assert config[field]["error"] and config[field]["editable"] and not config["available"]


@pytest.mark.parametrize("legacy", [False, True])
def test_primary_key_rotation_deletion_archive_is_encrypted_and_deduplicated(client, legacy):
    old, new = secrets.token_hex(24), secrets.token_hex(24)
    assert save(client, [primary(config={**CONNECTION, "api_key": old})]).status_code == 200
    if legacy:
        assert client.put("/api/v1/integrations/vision", json={"api_key": new}).status_code == 200
        assert client.delete("/api/v1/integrations/vision").status_code == 200
    else:
        assert save(client, [primary(config={"api_key": new})], 1).status_code == 200
        assert client.delete("/api/v1/integrations/vision").status_code == 200
    with client.app.state.session_factory() as session:
        service = registry(client, session)
        assert set(service.known_api_keys()) == {old, new}
        assert service.select_runtime("assistant") is None
        doc, _ = service._load()
        assert doc.redaction_keys == [old, new]
        assert session.get(IntegrationConfig, "vision.api_key") is None
        ciphertext = session.get(IntegrationConfig, REGISTRY_KEY).value_ciphertext
        assert old.encode() not in ciphertext and new.encode() not in ciphertext
    response = client.get(PATH)
    assert response.json()["revision"] == 3
    assert old not in response.text and new not in response.text
    assert "redaction_keys" not in response.text


@pytest.mark.parametrize("legacy", [False, True])
def test_locked_config_preserves_shadowed_db_and_effective_env_primary_keys(client, legacy):
    from pydantic import SecretStr

    db_key, env_key = secrets.token_hex(24), secrets.token_hex(24)
    with client.app.state.session_factory() as session:
        registry(client, session).primary._upsert("vision.api_key", db_key, "test")
        session.commit()
    client.app.state.settings.vision_api_key = SecretStr(env_key)
    response = (client.put("/api/v1/integrations/vision", json={"model": "changed"}) if legacy
                else save(client, [primary(config={"model": "changed"})]))
    assert response.status_code == 409
    with client.app.state.session_factory() as session:
        service = registry(client, session)
        assert service._load()[0].redaction_keys == []
        assert set(service.known_api_keys()) == {db_key, env_key}
        assert service.primary._decrypt_row("vision.api_key", service.primary._row("vision.api_key")) == db_key


def test_prearchive_document_is_read_without_migration_writes(client):
    raw = json.dumps({"version": 1, "revision": 7, "items": [primary()]})
    with client.app.state.session_factory() as session:
        service = registry(client, session)
        service.primary._upsert(REGISTRY_KEY, raw, "test")
        session.commit()
        before = session.get(IntegrationConfig, REGISTRY_KEY).value_ciphertext
    response = client.get(PATH)
    assert response.status_code == 200 and response.json()["revision"] == 7
    with client.app.state.session_factory() as session:
        service = registry(client, session)
        assert service.known_api_keys() == []
        assert service._load()[0].redaction_keys == []
        assert session.get(IntegrationConfig, REGISTRY_KEY).value_ciphertext == before


@pytest.mark.parametrize("archive", [None, "secret", [None], [True], [" "], ["a", "a"], ["a" * 4097]])
def test_corrupt_archive_fails_closed_for_every_consumer(client, archive):
    with client.app.state.session_factory() as session:
        service = registry(client, session)
        service.primary._upsert(REGISTRY_KEY, json.dumps({
            "version": 1, "revision": 1, "items": [primary()], "redaction_keys": archive,
        }), "test")
        session.commit()
        for read in (service.view, service.known_api_keys, lambda: service.select_runtime("assistant")):
            with pytest.raises(ModelRegistryError) as error:
                read()
            assert error.value.status == 503
    assert save(client, [primary()], 1).status_code == 503


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("capacity", ["count", "bytes"])
def test_archive_capacity_never_drops_keys_and_rolls_back_primary_and_revision(client, legacy, capacity):
    old = secrets.token_hex(24)
    assert save(client, [primary(config={**CONNECTION, "api_key": old})]).status_code == 200
    with client.app.state.session_factory() as session:
        service = registry(client, session)
        doc, _ = service._load()
        if capacity == "count":
            doc.redaction_keys = [f"retired-{index}" for index in range(512)]
        else:
            doc.redaction_keys = [str(index) + "x" * 4090 for index in range(63)]
        service.primary._upsert(REGISTRY_KEY, json.dumps(doc.model_dump(exclude_unset=True)), "test")
        session.commit()
        before = session.get(IntegrationConfig, REGISTRY_KEY).value_ciphertext
    # The byte case starts below 256 KiB but cannot fit a second long active key plus the archive.
    new = "n" * 4096 if capacity == "bytes" else "new-key"
    if legacy:
        # Legacy writes archive the old effective key before mutation.
        if capacity == "bytes":
            with client.app.state.session_factory() as session:
                registry(client, session).primary._upsert("vision.api_key", "o" * 4096, "test")
                session.commit()
                old = "o" * 4096
        response = client.put("/api/v1/integrations/vision", json={"api_key": new})
    else:
        if capacity == "bytes":
            with client.app.state.session_factory() as session:
                registry(client, session).primary._upsert("vision.api_key", "o" * 4096, "test")
                session.commit()
                old = "o" * 4096
        response = save(client, [primary(config={"api_key": new})], 1)
    assert response.status_code == 422, response.text
    assert response.json() == {"detail": {"code": "model_registry_limit"}}
    with client.app.state.session_factory() as session:
        service = registry(client, session)
        assert service._load()[0].revision == 1
        assert session.get(IntegrationConfig, REGISTRY_KEY).value_ciphertext == before
        assert service.primary.resolve_vision("api_key").value == old


def test_primary_locked_write_rolls_back_retired_secondary_archive(client):
    second = extra()
    assert save(client, [primary(), second]).status_code == 200
    client.app.state.settings.vision_model = "env-locked"
    response = save(client, [primary(config={"model": "blocked"})], 1)
    assert response.status_code == 409
    with client.app.state.session_factory() as session:
        doc, _ = registry(client, session)._load()
        assert doc.revision == 1 and doc.redaction_keys == []
        assert len(doc.items) == 2


def test_retired_secondary_keys_redact_real_artifact_after_rotation_deletion_and_restart(tools):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from tests.test_assistant_operations import artifact, call

    manager = tools.manager
    old, new = secrets.token_hex(24), secrets.token_hex(24)
    second = extra(config={**CONNECTION, "api_key": old})
    identifier = artifact(tools, f"Useful historical diagnostic {old} {new}".encode())
    with manager.session_factory() as session:
        service = ModelRegistryService(session, manager.settings, manager.crypto)
        service.update({"expected_revision": 0, "items": [primary(enabled=False), second]}, "test")
        second["config"] = {"api_key": new}
        service.update({"expected_revision": 1, "items": [primary(enabled=False), second]}, "test")
        service.update({"expected_revision": 2, "items": [primary(enabled=False)]}, "test")
    engine = create_engine(manager.settings.database_url)
    manager.session_factory = sessionmaker(engine, autoflush=False)
    try:
        result = call(tools, "read_artifact_log", {"artifact_id": identifier})
        assert result["ok"] and "Useful historical diagnostic" in result["content"]
        assert old not in json.dumps(result) and new not in json.dumps(result)
        assert result["content"].count("[redacted]") == 2
        with manager.session_factory() as session:
            service = ModelRegistryService(session, manager.settings, manager.crypto)
            assert service.select_runtime("assistant") is None
            public = service.view().model_dump_json()
            assert old not in public and new not in public and "redaction_keys" not in public
            assert set(service.known_api_keys()) == {old, new}
    finally:
        engine.dispose()


@pytest.mark.parametrize("delete", [False, True])
def test_legacy_interleaved_primary_rotation_archives_fresh_key_despite_cached_identity(tools, delete):
    from tests.test_assistant_operations import artifact, call

    manager = tools.manager
    old, intermediate, final = [secrets.token_hex(24) for _ in range(3)]
    identifier = artifact(tools, f"Historical diagnostic {old} {intermediate} {final}".encode())
    with manager.session_factory() as stale:
        first = ModelRegistryService(stale, manager.settings, manager.crypto)
        first.primary.update_vision(**{**CONNECTION, "api_key": old})
        # Keep the old ORM identity alive across another session's committed rotation.
        cached_key = stale.get(IntegrationConfig, "vision.api_key")
        cached_model = stale.get(IntegrationConfig, "vision.model")
        assert first.primary.resolve_vision("api_key").value == old
        with manager.session_factory() as other:
            ModelRegistryService(other, manager.settings, manager.crypto).primary.update_vision(
                api_key=intermediate, model="intermediate-model",
            )
        assert manager.crypto.decrypt("vision.api_key", cached_key.value_ciphertext,
                                      cached_key.nonce, cached_key.key_version) == old
        assert first._primary_api_keys() == [intermediate]
        assert first.primary.resolve_vision("model").value == CONNECTION["model"]
        if delete:
            first.primary.delete_vision()
        else:
            first.primary.update_vision(api_key=final)
        assert {old, intermediate} <= set(first.known_api_keys())
        if not delete:
            assert first.primary.vision_runtime()["api_key"] == final
            assert first.primary.vision_runtime()["model"] == "intermediate-model"
            assert first.primary._row("vision.model") is cached_model
        else:
            assert first.primary.vision_runtime() is None
        doc, _ = first._load()
        assert doc.revision == 3 and set(doc.redaction_keys) == {old, intermediate}
    result = call(tools, "read_artifact_log", {"artifact_id": identifier})
    assert result["ok"] and "Historical diagnostic" in result["content"]
    assert old not in json.dumps(result) and intermediate not in json.dumps(result)
    if not delete:
        assert final not in json.dumps(result)
        assert result["content"].count("[redacted]") == 3


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", ["invalid", "locked", "delete_locked"])
def test_legacy_validation_failure_rolls_back_lock_revision_and_archive(client, existing, failure):
    from app.services.integrations import ConfigLockedError, IntegrationValidationError

    if existing:
        assert save(client, [primary(config=CONNECTION)]).status_code == 200
    if failure != "invalid":
        client.app.state.settings.vision_model = "env-locked"
    with client.app.state.session_factory() as session:
        service = registry(client, session)
        before = session.get(IntegrationConfig, REGISTRY_KEY)
        ciphertext = before.value_ciphertext if before else None
        with pytest.raises(IntegrationValidationError if failure == "invalid" else ConfigLockedError):
            if failure == "delete_locked":
                service.primary.delete_vision()
            else:
                service.primary.update_vision(**({"max_tokens": -1} if failure == "invalid"
                                                else {"model": "blocked"}))
        assert not session.in_transaction()
        row = session.get(IntegrationConfig, REGISTRY_KEY)
        assert (row.value_ciphertext if row else None) == ciphertext
        assert service._load()[0].redaction_keys == []
