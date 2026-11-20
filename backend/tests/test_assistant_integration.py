from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from jsonschema import Draft202012Validator
import pytest
from sqlalchemy import select

from app.models.assistant import AssistantMessage, AssistantSession
from app.models.execution import Run
from app.models.workflow import Site, Template, WorkflowStep
from app.schemas.workflow import SiteWrite
from app.services.assistant import AssistantManager
from app.services.assistant_data import AssistantData
from app.services.assistant_execution import AssistantExecution, SUPPORTED_FEATURES
from app.services.assistant_prompt import tool_definitions
from app.services.assistant_tools import AssistantTools, _AssistantWorkflowService
from app.services.credentials import CredentialService, SiteSecret
from app.services.lanes import LaneService
from app.services.workflows import WorkflowService
from tests.test_assistant_api import (
    KEY, ROOT, TARGET, browser_open_response, counts, create,
    send, target, text, tool, wait,
)
from tests.test_assistant_api import (  # noqa: F401
    assistant_client, provider, title_provider,
)
from tests.test_assistant_execution import response as execution_response


USERNAME = "integration-private-account@example.com"
PASSWORD = "integration-private-password-never-export"


@pytest.fixture
def records(assistant_client):
    manager = assistant_client.app.state.assistant_manager
    with manager.session_factory() as db:
        site = Site(name="Saved Example", base_url="https://EXAMPLE.com:443/saved")
        template = Template(name="Login example")
        run = Run(created_by="admin", summary_json=json.dumps({"private": PASSWORD}))
        db.add_all([site, template, run])
        db.flush()
        db.add(WorkflowStep(
            owner_type="template", owner_id=template.id, step_key="login",
            feature_type="auth.form_login", category="auth", config_json=json.dumps({
                "username_selector": ["#username"], "password_selector": ["#password"],
                "submit_selector": ["#submit"],
            }),
        ))
        db.commit()
        CredentialService(db, manager.crypto).update(
            site.id, username=USERNAME, password=PASSWORD,
        )
        return {
            "site_id": site.id, "template_id": template.id, "run_id": run.id,
            "binding": {"id": site.id, "history_key": site.history_key,
                        "revision": site.config_revision},
        }


@pytest.mark.parametrize("changes,code", [
    ({"execution_features": ["action.wait"]}, "execution_consent_required"),
    ({"execution_consent": True}, "execution_features_required"),
    ({"credential_consent": True}, "execution_consent_required"),
    ({"allow_sign": True}, "execution_consent_required"),
    ({"execution_consent": True, "execution_features": ["action.wait", "action.wait"]},
     "invalid_execution_features"),
    ({"execution_consent": True, "execution_features": ["plugin.private"]},
     "invalid_execution_features"),
    ({"execution_consent": True, "execution_features": ["post.buy_card"]},
     "invalid_execution_features"),
    ({"execution_consent": True, "execution_features": ["auth.form_login"]},
     "credential_consent_required"),
    ({"execution_consent": True, "credential_consent": True,
      "execution_features": ["auth.form_login"]}, "credential_site_required"),
    ({"credential_mode": "cookie", "execution_consent": True, "credential_consent": True,
      "execution_features": ["auth.form_login"]}, "credentials_unavailable"),
    ({"credential_mode": "none", "execution_consent": True, "credential_consent": True,
       "execution_features": ["auth.password_verify"]}, "credentials_unavailable"),
    ({"execution_consent": True, "execution_features": ["challenge.2fa"], "allow_sign": True},
     "credential_consent_required"),
    ({"execution_consent": True, "execution_features": ["challenge.2fa"],
      "credential_consent": True, "allow_sign": True}, "credential_site_required"),
    ({"credential_mode": "cookie", "execution_consent": True, "credential_consent": True,
      "execution_features": ["challenge.2fa"], "allow_sign": True}, "credentials_unavailable"),
    *[({"execution_consent": True, "execution_features": [feature]}, "sign_consent_required")
       for feature in ("sign.button", "sign.unit3d_claim", "action.click",
                       "challenge.altcha", "challenge.slider", "action.fill",
                       "challenge.turnstile", "challenge.image_captcha", "challenge.grid_captcha",
                       "sign.iframe.image_captcha", "sign.image_quiz")],
])
def test_target_cross_field_errors_are_atomic(assistant_client, changes, code):
    client = assistant_client
    identifier = create(client)
    original = target(client, identifier)
    response = client.put(f"{ROOT}/{identifier}/target", json={**TARGET, **changes})
    assert response.status_code == 422, response.text
    assert response.json() == {"detail": {"code": code}}
    assert response.headers["cache-control"] == "no-store"
    assert client.get(f"{ROOT}/{identifier}").json() == original


