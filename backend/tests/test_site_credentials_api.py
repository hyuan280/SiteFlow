from __future__ import annotations

from fastapi.testclient import TestClient

from app.services.credentials import CredentialNotFoundError, CredentialService


def _create_site(client: TestClient, name: str = "credential-site") -> dict:
    response = client.post(
        "/api/v1/sites",
        json={"name": name, "base_url": "https://pt.example.com"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_credential_roundtrip_masks_and_encrypts(client: TestClient) -> None:
    site = _create_site(client)
    site_id = site["id"]
    assert site["credential"]["configured"] is False
    assert client.get(f"/api/v1/sites/{site_id}/credential").json()["configured"] is False

    response = client.put(
        f"/api/v1/sites/{site_id}/credential",
        json={"username": "alice@example.com", "password": "s3cret-pass"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["configured"] is True
    assert body["username_masked"] == "a***@example.com"
    assert "alice@example.com" not in response.text
    assert "s3cret-pass" not in response.text

    detail = client.get(f"/api/v1/sites/{site_id}").json()
    assert detail["credential"]["configured"] is True
    assert detail["credential"]["username_masked"] == "a***@example.com"
    listing = client.get("/api/v1/sites").json()
    assert listing[0]["credential"]["configured"] is True

    site_updated = client.put(
        f"/api/v1/sites/{site_id}",
        json={"name": "credential-site", "base_url": "https://pt.example.com"},
    )
    assert site_updated.status_code == 200, site_updated.text
    assert site_updated.json()["credential"]["configured"] is True

    app = client.app
    session = app.state.session_factory()
    try:
        from app.models.workflow import SiteCredential

        row = session.get(SiteCredential, site_id)
        assert row is not None
        assert b"alice" not in row.username_ciphertext
        assert b"s3cret-pass" not in row.password_ciphertext
        secret = CredentialService(session, app.state.crypto).resolve(site_id)
        assert secret.username == "alice@example.com"
        assert secret.password == "s3cret-pass"
    finally:
        session.close()


def test_credential_update_can_change_one_field(client: TestClient) -> None:
    site = _create_site(client)
    site_id = site["id"]
    assert client.put(
        f"/api/v1/sites/{site_id}/credential",
        json={"username": "alice", "password": "first-pass"},
    ).status_code == 200

    response = client.put(
        f"/api/v1/sites/{site_id}/credential",
        json={"username": "bob"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["username_masked"] == "b***b"

    app = client.app
    session = app.state.session_factory()
    try:
        secret = CredentialService(session, app.state.crypto).resolve(site_id)
        assert secret.username == "bob"
        assert secret.password == "first-pass"
    finally:
        session.close()


def test_first_credential_requires_both_fields(client: TestClient) -> None:
    site = _create_site(client)
    site_id = site["id"]
    response = client.put(
        f"/api/v1/sites/{site_id}/credential",
        json={"username": "alice"},
    )
    assert response.status_code == 422
    assert "同时" in response.json()["detail"]

    response = client.put(f"/api/v1/sites/{site_id}/credential", json={})
    assert response.status_code == 422
    assert "至少" in response.json()["detail"]


def test_credential_delete_and_site_cleanup(client: TestClient) -> None:
    site = _create_site(client)
    site_id = site["id"]
    assert client.put(
        f"/api/v1/sites/{site_id}/credential",
        json={"username": "alice", "password": "secret"},
    ).status_code == 200

    assert client.delete(f"/api/v1/sites/{site_id}/credential").status_code == 204
    assert client.get(f"/api/v1/sites/{site_id}/credential").json()["configured"] is False

    assert client.put(
        f"/api/v1/sites/{site_id}/credential",
        json={"username": "alice", "password": "secret"},
    ).status_code == 200
    assert client.delete(f"/api/v1/sites/{site_id}").status_code == 204

    app = client.app
    session = app.state.session_factory()
    try:
        from app.models.workflow import SiteCredential

        assert session.get(SiteCredential, site_id) is None
        try:
            CredentialService(session, app.state.crypto).resolve(site_id)
        except CredentialNotFoundError:
            pass
        else:
            raise AssertionError("deleted site should not resolve")
    finally:
        session.close()


def test_credential_unknown_site_and_csrf(client: TestClient, anon_client: TestClient) -> None:
    assert client.get("/api/v1/sites/999/credential").status_code == 404
    assert client.delete("/api/v1/sites/999/credential").status_code == 404
    assert anon_client.get("/api/v1/sites/1/credential").status_code == 401


def test_cookie_import_header_and_cleanup(client: TestClient) -> None:
    site = _create_site(client)
    site_id = site["id"]
    response = client.put(
        f"/api/v1/sites/{site_id}/cookies",
        json={"cookies": "uid=123; pass=abc=def; theme=dark"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cookies_configured"] is True
    assert body["cookie_count"] == 3
    assert body["configured"] is False
    assert "abc=def" not in response.text

    app = client.app
    session = app.state.session_factory()
    try:
        from app.models.workflow import SiteCredential

        row = session.get(SiteCredential, site_id)
        assert row is not None
        assert row.cookies_ciphertext is not None
        assert b"abc=def" not in row.cookies_ciphertext
        cookies = CredentialService(session, app.state.crypto).resolve_cookies(site_id)
        assert cookies is not None
        assert [(item["name"], item["value"]) for item in cookies] == [
            ("uid", "123"),
            ("pass", "abc=def"),
            ("theme", "dark"),
        ]
        assert cookies[0]["domain"] == "pt.example.com"
        assert cookies[0]["path"] == "/"
        assert cookies[0]["secure"] is True
    finally:
        session.close()

    assert client.put(
        f"/api/v1/sites/{site_id}/credential",
        json={"username": "alice", "password": "secret"},
    ).status_code == 200
    assert client.delete(f"/api/v1/sites/{site_id}/credential").status_code == 204
    after_credential = client.get(f"/api/v1/sites/{site_id}/credential").json()
    assert after_credential["configured"] is False
    assert after_credential["cookies_configured"] is True

    assert client.delete(f"/api/v1/sites/{site_id}/cookies").status_code == 204
    after_cookies = client.get(f"/api/v1/sites/{site_id}/credential").json()
    assert after_cookies["cookies_configured"] is False
    assert after_cookies["cookie_count"] == 0


def test_cookie_import_json_array_and_validation(client: TestClient) -> None:
    site = _create_site(client)
    site_id = site["id"]
    response = client.put(
        f"/api/v1/sites/{site_id}/cookies",
        json={
            "cookies": (
                '[{"name":"token","value":"xyz","domain":".example.com",'
                '"path":"/","secure":true,"httpOnly":true,"sameSite":"lax",'
                '"expirationDate":1893456000}]'
            )
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["cookie_count"] == 1

    app = client.app
    session = app.state.session_factory()
    try:
        cookies = CredentialService(session, app.state.crypto).resolve_cookies(site_id)
        assert cookies is not None
        assert cookies[0]["domain"] == ".example.com"
        assert cookies[0]["httpOnly"] is True
        assert cookies[0]["sameSite"] == "Lax"
        assert cookies[0]["expires"] == 1893456000.0
    finally:
        session.close()

    response = client.put(f"/api/v1/sites/{site_id}/cookies", json={"cookies": ";"})
    assert response.status_code == 422
    assert "Cookie" in response.json()["detail"]

    response = client.put(
        f"/api/v1/sites/{site_id}/credential",
        json={"username": "alice"},
    )
    assert response.status_code == 422
    assert "同时" in response.json()["detail"]


def test_cookie_update_does_not_change_account_timestamp(client: TestClient) -> None:
    site = _create_site(client)
    site_id = site["id"]
    account = client.put(
        f"/api/v1/sites/{site_id}/credential",
        json={"username": "alice", "password": "secret"},
    ).json()
    account_updated = account["updated_at"]

    cookie = client.put(
        f"/api/v1/sites/{site_id}/cookies",
        json={"cookies": "uid=1; pass=abc"},
    ).json()
    assert cookie["updated_at"].rstrip("Z") == account_updated.rstrip("Z")
    assert cookie["cookies_updated_at"] is not None
    assert cookie["cookies_updated_at"] != account_updated
