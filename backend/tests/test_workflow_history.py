from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text

from app.core.crypto import CryptoError
from app.core.schema_updates import ensure_workflow_history_keys
from app.main import create_app
from app.models.workflow import Site, SiteCredential, Template, WorkflowHistory, WorkflowStep
from app.schemas.workflow import SiteWrite
from app.services.workflows import WorkflowConflictError, WorkflowService
from tests.helpers import login_client


def create(client, kind="sites", **values):
    payload = {"name": "owner", **({"base_url": "https://example.com"} if kind == "sites" else {}), **values}
    response = client.post(f"/api/v1/{kind}", json=payload)
    assert response.status_code == 201, response.text
    return response.json(), payload


def history_bytes(client):
    with client.app.state.session_factory() as session:
        return [(row.history_key, row.revision, row.ciphertext, row.nonce, row.content_hash)
                for row in session.scalars(select(WorkflowHistory).order_by(WorkflowHistory.id))]


@pytest.mark.parametrize("kind,revision_field", [("sites", "config_revision"), ("templates", "version")])
def test_history_create_update_restore_delete_immutable(client, kind, revision_field):
    created, payload = create(client, kind, steps=[{
        "step_key": "fill", "feature_type": "action.fill", "position": None,
        "enabled": None, "config": {"selector": ["#token"], "value": "inline-secret"},
    }])
    url = f"/api/v1/{kind}/{created['id']}"
    first = client.get(f"{url}/versions/1").json()
    assert set(first) == {"revision", "operation", "created_at", "created_by", "restored_from_revision",
                          "content_hash", "schema_version", "history_key", "owner_type", "snapshot"}
    assert first["operation"] == "create"
    assert first["created_by"] == "admin"
    assert first["snapshot"]["resolved"] == created["resolved"]
    assert "expected_revision" not in first["snapshot"]["local"]
    original = history_bytes(client)
    assert b"inline-secret" not in original[0][2]
    assert client.put(url, json={**payload, "name": "updated", "expected_revision": 1}).status_code == 200
    assert client.post(f"{url}/versions/1/restore", json={"expected_revision": 2}).status_code == 200
    assert client.get(url).json()[revision_field] == 3
    assert client.get(url).json()["name"] == "owner"
    assert client.get(f"{url}/versions/1").json() == first
    assert history_bytes(client)[0] == original[0]
    listing = client.get(f"{url}/versions?limit=2&offset=1").json()
    assert set(listing) == {"history_key", "current_revision", "total", "items"}
    assert listing["current_revision"] == listing["total"] == 3
    assert [item["revision"] for item in listing["items"]] == [2, 1]
    restore = client.get(f"{url}/versions/3").json()
    assert restore["operation"] == "restore" and restore["restored_from_revision"] == 1
    assert restore["snapshot"]["local"] == first["snapshot"]["local"]
    assert client.delete(f"{url}?expected_revision=2").status_code == 409
    assert client.delete(f"{url}?expected_revision=3").status_code == 204
    assert client.get(f"{url}/versions").status_code == 404
    archive = f"/api/v1/workflow-history/{created['history_key']}/versions"
    assert client.get(archive).json()["current_revision"] == 4
    assert client.get(f"{archive}/4").json()["operation"] == "delete"
    assert client.get(f"{archive}/1").json() == first
    replacement, _ = create(client, kind)
    assert replacement["history_key"] != created["history_key"]
    assert client.get(f"/api/v1/{kind}/{replacement['id']}/versions").json()["total"] == 1
    assert client.post(f"{archive}/1/restore", json={"expected_revision": 1}).status_code == 404


@pytest.mark.parametrize("kind", ["sites", "templates"])
def test_stale_cas_and_failed_validation_rollback(client, kind):
    owner, payload = create(client, kind)
    url = f"/api/v1/{kind}/{owner['id']}"
    assert client.put(url, json={**payload, "expected_revision": 1}).status_code == 200
    before = history_bytes(client)
    assert client.put(url, json={**payload, "name": "stale", "expected_revision": 1}).status_code == 409
    assert client.post(f"{url}/versions/1/restore", json={"expected_revision": 1}).status_code == 409
    bad = {**payload, "name": "invalid", "expected_revision": 2, "steps": [
        {"step_key": "wait", "feature_type": "action.wait", "config": {"seconds": -1}},
    ]}
    assert client.put(url, json=bad).status_code == 422
    assert history_bytes(client) == before
    assert client.get(url).json()["name"] == "owner"
    # Legacy callers can still omit expected_revision.
    assert client.put(url, json=payload).status_code == 200


