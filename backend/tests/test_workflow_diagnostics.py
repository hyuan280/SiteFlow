from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from unittest.mock import Mock

from jsonschema import Draft202012Validator
import pytest
from pydantic import Field, ValidationError, field_validator, model_validator

from app.features.base import FeatureConfig, FeatureConfigError
from app.features.catalog import get_feature
from app.models.setting import AppSetting
from app.models.workflow import Template, WorkflowStep
from app.schemas.config_ai import GenerationSite
from app.schemas.workflow import SiteWrite, TemplateWrite
from app.services.config_ai_tools import ConfigAITools
from app.services.workflow_diagnostics import (
    WorkflowDiagnostic,
    feature_diagnostics,
    jsonschema_diagnostics,
    public_diagnostics,
    safe_errors,
    validation_diagnostics,
)
from app.services.workflows import (
    WorkflowConflictError, WorkflowNotFoundError, WorkflowService, WorkflowValidationError,
)
from tests.test_config_ai_tools import SITE, _consult, _draft, _submit
from tests.test_workflow_drafts import _read_only


SECRET = "private-secret-provider-body"


@pytest.fixture
def tools(client):
    with client.app.state.session_factory() as session:
        yield ConfigAITools(WorkflowService(session), GenerationSite(**SITE), [
            "action.wait", "action.click", "flow.open", "auth.form_login", "challenge.turnstile",
            "flow.jump", "challenge.2fa",
        ])


def _assert_issue(result, code, path):
    assert result["ok"] is False
    assert 1 <= len(result["diagnostics"]) <= 10
    issue = result["diagnostics"][0]
    assert issue["code"] == code
    assert issue["path"] == path
    assert issue["message"]
    assert SECRET not in json.dumps(result)
    return issue


@pytest.mark.parametrize("name", ["validate_workflow_draft", "submit_workflow_draft"])
@pytest.mark.parametrize("changes,code,field", [
    ({"group_key": "missing"}, "group_missing", "group_key"),
    ({"condition": "signal:missing:true"}, "signal_missing", "condition"),
    ({"condition": f"all({SECRET},)"}, "condition_invalid", "condition"),
])
def test_references_and_condition_diagnostics(tools, client, name, changes, code, field):
    _consult(tools)
    draft = _draft(**changes)
    original = deepcopy(draft)
    args = _submit(draft) if name.startswith("submit") else {"draft": draft}
    with _read_only(client):
        issue = _assert_issue(tools.dispatch(name, args), code, ["draft", "steps", 0, field])
    assert issue["step_key"] == "first"
    assert issue["feature_type"] == "action.wait"
    assert draft == original
    assert tools.proposal is None


def test_disabled_signal_uses_condition_path_not_private_label(tools):
    _consult(tools)
    draft = _draft(condition="signal:ready:true")
    draft["signals"] = [{"signal_key": "ready", "label": SECRET,
                         "include_texts": [SECRET], "enabled": False}]
    _assert_issue(tools.dispatch("validate_workflow_draft", {"draft": draft}),
                  "signal_disabled", ["draft", "steps", 0, "condition"])


def test_full_validation_maps_execution_order_back_to_original_draft_index(tools):
    _consult(tools)
    draft = _draft(position=9000)
    draft["steps"].append({"step_key": "second", "feature_type": "action.wait",
                           "position": 1000, "condition": "signal:missing:true"})
    issue = _assert_issue(tools.dispatch("validate_workflow_draft", {"draft": draft}),
                          "signal_missing", ["draft", "steps", 1, "condition"])
    assert issue["step_key"] == "second"


@pytest.mark.parametrize("entry,code,field", [
    ({"signal_key": "ready", "label": SECRET}, "signal_evidence_required", "include_texts"),
    ({"signal_key": "sys_resign_pending", "label": SECRET,
      "include_texts": [SECRET]}, "signal_reserved", "signal_key"),
])
def test_signal_definition_diagnostics(tools, entry, code, field):
    draft = {"steps": [], "groups": [], "signals": [entry]}
    _assert_issue(tools.dispatch("validate_workflow_draft", {"draft": draft}),
                  code, ["draft", "signals", 0, field])


def test_feature_denial_and_schema_required_identify_the_step(tools):
    _assert_issue(tools.dispatch("validate_workflow_draft", {"draft": _draft(SECRET)}),
                  "feature_not_allowed", ["draft", "steps", 0, "feature_type"])
    issue = _assert_issue(tools.dispatch("validate_workflow_draft", {"draft": _draft()}),
                          "schema_required", ["draft", "steps", 0, "feature_type"])
    assert issue["feature_type"] == "action.wait"


