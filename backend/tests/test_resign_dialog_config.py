from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from app.features.base import FeatureConfig, FeatureConfigError
from app.features.catalog import PostResignDialogConfig, get_feature


KEY = "post.resign_dialog"
DEFAULTS = {
    "entry_selector": ['.attendance-card header button[title*="补签卡"]'],
    "dialog_selector": ['[role="dialog"][aria-label^="补签"]'],
    "dialog_ready_selector": [],
    "button_selector": ["button"],
    "date_regex": r"(?<!\d)(?P<month>\d{2})-(?P<day>\d{2})(?!\d)",
    "missing_button_as_signed": False,
    "dialog_wait_seconds": 10,
    "success_wait_seconds": 10,
    "success_selector": [],
    "success_regex": "补签成功",
    "done_selector": ['.attendance-calendar__day.is-done[title^="{date}"]'],
    "card_count_selector": ['.attendance-card header button[title*="补签卡"]'],
    "card_count_regex": r"补签\s*[×x]\s*(\d+)",
}
SELECTORS = tuple(key for key in DEFAULTS if key.endswith("_selector"))
OPTIONAL_SELECTORS = {"success_selector", "done_selector", "dialog_ready_selector"}


def test_resign_dialog_catalog_schema_defaults_and_independence(client):
    response = client.get("/api/v1/features")
    assert response.status_code == 200, response.text
    view = next(item for item in response.json() if item["key"] == KEY)
    assert view["name"] == "弹窗按钮补签"
    assert view["category"] == "post"
    assert view["cardinality"] == "multiple"
    assert view["requires"] == []
    assert view["version"] == 1
    assert view["defaults"] == DEFAULTS
    assert view["required_fields"] == view["base_required_fields"] == []
    assert get_feature(KEY).validate_full({}) == DEFAULTS
    assert PostResignDialogConfig.__bases__ == (FeatureConfig,)
    schema = view["config_schema"]
    assert schema["title"] == "PostResignDialogConfig"
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == set(DEFAULTS)
    for key, value in DEFAULTS.items():
        prop = schema["properties"][key]
        assert prop["default"] == value
        assert re.search(r"[\u4e00-\u9fff]", prop["title"])
        assert re.search(r"[\u4e00-\u9fff]", prop["description"])
    for key in SELECTORS:
        prop = schema["properties"][key]
        assert prop["type"] == "array"
        assert prop["items"] == {"type": "string", "minLength": 1}
        assert prop.get("minItems", 0) == (0 if key in OPTIONAL_SELECTORS else 1)
        assert prop["maxItems"] == 20
    for key in ("dialog_wait_seconds", "success_wait_seconds"):
        assert schema["properties"][key]["type"] == "integer"
        assert schema["properties"][key]["minimum"] == 1
        assert schema["properties"][key]["maximum"] == 60
    config = PostResignDialogConfig()
    assert PostResignDialogConfig.model_validate_json(config.model_dump_json()) == config
    for key in SELECTORS:
        getattr(config, key).append("#other-{date}")
    assert PostResignDialogConfig().model_dump() == DEFAULTS
    assert "ajax_path" not in schema["properties"]
    assert "attendance_url" not in schema["properties"]


def test_resign_dialog_schema_explains_runtime_safety_requirements():
    props = get_feature(KEY).config_schema()["properties"]
    assert props["dialog_ready_selector"]["title"] == "弹窗内容就绪选择器"
    for key in ("dialog_ready_selector", "missing_button_as_signed"):
        for text in ("可解析日期按钮", "唯一可见命中", "waiting_human"):
            assert text in props[key]["description"]
    for text in ("相对唯一弹窗", "加载完成", "合法空列表", "不是日期模板"):
        assert text in props["dialog_ready_selector"]["description"]
    for text in ("ISO 完整日期", "不得忽略年份截取月日", "DOM 完整日期矛盾时拒绝"):
        assert text in props["date_regex"]["description"]
    assert "baseline 在 prepare ACK + authorize 之后、实际点击之前" in props["success_selector"]["description"]


