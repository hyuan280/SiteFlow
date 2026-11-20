from copy import deepcopy

import pytest

from app.models.assistant import AssistantSession
from app.models.execution import SiteJob
from app.models.plugin import PluginRegistration, utcnow
from app.services.assistant_feature_policy import set_assistant_allowed
from app.services.assistant_plugin_authorization import assistant_plugin_authorization
from app.services.secret_providers import SecretDenied
from tests.test_assistant_execution import execution, arguments, bind, response, run, save_target


def test_builtin_policy_revision_revokes_dispatch(execution):
    async def worker(path, payload):
        with execution.manager.session_factory() as db:
            set_assistant_allowed(db, "action.wait", False)
            db.commit()
        return response(execution.manager.browsers[execution.session_id], payload["operation_id"])

    execution.manager._worker.side_effect = worker
    assert run(execution)["code"] == "execution_feature_denied"


def test_direct_plugin_callback_and_revocation(execution, monkeypatch):
    import json
    from app.features.plugin_registry import refresh_plugin_registry, clear_plugin_registry
    from app.services.plugin_runtime import PluginRuntimeService

    key = "plugin.example.work"
    with execution.manager.session_factory() as db:
        db.add(PluginRegistration(plugin_id="example", name="Example", version="1", protocol_version=1,
            endpoint="example:7000", capabilities_json='["features"]', approved_at=utcnow(),
            approved_capabilities_json='["features"]', features_json=json.dumps([{
                "key": key, "name": "Work", "category": "action", "config_schema": {
                    "type": "object", "properties": {"count": {"type": "integer", "default": 2}}}}])))
        db.flush()
        set_assistant_allowed(db, key, True)
        db.commit()
        refresh_plugin_registry(db)
    monkeypatch.setattr(PluginRuntimeService, "_tokens", lambda self: {"example": "private-plugin-token"})
    save_target(execution, {**execution.target, "execution_features": [key]})
    access = {}

    async def worker(path, payload):
        runtime = payload["plugin_runtimes"]["example"]
        access.update(runtime["authorization"])
        access.update(plugin_id="example", generation=runtime["generation"], capability="features")
        access.pop("scope")
        assistant_plugin_authorization.authorize(**access)
        assert payload["step"]["config"] == {"count": 2}
        with execution.manager.session_factory() as db:
            db.get(PluginRegistration, "example").permission_epoch += 1
            db.commit()
        with pytest.raises(SecretDenied):
            assistant_plugin_authorization.authorize(**access)
        return response(execution.manager.browsers[execution.session_id], payload["operation_id"])

    execution.manager._worker.side_effect = worker
    try:
        assert run(execution, arguments(key))["code"] == "execution_authorization_changed"
        with pytest.raises(SecretDenied):
            assistant_plugin_authorization.authorize(**access)
        with execution.manager.session_factory() as db:
            data = execution.manager._decrypt(db.get(AssistantSession, execution.session_id))
            assert next(iter(data["_execution_records"].values()))["feature_bindings"][key]["plugin"]["generation"] == 1
    finally:
        clear_plugin_registry()


def test_post_runtime_is_real_job_and_operation_bound(execution):
    site_id = bind(execution)
    save_target(execution, {**execution.target, "workflow_site_id": site_id,
                           "_workflow_binding": execution.target["_credential_binding"]})
    with execution.manager.session_factory() as db:
        set_assistant_allowed(db, "post.resign_loop", True)
        db.commit()
    captured = {}

    async def worker(path, payload):
        captured.update(deepcopy(payload["job_runtime"]))
        with execution.manager.session_factory() as db:
            job, feature = assistant_plugin_authorization.authorize_resign(
                captured["secret_access"]["token"], int(captured["job_id"]), site_id, "execute", db)
            assert job.status == "running" and feature == "post.resign_loop"
        return response(execution.manager.browsers[execution.session_id], payload["operation_id"])

    execution.manager._worker.side_effect = worker
    assert run(execution, arguments("post.resign_loop"))["ok"]
    with execution.manager.session_factory() as db:
        assert db.get(SiteJob, int(captured["job_id"])).status == "success"
        with pytest.raises(SecretDenied):
            assistant_plugin_authorization.authorize_resign(
                captured["secret_access"]["token"], int(captured["job_id"]), site_id, "execute", db)


