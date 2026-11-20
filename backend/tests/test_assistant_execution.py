from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import time
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.main import create_app
from app.models.assistant import AssistantSession
from app.models.workflow import Site, SiteCredential, WorkflowHistory, WorkflowStep
from app.schemas.assistant import CreateSession
from app.schemas.workflow import SiteWrite
from app.services.assistant import BrowserScope
from app.services.assistant_execution import AssistantExecution, SUPPORTED_FEATURES
from app.services.bitwarden import BitwardenSecret, BitwardenService
from app.services.credentials import CredentialService, SiteSecret
from app.services.feature_settings import FeatureSettingsService
from tests.helpers import make_settings
from tests.test_assistant_snapshot import node, snapshot


OWNER = "execution-owner"
USERNAME = "private-alice@example.com"
PASSWORD = "private-password-no-export"
TOKEN = "private-browser-token-no-export"
BASE = "https://example.com/"


def arguments(feature="action.wait", config=None, **fields):
    return {"operation_id": str(uuid4()), "step": {
        "step_key": "execute", "feature_type": feature,
        "config": config if config is not None else {}, **fields,
    }}


@pytest.fixture
def execution(tmp_path):
    manager = create_app(make_settings(tmp_path, config_ai_enabled=True)).state.assistant_manager
    identifier = manager.create(OWNER, CreateSession())["id"]
    target = {
        "name": "Example", "base_url": BASE, "credential_mode": "manual",
        "confirmed": True, "proxy": False, "execution_consent": True,
        "credential_consent": True, "allow_sign": True,
        "execution_features": sorted(SUPPORTED_FEATURES), "site_id": None,
    }
    scope = BrowserScope(OWNER, str(uuid4()), TOKEN, "interaction", "", time.monotonic() + 900)
    manager.browsers[identifier] = scope
    helper = AssistantExecution(manager, OWNER, identifier, target)
    save_target(helper, target)

    async def worker(path, payload):
        return response(manager.browsers[identifier], payload["operation_id"])

    manager._worker = AsyncMock(side_effect=worker)
    manager._close_browser = AsyncMock(side_effect=lambda sid: manager.browsers.pop(sid, None))
    return helper


def save_target(helper, target):
    helper.target = deepcopy(target)
    with helper.manager.session_factory() as db:
        row = db.get(AssistantSession, helper.session_id)
        data = helper.manager._decrypt(row)
        data["target"] = deepcopy(target)
        helper.manager._encrypt(row, data)
        row.status = "running"
        db.commit()


def bind(helper, *, base_url=BASE):
    with helper.manager.session_factory() as db:
        site = Site(name="Saved Example", base_url=base_url)
        db.add(site)
        db.commit()
        binding = {"id": site.id, "history_key": site.history_key, "revision": site.config_revision}
        CredentialService(db, helper.manager.crypto).update(site.id, username=USERNAME, password=PASSWORD)
    save_target(helper, {**helper.target, "site_id": binding["id"], "_credential_binding": binding})
    return binding["id"]


def response(scope, operation_id, *, status="success", code="step_succeeded"):
    return {
        "session_id": scope.session_id, "lane_key": scope.lane_key,
        "operation_id": operation_id, "status": status, "code": code,
        "logs": ["untrusted-executor-log"],
        "before": snapshot(url=BASE),
        "after": snapshot(url=BASE + "home"),
        "result": {"submitted": True, "verified": False, "count": 3},
    }


def run(helper, args=None):
    return asyncio.run(helper.execute(args if args is not None else arguments()))


