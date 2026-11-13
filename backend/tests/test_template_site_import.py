# ruff: noqa: F811 - pytest injects the imported shared context fixture by name

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import event, func, select

from app.models.assistant import AssistantSession
from app.models.assistant_workflow import AssistantWorkflowAction
from app.models.execution import Run, SiteJob
from app.models.workflow import Site, Template, WorkflowHistory, WorkflowStep
from app.schemas.assistant import InteractionResponse
from app.schemas.workflow import TemplateWrite
from app.services.assistant_interactions import PENDING, respond_interaction
from app.services.assistant_templates import (
    TemplateFromSiteRequest,
    apply_template_import,
    dispatch,
    prepare_template_import,
)
from app.services.assistant_workflows import WorkflowDenied
from app.services.template_site_import import site_import_candidate, template_import_write
from app.services.workflows import WorkflowError, WorkflowService
from tests.test_assistant_workflows import configure, context, make_site  # noqa: F401
from tests.test_workflows_api import _create_site, _create_template


def test_public_dispatch_routes_only_typed_request_to_explicit_review(context, monkeypatch):
    site = make_site(context, steps=[{"step_key": "wait", "feature_type": "action.wait"}])
    tools = configure(context, workflow_site_id=site.id)
    interaction = AsyncMock(return_value={"ok": False, "code": "interaction_denied"})
    monkeypatch.setattr("app.services.assistant_interactions.request_interaction", interaction)
    req = request().model_dump(mode="json")
    assert asyncio.run(dispatch(tools, "create_template_from_site", req))["code"] == "interaction_denied"
    interaction.assert_awaited_once_with(tools, {"purpose": "template_import", **req})
    for extra in ({"site_id": site.id}, {"workflow_site_id": site.id}, {"expected_revision": 1}):
        assert asyncio.run(dispatch(tools, "create_template_from_site", {**req, **extra}))["code"] == "invalid_arguments"
    with pytest.raises(ValidationError):
        TemplateFromSiteRequest.model_validate({**req, "destination_template_id": 1})
    assert "purpose" not in TemplateFromSiteRequest.model_fields
    assert asyncio.run(tools.dispatch("request_user_interaction", {"request": {
        "purpose": "template_import", **req}}))["code"] == "invalid_arguments"
    from app.services.assistant_prompt import tool_definitions

    definition = next(item["function"] for item in tool_definitions()
                      if item["function"]["name"] == "create_template_from_site")
    assert "title" in definition["parameters"]["required"]
    assert not {"site_id", "workflow_site_id", "purpose"}.intersection(definition["parameters"]["properties"])
    with tools.manager.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Template)) == 0
        assert db.scalar(select(func.count()).select_from(AssistantWorkflowAction)) == 0


@pytest.mark.parametrize("decision", ["approve", "deny", "stale", "timeout"])
def test_real_tool_waits_for_exact_user_approval(context, decision):
    site = make_site(context, steps=[{"step_key": "open", "feature_type": "flow.open"}])
    tools = configure(context, workflow_site_id=site.id, workflow_consent=True)
    tools.config["_assistant_settings"] = {"auto_authorize_workflow": True}
    manager = tools.manager
    req = request().model_dump(mode="json")

    async def run():
        task = asyncio.create_task(tools.dispatch("create_template_from_site", req))
        for _ in range(500):
            with manager.session_factory() as db:
                data = data_for(db, tools)
                item = data.get(PENDING)
                assert db.scalar(select(func.count()).select_from(Template)) == 0
                assert db.scalar(select(func.count()).select_from(AssistantWorkflowAction)) == 0
            if item:
                break
            if task.done():
                pytest.fail(str(task.result()))
            await asyncio.sleep(0.001)
        else:
            pytest.fail("No explicit template review")
        assert item["purpose"] == "template_import"
        assert item["review"]["source_site_id"] == site.id
        if decision == "stale":
            with manager.session_factory() as db:
                db.get(Site, site.id).config_revision += 1
                db.commit()
        if decision == "timeout":
            with manager.session_factory() as db:
                row = db.get(AssistantSession, tools.session_id)
                data = manager._decrypt(row)
                data[PENDING]["expires_epoch"] = 0
                for previous in data["_interaction_decisions"].values():
                    if previous["id"] == item["id"]:
                        previous["expires_epoch"] = 0
                manager._encrypt(row, data)
                db.commit()
        else:
            await respond_interaction(manager, tools.owner, tools.session_id, item["id"],
                                      InteractionResponse(decision="deny" if decision == "deny" else "approve"))
        return await task

    result = asyncio.run(run())
    assert result["ok"] == (decision == "approve"), result
    with manager.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Template)) == (decision == "approve")
        assert db.scalar(select(func.count()).select_from(AssistantWorkflowAction)) == (decision == "approve")
        assert data_for(db, tools).get(PENDING) is None
        assert db.get(Site, site.id).config_revision == (2 if decision == "stale" else 1)
    if decision in {"approve", "deny", "timeout"}:
        replay = asyncio.run(tools.dispatch("create_template_from_site", req))
        assert replay["ok"] == result["ok"]
        if decision == "approve":
            assert replay["template_id"] == result["template_id"] and replay["replayed"]
            duplicate = {**req, "operation_id": str(uuid4())}
            response = asyncio.run(tools.dispatch("create_template_from_site", duplicate))
            assert not response["ok"], "A new operation cannot claim an earlier receipt or overwrite the created template"


