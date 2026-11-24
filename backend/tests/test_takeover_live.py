from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models.assistant import AssistantSession
from app.models.assistant_takeover import AssistantTakeoverAction
from app.models.execution import Run, SiteJob
from app.models.setting import AppSetting
from app.models.workflow import Site
from app.schemas.assistant import SendMessage
from app.schemas.assistant_takeover import TakeoverSettingsUpdate, TakeoverValues
from app.services.assistant import BrowserScope
from app.services.assistant_categories import builtin_category
from app.services.assistant_execution import AssistantExecution, SUPPORTED_FEATURES
from app.services.assistant_takeover import (
    LIVE_TOOLS, _target, remove_takeover, takeover_tool_allowed, takeover_tool_definitions,
)
from app.services.assistant_takeover_settings import KEY, SITE_GRANTS_KEY, TakeoverSettingsService
from app.services.assistant_tools import AssistantTools
from app.services.assistant_workflows import AssistantWorkflows
from app.services.bitwarden import BitwardenSecret, BitwardenService
from app.services.credentials import CredentialService
from app.services.assistant_settings import AssistantSettingsError
from app.services.workflows import WorkflowService
from app.schemas.workflow import SiteWrite
from tests.test_assistant_execution import arguments, response, USERNAME, PASSWORD
from tests.test_assistant_private_execution import integrations  # noqa: F401
from tests.test_assistant_snapshot import snapshot
from tests.test_assistant_takeover import (  # noqa: F401
    OWNER, ctx, failed_job, grant, queued, tools_for, uncertain_job,
)
from tests.test_assistant_workflows import submit
from tests.test_takeover_daily import RUNTIME, action_for, install_model, session_data
from tests.test_assistant_api import assistant_client, provider, text, tool  # noqa: F401


def scope_for(tools):
    scope = BrowserScope(OWNER, str(uuid4()), "private-browser-token", "proxy_interaction", "",
                         time.monotonic() + 900, site_origin="https://example.com", same_origin_only=True,
                         automation_request_id=(tools.manager._active_turns.get(
                             tools.session_id) or {}).get("_takeover_request_id"))
    tools.manager.browsers[tools.session_id] = scope
    return scope


def test_policy_requires_explicit_live_decision_and_migrates_only_exact_legacy(ctx):
    with pytest.raises(ValidationError, match="Explicit live-access"):
        TakeoverSettingsUpdate.model_validate({"expected_revision": 0, "confirm": True,
                                               "values": {"enabled": True}})
    assert TakeoverValues().live_access is True
    policy = grant(ctx, live_access=True)
    legacy = policy.model_dump(mode="json")
    legacy["values"].pop("live_access")
    with ctx[1].session_factory() as db:
        db.get(AppSetting, KEY).value_json = json.dumps(legacy)
        db.commit()
        service = TakeoverSettingsService(db)
        migrated = service.view()
        assert migrated.values.enabled and migrated.values.live_access is False
        assert migrated.revision == policy.revision
        # Merely loading the old policy does not rewrite its authorization record.
        assert "live_access" not in db.get(AppSetting, KEY).value_json
        saved = service.update(TakeoverSettingsUpdate(expected_revision=migrated.revision,
            confirm=True, values=migrated.values), OWNER)
        assert not saved.values.live_access


@pytest.mark.parametrize("change", ["origin", "reused_id"])
def test_selected_live_policy_freezes_site_identity_until_explicit_resave(ctx, change):
    policy = grant(ctx, live_access=True)
    manager = ctx[1]
    with manager.session_factory() as db:
        saved = json.loads(db.get(AppSetting, SITE_GRANTS_KEY).value_json)
        original = db.get(Site, ctx[2])
        assert saved == {"revision": policy.revision, "bindings": {str(ctx[2]): {
            "history_key": original.history_key, "origin": "https://example.com"}}}
        if change == "origin":
            original.base_url = "https://new.example/"
            original.config_revision += 1
            db.commit()
        else:
            service = WorkflowService(db, manager.settings, manager.crypto)
            service.delete_site(ctx[2])
            replacement = service.create_site(SiteWrite(name="Replacement", base_url="https://example.com/"))
            assert replacement.id == ctx[2] and replacement.history_key != saved["bindings"][str(ctx[2])]["history_key"]
    failed_job(ctx)
    assert queued(ctx) is None
    with manager.session_factory() as db:
        service = TakeoverSettingsService(db)
        service.update(TakeoverSettingsUpdate(expected_revision=policy.revision, confirm=True,
                                              values=policy.values), OWNER)
    # Only failures after the new authorization are eligible.
    job_id, _ = failed_job(ctx)
    assert queued(ctx).source_job_id == job_id


