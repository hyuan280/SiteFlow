from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.models.execution import JobStep, Run, SiteJob
from app.models.workflow import Site, SiteResignRecord, WorkflowStep
from app.schemas.execution import RunCreate
from app.services.execution import ExecutionService
from app.services.resign import ResignService
from tests.helpers import make_settings
from tests.test_worker_resign_click import BASE, calendar, install_checkpoint_transport
from worker.resign_checkpoint import CHECKPOINT_ERROR, make_resign_checkpoint
from worker.resign_click import run_resign_click


CHECKPOINT_PATH = "/api/v1/internal/resign/checkpoint"


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path, scheduler_timezone="America/Los_Angeles")


def _records(manager, site_id):
    # Always use a new session: a flush without a commit is not sufficient.
    with manager.session_factory() as session:
        return {
            row.resign_date: (row.status, row.run_id, row.job_id)
            for row in session.query(SiteResignRecord).filter_by(site_id=site_id).all()
        }


def _pending(manager, site_id):
    with manager.session_factory() as session:
        return ResignService(session, manager.settings).pending_dates(site_id, 30)


@pytest.fixture
def integration(anon_client, monkeypatch):
    manager = anon_client.app.state.executor
    monkeypatch.setattr(manager, "schedule_run_notification", lambda _run_id: None)
    monkeypatch.setenv("SITEFLOW_RESIGN_CHECKPOINT_URL", BASE + CHECKPOINT_PATH)
    assert not anon_client.cookies and "X-CSRF-Token" not in anon_client.headers

    with manager.session_factory() as session:
        service = ResignService(session, manager.settings)
        today = datetime.now(service.timezone).date()
        dates = [(today - timedelta(days=n)).isoformat() for n in (2, 1)]
        site = Site(name="isolated-resign-click", base_url=BASE, lane_policy="general",
                    support_resign=True, resign_max_days=30)
        history = Run(mode="manual", trigger="manual", status="fail", created_by="test")
        session.add_all([site, history])
        session.flush()
        site_id = site.id
        session.add(WorkflowStep(
            owner_type="site", owner_id=site_id, step_key="resign",
            feature_type="post.resign_click", category="post", enabled=True,
            config_json=json.dumps({"success_wait_seconds": 1}),
        ))
        session.add(SiteJob(
            run_id=history.id, site_id=site_id, site_name=site.name, lane="general",
            status="fail", finished_at=datetime.combine(
                today - timedelta(days=2), time(12), tzinfo=service.timezone,
            ).astimezone(timezone.utc),
        ))
        session.commit()
    assert _pending(manager, site_id) == dates

    state = SimpleNamespace(manager=manager, site_id=site_id, today=today.isoformat(),
                            dates=dates, http=[], lose_prepare_ack=False)

    async def transport(request):
        assert request.method == "POST" and str(request.url) == BASE + CHECKPOINT_PATH
        assert "cookie" not in request.headers and "x-csrf-token" not in request.headers
        assert request.headers["Authorization"].startswith("Bearer ")
        body = json.loads(request.content)
        response = await asyncio.to_thread(
            anon_client.post, CHECKPOINT_PATH, json=body,
            headers={"Authorization": request.headers["Authorization"]},
        )
        state.http.append({
            "body": body, "status": response.status_code,
            "records": _records(manager, site_id),
        })
        if response.status_code == 200:
            assert response.json() == {"ok": True}
            assert response.headers["cache-control"] == "no-store"
        if state.lose_prepare_ack and body["phase"] == "prepare" and response.status_code == 200:
            state.lose_prepare_ack = False
            # The real endpoint already committed; only delivery of its ACK is lost.
            raise httpx.ReadTimeout("isolated ACK loss", request=request)
        return httpx.Response(response.status_code, content=response.content, headers=response.headers)

    install_checkpoint_transport(monkeypatch, transport)

    async def execute(options=None, *, stale_unresolved=False):
        with manager.session_factory() as session:
            run = ExecutionService(session, manager.settings, manager.crypto).create_run(
                RunCreate(site_ids=[site_id]), "test",
            )
            job = session.query(SiteJob).filter_by(run_id=run.id).one()
            observed = SimpleNamespace(run_id=run.id, job_id=job.id, progress=[], result=None)

        async with calendar({"month": today.strftime("%Y-%m"), **(options or {})}) as (page, posts):
            async def worker(payload):
                observed.payload = copy.deepcopy(payload)
                if stale_unresolved:
                    payload = {**payload, "resign_unresolved_dates": []}
                observed.worker_payload = copy.deepcopy(payload)
                step, = payload["steps"]

                async def progress(data):
                    # Capture before stream persistence so it cannot manufacture ACK evidence.
                    observed.progress.append({
                        "data": copy.deepcopy(data), "records": _records(manager, site_id),
                        "posts": list(posts), "clicks": await page.evaluate("clicks"),
                        "http_count": len(state.http),
                    })
                    manager._persist_progress(payload["job_id"], {
                        "status": "running", "steps": [{
                            "step_key": step["step_key"], "feature_type": step["feature_type"],
                            "status": "running", "data": data,
                        }],
                    })

                status, message, data = await run_resign_click(
                    page, step["config"], payload["resign_pending_dates"], lambda *_: None,
                    today=payload["resign_today"], unresolved=payload["resign_unresolved_dates"],
                    base_url=payload["base_url"], progress=progress,
                    durable_progress=make_resign_checkpoint(payload, step["step_key"]),
                )
                observed.result = (status, message, data)
                return {"status": status, "message": message, "steps": [{
                    "step_key": step["step_key"], "feature_type": step["feature_type"],
                    "status": status, "message": message, "data": data,
                }]}

            monkeypatch.setattr(manager, "_post_execute", worker)
            await manager._execute_run(observed.run_id)
            observed.posts = list(posts)
            observed.clicks = await page.evaluate("clicks")

        with manager.session_factory() as session:
            job = session.get(SiteJob, observed.job_id)
            assert observed.result is not None, job.message
            observed.saved_job_status = job.status
            observed.saved_run_status = session.get(Run, observed.run_id).status
            observed.saved_result = json.loads(job.result_json)
            saved = session.query(JobStep).filter_by(site_job_id=job.id).one()
            observed.saved_step_status = saved.status
            observed.saved_data = json.loads(saved.data_json)
        assert observed.payload["resign_today"] == state.today
        assert observed.saved_job_status == observed.saved_run_status == observed.result[0]
        assert observed.saved_step_status == observed.result[0]
        assert observed.saved_result["status"] == observed.result[0]
        assert observed.saved_result["step_count"] == 1
        assert observed.saved_data == observed.result[2]
        return observed

    state.execute = execute
    yield state
    manager.secret_providers.clear()
    anon_client.close()
    anon_client.app.state.engine.dispose()