def test_preview_local_only_read_only_and_no_metadata(client, anon_client):
    parent = _create_template(client, {"name": "Base", "steps": [
        {"step_key": "open", "feature_type": "flow.open", "position": 1000,
         "config": {"wait_until": "load"}},
    ]})
    source = _create_site(client, {
        "name": "Source", "base_url": "https://example.com", "template_id": parent["id"],
        "notes": "PRIVATE NOTES", "tags": ["PRIVATE TAG"], "enabled": False,
        "groups": [{"group_key": "local", "name": "Local", "position": 700,
                    "step_anchor": 4500}],
        "steps": [
            {"step_key": "open", "feature_type": "flow.open", "enabled": None,
             "config": {"wait_until": None}},
            {"step_key": "wait", "feature_type": "action.wait", "position": None,
             "group_key": "local", "config": {}},
        ],
        "signals": [{"signal_key": "done", "label": "Done", "include_texts": ["ok"]}],
    })
    source = client.get(f"/api/v1/sites/{source['id']}").json()
    path = f"/api/v1/templates/import-from-site/{source['id']}"
    assert anon_client.get(path).status_code == 401
    statements = []
    engine = client.app.state.session_factory.kw["bind"]

    def observe(_conn, _cursor, statement, *_args):
        statements.append(statement.lstrip().split()[0].upper())

    event.listen(engine, "before_cursor_execute", observe)
    try:
        response = client.get(path)
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert response.status_code == 200, response.text
    candidate = response.json()
    assert set(candidate) == {"parent_id", "steps", "groups", "signals"}
    assert not {"INSERT", "UPDATE", "DELETE"}.intersection(statements)
    assert candidate["parent_id"] == parent["id"]
    assert candidate["steps"][0]["config"] == {"wait_until": None}
    assert candidate["steps"][0]["enabled"] is None
    assert candidate["steps"][1]["position"] is None
    assert candidate["groups"][0]["step_anchor"] == 4500
    assert [signal["signal_key"] for signal in candidate["signals"]] == ["done"]
    assert client.get(f"/api/v1/sites/{source['id']}").json() == source
    created = _create_template(client, {"name": "Imported", **candidate})
    assert created["parent_id"] == parent["id"]
    assert created["steps"][0]["config"] == {"wait_until": None}
    assert client.get("/api/v1/templates/import-from-site/99999").status_code == 404


def service_for(db, tools):
    return WorkflowService(db, tools.manager.settings, tools.manager.crypto)


def data_for(db, tools):
    return tools.manager._decrypt(db.get(AssistantSession, tools.session_id))


def request(**changes):
    return TemplateFromSiteRequest(operation_id=uuid4(), name="Imported", **changes)


