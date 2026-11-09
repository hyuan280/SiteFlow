from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.models.execution import JobStep, Run, SiteJob
from app.models.workflow import Site, WorkflowStep
from app.schemas.execution import RunCreate
from app.services.execution import ExecutionService
from app.services.resign import ResignService
from tests.helpers import make_settings
from tests.test_resign_click_integration import CHECKPOINT_PATH, _pending, _records
from tests.test_worker_resign_click import install_checkpoint_transport
from tests.test_worker_resign_dialog import BASE, dialog_page
from worker import executor
from worker.resign_checkpoint import CHECKPOINT_ERROR


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path, scheduler_timezone="America/Los_Angeles")


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
        site = Site(name="isolated-resign-dialog", base_url=BASE, lane_policy="general",
                    support_resign=True, resign_max_days=30)
        history = Run(mode="manual", trigger="manual", status="fail", created_by="test")
        session.add_all([site, history])
        session.flush()
        site_id = site.id
        session.add(WorkflowStep(
            owner_type="site", owner_id=site_id, step_key="resign-dialog",
            feature_type="post.resign_dialog", category="post", enabled=True,
            config_json=json.dumps({"dialog_wait_seconds": 1, "success_wait_seconds": 3}),
        ))
        session.add(SiteJob(
            run_id=history.id, site_id=site_id, site_name=site.name, lane="general",
            status="fail", finished_at=datetime.combine(
                today - timedelta(days=2), time(12), tzinfo=service.timezone,
            ).astimezone(timezone.utc),
        ))
        session.commit()
    assert _pending(manager, site_id) == dates
    env = SimpleNamespace(manager=manager, site_id=site_id, dates=dates, http=[],
                          denied=[], probe_authorization=False, lose_prepare_ack=False)

    async def transport(request):
        assert request.method == "POST" and str(request.url) == BASE + CHECKPOINT_PATH
        assert "cookie" not in request.headers and "x-csrf-token" not in request.headers
        authorization = request.headers["Authorization"]
        assert authorization == "Bearer " + env.current.payload["secret_access"]["token"]
        body = json.loads(request.content)
        assert body["job_id"] == env.current.job_id and body["site_id"] == site_id
        assert body["step_key"] == "resign-dialog"

        if env.probe_authorization:
            env.probe_authorization = False
            before = _records(manager, site_id)
            for scope in ("missing-token", "wrong-token", "job_id", "site_id", "step_key"):
                headers = {"Authorization": authorization}
                denied_body = copy.deepcopy(body)
                if scope == "missing-token":
                    headers = {}
                elif scope == "wrong-token":
                    headers["Authorization"] = "Bearer invalid-capability"
                elif scope == "step_key":
                    denied_body[scope] = "not-authorized"
                else:
                    denied_body[scope] += 10000
                denied = await asyncio.to_thread(
                    anon_client.post, CHECKPOINT_PATH, json=denied_body, headers=headers,
                )
                assert denied.status_code == 403, denied.text
                assert _records(manager, site_id) == before
                env.denied.append(scope)

        response = await asyncio.to_thread(
            anon_client.post, CHECKPOINT_PATH, json=body,
            headers={"Authorization": authorization},
        )
        snapshot = {
            "body": body, "status": response.status_code,
            "records": _records(manager, site_id), "posts": list(env.server["posts"]),
            "clicks": await env.page.evaluate("clicks"),
        }
        env.http.append(snapshot)
        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True}
        assert response.headers["cache-control"] == "no-store"
        if body["phase"] == "prepare":
            # A separate session sees the commit while delivery of the ACK is held.
            assert snapshot["records"][body["date"]] == (
                "unknown", env.current.run_id, env.current.job_id,
            )
            await asyncio.sleep(.1)
            assert env.server["posts"] == [] and await env.page.evaluate("clicks") == []
            assert all(not row["data"]["resign"]["submit_attempted"]
                       for row in env.current.progress)
            if env.lose_prepare_ack:
                env.lose_prepare_ack = False
                raise httpx.ReadTimeout("isolated ACK loss after commit", request=request)
        return httpx.Response(response.status_code, content=response.content, headers=response.headers)

    install_checkpoint_transport(monkeypatch, transport)

    async def execute(options=None):
        with manager.session_factory() as session:
            run = ExecutionService(session, manager.settings, manager.crypto).create_run(
                RunCreate(site_ids=[site_id]), "test",
            )
            job = session.query(SiteJob).filter_by(run_id=run.id).one()
            observed = SimpleNamespace(run_id=run.id, job_id=job.id, progress=[], result=None)
        env.current = observed
        async with dialog_page(options, buttons=[{"date": stamp} for stamp in dates]) as (page, server):
            env.page, env.server = page, server

            async def worker(payload):
                observed.payload = copy.deepcopy(payload)
                step, = payload["steps"]
                assert step["feature_type"] == "post.resign_dialog"

                async def progress(data):
                    # Observe before stream persistence: streaming must not create the reservation.
                    observed.progress.append({
                        "data": copy.deepcopy(data), "records": _records(manager, site_id),
                        "posts": list(server["posts"]), "clicks": await page.evaluate("clicks"),
                        "http_count": len(env.http),
                    })
                    manager._persist_progress(payload["job_id"], {
                        "status": "running", "steps": [{
                            "step_key": step["step_key"], "feature_type": step["feature_type"],
                            "status": "running", "data": data,
                        }],
                    })

                status, message, data = await executor._run_step(
                    page, step, None, None, None, None, None, payload["lane_key"],
                    resign_pending_dates=payload["resign_pending_dates"],
                    secret_payload=payload, resign_progress=progress,
                )
                observed.result = (status, message, data)
                return {"status": status, "message": message, "steps": [{
                    "step_key": step["step_key"], "feature_type": step["feature_type"],
                    "status": status, "message": message, "data": data,
                }]}

            monkeypatch.setattr(manager, "_post_execute", worker)
            await manager._execute_run(observed.run_id)
            observed.posts = list(server["posts"])
            observed.clicks = await page.evaluate("clicks")
            observed.opens, observed.loads = server["opens"], server["loads"]
            observed.cards = server["cards"]

        with manager.session_factory() as session:
            job = session.get(SiteJob, observed.job_id)
            assert observed.result is not None, job.message
            assert job.status == session.get(Run, observed.run_id).status == observed.result[0]
            saved = session.query(JobStep).filter_by(site_job_id=job.id).one()
            assert saved.feature_type == "post.resign_dialog"
            assert saved.status == observed.result[0]
            observed.saved_data = json.loads(saved.data_json)
            assert observed.saved_data == observed.result[2]
            result = json.loads(job.result_json)
            assert result["status"] == observed.result[0] and result["step_count"] == 1
        assert observed.payload["resign_today"] == today.isoformat()
        assert observed.job_id not in manager.secret_providers.jobs
        return observed

    env.execute = execute
    try:
        yield env
    finally:
        manager.secret_providers.clear()
        anon_client.close()
        anon_client.app.state.engine.dispose()


