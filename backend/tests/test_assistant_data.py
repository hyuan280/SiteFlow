from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from jsonschema import Draft202012Validator
import pytest
import httpx
from sqlalchemy import create_engine, event, insert, select, update
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.models.execution import JobArtifact, JobStep, Run, SiteJob
from app.models.integration import IntegrationConfig
from app.models.setting import AppSetting
from app.models.workflow import Site, SiteCredential, Template, WorkflowSignal, WorkflowStep
from app.schemas.integration import BitwardenView, TokenView, ValueView
from app.services.assistant_data import AssistantData, OUTPUT_LIMIT, redact_config
from app.services.bitwarden import BitwardenService
from app.services.integrations import IntegrationService
from app.services.lanes import LaneService


PRIVATE = "DO-NOT-EXPOSE-PRIVATE"
URL = f"https://user:{PRIVATE}@example.com/start?session={PRIVATE}#{PRIVATE}"


def call(tools, name, args=None):
    return asyncio.run(tools.dispatch(name, {} if args is None else args))


@pytest.fixture
def tools(tmp_path, settings, monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, autoflush=False)
    settings = settings.model_copy(update={
        "config_ai_enabled": True, "vision_base_url": f"https://{PRIVATE}.invalid",
        "vision_model": "vendor/safe-model", "vision_max_tokens": 1234,
        "vision_supports_vision": False,
    })
    manager = SimpleNamespace(session_factory=factory, settings=settings, crypto=Mock())
    manager.crypto.decrypt.side_effect = AssertionError("Must not decrypt anything")
    monkeypatch.setattr(LaneService, "get_lanes", AsyncMock(return_value=SimpleNamespace(
        worker_connected=True, lanes=[SimpleNamespace(
            key="general", enabled=True, status="running", in_use=1, max_concurrency=2,
            error=PRIVATE, profile=PRIVATE, display=PRIVATE,
        ), SimpleNamespace(key=PRIVATE)],
    )))
    with engine.begin() as db:
        db.execute(insert(Template).values(id=1, name="Parent"))
        db.execute(insert(Template).values(id=2, name="Child", parent_id=1, description=PRIVATE))
        db.execute(insert(Site).values(
            id=1, name="First", base_url=URL, template_id=2, notes=PRIVATE,
            bitwarden_enabled=True, credential_provider_id="builtin.bitwarden",
            bitwarden_custom_match=PRIVATE, trusted_urls_json=json.dumps([PRIVATE]),
        ))
        db.execute(insert(Site).values(id=2, name="Second", base_url="https://second.example"))
        db.execute(insert(SiteCredential).values(
            site_id=1, username_ciphertext=PRIVATE.encode(), username_nonce=b"nonce",
            password_ciphertext=PRIVATE.encode(), password_nonce=b"nonce",
            cookies_ciphertext=PRIVATE.encode(), cookies_nonce=b"nonce", last_error=PRIVATE,
        ))
        db.execute(insert(WorkflowStep).values(
            owner_type="template", owner_id=1, step_key="wait", feature_type="action.wait",
            category="action", config_json='{"seconds": 1}',
        ))
        db.execute(insert(WorkflowStep).values(
            owner_type="site", owner_id=1, step_key="fill", feature_type="action.fill",
            category="action", config_json=json.dumps({"selector": ["#field"], "value": PRIVATE}),
        ))
        db.execute(insert(WorkflowSignal).values(
            owner_type="template", owner_id=1, signal_key="ready", label="Ready",
            description=PRIVATE, include_texts_json='["ready"]',
        ))
        for key in ("vision.api_key", "vision.model", "proxy.url", "twofauth.url",
                    "twofauth.token", "cfbox.url", "cfbox.api_key", "bitwarden.token"):
            db.execute(insert(IntegrationConfig).values(
                key=key, value_ciphertext=PRIVATE.encode(), nonce=b"nonce", fingerprint=PRIVATE,
            ))
        db.execute(insert(Run).values(id=1, created_by=PRIVATE, summary_json=json.dumps({"raw": PRIVATE}),
                                      requested_sites_json=json.dumps([PRIVATE])))
        db.execute(insert(Run).values(id=2, created_by=PRIVATE, status="completed"))
        db.execute(insert(SiteJob).values(
            id=1, run_id=1, site_id=1, site_name="First", lane="general", status="failed",
            message=PRIVATE, result_json=json.dumps({"raw": PRIVATE}), worker_id=PRIVATE,
        ))
        db.execute(insert(SiteJob).values(
            id=2, run_id=2, site_id=2, site_name="Second", lane="general", status="success",
        ))
        db.execute(insert(JobStep).values(
            site_job_id=1, step_key="fill", feature_type="action.fill", status="failed",
            message=PRIVATE, data_json=json.dumps({"raw": PRIVATE}),
        ))
        db.execute(insert(JobArtifact).values(
            run_id=1, site_job_id=1, kind="screenshot", relative_path=PRIVATE,
            content_type="image/png",
        ))
    monkeypatch.setattr(socket, "getaddrinfo", Mock(side_effect=AssertionError("No DNS")))
    monkeypatch.setattr(httpx.Client, "send", Mock(side_effect=AssertionError("No HTTP")))
    monkeypatch.setattr(httpx.AsyncClient, "send", AsyncMock(side_effect=AssertionError("No HTTP")))
    monkeypatch.setattr(BitwardenService, "view", Mock(return_value=BitwardenView(
        enabled=True, enabled_setting=True, enabled_source="auto", available=True,
        configured=True, auto_detected=True, source="db",
        url=ValueView(value=URL, source="auto", editable=True),
        token=TokenView(configured=True, source="db", editable=True, masked=PRIVATE),
        default_url=URL, server_version=PRIVATE, user_email=PRIVATE, error=PRIVATE,
    )))
    monkeypatch.setattr(IntegrationService, "get_integrations", Mock(side_effect=AssertionError("No decryption")))
    tool = AssistantData(manager)
    tool.engine = engine
    yield tool
    engine.dispose()