@pytest.mark.parametrize("kind,revision_field", [("sites", "config_revision"), ("templates", "version")])
@pytest.mark.parametrize("operation", ["update", "delete", "restore"])
def test_client_history_key_rejects_aba_without_touching_replacement(client, kind, revision_field, operation):
    original, payload = create(client, kind)
    url = f"/api/v1/{kind}/{original['id']}"
    assert client.delete(url).status_code == 204
    replacement, _ = create(client, kind, name="replacement")
    assert replacement["id"] == original["id"]
    assert replacement[revision_field] == original[revision_field] == 1
    assert replacement["history_key"] != original["history_key"]
    current = client.get(url).json()
    before = history_bytes(client)
    preconditions = {"expected_revision": 1, "expected_history_key": original["history_key"]}
    if operation == "update":
        response = client.put(url, json={**payload, **preconditions})
    elif operation == "delete":
        response = client.delete(url, params=preconditions)
    else:
        response = client.post(f"{url}/versions/1/restore", json=preconditions)
    assert response.status_code == 409, response.text
    assert history_bytes(client) == before
    assert client.get(url).json() == current


@pytest.mark.parametrize("kind", ["sites", "templates"])
def test_history_key_precondition_success_and_snapshot_exclusion(client, kind):
    requested_key = str(uuid4())
    owner, payload = create(client, kind, expected_history_key=requested_key)
    # POST always generates a fresh identity, never adopts the precondition.
    assert owner["history_key"] != requested_key
    url = f"/api/v1/{kind}/{owner['id']}"
    payload["expected_history_key"] = owner["history_key"]
    assert client.put(url, json=payload).status_code == 200
    restored = client.post(f"{url}/versions/1/restore", json={
        "expected_revision": 2, "expected_history_key": owner["history_key"],
    })
    assert restored.status_code == 200, restored.text
    for revision in (1, 2, 3):
        local = client.get(f"{url}/versions/{revision}").json()["snapshot"]["local"]
        assert "expected_history_key" not in local and "expected_revision" not in local
    assert client.delete(url, params={
        "expected_revision": 3, "expected_history_key": owner["history_key"],
    }).status_code == 204


@pytest.mark.parametrize("kind", ["sites", "templates"])
def test_history_key_requires_uuid_on_write_restore_and_delete(client, kind):
    owner, payload = create(client, kind)
    url = f"/api/v1/{kind}/{owner['id']}"
    before = history_bytes(client)
    assert client.put(url, json={**payload, "expected_history_key": "invalid"}).status_code == 422
    assert client.post(f"{url}/versions/1/restore", json={
        "expected_revision": 1, "expected_history_key": "invalid",
    }).status_code == 422
    assert client.delete(url, params={"expected_history_key": "invalid"}).status_code == 422
    assert history_bytes(client) == before


def test_database_cas_rejects_stale_session_not_just_python_comparison(client):
    owner, payload = create(client)
    app = client.app
    with app.state.session_factory() as stale:
        stale_owner = stale.get(Site, owner["id"])
        assert stale_owner.config_revision == 1
        assert client.put(f"/api/v1/sites/{owner['id']}", json=payload).status_code == 200
        with pytest.raises(WorkflowConflictError):
            WorkflowService(stale, app.state.settings, app.state.crypto).update_site(
                owner["id"], SiteWrite(**payload, expected_revision=1))
    assert client.get(f"/api/v1/sites/{owner['id']}/versions").json()["total"] == 2


