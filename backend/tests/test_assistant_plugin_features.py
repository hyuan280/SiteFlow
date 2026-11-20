import json
import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.features.plugin_registry import refresh_plugin_registry
from app.models.execution import SiteJob
from app.models.plugin import PluginRegistration, utcnow
from app.models.setting import AppSetting
from app.models.workflow import Site
from app.services.assistant_categories import Denied, builtin_category, execution_category
from app.services.assistant_run_guard import validate_guard
from app.services.config_ai_tools import ConfigAITools
from app.services.workflows import WorkflowService
from app.schemas.config_ai import GenerationSite
from tests.test_assistant_workflows import context, configure, call, import_draft  # noqa: F401


KEY = "plugin.example.work"


@pytest.fixture
def approved(context):
    manager, _ = context
    with manager.session_factory() as db:
        db.add(PluginRegistration(
            plugin_id="example", name="Example", version="1", protocol_version=1,
            endpoint="example:7000", capabilities_json='["features"]',
            approved_capabilities_json='["features"]', approved_at=utcnow(),
            features_json=json.dumps([{
                "key": KEY, "name": "Work", "category": "action",
                "config_schema": {"type": "object", "properties": {
                    "selector": {"type": "string"},
                    "password": {"type": "string", "writeOnly": True},
                }},
            }]),
        ))
        db.flush()
        from app.services.assistant_feature_policy import set_assistant_allowed
        set_assistant_allowed(db, KEY, True)
        db.commit()
        refresh_plugin_registry(db)
    yield manager
    from app.features.plugin_registry import clear_plugin_registry
    clear_plugin_registry()


def draft():
    return {"steps": [{"step_key": "work", "feature_type": KEY,
                       "config": {"selector": "#work"}}], "groups": [], "signals": []}


def test_discover_configure_import_run_and_revoke(context, approved):
    tools = configure(context)
    features = call(tools, "list_features")["features"]
    assert KEY in {item["feature_type"] for item in features}
    schema = call(tools, "get_feature_schema", {"feature_type": KEY})
    assert schema["category"] == "action"
    assert "password" not in schema["config_schema"]["properties"]
    imported, _ = import_draft(tools, draft())
    result = call(tools, "run_site")
    assert result["ok"], result
    with approved.session_factory() as db:
        site = db.get(Site, imported["site_id"])
        assert validate_guard(db, result["run_id"], site, approved.settings, approved.crypto)
        # No registry refresh: current DB approval must still win over cached metadata.
        db.get(PluginRegistration, "example").approved_at = None
        db.commit()
        assert not validate_guard(db, result["run_id"], site, approved.settings, approved.crypto)
    assert KEY not in {item["feature_type"] for item in call(tools, "list_features")["features"]}
    assert not call(tools, "get_feature_schema", {"feature_type": KEY})["ok"]


@pytest.mark.parametrize("category", ["post", "auth"])
def test_effective_category_not_declared_or_model(context, approved, category):
    from app.services.plugin_feature_policy import category_setting_key

    with approved.session_factory() as db:
        db.add(AppSetting(key=category_setting_key("example"), value_json=json.dumps({KEY: category})))
        db.commit()
        refresh_plugin_registry(db)
        assert execution_category(db, KEY) == category
        with pytest.raises(Denied):
            builtin_category(KEY)
    tools = configure(context)
    assert call(tools, "get_feature_schema", {"feature_type": KEY})["category"] == category
    import_draft(tools, draft())
    result = call(tools, "run_site")
    if category == "post":
        assert result["ok"], result
    else:
        assert result["code"] == "credential_mode_incompatible", result
        approved.executor.submit.assert_not_called()


@pytest.mark.parametrize("field", ["generation", "permission_epoch"])
def test_guard_binds_plugin_generation_and_epoch(context, approved, field):
    tools = configure(context)
    imported, _ = import_draft(tools, draft())
    result = call(tools, "run_site")
    assert result["ok"]
    with approved.session_factory() as db:
        row = db.get(PluginRegistration, "example")
        setattr(row, field, getattr(row, field) + 1)
        db.commit()
        refresh_plugin_registry(db)
        assert not validate_guard(db, result["run_id"], db.get(Site, imported["site_id"]),
                                  approved.settings, approved.crypto)


def test_plugin_single_step_requires_browser(context, approved):
    tools = configure(context)
    result = call(tools, "execute_step", {"operation_id": str(uuid4()), "step": draft()["steps"][0]})
    assert result["code"] == "browser_reopen_required"


def test_unapproved_plugin_is_not_discoverable_even_with_cached_registry(context, approved):
    with approved.session_factory() as db:
        db.get(PluginRegistration, "example").approved_capabilities_json = "[]"
        db.commit()
        with pytest.raises(Denied):
            execution_category(db, KEY)
    tools = configure(context)
    assert KEY not in {item["feature_type"] for item in call(tools, "list_features")["features"]}
    assert not call(tools, "get_feature_schema", {"feature_type": KEY})["ok"]


