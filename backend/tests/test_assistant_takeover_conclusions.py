from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.main import create_app
from app.models.assistant import AssistantMessage, AssistantSession
from app.models.assistant_takeover import AssistantTakeoverAction, DailyConversation
from app.models.assistant_takeover_conclusion import (
    AssistantTakeoverConclusion, AssistantTakeoverConclusionCandidate,
)
from app.models.execution import Run, SiteJob
from app.models.integration import IntegrationConfig
from app.models.workflow import Site
from app.schemas.assistant import CreateSession
from app.schemas.assistant_takeover import TakeoverSettingsUpdate, TakeoverValues
from app.schemas.workflow import SiteWrite
from app.services.assistant_takeover import capture_job_config
from app.services.assistant_takeover_conclusions import (
    finalize_takeover_conclusion, notification_conclusions, prior_takeover_context, public_conclusion,
    submit_conclusion, TOOL_NAME, tool_definitions,
)
from app.services.assistant_takeover_settings import TakeoverSettingsService
from app.services.assistant_tools import AssistantTools
from app.services.config_ai_generation import _json
from app.services.workflows import WorkflowService
from tests.helpers import make_settings


OWNER = "conclusion-owner"
GOOD = "The login selector no longer matches; review the page before retrying."


@pytest.fixture
def context(tmp_path):
    manager = create_app(make_settings(tmp_path, config_ai_enabled=True)).state.assistant_manager
    sid = manager.create(OWNER, CreateSession())["id"]
    with manager.session_factory() as db:
        service = WorkflowService(db, manager.settings, manager.crypto)
        site = service.create_site(SiteWrite(
            name="Example", base_url="https://example.com/",
            steps=[{"step_key": "open", "feature_type": "flow.open", "config": {}}]))
        run = Run(created_by=OWNER, status="fail", finished_at=datetime.now(timezone.utc))
        db.add(run)
        db.flush()
        job = SiteJob(run_id=run.id, site_id=site.id, site_name="Example", lane="general",
                      status="fail", message="Original failure", attempt=1)
        db.add(job)
        db.flush()
        source = db.get(Site, site.id)
        resolved = service.resolve_site(site.id)
        capture_job_config(db, manager.settings, manager.crypto, source, job, {
            "steps": [step.model_dump(mode="json") for step in resolved.steps],
            "signals": [signal.model_dump(mode="json") for signal in resolved.signals],
            "base_url": site.base_url, "lane_key": job.lane,
        })
        db.flush()
        from app.models.assistant_takeover import AssistantJobConfigSnapshot

        binding = db.get(AssistantJobConfigSnapshot, job.id)
        action = AssistantTakeoverAction(
            id=str(uuid4()), source_job_id=job.id, source_run_id=run.id, site_id=site.id,
            site_history_key=source.history_key, site_origin="https://example.com",
            owner=OWNER, grant_revision=1, source_fingerprint=binding.fingerprint,
            mode="analysis", status="completed_analysis", session_id=sid, request_id=str(uuid4()),
        )
        db.add(action)
        manager._message(db, sid, "user", "Analyze this failure", request_id=action.request_id)
        db.commit()
        return manager, action.id


def promote(context, text=GOOD, *, fields=None, outcome=None, request=None):
    manager, identifier = context
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, identifier)
        if text is not None:
            manager._message(db, action.session_id, "assistant", text, **(fields or {}))
        db.flush()
        result = finalize_takeover_conclusion(
            manager, db, action, request or action.request_id,
            {"ok": True} if outcome is None else outcome,
        )
        db.commit()
        return result


def update_receipt(context, **changes):
    manager, identifier = context
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, identifier)
        action.status, action.mode = "updated", "autofix"
        action.import_receipt_json = _json({"type": "workflow_import", "result": {
            "ok": True, "site_id": action.site_id, "operation": "updated",
            "history_key": action.site_history_key, "config_revision": 2, **changes,
        }})
        db.commit()


