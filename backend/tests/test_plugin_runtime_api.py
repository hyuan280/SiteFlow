from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from app.main import create_app
from app.models.plugin import utcnow
from tests.helpers import login_client, make_settings

TOKENS = '{"acme":"acme-token","vault":"vault-token"}'
HEADERS = {"X-Plugin-Token": "acme-token"}


def plugin_client(tmp_path, **overrides) -> TestClient:
    values = {
        "plugin_runtime_tokens": TOKENS,
        "plugin_verify_endpoint": False,
        "plugin_resolve_endpoint": False,
    }
    values.update(overrides)
    settings = make_settings(tmp_path, **values)
    return TestClient(create_app(settings))


def register_payload(**overrides) -> dict:
    payload = {
        "plugin_id": "acme",
        "protocol_version": 1,
        "endpoint": "plugin-acme:7000",
        "manifest": {
            "id": "acme",
            "name": "Acme Plugin",
            "version": "1.0.0",
            "protocol_version": 1,
            "entrypoint": "plugin:register",
            "capabilities": ["features", "captcha_methods"],
        },
    }
    payload.update(overrides)
    return payload


def test_provider_catalog_tracks_explicit_approval(tmp_path):
    client = plugin_client(tmp_path)
    assert client.get("/api/v1/secret-providers").status_code == 401
    login_client(client)
    payload = register_payload()
    payload["manifest"]["capabilities"] = ["secret.credentials", "secret.totp"]
    assert client.post("/api/v1/plugin-runtime/register", json=payload, headers=HEADERS).status_code == 201
    def remote():
        response = client.get("/api/v1/secret-providers")
        assert "acme-token" not in response.text and "endpoint" not in response.text
        return [item for item in response.json() if item["id"] == "plugin.acme"]
    assert remote() == []
    assert client.post("/api/v1/plugin-runtime/plugins/acme/approve", json={}).status_code == 200
    assert {item["capability"] for item in remote()} == {"secret.credentials", "secret.totp"}
    assert client.post("/api/v1/plugin-runtime/plugins/acme/disable").status_code == 200
    assert remote() == []


