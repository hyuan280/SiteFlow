from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Any
from uuid import uuid4

from pydantic import Field
import pytest

from app.features.catalog import ActionWaitConfig, FEATURES
from app.schemas.workflow import GroupInput, SignalInput, SiteWrite, StepInput
from app.services.workflows import WorkflowService, WorkflowValidationError
from tests.test_workflow_drafts import _create, _read_only, _signal, _wait


def _cas(site):
    return {"expected_revision": site["config_revision"], "expected_history_key": site["history_key"]}


def _effective(resolved):
    return {
        key: [{field: item[field] for field in model.model_fields} for item in resolved[key]
              if key != "signals" or item["source"] != "system"]
        for key, model in (("steps", StepInput), ("groups", GroupInput), ("signals", SignalInput))
    }


@pytest.mark.parametrize("existing", [False, True])
def test_export_current_draft_flattens_chain_and_roundtrips(client, monkeypatch, existing):
    class NestedWaitConfig(ActionWaitConfig):
        nested: dict[str, Any] = Field(default_factory=dict)

    monkeypatch.setitem(FEATURES, "action.wait", replace(
        FEATURES["action.wait"], config_model=NestedWaitConfig))
    root, _ = _create(client, name="root", groups=[{
        "group_key": "base", "name": "Base", "position": 2000000, "step_anchor": 1000,
    }], signals=[_signal(), _signal(signal_key="unused", enabled=False)], steps=[
        _wait("nested", group_key="base", position=1000, config={
            "nested": {"a": {"keep": 1, "delete": 2}, "list": [1, 2]}, "seconds": 8}),
        _wait("disabled", group_key="base", position=2000),
        _wait("outside", position=3000),
    ])
    child, _ = _create(client, name="child", parent_id=root["id"], steps=[
        _wait("nested", config={"nested": {"a": {"delete": None, "child": 3}, "list": [9]}}),
        _wait("disabled", enabled=False),
    ])
    site, _ = _create(client, "sites", name="saved", steps=[_wait("saved_only")])
    payload = {
        "name": site["name"] if existing else "new draft", "base_url": "https://draft.example/path",
        "template_id": child["id"], "notes": "unsaved metadata", "tags": ["draft"],
        "trusted_urls": ["^https://trusted[.]example/"],
        "groups": [{"group_key": "first", "name": "First", "position": 1000000, "step_anchor": 4000}],
        "steps": [
            _wait("nested", config={"nested": {"a": {"draft": 4}}, "seconds": None}),
            _wait("draft_only", group_key="first", position=4000,
                  condition="all(signal:ready:true,signal:sys_resign_pending:false)"),
        ],
        "signals": [_signal(label="Draft Ready")], **_cas(site),
    }
    suffix = f"/{site['id']}" if existing else ""
    original = deepcopy(payload)
    with _read_only(client):
        preview = client.post(f"/api/v1/sites{suffix}/validate", json=payload)
        exported = client.post(f"/api/v1/sites{suffix}/config/export", json=payload)
    assert payload == original
    assert preview.status_code == exported.status_code == 200, exported.text
    assert exported.headers["cache-control"] == "no-store"
    exported = exported.json()
    assert exported == SiteWrite.model_validate(exported).model_dump(mode="json")
    assert exported["template_id"] is None
    assert exported["expected_revision"] is exported["expected_history_key"] is None
    assert exported["notes"] == "unsaved metadata" and exported["tags"] == ["draft"]
    assert _effective(preview.json()["resolved"]) == {key: exported[key] for key in ("steps", "groups", "signals")}
    steps = {step["step_key"]: step for step in exported["steps"]}
    assert "saved_only" not in steps
    assert steps["nested"]["config"]["nested"] == {"a": {"keep": 1, "child": 3, "draft": 4}, "list": [9]}
    assert steps["nested"]["config"]["seconds"] != 8
    assert steps["disabled"]["enabled"] is False
    assert [step["step_key"] for step in exported["steps"]] == ["draft_only", "outside", "nested", "disabled"]
    assert [group["step_anchor"] for group in exported["groups"]] == [4000, 1000]
    assert {signal["signal_key"] for signal in exported["signals"]} == {"ready", "unused"}
    exported["name"] = "roundtrip"
    created = client.post("/api/v1/sites", json=exported)
    assert created.status_code == 201, created.text
    assert _effective(created.json()["resolved"]) == _effective(preview.json()["resolved"])
    assert created.json()["template_id"] is None


