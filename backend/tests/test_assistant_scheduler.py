from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.models.assistant import AssistantSession
from app.models.assistant_workflow import AssistantWorkflowAction
from app.models.setting import AppSetting
from app.schemas.assistant_scheduler import UpdateScheduler
from app.schemas.scheduler import SchedulerSettingsUpdate
from app.services.assistant_scheduler import (
    apply_scheduler_update, dispatch, prepare_scheduler_update, tool_definitions,
)
from app.services.assistant_tools import AssistantTools
from app.services.assistant_workflows import AssistantWorkflows, WorkflowDenied
from app.services.scheduler_settings import (
    CRON_KEY, REVISION_KEY, SchedulerLockedError, SchedulerSettingsService,
    SchedulerValidationError,
)
from tests.test_assistant_workflows import OWNER, context  # noqa: F401


@pytest.fixture
def tools(context):
    manager, sid = context
    with manager.session_factory() as db:
        db.get(AssistantSession, sid).status = "running"
        db.commit()
    return AssistantTools(manager, OWNER, sid, None, config={"_assistant_settings": {
        "auto_authorize_workflow": True, "auto_authorize_execution": True}})


def request(patch=None):
    return {"operation_id": str(uuid4()), "patch": patch or {"enabled": False}}


def prepare(tools, arguments=None):
    with tools.manager.session_factory() as db:
        return prepare_scheduler_update(db, tools, UpdateScheduler.model_validate(
            arguments or request()), {})


def apply(tools, private, *, commit=True):
    with tools.manager.session_factory() as db:
        row = tools.manager._owned(db, tools.owner, tools.session_id)
        data = tools.manager._decrypt(row)
        result = apply_scheduler_update(db, tools, data, private)
        tools.manager._encrypt(row, data)
        if commit:
            db.commit()
        return result


def call(tools, name, args):
    return asyncio.run(dispatch(tools, name, args))


def test_read_aliases_and_schema_are_global_and_read_only(tools):
    for name in ("read_scheduler_settings", "get_scheduler_settings"):
        view = call(tools, name, {})
        assert view["ok"] and view["scope"] == "global_scheduler"
        assert view["settings"]["revision"] == 0 and view["settings"]["timezone"]
        assert call(tools, name, {"site_id": 1})["code"] == "invalid_arguments"
    with tools.manager.session_factory() as db:
        assert list(db.scalars(select(AppSetting))) == []
    definitions = {d["function"]["name"]: d["function"] for d in tool_definitions()}
    assert set(definitions) == {"get_scheduler_settings", "update_scheduler_settings"}
    assert definitions["get_scheduler_settings"]["parameters"]["properties"] == {}
    from jsonschema import Draft202012Validator
    for definition in definitions.values():
        Draft202012Validator.check_schema(definition["parameters"])


@pytest.mark.parametrize("patch", [{}, {"timezone": "UTC"}, {"enabled": None}, {"enabled": 1},
    {"enabled": "false"}, {"mode": "daily"}, {"ranges": [1]}, {"interval_seconds": True},
    {"interval_seconds": 59}, {"interval_seconds": 2592001}, {"ranges": ["x"] * 21}])
def test_strict_sparse_schema(tools, patch):
    arguments = {"operation_id": str(uuid4()), "patch": patch}
    with pytest.raises(ValidationError):
        UpdateScheduler.model_validate(arguments)
    assert call(tools, "update_scheduler_settings", arguments)["code"] == "invalid_arguments"


