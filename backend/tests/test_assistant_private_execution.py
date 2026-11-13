from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
from httpx import Client as HTTPClient
import pytest

from app.features.catalog import FEATURES
from app.models.assistant import AssistantSession
from app.schemas.assistant import SetTarget
from app.services.assistant_execution import SUPPORTED_FEATURES
from app.services.assistant_interactions import ADAPTER, grant, prepare
from app.services.assistant_retry import READ_ONLY, RECORDS
from app.services.bitwarden import BitwardenSecret, BitwardenService
from app.services.feature_settings import FeatureSettingsService
from app.services.integrations import IntegrationService
from tests.test_assistant_execution import (
    BASE, PASSWORD, USERNAME, arguments, bind, execution, response, run, save_target,
)
from tests.test_assistant_snapshot import node, snapshot


VISION_KEY = "private-vision-provider-key"
CFBOX_KEY = "private-cfbox-provider-key"
TOTP_TOKEN = "private-twofauth-token"
CODE = "719632"
VISION = {"base_url": "http://solver.internal/v1", "api_key": VISION_KEY,
          "model": "private-solver-model", "timeout": 20, "max_tokens": 0,
          "supports_vision": True}


@pytest.fixture
def integrations(monkeypatch):
    monkeypatch.setattr(IntegrationService, "vision_runtime", lambda self: deepcopy(VISION))
    for method, value in {
        "resolve_cfbox_url": "http://cfbox.internal", "resolve_cfbox_api_key": CFBOX_KEY,
        "resolve_twofauth_url": "http://twofauth.internal", "resolve_twofauth_token": TOTP_TOKEN,
    }.items():
        monkeypatch.setattr(IntegrationService, method, lambda self, value=value: SimpleNamespace(value=value))
    requests = []

    def provider(request):
        requests.append(request)
        assert request.headers["Authorization"] == f"Bearer {TOTP_TOKEN}"
        if request.url.path.endswith("/otp"):
            return httpx.Response(200, json={"password": CODE})
        return httpx.Response(200, json=[{"id": 7, "service": "Example", "account": USERNAME}])

    monkeypatch.setattr("app.services.assistant_execution.httpx.Client", lambda **kw: HTTPClient(
        **kw, transport=httpx.MockTransport(provider)))
    return requests


@pytest.mark.parametrize("feature,config,private_key", [
    ("challenge.2fa", {"service_hint": "Example", "username_hint": USERNAME}, "totp_code"),
    ("challenge.turnstile", {}, "cfbox"),
    ("challenge.image_captcha", {"captcha_method": "vision"}, "vision"),
    ("sign.iframe.image_captcha", {"captcha_method": "vision"}, "vision"),
    ("challenge.grid_captcha", {}, "vision"),
    ("sign.image_quiz", {"message_text": ""}, "vision"),
])
def test_private_provider_delivery_matches_worker_contract(execution, integrations, monkeypatch,
                                                         feature, config, private_key):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    from worker.assistant_step import ExecuteRequest, validate_step

    bind(execution)
    captured, references = [], []

    async def worker(path, payload):
        captured.append(deepcopy(payload))
        references.append(payload)
        request = ExecuteRequest.model_validate(payload)
        assert validate_step(request)["feature_type"] == feature
        raw = response(execution.manager.browsers[execution.session_id], payload["operation_id"])
        raw['before'] = snapshot([node(tag='canvas')], url=BASE)
        raw['after'] = snapshot([node(text='Solved captcha answer: solverA9')], url=BASE + 'home')
        raw["diagnostics"] = {"reason": "provider-private-reason"}
        return raw

    execution.manager._worker.side_effect = worker
    args = arguments(feature, config)
    result = run(execution, args)
    assert result["ok"], result
    assert result["before"]["elements"] and result["after"]["elements"]
    assert result['after']['elements'][0]['text'] == 'Solved captcha answer: solverA9'
    assert 'diagnostics' not in result
    runtime = captured[0]["private_runtime"]
    assert set(runtime) == {private_key}
    assert "credentials" not in captured[0]
    assert "private_runtime" not in references[0]
    if private_key == "totp_code":
        assert runtime == {"totp_code": CODE}
        assert len(integrations) == 2
    if private_key == "vision":
        assert runtime["vision"]["api_key"] == VISION_KEY
        assert runtime["vision"]["execution_settings"]["llm_prompt"]
    with execution.manager.session_factory() as db:
        data = execution.manager._decrypt(db.get(AssistantSession, execution.session_id))
    exported = json.dumps([result, data, args])
    assert all(secret not in exported for secret in [VISION_KEY, CFBOX_KEY, TOTP_TOKEN, CODE, PASSWORD])