def test_flow_jump_is_resolved_dynamically(execution):
    result = run(execution, arguments("flow.jump", {
        "action": "restart", "texts": ["Complete"]}))
    assert result["ok"], result


def test_policy_toggle_back_invalidates_pinned_operation(execution):
    async def worker(path, payload):
        with execution.manager.session_factory() as db:
            set_assistant_allowed(db, "action.wait", False)
            db.commit()
            set_assistant_allowed(db, "action.wait", True)
            db.commit()
        return response(execution.manager.browsers[execution.session_id], payload["operation_id"])

    execution.manager._worker.side_effect = worker
    assert run(execution)["code"] == "execution_authorization_changed"


@pytest.mark.parametrize("category", ["action", "post"])
def test_real_sync_http_plugin_callback_and_policy_revocation(execution, monkeypatch, category):
    import json
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.v1.secrets import router
    from app.features.plugin_registry import refresh_plugin_registry, clear_plugin_registry
    from app.services.plugin_runtime import PluginRuntimeService

    key = "plugin.example.work"
    with execution.manager.session_factory() as db:
        db.add(PluginRegistration(plugin_id="example", name="Example", version="1",
            protocol_version=1, endpoint="example:7000", capabilities_json='["features"]',
            approved_at=utcnow(), approved_capabilities_json='["features"]',
            features_json=json.dumps([{"key": key, "name": "Work", "category": category,
                "config_schema": {"type": "object", "properties": {}}}])))
        db.flush()
        set_assistant_allowed(db, key, True)
        db.commit()
        refresh_plugin_registry(db)
    if category == "post":
        site_id = bind(execution)
        save_target(execution, {**execution.target, "workflow_site_id": site_id,
                               "_workflow_binding": execution.target["_credential_binding"]})
    save_target(execution, {**execution.target, "execution_features": [key]})
    monkeypatch.setattr(PluginRuntimeService, "_tokens", lambda self: {"example": "private-token"})
    app = FastAPI()
    app.include_router(router)
    captured = {}
    with TestClient(app) as client:
        def callback():
            return client.post("/api/v1/internal/assistant-plugins/authorize", **captured)

        async def worker(path, payload):
            runtime = payload["plugin_runtimes"]["example"]
            access = runtime["authorization"]
            captured.update(headers={"Authorization": "Bearer " + access["token"]}, json={
                "session_id": access["session_id"], "operation_id": access["operation_id"],
                "plugin_id": "example", "generation": runtime["generation"], "capability": "features"})
            assert callback().status_code == 200
            with execution.manager.session_factory() as db:
                set_assistant_allowed(db, key, False)
                db.commit()
            assert callback().status_code == 403
            return response(execution.manager.browsers[execution.session_id], payload["operation_id"])

        execution.manager._worker.side_effect = worker
        try:
            assert run(execution, arguments(key))["code"] == "execution_feature_denied"
            assert callback().status_code == 403
        finally:
            clear_plugin_registry()


