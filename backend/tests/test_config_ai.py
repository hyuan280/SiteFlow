from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

from jsonschema import Draft202012Validator
import pytest
from pydantic import Field, ValidationError, create_model
from sqlalchemy import select

from app.api.v1 import config_ai
from app.api.v1.workflow_drafts import _DraftRoute
from app.core.auth import CSRF_COOKIE, SESSION_COOKIE
from app.features import plugin_registry
from app.features.base import FeatureConfig
from app.features.catalog import FEATURES, FEATURE_LIST
from app.models.auth import AuthSession
from app.models.setting import AppSetting
from app.services import config_ai_catalog
from app.services.config_ai_catalog import ConfigAICatalog
from app.services.config_ai_protocol import (
    GetFeatureSchemaArgs,
    ListFeaturesArgs,
    SubmitWorkflowDraftArgs,
    ValidateWorkflowDraftArgs,
    WorkflowProposal,
    _strip_schema_annotations,
    generation_guide,
    tool_definitions,
)
from tests.test_workflow_drafts import _create, _read_only, _validate


PREFIX = "/api/v1/config-ai"
ENDPOINTS = ["/guide", "/tools", "/features", "/features?category=auth",
             "/features/action.wait"]
EMPTY_DRAFT = {"steps": [], "groups": [], "signals": []}
TOOL_MODELS = {
    "list_features": ListFeaturesArgs,
    "get_feature_schema": GetFeatureSchemaArgs,
    "validate_workflow_draft": ValidateWorkflowDraftArgs,
    "submit_workflow_draft": SubmitWorkflowDraftArgs,
}


def _get(client, path, status=200):
    with _read_only(client):
        response = client.get(PREFIX + path)
    assert response.status_code == status, response.text
    assert response.headers.get("cache-control") == "no-store"
    return response.json()


def _assert_no_annotations(schema):
    if isinstance(schema, dict):
        assert "default" not in schema
        assert "examples" not in schema
        for value in schema.values():
            _assert_no_annotations(value)
    elif isinstance(schema, list):
        for value in schema:
            _assert_no_annotations(value)


@pytest.mark.parametrize("path", ENDPOINTS)
@pytest.mark.parametrize("auth_state", ["current", "older", "expired", "anonymous", "invalid"])
def test_catalog_auth_is_read_only_and_uncacheable_without_csrf(client, path, auth_state):
    client.headers.pop("X-CSRF-Token")
    client.cookies.delete(CSRF_COOKIE, domain="testserver.local", path="/")
    if auth_state in {"older", "expired"}:
        with client.app.state.session_factory() as session:
            auth = session.scalar(select(AuthSession))
            auth.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=5)
            if auth_state == "expired":
                auth.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            session.commit()
    elif auth_state == "anonymous":
        client.cookies.clear()
    elif auth_state == "invalid":
        client.cookies.set(SESSION_COOKIE, "not-a-session", domain="testserver.local", path="/")
    _get(client, path, 200 if auth_state in {"current", "older"} else 401)


def test_router_registers_discovery_and_generation_but_no_public_tool_execution(client):
    assert {(route.path, frozenset(route.methods)) for route in config_ai.router.routes} == {
        (PREFIX + path, frozenset({"GET"}))
        for path in ("/guide", "/tools", "/features", "/features/{feature_type}")
    } | {(PREFIX + "/generate", frozenset({"POST"}))}
    assert all(isinstance(route, _DraftRoute) for route in config_ai.router.routes)
    tools = _get(client, "/tools")
    assert tools == {
        "version": 1, "execution_enabled": False, "tools": tool_definitions(),
        "generation_endpoint": PREFIX + "/generate",
        "execution_scope": (
            "Tools execute only inside an enabled, authorized new-site generation request."
        ),
    }
    for name in TOOL_MODELS:
        with _read_only(client):
            response = client.post(PREFIX + "/tools/" + name, json={"draft": EMPTY_DRAFT})
        assert response.status_code == 404
    with _read_only(client):
        assert client.post(PREFIX + "/tools", json={}).status_code == 405


def test_guide_is_versioned_trusted_protocol_and_copy_isolated(client):
    guide = _get(client, "/guide")
    assert guide == generation_guide()
    assert guide["version"] == 1
    assert guide["limitations"]
    for text in ("untrusted data", "FULL REPLACEMENT", "Never flatten", "explicit user approval",
                 "NOT model-facing safe output", "host must bind", "runtime secrets"):
        assert text in guide["guide"]
    generation_guide()["limitations"].clear()
    assert generation_guide() == guide


