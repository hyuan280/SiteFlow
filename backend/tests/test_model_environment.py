import json

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.models.integration import IntegrationConfig
from app.schemas.model_registry import RegistryDocument, RegistryUpdate, RegistryView
from app.services.integrations import MODEL_CONFIG_FIELDS
from app.services.model_registry import ModelRegistryError
from tests.test_model_draft_test import readonly, snapshot, upstream  # noqa: F401
from tests.test_model_registry import CONNECTION, PATH, extra, primary, registry, save


def test_new_environment_prefix_only(monkeypatch):
    values = {"BASE_URL": CONNECTION["base_url"], "API_KEY": CONNECTION["api_key"],
              "ID": CONNECTION["model"], "MAX_TOKENS": "0",
              "SUPPORTS_VISION": "false", "TIMEOUT": "42"}
    for suffix, value in values.items():
        monkeypatch.delenv("SITEFLOW_MODEL_" + suffix, raising=False)
        monkeypatch.setenv("SITEFLOW_VISION_" + suffix, value)
    monkeypatch.setenv("SITEFLOW_VISION_MODEL", CONNECTION["model"])
    monkeypatch.setenv("SITEFLOW_MODEL_MODEL", CONNECTION["model"])
    old = Settings(_env_file=None)
    assert all(getattr(old, "vision_" + field) is None for field in MODEL_CONFIG_FIELDS)
    assert old.vision_timeout == 30
    for suffix, value in values.items():
        monkeypatch.setenv("SITEFLOW_MODEL_" + suffix, value)
    settings = Settings(_env_file=None)
    assert settings.vision_base_url == CONNECTION["base_url"]
    assert settings.vision_api_key.get_secret_value() == CONNECTION["api_key"]
    assert settings.vision_model == CONNECTION["model"]
    assert settings.vision_max_tokens == 0
    assert settings.vision_supports_vision is False
    assert settings.vision_timeout == 42