@pytest.mark.parametrize("changes", [
    {"site_id": True}, {"site_id": "1"}, {"execution_consent": 1},
    {"credential_consent": "true"}, {"allow_sign": 1},
    {"execution_features": "action.wait"}, {"execution_features": [True]},
    {"_credential_binding": {"id": 1, "history_key": PASSWORD, "revision": 1}},
])
def test_target_authority_fields_are_strict(assistant_client, changes):
    identifier = create(assistant_client)
    response = assistant_client.put(
        f"{ROOT}/{identifier}/target", json={**TARGET, **changes},
    )
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "invalid_request"}}
    detail = assistant_client.get(f"{ROOT}/{identifier}").json()
    assert detail["target"] is None and detail["status"] == "idle"
    assert PASSWORD not in response.text


@pytest.mark.parametrize("base_url,accepted", [
    ("https://example.com/other-path", True),
    ("https://EXAMPLE.COM:443/", True),
    ("http://example.com/", False),
    ("https://example.com:444/", False),
    ("https://sub.example.com/", False),
    ("https://other.example/", False),
])
@pytest.mark.parametrize("overall", [False, True])
def test_saved_site_binding_requires_exact_origin(
    assistant_client, records, base_url, accepted, overall,
):
    client = assistant_client
    manager = client.app.state.assistant_manager
    identifier = create(client)
    payload = {**TARGET, "site_id": records["site_id"], "base_url": base_url}
    if overall:
        payload.pop("confirmed")
        payload.pop("browser_consent")
        payload["authorized"] = True
    else:
        payload.update(execution_consent=True, credential_consent=True,
                       execution_features=["auth.form_login"])
    response = client.put(f"{ROOT}/{identifier}/target", json=payload)
    if not accepted:
        assert response.status_code == 422
        assert response.json() == {"detail": {"code": "credential_site_mismatch"}}
        assert client.get(f"{ROOT}/{identifier}").json()["target"] is None
        return
    assert response.status_code == 200, response.text
    public = response.json()["target"]
    private, history = manager._history("admin", identifier)
    assert private == {**public, "_credential_binding": records["binding"]}
    with manager.session_factory() as db:
        row = db.get(AssistantSession, identifier)
        assert manager._decrypt(row)["target"] == private
        assert records["binding"]["history_key"].encode() not in row.ciphertext
    assert json.loads(history[1]["content"].split(": ", 1)[1]) == public
    exposed = json.dumps([response.json(), history, client.get(ROOT).json()])
    for value in ("_credential_binding", records["binding"]["history_key"], USERNAME, PASSWORD):
        assert value not in exposed