def test_normalized_execution_uses_existing_browser_and_only_safe_result(execution):
    scope = execution.manager.browsers[execution.session_id]
    captured = []

    async def worker(path, payload):
        captured.append((path, deepcopy(payload)))
        raw = response(scope, payload["operation_id"])
        raw.update(credentials="ignored-metadata", token="ignored-metadata")
        raw["before"]["elements"] = [
            node(tag="input", attributes={"type": "password", "id": "password", "value": "ignored-input-value"}),
            node("n1", tag="input", visible=False, attributes={"type": "hidden"}, label="ignored-hidden"),
        ]
        raw["before"]["total"] = 2
        raw["result"].update(password="ignored-result", url="ignored-result", verified="not-boolean")
        return raw

    execution.manager._worker.side_effect = worker
    args = arguments("flow.open", enabled=None)
    result = run(execution, args)
    assert result == {
        "ok": True, "operation_id": args["operation_id"], "status": "success", "code": "step_succeeded",
        "logs": ["Step execution started", "Step execution finished"],
        "before": result["before"], "after": result["after"],
        "result": {"submitted": True, "count": 3},
    }
    assert result["before"]["elements"] == [
        node(tag="input", attributes={"type": "password", "id": "password"}),
        node("n1", tag="input", visible=False, attributes={"type": "hidden"}),
    ]
    assert result["after"]["elements"] == []
    assert result["before"]["total"] == 2 and result["after"]["total"] == 0
    assert result["before"]["url"] == BASE and result["after"]["url"] == BASE + "home"
    path, payload = captured[0]
    assert path == f"{scope.session_id}/execute"
    assert payload["token"] == TOKEN and payload["site_origin"] == BASE
    assert payload["execution"] == {"feature_types": ["flow.open"], "allow_sign": True}
    assert payload["step"]["config"] == {"url": BASE, "wait_until": "domcontentloaded", "retry_count": 0}
    assert payload["step"]["enabled"] is True and payload["step"]["condition"] == "always"
    assert payload["step"]["on_error"] == "stop"
    assert "credentials" not in payload
    assert args["step"] == {"step_key": "execute", "feature_type": "flow.open", "config": {}, "enabled": None}
    assert execution.manager.browsers[execution.session_id] is scope
    execution.manager._close_browser.assert_not_awaited()
    assert all(value not in json.dumps(result) for value in [TOKEN, "ignored", "untrusted"])


@pytest.mark.parametrize("change,expected", [
    ({"execution_consent": False}, "execution_consent_required"),
    ({"execution_consent": 1}, "execution_consent_required"),
    ({"confirmed": False}, "target_confirmation_required"),
    ({"execution_features": []}, "execution_feature_denied"),
    ({"execution_features": ["action.wait", "plugin.bad"]}, "execution_feature_denied"),
])
def test_host_execution_permissions(execution, monkeypatch, change, expected):
    resolver = AsyncMock()
    monkeypatch.setattr(asyncio, "to_thread", resolver)
    save_target(execution, {**execution.target, **change})
    assert run(execution) == {"ok": False, "code": expected}
    resolver.assert_not_awaited()
    execution.manager._worker.assert_not_awaited()


@pytest.mark.parametrize("feature", [
    "sign.button", "sign.unit3d_claim", "action.click", "challenge.altcha", "challenge.slider",
    "action.fill", "challenge.2fa", "challenge.turnstile", "challenge.image_captcha",
    "challenge.grid_captcha", "sign.iframe.image_captcha", "sign.image_quiz",
])
@pytest.mark.parametrize("consent", [False, None, 1])
def test_selector_actions_require_explicit_sign_consent(execution, feature, consent):
    save_target(execution, {**execution.target, "allow_sign": consent})
    assert run(execution, arguments(feature)) == {"ok": False, "code": "sign_consent_required"}
    execution.manager._worker.assert_not_awaited()


@pytest.mark.parametrize("feature", ["challenge.altcha", "challenge.slider"])
def test_challenge_actions_with_sign_consent_reach_worker(execution, feature):
    assert run(execution, arguments(feature))["ok"]
    execution.manager._worker.assert_awaited_once()


@pytest.mark.parametrize("feature", [
    "auth.totp_login", "plugin.example",
])
def test_v1_unsupported_features_are_denied_even_if_host_lists_them(execution, feature):
    save_target(execution, {**execution.target, "execution_features": [feature]})
    assert run(execution, arguments(feature))["code"] == "execution_feature_denied"
    execution.manager._worker.assert_not_awaited()


@pytest.mark.parametrize("args", [
    {}, {"step": {}}, {"step": {}, "operation_id": None}, {"step": {}, "operation_id": 1},
    {"step": {}, "operation_id": "not-a-uuid"}, {"step": {}, "operation_id": uuid4()},
    {"step": [], "operation_id": str(uuid4())},
    {**arguments(), "credentials": {"password": PASSWORD}},
    {**arguments(), "private_runtime": {"totp_code": "123456"}},
    {**arguments(), "site_id": 1}, {**arguments(), "execution": {"allow_sign": True}},
])
def test_arguments_require_string_uuid_and_no_authority_fields(execution, args):
    assert run(execution, args) == {"ok": False, "code": "invalid_arguments"}
    execution.manager._worker.assert_not_awaited()