@pytest.mark.parametrize("with_groups", [False, True])
def test_export_normalizes_inherited_ungrouped_collision(client, with_groups):
    groups = [
        {"group_key": "last", "name": "Last", "position": 4000000, "step_anchor": 500},
        {"group_key": "empty", "name": "Empty", "position": 2000000, "step_anchor": 2000},
        {"group_key": "empty_tie", "name": "Empty Tie", "position": 3000000, "step_anchor": 2000},
    ] if with_groups else []
    grouped_steps = [
        _wait("last_step", group_key="last", position=8000, config={"seconds": 7}),
    ] if with_groups else []
    root, _ = _create(client, name="root", groups=groups, steps=[
        _wait("prefix", position=500), _wait("a", position=1000),
        _wait("b", position=1001, enabled=False), _wait("tail", position=6000),
        *grouped_steps,
    ])
    child, _ = _create(client, name="child", parent_id=root["id"], steps=[
        _wait("a", config={"seconds": 3}),
        *([_wait("first_step", group_key="first", position=9000, enabled=False)] if with_groups else []),
    ], groups=[{
        "group_key": "first", "name": "First", "position": 1000000, "step_anchor": 5000,
    }] if with_groups else [])
    payload = {
        "name": "flattened", "base_url": "https://example.com", "template_id": child["id"],
        "steps": [], "groups": [], "signals": [],
    }
    with _read_only(client):
        preview = client.post("/api/v1/sites/validate", json=payload)
        response = client.post("/api/v1/sites/config/export?replace_local=true", json=payload)
    assert preview.status_code == response.status_code == 200, response.text
    before = _effective(preview.json()["resolved"])
    assert [step["position"] for step in before["steps"] if step["step_key"] in {"a", "b"}] == [1001, 1001]
    exported = response.json()
    positions = [step["position"] for step in exported["steps"] if step["group_key"] is None]
    assert len(positions) == len(set(positions))
    validated = client.post("/api/v1/sites/validate?replace_local=true", json=exported)
    assert validated.status_code == 200, validated.text
    created = client.post("/api/v1/sites", json=exported)
    assert created.status_code == 201, created.text
    after = _effective(created.json()["resolved"])
    expected_order = ["first_step", "prefix", "a", "b", "last_step", "tail"] if with_groups else ["prefix", "a", "b", "tail"]
    assert [step["step_key"] for step in before["steps"]] == expected_order
    for effective in (before, after):
        for step in effective["steps"]:
            step.pop("position")
        for group in effective["groups"]:
            group.pop("position")
            group.pop("step_anchor")
    assert after == before


@pytest.mark.parametrize("empty", [False, True])
def test_export_normalizes_resolved_group_position_overflow(client, empty):
    root, _ = _create(client, name="root", groups=[{
        "group_key": "last", "name": "Last", "position": 1_000_000_000,
    }], steps=[] if empty else [_wait("last_step", group_key="last", position=2000, enabled=False)])
    child, _ = _create(client, name="child", parent_id=root["id"], groups=[{
        "group_key": "first", "name": "First", "position": 1000000, "step_anchor": 1000,
    }], steps=[] if empty else [
        _wait("first_step", group_key="first", position=3000), _wait("outside", position=1500),
    ])
    payload = {"name": "flattened", "base_url": "https://example.com", "template_id": child["id"]}
    with _read_only(client):
        preview = client.post("/api/v1/sites/validate", json=payload)
        response = client.post("/api/v1/sites/config/export", json=payload)
    assert preview.status_code == response.status_code == 200, response.text
    assert preview.json()["resolved"]["groups"][-1]["position"] > 1_000_000_000
    exported = response.json()
    SiteWrite.model_validate(exported)
    destination, _ = _create(client, "sites", name="destination", groups=[{
        "group_key": "first", "name": "Old First", "step_anchor": 999999,
    }])
    imported = client.post(f"/api/v1/sites/{destination['id']}/config/import", json={
        **exported, **_cas(destination),
    })
    assert imported.status_code == 200, imported.text
    assert imported.json()["template_id"] is None
    before, after = _effective(preview.json()["resolved"]), _effective(imported.json()["resolved"])
    for effective in (before, after):
        for step in effective["steps"]:
            step.pop("position")
        for group in effective["groups"]:
            group.pop("position")
            group.pop("step_anchor")
    assert after == before


