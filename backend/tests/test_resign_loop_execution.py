from __future__ import annotations

import asyncio
import json
from datetime import datetime, time, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

from app.models.execution import JobStep, Run, SiteJob
from app.models.workflow import Site, SiteResignRecord, WorkflowStep
from app.schemas.execution import RunCreate
from app.services.execution import ExecutionService
from app.services.executor import ExecutionManager
from app.services.resign import ResignConflict, ResignService
from app.services.secret_providers import SecretDenied


@pytest.fixture(params=["post.resign_loop", "post.resign_dialog"])
def checkpoint_feature(request):
    return request.param


@pytest.fixture
def execution(client, monkeypatch, fake_dispatch):
    response = client.post("/api/v1/sites", json={
        "name": "resign-loop-execution",
        "base_url": "https://pt.example.com",
        "lane_policy": "general",
        "steps": [{"step_key": "open", "feature_type": "flow.open"}],
    })
    assert response.status_code == 201, response.text
    site_id = response.json()["id"]
    app = client.app
    manager = ExecutionManager(app.state.session_factory, app.state.settings, app.state.crypto)
    monkeypatch.setattr(manager, "schedule_run_notification", lambda _run_id: None)
    with app.state.session_factory() as session:
        run = ExecutionService(session, app.state.settings, app.state.crypto).create_run(
            RunCreate(site_ids=[site_id]), "admin",
        )
        job = session.query(SiteJob).filter_by(run_id=run.id).one()
        return manager, site_id, run.id, job.id


def _day(manager, days_ago=1):
    with manager.session_factory() as session:
        service = ResignService(session, manager.settings)
        return (datetime.now(service.timezone).date() - timedelta(days=days_ago)).isoformat()


def _result(day, **overrides):
    return {
        "pending": [day], "success": [], "already_signed": [], "failed": [],
        "remaining": [day], "cards_before": 2, "cards_after": 2,
        "submit_attempted": False, "unconfirmed": [], **overrides,
    }


def _step(result, *, feature="post.resign_loop", status="running"):
    return {
        "step_key": "resign", "feature_type": feature, "status": status,
        "message": "checkpoint", "data": {"resign": result},
    }


def _records(manager, site_id):
    with manager.session_factory() as session:
        return {
            row.resign_date: row.status
            for row in session.query(SiteResignRecord).filter_by(site_id=site_id).all()
        }


def test_progress_unknown_is_committed_and_final_promotes_it(execution, monkeypatch, checkpoint_feature):
    manager, site_id, run_id, job_id = execution
    day = _day(manager)

    async def execute(_payload):
        manager._persist_progress(job_id, {"status": "running", "steps": [_step(_result(
            day, submit_attempted=True, unconfirmed=[{"date": day, "message": "before click"}],
        ), feature=checkpoint_feature)]})
        # A separate session must see the committed checkpoint, not just a pending flush.
        assert _records(manager, site_id) == {day: "unknown"}
        return {"status": "success", "steps": [_step(_result(
            day, success=[{"date": day, "message": "observed done"}], remaining=[],
        ), feature=checkpoint_feature, status="success")]}

    monkeypatch.setattr(manager, "_post_execute", execute)
    asyncio.run(manager._execute_run(run_id))
    assert _records(manager, site_id) == {day: "success"}
    with manager.session_factory() as session:
        saved = session.query(JobStep).filter_by(site_job_id=job_id).one()
        result = json.loads(saved.data_json)["resign"]
        assert result["submit_attempted"] is True
        assert result["unconfirmed"] == result["remaining"] == []


@pytest.mark.parametrize("terminal", ["success", "already_signed"])
def test_record_results_does_not_downgrade_confirmed_dates(execution, terminal, checkpoint_feature):
    manager, site_id, run_id, job_id = execution
    day = _day(manager)
    with manager.session_factory() as session:
        service = ResignService(session, manager.settings)
        service.record_results(site_id, run_id, job_id, _result(
            day, **{terminal: [{"date": day, "message": "confirmed"}]},
        ), feature_type=checkpoint_feature)
        session.commit()
        service.record_results(site_id, run_id, job_id, _result(
            day, unconfirmed=[{"date": day}], failed=[{"date": day}],
        ), feature_type=checkpoint_feature)
        session.commit()
    assert _records(manager, site_id) == {day: terminal}


def test_success_wins_unconfirmed_in_same_payload(execution, checkpoint_feature):
    manager, site_id, run_id, job_id = execution
    day = _day(manager)
    with manager.session_factory() as session:
        ResignService(session, manager.settings).record_results(site_id, run_id, job_id, _result(
            day, unconfirmed=[{"date": day}, {"date": day}],
            success=[{"date": day, "message": "done"}], failed=[{"date": day}],
        ), feature_type=checkpoint_feature)
        session.commit()
    assert _records(manager, site_id) == {day: "success"}


@pytest.mark.parametrize("bucket", ["success", "already_signed", "failed", "unconfirmed"])
def test_new_results_reject_invalid_and_out_of_scope_dates(execution, bucket, checkpoint_feature):
    manager, site_id, run_id, job_id = execution
    day, today, future, outside = (_day(manager, n) for n in [1, 0, -1, 2])
    noncanonical = day.replace("-", "")
    invalid = [today, future, "2026-02-30", noncanonical, "not-a-date", outside]
    result = _result(day, pending=[day, *invalid[:-1]])
    result[bucket] = [{"date": value} for value in [day, *invalid]]
    with manager.session_factory() as session:
        ResignService(session, manager.settings).record_results(
            site_id, run_id, job_id, result, feature_type=checkpoint_feature,
        )
        session.commit()
    assert _records(manager, site_id) == {day: "unknown" if bucket == "unconfirmed" else bucket}


@pytest.mark.parametrize("feature", ["post.resign", "post.resign_loop", "post.resign_dialog"])
@pytest.mark.parametrize("overall", ["waiting_human", "canceled", "cancel_requested"])
def test_final_confirmations_survive_overall_status(execution, monkeypatch, feature, overall):
    manager, site_id, run_id, job_id = execution
    day = _day(manager)

    async def execute(_payload):
        if overall == "cancel_requested":
            with manager.session_factory() as session:
                session.get(Run, run_id).cancel_requested = True
                session.commit()
        return {
            "status": "success" if overall == "cancel_requested" else overall,
            "steps": [_step(_result(day, success=[{"date": day, "message": "done"}]),
                            feature=feature, status="success")],
        }

    monkeypatch.setattr(manager, "_post_execute", execute)
    asyncio.run(manager._execute_run(run_id))
    assert _records(manager, site_id) == {day: "success"}
    with manager.session_factory() as session:
        assert session.get(SiteJob, job_id).status == (
            "canceled" if overall == "cancel_requested" else overall
        )