def candidate_tools(context, args=None, *, runtime_key="runtime-key-not-in-settings"):
    manager, identifier = context
    args = {"conclusion": GOOD} if args is None else args
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, identifier)
        policy = TakeoverSettingsService(db).view()
        TakeoverSettingsService(db).update(TakeoverSettingsUpdate(
            expected_revision=policy.revision, confirm=True,
            values=TakeoverValues(enabled=True, live_access=False, site_ids=[action.site_id])), OWNER)
        action.status = "dispatched"
        session = db.get(AssistantSession, action.session_id)
        session.status = "running"
        data = manager._decrypt(session)
        site = db.get(Site, action.site_id)
        target = {"workflow_site_id": site.id, "base_url": site.base_url,
                  "run_lane_policy": site.lane_policy, "_workflow_binding": {
                      "id": site.id, "history_key": site.history_key, "revision": site.config_revision}}
        data.update(target=target, _takeover={
            "action_id": action.id, "request_id": action.request_id,
            "grant_revision": action.grant_revision, "source_fingerprint": action.source_fingerprint,
            "site_id": action.site_id,
        })
        manager._encrypt(session, data)
        call_id = "conclusion-call"
        message = manager._message(db, action.session_id, "tool", "", status="running",
                                   tool_name=TOOL_NAME, arguments=args,
                                   _model_message={"role": "tool", "tool_call_id": call_id})
        db.flush()
        turn = {"id": str(uuid4()), "_takeover_action_id": action.id,
                "_takeover_request_id": action.request_id, "_request_id": action.request_id}
        manager._active_turns[action.session_id] = turn
        tools = AssistantTools(manager, OWNER, action.session_id, target,
                               config={**turn, "api_key": runtime_key}, turn=turn)
        tools.call_context = {"tool_message_id": message.id, "tool_call_id": call_id}
        db.commit()
        return tools, args


def test_encrypted_exact_binding_idempotence_and_no_execution_mutations(context):
    manager, identifier = context
    result = promote(context)
    assert result == "\u52a9\u624b\u5206\u6790\uff1a" + GOOD
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, identifier)
        saved = db.get(AssistantTakeoverConclusion, identifier)
        assert GOOD.encode() not in saved.ciphertext
        payload = json.loads(manager.crypto.decrypt(
            f"assistant-takeover-conclusion:{identifier}", saved.ciphertext, saved.nonce, saved.key_version))
        assert payload["model_text"] == GOOD
        assert payload["source_message_id"] > 0
        assert action.import_receipt_json is None
        assert public_conclusion(manager, db, action) == result
        assert finalize_takeover_conclusion(manager, db, action, action.request_id, {"ok": True}) == result
        projection = notification_conclusions(db, manager.crypto, action.source_run_id)
        assert projection == {action.source_job_id: {
            "message": result, "action_id": identifier, "status": "completed_analysis", "conclusion": result}}
        assert notification_conclusions(db, manager.crypto, action.source_run_id + 1) == {}
        assert db.get(Run, action.source_run_id).status == "fail"
        job = db.get(SiteJob, action.source_job_id)
        assert job.status == "fail" and job.message == "Original failure"
        assert len(result) <= 300 and len(projection[job.id]["message"]) <= 500


@pytest.mark.parametrize("text", [
    "", "x" * 241, "x" * 2001, "<think>private thought</think>short answer",
    "<b>HTML</b>", "reasoning_content: private thought", "[analysis]private",
    "Thinking: private", "safe\x00unsafe", "safe\nunsafe", "safe\u202eunsafe",
    "password: private", "\u5bc6\u7801\uff1aprivate", "Bearer abcdefghijk",
    "api_key=private", "https://example.com/path?token=private", "![image](secret.png)",
])
def test_invalid_private_secret_or_overlong_text_not_promoted(context, text):
    assert promote(context, text) is None
    with context[0].session_factory() as db:
        assert db.get(AssistantTakeoverConclusion, context[1]) is None


