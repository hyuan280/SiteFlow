from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models.assistant import AssistantSession
from app.models.assistant_workflow import AssistantWorkflowAction
from app.models.execution import Run, SiteJob
from app.models.workflow import Site, WorkflowHistory, WorkflowStep
from app.schemas.assistant import InteractionResponse
from app.services.assistant_interactions import LEDGER, PENDING, respond_interaction
from app.services.assistant_site_config import unsafe_site_config
from tests.test_assistant_workflows import (  # noqa: F401
    OWNER, DRAFT, context, configure, make_site, call, import_draft,
)
from tests.test_assistant_api import (  # noqa: F401
    ROOT, assistant_client, provider, create, target, send, tool, text, wait,
)


def state(tools):
    with tools.manager.session_factory() as db:
        return tools.manager._decrypt(db.get(AssistantSession, tools.session_id))


async def pending(tools, task):
    for _ in range(500):
        item = state(tools).get(PENDING)
        if item:
            return item
        if task.done():
            pytest.fail(str(task.result()))
        await asyncio.sleep(0.001)
    pytest.fail("No pending review")


async def reviewed(tools, args, *, decision="approve", before=None):
    task = asyncio.create_task(tools.dispatch("update_site_config", args))
    item = await pending(tools, task)
    if before:
        before(item)
    await respond_interaction(tools.manager, tools.owner, tools.session_id, item["id"],
                              InteractionResponse(decision=decision))
    return await task, item


def args(patch=None, **extra):
    return {"operation_id": str(uuid4()), **({"patch": patch} if patch is not None else {}), **extra}


def test_atomic_patch_exact_review_replay_and_no_workflow_rewrites(context):
    site = make_site(context, notes="private old notes", trusted_urls=["^https://private.example/?token=old"],
                     checkin_after="14:30", steps=DRAFT["steps"])
    tools = configure(context, workflow_site_id=site.id, site_id=site.id, workflow_consent=False)
    manager = tools.manager
    with manager.session_factory() as db:
        step = db.scalar(select(WorkflowStep).where(WorkflowStep.owner_id == site.id))
        original = (step.id, step.config_json)
        row = db.get(AssistantSession, tools.session_id)
        data = manager._decrypt(row)
        data.update(_workflow_proposal_id=12, _context_summary={"summary": "old", "boundary_id": 1})
        manager._encrypt(row, data)
        db.commit()
    request = args({"name": "Renamed", "notes": "Reviewed note", "checkin_after": None,
                    "trusted_urls": [r"^https://auth\.example\.com(?:[/?#].*)?$"],
                    "lane_policy": "interaction"})

    def inspect(item):
        assert item["purpose"] == "site_config" and item["kind"] == "authorization"
        review = item["review"]
        assert set(review) == {"name", "base_url", "site_id", "changes", "warnings"}
        assert "private old notes" not in json.dumps(review) and "token=old" not in json.dumps(review)
        changes = {c["field"]: c for c in review["changes"]}
        assert changes["checkin_after"]["after"] is None
        assert changes["trusted_urls"]["after"] == request["patch"]["trusted_urls"]
        assert review["warnings"]
        with manager.session_factory() as db:
            assert db.get(Site, site.id).config_revision == 1
            assert db.scalar(select(func.count()).select_from(AssistantWorkflowAction)) == 0

    result, _ = asyncio.run(reviewed(tools, request, before=inspect))
    assert result["ok"] and result["persisted"] and result["config_revision"] == 2
    assert result["name"] == "Renamed" and result["changed_fields"] == sorted(request["patch"])
    assert call(tools, "update_site_config", request) == {**result, "replayed": True}
    changed = deepcopy(request)
    changed["patch"]["name"] = "Different"
    assert call(tools, "update_site_config", changed)["code"] == "site_config_operation_conflict"
    after = state(tools)
    assert after["target"]["name"] == "Renamed" and after["target"]["run_lane_policy"] == "interaction"
    assert after["target"]["_credential_binding"] == after["target"]["_workflow_binding"]
    assert "_workflow_proposal_id" not in after and "_context_summary" not in after
    assert not after["target"]["workflow_consent"]
    assert manager._close_browser.await_count == 2  # Initial target plus committed edit.
    with manager.session_factory() as db:
        current = db.get(Site, site.id)
        assert current.checkin_after is None and current.notes == "Reviewed note"
        step = db.get(WorkflowStep, original[0])
        assert step.config_json == original[1]
        history = list(db.scalars(select(WorkflowHistory).order_by(WorkflowHistory.revision)))
        assert len(history) == 2 and history[-1].created_by == "assistant"
        action = db.scalar(select(AssistantWorkflowAction))
        assert action.kind == "site_config" and action.state == "complete" and action.owner == OWNER
        assert b"Reviewed note" not in action.ciphertext


