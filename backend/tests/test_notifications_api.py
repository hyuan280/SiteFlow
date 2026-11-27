from __future__ import annotations

import asyncio
import json
from email import policy
from email.parser import BytesParser
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.models.execution import JobArtifact, Run, SiteJob
from app.models.integration import IntegrationConfig
from app.main import create_app
from app.services.executor import ExecutionManager
from app.services.notifications import (
    DeliveryTarget,
    HTTP_TEST_IMAGE_PATH,
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
    assert body["layout"]["title_template"] == "[SiteFlow] 运行任务通知 #{{run_id}}"
    assert body["layout"]["body_template"] == (
        "状态：{{status_label}}\n"
        "站点：成功 {{success_count}}，失败 {{fail_count}}，推迟 {{deferred_count}}，"
        "等待人工 {{waiting_count}}\n"
        "触发：{{trigger_label}}\n"
        "结束时间：{{finished_at}}\n\n\n"
        "{{sites}}\n"
        "-------------------------------------\n"
        "详细信息查看：{{siteflow_domain}}"
    )
    assert body["layout"]["format"] == "text"
    assert body["email"]["enabled"] is False
    assert body["email"]["notify_success"] is True
    assert body["email"]["notify_manual"] is True
    assert body["email"]["notify_assistant"] is True
    assert body["email"]["available"] is False
    assert body["email"]["smtp_port"]["value"] == 587
    assert body["email"]["security"]["value"] == "starttls"
    assert body["email"]["password"]["configured"] is False
    assert body["http"]["enabled"] is False
    assert body["http"]["notify_success"] is True
    assert body["http"]["notify_manual"] is True
    assert body["http"]["notify_assistant"] is True
    assert body["http"]["method"]["value"] == "POST"
    assert body["http"]["title_field"]["value"] == "title"
    assert body["http"]["body_field"]["value"] == "content"
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
        json={"notify_success": False, "notify_manual": False, "notify_assistant": False},
    )
    assert response.status_code == 200, response.text
    email = response.json()["email"]
    assert email["notify_success"] is False
    assert email["notify_manual"] is False
    assert email["notify_assistant"] is False
    assert response.json()["http"]["notify_assistant"] is True

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


@pytest.mark.parametrize("channel", ["email", "http"])
def test_assistant_flag_persists_independently_in_database(client: TestClient, channel: str) -> None:
    from app.models.setting import AppSetting

    other = "http" if channel == "email" else "email"
    response = client.put(f"/api/v1/notifications/{channel}", json={"notify_assistant": False})
    assert response.status_code == 200, response.text
    settings = client.get("/api/v1/notifications").json()
    assert settings[channel]["notify_assistant"] is False
    assert settings[other]["notify_assistant"] is True
    assert settings[channel]["notify_success"] is True
    assert settings[channel]["notify_manual"] is True
    with client.app.state.session_factory() as db:
        row = db.get(AppSetting, f"notification.{channel}.notify_assistant")
        assert json.loads(row.value_json) is False
        assert db.get(IntegrationConfig, f"notification.{channel}") is None
    response = client.put(f"/api/v1/notifications/{channel}", json={"notify_assistant": True})
    assert response.status_code == 200 and response.json()[channel]["notify_assistant"] is True


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

    response = env_client.put(
        "/api/v1/notifications/email", json={"enabled": False, "notify_assistant": False})
    assert response.status_code == 200, response.text
    assert response.json()["email"]["enabled"] is False
    assert response.json()["email"]["notify_assistant"] is False
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
    response = locked_client.put(
        "/api/v1/notifications/http", json={"enabled": False, "notify_assistant": False})
    assert response.status_code == 200, response.text
    assert response.json()["http"]["enabled"] is False
    assert response.json()["http"]["notify_assistant"] is False


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
            "body_template": (
                "{{summary}}\n{{sites}}\n推迟：{{deferred_sites}}\n"
                "未成功 {{unsuccessful_count}}：{{unsuccessful_sites}}\n{{siteflow_domain}}"
            ),
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
    assert "推迟：示例站点 D：已推迟" in calls[0][1]["body"]
    assert "未成功 2：示例站点 C：失败" in calls[1][1]["body"]
    assert "{{siteflow_domain}}" not in calls[0][1]["body"]


