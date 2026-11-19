from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from app.models.execution import JobStep, Run, SiteJob
from app.models.workflow import Site
from app.schemas.execution import RunCreate
from app.schemas.scheduler import SchedulerSettingsUpdate
from app.services.execution import ExecutionService
from app.services.lanes import LANE_LABELS, LaneService
from app.services.scheduler_settings import SchedulerSettingsService, next_random_run
from app.services.workflows import WorkflowService
from tests.test_scheduler import FakeExecutor, _create_site, _scheduler


def readiness(**overrides):
    return {
        "backend": "cloak-manager", "ready": True, "reason": None, "first_lane": "general",
        "lanes": [{"key": key, "ready": True} for key in LANE_LABELS],
        **overrides,
    }


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk


@pytest.fixture
def worker(client, monkeypatch):
    settings = client.app.state.settings
    settings.browser_token = SecretStr("private-browser-token")
    settings.worker_url = "http://fixed-worker:6300/"
    real_client = httpx.AsyncClient
    requests, options = [], []
    state = SimpleNamespace(payload=readiness(), requests=requests, options=options)

    def handle(request):
        requests.append(request)
        payload = state.payload
        if callable(payload):
            payload = payload(request)
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, httpx.Response):
            return payload
        return httpx.Response(200, stream=Chunks([json.dumps(payload).encode()]))

    def factory(**kwargs):
        options.append(kwargs)
        return real_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr("app.services.lanes.httpx.AsyncClient", factory)
    return state


def configure(client, mode="interval"):
    _create_site(client)
    response = client.put("/api/v1/scheduler-settings", json={
        "mode": mode, "cron": "0 10 * * *", "interval_seconds": 600,
        "ranges": ["10:00-10:10"] if mode == "random" else [],
    })
    assert response.status_code == 200, response.text
    return datetime.fromisoformat(response.json()["next_run_at"])


def assert_deferred(client, executor, expected):
    with client.app.state.session_factory() as session:
        for model in (Run, SiteJob, JobStep):
            assert session.query(model).count() == 0
        assert SchedulerSettingsService(session, client.app.state.settings).view().next_run_at == expected
    assert executor.submitted == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["cron", "interval"])
@pytest.mark.parametrize("reason", ["none", "unknown", "settings_pending", "backend_unavailable"])
async def test_due_schedule_defers_and_recovers_once(client, worker, mode, reason, caplog):
    caplog.set_level(logging.INFO, logger="siteflow.scheduler")
    target = configure(client, mode)
    executor = FakeExecutor()
    scheduler = _scheduler(client, executor)
    worker.payload = readiness(ready=False, reason=reason)
    now = target + timedelta(days=3)
    for _ in range(2):
        assert await scheduler.scan_once_async(now) == []
        assert_deferred(client, executor, target)
    worker.payload = readiness()
    run_ids = await scheduler.scan_once_async(now)
    assert len(run_ids) == 1 and executor.submitted == run_ids
    assert await scheduler.scan_once_async(now) == []
    with client.app.state.session_factory() as session:
        assert session.query(Run).count() == session.query(SiteJob).count() == 1
        run = session.get(Run, run_ids[0])
        assert run.trigger == "schedule" and run.mode == "scheduled"
        assert SchedulerSettingsService(session, client.app.state.settings).view().next_run_at > now
    messages = [record.getMessage() for record in caplog.records if record.name == "siteflow.scheduler"]
    assert messages == [
        "scheduled browser admission deferred reason=browser_or_required_lane_unavailable required_lanes=general",
        "scheduled browser admission readiness check recovered required_lanes=general",
    ]


def test_sync_scan_is_fail_closed_without_network(client, worker):
    target = configure(client)
    executor = FakeExecutor()
    assert _scheduler(client, executor).scan_once(target) == []
    assert_deferred(client, executor, target)
    assert worker.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("recover", [False, True])
