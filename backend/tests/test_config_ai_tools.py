from __future__ import annotations

from copy import deepcopy
import json
import socket
from unittest.mock import Mock

import httpx
from jsonschema import Draft202012Validator
import pytest
from pydantic import ValidationError

from app.features.catalog import FEATURE_LIST
from app.models.setting import AppSetting
from app.schemas.config_ai import GenerateSiteDraftRequest, GenerationResult, GenerationSite
from app.schemas.workflow import SiteWrite
from app.services.config_ai_tools import ConfigAITools
from app.services.workflows import (
    WorkflowConflictError,
    WorkflowNotFoundError,
    WorkflowService,
    WorkflowValidationError,
)
from tests.test_workflow_drafts import _read_only


SITE = {"name": "new-generated-site", "base_url": "https://example.com"}
EMPTY = {"steps": [], "groups": [], "signals": []}
ALLOWED = [feature.key for feature in FEATURE_LIST
           if not feature.key.startswith("post.") and feature.key != "action.fill"]
PRIVATE = "PRIVATE-ADMIN-SECRET"


def _request(**values):
    return {"site": SITE, "requirements": "Propose a workflow", "consent_to_send": True,
            "allowed_features": ["action.wait"], **values}


def _draft(feature="action.wait", config=None, **step_values):
    return {**EMPTY, "steps": [{"step_key": "first", "feature_type": feature,
                               "config": config or {}, **step_values}]}


def _submit(draft):
    return {"draft": draft, "explanation": "Review the proposed workflow.",
            "unresolved_questions": []}


def _consult(tools, *keys):
    for key in keys or ("action.wait",):
        assert tools.dispatch("get_feature_schema", {"feature_type": key})["ok"]


def _error(result, code, path=None):
    assert result["ok"] is False
    assert result["diagnostics"][0]["code"] == code
    assert 1 <= len(result["diagnostics"]) <= 10
    assert isinstance(result["diagnostics"][0]["path"], list)
    if path is not None:
        assert result["diagnostics"][0]["path"] == path
    assert PRIVATE not in json.dumps(result)


@pytest.fixture
def tools(client):
    with client.app.state.session_factory() as session:
        yield ConfigAITools(WorkflowService(session), GenerationSite(**SITE), ALLOWED)


def test_request_normalizes_like_site_write_and_requires_host_selection():
    url = "  https://example.com/path///  "
    request = GenerateSiteDraftRequest.model_validate(_request(site={**SITE, "base_url": url}))
    assert request.site.base_url == SiteWrite(**{**SITE, "base_url": url}).base_url
    assert request.evidence == ""
    for field in ("allowed_features", "consent_to_send"):
        payload = _request()
        del payload[field]
        with pytest.raises(ValidationError):
            GenerateSiteDraftRequest.model_validate(payload)


@pytest.mark.parametrize("value", [False, None, 1, 0, "true", "True", "1", [], {}])
def test_consent_requires_explicit_boolean_true(value):
    with pytest.raises(ValidationError):
        GenerateSiteDraftRequest.model_validate(_request(consent_to_send=value))


@pytest.mark.parametrize("changes", [
    {"requirements": ""}, {"requirements": "x" * 8001}, {"evidence": "x" * 16001},
    {"allowed_features": []}, {"allowed_features": ["action.wait"] * 2},
    {"allowed_features": [str(index) for index in range(23)]},
    {"site": {**SITE, "name": ""}}, {"site": {**SITE, "name": "x" * 129}},
    {"site": {**SITE, "base_url": "file:///private"}},
])
def test_request_bounds(changes):
    with pytest.raises(ValidationError):
        GenerateSiteDraftRequest.model_validate(_request(**changes))


@pytest.mark.parametrize("key", [
    "owner_id", "owner_type", "site_id", "template_id", "parent_id", "metadata",
    "credentials", "credential_provider_id", "trusted_urls", "enabled", "checkin_after",
    "expected_revision", "expected_history_key", "tags", "notes", "lane_policy",
])
def test_request_and_site_forbid_privileged_metadata(key):
    for payload in (_request(**{key: None}), _request(site={**SITE, key: None})):
        with pytest.raises(ValidationError):
            GenerateSiteDraftRequest.model_validate(payload)