def test_known_config_key_rejected_even_in_unlabelled_prose(context):
    manager, _ = context
    secret = "configured-key-not-pattern-detectable"
    with manager.session_factory() as db:
        encrypted = manager.crypto.encrypt("vision.api_key", secret)
        db.add(IntegrationConfig(key="vision.api_key", value_ciphertext=encrypted.ciphertext,
                                 nonce=encrypted.nonce, key_version=encrypted.key_version,
                                 fingerprint=manager.crypto.fingerprint(secret), updated_by=OWNER))
        db.commit()
    assert promote(context, "The page showed " + secret) is None


@pytest.mark.parametrize("outcome", [
    {}, {"ok": 1}, {"ok": "true"}, {"ok": False}, {"ok": False, "code": "provider_error"},
    {"ok": True, "code": "unknown"}, {"ok": True, "code": "raw provider text"},
    {"ok": True, "outcome_uncertain": True}, {"ok": True, "status": "interrupted"},
    {"ok": True, "status": []}, {"ok": True, "code": []},
    {"ok": True, "outcome_uncertain": 1},
])
def test_requires_typed_successful_known_turn_outcome(context, outcome):
    assert promote(context, outcome=outcome) is None


@pytest.mark.parametrize("status", ["queued", "dispatched", "revoked", "failed", "unknown"])
def test_noncomplete_status_cannot_promote_or_project(context, status):
    manager, identifier = context
    result = promote(context)
    assert result
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, identifier)
        action.status = status
        assert finalize_takeover_conclusion(manager, db, action, action.request_id, {"ok": True}) is None
        assert public_conclusion(manager, db, action) is None
        assert notification_conclusions(db, manager.crypto, action.source_run_id) == {}
        assert db.get(Run, action.source_run_id).status == "fail"


def test_wrong_request_and_nonuser_request_never_select_other_final(context):
    assert promote(context, request=str(uuid4())) is None
    manager, identifier = context
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, identifier)
        user = db.scalar(select(AssistantMessage).where(AssistantMessage.request_id == action.request_id))
        user.role = "system"
        db.commit()
    assert promote(context) is None


def test_next_user_bounds_turn_and_system_image_is_not_error(context):
    manager, identifier = context
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, identifier)
        final = manager._message(db, action.session_id, "assistant", GOOD)
        manager._message(db, action.session_id, "system", "Image diagnostic notice", _image={"private": "pixels"})
        manager._message(db, action.session_id, "user", "Another turn", request_id=str(uuid4()))
        manager._message(db, action.session_id, "assistant", "This is not the requested conclusion")
        db.flush()
        assert finalize_takeover_conclusion(manager, db, action, action.request_id, {"ok": True}).endswith(GOOD)
        assert db.get(AssistantTakeoverConclusion, identifier).source_message_id == final.id


def test_missing_final_before_next_user_cannot_borrow_next_turn(context):
    manager, identifier = context
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, identifier)
        manager._message(db, action.session_id, "user", "Next turn", request_id=str(uuid4()))
        db.commit()
    assert promote(context) is None


@pytest.mark.parametrize("fields", [
    {"_model_message": {"tool_calls": [{"id": "call"}]}}, {"tool_calls": [{"id": "call"}]},
    {"_image": {"private": "pixels"}}, {"_model_message": []},
])
def test_tool_calls_images_or_malformed_model_message_not_final(context, fields):
    assert promote(context, fields=fields) is None


def test_never_reads_reasoning_field_but_accepts_short_final(context):
    assert promote(context, fields={"reasoning_content": "private chain", "_model_message": {
        "content": GOOD, "reasoning_content": "more private chain"}}).endswith(GOOD)
    with context[0].session_factory() as db:
        saved = db.get(AssistantTakeoverConclusion, context[1])
        raw = context[0].crypto.decrypt(f"assistant-takeover-conclusion:{context[1]}",
                                      saved.ciphertext, saved.nonce, saved.key_version)
        assert "private chain" not in raw and "reasoning_content" not in raw