def test_assistant_frozen_create_atomic_receipt_and_source_unchanged(context):
    site = make_site(context, steps=[{"step_key": "open", "feature_type": "flow.open"}])
    tools = configure(context, workflow_site_id=site.id, workflow_consent=False)
    manager = tools.manager
    req = request()
    with manager.session_factory() as db:
        original = service_for(db, tools)._local_snapshot(db.get(Site, site.id))
        public, args, subject = prepare_template_import(db, tools, req, data_for(db, tools))
        assert public["purpose"] == "template_import" and subject.startswith("template_import:")
        assert public["review"]["steps"][0]["step_key"] == "open"
        assert public["review"]["before"] is None
        assert set(public["review"]) == {
            "source_site_id", "destination_template_id", "name", "description", "enabled",
            "parent_id", "steps", "groups", "signals", "before", "warnings",
        }
        assert db.scalar(select(func.count()).select_from(Template)) == 0
        assert db.scalar(select(func.count()).select_from(AssistantWorkflowAction)) == 0
    with manager.session_factory() as db:
        result = apply_template_import(db, tools, data_for(db, tools), args)
        assert result["ok"] and result["operation"] == "created"
        db.rollback()
    with manager.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Template)) == 0
        assert db.scalar(select(func.count()).select_from(AssistantWorkflowAction)) == 0
        result = apply_template_import(db, tools, data_for(db, tools), args)
        db.commit()
    with manager.session_factory() as db:
        assert service_for(db, tools)._local_snapshot(db.get(Site, site.id)) == original
        assert db.get(Site, site.id).config_revision == 1
        action = db.scalar(select(AssistantWorkflowAction))
        assert action.state == "complete" and action.kind == "template_import"
        assert b"Imported" not in action.ciphertext
        history = db.scalar(select(WorkflowHistory).where(
            WorkflowHistory.history_key == result["history_key"]))
        assert history.created_by == "assistant"
    replay = asyncio.run(dispatch(tools, "create_template_from_site", req.model_dump(mode="json")))
    assert replay == {**result, "replayed": True}
    conflict = req.model_dump(mode="json")
    conflict["name"] = "Other"
    assert asyncio.run(dispatch(tools, "create_template_from_site", conflict))["code"] == "template_import_operation_conflict"


def test_replace_all_locals_preserve_metadata_and_null_anchor(context):
    site = make_site(context, groups=[{"group_key": "local", "name": "Local", "position": 700}],
                     steps=[{"step_key": "wait", "feature_type": "action.wait",
                             "group_key": "local", "position": 123, "enabled": None}])
    tools = configure(context, workflow_site_id=site.id)
    manager = tools.manager
    with manager.session_factory() as db:
        destination = service_for(db, tools).create_template(TemplateWrite(
            name="Keep", description="Keep description", enabled=False,
            groups=[{"group_key": "local", "name": "Local", "position": 999, "step_anchor": 321}],
            steps=[{"step_key": "wait", "feature_type": "action.wait", "group_key": "local"}],
            signals=[{"signal_key": "old", "label": "Old", "include_texts": ["old"]}],
        ))
        req = request(description="Not copied", destination_template_id=destination.id,
                      expected_revision=destination.version,
                      expected_history_key=destination.history_key)
        public, args, _ = prepare_template_import(db, tools, req, data_for(db, tools))
        assert public["review"]["name"] == "Keep"
        assert public["review"]["before"]["groups"][0]["step_anchor"] == 321
    with manager.session_factory() as db:
        result = apply_template_import(db, tools, data_for(db, tools), args)
        db.commit()
        saved = service_for(db, tools).get_template(destination.id)
        assert (saved.name, saved.description, saved.enabled) == ("Keep", "Keep description", False)
        assert [step.step_key for step in saved.steps] == ["wait"]
        assert saved.steps[0].enabled is None and saved.steps[0].position == 123
        assert saved.groups[0].step_anchor is None
        assert saved.signals == [] and result["version"] == 2


