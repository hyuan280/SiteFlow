from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.models.execution import Run, SiteJob
from app.features.catalog import PostBuyCardConfig
from app.services.resign import ResignService


def _site(client: TestClient) -> dict:
    response = client.post(
        "/api/v1/sites",
        json={
            "name": "resign-site",
            "base_url": "https://pt.example.com",
            "steps": [{"step_key": "open", "feature_type": "flow.open"}],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _job(client: TestClient, site: dict, *, status: str, days_ago: int) -> None:
    now = datetime.now(timezone.utc)
    finished = now - timedelta(days=days_ago)
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
            created_at=finished,
            started_at=finished,
            finished_at=finished,
        )
        session.add(run)
        session.flush()
        session.add(
            SiteJob(
                run_id=run.id,
                site_id=site["id"],
                site_name=site["name"],
                lane="general",
                attempt=1,
                status=status,
                message=status,
                result_json="{}",
                started_at=finished,
                finished_at=finished,
            )
        )
        session.commit()
    finally:
        session.close()


def _job_on(client: TestClient, site: dict, *, status: str, day: date) -> None:
    tz = ZoneInfo(client.app.state.settings.scheduler_timezone)
    finished = datetime.combine(day, time(hour=12), tzinfo=tz).astimezone(timezone.utc)
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
            created_at=finished,
            started_at=finished,
            finished_at=finished,
        )
        session.add(run)
        session.flush()
        session.add(
            SiteJob(
                run_id=run.id,
                site_id=site["id"],
                site_name=site["name"],
                lane="general",
                attempt=1,
                status=status,
                message=status,
                result_json="{}",
                started_at=finished,
                finished_at=finished,
            )
        )
        session.commit()
    finally:
        session.close()


def test_post_resign_features_replace_makeup_sign(client: TestClient) -> None:
    body = client.get("/api/v1/features").json()
    features = {item["key"]: item for item in body}
    assert "post.resign" in features
    assert "post.buy_card" in features
    assert "post.makeup_sign" not in features
    assert "history_days" not in features["post.resign"]["config_schema"]["properties"]
    assert "shop_url" in features["post.buy_card"]["config_schema"]["properties"]
    assert "max_cards_per_run" not in features["post.buy_card"]["config_schema"]["properties"]
    assert features["post.buy_card"]["config_schema"]["properties"]["card_title"]["type"] == "array"
    assert PostBuyCardConfig(card_title="旧标题").card_title == ["旧标题"]


def test_pending_dates_and_resign_records(client: TestClient) -> None:
    site = _site(client)
    _job(client, site, status="fail", days_ago=2)
    _job(client, site, status="success", days_ago=1)
    _job(client, site, status="success", days_ago=0)
    session = client.app.state.session_factory()
    try:
        service = ResignService(session, client.app.state.settings)
        pending = service.pending_dates(site["id"], 30)
        assert len(pending) == 1
        service.record_results(
            site["id"],
            1,
            1,
            {
                "success": [{"date": pending[0], "message": "补签成功"}],
                "already_signed": [],
                "failed": [],
            },
        )
        session.commit()
        assert service.pending_dates(site["id"], 30) == []
    finally:
        session.close()


def test_pending_dates_without_run_history_is_empty(client: TestClient) -> None:
    site = _site(client)
    session = client.app.state.session_factory()
    try:
        service = ResignService(session, client.app.state.settings)
        assert service.pending_dates(site["id"], 30) == []
    finally:
        session.close()


def test_pending_dates_counts_days_without_success_after_first_record(
    client: TestClient,
) -> None:
    site = _site(client)
    today = datetime.now(ZoneInfo(client.app.state.settings.scheduler_timezone)).date()
    _job_on(client, site, status="success", day=today - timedelta(days=4))
    _job_on(client, site, status="fail", day=today - timedelta(days=2))
    session = client.app.state.session_factory()
    try:
        service = ResignService(session, client.app.state.settings)
        assert service.pending_dates(site["id"], 30) == [
            (today - timedelta(days=3)).isoformat(),
            (today - timedelta(days=2)).isoformat(),
            (today - timedelta(days=1)).isoformat(),
        ]
    finally:
        session.close()


def test_pending_dates_does_not_count_before_first_site_record(
    client: TestClient,
) -> None:
    site = _site(client)
    today = datetime.now(ZoneInfo(client.app.state.settings.scheduler_timezone)).date()
    _job_on(client, site, status="success", day=today - timedelta(days=2))
    session = client.app.state.session_factory()
    try:
        service = ResignService(session, client.app.state.settings)
        assert service.pending_dates(site["id"], 30) == [
            (today - timedelta(days=1)).isoformat()
        ]
    finally:
        session.close()


def test_pending_dates_skips_terminal_resign_records(client: TestClient) -> None:
    site = _site(client)
    today = datetime.now(ZoneInfo(client.app.state.settings.scheduler_timezone)).date()
    failed_day = today - timedelta(days=2)
    _job_on(client, site, status="fail", day=failed_day)
    session = client.app.state.session_factory()
    try:
        service = ResignService(session, client.app.state.settings)
        service.record_results(
            site["id"],
            1,
            1,
            {"already_signed": [{"date": failed_day.isoformat()}]},
        )
        session.commit()
        assert service.pending_dates(site["id"], 30) == [
            (today - timedelta(days=1)).isoformat()
        ]
    finally:
        session.close()