@pytest.mark.parametrize("mode,bound,credentials_available", [
    ("manual", False, False), ("manual", True, True),
    ("bitwarden", False, True), ("cookie", False, False), ("none", False, False),
])
def test_overall_target_grant_freezes_reviewed_features_and_private_binding(
    assistant_client, records, monkeypatch, mode, bound, credentials_available,
):
    client = assistant_client
    manager = client.app.state.assistant_manager
    identifier = create(client)
    payload = {"name": "Example", "base_url": "https://EXAMPLE.com:443",
               "credential_mode": mode, "proxy": True, "authorized": True}
    if bound:
        payload["site_id"] = records["site_id"]
    response = client.put(f"{ROOT}/{identifier}/target", json=payload)
    assert response.status_code == 200, response.text
    public = response.json()["target"]
    features = sorted(key for key in SUPPORTED_FEATURES
                      if credentials_available or not (key.startswith("auth.") or key == "challenge.2fa"))
    assert public == {
        **payload, "base_url": "https://example.com/", "confirmed": True, "browser_consent": True,
        "execution_consent": True, "allow_sign": True,
        "credential_consent": credentials_available, "execution_features": features,
        "run_lane_policy": "auto", "workflow_site_id": None, "workflow_consent": False,
    }
    assert {"action.click", "action.fill", "sign.button", "challenge.slider", "flow.result"} <= set(features)
    assert not any(key.startswith(("post.", "plugin.")) or key == "flow.jump"
                   for key in features)
    private, history = manager._history("admin", identifier)
    assert private == ({**public, "_credential_binding": records["binding"]} if bound else public)
    with manager.session_factory() as db:
        stored = manager._decrypt(db.get(AssistantSession, identifier))["target"]
        assert stored == private
    monkeypatch.setattr("app.services.assistant_execution.SUPPORTED_FEATURES",
                        SUPPORTED_FEATURES | {"action.future_reviewed"})
    fresh = AssistantManager(manager.session_factory, manager.settings, manager.crypto)
    assert fresh.detail("admin", identifier)["target"] == public
    assert json.loads(history[1]["content"].split(": ", 1)[1]) == public
    exposed = json.dumps([response.json(), history, client.get(ROOT).json()])
    for value in ("_credential_binding", records["binding"]["history_key"], USERNAME, PASSWORD):
        assert value not in exposed
    tools = AssistantTools(manager, "admin", identifier, private)
    assert asyncio.run(tools.dispatch("browser_open", {"lane_key": "general"})) == {
        "ok": False, "code": "invalid_arguments",
    }


def test_overall_grant_normalizes_legacy_permission_fields(assistant_client):
    client = assistant_client
    identifier = create(client)
    public = target(client, identifier, authorized=True, execution_consent=False,
                    credential_consent=True, allow_sign=False,
                    execution_features=["post.buy_card"])["target"]
    assert public["execution_consent"] is True and public["allow_sign"] is True
    assert public["credential_consent"] is False
    assert public["execution_features"] == sorted(
        key for key in SUPPORTED_FEATURES if not key.startswith("auth.") and key != "challenge.2fa"
    )


def test_legacy_targets_keep_their_concrete_grants_after_reload(assistant_client):
    client = assistant_client
    identifier = create(client)
    original = target(client, identifier, execution_consent=True,
                      execution_features=["action.wait"])["target"]
    assert original == {
        **TARGET, "execution_consent": True, "execution_features": ["action.wait"],
        "run_lane_policy": "auto", "workflow_site_id": None, "workflow_consent": False,
    }
    manager = client.app.state.assistant_manager
    fresh = AssistantManager(manager.session_factory, manager.settings, manager.crypto)
    fresh.recover()
    assert fresh.detail("admin", identifier)["target"] == original
    private, _ = fresh._history("admin", identifier)
    assert private == original
    assert "authorized" not in private and "allow_sign" not in private