async def test_random_window_retry_and_expiry(client, worker, recover, caplog):
    caplog.set_level(logging.INFO, logger="siteflow.scheduler")
    configure(client, "random")
    executor = FakeExecutor()
    scheduler = _scheduler(client, executor)
    day = datetime(2026, 10, 23, tzinfo=scheduler.timezone)
    target, end = scheduler._range_target(day.date(), 0, "10:00-10:10", scheduler.timezone)
    worker.payload = readiness(ready=False)
    for now in (target, target + (end - target) / 2):
        assert await scheduler.scan_once_async(now) == []
        assert_deferred(client, executor, now)
    if recover:
        worker.payload = readiness()
        assert len(await scheduler.scan_once_async(now)) == 1
        assert len(executor.submitted) == 1
    else:
        calls = len(worker.requests)
        now = end + timedelta(seconds=1)
        assert await scheduler.scan_once_async(now) == []
        assert_deferred(client, executor, next_random_run(
            ["10:00-10:10"], now, tz=scheduler.timezone,
        ))
        assert len(worker.requests) == calls
        assert await scheduler.scan_once_async(now + timedelta(seconds=1)) == []
    messages = [record.getMessage() for record in caplog.records if record.name == "siteflow.scheduler"]
    assert len(messages) == (2 if recover else 1)
    assert "admission deferred" in messages[0]
    if recover:
        assert "readiness check recovered" in messages[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["provider", "exception", "unknown_lane"])
async def test_admission_logs_are_static_allowlisted_and_transition_only(
    client, worker, monkeypatch, caplog, failure,
):
    caplog.set_level(logging.INFO, logger="siteflow.scheduler")
    target = configure(client)
    executor = FakeExecutor()
    scheduler = _scheduler(client, executor)
    secret = "private-secret-provider-or-exception-text"
    resolve_site = WorkflowService.resolve_site
    if failure == "provider":
        worker.payload = readiness(ready=False, reason=secret, error=secret)
    elif failure == "exception":
        async def probe(*args):
            raise RuntimeError(secret)

        monkeypatch.setattr(LaneService, "scheduler_preflight", probe)
    else:
        def unknown_lane(self, site_id):
            return resolve_site(self, site_id).model_copy(update={"lane": secret})

        monkeypatch.setattr(WorkflowService, "resolve_site", unknown_lane)
    for _ in range(2):
        assert await scheduler.scan_once_async(target) == []
        assert_deferred(client, executor, target)
    with monkeypatch.context() as recovery:
        async def ready(*args):
            return True

        recovery.setattr(LaneService, "scheduler_preflight", ready)
        recovery.setattr(WorkflowService, "resolve_site", resolve_site)
        assert len(await scheduler.scan_once_async(target)) == 1
        # A further successful check must not emit a second recovery message.
        with client.app.state.session_factory() as session:
            session.query(SiteJob).update({SiteJob.status: "fail"})
            session.query(Run).update({Run.status: "fail"})
            SchedulerSettingsService(session, client.app.state.settings).set_next_run(target)
        assert len(await scheduler.scan_once_async(target)) == 1
    records = [record for record in caplog.records if record.name == "siteflow.scheduler"]
    assert [(record.levelno, record.getMessage()) for record in records] == [
        (logging.WARNING,
         "scheduled browser admission deferred reason=browser_or_required_lane_unavailable required_lanes="
         + ("" if failure == "unknown_lane" else "general")),
        (logging.INFO, "scheduled browser admission readiness check recovered required_lanes=general"),
    ]
    assert all(record.exc_info is None for record in records)
    assert secret not in caplog.text and "private-browser-token" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_all_resolved_lanes_required(client, worker, missing):
    target = configure(client)
    other = _create_site(client, "proxy-site")
    with client.app.state.session_factory() as session:
        session.get(Site, other["id"]).lane_policy = "proxy_interaction"
        session.commit()
    worker.payload = readiness(lanes=[
        {"key": key, "ready": key != "proxy_interaction"}
        for key in LANE_LABELS if not (missing and key == "proxy_interaction")
    ])
    executor = FakeExecutor()
    scheduler = _scheduler(client, executor)
    assert await scheduler.scan_once_async(target) == []
    assert_deferred(client, executor, target)
    worker.payload = readiness()
    ids = await scheduler.scan_once_async(target)
    assert len(ids) == 1
    with client.app.state.session_factory() as session:
        assert {job.lane for job in session.query(SiteJob)} == {"general", "proxy_interaction"}


