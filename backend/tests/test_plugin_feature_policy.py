import json

import pytest

from app.features.plugin_registry import get_plugin_feature
from app.models.plugin import PluginRegistration
from app.schemas.plugin_runtime import PluginManifest
from app.services.plugin_feature_policy import CATEGORY_RISKS, approved_plugin_features
from app.services.plugin_runtime import PluginValidationError, _validate_plugin_metadata
from tests.helpers import login_client
from tests.test_plugin_runtime_api import HEADERS, plugin_client, register_payload

BASE = "/api/v1/plugin-runtime"
PLUGIN = f"{BASE}/plugins/acme"
KEY = "plugin.acme.test"


@pytest.fixture
def policy_client(tmp_path, monkeypatch):
    payload = register_payload()
    payload["manifest"]["capabilities"] = ["features"]
    monkeypatch.setattr("app.services.plugin_runtime.verify_plugin_health", lambda *args: None)
    monkeypatch.setattr(
        "app.services.plugin_runtime.fetch_plugin_manifest",
        lambda *args: PluginManifest(**payload["manifest"]),
    )
    monkeypatch.setattr("app.services.plugin_runtime.fetch_plugin_features", lambda *args: [{
        "key": KEY, "name": "Test", "category": "action", "config_schema": {"type": "object"},
    }])
    client = plugin_client(tmp_path, plugin_verify_endpoint=True)
    assert client.post(f"{BASE}/register", json=payload, headers=HEADERS).status_code == 201
    login_client(client)
    return client, payload


def revision(body):
    return {key: body[key] for key in ("generation", "permission_epoch")}


def test_category_override_approval_reset_persistence_and_audit(policy_client):
    client, payload = policy_client
    original = client.get(PLUGIN).json()
    assert original["features"][0] == {
        "key": KEY, "name": "Test", "declared_category": "action", "category": "action",
        "category_overridden": False, "risk_index": 3, "risk_label": CATEGORY_RISKS["action"][1],
        "assistant_allowed": False, "assistant_policy_revision": 0,
    }
    approved = client.post(f"{PLUGIN}/approve", json=revision(original)).json()
    assert get_plugin_feature(KEY).category == "action"
    changed = client.put(f"{PLUGIN}/feature-categories", json={
        "categories": {KEY: "post"}, **revision(approved),
    })
    assert changed.status_code == 200, changed.text
    body = changed.json()
    assert body["permission_epoch"] == approved["permission_epoch"] + 1
    assert body["generation"] == approved["generation"]
    assert body["approved_capabilities"] == []
    assert body["approved_at"] is None and body["approved_by"] is None
    assert body["status"] == "unapproved"
    assert body["features"][0]["category"] == "post"
    assert body["features"][0]["declared_category"] == "action"
    assert body["features"][0]["risk_index"] == 5
    assert body["features"][0]["category_overridden"] is True
    assert get_plugin_feature(KEY) is None
    with client.app.state.session_factory() as db:
        assert approved_plugin_features(db) == []
        assert json.loads(db.get(PluginRegistration, "acme").features_json)[0]["category"] == "action"
    audit = client.get(f"{BASE}/audit", params={"event": "metadata_change"}).json()
    assert any(item["actor"] == "admin" and item["plugin_id"] == "acme" for item in audit["items"])
    registered = client.post(f"{BASE}/register", json=payload, headers=HEADERS).json()
    assert registered["features"][0]["category"] == "post"
    assert client.post(f"{PLUGIN}/approve", json=revision(registered)).status_code == 200
    assert get_plugin_feature(KEY).category == "post"
    with client.app.state.session_factory() as db:
        assert approved_plugin_features(db)[0].category == "post"
    current = client.get(PLUGIN).json()
    reset = client.put(f"{PLUGIN}/feature-categories", json={
        "categories": {KEY: None}, **revision(current),
    }).json()
    assert reset["features"][0]["category"] == "action"
    assert reset["features"][0]["category_overridden"] is False
    assert client.post(f"{PLUGIN}/approve", json={}).status_code == 200
    with client.app.state.session_factory() as db:
        assert approved_plugin_features(db)[0].category == "action"
    assert client.post(f"{PLUGIN}/disable").status_code == 200
    with client.app.state.session_factory() as db:
        assert approved_plugin_features(db) == []


def test_category_revision_validation_and_auth(policy_client):
    client, _ = policy_client
    current = client.get(PLUGIN).json()
    payload = {"categories": {KEY: "flow"}, **revision(current)}
    csrf = client.headers.pop("X-CSRF-Token")
    assert client.put(f"{PLUGIN}/feature-categories", json=payload).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    for categories in ({"plugin.acme.unknown": "flow"}, {KEY: "invalid"}):
        assert client.put(f"{PLUGIN}/feature-categories", json={**payload, "categories": categories}).status_code == 422
    assert client.put(f"{PLUGIN}/feature-categories", json={"categories": {}}).status_code == 422
    for key in ("generation", "permission_epoch"):
        stale = {**payload, key: current[key] + 1}
        assert client.put(f"{PLUGIN}/feature-categories", json=stale).status_code == 409
        assert client.post(f"{PLUGIN}/approve", json={key: stale[key]}).status_code == 409
    assert client.get(PLUGIN).json()["permission_epoch"] == current["permission_epoch"]
    assert client.put(f"{PLUGIN}/feature-categories", json=payload).status_code == 200
    assert client.post(f"{PLUGIN}/approve", json=revision(current)).status_code == 409
    assert client.put(f"{PLUGIN}/feature-categories", json=payload).status_code == 409
    client.cookies.clear()
    assert client.put(f"{PLUGIN}/feature-categories", json=payload).status_code == 401


@pytest.mark.parametrize("category,risk", [("flow", 1), ("action", 3), ("auth", 4), ("challenge", 3), ("sign", 4), ("post", 5)])
def test_host_risk_map(policy_client, category, risk):
    client, _ = policy_client
    current = client.get(PLUGIN).json()
    response = client.put(f"{PLUGIN}/feature-categories", json={
        "categories": {KEY: category}, **revision(current),
    })
    assert response.status_code == 200
    feature = response.json()["features"][0]
    assert feature["risk_index"] == risk
    assert feature["risk_label"] == CATEGORY_RISKS[category][1]


@pytest.mark.parametrize("category", [None, "invalid"])
def test_plugin_declared_category_still_required(category):
    with pytest.raises(PluginValidationError):
        _validate_plugin_metadata("acme", [{
            "key": KEY, "name": "Test", "category": category, "config_schema": {},
        }], [])