def test_notification_tests_use_unsaved_config_without_persisting(client: TestClient, monkeypatch) -> None:
    sent = {}

    def send_email(self, target, message):
        sent["email"] = target.values
        return "email ok"

    def send_http(self, target, message, event):
        sent["http"] = target.values
        return "http ok"

    monkeypatch.setattr(NotificationService, "_send_email", send_email)
    monkeypatch.setattr(NotificationService, "_send_http", send_http)
    assert client.put("/api/v1/notifications/email", json=_email_payload()).status_code == 200
    assert client.put("/api/v1/notifications/http", json=_http_payload()).status_code == 200
    saved = client.get("/api/v1/notifications").json()

    response = client.post("/api/v1/notifications/email/test", json={
        "smtp_host": "draft.example.com", "to_addresses": ["draft@example.com"],
    })
    assert response.json()["ok"], response.text
    assert sent["email"]["smtp_host"] == "draft.example.com"
    assert sent["email"]["to_addresses"] == ["draft@example.com"]
    assert sent["email"]["password"] == "smtp-password"

    response = client.post("/api/v1/notifications/email/test", json={"password": "draft-password"})
    assert response.json()["ok"], response.text
    assert sent["email"]["password"] == "draft-password"

    response = client.post("/api/v1/notifications/http/test", json={
        "url": "https://draft.example.com/hook", "title_field": "subject",
        "body_field": "message", "image_field": "file", "headers": {},
    })
    assert response.json()["ok"], response.text
    assert sent["http"]["url"] == "https://draft.example.com/hook"
    assert sent["http"]["title_field"] == "subject"
    assert sent["http"]["body_field"] == "message"
    assert sent["http"]["image_field"] == "file"
    assert sent["http"]["headers"] == {}

    assert client.get("/api/v1/notifications").json() == saved


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


def test_notification_domain_uses_environment(monkeypatch) -> None:
    monkeypatch.setenv("SITEFLOW_DOMAIN", "https://siteflow.example.com")
    assert Settings(_env_file=None).siteflow_domain == "https://siteflow.example.com"


def test_notification_run_site_groups(client: TestClient) -> None:
    response = client.put(
        "/api/v1/notifications/layout",
        json={
            "title_template": "{{site_count}}/{{unsuccessful_count}}/{{deferred_count}}",
            "body_template": "{{deferred_sites}}\n---\n{{unsuccessful_sites}}",
            "format": "text",
        },
    )
    assert response.status_code == 200, response.text
    statuses = [
        "success", "skipped", "queued", "running", "fail", "error", "unknown",
        "waiting_human", "deferred", "canceled", "interrupted",
    ]
    with client.app.state.session_factory() as session:
        run = Run(mode="manual", trigger="manual", status="fail", created_by="admin")
        session.add(run)
        session.flush()
        session.add_all([
            SiteJob(
                run_id=run.id,
                site_name=status,
                lane="general",
                status=status,
                message="detail",
            )
            for status in statuses
        ])
        session.commit()
        service = NotificationService(
            session, client.app.state.settings, client.app.state.crypto
        )
        message = service._render(service._run_event(run.id))
    assert message["title"] == "11/7/1"
    deferred, unsuccessful = message["body"].split("\n---\n")
    assert deferred == "deferred：已推迟 · detail"
    assert [line.split("：")[0] for line in unsuccessful.splitlines()] == statuses[4:]


@pytest.mark.parametrize("format", ["text", "html"])
@pytest.mark.parametrize("empty", [False, True])
def test_notification_new_variables_render(client: TestClient, format: str, empty: bool) -> None:
    response = client.put(
        "/api/v1/notifications/layout",
        json={
            "title_template": "{{unsuccessful_count}}/{{deferred_count}}",
            "body_template": (
                "{{siteflow_domain}}|{{deferred_sites}}|{{unsuccessful_sites}}|{{summary}}"
            ),
            "format": format,
        },
    )
    assert response.status_code == 200, response.text
    settings = client.app.state.settings.model_copy(update={
        "siteflow_domain": "" if empty else " https://siteflow.example.com/?a=1&b=2 "
    })
    with client.app.state.session_factory() as session:
        service = NotificationService(session, settings, client.app.state.crypto)
        event = service._sample_event()
        event["sites"][2]["site_name"] = "<failed>"
        event["sites"][3]["message"] = "<later> & retry"
        if empty:
            event["sites"] = event["sites"][:2]
            event.update(site_count=2, fail_count=0, deferred_count=0)
        message = service._render(event)
    domain, deferred, unsuccessful, summary = message["body"].split("|")
    if empty:
        assert message["title"] == "0/0"
        assert (domain, deferred, unsuccessful) == ("", "", "")
        assert summary == "成功 2，失败 0，推迟 0，等待人工 0"
    else:
        assert message["title"] == "2/1"
        if format == "html":
            assert domain == "https://siteflow.example.com/?a=1&amp;b=2"
            assert deferred == "示例站点 D：已推迟 · &lt;later&gt; &amp; retry"
            assert unsuccessful == "&lt;failed&gt;：失败 · 登录失败<br>" + deferred
        else:
            assert domain == "https://siteflow.example.com/?a=1&b=2"
            assert deferred == "示例站点 D：已推迟 · <later> & retry"
            assert unsuccessful == "<failed>：失败 · 登录失败\n" + deferred
        assert summary == "成功 2，失败 1，推迟 1，等待人工 0"


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


