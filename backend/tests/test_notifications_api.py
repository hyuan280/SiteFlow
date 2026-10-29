from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient

from app.models.execution import Run, SiteJob
from app.models.integration import IntegrationConfig
from app.main import create_app
from app.services.executor import ExecutionManager
from app.services.notifications import (
    DeliveryTarget,
    NotificationDeliveryError,
    NotificationService,
)
from tests.helpers import login_client, make_settings


def _email_payload() -> dict:
    return {
        "enabled": True,
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "security": "starttls",
        "username": "mailer",
        "password": "smtp-password",
        "from_address": "siteflow@example.com",
        "to_addresses": ["admin@example.com", "ops@example.com"],
    }


def _http_payload() -> dict:
    return {
        "enabled": True,
        "url": "https://hooks.example.com/siteflow",
        "method": "POST",
        "headers": {"Authorization": "Bearer hook-secret", "X-Source": "siteflow"},
        "timeout_seconds": 5,
    }


def test_notification_defaults_and_layout_roundtrip(client: TestClient) -> None:
    response = client.get("/api/v1/notifications")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["layout"]["title_template"]
    assert body["layout"]["body_template"]
    assert body["layout"]["format"] == "text"
    assert body["email"]["enabled"] is False
    assert body["email"]["notify_success"] is True
    assert body["email"]["notify_manual"] is True
    assert body["email"]["available"] is False
    assert body["email"]["smtp_port"]["value"] == 587
    assert body["email"]["security"]["value"] == "starttls"
    assert body["email"]["password"]["configured"] is False
    assert body["http"]["enabled"] is False
    assert body["http"]["notify_success"] is True
    assert body["http"]["notify_manual"] is True
    assert body["http"]["method"]["value"] == "POST"
    assert body["http"]["timeout_seconds"]["value"] == 10

    response = client.put(
        "/api/v1/notifications/layout",
        json={
            "title_template": "[PT] {{run_id}} {{status_label}}",
            "body_template": "{{sites}}\n{{summary}}",
            "format": "html",
        },
    )
    assert response.status_code == 200, response.text
    layout = response.json()["layout"]
    assert layout["title_template"] == "[PT] {{run_id}} {{status_label}}"
    assert layout["format"] == "html"

    response = client.put(
        "/api/v1/notifications/layout",
        json={"title_template": "   ", "body_template": "x", "format": "text"},
    )
    assert response.status_code == 422


def test_notification_filters_apply_per_channel(client: TestClient, monkeypatch) -> None:
    response = client.put("/api/v1/notifications/email", json=_email_payload())
    assert response.status_code == 200, response.text
    response = client.put(
        "/api/v1/notifications/email",
        json={"notify_success": False, "notify_manual": False},
    )
    assert response.status_code == 200, response.text
    email = response.json()["email"]
    assert email["notify_success"] is False
    assert email["notify_manual"] is False

    session = client.app.state.session_factory()
    try:
        run = Run(
            mode="scheduled",
            trigger="schedule",
            status="success",
            requested_sites_json="[]",
            force=False,
            created_by="scheduler",
            summary_json="{}",
        )
        session.add(run)
        session.flush()
        session.add(
            SiteJob(
                run_id=run.id,
                site_id=None,
                site_name="filter-site",
                lane="general",
                attempt=1,
                status="success",
                message="ok",
            )
        )
        session.commit()
        run_id = run.id
    finally:
        session.close()

    sent: list[dict] = []

    def fake_email(self, target, message):  # type: ignore[no-untyped-def]
        sent.append(message)
        return "email ok"

    monkeypatch.setattr(NotificationService, "_send_email", fake_email)

    def send() -> None:
        with client.app.state.session_factory() as service_session:
            NotificationService(
                service_session,
                client.app.state.settings,
                client.app.state.crypto,
            ).send_run(run_id)

    def update_run(**values) -> None:
        with client.app.state.session_factory() as run_session:
            row = run_session.get(Run, run_id)
            assert row is not None
            for key, value in values.items():
                setattr(row, key, value)
            run_session.commit()

    send()
    assert sent == []

    update_run(status="fail", trigger="manual")
    send()
    assert sent == []

    response = client.put("/api/v1/notifications/email", json={"notify_manual": True})
    assert response.status_code == 200, response.text
    send()
    assert len(sent) == 1

    update_run(status="success")
    send()
    assert len(sent) == 1

    response = client.put("/api/v1/notifications/email", json={"notify_success": True})
    assert response.status_code == 200, response.text
    send()
    assert len(sent) == 2

    response = client.put("/api/v1/notifications/http", json={"notify_success": False})
    assert response.status_code == 200, response.text
    assert response.json()["http"]["notify_success"] is False
    assert response.json()["http"]["notify_manual"] is True


