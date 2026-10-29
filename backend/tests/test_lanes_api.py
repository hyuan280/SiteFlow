from __future__ import annotations

from fastapi.testclient import TestClient

from app.services.lanes import LaneService


def test_lanes_requires_auth(anon_client: TestClient) -> None:
    assert anon_client.get("/api/v1/lanes").status_code == 401


def test_lanes_maps_worker_status(client: TestClient, monkeypatch) -> None:
    async def fake_status(_self):
        return {
            "ok": True,
            "proxy_configured": True,
            "lanes": [
                {
                    "key": "general",
                    "label": "普通通用",
                    "enabled": True,
                    "status": "running",
                    "browser": "running",
                    "display": ":120",
                    "ws_port": 6310,
                    "profile": "profiles/general",
                    "vnc_available": True,
                    "error": None,
                    "started_at": "2026-10-21T00:00:00+00:00",
                },
                {
                    "key": "interaction",
                    "label": "普通交互",
                    "enabled": True,
                    "status": "running",
                    "browser": "running",
                    "display": ":121",
                    "ws_port": 6311,
                    "profile": "profiles/interaction",
                    "vnc_available": True,
                    "error": None,
                    "started_at": "2026-10-21T00:00:00+00:00",
                },
            ],
        }

    monkeypatch.setattr(LaneService, "raw_status", fake_status)
    response = client.get("/api/v1/lanes")
    assert response.status_code == 200
    body = response.json()
    assert body["worker_connected"] is True
    assert len(body["lanes"]) == 2
    assert body["lanes"][0]["key"] == "general"
    assert body["lanes"][0]["label"] == "普通通用"
    assert body["lanes"][0]["vnc_available"] is True
    assert "ws_port" not in body["lanes"][0]


def test_lanes_worker_unavailable(client: TestClient, monkeypatch) -> None:
    async def fake_status(_self):
        return None

    monkeypatch.setattr(LaneService, "raw_status", fake_status)
    response = client.get("/api/v1/lanes")
    assert response.status_code == 200
    body = response.json()
    assert body["worker_connected"] is False
    assert len(body["lanes"]) == 4
    assert all(lane["status"] == "unavailable" for lane in body["lanes"])
    assert [lane["label"] for lane in body["lanes"]] == [
        "普通通用",
        "普通交互",
        "代理通用",
        "代理交互",
    ]


def test_start_and_stop_lane(client: TestClient, monkeypatch) -> None:
    lane = {
        "key": "general",
        "label": "普通通用",
        "enabled": True,
        "status": "running",
        "browser": "running",
        "display": ":120",
        "ws_port": 6310,
        "profile": "profiles/general",
        "vnc_available": True,
        "in_use": 0,
        "can_start": False,
        "can_stop": True,
        "error": None,
        "started_at": "2026-10-21T00:00:00+00:00",
    }

    async def fake_start(_self, _key):
        return lane

    async def fake_stop(_self, _key):
        return {**lane, "status": "stopped", "browser": "stopped", "display": None, "vnc_available": False, "can_start": True, "can_stop": False}

    monkeypatch.setattr(LaneService, "start_lane", fake_start)
    monkeypatch.setattr(LaneService, "stop_lane", fake_stop)
    start = client.post("/api/v1/lanes/general/start")
    assert start.status_code == 200
    assert start.json()["status"] == "running"
    stop = client.post("/api/v1/lanes/general/stop")
    assert stop.status_code == 200
    assert stop.json()["status"] == "stopped"


def test_focus_lane_task_page(client: TestClient, monkeypatch) -> None:
    async def fake_focus(_self, key, job_id):
        assert key == "general"
        assert job_id == "26"
        return {"focused": True, "lane": {"key": key}}

    monkeypatch.setattr(LaneService, "focus_job", fake_focus)
    response = client.post("/api/v1/lanes/general/focus", json={"job_id": "26"})
    assert response.status_code == 200
    assert response.json()["focused"] is True


def test_stop_lane_busy(client: TestClient, monkeypatch) -> None:
    from app.services.lanes import WorkerRequestError

    async def fake_stop(_self, _key):
        raise WorkerRequestError(409, "任务运行中")

    monkeypatch.setattr(LaneService, "stop_lane", fake_stop)
    response = client.post("/api/v1/lanes/general/stop")
    assert response.status_code == 409
    assert "任务运行中" in response.json()["detail"]
