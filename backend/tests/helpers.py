from __future__ import annotations

import base64
import secrets

from app.core.config import Settings


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "_env_file": None,
        "database_url": f"sqlite:///{tmp_path}/test.db",
        "artifact_root": str(tmp_path / "artifacts"),
        "master_key": base64.b64encode(secrets.token_bytes(32)).decode(),
        "proxy_url": None,
        "twofauth_url": None,
        "twofauth_token": None,
        "cfbox_url": "http://cfbox.example.com",
        "cfbox_api_key": "test-cfbox-key",
        "bitwarden_url": None,
        "bitwarden_token": None,
        "bitwarden_enabled": None,
        "bitwarden_auto_detect": False,
        "admin_username": "admin",
        "admin_password": "test-password",
        "worker_timeout": 0.2,
    }
    values.update(overrides)
    return Settings(**values)


def login_client(client, username: str = "admin", password: str = "test-password") -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    csrf = response.json()["csrf_token"]
    client.headers.update({"X-CSRF-Token": csrf})
    return csrf