def test_http_payload_uses_default_title_and_content_fields(client: TestClient, monkeypatch) -> None:
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
    assert captured["json"]["title"] == "title"
    assert "body" not in captured["json"]
    assert captured["json"]["content"] == "body text"


def test_http_notification_sends_configured_multipart_fields(client: TestClient, monkeypatch) -> None:
    requests = []

    def capture(request):
        requests.append(request)
        return httpx.Response(204)

    client_class = httpx.Client
    monkeypatch.setattr(
        "app.services.notifications.httpx.Client",
        lambda **kwargs: client_class(transport=httpx.MockTransport(capture), **kwargs),
    )

    response = client.put("/api/v1/notifications/http", json={
        **_http_payload(),
        "title_field": "subject",
        "body_field": "message",
        "image_field": "attachment",
        "headers": {"content-type": "application/json", "X-Source": "siteflow"},
    })
    assert response.status_code == 200, response.text
    assert response.json()["http"]["title_field"]["value"] == "subject"
    assert response.json()["http"]["body_field"]["value"] == "message"
    assert response.json()["http"]["image_field"]["value"] == "attachment"

    def parts(request):
        content_type = request.headers["content-type"]
        assert content_type.startswith("multipart/form-data; boundary=")
        assert request.headers["x-source"] == "siteflow"
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {content_type}\r\n\r\n".encode() + request.content,
        )
        return {
            part.get_param("name", header="content-disposition"): part
            for part in message.iter_parts()
        }

    response = client.post("/api/v1/notifications/http/test")
    assert response.json()["ok"], response.text
    test_parts = parts(requests.pop())
    test_image = HTTP_TEST_IMAGE_PATH.read_bytes()
    assert test_parts["attachment"].get_filename() == "notification-test.png"
    assert test_parts["attachment"].get_content_type() == "image/png"
    assert test_parts["attachment"].get_payload(decode=True) == test_image
    assert "失败" in test_parts["message"].get_payload(decode=True).decode()
    assert "[SiteFlow]" in test_parts["subject"].get_payload(decode=True).decode()
    assert not {"title", "body", "content"} & test_parts.keys()

    settings = client.app.state.settings
    root = Path(settings.artifact_root)
    root.mkdir(exist_ok=True)
    screenshot_path = root / "failure.png"
    screenshot_path.write_bytes(test_image)
    with client.app.state.session_factory() as session:
        run = Run(mode="scheduled", trigger="schedule", status="fail", created_by="scheduler")
        session.add(run)
        session.flush()
        job = SiteJob(run_id=run.id, site_name="失败站点", lane="general", status="fail")
        session.add(job)
        session.flush()
        session.add(JobArtifact(
            run_id=run.id, site_job_id=job.id, kind="screenshot",
            relative_path=screenshot_path.name, content_type="image/png",
            size_bytes=len(test_image),
        ))
        run_id = run.id
        session.commit()
        service = NotificationService(session, settings, client.app.state.crypto)
        assert service.send_run(run_id)[0].ok
        run_parts = parts(requests.pop())
        assert json.loads(run_parts["run"].get_payload(decode=True))["run_id"] == run_id
        assert "message" in run_parts and "attachment" not in run_parts

    response = client.put("/api/v1/notifications/http", json={"image_field": "none"})
    assert response.status_code == 200, response.text
    response = client.post("/api/v1/notifications/http/test")
    assert response.json()["ok"], response.text
    payload = json.loads(requests.pop().content)
    assert {"subject", "message"} <= payload.keys()
    assert not {"title", "body", "content", "attachment"} & payload.keys()


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

    def fake_send(self, event, message, *, targets):  # type: ignore[no-untyped-def]
        sent.append(event["run_id"])
        return []

    monkeypatch.setattr(NotificationService, "send_event", fake_send)
    manager: ExecutionManager = client.app.state.executor

    async def finalize() -> None:
        manager._finalize_run(run_id)
        await asyncio.to_thread(manager.notification_dispatch.dispatch_pending)

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