@pytest.mark.asyncio
async def test_post_startup_denial_blocks_records_and_preserves_due(client, worker):
    target = configure(client)
    executor = FakeExecutor()

    def denied(request):
        assert request.method == "POST" and request.url.path == "/browser/preflight"
        assert json.loads(request.content) == {"lanes": ["general"]}
        assert_deferred(client, executor, target)
        # A read-only scan could pass while actual profile startup is denied.
        return readiness(ready=False, first_lane=None, reason="browser_unavailable",
                         error="private provider startup denial")

    worker.payload = denied
    assert await _scheduler(client, executor).scan_once_async(target) == []
    assert_deferred(client, executor, target)
    assert len(worker.requests) == 1
    assert worker.requests[0].method == "POST"
    assert worker.requests[0].url.path == "/browser/preflight"
    assert json.loads(worker.requests[0].content) == {"lanes": ["general"]}


@pytest.mark.asyncio
async def test_multiple_lanes_one_seat_accepts_first_start_and_canonical_job_order(client, worker):
    target = configure(client)
    with client.app.state.session_factory() as session:
        session.query(Site).one().lane_policy = "proxy_interaction"
        session.commit()
    for key in ("proxy_general", "interaction", "general"):
        site = _create_site(client, key)
        with client.app.state.session_factory() as session:
            session.get(Site, site["id"]).lane_policy = key
            session.commit()
    executor = FakeExecutor()

    def started_first(request):
        assert request.method == "POST" and request.url.path == "/browser/preflight"
        assert json.loads(request.content) == {"lanes": list(LANE_LABELS)}
        assert_deferred(client, executor, target)
        return readiness(license_tier="free", max_profiles=1, active_profiles=1)

    worker.payload = started_first
    ids = await _scheduler(client, executor).scan_once_async(target)
    assert len(ids) == 1 and executor.submitted == ids
    assert len(worker.requests) == 1
    with client.app.state.session_factory() as session:
        assert [job.lane for job in session.query(SiteJob).order_by(SiteJob.id)] == list(LANE_LABELS)


@pytest.mark.asyncio
@pytest.mark.parametrize("remove_first", [False, True])
async def test_subset_proof_requires_same_first_lane(client, worker, remove_first):
    target = configure(client)
    other = _create_site(client, "interaction")
    with client.app.state.session_factory() as session:
        first = session.query(Site).filter(Site.id != other["id"]).one().id
        session.get(Site, other["id"]).lane_policy = "interaction"
        session.commit()
    executor = FakeExecutor()
    scheduler = _scheduler(client, executor)

    def changed(request):
        assert json.loads(request.content) == {"lanes": ["general", "interaction"]}
        assert_deferred(client, executor, target)
        with client.app.state.session_factory() as session:
            session.get(Site, first if remove_first else other["id"]).enabled = False
            session.commit()
        return readiness()

    worker.payload = changed
    ids = await scheduler.scan_once_async(target)
    assert len(worker.requests) == 1
    if remove_first:
        assert ids == []
        assert_deferred(client, executor, target)
        # Only the next poll may start the newly first lane; no inline retry.
        worker.payload = readiness(first_lane="interaction")
        ids = await scheduler.scan_once_async(target)
        assert json.loads(worker.requests[-1].content) == {"lanes": ["interaction"]}
    assert len(ids) == 1 and executor.submitted == ids
    with client.app.state.session_factory() as session:
        assert [job.lane for job in session.query(SiteJob)] == [
            "interaction" if remove_first else "general",
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["lane", "disabled", "schedule", "stale"])
async def test_second_scan_rechecks_configuration_and_proof(client, monkeypatch, change):
    target = configure(client)
    executor = FakeExecutor()
    scheduler = _scheduler(client, executor)
    ticks = iter([0, 11])
    if change == "stale":
        monkeypatch.setattr("app.services.scheduler.monotonic", lambda: next(ticks))

    async def probe(self, required):
        assert required == {"general"}
        with client.app.state.session_factory() as session:
            assert session.query(Run).count() == session.query(SiteJob).count() == 0
            if change == "lane":
                session.query(Site).one().lane_policy = "interaction"
            elif change == "disabled":
                session.query(Site).one().enabled = False
            elif change == "schedule":
                SchedulerSettingsService(session, self.settings).update(
                    SchedulerSettingsUpdate(enabled=False))
            session.commit()
        await asyncio.sleep(0)
        return True

    monkeypatch.setattr(LaneService, "scheduler_preflight", probe)
    assert await scheduler.scan_once_async(target) == []
    if change in {"lane", "stale"}:
        assert_deferred(client, executor, target)
    else:
        with client.app.state.session_factory() as session:
            assert session.query(Run).count() == session.query(SiteJob).count() == 0
        assert executor.submitted == []