def test_catalog_lists_only_reviewed_builtins_and_filters_categories(client, monkeypatch):
    body = _get(client, "/features")
    reviewed = [feature for feature in FEATURE_LIST if feature.generation_hints is not None]
    assert body["version"] == 1
    assert "excluded" in body["plugin_policy"]
    assert [item["feature_type"] for item in body["features"]] == [f.key for f in reviewed]
    for item, feature in zip(body["features"], reviewed):
        assert item == {
            "feature_type": feature.key, "name": feature.name,
            "description": feature.description, "category": feature.category,
            "cardinality": feature.cardinality, "requires": sorted(feature.requires),
            "version": feature.version, "source": "builtin",
            "when_to_use": feature.generation_hints.when_to_use,
        }
    assert _get(client, "/features?category=auth")["features"] == [
        item for item in body["features"] if item["category"] == "auth"
    ]
    _get(client, "/features?category=not-a-category", 422)
    monkeypatch.setattr(config_ai_catalog, "FEATURE_LIST", (
        replace(FEATURES["action.wait"], generation_hints=None),
    ))
    assert _get(client, "/features")["features"] == []
    _get(client, "/features/action.wait", 404)


def test_plugin_features_and_dynamic_captcha_metadata_never_enter_catalog(client, monkeypatch):
    marker = "UNTRUSTED-PLUGIN-INSTRUCTIONS"
    remote = plugin_registry.build_remote_feature("injected", {
        "key": "plugin.injected.wait", "name": marker, "description": marker,
        "category": "action", "config_schema": {
            "type": "object", "properties": {"value": {"type": "string", "default": marker}},
            "x-siteflow-generation": {"when_to_use": marker},
        },
    })
    method = plugin_registry.RemoteCaptchaMethod(
        key="plugin.injected.captcha", label=marker, description=marker,
        supports_vision=True, plugin_id="injected",
    )
    monkeypatch.setattr(plugin_registry, "_plugin_features", {remote.key: remote})
    monkeypatch.setattr(plugin_registry, "_plugin_captcha_methods", {method.key: method})
    # Prove the injection is active in the normal administrator-facing schema.
    admin = client.get("/api/v1/features")
    assert admin.status_code == 200, admin.text
    assert marker in admin.text
    assert remote.key in {item["key"] for item in admin.json()}
    captcha = FEATURES["challenge.image_captcha"].config_schema()["properties"]["captcha_method"]
    assert captcha["enum_descriptions"][method.key] == marker
    for path in ENDPOINTS + ["/features/challenge.image_captcha", "/features/sign.iframe.image_captcha"]:
        serialized = json.dumps(_get(client, path))
        assert marker not in serialized
        assert remote.key not in serialized
        assert method.key not in serialized
    assert _get(client, "/features/" + remote.key, 404) == {
        "detail": "Feature unavailable for configuration generation",
    }


def test_current_admin_policy_required_fields_export_without_defaults_or_execution_settings(client):
    default_secret = "PRIVATE-ADMIN-DEFAULT"
    prompt_secret = "PRIVATE-EXECUTION-PROMPT"
    saved = client.put("/api/v1/features/flow.result/settings", json={
        "defaults": {"message": default_secret, "llm_prompt": default_secret},
        "required_fields": ["message", "llm_prompt", "llm_enabled"],
        "execution_settings": {"llm_enabled": True, "llm_prompt": prompt_secret},
    })
    assert saved.status_code == 200, saved.text
    body = _get(client, "/features/flow.result")
    assert body["required_fields"] == body["config_schema"]["required"] == ["message"]
    assert body["excluded_fields"] == ["llm_enabled", "llm_prompt"]
    assert body["user_input_required"] == ["llm_enabled", "llm_prompt"]
    for path in ENDPOINTS + ["/features/flow.result"]:
        serialized = json.dumps(_get(client, path))
        assert default_secret not in serialized
        assert prompt_secret not in serialized
    assert "defaults" not in body
    assert "execution_settings" not in body
    admin = client.get("/api/v1/features")
    assert admin.status_code == 200, admin.text
    result = next(item for item in admin.json() if item["key"] == "flow.result")
    assert result["defaults"]["message"] == default_secret
    assert result["execution_settings"]["llm_prompt"] == prompt_secret
    assert result["required_fields"] == saved.json()["required_fields"]
    for item in admin.json():
        assert "generation_hints" not in item
        assert "x-siteflow-generation" not in item["config_schema"]