@pytest.mark.parametrize("key", SELECTORS)
@pytest.mark.parametrize("value", ["#selector", [""], [" \t"], [1], [None], None])
def test_resign_dialog_rejects_non_array_and_blank_selectors(key, value):
    with pytest.raises(ValidationError):
        PostResignDialogConfig.model_validate({key: value})


@pytest.mark.parametrize("key", SELECTORS)
def test_resign_dialog_selector_limits_trimming_and_empty(key):
    selector = '[data-date="{date}"]'
    assert get_feature(KEY).validate_full({key: [f" {selector} "] * 20})[key] == [selector] * 20
    with pytest.raises(FeatureConfigError):
        get_feature(KEY).validate_full({key: [selector] * 21})
    if key in OPTIONAL_SELECTORS:
        config = {key: [], "success_selector" if key == "done_selector" else "done_selector": [selector]}
        assert get_feature(KEY).validate_full(config)[key] == []
    else:
        with pytest.raises(ValidationError):
            PostResignDialogConfig.model_validate({key: []})


@pytest.mark.parametrize("selector", [
    ".done", '[data-month="{year}-{month}"]', '#day-{day}-{foo}',
    '#day-{day:02d}', '#day-{{day}}', '#day-{date.year}', '#day-{day}{', '#day-{day}}',
])
def test_resign_dialog_every_done_candidate_must_bind_date_with_known_tokens(selector):
    with pytest.raises(ValidationError):
        PostResignDialogConfig(done_selector=[DEFAULTS["done_selector"][0], selector])


def test_resign_dialog_only_done_selector_is_a_template():
    selectors = ['button:text-matches("\\d{2}-\\d{2}")', '[data-value="{literal}"]']
    for key in set(SELECTORS) - {"done_selector"}:
        assert get_feature(KEY).validate_full({key: selectors})[key] == selectors
    done = ['[data-date="{date}"]', '#done-{year}-{month}-{day}']
    assert PostResignDialogConfig(done_selector=done).done_selector == done


@pytest.mark.parametrize("pattern", [
    r"(?P<month>\d{2})-(?P<day>\d{2})",
    r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})",
    r"(?P<date>\d{4}-\d{2}-\d{2})",
    r"(?P<date>\d{4}-\d{2}-\d{2})|(?P<month>\d{2})-(?P<day>\d{2})",
])
def test_resign_dialog_accepts_date_capture_forms(pattern):
    assert PostResignDialogConfig(date_regex=pattern).date_regex == pattern


@pytest.mark.parametrize("pattern", [
    "[", r"\d{2}-\d{2}", r"(\d{2})-(\d{2})", r"(?P<month>\d{2})",
    r"(?P<day>\d{2})", r"(?P<year>\d{4})",
    r"(?P<year>\d{4})-(?P<month>\d{2})", r"(?P<year>\d{4})-(?P<day>\d{2})",
    r"(?P<date>\d{4}-\d{2}-\d{2})(?P<year>\d{4})",
])
def test_resign_dialog_rejects_invalid_or_incomplete_date_regex(pattern):
    with pytest.raises(ValidationError):
        PostResignDialogConfig(date_regex=pattern)


@pytest.mark.parametrize("config", [
    {"success_regex": "["}, {"card_count_regex": "["}, {"card_count_regex": r"\d+"},
    {"card_count_regex": r"(?:\d+)"}, {"done_selector": [], "success_selector": []},
    {"done_selector": [], "success_selector": [], "dialog_ready_selector": [".loaded"]},
    {"unknown": True}, {"attendance_url": "/attendance"}, {"ajax_path": "ajax.php"},
    {"missing_button_as_signed": "true"}, {"missing_button_as_signed": 1},
])
def test_resign_dialog_rejects_invalid_config(config):
    with pytest.raises(ValidationError):
        PostResignDialogConfig.model_validate(config)
    with pytest.raises(FeatureConfigError):
        get_feature(KEY).validate_full(config)


@pytest.mark.parametrize("key", ["dialog_wait_seconds", "success_wait_seconds"])
@pytest.mark.parametrize("value", [0, 61, 1.5, "10", True])
def test_resign_dialog_rejects_invalid_waits(key, value):
    with pytest.raises(ValidationError):
        PostResignDialogConfig.model_validate({key: value})