def test_email_db_roundtrip_masks_password(client: TestClient) -> None:
    response = client.put("/api/v1/notifications/email", json=_email_payload())
    assert response.status_code == 200, response.text
    email = response.json()["email"]
    assert email["enabled"] is True
    assert email["available"] is True
    assert email["smtp_host"]["value"] == "smtp.example.com"
    assert email["smtp_host"]["source"] == "db"
    assert email["to_addresses"]["value"] == ["admin@example.com", "ops@example.com"]
    assert email["password"]["configured"] is True
    assert email["password"]["masked"].startswith("****")
    assert "smtp-password" not in response.text

    session = client.app.state.session_factory()
    try:
        row = session.get(IntegrationConfig, "notification.email")
        assert row is not None
        assert b"smtp-password" not in row.value_ciphertext
    finally:
        session.close()

    response = client.put("/api/v1/notifications/email", json={"enabled": False})
    assert response.status_code == 200, response.text
    email = response.json()["email"]
    assert email["enabled"] is False
    assert email["available"] is True
    assert email["smtp_host"]["value"] == "smtp.example.com"
    assert email["password"]["configured"] is True

    response = client.put(
        "/api/v1/notifications/email",
        json={"to_addresses": ["not-an-email"]},
    )
    assert response.status_code == 422


def test_env_email_config_is_locked_but_enabled_can_change(client: TestClient, tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        database_url=f"sqlite:///{tmp_path}/email-env.db",
        notification_email_enabled=True,
        notification_email_smtp_host="smtp.env.example.com",
        notification_email_smtp_port=465,
        notification_email_security="ssl",
        notification_email_username="env-mailer",
        notification_email_password="env-smtp-password",
        notification_email_from="env@example.com",
        notification_email_to="one@example.com,two@example.com",
    )
    env_client = TestClient(create_app(settings))
    login_client(env_client)
    body = env_client.get("/api/v1/notifications").json()
    email = body["email"]
    assert email["enabled"] is True
    assert email["available"] is True
    assert email["smtp_host"]["source"] == "env"
    assert email["smtp_host"]["editable"] is False
    assert email["password"]["source"] == "env"
    assert email["password"]["configured"] is True
    assert email["to_addresses"]["value"] == ["one@example.com", "two@example.com"]
    assert "env-smtp-password" not in env_client.get("/api/v1/notifications").text

    response = env_client.put(
        "/api/v1/notifications/email",
        json={"smtp_host": "smtp.changed.example.com"},
    )
    assert response.status_code == 409

    response = env_client.put("/api/v1/notifications/email", json={"enabled": False})
    assert response.status_code == 200, response.text
    assert response.json()["email"]["enabled"] is False
    assert env_client.get("/api/v1/notifications").json()["email"]["enabled"] is False

    response = env_client.put("/api/v1/notifications/email", json={"enabled": True})
    assert response.status_code == 200, response.text
    assert response.json()["email"]["enabled"] is True


def test_http_db_roundtrip_hides_headers_and_env_locks_url(client: TestClient, tmp_path) -> None:
    response = client.put("/api/v1/notifications/http", json=_http_payload())
    assert response.status_code == 200, response.text
    http = response.json()["http"]
    assert http["enabled"] is True
    assert http["available"] is True
    assert http["url"]["source"] == "db"
    assert http["header_names"] == ["Authorization", "X-Source"]
    assert "hook-secret" not in response.text

    response = client.put("/api/v1/notifications/http", json={"enabled": False})
    assert response.status_code == 200, response.text
    assert response.json()["http"]["header_names"] == ["Authorization", "X-Source"]

    response = client.put(
        "/api/v1/notifications/http",
        json={"url": "file:///tmp/notify"},
    )
    assert response.status_code == 422

    locked = make_settings(
        tmp_path,
        database_url=f"sqlite:///{tmp_path}/http-env.db",
        notification_http_enabled=True,
        notification_http_url="https://env.example.com/hook",
        notification_http_method="PUT",
        notification_http_headers='{"Authorization": "Bearer env-secret"}',
    )
    locked_client = TestClient(create_app(locked))
    login_client(locked_client)
    body = locked_client.get("/api/v1/notifications").json()["http"]
    assert body["enabled"] is True
    assert body["available"] is True
    assert body["url"]["source"] == "env"
    assert body["url"]["editable"] is False
    assert body["headers"]["source"] == "env"
    assert body["header_names"] == ["Authorization"]
    assert "env-secret" not in locked_client.get("/api/v1/notifications").text

    assert locked_client.put(
        "/api/v1/notifications/http",
        json={"url": "https://changed.example.com"},
    ).status_code == 409
    response = locked_client.put("/api/v1/notifications/http", json={"enabled": False})
    assert response.status_code == 200, response.text
    assert response.json()["http"]["enabled"] is False


