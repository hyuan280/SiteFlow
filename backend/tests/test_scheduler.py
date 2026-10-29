from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.models.execution import Run, SiteJob
from app.models.workflow import Site, WorkflowStep
from app.services.execution import ExecutionService
from app.services.scheduler import Scheduler
from tests.helpers import login_client, make_settings
from app.main import create_app


class FakeExecutor:
    def __init__(self) -> None:
        self.submitted: list[int] = []

    def submit(self, run_id: int) -> None:
        self.submitted.append(run_id)


def _create_site(client, name: str = "scheduled-site") -> dict:
    response = client.post(
        "/api/v1/sites",
        json={
            "name": name,
            "base_url": "https://pt.example.com",
            "checkin_after": "00:00",
            "timezone": "UTC",
            "steps": [
                {"step_key": "open", "feature_type": "flow.open"},
                {"step_key": "sign", "feature_type": "sign.button"},
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _scheduler(client, executor: FakeExecutor) -> Scheduler:
    app = client.app
    return Scheduler(
        app.state.session_factory,
        app.state.settings,
        app.state.crypto,
        executor,
    )


def test_scheduler_settings_default_and_env_lock(tmp_path) -> None:
    settings = make_settings(tmp_path)
    client = TestClient(create_app(settings))
    login_client(client)
    view = client.get("/api/v1/scheduler-settings").json()
    assert view["enabled"] is True
    assert view["enabled_source"] == "default"
    assert view["mode"] == "cron"
    assert view["cron"] == "0 10 * * *"
    assert view["source"] == "default"
    assert view["editable"] is True

    locked = make_settings(
        tmp_path,
        scheduler_mode="interval",
        scheduler_interval_seconds=600,
        scheduler_enabled=False,
    )
    locked_client = TestClient(create_app(locked))
    login_client(locked_client)
    view = locked_client.get("/api/v1/scheduler-settings").json()
    assert view["enabled"] is False
    assert view["enabled_source"] == "env"
    assert view["next_run_at"] is None
    assert view["mode"] == "interval"
    assert view["interval_seconds"] == 600
    assert view["source"] == "env"
    assert view["editable"] is False
    response = locked_client.put(
        "/api/v1/scheduler-settings",
        json={"mode": "cron", "cron": "0 10 * * *", "interval_seconds": 600, "ranges": []},
    )
    assert response.status_code == 409


def test_scheduler_enabled_toggle_clears_and_rebuilds_next_run(client) -> None:
    _create_site(client)
    executor = FakeExecutor()
    scheduler = _scheduler(client, executor)
    response = client.put(
        "/api/v1/scheduler-settings",
        json={
            "enabled": True,
            "mode": "interval",
            "cron": "",
            "interval_seconds": 600,
            "ranges": [],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["next_run_at"] is not None

    response = client.put(
        "/api/v1/scheduler-settings",
        json={
            "enabled": False,
            "mode": "interval",
            "cron": "",
            "interval_seconds": 600,
            "ranges": [],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enabled"] is False
    assert body["enabled_source"] == "db"
    assert body["next_run_at"] is None
    assert scheduler.scan_once(datetime.now(timezone.utc) + timedelta(days=1)) == []
    assert executor.submitted == []

    response = client.put(
        "/api/v1/scheduler-settings",
        json={
            "enabled": True,
            "mode": "interval",
            "cron": "",
            "interval_seconds": 600,
            "ranges": [],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is True
    assert response.json()["next_run_at"] is not None


def test_scheduler_cron_triggers_once(client) -> None:
    _create_site(client)
    executor = FakeExecutor()
    scheduler = _scheduler(client, executor)
    response = client.put(
        "/api/v1/scheduler-settings",
        json={"mode": "cron", "cron": "0 10 * * *", "interval_seconds": 3600, "ranges": []},
    )
    assert response.status_code == 200, response.text
    next_run_at = response.json()["next_run_at"]
    assert next_run_at is not None
    next_run = datetime.fromisoformat(next_run_at)

    assert scheduler.scan_once(next_run - timedelta(minutes=1)) == []
    run_ids = scheduler.scan_once(next_run)
    assert len(run_ids) == 1
    assert executor.submitted == run_ids
    assert scheduler.scan_once(next_run + timedelta(minutes=1)) == []

    session = client.app.state.session_factory()
    try:
        run = session.get(Run, run_ids[0])
        assert run is not None
        assert run.mode == "scheduled"
        assert run.trigger == "schedule"
    finally:
        session.close()


def test_scheduler_cron_uses_configured_timezone(tmp_path) -> None:
    settings = make_settings(tmp_path, scheduler_timezone="Asia/Shanghai")
    client = TestClient(create_app(settings))
    login_client(client)
    _create_site(client)
    view = client.get("/api/v1/scheduler-settings").json()
    assert view["timezone"] == "Asia/Shanghai"
    response = client.put(
        "/api/v1/scheduler-settings",
        json={"mode": "cron", "cron": "0 10 * * *", "interval_seconds": 3600, "ranges": []},
    )
    assert response.status_code == 200, response.text
    next_run = datetime.fromisoformat(response.json()["next_run_at"])
    local = next_run.astimezone(ZoneInfo("Asia/Shanghai"))
    assert (local.hour, local.minute) == (10, 0)


def test_scheduler_interval_and_random_ranges(client, settings) -> None:
    _create_site(client)
    executor = FakeExecutor()
    scheduler = _scheduler(client, executor)
    response = client.put(
        "/api/v1/scheduler-settings",
        json={"mode": "interval", "cron": "", "interval_seconds": 600, "ranges": []},
    )
    assert response.status_code == 200, response.text
    next_run_at = response.json()["next_run_at"]
    assert next_run_at is not None
    next_run = datetime.fromisoformat(next_run_at)
    assert scheduler.scan_once(next_run - timedelta(seconds=1)) == []
    interval_runs = scheduler.scan_once(next_run)
    assert len(interval_runs) == 1
    session = client.app.state.session_factory()
    try:
        run = session.get(Run, interval_runs[0])
        for job in session.query(SiteJob).filter_by(run_id=run.id):
            job.status = "success"
        run.status = "success"
        session.commit()
    finally:
        session.close()

    response = client.put(
        "/api/v1/scheduler-settings",
        json={
            "mode": "random",
            "cron": "",
            "interval_seconds": 600,
            "ranges": ["10:00-10:10"],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["next_run_at"] is not None
    current = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)
    tz = ZoneInfo(settings.scheduler_timezone)
    local = current.astimezone(tz)
    target, _ = Scheduler._range_target(local.date(), 0, "10:00-10:10", tz)
    assert scheduler.scan_once(target) != []
    assert scheduler.scan_once(target) == []


def test_scheduler_skips_sites_before_checkin_after(client) -> None:
    response = client.post(
        "/api/v1/sites",
        json={
            "name": "late-site",
            "base_url": "https://pt.example.com",
            "checkin_after": "12:00",
            "timezone": "UTC",
            "steps": [
                {"step_key": "open", "feature_type": "flow.open"},
                {"step_key": "sign", "feature_type": "sign.button"},
            ],
        },
    )
    assert response.status_code == 201, response.text
    executor = FakeExecutor()
    scheduler = _scheduler(client, executor)
    client.put(
        "/api/v1/scheduler-settings",
        json={"mode": "cron", "cron": "0 10 * * *", "interval_seconds": 3600, "ranges": []},
    )
    scheduler.scan_once(datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc))
    assert scheduler.scan_once(datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc)) == []


def test_scheduler_selects_only_today_unsuccessful_valid_idle_sites(tmp_path) -> None:
    settings = make_settings(tmp_path, scheduler_timezone="Asia/Shanghai")
    client = TestClient(create_app(settings))
    login_client(client)
    names = [
        "success-at-start", "success-before-start", "success-at-end", "failed",
        "queued", "running", "never-run", "disabled", "invalid", "success-then-fail",
    ]
    sites = {name: _create_site(client, name) for name in names}
    # Shanghai's Oct 22 starts on Oct 21 UTC; site timezones remain UTC.
    start = datetime(2026, 10, 21, 16, tzinfo=timezone.utc)
    current = start + timedelta(hours=1)
    executor = FakeExecutor()
    scheduler = _scheduler(client, executor)
    with client.app.state.session_factory() as session:
        history = Run(status="fail", trigger="manual", created_by="test")
        session.add(history)
        session.flush()
        for name, status, finished in [
            ("success-at-start", "success", start),
            ("success-before-start", "success", start - timedelta(microseconds=1)),
            ("success-at-end", "success", start + timedelta(days=1)),
            ("failed", "fail", current),
            ("queued", "queued", None),
            ("running", "running", None),
            ("success-then-fail", "success", start),
            ("success-then-fail", "fail", current),
        ]:
            session.add(SiteJob(
                run_id=history.id, site_id=sites[name]["id"], site_name=name,
                lane="general", status=status, finished_at=finished,
            ))
        session.get(Site, sites["disabled"]["id"]).enabled = False
        session.query(WorkflowStep).filter_by(
            owner_type="site", owner_id=sites["invalid"]["id"]
        ).delete()
        session.commit()

        expected = {sites[name]["id"] for name in (
            "success-before-start", "success-at-end", "failed", "never-run",
        )}
        assert set(scheduler._eligible_sites(session, current)) == expected
        excluded = ExecutionService(session, settings, client.app.state.crypto).today_excluded_site_ids(current)
        assert excluded == {sites[name]["id"] for name in (
            "success-at-start", "queued", "running", "success-then-fail",
        )}
        run_ids = scheduler._trigger(session, current)
        assert len(run_ids) == 1
        jobs = session.query(SiteJob).filter_by(run_id=run_ids[0]).all()
        assert {job.site_id for job in jobs} == expected
        assert executor.submitted == run_ids


def test_scheduler_does_not_submit_when_all_sites_succeeded(client) -> None:
    site = _create_site(client)
    executor = FakeExecutor()
    scheduler = _scheduler(client, executor)
    current = datetime(2026, 10, 22, 10, tzinfo=timezone.utc)
    with client.app.state.session_factory() as session:
        run = Run(status="success", trigger="manual", created_by="test")
        session.add(run)
        session.flush()
        session.add(SiteJob(
            run_id=run.id, site_id=site["id"], site_name=site["name"],
            lane="general", status="success", finished_at=current,
        ))
        session.commit()
        assert scheduler._trigger(session, current) == []
        assert session.query(Run).count() == 1
    assert executor.submitted == []
