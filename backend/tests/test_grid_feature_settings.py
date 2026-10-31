import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from app.features.base import (
    EXECUTION_SETTINGS_FEATURES,
    GRID_CAPTCHA_DEFAULT_PROMPT,
    IMAGE_CAPTCHA_FEATURES,
    VISION_FEATURES,
    FeatureConfigError,
)
from app.features.captcha import CAPTCHA_METHOD_KEYS
from app.features.catalog import get_feature
from app.models.setting import AppSetting
from app.schemas.execution import RunCreate
from app.services.execution import ExecutionService
from app.services.executor import ExecutionManager
from app.services.feature_settings import FeatureSettingsService


KEY = "challenge.grid_captcha"
SETTINGS_PATH = f"/api/v1/features/{KEY}/settings"
DEFAULTS = {
    "image_selector": [".geetest_panel_box", ".geetest_widget"],
    "cell_selector": [".geetest_item"],
    "selected_selector": '.geetest_selected, .selected, [aria-checked="true"], [aria-selected="true"]',
    "refresh_selector": [".geetest_refresh"],
    "submit_selector": [".geetest_commit"],
    "max_refresh": 3,
    "success_signals": ["验证成功", "驗證成功", "今天已经签到过啦", "今天已經簽到過啦"],
    "success_selector": [],
    "success_when_missing": [],
    "success_wait_seconds": 10,
}


def test_grid_feature_schema_and_method_catalog(client):
    features = {item["key"]: item for item in client.get("/api/v1/features").json()}
    view = features[KEY]
    assert view["requires"] == ["vision"]
    assert view["category"] == "challenge"
    assert view["cardinality"] == "multiple"
    assert view["defaults"] == DEFAULTS
    assert view["required_fields"] == view["base_required_fields"] == []
    assert view["customized"] is False
    assert view["execution_settings"] == {"llm_prompt": GRID_CAPTCHA_DEFAULT_PROMPT}
    assert KEY in VISION_FEATURES
    assert KEY in EXECUTION_SETTINGS_FEATURES
    assert KEY not in IMAGE_CAPTCHA_FEATURES
    props = view["config_schema"]["properties"]
    assert set(props) == set(DEFAULTS)
    assert props["cell_selector"]["title"] == "九宫格单元选择器"
    assert props["selected_selector"]["type"] == "string"
    for field in ("image_selector", "cell_selector", "refresh_selector", "submit_selector",
                  "success_selector", "success_when_missing", "success_signals"):
        assert props[field]["type"] == "array"
        assert props[field]["items"]["minLength"] == 1
    assert props["max_refresh"]["minimum"] == 0
    assert props["max_refresh"]["maximum"] == 10
    assert props["max_refresh"]["default"] == 3
    assert "retry_count" not in props
    assert props["success_wait_seconds"]["minimum"] == 1
    assert props["success_wait_seconds"]["maximum"] == 120
    assert get_feature(KEY).validate_full({}) == DEFAULTS
    assert CAPTCHA_METHOD_KEYS == ("ddddocr", "vision", "nexusphp_gd5")
    for key in IMAGE_CAPTCHA_FEATURES:
        assert features[key]["config_schema"]["properties"]["captcha_method"]["enum"] == list(
            CAPTCHA_METHOD_KEYS
        )


@pytest.mark.parametrize("config", [
    {"image_selector": []}, {"cell_selector": []},
    {"image_selector": [" "]}, {"cell_selector": "div"},
    {"selected_selector": []}, {"refresh_selector": [""]},
    {"submit_selector": [""]}, {"success_signals": [" "]},
    {"success_selector": [""]}, {"success_when_missing": [""]},
    {"max_refresh": -1}, {"max_refresh": 11}, {"max_refresh": 1.5},
    {"retry_count": 3},
    {"success_wait_seconds": 0}, {"success_wait_seconds": 121},
    {"captcha_method": "vision"}, {"llm_prompt": "not a step field"},
])
def test_grid_config_rejects_invalid_fields(client, config):
    feature = get_feature(KEY)
    with pytest.raises(FeatureConfigError):
        feature.validate_full(config)
    response = client.put(SETTINGS_PATH, json={"defaults": config, "required_fields": []})
    assert response.status_code == 422, response.text


def test_grid_config_boundaries_and_explicit_success_conditions():
    feature = get_feature(KEY)
    for refresh, wait in ((0, 1), (10, 120)):
        config = feature.validate_full({"max_refresh": refresh, "success_wait_seconds": wait})
        assert config["max_refresh"] == refresh
        assert config["success_wait_seconds"] == wait
    # Empty conditions remain representable; runtime must not infer success from disappearance.
    config = feature.validate_full({"success_signals": []})
    assert not any(config[key] for key in (
        "success_signals", "success_selector", "success_when_missing",
    ))
    assert feature.validate_full({"success_when_missing": [" #captcha "]})[
        "success_when_missing"
    ] == ["#captcha"]