def test_real_capability_prepare_commit_before_ack_and_confirm_success(integration):
    async def check():
        env = integration
        a, b = env.dates
        env.probe_authorization = True
        run = await env.execute({"done": [a], "dialogDelay": 100, "buttonsDelay": 150})
        status, message, data = run.result
        assert status == "success", message
        assert run.payload["resign_pending_dates"] == [a, b]
        assert run.payload["resign_unresolved_dates"] == []
        assert env.denied == ["missing-token", "wrong-token", "job_id", "site_id", "step_key"]
        assert run.posts == [b] and (run.opens, run.loads, run.cards) == (1, 2, 2)
        state = data["resign"]
        assert state["pending"] == [a, b] and state["already_signed"] == [a]
        assert [row["date"] for row in state["success"]] == [b]
        assert state["remaining"] == state["unconfirmed"] == state["failed"] == []
        assert state["submit_attempted"] is True
        assert (state["cards_before"], state["cards_after"]) == (3, 2)
        assert [(row["body"]["phase"], row["body"]["date"], row["status"])
                for row in env.http] == [("prepare", b, 200), ("confirm", b, 200)]
        owner = (run.run_id, run.job_id)
        assert env.http[0]["records"][b] == ("unknown", *owner)
        assert env.http[0]["posts"] == env.http[0]["clicks"] == []
        prepared, = [row for row in run.progress if row["data"]["resign"]["unconfirmed"]]
        assert prepared["records"][b] == ("unknown", *owner)
        assert prepared["http_count"] == 1 and prepared["posts"] == prepared["clicks"] == []
        confirmed, = [row for row in run.progress if row["data"]["resign"]["success"]]
        assert confirmed["records"][b] == ("success", *owner)
        assert confirmed["http_count"] == 2 and confirmed["posts"] == [b]
        assert env.http[1]["records"][b] == ("success", *owner)
        assert env.http[1]["body"]["result"] == data
        assert _records(env.manager, env.site_id) == {
            a: ("already_signed", *owner), b: ("success", *owner),
        }
        assert _pending(env.manager, env.site_id) == []

    asyncio.run(check())