@pytest.mark.parametrize("retry", ["restart", "fallback_interaction"])
@pytest.mark.parametrize("evidence", ["attempted", "success"])
@pytest.mark.parametrize("final_steps", ["empty", "present", "cleared"])
def test_retry_guard_uses_persisted_effects_without_incrementing_attempt(
    execution, monkeypatch, retry, evidence, final_steps, checkpoint_feature,
):
    manager, site_id, run_id, job_id = execution
    day = _day(manager)
    calls = []
    result = _result(day, **(
        {"submit_attempted": True, "unconfirmed": [{"date": day}]}
        if evidence == "attempted" else {"success": [{"date": day}]}
    ))

    async def execute(payload):
        calls.append(payload["attempt"])
        manager._persist_progress(job_id, {"status": "running", "steps": [
            {"step_key": "open", "feature_type": "flow.open", "status": "running"},
            _step(result, feature=checkpoint_feature),
        ]})
        # Later incomplete progress must not erase the only persisted side-effect evidence.
        if final_steps == "cleared":
            manager._persist_progress(job_id, {"steps": []})
        rows = [] if final_steps == "empty" else [_step(
            result if final_steps == "present" else _result(day), feature=checkpoint_feature,
        )]
        return {"status": retry, "steps": rows}

    monkeypatch.setattr(manager, "_post_execute", execute)
    asyncio.run(manager._execute_run(run_id))
    assert calls == [1]
    with manager.session_factory() as session:
        job = session.get(SiteJob, job_id)
        assert job.attempt == 1 and job.lane == "general"
        assert job.status == "waiting_human" and "补签" in job.message
        rows = session.query(JobStep).filter_by(site_job_id=job_id).all()
        assert [row.feature_type for row in rows] == [checkpoint_feature]
        assert rows[0].status == "waiting_human"
        saved = json.loads(rows[0].data_json)["resign"]
        assert saved["submit_attempted"] or saved["success"]
    assert _records(manager, site_id) == {day: "unknown" if evidence == "attempted" else "success"}


@pytest.mark.parametrize("ending", ["error", "canceled", "api_cancel"])
def test_checkpoint_survives_worker_error_and_cancellation(execution, monkeypatch, ending, checkpoint_feature):
    manager, site_id, run_id, job_id = execution
    day = _day(manager)

    async def execute(_payload):
        manager._persist_progress(job_id, {"status": "running", "steps": [_step(_result(
            day, submit_attempted=True, unconfirmed=[{"date": day}],
        ), feature=checkpoint_feature)]})
        if ending == "error":
            raise RuntimeError("stream disconnected")
        if ending == "api_cancel":
            with manager.session_factory() as session:
                ExecutionService(session, manager.settings, manager.crypto).cancel_run(run_id)
        return {"status": "canceled", "steps": []}

    monkeypatch.setattr(manager, "_post_execute", execute)
    asyncio.run(manager._execute_run(run_id))
    assert _records(manager, site_id) == {day: "unknown"}
    with manager.session_factory() as session:
        assert session.get(SiteJob, job_id).status == ("error" if ending == "error" else "canceled")
        assert session.query(JobStep).filter_by(site_job_id=job_id).count() == 1


def test_payload_uses_resign_timezone_and_keeps_unknown_pending(execution, monkeypatch):
    manager, site_id, run_id, job_id = execution
    manager.settings.scheduler_timezone = "America/Los_Angeles"

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 2, 0, 30, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr("app.services.resign.datetime", Clock)
    monkeypatch.setattr("app.services.executor.datetime", Clock)
    today = "2026-11-01"
    unknown = "2026-10-30"
    with manager.session_factory() as session:
        service = ResignService(session, manager.settings)
        history = Run(mode="manual", trigger="manual", status="fail", created_by="admin")
        session.add(history)
        session.flush()
        session.add(SiteJob(
            run_id=history.id, site_id=site_id, site_name="history", lane="general",
            status="fail", finished_at=datetime.combine(
                datetime.fromisoformat(unknown).date(), time(12), tzinfo=service.timezone,
            ).astimezone(timezone.utc),
        ))
        session.add_all([
            SiteResignRecord(site_id=site_id, resign_date=unknown, status="unknown"),
            SiteResignRecord(site_id=site_id, resign_date="2026-10-31", status="failed"),
            SiteResignRecord(site_id=site_id, resign_date="2026-10-01", status="unknown"),
        ])
        session.commit()
        assert service.pending_dates(site_id, 30) == [unknown, "2026-10-31"]

    with manager.session_factory() as other:
        foreign_site = Site(name="other", base_url="https://other.example.com")
        other.add(foreign_site)
        other.flush()
        other.add(SiteResignRecord(site_id=foreign_site.id, resign_date="2026-10-29", status="unknown"))
        other.commit()

    async def execute(payload):
        assert payload["resign_today"] == today
        assert payload["resign_pending_dates"] == [unknown, "2026-10-31"]
        assert payload["resign_unresolved_dates"] == ["2026-10-01", unknown]
        assert "credentials" not in payload
        assert "test-password" not in json.dumps(payload)
        return {"status": "waiting_human", "steps": []}

    monkeypatch.setattr(manager, "_post_execute", execute)
    asyncio.run(manager._execute_run(run_id))
    with manager.session_factory() as session:
        assert session.get(SiteJob, job_id).status == "waiting_human"


def test_legacy_result_scope_is_unchanged(execution):
    manager, site_id, run_id, job_id = execution
    day = _day(manager)
    with manager.session_factory() as session:
        ResignService(session, manager.settings).record_results(
            site_id, run_id, job_id,
            {"pending": [], "success": [{"date": day, "message": "legacy"}]},
        )
        session.commit()
    assert _records(manager, site_id) == {day: "success"}


@pytest.mark.parametrize("retry", ["restart", "fallback_interaction"])
def test_resign_without_side_effects_can_retry(execution, monkeypatch, retry, checkpoint_feature):
    manager, site_id, run_id, job_id = execution
    day = _day(manager)
    calls = []

    async def execute(payload):
        calls.append(payload["attempt"])
        with manager.session_factory() as session:
            assert session.query(JobStep).filter_by(site_job_id=job_id).count() == 0
        if len(calls) == 1:
            rows = [_step(_result(day), feature=checkpoint_feature)]
            manager._persist_progress(job_id, {"status": "running", "steps": rows})
            return {"status": retry, "steps": rows}
        return {"status": "success", "steps": []}

    monkeypatch.setattr(manager, "_post_execute", execute)
    asyncio.run(manager._execute_run(run_id))
    assert calls == [1, 2]
    assert _records(manager, site_id) == {}
    with manager.session_factory() as session:
        assert session.get(SiteJob, job_id).status == "success"
        assert session.query(JobStep).filter_by(site_job_id=job_id).count() == 0


@pytest.mark.parametrize("confirmed", [False, True])
def test_ndjson_checkpoint_survives_empty_stream_final(execution, monkeypatch, confirmed, checkpoint_feature):
    manager, site_id, run_id, job_id = execution
    day = _day(manager)
    checkpoint = _step(_result(
        day, submit_attempted=True, unconfirmed=[{"date": day, "message": "click pending"}],
    ), feature=checkpoint_feature)

    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def raise_for_status(self):
            pass

        async def aiter_lines(self):
            yield json.dumps({"type": "progress", "status": "running", "steps": [checkpoint]})
            assert _records(manager, site_id) == {day: "unknown"}
            if confirmed:
                yield json.dumps({"type": "progress", "steps": [_step(_result(
                    day, success=[{"date": day, "message": "done"}], remaining=[],
                ), feature=checkpoint_feature, status="success")]})
                assert _records(manager, site_id) == {day: "success"}
            yield json.dumps({"type": "result", "result": {"status": "restart", "steps": []}})

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, method, url, *, json):
            assert method == "POST" and url.endswith("/execute/stream")
            assert json["job_id"] == job_id
            return Response()

    monkeypatch.setattr("app.services.executor.httpx.AsyncClient", Client)
    asyncio.run(manager._execute_run(run_id))
    with manager.session_factory() as session:
        job = session.get(SiteJob, job_id)
        assert job.status == "waiting_human" and job.attempt == 1
        saved = session.query(JobStep).filter_by(site_job_id=job_id).one()
        result = json.loads(saved.data_json)["resign"]
        assert result["unconfirmed"] == ([] if confirmed else checkpoint["data"]["resign"]["unconfirmed"])
        assert result["remaining"] == ([] if confirmed else [day])
        assert result["submit_attempted"] is True
        assert bool(result["success"]) is confirmed