@pytest.mark.parametrize("bound_template", [False, True])
def test_import_replaces_local_collections_and_null_anchor(client, bound_template):
    template, _ = _create(client, name="base", steps=[_wait("inherited", config={"seconds": 7})])
    site, payload = _create(client, "sites", name="destination", trusted_urls=["keep"],
                           groups=[{"group_key": "keep", "name": "Keep", "step_anchor": 6000},
                                   {"group_key": "old", "name": "Old"}],
                           steps=[_wait("old", condition="signal:ready:true")], signals=[_signal()])
    payload.update(_cas(site), template_id=template["id"] if bound_template else None,
                   groups=[{"group_key": "keep", "name": "Keep", "step_anchor": None}],
                   steps=[_wait("inherited", config={"seconds": 3})] if bound_template else [], signals=[])
    payload.pop("trusted_urls")
    with _read_only(client):
        ordinary = client.post(f"/api/v1/sites/{site['id']}/validate", json=payload)
        preview = client.post(f"/api/v1/sites/{site['id']}/validate?replace_local=true", json=payload)
    assert ordinary.status_code == preview.status_code == 200, preview.text
    assert ordinary.json()["local"]["groups"][0]["step_anchor"] == 6000
    assert ordinary.json()["local"]["trusted_urls"] == ["keep"]
    assert preview.json()["local"]["groups"][0]["step_anchor"] is None
    assert preview.json()["local"]["trusted_urls"] == []
    exported = client.post(f"/api/v1/sites/{site['id']}/config/export?replace_local=true", json=payload)
    assert exported.status_code == 200, exported.text
    assert exported.json()["groups"][0]["step_anchor"] is None
    form = SiteWrite.model_validate(preview.json()["local"]).model_dump(mode="json")
    response = client.post(f"/api/v1/sites/{site['id']}/config/import", json={**form, **_cas(site)})
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["template_id"] == payload["template_id"]
    assert saved["groups"] == preview.json()["local"]["groups"]
    assert saved["signals"] == [] and saved["trusted_urls"] == []
    assert [step["step_key"] for step in saved["steps"]] == (["inherited"] if bound_template else [])
    assert saved["resolved"] == preview.json()["resolved"]
    assert saved["config_revision"] == 2
    history = client.get(f"/api/v1/sites/{site['id']}/versions/2").json()
    assert history["snapshot"]["local"] == preview.json()["local"]
    assert history["operation"] == "update"


@pytest.mark.parametrize("change,status", [
    ({"expected_revision": None}, 422), ({"expected_history_key": None}, 422),
    ({"expected_revision": 2}, 409), ({"expected_history_key": str(uuid4())}, 409),
    ({"steps": [_wait(config={"seconds": -1})]}, 422),
    ({"steps": [_wait(condition="signal:missing:true")]}, 422),
    ({"groups": [{"group_key": "bad", "name": "Bad"}] , "steps": [_wait(group_key="missing")]}, 422),
    ({"signals": [_signal(signal_key="sys_resign_pending")]}, 422),
])
def test_import_failure_rolls_back_site_collections_and_history(client, change, status):
    site, payload = _create(client, "sites", steps=[_wait("old")], groups=[], signals=[])
    before = client.get(f"/api/v1/sites/{site['id']}").json()
    versions = client.get(f"/api/v1/sites/{site['id']}/versions").json()
    response = client.post(f"/api/v1/sites/{site['id']}/config/import", json={**payload, **_cas(site), **change})
    assert response.status_code == status, response.text
    assert client.get(f"/api/v1/sites/{site['id']}").json() == before
    assert client.get(f"/api/v1/sites/{site['id']}/versions").json() == versions


@pytest.mark.parametrize("field", ["steps", "groups", "signals"])
def test_import_and_replacement_validation_require_explicit_collections(client, field):
    site, payload = _create(client, "sites", steps=[], groups=[], signals=[])
    payload.update(_cas(site))
    payload.pop(field)
    for path in (f"/{site['id']}/config/import", f"/{site['id']}/validate?replace_local=true",
                 "/validate?replace_local=true"):
        response = client.post(f"/api/v1/sites{path}", json=payload)
        assert response.status_code == 422, response.text
        assert "explicitly supplied" in response.json()["detail"]
    with client.app.state.session_factory() as db:
        service = WorkflowService(db, client.app.state.settings, client.app.state.crypto)
        with pytest.raises(WorkflowValidationError, match="explicitly supplied"):
            service.import_site_config(site["id"], SiteWrite.model_validate(payload))


@pytest.mark.parametrize("field,value", [("expected_revision", 2), ("expected_history_key", str(uuid4()))])
def test_export_existing_draft_checks_cas_without_writes(client, field, value):
    site, payload = _create(client, "sites")
    with _read_only(client):
        response = client.post(f"/api/v1/sites/{site['id']}/config/export", json={
            **payload, **_cas(site), field: value,
        })
    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", ["/config/export", "/1/config/export", "/1/config/import"])
def test_config_endpoints_require_auth_and_csrf(client, anon_client, path):
    payload = {"name": "test", "base_url": "https://example.com", "steps": [], "groups": [], "signals": []}
    assert anon_client.post(f"/api/v1/sites{path}", json=payload).status_code == 401
    client.headers.pop("X-CSRF-Token")
    assert client.post(f"/api/v1/sites{path}", json=payload).status_code == 403