def test_lost_prepare_ack_never_clicks_and_retains_committed_unknown(integration):
    async def check():
        env = integration
        a, b = env.dates
        env.lose_prepare_ack = True
        run = await env.execute({"done": [a]})
        status, message, data = run.result
        assert status == "waiting_human" and CHECKPOINT_ERROR in message
        assert run.posts == run.clicks == []
        assert run.opens == 1 and run.loads == 1 and run.cards == 3
        state = data["resign"]
        assert state["submit_attempted"] is False
        assert state["success"] == state["unconfirmed"] == []
        assert state["remaining"] == [b]
        assert all(not row["data"]["resign"]["submit_attempted"]
                   and not row["data"]["resign"]["unconfirmed"] for row in run.progress)
        assert [(row["body"]["phase"], row["status"]) for row in env.http] == [("prepare", 200)]
        assert env.http[0]["body"]["result"]["resign"]["unconfirmed"][0]["date"] == b
        assert env.http[0]["records"][b] == ("unknown", run.run_id, run.job_id)
        assert _records(env.manager, env.site_id) == {
            a: ("already_signed", run.run_id, run.job_id),
            b: ("unknown", run.run_id, run.job_id),
        }
        assert _pending(env.manager, env.site_id) == [b]

    asyncio.run(check())


def test_historical_unknown_requires_readonly_done_without_new_checkpoint(integration):
    async def check():
        env = integration
        a, b = env.dates
        first = await env.execute({"done": [a], "noResult": True})
        assert first.result[0] == "waiting_human"
        assert first.posts == first.clicks == [b]
        assert first.result[2]["resign"]["unconfirmed"][0]["date"] == b
        owner = (first.run_id, first.job_id)
        assert _records(env.manager, env.site_id)[b] == ("unknown", *owner)
        assert _pending(env.manager, env.site_id) == [b]

        blocked = await env.execute({"done": [a]})
        assert blocked.payload["resign_pending_dates"] == blocked.payload["resign_unresolved_dates"] == [b]
        assert blocked.result[0] == "waiting_human"
        assert blocked.posts == blocked.clicks == [] and blocked.opens == 0
        assert blocked.result[2]["resign"]["submit_attempted"] is False
        assert _records(env.manager, env.site_id)[b] == ("unknown", *owner)

        done = await env.execute({"done": [a, b]})
        assert done.result[0] == "success", done.result[1]
        assert done.payload["resign_pending_dates"] == done.payload["resign_unresolved_dates"] == [b]
        assert done.posts == done.clicks == [] and done.opens == 0
        state = done.result[2]["resign"]
        assert state["already_signed"] == [b]
        assert state["success"] == state["unconfirmed"] == state["remaining"] == []
        assert state["submit_attempted"] is False
        assert [(row["body"]["phase"], row["status"]) for row in env.http] == [("prepare", 200)]
        assert _records(env.manager, env.site_id) == {
            a: ("already_signed", *owner), b: ("already_signed", *owner),
        }
        assert _pending(env.manager, env.site_id) == []

    asyncio.run(check())