@pytest.mark.parametrize("key", [
    "post.buy_card", "post.resign", "post.resign_click", "post.resign_dialog", "plugin.private.wait",
    "sign.iframe_form", "action.fill", "unknown", PRIVATE,
])
def test_host_cannot_authorize_unreviewed_or_excluded_features(tools, key):
    with pytest.raises(ValueError, match="^Invalid generation feature selection$"):
        ConfigAITools(tools._service, GenerationSite(**SITE), [key])
    _error(tools.dispatch("get_feature_schema", {"feature_type": key}), "feature_not_allowed")
    for name, args in (("validate_workflow_draft", {"draft": _draft(key)}),
                       ("submit_workflow_draft", _submit(_draft(key)))):
        _error(tools.dispatch(name, args), "feature_not_allowed")
    assert tools.proposal is None


@pytest.mark.parametrize("selection", [[], ["action.wait"] * 2, [None], "action.wait", None])
def test_constructor_rejects_invalid_host_selection(tools, selection):
    with pytest.raises(ValueError, match="^Invalid generation feature selection$"):
        ConfigAITools(tools._service, GenerationSite(**SITE), selection)


def test_discovery_and_tool_definitions_are_host_filtered_and_copy_isolated(tools):
    tools = ConfigAITools(tools._service, GenerationSite(**SITE), ["action.wait"])
    result = tools.dispatch("list_features", {})
    assert [item["feature_type"] for item in result["features"]] == ["action.wait"]
    assert tools.dispatch("list_features", {"category": "auth"})["features"] == []
    _error(tools.dispatch("get_feature_schema", {"feature_type": "action.click"}), "feature_not_allowed")
    definitions = tools.tool_definitions()
    assert [item["function"]["name"] for item in definitions] == [
        "list_features", "get_feature_schema", "validate_workflow_draft", "submit_workflow_draft",
    ]
    for definition in definitions:
        function = definition["function"]
        schema = function["parameters"]
        Draft202012Validator.check_schema(schema)
        if function["name"] == "get_feature_schema":
            assert schema["properties"]["feature_type"]["enum"] == ["action.wait"]
        if "WorkflowProposal" in schema.get("$defs", {}):
            assert schema["$defs"]["WorkflowProposal"]["properties"]["steps"]["maxItems"] == 50
            assert schema["$defs"]["StepInput"]["properties"]["feature_type"]["enum"] == ["action.wait"]
    baseline = deepcopy(definitions)
    definitions[0]["function"]["parameters"].clear()
    assert tools.tool_definitions() == baseline


def test_model_context_is_only_sanitized_user_site_and_request_policy(tools):
    site = GenerationSite(name=PRIVATE, base_url="https://user:password@example.com/path?token=private#secret")
    selected = ["action.wait"]
    scoped = ConfigAITools(tools._service, site, selected)
    selected.append("post.buy_card")
    site.base_url = "https://changed.example"
    context = scoped.model_context()
    assert context["site"] == {"base_url": "https://example.com/path"}
    assert context["allowed_features"] == ["action.wait"]
    assert context["constraints"] and context["warnings"]
    assert not any(value in json.dumps(context) for value in (PRIVATE, "password", "token=", "#secret"))
    context["allowed_features"].clear()
    assert scoped.model_context()["allowed_features"] == ["action.wait"]


def test_schema_consultation_is_per_feature_and_per_generation(tools):
    _error(tools.dispatch("validate_workflow_draft", {"draft": _draft()}), "schema_required")
    tools.dispatch("list_features", {})
    _error(tools.dispatch("submit_workflow_draft", _submit(_draft())), "schema_required")
    _consult(tools, "flow.open")
    _error(tools.dispatch("submit_workflow_draft", _submit(_draft())), "schema_required")
    _consult(tools)
    assert tools.dispatch("submit_workflow_draft", _submit(_draft())) == {"ok": True, "submitted": True}
    fresh = ConfigAITools(tools._service, GenerationSite(**SITE), ALLOWED)
    _error(fresh.dispatch("submit_workflow_draft", _submit(_draft())), "schema_required")
    assert fresh.proposal is None