def test_simultaneous_expected_revision_only_one_writer_commits(client):
    owner, payload = create(client)
    app = client.app

    def write(index):
        with app.state.session_factory() as session:
            service = WorkflowService(session, app.state.settings, app.state.crypto)
            try:
                service.update_site(owner["id"], SiteWrite(**{
                    **payload, "name": f"writer-{index}", "expected_revision": 1,
                }))
                return "committed"
            except WorkflowConflictError:
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(write, range(2))) == ["committed", "conflict"]
    assert client.get(f"/api/v1/sites/{owner['id']}/versions").json()["total"] == 2


@pytest.mark.parametrize("operation", ["create", "update", "restore", "delete"])
def test_history_encryption_failure_rolls_back_everything(client, monkeypatch, operation):
    owner, payload = create(client)
    url = f"/api/v1/sites/{owner['id']}"
    before = history_bytes(client)

    def broken(*args, **kwargs):
        raise CryptoError("DO NOT LEAK encryption details")

    monkeypatch.setattr(client.app.state.crypto, "encrypt", broken)
    if operation == "create":
        response = client.post("/api/v1/sites", json={**payload, "name": "failed"})
    elif operation == "update":
        response = client.put(url, json={**payload, "name": "failed"})
    elif operation == "restore":
        response = client.post(f"{url}/versions/1/restore", json={"expected_revision": 1})
    else:
        response = client.delete(url)
    assert response.status_code == 422, response.text
    assert "DO NOT LEAK" not in response.text
    assert history_bytes(client) == before
    assert client.get(url).json()["config_revision"] == 1
    assert len(client.get("/api/v1/sites").json()) == 1


def test_unexpected_failure_rolls_back_service_session(client, monkeypatch):
    owner, payload = create(client)
    app = client.app
    with app.state.session_factory() as session:
        service = WorkflowService(session, app.state.settings, app.state.crypto)

        def broken(*args, **kwargs):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(service, "_record_history", broken)
        with pytest.raises(RuntimeError):
            service.update_site(owner["id"], SiteWrite(**{**payload, "name": "failed"}))
        assert session.get(Site, owner["id"]).config_revision == 1
        assert session.get(Site, owner["id"]).name == "owner"


@pytest.mark.parametrize("kind", ["sites", "templates"])
def test_frozen_inheritance_drift_and_restore_local_only(client, kind):
    parent, parent_payload = create(client, "templates", name="parent", steps=[{
        "step_key": "wait", "feature_type": "action.wait", "position": 1000, "config": {"seconds": 2},
    }])
    owner, payload = create(client, kind, **{
        "template_id" if kind == "sites" else "parent_id": parent["id"],
    }, steps=[{"step_key": "wait", "feature_type": "action.wait", "config": {"seconds": None}}])
    url = f"/api/v1/{kind}/{owner['id']}"
    first = client.get(f"{url}/versions/1").json()
    assert first["snapshot"]["dependencies"][0]["history_key"] == parent["history_key"]
    assert first["snapshot"]["dependencies"][0]["local"]["steps"][0]["config"]["seconds"] == 2
    assert client.put(f"/api/v1/templates/{parent['id']}", json={**parent_payload, "description": "drift"}).status_code == 200
    assert client.get(f"{url}/versions/1").json() == first
    assert client.post(f"{url}/versions/1/restore", json={"expected_revision": 1}).status_code == 409
    restored = client.post(f"{url}/versions/1/restore", json={"expected_revision": 1, "accept_current_dependencies": True})
    assert restored.status_code == 200, restored.text
    second = client.get(f"{url}/versions/2").json()
    assert second["snapshot"]["local"] == first["snapshot"]["local"]
    assert second["snapshot"]["dependencies"][0]["revision"] == 2
    assert client.get(f"/api/v1/templates/{parent['id']}").json()["description"] == "drift"