def test_review_chinese_copy_explains_global_scope_timing_and_timezone(tools):
    public, _, _ = prepare(tools, request({
        "enabled": False, "mode": "random", "cron": "0 11 * * *",
        "interval_seconds": 7200, "ranges": ["09:00-10:00"],
    }))
    assert public["title"] == "\u786e\u8ba4\u5168\u5c40\u8c03\u5ea6\u8bbe\u7f6e\u53d8\u66f4"
    assert public["description"] == (
        "\u4ec5\u6388\u6743\u4e0b\u5217\u7cbe\u786e\u7684\u5168\u5c40\u8c03\u5ea6\u53d8\u66f4\u3002\u5168\u5c40\u81ea\u52a8\u6388\u6743\u4e0d\u9002\u7528\uff0c\u62d2\u7edd\u6216\u8d85\u65f6\u4e0d\u4f1a\u4fee\u6539\u8bbe\u7f6e\u3002")
    review = public["review"]
    assert review["scope"] == "global_scheduler"
    assert {change["field"]: change["label"] for change in review["changes"]} == {
        "enabled": "\u5168\u5c40\u8c03\u5ea6\u5f00\u5173", "mode": "\u8c03\u5ea6\u6a21\u5f0f",
        "cron": "cron \u8868\u8fbe\u5f0f", "interval_seconds": "\u95f4\u9694\u65f6\u95f4\uff08\u79d2\uff09",
        "ranges": "\u968f\u673a\u65f6\u95f4\u8303\u56f4",
    }
    assert review["warnings"] == [
        "\u672c\u6b21\u53d8\u66f4\u4ec5\u5f71\u54cd\u5168\u5c40\u8c03\u5ea6\uff0c\u4e0d\u4f1a\u4fee\u6539\u4efb\u4f55\u7ad9\u70b9\u8bbe\u7f6e\u3002",
        "\u4e0b\u6b21\u8fd0\u884c\u65f6\u95f4\u5c06\u91cd\u65b0\u8ba1\u7b97\u3002",
        "\u65f6\u533a\u7531\u73af\u5883\u53d8\u91cf\u7ba1\u7406\uff0c\u65e0\u6cd5\u5728\u6b64\u4fee\u6539\u3002",
    ]


def test_exact_global_review_receipt_replay_and_sparse_persistence(tools):
    arguments = request({"mode": "random", "ranges": ["9:00-10:00", "09:00-10:00"]})
    public, private, subject = prepare(tools, arguments)
    assert public["purpose"] == "scheduler_config" and public["kind"] == "authorization"
    review = public["review"]
    assert set(review) == {"scope", "timezone", "revision", "changes", "warnings"}
    assert review["scope"] == "global_scheduler" and review["revision"] == 0
    changes = {c["field"]: c for c in review["changes"]}
    assert set(changes) == {"mode", "ranges"}
    assert changes["ranges"]["after"] == ["09:00-10:00"]
    assert subject.startswith("scheduler_config:") and review["warnings"]
    result = apply(tools, private)
    assert result["ok"] and result["revision"] == 1
    assert "target" not in result and tools.target is None
    with tools.manager.session_factory() as db:
        action = db.scalar(select(AssistantWorkflowAction))
        assert action.kind == "scheduler_config" and action.owner == tools.owner
        assert action.site_id is None and action.state == "complete"
        saved = AssistantWorkflows(tools.manager, tools.owner, tools.session_id, None)._open(action)
        assert saved["binding"] == private["binding"] and saved["owner"] == tools.owner
        assert db.get(AppSetting, CRON_KEY) is None  # Unpatched defaults are not persisted.
        assert b"operation_id" not in action.ciphertext
    assert call(tools, "update_scheduler_settings", arguments) == {**result, "replayed": True}
    changed = deepcopy(arguments)
    changed["patch"]["mode"] = "interval"
    assert call(tools, "update_scheduler_settings", changed)["code"] == "scheduler_operation_conflict"
    assert apply(tools, private) == {**result, "replayed": True}
    with tools.manager.session_factory() as db:
        assert SchedulerSettingsService(db, tools.manager.settings).view().revision == 1


@pytest.mark.parametrize("change", ["revision", "raw", "environment", "timezone", "owner", "disabled"])
def test_stale_review_revalidation_writes_nothing(tools, change):
    _, private, _ = prepare(tools)
    if change in {"revision", "raw"}:
        with tools.manager.session_factory() as db:
            if change == "revision":
                SchedulerSettingsService(db, tools.manager.settings).update(
                    SchedulerSettingsUpdate(mode="interval", interval_seconds=900))
            else:
                db.add(AppSetting(key=CRON_KEY, value_json=json.dumps("0 11 * * *")))
                db.commit()
    elif change == "environment":
        tools.manager.settings.scheduler_enabled = True
    elif change == "timezone":
        tools.manager.settings.scheduler_timezone = (
            "UTC" if tools.manager.settings.scheduler_timezone != "UTC" else "Asia/Shanghai")
    elif change == "disabled":
        tools.manager.settings.config_ai_enabled = False
    else:
        private["owner"] = "someone-else"
    with pytest.raises((WorkflowDenied, SchedulerLockedError)):
        apply(tools, private)
    with tools.manager.session_factory() as db:
        assert db.scalar(select(AssistantWorkflowAction)) is None
        assert SchedulerSettingsService(db, tools.manager.settings).view().enabled