@pytest.mark.parametrize("feature,field", [
    ("sign.iframe.image_captcha", "fields"), ("flow.result", "llm_enabled"),
    ("flow.result", "llm_prompt"), ("challenge.image_captcha", "llm_prompt"),
    ("action.wait", "unknown"), ("action.wait", "owner_id"),
])
@pytest.mark.parametrize("value", [None, PRIVATE])
def test_protected_and_unknown_config_fields_are_rejected_even_null(tools, feature, field, value):
    _consult(tools, feature)
    schema = tools.dispatch("get_feature_schema", {"feature_type": feature})
    assert field not in schema["override_config_schema"]["properties"]
    for name, args in (("validate_workflow_draft", {"draft": _draft(feature, {field: value})}),
                       ("submit_workflow_draft", _submit(_draft(feature, {field: value})))):
        _error(tools.dispatch(name, args), "extra_fields", ["draft", "steps", 0, "config"])
    assert tools.proposal is None


@pytest.mark.parametrize("feature,config,code", [
    ("challenge.2fa", {"service_hint": "Example", "totp_provider_id": "plugin.private"}, "field_enum"),
    ("challenge.2fa", {"service_hint": "Example", "totp_provider_id": "builtin.unknown"}, "field_enum"),
    ("challenge.image_captcha", {"captcha_method": "plugin.private.captcha"}, "field_enum"),
    ("challenge.image_captcha", {"captcha_method": "unknown"}, "field_enum"),
    ("action.wait", {"seconds": -1}, "field_bounds"), ("action.wait", {"seconds": "2"}, "field_type"),
    ("action.click", {"selector": []}, "field_bounds"),
])
def test_override_schema_enforces_builtin_provider_enum_and_field_constraints(tools, feature, config, code):
    _consult(tools, feature)
    _error(tools.dispatch("submit_workflow_draft", _submit(_draft(feature, config))), code)


@pytest.mark.parametrize("feature,config", [
    ("challenge.2fa", {"service_hint": "Example", "totp_provider_id": "builtin.twofauth"}),
    ("action.click", {"selector": ["#observed"]}),
    ("action.navigate", {"url": "https://example.com/observed"}),
])
def test_builtin_providers_and_reviewed_generic_actions_can_be_proposed(tools, feature, config):
    _consult(tools, feature)
    result = tools.dispatch("validate_workflow_draft", {"draft": _draft(feature, config)})
    assert result["ok"] and result["valid"] and result["warnings"]
    assert tools.dispatch("submit_workflow_draft", _submit(_draft(feature, config))) == {
        "ok": True, "submitted": True,
    }
    assert tools.proposal["requires_review"] is True


@pytest.mark.parametrize("name,args,code", [
    (PRIVATE, {}, "tool_unavailable"), (None, {}, "tool_unavailable"),
    ("list_features", [], "invalid_arguments"), ("list_features", None, "invalid_arguments"),
    ("list_features", {"category": PRIVATE}, "field_enum"),
    ("get_feature_schema", {"feature_type": []}, "field_type"),
    ("validate_workflow_draft", {"draft": {"steps": []}}, "field_required"),
    ("submit_workflow_draft", {"draft": EMPTY}, "field_required"),
    ("list_features", {PRIVATE: float("nan")}, "invalid_arguments"),
    ("list_features", {PRIVATE: float("inf")}, "invalid_arguments"),
    ("list_features", {PRIVATE: object()}, "invalid_arguments"),
    ("list_features", {PRIVATE: "x" * 65536}, "invalid_arguments"),
])
def test_malformed_unknown_and_nonfinite_arguments_have_static_diagnostics(tools, name, args, code):
    _error(tools.dispatch(name, args), code)


@pytest.mark.parametrize("level", ["args", "draft", "step", "group", "signal"])
def test_privileged_extra_fields_never_reach_workflow_validation(tools, monkeypatch, level):
    payload = _submit(_draft())
    target = payload
    if level == "draft":
        target = payload["draft"]
    elif level == "step":
        target = payload["draft"]["steps"][0]
    elif level == "group":
        payload["draft"]["groups"] = [{"group_key": "local", "name": "Local"}]
        target = payload["draft"]["groups"][0]
    elif level == "signal":
        payload["draft"]["signals"] = [{"signal_key": "ready", "label": "Ready"}]
        target = payload["draft"]["signals"][0]
    target["owner_id"] = PRIVATE
    validation = Mock(side_effect=AssertionError("must not validate"))
    monkeypatch.setattr(tools._service, "validate_draft", validation)
    _error(tools.dispatch("submit_workflow_draft", payload), "extra_fields")
    validation.assert_not_called()


