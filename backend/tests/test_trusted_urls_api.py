import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models.workflow import Site
from app.schemas.execution import RunCreate
from app.services.execution import ExecutionService
from app.services.executor import ExecutionManager
from tests.helpers import login_client


SITE = {"name": "trusted-site", "base_url": "https://site.example"}
PATTERNS = [r"^https://login\.example/", r"^https://site\.example/\p{L}+$"]


def test_trusted_urls_roundtrip_persistence_and_legacy_put(client, settings):
    response = client.post("/api/v1/sites", json={
        **SITE, "trusted_urls": [f"  {pattern}\n" for pattern in PATTERNS],
    })
    assert response.status_code == 201, response.text
    site_id = response.json()["id"]
    assert response.json()["trusted_urls"] == PATTERNS
    path = f"/api/v1/sites/{site_id}"
    with client.app.state.session_factory() as session:
        assert json.loads(session.get(Site, site_id).trusted_urls_json) == PATTERNS

    reopened = TestClient(create_app(settings))
    login_client(reopened)
    assert reopened.get(path).json()["trusted_urls"] == PATTERNS
    assert reopened.get("/api/v1/sites").json()[0]["trusted_urls"] == PATTERNS

    response = reopened.put(path, json={**SITE, "notes": "older client"})
    assert response.status_code == 200, response.text
    assert response.json()["trusted_urls"] == PATTERNS
    replacement = [r"^https://other\.example/"]
    response = reopened.put(path, json={**SITE, "trusted_urls": replacement})
    assert response.status_code == 200, response.text
    assert response.json()["trusted_urls"] == replacement
    response = reopened.put(path, json={**SITE, "trusted_urls": []})
    assert response.status_code == 200, response.text
    assert response.json()["trusted_urls"] == []
    assert reopened.get(path).json()["trusted_urls"] == []
    assert reopened.get("/api/v1/sites").json()[0]["trusted_urls"] == []
    with reopened.app.state.session_factory() as session:
        assert session.get(Site, site_id).trusted_urls_json == "[]"


def test_trusted_urls_omitted_on_create_defaults_to_empty(client):
    response = client.post("/api/v1/sites", json=SITE)
    assert response.status_code == 201, response.text
    assert response.json()["trusted_urls"] == []
    with client.app.state.session_factory() as session:
        assert session.get(Site, response.json()["id"]).trusted_urls_json == "[]"


@pytest.mark.parametrize("invalid", [
    None, "https://example.com", {}, [None], [1], [True], [{}], [[]],
    [""], [" \n\t "], ["["], ["("], [r"\q"], ["(?V1)a"], ["(?V1)(?V0)a"],
    ["a" * 1025], ["a"] * 21,
])
def test_trusted_urls_invalid_create_and_update_are_422(client, invalid):
    response = client.post("/api/v1/sites", json={**SITE, "trusted_urls": invalid})
    assert response.status_code == 422, response.text
    assert client.get("/api/v1/sites").json() == []

    response = client.post("/api/v1/sites", json={**SITE, "trusted_urls": PATTERNS})
    assert response.status_code == 201, response.text
    path = f"/api/v1/sites/{response.json()['id']}"
    response = client.put(path, json={**SITE, "trusted_urls": invalid})
    assert response.status_code == 422, response.text
    assert client.get(path).json()["trusted_urls"] == PATTERNS


def test_trusted_urls_length_boundaries(client):
    patterns = ["a" * 1024, *[str(index) for index in range(19)]]
    response = client.post("/api/v1/sites", json={
        **SITE, "trusted_urls": [f"  {pattern}  " for pattern in patterns],
    })
    assert response.status_code == 201, response.text
    assert response.json()["trusted_urls"] == patterns


@pytest.mark.parametrize("patterns", [[], PATTERNS])
def test_executor_payload_includes_persisted_trusted_urls(client, patterns):
    response = client.post("/api/v1/sites", json={
        **SITE, "trusted_urls": patterns,
        "steps": [{"step_key": "open", "feature_type": "flow.open"}],
    })
    assert response.status_code == 201, response.text
    app = client.app
    with app.state.session_factory() as session:
        run = ExecutionService(session, app.state.settings, app.state.crypto).create_run(
            RunCreate(site_ids=[response.json()["id"]]), "admin",
        )
        run_id = run.id
    manager = ExecutionManager(app.state.session_factory, app.state.settings, app.state.crypto)
    manager._post_execute = AsyncMock(return_value={"status": "success", "steps": []})
    asyncio.run(manager._execute_run(run_id))
    manager._post_execute.assert_awaited_once()
    payload = manager._post_execute.call_args.args[0]
    assert payload["trusted_urls"] == patterns
    assert "trusted_urls_json" not in payload