def test_all_recognized_tool_schemas_and_local_fragment_references():
    definitions = tool_definitions()
    names = [item["function"]["name"] for item in definitions]
    assert len(names) == len(set(names)) == 39
    assert "create_template_from_site" in names
    assert set(names) == {
        "list_features", "get_feature_schema", "validate_workflow_draft",
        "submit_workflow_draft", "browser_open", "browser_observe", "browser_navigate",
        "clear_bound_site_cookies",
        "browser_inspect", "browser_check_selector",
        "check_site", "execute_step", "list_sites", "get_site_config", "list_templates",
        "get_template_config", "get_system_status", "list_recent_runs", "get_run_summary",
        "get_tool_result", "request_user_interaction",
        "browser_screenshot", "browser_refresh", "retry_step",
        "import_workflow_draft", "run_site", "get_run_results", "get_run_log",
        "update_site_config", "create_template_from_site",
        "get_scheduler_settings", "update_scheduler_settings", "cancel_run",
        "list_workflow_versions", "get_workflow_version", "list_run_artifacts",
        "read_artifact_log", "read_artifact_image", "submit_takeover_conclusion",
    }
    for definition in definitions:
        schema = definition["function"]["parameters"]
        Draft202012Validator.check_schema(schema)
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert schema["required"].count("title") == 1
        assert schema["properties"]["title"] == {
            "type": "string", "minLength": 1, "maxLength": 120,
        }
        pending = [schema]
        while pending:
            node = pending.pop()
            if isinstance(node, list):
                pending.extend(node)
            elif isinstance(node, dict):
                if "$ref" in node:
                    reference = node["$ref"]
                    assert reference.startswith("#/"), reference
                    resolved = schema
                    for fragment in reference[2:].split("/"):
                        resolved = resolved[fragment.replace("~1", "/").replace("~0", "~")]
                    assert isinstance(resolved, (dict, bool))
                pending.extend(node.values())
        if definition["function"]["name"] == "execute_step":
            assert {"step_key", "feature_type"} <= set(
                schema["properties"]["step"]["required"],
            )
            Draft202012Validator(schema).validate({
                "title": "Wait safely", "operation_id": str(uuid4()),
                "step": {"step_key": "wait", "feature_type": "action.wait",
                         "config": {"seconds": 1}, "on_error": "stop"},
            })
        elif definition["function"]["name"] in {
            "validate_workflow_draft", "submit_workflow_draft",
        }:
            candidate = {"title": "Review", "draft": {
                "steps": [{"step_key": "wait", "feature_type": "action.wait",
                           "on_error": "stop"}], "groups": [], "signals": [],
            }}
            if definition["function"]["name"] == "submit_workflow_draft":
                candidate.update(explanation="Review manually", unresolved_questions=[])
            Draft202012Validator(schema).validate(candidate)
    original = deepcopy(definitions)
    definitions[0]["function"]["parameters"].clear()
    assert tool_definitions() == original


def test_manager_dispatches_all_seven_system_reads_and_preserves_tool_rounds(
    assistant_client, provider, title_provider, records, monkeypatch,
):
    client = assistant_client
    identifier = create(client)
    lanes = AsyncMock(return_value=SimpleNamespace(worker_connected=True, lanes=[]))
    monkeypatch.setattr(LaneService, "get_lanes", lanes)
    calls = [
        ("list_sites", {}), ("get_site_config", {"site_id": records["site_id"]}),
        ("list_templates", {}), ("get_template_config", {"template_id": records["template_id"]}),
        ("get_system_status", {}), ("list_recent_runs", {}),
        ("get_run_summary", {"run_id": records["run_id"]}),
    ]
    before = counts(client)
    for start in (0, 4):
        batch = tool(calls[start][0])
        batch["choices"][0]["message"]["tool_calls"] = [
            tool(name, {"title": f"Read {name}", **args}, f"read_{index}")
            ["choices"][0]["message"]["tool_calls"][0]
            for index, (name, args) in enumerate(calls[start:start + 4], start)
        ]
        provider[0].append(batch)
    provider[0].append(text("Read-only discovery complete"))
    title_provider[0].append(text('{"title":"Configuration discovery"}'))
    send(client, identifier, content="Inspect the available configuration")
    detail = wait(client, identifier)
    logs = [item for item in detail["messages"] if item["role"] == "tool"]
    assert [item["tool_name"] for item in logs] == [name for name, _ in calls]
    assert [item["title"] for item in logs] == [f"Read {name}" for name, _ in calls]
    assert all(item["status"] == "success" and json.loads(item["content"])["ok"] for item in logs)
    assert json.loads(logs[1]["content"])["site"]["credentials_configured"] is True
    assert json.loads(logs[3]["content"])["local"]["steps"][0]["config"][
        "password_selector"
    ] == ["#password"]
    assert json.loads(logs[4]["content"])["worker_connected"] is True
    assert json.loads(logs[6]["content"])["run"]["id"] == records["run_id"]
    lanes.assert_awaited_once()
    assert detail["title"] == "Configuration discovery" and detail["title_revision"] == 1
    assert len(provider[1]) == 3 and len(title_provider[1]) == 1
    last = json.loads(provider[1][-1].content)
    assert [item["tool_call_id"] for item in last["messages"] if item["role"] == "tool"] == [
        f"read_{index}" for index in range(7)
    ]
    assert counts(client) == before
    assert client.get(f"{ROOT}/{identifier}").json() == detail
    for value in (USERNAME, PASSWORD, "_credential_binding", "_execution_operations"):
        assert value not in json.dumps(detail)
        assert all(value not in request.content.decode() for request in provider[1])
    tools = AssistantTools(client.app.state.assistant_manager, "admin", identifier, None)
    unknown = asyncio.run(tools.dispatch("execute_sql", {"query": "SELECT 1"}))
    assert unknown["ok"] is False
    assert unknown["diagnostics"][0]["code"] == "tool_unavailable"