def test_required_2fa_service_hint_cannot_be_relaxed_by_stored_policy(client):
    with client.app.state.session_factory() as session:
        session.add(AppSetting(key="feature.challenge.2fa", value_json=json.dumps({
            "defaults": {"service_hint": "PRIVATE-SERVICE"}, "required_fields": [],
        })))
        session.commit()
    body = _get(client, "/features/challenge.2fa")
    assert body["required_fields"] == body["config_schema"]["required"] == ["service_hint"]
    assert "PRIVATE-SERVICE" not in json.dumps(body)


def test_all_builtin_schemas_are_valid_sanitized_and_copy_isolated(client):
    originals = {feature.key: deepcopy(feature.config_model.model_json_schema()) for feature in FEATURE_LIST}
    for feature in FEATURE_LIST:
        body = _get(client, "/features/" + feature.key)
        schema = body["config_schema"]
        override = body["override_config_schema"]
        for exported in (schema, override):
            Draft202012Validator.check_schema(exported)
            _assert_no_annotations(exported)
            assert exported["additionalProperties"] is False
            assert not Draft202012Validator(exported).is_valid({"unknown": None})
        assert "required" not in override
        assert schema["required"] == body["required_fields"]
        expected_override = deepcopy(schema)
        expected_override.pop("required")
        expected_override["properties"] = {
            key: {"anyOf": [field, {"type": "null"}]}
            for key, field in schema["properties"].items()
        }
        assert override == expected_override
        assert body["generation_hints"] == feature.generation_hints.model_dump(mode="json")
        for key, field in schema["properties"].items():
            assert key not in {"llm_prompt", "llm_enabled"}
            assert not any(field.get(flag) for flag in ("ui:hidden", "readOnly", "writeOnly"))
            assert field.get("format") != "password"
        if feature.key == "challenge.2fa":
            assert "service_hint" in body["required_fields"]
        if feature.key == "post.buy_card":
            assert {"option_selector", "exchange_action", "submit_value"} <= set(body["excluded_fields"])
        body["generation_hints"]["avoid"].clear()
        with client.app.state.session_factory() as session, _read_only(client):
            direct = ConfigAICatalog(session).get_feature_schema(feature.key)
            assert direct["config_schema"] == schema
            direct["override_config_schema"]["properties"].clear()
            for definition in direct["override_config_schema"].get("$defs", {}).values():
                definition.clear()
            assert direct["config_schema"] == schema
            direct["config_schema"]["properties"].clear()
            assert ConfigAICatalog(session).get_feature_schema(feature.key)["config_schema"] == schema
        override["properties"].clear()
        schema["properties"].clear()
        assert feature.config_model.model_json_schema() == originals[feature.key]
        fresh = _get(client, "/features/" + feature.key)
        assert fresh["config_schema"]["properties"]
        assert fresh["override_config_schema"]["properties"]


def test_inherited_click_disable_accepts_empty_override_but_effective_requires_selector(client):
    body = _get(client, "/features/action.click")
    effective = Draft202012Validator(body["config_schema"])
    override = Draft202012Validator(body["override_config_schema"])
    assert "selector" in body["required_fields"]
    assert not effective.is_valid({})
    override.validate({})
    effective.validate({"selector": ["#observed-button"]})
    assert not override.is_valid({"selector": []})
    parent, _ = _create(client, steps=[{
        "step_key": "click", "feature_type": "action.click",
        "config": {"selector": ["#observed-button"]},
    }])
    step = {"step_key": "click", "feature_type": "action.click", "enabled": False, "config": {}}
    WorkflowProposal.model_validate({"steps": [step], "groups": [], "signals": []})
    draft = _validate(client, "templates", {
        "name": "disabled-inherited-click", "parent_id": parent["id"], "steps": [step],
    })
    assert draft["local"]["steps"][0]["config"] == {}
    resolved = draft["resolved"]["steps"][0]
    assert resolved["enabled"] is False
    assert resolved["config"]["selector"] == ["#observed-button"]
    effective.validate(resolved["config"])