@pytest.mark.parametrize("secret", [VISION_KEY, VISION["base_url"], VISION["model"]])
@pytest.mark.parametrize("location", ["reason", "code", "logs", "metadata"])
def test_known_provider_values_rejected_even_in_discarded_fields(execution, integrations, secret, location):
    async def worker(path, payload):
        raw = response(execution.manager.browsers[execution.session_id], payload["operation_id"])
        if location == "reason":
            raw["result"]["reason"] = secret
        else:
            raw[location] = secret
        return raw

    execution.manager._worker.side_effect = worker
    assert run(execution, arguments("sign.image_quiz")) == {"ok": False, "code": "secret_output"}
    execution.manager._close_browser.assert_awaited_once()
    assert "private_runtime" not in execution.manager._worker.call_args.args[1]


@pytest.mark.parametrize("secret", [CODE, TOTP_TOKEN, PASSWORD, USERNAME])
def test_totp_and_account_source_secrets_never_return(execution, integrations, secret):
    bind(execution)

    async def worker(path, payload):
        raw = response(execution.manager.browsers[execution.session_id], payload["operation_id"])
        raw["result"]["reason"] = secret
        return raw

    execution.manager._worker.side_effect = worker
    assert run(execution, arguments("challenge.2fa", {"service_hint": "Example"})) == {
        "ok": False, "code": "secret_output"}


@pytest.mark.parametrize("mode", ["none", "cookie", "manual"])
def test_totp_requires_real_account_source_before_provider_request(execution, integrations, mode):
    save_target(execution, {**execution.target, "credential_mode": mode})
    assert run(execution, arguments("challenge.2fa", {"service_hint": "Example"}))["code"] == "credentials_unavailable"
    assert not integrations
    execution.manager._worker.assert_not_awaited()


def test_totp_requires_credential_consent(execution, integrations):
    bind(execution)
    save_target(execution, {**execution.target, "credential_consent": False})
    assert run(execution, arguments("challenge.2fa", {"service_hint": "Example"}))["code"] == "credential_consent_required"
    assert not integrations


@pytest.mark.parametrize("feature,config", [
    ("challenge.2fa", {"service_hint": "Example", "totp_provider_id": "plugin.other"}),
    ("challenge.2fa", {"service_hint": "Example", "totp_code": CODE}),
    ("challenge.image_captcha", {"captcha_method": "plugin.other.solve"}),
    ("flow.result", {"llm_enabled": True}), ("flow.result", {"llm_prompt": "override"}),
    ("sign.iframe.image_captcha", {"fields": {"password": PASSWORD}}),
])
def test_private_input_and_plugin_overrides_denied(execution, feature, config):
    assert run(execution, arguments(feature, config)) == {"ok": False, "code": "invalid_step"}
    execution.manager._worker.assert_not_awaited()


def test_flow_result_false_is_success_and_read_only_during_uncertainty(execution):
    assert "flow.result" in READ_ONLY
    with execution.manager.session_factory() as db:
        row = db.get(AssistantSession, execution.session_id)
        data = execution.manager._decrypt(row)
        data[RECORDS] = {"older": {"state": "terminal", "status": "unknown"}}
        execution.manager._encrypt(row, data)
        db.commit()

    async def worker(path, payload):
        assert "private_runtime" not in payload
        assert payload["step"]["config"] == {"output_value": False, "message": "Observed failure"}
        raw = response(execution.manager.browsers[execution.session_id], payload["operation_id"])
        raw["result"] = {"value": False, "reason": "private-provider-reason", "message": "private-message"}
        return raw

    execution.manager._worker.side_effect = worker
    result = run(execution, arguments("flow.result", {"output_value": False, "message": "Observed failure"}))
    assert result["ok"] and result["status"] == "success" and result["result"] == {"value": False}
    with execution.manager.session_factory() as db:
        data = execution.manager._decrypt(db.get(AssistantSession, execution.session_id))
    assert data[RECORDS]["older"]["status"] == "unknown"


def test_only_flow_result_projects_boolean_value(execution):
    scope = execution.manager.browsers[execution.session_id]
    raw = response(scope, "operation")
    raw["result"] = {"value": False}
    assert execution._project(raw, scope, "operation", "action.wait")["result"] == {}
    raw["result"]["value"] = 0
    assert execution._project(raw, scope, "operation", "flow.result")["result"] == {}


