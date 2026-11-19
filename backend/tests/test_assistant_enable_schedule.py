from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.crypto import CryptoService
from app.core.db import Base, create_db_engine, create_session_factory
from app.models.assistant import AssistantMessage, AssistantSession
from app.models.assistant_run import AssistantRunGuard
from app.models.assistant_workflow import AssistantWorkflowAction
from app.models.execution import Run, SiteJob
from app.models.setting import AppSetting
from app.models.workflow import Site, Template
from app.schemas.execution import RunCreate
from app.schemas.workflow import SiteWrite, TemplateWrite
from app.services.assistant_run_guard import create_guard, require_enable_success, validate_guard
from app.services.assistant_workflows import WorkflowDenied
from app.services.credentials import CredentialService
from app.services.execution import ExecutionService
from app.services.executor import ExecutionManager
from app.services.workflows import WorkflowService


def seal(context, row, data):
    kind = "session" if isinstance(row, AssistantSession) else "message"
    identifier = row.id if kind == "session" else row.uuid
    encrypted = context.crypto.encrypt(f"assistant:{kind}:{identifier}", json.dumps(data))
    row.ciphertext, row.nonce, row.key_version = encrypted.ciphertext, encrypted.nonce, encrypted.key_version


@pytest.fixture
def context(settings, monkeypatch, fake_dispatch):
    engine = create_db_engine(settings.database_url)
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    context = SimpleNamespace(factory=factory, settings=settings, crypto=CryptoService(b"x" * 32),
                              owner="human-owner", session_id=str(uuid4()))
    with factory() as db:
        conversation = AssistantSession(id=context.session_id, owner=context.owner)
        seal(context, conversation, {})
        db.add(conversation)
        db.commit()
        service = WorkflowService(db, settings, context.crypto)
        template = service.create_template(TemplateWrite(name="parent"))
        site = service.create_site(SiteWrite(name="disabled", base_url="https://example.test",
                                            enabled=False, template_id=template.id,
                                            steps=[{"step_key": "open", "feature_type": "flow.open"}]))
        context.site_id, context.template_id = site.id, template.id
        run = ExecutionService(db, settings, context.crypto).create_run(
            RunCreate(site_ids=[site.id]), "real-human", trigger="assistant", _commit=False,
            _allow_disabled=True,
        )
        create_guard(db, run.id, site.id, service)
        db.add(AssistantWorkflowAction(
            owner=context.owner, session_id=context.session_id, kind="run", state="complete",
            request_fingerprint="full-run", site_id=site.id, site_history_key=site.history_key,
            site_revision=site.config_revision, run_id=run.id,
            ciphertext=b"not-read", nonce=b"not-read", key_version=1,
        ))
        db.commit()
        context.run_id = run.id
    manager = ExecutionManager(factory, settings, context.crypto)
    manager._post_execute = AsyncMock(return_value={"status": "success", "steps": []})
    monkeypatch.setattr(CredentialService, "resolve_cookies", Mock(return_value=None))
    monkeypatch.setattr(manager, "schedule_run_notification", Mock())
    asyncio.run(manager._execute_run(run.id))
    manager._post_execute.assert_awaited_once()
    yield context
    engine.dispose()


def require(context, db, **kwargs):
    return require_enable_success(
        db, kwargs.pop("owner", context.owner), kwargs.pop("session_id", context.session_id),
        db.get(Site, context.site_id), WorkflowService(db, context.settings, context.crypto), **kwargs,
    )


def test_full_success_can_enable_only_before_state_change(context):
    with context.factory() as db:
        assert db.get(Run, context.run_id).created_by == "real-human"
        assert require(context, db) == require(context, db, run_id=context.run_id) == context.run_id
        site = db.get(Site, context.site_id)
        assert not site.enabled
        result = WorkflowService(db, context.settings, context.crypto).patch_site_settings(
            site.id, {"enabled": True}, expected_revision=site.config_revision,
            expected_history_key=site.history_key, _commit=False,
        )
        assert result.enabled
        assert not validate_guard(db, context.run_id, site, context.settings, context.crypto)
        with pytest.raises(WorkflowDenied, match="enable_success_required"):
            require(context, db)
        db.rollback()
        assert not db.get(Site, context.site_id).enabled


