from __future__ import annotations

from fastapi.testclient import TestClient

from app.services.lanes import LaneService, WorkerRequestError


def test_picker_requires_auth(anon_client: TestClient) -> None:
    assert anon_client.post("/api/v1/picker/sessions", json={"url": "https://example.com"}).status_code == 401
    assert anon_client.get("/api/v1/picker/sessions/abc").status_code == 401


def test_picker_open_get_close(client: TestClient, monkeypatch) -> None:
    session = {
        "session_id": "abc123",
        "lane_key": "interaction",
        "url": "https://example.com",
        "current_url": "https://example.com/login",
        "picked": None,
        "expires_in": 900,
    }

    async def fake_open(_self, url, lane_key="interaction"):
        return {**session, "url": url, "lane_key": lane_key}

    async def fake_get(_self, session_id):
        return {
            **session,
            "session_id": session_id,
            "picked": {
                "selector": "input[name='username']",
                "tag": "input",
                "text": "",
                "id": "",
                "name": "username",
                "url": "https://example.com/login",
            },
        }

    async def fake_close(_self, _session_id):
        return {"closed": True}

    monkeypatch.setattr(LaneService, "open_picker", fake_open)
    monkeypatch.setattr(LaneService, "get_picker", fake_get)
    monkeypatch.setattr(LaneService, "close_picker", fake_close)

    created = client.post(
        "/api/v1/picker/sessions",
        json={"url": "https://example.com", "lane_key": "interaction"},
    )
    assert created.status_code == 200, created.text
    assert created.json()["session_id"] == "abc123"

    fetched = client.get("/api/v1/picker/sessions/abc123")
    assert fetched.status_code == 200
    assert fetched.json()["picked"]["selector"] == "input[name='username']"

    closed = client.delete("/api/v1/picker/sessions/abc123")
    assert closed.status_code == 200
    assert closed.json()["closed"] is True


def test_picker_worker_error(client: TestClient, monkeypatch) -> None:
    async def fake_open(_self, _url, _lane_key="interaction"):
        raise WorkerRequestError(409, "车道未启用")

    monkeypatch.setattr(LaneService, "open_picker", fake_open)
    response = client.post(
        "/api/v1/picker/sessions",
        json={"url": "https://example.com"},
    )
    assert response.status_code == 409
    assert "车道未启用" in response.json()["detail"]


def test_picker_rejects_invalid_url(client: TestClient) -> None:
    response = client.post(
        "/api/v1/picker/sessions",
        json={"url": "file:///etc/passwd"},
    )
    assert response.status_code == 422