@pytest.mark.parametrize("suffix,value", [
    ("MAX_TOKENS", "-1"), ("MAX_TOKENS", "1.5"), ("MAX_TOKENS", "true"),
    ("SUPPORTS_VISION", "invalid"), ("TIMEOUT", "0"), ("TIMEOUT", "301"),
])
def test_environment_validation(monkeypatch, suffix, value):
    monkeypatch.setenv("SITEFLOW_MODEL_" + suffix, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_empty_initial_registry_and_timeout_alone_are_readonly(client):
    client.app.state.settings.vision_timeout = 42
    before = snapshot(client)
    with readonly(client):
        assert client.get(PATH).json() == {"revision": 0, "items": []}
        with client.app.state.session_factory() as session:
            assert registry(client, session).select_runtime("assistant") is None
    assert snapshot(client) == before
    assert save(client, []).json() == {"revision": 1, "items": []}
    assert client.get(PATH).json() == {"revision": 1, "items": []}


def test_schema_accepts_ordinary_lists_but_rejects_duplicate_ids():
    for items in ([], [extra()], [primary()], [extra(), primary()]):
        RegistryUpdate(expected_revision=0, items=items)
        RegistryDocument(version=1, revision=0, items=items)
    for schema, fields in ((RegistryUpdate, {"expected_revision": 0}),
                           (RegistryDocument, {"version": 1, "revision": 0})):
        with pytest.raises(ValidationError):
            schema(**fields, items=[primary(), primary()])


def test_single_legacy_db_row_migrates_without_write_and_is_ordinary(client):
    with client.app.state.session_factory() as session:
        service = registry(client, session)
        for field, value in CONNECTION.items():
            service.primary._upsert("vision." + field, value, "test")
        session.commit()
    before = snapshot(client)
    with readonly(client):
        item, = client.get(PATH).json()["items"]
        assert item["name"] == "\u9ed8\u8ba4\u6a21\u578b"
        assert item["source"] == "db" and item["config_editable"]
        assert all(item["config"][field]["editable"] for field in MODEL_CONFIG_FIELDS)
        with client.app.state.session_factory() as session:
            assert registry(client, session).select_runtime("assistant")["model"] == CONNECTION["model"]
    assert snapshot(client) == before
    second = extra(config={**CONNECTION, "model": "first-in-list"})
    assert save(client, [second, primary()]).status_code == 200
    with client.app.state.session_factory() as session:
        assert registry(client, session).select_runtime("assistant")["model"] == "first-in-list"


def test_remove_legacy_row_deletes_config_and_archives_key_without_resurrection(client):
    assert client.put("/api/v1/integrations/vision", json={
        **CONNECTION, "supports_vision": False, "max_tokens": 0,
    }).status_code == 200
    assert save(client, [], 1).json() == {"revision": 2, "items": []}
    for _ in range(2):
        assert client.get(PATH).json() == {"revision": 2, "items": []}
        with client.app.state.session_factory() as session:
            service = registry(client, session)
            assert all(session.get(IntegrationConfig, "vision." + field) is None
                       for field in MODEL_CONFIG_FIELDS)
            assert service.select_runtime("assistant") is None
            assert service.known_api_keys() == [CONNECTION["api_key"]]
            row = session.get(IntegrationConfig, "models.registry")
            raw = json.loads(service.crypto.decrypt(row.key, row.value_ciphertext, row.nonce, row.key_version))
            assert raw["items"] == [] and raw["redaction_keys"] == [CONNECTION["api_key"]]
            assert CONNECTION["api_key"].encode() not in row.value_ciphertext
    # A compatibility clear must not create metadata; an explicit legacy PUT may.
    assert client.delete("/api/v1/integrations/vision").status_code == 200
    assert client.get(PATH).json() == {"revision": 3, "items": []}
    assert client.put("/api/v1/integrations/vision", json=CONNECTION).status_code == 200
    assert client.get(PATH).json()["items"][0]["id"] == "primary"


@pytest.mark.parametrize("full_archive", [False, True])
def test_remove_legacy_row_preserves_other_metadata_and_is_atomic(client, full_archive):
    second = extra(enabled=False, usages=["mcp"])
    assert save(client, [second, primary(config={
        **CONNECTION, "supports_vision": False, "max_tokens": 0,
    })]).status_code == 200
    if full_archive:
        with client.app.state.session_factory() as session:
            service = registry(client, session)
            doc, _ = service._load()
            doc.redaction_keys = [f"retired-{index}" for index in range(512)]
            service.primary._upsert("models.registry", json.dumps(doc.model_dump(exclude_unset=True)), "test")
            session.commit()
    before = snapshot(client)
    response = save(client, [second], 1)
    if full_archive:
        assert response.status_code == 422
        assert response.json() == {"detail": {"code": "model_registry_limit"}}
        assert snapshot(client) == before
    else:
        assert response.status_code == 200
        item, = response.json()["items"]
        assert item["id"] == second["id"] and not item["enabled"] and item["usages"] == ["mcp"]
        with client.app.state.session_factory() as session:
            service = registry(client, session)
            assert all(session.get(IntegrationConfig, "vision." + field) is None
                       for field in MODEL_CONFIG_FIELDS)
            assert set(service.known_api_keys()) == {CONNECTION["api_key"]}


@pytest.mark.parametrize("field,value", [
    ("base_url", CONNECTION["base_url"]), ("model", CONNECTION["model"]),
    ("api_key", SecretStr(CONNECTION["api_key"])),
    ("supports_vision", False), ("max_tokens", 0),
])
def test_any_env_field_locks_entire_row_and_legacy_mutations(client, field, value):
    second = extra()
    assert save(client, [second]).status_code == 200
    setattr(client.app.state.settings, "vision_" + field, value)
    before = snapshot(client)
    with readonly(client):
        view = client.get(PATH).json()
        db, env = view["items"]
        assert db["source"] == "db" and db["config_editable"]
        assert env["id"] == "primary" and env["name"] == "\u73af\u5883\u53d8\u91cf\u6a21\u578b"
        assert env["source"] == "env" and not env["config_editable"]
        assert all(not env["config"][name]["editable"] for name in MODEL_CONFIG_FIELDS)
        assert env["config"][field]["source"] == "env"
    assert snapshot(client) == before
    for config in ({"model": CONNECTION["model"]}, {"api_key": CONNECTION["api_key"]},
                   {"base_url": CONNECTION["base_url"]}, {"supports_vision": False}, {"max_tokens": 0}):
        response = save(client, [second, primary(config=config)], 1)
        assert response.status_code == 409
        assert response.json() == {"detail": {"code": "model_config_locked"}}
        assert client.put("/api/v1/integrations/vision", json=config).status_code == 409
    for items in ([], [second]):
        assert save(client, items, 1).status_code == 409
    assert client.delete("/api/v1/integrations/vision").status_code == 409
    assert snapshot(client) == before
    response = save(client, [primary(enabled=False, usages=["mcp"], name="Metadata"), second], 1)
    assert response.status_code == 200
    assert response.json()["items"][0]["name"] == "Metadata"
    with client.app.state.session_factory() as session:
        assert registry(client, session).select_runtime("assistant")["model"] == CONNECTION["model"]
        row = session.get(IntegrationConfig, "models.registry")
        raw = json.loads(
            client.app.state.crypto.decrypt(row.key, row.value_ciphertext, row.nonce, row.key_version))
        assert all("source" not in item and "config_editable" not in item for item in raw["items"])


def test_env_injected_into_explicit_empty_registry_for_view_and_selection(client):
    assert save(client, []).status_code == 200
    settings = client.app.state.settings
    for field, value in CONNECTION.items():
        setattr(settings, "vision_" + field, SecretStr(value) if field == "api_key" else value)
    before = snapshot(client)
    with readonly(client):
        assert client.get(PATH).json()["items"][0]["source"] == "env"
        with client.app.state.session_factory() as session:
            assert registry(client, session).select_runtime("assistant")["api_key"] == CONNECTION["api_key"]
    assert snapshot(client) == before
    settings.vision_model = settings.vision_api_key = settings.vision_base_url = None
    assert client.get(PATH).json() == {"revision": 1, "items": []}


def test_timeout_is_global_and_does_not_lock_db_rows(client):
    client.app.state.settings.vision_timeout = 42
    second = extra()
    response = save(client, [second, primary(config=CONNECTION)])
    assert response.status_code == 200
    assert all(item["source"] == "db" and item["config_editable"]
               for item in response.json()["items"])
    with client.app.state.session_factory() as session:
        service = registry(client, session)
        assert service.select_runtime("assistant")["timeout"] == 42
        assert service.primary.vision_runtime()["timeout"] == 42


def test_explicit_registry_never_restores_excluded_legacy_db_row(client):
    assert save(client, []).status_code == 200
    with client.app.state.session_factory() as session:
        service = registry(client, session)
        for field, value in CONNECTION.items():
            service.primary._upsert("vision." + field, value, "test")
        session.commit()
        assert service.primary.vision_runtime() is not None
        assert service.select_runtime("assistant") is None
    assert client.get(PATH).json() == {"revision": 1, "items": []}


def test_env_saved_probe_allowed_but_all_draft_overrides_rejected(client, upstream):
    assert save(client, [primary(config=CONNECTION)]).status_code == 200
    client.app.state.settings.vision_supports_vision = False
    before = snapshot(client)
    with readonly(client):
        for kwargs in ({}, {"json": {"expected_revision": 1, "config": {}}}):
            assert client.request("POST", PATH + "/primary/test", **kwargs).json()["ok"]
        for config in ({"model": CONNECTION["model"]}, {"base_url": CONNECTION["base_url"]},
                       {"max_tokens": 0}, {"supports_vision": False}):
            response = client.request("POST", PATH + "/primary/test",
                                      json={"expected_revision": 1, "config": config})
            assert response.status_code == 409
            assert response.json() == {"detail": {"code": "model_config_locked"}}
    assert len(upstream[0]) == 2 and snapshot(client) == before


def test_twenty_db_rows_plus_env_remain_visible_selectable_and_editable(client):
    rows = [extra(name=f"DB {index}", config={**CONNECTION, "model": f"db-{index}"})
            for index in range(20)]
    assert save(client, rows).status_code == 200
    settings = client.app.state.settings
    for field, value in CONNECTION.items():
        setattr(settings, "vision_" + field, SecretStr(value) if field == "api_key" else value)
    before = snapshot(client)
    with readonly(client):
        view = client.get(PATH).json()
        RegistryView.model_validate(view)
        assert len(view["items"]) == 21
        assert [item["id"] for item in view["items"]] == [row["id"] for row in rows] + ["primary"]
        assert [item["source"] for item in view["items"]] == ["db"] * 20 + ["env"]
        with client.app.state.session_factory() as session:
            assert registry(client, session).select_runtime("assistant")["model"] == "db-0"
    assert snapshot(client) == before
    # Metadata-only saves must fit even when every DB slot was already occupied.
    metadata = [{key: row[key] for key in ("id", "name", "enabled", "usages")}
                for row in view["items"]]
    metadata[-1]["name"] = "Environment metadata"
    response = save(client, metadata, 1)
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 21
    assert response.json()["revision"] == 2
    before = snapshot(client)
    assert save(client, metadata[:-1], 2).json() == {"detail": {"code": "model_config_locked"}}
    assert snapshot(client) == before
    # Removing a DB entry and reordering metadata preserves all remaining entries.
    metadata = [metadata[-1], *reversed(metadata[1:-1])]
    metadata[0]["enabled"] = False
    response = save(client, metadata, 2)
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == [item["id"] for item in metadata]
    with client.app.state.session_factory() as session:
        service = registry(client, session)
        assert service.select_runtime("assistant")["model"] == "db-19"
        doc, _ = service._load()
        RegistryDocument.model_validate(doc.model_dump(exclude_unset=True))
        assert len(doc.items) == 20


@pytest.mark.parametrize("env", [False, True])
def test_twenty_one_db_rows_rejected_before_write(client, env):
    if env:
        client.app.state.settings.vision_supports_vision = False
    rows = [extra() for _ in range(21)]
    before = snapshot(client)
    with readonly(client):
        response = save(client, rows)
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "model_registry_limit"}}
    assert snapshot(client) == before


