import json

import pytest
from sqlalchemy import select

from app.models.workflow import WorkflowStep


@pytest.mark.parametrize("kind", ["sites", "templates"])
@pytest.mark.parametrize("prompt", ["", " \n\t ", "  Return True or False.\n", " " + "p" * 4000 + " "])
def test_result_step_prompt_roundtrip_and_persistence(client, kind, prompt):
    config = {"llm_enabled": True, "llm_prompt": prompt}
    payload = {
        "name": "result-step",
        "steps": [{"step_key": "result", "feature_type": "flow.result", "config": config}],
    }
    if kind == "sites":
        payload["base_url"] = "https://site.example"
    response = client.post(f"/api/v1/{kind}", json=payload)
    assert response.status_code == 201, response.text
    owner_id = response.json()["id"]
    path = f"/api/v1/{kind}/{owner_id}"
    expected = {
        "output_value": True, "message": "", "llm_enabled": True, "llm_prompt": prompt.strip(),
    }
    assert response.json()["resolved"]["steps"][0]["config"] == expected
    for _ in range(2):
        detail = client.get(path).json()
        assert detail["steps"][0]["config"] == config
        assert detail["resolved"]["steps"][0]["config"] == expected
        response = client.put(path, json=payload)
        assert response.status_code == 200, response.text
    with client.app.state.session_factory() as session:
        row = session.scalar(select(WorkflowStep).where(
            WorkflowStep.owner_type == kind[:-1], WorkflowStep.owner_id == owner_id,
        ))
        assert json.loads(row.config_json) == config
    assert client.get(f"{path}/resolved").json()["steps"][0]["config"] == expected


@pytest.mark.parametrize("kind", ["sites", "templates"])
@pytest.mark.parametrize("config", [
    {"llm_enabled": value} for value in ("true", "false", 0, 1, [], {})
] + [
    {"llm_prompt": value} for value in ("p" * 4001, 123, False, [], {})
])
def test_result_step_rejects_invalid_config(client, kind, config):
    payload = {
        "name": "invalid-result",
        "steps": [{"step_key": "result", "feature_type": "flow.result", "config": config}],
    }
    if kind == "sites":
        payload["base_url"] = "https://site.example"
    response = client.post(f"/api/v1/{kind}", json=payload)
    assert response.status_code == 422, response.text
    assert client.get(f"/api/v1/{kind}").json() == []


@pytest.mark.parametrize("kind", ["sites", "templates"])
def test_legacy_result_step_resolves_llm_disabled(client, kind):
    payload = {
        "name": "legacy-result",
        "steps": [{
            "step_key": "result", "feature_type": "flow.result",
            "config": {"output_value": False, "message": "legacy"},
        }],
    }
    if kind == "sites":
        payload["base_url"] = "https://site.example"
    response = client.post(f"/api/v1/{kind}", json=payload)
    assert response.status_code == 201, response.text
    path = f"/api/v1/{kind}/{response.json()['id']}"
    assert client.get(f"{path}/resolved").json()["steps"][0]["config"] == {
        "output_value": False, "message": "legacy", "llm_enabled": False, "llm_prompt": "",
    }
    assert client.get(path).json()["steps"][0]["config"] == payload["steps"][0]["config"]


def test_result_step_template_opt_in_site_override(client):
    response = client.post("/api/v1/templates", json={
        "name": "result-template",
        "steps": [{
            "step_key": "result", "feature_type": "flow.result",
            "config": {"llm_enabled": True, "llm_prompt": " Template prompt "},
        }],
    })
    assert response.status_code == 201, response.text
    payload = {
        "name": "result-site", "base_url": "https://site.example",
        "template_id": response.json()["id"],
    }
    response = client.post("/api/v1/sites", json=payload)
    assert response.status_code == 201, response.text
    path = f"/api/v1/sites/{response.json()['id']}"
    inherited = response.json()["resolved"]["steps"][0]["config"]
    assert inherited["llm_enabled"] is True
    assert inherited["llm_prompt"] == "Template prompt"
    for override, expected in [
        ({"llm_enabled": False}, {**inherited, "llm_enabled": False}),
        ({"llm_prompt": " \n "}, {**inherited, "llm_prompt": ""}),
        ({"llm_enabled": None, "llm_prompt": None}, {
            **inherited, "llm_enabled": False, "llm_prompt": "",
        }),
    ]:
        payload["steps"] = [{
            "step_key": "result", "feature_type": "flow.result", "config": override,
        }]
        response = client.put(path, json=payload)
        assert response.status_code == 200, response.text
        assert client.get(f"{path}/resolved").json()["steps"][0]["config"] == expected
        assert client.get(path).json()["steps"][0]["config"] == override
    template = client.get(f"/api/v1/templates/{payload['template_id']}/resolved").json()
    assert template["steps"][0]["config"] == inherited


def test_result_step_options_not_supported_by_other_features(client):
    response = client.post("/api/v1/sites", json={
        "name": "not-result", "base_url": "https://site.example",
        "steps": [{
            "step_key": "sign", "feature_type": "sign.button",
            "config": {"llm_enabled": True, "llm_prompt": "Return True or False."},
        }],
    })
    assert response.status_code == 422, response.text