@pytest.mark.parametrize("collection,key,entry,code", [
    ("steps", "step_key", {"step_key": "same", "feature_type": "action.wait"}, "duplicate_step"),
    ("signals", "signal_key", {"signal_key": "same", "label": SECRET,
                             "include_texts": [SECRET]}, "duplicate_signal"),
    ("groups", "group_key", {"group_key": "same", "name": SECRET}, "duplicate_group"),
])
def test_duplicate_entries_identify_second_local_index(tools, collection, key, entry, code):
    _consult(tools)
    draft = {"steps": [], "groups": [], "signals": [], collection: [entry, deepcopy(entry)]}
    _assert_issue(tools.dispatch("submit_workflow_draft", _submit(draft)),
                  code, ["draft", collection, 1, key])


def test_singleton_and_order_collision(tools):
    _consult(tools, "flow.open", "action.wait")
    draft = _draft("flow.open")
    draft["steps"].append({"step_key": "second", "feature_type": "flow.open"})
    issue = _assert_issue(tools.dispatch("validate_workflow_draft", {"draft": draft}),
                          "singleton_duplicate", ["draft", "steps", 1, "enabled"])
    assert issue["feature_type"] == "flow.open"
    draft = _draft(position=1000)
    draft["steps"].append({"step_key": "second", "feature_type": "action.wait", "position": 1000})
    _assert_issue(tools.dispatch("validate_workflow_draft", {"draft": draft}),
                  "order_collision", ["draft", "steps", 0, "position"])


@pytest.mark.parametrize("config,code,path", [
    ({"retry_count": -1}, "field_bounds", ["retry_count"]),
    ({"wait_until": SECRET}, "field_enum", ["wait_until"]),
    ({SECRET: SECRET}, "extra_fields", []),
])
def test_config_schema_diagnostics_are_value_free(tools, config, code, path):
    _consult(tools, "flow.open")
    issue = _assert_issue(tools.dispatch("submit_workflow_draft", _submit(_draft("flow.open", config))),
                          code, ["draft", "steps", 0, "config", *path])
    if path:
        assert issue["field"] == path[0]
    if code == "field_bounds":
        assert issue["hint"] == "ge=0"


@pytest.mark.parametrize("selector,code", [(["#ok", SECRET, 17], "field_type"), (["#ok", ""], "field_bounds")])
def test_nested_selector_index_survives_schema_validation(tools, selector, code):
    _consult(tools, "action.click")
    index = 2 if code == "field_type" else 1
    _assert_issue(tools.dispatch("validate_workflow_draft", {"draft": _draft("action.click", {"selector": selector})}),
                  code, ["draft", "steps", 0, "config", "selector", index])


@pytest.mark.parametrize("name", ["validate_workflow_draft", "submit_workflow_draft"])
def test_override_error_with_duplicate_key_uses_current_step_index(tools, name):
    _consult(tools, "action.click")
    draft = _draft("action.click", {"selector": ["#valid"]})
    draft["steps"].append({"step_key": "first", "feature_type": "action.click",
                           "config": {"selector": ["   "]}})
    args = _submit(draft) if name.startswith("submit") else {"draft": draft}
    issue = _assert_issue(tools.dispatch(name, args), "field_bounds",
                          ["draft", "steps", 1, "config", "selector", 0])
    assert issue["step_key"] == "first"
    assert issue["feature_type"] == "action.click"


@pytest.mark.parametrize("feature,config,code,field", [
    ("flow.jump", {"texts": [SECRET], "target_step_key": "bad:key"},
     "flow_jump_target_invalid", "target_step_key"),
    ("flow.jump", {"texts": [SECRET], "target_step_key": "   "},
     "flow_jump_target_required", "target_step_key"),
    ("flow.jump", {"target_step_key": "first"}, "flow_jump_evidence_required", "texts"),
    ("challenge.2fa", {"service_hint": "   "}, "totp_service_hint_required", "service_hint"),
])
def test_builtin_model_rules_provide_actionable_field_diagnostics(tools, feature, config, code, field):
    _consult(tools, feature)
    issue = _assert_issue(tools.dispatch("submit_workflow_draft", _submit(_draft(feature, config))),
                          code, ["draft", "steps", 0, "config", field])
    assert issue["field"] == field
    assert issue["feature_type"] == feature


