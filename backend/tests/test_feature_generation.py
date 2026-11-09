import json
from copy import deepcopy
from dataclasses import replace

import pytest
from pydantic import ValidationError

from app.features.base import FeatureConfigError, GenerationHints
from app.features.catalog import FEATURES, FEATURE_LIST
from app.features.generation import GENERATION_HINTS
from app.features.plugin_registry import build_remote_feature, schema_defaults, schema_required
from app.schemas.workflow import FeatureView


def test_all_builtins_have_reviewed_generation_metadata():
    assert set(GENERATION_HINTS) == set(FEATURES)
    assert len(FEATURE_LIST) == 23
    for feature in FEATURE_LIST:
        hints = feature.generation_hints
        assert isinstance(hints, GenerationHints), feature.key
        assert hints.when_to_use
        assert hints.prerequisites
        assert hints.avoid
        assert hints.evidence_required
        assert GenerationHints.model_validate_json(hints.model_dump_json()) == hints
        assert "x-siteflow-generation" not in feature.config_schema()
        assert "generation_hints" not in feature.config_schema().get("properties", {})
        for example in hints.examples:
            assert example["feature_type"] == feature.key
            feature.validate_full(example["config"])
            assert "selector" not in json.dumps(example)
    assert FEATURES["action.wait"].generation_hints.examples
    assert FEATURES["flow.open"].generation_hints.examples


def test_generation_hints_are_optional_and_not_added_to_feature_view(client):
    feature = replace(FEATURES["action.wait"], generation_hints=None)
    assert feature.generation_hints is None
    assert feature.validate_full({}) == {"seconds": 2}
    assert feature.config_schema() == FEATURES["action.wait"].config_schema()
    assert "generation_hints" not in FeatureView.model_fields
    response = client.get("/api/v1/features")
    assert response.status_code == 200
    for item in response.json():
        assert "generation_hints" not in item
        assert "x-siteflow-generation" not in item["config_schema"]


def test_sensitive_hints_cover_security_and_result_semantics():
    for key in ("action.fill", "auth.form_login", "auth.password_verify"):
        assert "密码" in " ".join(FEATURES[key].generation_hints.avoid)
    assert "不证明验证成功" in " ".join(FEATURES["auth.password_verify"].generation_hints.avoid)
    purchase = FEATURES["post.buy_card"].generation_hints
    assert "授权" in purchase.when_to_use
    assert "自动" in " ".join(purchase.avoid)
    resign = FEATURES["post.resign_click"].generation_hints
    assert "pending" in " ".join(resign.prerequisites)
    assert "过去日期" in " ".join(resign.prerequisites)
    dialog = FEATURES["post.resign_dialog"].generation_hints
    assert "直接消费" in dialog.when_to_use
    assert "pending" in " ".join(dialog.prerequisites)
    for text in ("明确就绪", "unknown", "卡数", "不做模板"):
        assert text in " ".join(dialog.avoid)
    for text in ("fullmatch", "多个 year", "新出现", "稳定库存", "至少一项"):
        assert text in " ".join(dialog.evidence_required)
    for text in ("dialog_ready_selector", "可解析日期按钮", "合法空列表", "waiting_human",
                 "不得忽略年份截取月日", "DOM 完整日期矛盾时拒绝",
                 "baseline 在 prepare ACK + authorize 之后、实际点击之前"):
        assert text in " ".join(dialog.evidence_required)
    result = FEATURES["flow.result"].generation_hints
    assert "最终有效结果为 false 表示任务失败并结束任务" in " ".join(result.avoid)
    assert "改为 true" in " ".join(result.avoid)


def test_generation_hints_bounds_accept_limits_and_defaults_are_isolated():
    text = "x" * 500
    hints = GenerationHints(
        when_to_use="x" * 1000,
        prerequisites=[text] * 12,
        avoid=[text] * 12,
        evidence_required=[text] * 12,
        examples=[{}, {}, {}],
    )
    assert len(hints.examples) == 3
    first = GenerationHints(when_to_use="test")
    second = GenerationHints(when_to_use="test")
    first.avoid.append("changed")
    first.examples.append({"config": {}})
    assert second.avoid == []
    assert second.examples == []


@pytest.mark.parametrize("field,value", [
    ("when_to_use", "x" * 1001),
    ("when_to_use", 1),
    ("prerequisites", ["x"] * 13),
    ("avoid", ["x"] * 13),
    ("evidence_required", ["x"] * 13),
    ("prerequisites", ["x" * 501]),
    ("avoid", ["x" * 501]),
    ("evidence_required", ["x" * 501]),
    ("prerequisites", [1]),
    ("avoid", "not a list"),
    ("evidence_required", [None]),
    ("examples", [{}, {}, {}, {}]),
    ("examples", ["not an object"]),
    ("examples", [{"config": {"value": "x" * 12000}}]),
    ("examples", [{"config": {"value": float("nan")}}]),
    ("examples", [{"config": {"value": float("inf")}}]),
    ("examples", [{"config": {"value": float("-inf")}}]),
    ("examples", [{"config": {"value": object()}}]),
    ("examples", [{"config": {"value": {1, 2}}}]),
    ("unexpected", "forbidden"),
])
def test_generation_hints_reject_invalid_metadata(field, value):
    with pytest.raises(ValidationError):
        GenerationHints.model_validate({"when_to_use": "test", field: value})


