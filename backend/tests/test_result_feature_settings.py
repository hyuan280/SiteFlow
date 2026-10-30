import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from app.features.base import RESULT_DEFAULT_PROMPT, VISION_FEATURES
from app.models.setting import AppSetting
from app.schemas.execution import RunCreate
from app.services.execution import ExecutionService
from app.services.executor import ExecutionManager


SETTINGS_PATH = "/api/v1/features/flow.result/settings"
DEFAULT_SETTINGS = {
    "llm_enabled": False,
    "use_vision": True,
    "judgment_scope": "success_only",
    "model": "",
    "llm_prompt": RESULT_DEFAULT_PROMPT,
}


def _execute_payload(client, site_id):
    app = client.app
    with app.state.session_factory() as session:
        run = ExecutionService(session, app.state.settings, app.state.crypto).create_run(
            RunCreate(site_ids=[site_id]), "admin",
        )
        run_id = run.id
    manager = ExecutionManager(app.state.session_factory, app.state.settings, app.state.crypto)
    manager._post_execute = AsyncMock(return_value={"status": "success", "steps": []})
    asyncio.run(manager._execute_run(run_id))
    manager._post_execute.assert_awaited_once()
    return manager._post_execute.call_args.args[0]


def test_result_settings_default_roundtrip_and_reset(client):
    features = {item["key"]: item for item in client.get("/api/v1/features").json()}
    view = features["flow.result"]
    assert "action.result" not in features
    assert "flow.result" not in VISION_FEATURES
    assert view["requires"] == []
    assert view["execution_settings"] == DEFAULT_SETTINGS
    assert set(view["config_schema"]["properties"]) == {
        "output_value", "message", "llm_enabled", "llm_prompt",
    }
    assert view["defaults"] == {
        "output_value": True, "message": "", "llm_enabled": False, "llm_prompt": "",
    }
    assert view["customized"] is False

    payload = {
        "defaults": view["defaults"],
        "required_fields": view["required_fields"],
        "execution_settings": {
            "llm_enabled": True,
            "use_vision": False,
            "judgment_scope": "all",
            "model": "  custom-model  ",
            "llm_prompt": "  Return only True or False.\n",
        },
    }
    expected = {
        **payload["execution_settings"],
        "model": "custom-model",
        "llm_prompt": "Return only True or False.",
    }
    for _ in range(2):
        response = client.put(SETTINGS_PATH, json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["execution_settings"] == expected
        assert response.json()["customized"] is True
        features = {item["key"]: item for item in client.get("/api/v1/features").json()}
        assert features["flow.result"]["execution_settings"] == expected
        with client.app.state.session_factory() as session:
            stored = json.loads(session.get(AppSetting, "feature.flow.result").value_json)
            assert stored["execution_settings"] == expected

    # Updating step defaults must not discard global execution settings.
    payload.pop("execution_settings")
    assert client.put(SETTINGS_PATH, json=payload).json()["execution_settings"] == expected
    reset = client.delete(SETTINGS_PATH)
    assert reset.status_code == 200
    assert reset.json()["execution_settings"] == DEFAULT_SETTINGS
    assert reset.json()["customized"] is False
    with client.app.state.session_factory() as session:
        assert session.get(AppSetting, "feature.flow.result") is None


def test_result_settings_empty_object_restores_defaults(client):
    for settings in ({"llm_enabled": True, "judgment_scope": "all"}, {}):
        response = client.put(SETTINGS_PATH, json={
            "defaults": {}, "required_fields": [], "execution_settings": settings,
        })
        assert response.status_code == 200, response.text
        assert response.json()["execution_settings"] == {**DEFAULT_SETTINGS, **settings}


@pytest.mark.parametrize("invalid_settings", [
    {"llm_enabled": value} for value in ("true", "false", 0, 1, None)
] + [
    {"use_vision": value} for value in ("true", "false", 0, 1, None)
] + [
    {"judgment_scope": "success"},
    {"judgment_scope": None},
    {"model": "x" * 257},
    {"model": 123},
    {"llm_prompt": ""},
    {"llm_prompt": " \n\t "},
    {"llm_prompt": "x" * 4001},
    {"llm_prompt": None},
    {"unknown": True},
    {"api_key": "not-a-result-setting"},
])
def test_result_settings_reject_invalid_values(client, invalid_settings):
    response = client.put(SETTINGS_PATH, json={
        "defaults": {}, "required_fields": [], "execution_settings": invalid_settings,
    })
    assert response.status_code == 422, response.text
    with client.app.state.session_factory() as session:
        assert session.get(AppSetting, "feature.flow.result") is None


def test_result_settings_string_boundaries(client):
    response = client.put(SETTINGS_PATH, json={
        "defaults": {}, "required_fields": [],
        "execution_settings": {"model": " " + "m" * 256 + " ", "llm_prompt": "p" * 4000},
    })
    assert response.status_code == 200, response.text
    assert response.json()["execution_settings"]["model"] == "m" * 256
    assert client.put(SETTINGS_PATH, json={
        "defaults": {}, "required_fields": [],
        "execution_settings": {"model": " \n ", "llm_prompt": " p "},
    }).json()["execution_settings"] == {**DEFAULT_SETTINGS, "llm_prompt": "p"}


@pytest.mark.parametrize("key", [
    "challenge.image_captcha", "sign.iframe.image_captcha", "sign.image_quiz",
])
def test_existing_prompt_settings_unchanged(client, key):
    path = f"/api/v1/features/{key}/settings"
    response = client.put(path, json={
        "defaults": {}, "required_fields": [],
        "execution_settings": {"llm_prompt": "  managed prompt  "},
    })
    assert response.status_code == 200, response.text
    assert response.json()["execution_settings"] == {"llm_prompt": "  managed prompt  "}
    assert client.put(path, json={
        "defaults": {}, "required_fields": [],
        "execution_settings": {"llm_enabled": True},
    }).status_code == 422


@pytest.mark.parametrize(
    "llm_enabled,step_llm_enabled,scope,output_value,enabled,use_vision,other_feature,include_result,include_vision",
    [
        (False, True, "all", True, True, True, None, False, False),
        (True, None, "all", True, True, True, None, False, False),
        (True, False, "all", True, True, True, None, False, False),
        (True, True, "success_only", True, True, True, None, True, True),
        (True, True, "success_only", None, True, True, None, True, True),
        (True, True, "success_only", False, True, True, None, False, False),
        (True, True, "all", False, True, True, None, True, True),
        (True, True, "all", True, False, True, None, False, False),
        (True, True, "success_only", True, True, False, None, True, True),
        (True, True, "all", True, None, True, None, False, False),
        (False, True, "all", True, True, True, "sign.image_quiz", False, True),
        (True, True, "success_only", False, True, True, "challenge.image_captcha", False, True),
        (True, True, "all", False, True, True, "challenge.image_captcha", True, True),
        (True, False, "all", True, True, True, "sign.image_quiz", False, True),
    ],
)
def test_executor_result_settings_payload(
    client, llm_enabled, step_llm_enabled, scope, output_value, enabled, use_vision, other_feature,
    include_result, include_vision,
):
    behavior = {
        **DEFAULT_SETTINGS,
        "llm_enabled": llm_enabled,
        "judgment_scope": scope,
        "use_vision": use_vision,
        "model": "result-only-model",
    }
    response = client.put(SETTINGS_PATH, json={
        "defaults": {}, "required_fields": [], "execution_settings": behavior,
    })
    assert response.status_code == 200, response.text
    assert client.put("/api/v1/integrations/vision", json={
        "base_url": "https://llm.example/v1", "model": "shared-model", "api_key": "shared-key",
    }).status_code == 200
    steps = [{"step_key": "open", "feature_type": "flow.open"}]
    if enabled is not None:
        config = {"output_value": output_value} if output_value is not None else {}
        if step_llm_enabled is not None:
            config["llm_enabled"] = step_llm_enabled
        steps.append({
            "step_key": "result", "feature_type": "flow.result", "enabled": enabled,
            "config": config,
        })
    if other_feature:
        steps.append({
            "step_key": "other", "feature_type": other_feature,
            "config": {"captcha_method": "vision"} if other_feature == "challenge.image_captcha" else {},
        })
    response = client.post("/api/v1/sites", json={
        "name": "result-payload", "base_url": "https://site.example", "steps": steps,
    })
    assert response.status_code == 201, response.text
    payload = _execute_payload(client, response.json()["id"])
    execution_settings = payload["feature_execution_settings"]
    assert ("flow.result" in execution_settings) is include_result
    if include_result:
        assert execution_settings["flow.result"] == behavior
    assert (payload["vision"] is not None) is include_vision
    if include_vision:
        assert payload["vision"]["model"] == "shared-model"
        assert payload["vision"]["api_key"] == "shared-key"
        assert payload["vision"]["base_url"] == "https://llm.example/v1"
    if other_feature:
        assert set(execution_settings[other_feature]) == {"llm_prompt"}
    for step in payload["steps"]:
        if step["feature_type"] == "flow.result":
            assert step["config"] == {
                "output_value": True if output_value is None else output_value,
                "message": "",
                "llm_enabled": step_llm_enabled is True,
                "llm_prompt": "",
            }


@pytest.mark.parametrize("site_opt_in", [None, False, True])
def test_executor_inherited_result_opt_in(client, site_opt_in):
    response = client.put(SETTINGS_PATH, json={
        "defaults": {}, "required_fields": [],
        "execution_settings": {"llm_enabled": True},
    })
    assert response.status_code == 200, response.text
    assert client.put("/api/v1/integrations/vision", json={
        "base_url": "https://llm.example/v1", "model": "shared-model", "api_key": "shared-key",
    }).status_code == 200
    response = client.post("/api/v1/templates", json={
        "name": "result-template",
        "steps": [{
            "step_key": "result", "feature_type": "flow.result",
            "config": {"llm_enabled": True, "llm_prompt": " Template prompt "},
        }],
    })
    assert response.status_code == 201, response.text
    response = client.post("/api/v1/sites", json={
        "name": "inherited-result", "base_url": "https://site.example",
        "template_id": response.json()["id"],
        "steps": [] if site_opt_in is None else [{
            "step_key": "result", "feature_type": "flow.result",
            "config": {"llm_enabled": site_opt_in, "llm_prompt": " \n "},
        }],
    })
    assert response.status_code == 201, response.text
    payload = _execute_payload(client, response.json()["id"])
    expected_opt_in = site_opt_in is not False
    assert ("flow.result" in payload["feature_execution_settings"]) is expected_opt_in
    assert (payload["vision"] is not None) is expected_opt_in
    assert payload["steps"][0]["config"]["llm_enabled"] is expected_opt_in
    assert payload["steps"][0]["config"]["llm_prompt"] == (
        "Template prompt" if site_opt_in is None else ""
    )
    if expected_opt_in:
        assert payload["feature_execution_settings"]["flow.result"]["llm_prompt"] == RESULT_DEFAULT_PROMPT


@pytest.mark.parametrize("eligible_opt_in", [False, True])
def test_executor_result_gates_apply_to_same_step(client, eligible_opt_in):
    assert client.put(SETTINGS_PATH, json={
        "defaults": {}, "required_fields": [],
        "execution_settings": {"llm_enabled": True, "judgment_scope": "success_only"},
    }).status_code == 200
    assert client.put("/api/v1/integrations/vision", json={
        "base_url": "https://llm.example/v1", "model": "shared-model", "api_key": "shared-key",
    }).status_code == 200
    response = client.post("/api/v1/sites", json={
        "name": "multiple-results", "base_url": "https://site.example",
        "steps": [
            {"step_key": "failure", "feature_type": "flow.result",
             "config": {"output_value": False, "llm_enabled": True}},
            {"step_key": "disabled", "feature_type": "flow.result", "enabled": False,
             "config": {"llm_enabled": True}},
            {"step_key": "success", "feature_type": "flow.result",
             "config": {"llm_enabled": eligible_opt_in}},
        ],
    })
    assert response.status_code == 201, response.text
    payload = _execute_payload(client, response.json()["id"])
    assert ("flow.result" in payload["feature_execution_settings"]) is eligible_opt_in
    assert (payload["vision"] is not None) is eligible_opt_in