def test_wait_null_clears_override_but_is_not_an_effective_value(client):
    body = _get(client, "/features/action.wait")
    override = Draft202012Validator(body["override_config_schema"])
    effective = Draft202012Validator(body["config_schema"])
    override.validate({"seconds": None})
    assert not effective.is_valid({"seconds": None})
    for invalid in (-1, "not-a-number"):
        assert not override.is_valid({"seconds": invalid})
        assert not effective.is_valid({"seconds": invalid})
    parent, _ = _create(client, steps=[{
        "step_key": "wait", "feature_type": "action.wait", "config": {"seconds": 17},
    }])
    draft = _validate(client, "templates", {
        "name": "cleared-inherited-wait", "parent_id": parent["id"], "steps": [{
            "step_key": "wait", "feature_type": "action.wait", "config": {"seconds": None},
        }],
    })
    assert draft["local"]["steps"][0]["config"] == {"seconds": None}
    resolved = draft["resolved"]["steps"][0]["config"]
    assert resolved == FEATURES["action.wait"].config_model().model_dump(mode="json")
    effective.validate(resolved)


def test_override_schema_keeps_nested_required_fields_and_references(client):
    body = _get(client, "/features/post.buy_card")
    effective = Draft202012Validator(body["config_schema"])
    override = Draft202012Validator(body["override_config_schema"])
    for validator in (effective, override):
        validator.validate({"purchase_clicks": [{"selectors": ["#observed-purchase"]}]})
        assert not validator.is_valid({"purchase_clicks": [{}]})
        assert not validator.is_valid({"purchase_clicks": [{"selectors": None}]})
    override.validate({"purchase_clicks": None})
    assert not effective.is_valid({"purchase_clicks": None})


def test_result_hints_distinguish_final_failure_from_model_overridable_output(client):
    body = _get(client, "/features/flow.result")
    avoid = " ".join(body["generation_hints"]["avoid"])
    for text in (
        "\u6700\u7ec8\u6709\u6548\u7ed3\u679c\u4e3a false \u8868\u793a\u4efb\u52a1\u5931\u8d25\u5e76\u7ed3\u675f\u4efb\u52a1",
        "\u8303\u56f4\u4e3a all", "output_value=false \u4ecd\u53ef\u80fd\u88ab\u6a21\u578b\u6539\u4e3a true",
        "\u7531\u5bbf\u4e3b\u786e\u8ba4\u53d7\u4fdd\u62a4\u7684\u5168\u5c40\u53ca\u6b65\u9aa4\u6a21\u578b\u5224\u65ad\u8bbe\u7f6e",
        "\u4e0d\u80fd\u53ea\u4f9d\u8d56 output_value=false",
    ):
        assert text in avoid
    assert "output_value=false \u8868\u793a\u4efb\u52a1\u5931\u8d25\u5e76\u7ed3\u675f\u4efb\u52a1" not in avoid


def test_schema_excludes_protected_fields_and_reports_required_user_input(client, monkeypatch):
    flags = {
        "hidden": {"ui:hidden": True}, "read_only": {"readOnly": True},
        "write_only": {"writeOnly": True}, "password": {"format": "password"},
        "llm_prompt": {}, "llm_enabled": {},
    }
    model = create_model("ProtectedConfig", __base__=FeatureConfig, visible=(str, ...), **{
        key: (str, Field(json_schema_extra=extra)) for key, extra in flags.items()
    })
    monkeypatch.setattr(config_ai_catalog, "FEATURE_LIST", (
        replace(FEATURES["action.wait"], config_model=model),
    ))
    body = _get(client, "/features/action.wait")
    assert set(body["config_schema"]["properties"]) == {"visible"}
    assert body["required_fields"] == ["visible"]
    assert body["excluded_fields"] == body["user_input_required"] == sorted(flags)
    for name in ("config_schema", "override_config_schema"):
        schema = body[name]
        assert set(schema["properties"]) == {"visible"}
        validator = Draft202012Validator(schema)
        for key in [*flags, "unknown"]:
            assert not validator.is_valid({"visible": "safe", key: None})