@pytest.mark.parametrize("value", [1, 10, 60])
def test_resign_dialog_accepts_boundaries_and_success_only(value):
    config = PostResignDialogConfig(
        dialog_wait_seconds=value, success_wait_seconds=value, missing_button_as_signed=True,
        done_selector=[], success_selector=['[role="dialog"][aria-label^="补签"] p'],
    )
    assert config.success_wait_seconds == config.dialog_wait_seconds == value
    assert config.done_selector == []
    assert re.search(config.card_count_regex, "补签 × 9").group(1) == "9"
    assert re.search(config.card_count_regex, "补签 x 10").group(1) == "10"
    assert re.search(config.date_regex, "补签 11-08").groupdict() == {"month": "11", "day": "08"}


@pytest.mark.parametrize("config", [
    {"entry_selector": []}, {"button_selector": "button"}, {"date_regex": "(.*)"},
    {"done_selector": [".done"]}, {"done_selector": [], "success_selector": []},
    {"success_regex": "["}, {"card_count_regex": "count"}, {"dialog_wait_seconds": 61},
    {"dialog_ready_selector": "button"}, {"dialog_ready_selector": [" "]},
    {"dialog_ready_selector": [".ready"] * 21},
])
def test_resign_dialog_api_rejects_invalid_effective_config(client, config):
    for path, extra in (("templates", {}), ("sites", {"base_url": "https://example.test"})):
        response = client.post(f"/api/v1/{path}", json={
            "name": "invalid-dialog", **extra,
            "steps": [{"step_key": "dialog", "feature_type": KEY, "config": config}],
        })
        assert response.status_code == 422, response.text
    response = client.put(f"/api/v1/features/{KEY}/settings", json={"defaults": config, "required_fields": []})
    assert response.status_code == 422, response.text


def test_resign_dialog_settings_and_inherited_workflow_roundtrip(client):
    legacy = {key: get_feature(key).validate_full({}) for key in ("post.resign", "post.resign_loop")}
    custom = {"success_selector": ['[role="dialog"][aria-label^="补签"] p'], "dialog_wait_seconds": 20}
    path = f"/api/v1/features/{KEY}/settings"
    response = client.put(path, json={"defaults": custom, "required_fields": []})
    assert response.status_code == 200, response.text
    assert response.json()["defaults"] == {**DEFAULTS, **custom}
    parent_config = {
        "entry_selector": ["#entry", ".entry"], "done_selector": [],
        "dialog_ready_selector": [".dates-loaded", ".empty-dates"],
    }
    response = client.post("/api/v1/templates", json={
        "name": "dialog-parent",
        "steps": [{"step_key": "dialog", "feature_type": KEY, "config": parent_config}],
    })
    assert response.status_code == 201, response.text
    child_config = {"entry_selector": [" #child "], "dialog_wait_seconds": None}
    response = client.post("/api/v1/sites", json={
        "name": "dialog-child", "base_url": "https://example.test", "template_id": response.json()["id"],
        "steps": [
            {"step_key": "dialog", "feature_type": KEY, "config": child_config},
            {"step_key": "second", "feature_type": KEY},
        ],
    })
    assert response.status_code == 201, response.text
    site = response.json()
    assert site["valid"] is True
    steps = {step["step_key"]: step for step in site["resolved"]["steps"]}
    assert steps["dialog"]["config"] == {
        **DEFAULTS, **custom, **parent_config, "entry_selector": ["#child"],
    }
    assert steps["second"]["config"] == {**DEFAULTS, **custom}
    history = client.get(f"/api/v1/sites/{site['id']}/versions/1").json()["snapshot"]
    assert history["local"]["steps"][0]["config"] == child_config
    assert history["dependencies"][0]["local"]["steps"][0]["config"] == parent_config
    views = {item["key"]: item for item in client.get("/api/v1/features").json()}
    for key, defaults in legacy.items():
        assert views[key]["defaults"] == defaults
    assert client.delete(path).json()["defaults"] == DEFAULTS