def test_legacy_primary_counts_as_db_without_env_and_cannot_exceed_twenty(client):
    rows = [extra() for _ in range(20)]
    assert save(client, rows).status_code == 200
    before = snapshot(client)
    response = save(client, [*rows, primary()], 1)
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "model_registry_limit"}}
    assert snapshot(client) == before
    response = client.put("/api/v1/integrations/vision", json=CONNECTION)
    assert response.status_code == 422
    # The legacy endpoint touches authentication state, but configuration is atomic.
    assert snapshot(client)[0] == before[0]
    assert len(client.get(PATH).json()["items"]) == 20


@pytest.mark.parametrize("env", [False, True])
def test_save_guard_counts_db_rows_and_env_allowance(client, env):
    rows = [extra() for _ in range(20)]
    assert save(client, rows).status_code == 200
    if env:
        client.app.state.settings.vision_max_tokens = 0
    with client.app.state.session_factory() as session:
        service = registry(client, session)
        doc, snapshot_value = service._load()
        if env:
            assert len(doc.items) == 21
            service._save(doc, snapshot_value, "test")
            session.commit()
            assert len(service.view().items) == 21
            saved, _ = service._load()
            RegistryDocument.model_validate(saved.model_dump(exclude_unset=True))
            service.touch_legacy()
            session.commit()
            assert len(service.view().items) == 21
        else:
            doc.items.append(service._legacy_item())
            with pytest.raises(ModelRegistryError) as error:
                service._save(doc, snapshot_value, "test")
            assert error.value.code == "model_registry_limit"
            session.rollback()
    assert client.get(PATH).json()["revision"] == (3 if env else 1)