def test_grid_settings_roundtrip_preserve_prompt_and_reset(client):
    payload = {
        "defaults": {"max_refresh": 4, "success_signals": [], "success_selector": ["#done"]},
        "required_fields": ["cell_selector"],
        "execution_settings": {"llm_prompt": "Select every matching grid cell; do not guess."},
    }
    for _ in range(2):
        response = client.put(SETTINGS_PATH, json=payload)
        assert response.status_code == 200, response.text
        view = response.json()
        assert view["customized"] is True
        assert view["defaults"] == {**DEFAULTS, **payload["defaults"]}
        assert view["required_fields"] == payload["required_fields"]
        assert view["execution_settings"] == payload["execution_settings"]
        listed = next(item for item in client.get("/api/v1/features").json() if item["key"] == KEY)
        assert listed == view
        with client.app.state.session_factory() as session:
            assert json.loads(session.get(AppSetting, f"feature.{KEY}").value_json) == payload
            config = FeatureSettingsService(session).validate(get_feature(KEY), {"max_refresh": 1})
            assert config["max_refresh"] == 1
            assert config["success_selector"] == ["#done"]
            assert "llm_prompt" not in config
    for behavior in (None, "omitted"):
        update = {"defaults": {}, "required_fields": []}
        if behavior is None:
            update["execution_settings"] = None
        assert client.put(SETTINGS_PATH, json=update).json()["execution_settings"] == payload[
            "execution_settings"
        ]
    response = client.put(SETTINGS_PATH, json={
        "defaults": {}, "required_fields": [], "execution_settings": {},
    })
    assert response.json()["execution_settings"] == {"llm_prompt": GRID_CAPTCHA_DEFAULT_PROMPT}
    reset = client.delete(SETTINGS_PATH)
    assert reset.status_code == 200
    assert reset.json()["defaults"] == DEFAULTS
    assert reset.json()["execution_settings"] == {"llm_prompt": GRID_CAPTCHA_DEFAULT_PROMPT}
    assert reset.json()["customized"] is False
    with client.app.state.session_factory() as session:
        assert session.get(AppSetting, f"feature.{KEY}") is None


@pytest.mark.parametrize("behavior", [
    {"llm_prompt": ""}, {"llm_prompt": "p" * 4001},
    {"llm_enabled": True}, {"api_key": "not-a-setting"},
])
def test_grid_settings_reject_invalid_execution_settings(client, behavior):
    response = client.put(SETTINGS_PATH, json={
        "defaults": {}, "required_fields": [], "execution_settings": behavior,
    })
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("enabled", [False, True])
def test_grid_executor_forwards_credentials_only_when_enabled(client, enabled):
    assert client.put("/api/v1/integrations/vision", json={
        "base_url": "https://vision.example/v1", "model": "grid-model", "api_key": "grid-secret",
    }).status_code == 200
    prompt = "Match all cells to the target; refresh if unclear."
    assert client.put(SETTINGS_PATH, json={
        "defaults": {}, "required_fields": [], "execution_settings": {"llm_prompt": prompt},
    }).status_code == 200
    response = client.post("/api/v1/sites", json={
        "name": "grid-workflow", "base_url": "https://site.example",
        "steps": [
            {"step_key": "open", "feature_type": "flow.open"},
            {"step_key": "signup", "feature_type": "action.click", "config": {"selector": ["#signup"]}},
            {"step_key": "grid", "feature_type": KEY, "enabled": enabled},
            {"step_key": "result", "feature_type": "flow.result", "config": {"llm_enabled": True}},
        ],
    })
    assert response.status_code == 201, response.text
    assert response.json()["valid"] is True
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
    assert (KEY in payload["feature_execution_settings"]) is enabled
    assert "flow.result" not in payload["feature_execution_settings"], "global result gate stays off"
    assert (payload["vision"] is not None) is enabled
    if enabled:
        assert payload["feature_execution_settings"][KEY] == {"llm_prompt": prompt}
        assert payload["vision"]["api_key"] == "grid-secret"
        assert payload["vision"]["model"] == "grid-model"
    grid = next(step for step in payload["steps"] if step["feature_type"] == KEY)
    assert grid["requires"] == ["vision"]
    assert grid["config"] == DEFAULTS
    assert "grid-secret" not in json.dumps(payload["steps"])
    assert prompt not in json.dumps(payload["steps"])