@pytest.mark.parametrize("proxy", [False, True])
def test_general_stopped_status_reaches_model_without_blocking_interaction_browser(
    assistant_client, provider, monkeypatch, proxy,
):
    client = assistant_client
    identifier = create(client)
    target(client, identifier, authorized=True, proxy=proxy)
    lane = "proxy_interaction" if proxy else "interaction"
    lanes = AsyncMock(return_value=SimpleNamespace(worker_connected=True, lanes=[
        SimpleNamespace(key="general", enabled=True, status="stopped", in_use=0,
                        max_concurrency=1),
        SimpleNamespace(key=lane, enabled=True, status="stopped", in_use=0,
                        max_concurrency=1),
    ]))
    monkeypatch.setattr(LaneService, "get_lanes", lanes)
    worker_ids = []
    expires = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
    provider[0].extend([
        tool("get_system_status", call_id="status"),
        tool("browser_open", call_id="open"),
        browser_open_response(worker_ids, lane, expires),
        text("The selected interaction browser is ready."),
    ])
    send(client, identifier)
    detail = wait(client, identifier)
    logs = [message for message in detail["messages"] if message["role"] == "tool"]
    assert [message["tool_name"] for message in logs] == ["get_system_status", "browser_open"]
    assert all(message["status"] == "success" for message in logs)
    status = json.loads(logs[0]["content"])
    assert status["worker_connected"] is True
    assert status["lanes"][0]["key"] == "general" and status["lanes"][0]["status"] == "stopped"
    assert detail["browser"]["lane_key"] == lane and detail["browser"]["status"] == "ready"
    model_request = json.loads(provider[1][-1].content)
    assert next(message for message in model_request["messages"]
                if message.get("tool_call_id") == "status")["content"] == logs[0]["content"]
    prompt = model_request["messages"][0]["content"]
    section = " ".join(prompt.split("BROWSER LANES AND FAILURES:\n", 1)[1].split("\n\n", 1)[0].split())
    assert all(phrase in section for phrase in (
        "never general/proxy_general/model-selected",
        "browser_open starts lanes, not users",
        "stopped general lanes cannot block browsing",
        "Diagnose returned lane/reason/diagnostics; no guessed causes/stages",
    ))
    worker_requests = [request for request in provider[1]
                       if "/assistant-browser/" in str(request.url)]
    assert len(worker_requests) == 1
    assert json.loads(worker_requests[0].content)["lane_key"] == lane
    lanes.assert_awaited_once()
    provider[0].append({"closed": True})
    assert client.post(f"{ROOT}/{identifier}/cancel").status_code == 200
    assert not provider[0]


@pytest.mark.parametrize("feature", ["auth.form_login", "auth.password_verify"])
def test_overall_manual_unbound_auth_returns_saved_site_guidance_without_worker_call(
    assistant_client, provider, feature,
):
    client = assistant_client
    identifier = create(client)
    target(client, identifier, authorized=True)
    provider[0].extend([
        tool("execute_step", {"operation_id": str(uuid4()), "step": {
            "step_key": "login", "feature_type": feature, "config": {},
        }}, "login"), text("Select a saved same-origin account or Bitwarden in target settings."),
    ])
    send(client, identifier)
    detail = wait(client, identifier)
    log = next(message for message in detail["messages"] if message["role"] == "tool")
    assert json.loads(log["content"]) == {
        "ok": False, "code": "credentials_unavailable",
        "notice": "Bind a same-origin saved site with credentials or select Bitwarden; do not provide secrets in chat.",
    }
    assert log["status"] == "error"
    assert all("chat/completions" in str(request.url) for request in provider[1])
    manager = client.app.state.assistant_manager
    with manager.session_factory() as db:
        data = manager._decrypt(db.get(AssistantSession, identifier))
        assert not data.get("_execution_operations")