@pytest.mark.parametrize("confirmation", ["success", "already_signed", "already_signed_string"])
@pytest.mark.parametrize("current", [True, False])
@pytest.mark.parametrize("remaining", [True, False])
def test_merge_removes_only_confirmed_dates_and_preserves_retry_evidence(confirmation, current, remaining, checkpoint_feature):
    first, second = "2026-10-01", "2026-10-02"
    bucket = "already_signed" if confirmation.startswith("already_signed") else "success"
    confirmed = [first] if confirmation.endswith("string") else [{"date": first}]
    previous = _result(
        first, pending=[first, second], submit_attempted=True,
        unconfirmed=[{"date": first}, {"date": second}], remaining=[first, second],
    )
    incoming = {bucket: confirmed, "submit_attempted": False}
    if remaining:
        incoming["remaining"] = [first, second]
    else:
        previous.pop("remaining")
    if not current:
        previous.update(incoming)
        previous["submit_attempted"] = True
    saved = SimpleNamespace(
        step_key="resign", feature_type=checkpoint_feature, label="", status="running",
        message="", data_json=json.dumps({"resign": previous}), started_at=None, finished_at=None,
    )
    session = SimpleNamespace(scalars=lambda *_: [saved])
    rows = ExecutionManager._merge_resign_checkpoints(
        session, SimpleNamespace(id=1), [_step(incoming, feature=checkpoint_feature)] if current else [],
    )
    result = rows[0]["data"]["resign"]
    assert result[bucket] == confirmed
    assert result["unconfirmed"] == [{"date": second}]
    assert result["submit_attempted"] is True
    if remaining:
        assert result["remaining"] == [second]
    else:
        assert "remaining" not in result


@pytest.mark.parametrize("same_key", [False, True])
@pytest.mark.parametrize("same_feature", [False, True])
def test_checkpoint_merge_requires_both_step_key_and_feature(
    execution, checkpoint_feature, same_key, same_feature,
):
    manager, _, _, job_id = execution
    previous = _result("2026-10-01", submit_attempted=True, unconfirmed=[{"date": "2026-10-01"}])
    incoming = _step(_result("2026-10-02"), feature=(
        checkpoint_feature if same_feature else
        "post.resign_dialog" if checkpoint_feature == "post.resign_loop" else "post.resign_loop"
    ))
    incoming["step_key"] = "resign" if same_key else "other"
    with manager.session_factory() as session:
        session.add(JobStep(
            site_job_id=job_id, step_key="resign", feature_type=checkpoint_feature,
            status="running", data_json=json.dumps({"resign": previous}),
        ))
        session.commit()
        rows = manager._merge_resign_checkpoints(session, session.get(SiteJob, job_id), [incoming])
    if same_key and same_feature:
        assert len(rows) == 1
        assert rows[0]["data"]["resign"]["submit_attempted"] is True
    else:
        assert len(rows) == 2
        assert rows[0] == incoming
        assert rows[1]["step_key"] == "resign"
        assert rows[1]["feature_type"] == checkpoint_feature
        assert rows[1]["data"]["resign"] == previous


def test_legacy_progress_does_not_acquire_strong_checkpoint_semantics(execution):
    manager, site_id, _, job_id = execution
    day = _day(manager)
    manager._persist_progress(job_id, {"steps": [_step(_result(
        day, submit_attempted=True, unconfirmed=[{"date": day}],
    ), feature="post.resign")]})
    assert _records(manager, site_id) == {}
    manager._persist_progress(job_id, {"steps": []})
    with manager.session_factory() as session:
        assert session.query(JobStep).filter_by(site_job_id=job_id).count() == 0


@pytest.mark.parametrize("retry", ["restart", "fallback_interaction"])
@pytest.mark.parametrize("evidence", ["attempted", "success"])
def test_retry_guard_also_checks_final_without_checkpoint(execution, monkeypatch, retry, evidence, checkpoint_feature):
    manager, site_id, run_id, job_id = execution
    day = _day(manager)
    calls = []

    async def execute(payload):
        calls.append(payload["attempt"])
        result = _result(day, **(
            {"submit_attempted": True} if evidence == "attempted" else {"success": [{"date": day}]}
        ))
        return {"status": retry, "steps": [_step(result, feature=checkpoint_feature, status="success")]}

    monkeypatch.setattr(manager, "_post_execute", execute)
    asyncio.run(manager._execute_run(run_id))
    assert calls == [1]
    with manager.session_factory() as session:
        job = session.get(SiteJob, job_id)
        assert job.status == "waiting_human" and job.attempt == 1
        assert "补签" in job.message
    assert _records(manager, site_id) == ({day: "success"} if evidence == "success" else {})


CHECKPOINT_PATH = "/api/v1/internal/resign/checkpoint"


@pytest.fixture
def checkpoint(client, execution, checkpoint_feature):
    manager, site_id, run_id, job_id = execution
    client.app.state.executor = manager
    day = _day(manager)
    with manager.session_factory() as session:
        history = Run(mode="manual", trigger="manual", status="fail", created_by="admin")
        session.add(history)
        session.flush()
        session.add(SiteJob(
            run_id=history.id, site_id=site_id, site_name="history", lane="general",
            status="fail", finished_at=datetime.now(timezone.utc) - timedelta(days=3),
        ))
        session.commit()
        job = session.get(SiteJob, job_id)
        token = manager.secret_providers.register(job, session.get(Site, site_id), [
            SimpleNamespace(
                enabled=True, feature_type=checkpoint_feature, step_key="resign",
                config={},
            ),
            SimpleNamespace(enabled=False, feature_type=checkpoint_feature, step_key="disabled"),
            SimpleNamespace(enabled=True, feature_type="post.resign", step_key="legacy"),
            SimpleNamespace(enabled=True, feature_type="post.resign_click", step_key="removed"),
        ])
    body = {
        "job_id": job_id, "site_id": site_id, "step_key": "resign", "phase": "prepare",
        "date": day, "result": {"resign": _result(
            day, submit_attempted=True, unconfirmed=[{"date": day, "message": "private-input"}],
        )},
    }
    return manager, token, body


def _post_checkpoint(client, token, body):
    return client.post(CHECKPOINT_PATH, json=body, headers={"Authorization": f"Bearer {token}"})


def _second_grant(manager, site_id, *, feature="post.resign_loop"):
    with manager.session_factory() as session:
        run = Run(mode="manual", trigger="manual", status="running", created_by="admin")
        session.add(run)
        session.flush()
        job = SiteJob(run_id=run.id, site_id=site_id, site_name="second", lane="general", status="running")
        session.add(job)
        session.commit()
        token = manager.secret_providers.register(job, session.get(Site, site_id), [
            SimpleNamespace(enabled=True, feature_type=feature, step_key="resign", config={}),
        ])
        return job.id, token


def test_prepare_ack_is_committed_and_replay_follows_feature_identity(client, checkpoint, checkpoint_feature):
    manager, token, body = checkpoint
    response = _post_checkpoint(client, token, body)
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert response.headers["cache-control"] == "no-store"
    with manager.session_factory() as session:
        row = session.query(SiteResignRecord).filter_by(site_id=body["site_id"]).one()
        assert row.status == "unknown" and row.job_id == body["job_id"]
        assert row.run_id == session.get(SiteJob, body["job_id"]).run_id
        assert "private-input" not in row.message
    assert _post_checkpoint(client, token, body).status_code == (
        200 if checkpoint_feature == "post.resign_loop" else 409
    )