def test_no_fallback_to_earlier_safe_prose_and_bound_200_messages(context):
    manager, identifier = context
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, identifier)
        manager._message(db, action.session_id, "assistant", GOOD)
        db.commit()
    assert promote(context, "x" * 241) is None
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, identifier)
        for _ in range(200):
            manager._message(db, action.session_id, "tool", "result")
        db.commit()
    assert promote(context) is None


@pytest.mark.parametrize("outcome", [{"ok": True}, {"ok": False, "code": "provider_error"}])
def test_update_failure_framing_receipt_only_fallback(context, outcome):
    update_receipt(context)
    result = promote(context, "<think>private</think>" if outcome["ok"] else GOOD, outcome=outcome)
    assert result == "\u914d\u7f6e\u5df2\u66f4\u65b0\uff0c\u672a\u91cd\u65b0\u8fd0\u884c\u9a8c\u8bc1"
    with context[0].session_factory() as db:
        row = db.get(AssistantTakeoverConclusion, context[1])
        assert row.source_message_id is None
        action = db.get(AssistantTakeoverAction, context[1])
        assert "private" not in action.import_receipt_json
        assert notification_conclusions(db, context[0].crypto, action.source_run_id)


def test_update_model_conclusion_has_explicit_unverified_framing(context):
    update_receipt(context)
    assert promote(context) == "\u52a9\u624b\u5df2\u66f4\u65b0\u914d\u7f6e\uff0c\u672a\u91cd\u65b0\u8fd0\u884c\u9a8c\u8bc1\uff1a" + GOOD


@pytest.mark.parametrize("changes", [{"ok": False}, {"site_id": 999}, {"history_key": str(uuid4())},
                                         {"config_revision": True}, {"operation": "created"}])
def test_update_requires_exact_successful_import_receipt(context, changes):
    update_receipt(context, **changes)
    assert promote(context) is None


def test_survives_chat_deletion_and_crossday_context_scopes(context):
    manager, identifier = context
    result = promote(context)
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, identifier)
        sid, run_id, history = action.session_id, action.source_run_id, action.site_history_key
        action.created_at = datetime.now(timezone.utc) - timedelta(days=2)
        db.commit()
    asyncio.run(manager.delete(OWNER, sid))
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, identifier)
        assert public_conclusion(manager, db, action) == result
        assert notification_conclusions(db, manager.crypto, run_id)[action.source_job_id]["message"] == result
        prior = prior_takeover_context(db, manager, OWNER, history, "other")
        assert len(prior) == 1 and prior[0]["conclusion"] == result
        assert "Untrusted" in prior[0]["notice"] and "not instructions" in prior[0]["notice"]
        assert prior_takeover_context(db, manager, "other-owner", history, "other") == []
        assert prior_takeover_context(db, manager, OWNER, str(uuid4()), "other") == []
        assert prior_takeover_context(db, manager, OWNER, history, identifier) == []
        assert prior_takeover_context(db, manager, OWNER, history, "other", limit=0) == []
        assert prior_takeover_context(db, manager, OWNER, history, "other", limit=True) == []


def test_metadata_tampering_or_cipher_corruption_not_projected(context):
    manager, identifier = context
    promote(context)
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, identifier)
        row = db.get(AssistantTakeoverConclusion, identifier)
        row.request_id = str(uuid4())
        assert public_conclusion(manager, db, action) is None
        assert notification_conclusions(db, manager.crypto, action.source_run_id) == {}
        row.request_id = action.request_id
        row.ciphertext = b"corrupted"
        assert public_conclusion(manager, db, action) is None