def test_tool_schemas_are_exact_bounded_and_title_free():
    definitions = AssistantData.tool_definitions()
    assert [item["function"]["name"] for item in definitions] == [
        "list_sites", "get_site_config", "list_templates", "get_template_config",
        "get_system_status", "list_recent_runs", "get_run_summary",
    ]
    expected = {
        "list_sites": ({"query", "limit"}, []), "get_site_config": ({"site_id"}, ["site_id"]),
        "list_templates": (set(), []), "get_template_config": ({"template_id"}, ["template_id"]),
        "get_system_status": (set(), []), "list_recent_runs": ({"site_id", "limit"}, []),
        "get_run_summary": ({"run_id"}, ["run_id"]),
    }
    for item in definitions:
        assert set(item) == {"type", "function"}
        assert item["type"] == "function"
        function = item["function"]
        assert set(function) == {"name", "description", "parameters"}
        schema = function["parameters"]
        Draft202012Validator.check_schema(schema)
        properties, required = expected[function["name"]]
        assert set(schema["properties"]) == properties
        assert schema["required"] == required
        assert schema["additionalProperties"] is False
    assert definitions[0]["function"]["parameters"]["properties"]["limit"]["maximum"] == 100
    assert definitions[5]["function"]["parameters"]["properties"]["limit"]["maximum"] == 20
    baseline = deepcopy(definitions)
    definitions[0]["function"]["parameters"].clear()
    assert AssistantData.tool_definitions() == baseline


@pytest.mark.parametrize("name,args", [
    ("get_site_config", {}), ("get_site_config", {"site_id": True}),
    ("get_site_config", {"site_id": "1"}), ("get_site_config", {"site_id": 1.0}),
    ("get_site_config", {"site_id": 0}), ("get_site_config", {"site_id": 2**64}),
    ("get_site_config", {"site_id": None}), ("get_site_config", {"site_id": 1, "sql": PRIVATE}),
    ("get_template_config", {"template_id": -1}), ("get_run_summary", {"run_id": False}),
    ("list_sites", {"query": PRIVATE * 20}), ("list_sites", {"query": None}),
    ("list_sites", {"limit": 101}), ("list_sites", {"limit": 0}),
    ("list_recent_runs", {"limit": 21}), ("list_recent_runs", {"limit": True}),
    ("list_recent_runs", {"site_id": None}), ("list_templates", {"limit": 10}),
    ("get_system_status", {"endpoint": PRIVATE}), ("list_sites", []),
])
def test_invalid_arguments_never_query_database(tools, name, args):
    tools.manager.session_factory = Mock(side_effect=AssertionError("No reads"))
    assert asyncio.run(tools.dispatch(name, args)) == {"ok": False, "code": "invalid_arguments"}
    tools.manager.session_factory.assert_not_called()


