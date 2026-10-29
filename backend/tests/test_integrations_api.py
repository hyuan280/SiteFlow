from __future__ import annotations

import base64
import secrets

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from tests.helpers import login_client, make_settings


def test_unset_state(client: TestClient) -> None:
    response = client.get("/api/v1/integrations")
    assert response.status_code == 200
    body = response.json()
    assert body["proxy"]["enabled"] is False
    assert body["proxy"]["source"] == "unset"
    assert body["twofauth"]["enabled"] is False
    assert body["twofauth"]["token"]["configured"] is False


def test_proxy_db_roundtrip_and_masking(client: TestClient, settings: Settings) -> None:
    response = client.put(
        "/api/v1/integrations/proxy",
        json={"url": "socks5://user:pass@192.0.2.10:1080"},
    )
    assert response.status_code == 200
    proxy = response.json()["proxy"]
    assert proxy["enabled"] is True
    assert proxy["source"] == "db"
    assert proxy["editable"] is True
    assert proxy["url_masked"] == "socks5://user:***@192.0.2.10:1080"
    assert "pass" not in proxy["url_masked"]

    app = client.app
    session = app.state.session_factory()
    try:
        from app.models.integration import IntegrationConfig
        row = session.get(IntegrationConfig, "proxy.url")
        assert row is not None
        assert b"pass" not in row.value_ciphertext
    finally:
        session.close()


def test_cfbox_config_roundtrip(tmp_path) -> None:
    settings = make_settings(tmp_path, cfbox_url=None, cfbox_api_key=None)
    client = TestClient(create_app(settings))
    login_client(client)

    body = client.get("/api/v1/integrations").json()
    assert body["cfbox"]["enabled"] is False
    assert body["cfbox"]["url"]["source"] == "unset"
    assert body["cfbox"]["api_key"]["configured"] is False

    response = client.put(
        "/api/v1/integrations/cfbox",
        json={"url": "http://cfbox.example.com/", "api_key": "dummy-cfbox-key"},
    )
    assert response.status_code == 200, response.text
    cfbox = response.json()["cfbox"]
    assert cfbox["enabled"] is True
    assert cfbox["url"]["value"] == "http://cfbox.example.com"
    assert cfbox["url"]["source"] == "db"
    assert cfbox["api_key"]["configured"] is True
    assert cfbox["api_key"]["masked"].startswith("****")
    assert "dummy-cfbox-key" not in response.text

    response = client.delete("/api/v1/integrations/cfbox")
    assert response.status_code == 200
    assert response.json()["cfbox"]["enabled"] is False


def test_twofauth_token_never_returned(client: TestClient) -> None:
    response = client.put(
        "/api/v1/integrations/twofauth",
        json={"url": "https://2fa.example.com/", "token": "dummy-2fauth-token"},
    )
    assert response.status_code == 200
    body = response.json()["twofauth"]
    assert body["enabled"] is True
    assert body["url"]["value"] == "https://2fa.example.com"
    assert body["token"]["configured"] is True
    assert body["token"]["source"] == "db"
    assert body["token"]["editable"] is True
    assert body["token"]["masked"].startswith("****")
    assert "dummy-2fauth-token" not in response.text


def test_validation_rejects_bad_proxy(client: TestClient) -> None:
    response = client.put("/api/v1/integrations/proxy", json={"url": "ftp://host:21"})
    assert response.status_code == 422


def test_env_locks_fields(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        proxy_url="socks5://env:pass@192.0.2.11:1080",
        twofauth_url="https://2fa.env.example.com",
        twofauth_token="env-token",
    )
    client = TestClient(create_app(settings))
    login_client(client)
    response = client.get("/api/v1/integrations")
    assert response.status_code == 200
    body = response.json()
    assert body["proxy"]["source"] == "env"
    assert body["proxy"]["editable"] is False
    assert body["proxy"]["url_masked"] == "socks5://env:***@192.0.2.11:1080"
    assert body["twofauth"]["url"]["source"] == "env"
    assert body["twofauth"]["url"]["editable"] is False
    assert body["twofauth"]["token"]["source"] == "env"
    assert body["twofauth"]["token"]["editable"] is False
    assert "env-token" not in response.text
    assert body["cfbox"]["url"]["source"] == "env"
    assert body["cfbox"]["url"]["editable"] is False
    assert body["cfbox"]["api_key"]["source"] == "env"

    assert client.put("/api/v1/integrations/proxy", json={"url": "http://x:1"}).status_code == 409
    assert client.delete("/api/v1/integrations/proxy").status_code == 409
    assert client.put(
        "/api/v1/integrations/twofauth", json={"url": "https://x.example.com"}
    ).status_code == 409
    assert client.delete("/api/v1/integrations/twofauth").status_code == 409
    assert client.put(
        "/api/v1/integrations/cfbox", json={"url": "http://x:1"}
    ).status_code == 409
    assert client.delete("/api/v1/integrations/cfbox").status_code == 409