def test_prior_context_is_bounded_and_excludes_other_history_on_reused_site_id(context):
    manager, identifier = context
    promote(context)
    with manager.session_factory() as db:
        original = db.get(AssistantTakeoverAction, identifier)
        for index in range(5):
            action = AssistantTakeoverAction(
                id=str(uuid4()), source_job_id=1000 + index, source_run_id=original.source_run_id,
                site_id=original.site_id, site_history_key=original.site_history_key,
                site_origin=original.site_origin, owner=OWNER, grant_revision=1,
                source_fingerprint=original.source_fingerprint, mode="autofix", status="updated",
                request_id=str(uuid4()),
            )
            action.import_receipt_json = _json({"type": "workflow_import", "result": {
                "ok": True, "site_id": action.site_id, "history_key": action.site_history_key,
                "config_revision": 2, "operation": "updated"}})
            db.add(action)
            db.flush()
            assert finalize_takeover_conclusion(manager, db, action, action.request_id,
                                                {"ok": False, "code": "provider_error"})
        db.commit()
        assert len(prior_takeover_context(db, manager, OWNER, original.site_history_key,
                                         "other", limit=99)) == 3
        assert len(prior_takeover_context(db, manager, OWNER, original.site_history_key,
                                         "other", limit=1)) == 1
        # An integer site ID can now refer to a different workflow identity.
        site = db.get(Site, original.site_id)
        site.history_key = str(uuid4())
        db.flush()
        assert prior_takeover_context(db, manager, OWNER, site.history_key, "other") == []


def test_prior_context_contains_structured_diff_without_local_values(context):
    manager, identifier = context
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, identifier)
        service = WorkflowService(db, manager.settings, manager.crypto)
        site = db.get(Site, action.site_id)
        before = service._local_snapshot(site)
        after = deepcopy(before)
        after["steps"][0]["config"] = {"url": "https://example.com/private-changed-value"}
        after["groups"] = [{"group_key": "new_group", "name": "Private group name"}]
        after["signals"] = [{"signal_key": "success", "label": "Private signal label",
                             "include_texts": ["private signal contents"]}]
        saved = service.update_site(site.id, SiteWrite(
            **after, expected_revision=site.config_revision, expected_history_key=site.history_key))
        action.status, action.mode = "updated", "autofix"
        action.import_receipt_json = _json({"type": "workflow_import", "result": {
            "ok": True, "site_id": site.id, "history_key": site.history_key,
            "config_revision": saved.config_revision, "operation": "updated"}})
        db.commit()
    promote(context)
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, identifier)
        prior = prior_takeover_context(db, manager, OWNER, action.site_history_key, "other")
        assert len(prior) == 1
        item = prior[0]
        assert item["before_revision"] == 1 and item["after_revision"] == 2
        changes = {change["key"]: change for change in item["changed_steps"]}
        assert changes["open"]["changed_fields"] == ["config"]
        assert changes["open"]["versions"] == {"before": [1], "after": [1]}
        assert changes["new_group"]["kind"] == "groups"
        assert changes["success"]["kind"] == "signals"
        assert "private-changed-value" not in json.dumps(prior)
        assert "Private group name" not in json.dumps(prior)
        assert "private signal contents" not in json.dumps(prior)


def test_explicit_candidate_promotes_despite_long_multiline_final_and_removed_metadata(context):
    manager, identifier = context
    tools, args = candidate_tools(context)
    assert asyncio.run(submit_conclusion(tools, args)) == {"ok": True, "accepted": True}
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, identifier)
        candidate = db.get(AssistantTakeoverConclusionCandidate, (identifier, action.request_id))
        assert GOOD.encode() not in candidate.ciphertext
        assert db.get(AssistantTakeoverConclusion, identifier) is None
        assert public_conclusion(manager, db, action) is None
        assert notification_conclusions(db, manager.crypto, action.source_run_id) == {}
        assert action.import_receipt_json is None
        session = db.get(AssistantSession, action.session_id)
        data = manager._decrypt(session)
        data.pop("_takeover")
        manager._encrypt(session, data)
        action.status = "completed_analysis"
        db.commit()
    result = promote(context, "Detailed explanation.\n" * 100)
    assert result == "\u52a9\u624b\u5206\u6790\uff1a" + GOOD
    with manager.session_factory() as db:
        saved = db.get(AssistantTakeoverConclusion, identifier)
        assert saved.source_message_id == tools.call_context["tool_message_id"]
        assert db.get(Run, action.source_run_id).status == "fail"
        assert db.get(SiteJob, action.source_job_id).message == "Original failure"