@pytest.mark.parametrize("policy", ["auto", "general", "interaction", "proxy_general", "proxy_interaction"])
def test_all_five_persisted_lanes(context, policy):
    site = make_site(context, lane_policy="interaction" if policy == "auto" else "auto")
    tools = configure(context, workflow_site_id=site.id)
    result, _ = asyncio.run(reviewed(tools, args({"lane_policy": policy})))
    assert result["ok"] and tools.target["run_lane_policy"] == policy


def test_observation_before_import_is_separate_and_does_not_create_site(context):
    tools = configure(context, proxy=False, workflow_consent=False)
    result, item = asyncio.run(reviewed(tools, args({}, observation={
        "proxy": True, "trusted_origins": ["https://sso.example"]})))
    assert result["ok"] and not result["persisted"]
    assert result["name"] == tools.target["name"] and result["changed_fields"] == []
    assert {c["field"] for c in item["review"]["changes"]} == {
        "observation.proxy", "observation.trusted_origins"}
    assert tools.target["proxy"] and tools.target["trusted_origins"] == ["https://sso.example"]
    assert tools.target["run_lane_policy"] == "auto"
    with tools.manager.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Site)) == 0
    assert call(tools, "update_site_config", item["arguments"]["request"]) == {**result, "replayed": True}


def test_public_regex_and_nonsecret_notes_are_not_rewritten(context):
    site = make_site(context)
    tools = configure(context, workflow_site_id=site.id)
    patch = {"notes": "Reminder? Check after lunch # daily", "trusted_urls": [
        r"^https://auth\.example\.com/login\?tab=foo(?:#public)?$",
        r"^https?://a\.example/(?:login)?\?tab=public(?:#section)?$",
        r"^http(s)?://a\.example/(?:[/?#].*)?$"]}
    result, item = asyncio.run(reviewed(tools, args(patch)))
    assert result["ok"]
    assert {c["field"]: c["after"] for c in item["review"]["changes"]} == patch
    with tools.manager.session_factory() as db:
        saved = db.get(Site, site.id)
        assert saved.notes == patch["notes"]
        assert json.loads(saved.trusted_urls_json) == patch["trusted_urls"]


def test_new_operation_cannot_claim_old_completed_subject_success(context):
    site = make_site(context)
    tools = configure(context, workflow_site_id=site.id)
    first = args({"notes": "first"})
    assert asyncio.run(reviewed(tools, first))[0]["ok"]
    assert asyncio.run(reviewed(tools, args({"notes": "second"})))[0]["ok"]
    result = call(tools, "update_site_config", args({"notes": "first"}))
    assert not result["ok"] and result["terminal"] and result["code"] == "interaction_already_requested"
    with tools.manager.session_factory() as db:
        assert db.get(Site, site.id).notes == "second"
        assert db.get(Site, site.id).config_revision == 3


def test_shared_patch_schema_retains_assistant_bounds_and_observation_only():
    from pydantic import ValidationError
    from app.schemas.assistant_site_config import SiteConfigPatch, UpdateSiteConfig
    from app.schemas.workflow import SiteSettingsPatch

    assert issubclass(SiteConfigPatch, SiteSettingsPatch)
    assert SiteConfigPatch().model_dump(exclude_unset=True) == {}
    assert UpdateSiteConfig.model_validate(args(observation={"proxy": True})).patch.model_fields_set == set()
    for patch in ({"tags": ["x"] * 51}, {"tags": ["x" * 129]}, {"tags": [1]},
                  {"trusted_urls": [" ^https://example.com/"]}, {"notes": None}):
        with pytest.raises(ValidationError):
            SiteConfigPatch.model_validate(patch)
    assert SiteConfigPatch(checkin_after=None).model_dump(exclude_unset=True) == {"checkin_after": None}