@pytest.mark.parametrize("field,count", [("steps", 51), ("groups", 21), ("signals", 31)])
def test_draft_count_bounds(tools, field, count):
    entries = {
        "steps": _draft()["steps"][0], "groups": {"group_key": "g", "name": "Group"},
        "signals": {"signal_key": "ready", "label": "Ready"},
    }
    payload = _submit({**EMPTY, field: [entries[field]] * count})
    _error(tools.dispatch("submit_workflow_draft", payload), "field_bounds", ["draft", field])


@pytest.mark.parametrize("exception,code", [
    (WorkflowNotFoundError, "workflow_dependency_missing"),
    (WorkflowConflictError, "workflow_conflict"),
    (WorkflowValidationError, "workflow_invalid"), (RuntimeError, "tool_failed"),
])
def test_validation_errors_never_echo_input_or_exception_details(tools, monkeypatch, exception, code):
    _consult(tools)
    monkeypatch.setattr(tools._service, "validate_draft", Mock(side_effect=exception(PRIVATE)))
    _error(tools.dispatch("submit_workflow_draft", _submit(_draft())), code)
    assert tools.proposal is None


def test_submit_uses_validated_arguments_not_private_validation_preview(tools, monkeypatch):
    _consult(tools)
    validator = Mock(return_value={"local": {"password": PRIVATE}, "resolved": {"message": PRIVATE}})
    monkeypatch.setattr(tools._service, "validate_draft", validator)
    args = _submit(_draft())
    original = deepcopy(args)
    assert tools.dispatch("submit_workflow_draft", args) == {"ok": True, "submitted": True}
    assert args == original
    proposal = GenerationResult.model_validate(tools.proposal)
    assert proposal.status == "proposal" and proposal.requires_review is True
    assert proposal.draft.steps[0].config == {}
    assert PRIVATE not in json.dumps(tools.proposal)
    owner_type, candidate = validator.call_args.args
    assert owner_type == "site" and candidate.name == SITE["name"]
    assert candidate.base_url == SITE["base_url"] and candidate.template_id is None
    assert validator.call_args.kwargs == {"owner_id": None}
    args["draft"]["steps"][0]["config"]["seconds"] = 99
    assert tools.proposal["draft"]["steps"][0]["config"] == {}


def test_each_submit_gets_a_fresh_candidate_and_failure_clears_prior_proposal(tools, monkeypatch):
    _consult(tools)
    validator = Mock(return_value={})
    monkeypatch.setattr(tools._service, "validate_draft", validator)
    args = _submit(_draft())
    for _ in range(2):
        assert tools.dispatch("submit_workflow_draft", args) == {"ok": True, "submitted": True}
    assert validator.call_count == 2
    assert validator.call_args_list[0].args[1] is not validator.call_args_list[1].args[1]
    _error(tools.dispatch("submit_workflow_draft", {"draft": EMPTY}), "field_required")
    assert tools.proposal is None


def test_every_exported_field_policy_is_enforced_even_for_null(tools, monkeypatch):
    original = tools._catalog.get_feature_schema

    def restricted(key):
        result = original(key)
        for name in ("config_schema", "override_config_schema"):
            result[name]["properties"].pop("seconds")
        result["excluded_fields"].append("seconds")
        return result

    monkeypatch.setattr(tools._catalog, "get_feature_schema", restricted)
    _consult(tools)
    _error(tools.dispatch("submit_workflow_draft", _submit(_draft(config={"seconds": None}))),
           "extra_fields", ["draft", "steps", 0, "config"])


def test_unconsulted_disabled_features_and_nested_unknown_config_are_rejected(tools):
    _error(tools.dispatch("submit_workflow_draft", _submit(_draft(enabled=False))), "schema_required")
    _consult(tools)
    _error(tools.dispatch("submit_workflow_draft", _submit(_draft(config={"seconds": {PRIVATE: None}}))),
           "field_type", ["draft", "steps", 0, "config", "seconds"])


