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
from tests.test_worker_resign_loop import BASE, CHALLENGE, calendar, install_checkpoint_transport
from worker.resign_checkpoint import CHECKPOINT_ERROR, make_resign_checkpoint
from worker.resign_loop import run_resign_loop


CHECKPOINT_PATH = "/api/v1/internal/resign/checkpoint"


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path, scheduler_timezone="America/Los_Angeles")


def _records(manager, site_id):
    # A fresh session verifies commits, not just in-session flushes.
    with manager.session_factory() as session:
        return {
            row.resign_date: (row.status, row.run_id, row.job_id)
            for row in session.query(SiteResignRecord).filter_by(site_id=site_id).all()
        }


def _pending(manager, site_id):
    with manager.session_factory() as session:
        return ResignService(session, manager.settings).pending_dates(site_id, 30)


@pytest.fixture
def integration(anon_client, monkeypatch, fake_dispatch):
    manager = anon_client.app.state.executor
    monkeypatch.setattr(manager, "schedule_run_notification", lambda _run_id: None)
    monkeypatch.setenv("SITEFLOW_RESIGN_CHECKPOINT_URL", BASE + CHECKPOINT_PATH)
    assert not anon_client.cookies and "X-CSRF-Token" not in anon_client.headers

    with manager.session_factory() as session:
        service = ResignService(session, manager.settings)
        today = datetime.now(service.timezone).date()
        dates = [(today - timedelta(days=n)).isoformat() for n in (2, 1)]
        site = Site(name="isolated-resign-loop", base_url=BASE, lane_policy="general",
                    support_resign=True, resign_max_days=30)
        history = Run(mode="manual", trigger="manual", status="fail", created_by="test")
        session.add_all([site, history])
        session.flush()
        site_id = site.id
        session.add(WorkflowStep(
            owner_type="site", owner_id=site_id, step_key="resign",
            feature_type="post.resign_loop", category="post", enabled=True,
            config_json=json.dumps({"wait_seconds": 1}),
        ))
        session.add(SiteJob(
            run_id=history.id, site_id=site_id, site_name=site.name, lane="general",
            status="fail", finished_at=datetime.combine(
                today - timedelta(days=2), time(12), tzinfo=service.timezone,
            ).astimezone(timezone.utc),
        ))
        session.commit()
    assert _pending(manager, site_id) == dates
    env = SimpleNamespace(manager=manager, site_id=site_id, today=today.isoformat(),
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
        env.http.append({"body": body, "status": response.status_code,
                         "records": _records(manager, site_id)})
        if response.status_code == 200:
            assert response.json() == {"ok": True}
            assert response.headers["cache-control"] == "no-store"
        if env.lose_prepare_ack and body["phase"] == "prepare" and response.status_code == 200:
            env.lose_prepare_ack = False
            raise httpx.ReadTimeout("isolated ACK loss after real commit", request=request)
        return httpx.Response(response.status_code, content=response.content, headers=response.headers)

    install_checkpoint_transport(monkeypatch, transport)

    async def execute(options=None, *, cycles=1, between_calls=None):
        with manager.session_factory() as session:
            run = ExecutionService(session, manager.settings, manager.crypto).create_run(
                RunCreate(site_ids=[site_id]), "test",
            )
            job = session.query(SiteJob).filter_by(run_id=run.id).one()
            observed = SimpleNamespace(run_id=run.id, job_id=job.id, progress=[], result=None,
                                       calls=[], clicks=[], runtime={})

        async with calendar({"month": today.strftime("%Y-%m"), "persistent": True,
                             **(options or {})}) as (page, posts):
            async def worker(payload):
                observed.payload = copy.deepcopy(payload)
                step, = payload["steps"]

                async def progress(data):
                    observed.progress.append({
                        "data": copy.deepcopy(data), "records": _records(manager, site_id),
                        "posts": list(posts), "clicks": await page.evaluate("clicks"),
                        "http_count": len(env.http), "call_index": len(observed.calls),
                    })
                    manager._persist_progress(payload["job_id"], {
                        "status": "running", "steps": [{
                            "step_key": step["step_key"], "feature_type": step["feature_type"],
                            "status": "running", "data": data,
                        }],
                    })

                for index in range(cycles):
                    status, message, data = await run_resign_loop(
                        page, step["config"], payload["resign_pending_dates"], lambda *_: None,
                        today=payload["resign_today"], base_url=payload["base_url"],
                        progress=progress, runtime=observed.runtime,
                        durable_progress=make_resign_checkpoint(payload, step["step_key"]),
                    )
                    observed.result = (status, message, data)
                    clicks = await page.evaluate("clicks")
                    observed.clicks.extend(clicks)
                    observed.calls.append({
                        "result": copy.deepcopy(observed.result), "posts": list(posts),
                        "clicks": clicks, "records": _records(manager, site_id),
                        "http_count": len(env.http),
                    })
                    if status != "success" or not data["resign"]["remaining"]:
                        break
                    if between_calls is not None and index + 1 < cycles:
                        await between_calls(page, observed)
                return {"status": status, "message": message, "steps": [{
                    "step_key": step["step_key"], "feature_type": step["feature_type"],
                    "status": status, "message": message, "data": data,
                }]}

            monkeypatch.setattr(manager, "_post_execute", worker)
            await manager._execute_run(observed.run_id)
            observed.posts = list(posts)

        with manager.session_factory() as session:
            job = session.get(SiteJob, observed.job_id)
            assert observed.result is not None, job.message
            observed.saved_summary = json.loads(session.get(Run, observed.run_id).summary_json)
            saved = session.query(JobStep).filter_by(site_job_id=job.id).one()
            observed.saved_data = json.loads(saved.data_json)
            assert job.status == session.get(Run, observed.run_id).status == observed.result[0]
            assert saved.status == observed.result[0]
            assert observed.saved_data == observed.result[2]
        assert observed.payload["resign_today"] == env.today
        return observed

    env.execute = execute
    yield env
    manager.secret_providers.clear()
    anon_client.close()
    anon_client.app.state.engine.dispose()


@pytest.mark.parametrize("challenge", [False, True], ids=["server-done", "downstream-challenge"])
def test_loop_commits_prepare_before_click_and_confirms_before_next_date(integration, challenge):
    async def check():
        env = integration
        a, b = env.dates
        handoffs = []

        async def downstream(page, observed):
            state = observed.result[2]["resign"]
            current = state["current_date"]
            handoffs.append(current)
            assert [row["date"] for row in state["unconfirmed"]] == [current]
            assert current in state["remaining"]
            assert _records(env.manager, env.site_id)[current] == (
                "unknown", observed.run_id, observed.job_id,
            )
            if challenge:
                await page.frame_locator("iframe").get_by_text("verification loaded").wait_for(timeout=3000)
                await page.evaluate("""date => {
                    const dates = JSON.parse(sessionStorage.getItem('fixture-signed') || '[]');
                    sessionStorage.setItem('fixture-signed', JSON.stringify([...dates, date]));
                }""", current)

        run = await env.execute(
            {"noResult": challenge, **({"challengeFrame": CHALLENGE} if challenge else {})},
            cycles=3, between_calls=downstream,
        )
        assert run.result[0] == "success", run.result[1]
        config = run.payload["steps"][0]["config"]
        assert config["max_retries"] == 3 and config["wait_seconds"] == 1
        assert "execution_mode" not in config
        assert handoffs == run.posts == run.clicks == [a, b]
        states = [call["result"][2]["resign"] for call in run.calls]
        assert [state["current_date"] for state in states] == [a, b, None]
        assert [state["remaining"] for state in states] == [[a, b], [b], []]
        assert [[row["date"] for row in state["success"]] for state in states] == [[], [a], [a, b]]
        assert [call["http_count"] for call in run.calls] == [1, 3, 4]
        assert [(row["body"]["phase"], row["body"]["date"], row["status"])
                for row in env.http] == [
            ("prepare", a, 200), ("confirm", a, 200),
            ("prepare", b, 200), ("confirm", b, 200),
        ]
        owner = (run.run_id, run.job_id)
        for index, stamp in enumerate([a, b]):
            prepared, = [row for row in run.progress
                         if row["call_index"] == index and row["http_count"] == 2 * index + 1
                         and [item["date"] for item in row["data"]["resign"]["unconfirmed"]] == [stamp]
                         and row["clicks"] == []]
            assert prepared["records"][stamp] == ("unknown", *owner)
            assert prepared["posts"] == [a, b][:index]
        assert run.runtime == run.saved_data["resign"] == states[-1]
        assert run.runtime["attempts"] == {a: 1, b: 1}
        assert not {"execution_mode", "cards_before", "cards_after"} & run.runtime.keys()
        assert env.http[-1]["body"]["result"] == run.result[2]
        assert _records(env.manager, env.site_id) == {a: ("success", *owner), b: ("success", *owner)}
        assert _pending(env.manager, env.site_id) == []

    asyncio.run(check())


def test_default_loop_repeated_prepare_until_retry_budget_exhausted(integration):
    async def check():
        env = integration
        a, b = env.dates
        run = await env.execute({"noResult": True}, cycles=6)
        assert run.result[0] == "waiting_human" and "已用尽 3 次重试" in run.result[1]
        assert run.posts == run.clicks == [a] * 4
        assert [call["http_count"] for call in run.calls] == [1, 2, 3, 4, 4]
        assert [(row["body"]["phase"], row["body"]["date"], row["status"])
                for row in env.http] == [("prepare", a, 200)] * 4
        state = run.result[2]["resign"]
        assert state == run.runtime == run.saved_data["resign"]
        assert state["attempts"] == {a: 4} and state["remaining"] == [a, b]
        assert [row["date"] for row in state["unconfirmed"]] == [a]
        assert state["success"] == state["failed"] == []
        assert _records(env.manager, env.site_id) == {a: ("unknown", run.run_id, run.job_id)}

    asyncio.run(check())


@pytest.mark.parametrize("lose_ack", [False, True], ids=["unknown-submit", "prepare-ack-lost"])
def test_unknown_can_be_prepared_again_by_default_loop(integration, lose_ack):
    async def check():
        env = integration
        a, b = env.dates
        env.lose_prepare_ack = lose_ack
        first = await env.execute({"signed": [a], "noResult": True})
        assert first.result[0] == ("waiting_human" if lose_ack else "success")
        assert first.posts == ([] if lose_ack else [b])
        assert _records(env.manager, env.site_id)[b] == ("unknown", first.run_id, first.job_id)
        if lose_ack:
            assert CHECKPOINT_ERROR in first.result[1]
            assert not first.result[2]["resign"]["submit_attempted"]
            assert all(not row["data"]["resign"]["unconfirmed"] for row in first.progress)
        second = await env.execute({"signed": [a]}, cycles=2)
        assert second.result[0] == "success", second.result[1]
        assert second.payload["resign_pending_dates"] == [b]
        assert second.payload["resign_unresolved_dates"] == [b]
        assert second.posts == second.clicks == [b]
        assert [(row["body"]["phase"], row["status"]) for row in env.http] == [
            ("prepare", 200), ("prepare", 200), ("confirm", 200),
        ]
        assert _records(env.manager, env.site_id)[b] == ("success", second.run_id, second.job_id)
        assert _pending(env.manager, env.site_id) == []

    asyncio.run(check())


def test_unknown_done_is_resolved_readonly_in_new_job(integration):
    async def check():
        env = integration
        a, b = env.dates
        first = await env.execute({"signed": [a], "noResult": True})
        assert first.result[0] == "success" and first.posts == [b]
        second = await env.execute({"signed": [a, b]})
        assert second.result[0] == "success", second.result[1]
        assert second.posts == second.clicks == []
        assert second.result[2]["resign"]["already_signed"] == [b]
        assert len(env.http) == 1
        assert _records(env.manager, env.site_id)[b] == ("already_signed", first.run_id, first.job_id)
        assert _pending(env.manager, env.site_id) == []

    asyncio.run(check())