@pytest.mark.parametrize("damage", ["missing", "revision", "history", "origin", "malformed"])
def test_pre_binding_live_policies_and_corrupt_bindings_fail_closed(ctx, damage):
    grant(ctx, live_access=True)
    with ctx[1].session_factory() as db:
        row = db.get(AppSetting, SITE_GRANTS_KEY)
        value = json.loads(row.value_json)
        if damage == "missing":
            db.delete(row)
        elif damage == "malformed":
            row.value_json = "{}"
        else:
            if damage == "revision":
                value["revision"] += 1
            else:
                value["bindings"][str(ctx[2])]["history_key" if damage == "history" else "origin"] = "changed"
            row.value_json = json.dumps(value)
        db.commit()
        assert TakeoverSettingsService(db).view().values.live_access
    failed_job(ctx)
    assert queued(ctx) is None


def test_selected_missing_site_rejects_save_atomically_and_conflict_cannot_replace_bindings(ctx):
    policy = grant(ctx, live_access=True)
    with ctx[1].session_factory() as db:
        service = TakeoverSettingsService(db)
        old = db.get(AppSetting, SITE_GRANTS_KEY).value_json
        with pytest.raises(AssistantSettingsError, match="takeover_site_unavailable"):
            service.update(TakeoverSettingsUpdate(expected_revision=policy.revision, confirm=True,
                values=policy.values.model_copy(update={"site_ids": [ctx[2], 99999]})), "another-owner")
        assert service.view() == policy
        assert db.get(AppSetting, SITE_GRANTS_KEY).value_json == old
        with pytest.raises(AssistantSettingsError, match="conflict"):
            service.update(TakeoverSettingsUpdate(expected_revision=0, confirm=True, values=policy.values), "another-owner")
        assert service.view().authorizing_owner == OWNER
        assert db.get(AppSetting, SITE_GRANTS_KEY).value_json == old


def test_live_binding_loss_blocks_activation_and_existing_turn_without_reissuing_grants(ctx):
    from app.services.assistant import AssistantError
    from app.services.assistant_takeover import activate_takeover

    grant(ctx, live_access=True)
    failed_job(ctx)
    tools, action_id = tools_for(ctx)
    with ctx[1].session_factory() as db:
        db.delete(db.get(AppSetting, SITE_GRANTS_KEY))
        db.commit()
    assert takeover_tool_allowed(tools, "browser_open", {})["code"] == "takeover_tool_denied"
    with ctx[1].session_factory() as db:
        row = db.get(AssistantSession, tools.session_id)
        action = db.get(AssistantTakeoverAction, action_id)
        before = ctx[1]._decrypt(row)
        with pytest.raises(AssistantError) as caught:
            activate_takeover(ctx[1], db, row, action.id, action.request_id)
        assert caught.value.code == "takeover_authorization_changed"
        assert ctx[1]._decrypt(row) == before


def test_api_missing_selected_site_rejects_without_policy_or_grant_write(client):
    root = "/api/v1/assistant/takeover/settings"
    response = client.put(root, json={"expected_revision": 0, "confirm": True,
        "values": {"enabled": True, "live_access": True, "mode": "analysis",
                   "site_ids": [999999], "max_attempts": 1}})
    assert response.status_code == 422
    assert client.get(root).json()["revision"] == 0
    with client.app.state.session_factory() as db:
        assert db.get(AppSetting, KEY) is None and db.get(AppSetting, SITE_GRANTS_KEY) is None


def test_all_sites_live_consent_allows_new_identity_without_hidden_selected_scope(ctx):
    with ctx[1].session_factory() as db:
        TakeoverSettingsService(db).update(TakeoverSettingsUpdate(expected_revision=0, confirm=True,
            values=TakeoverValues(enabled=True, live_access=True, site_ids=None)), OWNER)
        service = WorkflowService(db, ctx[1].settings, ctx[1].crypto)
        service.delete_site(ctx[2])
        replacement = service.create_site(SiteWrite(name="Future", base_url="https://future.example/"))
        assert replacement.id == ctx[2]
    failed_job(ctx)
    tools, _ = tools_for(ctx)
    assert tools.target["_takeover_site_grant"]["origin"] == "https://future.example"
    assert takeover_tool_allowed(tools, "browser_open", {}) is True