@pytest.mark.parametrize("args", [
    arguments(enabled=False), arguments(enabled=1), arguments(condition="some_signal"),
    arguments(timeout_seconds=True), arguments(timeout_seconds=0), arguments(timeout_seconds=3601),
    arguments("action.wait", {"seconds": "1"}), arguments("action.wait", {"seconds": True}),
    arguments("action.wait", {"seconds": 301}), arguments("action.wait", {"seconds": 1, "llm_prompt": "x"}),
    arguments("auth.form_login", {"username": USERNAME, "password": PASSWORD}),
    arguments("auth.form_login", {"credential_provider_id": "plugin.bad"}),
    arguments("action.click", {"selector": []}), arguments("action.click", {"selector": ["x"] * 31}),
    arguments("action.click", {"selector": ["x" * 2049]}),
    arguments("flow.open", {"retry_count": 1}), arguments("flow.open", {"retry_count": False}),
    arguments("flow.open", {"url": "https://other.example/"}),
    arguments("flow.open", {"url": "http://example.com/"}),
    arguments("flow.open", {"url": "https://example.com:444/"}),
    arguments("flow.open", {"url": "/login?token=secret"}),
    arguments("flow.open", {"url": "/login?token=[REDACTED]"}),
    arguments("flow.open", {"url": "/login?next=%5BREDACTED%5D"}),
    arguments("flow.open", {"url": "\n/login"}),
    arguments("flow.open", {"url": "/lo\tgin"}),
    arguments("flow.open", {"url": "https://user:password@example.com/"}),
    arguments("flow.open", {"url": "javascript:alert(1)"}),
    arguments("flow.open", {"url": "//other.example/"}),
])
def test_invalid_steps_never_resolve_credentials_or_reach_worker(execution, monkeypatch, args):
    resolver = AsyncMock()
    monkeypatch.setattr(asyncio, "to_thread", resolver)
    assert run(execution, args) == {"ok": False, "code": "invalid_step"}
    resolver.assert_not_awaited()
    execution.manager._worker.assert_not_awaited()


@pytest.mark.parametrize("url,expected", [
    ("{base_url}/login", BASE + "login"), ("/login", BASE + "login"),
    ("https://EXAMPLE.com:443/login", BASE + "login"),
])
def test_navigation_expands_placeholders_relative_urls_and_normalizes_origin(execution, url, expected):
    captured = []

    async def worker(path, payload):
        captured.append(deepcopy(payload))
        return response(execution.manager.browsers[execution.session_id], payload["operation_id"])

    execution.manager._worker.side_effect = worker
    assert run(execution, arguments("action.navigate", {"url": url}))["ok"]
    assert captured[0]["step"]["config"]["url"] == expected


@pytest.mark.parametrize("feature", ["flow.open", "action.navigate"])
@pytest.mark.parametrize("inherited", [False, True])
def test_unsafe_placeholder_substitution_matches_saved_workflow_and_never_executes(execution, feature, inherited):
    from app.features.catalog import FEATURES

    config = {"url": "{base_url}login"}
    canonical = SiteWrite(name=execution.target["name"], base_url=BASE).base_url
    assert canonical == "https://example.com"
    assert config["url"].replace("{base_url}", canonical) == "https://example.comlogin"
    if inherited:
        with execution.manager.session_factory() as db:
            FeatureSettingsService(db).update(FEATURES[feature], config, [])
        config = {}
    assert run(execution, arguments(feature, config)) == {"ok": False, "code": "invalid_step"}
    execution.manager._worker.assert_not_awaited()


def test_placeholder_substitution_uses_saved_workflow_base_url_for_all_fields(execution):
    save_target(execution, {**execution.target, "base_url": BASE + "app///"})
    captured = []

    async def worker(path, payload):
        captured.append(deepcopy(payload))
        return response(execution.manager.browsers[execution.session_id], payload["operation_id"])

    execution.manager._worker.side_effect = worker
    assert run(execution, arguments("sign.button", {"success_signals": ["{base_url}"]}))["ok"]
    assert captured[0]["step"]["config"]["success_signals"] == [
        SiteWrite(name=execution.target["name"], base_url=execution.target["base_url"]).base_url,
    ]