def test_examples_serialized_size_boundary_and_finite_json():
    overhead = len(json.dumps([{"value": ""}], separators=(",", ":")))
    examples = [{"value": "x" * (12000 - overhead)}]
    assert GenerationHints(when_to_use="test", examples=examples).examples == examples
    examples[0]["value"] += "x"
    with pytest.raises(ValidationError):
        GenerationHints(when_to_use="test", examples=examples)
    hints = GenerationHints(when_to_use="test", examples=[{
        "config": {"nested": [None, True, False, 1, 0.5, "text", {"value": []}]},
    }])
    assert json.loads(hints.model_dump_json())["examples"] == hints.examples


def _plugin_item():
    return {
        "key": "plugin.acme.wait",
        "name": "Acme wait",
        "category": "action",
        "config_schema": {
            "type": "object",
            "properties": {"seconds": {"type": "integer", "minimum": 1, "default": 2}},
            "required": ["seconds"],
            "additionalProperties": False,
        },
    }


@pytest.mark.parametrize("annotation", [
    None,
    "invalid",
    [],
    {},
    {"when_to_use": "x" * 1001},
    {"when_to_use": "test", "unknown": True},
    {"when_to_use": "test", "examples": [{"value": float("nan")}]},
    {"when_to_use": "test", "examples": [{"value": "x" * 12000}]},
])
def test_plugin_malformed_hints_do_not_invalidate_feature_or_config(annotation):
    item = _plugin_item()
    item["config_schema"]["x-siteflow-generation"] = annotation
    feature = build_remote_feature("acme", item)
    assert feature.generation_hints is None
    assert feature.validate_full({"seconds": 2}) == {"seconds": 2}
    assert feature.validate_override({}) == {}
    assert feature.validate_override({"seconds": None}) == {"seconds": None}
    feature.validate_field("seconds", 2)
    assert schema_defaults(feature.config_schema()) == {"seconds": 2}
    assert schema_required(feature.config_schema()) == ["seconds"]


def test_plugin_only_top_level_schema_annotation_is_used():
    item = _plugin_item()
    item["generation_hints"] = {"when_to_use": "not part of the protocol"}
    item["config_schema"]["properties"]["seconds"]["x-siteflow-generation"] = {
        "when_to_use": "not a top-level annotation",
    }
    feature = build_remote_feature("acme", item)
    assert feature.generation_hints is None
    assert feature.validate_full({"seconds": 2}) == {"seconds": 2}


def test_plugin_valid_hints_do_not_change_configuration_semantics():
    item = _plugin_item()
    original = build_remote_feature("acme", deepcopy(item))
    annotation = GenerationHints(
        when_to_use="Wait for the observed asynchronous page update.",
        prerequisites=["Confirm the delay is needed."],
        avoid=["Do not treat a delay as proof of success."],
        evidence_required=["Observed page update."],
        examples=[{"feature_type": item["key"], "config": {"seconds": 2}}],
    ).model_dump(mode="json")
    item["config_schema"]["x-siteflow-generation"] = annotation
    feature = build_remote_feature("acme", item)
    assert feature.generation_hints.model_dump(mode="json") == annotation
    assert feature.config_schema() == item["config_schema"]
    for config in ({"seconds": 2}, {"seconds": 0}, {}, {"x-siteflow-generation": {}}):
        for method in ("validate_full", "validate_override"):
            try:
                expected = getattr(original, method)(config)
            except FeatureConfigError:
                with pytest.raises(FeatureConfigError):
                    getattr(feature, method)(config)
            else:
                assert getattr(feature, method)(config) == expected
    for value in (None, 2, 0, "invalid"):
        try:
            original.validate_field("seconds", value)
        except FeatureConfigError:
            with pytest.raises(FeatureConfigError):
                feature.validate_field("seconds", value)
        else:
            feature.validate_field("seconds", value)
    assert schema_defaults(feature.schema) == schema_defaults(original.schema)
    assert schema_required(feature.schema) == schema_required(original.schema)


def test_plugin_generation_hints_are_copy_isolated():
    item = _plugin_item()
    annotation = {
        "when_to_use": "Wait for a page update.",
        "prerequisites": ["Observed asynchronous update."],
        "examples": [{"config": {"seconds": 2, "nested": [1, {"value": "original"}]}}],
    }
    item["config_schema"]["x-siteflow-generation"] = deepcopy(annotation)
    feature = build_remote_feature("acme", item)
    item["config_schema"]["x-siteflow-generation"]["prerequisites"].clear()
    schema_copy = feature.config_schema()
    schema_copy["x-siteflow-generation"]["examples"].clear()
    first = feature.generation_hints
    first.prerequisites.clear()
    first.examples[0]["config"]["nested"][1]["value"] = "changed"
    first.examples.append({})
    second = feature.generation_hints
    assert second is not first
    assert second.prerequisites == annotation["prerequisites"]
    assert second.examples == annotation["examples"]
    assert feature.config_schema()["x-siteflow-generation"] == annotation