@pytest.mark.parametrize("feature,config,code,field,legacy_message", [
    ("challenge.2fa", {"service_hint": " "}, "totp_service_hint_required", "service_hint",
     "service_hint is required for every 2FA provider"),
    ("flow.jump", {}, "flow_jump_evidence_required", "texts",
     "至少需要配置一项页面文字或选择器"),
    ("flow.jump", {"texts": [SECRET]}, "flow_jump_target_required", "target_step_key",
     "跳转到指定步骤时需要填写目标步骤 key"),
    ("flow.jump", {"texts": [SECRET], "target_step_key": "bad:key"},
     "flow_jump_target_invalid", "target_step_key", "目标步骤 key 不能包含冒号"),
    ("post.buy_card", {"layout": "custom"}, "buy_card_product_selectors_required",
     "product_selectors", "自定义布局必须提供商品选择器 product_selectors"),
    ("post.buy_card", {"layout": "grid"}, "buy_card_price_selectors_required",
     "price_selectors", "网格卡片和自定义布局必须提供价格选择器 price_selectors"),
    ("post.resign_click", {"calendar_title_selector": ["{secret}"]},
     "resign_selector_template_invalid", "calendar_title_selector",
     "Playwright 选择器模板仅支持 {date}/{day}/{month}/{year}，不支持其他花括号语法"),
    ("post.resign_click", {"calendar_day_selector": ["#day"]},
     "resign_selector_date_required", "calendar_day_selector",
     "每个目标日期选择器模板必须包含 {date} 或 {day}"),
])
def test_semantic_metadata_preserves_exact_admin_error_text(feature, config, code, field, legacy_message):
    feature = get_feature(feature)
    with pytest.raises(FeatureConfigError) as caught:
        feature.validate_full(config)
    prefix = f"{field}: " if code.startswith("resign_") else ""
    assert str(caught.value) == f"{prefix}Value error, {legacy_message}"
    assert caught.value.errors[0]["type"] == code
    assert SECRET not in json.dumps(caught.value.errors)
    issues = feature_diagnostics(caught.value, "first", feature.key)
    assert issues[0].path == ("steps", "config", field)
    result = public_diagnostics(issues)
    assert result[0]["code"] == code
    assert result[0]["field"] == field
    assert SECRET not in json.dumps(result)


def test_unknown_model_validator_keeps_value_free_fallback():
    class PrivateModelValidator(FeatureConfig):
        value: str

        @model_validator(mode="after")
        def fail(self):
            raise ValueError(SECRET)

    feature = replace(get_feature("action.click"), config_model=PrivateModelValidator)
    with pytest.raises(ValidationError) as raw:
        PrivateModelValidator.model_validate({"value": SECRET})
    assert raw.value.errors()[0]["input"] == {"value": SECRET}
    assert SECRET in str(raw.value.errors()[0]["ctx"]["error"])
    with pytest.raises(FeatureConfigError) as caught:
        feature.validate_full({"value": SECRET})
    assert str(caught.value) == f"Value error, {SECRET}"
    assert caught.value.errors == [{"loc": (), "type": "value_error", "ctx": {}}]
    result = public_diagnostics(feature_diagnostics(caught.value, "first", feature.key))
    assert result[0]["code"] == "field_invalid"
    assert result[0]["path"] == ["steps", "config"]
    assert SECRET not in json.dumps(result)


def test_custom_required_field_and_private_invalid_defaults(tools, client):
    key = "feature.auth.form_login"
    with client.app.state.session_factory() as writer:
        writer.add(AppSetting(key=key, value_json=json.dumps({
            "defaults": {"submit_selector": None, "username_field": SECRET},
            "required_fields": ["submit_selector"],
        })))
        writer.commit()
    _consult(tools, "auth.form_login")
    with _read_only(client):
        issue = _assert_issue(tools.dispatch("submit_workflow_draft", _submit(_draft("auth.form_login"))),
                              "field_required", ["draft", "steps", 0, "config", "submit_selector"])
    assert issue["field"] == "submit_selector"
    with client.app.state.session_factory() as writer:
        writer.get(AppSetting, key).value_json = json.dumps({
            "defaults": {"submit_selector": [SECRET, 12]}, "required_fields": [],
        })
        writer.commit()
    _assert_issue(tools.dispatch("validate_workflow_draft", {"draft": _draft("auth.form_login")}),
                  "field_type", ["draft", "steps", 0, "config", "submit_selector", 1])


def test_feature_errors_preserve_admin_text_but_sanitize_details():
    feature = get_feature("action.click")
    for validate in (feature.validate_override, feature.validate_full):
        with pytest.raises(FeatureConfigError) as caught:
            validate({"selector": [SECRET, 42]})
        assert caught.value.errors[0]["loc"] == ("selector", 1)
        assert caught.value.errors[0]["type"] == "string_type"
        assert SECRET not in json.dumps(caught.value.errors)
        assert "selector" in str(caught.value)

    class PrivateValidator(FeatureConfig):
        value: str = Field(min_length=2)

        @field_validator("value")
        @classmethod
        def fail(cls, value):
            raise ValueError(SECRET)

    feature = replace(feature, config_model=PrivateValidator)
    with pytest.raises(FeatureConfigError) as caught:
        feature.validate_full({"value": SECRET})
    assert SECRET in str(caught.value)  # Existing administrator behavior is unchanged.
    assert caught.value.errors == [{"loc": ("value",), "type": "value_error", "ctx": {}}]