def test_restore_preserves_stored_override_position_nulls_and_group_anchor(client):
    parent, _ = create(client, "templates", name="parent", steps=[{
        "step_key": "wait", "feature_type": "action.wait", "position": 1000,
    }])
    owner, _ = create(client, template_id=parent["id"],
        groups=[{"group_key": "local", "name": "Local", "step_anchor": None}],
        steps=[{"step_key": "wait", "feature_type": "action.wait", "label": None,
                "enabled": None, "config": {"seconds": None}},
               {"step_key": "own", "feature_type": "action.wait", "group_key": "local"}])
    app = client.app
    # Seed a historically stored position that normal writes would renormalize.
    with app.state.session_factory() as session:
        session.query(WorkflowHistory).filter_by(history_key=owner["history_key"]).delete()
        step = session.scalar(select(WorkflowStep).where(WorkflowStep.owner_type == "site", WorkflowStep.owner_id == owner["id"], WorkflowStep.step_key == "wait"))
        step.position = 42
        session.flush()
        WorkflowService(session, app.state.settings, app.state.crypto).baseline_workflows()
    url = f"/api/v1/sites/{owner['id']}"
    local = client.get(f"{url}/versions/1").json()["snapshot"]["local"]
    changed = json.loads(json.dumps(local))
    changed["groups"][0]["step_anchor"] = 500
    assert client.put(url, json=changed).status_code == 200
    restored = client.post(f"{url}/versions/1/restore", json={"expected_revision": 2})
    assert restored.status_code == 200, restored.text
    assert restored.json()["groups"][0]["step_anchor"] is None
    assert restored.json()["steps"][0]["position"] == 42
    assert client.get(f"{url}/versions/3").json()["snapshot"]["local"] == local


@pytest.mark.parametrize("kind,revision_field", [("sites", "config_revision"), ("templates", "version")])
def test_restore_checks_effective_override_position_after_dependency_drift(client, kind, revision_field):
    parent, parent_payload = create(client, "templates", name="parent", steps=[{
        "step_key": "x", "feature_type": "action.wait", "position": 1000,
    }])
    owner, payload = create(client, kind, **{
        "template_id" if kind == "sites" else "parent_id": parent["id"],
    }, steps=[
        {"step_key": "x", "feature_type": "action.wait"},
        {"step_key": "y", "feature_type": "action.wait", "position": 3000},
    ])
    url = f"/api/v1/{kind}/{owner['id']}"
    first = client.get(f"{url}/versions/1").json()
    assert first["snapshot"]["local"]["steps"][0]["position"] == 1001
    assert client.put(url, json={**payload, "steps": payload["steps"][:1]}).status_code == 200
    parent_payload["steps"][0]["position"] = 2999
    response = client.put(f"/api/v1/templates/{parent['id']}", json=parent_payload)
    assert response.status_code == 200, response.text
    before = history_bytes(client)
    current = client.get(url).json()
    response = client.post(f"{url}/versions/1/restore", json={
        "expected_revision": 2, "expected_history_key": owner["history_key"],
        "accept_current_dependencies": True,
    })
    assert response.status_code == 422, response.text
    assert "Ambiguous ungrouped step position 3000" in response.json()["detail"]
    assert history_bytes(client) == before
    assert client.get(url).json() == current
    assert current[revision_field] == 2
    assert client.get(f"{url}/versions/1").json() == first


def test_missing_and_replaced_dependency_identity_always_rejected(client):
    parent, _ = create(client, "templates", name="parent")
    owner, payload = create(client, template_id=parent["id"])
    url = f"/api/v1/sites/{owner['id']}"
    assert client.put(url, json={**payload, "template_id": None}).status_code == 200
    assert client.delete(f"/api/v1/templates/{parent['id']}").status_code == 204
    restore = {"expected_revision": 2, "accept_current_dependencies": True}
    assert client.post(f"{url}/versions/1/restore", json=restore).status_code == 409
    replacement, _ = create(client, "templates", name="replacement")
    assert replacement["id"] == parent["id"]
    assert replacement["history_key"] != parent["history_key"]
    assert client.post(f"{url}/versions/1/restore", json=restore).status_code == 409
    assert client.get(url).json()["config_revision"] == 2