def test_dynamic_next_run_does_not_invalidate_review_and_replays_survive_later_edit(tools):
    arguments = request()
    _, private, _ = prepare(tools, arguments)
    with tools.manager.session_factory() as db:
        SchedulerSettingsService(db, tools.manager.settings).set_next_run(datetime.now(timezone.utc))
    result = apply(tools, private)
    with tools.manager.session_factory() as db:
        SchedulerSettingsService(db, tools.manager.settings).update(
            SchedulerSettingsUpdate(enabled=True, mode="interval", interval_seconds=900))
    assert call(tools, "update_scheduler_settings", arguments) == {**result, "replayed": True}


def test_update_always_uses_shared_delayed_interaction(tools, monkeypatch):
    seen = []
    async def interaction(actual_tools, args):
        assert actual_tools is tools
        seen.append(args)
        return {"ok": False, "code": "interaction_denied", "terminal": True}
    monkeypatch.setattr("app.services.assistant_interactions.request_interaction", interaction)
    arguments = request()
    assert call(tools, "update_scheduler_settings", arguments)["code"] == "interaction_denied"
    assert seen == [{"purpose": "scheduler_config", **arguments}]
    with tools.manager.session_factory() as db:
        assert db.get(AppSetting, REVISION_KEY) is None
        assert db.scalar(select(AssistantWorkflowAction)) is None


def test_noop_and_terminal_operation_replays_do_not_write(tools):
    with pytest.raises(WorkflowDenied, match="scheduler_no_changes"):
        prepare(tools, request({"enabled": True}))
    arguments = request()
    with tools.manager.session_factory() as db:
        row = db.get(AssistantSession, tools.session_id)
        data = tools.manager._decrypt(row)
        data["_interaction_decisions"] = {"denied": {
            "purpose": "scheduler_config", "arguments": {"request": arguments},
            "result": {"ok": False, "code": "interaction_denied", "terminal": True}}}
        tools.manager._encrypt(row, data)
        db.commit()
    assert call(tools, "update_scheduler_settings", arguments)["code"] == "interaction_denied"
    assert call(tools, "update_scheduler_settings", {
        **arguments, "patch": {"enabled": True}})["code"] == "scheduler_operation_conflict"


@pytest.mark.parametrize("patch", [{"cron": "* * * * * *"}, {"cron": "0 0 31 2 *"},
    {"mode": "random"}, {"ranges": ["23:00-01:00"]}, {"ranges": ["09:00:00-10:00"]}])
def test_invalid_schedule_never_reaches_review_or_reserves_receipt(tools, patch):
    with pytest.raises(SchedulerValidationError):
        prepare(tools, request(patch))
    with tools.manager.session_factory() as db:
        assert list(db.scalars(select(AppSetting))) == []
        assert db.scalar(select(AssistantWorkflowAction)) is None


def test_receipt_owner_isolation(tools):
    arguments = request()
    _, private, _ = prepare(tools, arguments)
    apply(tools, private)
    tools.owner = "other-owner"
    assert not call(tools, "update_scheduler_settings", arguments)["ok"]
    assert not call(tools, "get_scheduler_settings", {})["ok"]


def test_receipt_and_setting_compose_atomically_and_failure_rolls_back(tools, monkeypatch):
    _, private, _ = prepare(tools)
    apply(tools, private, commit=False)
    with tools.manager.session_factory() as db:
        assert db.get(AppSetting, REVISION_KEY) is None
        assert db.scalar(select(AssistantWorkflowAction)) is None
    seal = AssistantWorkflows._seal
    def fail(self, action, value):
        if value.get("result"):
            raise RuntimeError("finalization failed")
        return seal(self, action, value)
    monkeypatch.setattr(AssistantWorkflows, "_seal", fail)
    with pytest.raises(RuntimeError, match="finalization failed"):
        apply(tools, private)
    with tools.manager.session_factory() as db:
        assert db.get(AppSetting, REVISION_KEY) is None
        assert db.scalar(select(AssistantWorkflowAction)) is None