def test_host_result_prompt_and_flags_are_private(execution, integrations, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    from unittest.mock import AsyncMock
    from worker.assistant_step import ExecuteRequest, validate_step
    from worker.executor import _run_step

    with execution.manager.session_factory() as db:
        FeatureSettingsService(db).update(FEATURES["flow.result"], {
            "llm_enabled": True, "llm_prompt": "host-private-result-prompt"}, [], {
            "llm_enabled": True, "judgment_scope": "all", "use_vision": False})

    judge = AsyncMock(return_value={"result": False, "reason": "Observed failure"})
    monkeypatch.setattr("worker.vision.judge_result", judge)

    async def worker(path, payload):
        assert payload["step"]["config"] == {"output_value": False, "message": "", "llm_enabled": True}
        behavior = payload["private_runtime"]["vision"]["execution_settings"]
        assert behavior["llm_prompt"] == "host-private-result-prompt"
        request = ExecuteRequest.model_validate(payload)
        step = validate_step(request)
        status, _, data = await _run_step(
            SimpleNamespace(evaluate=AsyncMock(return_value="Visible page evidence")),
            step, credentials=None, twofauth=None, cfbox=None,
            vision=request.private_runtime.vision.model_dump(), cancel=None,
            xdisplay=None, lane_key="interaction")
        assert status == "success"
        raw = response(execution.manager.browsers[execution.session_id], payload["operation_id"])
        raw["result"] = data
        return raw

    execution.manager._worker.side_effect = worker
    result = run(execution, arguments("flow.result", {"output_value": False}))
    assert result["ok"] and result["result"] == {"value": False}
    judge.assert_awaited_once()
    assert judge.call_args.args[1] == "host-private-result-prompt"
    assert judge.call_args.kwargs["page_text"] == "Visible page evidence"
    with execution.manager.session_factory() as db:
        data = execution.manager._decrypt(db.get(AssistantSession, execution.session_id))
    assert "host-private-result-prompt" not in json.dumps([result, data])


@pytest.mark.parametrize("credentials", [False, True])
def test_grants_and_target_capacity_cover_eighteen_features(execution, credentials):
    target = {**execution.target, "authorized": True, "browser_consent": True,
              "credential_mode": "bitwarden" if credentials else "none"}
    grant(target, "execution")
    expected = SUPPORTED_FEATURES if credentials else SUPPORTED_FEATURES - {
        "auth.form_login", "auth.password_verify", "challenge.2fa"}
    assert set(target["execution_features"]) == expected
    assert target["credential_consent"] is credentials
    assert target["allow_sign"] is True
    SetTarget.model_validate({key: value for key, value in target.items() if not key.startswith("_")})


def test_prepare_totp_without_account_source_is_denied(execution):
    from app.services.assistant import AssistantError

    request = ADAPTER.validate_python({"purpose": "authorization", "capability": "execution",
                                      "feature_type": "challenge.2fa"})
    with execution.manager.session_factory() as db, pytest.raises(AssistantError) as caught:
        prepare(db, request, {"target": {**execution.target, "credential_mode": "cookie"}}, {})
    assert caught.value.code == "credentials_unavailable"


def test_totp_bitwarden_provenance_uses_exact_origin_without_saved_site(execution, integrations, monkeypatch):
    save_target(execution, {**execution.target, "credential_mode": "bitwarden"})
    lookups = []

    def resolve(service, site):
        lookups.append((site.id, site.base_url, site.bitwarden_match_mode, site.bitwarden_custom_match))
        return BitwardenSecret(USERNAME, PASSWORD, item_name="private-item", matched_by="private-match")

    monkeypatch.setattr(BitwardenService, "resolve_for_site", resolve)
    assert run(execution, arguments("challenge.2fa", {"service_hint": "Example"}))["ok"]
    assert lookups == [(None, BASE, "domain", None)]
    assert len(integrations) == 2


@pytest.mark.parametrize("change", ["origin", "revision", "history_key"])
def test_totp_binding_rechecked_before_provider(execution, integrations, change):
    from app.models.workflow import Site

    identifier = bind(execution)
    with execution.manager.session_factory() as db:
        site = db.get(Site, identifier)
        if change == "origin":
            site.base_url = "https://different.example/"
        elif change == "revision":
            site.config_revision += 1
        else:
            site.history_key = "replacement"
        db.commit()
    assert run(execution, arguments("challenge.2fa", {"service_hint": "Example"}))["code"] == "credential_binding_changed"
    assert not integrations
    execution.manager._worker.assert_not_awaited()


def test_private_runtime_authority_rechecked_after_thread(execution, monkeypatch):
    async def resolve(function, step):
        with execution.manager.session_factory() as db:
            row = db.get(AssistantSession, execution.session_id)
            data = execution.manager._decrypt(row)
            data["target"]["execution_consent"] = False
            execution.manager._encrypt(row, data)
            db.commit()
        return {"vision": deepcopy(VISION)}, [VISION_KEY]

    monkeypatch.setattr(asyncio, "to_thread", resolve)
    assert run(execution, arguments("sign.image_quiz"))["code"] == "execution_authorization_changed"
    execution.manager._worker.assert_not_awaited()


def test_totp_resolution_has_thirty_second_budget_and_static_error(execution, monkeypatch):
    budgets = []
    timeout = asyncio.timeout

    def budget(seconds):
        budgets.append(seconds)
        return timeout(seconds)

    async def resolve(function, step):
        raise TimeoutError(TOTP_TOKEN)

    monkeypatch.setattr(asyncio, "timeout", budget)
    monkeypatch.setattr(asyncio, "to_thread", resolve)
    assert run(execution, arguments("challenge.2fa", {"service_hint": "Example"})) == {
        "ok": False, "code": "credentials_unavailable"}
    assert budgets == [30]
    execution.manager._worker.assert_not_awaited()


@pytest.mark.parametrize("cancel", [False, True])
def test_worker_failure_always_removes_private_payload(execution, integrations, cancel):
    references = []

    async def worker(path, payload):
        references.append(payload)
        if cancel:
            raise asyncio.CancelledError()
        raise ValueError(VISION_KEY)

    execution.manager._worker.side_effect = worker
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            run(execution, arguments("sign.image_quiz"))
    else:
        assert run(execution, arguments("sign.image_quiz")) == {"ok": False, "code": "execution_unavailable"}
    assert "private_runtime" not in references[0]
    execution.manager._close_browser.assert_awaited_once()


@pytest.mark.parametrize("body,expected", [
    ({"detail": {"code": "browser_snapshot_stale"}}, "browser_snapshot_stale"),
    ({"detail": {"code": "private-provider-unreviewed-reason"}}, "browser_lane_busy"),
])
def test_worker_http409_reason_is_static(execution, monkeypatch, body, expected):
    from app.services.assistant import AssistantError, AssistantManager

    client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(409, json=body))
    monkeypatch.setattr("app.services.assistant.httpx.AsyncClient", lambda **kw: client(**kw, transport=transport))
    with pytest.raises(AssistantError) as caught:
        asyncio.run(AssistantManager._worker(execution.manager, "browser/screenshot", {}))
    assert caught.value.reason == expected


