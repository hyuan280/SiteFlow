from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from app.features.base import FeatureConfig, FeatureConfigError
from app.features.catalog import PostResignClickConfig, PostResignConfig, get_feature


KEY = "post.resign_click"
DEFAULTS = {
    "attendance_url": "{base_url}/calendar",
    "calendar_title_selector": [".cal-board > .cal-water"],
    "calendar_prev_selector": ['.cal-arr[aria-label="上一月"]'],
    "calendar_next_selector": ['.cal-arr[aria-label="下一月"]'],
    "calendar_day_selector": [
        '.cal-grid > button.cal-cell:not(.mute):has(.cal-num:text-is("{day}"))',
    ],
    "calendar_selected_selector": [
        '.cal-grid > button.cal-cell.on:not(.mute):has(.cal-num:text-is("{day}"))',
    ],
    "done_selector": [
        '.cal-grid > button.cal-cell:not(.mute):has(.cal-num:text-is("{day}")) .cal-badge.att',
    ],
    "button_selector": [".cal-att button.cal-att-pill.act"],
    "card_count_selector": [".cal-help"],
    "card_count_regex": r"补签卡（当前\s*(\d+)\s*张",
    "success_wait_seconds": 10,
    "max_month_jumps": 12,
}
OLD_DEFAULTS = {
    "attendance_url": "{base_url}/attendance.php",
    "card_count_regex": r"[补補][签簽]卡[:：]\s*(\d+)",
    "calendar_title_selector": ".fc-toolbar-title",
    "calendar_prev_selector": ".fc-prev-button",
    "calendar_next_selector": ".fc-next-button",
    "calendar_day_selector": ".fc-daygrid-day",
    "calendar_day_number_selector": ".fc-daygrid-day-number",
    "calendar_event_selector": ".fc-daygrid-event-harness a",
    "calendar_block_selector": ".fc-daygrid-block-event",
    "calendar_dot_selector": ".fc-daygrid-dot-event",
    "ajax_path": "ajax.php",
    "ajax_action": "attendanceRetroactive",
    "ajax_date_param": "params[date]",
}
SELECTORS = tuple(key for key in DEFAULTS if key.endswith("_selector"))
DATE_SELECTORS = ("calendar_day_selector", "calendar_selected_selector", "done_selector")


def test_resign_click_catalog_schema_and_defaults(client):
    response = client.get("/api/v1/features")
    assert response.status_code == 200, response.text
    features = {item["key"]: item for item in response.json()}
    view = features[KEY]
    assert view["name"] == "页面点击补签"
    assert view["category"] == "post"
    assert view["cardinality"] == "multiple"
    assert view["requires"] == []
    assert view["version"] == 1
    assert view["required_fields"] == view["base_required_fields"] == []
    assert view["defaults"] == DEFAULTS
    assert get_feature(KEY).validate_full({}) == DEFAULTS
    schema = view["config_schema"]
    assert schema["additionalProperties"] is False
    props = schema["properties"]
    assert set(props) == set(DEFAULTS)
    for key, value in DEFAULTS.items():
        assert props[key]["default"] == value
        assert re.search(r"[\u4e00-\u9fff]", props[key]["title"])
        assert re.search(r"[\u4e00-\u9fff]", props[key]["description"])
        assert PostResignClickConfig.model_fields[key].title == props[key]["title"]
    for key in SELECTORS:
        assert props[key]["type"] == "array"
        assert props[key]["items"] == {"type": "string", "minLength": 1}
        assert props[key].get("minItems", 0) == (0 if key == "card_count_selector" else 1)
        assert props[key]["maxItems"] == 20
    for key, low, high in (("success_wait_seconds", 1, 60), ("max_month_jumps", 0, 120)):
        assert props[key]["type"] == "integer"
        assert props[key]["minimum"] == low
        assert props[key]["maximum"] == high
    for hint in ("pending", "过去日期", "当月", "相邻月", "唯一", "目标日期", "补签", "模板"):
        assert hint in view["description"]
    for key in DATE_SELECTORS:
        for hint in ("当月", "相邻月", "唯一", "{date}", "{day}", "{month}", "{year}"):
            assert hint in props[key]["description"]
    assert "绑定目标日期" in props["done_selector"]["description"]
    assert "按钮文字必须" in props["button_selector"]["description"]
    assert "body" in props["card_count_selector"]["description"]
    assert "数字年月" in props["calendar_title_selector"]["description"]
    assert features["post.resign"]["defaults"] == OLD_DEFAULTS
    assert features["post.resign"]["version"] == 1


def test_resign_click_defaults_roundtrip_and_independent_instances():
    config = PostResignClickConfig()
    assert config.model_dump(mode="json") == DEFAULTS
    assert PostResignClickConfig.model_validate_json(config.model_dump_json()) == config
    assert PostResignClickConfig.model_validate(DEFAULTS) == config
    for key in SELECTORS:
        getattr(config, key).append("#other-{date}")
    assert PostResignClickConfig().model_dump() == DEFAULTS
    assert re.search(config.card_count_regex, "补签卡（当前 12 张）").group(1) == "12"


