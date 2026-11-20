import json

import pytest

from app.features.catalog import FEATURES
from app.features.plugin_registry import clear_plugin_registry
from app.models.setting import AppSetting
from app.schemas.plugin_runtime import PluginManifest
from app.services.assistant_categories import Denied
from app.services.assistant_feature_policy import (
    assistant_allowed,
    assistant_explicitly_allowed,
    policy_binding,
    require_assistant_allowed,
    set_assistant_allowed,
)
from tests.helpers import login_client
from tests.test_plugin_runtime_api import HEADERS, plugin_client, register_payload


BASE = "/api/v1/plugin-runtime"
PLUGIN = f"{BASE}/plugins/acme"
KEY = "plugin.acme.post"
METHOD = "plugin.acme.ocr"


@pytest.fixture
def client(tmp_path, monkeypatch):
    clear_plugin_registry()
    payload = register_payload()
    monkeypatch.setattr("app.services.plugin_runtime.verify_plugin_health", lambda *args: None)
    monkeypatch.setattr("app.services.plugin_runtime.fetch_plugin_manifest",
                        lambda *args: PluginManifest(**payload["manifest"]))
    monkeypatch.setattr("app.services.plugin_runtime.fetch_plugin_features", lambda *args: [{
        "key": KEY, "name": "Post", "category": "post", "cardinality": "single",
        "config_schema": {"type": "object"},
    }])
    monkeypatch.setattr("app.services.plugin_runtime.fetch_plugin_captcha_methods", lambda *args: [{
        "key": METHOD, "label": "OCR",
    }])
    client = plugin_client(tmp_path, plugin_verify_endpoint=True)
    assert client.post(f"{BASE}/register", json=payload, headers=HEADERS).status_code == 201
    login_client(client)
    yield client
    clear_plugin_registry()


def revision(body):
    return {key: body[key] for key in ("generation", "permission_epoch")}


def test_defaults_persistence_revision_and_no_commit(client):
    with client.app.state.session_factory() as db:
        assert all(assistant_allowed(db, key) for key in FEATURES)
        for key in (KEY, METHOD, "missing"):
            assert policy_binding(db, key) == {"allowed": False, "revision": 0}
            with pytest.raises(Denied):
                require_assistant_allowed(db, key)
        assert not assistant_explicitly_allowed(db, "post.buy_card")
        set_assistant_allowed(db, "post.buy_card", True)
        assert assistant_explicitly_allowed(db, "post.buy_card")
        db.rollback()
        assert policy_binding(db, "post.buy_card")["revision"] == 0
        assert not assistant_explicitly_allowed(db, "post.buy_card")
        set_assistant_allowed(db, "post.buy_card", True)
        assert policy_binding(db, "post.buy_card") == {"allowed": True, "revision": 1}
        set_assistant_allowed(db, "post.buy_card", True)
        assert policy_binding(db, "post.buy_card")["revision"] == 1
        db.rollback()
        set_assistant_allowed(db, "post.buy_card", False)
        with pytest.raises(Denied, match="assistant_feature_denied"):
            require_assistant_allowed(db, "post.buy_card")
        db.commit()
    with client.app.state.session_factory() as db:
        assert policy_binding(db, "post.buy_card") == {"allowed": False, "revision": 1}
        set_assistant_allowed(db, "post.buy_card", False)
        assert policy_binding(db, "post.buy_card")["revision"] == 1
        set_assistant_allowed(db, "post.buy_card", True)
        db.commit()
        assert json.loads(db.get(AppSetting, "assistant.feature_policy.post.buy_card").value_json) == {
            "allowed": True, "revision": 2,
        }
        with pytest.raises(ValueError):
            set_assistant_allowed(db, "missing", True)