def test_catalog_reads_committed_scalar_policy_without_refresh_or_autoflush(client, monkeypatch):
    factory = client.app.state.session_factory
    key = "feature.action.wait"
    initial = json.dumps({"defaults": {"seconds": 3}, "required_fields": []})
    with factory() as writer:
        writer.add(AppSetting(key=key, value_json=initial))
        writer.commit()
    with factory() as session:
        held = session.get(AppSetting, key)
        catalog = ConfigAICatalog(session)
        with factory() as writer:
            writer.get(AppSetting, key).value_json = json.dumps({
                "defaults": {"seconds": 17}, "required_fields": ["seconds", "unknown-secret"],
            })
            writer.commit()
        assert held.value_json == initial
        with _read_only(client):
            assert catalog.get_feature_schema("action.wait")["required_fields"] == ["seconds"]
        assert held.value_json == initial
        held.value_json = json.dumps({"required_fields": []})
        pending = AppSetting(key="pending-unrelated", value_json="private-pending-value")
        session.add(pending)
        session.autoflush = True
        before = (set(session.new), set(session.dirty), set(session.deleted))

        def forbidden(*args, **kwargs):
            pytest.fail("Catalog must not flush or change the caller's transaction")

        with monkeypatch.context() as patch, _read_only(client):
            for method in ("flush", "commit", "rollback"):
                patch.setattr(session, method, forbidden)
            body = catalog.get_feature_schema("action.wait")
            assert body["required_fields"] == ["seconds"]
            assert "unknown-secret" not in json.dumps(body)
        assert held.value_json == json.dumps({"required_fields": []})
        assert (set(session.new), set(session.dirty), set(session.deleted)) == before


@pytest.mark.parametrize("raw", [
    "PRIVATE-MALFORMED-JSON", "null", "[]", '"PRIVATE-POLICY"',
    '{"defaults":{"message":"PRIVATE-DEFAULT"}}',
    '{"required_fields":"PRIVATE-REQUIRED"}',
    '{"required_fields":[{"PRIVATE-KEY":"PRIVATE-VALUE"}]}',
    '{"required_fields":null}',
])
def test_malformed_policy_is_generic_uncacheable_422_without_writes_or_value_leaks(client, raw):
    with client.app.state.session_factory() as session:
        session.add(AppSetting(key="feature.action.wait", value_json=raw))
        auth = session.scalar(select(AuthSession))
        auth.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        session.commit()
    assert _get(client, "/features/action.wait", 422) == {
        "detail": "Feature generation policy unavailable",
    }


@pytest.mark.parametrize("key", ["unknown-private-value", "plugin.private.instructions", "sign.iframe_form"])
def test_unknown_or_unreviewed_feature_returns_generic_404_without_echo(client, key):
    with client.app.state.session_factory() as session:
        auth = session.scalar(select(AuthSession))
        auth.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        session.commit()
    assert _get(client, "/features/" + key, 404) == {
        "detail": "Feature unavailable for configuration generation",
    }


def test_four_tool_schemas_are_draft202012_valid_and_do_not_mutate_models_or_results():
    originals = {name: deepcopy(model.model_json_schema()) for name, model in TOOL_MODELS.items()}
    draft = {"steps": [{"step_key": "wait", "feature_type": "action.wait",
                        "enabled": False, "config": {"seconds": None}}],
             "groups": [{"group_key": "local", "name": "Local"}],
             "signals": [{"signal_key": "ready", "label": "Ready", "enabled": False}]}
    examples = {
        "list_features": {"category": "auth"},
        "get_feature_schema": {"feature_type": "action.wait"},
        "validate_workflow_draft": {"draft": draft},
        "submit_workflow_draft": {"draft": draft, "explanation": "Review",
                                  "unresolved_questions": []},
    }
    definitions = tool_definitions()
    assert [item["function"]["name"] for item in definitions] == list(TOOL_MODELS)
    for item in definitions:
        assert item["type"] == "function"
        function = item["function"]
        schema = function["parameters"]
        Draft202012Validator.check_schema(schema)
        _assert_no_annotations(schema)
        assert schema["additionalProperties"] is False
        assert "strict" not in function
        assert TOOL_MODELS[function["name"]].model_json_schema() == originals[function["name"]]
        example = examples[function["name"]]
        original = deepcopy(example)
        Draft202012Validator(schema).validate(example)
        TOOL_MODELS[function["name"]].model_validate(example)
        assert example == original
    baseline = deepcopy(definitions)
    definitions[0]["function"]["parameters"]["properties"].clear()
    assert tool_definitions() == baseline