@pytest.mark.asyncio
@pytest.mark.parametrize("age,accepted", [(10, True), (10.001, False)])
async def test_proof_freshness_boundary(client, monkeypatch, age, accepted):
    target = configure(client)
    executor = FakeExecutor()
    ticks = iter([120, 120 + age])

    async def ready(*args):
        return True

    monkeypatch.setattr(LaneService, "scheduler_preflight", ready)
    monkeypatch.setattr("app.services.scheduler.monotonic", lambda: next(ticks))
    ids = await _scheduler(client, executor).scan_once_async(target)
    if accepted:
        assert len(ids) == 1 and executor.submitted == ids
    else:
        assert ids == []
        assert_deferred(client, executor, target)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("private preflight failure"), TimeoutError(),
                                     asyncio.CancelledError()])
async def test_preflight_exception_does_not_advance(client, monkeypatch, failure, caplog):
    target = configure(client)
    executor = FakeExecutor()

    async def probe(*args):
        raise failure

    monkeypatch.setattr(LaneService, "scheduler_preflight", probe)
    if isinstance(failure, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await _scheduler(client, executor).scan_once_async(target)
    else:
        assert await _scheduler(client, executor).scan_once_async(target) == []
    assert_deferred(client, executor, target)
    assert "private preflight failure" not in caplog.text


@pytest.mark.asyncio
async def test_scheduler_outer_deadline_defers_without_retry(client, monkeypatch):
    target = configure(client)
    executor = FakeExecutor()
    real_timeout = asyncio.timeout
    budgets, calls = [], []

    def short_timeout(seconds):
        budgets.append(seconds)
        return real_timeout(0.01)

    async def stalled(self, lanes):
        calls.append(lanes)
        await asyncio.Event().wait()

    monkeypatch.setattr("app.services.scheduler.asyncio.timeout", short_timeout)
    monkeypatch.setattr(LaneService, "scheduler_preflight", stalled)
    assert await _scheduler(client, executor).scan_once_async(target) == []
    assert budgets == [135] and calls == [{"general"}]
    assert_deferred(client, executor, target)


@pytest.mark.asyncio
async def test_proof_is_fresh_after_long_successful_startup(client, monkeypatch):
    target = configure(client)
    executor = FakeExecutor()
    clock = SimpleNamespace(tick=0)

    async def startup(*args):
        clock.tick += 120
        assert_deferred(client, executor, target)
        return True

    monkeypatch.setattr("app.services.scheduler.monotonic", lambda: clock.tick)
    monkeypatch.setattr(LaneService, "scheduler_preflight", startup)
    assert len(await _scheduler(client, executor).scan_once_async(target)) == 1


@pytest.mark.asyncio
async def test_random_window_expires_during_preflight(client, monkeypatch):
    configure(client, "random")
    executor = FakeExecutor()
    scheduler = _scheduler(client, executor)
    day = datetime(2026, 10, 23, tzinfo=scheduler.timezone)
    target, end = scheduler._range_target(day.date(), 0, "10:00-10:10", scheduler.timezone)
    clock = SimpleNamespace(current=max(target, end - timedelta(seconds=60)))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.current.astimezone(tz)

    async def probe(*args):
        clock.current += timedelta(seconds=120)
        return True

    monkeypatch.setattr("app.services.scheduler.datetime", Clock)
    monkeypatch.setattr(LaneService, "scheduler_preflight", probe)
    assert await scheduler.scan_once_async() == []
    assert_deferred(client, executor, next_random_run(
        ["10:00-10:10"], clock.current, tz=scheduler.timezone,
    ))


@pytest.mark.asyncio
@pytest.mark.parametrize("method,budget", [("scheduler_readiness", 10), ("scheduler_preflight", 130)])
async def test_worker_probe_total_deadline_and_cancellation(client, worker, monkeypatch, method, budget):
    real_timeout = asyncio.timeout
    entered = asyncio.Event()

    async def stalled(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    # Stall the raw iterator, rather than the HTTP transport's own timeout machinery.
    async def stream(self):
        await stalled()
        yield b""

    monkeypatch.setattr(Chunks, "__aiter__", stream)
    budgets = []

    def short_timeout(seconds):
        budgets.append(seconds)
        return real_timeout(0.01)

    monkeypatch.setattr("app.services.lanes.asyncio.timeout", short_timeout)
    service = LaneService(client.app.state.settings)
    probe = getattr(service, method)
    assert await probe({"general"}) is False
    assert budgets == [budget]
    monkeypatch.setattr("app.services.lanes.asyncio.timeout", real_timeout)
    entered.clear()
    task = asyncio.create_task(probe({"general"}))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_production_loop_awaits_scan_and_stop_cancels_preflight(client, monkeypatch):
    target = configure(client)
    executor = FakeExecutor()
    scheduler = _scheduler(client, executor)
    entered = asyncio.Event()

    async def sleep(_seconds):
        return

    async def scan():
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("app.services.scheduler.asyncio.sleep", sleep)
    monkeypatch.setattr(scheduler, "scan_once_async", scan)
    scheduler._task = asyncio.create_task(scheduler._loop())
    await asyncio.wait_for(entered.wait(), 1)
    task = scheduler._task
    await scheduler.stop()
    assert task.cancelled() and scheduler._task is None
    assert_deferred(client, executor, target)


@pytest.mark.asyncio
async def test_probe_yields_and_submit_has_running_event_loop(client, monkeypatch):
    target = configure(client)
    executor = FakeExecutor()
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), asyncio.Event()

    async def probe(*args):
        entered.set()
        await release.wait()
        return True

    def submit(run_id):
        assert asyncio.get_running_loop() is loop
        executor.submitted.append(run_id)

    monkeypatch.setattr(LaneService, "scheduler_preflight", probe)
    monkeypatch.setattr(executor, "submit", submit)
    task = asyncio.create_task(_scheduler(client, executor).scan_once_async(target))
    await asyncio.wait_for(entered.wait(), 1)
    assert_deferred(client, executor, target)
    release.set()
    assert await task == executor.submitted
    assert len(executor.submitted) == 1


@pytest.mark.parametrize("trigger", ["manual", "assistant"])
def test_manual_and_assistant_creation_unchanged(client, worker, trigger):
    configure(client)
    worker.payload = readiness(ready=False)
    with client.app.state.session_factory() as session:
        service = ExecutionService(session, client.app.state.settings, client.app.state.crypto)
        run = service.create_run(RunCreate(), "test", trigger=trigger)
        assert len(run.jobs) == 1 and run.trigger == trigger
    assert worker.requests == []


@pytest.mark.asyncio
async def test_readiness_uses_only_bounded_authenticated_get(client, worker):
    assert await LaneService(client.app.state.settings).scheduler_readiness({"general"}) is True
    assert worker.options == [{"timeout": 10.0, "follow_redirects": False, "trust_env": False}]
    assert len(worker.requests) == 1
    request = worker.requests[0]
    assert request.method == "GET"
    assert str(request.url) == "http://fixed-worker:6300/browser/readiness"
    assert request.headers["Authorization"] == "Bearer private-browser-token"
    assert request.headers["Accept-Encoding"] == "identity"
    assert "origin" not in request.headers and "cookie" not in request.headers
    assert not request.content


@pytest.mark.asyncio
@pytest.mark.parametrize("required", [{"general"}, {"proxy_interaction", "interaction"}, set(LANE_LABELS)])
async def test_preflight_uses_only_bounded_authenticated_canonical_post(client, worker, required):
    ordered = [key for key in LANE_LABELS if key in required]
    worker.payload = readiness(first_lane=ordered[0])
    assert await LaneService(client.app.state.settings).scheduler_preflight(required) is True
    assert worker.options == [{"timeout": 130.0, "follow_redirects": False, "trust_env": False}]
    assert len(worker.requests) == 1
    request = worker.requests[0]
    assert request.method == "POST"
    assert str(request.url) == "http://fixed-worker:6300/browser/preflight"
    assert request.headers["Authorization"] == "Bearer private-browser-token"
    assert request.headers["Accept-Encoding"] == "identity"
    assert "origin" not in request.headers and "cookie" not in request.headers
    assert json.loads(request.content) == {"lanes": ordered}


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [None, "interaction", "unknown", True, 1, [], {}])
async def test_preflight_rejects_wrong_or_missing_first_lane(client, worker, first):
    worker.payload = readiness(first_lane=first)
    assert await LaneService(client.app.state.settings).scheduler_preflight({"general"}) is False
    worker.payload.pop("first_lane")
    assert await LaneService(client.app.state.settings).scheduler_preflight({"general"}) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["scheduler_readiness", "scheduler_preflight"])