@pytest.mark.parametrize("old_anchor", [10, 40], ids=["reject_reorder", "allow_insertion"])
def test_replacement_null_anchor_guard_matches_exact_saved_order(context, old_anchor):
    groups = [{"group_key": "local", "name": "Local", "position": 10, "step_anchor": None}]
    steps = [
        {"step_key": "grouped", "feature_type": "action.wait", "group_key": "local", "position": 30},
        {"step_key": "ungrouped", "feature_type": "action.wait", "position": 20},
    ]
    source_steps = [*steps, {"step_key": "inserted", "feature_type": "action.wait", "position": 25}]
    site = make_site(context, groups=groups, steps=source_steps)
    tools = configure(context, workflow_site_id=site.id)
    with tools.manager.session_factory() as db:
        service = service_for(db, tools)
        destination = service.create_template(TemplateWrite(
            name="Dest", groups=[{**groups[0], "step_anchor": old_anchor}], steps=steps))
        req = request(destination_template_id=destination.id, expected_revision=destination.version,
                      expected_history_key=destination.history_key)
        candidate = site_import_candidate(service, site.id)
        write = template_import_write(
            service, candidate, name="Ignored", destination_id=destination.id,
            expected_revision=destination.version, expected_history_key=destination.history_key)
        # The default editor draft keeps the old anchor, masking the reorder.
        editor_draft = service.validate_draft("template", write, destination.id)
        assert editor_draft["local"]["groups"][0]["step_anchor"] == old_anchor
        draft = service.validate_draft("template", write, destination.id, preserve_local=True)
        assert draft["local"]["groups"][0]["step_anchor"] is None
        assert [step["step_key"] for step in draft["resolved"]["steps"]] == [
            "ungrouped", "inserted", "grouped"]
        if old_anchor == 10:
            assert [step["step_key"] for step in editor_draft["resolved"]["steps"]] == [
                "grouped", "ungrouped", "inserted"]
            with pytest.raises(WorkflowDenied, match="template_import_structure_changed"):
                prepare_template_import(db, tools, req, data_for(db, tools))
            assert db.get(Template, destination.id).version == 1
            assert db.scalar(select(func.count()).select_from(AssistantWorkflowAction)) == 0
        else:
            public, args, _ = prepare_template_import(db, tools, req, data_for(db, tools))
            assert public["review"]["groups"][0]["step_anchor"] is None
            assert public["review"]["before"]["groups"][0]["step_anchor"] == old_anchor
            assert public["review"]["steps"] == candidate.model_dump(mode="json")["steps"]
            apply_template_import(db, tools, data_for(db, tools), args)
            db.commit()
            saved = service_for(db, tools).get_template(destination.id)
            assert saved.groups[0].step_anchor is None
            assert saved.resolved.model_dump(mode="json") == draft["resolved"]


@pytest.mark.parametrize("position", [None, 77])
def test_import_draft_preserves_inherited_override_local_position(context, position):
    manager, _ = context
    with manager.session_factory() as db:
        parent = WorkflowService(db, manager.settings, manager.crypto).create_template(TemplateWrite(
            name="Parent", steps=[{"step_key": "wait", "feature_type": "action.wait", "position": 100}]))
    site = make_site(context, template_id=parent.id,
                     steps=[{"step_key": "wait", "feature_type": "action.wait"}])
    with manager.session_factory() as db:
        row = db.scalar(select(WorkflowStep).where(
            WorkflowStep.owner_type == "site", WorkflowStep.owner_id == site.id))
        row.position = position
        db.commit()
    tools = configure(context, workflow_site_id=site.id)
    with manager.session_factory() as db:
        service = service_for(db, tools)
        candidate = site_import_candidate(service, site.id)
        write = template_import_write(service, candidate, name="Imported")
        original = write.model_dump(mode="json")
        assert service.validate_draft("template", write)["local"]["steps"][0]["position"] == 101
        draft = service.validate_draft("template", write, preserve_local=True)
        assert draft["local"]["steps"][0]["position"] == position
        assert write.model_dump(mode="json") == original
        public, args, _ = prepare_template_import(db, tools, request(), data_for(db, tools))
        assert public["review"]["steps"][0]["position"] == position
        result = apply_template_import(db, tools, data_for(db, tools), args)
        db.commit()
        saved = service_for(db, tools).get_template(result["template_id"])
        assert saved.steps[0].position == position
        assert saved.resolved.model_dump(mode="json") == {**draft["resolved"], "owner_id": saved.id}