def test_unknown_tools_have_no_sql_or_shell_authority(tools):
    tools.manager.session_factory = Mock(side_effect=AssertionError("No reads"))
    for name in ("execute_sql", "shell", PRIVATE):
        assert call(tools, name, {"query": PRIVATE}) == {"ok": False, "code": "unknown_tool"}
    tools.manager.session_factory.assert_not_called()


def test_lists_are_sanitized_filtered_and_bounded(tools):
    result = call(tools, "list_sites", {"limit": 1})
    assert result["ok"] and result["truncated"]
    assert result["items"][0]["base_url"] == "https://example.com/start"
    assert [item["id"] for item in call(tools, "list_sites", {"query": "seCONd"})["items"]] == [2]
    assert not call(tools, "list_sites", {"query": "%"})["items"]
    assert not call(tools, "list_sites", {"query": PRIVATE})["items"]
    templates = call(tools, "list_templates")
    assert templates["ok"] and len(templates["items"]) == 2
    assert PRIVATE not in json.dumps([result, templates])


def test_list_caps_and_run_job_summary_truncation(tools):
    with tools.engine.begin() as db:
        db.execute(insert(Site), [{"name": f"Extra-{index}", "base_url": "https://example.com"}
                                  for index in range(101)])
        db.execute(insert(Template), [{"name": f"Extra-{index}"} for index in range(101)])
        db.execute(insert(Run), [{"created_by": "admin"} for _ in range(21)])
        db.execute(insert(SiteJob), [{"run_id": 1, "site_id": 1, "site_name": "First",
                                     "lane": "general", "status": "queued"} for _ in range(101)])
    for name in ("list_sites", "list_templates"):
        result = call(tools, name)
        assert result["ok"] and len(result["items"]) == 100 and result["truncated"]
    assert len(call(tools, "list_recent_runs")["items"]) == 20
    summary = call(tools, "get_run_summary", {"run_id": 1})
    assert len(summary["jobs"]) == 100 and summary["jobs_truncated"]
    assert summary["run"]["job_count"] == 102
    assert summary["run"]["job_counts"] == {"failed": 1, "queued": 101}


@pytest.mark.parametrize("model", [URL, PRIVATE + " with spaces", "model?key=" + PRIVATE])
def test_unsafe_model_ids_are_omitted(tools, model):
    tools.manager.settings = tools.manager.settings.model_copy(update={"vision_model": model})
    result = call(tools, "get_system_status")
    assert result["ok"] and result["model"] is None
    assert PRIVATE not in json.dumps(result)


def test_current_local_resolved_workflows_and_signals_without_credentials(tools):
    site = call(tools, "get_site_config", {"site_id": 1})
    assert site["ok"], site
    assert site["site"]["id"] == 1
    assert site["site"]["credentials_configured"] is True
    assert site["site"]["base_url"] == "https://example.com/start"
    assert "credential" not in site["site"]
    assert site["site"]["credential_provider_id"] == "builtin.bitwarden"
    assert site["local"]["steps"][0]["config"] == {"selector": ["#field"]}
    assert {step["step_key"] for step in site["resolved"]["steps"]} == {"wait", "fill"}
    assert site["resolution_error"] is None
    assert {signal["signal_key"] for signal in site["resolved_signals"]} == {"ready", "sys_resign_pending"}
    template = call(tools, "get_template_config", {"template_id": 2})
    assert template["ok"] and template["local"]["steps"] == []
    assert template["resolved"]["steps"][0]["config"]["seconds"] == 1
    assert PRIVATE not in json.dumps([site, template])
    tools.manager.crypto.decrypt.assert_not_called()
    with tools.engine.begin() as db:
        db.execute(update(WorkflowStep).where(WorkflowStep.step_key == "wait")
                   .values(config_json='{"seconds": 2}'))
    refreshed = call(tools, "get_site_config", {"site_id": 1})
    assert next(step for step in refreshed["resolved"]["steps"] if step["step_key"] == "wait")["config"]["seconds"] == 2