@pytest.mark.parametrize("change", [
    "missing", "wrongtoken", "revoked", "expired", "wrongstep", "disabled", "wrongsite",
    "wrongjob", "attempt", "finished", "cancel", "runfinished", "clear", "reregister", "legacy", "removed",
])
def test_checkpoint_authorization_fails_closed(client, checkpoint, monkeypatch, change):
    manager, token, body = checkpoint
    if change == "missing":
        assert client.post(CHECKPOINT_PATH, json=body).status_code == 403
        return
    if change == "wrongtoken":
        token = "wrong"
    elif change == "revoked":
        manager.secret_providers.revoke(body["job_id"])
    elif change == "clear":
        manager.secret_providers.clear()
    elif change == "expired":
        import app.services.secret_providers as module
        now = module.time.monotonic()
        monkeypatch.setattr(module.time, "monotonic", lambda: now + manager.settings.execution_timeout + 1)
    elif change in {"wrongstep", "disabled", "legacy", "removed"}:
        body["step_key"] = "open" if change == "wrongstep" else change
    elif change in {"wrongsite", "wrongjob"}:
        body["site_id" if change == "wrongsite" else "job_id"] += 1000
    else:
        with manager.session_factory() as session:
            job = session.get(SiteJob, body["job_id"])
            if change == "attempt":
                job.attempt += 1
            elif change == "finished":
                job.status = "success"
            elif change == "cancel":
                session.get(Run, job.run_id).cancel_requested = True
            elif change == "runfinished":
                session.get(Run, job.run_id).status = "success"
            elif change == "reregister":
                manager.secret_providers.register(job, session.get(Site, body["site_id"]), [])
            session.commit()
    assert _post_checkpoint(client, token, body).status_code == 403
    assert _records(manager, body["site_id"]) == {}


def test_resign_snapshot_does_not_grant_credentials(client, checkpoint):
    manager, token, body = checkpoint
    with pytest.raises(SecretDenied):
        asyncio.run(manager.secret_providers.request(
            token, body["job_id"], body["site_id"], "resign", "auth.form_login", "builtin.manual",
        ))
    # The current workflow contains no resign step; authorization uses the dispatch snapshot.
    assert _post_checkpoint(client, token, body).status_code == 200


@pytest.mark.parametrize("feature", [None, "post.resign_dialog", "post.resign", "post.resign_click", "unknown"])
def test_service_default_and_non_loop_identity_cannot_reprepare(client, checkpoint, feature):
    manager, token, body = checkpoint
    assert _post_checkpoint(client, token, body).status_code == 200
    with manager.session_factory() as session:
        args = () if feature is None else (feature,)
        with pytest.raises(ResignConflict):
            ResignService(session, manager.settings).checkpoint(
                session.get(SiteJob, body["job_id"]), "prepare", body["date"],
                body["result"]["resign"], *args,
            )


@pytest.mark.parametrize("invalid", [
    "today", "future", "noncanonical", "outsidewindow", "notpending", "attempted",
    "unconfirmed", "wrongunconfirmed", "nonnested", "badphase", "invalidsuccess", "stringentry",
])
def test_checkpoint_rejects_invalid_payloads(client, checkpoint, invalid):
    manager, token, body = checkpoint
    result = body["result"]["resign"]
    if invalid in {"today", "future", "noncanonical", "outsidewindow"}:
        day = {
            "today": _day(manager, 0), "future": _day(manager, -1),
            "noncanonical": body["date"].replace("-", ""), "outsidewindow": _day(manager, 40),
        }[invalid]
        body["date"] = day
        result["pending"] = [day]
        result["unconfirmed"] = [{"date": day}]
    elif invalid == "notpending":
        result["pending"] = []
    elif invalid == "attempted":
        result["submit_attempted"] = False
    elif invalid == "unconfirmed":
        result["unconfirmed"] = []
    elif invalid == "wrongunconfirmed":
        result["unconfirmed"] = [{"date": _day(manager, 2)}]
    elif invalid == "nonnested":
        body["result"] = result
    elif invalid == "badphase":
        body["phase"] = "click"
    elif invalid == "invalidsuccess":
        result["success"] = [{"date": _day(manager, 0)}]
    elif invalid == "stringentry":
        result["unconfirmed"] = [body["date"]]
    assert _post_checkpoint(client, token, body).status_code == 422
    assert _records(manager, body["site_id"]) == {}


@pytest.mark.parametrize("existing", [None, "failed"])
def test_concurrent_jobs_only_one_prepare_succeeds(client, checkpoint, monkeypatch, existing):
    manager, token, body = checkpoint
    second_job, second_token = _second_grant(manager, body["site_id"])
    other = {**body, "job_id": second_job}
    if existing:
        with manager.session_factory() as session:
            session.add(SiteResignRecord(
                site_id=body["site_id"], resign_date=body["date"], status=existing,
            ))
            session.commit()
    # Both workers were dispatched with identical stale, unresolved-free payloads.
    with manager.session_factory() as session:
        assert ResignService(session, manager.settings).pending_dates(body["site_id"], 30)
    barrier = Barrier(2)
    original = ResignService.pending_dates

    def pending(self, *args):
        dates = original(self, *args)
        barrier.wait(timeout=5)
        return dates

    monkeypatch.setattr(ResignService, "pending_dates", pending)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_post_checkpoint, client, key, item) for key, item in [
            (token, body), (second_token, other),
        ]]
        statuses = [future.result().status_code for future in futures]
    assert sorted(statuses) == [200, 409]
    with manager.session_factory() as session:
        rows = session.query(SiteResignRecord).filter_by(site_id=body["site_id"]).all()
        assert len(rows) == 1 and rows[0].status == "unknown"
        assert rows[0].job_id == (body["job_id"] if statuses[0] == 200 else second_job)


@pytest.mark.parametrize("terminal", ["success", "already_signed"])
def test_confirm_requires_owner_and_is_idempotent(client, checkpoint, terminal):
    manager, token, body = checkpoint
    assert _post_checkpoint(client, token, body).status_code == 200
    second_job, second_token = _second_grant(manager, body["site_id"])
    confirm = {**body, "phase": "confirm", "result": {"resign": _result(
        body["date"], **{terminal: [{"date": body["date"]}]},
    )}}
    assert _post_checkpoint(client, second_token, {**confirm, "job_id": second_job}).status_code == 409
    assert _post_checkpoint(client, token, confirm).status_code == 200
    assert _post_checkpoint(client, token, confirm).status_code == 200
    assert _post_checkpoint(client, token, body).status_code == 409
    assert _records(manager, body["site_id"]) == {body["date"]: terminal}