def test_env_only_metadata_lifecycle_preserves_twenty_db_rows_without_read_writes(client):
    rows = [extra(name=f"DB {index}", config={**CONNECTION, "model": f"db-{index}"})
            for index in range(20)]
    assert save(client, rows).status_code == 200
    settings = client.app.state.settings
    settings.vision_supports_vision = False
    metadata = [{key: row[key] for key in ("id", "name", "enabled", "usages")}
                for row in rows]
    env_metadata = primary(name="Saved ENV metadata", enabled=False, usages=["mcp"])
    metadata.insert(7, env_metadata)
    response = save(client, metadata, 1)
    assert response.status_code == 200, response.text
    assert "environment_only" not in response.text
    with client.app.state.session_factory() as session:
        service = registry(client, session)
        assert service._load()[0].environment_only is True
    before = snapshot(client)
    settings.vision_supports_vision = None
    with readonly(client):
        for _ in range(2):
            response = client.get(PATH)
            view = response.json()
            RegistryView.model_validate(view)
            assert view["revision"] == 2 and len(view["items"]) == 20
            assert [item["id"] for item in view["items"]] == [row["id"] for row in rows]
            assert all(item["source"] == "db" for item in view["items"])
            assert "environment_only" not in response.text
        with client.app.state.session_factory() as session:
            assert registry(client, session).select_runtime("assistant")["model"] == "db-0"
    assert snapshot(client) == before
    settings.vision_supports_vision = False
    with readonly(client):
        restored = client.get(PATH).json()["items"]
        assert len(restored) == 21
        assert [item["id"] for item in restored] == [item["id"] for item in metadata]
        assert {key: restored[7][key] for key in env_metadata} == env_metadata
        assert restored[7]["source"] == "env"
    assert snapshot(client) == before
    settings.vision_supports_vision = None
    response = save(client, [item for item in metadata if item["id"] != "primary"], 2)
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 20
    with client.app.state.session_factory() as session:
        doc, _ = registry(client, session)._load()
        assert doc.environment_only is False
        assert all(item.id != "primary" for item in doc.items)