def test_bitwarden_proxy_draft_uses_host_sitewrite_settings_without_exporting_binding(
    assistant_client, provider, records, monkeypatch,
):
    client = assistant_client
    identifier = create(client)
    target(client, identifier, credential_mode="bitwarden", proxy=True,
           site_id=records["site_id"], trusted_origins=["https://sso.example"])
    captured = []
    validate = WorkflowService.validate_draft

    def inspect(service, owner_type, payload, owner_id=None, *, replace_local):
        assert isinstance(service, _AssistantWorkflowService)
        assert isinstance(payload, SiteWrite)
        assert replace_local is True
        captured.append(payload.model_dump())
        return validate(service, owner_type, payload, owner_id, replace_local=replace_local)

    monkeypatch.setattr(WorkflowService, "validate_draft", inspect)
    draft = {"steps": [{"step_key": "login", "feature_type": "auth.form_login",
                        "config": {"username_selector": ["#username"],
                                   "password_selector": ["#password"],
                                   "submit_selector": ["#submit"]}}], "groups": [], "signals": []}
    before = counts(client)
    provider[0].extend([
        tool("get_feature_schema", {"feature_type": "auth.form_login"}, "schema"),
        tool("validate_workflow_draft", {"draft": draft}, "validate"),
        tool("submit_workflow_draft", {"draft": draft, "explanation": "Review login",
                                       "unresolved_questions": []}, "submit"),
    ])
    send(client, identifier)
    detail = wait(client, identifier)
    logs = [item for item in detail["messages"] if item["role"] == "tool"]
    assert all(item["status"] == "success" for item in logs), logs
    assert len(captured) == 2
    for candidate in captured:
        assert candidate["bitwarden_enabled"] is True
        assert candidate["credential_provider_id"] == "builtin.bitwarden"
        assert candidate["bitwarden_match_mode"] == "domain"
        assert candidate["lane_policy"] == "auto"
        assert candidate["trusted_urls"] == []
    for log in logs[1:]:
        result = json.loads(log["content"])
        settings = result["required_site_settings"]
        assert settings == {
            "credential_mode": "bitwarden",
            "credential_provider_id": "builtin.bitwarden",
            "lane_policy": "auto",
            "trusted_origins": ["https://sso.example"],
            "notice": (
                "Import applies host-confirmed settings only with workflow_consent; "
                "manual saving requires separate review. Credentials and Cookie remain "
                "outside the proposal. trusted_origins is observation only and does not "
                "authorize credential trusted_urls; credential trust requires separate user review."
            ),
        }
        assert "local" not in result and "resolved" not in result
    proposal = json.loads(logs[-1]["content"])["proposal"]
    assert proposal["requires_review"] is True
    assert proposal["draft"]["steps"][0]["config"] == draft["steps"][0]["config"]
    assert counts(client) == before
    _, history = client.app.state.assistant_manager._history("admin", identifier)
    for value in ("_credential_binding", records["binding"]["history_key"], USERNAME, PASSWORD):
        assert value not in json.dumps([detail, history])
        assert all(value not in request.content.decode() for request in provider[1])


@pytest.mark.parametrize("mode", ["cookie", "none"])
@pytest.mark.parametrize("name", ["validate_workflow_draft", "submit_workflow_draft"])
def test_cookie_and_none_reject_auth_drafts_but_accept_non_auth(
    assistant_client, mode, name,
):
    client = assistant_client
    identifier = create(client)
    target(client, identifier, credential_mode=mode)
    manager = client.app.state.assistant_manager
    private, _ = manager._history("admin", identifier)
    tools = AssistantTools(manager, "admin", identifier, private)

    async def scenario():
        for feature in ("auth.form_login", "action.wait"):
            assert (await tools.dispatch("get_feature_schema", {"feature_type": feature}))["ok"]
            args = {"draft": {"steps": [{"step_key": "test", "feature_type": feature,
                                        "config": {}}], "groups": [], "signals": []}}
            if name == "submit_workflow_draft":
                args.update(explanation="Review safely", unresolved_questions=[])
            result = await tools.dispatch(name, args)
            if feature.startswith("auth."):
                assert result == {"ok": False, "code": "credential_mode_incompatible"}
            else:
                assert result["ok"], result
                assert result["required_site_settings"]["credential_mode"] == mode

    before = counts(client)
    asyncio.run(scenario())
    assert counts(client) == before