@pytest.mark.parametrize("feature", sorted(SUPPORTED_FEATURES))
def test_fully_normalized_supported_steps_satisfy_worker_contract(execution, monkeypatch, feature):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    from worker.assistant_step import ExecuteRequest, validate_step

    bind(execution)
    if feature.startswith("post."):
        from app.services.assistant_feature_policy import set_assistant_allowed
        save_target(execution, {**execution.target, "workflow_site_id": execution.target["site_id"],
                               "_workflow_binding": execution.target["_credential_binding"]})
        with execution.manager.session_factory() as db:
            set_assistant_allowed(db, feature, True)
            db.commit()
    if feature == "challenge.2fa":
        monkeypatch.setattr(execution, "_private_runtime", lambda step: ({"totp_code": "123456"}, ["123456"]))
    elif feature in {"sign.image_quiz", "challenge.grid_captcha"}:
        monkeypatch.setattr(execution, "_private_runtime", lambda step: ({"vision": {
            "base_url": "http://solver.internal/v1", "api_key": "private-solver-key",
            "model": "fixture-model", "timeout": 20, "supports_vision": True,
        }}, ["private-solver-key"]))

    async def worker(path, payload):
        request = ExecuteRequest.model_validate(payload)
        assert validate_step(request)["feature_type"] == feature
        return response(execution.manager.browsers[execution.session_id], payload["operation_id"])

    execution.manager._worker.side_effect = worker
    config = {"selector": ["#button"]} if feature in {"action.click", "action.fill"} else {}
    if feature == "challenge.2fa":
        config = {"service_hint": "Example"}
    elif feature == "flow.jump":
        config = {"action": "restart", "texts": ["Complete"]}
    elif feature == "post.buy_card":
        config = {"max_price": 1}
    assert run(execution, arguments(feature, config))["ok"]


def test_feature_settings_defaults_are_validated_and_navigation_retries_forced_off(execution):
    from app.features.catalog import FEATURES

    with execution.manager.session_factory() as db:
        FeatureSettingsService(db).update(FEATURES["flow.open"], {"url": "{base_url}/login", "retry_count": 9}, [])
    captured = []

    async def worker(path, payload):
        captured.append(deepcopy(payload))
        return response(execution.manager.browsers[execution.session_id], payload["operation_id"])

    execution.manager._worker.side_effect = worker
    assert run(execution, arguments("flow.open"))["ok"]
    assert captured[0]["step"]["config"]["url"] == BASE + "login"
    assert captured[0]["step"]["config"]["retry_count"] == 0


@pytest.mark.parametrize("feature", ["auth.form_login", "auth.password_verify"])
def test_saved_manual_credentials_are_typed_and_worker_only(execution, feature):
    site_id = bind(execution, base_url="https://EXAMPLE.COM:443/saved-path")
    captured = []

    async def worker(path, payload):
        captured.append(deepcopy(payload))
        return response(execution.manager.browsers[execution.session_id], payload["operation_id"])

    execution.manager._worker.side_effect = worker
    args = arguments(feature)
    assert run(execution, args)["ok"]
    assert captured[0]["credentials"] == {"username": USERNAME, "password": PASSWORD}
    with execution.manager.session_factory() as db:
        data = execution.manager._decrypt(db.get(AssistantSession, execution.session_id))
        assert args["operation_id"] in data["_execution_operations"]
        assert all(secret not in json.dumps(data) for secret in [USERNAME, PASSWORD, TOKEN])
        assert db.get(SiteCredential, site_id).last_verified_at is None


@pytest.mark.parametrize("change,expected", [
    ({"credential_consent": False}, "credential_consent_required"),
    ({"credential_consent": 1}, "credential_consent_required"),
    ({"credential_mode": "manual"}, "credentials_unavailable"),
    ({"credential_mode": "cookie"}, "credentials_unavailable"),
    ({"credential_mode": "none"}, "credentials_unavailable"),
])
def test_auth_requires_consent_and_usable_server_selected_credentials(execution, monkeypatch, change, expected):
    manual = AsyncMock()
    vault = AsyncMock()
    monkeypatch.setattr(CredentialService, "resolve", manual)
    monkeypatch.setattr(BitwardenService, "resolve_for_site", vault)
    save_target(execution, {**execution.target, **change})
    assert run(execution, arguments("auth.form_login")) == {"ok": False, "code": expected}
    manual.assert_not_called()
    vault.assert_not_called()
    execution.manager._worker.assert_not_awaited()


@pytest.mark.parametrize("mode", ["manual", "bitwarden"])
def test_non_auth_never_resolves_credentials(execution, monkeypatch, mode):
    manual = AsyncMock()
    vault = AsyncMock()
    monkeypatch.setattr(CredentialService, "resolve", manual)
    monkeypatch.setattr(BitwardenService, "resolve_for_site", vault)
    save_target(execution, {**execution.target, "credential_mode": mode})
    assert run(execution)["ok"]
    manual.assert_not_called()
    vault.assert_not_called()