def test_template_change_and_restore_validate_descendants(client):
    parent, payload = create(client, "templates", name="parent", steps=[{
        "step_key": "condition", "feature_type": "action.wait", "condition": "always",
    }])
    assert client.put(f"/api/v1/templates/{parent['id']}", json={**payload, "steps": []}).status_code == 200
    create(client, template_id=parent["id"], steps=[{
        "step_key": "condition", "feature_type": "action.fill",
        "config": {"selector": ["#x"], "value": "x"},
    }])
    before = history_bytes(client)
    response = client.post(f"/api/v1/templates/{parent['id']}/versions/1/restore", json={"expected_revision": 2})
    assert response.status_code == 422, response.text
    assert history_bytes(client) == before
    assert client.get(f"/api/v1/templates/{parent['id']}").json()["version"] == 2


@pytest.mark.parametrize("kind", ["sites", "templates"])
def test_parent_restore_rejects_descendant_effective_position_collision(client, kind):
    parent, payload = create(client, "templates", name="parent", steps=[{
        "step_key": "inherited", "feature_type": "action.wait", "position": 2999,
    }])
    parent_url = f"/api/v1/templates/{parent['id']}"
    changed = {**payload, "steps": [{**payload["steps"][0], "position": 1000}]}
    assert client.put(parent_url, json=changed).status_code == 200
    child, _ = create(client, kind, name="child", **{
        "template_id" if kind == "sites" else "parent_id": parent["id"],
    }, steps=[
        {"step_key": "inherited", "feature_type": "action.wait"},
        {"step_key": "local", "feature_type": "action.wait", "position": 3000},
    ])
    child_url = f"/api/v1/{kind}/{child['id']}"
    before = history_bytes(client)
    child_before = client.get(child_url).json()
    response = client.post(f"{parent_url}/versions/1/restore", json={"expected_revision": 2})
    assert response.status_code == 422, response.text
    assert "Ambiguous ungrouped step position" in response.text
    assert history_bytes(client) == before
    assert client.get(parent_url).json()["version"] == 2
    assert client.get(child_url).json() == child_before
    response = client.put(parent_url, json=payload)
    assert response.status_code == 422, response.text
    assert history_bytes(client) == before


def test_feature_settings_frozen_drift_and_version_compatibility(client, monkeypatch):
    owner, _ = create(client, steps=[{"step_key": "wait", "feature_type": "action.wait"}])
    url = f"/api/v1/sites/{owner['id']}"
    first = client.get(f"{url}/versions/1").json()
    assert first["snapshot"]["features"]["action.wait"]["versions"] == [1]
    from app.models.setting import AppSetting
    with client.app.state.session_factory() as session:
        session.add(AppSetting(key="feature.action.wait", value_json=json.dumps({
            "defaults": {"seconds": 7}, "required_fields": [],
        })))
        session.commit()
    assert client.get(f"{url}/versions/1").json() == first
    assert client.post(f"{url}/versions/1/restore", json={"expected_revision": 1}).status_code == 409
    response = client.post(f"{url}/versions/1/restore", json={"expected_revision": 1, "accept_current_dependencies": True})
    assert response.status_code == 200, response.text
    assert response.json()["resolved"]["steps"][0]["config"]["seconds"] == 7
    from app.features.catalog import get_feature
    original_get = get_feature
    monkeypatch.setattr("app.services.workflow_history.get_feature", lambda key: replace(original_get(key), version=2))
    assert client.post(f"{url}/versions/1/restore", json={"expected_revision": 2, "accept_current_dependencies": True}).status_code == 409


def test_credentials_and_cookies_not_snapshotted_or_changed_by_restore(client):
    owner, payload = create(client)
    url = f"/api/v1/sites/{owner['id']}"
    assert client.put(f"{url}/credential", json={"username": "secret-user", "password": "dedicated-password"}).status_code == 200
    assert client.put(f"{url}/cookies", json={"cookies": "session=cookie-secret"}).status_code == 200
    with client.app.state.session_factory() as session:
        row = session.get(SiteCredential, owner["id"])
        before = (row.username_ciphertext, row.password_ciphertext, row.cookies_ciphertext)
    assert client.put(url, json={**payload, "notes": "changed"}).status_code == 200
    assert client.post(f"{url}/versions/1/restore", json={"expected_revision": 2}).status_code == 200
    with client.app.state.session_factory() as session:
        row = session.get(SiteCredential, owner["id"])
        assert (row.username_ciphertext, row.password_ciphertext, row.cookies_ciphertext) == before
    for revision in (1, 2, 3):
        body = client.get(f"{url}/versions/{revision}").text
        for secret in ("secret-user", "dedicated-password", "cookie-secret", "test-cfbox-key"):
            assert secret not in body
        assert "credential\":" not in body and "cookies\":" not in body