@pytest.mark.parametrize("change", ["notes", "template"])
def test_current_guard_does_not_reuse_stale_loaded_rows(context, change):
    with context.factory() as db:
        site = db.get(Site, context.site_id)
        template = db.get(Template, context.template_id)
        assert require(context, db) == context.run_id
        with context.factory() as writer:
            if change == "notes":
                writer.get(Site, site.id).notes = "external change"
            else:
                writer.get(Template, template.id).version += 1
            writer.commit()
        with pytest.raises(WorkflowDenied, match="enable_success_required"):
            require(context, db)


def test_pending_enabled_change_is_not_discarded_to_accept_old_proof(context):
    with context.factory() as db:
        db.get(Site, context.site_id).enabled = True
        with pytest.raises(WorkflowDenied, match="enable_success_required"):
            require(context, db)
        assert db.get(Site, context.site_id).enabled
        db.rollback()
        assert not db.get(Site, context.site_id).enabled


def test_real_metadata_revision_requires_new_test_and_helper_never_commits(context, monkeypatch):
    with context.factory() as db:
        service = WorkflowService(db, context.settings, context.crypto)
        site = db.get(Site, context.site_id)
        commit = Mock(wraps=db.commit)
        monkeypatch.setattr(db, "commit", commit)
        assert require(context, db) == context.run_id
        service.patch_site_settings(site.id, {"checkin_after": "12:00"},
                                    expected_revision=1, expected_history_key=site.history_key,
                                    _commit=False)
        with pytest.raises(WorkflowDenied, match="enable_success_required"):
            require(context, db)
        commit.assert_not_called()
        db.rollback()
        assert require(context, db) == context.run_id


@pytest.mark.parametrize("change", [
    "owner", "session", "kind", "state", "action_site", "action_history", "action_revision",
    "trigger", "status", "unfinished_run", "cancel", "job_status", "unfinished_job",
    "job_site", "extra_job", "missing_guard", "guard_revision", "guard_hash", "lane",
    "notes", "checkin_after", "enabled", "template", "feature_policy",
])
def test_enable_requires_exact_full_run_scope_and_current_strict_guard(context, change):
    with context.factory() as db:
        site = db.get(Site, context.site_id)
        action = db.scalar(select(AssistantWorkflowAction))
        run = db.get(Run, context.run_id)
        job = db.scalar(select(SiteJob))
        guard = db.get(AssistantRunGuard, context.run_id)
        if change in {"owner", "session", "kind", "state", "action_site", "action_history", "action_revision"}:
            field, value = {
                "owner": ("owner", "foreign"), "session": ("session_id", str(uuid4())),
                "kind": ("kind", "import"), "state": ("state", "reserved"),
                "action_site": ("site_id", site.id + 1), "action_history": ("site_history_key", "other"),
                "action_revision": ("site_revision", 2),
            }[change]
            if change == "session":
                conversation = AssistantSession(id=value, owner=context.owner)
                seal(context, conversation, {})
                db.add(conversation)
                db.flush()
            setattr(action, field, value)
        elif change == "trigger":
            run.trigger = "manual"
        elif change == "status":
            run.status = "skipped"
        elif change == "unfinished_run":
            run.finished_at = None
        elif change == "cancel":
            run.cancel_requested = True
        elif change == "job_status":
            job.status = "skipped"
        elif change == "unfinished_job":
            job.finished_at = None
        elif change == "job_site":
            job.site_id = None
        elif change == "extra_job":
            db.add(SiteJob(run_id=run.id, site_id=site.id, site_name=site.name,
                           lane=job.lane, status="success", finished_at=run.finished_at))
        elif change == "missing_guard":
            db.delete(guard)
        elif change == "guard_revision":
            guard.site_revision += 1
        elif change == "guard_hash":
            guard.fingerprint = "0" * 64
        elif change == "lane":
            job.lane = "interaction"
        elif change in {"notes", "checkin_after", "enabled"}:
            setattr(site, change, {"notes": "changed", "checkin_after": "12:00", "enabled": True}[change])
        elif change == "template":
            db.get(Template, context.template_id).version += 1
        else:
            db.add(AppSetting(key="feature.flow.open", value_json=json.dumps({
                "defaults": {"wait_until": "networkidle"}, "required_fields": [],
            })))
        db.commit()
        with pytest.raises(WorkflowDenied, match="enable_success_required"):
            require(context, db)