@pytest.mark.parametrize("change", [
    "delete_group", "rename_group", "rename_group_key", "reorder_groups",
    "delete_step", "move_step", "group_ungrouped", "reorder_steps", "reanchor_steps",
])
def test_unused_destination_structure_cannot_be_replaced(context, change):
    groups = [{"group_key": "first", "name": "First", "position": 10, "step_anchor": 10},
              {"group_key": "last", "name": "Last", "position": 30, "step_anchor": 30}]
    steps = [{"step_key": "one", "feature_type": "action.wait", "group_key": "first", "position": 10},
             {"step_key": "two", "feature_type": "action.wait", "group_key": "first", "position": 20},
             {"step_key": "three", "feature_type": "action.wait", "position": 25}]
    candidate_groups, candidate_steps = deepcopy(groups), deepcopy(steps)
    if change == "delete_group":
        candidate_groups.pop()
    elif change == "rename_group":
        candidate_groups[0]["name"] = "Renamed"
    elif change == "rename_group_key":
        candidate_groups[1]["group_key"] = "renamed"
    elif change == "reorder_groups":
        candidate_groups[0]["position"] = 40
    elif change == "delete_step":
        candidate_steps.pop(1)
    elif change == "move_step":
        candidate_steps[0]["group_key"] = "last"
    elif change == "group_ungrouped":
        candidate_steps[2]["group_key"] = "last"
    elif change == "reorder_steps":
        candidate_steps[0]["position"] = 21
    else:
        candidate_groups[0]["step_anchor"] = 40
        candidate_groups[1]["step_anchor"] = 50
    site = make_site(context, groups=candidate_groups, steps=candidate_steps)
    tools = configure(context, workflow_site_id=site.id)
    with tools.manager.session_factory() as db:
        service = service_for(db, tools)
        destination = service.create_template(TemplateWrite(name="Dest", groups=groups, steps=steps))
        original = service._snapshot(db.get(Template, destination.id))
        with pytest.raises(WorkflowDenied, match="template_import_structure_changed"):
            prepare_template_import(db, tools, request(
                destination_template_id=destination.id, expected_revision=destination.version,
                expected_history_key=destination.history_key), data_for(db, tools))
        assert service._snapshot(db.get(Template, destination.id)) == original
        assert db.scalar(select(func.count()).select_from(AssistantWorkflowAction)) == 0


def test_insertions_and_config_changes_review_exact_local_before_and_candidate(context):
    groups = [{"group_key": "first", "name": "First", "position": 10, "step_anchor": 10},
              {"group_key": "last", "name": "Last", "position": 30, "step_anchor": 30}]
    steps = [{"step_key": "one", "feature_type": "action.wait", "group_key": "first",
              "position": 10, "config": {"seconds": 1}},
             {"step_key": "two", "feature_type": "action.wait", "group_key": "first", "position": 30},
             {"step_key": "three", "feature_type": "action.wait", "group_key": "last", "position": 50}]
    new_groups = deepcopy(groups)
    new_groups[0].update(position=5, step_anchor=5)
    new_groups.insert(1, {"group_key": "middle", "name": "Middle", "position": 20, "step_anchor": 40})
    new_steps = deepcopy(steps)
    new_steps[0]["config"] = {"seconds": 2}
    new_steps.insert(1, {"step_key": "inserted", "feature_type": "action.wait",
                         "group_key": "first", "position": 20})
    new_steps.insert(3, {"step_key": "middle", "feature_type": "action.wait",
                         "group_key": "middle", "position": 40})
    site = make_site(context, groups=new_groups, steps=new_steps,
                     notes="PRIVATE NOTES", tags=["PRIVATE TAG"])
    tools = configure(context, workflow_site_id=site.id)
    with tools.manager.session_factory() as db:
        service = service_for(db, tools)
        destination = service.create_template(TemplateWrite(name="Dest", groups=groups, steps=steps))
        old_local = service._local_snapshot(db.get(Template, destination.id))
        exact_candidate = site_import_candidate(service, site.id).model_dump(mode="json")
        statements = []

        def observe(_conn, _cursor, statement, *_args):
            statements.append(statement.lstrip().split()[0].upper())

        engine = db.get_bind()
        event.listen(engine, "before_cursor_execute", observe)
        try:
            public, args, _ = prepare_template_import(db, tools, request(
                destination_template_id=destination.id, expected_revision=destination.version,
                expected_history_key=destination.history_key), data_for(db, tools))
        finally:
            event.remove(engine, "before_cursor_execute", observe)
        assert not {"INSERT", "UPDATE", "DELETE"}.intersection(statements)
        review = public["review"]
        assert review["before"] == {key: old_local[key] for key in exact_candidate}
        assert {key: review[key] for key in exact_candidate} == exact_candidate
        assert review["before"]["steps"][0]["config"] == {"seconds": 1}
        assert review["steps"][0]["config"] == {"seconds": 2}
        assert set(review["before"]) == {"parent_id", "steps", "groups", "signals"}
        assert not any(value in str(public) for value in (
            "PRIVATE NOTES", "PRIVATE TAG", "dependencies", "base_url", "history_key", "snapshot"))
        assert len(review["warnings"]) == 3
        result = apply_template_import(db, tools, data_for(db, tools), args)
        db.commit()
        saved = service_for(db, tools)._local_snapshot(db.get(Template, destination.id))
        assert {key: saved[key] for key in exact_candidate} == exact_candidate
        assert result["operation"] == "updated"