def test_auth_csrf_validation_and_missing_versions(client, anon_client):
    owner, _ = create(client)
    url = f"/api/v1/sites/{owner['id']}"
    assert anon_client.get(f"{url}/versions").status_code == 401
    assert anon_client.get(f"/api/v1/workflow-history/{owner['history_key']}/versions").status_code == 401
    assert client.post(f"{url}/versions/1/restore", json={}).status_code == 422
    assert client.get(f"{url}/versions?limit=0").status_code == 422
    assert client.get(f"{url}/versions?offset=-1").status_code == 422
    assert client.get(f"{url}/versions/999").status_code == 404
    assert client.post(f"{url}/versions/999/restore", json={"expected_revision": 1}).status_code == 404
    client.headers.pop("X-CSRF-Token")
    assert client.post(f"{url}/versions/1/restore", json={"expected_revision": 1}).status_code == 403
    assert client.delete(url).status_code == 403


def test_ciphertext_aad_prevents_swapping_revision_and_hides_crypto_error(client):
    owner, payload = create(client)
    url = f"/api/v1/sites/{owner['id']}"
    assert client.put(url, json=payload).status_code == 200
    with client.app.state.session_factory() as session:
        first, second = session.scalars(select(WorkflowHistory).order_by(WorkflowHistory.revision)).all()
        second.ciphertext, second.nonce = first.ciphertext, first.nonce
        session.commit()
    response = client.get(f"{url}/versions/2")
    assert response.status_code == 422
    assert "decrypt" not in response.text and "ciphertext" not in response.text
    assert client.get(f"{url}/versions/1").status_code == 200


def test_startup_baseline_current_non_one_invalid_and_idempotent(settings):
    app = create_app(settings)
    with app.state.session_factory() as session:
        template = Template(name="legacy-template", version=9)
        site = Site(name="legacy-site", base_url="https://example.com", config_revision=7)
        session.add_all([template, site])
        session.flush()
        session.add(WorkflowStep(owner_type="site", owner_id=site.id, step_key="gone",
                                 feature_type="plugin.removed.feature", feature_version=4,
                                 category="action", config_json='{"value": "legacy-secret"}'))
        session.commit()
        template_key, site_key, site_id = template.history_key, site.history_key, site.id
    with TestClient(create_app(settings)) as client:
        login_client(client)
        first = client.get(f"/api/v1/sites/{site_id}/versions/7").json()
        assert first["operation"] == "baseline" and first["created_by"] is None
        assert first["snapshot"]["resolved"] is None
        assert first["snapshot"]["resolution_error"]
        assert first["snapshot"]["features"]["plugin.removed.feature"]["versions"] == [4]
        before = history_bytes(client)
        assert len(before) == 2
        assert {(key, revision) for key, revision, *_ in before} == {(template_key, 9), (site_key, 7)}
        assert client.get(f"/api/v1/sites/{site_id}/versions/1").status_code == 404
    with TestClient(create_app(settings)) as client:
        login_client(client)
        assert history_bytes(client) == before
        assert client.get(f"/api/v1/sites/{site_id}/versions/7").json() == first


@pytest.mark.parametrize("raw", ['{"broken"', '[]'])
def test_baseline_keeps_corrupt_legacy_config_without_blocking_startup(settings, raw):
    app = create_app(settings)
    with app.state.session_factory() as session:
        site = Site(name="corrupt-legacy", base_url="https://example.com", config_revision=5)
        session.add(site)
        session.flush()
        session.add(WorkflowStep(owner_type="site", owner_id=site.id, step_key="wait",
                                 feature_type="action.wait", feature_version=1,
                                 category="action", config_json=raw))
        session.commit()
        site_id = site.id
    with TestClient(create_app(settings)) as client:
        login_client(client)
        response = client.get(f"/api/v1/sites/{site_id}/versions/5")
        assert response.status_code == 200, response.text
        snapshot = response.json()["snapshot"]
        assert snapshot["resolved"] is None and snapshot["resolution_error"]
        assert snapshot["local"]["steps"][0]["config"] == (raw if raw != '[]' else [])
        response = client.post(f"/api/v1/sites/{site_id}/versions/5/restore", json={"expected_revision": 5})
        assert response.status_code == 422
        assert len(history_bytes(client)) == 1