@pytest.mark.parametrize("saved", [False, True])
def test_bitwarden_lookup_is_transient_exact_domain_without_provider_overrides(execution, monkeypatch, saved):
    if saved:
        identifier = bind(execution)
        with execution.manager.session_factory() as db:
            site = db.get(Site, identifier)
            site.bitwarden_match_mode = "custom"
            site.bitwarden_custom_match = "another-account"
            site.credential_provider_id = "plugin.not-authorized"
            db.commit()
    save_target(execution, {**execution.target, "credential_mode": "bitwarden"})
    lookups = []

    def resolve(service, site):
        lookups.append((site.id, site.base_url, site.bitwarden_match_mode, site.bitwarden_custom_match,
                        site.credential_provider_id))
        assert not service.session.new
        return BitwardenSecret(USERNAME, PASSWORD, item_name="private-vault-item", matched_by="private-match")

    monkeypatch.setattr(BitwardenService, "resolve_for_site", resolve)
    result = run(execution, arguments("auth.form_login"))
    assert result["ok"]
    assert lookups == [(None, BASE, "domain", None, None)]
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("change", [
    "origin", "scheme", "port", "revision", "history_key", "deleted", "aba",
    "missing_binding", "binding_id",
])
def test_saved_binding_origin_revision_and_aba_checked_before_secrets(execution, monkeypatch, change):
    identifier = bind(execution)
    if change in {"missing_binding", "binding_id"}:
        target = deepcopy(execution.target)
        if change == "missing_binding":
            target.pop("_credential_binding")
        else:
            target["_credential_binding"]["id"] += 1
        save_target(execution, target)
    else:
        with execution.manager.session_factory() as db:
            site = db.get(Site, identifier)
            if change in {"origin", "scheme", "port"}:
                site.base_url = {"origin": "https://other.example/", "scheme": "http://example.com/",
                                 "port": "https://example.com:444/"}[change]
            elif change == "revision":
                site.config_revision += 1
            elif change == "history_key":
                site.history_key = str(uuid4())
            else:
                db.delete(site)
            db.commit()
            if change == "aba":
                db.add(Site(id=identifier, name="Replacement", base_url=BASE))
                db.commit()
    resolver = AsyncMock()
    monkeypatch.setattr(asyncio, "to_thread", resolver)
    assert run(execution, arguments("auth.form_login")) == {"ok": False, "code": "credential_binding_changed"}
    resolver.assert_not_awaited()
    execution.manager._worker.assert_not_awaited()


@pytest.mark.parametrize("change", ["target", "site", "browser", "idle", "disabled"])
def test_authorization_rechecked_after_async_credentials(execution, monkeypatch, change):
    identifier = bind(execution)

    async def resolve(function):
        if change == "target":
            with execution.manager.session_factory() as db:
                row = db.get(AssistantSession, execution.session_id)
                data = execution.manager._decrypt(row)
                data["target"]["credential_consent"] = False
                execution.manager._encrypt(row, data)
                db.commit()
        elif change == "site":
            with execution.manager.session_factory() as db:
                db.get(Site, identifier).config_revision += 1
                db.commit()
        elif change == "browser":
            execution.manager.browsers[execution.session_id] = deepcopy(execution.manager.browsers[execution.session_id])
        elif change == "idle":
            execution.manager._idle(execution.session_id)
        else:
            execution.manager.settings.config_ai_enabled = False
        return {"username": USERNAME, "password": PASSWORD}

    monkeypatch.setattr(asyncio, "to_thread", resolve)
    assert not run(execution, arguments("auth.form_login"))["ok"]
    execution.manager._worker.assert_not_awaited()


@pytest.mark.parametrize("change", ["owner", "idle", "expired", "not_ready", "proxy", "disabled", "stopping"])
def test_session_browser_and_runtime_authority(execution, change):
    scope = execution.manager.browsers[execution.session_id]
    if change == "owner":
        execution.owner = "another-owner"
    elif change == "idle":
        execution.manager._idle(execution.session_id)
    elif change == "expired":
        scope.deadline = 0
    elif change == "not_ready":
        scope.ready = False
    elif change == "proxy":
        scope.lane_key = "proxy_interaction"
    elif change == "disabled":
        execution.manager.settings.config_ai_enabled = False
    else:
        execution.manager._stopping = True
    assert not run(execution)["ok"]
    execution.manager._worker.assert_not_awaited()


@pytest.mark.parametrize("status,code", [
    ("failed", "step_failed"), ("error", "execution_error"),
    ("error", "execution_timeout"), ("error", "execution_canceled"),
])
def test_static_worker_failures_preserve_existing_page_and_observations(execution, status, code):
    scope = execution.manager.browsers[execution.session_id]

    async def worker(path, payload):
        raw = response(scope, payload["operation_id"], status=status, code=code)
        raw["logs"] = ["arbitrary-secret-executor-log"]
        raw["result"] = {"password": "arbitrary-secret", "verified": "arbitrary-secret"}
        return raw

    execution.manager._worker.side_effect = worker
    result = run(execution)
    assert result["status"] == status and result["code"] == code and result["ok"] is False
    assert result["before"]["url"] == BASE and result["after"]["url"] == BASE + "home"
    assert result["result"] == {} and "arbitrary" not in json.dumps(result)
    assert execution.manager.browsers[execution.session_id] is scope
    execution.manager._close_browser.assert_not_awaited()