@pytest.mark.parametrize("change", ["origin", "reused_id"])
@pytest.mark.parametrize("human", [False, True])
def test_changed_site_identity_revokes_active_and_inherited_service_access(ctx, monkeypatch, change, human):
    grant(ctx, live_access=True)
    failed_job(ctx)
    tools, _ = tools_for(ctx)
    scope_for(tools)
    if human:
        with tools.manager.session_factory() as db:
            row = db.get(AssistantSession, tools.session_id)
            data = tools.manager._decrypt(row)
            remove_takeover(data)
            tools.manager._encrypt(row, data)
            db.commit()
        tools.manager._active_turns[tools.session_id] = {"id": "human"}
        tools = AssistantTools(tools.manager, OWNER, tools.session_id,
            session_data(tools.manager, tools.session_id)["target"], turn=tools.manager._active_turns[tools.session_id])
    with tools.manager.session_factory() as db:
        if change == "origin":
            db.get(Site, ctx[2]).base_url = "https://new.example/"
            db.commit()
        else:
            service = WorkflowService(db, tools.manager.settings, tools.manager.crypto)
            service.delete_site(ctx[2])
            replacement = service.create_site(SiteWrite(name="Replacement", base_url="https://example.com/"))
            assert replacement.id == ctx[2]
    worker = AsyncMock()
    monkeypatch.setattr(tools.manager, "_worker", worker)
    assert asyncio.run(tools.manager.browser_tool(OWNER, tools.session_id, tools.target,
                                                 "browser_open", None))["code"] == "takeover_tool_denied"
    assert asyncio.run(AssistantExecution(tools.manager, OWNER, tools.session_id, tools.target).execute(
        arguments()))["code"] == "takeover_tool_denied"
    output = asyncio.run(AssistantWorkflows(tools.manager, OWNER, tools.session_id, tools.target).dispatch(
        "import_workflow_draft", {"proposal_message_id": 123}))
    assert output["code"] == "takeover_write_denied"
    worker.assert_not_awaited()


@pytest.mark.parametrize("lane,proxy", [
    ("general", False), ("interaction", False),
    ("proxy_general", True), ("proxy_interaction", True),
])
@pytest.mark.parametrize("provider,mode", [
    ("builtin.manual", "manual"), ("builtin.bitwarden", "bitwarden"), ("plugin.private", "site"),
])
def test_target_grants_saved_identity_supported_features_and_independent_lane(ctx, lane, proxy, provider, mode):
    with ctx[1].session_factory() as db:
        site = db.get(Site, ctx[2])
        site.lane_policy, site.credential_provider_id = lane, provider
        target = _target(site, live_access=True, policy_revision=7)
    assert target["credential_mode"] == mode
    assert target["proxy"] is proxy and target["run_lane_policy"] == lane
    assert target["browser_consent"] and target["screenshot_consent"] and target["execution_consent"]
    assert target["authorized"] and target["allow_sign"] and target["trusted_origins"] == []
    auth = {"auth.form_login", "auth.password_verify", "challenge.2fa"}
    assert set(target["execution_features"]) == SUPPORTED_FEATURES - (auth if mode == "none" else set())
    assert target["credential_consent"] is (mode != "none")
    assert target["_takeover_site_grant"]["policy_revision"] == 7


@pytest.mark.parametrize("mode", ["analysis", "autofix"])
def test_live_analysis_is_not_read_only_but_never_authorizes_full_run_or_mutations(ctx, mode):
    grant(ctx, live_access=True, mode=mode)
    failed_job(ctx)
    tools, _ = tools_for(ctx)
    for name in LIVE_TOOLS:
        args = arguments() if name == "execute_step" else {}
        assert takeover_tool_allowed(tools, name, args) is True
    denied = {"ok": False, "code": "takeover_tool_denied"}
    for name in ["retry_step", "run_site", "update_site_config", "create_template_from_site",
                 "request_user_interaction", "update_scheduler_settings", "cancel_run"]:
        assert asyncio.run(tools.dispatch(name, {})) == denied
    definitions = [{"function": {"name": name}} for name in LIVE_TOOLS | {"retry_step", "run_site"}]
    assert {item["function"]["name"] for item in takeover_tool_definitions(
        tools.manager, OWNER, tools.session_id, definitions)} == LIVE_TOOLS
    workflows = AssistantWorkflows(tools.manager, OWNER, tools.session_id, tools.target)
    proposal = submit(tools)
    result = asyncio.run(workflows.dispatch("import_workflow_draft", {"proposal_message_id": proposal}))
    assert result["ok"] is (mode == "autofix")
    if mode == "autofix":
        assert not takeover_tool_allowed(tools, "import_workflow_draft", {"proposal_message_id": proposal})["ok"]
    assert asyncio.run(workflows.dispatch("run_site", {}))["code"] == (
        "takeover_write_denied" if mode == "autofix" else "workflow_consent_required")


@pytest.mark.parametrize("feature", sorted(
    key for key in SUPPORTED_FEATURES if builtin_category(key) != "post"))