def test_delete_db_config(tmp_path) -> None:
    settings = make_settings(tmp_path)
    client = TestClient(create_app(settings))
    login_client(client)
    assert client.put(
        "/api/v1/integrations/proxy", json={"url": "http://user:pass@192.0.2.5:8080"}
    ).status_code == 200
    assert client.delete("/api/v1/integrations/proxy").status_code == 200
    body = client.get("/api/v1/integrations").json()
    assert body["proxy"]["source"] == "unset"
    assert body["proxy"]["enabled"] is False


def test_bitwarden_db_config_and_masking(
    client: TestClient, monkeypatch
) -> None:
    from app.services.bitwarden import BitwardenHealth, BitwardenService

    def fake_health(self, url: str, deep: bool = False, token=None) -> BitwardenHealth:
        return BitwardenHealth(True, True, True, "2025.1.0", "vault@example.com", None)

    monkeypatch.setattr(BitwardenService, "_request_health", fake_health)
    response = client.put(
        "/api/v1/integrations/bitwarden",
        json={
            "url": "http://bw.example.com:8090/",
            "token": "dummy-bitwarden-token",
            "enabled": True,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enabled"] is True
    assert body["enabled_setting"] is True
    assert body["enabled_source"] == "db"
    assert body["url"]["value"] == "http://bw.example.com:8090"
    assert body["url"]["source"] == "db"
    assert body["token"]["configured"] is True
    assert body["token"]["masked"].startswith("****")
    assert "dummy-bitwarden-token" not in response.text
    assert body["server_version"] == "2025.1.0"


def test_bitwarden_auto_detect_enables_when_ready(
    tmp_path, monkeypatch
) -> None:
    from app.services.bitwarden import BitwardenHealth, BitwardenService

    def fake_health(self, url: str, deep: bool = False, token=None) -> BitwardenHealth:
        assert url == "http://bitwarden:8090"
        return BitwardenHealth(True, True, True, "2025.1.0", None, None)

    from app.services.bitwarden import clear_health_cache

    monkeypatch.setattr(BitwardenService, "_request_health", fake_health)
    clear_health_cache()
    settings = make_settings(tmp_path, bitwarden_auto_detect=True)
    client = TestClient(create_app(settings))
    login_client(client)
    body = client.get("/api/v1/integrations").json()["bitwarden"]
    assert body["auto_detected"] is True
    assert body["source"] == "auto"
    assert body["enabled"] is True
    assert body["url"]["value"] == "http://bitwarden:8090"


def test_bitwarden_auto_detect_does_not_enable_unconfigured(
    tmp_path, monkeypatch
) -> None:
    from app.services.bitwarden import BitwardenHealth, BitwardenService

    def fake_health(self, url: str, deep: bool = False, token=None) -> BitwardenHealth:
        return BitwardenHealth(True, False, False, "2025.1.0", None, "未配置")

    from app.services.bitwarden import clear_health_cache

    monkeypatch.setattr(BitwardenService, "_request_health", fake_health)
    clear_health_cache()
    settings = make_settings(tmp_path, bitwarden_auto_detect=True)
    client = TestClient(create_app(settings))
    login_client(client)
    body = client.get("/api/v1/integrations").json()["bitwarden"]
    assert body["available"] is True
    assert body["configured"] is False
    assert body["enabled"] is False


def test_bitwarden_env_locks_fields(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        bitwarden_url="http://bw.env.example.com:8090",
        bitwarden_token="env-bw-token",
        bitwarden_enabled=False,
    )
    client = TestClient(create_app(settings))
    login_client(client)
    body = client.get("/api/v1/integrations").json()["bitwarden"]
    assert body["url"]["source"] == "env"
    assert body["url"]["editable"] is False
    assert body["token"]["source"] == "env"
    assert body["token"]["editable"] is False
    assert body["enabled_source"] == "env"
    assert body["enabled_setting"] is False
    assert body["enabled"] is False
    assert "env-bw-token" not in str(body)
    assert client.put(
        "/api/v1/integrations/bitwarden", json={"url": "http://x:1"}
    ).status_code == 409
    assert client.put(
        "/api/v1/integrations/bitwarden", json={"enabled": True}
    ).status_code == 409
    assert client.delete("/api/v1/integrations/bitwarden").status_code == 409


def test_bitwarden_db_disable_overrides_auto_detect(tmp_path, monkeypatch) -> None:
    from app.services.bitwarden import BitwardenHealth, BitwardenService, clear_health_cache

    def fake_health(self, url: str, deep: bool = False, token=None) -> BitwardenHealth:
        return BitwardenHealth(True, True, True, "2025.1.0", None, None)

    monkeypatch.setattr(BitwardenService, "_request_health", fake_health)
    clear_health_cache()
    settings = make_settings(tmp_path, bitwarden_auto_detect=True)
    client = TestClient(create_app(settings))
    login_client(client)
    response = client.put(
        "/api/v1/integrations/bitwarden", json={"enabled": False}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enabled"] is False
    assert body["enabled_setting"] is False
    assert body["enabled_source"] == "db"