@pytest.mark.parametrize("field", ["trusted_urls", "tags"])
@pytest.mark.parametrize("raw", ['{"broken"', '{"wrong": "shape"}', 'null'])
def test_baseline_preserves_corrupt_site_json_fields_and_rejects_restore(settings, field, raw):
    app = create_app(settings)
    with app.state.session_factory() as session:
        site = Site(name="corrupt-site-field", base_url="https://example.com", config_revision=6,
                    **{f"{field}_json": raw})
        session.add(site)
        session.commit()
        site_id, history_key = site.id, site.history_key
    for restart in range(2):
        with TestClient(create_app(settings)) as client:
            login_client(client)
            url = f"/api/v1/sites/{site_id}"
            version = client.get(f"{url}/versions/6")
            assert version.status_code == 200, version.text
            snapshot = version.json()["snapshot"]
            assert snapshot["local"][field] == (raw if raw == '{"broken"' else json.loads(raw))
            assert snapshot["resolved"] is None
            assert snapshot["resolution_error"] == "当前配置无法解析；此基线仅记录迁移时的配置，未推断旧版本。"
            before = history_bytes(client)
            assert len(before) == 1
            assert client.post(f"{url}/versions/6/restore", json={
                "expected_revision": 6, "expected_history_key": history_key,
            }).status_code == 422
            assert history_bytes(client) == before
            with client.app.state.session_factory() as session:
                row = session.get(Site, site_id)
                assert getattr(row, f"{field}_json") == raw
                assert row.config_revision == 6
            if restart == 0:
                baseline = version.json()
                baseline_bytes = before
            else:
                assert version.json() == baseline
                assert before == baseline_bytes


def test_incremental_history_key_migration_preserves_ids_and_revisions(settings):
    app = create_app(settings)
    with app.state.engine.begin() as connection:
        connection.execute(text("DROP TABLE workflow_history"))
        for table in ("sites", "templates"):
            connection.execute(text(f"DROP TABLE {table}"))
        connection.execute(text("CREATE TABLE templates (id INTEGER PRIMARY KEY, name TEXT, version INTEGER)"))
        connection.execute(text("CREATE TABLE sites (id INTEGER PRIMARY KEY, name TEXT, config_revision INTEGER)"))
        connection.execute(text("INSERT INTO templates VALUES (12, 'old', 13)"))
        connection.execute(text("INSERT INTO sites VALUES (15, 'old', 17)"))
    ensure_workflow_history_keys(app.state.engine)
    with app.state.engine.connect() as connection:
        before = {table: connection.execute(text(f"SELECT * FROM {table}")).all()
                  for table in ("sites", "templates")}
    ensure_workflow_history_keys(app.state.engine)
    with app.state.engine.connect() as connection:
        after = {table: connection.execute(text(f"SELECT * FROM {table}")).all()
                 for table in ("sites", "templates")}
    assert before == after
    assert before["sites"][0][:3] == (15, "old", 17)
    assert len(before["sites"][0][3]) == len(before["templates"][0][3]) == 36
    assert any(index["unique"] for index in inspect(app.state.engine).get_indexes("sites"))


def test_reads_and_writes_do_not_silently_baseline(client):
    owner, payload = create(client)
    with client.app.state.session_factory() as session:
        session.query(WorkflowHistory).filter_by(history_key=owner["history_key"]).delete()
        session.commit()
    url = f"/api/v1/sites/{owner['id']}"
    assert client.get(f"{url}/versions").json()["total"] == 0
    assert client.get(url).status_code == 200
    assert client.put(url, json=payload).status_code == 409
    assert client.delete(url).status_code == 409
    assert history_bytes(client) == []