@pytest.mark.parametrize("model,payload", [
    (ListFeaturesArgs, {}), (GetFeatureSchemaArgs, {"feature_type": "action.wait"}),
    (ValidateWorkflowDraftArgs, {"draft": EMPTY_DRAFT}),
    (SubmitWorkflowDraftArgs, {"draft": EMPTY_DRAFT, "explanation": "Review", "unresolved_questions": []}),
])
@pytest.mark.parametrize("key,value", [
    ("owner_id", 1), ("owner_type", "site"), ("site_id", 1), ("template_id", 1),
    ("expected_revision", 1), ("expected_history_key", "private-history"),
    ("credentials", {}), ("trusted_urls", []), ("enabled", True),
])
def test_tool_arguments_forbid_host_owned_metadata(model, payload, key, value):
    model.model_validate(payload)
    with pytest.raises(ValidationError):
        model.model_validate({**payload, key: value})
    schema = next(item["function"]["parameters"] for item in tool_definitions()
                  if TOOL_MODELS[item["function"]["name"]] is model)
    assert not Draft202012Validator(schema).is_valid({**payload, key: value})


@pytest.mark.parametrize("model", [ValidateWorkflowDraftArgs, SubmitWorkflowDraftArgs])
def test_draft_arguments_require_full_replacement_arrays_and_forbid_owner_fields(model):
    base = {"draft": deepcopy(EMPTY_DRAFT)}
    if model is SubmitWorkflowDraftArgs:
        base.update(explanation="Review", unresolved_questions=[])
    original = deepcopy(base)
    schema = next(item["function"]["parameters"] for item in tool_definitions()
                  if TOOL_MODELS[item["function"]["name"]] is model)
    validator = Draft202012Validator(schema)
    assert model.model_validate(base).draft.model_dump() == EMPTY_DRAFT
    validator.validate(base)
    assert base == original
    for key in EMPTY_DRAFT:
        invalid = deepcopy(base)
        del invalid["draft"][key]
        with pytest.raises(ValidationError):
            model.model_validate(invalid)
        assert not validator.is_valid(invalid)
        invalid["draft"][key] = None
        with pytest.raises(ValidationError):
            model.model_validate(invalid)
        assert not validator.is_valid(invalid)
    for key in ("owner_id", "template_id", "expected_revision", "trusted_urls", "enabled"):
        with pytest.raises(ValidationError):
            model.model_validate({**base, "draft": {**EMPTY_DRAFT, key: 1}})
    assert set(WorkflowProposal.model_fields) == set(EMPTY_DRAFT)


@pytest.mark.parametrize("changes", [
    {"explanation": ""}, {"explanation": "x" * 4001}, {"unresolved_questions": ["x"] * 21},
    {"unresolved_questions": [""]}, {"unresolved_questions": ["x" * 1001]},
    {"unresolved_questions": [1]}, {"unresolved_questions": None},
])
def test_submit_arguments_are_bounded(changes):
    payload = {"draft": EMPTY_DRAFT, "explanation": "x" * 4000,
               "unresolved_questions": ["x" * 1000] * 20}
    SubmitWorkflowDraftArgs.model_validate(payload)
    with pytest.raises(ValidationError):
        SubmitWorkflowDraftArgs.model_validate({**payload, **changes})
    schema = tool_definitions()[-1]["function"]["parameters"]
    Draft202012Validator(schema).validate(payload)
    assert not Draft202012Validator(schema).is_valid({**payload, **changes})


@pytest.mark.parametrize("keyword", [
    "$defs", "definitions", "properties", "patternProperties", "dependentSchemas",
    "allOf", "anyOf", "oneOf", "prefixItems", "items", "contains", "additionalProperties",
    "unevaluatedProperties", "unevaluatedItems", "propertyNames", "not", "if", "then", "else",
    "contentSchema",
])
def test_annotation_stripping_recurses_schema_positions_and_preserves_property_names(keyword):
    child = {"type": "string", "default": "PRIVATE-DEFAULT", "examples": ["PRIVATE-EXAMPLE"]}
    if keyword in {"$defs", "definitions", "properties", "patternProperties", "dependentSchemas"}:
        nested = {"default": deepcopy(child), "examples": deepcopy(child)}
        expected = {"default": {"type": "string"}, "examples": {"type": "string"}}
    elif keyword in {"allOf", "anyOf", "oneOf", "prefixItems"}:
        nested, expected = [deepcopy(child)], [{"type": "string"}]
    else:
        nested, expected = deepcopy(child), {"type": "string"}
    schema = {"default": "PRIVATE-ROOT", "examples": ["PRIVATE-ROOT"], keyword: nested,
              "const": {"default": "literal-data", "examples": ["literal-data"]}}
    _strip_schema_annotations(schema)
    assert schema == {keyword: expected,
                      "const": {"default": "literal-data", "examples": ["literal-data"]}}
    assert child["default"] == "PRIVATE-DEFAULT"
    _strip_schema_annotations(False)