def test_commit_failure_never_returns_ack(client, checkpoint, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session
    manager, token, body = checkpoint
    # Unconditionally fail the endpoint's commit, after its reservation flush.
    def fail(_session):
        raise RuntimeError("commit failed")

    monkeypatch.setattr(Session, "commit", fail)
    transport = TestClient(client.app, raise_server_exceptions=False)
    response = _post_checkpoint(transport, token, body)
    assert response.status_code == 500 and response.text != '{"ok":true}'
    assert _records(manager, body["site_id"]) == {}


@pytest.mark.parametrize("retry", ["restart", "fallback_interaction"])
def test_ack_without_stream_still_blocks_retry(client, checkpoint, monkeypatch, retry, checkpoint_feature):
    manager, token, body = checkpoint
    calls = []

    async def execute(payload):
        calls.append(payload["attempt"])
        # Use the newly registered dispatch token, not the fixture's superseded token.
        headers_token = payload["secret_access"]["token"]
        response = _post_checkpoint(client, headers_token, body)
        assert response.status_code == 200
        return {"status": retry, "steps": []}

    # Ensure the actual dispatch has the capability in its resolved snapshot.
    from app.services.workflows import WorkflowService
    original = WorkflowService.resolve_site

    def resolve(service, site_id):
        resolved = original(service, site_id)
        step = resolved.steps[0].model_copy(update={"step_key": "resign", "feature_type": checkpoint_feature})
        return resolved.model_copy(update={"steps": [step]})

    monkeypatch.setattr(WorkflowService, "resolve_site", resolve)
    monkeypatch.setattr(manager, "_post_execute", execute)
    with manager.session_factory() as session:
        run_id = session.get(SiteJob, body["job_id"]).run_id
    asyncio.run(manager._execute_run(run_id))
    assert calls == [1]
    with manager.session_factory() as session:
        job = session.get(SiteJob, body["job_id"])
        assert job.status == "waiting_human" and job.attempt == 1
    assert _records(manager, body["site_id"]) == {body["date"]: "unknown"}


@pytest.mark.parametrize("status", ["unknown", "success"])
def test_stream_cannot_steal_another_jobs_reservation(client, checkpoint, status, checkpoint_feature):
    manager, token, body = checkpoint
    assert _post_checkpoint(client, token, body).status_code == 200
    second_job, _ = _second_grant(manager, body["site_id"])
    result = _result(body["date"], **{
        "unconfirmed" if status == "unknown" else status: [{"date": body["date"]}],
    })
    manager._persist_progress(second_job, {"steps": [_step(result, feature=checkpoint_feature)]})
    with manager.session_factory() as session:
        job = session.get(SiteJob, second_job)
        ResignService(session, manager.settings).record_results(
            body["site_id"], job.run_id, job.id, result, feature_type=checkpoint_feature,
        )
        session.commit()
        row = session.query(SiteResignRecord).filter_by(site_id=body["site_id"]).one()
        assert row.job_id == body["job_id"] and row.status == "unknown"


@pytest.mark.parametrize("status", ["unknown", "success", "already_signed"])
def test_existing_reservation_follows_feature_and_terminal_dates_never_prepare(
    client, checkpoint, checkpoint_feature, status,
):
    manager, token, body = checkpoint
    with manager.session_factory() as session:
        session.add(SiteResignRecord(
            site_id=body["site_id"], resign_date=body["date"], status=status,
        ))
        session.commit()
    assert _post_checkpoint(client, token, body).status_code == (
        200 if status == "unknown" and checkpoint_feature == "post.resign_loop" else 409
    )


def test_confirm_only_updates_owned_target_in_cumulative_state(client, checkpoint):
    manager, token, body = checkpoint
    confirm = {**body, "phase": "confirm", "result": {"resign": _result(
        body["date"], success=[{"date": body["date"]}],
    )}}
    assert _post_checkpoint(client, token, confirm).status_code == 409
    assert _post_checkpoint(client, token, body).status_code == 200
    other_day = _day(manager, 2)
    with manager.session_factory() as session:
        session.add(SiteResignRecord(
            site_id=body["site_id"], resign_date=other_day, status="unknown", job_id=999,
        ))
        session.commit()
    confirm["result"]["resign"]["pending"].append(other_day)
    confirm["result"]["resign"]["success"].append({"date": other_day})
    assert _post_checkpoint(client, token, confirm).status_code == 200
    assert _records(manager, body["site_id"]) == {body["date"]: "success", other_day: "unknown"}
    with manager.session_factory() as session:
        row = session.query(SiteResignRecord).filter_by(resign_date=other_day).one()
        assert row.job_id == 999


@pytest.mark.parametrize("readonly_owner", [None, "other_job"])
def test_confirm_accepts_readonly_already_signed_without_claiming_it(client, checkpoint, readonly_owner):
    manager, token, body = checkpoint
    readonly_day = _day(manager, 2)
    previous_day = _day(manager, 3)
    with manager.session_factory() as session:
        job = session.get(SiteJob, body["job_id"])
        session.add(SiteResignRecord(
            site_id=body["site_id"], resign_date=previous_day, status="success",
            job_id=job.id, run_id=job.run_id,
        ))
        if readonly_owner:
            session.add(SiteResignRecord(
                site_id=body["site_id"], resign_date=readonly_day, status="unknown",
                job_id=999, run_id=999, message="original reservation",
            ))
        session.commit()
    state = _result(
        body["date"], pending=[readonly_day, previous_day, body["date"]],
        already_signed=[{"date": readonly_day, "message": "observed done"}],
        success=[{"date": previous_day}], submit_attempted=True,
        unconfirmed=[{"date": body["date"]}],
    )
    prepare = {**body, "result": {"resign": state}}
    assert _post_checkpoint(client, token, prepare).status_code == 200
    state["success"].append({"date": body["date"], "message": "confirmed done"})
    state["unconfirmed"] = []
    state["remaining"] = []
    confirm = {**prepare, "phase": "confirm"}
    assert _post_checkpoint(client, token, confirm).status_code == 200
    assert _post_checkpoint(client, token, confirm).status_code == 200
    with manager.session_factory() as session:
        target = session.query(SiteResignRecord).filter_by(resign_date=body["date"]).one()
        assert target.status == "success" and target.job_id == body["job_id"]
        readonly = session.query(SiteResignRecord).filter_by(resign_date=readonly_day).one_or_none()
        if readonly_owner:
            assert (readonly.status, readonly.job_id, readonly.run_id, readonly.message) == (
                "unknown", 999, 999, "original reservation",
            )
        else:
            assert readonly is None
    second_job, second_token = _second_grant(manager, body["site_id"])
    assert _post_checkpoint(client, second_token, {**confirm, "job_id": second_job}).status_code == 409


def test_confirm_requires_explicit_target_result(client, checkpoint):
    manager, token, body = checkpoint
    assert _post_checkpoint(client, token, body).status_code == 200
    other_day = _day(manager, 2)
    confirm = {**body, "phase": "confirm", "result": {"resign": _result(
        body["date"], pending=[body["date"], other_day],
        success=[{"date": other_day}], already_signed=[{"date": other_day}],
    )}}
    assert _post_checkpoint(client, token, confirm).status_code == 422
    assert _records(manager, body["site_id"]) == {body["date"]: "unknown"}


def test_stale_unresolved_free_payload_cannot_claim_after_first_ack(client, checkpoint):
    manager, token, body = checkpoint
    second_job, second_token = _second_grant(manager, body["site_id"])
    with manager.session_factory() as session:
        assert session.query(SiteResignRecord).filter_by(status="unknown").count() == 0
    assert _post_checkpoint(client, token, body).status_code == 200
    assert _post_checkpoint(client, second_token, {**body, "job_id": second_job}).status_code == 409


def test_revocation_before_commit_rolls_back_reservation(client, checkpoint, monkeypatch):
    manager, token, body = checkpoint
    original = ResignService.checkpoint

    def reserve_then_revoke(service, job, *args):
        original(service, job, *args)
        manager.secret_providers.revoke(job.id)

    monkeypatch.setattr(ResignService, "checkpoint", reserve_then_revoke)
    assert _post_checkpoint(client, token, body).status_code == 403
    assert _records(manager, body["site_id"]) == {}


def test_owned_unknown_cannot_be_downgraded_to_failed_by_stream(client, checkpoint, checkpoint_feature):
    manager, token, body = checkpoint
    assert _post_checkpoint(client, token, body).status_code == 200
    manager._persist_progress(body["job_id"], {"steps": [_step(_result(
        body["date"], failed=[{"date": body["date"]}],
    ), feature=checkpoint_feature)]})
    assert _records(manager, body["site_id"]) == {body["date"]: "unknown"}


@pytest.mark.parametrize("feature", ["post.resign", "post.resign_loop", "post.resign_dialog"])
def test_legacy_failure_cannot_release_reservation_for_another_prepare(client, checkpoint, feature):
    manager, token, body = checkpoint
    assert _post_checkpoint(client, token, body).status_code == 200
    other_job, _ = _second_grant(manager, body["site_id"])
    with manager.session_factory() as session:
        job = session.get(SiteJob, other_job)
        ResignService(session, manager.settings).record_results(
            body["site_id"], job.run_id, job.id,
            _result(body["date"], failed=[{"date": body["date"]}]), feature_type=feature,
        )
        session.commit()
        row = session.query(SiteResignRecord).filter_by(site_id=body["site_id"]).one()
        assert row.status == "unknown" and row.job_id == body["job_id"]
    third_job, third_token = _second_grant(manager, body["site_id"])
    assert _post_checkpoint(client, third_token, {**body, "job_id": third_job}).status_code == 409


@pytest.mark.parametrize("previous", ["failed", "unknown"])
def test_readonly_done_closes_foreign_history_without_taking_ownership(client, checkpoint, previous, checkpoint_feature):
    manager, token, body = checkpoint
    with manager.session_factory() as session:
        owner = session.get(SiteJob, body["job_id"])
        owner_run = owner.run_id
        session.add(SiteResignRecord(
            site_id=body["site_id"], resign_date=body["date"], status=previous,
            job_id=owner.id, run_id=owner_run,
        ))
        session.commit()
        assert body["date"] in ResignService(session, manager.settings).pending_dates(body["site_id"], 30)
    other_job, other_token = _second_grant(manager, body["site_id"])
    manager._persist_progress(other_job, {"steps": [_step(_result(
        body["date"], already_signed=[{"date": body["date"], "message": "exact done observed"}],
    ), feature=checkpoint_feature)]})
    with manager.session_factory() as session:
        row = session.query(SiteResignRecord).filter_by(site_id=body["site_id"]).one()
        assert (row.status, row.job_id, row.run_id) == ("already_signed", body["job_id"], owner_run)
        assert body["date"] not in ResignService(session, manager.settings).pending_dates(body["site_id"], 30)
    assert _post_checkpoint(client, other_token, {**body, "job_id": other_job}).status_code == 409
    assert _post_checkpoint(client, token, body).status_code == 409


@pytest.mark.parametrize("incoming", ["failed", "success", "already_signed", "unconfirmed"])
def test_legacy_results_cannot_change_foreign_unknown_owner(client, checkpoint, incoming):
    manager, token, body = checkpoint
    assert _post_checkpoint(client, token, body).status_code == 200
    other_job, _ = _second_grant(manager, body["site_id"])
    with manager.session_factory() as session:
        job = session.get(SiteJob, other_job)
        ResignService(session, manager.settings).record_results(
            body["site_id"], job.run_id, job.id,
            {incoming: [{"date": body["date"], "message": "legacy result"}]},
        )
        session.commit()
        row = session.query(SiteResignRecord).filter_by(site_id=body["site_id"]).one()
        assert row.status == "unknown" and row.job_id == body["job_id"]


@pytest.mark.parametrize("scope", ["missing", "outside", "noncanonical"])
def test_readonly_done_requires_valid_pending_scope_to_close_foreign_reservation(client, checkpoint, scope, checkpoint_feature):
    manager, token, body = checkpoint
    assert _post_checkpoint(client, token, body).status_code == 200
    other_job, _ = _second_grant(manager, body["site_id"])
    day = body["date"]
    result = _result(day, already_signed=[{"date": day}])
    if scope == "missing":
        result.pop("pending")
    elif scope == "outside":
        result["pending"] = []
    else:
        result["already_signed"] = [{"date": day.replace("-", "")}]
        result["pending"] = [day.replace("-", "")]
    manager._persist_progress(other_job, {"steps": [_step(result, feature=checkpoint_feature)]})
    assert _records(manager, body["site_id"]) == {day: "unknown"}


@pytest.mark.parametrize("feature", ["post.resign", "post.resign_loop", "post.resign_dialog"])
@pytest.mark.parametrize("terminal", ["success", "already_signed"])
@pytest.mark.parametrize("incoming", ["failed", "unknown", "success", "already_signed"])
def test_all_features_preserve_terminal_status_and_owner(execution, feature, terminal, incoming):
    manager, site_id, run_id, job_id = execution
    day = _day(manager)
    with manager.session_factory() as session:
        session.add(SiteResignRecord(
            site_id=site_id, resign_date=day, status=terminal, job_id=999, run_id=999,
            message="original confirmation",
        ))
        session.commit()
        result = _result(day, **{"unconfirmed" if incoming == "unknown" else incoming: [{"date": day}]})
        ResignService(session, manager.settings).record_results(
            site_id, run_id, job_id, result, feature_type=feature,
        )
        session.commit()
        row = session.query(SiteResignRecord).filter_by(site_id=site_id).one()
        assert (row.status, row.job_id, row.run_id, row.message) == (
            terminal, 999, 999, "original confirmation",
        )


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("incoming_source", ["missing", "empty"])
def test_dialog_checkpoint_merge_preserves_inferred_source_and_unknown_reservation(
    execution, owned, incoming_source,
):
    manager, site_id, run_id, job_id = execution
    day = _day(manager)
    previous = _result(day, already_signed=[day], inferred_signed=[day], remaining=[])
    with manager.session_factory() as session:
        manager._save_steps(session, session.get(SiteJob, job_id), [
            _step(previous, feature="post.resign_dialog"),
        ], allow_running=True)
        session.commit()
    # A reservation may be created after the runner's inferred observation.
    with manager.session_factory() as session:
        reserved = SiteResignRecord(
            site_id=site_id, resign_date=day, status="unknown",
            job_id=job_id if owned else 999, run_id=run_id if owned else 999,
            message="reservation after snapshot",
        )
        session.add(reserved)
        session.commit()
        session.refresh(reserved)
        original = (reserved.status, reserved.job_id, reserved.run_id, reserved.message, reserved.updated_at)
    incoming = {"already_signed": [day]}
    if incoming_source == "empty":
        incoming["inferred_signed"] = []
    manager._persist_progress(job_id, {"steps": [
        _step(incoming, feature="post.resign_dialog", status="success"),
    ]})
    with manager.session_factory() as session:
        saved = session.query(JobStep).filter_by(site_job_id=job_id).one()
        merged = json.loads(saved.data_json)["resign"]
        assert merged["inferred_signed"] == [day]
        assert merged["already_signed"] == [day]
        assert merged["pending"] == [day]
        row = session.query(SiteResignRecord).filter_by(site_id=site_id).one()
        assert (row.status, row.job_id, row.run_id, row.message, row.updated_at) == original


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("inferred", [False, True])
@pytest.mark.parametrize("writer", ["progress", "record_results"])
def test_missing_inference_cannot_close_reservation_created_after_dispatch_snapshot(
    client, checkpoint, checkpoint_feature, owned, inferred, writer,
):
    manager, token, body = checkpoint
    with manager.session_factory() as session:
        # Dispatch observed a pending date with no unresolved reservation.
        pending = ResignService(session, manager.settings).pending_dates(body["site_id"], 30)
        assert body["date"] in pending
        assert session.query(SiteResignRecord).filter_by(site_id=body["site_id"]).count() == 0
    observing_job = body["job_id"] if owned else _second_grant(manager, body["site_id"])[0]
    assert _post_checkpoint(client, token, body).status_code == 200
    with manager.session_factory() as session:
        reserved = session.query(SiteResignRecord).filter_by(site_id=body["site_id"]).one()
        original = (reserved.status, reserved.job_id, reserved.run_id, reserved.message, reserved.updated_at)
    result = _result(
        body["date"], pending=pending, already_signed=[body["date"]], remaining=[],
        inferred_signed=[body["date"]] if inferred else [],
    )
    if writer == "progress":
        manager._persist_progress(observing_job, {"steps": [
            _step(result, feature=checkpoint_feature, status="success"),
        ]})
    else:
        with manager.session_factory() as session:
            job = session.get(SiteJob, observing_job)
            ResignService(session, manager.settings).record_results(
                body["site_id"], job.run_id, job.id, result, feature_type=checkpoint_feature,
            )
            session.commit()
    with manager.session_factory() as session:
        row = session.query(SiteResignRecord).filter_by(site_id=body["site_id"]).one()
        if inferred and checkpoint_feature == "post.resign_dialog":
            assert (row.status, row.job_id, row.run_id, row.message, row.updated_at) == original
            assert body["date"] in ResignService(session, manager.settings).pending_dates(body["site_id"], 30)
        else:
            assert (row.status, row.job_id, row.run_id) == ("already_signed", original[1], original[2])


@pytest.mark.parametrize("previous", [None, "failed"])
def test_dialog_missing_inference_still_records_unreserved_dates(execution, previous):
    manager, site_id, run_id, job_id = execution
    day = _day(manager)
    with manager.session_factory() as session:
        if previous:
            session.add(SiteResignRecord(
                site_id=site_id, resign_date=day, status=previous, run_id=999, job_id=999,
            ))
            session.commit()
        ResignService(session, manager.settings).record_results(
            site_id, run_id, job_id,
            _result(day, already_signed=[day], inferred_signed=[day]),
            feature_type="post.resign_dialog",
        )
        session.commit()
        row = session.query(SiteResignRecord).filter_by(site_id=site_id).one()
        assert row.status == "already_signed"
        assert (row.run_id, row.job_id) == ((999, 999) if previous else (run_id, job_id))


@pytest.mark.parametrize("feature", ["post.resign", "post.resign_loop", "post.resign_dialog"])
@pytest.mark.parametrize("incoming", ["failed", "success", "already_signed", "inferred_signed"])
def test_result_cas_does_not_overwrite_a_concurrent_prepare(
    client, checkpoint, checkpoint_feature, feature, incoming,
):
    from sqlalchemy import event

    manager, token, body = checkpoint
    other_job, _ = _second_grant(manager, body["site_id"])
    with manager.session_factory() as session:
        job = session.get(SiteJob, other_job)
        session.add(SiteResignRecord(
            site_id=body["site_id"], resign_date=body["date"], status="failed",
            job_id=job.id, run_id=job.run_id,
        ))
        session.commit()
        engine = session.get_bind()
        raced = []

        def prepare_before_update(_conn, _cursor, statement, parameters, _context, _many):
            if raced or not statement.startswith("UPDATE site_resign_records"):
                return
            if "stale result" not in parameters:
                return
            if feature == "post.resign_dialog" and incoming == "inferred_signed":
                assert "site_resign_records.status !=" in statement
                assert "unknown" in parameters
            raced.append(True)
            assert _post_checkpoint(client, token, body).status_code == 200

        event.listen(engine, "before_cursor_execute", prepare_before_update)
        try:
            result = _result(body["date"], **{
                "already_signed" if incoming == "inferred_signed" else incoming:
                    [{"date": body["date"], "message": "stale result"}],
            })
            if incoming == "inferred_signed":
                result["inferred_signed"] = [body["date"]]
            ResignService(session, manager.settings).record_results(
                body["site_id"], job.run_id, job.id,
                result,
                feature_type=feature,
            )
            session.commit()
        finally:
            event.remove(engine, "before_cursor_execute", prepare_before_update)
        assert raced == [True]
    with manager.session_factory() as session:
        row = session.query(SiteResignRecord).filter_by(site_id=body["site_id"]).one()
        assert row.status == "unknown" and row.job_id == body["job_id"]
    assert _post_checkpoint(client, token, body).status_code == (
        200 if checkpoint_feature == "post.resign_loop" else 409
    )


@pytest.mark.parametrize("checkpoint_feature", ["post.resign_loop"])
class TestLoopCheckpoint:
    def test_same_job_reprepares_and_only_owner_confirms(self, client, checkpoint):
        manager, token, body = checkpoint
        # Only the dispatched feature identity grants repeated preparation.
        for _ in range(4):
            assert _post_checkpoint(client, token, body).status_code == 200
        other_job, other_token = _second_grant(manager, body["site_id"])
        assert _post_checkpoint(client, other_token, {**body, "job_id": other_job}).status_code == 409
        confirm = {**body, "phase": "confirm", "result": {"resign": _result(
            body["date"], success=[{"date": body["date"]}],
        )}}
        assert _post_checkpoint(client, other_token, {**confirm, "job_id": other_job}).status_code == 409
        assert _post_checkpoint(client, token, confirm).status_code == 200
        assert _post_checkpoint(client, token, confirm).status_code == 200
        assert _post_checkpoint(client, token, body).status_code == 409

    @pytest.mark.parametrize("status", ["success", "already_signed"])
    def test_terminal_dates_never_reprepare(self, client, checkpoint, status):
        manager, token, body = checkpoint
        with manager.session_factory() as session:
            job = session.get(SiteJob, body["job_id"])
            session.add(SiteResignRecord(
                site_id=body["site_id"], resign_date=body["date"], status=status,
                job_id=job.id, run_id=job.run_id, message="original confirmation",
            ))
            session.commit()
        assert _post_checkpoint(client, token, body).status_code == 409
        assert _records(manager, body["site_id"]) == {body["date"]: status}

    @pytest.mark.parametrize("status", ["queued", "running", "waiting_human", "fail", "error", "canceled", "unknown"])
    def test_foreign_unknown_owner_activity_and_takeover(self, client, checkpoint, status):
        manager, token, body = checkpoint
        old_job_id, old_token = _second_grant(manager, body["site_id"])
        old_body = {**body, "job_id": old_job_id}
        assert _post_checkpoint(client, old_token, old_body).status_code == 200
        with manager.session_factory() as session:
            old_job = session.get(SiteJob, old_job_id)
            old_run_id = old_job.run_id
            old_job.status = status
            if status not in {"queued", "running"}:
                old_job.finished_at = datetime.now(timezone.utc)
            session.commit()
        response = _post_checkpoint(client, token, body)
        if status in {"queued", "running"}:
            assert response.status_code == 409
            with manager.session_factory() as session:
                row = session.query(SiteResignRecord).filter_by(site_id=body["site_id"]).one()
                assert (row.job_id, row.run_id, row.status) == (old_job_id, old_run_id, "unknown")
            return
        assert response.status_code == 200
        assert _post_checkpoint(client, token, body).status_code == 200
        with manager.session_factory() as session:
            row = session.query(SiteResignRecord).filter_by(site_id=body["site_id"]).one()
            assert row.job_id == body["job_id"] and row.status == "unknown"
            assert row.run_id == session.get(SiteJob, body["job_id"]).run_id
            # Ownership is enforced by the service even independently of grant expiry.
            with pytest.raises(ResignConflict):
                ResignService(session, manager.settings).checkpoint(
                    session.get(SiteJob, old_job_id), "confirm", body["date"],
                    _result(body["date"], success=[{"date": body["date"]}]), "post.resign_loop",
                )
        confirm = {**body, "phase": "confirm", "result": {"resign": _result(
            body["date"], success=[{"date": body["date"]}],
        )}}
        assert _post_checkpoint(client, old_token, {**confirm, "job_id": old_job_id}).status_code == 403
        assert _post_checkpoint(client, token, confirm).status_code == 200

    @pytest.mark.parametrize("owner", [None, "missing"])
    def test_unknown_without_live_owner_can_be_claimed(self, client, checkpoint, owner):
        manager, token, body = checkpoint
        with manager.session_factory() as session:
            session.add(SiteResignRecord(
                site_id=body["site_id"], resign_date=body["date"], status="unknown",
                job_id=999 if owner else None, run_id=999 if owner else None,
            ))
            session.commit()
        assert _post_checkpoint(client, token, body).status_code == 200
        with manager.session_factory() as session:
            row = session.query(SiteResignRecord).filter_by(site_id=body["site_id"]).one()
            assert row.job_id == body["job_id"] and row.status == "unknown"

    def test_concurrent_takeovers_compare_and_swap_owner(self, client, checkpoint, monkeypatch):
        manager, token, body = checkpoint
        second_job, second_token = _second_grant(manager, body["site_id"])
        with manager.session_factory() as session:
            session.add(SiteResignRecord(
                site_id=body["site_id"], resign_date=body["date"], status="unknown",
            ))
            session.commit()
        barrier = Barrier(2)
        original = ResignService.pending_dates

        def pending(service, *args):
            dates = original(service, *args)
            barrier.wait(timeout=5)
            return dates

        monkeypatch.setattr(ResignService, "pending_dates", pending)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(_post_checkpoint, client, key, item) for key, item in [
                (token, body), (second_token, {**body, "job_id": second_job}),
            ]]
            statuses = [future.result().status_code for future in futures]
        assert sorted(statuses) == [200, 409]
        with manager.session_factory() as session:
            row = session.query(SiteResignRecord).filter_by(site_id=body["site_id"]).one()
            assert row.status == "unknown"
            assert row.job_id == (body["job_id"] if statuses[0] == 200 else second_job)

    def test_owner_activity_is_checked_at_update(self, client, checkpoint, monkeypatch):
        manager, token, body = checkpoint
        old_job_id, old_token = _second_grant(manager, body["site_id"])
        assert _post_checkpoint(client, old_token, {**body, "job_id": old_job_id}).status_code == 200
        with manager.session_factory() as session:
            session.get(SiteJob, old_job_id).status = "fail"
            session.commit()
        original = ResignService.pending_dates

        def pending(service, *args):
            dates = original(service, *args)
            with manager.session_factory() as session:
                session.get(SiteJob, old_job_id).status = "running"
                session.commit()
            return dates

        monkeypatch.setattr(ResignService, "pending_dates", pending)
        assert _post_checkpoint(client, token, body).status_code == 409
        with manager.session_factory() as session:
            row = session.query(SiteResignRecord).filter_by(site_id=body["site_id"]).one()
            assert row.job_id == old_job_id and row.status == "unknown"

    @pytest.mark.parametrize("change", ["owner", "terminal"])
    def test_takeover_cas_preserves_concurrent_owner_or_confirmation(
        self, client, checkpoint, monkeypatch, change,
    ):
        manager, token, body = checkpoint
        new_job_id, _ = _second_grant(manager, body["site_id"])
        with manager.session_factory() as session:
            session.add(SiteResignRecord(
                site_id=body["site_id"], resign_date=body["date"], status="unknown",
            ))
            session.commit()
        original = ResignService.pending_dates

        def pending(service, *args):
            dates = original(service, *args)
            with manager.session_factory() as session:
                row = session.query(SiteResignRecord).filter_by(site_id=body["site_id"]).one()
                if change == "owner":
                    new_job = session.get(SiteJob, new_job_id)
                    row.job_id, row.run_id = new_job.id, new_job.run_id
                    # No active-job guard can mask a missing owner CAS here.
                    new_job.status = "fail"
                else:
                    row.status = "success"
                session.commit()
            return dates

        monkeypatch.setattr(ResignService, "pending_dates", pending)
        assert _post_checkpoint(client, token, body).status_code == 409
        with manager.session_factory() as session:
            row = session.query(SiteResignRecord).filter_by(site_id=body["site_id"]).one()
            if change == "owner":
                assert row.job_id == new_job_id and row.status == "unknown"
            else:
                assert row.job_id is None and row.status == "success"