def test_register_and_list_plugin(tmp_path) -> None:
    client = plugin_client(tmp_path)
    response = client.post(
        "/api/v1/plugin-runtime/register", json=register_payload(), headers=HEADERS
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["plugin_id"] == "acme"
    assert body["status"] == "unapproved"
    assert body["generation"] == 1
    assert body["endpoint"] == "plugin-acme:7000"
    assert body["capabilities"] == ["captcha_methods", "features"]
    assert body["approved_capabilities"] == []
    assert body["approved_at"] is None
    assert body["feature_count"] == 0
    assert body["captcha_method_count"] == 0
    assert {item["key"] for item in body["checks"]} == {
        "protocol",
        "endpoint",
        "capabilities",
        "health",
        "manifest",
        "features",
        "captcha_methods",
    }
    assert all(item["ok"] for item in body["checks"])

    assert client.get("/api/v1/plugin-runtime/plugins").status_code == 401
    login_client(client)
    listed = client.get("/api/v1/plugin-runtime/plugins")
    assert listed.status_code == 200
    assert [item["plugin_id"] for item in listed.json()] == ["acme"]
    assert listed.json()[0]["status"] == "unapproved"

    approved = client.post(
        "/api/v1/plugin-runtime/plugins/acme/approve",
        json={},
    )
    assert approved.status_code == 200, approved.text
    approved_body = approved.json()
    assert approved_body["status"] == "available"
    assert approved_body["approved_capabilities"] == ["captcha_methods", "features"]
    assert approved_body["approved_by"] == "admin"
    assert approved_body["approved_at"] is not None

    session = client.app.state.session_factory()
    try:
        from app.models.plugin import PluginRegistration

        row = session.get(PluginRegistration, "acme")
        assert row is not None
        assert row.version == "1.0.0"
        assert "acme-token" not in row.manifest_json
    finally:
        session.close()


def test_register_requires_matching_token(tmp_path) -> None:
    client = plugin_client(tmp_path)
    response = client.post("/api/v1/plugin-runtime/register", json=register_payload())
    assert response.status_code == 401
    response = client.post(
        "/api/v1/plugin-runtime/register",
        json=register_payload(),
        headers={"X-Plugin-Token": "wrong"},
    )
    assert response.status_code == 403
    response = client.post(
        "/api/v1/plugin-runtime/register",
        json=register_payload(plugin_id="unknown"),
        headers=HEADERS,
    )
    assert response.status_code == 403


def test_register_bearer_token(tmp_path) -> None:
    client = plugin_client(tmp_path)
    response = client.post(
        "/api/v1/plugin-runtime/register",
        json=register_payload(),
        headers={"Authorization": "Bearer acme-token"},
    )
    assert response.status_code == 201, response.text


def test_register_validates_manifest_and_protocol(tmp_path) -> None:
    client = plugin_client(tmp_path)
    payload = register_payload()
    payload["manifest"]["id"] = "other"
    response = client.post("/api/v1/plugin-runtime/register", json=payload, headers=HEADERS)
    assert response.status_code == 422

    payload = register_payload(protocol_version=2)
    payload["manifest"]["protocol_version"] = 2
    response = client.post("/api/v1/plugin-runtime/register", json=payload, headers=HEADERS)
    assert response.status_code == 422

    payload = register_payload()
    payload["manifest"]["protocol_version"] = 2
    response = client.post("/api/v1/plugin-runtime/register", json=payload, headers=HEADERS)
    assert response.status_code == 422

    payload = register_payload()
    payload["manifest"]["capabilities"] = ["unknown_capability"]
    response = client.post("/api/v1/plugin-runtime/register", json=payload, headers=HEADERS)
    assert response.status_code == 422


def test_register_validates_endpoint_network(tmp_path) -> None:
    client = plugin_client(tmp_path)
    response = client.post(
        "/api/v1/plugin-runtime/register",
        json=register_payload(endpoint="http://plugin-acme:7000"),
        headers=HEADERS,
    )
    assert response.status_code == 422
    response = client.post(
        "/api/v1/plugin-runtime/register",
        json=register_payload(endpoint="8.8.8.8:7000"),
        headers=HEADERS,
    )
    assert response.status_code == 422
    response = client.post(
        "/api/v1/plugin-runtime/register",
        json=register_payload(endpoint="127.0.0.1:7000"),
        headers=HEADERS,
    )
    assert response.status_code == 422
    response = client.post(
        "/api/v1/plugin-runtime/register",
        json=register_payload(endpoint="plugin-acme:7000/plugin"),
        headers=HEADERS,
    )
    assert response.status_code == 422


def test_endpoint_resolution_blocks_public_address(tmp_path, monkeypatch) -> None:
    def fake_getaddrinfo(host, port, type):  # type: ignore[no-untyped-def]
        assert host == "plugin-evil"
        return [(2, 1, 6, "", ("8.8.8.8", port))]

    monkeypatch.setattr("app.services.plugin_runtime.socket.getaddrinfo", fake_getaddrinfo)
    client = plugin_client(tmp_path, plugin_resolve_endpoint=True)
    response = client.post(
        "/api/v1/plugin-runtime/register",
        json=register_payload(endpoint="plugin-evil:7000"),
        headers=HEADERS,
    )
    assert response.status_code == 422


def test_endpoint_allowlist(tmp_path) -> None:
    client = plugin_client(tmp_path, plugin_endpoint_allowlist="127.0.0.1")
    response = client.post(
        "/api/v1/plugin-runtime/register",
        json=register_payload(endpoint="127.0.0.1:7000"),
        headers=HEADERS,
    )
    assert response.status_code == 201, response.text


def test_register_accepts_grpc_scheme(tmp_path) -> None:
    client = plugin_client(tmp_path)
    response = client.post(
        "/api/v1/plugin-runtime/register",
        json=register_payload(endpoint="grpc://plugin-acme:7000"),
        headers=HEADERS,
    )
    assert response.status_code == 201, response.text
    assert response.json()["endpoint"] == "plugin-acme:7000"


def test_register_health_check(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, float]] = []

    from app.schemas.plugin_runtime import PluginManifest

    def fake_health(endpoint: str, token: str, timeout: float) -> None:
        calls.append((endpoint, token, timeout))

    def fake_manifest(endpoint: str, token: str, timeout: float) -> PluginManifest:
        calls.append((endpoint, token, timeout))
        return PluginManifest.model_validate(register_payload()["manifest"])

    monkeypatch.setattr("app.services.plugin_runtime.verify_plugin_health", fake_health)
    monkeypatch.setattr("app.services.plugin_runtime.fetch_plugin_manifest", fake_manifest)
    monkeypatch.setattr(
        "app.services.plugin_runtime.fetch_plugin_features",
        lambda endpoint, token, timeout: [],
    )
    monkeypatch.setattr(
        "app.services.plugin_runtime.fetch_plugin_captcha_methods",
        lambda endpoint, token, timeout: [],
    )
    settings = make_settings(
        tmp_path,
        plugin_runtime_tokens=TOKENS,
        plugin_verify_endpoint=True,
        plugin_resolve_endpoint=False,
    )
    client = TestClient(create_app(settings))
    response = client.post(
        "/api/v1/plugin-runtime/register", json=register_payload(), headers=HEADERS
    )
    assert response.status_code == 201, response.text
    assert calls == [
        ("plugin-acme:7000", "acme-token", 3.0),
        ("plugin-acme:7000", "acme-token", 3.0),
    ]

    from app.services.plugin_runtime import PluginUnreachableError

    def broken_health(endpoint: str, token: str, timeout: float) -> None:
        raise PluginUnreachableError("offline")

    monkeypatch.setattr("app.services.plugin_runtime.verify_plugin_health", broken_health)
    payload = register_payload(plugin_id="vault")
    payload["manifest"]["id"] = "vault"
    response = client.post(
        "/api/v1/plugin-runtime/register",
        json=payload,
        headers={"X-Plugin-Token": "vault-token"},
    )
    assert response.status_code == 502