@pytest.mark.parametrize("allowed", [False, True])
def test_plugin_captcha_direct_http_policy(execution, monkeypatch, allowed):
    import json
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.v1.secrets import router
    from app.features.plugin_registry import refresh_plugin_registry, clear_plugin_registry
    from app.services.plugin_runtime import PluginRuntimeService

    method = "plugin.example.ocr"
    with execution.manager.session_factory() as db:
        db.add(PluginRegistration(plugin_id="example", name="Example", version="1",
            protocol_version=1, endpoint="example:7000", capabilities_json='["captcha_methods"]',
            approved_at=utcnow(), approved_capabilities_json='["captcha_methods"]',
            captcha_methods_json=json.dumps([{"key": method, "label": "OCR"}])))
        db.flush()
        set_assistant_allowed(db, method, allowed)
        db.commit()
        refresh_plugin_registry(db)
    monkeypatch.setattr(PluginRuntimeService, "_tokens", lambda self: {"example": "private-token"})
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        async def worker(path, payload):
            runtime = payload["plugin_runtimes"]["example"]
            access = runtime["authorization"]
            headers = {"Authorization": "Bearer " + access["token"]}
            body = {"session_id": access["session_id"], "operation_id": access["operation_id"],
                    "plugin_id": "example", "generation": runtime["generation"],
                    "capability": "captcha_methods"}
            endpoint = "/api/v1/internal/assistant-plugins/authorize"
            assert client.post(endpoint, headers=headers, json=body).status_code == 200
            assert client.post(endpoint, headers=headers, json={**body, "capability": "features"}).status_code == 403
            with execution.manager.session_factory() as db:
                set_assistant_allowed(db, method, False)
                db.commit()
            assert client.post(endpoint, headers=headers, json=body).status_code == 403
            return response(execution.manager.browsers[execution.session_id], payload["operation_id"])

        execution.manager._worker.side_effect = worker
        try:
            result = run(execution, arguments("challenge.image_captcha", {"captcha_method": method}))
            assert result["code"] == "execution_feature_denied", result
            assert execution.manager._worker.await_count == int(allowed)
        finally:
            clear_plugin_registry()


def test_settings_change_revokes_pinned_step_without_exposing_values(execution):
    from app.models.setting import AppSetting

    async def worker(path, payload):
        with execution.manager.session_factory() as db:
            db.add(AppSetting(key="feature.action.wait", value_json='{"defaults":{"seconds":2},"required_fields":[]}'))
            db.commit()
        return response(execution.manager.browsers[execution.session_id], payload["operation_id"])

    execution.manager._worker.side_effect = worker
    assert run(execution)["code"] == "execution_authorization_changed"


def test_direct_post_real_sync_checkpoint_is_durable(execution):
    from datetime import datetime, timedelta, timezone
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.v1.secrets import router
    from app.models.execution import Run
    from app.models.workflow import Site, SiteResignRecord
    from app.services.executor import ExecutionManager

    site_id = bind(execution)
    save_target(execution, {**execution.target, "workflow_site_id": site_id,
                           "_workflow_binding": execution.target["_credential_binding"]})
    with execution.manager.session_factory() as db:
        site = db.get(Site, site_id)
        site.support_resign = True
        site.resign_max_days = 30
        history = Run(trigger="manual", status="fail", created_by=execution.owner)
        db.add(history)
        db.flush()
        db.add(SiteJob(run_id=history.id, site_id=site_id, site_name=site.name,
            lane="interaction", status="fail", finished_at=datetime.now(timezone.utc) - timedelta(days=3)))
        set_assistant_allowed(db, "post.resign_loop", True)
        db.commit()
    app = FastAPI()
    app.include_router(router)
    app.state.executor = ExecutionManager(execution.manager.session_factory,
        execution.manager.settings, execution.manager.crypto)
    captured = {}
    with TestClient(app) as client:
        async def worker(path, payload):
            runtime = payload["job_runtime"]
            day = runtime["resign_pending_dates"][0]
            captured.update(headers={"Authorization": "Bearer " + runtime["secret_access"]["token"]},
                json={"job_id": int(runtime["job_id"]), "site_id": site_id, "step_key": "execute",
                      "phase": "prepare", "date": day, "result": {"resign": {
                          "pending": runtime["resign_pending_dates"], "submit_attempted": True,
                          "unconfirmed": [{"date": day}]}}})
            result = client.post("/api/v1/internal/resign/checkpoint", **captured)
            assert result.status_code == 200, result.text
            with execution.manager.session_factory() as db:
                from sqlalchemy import select
                record = db.scalar(select(SiteResignRecord).where(SiteResignRecord.site_id == site_id))
                assert record.status == "unknown" and record.job_id == int(runtime["job_id"])
            return response(execution.manager.browsers[execution.session_id], payload["operation_id"])

        execution.manager._worker.side_effect = worker
        assert run(execution, arguments("post.resign_loop"))["ok"]
        assert client.post("/api/v1/internal/resign/checkpoint", **captured).status_code == 403