def test_destination_never_defaults_to_credential_source_and_defaults_never_bypass_review(context):
    source = make_site(context)
    tools = configure(context, site_id=source.id, workflow_consent=True)
    assert call(tools, "update_site_config", args({"notes": "desired"}))["code"] == "workflow_binding_changed"
    from app.schemas.workflow import SiteWrite
    from app.services.workflows import WorkflowService

    with tools.manager.session_factory() as db:
        destination = WorkflowService(db, tools.manager.settings, tools.manager.crypto).create_site(
            SiteWrite(name="Destination", base_url=tools.target["base_url"]))
    with tools.manager.session_factory() as db:
        db.get(AssistantSession, tools.session_id).status = "idle"
        db.commit()
    tools = configure(context, site_id=source.id, workflow_site_id=destination.id, workflow_consent=True)
    tools.config["_assistant_settings"] = {"auto_authorize_workflow": True}
    result, _ = asyncio.run(reviewed(tools, args({"notes": "desired"}), decision="deny"))
    assert result["code"] == "interaction_denied"
    with tools.manager.session_factory() as db:
        assert db.get(Site, source.id).config_revision == db.get(Site, destination.id).config_revision == 1


@pytest.mark.parametrize("patch", [
    {"site_id": 1}, {"base_url": "https://other.example/"}, {"steps": []}, {"enabled": 1},
    {"enabled": "true"}, {"enabled": None}, {"notes": None}, {"resign_max_days": True},
    {"resign_max_days": 366}, {"checkin_after": "24:01"}, {"timezone": "bad/timezone"},
    {"tags": [1]}, {"name": " "}, {"notes": "x" * 4001}, {"trusted_urls": ["["]},
    {"enabled": True, "notes": "also"},
])
def test_closed_strict_bounded_patch(context, patch):
    tools = configure(context)
    assert call(tools, "update_site_config", args(patch))["code"] == "invalid_arguments"
    assert PENDING not in state(tools)


@pytest.mark.parametrize("extra", [{}, {"patch": {}}, {"observation": {}}, {"site_id": 1},
    {"patch": {"enabled": True}, "observation": {"proxy": True}},
    {"observation": {"proxy": 1}}, {"observation": {"trusted_origins": ["https://sso.example/path"]}}])
def test_invalid_request_shapes(context, extra):
    tools = configure(context)
    assert call(tools, "update_site_config", args(**extra))["code"] == "invalid_arguments"


@pytest.mark.parametrize("value", ["password=private", "Bearer private", "https://a.example/?token=private",
    r"^https://a\.example/\?t%6fken=private$", "https://user:private@a.example/", "[REDACTED]",
    r"^.*\?session=private$"])
def test_secret_inputs_never_become_reviews(context, value):
    tools = configure(context)
    assert unsafe_site_config(value)
    assert call(tools, "update_site_config", args({"notes": value}))["code"] == "secret_input"
    assert PENDING not in state(tools)


@pytest.mark.parametrize("pattern", [
    r"^https?://a\.example/\?session=private$",
    r"^https?://user:private@a\.example/$",
    r"^http(s)?://user:private@a\.example/$",
    r"^https?://a\.example/(?:login)?\?s%65ssion=private$",
    r"^https?://a\.example/(?:login)?\?s%2565ssion=private$",
    r"^https?://a\.example/(?:login)?#st%61te=private$",
    r"^https?://a\.example/(?:login)?%3Fsession%3Dprivate$",
    r"^https?://a\.example/(?:login)?\?tab=public#route?state=private$",
    r"^https?://a\.example/\?tab=public&session=private$",
])
def test_regex_privacy_rejects_optional_scheme_secrets_before_review(context, pattern):
    tools = configure(context)
    request = args({"trusted_urls": [pattern]})
    assert unsafe_site_config(request)
    assert call(tools, "update_site_config", request) == {"ok": False, "code": "secret_input"}
    assert PENDING not in state(tools) and not state(tools).get(LEDGER)