def test_register_rejects_mismatched_remote_manifest(tmp_path, monkeypatch) -> None:
    from app.schemas.plugin_runtime import PluginManifest

    def fake_health(endpoint: str, token: str, timeout: float) -> None:
        return None

    def fake_manifest(endpoint: str, token: str, timeout: float) -> PluginManifest:
        payload = register_payload()["manifest"]
        payload["version"] = "9.9.9"
        return PluginManifest.model_validate(payload)

    monkeypatch.setattr("app.services.plugin_runtime.verify_plugin_health", fake_health)
    monkeypatch.setattr("app.services.plugin_runtime.fetch_plugin_manifest", fake_manifest)
    settings = make_settings(
        tmp_path,
        plugin_runtime_tokens=TOKENS,
        plugin_verify_endpoint=True,
        plugin_resolve_endpoint=False,
    )
    client = TestClient(create_app(settings))
    response = client.post(
        "/api/v1/plugin-runtime/register", json=register_payload(), headers=HEADERS
    )
    assert response.status_code == 422


def test_reregister_metadata_change_requires_reapproval(tmp_path) -> None:
    client = plugin_client(tmp_path)
    first = client.post(
        "/api/v1/plugin-runtime/register", json=register_payload(), headers=HEADERS
    )
    assert first.status_code == 201
    login_client(client)
    assert client.post(
        "/api/v1/plugin-runtime/plugins/acme/approve", json={}
    ).status_code == 200

    payload = register_payload(endpoint="plugin-acme-v2:7000")
    payload["manifest"]["version"] = "1.1.0"
    response = client.post("/api/v1/plugin-runtime/register", json=payload, headers=HEADERS)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["generation"] == 2
    assert body["version"] == "1.1.0"
    assert body["status"] == "unapproved"
    assert body["approved_capabilities"] == []