@pytest.mark.parametrize("checkpoint_feature", ["post.resign_dialog"])
def test_result_feature_identity_cannot_upgrade_dialog_grant(client, checkpoint):
    _, token, body = checkpoint
    body["feature_type"] = "post.resign_loop"
    body["result"]["feature_type"] = "post.resign_loop"
    body["result"]["resign"]["feature_type"] = "post.resign_loop"
    assert _post_checkpoint(client, token, body).status_code == 200
    assert _post_checkpoint(client, token, body).status_code == 409


@pytest.mark.parametrize("feature", ["post.resign_loop", "post.resign_dialog"])
def test_dispatch_feature_snapshot_is_not_a_live_step_reference(client, checkpoint, feature):
    manager, _, body = checkpoint
    step = SimpleNamespace(enabled=True, feature_type=feature, step_key="resign", config={})
    with manager.session_factory() as session:
        token = manager.secret_providers.register(
            session.get(SiteJob, body["job_id"]), session.get(Site, body["site_id"]), [step],
        )
        step.feature_type = "post.resign_dialog" if feature == "post.resign_loop" else "post.resign_loop"
        job, granted_feature = manager.secret_providers.authorize_resign(
            token, body["job_id"], body["site_id"], "resign", session,
        )
        assert job.id == body["job_id"] and granted_feature == feature
    assert _post_checkpoint(client, token, body).status_code == 200
    assert _post_checkpoint(client, token, body).status_code == (200 if feature == "post.resign_loop" else 409)


