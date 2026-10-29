from __future__ import annotations

from fastapi.testclient import TestClient

from tests.helpers import make_settings
from app.main import create_app
from tests.helpers import login_client


def test_lane_settings_default_and_update(client: TestClient) -> None:
    response = client.get("/api/v1/lane-settings")
    assert response.status_code == 200
    body = response.json()
    assert body["idle_stop_seconds"] == {
        "value": 300,
        "source": "default",
        "editable": True,
    }
    assert body["general_concurrency"]["value"] == 5
    assert body["proxy_concurrency"]["value"] == 3

    updated = client.put(
        "/api/v1/lane-settings",
        json={
            "idle_stop_seconds": 45,
            "general_concurrency": 8,
            "proxy_concurrency": 4,
        },
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["idle_stop_seconds"]["value"] == 45
    assert body["idle_stop_seconds"]["source"] == "db"
    assert body["general_concurrency"]["value"] == 8
    assert body["proxy_concurrency"]["value"] == 4
    assert client.get("/api/v1/lane-settings").json()["general_concurrency"]["value"] == 8


def test_lane_settings_env_lock(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        lane_idle_stop_seconds=60,
        lane_general_concurrency=2,
        lane_proxy_concurrency=4,
    )
    client = TestClient(create_app(settings))
    login_client(client)
    response = client.get("/api/v1/lane-settings")
    assert response.status_code == 200
    body = response.json()
    assert body["idle_stop_seconds"] == {
        "value": 60,
        "source": "env",
        "editable": False,
    }
    assert body["general_concurrency"]["value"] == 2
    assert body["general_concurrency"]["source"] == "env"
    assert body["proxy_concurrency"]["value"] == 4
    assert body["proxy_concurrency"]["source"] == "env"
    response = client.put(
        "/api/v1/lane-settings",
        json={
            "idle_stop_seconds": 10,
            "general_concurrency": 6,
            "proxy_concurrency": 6,
        },
    )
    assert response.status_code == 409


def test_lane_settings_validation(client: TestClient) -> None:
    assert client.put(
        "/api/v1/lane-settings", json={"idle_stop_seconds": -1}
    ).status_code == 422
    assert client.put(
        "/api/v1/lane-settings", json={"idle_stop_seconds": 999999}
    ).status_code == 422
    assert client.put(
        "/api/v1/lane-settings", json={"general_concurrency": 11}
    ).status_code == 422
    assert client.put(
        "/api/v1/lane-settings", json={"proxy_concurrency": 0}
    ).status_code == 422