def test_reregister_capability_change_requires_reapproval(tmp_path) -> None:
    client = plugin_client(tmp_path)
    first = client.post(
        "/api/v1/plugin-runtime/register", json=register_payload(), headers=HEADERS
    )
    assert first.status_code == 201
    login_client(client)
    assert client.post(
        "/api/v1/plugin-runtime/plugins/acme/approve", json={}
    ).status_code == 200

    payload = register_payload()
    payload["manifest"]["capabilities"] = [
        "features",
        "captcha_methods",
        "browser_actions",
    ]
    response = client.post("/api/v1/plugin-runtime/register", json=payload, headers=HEADERS)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "unapproved"
    assert body["approved_capabilities"] == []


def test_heartbeat_and_stale_status(tmp_path) -> None:
    client = plugin_client(tmp_path, plugin_heartbeat_ttl_seconds=60)
    registered = client.post(
        "/api/v1/plugin-runtime/register", json=register_payload(), headers=HEADERS
    ).json()

    response = client.post(
        "/api/v1/plugin-runtime/heartbeat",
        json={"plugin_id": "acme", "generation": registered["generation"]},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "unapproved"

    login_client(client)
    assert client.post(
        "/api/v1/plugin-runtime/plugins/acme/approve", json={}
    ).status_code == 200
    response = client.post(
        "/api/v1/plugin-runtime/heartbeat",
        json={"plugin_id": "acme", "generation": registered["generation"]},
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "available"

    response = client.post(
        "/api/v1/plugin-runtime/heartbeat",
        json={"plugin_id": "acme", "generation": 99},
        headers=HEADERS,
    )
    assert response.status_code == 409

    session = client.app.state.session_factory()
    try:
        from app.models.plugin import PluginRegistration

        row = session.get(PluginRegistration, "acme")
        assert row is not None
        row.last_heartbeat_at = utcnow() - timedelta(seconds=120)
        session.commit()
    finally:
        session.close()

    listed = client.get("/api/v1/plugin-runtime/plugins").json()
    assert listed[0]["status"] == "unhealthy"

    response = client.post(
        "/api/v1/plugin-runtime/heartbeat",
        json={"plugin_id": "acme"},
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "available"


def test_heartbeat_unknown_plugin(tmp_path) -> None:
    client = plugin_client(tmp_path)
    response = client.post(
        "/api/v1/plugin-runtime/heartbeat",
        json={"plugin_id": "acme"},
        headers=HEADERS,
    )
    assert response.status_code == 404


def test_high_trust_plugin_requires_approval(tmp_path) -> None:
    client = plugin_client(tmp_path)
    payload = register_payload(plugin_id="vault")
    payload["manifest"].update(
        {"id": "vault", "name": "Vault", "capabilities": ["secret.totp"]}
    )
    response = client.post(
        "/api/v1/plugin-runtime/register",
        json=payload,
        headers={"X-Plugin-Token": "vault-token"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "unapproved"


def test_approve_partial_and_invalid_capabilities(tmp_path) -> None:
    client = plugin_client(tmp_path)
    assert client.post(
        "/api/v1/plugin-runtime/register", json=register_payload(), headers=HEADERS
    ).status_code == 201
    login_client(client)
    partial = client.post(
        "/api/v1/plugin-runtime/plugins/acme/approve",
        json={"capabilities": ["features"]},
    )
    assert partial.status_code == 200, partial.text
    assert partial.json()["status"] == "unapproved"
    assert partial.json()["approved_capabilities"] == ["features"]

    invalid = client.post(
        "/api/v1/plugin-runtime/plugins/acme/approve",
        json={"capabilities": ["secret.totp"]},
    )
    assert invalid.status_code == 422

    complete = client.post(
        "/api/v1/plugin-runtime/plugins/acme/approve", json={}
    )
    assert complete.status_code == 200, complete.text
    assert complete.json()["status"] == "available"


def test_approve_requires_authenticated_admin(tmp_path) -> None:
    client = plugin_client(tmp_path)
    assert client.post(
        "/api/v1/plugin-runtime/register", json=register_payload(), headers=HEADERS
    ).status_code == 201
    response = client.post(
        "/api/v1/plugin-runtime/plugins/acme/approve", json={}
    )
    assert response.status_code == 401


def test_check_refreshes_checks(tmp_path, monkeypatch) -> None:
    from app.schemas.plugin_runtime import PluginManifest

    client = plugin_client(tmp_path)
    registered = client.post(
        "/api/v1/plugin-runtime/register", json=register_payload(), headers=HEADERS
    ).json()
    assert next(
        item for item in registered["checks"] if item["key"] == "health"
    )["skipped"] is True
    login_client(client)

    def fake_health(endpoint: str, token: str, timeout: float) -> None:
        assert endpoint == "plugin-acme:7000"
        assert token == "acme-token"

    def fake_manifest(endpoint: str, token: str, timeout: float) -> PluginManifest:
        return PluginManifest.model_validate(register_payload()["manifest"])

    monkeypatch.setattr("app.services.plugin_runtime.verify_plugin_health", fake_health)
    monkeypatch.setattr("app.services.plugin_runtime.fetch_plugin_manifest", fake_manifest)
    response = client.post("/api/v1/plugin-runtime/plugins/acme/check")
    assert response.status_code == 200, response.text
    checks = {item["key"]: item for item in response.json()["checks"]}
    assert checks["runtime"]["ok"] is True
    assert checks["health"]["ok"] is True
    assert checks["health"]["skipped"] is False
    assert checks["manifest"]["ok"] is True


def test_disable_keeps_connection_but_blocks_integration(tmp_path) -> None:
    client = plugin_client(tmp_path)
    registered = client.post(
        "/api/v1/plugin-runtime/register", json=register_payload(), headers=HEADERS
    ).json()
    login_client(client)
    assert client.post(
        "/api/v1/plugin-runtime/plugins/acme/approve", json={}
    ).status_code == 200

    disabled = client.post("/api/v1/plugin-runtime/plugins/acme/disable")
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["status"] == "disabled"
    assert disabled.json()["enabled"] is False

    heartbeat = client.post(
        "/api/v1/plugin-runtime/heartbeat",
        json={"plugin_id": "acme", "generation": registered["generation"]},
        headers=HEADERS,
    )
    assert heartbeat.status_code == 200, heartbeat.text
    assert heartbeat.json()["status"] == "disabled"

    registered_again = client.post(
        "/api/v1/plugin-runtime/register", json=register_payload(), headers=HEADERS
    )
    assert registered_again.status_code == 201, registered_again.text
    assert registered_again.json()["status"] == "disabled"
    assert registered_again.json()["generation"] == 2
    assert registered_again.json()["approved_capabilities"] == [
        "captcha_methods",
        "features",
    ]

    enabled = client.post("/api/v1/plugin-runtime/plugins/acme/enable")
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["status"] == "available"
    assert enabled.json()["enabled"] is True


def test_enable_unapproved_plugin_stays_pending(tmp_path) -> None:
    client = plugin_client(tmp_path)
    assert client.post(
        "/api/v1/plugin-runtime/register", json=register_payload(), headers=HEADERS
    ).status_code == 201
    login_client(client)
    assert client.post(
        "/api/v1/plugin-runtime/plugins/acme/disable"
    ).json()["status"] == "disabled"
    response = client.post("/api/v1/plugin-runtime/plugins/acme/enable")
    assert response.status_code == 200
    assert response.json()["status"] == "unapproved"


def test_unregister_keeps_approval_for_reconnect(tmp_path) -> None:
    client = plugin_client(tmp_path)
    registered = client.post(
        "/api/v1/plugin-runtime/register", json=register_payload(), headers=HEADERS
    ).json()
    login_client(client)
    assert client.post(
        "/api/v1/plugin-runtime/plugins/acme/approve", json={}
    ).status_code == 200
    response = client.post(
        "/api/v1/plugin-runtime/unregister",
        json={"plugin_id": "acme", "generation": registered["generation"]},
        headers=HEADERS,
    )
    assert response.status_code == 204
    listed = client.get("/api/v1/plugin-runtime/plugins").json()
    assert [item["plugin_id"] for item in listed] == ["acme"]
    assert listed[0]["status"] == "unhealthy"

    reconnected = client.post(
        "/api/v1/plugin-runtime/register", json=register_payload(), headers=HEADERS
    )
    assert reconnected.status_code == 201, reconnected.text
    assert reconnected.json()["status"] == "available"
    assert reconnected.json()["generation"] == 2
    assert reconnected.json()["approved_capabilities"] == [
        "captcha_methods",
        "features",
    ]


def test_plugin_features_and_captcha_methods_are_injected_after_approval(
    tmp_path, monkeypatch
) -> None:
    from app.schemas.plugin_runtime import PluginManifest

    features = [
        {
            "key": "plugin.acme.checkin",
            "name": "Acme 示例签到",
            "category": "sign",
            "cardinality": "multiple",
            "version": 1,
            "description": "示例插件签到功能",
            "config_schema": {
                "type": "object",
                "properties": {
                    "button_selector": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": ["#sign"],
                    },
                    "message": {"type": "string", "default": ""},
                },
                "required": ["button_selector"],
                "additionalProperties": False,
            },
            "requires": [],
        }
    ]
    methods = [
        {
            "key": "plugin.acme.gd5",
            "label": "Acme GD5",
            "description": "Acme 示例验证码",
            "supports_vision": False,
        }
    ]

    def fake_health(endpoint: str, token: str, timeout: float) -> None:
        return None

    def fake_manifest(endpoint: str, token: str, timeout: float) -> PluginManifest:
        return PluginManifest.model_validate(register_payload()["manifest"])

    monkeypatch.setattr("app.services.plugin_runtime.verify_plugin_health", fake_health)
    monkeypatch.setattr("app.services.plugin_runtime.fetch_plugin_manifest", fake_manifest)
    monkeypatch.setattr(
        "app.services.plugin_runtime.fetch_plugin_features",
        lambda endpoint, token, timeout: features,
    )
    monkeypatch.setattr(
        "app.services.plugin_runtime.fetch_plugin_captcha_methods",
        lambda endpoint, token, timeout: methods,
    )
    settings = make_settings(
        tmp_path,
        plugin_runtime_tokens=TOKENS,
        plugin_verify_endpoint=True,
        plugin_resolve_endpoint=False,
        plugin_endpoint_allowlist="plugin-acme",
    )
    client = TestClient(create_app(settings))
    registered = client.post(
        "/api/v1/plugin-runtime/register", json=register_payload(), headers=HEADERS
    )
    assert registered.status_code == 201, registered.text
    assert registered.json()["feature_count"] == 1
    assert registered.json()["captcha_method_count"] == 1
    login_client(client)

    before = client.get("/api/v1/features").json()
    assert all(item["key"] != "plugin.acme.checkin" for item in before)
    captcha_before = next(
        item for item in before if item["key"] == "challenge.image_captcha"
    )
    assert "plugin.acme.gd5" not in captcha_before["config_schema"]["properties"][
        "captcha_method"
    ]["enum"]

    approved = client.post("/api/v1/plugin-runtime/plugins/acme/approve", json={})
    assert approved.status_code == 200, approved.text
    after = client.get("/api/v1/features").json()
    plugin_feature = next(
        item for item in after if item["key"] == "plugin.acme.checkin"
    )
    assert plugin_feature["source"] == "acme"
    assert plugin_feature["defaults"] == {"button_selector": ["#sign"], "message": ""}
    assert plugin_feature["required_fields"] == ["button_selector"]
    captcha_after = next(
        item for item in after if item["key"] == "challenge.image_captcha"
    )
    method_property = captcha_after["config_schema"]["properties"]["captcha_method"]
    assert "plugin.acme.gd5" in method_property["enum"]
    assert method_property["enum_titles"]["plugin.acme.gd5"] == "Acme GD5"
    assert method_property["enum_descriptions"]["plugin.acme.gd5"] == "Acme 示例验证码"

    from app.features.base import FeatureConfigError
    from app.features.catalog import get_feature

    registered_feature = get_feature("plugin.acme.checkin")
    assert registered_feature is not None
    assert registered_feature.validate_full(
        {"button_selector": ["#sign"], "message": "ok"}
    ) == {"button_selector": ["#sign"], "message": "ok"}
    try:
        registered_feature.validate_full({"message": "missing selector"})
    except FeatureConfigError:
        pass
    else:
        raise AssertionError("expected plugin feature validation to fail")

    updated = client.put(
        "/api/v1/features/plugin.acme.checkin/settings",
        json={
            "defaults": {"button_selector": ["#sign"], "message": "updated"},
            "required_fields": [],
            "execution_settings": None,
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["defaults"] == {
        "button_selector": ["#sign"],
        "message": "updated",
    }
    assert updated.json()["required_fields"] == []

    assert client.post(
        "/api/v1/plugin-runtime/plugins/acme/disable"
    ).status_code == 200
    disabled = client.get("/api/v1/features").json()
    assert all(item["key"] != "plugin.acme.checkin" for item in disabled)
    disabled_captcha = next(
        item for item in disabled if item["key"] == "challenge.image_captcha"
    )
    assert "plugin.acme.gd5" not in disabled_captcha["config_schema"]["properties"][
        "captcha_method"
    ]["enum"]

    assert client.post(
        "/api/v1/plugin-runtime/plugins/acme/enable"
    ).status_code == 200
    enabled = client.get("/api/v1/features").json()
    assert any(item["key"] == "plugin.acme.checkin" for item in enabled)


def test_invalid_token_configuration(tmp_path) -> None:
    client = plugin_client(tmp_path, plugin_runtime_tokens="{not-json")
    response = client.post(
        "/api/v1/plugin-runtime/register", json=register_payload(), headers=HEADERS
    )
    assert response.status_code == 503


def test_runtime_for_plugin_requires_approval_and_enabled(tmp_path) -> None:
    from app.services.plugin_runtime import (
        PluginRuntimeService,
        PluginValidationError,
    )

    client = plugin_client(tmp_path)
    assert client.post(
        "/api/v1/plugin-runtime/register", json=register_payload(), headers=HEADERS
    ).status_code == 201
    session = client.app.state.session_factory()
    try:
        service = PluginRuntimeService(session, client.app.state.settings)
        try:
            service.runtime_for_plugin("acme")
        except PluginValidationError:
            pass
        else:
            raise AssertionError("expected unapproved plugin runtime to be rejected")

        login_client(client)
        assert client.post(
            "/api/v1/plugin-runtime/plugins/acme/approve", json={}
        ).status_code == 200
        runtime = service.runtime_for_plugin("acme")
        assert runtime == {
            "plugin_id": "acme",
            "endpoint": "plugin-acme:7000",
            "token": "acme-token",
            "generation": 1,
            "permission_epoch": 2,
            "approved_capabilities": ["captcha_methods", "features"],
        }

        assert client.post(
            "/api/v1/plugin-runtime/plugins/acme/disable"
        ).status_code == 200
        try:
            service.runtime_for_plugin("acme")
        except PluginValidationError:
            pass
        else:
            raise AssertionError("expected disabled plugin runtime to be rejected")
    finally:
        session.close()