@pytest.mark.parametrize("pattern", [
    r"^https?://a\.example/\?session=private$",
    r"^https?://user:private@a\.example/$",
])
def test_regex_secrets_never_enter_canonical_history(assistant_client, provider, pattern):
    from app.models.assistant import AssistantMessage

    client = assistant_client
    sid = create(client)
    target(client, sid)
    provider[0].append(tool("update_site_config", args({"trusted_urls": [pattern]}), "private-pattern"))
    send(client, sid, content="Update credential trust")
    detail = wait(client, sid)
    assert not detail.get("pending_interaction")
    manager = client.app.state.assistant_manager
    with manager.session_factory() as db:
        records = [manager._decrypt(row) for row in db.scalars(select(AssistantMessage).where(
            AssistantMessage.session_id == sid))]
        assert "private" not in json.dumps(records)
        assert not any(record.get("tool_name") == "update_site_config" for record in records)


@pytest.mark.parametrize("observation_only", [False, True])
@pytest.mark.parametrize("rebind", [False, True])
def test_receipt_rejects_recreated_destination_even_after_rebinding(context, observation_only, rebind):
    from app.services.assistant_workflows import workflow_binding

    site = make_site(context)
    tools = configure(context, workflow_site_id=site.id, proxy=False)
    request = args(observation={"proxy": True}) if observation_only else args({"notes": "desired"})
    assert asyncio.run(reviewed(tools, request))[0]["ok"]
    with tools.manager.session_factory() as db:
        original = db.get(Site, site.id)
        old_key = original.history_key
        db.delete(original)
        db.flush()
        replacement = Site(id=site.id, name="Replacement", base_url=site.base_url)
        db.add(replacement)
        db.flush()
        assert replacement.history_key != old_key
        if rebind:
            row = db.get(AssistantSession, tools.session_id)
            data = tools.manager._decrypt(row)
            data["target"]["_workflow_binding"] = workflow_binding(replacement)
            tools.manager._encrypt(row, data)
        db.commit()
    assert call(tools, "update_site_config", request) == {"ok": False, "code": "workflow_binding_changed"}


@pytest.mark.parametrize("observation_only", [False, True])
@pytest.mark.parametrize("stale_revision", [False, True])
def test_receipt_same_site_later_revision_still_replays(context, observation_only, stale_revision):
    site = make_site(context)
    tools = configure(context, workflow_site_id=site.id, proxy=False)
    request = args(observation={"proxy": True}) if observation_only else args({"notes": "first"})
    result, _ = asyncio.run(reviewed(tools, request))
    assert result["ok"]
    binding = deepcopy(tools.target["_workflow_binding"])
    assert asyncio.run(reviewed(tools, args({"notes": "later"})))[0]["ok"]
    if stale_revision:
        with tools.manager.session_factory() as db:
            row = db.get(AssistantSession, tools.session_id)
            data = tools.manager._decrypt(row)
            data["target"]["_workflow_binding"] = binding
            tools.manager._encrypt(row, data)
            db.commit()
    assert call(tools, "update_site_config", request) == {**result, "replayed": True}


def test_receipt_missing_durable_identity_fails_closed(context):
    from app.services.assistant_workflows import AssistantWorkflows

    site = make_site(context)
    tools = configure(context, workflow_site_id=site.id)
    request = args({"notes": "desired"})
    assert asyncio.run(reviewed(tools, request))[0]["ok"]
    with tools.manager.session_factory() as db:
        workflows = AssistantWorkflows(tools.manager, tools.owner, tools.session_id, tools.target)
        action = db.scalar(select(AssistantWorkflowAction).where(AssistantWorkflowAction.kind == "site_config"))
        receipt = workflows._open(action)
        receipt["destination"].pop("history_key", None)
        workflows._seal(action, receipt)
        db.commit()
    assert call(tools, "update_site_config", request) == {"ok": False, "code": "workflow_binding_changed"}


@pytest.mark.parametrize("decision", ["deny", "timeout", "cancel"])
def test_terminal_no_write_and_no_new_id_reprompt(context, monkeypatch, decision):
    site = make_site(context)
    tools = configure(context, workflow_site_id=site.id)
    request = args({"notes": "desired"})
    if decision == "timeout":
        monkeypatch.setattr("app.services.assistant_interactions.INTERACTION_TIMEOUT", 0.01)
        result = call(tools, "update_site_config", request)
    elif decision == "deny":
        result, _ = asyncio.run(reviewed(tools, request, decision="deny"))
    else:
        async def cancel():
            task = asyncio.create_task(tools.dispatch("update_site_config", request))
            await pending(tools, task)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        asyncio.run(cancel())
        result = call(tools, "update_site_config", request)
    assert not result["ok"] and result["terminal"]
    assert call(tools, "update_site_config", args({"notes": "desired"})) == result
    with tools.manager.session_factory() as db:
        assert db.get(Site, site.id).config_revision == 1
        assert db.scalar(select(func.count()).select_from(AssistantWorkflowAction)) == 0


