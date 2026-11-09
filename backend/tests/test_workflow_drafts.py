from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import re
import socket
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, select

from app.core.db import Base
from app.models.auth import AuthSession
from app.models.integration import IntegrationConfig
from app.models.setting import AppSetting
from app.models.workflow import Site, Template, WorkflowHistory
from app.main import create_app
from app.schemas.workflow import ResolvedWorkflowView, SiteWrite, StepInput, TemplateWrite
from app.services.bitwarden import BitwardenService
from app.services.credentials import CredentialService
from app.services.integrations import IntegrationService
from app.services.workflow_drafts import _DraftWorkflowService
from app.services.workflows import (
    WorkflowConflictError,
    WorkflowNotFoundError,
    WorkflowService,
    WorkflowValidationError,
)
from tests.helpers import login_client, make_settings


KINDS = ["sites", "templates"]


def _payload(kind, **values):
    return {
        "name": "draft",
        **({"base_url": "https://example.com"} if kind == "sites" else {}),
        **values,
    }


def _create(client, kind="templates", **values):
    payload = _payload(kind, **values)
    response = client.post(f"/api/v1/{kind}", json=payload)
    assert response.status_code == 201, response.text
    return response.json(), payload


def _wait(key="wait", **values):
    return {"step_key": key, "feature_type": "action.wait", **values}


def _signal(**values):
    return {
        "signal_key": "ready", "label": " Ready ", "include_texts": [" ready ", " "],
        **values,
    }


def _snapshot(client):
    # Include auth, encrypted history/credentials, settings, jobs, and audit tables.
    with client.app.state.engine.connect() as connection:
        return {
            table.name: connection.execute(
                select(table).order_by(*table.primary_key.columns)
            ).all()
            for table in Base.metadata.sorted_tables
        }


@contextmanager
def _read_only(client):
    before = _snapshot(client)
    engine = client.app.state.engine

    def reject_dml(conn, cursor, statement, parameters, context, executemany):
        compiled_dml = context.isinsert or context.isupdate or context.isdelete
        raw_dml = re.match(r"\s*(INSERT|UPDATE|DELETE|REPLACE)\b", statement, re.I)
        assert not (compiled_dml or raw_dml), f"Validation emitted SQL DML: {statement}"

    event.listen(engine, "before_cursor_execute", reject_dml)
    try:
        yield
    finally:
        event.remove(engine, "before_cursor_execute", reject_dml)
        assert _snapshot(client) == before


def _validate(client, kind, payload, owner_id=None, status=200):
    original = deepcopy(payload)
    suffix = "" if owner_id is None else f"/{owner_id}"
    with _read_only(client):
        response = client.post(f"/api/v1/{kind}{suffix}/validate", json=payload)
    assert payload == original
    assert response.status_code == status, response.text
    assert response.headers.get("cache-control") == "no-store"
    return response.json()


def _reject_like_save(client, kind, payload, owner_id=None, status=422):
    draft = _validate(client, kind, payload, owner_id, status)
    before = _snapshot(client)
    url = f"/api/v1/{kind}"
    if owner_id is None:
        saved = client.post(url, json=payload)
    else:
        saved = client.put(f"{url}/{owner_id}", json=payload)
    assert saved.status_code == status, saved.text
    assert saved.json() == draft
    assert _snapshot(client) == before


def _assert_success(body, kind, owner=None):
    owner_type = kind[:-1]
    owner_id = owner["id"] if owner else None
    assert set(body) == {
        "valid", "owner_type", "owner_id", "history_key", "current_revision", "local", "resolved",
    }
    assert body["valid"] is True
    assert body["owner_type"] == owner_type
    assert body["owner_id"] == owner_id
    assert body["history_key"] == (owner["history_key"] if owner else None)
    revision_field = "config_revision" if kind == "sites" else "version"
    assert body["current_revision"] == (owner[revision_field] if owner else None)
    model = SiteWrite if kind == "sites" else TemplateWrite
    assert set(body["local"]) == set(model.model_fields) - {
        "expected_revision", "expected_history_key",
    }
    assert not {"id", "history_key", "expected_revision", "expected_history_key"} & body["local"].keys()
    assert set(body["resolved"]) == set(ResolvedWorkflowView.model_fields)
    assert body["resolved"]["owner_type"] == owner_type
    assert body["resolved"]["owner_id"] == owner_id
    # Saves require an ID; create previews deliberately do not allocate one.
    resolved = {**body["resolved"], "owner_id": owner_id if owner_id is not None else 1}
    assert ResolvedWorkflowView.model_validate(resolved).model_dump(mode="json") == resolved


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("updating", [False, True], ids=["create", "update"])
def test_draft_matches_saved_normalization_inheritance_and_group_anchors(client, kind, updating):
    parent, _ = _create(client, name="parent", signals=[_signal()], groups=[{
        "group_key": "inherited", "name": "Inherited", "position": 1000000, "step_anchor": 1000,
    }], steps=[
        {"step_key": "open", "feature_type": "flow.open", "group_key": "inherited", "position": 1000},
        _wait("base", position=3000, config={"seconds": 7}),
    ])
    values = {
        "template_id" if kind == "sites" else "parent_id": parent["id"],
        "groups": [{"group_key": "local", "name": " Local ", "position": 2000000, "step_anchor": 5000}],
        "signals": [_signal(description=" description ", exclude_texts=[" no ", ""])],
        "steps": [
            _wait("base", position=99, enabled=None, config={"seconds": None}),
            _wait("own", position=5000, group_key="local", condition=(
                "all(signal:ready:true,any(signal:ready:false,signal:sys_resign_pending:true))"
            )),
        ],
    }
    if kind == "sites":
        values.update({
            "base_url": "  https://example.com/path///  ", "tags": [" alpha ", "alpha", "", "beta"],
            "trusted_urls": [" ^https://trusted[.]example/ "], "lane_policy": "interaction",
            "bitwarden_enabled": True, "bitwarden_match_mode": "custom",
            "bitwarden_custom_match": " entry ",
        })
    owner = None
    if updating:
        owner, _ = _create(client, kind, name="existing", **values)
        # Null/omitted anchors and omitted trusted URLs preserve existing values on update.
        values["groups"][0]["step_anchor"] = None
        values.pop("trusted_urls", None)
    payload = _payload(kind, **values, expected_revision=1, expected_history_key=(
        owner["history_key"] if owner else str(uuid4())
    ))
    draft = _validate(client, kind, payload, owner["id"] if owner else None)
    _assert_success(draft, kind, owner)
    assert draft["local"]["steps"][0]["position"] == 3001
    assert draft["local"]["steps"][0]["config"] == {"seconds": None}
    assert draft["local"]["groups"][0]["name"] == "Local"
    assert draft["local"]["groups"][0]["step_anchor"] == 5000
    assert draft["local"]["signals"][0]["include_texts"] == ["ready"]
    assert draft["local"]["signals"][0]["description"] == "description"
    steps = {step["step_key"]: step for step in draft["resolved"]["steps"]}
    assert steps["base"]["config"]["seconds"] != 7
    assert steps["base"]["position"] == 3001
    assert steps["own"]["condition_unknown_action"] == "needs_human"
    assert {signal["signal_key"] for signal in draft["resolved"]["signals"]} == {
        "ready", "sys_resign_pending",
    }
    if kind == "sites":
        assert draft["local"]["base_url"] == "https://example.com/path"
        assert draft["local"]["tags"] == ["alpha", "beta"]
        assert draft["local"]["bitwarden_custom_match"] == "entry"
        assert draft["local"]["trusted_urls"] == ["^https://trusted[.]example/"]
        assert draft["resolved"]["lane"] == "interaction"
        assert steps["open"]["config"]["url"] == "https://example.com/path"
    url = f"/api/v1/{kind}"
    saved = (client.put(f"{url}/{owner['id']}", json=payload) if owner else
             client.post(url, json=payload))
    assert saved.status_code == (200 if owner else 201), saved.text
    saved = saved.json()
    assert {**draft["resolved"], "owner_id": saved["id"]} == saved["resolved"]
    revision = saved["config_revision" if kind == "sites" else "version"]
    history = client.get(f"{url}/{saved['id']}/versions/{revision}")
    assert history.status_code == 200, history.text
    assert draft["local"] == history.json()["snapshot"]["local"]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("updating", [False, True], ids=["create", "update"])