def test_request_scoped_authoring_tools_recheck_revocation(context, approved):
    with approved.session_factory() as db:
        tools = ConfigAITools(WorkflowService(db, approved.settings, approved.crypto),
                              GenerationSite(name="Example", base_url="https://example.com"),
                              [KEY], include_plugins=True)
        assert tools.dispatch("get_feature_schema", {"feature_type": KEY})["ok"]
        db.get(PluginRegistration, "example").enabled = False
        db.commit()
        assert not tools.dispatch("get_feature_schema", {"feature_type": KEY})["ok"]


def test_executor_uses_job_scoped_plugin_chain_and_auth_budget(context, approved, monkeypatch):
    from sqlalchemy import select
    from app.models.assistant import AssistantSession
    from app.services.executor import ExecutionManager
    from app.services.plugin_feature_policy import category_setting_key
    from app.services.plugin_runtime import PluginRuntimeService
    from app.services.secret_providers import SecretDenied

    with approved.session_factory() as db:
        db.add(AppSetting(key=category_setting_key("example"), value_json=json.dumps({KEY: "auth"})))
        db.commit()
        refresh_plugin_registry(db)
    tools = configure(context, credential_mode="bitwarden")
    imported, _ = import_draft(tools, draft())
    result = call(tools, "run_site")
    assert result["ok"], result
    executor = ExecutionManager(approved.session_factory, approved.settings, approved.crypto)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.v1.secrets import router

    app = FastAPI()
    app.include_router(router)
    app.state.executor = executor
    client = TestClient(app)
    monkeypatch.setattr(PluginRuntimeService, "_tokens", lambda self: {"example": "private-token"})
    with approved.session_factory() as db:
        job_id = db.scalar(select(SiteJob.id).where(SiteJob.run_id == result["run_id"]))

    async def worker(payload):
        runtime = payload["plugin_runtimes"]["example"]
        assert payload["assistant_run"] is True
        assert payload["steps"][0]["feature_type"] == KEY
        with approved.session_factory() as db:
            data = approved._decrypt(db.get(AssistantSession, tools.session_id))
            assert len(data["_execution_records"]) == 1
            assert next(iter(data["_execution_records"].values()))["category"] == "auth"
        access = runtime["authorization"]
        request = {"headers": {"Authorization": "Bearer " + access["token"]}, "json": {
            "job_id": job_id, "plugin_id": "example", "generation": runtime["generation"],
            "capability": "features"}}
        assert client.post("/api/v1/internal/plugins/authorize", **request).status_code == 200
        with approved.session_factory() as db:
            db.get(PluginRegistration, "example").permission_epoch += 1
            db.commit()
        with pytest.raises(SecretDenied):
            executor.secret_providers.authorize_plugin(access["token"], job_id, "example",
                                                        runtime["generation"], "features")
        assert client.post("/api/v1/internal/plugins/authorize", **request).status_code == 403
        return {"status": "success", "steps": []}

    executor._post_execute = AsyncMock(side_effect=worker)
    asyncio.run(executor._execute_job(result["run_id"], job_id))
    executor._post_execute.assert_awaited_once()


def test_workflow_plugin_captcha_policy_and_live_guard(context, approved):
    from app.services.assistant_feature_policy import set_assistant_allowed

    method = "plugin.example.ocr"
    with approved.session_factory() as db:
        row = db.get(PluginRegistration, "example")
        row.capabilities_json = row.approved_capabilities_json = '["features","captcha_methods"]'
        row.captcha_methods_json = json.dumps([{"key": method, "label": "OCR"}])
        db.flush()
        set_assistant_allowed(db, method, True)
        db.commit()
        refresh_plugin_registry(db)
    tools = configure(context)
    imported, _ = import_draft(tools, {"steps": [{"step_key": "captcha",
        "feature_type": "challenge.image_captcha", "config": {"captcha_method": method}}],
        "groups": [], "signals": []})
    result = call(tools, "run_site")
    assert result["ok"], result
    with approved.session_factory() as db:
        site = db.get(Site, imported["site_id"])
        assert validate_guard(db, result["run_id"], site, approved.settings, approved.crypto)
        set_assistant_allowed(db, method, False)
        db.commit()
        assert not validate_guard(db, result["run_id"], site, approved.settings, approved.crypto)


def test_stale_category_setting_cannot_preserve_authority(context, approved):
    from app.services.plugin_feature_policy import category_setting_key

    with approved.session_factory() as db:
        db.add(AppSetting(key=category_setting_key("example"), value_json="{}"))
        db.commit()
        cached = db.get(AppSetting, category_setting_key("example"))
        assert execution_category(db, KEY) == "action"
        with approved.session_factory() as other:
            other.get(AppSetting, cached.key).value_json = json.dumps({KEY: "post"})
            other.commit()
        with pytest.raises(Denied):
            execution_category(db, KEY)
