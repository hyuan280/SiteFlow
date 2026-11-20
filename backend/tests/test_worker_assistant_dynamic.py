from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from tests.test_assistant_browser import Manager, TOKEN, URL, public_dns  # noqa: F401
from tests.test_assistant_private_steps import request
from worker import executor, plugin_authorization, plugin_client, resign_checkpoint, resign_dialog
from worker.assistant_browser import AssistantBrowser
from worker.assistant_runtime import JobRuntime
from worker.plugin_broker import BrokerDenied, PluginBroker
from worker.assistant_step import validate_dynamic_config


def job_runtime():
    return {"job_id": "real-host-job", "site_id": "42",
            "secret_access": {"token": "private-checkpoint-token"},
            "resign_pending_dates": ["2026-11-18"], "resign_today": "2026-11-20",
            "resign_unresolved_dates": ["2026-11-17"]}


def dynamic(feature, category, sid):
    payload = request(feature, {})
    payload.categories = {feature: category}
    payload.feature_schemas = {feature: {"type": "object", "additionalProperties": False}}
    if category == "post":
        payload.job_runtime = job_runtime()
    if feature.startswith("plugin."):
        payload.plugin_runtimes = {"demo": {
            "plugin_id": "demo", "endpoint": "http://private-plugin.internal",
            "token": "private-plugin-token", "generation": 1, "permission_epoch": 0,
            "approved_capabilities": ["features", "browser_actions"],
            "authorization": {"scope": "assistant_operation", "session_id": sid,
                              "operation_id": str(payload.operation_id), "token": "private-auth-token"},
        }}
    return payload


@pytest.mark.parametrize("feature,category", [
    ("post.buy_card", "post"), ("post.resign", "post"),
    ("post.resign_loop", "post"), ("post.resign_dialog", "post"),
    ("plugin.demo.login", "auth"), ("plugin.demo.claim", "sign"),
])
@pytest.mark.parametrize("outcome", ["success", "fail", "exception"])
def test_execute_dynamic_context_results_and_uncertainty(public_dns, monkeypatch, feature, category, outcome):
    async def run():
        manager = Manager()
        manager.plugin_broker = PluginBroker("http://worker/broker")
        browser = AssistantBrowser(manager)
        sid = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        session = browser.sessions[sid]
        payload = dynamic(feature, category, sid)

        async def execute(page, step, **kw):
            assert page is manager.page and step == payload.step
            assert session.executing and session.same_origin_only and session.writes_authorized
            assert session.credential_execution == (category == "auth")
            if category == "post":
                assert kw["resign_pending_dates"] == ["2026-11-18"]
                assert all(kw["secret_payload"][k] == v for k, v in job_runtime().items())
                await kw["resign_progress"]({"resign": {"submitted": ["2026-11-18"]}})
            else:
                assert kw["plugin_runtimes"] == payload.plugin_runtimes
                assert kw["plugin_broker"].broker is manager.plugin_broker
                assert kw["plugin_broker"].context.page is page
            if outcome == "exception":
                raise RuntimeError("private-auth-token")
            return outcome, "private-plugin-token", {"details": {"completed": 2},
                                                      "private": "private-checkpoint-token" if category == "post" else "private-auth-token"}

        callback = AsyncMock(side_effect=execute)
        monkeypatch.setattr(executor, "_run_step", callback)
        try:
            result = await browser.execute(sid, payload)
            assert result["status"] == ("success" if outcome == "success" else "unknown")
            if outcome != "exception":
                assert result["result"] == {"details": {"completed": 2}, "private": "[redacted]"}
            assert "private-auth-token" not in json.dumps(result)
            assert "private-checkpoint-token" not in json.dumps(result)
            assert await browser.execute(sid, payload) == result
            callback.assert_awaited_once()
            assert not session.executing and not session.writes_authorized
            assert session.uncertain == (outcome != "success")
        finally:
            await browser.stop()
    asyncio.run(run())


def test_execute_real_resign_dispatch_has_checkpoint_and_progress(public_dns, monkeypatch):
    async def run():
        browser = AssistantBrowser(Manager())
        sid = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        checkpoint = AsyncMock()
        captured = []

        def make(payload, key):
            captured.append((dict(payload), key))
            return checkpoint

        async def resign(page, config, pending, note, **kw):
            assert pending == ["2026-11-18"]
            assert kw["today"] == "2026-11-20"
            assert kw["unresolved"] == ["2026-11-17"]
            assert kw["base_url"] == "https://example.com"
            await kw["durable_progress"]({}, "prepare", pending[0])
            await kw["progress"]({"resign": {"success": pending}})
            return "success", "", {"resign": {"success": pending}}

        monkeypatch.setattr(resign_checkpoint, "make_resign_checkpoint", make)
        monkeypatch.setattr(resign_dialog, "run_resign_dialog", resign)
        try:
            result = await browser.execute(sid, dynamic("post.resign_dialog", "post", sid))
            assert result["status"] == "success", result
            assert result["result"] == {"resign": {"success": ["2026-11-18"]}}
            assert captured[0][0]["job_id"] == "real-host-job"
            assert captured[0][0]["secret_access"] == job_runtime()["secret_access"]
            assert captured[0][1] == "private"
            checkpoint.assert_awaited_once()
        finally:
            await browser.stop()
    asyncio.run(run())