def test_redacts_nested_configs_fill_static_fields_and_embedded_urls():
    raw = {"selector": "#field", "value": PRIVATE, "FiElDs": {"harmless": PRIVATE},
           "nested": [{"PaSsWoRd": PRIVATE, "USER_name": PRIVATE, "Api-Key": PRIVATE,
                       "clientSecret": PRIVATE, "requestHeaders": {"Authorization": PRIVATE},
                       "raw_credentials": PRIVATE, "SESSION_TOKEN": PRIVATE,
                       "cookieJar": PRIVATE, "llm_prompt": PRIVATE, "inlineFields": PRIVATE,
                       "static_fields": {"x": PRIVATE}, "url": URL,
                       "text": f"visit {URL} then https://safe.example/path?q={PRIVATE}"}],
           "relative": f"/path?token={PRIVATE}#{PRIVATE}",
           "protocol_relative": f"//user:{PRIVATE}@example.com/path?x={PRIVATE}"}
    before = deepcopy(raw)
    result = redact_config(raw)
    assert raw == before
    assert PRIVATE not in json.dumps(result)
    assert result["nested"][0] == {"url": "https://example.com/start",
                                   "text": "visit https://example.com/start then https://safe.example/path"}
    assert result["relative"] == "/path"
    assert result["protocol_relative"] == "//example.com/path"