def test_empty_draft_with_followup_questions_is_valid_and_never_persisted(tools, client):
    with _read_only(client):
        assert tools.dispatch("list_features", {})["ok"]
        args = _submit(EMPTY)
        args["unresolved_questions"] = ["Which selector identifies the check-in button?"]
        assert tools.dispatch("submit_workflow_draft", args) == {"ok": True, "submitted": True}
    assert tools.proposal["draft"] == EMPTY
    assert tools.proposal["unresolved_questions"] == args["unresolved_questions"]


def test_failed_full_validation_cannot_be_excused_by_questions(tools):
    _consult(tools, "action.click")
    args = _submit(_draft("action.click"))
    args["unresolved_questions"] = ["Which selector?"]
    _error(tools.dispatch("submit_workflow_draft", args), "field_required", ["draft", "steps", 0, "config", "selector"])
    assert tools.proposal is None


def test_submit_revalidates_fresh_policy_and_does_not_materialize_admin_defaults(tools, client):
    key = "feature.action.wait"
    with client.app.state.session_factory() as writer:
        writer.add(AppSetting(key=key, value_json=json.dumps({
            "defaults": {"seconds": 3}, "required_fields": [],
        })))
        writer.commit()
    _consult(tools)
    args = _submit(_draft())
    with _read_only(client):
        assert tools.dispatch("validate_workflow_draft", {"draft": args["draft"]})["ok"]
        assert tools.dispatch("submit_workflow_draft", args)["ok"]
    assert tools.proposal["draft"]["steps"][0]["config"] == {}
    with client.app.state.session_factory() as writer:
        writer.get(AppSetting, key).value_json = json.dumps({
            "defaults": {"seconds": PRIVATE}, "required_fields": [],
        })
        writer.commit()
    with _read_only(client):
        _error(tools.dispatch("submit_workflow_draft", args), "field_type", ["draft", "steps", 0, "config", "seconds"])
    assert tools.proposal is None


def test_admin_defaults_and_prompts_never_leak_in_success_or_schema(tools, client):
    with client.app.state.session_factory() as writer:
        writer.add(AppSetting(key="feature.flow.result", value_json=json.dumps({
            "defaults": {"message": PRIVATE}, "required_fields": [],
            "execution_settings": {"llm_enabled": True, "llm_prompt": PRIVATE},
        })))
        writer.commit()
    with _read_only(client):
        schema = tools.dispatch("get_feature_schema", {"feature_type": "flow.result"})
        result = tools.dispatch("submit_workflow_draft", _submit(_draft("flow.result")))
    assert result == {"ok": True, "submitted": True}
    assert PRIVATE not in json.dumps([schema, result, tools.proposal, tools.model_context()])
    assert tools.proposal["draft"]["steps"][0]["config"] == {}


def test_discovery_validation_and_submission_do_not_use_network_or_crypto(tools, client, monkeypatch):
    forbidden = Mock(side_effect=AssertionError(PRIVATE))
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    crypto = Mock()
    tools._service.crypto = crypto
    with _read_only(client):
        assert tools.dispatch("list_features", {})["ok"]
        _consult(tools)
        assert tools.dispatch("validate_workflow_draft", {"draft": _draft()})["ok"]
        assert tools.dispatch("submit_workflow_draft", _submit(_draft()))["ok"]
    forbidden.assert_not_called()
    assert crypto.mock_calls == []


def test_tools_never_flush_commit_rollback_or_change_pending_session_state(tools, client, monkeypatch):
    session = tools._service.session
    pending = AppSetting(key="unrelated-pending", value_json=PRIVATE)
    session.add(pending)
    session.autoflush = True
    before = (set(session.new), set(session.dirty), set(session.deleted))
    with monkeypatch.context() as patch, _read_only(client):
        for method in ("flush", "commit", "rollback"):
            patch.setattr(session, method, Mock(side_effect=AssertionError("transaction mutation")))
        assert tools.dispatch("list_features", {})["ok"]
        _consult(tools)
        assert tools.dispatch("validate_workflow_draft", {"draft": _draft()})["ok"]
        assert tools.dispatch("submit_workflow_draft", _submit(_draft()))["ok"]
        for method in ("flush", "commit", "rollback"):
            getattr(session, method).assert_not_called()
    assert (set(session.new), set(session.dirty), set(session.deleted)) == before