@pytest.mark.parametrize("payload", [
    None, [], {}, readiness(backend="unsupported"), readiness(backend="unknown"),
    readiness(ready="true"), readiness(ready=1), readiness(ready=False),
    readiness(lanes=[]), readiness(lanes=None), readiness(lanes=[None]),
    readiness(lanes=[{"key": "general", "ready": "true"}]),
    readiness(lanes=[{"key": "general", "ready": 1}]),
    readiness(lanes=[{"key": "unknown", "ready": True}]),
    readiness(lanes=[{"key": [], "ready": True}]),
    readiness(lanes=[{"key": "general", "ready": True}] * 2),
    readiness(lanes=[{"key": "interaction", "ready": True}]),
    httpx.ConnectError("private worker failure"), httpx.ReadTimeout("private timeout"),
])
async def test_untrusted_readiness_fails_closed(client, worker, payload, caplog, method):
    worker.payload = payload
    assert await getattr(LaneService(client.app.state.settings), method)({"general"}) is False
    assert "private" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["scheduler_readiness", "scheduler_preflight"])
@pytest.mark.parametrize("status", [201, 307, 401, 404, 500, 503])
async def test_http_errors_do_not_read_bodies_or_follow_redirects(client, worker, status, method):
    stream = Chunks([b"private error body"])
    worker.payload = httpx.Response(status, stream=stream, headers={"location": "http://attacker/"})
    assert await getattr(LaneService(client.app.state.settings), method)({"general"}) is False
    assert stream.reads == 0 and len(worker.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["scheduler_readiness", "scheduler_preflight"])