@pytest.mark.parametrize("change", ["revision", "unversioned", "active", "feature"])
def test_apply_revalidates_before_writing(context, change):
    site = make_site(context)
    tools = configure(context, workflow_site_id=site.id)

    def concurrent(_):
        if change == "feature":
            tools.manager.settings.config_ai_enabled = False
            return
        with tools.manager.session_factory() as db:
            current = db.get(Site, site.id)
            if change == "revision":
                current.config_revision += 1
            elif change == "unversioned":
                current.notes = "concurrent"
            else:
                run = Run(created_by="admin")
                db.add(run)
                db.flush()
                db.add(SiteJob(run_id=run.id, site_id=site.id, site_name="Saved", lane="general", status="running"))
            db.commit()
    result, _ = asyncio.run(reviewed(tools, args({"notes": "desired"}), before=concurrent))
    assert not result["ok"]
    assert result["code"] in {"workflow_binding_changed", "site_run_active", "workflow_execution_disabled"}
    with tools.manager.session_factory() as db:
        assert db.get(Site, site.id).notes != "desired"
        assert db.scalar(select(func.count()).select_from(AssistantWorkflowAction)) == 0


def test_finalization_failure_rolls_back_everything(context, monkeypatch):
    from app.services.assistant_workflows import AssistantWorkflows

    site = make_site(context)
    tools = configure(context, workflow_site_id=site.id)
    tools.manager._close_browser.reset_mock()
    original = AssistantWorkflows._seal

    def fail(self, action, value):
        if value.get("result"):
            raise RuntimeError("private error")
        return original(self, action, value)
    monkeypatch.setattr(AssistantWorkflows, "_seal", fail)
    before = state(tools)["target"]
    result, _ = asyncio.run(reviewed(tools, args({"name": "Renamed"})))
    assert result == {"ok": False, "code": "site_config_unavailable"}
    assert state(tools)["target"] == before
    with tools.manager.session_factory() as db:
        assert db.get(Site, site.id).config_revision == 1
        assert db.scalar(select(func.count()).select_from(WorkflowHistory)) == 1
        assert db.scalar(select(func.count()).select_from(AssistantWorkflowAction)) == 0
    tools.manager._close_browser.assert_not_awaited()


def finish_run(tools, run_id):
    with tools.manager.session_factory() as db:
        run = db.get(Run, run_id)
        run.status, run.finished_at = "success", datetime.now(timezone.utc)
        job = db.scalar(select(SiteJob).where(SiteJob.run_id == run_id))
        job.status, job.finished_at = "success", datetime.now(timezone.utc)
        db.commit()


def test_enable_requires_current_full_success_and_exact_review(context):
    tools = configure(context)
    imported, _ = import_draft(tools)
    assert call(tools, "update_site_config", args({"enabled": True}))["code"] == "enable_success_required"
    run_id = call(tools, "run_site")["run_id"]
    finish_run(tools, run_id)
    request = args({"enabled": True})
    result, item = asyncio.run(reviewed(tools, request))
    assert result["ok"] and item["review"]["successful_run_id"] == run_id
    assert call(tools, "update_site_config", request)["replayed"]
    with tools.manager.session_factory() as db:
        assert db.get(Site, imported["site_id"]).enabled


def test_settings_edit_invalidates_old_enable_evidence(context):
    tools = configure(context)
    import_draft(tools)
    run_id = call(tools, "run_site")["run_id"]
    finish_run(tools, run_id)
    assert asyncio.run(reviewed(tools, args({"checkin_after": "12:00"})))[0]["ok"]
    assert call(tools, "update_site_config", args({"enabled": True}, successful_run_id=run_id))[
        "code"] == "enable_success_required"