def test_execute_dispatch_receives_private_host_target_and_strips_model_title(
    assistant_client, provider, records, monkeypatch,
):
    client = assistant_client
    identifier = create(client)
    confirmed = target(client, identifier, site_id=records["site_id"], execution_consent=True,
                       execution_features=["action.wait"])
    args = {"operation_id": str(uuid4()), "step": {
        "step_key": "wait", "feature_type": "action.wait", "config": {"seconds": 1},
    }}
    captured = []

    async def execute(service, arguments):
        captured.append((service.owner, service.session_id, deepcopy(service.target), arguments))
        return {"ok": True, "code": "step_succeeded"}

    monkeypatch.setattr(AssistantExecution, "execute", execute)
    provider[0].extend([
        tool("execute_step", {"title": "Wait for page", **args}, "execute"), text("Wait completed"),
    ])
    send(client, identifier)
    detail = wait(client, identifier)
    assert captured == [("admin", identifier, {
        **confirmed["target"], "_credential_binding": records["binding"],
    }, args)]
    log = next(item for item in detail["messages"] if item["role"] == "tool")
    assert (log["tool_name"], log["title"], log["status"]) == (
        "execute_step", "Wait for page", "success",
    )
    assert json.loads(provider[1][-1].content)["messages"][-1] == {
        "role": "tool", "tool_call_id": "execute",
        "content": log["content"],
    }
    for value in ("_credential_binding", records["binding"]["history_key"], USERNAME, PASSWORD):
        assert value not in json.dumps(detail)
        assert all(value not in request.content.decode() for request in provider[1])