@pytest.mark.parametrize("empty_after", [False, True])
def test_uncertain_outcome_preserves_status_evidence_and_static_category_instruction(execution, empty_after):
    scope = execution.manager.browsers[execution.session_id]
    args = arguments("sign.button")

    async def worker(path, payload):
        raw = response(scope, payload["operation_id"], status="unknown", code="outcome_uncertain")
        if empty_after:
            raw["after"] = {"elements": []}
        raw["result"] = {"submitted": True, "verified": False, "message": "untrusted-detail"}
        return raw

    execution.manager._worker.side_effect = worker
    result = run(execution, args)
    assert result["ok"] is False
    assert result["status"] == "unknown" and result["code"] == "outcome_uncertain"
    assert result["operation_id"] == args["operation_id"]
    assert result["result"] == {"submitted": True, "verified": False}
    assert result["before"]["url"] == BASE and result["before"]["elements"] == []
    assert result["before"]["total"] == 0 and result["before"]["url_redacted"] is False
    if empty_after:
        assert result["after"] == {"elements": []}
    else:
        assert result["after"]["url"] == BASE + "home" and result["after"]["elements"] == []
        assert result["after"]["total"] == 0 and result["after"]["url_redacted"] is False
    assert result["logs"] == [
        "Step execution started", "Step execution finished",
        "Outcome uncertain. Inspect the retained page first. Non-post retries use "
        "the category policy; auth attempts share the per-turn budget.",
    ]
    assert "untrusted" not in json.dumps(result)
    assert execution.manager.browsers[execution.session_id] is scope
    execution.manager._close_browser.assert_not_awaited()
    assert run(execution, args) == {"ok": False, "code": "operation_already_used"}
    execution.manager._worker.assert_awaited_once()


@pytest.mark.parametrize("field", ["session_id", "lane_key", "operation_id", "status", "code", "before", "after", "result"])
def test_invalid_worker_response_is_never_passed_through(execution, field):
    async def worker(path, payload):
        raw = response(execution.manager.browsers[execution.session_id], payload["operation_id"])
        raw[field] = "DO-NOT-EXPORT"
        return raw

    execution.manager._worker.side_effect = worker
    assert run(execution) == {"ok": False, "code": "execution_unavailable"}
    execution.manager._close_browser.assert_awaited_once_with(execution.session_id)


def test_empty_observation_on_early_worker_error_is_safe(execution):
    async def worker(path, payload):
        raw = response(execution.manager.browsers[execution.session_id], payload["operation_id"],
                       status="error", code="execution_error")
        raw.update(before={"elements": []}, after={"elements": []}, result={})
        return raw

    execution.manager._worker.side_effect = worker
    result = run(execution)
    assert result["code"] == "execution_error"
    assert result["before"] == result["after"] == {"elements": []}
    execution.manager._close_browser.assert_not_awaited()


@pytest.mark.parametrize("valid", [False, True])
def test_timeout_login_evidence_is_projected_without_changing_outcome(execution, valid):
    from tests.test_login_evidence_projection import evidence

    bind(execution)
    scope = execution.manager.browsers[execution.session_id]

    async def worker(path, payload):
        raw = response(scope, payload["operation_id"], status="unknown", code="outcome_uncertain")
        diagnostic = evidence()
        diagnostic.update(selector="private-selector-value", message="private-provider-message")
        diagnostic["selectors"][0]["frames"][0]["url"] = "https://private.example/"
        if not valid:
            diagnostic["selectors"][0]["frames"][0]["error"] = "private-provider-error"
        raw["result"] = {"submitted": True, "verified": False, "success_evidence": diagnostic}
        return raw

    execution.manager._worker.side_effect = worker
    result = run(execution, arguments("auth.form_login"))
    assert result["status"] == "unknown" and result["code"] == "outcome_uncertain"
    assert result["result"] == {
        "submitted": True, "verified": False,
        **({"success_evidence": evidence()} if valid else {}),
    }
    assert "private-" not in json.dumps(result)
    assert "private.example" not in json.dumps(result)
    assert execution.manager.browsers[execution.session_id] is scope
    execution.manager._close_browser.assert_not_awaited()


