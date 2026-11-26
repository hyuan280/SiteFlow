from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.models.execution import JobStep, Run, SiteJob
from app.models.workflow import SiteResignRecord


def _site(
    client: TestClient, *, name: str = "analytics-site", enabled: bool = True
) -> dict:
    response = client.post(
        "/api/v1/sites",
        json={
            "name": name,
            "base_url": "https://pt.example.com",
            "enabled": enabled,
            "steps": [{"step_key": "open", "feature_type": "flow.open"}],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _job(
    client: TestClient,
    site: dict,
    *,
    status: str,
    message: str,
    step_status: str,
    feature_type: str = "sign.button",
) -> int:
    now = datetime.now(timezone.utc)
    session = client.app.state.session_factory()
    try:
        run = Run(
            mode="manual",
            trigger="manual",
            status=status,
            requested_sites_json=f"[{site['id']}]",
            force=False,
            cancel_requested=False,
            summary_json="{}",
            created_by="admin",
            created_at=now,
            started_at=now,
            finished_at=now,
        )
        session.add(run)
        session.flush()
        job = SiteJob(
            run_id=run.id,
            site_id=site["id"],
            site_name=site["name"],
            lane="general",
            attempt=1,
            status=status,
            message=message,
            result_json="{}",
            started_at=now,
            finished_at=now,
        )
        session.add(job)
        session.flush()
        session.add(
            JobStep(
                site_job_id=job.id,
                step_key="sign",
                feature_type=feature_type,
                label="签到",
                status=step_status,
                message=message,
                data_json="{}",
                started_at=now,
                finished_at=now,
            )
        )
        session.commit()
        return job.id
    finally:
        session.close()


def test_daily_matrix_counts_enabled_sites_once(client: TestClient) -> None:
    successful = _site(client, name="successful", enabled=True)
    failed = _site(client, name="failed", enabled=True)
    notrun = _site(client, name="notrun", enabled=True)
    disabled = _site(client, name="disabled", enabled=False)
    disabled_notrun = _site(client, name="disabled-notrun", enabled=False)
    for status in ("success", "fail", "success", "fail"):
        _job(client, successful, status=status, message=status, step_status=status)
    _job(client, failed, status="fail", message="failed", step_status="fail")
    _job(client, disabled, status="success", message="disabled", step_status="success")

    response = client.get("/api/v1/analytics/matrix?days=1&include_disabled=false")
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["dates"]) == 1
    today = body["dates"][0]
    assert body["start_date"] == body["end_date"] == today
    assert len(body["sites"]) == 3
    rows = {row["site_id"]: row for row in body["sites"]}
    assert set(rows) == {successful["id"], failed["id"], notrun["id"]}
    assert all(row["enabled"] is True for row in rows.values())
    assert rows[notrun["id"]]["cells"] == {}
    assert set(rows[successful["id"]]["cells"]) == {today}
    cell = rows[successful["id"]]["cells"][today]
    assert cell["status"] == "success"
    assert cell["attempts"] == 4
    assert cell["counts"] == {"success": 2, "fail": 2}
    assert set(rows[failed["id"]]["cells"]) == {today}
    assert rows[failed["id"]]["cells"][today]["attempts"] == 1
    # Dashboard counts each row once; an absent daily cell means notrun.
    assert {
        site_id: row["cells"].get(today, {}).get("status", "notrun")
        for site_id, row in rows.items()
    } == {successful["id"]: "success", failed["id"]: "fail", notrun["id"]: "notrun"}

    response = client.get("/api/v1/analytics/matrix?days=1&include_disabled=true")
    assert response.status_code == 200, response.text
    inclusive = response.json()
    assert inclusive["dates"] == [today]
    assert len(inclusive["sites"]) == 5
    all_rows = {row["site_id"]: row for row in inclusive["sites"]}
    assert set(all_rows) == {*rows, disabled["id"], disabled_notrun["id"]}
    for site_id, row in rows.items():
        assert all_rows[site_id] == row
    assert all_rows[disabled["id"]]["enabled"] is False
    assert set(all_rows[disabled["id"]]["cells"]) == {today}
    disabled_cell = all_rows[disabled["id"]]["cells"][today]
    assert disabled_cell["status"] == "success"
    assert disabled_cell["attempts"] == 1
    assert disabled_cell["counts"] == {"success": 1}
    assert all_rows[disabled_notrun["id"]]["enabled"] is False
    assert all_rows[disabled_notrun["id"]]["cells"] == {}


def test_daily_matrix_reflects_site_disable_and_reenable(client: TestClient) -> None:
    site = _site(client, name="toggle-enabled")
    notrun = _site(client, name="always-enabled")
    _job(client, site, status="success", message="success", step_status="success")
    response = client.get("/api/v1/analytics/matrix?days=1&include_disabled=false")
    assert response.status_code == 200, response.text
    initial = response.json()
    assert len(initial["dates"]) == 1
    today = initial["dates"][0]
    assert len(initial["sites"]) == 2
    initial_rows = {row["site_id"]: row for row in initial["sites"]}
    assert set(initial_rows) == {site["id"], notrun["id"]}
    original_cells = initial_rows[site["id"]]["cells"]
    assert set(original_cells) == {today}
    assert original_cells[today]["status"] == "success"

    for enabled in (False, True):
        response = client.put(
            f"/api/v1/sites/{site['id']}",
            json={
                "name": site["name"],
                "base_url": site["base_url"],
                "enabled": enabled,
                "steps": [{"step_key": "open", "feature_type": "flow.open"}],
                "expected_revision": site["config_revision"],
                "expected_history_key": site["history_key"],
            },
        )
        assert response.status_code == 200, response.text
        site = response.json()
        assert site["enabled"] is enabled

        response = client.get("/api/v1/analytics/matrix?days=1&include_disabled=false")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["dates"] == [today]
        expected_ids = {site["id"], notrun["id"]} if enabled else {notrun["id"]}
        assert len(body["sites"]) == len(expected_ids)
        rows = {row["site_id"]: row for row in body["sites"]}
        assert set(rows) == expected_ids
        assert all(row["enabled"] is True for row in rows.values())
        assert rows[notrun["id"]]["cells"] == {}
        if enabled:
            assert rows[site["id"]]["cells"] == original_cells

        response = client.get("/api/v1/analytics/matrix?days=1&include_disabled=true")
        assert response.status_code == 200, response.text
        inclusive = response.json()
        assert inclusive["dates"] == [today]
        assert len(inclusive["sites"]) == 2
        all_rows = {row["site_id"]: row for row in inclusive["sites"]}
        assert set(all_rows) == {site["id"], notrun["id"]}
        assert all_rows[site["id"]]["enabled"] is enabled
        assert all_rows[site["id"]]["cells"] == original_cells
        assert all_rows[notrun["id"]] == rows[notrun["id"]]


def test_matrix_includes_successful_resign_dates(client: TestClient) -> None:
    site = _site(client)
    matrix = client.get("/api/v1/analytics/matrix?days=7").json()
    today = matrix["dates"][-1]
    session = client.app.state.session_factory()
    try:
        session.add(
            SiteResignRecord(
                site_id=site["id"],
                resign_date=today,
                status="success",
                message="补签成功",
                run_id=None,
                job_id=None,
            )
        )
        session.commit()
    finally:
        session.close()

    body = client.get("/api/v1/analytics/matrix?days=7").json()
    row = next(item for item in body["sites"] if item["site_id"] == site["id"])
    assert row["cells"][today]["status"] == "success"
    assert row["cells"][today]["counts"]["resign"] == 1
    trend = client.get("/api/v1/analytics/trends?days=7").json()
    assert trend["total"] == 1
    assert trend["success"] == 1


def test_failures_show_daily_latest_unsuccessful_only(client: TestClient) -> None:
    site = _site(client)
    _job(client, site, status="fail", message="earlier failure", step_status="fail")
    _job(client, site, status="success", message="latest success", step_status="success")

    body = client.get("/api/v1/analytics/failures?days=7").json()
    assert body["total"] == 0

    latest_failure = _job(
        client,
        site,
        status="waiting_human",
        message="latest waiting",
        step_status="waiting_human",
    )
    body = client.get("/api/v1/analytics/failures?days=7").json()
    assert body["total"] == 1
    assert body["items"][0]["job_id"] == latest_failure
    assert body["items"][0]["status"] == "waiting_human"

    filtered = client.get(
        "/api/v1/analytics/failures?days=7&status=fail,error"
    ).json()
    assert filtered["total"] == 0


def test_analytics_requires_auth(anon_client: TestClient) -> None:
    assert anon_client.get("/api/v1/analytics/matrix").status_code == 401
    assert anon_client.get("/api/v1/analytics/trends").status_code == 401
    assert anon_client.get("/api/v1/analytics/failures").status_code == 401


def test_matrix_trends_and_failures(client: TestClient) -> None:
    site = _site(client)
    _job(client, site, status="success", message="签到成功", step_status="success")
    failed_job = _job(
        client,
        site,
        status="fail",
        message="步骤超时",
        step_status="fail",
    )

    matrix = client.get("/api/v1/analytics/matrix?days=7")
    assert matrix.status_code == 200, matrix.text
    body = matrix.json()
    today = body["dates"][-1]
    row = next(item for item in body["sites"] if item["site_id"] == site["id"])
    cell = row["cells"][today]
    assert cell["attempts"] == 2
    assert cell["status"] == "success"
    assert cell["counts"] == {"success": 1, "fail": 1}

    trends = client.get("/api/v1/analytics/trends?days=7")
    assert trends.status_code == 200, trends.text
    trend = trends.json()
    assert trend["total"] == 2
    assert trend["success"] == 1
    assert trend["fail"] == 1
    assert trend["success_rate"] == 50.0
    assert trend["points"][-1]["total"] == 2

    failures = client.get("/api/v1/analytics/failures?days=7")
    assert failures.status_code == 200, failures.text
    failure_body = failures.json()
    assert failure_body["total"] == 1
    assert failure_body["items"][0]["job_id"] == failed_job
    assert failure_body["items"][0]["failed_steps"][0]["feature_type"] == "sign.button"

    filtered = client.get(
        f"/api/v1/analytics/failures?days=7&site_id={site['id']}&feature_type=sign"
    )
    assert filtered.json()["total"] == 1
    empty = client.get(
        f"/api/v1/analytics/failures?days=7&site_id={site['id']}&feature_type=challenge"
    )
    assert empty.json()["total"] == 0
