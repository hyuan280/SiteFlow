import pytest

from app.features.plugin_registry import clear_plugin_registry
from app.models.plugin import PluginRegistration
from app.services.assistant_categories import Denied
from app.services.assistant_feature_policy import policy_binding, set_assistant_allowed
from app.services.assistant_provider_policy import (
    authorized_providers, provider_binding, provider_policy_key,
)
from tests.helpers import login_client
from tests.test_plugin_runtime_api import HEADERS, plugin_client, register_payload


BASE = "/api/v1/plugin-runtime"
PLUGIN = f"{BASE}/plugins/acme"
CREDENTIALS = "secret.credentials"
TOTP = "secret.totp"
PROVIDER = "plugin.acme"


@pytest.fixture
def client(tmp_path):
    clear_plugin_registry()
    client = plugin_client(tmp_path)
    payload = register_payload()
    payload["manifest"]["capabilities"] = [CREDENTIALS, TOTP]
    response = client.post(f"{BASE}/register", json=payload, headers=HEADERS)
    assert response.status_code == 201, response.text
    login_client(client)
    yield client
    clear_plugin_registry()


def revision(body):
    return {key: body[key] for key in ("generation", "permission_epoch")}


def save(client, body, permissions):
    response = client.put(f"{PLUGIN}/feature-categories", json={
        **revision(body), "assistant_permissions": permissions,
    })
    assert response.status_code == 200, response.text
    return response.json()


def test_declared_unapproved_metadata_and_independent_permissions(client):
    original = client.get(PLUGIN).json()
    providers = original["secret_providers"]
    assert len(providers) == 2
    for item in providers:
        category = "auth" if item["capability"] == CREDENTIALS else "challenge"
        assert item == {
            "key": provider_policy_key(PROVIDER, item["capability"]),
            "name": "Acme Plugin", "provider_id": PROVIDER, "capability": item["capability"],
            "declared_category": category, "category": category, "category_overridden": False,
            "risk_index": 5, "risk_label": "极高风险：秘密访问",
            "assistant_allowed": False, "assistant_policy_revision": 0,
        }
    key = provider_policy_key(PROVIDER, CREDENTIALS)
    assert key == "plugin.acme.secret.credentials"
    assert provider_policy_key(PROVIDER, TOTP) == "plugin.acme.secret.totp"
    changed = save(client, original, {key: True})
    assert changed["approved_at"] is None
    assert changed["permission_epoch"] == original["permission_epoch"] + 1
    with client.app.state.session_factory() as db:
        assert policy_binding(db, key) == {"allowed": True, "revision": 1}
        assert authorized_providers(db, CREDENTIALS) == []
        with pytest.raises(Denied):
            provider_binding(db, PROVIDER, CREDENTIALS)
    approved = client.post(f"{PLUGIN}/approve", json=revision(changed)).json()
    with client.app.state.session_factory() as db:
        assert provider_binding(db, PROVIDER, CREDENTIALS) == {
            "policy": {"allowed": True, "revision": 1}, **revision(approved),
        }
        assert authorized_providers(db, CREDENTIALS) == [
            {"id": PROVIDER, "name": "Acme Plugin", "capability": CREDENTIALS},
        ]
        assert authorized_providers(db, TOTP) == []
        with pytest.raises(Denied):
            provider_binding(db, PROVIDER, TOTP)
    totp_key = provider_policy_key(PROVIDER, TOTP)
    changed = save(client, approved, {key: False, totp_key: True})
    assert changed["generation"] == approved["generation"]
    assert changed["permission_epoch"] > approved["permission_epoch"]
    assert changed["approved_at"].rstrip("Z") == approved["approved_at"].rstrip("Z")
    for field in ("approved_by", "approved_capabilities", "status"):
        assert changed[field] == approved[field]
    unchanged = save(client, changed, {key: False, totp_key: True})
    assert revision(unchanged) == revision(changed)
    with client.app.state.session_factory() as db:
        assert authorized_providers(db, CREDENTIALS) == []
        assert len(authorized_providers(db, TOTP)) == 1
    assert client.put(f"{PLUGIN}/feature-categories", json={
        **revision(approved), "assistant_permissions": {key: True},
    }).status_code == 409