@pytest.mark.parametrize("key,codes", [
    ("reason", ["browser_url_denied", "browser_dns_denied", "browser_redirect_denied",
                "browser_trust_required", "browser_credential_endpoint_not_approved",
                "browser_snapshot_stale", "browser_frame_unreadable", "browser_frame_unavailable",
                "execution_error", "execution_timeout", "execution_canceled"]),
    ("evidence_errorcode", ["browser_url_denied", "browser_dns_denied", "browser_redirect_denied",
                            "browser_trust_required", "browser_credential_endpoint_not_approved",
                            "browser_snapshot_stale", "browser_frame_unreadable",
                            "browser_frame_unavailable", "browser_snapshot_unavailable"]),
])
def test_reviewed_worker_diagnostics_are_forwarded_without_changing_outcome(execution, key, codes):
    scope = execution.manager.browsers[execution.session_id]
    for code in codes:
        raw = response(scope, "unused", status="unknown", code="outcome_uncertain")
        raw["diagnostics"] = {key: code, "message": "DO-NOT-EXPORT"}

        async def worker(path, payload):
            return {**raw, "operation_id": payload["operation_id"]}

        execution.manager._worker.side_effect = worker
        result = run(execution)
        assert result["diagnostics"] == {key: code}
        assert result["status"] == "unknown" and result["code"] == "outcome_uncertain"
        assert "DO-NOT-EXPORT" not in json.dumps(result)
        assert execution.manager.browsers[execution.session_id] is scope
    execution.manager._close_browser.assert_not_awaited()


@pytest.mark.parametrize("diagnostics", [
    None, {}, [], "DO-NOT-EXPORT", {"message": "DO-NOT-EXPORT"},
    {"reason": "browser_post_endpoint_denied", "evidence_errorcode": "execution_timeout"},
    {"reason": "browser_url_denied: DO-NOT-EXPORT", "evidence_errorcode": "DO-NOT-EXPORT"},
    {"reason": ["browser_url_denied"], "evidence_errorcode": {"code": "browser_url_denied"}},
    {"reason": True, "evidence_errorcode": 1},
])
def test_invalid_or_empty_diagnostics_are_omitted(execution, diagnostics):
    async def worker(path, payload):
        raw = response(execution.manager.browsers[execution.session_id], payload["operation_id"])
        raw["diagnostics"] = diagnostics
        return raw

    execution.manager._worker.side_effect = worker
    result = run(execution)
    assert result["ok"] and "diagnostics" not in result
    assert "DO-NOT-EXPORT" not in json.dumps(result)
    execution.manager._close_browser.assert_not_awaited()


def test_diagnostics_project_independently_and_keep_empty_evidence(execution):
    async def worker(path, payload):
        raw = response(execution.manager.browsers[execution.session_id], payload["operation_id"],
                       status="unknown", code="outcome_uncertain")
        raw.update(after={"elements": []}, diagnostics={
            "reason": "browser_credential_endpoint_not_approved",
            "evidence_errorcode": "browser_snapshot_unavailable", "message": "DO-NOT-EXPORT",
        })
        return raw

    execution.manager._worker.side_effect = worker
    result = run(execution)
    assert result["diagnostics"] == {
        "reason": "browser_credential_endpoint_not_approved",
        "evidence_errorcode": "browser_snapshot_unavailable",
    }
    assert result["after"] == {"elements": []} and "DO-NOT-EXPORT" not in json.dumps(result)
    execution.manager._close_browser.assert_not_awaited()


def test_revoked_authority_while_worker_runs_discards_response_and_closes(execution):
    async def worker(path, payload):
        raw = response(execution.manager.browsers[execution.session_id], payload["operation_id"])
        execution.manager._idle(execution.session_id)
        return raw

    execution.manager._worker.side_effect = worker
    assert run(execution) == {"ok": False, "code": "execution_authorization_changed"}
    execution.manager._close_browser.assert_awaited_once_with(execution.session_id)


def test_cross_origin_snapshot_rejected(execution):
    async def worker(path, payload):
        raw = response(execution.manager.browsers[execution.session_id], payload["operation_id"])
        raw["after"]["url"] = "https://other.example/"
        return raw

    execution.manager._worker.side_effect = worker
    assert run(execution) == {"ok": False, "code": "execution_unavailable"}
    execution.manager._close_browser.assert_awaited_once()