def test_same_grant_keeps_loop_and_dialog_step_authorities_separate(client, checkpoint):
    manager, _, body = checkpoint
    with manager.session_factory() as session:
        token = manager.secret_providers.register(
            session.get(SiteJob, body["job_id"]), session.get(Site, body["site_id"]), [
                SimpleNamespace(enabled=True, feature_type=feature, step_key=key, config={})
                for key, feature in (("loop", "post.resign_loop"), ("dialog", "post.resign_dialog"))
            ],
        )
    loop_body = {**body, "step_key": "loop"}
    assert _post_checkpoint(client, token, loop_body).status_code == 200
    assert _post_checkpoint(client, token, {**body, "step_key": "dialog"}).status_code == 409
    assert _post_checkpoint(client, token, loop_body).status_code == 200


@pytest.mark.parametrize("feature", ["post.resign_loop", "post.resign_dialog"])
def test_actual_dispatch_captures_resolved_feature_identity(client, checkpoint, monkeypatch, feature):
    manager, _, body = checkpoint
    with manager.session_factory() as session:
        step = session.query(WorkflowStep).filter_by(owner_type="site", owner_id=body["site_id"]).one()
        step.feature_type = feature
        step.step_key = "resign"
        step.config_json = "{}"
        run_id = session.get(SiteJob, body["job_id"]).run_id
        session.commit()

    async def execute(payload):
        step, = payload["steps"]
        assert step["feature_type"] == feature
        assert "execution_mode" not in step["config"]
        # Worker payload edits must not change the server's dispatch authorization.
        step["feature_type"] = "post.resign_dialog" if feature == "post.resign_loop" else "post.resign_loop"
        token = payload["secret_access"]["token"]
        assert _post_checkpoint(client, token, body).status_code == 200
        assert _post_checkpoint(client, token, body).status_code == (200 if feature == "post.resign_loop" else 409)
        confirm = {**body, "phase": "confirm", "result": {"resign": _result(
            body["date"], success=[{"date": body["date"]}],
        )}}
        assert _post_checkpoint(client, token, confirm).status_code == 200
        return {"status": "success", "steps": []}

    monkeypatch.setattr(manager, "_post_execute", execute)
    asyncio.run(manager._execute_run(run_id))
    assert _records(manager, body["site_id"]) == {body["date"]: "success"}
    with manager.session_factory() as session:
        assert session.get(SiteJob, body["job_id"]).status == "success"