@pytest.mark.parametrize("operation", ["revoke", "disable", "remove"])
def test_current_approval_revocation_and_safe_discovery(client, operation):
    key = provider_policy_key(PROVIDER, CREDENTIALS)
    original = client.get(PLUGIN).json()
    changed = save(client, original, {key: True})
    approved = client.post(f"{PLUGIN}/approve", json=revision(changed)).json()
    with client.app.state.session_factory() as db:
        before = provider_binding(db, PROVIDER, CREDENTIALS)
        response = client.post(f"{PLUGIN}/{operation}")
        assert response.status_code == 200, response.text
        with pytest.raises(Denied):
            provider_binding(db, PROVIDER, CREDENTIALS)
        assert authorized_providers(db, CREDENTIALS) == []
        assert before["permission_epoch"] == approved["permission_epoch"]
    assert all(item["id"] != PROVIDER for item in client.get("/api/v1/secret-providers").json())


def test_catalog_general_availability_and_safe_permission_fields(client):
    assert all(item["id"] != PROVIDER for item in client.get("/api/v1/secret-providers").json())
    approved = client.post(f"{PLUGIN}/approve", json={}).json()
    key = provider_policy_key(PROVIDER, CREDENTIALS)
    save(client, approved, {key: True})
    response = client.get("/api/v1/secret-providers")
    assert response.status_code == 200
    rows = response.json()
    assert len([item for item in rows if item["id"].startswith("builtin.")]) == 3
    remote = [item for item in rows if item["id"] == PROVIDER]
    assert remote == [{
        "id": PROVIDER, "name": "Acme Plugin", "capability": capability,
        "assistant_allowed": capability == CREDENTIALS,
        "assistant_policy_revision": 1 if capability == CREDENTIALS else 0,
    } for capability in (CREDENTIALS, TOTP)]
    assert all(set(item) == {"id", "name", "capability", "assistant_allowed",
                            "assistant_policy_revision"} for item in remote)
    for sensitive in ("acme-token", "plugin-acme:7000", "password", "endpoint", "username"):
        assert sensitive not in response.text
    client.cookies.clear()
    assert client.get("/api/v1/secret-providers").status_code == 401


def test_scope_validation_and_transactional_setter(client):
    original = client.get(PLUGIN).json()
    for key in ("plugin.other.secret.credentials", "plugin.acme.secret.unknown"):
        assert client.put(f"{PLUGIN}/feature-categories", json={
            **revision(original), "assistant_permissions": {key: True},
        }).status_code == 422
        with client.app.state.session_factory() as db:
            with pytest.raises(ValueError):
                set_assistant_allowed(db, key, True)
    key = provider_policy_key(PROVIDER, CREDENTIALS)
    assert client.put(f"{PLUGIN}/feature-categories", json={
        **revision(original), "categories": {key: "flow"},
    }).status_code == 422
    assert client.put(f"{PLUGIN}/feature-categories", json={
        **revision(original), "assistant_permissions": {key: "true"},
    }).status_code == 422
    with client.app.state.session_factory() as db:
        set_assistant_allowed(db, key, True)
        assert db.get(PluginRegistration, "acme").permission_epoch > original["permission_epoch"]
        db.rollback()
        assert policy_binding(db, key) == {"allowed": False, "revision": 0}
        assert db.get(PluginRegistration, "acme").permission_epoch == original["permission_epoch"]
    csrf = client.headers.pop("X-CSRF-Token")
    assert client.put(f"{PLUGIN}/feature-categories", json={
        **revision(original), "assistant_permissions": {key: True},
    }).status_code == 403
    client.headers["X-CSRF-Token"] = csrf


def test_undeclared_capability_cannot_be_authorized(client):
    payload = register_payload()
    payload["manifest"]["capabilities"] = [CREDENTIALS]
    response = client.post(f"{BASE}/register", json=payload, headers=HEADERS)
    assert response.status_code == 201, response.text
    current = response.json()
    assert [item["capability"] for item in current["secret_providers"]] == [CREDENTIALS]
    totp_key = provider_policy_key(PROVIDER, TOTP)
    assert client.put(f"{PLUGIN}/feature-categories", json={
        **revision(current), "assistant_permissions": {totp_key: True},
    }).status_code == 422
    with client.app.state.session_factory() as db:
        with pytest.raises(ValueError):
            set_assistant_allowed(db, totp_key, True)
        with pytest.raises(Denied):
            provider_binding(db, PROVIDER, TOTP)
        assert authorized_providers(db, TOTP) == []