@pytest.mark.parametrize("key", SELECTORS)
@pytest.mark.parametrize("value", ["#date-{day}", [""], [" \t\n"], [1], [None], None])
def test_resign_click_rejects_non_arrays_and_blank_candidates(key, value):
    with pytest.raises(ValidationError):
        PostResignClickConfig.model_validate({key: value})


@pytest.mark.parametrize("key", SELECTORS)
def test_resign_click_selector_limits_and_trimming(key):
    selector = '[data-date="{date}"]'
    config = get_feature(KEY).validate_full({key: [f" {selector} "] * 20})
    assert config[key] == [selector] * 20
    with pytest.raises(FeatureConfigError):
        get_feature(KEY).validate_full({key: [selector] * 21})
    if key == "card_count_selector":
        assert get_feature(KEY).validate_full({key: []})[key] == []
    else:
        with pytest.raises(FeatureConfigError):
            get_feature(KEY).validate_full({key: []})


@pytest.mark.parametrize("key", SELECTORS)
@pytest.mark.parametrize("token", ["{foo}", "{base_url}", "{day:02d}", "{date.year}", "{{day}}", "{", "}"])
def test_resign_click_rejects_unknown_or_malformed_template_tokens(key, token):
    with pytest.raises(ValidationError, match="选择器模板仅支持"):
        PostResignClickConfig.model_validate({key: [f'#day-{{day}}[data-value="{token}"]']})


@pytest.mark.parametrize("key", DATE_SELECTORS)
@pytest.mark.parametrize("selector", [".day", '[data-month="{year}-{month}"]'])
def test_resign_click_every_date_candidate_must_bind_target(key, selector):
    with pytest.raises(ValidationError, match="必须包含"):
        PostResignClickConfig.model_validate({key: [DEFAULTS[key][0], selector]})


@pytest.mark.parametrize("key", SELECTORS)
def test_resign_click_accepts_playwright_templates(key):
    selectors = [
        'button:has(.num:text-is("{day}")):not(.mute)',
        '[data-date="{date}"][data-year="{year}"][data-month="{month}"]',
    ]
    assert get_feature(KEY).validate_full({key: selectors})[key] == selectors


@pytest.mark.parametrize("config", [
    {"unknown": True}, {"ajax_path": "ajax.php"}, {"calendar_month_regex": r"(\d+)"},
    {"calendar_day_number_selector": ".num"}, {"attendance_url": ""},
    {"success_wait_seconds": 0}, {"success_wait_seconds": 61}, {"success_wait_seconds": 1.5},
    {"max_month_jumps": -1}, {"max_month_jumps": 121}, {"max_month_jumps": 1.5},
])
def test_resign_click_rejects_extra_fields_and_invalid_limits(config):
    with pytest.raises(ValidationError):
        PostResignClickConfig.model_validate(config)
    with pytest.raises(FeatureConfigError):
        get_feature(KEY).validate_full(config)


@pytest.mark.parametrize("wait,jumps", [(1, 0), (10, 12), (60, 120)])
def test_resign_click_accepts_numeric_boundaries(wait, jumps):
    config = PostResignClickConfig(success_wait_seconds=wait, max_month_jumps=jumps)
    assert config.success_wait_seconds == wait
    assert config.max_month_jumps == jumps


def test_resign_click_and_legacy_models_are_independent():
    assert PostResignClickConfig.__bases__ == (FeatureConfig,)
    assert PostResignConfig().model_dump() == OLD_DEFAULTS
    old_schema = get_feature("post.resign").config_schema()
    new_schema = get_feature(KEY).config_schema()
    assert old_schema["title"] == "PostResignConfig"
    assert new_schema["title"] == "PostResignClickConfig"
    assert set(old_schema["properties"]) == set(OLD_DEFAULTS)
    for key in set(SELECTORS) & set(OLD_DEFAULTS):
        assert old_schema["properties"][key]["type"] == "string"
        assert new_schema["properties"][key]["type"] == "array"
        assert getattr(PostResignConfig(**{key: "#legacy"}), key) == "#legacy"
        with pytest.raises(ValidationError):
            PostResignConfig.model_validate({key: DEFAULTS[key]})
        with pytest.raises(ValidationError):
            PostResignClickConfig.model_validate({key: OLD_DEFAULTS[key]})
    with pytest.raises(ValidationError):
        PostResignConfig.model_validate(DEFAULTS)
    with pytest.raises(ValidationError):
        PostResignClickConfig.model_validate(OLD_DEFAULTS)