def test_candidate_replay_is_idempotent_and_cannot_replace_accepted_text(context):
    manager, identifier = context
    tools, args = candidate_tools(context)
    assert asyncio.run(submit_conclusion(tools, args))["ok"]
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, identifier)
        first = db.get(AssistantTakeoverConclusionCandidate, (identifier, action.request_id)).ciphertext
    assert asyncio.run(submit_conclusion(tools, args)) == {"ok": True, "accepted": True}
    replacement = {"conclusion": "A different conclusion"}
    with manager.session_factory() as db:
        message = db.get(AssistantMessage, tools.call_context["tool_message_id"])
        data = manager._decrypt(message)
        data["arguments"] = replacement
        manager._encrypt(message, data)
        db.commit()
    assert asyncio.run(submit_conclusion(tools, replacement)) == {
        "ok": False, "code": "conclusion_already_submitted"}
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, identifier)
        assert db.get(AssistantTakeoverConclusionCandidate, (identifier, action.request_id)).ciphertext == first


@pytest.mark.parametrize("args", [
    {}, {"conclusion": 1}, {"conclusion": "x" * 241}, {"conclusion": GOOD, "action_id": "forged"},
    {"conclusion": "<think>private</think>"}, {"conclusion": "password: private"},
    {"conclusion": "safe\nunsafe"}, {"conclusion": "The key is runtime-key-not-in-settings"},
    {"conclusion": "reasoning_content: private"},
])
def test_candidate_validation_never_echoes_or_saves_rejected_text(context, args):
    tools, _ = candidate_tools(context, args)
    result = asyncio.run(submit_conclusion(tools, args))
    assert result["ok"] is False and set(result) == {"ok", "code"}
    with tools.manager.session_factory() as db:
        assert db.scalar(select(AssistantTakeoverConclusionCandidate)) is None


@pytest.mark.parametrize("tamper", ["ordinary", "wrong_owner", "wrong_request", "wrong_tool_request",
                                   "missing_call", "wrong_call", "next_user", "revoked"])
def test_candidate_requires_exact_live_takeover_and_tool_record(context, tamper):
    manager, identifier = context
    tools, args = candidate_tools(context)
    if tamper == "ordinary":
        manager._active_turns[tools.session_id] = {}
    elif tamper == "wrong_owner":
        tools.owner = "someone-else"
    elif tamper == "wrong_request":
        manager._active_turns[tools.session_id] = {**tools.turn, "_request_id": str(uuid4())}
    elif tamper == "wrong_tool_request":
        tools.config["_takeover_request_id"] = str(uuid4())
    elif tamper == "missing_call":
        tools.call_context = {}
    elif tamper == "wrong_call":
        tools.call_context["tool_call_id"] = "different-call"
    else:
        with manager.session_factory() as db:
            if tamper == "next_user":
                # Move the tool record after the subsequent user's request.
                message = db.get(AssistantMessage, tools.call_context["tool_message_id"])
                data = manager._decrypt(message)
                manager._message(db, tools.session_id, "user", "New user", request_id=str(uuid4()))
                message = manager._message(db, tools.session_id, "tool", "", **{
                    key: value for key, value in data.items() if key != "content"})
                db.flush()
                tools.call_context["tool_message_id"] = message.id
            else:
                action = db.get(AssistantTakeoverAction, identifier)
                action.status = "revoked"
            db.commit()
    assert asyncio.run(submit_conclusion(tools, args))["ok"] is False
    with manager.session_factory() as db:
        assert db.scalar(select(AssistantTakeoverConclusionCandidate)) is None