def test_already_done_then_one_click_commits_prepare_before_submit_and_confirm(integration):
    async def check():
        env = integration
        a, b = env.dates
        run = await env.execute({"signed": [a]})
        status, message, data = run.result
        assert status == "success", message
        assert run.payload["resign_pending_dates"] == [a, b]
        assert run.payload["resign_unresolved_dates"] == []
        assert run.posts == run.clicks == [b]
        state = data["resign"]
        assert state["pending"] == [a, b] and state["already_signed"] == [a]
        assert [row["date"] for row in state["success"]] == [b]
        assert state["remaining"] == state["unconfirmed"] == state["failed"] == []
        assert run.saved_data["resign"]["unconfirmed"] == []
        assert run.saved_data["resign"]["remaining"] == []
        assert state["submit_attempted"] is True
        assert (state["cards_before"], state["cards_after"]) == (3, 2)
        assert [(row["body"]["phase"], row["body"]["date"], row["status"])
                for row in env.http] == [("prepare", b, 200), ("confirm", b, 200)]
        owner = (run.run_id, run.job_id)
        assert env.http[0]["records"][b] == ("unknown", *owner)
        assert env.http[1]["records"][b] == ("success", *owner)
        prepared, = [row for row in run.progress if row["data"]["resign"]["unconfirmed"]]
        assert prepared["records"][b] == ("unknown", *owner)
        assert prepared["http_count"] == 1
        assert prepared["posts"] == prepared["clicks"] == []
        confirmed, = [row for row in run.progress if row["data"]["resign"]["success"]]
        assert confirmed["records"][b] == ("success", *owner)
        assert confirmed["http_count"] == 2
        assert confirmed["posts"] == confirmed["clicks"] == [b]
        assert env.http[-1]["body"]["result"] == data
        assert _records(env.manager, env.site_id) == {
            a: ("already_signed", *owner), b: ("success", *owner),
        }
        assert _pending(env.manager, env.site_id) == []

    asyncio.run(check())