@pytest.mark.parametrize("browser_actions", [True, False])
def test_real_plugin_dispatch_optional_browser_callback(public_dns, monkeypatch, browser_actions):
    async def run():
        manager = Manager()
        manager.plugin_broker = PluginBroker("http://worker/broker")
        browser = AssistantBrowser(manager)
        sid = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        payload = dynamic("plugin.demo.run", "action", sid)
        if not browser_actions:
            payload.plugin_runtimes["demo"]["approved_capabilities"] = ["features"]
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(plugin_authorization, "authorize", AsyncMock())

        async def callback(token):
            bound = manager.plugin_broker._session(token)
            assert bound.assistant_context is not None
            with pytest.raises(BrokerDenied):
                await manager.plugin_broker.execute({"token": token, "op": "goto", "url": "https://evil.example/"})
            with pytest.raises(BrokerDenied):
                await manager.plugin_broker.execute({"token": token, "op": "screenshot"})
            return await manager.plugin_broker.execute({"token": token, "op": "goto", "url": URL + "/done"})

        def execute(*args, **kw):
            if browser_actions:
                assert kw["broker_url"] == manager.plugin_broker.public_url
                assert asyncio.run_coroutine_threadsafe(callback(kw["broker_token"]), loop).result(5)["ok"]
            else:
                assert kw["broker_url"] == kw["broker_token"] == ""
            return {"status": "success", "data": {"plugin": {"completed": True}}}

        monkeypatch.setattr(plugin_client, "execute_step", execute)
        try:
            result = await browser.execute(sid, payload)
            assert result["status"] == "success", result
            assert result["result"] == {"plugin": {"completed": True}}
            assert not manager.plugin_broker._sessions
        finally:
            await browser.stop()
    asyncio.run(run())


@pytest.mark.parametrize("field", ["resign_pending_dates", "resign_unresolved_dates"])
@pytest.mark.parametrize("dates", [["invalid"], ["2026-02-30"], ["2026-11-18", "2026-11-18"], ["2026-11-19", "2026-11-18"]])
def test_job_dates_validated_on_job_runtime(field, dates):
    with pytest.raises(ValidationError):
        JobRuntime.model_validate({**job_runtime(), field: dates})


def test_real_post_catalog_schemas_allow_local_definitions_only():
    from app.features.catalog import FEATURES

    for key in ("post.buy_card", "post.resign", "post.resign_loop", "post.resign_dialog"):
        model = FEATURES[key].config_model
        validate_dynamic_config(model().model_dump(mode="json"), model.model_json_schema())
    for schema in ({"$ref": "https://evil.example/schema"},
                   {"$defs": {"a": {"$ref": "#/$defs/a"}}, "$ref": "#/$defs/a"}):
        with pytest.raises(ValueError):
            validate_dynamic_config({}, schema)


def test_builtin_post_category_cannot_be_downgraded_to_skip_job_runtime():
    from worker.assistant_step import validate_step

    payload = dynamic("post.buy_card", "post", str(uuid.uuid4()))
    payload.categories = {"post.buy_card": "action"}
    payload.job_runtime = None
    with pytest.raises(ValueError, match="Post context unavailable"):
        validate_step(payload)


def test_private_captcha_receives_plugin_runtime(public_dns, monkeypatch):
    async def run():
        browser = AssistantBrowser(Manager())
        sid = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        payload = request("challenge.image_captcha", {
            "image_selector": ["#image"], "input_selector": ["#code"],
            "captcha_method": "plugin.demo.ocr"})
        plugin = dynamic("plugin.demo.run", "action", sid).plugin_runtimes["demo"]
        plugin["approved_capabilities"] = ["captcha_methods"]
        plugin["authorization"]["operation_id"] = str(payload.operation_id)
        payload.plugin_runtimes = {"demo": plugin}

        async def private_step(context, step, vision, cfbox, lane_key, plugin_runtimes):
            assert context.page is browser.sessions[sid].page
            assert plugin_runtimes == payload.plugin_runtimes
            assert lane_key == "interaction"
            return "success", "", {"submitted": False}

        monkeypatch.setattr("worker.assistant_private.run_private_step", private_step)
        try:
            result = await browser.execute(sid, payload)
            assert result["status"] == "success", result
            assert result["result"] == {"submitted": False}
        finally:
            await browser.stop()
    asyncio.run(run())


def test_plugin_operation_scope_and_origin_fail_before_dispatch(public_dns, monkeypatch):
    async def run():
        browser = AssistantBrowser(Manager())
        sid = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        execute = AsyncMock()
        monkeypatch.setattr(executor, "_run_step", execute)
        try:
            payload = dynamic("plugin.demo.run", "action", sid)
            payload.plugin_runtimes["demo"]["authorization"]["operation_id"] = str(uuid.uuid4())
            with pytest.raises(HTTPException) as exc:
                await browser.execute(sid, payload)
            assert exc.value.status_code == 422
            payload = dynamic("plugin.demo.run", "action", sid)
            payload.site_origin = "https://evil.example"
            with pytest.raises(HTTPException):
                await browser.execute(sid, payload)
            execute.assert_not_awaited()
        finally:
            await browser.stop()
    asyncio.run(run())