@pytest.mark.parametrize("outcome", [{"ok": False, "code": "provider_error"},
                                    {"ok": True, "code": "unknown"}, {"ok": "true"}])
def test_candidate_does_not_bypass_turn_classification(context, outcome):
    tools, args = candidate_tools(context)
    assert asyncio.run(submit_conclusion(tools, args))["ok"]
    with tools.manager.session_factory() as db:
        db.get(AssistantTakeoverAction, context[1]).status = "completed_analysis"
        db.commit()
    assert promote(context, "Detailed explanation\n" * 50, outcome=outcome) is None


def test_candidate_from_other_request_is_not_promoted(context):
    manager, identifier = context
    tools, args = candidate_tools(context)
    assert asyncio.run(submit_conclusion(tools, args))["ok"]
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, identifier)
        action.request_id = str(uuid4())
        action.status = "completed_analysis"
        manager._message(db, action.session_id, "user", "Another request", request_id=action.request_id)
        db.commit()
    assert promote(context, "Detailed explanation\n" * 50) is None


def test_candidate_registration_has_strict_short_conclusion_schema():
    from app.services.assistant_prompt import tool_definitions as all_definitions

    definition = tool_definitions()[0]["function"]
    assert definition["name"] == TOOL_NAME
    assert definition["parameters"]["properties"]["conclusion"]["maxLength"] == 240
    assert definition["parameters"]["additionalProperties"] is False
    assert any(item["function"]["name"] == TOOL_NAME for item in all_definitions())


def test_promoted_candidate_survives_chat_delete_with_notification_and_prior_rationale(context):
    manager, identifier = context
    tools, args = candidate_tools(context)
    assert asyncio.run(submit_conclusion(tools, args))["ok"]
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, identifier)
        action.status = "completed_analysis"
        session = db.get(AssistantSession, action.session_id)
        session.status = "idle"
        db.commit()
    result = promote(context, "Verbose analysis\n" * 100)
    asyncio.run(manager.delete(OWNER, tools.session_id))
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, identifier)
        assert public_conclusion(manager, db, action) == result
        assert notification_conclusions(db, manager.crypto, action.source_run_id)[
            action.source_job_id]["message"] == result
        assert prior_takeover_context(db, manager, OWNER, action.site_history_key, "other")[
            0]["conclusion"] == result


def test_explicit_candidate_update_still_requires_import_receipt_and_unverified_framing(context):
    with context[0].session_factory() as db:
        db.get(AssistantTakeoverAction, context[1]).mode = "autofix"
        db.commit()
    tools, args = candidate_tools(context)
    assert asyncio.run(submit_conclusion(tools, args))["ok"]
    update_receipt(context)
    result = promote(context, "Long final explanation\n" * 100)
    assert result == "\u52a9\u624b\u5df2\u66f4\u65b0\u914d\u7f6e\uff0c\u672a\u91cd\u65b0\u8fd0\u884c\u9a8c\u8bc1\uff1a" + GOOD


