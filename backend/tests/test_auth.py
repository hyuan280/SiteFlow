from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.models.auth import User
from tests.helpers import login_client, make_settings


def _bootstrap_password(output: str) -> str:
    match = re.search(r"^password: (\S+)$", output, re.MULTILINE)
    assert match is not None, output
    return match.group(1)


def test_env_login_protection_and_csrf(anon_client: TestClient) -> None:
    assert anon_client.get("/api/v1/integrations").status_code == 401
    assert anon_client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "wrong"}
    ).status_code == 401

    response = anon_client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "test-password"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["auth_source"] == "env"
    assert body["user"]["can_change_password"] is False
    csrf = body["csrf_token"]
    assert csrf

    me = anon_client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["username"] == "admin"

    # Mutations require the CSRF header.
    assert anon_client.put(
        "/api/v1/integrations/proxy", json={"url": "http://user:pass@192.0.2.5:8080"}
    ).status_code == 403
    anon_client.headers.update({"X-CSRF-Token": csrf})
    assert anon_client.put(
        "/api/v1/integrations/proxy", json={"url": "http://user:pass@192.0.2.5:8080"}
    ).status_code == 200

    assert anon_client.post("/api/v1/auth/logout").status_code == 200
    assert anon_client.get("/api/v1/integrations").status_code == 401


def test_env_admin_cannot_change_password(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/password",
        json={"current_password": "test-password", "new_password": "new-password-123"},
    )
    assert response.status_code == 403


def test_login_rate_limit(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        login_max_attempts=2,
        login_lockout_seconds=60,
    )
    client = TestClient(create_app(settings))
    for _ in range(2):
        assert client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "bad"}
        ).status_code == 401
    response = client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "bad"}
    )
    assert response.status_code == 429
    assert response.headers.get("retry-after")


def test_bootstrap_password_persists_to_database(tmp_path, capsys) -> None:
    settings = make_settings(tmp_path, admin_username=None, admin_password=None)
    app = create_app(settings)
    client = TestClient(app)
    password = _bootstrap_password(capsys.readouterr().out)
    assert app.state.bootstrap.mode == "bootstrap"

    response = client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": password}
    )
    assert response.status_code == 200
    assert response.json()["user"]["auth_source"] == "bootstrap"
    csrf = response.json()["csrf_token"]
    client.headers.update({"X-CSRF-Token": csrf})

    changed = client.post(
        "/api/v1/auth/password",
        json={"current_password": password, "new_password": "persisted-password-123"},
    )
    assert changed.status_code == 200
    assert changed.json()["user"]["auth_source"] == "db"
    new_csrf = changed.json()["csrf_token"]
    client.headers.update({"X-CSRF-Token": new_csrf})

    with app.state.session_factory() as session:
        user = session.query(User).first()
        assert user is not None
        assert user.password_hash != "persisted-password-123"
        assert user.password_hash.startswith("$argon2")

    # A restart must use the database password and must not print a new bootstrap password.
    app2 = create_app(settings)
    assert app2.state.bootstrap.mode == "db"
    assert "SiteFlow initial admin" not in capsys.readouterr().out
    client2 = TestClient(app2)
    assert client2.post(
        "/api/v1/auth/login", json={"username": "admin", "password": password}
    ).status_code == 401
    login = client2.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "persisted-password-123"},
    )
    assert login.status_code == 200
    assert login.json()["user"]["auth_source"] == "db"


def test_bootstrap_password_regenerated_each_start(tmp_path, capsys) -> None:
    settings = make_settings(tmp_path, admin_username=None, admin_password=None)
    app1 = create_app(settings)
    password1 = _bootstrap_password(capsys.readouterr().out)

    app2 = create_app(settings)
    password2 = _bootstrap_password(capsys.readouterr().out)
    assert password1 != password2

    client2 = TestClient(app2)
    assert client2.post(
        "/api/v1/auth/login", json={"username": "admin", "password": password1}
    ).status_code == 401
    assert client2.post(
        "/api/v1/auth/login", json={"username": "admin", "password": password2}
    ).status_code == 200


def test_env_change_takes_effect_on_next_login(tmp_path) -> None:
    old = make_settings(tmp_path, admin_password="old-password")
    client_old = TestClient(create_app(old))
    assert login_client(client_old, password="old-password")

    new = make_settings(tmp_path, admin_password="new-password")
    app_new = create_app(new)
    assert app_new.state.bootstrap.mode == "env"
    client_new = TestClient(app_new)
    assert client_new.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "old-password"}
    ).status_code == 401
    assert client_new.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "new-password"}
    ).status_code == 200