@pytest.mark.parametrize("field,value", [("status", "queued"), ("status", "running"),
                                         ("status", "unknown"), ("result_json", '{"outcome_uncertain":true}')])
def test_active_and_unresolved_guarded_runs_block_even_from_other_conversation(context, field, value):
    with context.factory() as db:
        site = db.get(Site, context.site_id)
        run = Run(created_by="other-human", trigger="assistant", status="success",
                  finished_at=datetime.now(timezone.utc))
        db.add(run)
        db.flush()
        job = SiteJob(run_id=run.id, site_id=site.id, site_name=site.name, lane="general", status="success")
        setattr(job, field, value)
        db.add_all([job, AssistantRunGuard(run_id=run.id, site_id=site.id,
                                         site_history_key=site.history_key, site_revision=1, fingerprint="old")])
        db.commit()
        with pytest.raises(WorkflowDenied):
            require(context, db)


def test_unknown_action_blocks_even_when_success_exists(context):
    with context.factory() as db:
        site = db.get(Site, context.site_id)
        db.add(AssistantWorkflowAction(
            owner="other-owner", session_id=context.session_id, kind="run", state="unknown",
            request_fingerprint="unknown", site_id=site.id, site_history_key=site.history_key,
            site_revision=1, ciphertext=b"", nonce=b"", key_version=1,
        ))
        db.commit()
        with pytest.raises(WorkflowDenied, match="workflow_outcome_uncertain"):
            require(context, db)


@pytest.mark.parametrize("source", ["record", "message", "safety", "corrupt"])
def test_current_conversation_uncertain_operations_fail_closed(context, source):
    with context.factory() as db:
        conversation = db.get(AssistantSession, context.session_id)
        data = {}
        if source == "record":
            data = {"_execution_records": {"operation": {"state": "dispatched"}}}
        elif source == "safety":
            data = {"_context_safety": [{"operation_id": "operation", "status": "unknown"}]}
        elif source == "message":
            message = AssistantMessage(uuid=str(uuid4()), session_id=context.session_id, role="tool")
            seal(context, message, {"tool_name": "execute_step", "content": json.dumps({
                "operation_id": "operation", "code": "outcome_uncertain",
            })})
            db.add(message)
        seal(context, conversation, data)
        if source == "corrupt":
            conversation.ciphertext = b"broken"
        db.commit()
        with pytest.raises(WorkflowDenied):
            require(context, db)


@pytest.mark.parametrize("run_id", [True, "1", 0, -1, 99999])
def test_explicit_run_selection_never_falls_back(context, run_id):
    with context.factory() as db:
        with pytest.raises(WorkflowDenied, match="enable_success_required"):
            require(context, db, run_id=run_id)


def test_single_step_success_without_full_run_action_is_not_proof(context):
    with context.factory() as db:
        db.delete(db.scalar(select(AssistantWorkflowAction)))
        seal(context, db.get(AssistantSession, context.session_id), {
            "_execution_records": {"one-step": {"state": "terminal", "status": "success"}},
        })
        db.commit()
        with pytest.raises(WorkflowDenied, match="enable_success_required"):
            require(context, db)


@pytest.mark.parametrize("lane,attempt,allowed", [("interaction", 2, True), ("interaction", 1, False),
                                                ("proxy_interaction", 2, False)])
def test_enable_reuses_existing_strict_fallback_lane_rules(context, lane, attempt, allowed):
    with context.factory() as db:
        job = db.scalar(select(SiteJob))
        job.lane, job.attempt = lane, attempt
        db.commit()
        if allowed:
            assert require(context, db) == context.run_id
        else:
            with pytest.raises(WorkflowDenied, match="enable_success_required"):
                require(context, db)