@pytest.mark.parametrize("change", ["delete_group", "rename_group", "reorder_groups",
                                 "delete_step", "move_step", "reorder_steps", "compatible"])
def test_replacement_guard_resolves_old_and_new_parents(context, change):
    groups = [{"group_key": "first", "name": "First", "position": 10},
              {"group_key": "last", "name": "Last", "position": 30}]
    steps = [{"step_key": "one", "feature_type": "action.wait", "group_key": "first", "position": 10},
             {"step_key": "two", "feature_type": "action.wait", "group_key": "first", "position": 20}]
    new_groups, new_steps = deepcopy(groups), deepcopy(steps)
    if change == "delete_group":
        new_groups.pop()
    elif change == "rename_group":
        new_groups[0]["name"] = "Renamed"
    elif change == "reorder_groups":
        new_groups[0]["position"] = 40
    elif change == "delete_step":
        new_steps.pop()
    elif change == "move_step":
        new_steps[0]["group_key"] = "last"
    elif change == "reorder_steps":
        new_steps[0]["position"] = 21
    manager, _ = context
    with manager.session_factory() as db:
        service = WorkflowService(db, manager.settings, manager.crypto)
        parent = service.create_template(TemplateWrite(name="Old parent", groups=groups, steps=steps))
        new_parent = service.create_template(TemplateWrite(name="New parent", groups=new_groups, steps=new_steps))
        destination = service.create_template(TemplateWrite(name="Dest", parent_id=parent.id))
    site = make_site(context, template_id=new_parent.id)
    tools = configure(context, workflow_site_id=site.id)
    with manager.session_factory() as db:
        req = request(destination_template_id=destination.id, expected_revision=destination.version,
                      expected_history_key=destination.history_key)
        if change != "compatible":
            with pytest.raises(WorkflowDenied, match="template_import_structure_changed"):
                prepare_template_import(db, tools, req, data_for(db, tools))
        else:
            public, args, _ = prepare_template_import(db, tools, req, data_for(db, tools))
            assert public["review"]["before"] == {
                "parent_id": parent.id, "steps": [], "groups": [], "signals": []}
            assert public["review"]["parent_id"] == new_parent.id
            assert public["review"]["steps"] == []
            apply_template_import(db, tools, data_for(db, tools), args)
            db.commit()