def test_descending_draft_groups_match_saved_local_and_resolved_order(client, kind, updating):
    parent, _ = _create(client, name="parent", groups=[{
        "group_key": "inherited", "name": "Inherited", "position": 1500000,
        "step_anchor": 1500,
    }], steps=[_wait("inherited", group_key="inherited", position=1500)])
    payload = _payload(kind, groups=[
        {"group_key": "third", "name": "Third", "position": 3000000, "step_anchor": 3000},
        {"group_key": "second", "name": "Second", "position": 2000000, "step_anchor": 2000},
        {"group_key": "first", "name": "First", "position": 1000000, "step_anchor": 1000},
    ], steps=[
        _wait("third", group_key="third", position=3000),
        _wait("second", group_key="second", position=2000),
        _wait("first", group_key="first", position=1000),
    ], **{"template_id" if kind == "sites" else "parent_id": parent["id"]})
    owner, _ = _create(client, kind, name="existing") if updating else (None, None)
    draft = _validate(client, kind, payload, owner["id"] if owner else None)
    _assert_success(draft, kind, owner)
    assert [group["group_key"] for group in draft["local"]["groups"]] == [
        "first", "second", "third",
    ]
    inherited = next(group for group in draft["resolved"]["groups"] if group["group_key"] == "inherited")
    assert inherited["position"] == 4500000
    url = f"/api/v1/{kind}"
    response = (client.put(f"{url}/{owner['id']}", json=payload) if owner else
                client.post(url, json=payload))
    assert response.status_code == (200 if updating else 201), response.text
    saved = response.json()
    assert draft["local"]["groups"] == saved["groups"]
    assert {**draft["resolved"], "owner_id": saved["id"]} == saved["resolved"]
    revision = saved["config_revision" if kind == "sites" else "version"]
    history = client.get(f"{url}/{saved['id']}/versions/{revision}")
    assert history.status_code == 200, history.text
    assert draft["local"] == history.json()["snapshot"]["local"]