def test_tool_schema_internal_purpose_and_known_key(context):
    from jsonschema import Draft202012Validator
    from app.services.assistant_prompt import tool_definitions

    definitions = {item["function"]["name"]: item["function"] for item in tool_definitions()}
    assert "create_template_from_site" in definitions
    for definition in definitions.values():
        Draft202012Validator.check_schema(definition["parameters"])
        assert "title" in definition["parameters"]["required"]
    assert "site_config" not in definitions["request_user_interaction"]["parameters"]["properties"][
        "request"]["discriminator"]["mapping"]
    tools = configure(context)
    assert call(tools, "request_user_interaction", {"request": {"purpose": "site_config", **args(
        observation={"proxy": False})}})["code"] == "invalid_arguments"
    tools.config["api_key"] = "known-private-value"
    assert call(tools, "update_site_config", args({"notes": "known-private-value"}))["code"] == "secret_input"


def test_approval_rechecks_success_evidence(context):
    tools = configure(context)
    imported, _ = import_draft(tools)
    run_id = call(tools, "run_site")["run_id"]
    finish_run(tools, run_id)

    def invalidate(_):
        with tools.manager.session_factory() as db:
            db.get(Run, run_id).status = "fail"
            db.commit()
    result, _ = asyncio.run(reviewed(tools, args({"enabled": True}), before=invalidate))
    assert result["code"] == "enable_success_required"
    with tools.manager.session_factory() as db:
        assert not db.get(Site, imported["site_id"]).enabled


def test_restart_cancels_approved_unapplied_patch_without_uncertainty(context):
    site = make_site(context)
    tools = configure(context, workflow_site_id=site.id)

    async def scenario():
        task = asyncio.create_task(tools.dispatch("update_site_config", args({"notes": "desired"})))
        item = await pending(tools, task)
        # Persist an approved decision as if the process stopped before its waiter resumed.
        with tools.manager.session_factory() as db:
            row = db.get(AssistantSession, tools.session_id)
            data = tools.manager._decrypt(row)
            saved = next(v for v in data[LEDGER].values() if v["id"] == item["id"])
            saved.update(status="approved", response={"decision": "approve"})
            data.pop(PENDING, None)
            tools.manager._encrypt(row, data)
            db.commit()
        tools.manager.recover()
        await task
    asyncio.run(scenario())
    assert {v["status"] for v in state(tools)[LEDGER].values()} == {"cancelled"}
    with tools.manager.session_factory() as db:
        assert db.get(Site, site.id).config_revision == 1
        assert db.scalar(select(func.count()).select_from(AssistantWorkflowAction)) == 0


def test_conversation_authenticated_review_refreshes_same_batch(assistant_client, provider):
    from tests.test_assistant_interactions import pending as api_pending, url
    from app.schemas.workflow import SiteWrite
    from app.services.workflows import WorkflowService

    client = assistant_client
    manager = client.app.state.assistant_manager
    with manager.session_factory() as db:
        saved = WorkflowService(db, manager.settings, manager.crypto).create_site(SiteWrite(
            name="Saved", base_url="https://example.com", notes="private original notes"))
    sid = create(client)
    target(client, sid, workflow_site_id=saved.id, workflow_consent=False)
    request = args({"name": "Reviewed destination", "notes": "Reviewed new notes"})
    provider[0].extend([tool("update_site_config", request, "settings"), text("Settings saved.")])
    send(client, sid, content="Rename the destination to Reviewed destination and set notes to Reviewed new notes")
    item = api_pending(client, sid)
    endpoint = url(sid, item["id"])
    csrf = client.headers.pop("X-CSRF-Token")
    assert client.post(endpoint, json={"decision": "approve"}).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    assert client.post(endpoint, json={"decision": "approve", "patch": {"notes": "tampered"}}).status_code == 422
    assert client.post(endpoint, json={"decision": "approve"}).status_code == 200
    detail = wait(client, sid)
    message = next(m for m in detail["messages"] if m.get("tool_name") == "update_site_config")
    assert message["status"] == "success" and json.loads(message["content"])["config_revision"] == 2
    assert detail["target"]["name"] == "Reviewed destination"
    assert not detail["target"]["workflow_consent"]
    exposed = json.dumps(detail)
    assert "private original notes" not in exposed and "_workflow_binding" not in exposed
    requests = [json.loads(r.content) for r in provider[1] if "chat/completions" in str(r.url)]
    assert "Reviewed destination" in requests[-1]["messages"][1]["content"]
    with manager.session_factory() as db:
        action = db.scalar(select(AssistantWorkflowAction))
        assert action.owner == "admin" and action.kind == "site_config"