def test_env_masking_legacy_db_row_keeps_it_when_env_removed(client):
    rows = [extra() for _ in range(19)]
    legacy = primary(name="DB origin", enabled=False, usages=["mcp"], config=CONNECTION)
    assert save(client, [*rows, legacy]).status_code == 200
    client.app.state.settings.vision_model = "env-mask"
    legacy.pop("config")
    metadata = [legacy, *rows]
    assert save(client, metadata, 1).status_code == 200
    with client.app.state.session_factory() as session:
        assert registry(client, session)._load()[0].environment_only is False
    before = snapshot(client)
    client.app.state.settings.vision_model = None
    with readonly(client):
        view = client.get(PATH).json()
        assert len(view["items"]) == 20
        item = view["items"][0]
        assert {key: item[key] for key in legacy} == legacy
        assert item["source"] == "db" and item["config_editable"]
        assert item["config"]["model"]["value"] == CONNECTION["model"]
    assert snapshot(client) == before
    assert save(client, metadata, 2).status_code == 200


def test_legacy_put_converts_env_only_origin_to_db(client):
    client.app.state.settings.vision_max_tokens = 0
    assert save(client, [primary(name="ENV metadata")]).status_code == 200
    client.app.state.settings.vision_max_tokens = None
    # A concrete legacy DB row prevents filtering, even before a registry mutation.
    with client.app.state.session_factory() as session:
        registry(client, session).primary._upsert("vision.model", "legacy", "test")
        session.commit()
    assert client.get(PATH).json()["items"][0]["name"] == "ENV metadata"
    assert client.put("/api/v1/integrations/vision", json=CONNECTION).status_code == 200
    with client.app.state.session_factory() as session:
        service = registry(client, session)
        assert service._load()[0].environment_only is False
        assert service.select_runtime("assistant")["model"] == CONNECTION["model"]
    assert client.get(PATH).json()["items"][0]["source"] == "db"