@pytest.mark.parametrize("side", ["source", "destination"])
@pytest.mark.parametrize("feature,config", [
    ("flow.open", {"url": "https://example.com/?token=private-value"}),
    ("flow.open", {"url": "https://example.com/known-private-value"}),
    ("post.buy_card", {"option_selector": "PRIVATE HIDDEN VALUE"}),
    ("flow.result", {"llm_prompt": "PRIVATE HIDDEN VALUE"}),
    ("flow.result", {"message": "password=private-value"}),
    ("flow.result", {"message": "Bearer private-value"}),
])
def test_public_review_fails_closed_for_unsafe_local_values(context, side, feature, config):
    site = make_site(context, steps=[{"step_key": "one", "feature_type": feature,
                                     "config": config if side == "source" else {}}])
    tools = configure(context, workflow_site_id=site.id)
    tools.config["api_key"] = "known-private-value"
    with tools.manager.session_factory() as db:
        destination = service_for(db, tools).create_template(TemplateWrite(
            name="Dest", steps=[{"step_key": "one", "feature_type": feature,
                                 "config": config if side == "destination" else {}}]))
        result = asyncio.run(dispatch(tools, "create_template_from_site", request(
            destination_template_id=destination.id, expected_revision=destination.version,
            expected_history_key=destination.history_key).model_dump(mode="json")))
        assert not result["ok"]
        assert result["code"] == "protected_config_changed"
        assert "private-value" not in str(result) and "PRIVATE HIDDEN VALUE" not in str(result)
        assert db.scalar(select(func.count()).select_from(AssistantWorkflowAction)) == 0


def test_apply_rechecks_replacement_structure_before_save(context):
    site = make_site(context, steps=[{"step_key": "one", "feature_type": "action.wait"}])
    tools = configure(context, workflow_site_id=site.id)
    with tools.manager.session_factory() as db:
        destination = service_for(db, tools).create_template(TemplateWrite(
            name="Dest", steps=[{"step_key": "one", "feature_type": "action.wait"}]))
        _, args, _ = prepare_template_import(db, tools, request(
            destination_template_id=destination.id, expected_revision=destination.version,
            expected_history_key=destination.history_key), data_for(db, tools))
        args["write"]["steps"] = []
        with pytest.raises(WorkflowDenied, match="template_import_structure_changed"):
            apply_template_import(db, tools, data_for(db, tools), args)
        db.rollback()
        assert db.get(Template, destination.id).version == 1
        assert db.scalar(select(func.count()).select_from(AssistantWorkflowAction)) == 0


@pytest.mark.parametrize("change", ["source", "destination", "dependency", "raw_config",
                                  "destination_dependency", "raw_destination_dependency"])
def test_review_invalidated_by_source_destination_or_dependency_change(context, change):
    manager, _ = context
    with manager.session_factory() as db:
        parent = WorkflowService(db, manager.settings, manager.crypto).create_template(TemplateWrite(
            name="Base", steps=[{"step_key": "open", "feature_type": "flow.open"}]))
        old_parent = WorkflowService(db, manager.settings, manager.crypto).create_template(TemplateWrite(
            name="Old base", steps=[{"step_key": "open", "feature_type": "flow.open"}]))
        destination = WorkflowService(db, manager.settings, manager.crypto).create_template(
            TemplateWrite(name="Dest", parent_id=old_parent.id))
    site = make_site(context, template_id=parent.id)
    tools = configure(context, workflow_site_id=site.id)
    with manager.session_factory() as db:
        _, args, _ = prepare_template_import(db, tools, request(
            destination_template_id=destination.id, expected_revision=destination.version,
            expected_history_key=destination.history_key), data_for(db, tools))
    with manager.session_factory() as db:
        if change == "source":
            db.get(Site, site.id).config_revision += 1
        elif change == "destination":
            db.get(Template, destination.id).version += 1
        elif change == "dependency":
            db.get(Template, parent.id).version += 1
        elif change == "destination_dependency":
            db.get(Template, old_parent.id).version += 1
        else:
            parent_id = old_parent.id if change == "raw_destination_dependency" else parent.id
            db.scalar(select(WorkflowStep).where(WorkflowStep.owner_type == "template",
                                               WorkflowStep.owner_id == parent_id)).config_json = '{"wait_until":"load"}'
        db.commit()
    with manager.session_factory() as db:
        with pytest.raises((WorkflowDenied, WorkflowError)):
            apply_template_import(db, tools, data_for(db, tools), args)
        db.rollback()
        assert db.scalar(select(func.count()).select_from(AssistantWorkflowAction)) == 0


def test_bound_workflow_only_not_credential_site(context):
    site = make_site(context, steps=[{"step_key": "wait", "feature_type": "action.wait"}])
    tools = configure(context, site_id=site.id)
    with tools.manager.session_factory() as db, pytest.raises(WorkflowDenied, match="workflow_binding_changed"):
        prepare_template_import(db, tools, request(), data_for(db, tools))