def test_error_projection_drops_inputs_messages_and_unsafe_context():
    errors = [{"loc": ("selector", 2), "type": "too_short", "input": SECRET,
               "msg": SECRET, "ctx": {"error": ValueError(SECRET), "expected": SECRET,
                                      "min_length": 1, "max_length": SECRET}},
              {"loc": (SECRET,), "type": "extra_forbidden", "input": SECRET}]
    safe = safe_errors(errors, {"selector"})
    assert safe[0] == {"loc": ("selector", 2), "type": "too_short", "ctx": {"min_length": 1}}
    assert safe[1]["loc"] == ()
    result = public_diagnostics(validation_diagnostics(safe))
    assert result[0]["hint"] == "min_length=1"
    assert SECRET not in json.dumps(result)


def test_argument_errors_are_bounded_and_unknown_property_names_are_removed(tools):
    draft = _draft()
    draft["steps"] = [{"step_key": "ok", "feature_type": "action.wait", "timeout_seconds": -1}] * 20
    result = tools.dispatch("validate_workflow_draft", {"draft": draft})
    _assert_issue(result, "field_bounds", ["draft", "steps", 0, "timeout_seconds"])
    assert len(result["diagnostics"]) == 10
    assert result["diagnostics"][0]["hint"] == "ge=1"
    for target in (draft, draft["steps"][0]):
        target[SECRET] = SECRET
    draft["steps"] = []
    _assert_issue(tools.dispatch("validate_workflow_draft", {"draft": draft}), "extra_fields", ["draft"])


def test_jsonschema_required_uses_schema_keys_and_nested_extra_does_not_echo_name():
    validator = Draft202012Validator({"type": "object", "properties": {
        "config": {"type": "object", "properties": {"selector": {"type": "string"}},
                   "required": ["selector"], "additionalProperties": False}},
    })
    result = public_diagnostics(jsonschema_diagnostics(validator, {"config": {SECRET: SECRET}}))
    assert {tuple(issue["path"]) for issue in result} == {("config",), ("config", "selector")}
    assert {issue["code"] for issue in result} == {"field_required", "extra_fields"}
    assert SECRET not in json.dumps(result)


def test_cfbox_unavailable_identifies_feature(tools, monkeypatch):
    from app.services.workflow_drafts import _DraftWorkflowService

    monkeypatch.setattr(_DraftWorkflowService, "_cfbox_enabled", lambda self: False)
    _consult(tools, "challenge.turnstile")
    issue = _assert_issue(tools.dispatch("validate_workflow_draft", {"draft": _draft("challenge.turnstile")}),
                          "cfbox_required", ["draft", "steps", 0, "feature_type"])
    assert issue["feature_type"] == "challenge.turnstile"


def test_missing_template_and_name_conflict_have_source_metadata(tools):
    service = tools._service
    with pytest.raises(WorkflowNotFoundError) as caught:
        service.validate_draft("site", SiteWrite(**SITE, template_id=999))
    assert caught.value.diagnostics == [WorkflowDiagnostic("template_missing", ("template_id",))]
    service.session.add(Template(name=SECRET))
    service.session.commit()
    with pytest.raises(WorkflowConflictError) as caught:
        service.validate_draft("template", TemplateWrite(name=SECRET))
    assert str(caught.value) == f"名称已存在: {SECRET}"
    assert public_diagnostics(caught.value.diagnostics)[0]["code"] == "name_conflict"
    assert SECRET not in json.dumps(public_diagnostics(caught.value.diagnostics))


def test_inherited_feature_mismatch_and_unsupported_version_metadata(tools):
    service = tools._service
    row = WorkflowStep(step_key="first", feature_type="action.wait", feature_version=99)
    with pytest.raises(WorkflowValidationError) as caught:
        service._apply_step({}, row, "site", [0])
    assert caught.value.diagnostics[0].code == "feature_version_unsupported"
    row.feature_version = 1
    row.config_json = "{}"
    steps = {}
    service._apply_step(steps, row, "site", [0])
    row.feature_type = "flow.open"
    with pytest.raises(WorkflowValidationError) as caught:
        service._apply_step(steps, row, "site", [1])
    assert caught.value.diagnostics[0].code == "feature_inheritance_mismatch"


@pytest.mark.parametrize("exception,code", [
    (WorkflowValidationError, "workflow_invalid"), (WorkflowConflictError, "workflow_conflict"),
    (FeatureConfigError, "config_not_allowed"), (RuntimeError, "tool_failed"),
])
def test_unknown_errors_are_not_parsed_or_echoed(tools, monkeypatch, exception, code):
    _consult(tools)
    monkeypatch.setattr(tools._service, "validate_draft", Mock(side_effect=exception(
        f"步骤 first (action.wait) submit_selector: {SECRET}",
    )))
    _assert_issue(tools.dispatch("submit_workflow_draft", _submit(_draft())), code, [])
    assert tools.proposal is None