@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("overall", [False, True])
def test_read_template_browser_and_real_auth_execution_keep_credentials_worker_only(
    assistant_client, provider, title_provider, records, monkeypatch, stale, overall,
):
    client = assistant_client
    manager = client.app.state.assistant_manager
    identifier = create(client)
    if overall:
        response = client.put(f"{ROOT}/{identifier}/target", json={
            "name": "Example", "base_url": "https://example.com/", "credential_mode": "manual",
            "proxy": False, "site_id": records["site_id"], "authorized": True,
        })
        assert response.status_code == 200, response.text
    else:
        target(client, identifier, site_id=records["site_id"], execution_consent=True,
               credential_consent=True, execution_features=["auth.form_login"])
    if stale:
        with manager.session_factory() as db:
            db.get(Site, records["site_id"]).config_revision += 1
            db.commit()
    resolved = []

    def credentials(service, site_id):
        resolved.append(site_id)
        return SiteSecret(USERNAME, PASSWORD)

    monkeypatch.setattr(CredentialService, "resolve", credentials)
    worker_ids = []
    expires = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
    operation_id = str(uuid4())
    args = {"operation_id": operation_id, "step": {
        "step_key": "login", "feature_type": "auth.form_login", "config": {
            "username_selector": ["#username"], "password_selector": ["#password"],
            "submit_selector": ["#submit"],
        },
    }}

    async def executed(request):
        payload = json.loads(request.content)
        assert payload["credentials"] == {"username": USERNAME, "password": PASSWORD}
        assert payload["step"]["condition"] == "always"
        assert payload["step"]["config"]["username_selector"] == ["#username"]
        scope = manager.browsers[identifier]
        assert payload["token"] == scope.token
        return execution_response(scope, payload["operation_id"])

    before = counts(client)
    provider[0].extend([
        tool("get_template_config", {"template_id": records["template_id"]}, "template"),
        tool("browser_open", call_id="open"),
    ])
    if not stale:
        provider[0].extend([browser_open_response(worker_ids, "interaction", expires),
                            tool("execute_step", args, "login"), executed])
    provider[0].append(text("Inspect login evidence before proceeding"))
    title_provider[0].append(text('{"title":"Inspect saved login"}'))
    send(client, identifier, content="Use the template to inspect this saved site's login")
    detail = wait(client, identifier)
    logs = [item for item in detail["messages"] if item["role"] == "tool"]
    assert [item["tool_name"] for item in logs] == (
        ["get_template_config", "browser_open"] if stale else
        ["get_template_config", "browser_open", "execute_step"])
    assert [item["status"] for item in logs] == (["success", "error"] if stale else ["success"] * 3)
    result = json.loads(logs[-1]["content"])
    assert result["code"] == ("cookie_binding_changed" if stale else "step_succeeded")
    assert resolved == ([] if stale else [records["site_id"]])
    execute_requests = [request for request in provider[1] if str(request.url).endswith("/execute")]
    assert len(execute_requests) == (0 if stale else 1)
    if stale:
        assert detail["browser"] is None
    else:
        assert detail["browser"]["status"] == "ready"
    assert detail["title"] == "Inspect saved login"
    assert len(title_provider[1]) == 1
    assert counts(client) == before
    scope = manager.browsers.get(identifier)
    _, history = manager._history("admin", identifier)
    fresh = AssistantManager(manager.session_factory, manager.settings, manager.crypto)
    persisted = fresh.detail("admin", identifier)
    assert persisted["messages"] == detail["messages"]
    assert persisted["target"] == detail["target"]
    with manager.session_factory() as db:
        data = manager._decrypt(db.get(AssistantSession, identifier))
        assert data["target"]["_credential_binding"] == records["binding"]
        assert (operation_id in data.get("_execution_operations", [])) is not stale
        messages = [manager._decrypt(row) for row in db.scalars(select(AssistantMessage).where(
            AssistantMessage.session_id == identifier,
        ))]
    model_requests = [request for request in provider[1] if "chat/completions" in str(request.url)]
    for value in (USERNAME, PASSWORD, *((scope.token, scope.session_id) if scope else ()),
                  "_credential_binding", "_execution_operations", records["binding"]["history_key"]):
        assert value not in json.dumps([detail, history, messages])
        assert all(value not in request.content.decode() for request in model_requests + title_provider[1])
    if not stale:
        provider[0].append({"closed": True})
    assert client.post(f"{ROOT}/{identifier}/cancel").status_code == 200
    assert not provider[0]


def test_secret_in_later_call_rejects_whole_batch_before_reads_or_execution(
    assistant_client, provider, monkeypatch,
):
    client = assistant_client
    identifier = create(client)
    calls = [tool("list_templates", call_id="safe_read"), tool("execute_step", {
        "operation_id": str(uuid4()), "step": {
            "step_key": "wait", "feature_type": "action.wait", "config": {"nested": [KEY]},
        },
    }, "secret_execute")]
    output = calls[0]
    output["choices"][0]["message"].update(content="Never persist partial batch")
    secret_call = calls[1]["choices"][0]["message"]["tool_calls"][0]
    secret_call["function"]["arguments"] = secret_call["function"]["arguments"].replace(
        KEY, "".join(f"\\u{ord(character):04x}" for character in KEY),
    )
    output["choices"][0]["message"]["tool_calls"].append(secret_call)
    reads, execution = AsyncMock(), AsyncMock()
    monkeypatch.setattr(AssistantData, "dispatch", reads)
    monkeypatch.setattr(AssistantExecution, "execute", execution)
    provider[0].append(output)
    send(client, identifier)
    detail = wait(client, identifier)
    assert [item["role"] for item in detail["messages"]] == ["assistant", "user", "system"]
    assert "[secret_output]" in detail["messages"][-1]["content"]
    reads.assert_not_awaited()
    execution.assert_not_awaited()
    manager = client.app.state.assistant_manager
    with manager.session_factory() as db:
        for row in db.scalars(select(AssistantMessage).where(AssistantMessage.session_id == identifier)):
            payload = json.dumps(manager._decrypt(row))
            assert KEY not in payload and "Never persist partial batch" not in payload