@pytest.mark.parametrize("prefix", ["x" * OUTPUT_LIMIT, "x+.-" * (OUTPUT_LIMIT // 4)],
                         ids=["letters", "scheme-characters"])
def test_redactor_preserves_long_non_url_runs_and_scrubs_following_urls(prefix):
    raw = {"selector": prefix, f"{prefix} {URL}": f"{prefix} {URL}"}
    expected_text = f"{prefix} https://example.com/start"
    result = redact_config(raw)
    assert result == {"selector": prefix, expected_text: expected_text}
    assert PRIVATE not in json.dumps(result)


def test_redactor_retains_only_typed_metadata_and_valid_provider_ids():
    metadata = {
        "USERNAME_SELECTOR": ["input[name='username']"],
        "password_selector": ["#password"],
        "token_selector": "input[name='token']",
        "username_field": "login", "PASSWORD_FIELD": "passwd",
        "credential_provider_id": "builtin.manual", "totp_provider_id": "builtin.twofauth",
    }
    raw = {**metadata, "username": PRIVATE, "password": PRIVATE, "token": PRIVATE,
           "llm_prompt": PRIVATE, "nested": {**metadata, "api_key": PRIVATE},
           "secret_selector": {"password": PRIVATE},
           "credential_selectors": [{"value": PRIVATE}], "username_field_bad": PRIVATE}
    before = deepcopy(raw)
    result = redact_config(raw)
    assert result == {**metadata, "nested": metadata}
    assert raw == before
    assert PRIVATE not in json.dumps(result)
    for key in ("credential_provider_id", "totp_provider_id"):
        assert redact_config({key: "plugin.vault-provider"}) == {key: "plugin.vault-provider"}
        for value in (PRIVATE, URL, {"password": PRIVATE}, [PRIVATE], "builtin.unknown"):
            assert redact_config({key: value}) == {}


@pytest.mark.parametrize("key", [
    "secret_selector", "APIkey_selector", "password_fake_selector",
    "username_fake_selector", "token_fake_selectors", "credential_selectors",
    "password_selectors", "token_selectors",
])
@pytest.mark.parametrize("value", [PRIVATE, [PRIVATE]])
def test_unknown_sensitive_selector_names_cannot_bypass_redaction(key, value):
    assert redact_config({key: value, "nested": {key: value}}) == {"nested": {}}


def test_template_auth_selectors_and_field_names_survive_local_and_resolved_reads(tools):
    auth_config = {
        "username_selector": ["input[name='username']"],
        "password_selector": ["input[type='password']"],
        "username_field": "login_name", "password_field": "login_password",
    }
    token_config = {
        "token_selector": ["input[name='altchaPayload']", f'a[href="{URL}"]'],
    }
    with tools.engine.begin() as db:
        for step_key, feature_type, category, config in (
            ("login", "auth.form_login", "auth", auth_config),
            ("challenge", "challenge.altcha", "challenge", token_config),
        ):
            db.execute(insert(WorkflowStep).values(
                owner_type="template", owner_id=1, step_key=step_key,
                feature_type=feature_type, category=category, config_json=json.dumps(config),
            ))
    for template_id in (1, 2):
        result = call(tools, "get_template_config", {"template_id": template_id})
        assert result["ok"] and result["resolution_error"] is None
        workflows = [result["resolved"]]
        if template_id == 1:
            workflows.append(result["local"])
        for workflow in workflows:
            configs = {step["step_key"]: step["config"] for step in workflow["steps"]}
            for key, value in auth_config.items():
                assert configs["login"][key] == value
            assert configs["challenge"]["token_selector"] == [
                "input[name='altchaPayload']", 'a[href="https://example.com/start"]',
            ]
        assert PRIVATE not in json.dumps(result)
    with tools.engine.begin() as db:
        db.execute(update(WorkflowStep).where(WorkflowStep.step_key == "login").values(
            config_json=json.dumps({**auth_config, "username": PRIVATE, "password": PRIVATE,
                                    "secret_selector": PRIVATE, "password_fake_selector": [PRIVATE]}),
        ))
        db.execute(update(WorkflowStep).where(WorkflowStep.step_key == "challenge").values(
            config_json=json.dumps({**token_config, "token": PRIVATE, "APIkey_selector": PRIVATE}),
        ))
    result = call(tools, "get_template_config", {"template_id": 1})
    assert result["ok"]
    assert PRIVATE not in json.dumps(result)
    configs = {step["step_key"]: step["config"] for step in result["local"]["steps"]}
    assert configs["login"] == auth_config
    assert "token" not in configs["challenge"]


def test_nested_secrets_in_saved_configs_and_resolved_defaults(tools):
    with tools.engine.begin() as db:
        db.execute(update(WorkflowStep).where(WorkflowStep.step_key == "fill").values(config_json=json.dumps({
            "selector": ["#input"], "value": PRIVATE, "nested": {"password": PRIVATE, "api_key": PRIVATE,
            "more": [{"Authorization": PRIVATE, "llm_prompt": PRIVATE, "url": URL}]},
            "fields": {"harmless": PRIVATE},
        })))
        db.execute(insert(AppSetting).values(key="feature.action.fill", value_json=json.dumps({
            "defaults": {"value": PRIVATE}, "required_fields": [],
        })))
    result = call(tools, "get_site_config", {"site_id": 1})
    assert result["ok"]
    assert PRIVATE not in json.dumps(result)
    assert result["local"]["steps"][0]["config"]["nested"]["more"] == [{"url": "https://example.com/start"}]
    with tools.engine.begin() as db:
        db.execute(update(WorkflowStep).where(WorkflowStep.step_key == "fill")
                   .values(config_json='{"selector": ["#input"]}'))
    result = call(tools, "get_site_config", {"site_id": 1})
    assert result["resolved"] is not None
    assert "value" not in result["resolved"]["steps"][1]["config"]
    assert PRIVATE not in json.dumps(result)


def test_run_projection_excludes_raw_jobs_messages_results_and_artifacts(tools):
    recent = call(tools, "list_recent_runs", {"site_id": 1, "limit": 20})
    assert recent["ok"] and [run["id"] for run in recent["items"]] == [1]
    assert len(call(tools, "list_recent_runs", {"limit": 1})["items"]) == 1
    summary = call(tools, "get_run_summary", {"run_id": 1})
    assert summary["ok"]
    assert summary["run"]["job_counts"] == {"failed": 1}
    assert summary["run"]["job_count"] == 1
    assert summary["jobs"][0]["site_name"] == "First"
    assert summary["jobs"][0]["status"] == "failed"
    assert not summary["jobs_truncated"]
    assert PRIVATE not in json.dumps([summary, recent])
    assert set(summary["jobs"][0]) == {
        "id", "run_id", "site_id", "site_name", "lane", "attempt", "status", "started_at", "finished_at",
    }


def test_status_projects_bitwarden_health_without_secrets_or_endpoints(tools):
    result = call(tools, "get_system_status")
    assert result["ok"] and result["config_ai_enabled"]
    assert result["model"] == "vendor/safe-model"
    assert result["max_tokens"] == 1234
    assert result["supports_vision"] is False
    integrations = {item["name"]: item for item in result["integrations"]}
    assert set(integrations) == {"proxy", "twofauth", "cfbox", "bitwarden", "vision"}
    assert integrations["vision"]["configured"]
    assert integrations["vision"]["sources"]["api_key"] == "db"
    assert integrations["vision"]["sources"]["model"] == "env"
    assert integrations["bitwarden"]["available"] is True
    assert integrations["bitwarden"]["health_checked"] is True
    assert integrations["bitwarden"]["sources"] == {
        "url": "auto", "token": "db", "enabled": "auto",
    }
    assert all(item["available"] is None and item["health_checked"] is False
               for name, item in integrations.items() if name != "bitwarden")
    assert result["health_checked"] is False
    assert result["health_scope"] == ["bitwarden"]
    BitwardenService.view.assert_called_once_with(force=False, deep=False)
    assert result["worker_connected"]
    assert result["lanes"] == [{"key": "general", "enabled": True, "status": "running",
                                "in_use": 1, "max_concurrency": 2}]
    assert PRIVATE not in json.dumps(result)
    assert "://" not in json.dumps(result)
    tools.manager.crypto.decrypt.assert_not_called()


@pytest.mark.parametrize("name,args", [
    ("list_sites", {}), ("get_site_config", {"site_id": 1}), ("list_templates", {}),
    ("get_template_config", {"template_id": 2}), ("list_recent_runs", {}),
    ("get_run_summary", {"run_id": 1}),
])
def test_other_data_tools_never_consult_bitwarden_or_decrypt(tools, name, args):
    assert call(tools, name, args)["ok"]
    BitwardenService.view.assert_not_called()
    tools.manager.crypto.decrypt.assert_not_called()


def test_status_db_public_values_unknown_without_decryption_and_worker_failure(tools, monkeypatch):
    tools.manager.settings = tools.manager.settings.model_copy(update={
        "vision_model": None, "vision_max_tokens": None, "vision_supports_vision": None,
    })
    monkeypatch.setattr(LaneService, "get_lanes", AsyncMock(side_effect=RuntimeError(PRIVATE)))
    result = call(tools, "get_system_status")
    assert result["ok"] and result["model"] is None and result["max_tokens"] is None
    assert result["supports_vision"] is None
    assert not result["worker_connected"] and result["lanes"] == []
    assert PRIVATE not in json.dumps(result)
    tools.manager.crypto.decrypt.assert_not_called()


@pytest.mark.parametrize("name,field", [
    ("get_site_config", "site_id"), ("get_template_config", "template_id"), ("get_run_summary", "run_id"),
])
def test_not_found_is_static(tools, name, field):
    assert call(tools, name, {field: 99999}) == {"ok": False, "code": "not_found"}


def test_errors_and_invalid_workflows_never_echo_raw_exception(tools):
    with tools.engine.begin() as db:
        db.execute(update(WorkflowStep).where(WorkflowStep.step_key == "fill")
                   .values(feature_type=PRIVATE))
    result = call(tools, "get_site_config", {"site_id": 1})
    assert result["ok"] and result["resolution_error"] == "workflow_invalid"
    assert result["resolved"] is None
    # Feature type is legitimate config data, not an exception string.
    tools.manager.session_factory = Mock(side_effect=RuntimeError(PRIVATE))
    assert call(tools, "list_sites") == {"ok": False, "code": "data_unavailable"}


def test_reads_only_no_commits_and_no_secret_column_reads(tools, monkeypatch):
    statements = []

    def check_sql(conn, cursor, statement, parameters, context, executemany):
        assert not (context.isinsert or context.isupdate or context.isdelete)
        assert statement.lstrip().upper().startswith("SELECT")
        statements.append(statement.lower())

    with tools.engine.connect() as db:
        before = {table.name: db.execute(select(table)).all() for table in Base.metadata.sorted_tables}
    monkeypatch.setattr(tools.manager.session_factory.class_, "commit", Mock(side_effect=AssertionError("No commit")))
    event.listen(tools.engine, "before_cursor_execute", check_sql)
    try:
        for name, args in [
            ("list_sites", {}), ("get_site_config", {"site_id": 1}), ("list_templates", {}),
            ("get_template_config", {"template_id": 2}), ("get_system_status", {}),
            ("list_recent_runs", {}), ("get_run_summary", {"run_id": 1}),
        ]:
            assert call(tools, name, args)["ok"], name
    finally:
        event.remove(tools.engine, "before_cursor_execute", check_sql)
    sql = "\n".join(statements)
    for forbidden in ("ciphertext", "nonce", "fingerprint", "workflow_history", "job_artifacts",
                      "job_steps", "result_json", "summary_json", "created_by", "site_jobs.message"):
        assert forbidden not in sql
    with tools.engine.connect() as db:
        assert {table.name: db.execute(select(table)).all() for table in Base.metadata.sorted_tables} == before


def test_outputs_are_json_safe_and_bounded(tools):
    with tools.engine.begin() as db:
        db.execute(update(WorkflowStep).where(WorkflowStep.step_key == "fill")
                   .values(config_json=json.dumps({"selector": "x" * OUTPUT_LIMIT})))
    result = call(tools, "get_site_config", {"site_id": 1})
    assert result == {"ok": False, "code": "output_limit_exceeded"}
    assert len(json.dumps(result).encode()) < OUTPUT_LIMIT
    assert redact_config({"date": datetime(2026, 1, 1, tzinfo=timezone.utc)}) == {
        "date": "2026-01-01T00:00:00+00:00",
    }