def test_backend_to_real_worker_executor_flow_result_false(execution, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    from worker.assistant_step import ExecuteRequest, validate_step
    from worker.executor import _run_step

    async def worker(path, payload):
        request = ExecuteRequest.model_validate(payload)
        step = validate_step(request)
        status, _, data = await _run_step(None, step, credentials=None, twofauth=None,
                                        cfbox=None, vision=None, cancel=None,
                                        xdisplay=None, lane_key="interaction")
        assert status == "success"
        raw = response(execution.manager.browsers[execution.session_id], payload["operation_id"])
        raw["result"] = data
        return raw

    execution.manager._worker.side_effect = worker
    result = run(execution, arguments("flow.result", {"output_value": False}))
    assert result["ok"] and result["result"] == {"value": False}


@pytest.mark.parametrize("case", ["ambiguous", "unmatched", "bad_code", "provider_error", "redirect"])
def test_twofauth_resolution_fails_closed_without_exporting_provider_details(execution, integrations, monkeypatch, case):
    bind(execution)
    requests = []

    def provider(request):
        requests.append(request)
        if case == "provider_error":
            return httpx.Response(500, text=TOTP_TOKEN)
        if case == "redirect":
            return httpx.Response(302, headers={"Location": "http://other.internal/"})
        if request.url.path.endswith("/otp"):
            return httpx.Response(200, json={"password": "not-a-code-" + TOTP_TOKEN})
        accounts = [{"id": 1, "service": "Example", "account": USERNAME}]
        if case == "ambiguous":
            accounts.append({"id": 2, "service": "Example", "account": USERNAME})
        elif case == "unmatched":
            accounts = []
        return httpx.Response(200, json=accounts)

    monkeypatch.setattr("app.services.assistant_execution.httpx.Client", lambda **kw: HTTPClient(
        **kw, transport=httpx.MockTransport(provider)))
    result = run(execution, arguments("challenge.2fa", {"service_hint": "Example"}))
    assert result == {"ok": False, "code": "credentials_unavailable"}
    assert requests
    if case == "redirect":
        assert len(requests) == 1
    execution.manager._worker.assert_not_awaited()