@pytest.mark.parametrize("kind", ["oversize", "compressed", "invalid_json"])
async def test_response_bounds(client, worker, kind, method):
    headers = {"content-encoding": "gzip"} if kind == "compressed" else {}
    stream = Chunks([b"x" * 65537] if kind == "oversize" else [b"private-invalid-json"])
    worker.payload = httpx.Response(200, headers=headers, stream=stream)
    assert await getattr(LaneService(client.app.state.settings), method)({"general"}) is False
    if kind == "compressed":
        assert stream.reads == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["scheduler_readiness", "scheduler_preflight"])
@pytest.mark.parametrize("token,required", [(None, {"general"}), ("", {"general"}),
                                           ("token", {"unknown"}), ("token", set())])
async def test_missing_token_or_unknown_requirements_never_contact_worker(
    client, worker, token, required, method,
):
    client.app.state.settings.browser_token = SecretStr(token) if token is not None else None
    assert await getattr(LaneService(client.app.state.settings), method)(required) is False
    assert worker.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["scheduler_readiness", "scheduler_preflight"])
async def test_unrelated_unavailable_lanes_and_provider_text_are_not_admission_evidence(client, worker, method):
    worker.payload = readiness(
        reason="private arbitrary provider text", license_tier="not-trusted-here",
        lanes=[{"key": key, "ready": key == "general"} for key in LANE_LABELS],
    )
    assert await getattr(LaneService(client.app.state.settings), method)({"general"}) is True