def test_real_unknown_converges_via_readonly_done_without_another_checkpoint(integration):
    async def check():
        env = integration
        a, b = env.dates
        first = await env.execute({"signed": [a], "noResult": True})
        assert first.result[0] == "waiting_human"
        assert first.posts == first.clicks == [b]
        assert first.result[2]["resign"]["unconfirmed"][0]["date"] == b
        assert _records(env.manager, env.site_id)[b] == ("unknown", first.run_id, first.job_id)
        assert _pending(env.manager, env.site_id) == [b]
        assert [(row["body"]["phase"], row["status"]) for row in env.http] == [("prepare", 200)]

        second = await env.execute({"signed": [a, b]})
        assert second.result[0] == "success", second.result[1]
        assert second.payload["resign_pending_dates"] == second.payload["resign_unresolved_dates"] == [b]
        assert second.posts == second.clicks == []
        state = second.result[2]["resign"]
        assert state["already_signed"] == [b]
        assert state["success"] == state["unconfirmed"] == state["remaining"] == []
        assert state["submit_attempted"] is False
        assert len(env.http) == 1
        assert _records(env.manager, env.site_id) == {
            a: ("already_signed", first.run_id, first.job_id),
            b: ("already_signed", first.run_id, first.job_id),
        }
        assert _pending(env.manager, env.site_id) == []

    asyncio.run(check())


@pytest.mark.parametrize("lose_ack", [False, True], ids=["reserved", "prepare-ack-lost"])
def test_foreign_reservation_rejects_stale_payload_without_browser_submit(integration, lose_ack):
    async def check():
        env = integration
        a, b = env.dates
        env.lose_prepare_ack = lose_ack
        # Both an unconfirmed submit and a lost prepare ACK must retain the reservation.
        first = await env.execute({"signed": [a]} if lose_ack else {"signed": [a], "noResult": True})
        assert first.result[0] == "waiting_human"
        assert first.posts == first.clicks == ([] if lose_ack else [b])
        assert _records(env.manager, env.site_id)[b] == ("unknown", first.run_id, first.job_id)
        assert env.http[0]["records"][b] == ("unknown", first.run_id, first.job_id)
        if lose_ack:
            assert CHECKPOINT_ERROR in first.result[1]
            assert first.result[2]["resign"]["submit_attempted"] is False
            assert first.result[2]["resign"]["unconfirmed"] == []
            assert all(not row["data"]["resign"]["unconfirmed"] for row in first.progress)

        before = _records(env.manager, env.site_id)
        second = await env.execute({"signed": [a]}, stale_unresolved=True)
        assert second.job_id != first.job_id
        assert second.payload["resign_pending_dates"] == [b]
        assert second.payload["resign_unresolved_dates"] == [b]
        assert second.worker_payload["resign_unresolved_dates"] == []
        assert second.result[0] == "waiting_human"
        assert CHECKPOINT_ERROR in second.result[1]
        assert second.posts == second.clicks == []
        state = second.result[2]["resign"]
        assert state["submit_attempted"] is False
        assert state["success"] == state["unconfirmed"] == []
        assert state["remaining"] == [b]
        assert all(not row["data"]["resign"]["unconfirmed"] for row in second.progress)
        assert [(row["body"]["phase"], row["status"]) for row in env.http] == [
            ("prepare", 200), ("prepare", 409),
        ]
        assert [row["body"]["job_id"] for row in env.http] == [first.job_id, second.job_id]
        assert _records(env.manager, env.site_id) == before
        assert _pending(env.manager, env.site_id) == [b]

    asyncio.run(check())