def test_unapproved_metadata_permissions_and_approval_preservation(client):
    original = client.get(PLUGIN).json()
    assert original["features"][0]["assistant_allowed"] is False
    method = original["captcha_methods"][0]
    assert method == {
        "key": METHOD, "name": "OCR", "declared_category": "challenge", "category": "challenge",
        "category_overridden": False, "risk_index": 3, "risk_label": method["risk_label"],
        "assistant_allowed": False, "assistant_policy_revision": 0,
    }
    changed = client.put(f"{PLUGIN}/feature-categories", json={
        **revision(original), "assistant_permissions": {KEY: True, METHOD: True},
    })
    assert changed.status_code == 200, changed.text
    body = changed.json()
    assert body["approved_at"] is None
    assert all(item["assistant_allowed"] for item in body["features"] + body["captcha_methods"])
    assert all(item["assistant_policy_revision"] == 1
               for item in body["features"] + body["captcha_methods"])
    approved = client.post(f"{PLUGIN}/approve", json=revision(body)).json()
    changed = client.put(f"{PLUGIN}/feature-categories", json={
        **revision(approved), "assistant_permissions": {METHOD: False},
    }).json()
    assert changed["permission_epoch"] == approved["permission_epoch"] + 1
    assert changed["approved_at"].rstrip("Z") == approved["approved_at"].rstrip("Z")
    assert changed["approved_capabilities"] == approved["approved_capabilities"]
    assert changed["status"] == approved["status"]
    unchanged = client.put(f"{PLUGIN}/feature-categories", json={
        **revision(changed), "assistant_permissions": {METHOD: False},
    }).json()
    assert unchanged["permission_epoch"] == changed["permission_epoch"]
    assert unchanged["captcha_methods"][0]["assistant_policy_revision"] == 2
    assert unchanged["captcha_methods"][0]["assistant_allowed"] is False
    with client.app.state.session_factory() as db:
        assert assistant_explicitly_allowed(db, KEY)
        with pytest.raises(Denied, match="assistant_feature_denied"):
            require_assistant_allowed(db, METHOD)
    assert client.put(f"{PLUGIN}/feature-categories", json={
        **revision(approved), "assistant_permissions": {KEY: False},
    }).status_code == 409
    assert client.put(f"{PLUGIN}/feature-categories", json={
        **revision(changed), "assistant_permissions": {"plugin.other.key": True},
    }).status_code == 422
    category = client.put(f"{PLUGIN}/feature-categories", json={
        **revision(changed), "categories": {KEY: "action"},
    }).json()
    assert category["approved_at"] is None
    assert category["features"][0]["assistant_allowed"] is True
    assert category["features"][0]["assistant_policy_revision"] == 1


def test_unchanged_category_patch_preserves_approval_and_revisions(client):
    approved = client.post(f"{PLUGIN}/approve", json={}).json()
    for categories in ({}, {KEY: None}):
        response = client.put(f"{PLUGIN}/feature-categories", json={
            **revision(approved), "categories": categories,
        })
        assert response.status_code == 200, response.text
        body = response.json()
        assert revision(body) == revision(approved)
        assert body["approved_at"].rstrip("Z") == approved["approved_at"].rstrip("Z")
        assert body["approved_capabilities"] == approved["approved_capabilities"]
        assert body["features"][0]["assistant_policy_revision"] == 0


def test_feature_api_and_independent_settings(client):
    url = "/api/v1/features/post.buy_card/assistant-policy"
    features = client.get("/api/v1/features").json()
    builtin = next(item for item in features if item["key"] == "post.buy_card")
    assert builtin["assistant_allowed"] is True
    assert builtin["assistant_policy_revision"] == 0
    assert builtin["risk_index"] == 5 and builtin["risk_label"]
    csrf = client.headers.pop("X-CSRF-Token")
    assert client.put(url, json={"assistant_allowed": False}).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    assert client.put(url, json={"assistant_allowed": "false"}).status_code == 422
    changed = client.put(url, json={"assistant_allowed": False})
    assert changed.status_code == 200, changed.text
    assert changed.json()["assistant_allowed"] is False
    assert changed.json()["assistant_policy_revision"] == 1
    reset = client.delete("/api/v1/features/post.buy_card/settings").json()
    assert reset["assistant_allowed"] is False
    assert reset["assistant_policy_revision"] == 1
    plugin_url = f"/api/v1/features/{KEY}/assistant-policy"
    assert client.put(plugin_url, json={"assistant_allowed": True}).status_code == 404
    approved = client.post(f"{PLUGIN}/approve", json={}).json()
    changed = client.put(plugin_url, json={"assistant_allowed": True})
    assert changed.status_code == 200, changed.text
    assert changed.json()["assistant_allowed"] is True
    assert changed.json()["category"] == "post"
    current = client.get(PLUGIN).json()
    assert current["approved_at"].rstrip("Z") == approved["approved_at"].rstrip("Z")
    assert current["permission_epoch"] == approved["permission_epoch"] + 1
    assert client.put("/api/v1/features/missing/assistant-policy", json={"assistant_allowed": True}).status_code == 404
    client.cookies.clear()
    assert client.put(url, json={"assistant_allowed": True}).status_code == 401