def test_preview_active_invalid_and_cycle_rejected(context):
    site = make_site(context, steps=[{"step_key": "wait", "feature_type": "action.wait"}])
    tools = configure(context, workflow_site_id=site.id)
    manager = tools.manager
    with manager.session_factory() as db:
        run = Run(mode="all", trigger="manual", status="running", requested_sites_json="[]", created_by="admin")
        db.add(run)
        db.flush()
        job = SiteJob(run_id=run.id, site_id=site.id, site_name="Saved", lane="general", status="running")
        db.add(job)
        db.commit()
        with pytest.raises(WorkflowError, match="active"):
            site_import_candidate(service_for(db, tools), site.id)
        job.status = "failed"
        db.commit()
        candidate = site_import_candidate(service_for(db, tools), site.id)
        assert candidate.steps[0].step_key == "wait", "No successful full-run evidence is required"
        parent = service_for(db, tools).create_template(TemplateWrite(name="Parent"))
        db.get(Site, site.id).template_id = parent.id
        db.commit()
        candidate = site_import_candidate(service_for(db, tools), site.id)
        with pytest.raises(WorkflowError):
            template_import_write(service_for(db, tools), candidate, name="Ignored", destination_id=parent.id,
                                  expected_revision=parent.version, expected_history_key=parent.history_key)
        step = db.scalar(select(WorkflowStep).where(WorkflowStep.owner_type == "site"))
        step.config_json = '{"seconds":-1}'
        db.commit()
        with pytest.raises(WorkflowError):
            site_import_candidate(service_for(db, tools), site.id)


def test_template_commit_false_rolls_back_history_and_rows(context):
    manager, _ = context
    with manager.session_factory() as db:
        service = WorkflowService(db, manager.settings, manager.crypto)
        saved = service.create_template(TemplateWrite(name="Temporary"), _commit=False)
        db.rollback()
        assert db.get(Template, saved.id) is None
        assert db.scalar(select(func.count()).select_from(WorkflowHistory)) == 0
        saved = service.create_template(TemplateWrite(name="Saved"))
        service.update_template(saved.id, TemplateWrite(name="Unsaved"), _commit=False)
        db.rollback()
        assert db.get(Template, saved.id).name == "Saved"
        assert db.get(Template, saved.id).version == 1
        assert db.scalar(select(func.count()).select_from(WorkflowHistory)) == 1


def test_used_destination_parent_restriction_and_manual_exact_locals(client):
    destination = _create_template(client, {
        "name": "Used", "groups": [{"group_key": "local", "name": "Local", "step_anchor": 500}],
        "steps": [{"step_key": "wait", "feature_type": "action.wait"}],
    })
    _create_site(client, {"name": "Dependent", "base_url": "https://dependent.example",
                          "template_id": destination["id"]})
    parent = _create_template(client, {"name": "Other parent"})
    source = _create_site(client, {
        "name": "Source", "base_url": "https://source.example", "template_id": parent["id"],
        "steps": [{"step_key": "wait", "feature_type": "action.wait"}],
    })
    candidate = client.get(f"/api/v1/templates/import-from-site/{source['id']}").json()
    payload = {"name": destination["name"], **candidate,
               "expected_revision": destination["version"], "expected_history_key": destination["history_key"]}
    response = client.put(f"/api/v1/templates/{destination['id']}?preserve_local=true", json=payload)
    assert response.status_code == 409
    assert client.get(f"/api/v1/templates/{destination['id']}").json()["version"] == 1

    unused = _create_template(client, {
        "name": "Unused", "groups": [{"group_key": "local", "name": "Local", "step_anchor": 500}],
    })
    candidate["groups"] = [{"group_key": "local", "name": "Local", "position": 700, "step_anchor": None}]
    candidate["steps"][0]["position"] = None
    response = client.put(f"/api/v1/templates/{unused['id']}?preserve_local=true", json={
        "name": "Unused", **candidate, "expected_revision": unused["version"],
        "expected_history_key": unused["history_key"],
    })
    assert response.status_code == 200, response.text
    assert response.json()["groups"][0]["step_anchor"] is None
    assert response.json()["steps"][0]["position"] is None