@pytest.mark.parametrize("trusted_urls", [None, []], ids=["omitted", "explicit-empty"])
def test_site_draft_trusted_urls_update_semantics_match_save(client, trusted_urls):
    owner, payload = _create(client, "sites", trusted_urls=["^https://trusted[.]example/"])
    payload.pop("trusted_urls")
    if trusted_urls is not None:
        payload["trusted_urls"] = trusted_urls
    draft = _validate(client, "sites", payload, owner["id"])
    assert draft["local"]["trusted_urls"] == (owner["trusted_urls"] if trusted_urls is None else [])
    response = client.put(f"/api/v1/sites/{owner['id']}", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["trusted_urls"] == draft["local"]["trusted_urls"]


INVALID_INPUTS = [
    pytest.param({"name": ""}, id="empty-name"),
    pytest.param({"unexpected": True}, id="extra-field"),
    pytest.param({"expected_revision": 0}, id="invalid-revision"),
    pytest.param({"expected_revision": True}, id="boolean-revision"),
    pytest.param({"expected_history_key": "not-a-uuid"}, id="invalid-history-key"),
    pytest.param({"steps": [{"step_key": "INVALID", "feature_type": "action.wait"}]}, id="step-key"),
    pytest.param({"steps": [_wait(timeout_seconds=0)]}, id="timeout"),
    pytest.param({"groups": [{"group_key": "g", "name": "G", "step_anchor": -1}]}, id="anchor"),
]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("updating", [False, True], ids=["create", "update"])
@pytest.mark.parametrize("changes", INVALID_INPUTS)
def test_draft_schema_failures_match_save_without_writes(client, kind, updating, changes):
    owner, _ = _create(client, kind) if updating else (None, None)
    _reject_like_save(client, kind, _payload(kind, **changes), owner["id"] if owner else None)


@pytest.mark.parametrize("changes", [
    {"base_url": "ftp://example.com"}, {"trusted_urls": ["["]},
    {"trusted_urls": [123]}, {"checkin_after": "25:00"},
    {"bitwarden_enabled": True, "bitwarden_match_mode": "custom"},
])
def test_site_draft_schema_checks_match_save(client, changes):
    _reject_like_save(client, "sites", _payload("sites", **changes))


INVALID_WORKFLOWS = [
    pytest.param({"steps": [_wait(), _wait()]}, id="duplicate-step"),
    pytest.param({"steps": [{"step_key": "unknown", "feature_type": "missing.feature"}]}, id="feature"),
    pytest.param({"steps": [_wait(config={"seconds": -1})]}, id="config-range"),
    pytest.param({"steps": [_wait(config={"seconds": "bad"})]}, id="config-type"),
    pytest.param({"steps": [_wait(config={"unknown": True})]}, id="config-extra"),
    pytest.param({"steps": [{"step_key": "totp", "feature_type": "challenge.2fa"}]}, id="required-config"),
    pytest.param({"steps": [
        {"step_key": "a", "feature_type": "flow.open"},
        {"step_key": "b", "feature_type": "flow.open"},
    ]}, id="singleton"),
    pytest.param({"steps": [_wait(condition="any()")]}, id="condition-syntax"),
    pytest.param({"steps": [_wait(condition="signal:missing:true")]}, id="missing-signal"),
    pytest.param({"signals": [_signal(enabled=False)], "steps": [
        _wait(condition="signal:ready:true"),
    ]}, id="disabled-signal"),
    pytest.param({"signals": [_signal(), _signal()]}, id="duplicate-signal"),
    pytest.param({"signals": [_signal(include_texts=[" "])]}, id="empty-signal"),
    pytest.param({"signals": [_signal(signal_key="sys_resign_pending")]}, id="reserved-signal"),
    pytest.param({"groups": [
        {"group_key": "g", "name": "One"}, {"group_key": "g", "name": "Two"},
    ]}, id="duplicate-group"),
    pytest.param({"groups": [{"group_key": "g", "name": " "}]}, id="blank-group-name"),
    pytest.param({"steps": [_wait(group_key="missing")]}, id="missing-group"),
    pytest.param({"steps": [_wait("a", position=1000), _wait("b", position=1000)]}, id="position-collision"),
]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("updating", [False, True], ids=["create", "update"])
@pytest.mark.parametrize("changes", INVALID_WORKFLOWS)
def test_draft_semantic_failures_match_save_without_writes(client, kind, updating, changes):
    owner, _ = _create(client, kind) if updating else (None, None)
    _reject_like_save(client, kind, _payload(kind, **changes), owner["id"] if owner else None)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("updating", [False, True], ids=["create", "update"])
def test_draft_missing_dependency_and_target_are_404(client, kind, updating):
    owner, _ = _create(client, kind) if updating else (None, None)
    dependency = "template_id" if kind == "sites" else "parent_id"
    payload = _payload(kind, **{dependency: 999999})
    _reject_like_save(client, kind, payload, owner["id"] if owner else None, status=404)
    _reject_like_save(client, kind, _payload(kind), 999999, status=404)


@pytest.mark.parametrize("kind", KINDS)
def test_draft_name_conflicts_and_unchanged_name_match_save(client, kind):
    owner, payload = _create(client, kind, name="taken")
    _reject_like_save(client, kind, payload, status=409)
    other, _ = _create(client, kind, name="other")
    _reject_like_save(client, kind, payload, other["id"], status=409)
    _assert_success(_validate(client, kind, payload, owner["id"]), kind, owner)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("conflict", ["revision", "identity", "missing-history", "aba"])
def test_draft_revision_and_history_conflicts_match_save(client, kind, conflict):
    owner, payload = _create(client, kind)
    owner_id = owner["id"]
    url = f"/api/v1/{kind}/{owner_id}"
    if conflict == "revision":
        assert client.put(url, json=payload).status_code == 200
        payload["expected_revision"] = 1
    elif conflict == "identity":
        payload["expected_history_key"] = str(uuid4())
    elif conflict == "missing-history":
        with client.app.state.session_factory() as session:
            session.execute(delete(WorkflowHistory).where(WorkflowHistory.history_key == owner["history_key"]))
            session.commit()
    else:
        assert client.delete(url).status_code == 204
        replacement, _ = _create(client, kind, name="replacement")
        assert replacement["id"] == owner_id
        assert replacement["history_key"] != owner["history_key"]
        payload.update(expected_revision=1, expected_history_key=owner["history_key"])
    _reject_like_save(client, kind, payload, owner_id, status=409)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("conflict", ["group-override", "inherited-group", "group-move", "feature-change", "position"])
def test_draft_inherited_conflicts_match_save(client, kind, conflict):
    parent, _ = _create(client, name="parent", groups=[{
        "group_key": "inherited", "name": "Inherited",
    }], steps=[
        _wait("grouped", group_key="inherited", position=1000),
        _wait("ungrouped", position=3000),
    ])
    payload = _payload(kind, **{"template_id" if kind == "sites" else "parent_id": parent["id"]})
    if conflict == "group-override":
        payload["groups"] = [{"group_key": "inherited", "name": "Override"}]
    elif conflict == "inherited-group":
        payload["steps"] = [_wait("local", group_key="inherited")]
    elif conflict == "group-move":
        payload["groups"] = [{"group_key": "local", "name": "Local"}]
        payload["steps"] = [_wait("grouped", group_key="local")]
    elif conflict == "feature-change":
        payload["steps"] = [{"step_key": "ungrouped", "feature_type": "flow.open"}]
    else:
        payload["steps"] = [_wait("local", position=3000)]
    _reject_like_save(client, kind, payload)


@pytest.mark.parametrize("self_parent", [True, False], ids=["self", "cycle"])
def test_template_draft_rejects_cycles_without_mutating_graph(client, self_parent):
    parent, payload = _create(client, name="parent")
    child, _ = _create(client, name="child", parent_id=parent["id"])
    payload["parent_id"] = parent["id"] if self_parent else child["id"]
    _reject_like_save(client, "templates", payload, parent["id"])


@pytest.mark.parametrize("dependent_kind", KINDS)
@pytest.mark.parametrize("change", ["parent", "rename-group", "remove-group", "reorder-groups", "move-step", "remove-overridden-step"])
def test_used_template_draft_structure_restrictions_match_save(client, dependent_kind, change):
    parent, payload = _create(client, name="parent", groups=[
        {"group_key": "first", "name": "First", "position": 1000000},
        {"group_key": "second", "name": "Second", "position": 2000000},
    ], steps=[_wait(group_key="first")])
    _create(client, dependent_kind, name="dependent", steps=[_wait()], **{
        "template_id" if dependent_kind == "sites" else "parent_id": parent["id"],
    })
    if change == "parent":
        other, _ = _create(client, name="other")
        payload["parent_id"] = other["id"]
    elif change == "rename-group":
        payload["groups"][0]["name"] = "Renamed"
    elif change == "remove-group":
        payload["groups"] = payload["groups"][:1]
    elif change == "reorder-groups":
        payload["groups"][0]["position"] = 3000000
    elif change == "move-step":
        payload["steps"][0]["group_key"] = "second"
    else:
        payload["steps"] = []
    _reject_like_save(client, "templates", payload, parent["id"], status=409)


def test_used_template_draft_allows_additive_changes_and_matches_save(client):
    parent, payload = _create(client, name="parent", groups=[{
        "group_key": "existing", "name": "Existing", "position": 1000000,
    }], steps=[_wait(group_key="existing")])
    _create(client, "sites", template_id=parent["id"])
    payload["groups"].append({"group_key": "added", "name": "Added", "position": 2000000})
    payload["steps"].append(_wait("added", group_key="added", position=3000))
    draft = _validate(client, "templates", payload, parent["id"])
    _assert_success(draft, "templates", parent)
    saved = client.put(f"/api/v1/templates/{parent['id']}", json=payload)
    assert saved.status_code == 200, saved.text
    assert draft["resolved"] == saved.json()["resolved"]


@pytest.mark.parametrize("kind", KINDS)
def test_draft_signal_removal_uses_new_steps_and_parent(client, kind):
    owner, payload = _create(client, kind, signals=[_signal()], steps=[_wait(condition="signal:ready:true")])
    payload["signals"] = []
    _reject_like_save(client, kind, payload, owner["id"], status=409)
    # Removing the reference in the same draft makes removal legal.
    payload["steps"][0]["condition"] = "always"
    _assert_success(_validate(client, kind, payload, owner["id"]), kind, owner)
    # A new parent can supply the removed local signal; the old parent must not be used.
    parent, _ = _create(client, name="signal-parent", signals=[_signal()])
    payload["steps"][0]["condition"] = "signal:ready:true"
    payload["template_id" if kind == "sites" else "parent_id"] = parent["id"]
    draft = _validate(client, kind, payload, owner["id"])
    assert next(s for s in draft["resolved"]["signals"] if s["signal_key"] == "ready")["source"] == "template:signal-parent"
    # Detaching a parent that supplied the signal is a semantic failure, not a local-removal conflict.
    inherited, inherited_payload = _create(client, kind, name="inherited", steps=payload["steps"], **{
        "template_id" if kind == "sites" else "parent_id": parent["id"],
    })
    inherited_payload["template_id" if kind == "sites" else "parent_id"] = None
    _reject_like_save(client, kind, inherited_payload, inherited["id"])


@pytest.mark.parametrize("kind", KINDS)
def test_template_draft_cannot_remove_signals_referenced_by_descendants(client, kind):
    parent, payload = _create(client, name="parent", signals=[_signal()])
    child, _ = _create(client, name="child", parent_id=parent["id"])
    _create(client, kind, name="dependent", steps=[_wait(condition="signal:ready:true")], **{
        "template_id" if kind == "sites" else "parent_id": child["id"],
    })
    payload["signals"] = []
    _reject_like_save(client, "templates", payload, parent["id"], status=409)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("change", ["feature", "position", "group"])
def test_template_draft_validates_transitive_descendants(client, kind, change):
    parent, payload = _create(client, name="parent", steps=[_wait("base", position=1000)])
    child, _ = _create(client, name="child", parent_id=parent["id"])
    _create(client, kind, name="dependent", groups=[{
        "group_key": "local", "name": "Local", "position": 1000000,
    }], steps=[_wait("base"), _wait("own", position=3000)], **{
        "template_id" if kind == "sites" else "parent_id": child["id"],
    })
    if change == "feature":
        payload["steps"] = [{"step_key": "base", "feature_type": "flow.open", "position": 1000}]
    elif change == "position":
        payload["steps"][0]["position"] = 2999
    else:
        payload["groups"] = [{"group_key": "local", "name": "Conflict"}]
    _reject_like_save(client, "templates", payload, parent["id"])


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("updating", [False, True], ids=["create", "update"])
@pytest.mark.parametrize("auth_state", ["anonymous", "missing-csrf", "missing-csrf-cookie", "wrong-csrf", "forged-csrf", "stale", "expired"])
def test_draft_auth_and_csrf_never_write_even_for_stale_or_expired_sessions(client, kind, updating, auth_state):
    owner, payload = _create(client, kind) if updating else (None, _payload(kind))
    if auth_state in {"stale", "expired"}:
        now = datetime.now(timezone.utc)
        with client.app.state.session_factory() as session:
            auth = session.scalar(select(AuthSession))
            auth.last_seen_at = now - timedelta(minutes=5)
            if auth_state == "expired":
                auth.expires_at = now - timedelta(seconds=1)
            session.commit()
    elif auth_state == "anonymous":
        client.cookies.clear()
    elif auth_state == "missing-csrf":
        client.headers.pop("X-CSRF-Token")
    elif auth_state == "missing-csrf-cookie":
        from app.core.auth import CSRF_COOKIE

        client.cookies.delete(CSRF_COOKIE, domain="testserver.local", path="/")
    elif auth_state == "wrong-csrf":
        client.headers["X-CSRF-Token"] = "wrong"
    else:
        from app.core.auth import CSRF_COOKIE

        client.cookies.set(CSRF_COOKIE, "forged", domain="testserver.local", path="/")
        client.headers["X-CSRF-Token"] = "forged"
    status = 200 if auth_state == "stale" else (401 if auth_state in {"anonymous", "expired"} else 403)
    body = _validate(client, kind, payload, owner["id"] if owner else None, status)
    if status == 200:
        _assert_success(body, kind, owner)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("status", [404, 409, 422])
def test_stale_auth_session_is_not_touched_on_draft_errors(client, kind, status):
    owner, payload = _create(client, kind)
    owner_id = owner["id"]
    if status == 404:
        owner_id = 999999
    elif status == 409:
        payload["expected_revision"] = 2
    else:
        payload["steps"] = [_wait(config={"seconds": -1})]
    with client.app.state.session_factory() as session:
        auth = session.scalar(select(AuthSession))
        auth.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        session.commit()
    _validate(client, kind, payload, owner_id, status)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("updating", [False, True], ids=["create", "update"])
def test_malformed_json_draft_errors_are_uncacheable_and_read_only(client, kind, updating):
    owner, _ = _create(client, kind) if updating else (None, None)
    suffix = f"/{owner['id']}" if owner else ""
    with _read_only(client):
        response = client.post(
            f"/api/v1/{kind}{suffix}/validate", content="{", headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 422, response.text
    assert response.headers.get("cache-control") == "no-store"


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("updating", [False, True], ids=["create", "update"])
def test_draft_service_preserves_input_and_pending_session_state(client, monkeypatch, kind, updating):
    parent, _ = _create(client, name="parent", steps=[_wait(config={"seconds": 7})])
    owner, _ = _create(client, kind, name="existing") if updating else (None, None)
    app = client.app
    with app.state.session_factory() as session:
        session.add(AppSetting(key="draft.sentinel", value_json='{"unchanged":true}'))
        session.commit()
        sentinel = session.get(AppSetting, "draft.sentinel")
        sentinel.value_json = '{"pending":true}'
        pending = Template(name="pending-unrelated", description="not persisted")
        session.add(pending)
        session.autoflush = True
        service = WorkflowService(session, app.state.settings, app.state.crypto)
        model = SiteWrite if kind == "sites" else TemplateWrite
        payload = model.model_validate(_payload(kind, groups=[{
            "group_key": "local", "name": " Local ", "step_anchor": None,
        }], signals=[_signal()], steps=[_wait(position=42, config={"seconds": None})], **{
            "template_id" if kind == "sites" else "parent_id": parent["id"],
        }))
        original = payload.model_copy(deep=True)
        fields_set = deepcopy(payload.model_fields_set)
        before_state = (set(session.new), set(session.dirty), set(session.deleted))

        def forbidden(*args, **kwargs):
            pytest.fail("Draft validation must not commit, flush, or roll back the caller's session")

        with monkeypatch.context() as patch, _read_only(client):
            for method in ("commit", "flush", "rollback"):
                patch.setattr(session, method, forbidden)
            body = service.validate_draft(kind[:-1], payload, owner["id"] if owner else None)
            _assert_success(body, kind, owner)
            assert body["local"]["steps"][0]["position"] == 1001
            invalid = payload.model_copy(deep=True)
            invalid.steps[0].feature_type = "missing.feature"
            invalid_before = invalid.model_copy(deep=True)
            with pytest.raises(WorkflowValidationError):
                service.validate_draft(kind[:-1], invalid, owner["id"] if owner else None)
            assert invalid == invalid_before
        assert payload == original
        assert payload.model_fields_set == fields_set
        assert payload.steps[0].position == 42
        assert (set(session.new), set(session.dirty), set(session.deleted)) == before_state
        assert sentinel.value_json == '{"pending":true}'
        assert pending.id is None
        if owner:
            with session.no_autoflush:
                persisted = session.get(Site if kind == "sites" else Template, owner["id"])
            assert persisted.name == "existing"
            assert persisted.history_key == owner["history_key"]
            assert service._revision(persisted) == 1


@pytest.mark.parametrize("kind", KINDS)
def test_repeated_service_validation_uses_fresh_candidates_and_not_callers_caches(client, kind):
    parent, parent_payload = _create(client, name="parent", signals=[_signal()], steps=[_wait(config={"seconds": 2})])
    owner, _ = _create(client, kind, name="existing", **{
        "template_id" if kind == "sites" else "parent_id": parent["id"],
    })
    app = client.app
    with app.state.session_factory() as session:
        service = WorkflowService(session, app.state.settings, app.state.crypto)
        service._preload_site_list()
        resolve = service.resolve_site if kind == "sites" else service.resolve_template
        original_resolved = resolve(owner["id"]).model_dump(mode="json")
        model = SiteWrite if kind == "sites" else TemplateWrite
        payload = model.model_validate(_payload(kind, **{
            "template_id" if kind == "sites" else "parent_id": parent["id"],
        }))
        with _read_only(client):
            first = service.validate_draft(kind[:-1], payload, owner["id"])
            changed = payload.model_copy(deep=True)
            changed.name = "candidate-two"
            changed.steps = [StepInput(**_wait(config={"seconds": 9}))]
            second = service.validate_draft(kind[:-1], changed, owner["id"])
            assert second["resolved"]["name"] == "candidate-two"
            assert second["resolved"]["steps"][0]["config"]["seconds"] == 9
            second["resolved"]["signals"][0]["label"] = "caller-mutated"
            second["local"]["steps"][0]["config"]["seconds"] = 999
            assert service.validate_draft(kind[:-1], payload, owner["id"]) == first
            assert resolve(owner["id"]).model_dump(mode="json") == original_resolved
        parent_payload["steps"][0]["config"]["seconds"] = 6
        parent_payload["signals"][0]["label"] = "Fresh signal"
        response = client.put(f"/api/v1/templates/{parent['id']}", json=parent_payload)
        assert response.status_code == 200, response.text
        with _read_only(client):
            fresh = service.validate_draft(kind[:-1], payload, owner["id"])
        assert fresh["resolved"]["steps"][0]["config"]["seconds"] == 6
        assert fresh["resolved"]["signals"][0]["label"] == "Fresh signal"
        with app.state.session_factory() as writer:
            writer.add(AppSetting(key="feature.action.wait", value_json=json.dumps({
                "defaults": {"seconds": 8}, "required_fields": [],
            })))
            writer.commit()
        cleared = payload.model_copy(deep=True)
        cleared.steps = [StepInput(**_wait(config={"seconds": None}))]
        with _read_only(client):
            fresh_policy = service.validate_draft(kind[:-1], cleared, owner["id"])
        assert fresh_policy["resolved"]["steps"][0]["config"]["seconds"] == 8


@pytest.mark.parametrize("kind", KINDS)
def test_draft_uses_current_owner_revision_without_refreshing_callers_pending_edits(client, monkeypatch, kind):
    owner, payload = _create(client, kind, groups=[{
        "group_key": "local", "name": "Local", "step_anchor": 1000,
    }], steps=[_wait(group_key="local", position=1000)], **(
        {"trusted_urls": ["^https://old[.]example/"]} if kind == "sites" else {}
    ))
    app = client.app
    owner_model = Site if kind == "sites" else Template
    write_model = SiteWrite if kind == "sites" else TemplateWrite
    revision_field = "config_revision" if kind == "sites" else "version"
    with app.state.session_factory() as session:
        stale = session.get(owner_model, owner["id"])
        service = WorkflowService(session, app.state.settings, app.state.crypto)
        service._preload_site_list()
        stale_group = service._groups(kind[:-1], owner["id"])[0]
        assert getattr(stale, revision_field) == 1
        changed = deepcopy(payload)
        changed["name"] = "externally-updated"
        changed["groups"][0]["step_anchor"] = 4000
        if kind == "sites":
            changed["trusted_urls"] = ["^https://fresh[.]example/"]
        response = client.put(f"/api/v1/{kind}/{owner['id']}", json=changed)
        assert response.status_code == 200, response.text
        current = response.json()
        assert current[revision_field] == 2
        assert getattr(stale, revision_field) == 1
        assert stale_group.step_anchor == 1000
        stale.name = "unsaved-caller-name"
        stale.history_key = str(uuid4())
        stale_group.step_anchor = 9999
        if kind == "sites":
            stale.trusted_urls_json = '["unsaved-caller-pattern"]'
        pending = Template(name="pending-unrelated", description="not persisted")
        session.add(pending)
        session.autoflush = True
        before_state = (set(session.new), set(session.dirty), set(session.deleted))
        before_owner = {column.key: getattr(stale, column.key) for column in owner_model.__table__.columns}
        candidate = write_model.model_validate(_payload(kind, expected_revision=2,
            expected_history_key=current["history_key"], groups=[{
                "group_key": "local", "name": "Local", "step_anchor": None,
            }], steps=payload["steps"]))
        original = candidate.model_copy(deep=True)

        def forbidden(*args, **kwargs):
            pytest.fail("Draft validation must preserve the caller's transaction and pending edits")

        with monkeypatch.context() as patch, _read_only(client):
            for method in ("commit", "flush", "rollback"):
                patch.setattr(session, method, forbidden)
            draft = service.validate_draft(kind[:-1], candidate, owner["id"])
            _assert_success(draft, kind, current)
            assert draft["local"]["groups"][0]["step_anchor"] == 4000
            assert draft["resolved"]["name"] == "draft"
            if kind == "sites":
                assert draft["local"]["trusted_urls"] == ["^https://fresh[.]example/"]
            with pytest.raises(WorkflowConflictError):
                service.validate_draft(kind[:-1], candidate.model_copy(update={"expected_revision": 1}), owner["id"])
        assert candidate == original
        assert (set(session.new), set(session.dirty), set(session.deleted)) == before_state
        assert {column.key: getattr(stale, column.key) for column in owner_model.__table__.columns} == before_owner
        assert getattr(stale, revision_field) == 1
        assert stale_group.step_anchor == 9999
        assert pending.id is None


def test_template_draft_checks_fresh_descendant_site_without_refreshing_caller(client):
    parent, payload = _create(client, name="parent", steps=[_wait("base", position=1000)])
    other, _ = _create(client, name="other")
    site, site_payload = _create(client, "sites", template_id=other["id"], steps=[_wait("own", position=3000)])
    app = client.app
    with app.state.session_factory() as session:
        stale_site = session.get(Site, site["id"])
        service = WorkflowService(session, app.state.settings, app.state.crypto)
        service._preload_site_list()
        response = client.put(f"/api/v1/sites/{site['id']}", json={**site_payload, "template_id": parent["id"]})
        assert response.status_code == 200, response.text
        assert stale_site.template_id == other["id"]
        stale_site.notes = "unsaved-caller-notes"
        pending = Template(name="pending-unrelated", description="not persisted")
        session.add(pending)
        session.autoflush = True
        before_state = (set(session.new), set(session.dirty), set(session.deleted))
        payload["steps"][0]["position"] = 3000
        candidate = TemplateWrite.model_validate(payload)
        with _read_only(client), pytest.raises(WorkflowValidationError, match="Ambiguous ungrouped step position"):
            service.validate_draft("template", candidate, parent["id"])
        assert (set(session.new), set(session.dirty), set(session.deleted)) == before_state
        assert stale_site.template_id == other["id"]
        assert stale_site.config_revision == 1
        assert stale_site.notes == "unsaved-caller-notes"
        assert pending.id is None
    _reject_like_save(client, "templates", payload, parent["id"])


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("corrupt", [False, True], ids=["custom-settings", "malformed-settings"])
def test_draft_reads_feature_policy_and_snapshot_settings_without_repair_writes(client, kind, corrupt):
    feature = "flow.result"
    policy = {
        "defaults": {"message": "custom-default"}, "required_fields": [],
        "execution_settings": {"llm_enabled": "not-a-boolean"} if corrupt else {"llm_enabled": True},
    }
    with client.app.state.session_factory() as session:
        session.add(AppSetting(key=f"feature.{feature}", value_json=json.dumps(policy)))
        session.commit()
    payload = _payload(kind, steps=[{"step_key": "result", "feature_type": feature}])
    if corrupt:
        _reject_like_save(client, kind, payload)
    else:
        draft = _validate(client, kind, payload)
        assert draft["resolved"]["steps"][0]["config"]["message"] == "custom-default"
        assert draft["local"]["steps"][0]["config"] == {}
        saved = client.post(f"/api/v1/{kind}", json=payload)
        assert saved.status_code == 201, saved.text
        assert {**draft["resolved"], "owner_id": saved.json()["id"]} == saved.json()["resolved"]


@pytest.mark.parametrize("kind", KINDS)
def test_draft_feature_policy_and_execution_settings_ignore_callers_identity_map(client, kind):
    app = client.app
    key = "feature.flow.result"
    initial = json.dumps({
        "defaults": {"message": "old-default"}, "required_fields": [],
        "execution_settings": {"llm_enabled": "invalid-stale-setting"},
    })
    with app.state.session_factory() as writer:
        writer.add(AppSetting(key=key, value_json=initial))
        writer.commit()
    model = SiteWrite if kind == "sites" else TemplateWrite
    payload = model.model_validate(_payload(kind, steps=[{
        "step_key": "result", "feature_type": "flow.result",
    }]))
    with app.state.session_factory() as session:
        held = session.get(AppSetting, key)
        service = WorkflowService(session, app.state.settings, app.state.crypto)
        pending = Template(name="pending-unrelated", description="not persisted")
        session.add(pending)
        session.autoflush = True
        for corrupt in (False, True):
            committed = json.dumps({
                "defaults": {"message": "committed-default"}, "required_fields": [],
                "execution_settings": {"llm_enabled": "invalid" if corrupt else True},
            })
            with app.state.session_factory() as writer:
                writer.get(AppSetting, key).value_json = committed
                writer.commit()
            if not corrupt:
                assert held.value_json == initial
            held.value_json = json.dumps({
                "defaults": {"message": "pending-caller-default"}, "required_fields": [],
                "execution_settings": {"llm_enabled": False if corrupt else "invalid-pending-setting"},
            })
            caller_value = held.value_json
            before_state = (set(session.new), set(session.dirty), set(session.deleted))
            with _read_only(client):
                if corrupt:
                    with pytest.raises(WorkflowValidationError):
                        service.validate_draft(kind[:-1], payload)
                else:
                    draft = service.validate_draft(kind[:-1], payload)
                    _assert_success(draft, kind)
                    assert draft["resolved"]["steps"][0]["config"]["message"] == "committed-default"
            assert held.value_json == caller_value
            assert (set(session.new), set(session.dirty), set(session.deleted)) == before_state
            assert pending.id is None


@pytest.mark.parametrize("kind", KINDS)
def test_draft_cfbox_reads_committed_rows_without_refreshing_caller(tmp_path, kind):
    settings = make_settings(tmp_path, cfbox_url=None, cfbox_api_key=None)
    client = TestClient(create_app(settings))
    app = client.app
    with app.state.session_factory() as writer:
        integrations = IntegrationService(writer, settings, app.state.crypto)
        integrations._upsert("cfbox.url", "ftp://stale.example", "test")
        integrations._upsert("cfbox.api_key", "old-api-key", "test")
        writer.commit()
    model = SiteWrite if kind == "sites" else TemplateWrite
    payload = model.model_validate(_payload(kind, steps=[{
        "step_key": "turnstile", "feature_type": "challenge.turnstile",
    }]))
    with app.state.session_factory() as session:
        held_url = session.get(IntegrationConfig, "cfbox.url")
        held_key = session.get(IntegrationConfig, "cfbox.api_key")
        stale_ciphertext = held_url.value_ciphertext
        service = WorkflowService(session, settings, app.state.crypto)
        pending = Template(name="pending-unrelated", description="not persisted")
        session.add(pending)
        session.autoflush = True
        for enabled in (True, False):
            with app.state.session_factory() as writer:
                integrations = IntegrationService(writer, settings, app.state.crypto)
                integrations._upsert("cfbox.url", "https://committed.example" if enabled else "ftp://disabled.example", "test")
                integrations._upsert("cfbox.api_key", "fresh-api-key", "test")
                writer.commit()
            if enabled:
                assert held_url.value_ciphertext == stale_ciphertext
            # Keep conflicting, uncommitted integration values strongly referenced.
            encrypted = app.state.crypto.encrypt("cfbox.url", "ftp://pending.example" if enabled else "https://pending.example")
            held_url.value_ciphertext = encrypted.ciphertext
            held_url.nonce = encrypted.nonce
            held_url.key_version = encrypted.key_version
            held_key.value_ciphertext = b"invalid-pending-ciphertext"
            held_key.nonce = b"invalid-pending-nonce"
            before_values = [
                {column.key: getattr(row, column.key) for column in IntegrationConfig.__table__.columns}
                for row in (held_url, held_key)
            ]
            before_state = (set(session.new), set(session.dirty), set(session.deleted))
            with _read_only(client):
                if enabled:
                    _assert_success(service.validate_draft(kind[:-1], payload), kind)
                else:
                    with pytest.raises(WorkflowValidationError, match="CFbox"):
                        service.validate_draft(kind[:-1], payload)
            assert [
                {column.key: getattr(row, column.key) for column in IntegrationConfig.__table__.columns}
                for row in (held_url, held_key)
            ] == before_values
            assert (set(session.new), set(session.dirty), set(session.deleted)) == before_state
            assert pending.id is None


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("updating", [False, True], ids=["create", "update"])
def test_draft_does_not_encrypt_decrypt_fetch_credentials_or_call_network(client, monkeypatch, kind, updating):
    owner, _ = _create(client, kind) if updating else (None, None)
    if kind == "sites" and owner:
        url = f"/api/v1/sites/{owner['id']}"
        assert client.put(f"{url}/credential", json={"username": "private-user", "password": "private-password"}).status_code == 200
        assert client.put(f"{url}/cookies", json={"cookies": "session=private-cookie"}).status_code == 200
    payload = _payload(kind, steps=[{
        "step_key": "login", "feature_type": "auth.form_login", "config": {
            "username_selector": ["#user"], "password_selector": ["#pass"], "submit_selector": ["button"],
        },
    }, {
        "step_key": "totp", "feature_type": "challenge.2fa", "config": {"service_hint": "example"},
    }])

    def forbidden(*args, **kwargs):
        pytest.fail("Draft validation accessed encryption, credentials, or external services")

    with monkeypatch.context() as patch:
        patch.setattr(client.app.state.crypto, "encrypt", forbidden)
        patch.setattr(client.app.state.crypto, "decrypt", forbidden)
        patch.setattr(CredentialService, "preload", forbidden)
        patch.setattr(CredentialService, "view", forbidden)
        patch.setattr(CredentialService, "resolve", forbidden)
        patch.setattr(CredentialService, "resolve_cookies", forbidden)
        patch.setattr(BitwardenService, "resolve_for_site", forbidden)
        # Patch transports rather than Client.send, which TestClient itself uses.
        patch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
        patch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
        patch.setattr(socket.socket, "connect", forbidden)
        draft = _validate(client, kind, payload, owner["id"] if owner else None)
        _assert_success(draft, kind, owner)
        payload["steps"].append(_wait(condition="signal:missing:true"))
        _validate(client, kind, payload, owner["id"] if owner else None, status=422)
    serialized = json.dumps(draft)
    for secret in ("private-user", "private-password", "private-cookie"):
        assert secret not in serialized


def test_template_free_site_create_draft_skips_workflow_preload_and_queries(client, monkeypatch):
    parent, _ = _create(client, name="unrelated-template", signals=[_signal()], groups=[{
        "group_key": "unrelated", "name": "Unrelated",
    }], steps=[_wait(group_key="unrelated")])
    _create(client, "sites", name="unrelated-site", template_id=parent["id"])
    payload = _payload("sites", template_id=None, signals=[_signal()], groups=[{
        "group_key": "local", "name": "Local",
    }], steps=[_wait(group_key="local", condition="signal:ready:true")])

    def forbidden(*args, **kwargs):
        pytest.fail("Template-free site create drafts must not preload workflows")

    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if re.match(r"\s*SELECT\b", statement, re.I):
            statements.append(statement)

    monkeypatch.setattr(_DraftWorkflowService, "_preload_site_list", forbidden)
    engine = client.app.state.engine
    with _read_only(client):
        event.listen(engine, "before_cursor_execute", capture)
        try:
            response = client.post("/api/v1/sites/validate", json=payload)
        finally:
            event.remove(engine, "before_cursor_execute", capture)
    assert response.status_code == 200, response.text
    draft = response.json()
    _assert_success(draft, "sites")
    assert draft["local"]["template_id"] is None
    assert [step["step_key"] for step in draft["resolved"]["steps"]] == ["wait"]
    assert [group["group_key"] for group in draft["resolved"]["groups"]] == ["local"]
    assert "ready" in {signal["signal_key"] for signal in draft["resolved"]["signals"]}
    assert statements
    assert not [statement for statement in statements if re.search(
        r"\b(workflow_steps|workflow_signals|workflow_groups|templates)\b", statement, re.I,
    )]


@pytest.mark.parametrize("kind", KINDS)
def test_repeated_api_drafts_do_not_share_candidate_caches(client, kind):
    first = _payload(kind, steps=[_wait(config={"seconds": 3})])
    original = _validate(client, kind, first)
    second = _payload(kind, name="second", steps=[_wait(config={"seconds": 9})])
    changed = _validate(client, kind, second)
    assert changed["resolved"]["name"] == "second"
    assert changed["resolved"]["steps"][0]["config"]["seconds"] == 9
    _validate(client, kind, _payload(kind, steps=[_wait(condition="signal:missing:true")]), status=422)
    assert _validate(client, kind, first) == original


@pytest.mark.parametrize("kind", KINDS)
def test_draft_cfbox_requirement_matches_save_without_network(tmp_path, monkeypatch, kind):
    settings = make_settings(tmp_path, cfbox_url=None, cfbox_api_key=None)
    client = TestClient(create_app(settings))
    login_client(client)
    payload = _payload(kind, steps=[{"step_key": "turnstile", "feature_type": "challenge.turnstile"}])

    def forbidden(*args, **kwargs):
        pytest.fail("Integration availability validation must not call the network")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    _reject_like_save(client, kind, payload)
    payload["steps"][0]["enabled"] = False
    _assert_success(_validate(client, kind, payload), kind)


@pytest.mark.parametrize("owner_type,model", [("invalid", TemplateWrite), ("site", TemplateWrite), ("template", SiteWrite)])
def test_draft_service_rejects_wrong_owner_type_or_payload_without_writes(client, owner_type, model):
    app = client.app
    payload = model.model_validate(_payload("sites" if model is SiteWrite else "templates"))
    with app.state.session_factory() as session, _read_only(client):
        service = WorkflowService(session, app.state.settings, app.state.crypto)
        with pytest.raises(WorkflowValidationError):
            service.validate_draft(owner_type, payload)
        assert not session.new and not session.dirty and not session.deleted


@pytest.mark.parametrize("kind", KINDS)
def test_draft_service_not_found_and_conflict_leave_session_clean(client, kind):
    owner, payload = _create(client, kind)
    app = client.app
    model = SiteWrite if kind == "sites" else TemplateWrite
    with app.state.session_factory() as session, _read_only(client):
        service = WorkflowService(session, app.state.settings, app.state.crypto)
        with pytest.raises(WorkflowNotFoundError):
            service.validate_draft(kind[:-1], model.model_validate(payload), 999999)
        with pytest.raises(WorkflowConflictError):
            service.validate_draft(kind[:-1], model.model_validate(payload))
        with pytest.raises(WorkflowConflictError):
            service.validate_draft(kind[:-1], model.model_validate({**payload, "expected_revision": 2}), owner["id"])
        assert not session.new and not session.dirty and not session.deleted