def test_notification_test_endpoints_use_saved_layout(
    client: TestClient, monkeypatch
) -> None:
    calls: list[tuple[str, dict]] = []

    def fake_email(self, target, message):  # type: ignore[no-untyped-def]
        calls.append(("email", message))
        return "email ok"

    def fake_http(self, target, message, event):  # type: ignore[no-untyped-def]
        calls.append(("http", message))
        return "http ok"

    monkeypatch.setattr(NotificationService, "_send_email", fake_email)
    monkeypatch.setattr(NotificationService, "_send_http", fake_http)
    assert client.put("/api/v1/notifications/email", json=_email_payload()).status_code == 200
    assert client.put("/api/v1/notifications/http", json=_http_payload()).status_code == 200
    assert client.put(
        "/api/v1/notifications/layout",
        json={
            "title_template": "Run {{run_id}} {{status_label}}",
            "body_template": "{{summary}}\n{{sites}}",
            "format": "text",
        },
    ).status_code == 200

    response = client.post("/api/v1/notifications/email/test")
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    response = client.post("/api/v1/notifications/http/test")
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert [channel for channel, _ in calls] == ["email", "http"]
    assert calls[0][1]["title"] == "Run 0 失败"
    assert "示例站点 A：成功" in calls[0][1]["body"]
    assert "示例站点 C：失败" in calls[1][1]["body"]


def test_html_layout_escapes_site_text(client: TestClient) -> None:
    response = client.put(
        "/api/v1/notifications/layout",
        json={
            "title_template": "{{title_missing}} {{run_id}}",
            "body_template": "{{sites}}",
            "format": "html",
        },
    )
    assert response.status_code == 200, response.text
    session = client.app.state.session_factory()
    try:
        service = NotificationService(
            session, client.app.state.settings, client.app.state.crypto
        )
        event = service._sample_event()
        event["sites"][0]["site_name"] = "<script>alert(1)</script>"
        message = service._render(event)
        assert message["title"] == "{{title_missing}} 0"
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in message["body"]
        assert "<script>" not in message["body"]
    finally:
        session.close()


def test_email_timeout_has_actionable_message(client: TestClient, monkeypatch) -> None:
    def factory(host, port, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr("app.services.notifications.smtplib.SMTP", factory)
    session = client.app.state.session_factory()
    try:
        service = NotificationService(
            session, client.app.state.settings, client.app.state.crypto
        )
        target = DeliveryTarget(
            channel="email",
            enabled=True,
            values={
                "smtp_host": "mail.example.com",
                "smtp_port": 465,
                "security": "starttls",
                "username": None,
                "password": None,
                "from_address": "from@example.com",
                "to_addresses": ["to@example.com"],
            },
        )
        try:
            service._send_email(
                target,
                {"title": "subject", "body": "body", "format": "text"},
            )
        except NotificationDeliveryError as exc:
            message = str(exc)
        else:
            raise AssertionError("expected NotificationDeliveryError")
    finally:
        session.close()
    assert "连接 SMTP 超时" in message
    assert "465 通常使用 SSL" in message


def test_http_payload_contains_content_alias(client: TestClient, monkeypatch) -> None:
    captured: dict = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def request(self, method, url, json, headers):
            captured.update({"method": method, "url": url, "json": json, "headers": headers})
            return FakeResponse()

    monkeypatch.setattr("app.services.notifications.httpx.Client", FakeClient)
    session = client.app.state.session_factory()
    try:
        service = NotificationService(
            session, client.app.state.settings, client.app.state.crypto
        )
        target = DeliveryTarget(
            channel="http",
            enabled=True,
            values={
                "url": "https://hook.example.com/notify",
                "method": "POST",
                "headers": {},
                "timeout_seconds": 10,
            },
        )
        detail = service._send_http(
            target,
            {"title": "title", "body": "body text", "format": "text"},
            {"run_id": 1},
        )
    finally:
        session.close()
    assert detail == "HTTP 200"
    assert captured["json"]["body"] == "body text"
    assert captured["json"]["content"] == "body text"


def test_run_finalization_schedules_notification(client: TestClient, monkeypatch) -> None:
    session = client.app.state.session_factory()
    try:
        run = Run(
            mode="manual",
            trigger="manual",
            status="running",
            requested_sites_json="[]",
            force=False,
            created_by="admin",
            summary_json="{}",
        )
        session.add(run)
        session.flush()
        session.add(
            SiteJob(
                run_id=run.id,
                site_id=None,
                site_name="notification-site",
                lane="general",
                attempt=1,
                status="success",
                message="ok",
            )
        )
        session.commit()
        run_id = run.id
    finally:
        session.close()

    sent: list[int] = []

    def fake_send(self, run_id: int):  # type: ignore[no-untyped-def]
        sent.append(run_id)
        return []

    monkeypatch.setattr(NotificationService, "send_run", fake_send)
    manager: ExecutionManager = client.app.state.executor

    async def finalize() -> None:
        manager._finalize_run(run_id)
        await asyncio.sleep(0)
        await asyncio.gather(*list(manager._notification_tasks))

    asyncio.run(finalize())
    assert sent == [run_id]
    session = client.app.state.session_factory()
    try:
        row = session.get(Run, run_id)
        assert row is not None
        assert row.status == "success"
        assert json.loads(row.summary_json) == {"success": 1}
    finally:
        session.close()