@pytest.mark.parametrize("config", [
    {"unknown": True}, {"calendar_title_selector": ".title"},
    {"calendar_day_selector": [".day"]}, {"done_selector": ['[data-date="{foo}"]']},
    {"button_selector": []}, {"card_count_selector": [" "]},
    {"calendar_next_selector": [".next"] * 21}, {"success_wait_seconds": 61},
    {"max_month_jumps": -1}, {"calendar_selected_selector": ["#day-{day}", ".selected"]},
])
def test_resign_click_api_rejects_invalid_config(client, config):
    for path, extra in (("templates", {}), ("sites", {"base_url": "https://example.test"})):
        response = client.post(f"/api/v1/{path}", json={
            "name": "invalid-click", **extra,
            "steps": [{"step_key": "click", "feature_type": KEY, "config": config}],
        })
        assert response.status_code == 422, response.text
    response = client.put(f"/api/v1/features/{KEY}/settings", json={
        "defaults": config, "required_fields": [],
    })
    assert response.status_code == 422, response.text


def test_resign_click_settings_roundtrip_leaves_legacy_defaults_unchanged(client):
    path = f"/api/v1/features/{KEY}/settings"
    custom = {"calendar_title_selector": ["#month", ".month"], "max_month_jumps": 24}
    response = client.put(path, json={"defaults": custom, "required_fields": []})
    assert response.status_code == 200, response.text
    assert response.json()["defaults"] == {**DEFAULTS, **custom}
    views = {item["key"]: item for item in client.get("/api/v1/features").json()}
    assert views[KEY]["defaults"] == {**DEFAULTS, **custom}
    assert views[KEY]["config_schema"]["properties"]["calendar_title_selector"]["default"] == (
        custom["calendar_title_selector"]
    )
    assert views["post.resign"]["defaults"] == OLD_DEFAULTS
    assert views["post.resign"]["version"] == 1
    response = client.delete(path)
    assert response.status_code == 200, response.text
    assert response.json()["defaults"] == DEFAULTS


@pytest.mark.parametrize("override,expected", [
    ({}, ["#parent-count"]),
    ({"card_count_selector": [" #child-count "]}, ["#child-count"]),
    ({"card_count_selector": []}, []),
    ({"card_count_selector": None}, DEFAULTS["card_count_selector"]),
])
def test_resign_click_api_inheritance_and_raw_history(client, override, expected):
    parent_config = {
        "attendance_url": "{base_url}/custom-calendar",
        "calendar_day_selector": ['[data-date="{date}"]', '#day-{year}-{month}-{day}'],
        "card_count_selector": ["#parent-count"],
        "max_month_jumps": 24,
    }
    response = client.post("/api/v1/templates", json={
        "name": "click-parent",
        "steps": [{"step_key": "click", "feature_type": KEY, "config": parent_config}],
    })
    assert response.status_code == 201, response.text
    parent = response.json()
    child_config = {
        "calendar_day_selector": [' button:has-text("{day}"):not(.mute) '], **override,
    }
    payload = {
        "name": "click-child", "base_url": "https://example.test", "template_id": parent["id"],
        "steps": [
            {"step_key": "click", "feature_type": KEY, "config": child_config},
            {"step_key": "second", "feature_type": KEY},
            {"step_key": "legacy", "feature_type": "post.resign"},
        ],
    }
    response = client.post("/api/v1/sites", json=payload)
    assert response.status_code == 201, response.text
    site = response.json()
    assert site["valid"] is True
    steps = {step["step_key"]: step for step in site["resolved"]["steps"]}
    assert steps["click"]["feature_version"] == steps["legacy"]["feature_version"] == 1
    resolved = steps["click"]["config"]
    assert resolved == {
        **DEFAULTS, **parent_config,
        "attendance_url": "https://example.test/custom-calendar",
        "calendar_day_selector": ['button:has-text("{day}"):not(.mute)'],
        "card_count_selector": expected,
    }
    assert steps["second"]["config"] == {
        **DEFAULTS, "attendance_url": "https://example.test/calendar",
    }
    assert steps["legacy"]["config"] == {
        **OLD_DEFAULTS, "attendance_url": "https://example.test/attendance.php",
    }
    url = f"/api/v1/sites/{site['id']}"
    assert client.get(url).json()["resolved"] == site["resolved"]
    response = client.get(f"{url}/versions/1")
    assert response.status_code == 200, response.text
    history = response.json()
    snapshot = history["snapshot"]
    assert snapshot["local"]["steps"][0]["config"] == child_config
    assert snapshot["dependencies"][0]["local"]["steps"][0]["config"] == parent_config
    assert snapshot["features"][KEY]["versions"] == [1]
    assert snapshot["features"]["post.resign"]["versions"] == [1]
    assert snapshot["resolved"] == site["resolved"]
    payload["steps"][0]["config"] = {"max_month_jumps": 0}
    response = client.put(url, json={**payload, "expected_revision": 1})
    assert response.status_code == 200, response.text
    response = client.post(f"{url}/versions/1/restore", json={"expected_revision": 2})
    assert response.status_code == 200, response.text
    assert response.json()["steps"][0]["config"] == child_config
    assert response.json()["resolved"] == site["resolved"]
    assert client.get(f"{url}/versions/1").json() == history
