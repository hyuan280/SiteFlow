from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.features.base import FeatureConfigError
from app.features.catalog import ActionNavigateConfig, get_feature
from app.schemas.execution import RunCreate
from app.services.execution import ExecutionService
from app.services.executor import ExecutionManager


KEY = "action.navigate"
SETTINGS_PATH = f"/api/v1/features/{KEY}/settings"


def test_navigation_retry_schema_and_catalog_defaults(client):
    assert ActionNavigateConfig().retry_count == 0
    assert get_feature(KEY).validate_full({})["retry_count"] == 0
    assert get_feature("flow.open").validate_full({})["retry_count"] == 5
    response = client.get("/api/v1/features")
    assert response.status_code == 200, response.text
    features = {item["key"]: item for item in response.json()}
    navigate = features[KEY]
    retry = navigate["config_schema"]["properties"]["retry_count"]
    assert retry["type"] == "integer"
    assert retry["default"] == navigate["defaults"]["retry_count"] == 0
    assert retry["minimum"] == 0
    assert retry["maximum"] == 20
    assert features["flow.open"]["defaults"]["retry_count"] == 5


@pytest.mark.parametrize("retry_count", [-1, 21, 1.5, "invalid"])
def test_navigation_retry_rejects_invalid_values_in_schema_and_api(client, retry_count):
    with pytest.raises(ValidationError):
        ActionNavigateConfig(retry_count=retry_count)
    with pytest.raises(FeatureConfigError, match="retry_count"):
        get_feature(KEY).validate_full({"retry_count": retry_count})
    response = client.put(SETTINGS_PATH, json={
        "defaults": {"retry_count": retry_count}, "required_fields": [],
    })
    assert response.status_code == 422, response.text
    response = client.post("/api/v1/sites", json={
        "name": "invalid-navigation-retry", "base_url": "https://pt.example.com",
        "steps": [{"step_key": "navigate", "feature_type": KEY,
                   "config": {"retry_count": retry_count}}],
    })
    assert response.status_code == 422, response.text
    assert "retry_count" in response.text


@pytest.mark.parametrize("retry_count", [0, 2, 20])
def test_navigation_retry_settings_roundtrip_and_reset(client, retry_count):
    assert ActionNavigateConfig(retry_count=retry_count).retry_count == retry_count
    response = client.put(SETTINGS_PATH, json={
        "defaults": {"retry_count": retry_count}, "required_fields": [],
    })
    assert response.status_code == 200, response.text
    assert response.json()["defaults"]["retry_count"] == retry_count
    assert response.json()["customized"] is True
    listed = next(item for item in client.get("/api/v1/features").json() if item["key"] == KEY)
    assert listed["defaults"]["retry_count"] == retry_count
    response = client.delete(SETTINGS_PATH)
    assert response.status_code == 200, response.text
    assert response.json()["defaults"]["retry_count"] == 0
    assert response.json()["customized"] is False


@pytest.mark.parametrize("global_retry, override, expected", [
    (None, {}, 0),
    (None, {"retry_count": 0}, 0),
    (None, {"retry_count": 20}, 20),
    (3, {}, 3),
    (3, {"retry_count": 0}, 0),
    (3, {"retry_count": 2}, 2),
])
def test_navigation_retry_resolves_and_reaches_worker_payload(
    client, global_retry, override, expected,
):
    if global_retry is not None:
        response = client.put(SETTINGS_PATH, json={
            "defaults": {"retry_count": global_retry}, "required_fields": [],
        })
        assert response.status_code == 200, response.text
    response = client.post("/api/v1/sites", json={
        "name": "navigation-retry", "base_url": "https://pt.example.com",
        "steps": [
            {"step_key": "open", "feature_type": "flow.open"},
            {"step_key": "navigate", "feature_type": KEY,
             "config": {"url": "{base_url}/next", **override}, "timeout_seconds": 7},
            {"step_key": "result", "feature_type": "flow.result"},
        ],
    })
    assert response.status_code == 201, response.text
    site = response.json()
    assert site["valid"] is True
    response = client.get(f"/api/v1/sites/{site['id']}")
    assert response.status_code == 200, response.text
    stored = next(step for step in response.json()["steps"] if step["step_key"] == "navigate")
    assert stored["config"] == {"url": "{base_url}/next", **override}
    response = client.get(f"/api/v1/sites/{site['id']}/resolved")
    assert response.status_code == 200, response.text
    resolved = {step["step_key"]: step for step in response.json()["steps"]}
    assert resolved["navigate"]["config"] == {
        "url": "https://pt.example.com/next", "wait_until": "domcontentloaded",
        "retry_count": expected,
    }
    assert resolved["open"]["config"]["retry_count"] == 5
    app = client.app
    with app.state.session_factory() as session:
        run = ExecutionService(session, app.state.settings, app.state.crypto).create_run(
            RunCreate(site_ids=[site["id"]]), "admin",
        )
        run_id = run.id
    manager = ExecutionManager(app.state.session_factory, app.state.settings, app.state.crypto)
    manager._post_execute = AsyncMock(return_value={"status": "success", "steps": []})
    asyncio.run(manager._execute_run(run_id))
    manager._post_execute.assert_awaited_once()
    payload = manager._post_execute.call_args.args[0]
    forwarded = {step["step_key"]: step for step in payload["steps"]}
    assert forwarded["navigate"]["config"] == resolved["navigate"]["config"]
    assert forwarded["navigate"]["timeout_seconds"] == 7
    assert forwarded["open"]["config"]["retry_count"] == 5