@pytest.mark.parametrize("location", [
    "before", "after", "result", "logs", "metadata", "diagnostics", "blocked_url", "blocked_body",
    "success_evidence",
])
@pytest.mark.parametrize("secret", [USERNAME, PASSWORD, TOKEN])
def test_known_secrets_rejected_in_entire_raw_worker_response(execution, location, secret):
    bind(execution)

    async def worker(path, payload):
        raw = response(execution.manager.browsers[execution.session_id], payload["operation_id"])
        if location in {"before", "after"}:
            raw[location]["elements"] = [{"tag": "h1", "text": "Welcome " + secret}]
        elif location == "result":
            raw[location]["ignored"] = secret
        elif location == "success_evidence":
            from tests.test_login_evidence_projection import evidence

            raw["result"]["success_evidence"] = evidence()
            raw["result"]["success_evidence"]["selectors"][0]["selector"] = secret
        elif location in {"blocked_url", "blocked_body"}:
            raw["diagnostics"] = {"blocked_requests": [{
                "code": "browser_url_denied", "method": "POST",
                "url": BASE + "login?password=" + secret if location == "blocked_url" else BASE,
                "post_data": secret if location == "blocked_body" else "ignored-body",
            }]}
        else:
            raw[location] = [secret]
        return raw

    execution.manager._worker.side_effect = worker
    assert run(execution, arguments("auth.form_login")) == {"ok": False, "code": "secret_output"}
    execution.manager._close_browser.assert_awaited_once()


def test_credential_resolution_errors_are_static(execution, monkeypatch):
    bind(execution)

    def fail(*args):
        raise ValueError(PASSWORD)

    monkeypatch.setattr(CredentialService, "resolve", fail)
    assert run(execution, arguments("auth.form_login")) == {"ok": False, "code": "credentials_unavailable"}
    execution.manager._worker.assert_not_awaited()


def test_operation_ids_survive_new_service_and_browser_and_are_reserved_before_http(execution):
    args = arguments()

    async def worker(path, payload):
        with execution.manager.session_factory() as db:
            data = execution.manager._decrypt(db.get(AssistantSession, execution.session_id))
            assert payload["operation_id"] in data["_execution_operations"]
        raise TimeoutError(PASSWORD)

    execution.manager._worker.side_effect = worker
    assert run(execution, args) == {"ok": False, "code": "execution_unavailable"}
    execution.manager.browsers[execution.session_id] = BrowserScope(
        OWNER, str(uuid4()), TOKEN, "interaction", "", time.monotonic() + 900,
    )
    other = AssistantExecution(execution.manager, OWNER, execution.session_id, execution.target)
    assert run(other, args) == {"ok": False, "code": "operation_already_used"}
    execution.manager._worker.assert_awaited_once()


def test_repeated_successful_id_is_not_resolved_or_posted_again(execution, monkeypatch):
    bind(execution)
    calls = []

    def resolve(service, site_id):
        calls.append(site_id)
        return SiteSecret(USERNAME, PASSWORD)

    monkeypatch.setattr(CredentialService, "resolve", resolve)
    args = arguments("auth.form_login")
    assert run(execution, args)["ok"]
    assert run(execution, args) == {"ok": False, "code": "operation_already_used"}
    assert len(calls) == 1
    execution.manager._worker.assert_awaited_once()


def test_cancel_while_resolving_credentials_closes_owned_browser_without_post(execution, monkeypatch):
    bind(execution)

    async def scenario():
        entered = asyncio.Event()

        async def resolve(function):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(asyncio, "to_thread", resolve)
        task = asyncio.create_task(execution.execute(arguments("auth.form_login")))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    execution.manager._worker.assert_not_awaited()
    execution.manager._close_browser.assert_awaited_once_with(execution.session_id)


def test_cancel_inflight_worker_closes_without_retry(execution):
    async def scenario():
        entered = asyncio.Event()

        async def worker(path, payload):
            entered.set()
            await asyncio.Event().wait()

        execution.manager._worker.side_effect = worker
        task = asyncio.create_task(execution.execute(arguments()))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    execution.manager._worker.assert_awaited_once()
    execution.manager._close_browser.assert_awaited_once_with(execution.session_id)


def test_no_workflow_site_or_credential_mutation(execution):
    identifier = bind(execution)

    def state():
        with execution.manager.session_factory() as db:
            site = db.get(Site, identifier)
            credential = db.get(SiteCredential, identifier)
            return (
                tuple(db.scalar(select(func.count()).select_from(model))
                      for model in (Site, SiteCredential, WorkflowStep, WorkflowHistory)),
                site.history_key, site.config_revision, site.base_url,
                credential.username_ciphertext, credential.password_ciphertext,
                credential.updated_at, credential.last_verified_at,
            )

    before = state()
    assert run(execution, arguments("auth.form_login"))["ok"]
    assert run(execution, arguments("sign.button"))["ok"]
    assert state() == before