def test_dispatch_routes_conclusion_only_for_authorized_takeover(context):
    from app.services.assistant_prompt import tool_definitions as registry
    from app.services.assistant_takeover import takeover_tool_allowed, takeover_tool_definitions

    manager, identifier = context
    with manager.session_factory() as db:
        action = db.get(AssistantTakeoverAction, identifier)
        sid = action.session_id
    ordinary = AssistantTools(manager, OWNER, sid, None)
    assert takeover_tool_allowed(ordinary, TOOL_NAME, {"conclusion": GOOD}) == {
        "ok": False, "code": "takeover_tool_denied"}
    assert asyncio.run(ordinary.dispatch(TOOL_NAME, {"conclusion": GOOD}))["ok"] is False
    ordinary_definitions = takeover_tool_definitions(manager, OWNER, sid, registry())
    assert {item["function"]["name"] for item in ordinary_definitions} == {
        item["function"]["name"] for item in registry()
    } - {TOOL_NAME}
    assert TOOL_NAME not in {item["function"]["name"] for item in ordinary_definitions}
    tools, args = candidate_tools(context)
    allowed = takeover_tool_definitions(manager, OWNER, sid, registry())
    assert TOOL_NAME in {item["function"]["name"] for item in allowed}
    assert asyncio.run(tools.dispatch(TOOL_NAME, args)) == {"ok": True, "accepted": True}


@pytest.mark.parametrize("same_day_count", [4, 35])
@pytest.mark.parametrize("deleted_chat", [False, True])
def test_prior_context_filters_same_local_day_before_limit_using_audited_zone(
    context, same_day_count, deleted_chat,
):
    manager, identifier = context
    promote(context)
    with manager.session_factory() as db:
        original = db.get(AssistantTakeoverAction, identifier)
        # UTC date is unchanged across Tokyo midnight. The site's zone has since changed.
        prior_finish = datetime(2026, 11, 10, 14, 59, tzinfo=timezone.utc)
        current_finish = datetime(2026, 11, 10, 15, 30, tzinfo=timezone.utc)
        db.get(SiteJob, original.source_job_id).finished_at = prior_finish
        db.get(Site, original.site_id).timezone = "UTC"
        old_sid = original.session_id
        current_sid = manager.create(OWNER, CreateSession())["id"] if deleted_chat else old_sid
        current = AssistantTakeoverAction(
            id=str(uuid4()), source_job_id=original.source_job_id + 1000,
            source_run_id=original.source_run_id, site_id=original.site_id,
            site_history_key=original.site_history_key, site_origin=original.site_origin,
            owner=OWNER, grant_revision=1, source_fingerprint=original.source_fingerprint,
            mode="analysis", status="queued", session_id=current_sid,
        )
        db.add(current)
        same_day_ids = []
        for index in range(same_day_count):
            job = SiteJob(run_id=original.source_run_id, site_id=original.site_id,
                          site_name="Example", lane="general", status="fail",
                          finished_at=current_finish)
            db.add(job)
            db.flush()
            action = AssistantTakeoverAction(
                id=str(uuid4()), source_job_id=job.id, source_run_id=original.source_run_id,
                site_id=original.site_id, site_history_key=original.site_history_key,
                site_origin=original.site_origin, owner=OWNER, grant_revision=1,
                source_fingerprint=original.source_fingerprint, mode="autofix", status="updated",
                session_id=old_sid, request_id=str(uuid4()),
            )
            action.import_receipt_json = _json({"type": "workflow_import", "result": {
                "ok": True, "site_id": action.site_id, "history_key": action.site_history_key,
                "config_revision": 2, "operation": "updated"}})
            db.add(action)
            db.flush()
            assert finalize_takeover_conclusion(manager, db, action, action.request_id,
                                                {"ok": False, "code": "provider_error"})
            same_day_ids.append(action.id)
        db.add(DailyConversation(
            id=str(uuid4()), owner=OWNER, site_history_key=original.site_history_key,
            local_date=date(2026, 11, 11), timezone="Asia/Tokyo",
            anchor_finished_at=current_finish, anchor_action_id=same_day_ids[0], session_id=current_sid,
        ))
        db.commit()
        history_key, current_id = original.site_history_key, current.id
    if deleted_chat:
        asyncio.run(manager.delete(OWNER, old_sid))
    with manager.session_factory() as db:
        prior = prior_takeover_context(db, manager, OWNER, history_key, current_id)
        assert [item["action_id"] for item in prior] == [identifier]
        assert prior[0]["conclusion"].endswith(GOOD)