@pytest.mark.parametrize("source_status", ["fail", "waiting_human"])
def test_nonpost_single_steps_use_private_saved_credentials_and_worker_contract(
    ctx, monkeypatch, integrations, feature, source_status,
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    from worker.assistant_step import ExecuteRequest, validate_step

    grant(ctx, live_access=True)
    with ctx[1].session_factory() as db:
        CredentialService(db, ctx[1].crypto).update(ctx[2], username=USERNAME, password=PASSWORD)
    if source_status == "waiting_human":
        from tests.test_takeover_waiting_human import waiting_job

        waiting_job(ctx)
    else:
        failed_job(ctx)
    tools, _ = tools_for(ctx)
    tools.turn["id"] = "test-turn"
    scope = scope_for(tools)
    captured = []

    async def worker(path, payload):
        assert path == f"{scope.session_id}/execute"
        captured.append(deepcopy(payload))
        assert validate_step(ExecuteRequest.model_validate(payload))["feature_type"] == feature
        return response(scope, payload["operation_id"])

    monkeypatch.setattr(tools.manager, "_worker", AsyncMock(side_effect=worker))
    config = {"selector": ["#button"]} if feature in {"action.click", "action.fill"} else {}
    if feature == "flow.jump":
        config = {"match_mode": "always", "action": "restart"}
    if feature == "challenge.2fa":
        config = {"service_hint": "Example", "username_hint": USERNAME}
    result = asyncio.run(tools.dispatch("execute_step", arguments(feature, config)))
    assert result["ok"], result
    assert captured[0]["execution"] == {"feature_types": [feature], "allow_sign": True}
    assert ("credentials" in captured[0]) is feature.startswith("auth.")
    if feature.startswith("auth."):
        assert captured[0]["credentials"] == {"username": USERNAME, "password": PASSWORD}
    data = session_data(tools.manager, tools.session_id)
    assert PASSWORD not in json.dumps([data, result])
    assert tools.manager.browsers[tools.session_id] is scope


@pytest.mark.parametrize("change", ["disable", "revision", "owner", "history", "origin", "lane", "enabled"])
def test_service_boundaries_revalidate_live_policy_even_without_dispatch(ctx, monkeypatch, change):
    grant(ctx, live_access=True)
    failed_job(ctx)
    tools, _ = tools_for(ctx)
    scope_for(tools)
    with tools.manager.session_factory() as db:
        if change in {"disable", "revision", "owner"}:
            service = TakeoverSettingsService(db)
            policy = service.view()
            service.update(TakeoverSettingsUpdate(expected_revision=policy.revision, confirm=True,
                values=policy.values.model_copy(update={"enabled": change != "disable"})),
                "another-owner" if change == "owner" else OWNER)
        else:
            site = db.get(Site, ctx[2])
            if change == "history":
                site.history_key = str(uuid4())
            elif change == "origin":
                site.base_url = "https://different.example/"
            elif change == "lane":
                site.lane_policy = "general"
            else:
                site.enabled = False
            db.commit()
    worker = AsyncMock()
    monkeypatch.setattr(tools.manager, "_worker", worker)
    result = asyncio.run(tools.manager.browser_tool(OWNER, tools.session_id, tools.target, "browser_open", None))
    assert result["code"] == "takeover_tool_denied"
    result = asyncio.run(AssistantExecution(tools.manager, OWNER, tools.session_id, tools.target).execute(arguments()))
    assert result["code"] == "takeover_tool_denied"
    worker.assert_not_awaited()


@pytest.mark.parametrize("change", ["busy", "uncertain", "stale"])
def test_busy_stale_or_uncertain_source_limits_steps_but_not_observation(ctx, monkeypatch, change):
    grant(ctx, live_access=True)
    failed_job(ctx)
    tools, _ = tools_for(ctx)
    scope = scope_for(tools)
    if change == "uncertain":
        uncertain_job(ctx, "manual", "unknown")
    else:
        with tools.manager.session_factory() as db:
            if change == "stale":
                db.get(Site, ctx[2]).config_revision += 1
            else:
                run = Run(created_by=OWNER, trigger="manual", status="running")
                db.add(run)
                db.flush()
                db.add(SiteJob(run_id=run.id, site_id=ctx[2], site_name="busy", lane="general", status="running"))
            db.commit()
    worker = AsyncMock(side_effect=lambda path, payload: (
        response(scope, payload["operation_id"]) if path.endswith("/execute") else {
            **snapshot(), "session_id": scope.session_id, "lane_key": scope.lane_key}))
    monkeypatch.setattr(tools.manager, "_worker", worker)
    assert asyncio.run(tools.dispatch("browser_observe", {}))["ok"]
    result = asyncio.run(AssistantExecution(tools.manager, OWNER, tools.session_id, tools.target).execute(
        arguments("action.click", {"selector": ["#submit"]})))
    if change == "uncertain":
        assert result["ok"]
        assert takeover_tool_allowed(tools, "execute_step", arguments()) is True
        assert worker.await_count == 2
    else:
        assert result["code"] == "takeover_tool_denied"
        worker.assert_awaited_once()


@pytest.mark.parametrize("source_status", ["fail", "waiting_human"])
def test_unknown_step_does_not_trigger_unattended_retry_popup_or_import(ctx, monkeypatch, source_status):
    grant(ctx, live_access=True, mode="autofix")
    if source_status == "waiting_human":
        from tests.test_takeover_waiting_human import waiting_job

        waiting_job(ctx)
    else:
        failed_job(ctx)
    tools, _ = tools_for(ctx)
    tools.turn["id"] = "test-turn"
    scope = scope_for(tools)
    worker = AsyncMock(side_effect=lambda path, payload: response(scope, payload["operation_id"],
                                      status="unknown", code="outcome_uncertain"))
    monkeypatch.setattr(tools.manager, "_worker", worker)
    popup = AsyncMock(side_effect=AssertionError("No unattended approval"))
    monkeypatch.setattr("app.services.assistant_retry.run_with_retry_approval", popup)
    args = arguments("action.click", {"selector": ["#sign"]})
    assert asyncio.run(tools.dispatch("execute_step", args))["status"] == "unknown"
    args["operation_id"] = str(uuid4())
    assert asyncio.run(tools.dispatch("execute_step", args))["status"] == "unknown"
    assert asyncio.run(tools.dispatch("retry_step", {"operation_id": args["operation_id"]}))["code"] == "takeover_tool_denied"
    assert takeover_tool_allowed(tools, "import_workflow_draft", {"proposal_message_id": 1})["ok"] is False
    popup.assert_not_awaited()
    assert worker.await_count == 2
    with tools.manager.session_factory() as db:
        row = db.get(AssistantSession, tools.session_id)
        data = tools.manager._decrypt(row)
        remove_takeover(data)
        tools.manager._encrypt(row, data)
        db.commit()
    tools.manager._active_turns[tools.session_id] = {"id": "human-turn"}
    popup.side_effect = None
    popup.return_value = {"ok": False, "code": "human_retry_approval_requested"}
    human = AssistantTools(tools.manager, OWNER, tools.session_id, session_data(
        tools.manager, tools.session_id)["target"], turn=tools.manager._active_turns[tools.session_id])
    assert asyncio.run(human.dispatch("retry_step", {"operation_id": args["operation_id"]}))["code"] == (
        "human_retry_approval_requested")
    popup.assert_awaited_once()


def test_model_failure_after_live_dispatch_is_never_automatically_replayed(ctx, monkeypatch):
    from app.models.assistant_takeover import AssistantTakeoverAction

    grant(ctx, live_access=True, max_attempts=3)
    takeover, manager, _ = ctx
    seen = []

    async def execute_then_fail(owner, sid, config):
        tools = AssistantTools(manager, owner, sid, session_data(manager, sid)["target"],
                               config=config, turn=manager._active_turns[sid])
        scope = scope_for(tools)
        seen.append(sid)
        manager._worker = AsyncMock(side_effect=lambda path, payload: (
            response(scope, payload["operation_id"]) if path.endswith("/execute") else {}))
        assert (await tools.dispatch("execute_step", arguments("action.click", {"selector": ["#sign"]})))["ok"]
        raise TimeoutError

    install_model(monkeypatch, manager, execute_then_fail)

    async def scenario():
        action = action_for(ctx)
        await takeover.process_one()
        await takeover.process_one()
        with manager.session_factory() as db:
            saved = db.get(AssistantTakeoverAction, action.id)
            assert saved.status == "failed" and saved.attempts == 1

    asyncio.run(scenario())
    assert len(seen) == 1


@pytest.mark.parametrize("feature", [
    *sorted(key for key in SUPPORTED_FEATURES if builtin_category(key) == "post"), "plugin.custom",
])
def test_unattended_unsupported_steps_are_denied_before_any_popup_or_worker(ctx, monkeypatch, feature):
    grant(ctx, live_access=True)
    failed_job(ctx)
    tools, _ = tools_for(ctx)
    scope_for(tools)
    worker = AsyncMock()
    monkeypatch.setattr(tools.manager, "_worker", worker)
    assert asyncio.run(tools.dispatch("execute_step", arguments(feature)))["code"] == "takeover_tool_denied"
    assert session_data(tools.manager, tools.session_id).get("_pending_interaction") is None
    worker.assert_not_awaited()


@pytest.mark.parametrize("mode", ["bitwarden", "missing_manual", "cookie_only", "plugin"])
def test_live_credentials_follow_saved_provider_without_fallback(ctx, monkeypatch, mode):
    grant(ctx, live_access=True)
    with ctx[1].session_factory() as db:
        site = db.get(Site, ctx[2])
        if mode == "bitwarden":
            site.credential_provider_id = "builtin.bitwarden"
        elif mode == "plugin":
            site.credential_provider_id = "plugin.private"
        if mode == "cookie_only":
            CredentialService(db, ctx[1].crypto).update_cookies(ctx[2], "session=private-cookie")
        db.commit()
    # Bitwarden resolution is a synchronous private host operation.
    from unittest.mock import Mock
    bw = Mock(return_value=BitwardenSecret(username=USERNAME, password=PASSWORD))
    monkeypatch.setattr(BitwardenService, "resolve_for_site", bw)
    failed_job(ctx)
    tools, _ = tools_for(ctx)
    scope = scope_for(tools)
    worker = AsyncMock(side_effect=lambda path, payload: response(scope, payload["operation_id"]))
    monkeypatch.setattr(tools.manager, "_worker", worker)
    output = asyncio.run(AssistantExecution(tools.manager, OWNER, tools.session_id, tools.target).execute(
        arguments("auth.form_login")))
    if mode == "bitwarden":
        assert output["ok"]
        bw.assert_called_once()
        lookup = bw.call_args.args[0]
        assert lookup.base_url == tools.target["base_url"] and lookup.bitwarden_match_mode == "domain"
        assert lookup.bitwarden_custom_match is None
        worker.assert_awaited_once()
    else:
        assert output["code"] == "credentials_unavailable"
        bw.assert_not_called()
        worker.assert_not_awaited()


@pytest.mark.parametrize("vision", [False, True])
def test_real_model_takeover_browser_observation_step_screenshot_and_human_continuation(
    ctx, monkeypatch, provider, vision,
):
    import base64
    from io import BytesIO
    from PIL import Image
    from app.models.assistant_takeover import AssistantTakeoverAction

    grant(ctx, live_access=True)
    takeover, manager, _ = ctx
    monkeypatch.setattr(manager, "_runtime", lambda: {**RUNTIME, "supports_vision": vision})
    opened, seen = [], []
    buffer = BytesIO()
    Image.new("RGB", (12, 12), "white").save(buffer, "JPEG")

    async def worker(path, payload):
        seen.append(path)
        if path == "open":
            opened.append(payload["session_id"])
            assert payload["lane_key"] == "proxy_interaction"
            assert payload["same_origin_only"] is True
            assert payload.get("wait_for_lane", False) is (len(opened) == 1)
            return {"session_id": opened[-1], "lane_key": "proxy_interaction", "status": "ready",
                    "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()}
        if path.endswith("/close"):
            return {"closed": True}
        identity = {"session_id": opened[-1], "lane_key": "proxy_interaction"}
        if path.endswith("/observe"):
            return {**snapshot(), **identity}
        if path.endswith("/screenshot"):
            return {**identity, "url": "https://example.com/", "mime_type": "image/jpeg",
                    "width": 12, "height": 12, "masked": True,
                    "image_base64": base64.b64encode(buffer.getvalue()).decode()}
        assert path.endswith("/execute")
        return response(next(iter(manager.browsers.values())), payload["operation_id"])

    monkeypatch.setattr(manager, "_worker", AsyncMock(side_effect=worker))

    async def scenario():
        action = action_for(ctx)
        provider[0].extend([
            tool("browser_open", {}, "open"),
            tool("browser_observe", {}, "observe"),
            tool("execute_step", arguments("action.click", {"selector": ["#sign"]}), "sign"),
            tool("browser_screenshot", {}, "shot"),
            text("Live action completed; no configuration was saved."),
        ])
        await takeover.process_one()
        with manager.session_factory() as db:
            saved = db.get(AssistantTakeoverAction, action.id)
            assert saved.status == "completed_analysis"
            sid = saved.session_id
        requests = [json.loads(request.content) for request in provider[1]]
        assert "overrides the static FAILURE TAKEOVER" in requests[0]["messages"][0]["content"]
        serialized = json.dumps(requests)
        assert "private-browser-token" not in serialized
        assert "_takeover_site_grant" not in serialized
        assert "_credential_binding" not in serialized
        assert ("data:image/jpeg;base64," in serialized) is vision
        shot = next(json.loads(message["content"]) for message in manager.detail(OWNER, sid)["messages"]
                    if message.get("tool_name") == "browser_screenshot")
        assert shot["ok"] is vision
        if not vision:
            assert shot["code"] == "model_vision_required"
        assert sid not in manager.browsers
        provider[0].extend([tool("browser_open", {}, "followup-open"),
                           tool("browser_observe", {}, "followup-observe"), text("Fresh page opened.")])
        await manager.send(OWNER, sid, SendMessage(request_id=uuid4(), content="Inspect this page again",
                                                  consent_to_send=True))
        await manager.tasks[sid]
        assert manager.browsers[sid].session_id == opened[-1]
        assert manager.browsers[sid].automation_request_id is None
        assert manager.detail(OWNER, sid)["pending_interaction"] is None
        assert all(outcome["ok"] for outcome in session_data(manager, sid)["_turn_outcomes"].values())

    asyncio.run(scenario())
    assert seen == ["open", f"{opened[0]}/observe", f"{opened[0]}/execute",
                    *([f"{opened[0]}/screenshot"] if vision else []), f"{opened[0]}/close",
                    "open", f"{opened[1]}/observe"]


@pytest.mark.parametrize("mode", ["analysis", "autofix"])
def test_success_closes_auto_page_but_retains_permissions_after_policy_disabled(ctx, monkeypatch, mode):
    grant(ctx, live_access=True, mode=mode)
    takeover, manager, _ = ctx
    scopes = []

    async def inspect(owner, sid, config):
        target = session_data(manager, sid)["target"]
        tools = AssistantTools(manager, owner, sid, target, config=config, turn=manager._active_turns[sid])
        if config.get("_takeover_action_id"):
            scopes.append(scope_for(tools))
        else:
            assert sid not in manager.browsers
            assert "_takeover" not in session_data(manager, sid)
            assert target["workflow_consent"] is True
            assert all(target[key + "_consent"] for key in ["browser", "execution", "credential", "screenshot"])
            opened = await tools.dispatch("browser_open", {})
            assert opened["ok"] and opened["lane_key"] == "proxy_interaction"
            assert (await tools.dispatch("execute_step", arguments()))["ok"]
            assert takeover_tool_allowed(tools, "retry_step", {}) is True
            assert config["_assistant_settings"]["auto_authorize_workflow"] is False
            proposal = await asyncio.to_thread(submit, tools)
            assert (await tools.dispatch("import_workflow_draft", {"proposal_message_id": proposal}))["ok"]

    install_model(monkeypatch, manager, inspect)
    async def worker_response(path, payload):
        if path == "open":
            assert "wait_for_lane" not in payload
            return {"session_id": payload["session_id"], "lane_key": payload["lane_key"],
                    "status": "ready",
                    "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()}
        if path.endswith("/close"):
            return {"closed": True}
        return response(next(iter(manager.browsers.values())), payload["operation_id"])

    worker = AsyncMock(side_effect=worker_response)
    monkeypatch.setattr(manager, "_worker", worker)

    async def scenario():
        action = action_for(ctx)
        await takeover.process_one()
        from app.models.assistant_takeover import AssistantTakeoverAction
        with manager.session_factory() as db:
            sid = db.get(AssistantTakeoverAction, action.id).session_id
            policy = TakeoverSettingsService(db).view()
            TakeoverSettingsService(db).update(TakeoverSettingsUpdate(expected_revision=policy.revision,
                confirm=True, values=policy.values.model_copy(update={"enabled": False})), OWNER)
        assert sid not in manager.browsers
        await manager.send(OWNER, sid, SendMessage(request_id=uuid4(), content="Continue on this site",
                                                  consent_to_send=True))
        await manager.tasks[sid]
        assert session_data(manager, sid)["_turn_outcomes"]
        assert all(item["ok"] for item in session_data(manager, sid)["_turn_outcomes"].values())
        assert manager.browsers[sid] is not scopes[0]
        assert manager.browsers[sid].automation_request_id is None

    asyncio.run(scenario())
    assert worker.await_count == 3


@pytest.mark.parametrize("lane", ["general", "interaction", "proxy_general", "proxy_interaction"])
def test_browser_open_selects_only_site_interaction_lane_without_fallback(ctx, monkeypatch, lane):
    with ctx[1].session_factory() as db:
        db.get(Site, ctx[2]).lane_policy = lane
        db.commit()
    grant(ctx, live_access=True)
    failed_job(ctx)
    tools, _ = tools_for(ctx)
    expected = "proxy_interaction" if lane.startswith("proxy_") else "interaction"

    async def worker(path, payload):
        assert path == "open" and payload["lane_key"] == expected
        assert payload["same_origin_only"] is True
        assert payload["trusted_origins"] == [] and payload["url"] == tools.target["base_url"]
        return {"session_id": payload["session_id"], "lane_key": expected,
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()}

    monkeypatch.setattr(tools.manager, "_worker", AsyncMock(side_effect=worker))
    assert asyncio.run(tools.dispatch("browser_open", {}))["lane_key"] == expected
    assert tools.target["run_lane_policy"] == lane


def test_proxy_lane_failure_never_falls_back(ctx, monkeypatch):
    from app.services.assistant import AssistantError

    grant(ctx, live_access=True)
    failed_job(ctx)
    tools, _ = tools_for(ctx)
    lanes = []

    async def worker(path, payload):
        if path == "open":
            lanes.append(payload["lane_key"])
            raise AssistantError("browser_unavailable", reason="browser_lane_unavailable")
        assert path.endswith("/close")
        return {}

    monkeypatch.setattr(tools.manager, "_worker", AsyncMock(side_effect=worker))
    output = asyncio.run(tools.dispatch("browser_open", {}))
    assert output["reason"] == "browser_lane_unavailable"
    assert output["lane_key"] == "proxy_interaction" and lanes == ["proxy_interaction"]
    assert tools.session_id not in tools.manager.browsers


def test_human_followup_reopens_with_strict_origin_after_analysis_and_policy_disabled(ctx, monkeypatch):
    grant(ctx, live_access=True)
    failed_job(ctx)
    tools, _ = tools_for(ctx)
    with ctx[1].session_factory() as db:
        row = db.get(AssistantSession, tools.session_id)
        data = ctx[1]._decrypt(row)
        remove_takeover(data)
        ctx[1]._encrypt(row, data)
        db.commit()
        policy = TakeoverSettingsService(db).view()
        TakeoverSettingsService(db).update(TakeoverSettingsUpdate(expected_revision=policy.revision,
            confirm=True, values=policy.values.model_copy(update={"enabled": False})), OWNER)
    ctx[1]._active_turns[tools.session_id] = {"id": "human"}
    target = session_data(ctx[1], tools.session_id)["target"]
    assert target["workflow_consent"]

    async def opened(path, payload):
        assert path == "open" and payload["same_origin_only"] is True
        assert "wait_for_lane" not in payload
        return {"session_id": payload["session_id"], "lane_key": payload["lane_key"],
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()}

    worker = AsyncMock(side_effect=opened)
    monkeypatch.setattr(ctx[1], "_worker", worker)
    human = AssistantTools(ctx[1], OWNER, tools.session_id, target, turn=ctx[1]._active_turns[tools.session_id])
    assert asyncio.run(human.dispatch("browser_open", {}))["ok"]
    assert ctx[1].browsers[tools.session_id].same_origin_only
    worker.assert_awaited_once()


@pytest.mark.parametrize("change", ["same", "lane", "origin", "no_live", "trust"])
def test_daily_reactivation_refreshes_bindings_without_retaining_auto_page(ctx, monkeypatch, change):
    grant(ctx, live_access=True)
    takeover, manager, _ = ctx
    calls, scopes, old_revision = [], [], []

    async def inspect(owner, sid, config):
        target = session_data(manager, sid)["target"]
        if not scopes:
            tools = AssistantTools(manager, owner, sid, target)
            scopes.append(scope_for(tools))
            if change == "trust":
                scopes[0].trusted_origins = ("https://other.example",)
            old_revision.append(target["_credential_binding"]["revision"])
        else:
            calls.append(sid)
            assert sid not in manager.browsers
            assert target["_credential_binding"]["revision"] == old_revision[0] + 1
            assert target["_workflow_binding"] == target["_credential_binding"]

    install_model(monkeypatch, manager, inspect)
    worker = AsyncMock(return_value={})
    monkeypatch.setattr(manager, "_worker", worker)

    async def scenario():
        action_for(ctx)
        await takeover.process_one()
        with manager.session_factory() as db:
            site = db.get(Site, ctx[2])
            site.config_revision += 1
            if change == "lane":
                site.lane_policy = "general"
            elif change == "origin":
                site.base_url = "https://replacement.example/"
            db.commit()
            if change == "no_live":
                policy = TakeoverSettingsService(db).view()
                TakeoverSettingsService(db).update(TakeoverSettingsUpdate(expected_revision=policy.revision,
                    confirm=True, values=policy.values.model_copy(update={"live_access": False})), OWNER)
            elif change == "origin":
                policy = TakeoverSettingsService(db).view()
                TakeoverSettingsService(db).update(TakeoverSettingsUpdate(expected_revision=policy.revision,
                    confirm=True, values=policy.values), OWNER)
        action_for(ctx)
        await takeover.process_one()

    asyncio.run(scenario())
    assert len(calls) == 1
    worker.assert_awaited_once_with(f"{scopes[0].session_id}/close", {"token": scopes[0].token})


@pytest.mark.parametrize("error", [TimeoutError, asyncio.CancelledError])
def test_failed_or_cancelled_live_turn_closes_browser_but_preserves_site_grant(ctx, monkeypatch, error):
    grant(ctx, live_access=True)
    takeover, manager, _ = ctx
    scopes = []

    async def fail(owner, sid, config):
        tools = AssistantTools(manager, owner, sid, session_data(manager, sid)["target"])
        scopes.append(scope_for(tools))
        raise error

    install_model(monkeypatch, manager, fail)
    worker = AsyncMock(return_value={})
    monkeypatch.setattr(manager, "_worker", worker)
    action = action_for(ctx)
    asyncio.run(takeover.process_one())
    from app.models.assistant_takeover import AssistantTakeoverAction
    with manager.session_factory() as db:
        sid = db.get(AssistantTakeoverAction, action.id).session_id
    assert sid not in manager.browsers
    target = session_data(manager, sid)["target"]
    assert target["_takeover_site_grant"] and target["execution_consent"] and target["workflow_consent"]
    worker.assert_awaited_once_with(f"{scopes[0].session_id}/close", {"token": scopes[0].token})
