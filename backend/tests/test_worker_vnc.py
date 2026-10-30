import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker.app import create_app


@pytest.mark.parametrize("token,headers", [
    ("fixture-viewer-token", {}),
    ("fixture-viewer-token", {"Authorization": "Bearer incorrect"}),
    ("", {"Authorization": "Bearer "}),
    ("fixture-viewer-token", {"Authorization": "Bearer fixture-viewer-token", "Origin": "https://attacker.test"}),
])
def test_worker_viewer_rejects_untrusted_connections(monkeypatch, token, headers):
    monkeypatch.setenv("SITEFLOW_BROWSER_TOKEN", token)
    app = create_app()
    app.state.manager = SimpleNamespace(get=lambda _: pytest.fail("Must authenticate before accessing lanes"))
    with pytest.raises(WebSocketDisconnect) as exc:
        with TestClient(app).websocket_connect("/lanes/general/vnc", headers=headers):
            pytest.fail("Untrusted viewer connected")
    assert exc.value.code == 4403


def test_worker_viewer_authenticated_request_reaches_lane_check(monkeypatch):
    monkeypatch.setenv("SITEFLOW_BROWSER_TOKEN", "fixture-viewer-token")
    app = create_app()
    app.state.manager = SimpleNamespace(get=lambda _: SimpleNamespace(session=None))
    with pytest.raises(WebSocketDisconnect) as exc:
        with TestClient(app).websocket_connect(
            "/lanes/general/vnc", headers={"Authorization": "Bearer fixture-viewer-token"},
        ):
            pytest.fail("Stopped lane should not connect")
    assert exc.value.code == 4404
