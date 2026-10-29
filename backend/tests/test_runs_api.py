from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.models.execution import JobArtifact, JobStep, Run, SiteJob
from app.schemas.execution import RunCreate
from app.services.credentials import CredentialService
from app.services.execution import ExecutionService
from app.services.executor import ExecutionManager


class FakeExecutor:
    def __init__(self) -> None:
        self.submitted: list[int] = []
        self.canceled: list[int] = []

    def submit(self, run_id: int) -> None:
        self.submitted.append(run_id)

    async def cancel(self, run_id: int) -> None:
        self.canceled.append(run_id)


def _install_executor(client: TestClient) -> FakeExecutor:
    executor = FakeExecutor()
    client.app.state.executor = executor
    return executor


def _create_site(client: TestClient, name: str = "run-site") -> dict:
    response = client.post(
        "/api/v1/sites",
        json={
            "name": name,
            "base_url": "https://pt.example.com",
            "steps": [
                {"step_key": "open", "feature_type": "flow.open"},
                {"step_key": "sign", "feature_type": "sign.button"},
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_runs_require_auth(anon_client: TestClient) -> None:
    assert anon_client.get("/api/v1/runs").status_code == 401
    assert anon_client.post("/api/v1/runs", json={}).status_code == 401


def test_create_list_get_and_cancel_queued_run(client: TestClient) -> None:
    executor = _install_executor(client)
    site = _create_site(client)

    response = client.post("/api/v1/runs", json={"site_ids": [site["id"]]})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "queued"
    assert body["site_count"] == 1
    assert body["requested_sites"] == [site["id"]]
    assert body["site_names"] == ["run-site"]
    assert body["jobs"][0]["site_name"] == "run-site"
    assert body["jobs"][0]["lane"] == "general"
    assert body["jobs"][0]["steps"] == []
    assert executor.submitted == [body["id"]]

    listing = client.get("/api/v1/runs").json()
    assert listing[0]["id"] == body["id"]
    assert listing[0]["summary"]["queued"] == 1

    detail = client.get(f"/api/v1/runs/{body['id']}")
    assert detail.status_code == 200
    assert detail.json()["jobs"][0]["status"] == "queued"

    canceled = client.post(f"/api/v1/runs/{body['id']}/cancel")
    assert canceled.status_code == 200, canceled.text
    assert canceled.json()["status"] == "canceled"
    assert canceled.json()["cancel_requested"] is True
    assert canceled.json()["jobs"][0]["status"] == "canceled"
    assert executor.canceled == [body["id"]]


def _next_update(lines) -> dict:  # type: ignore[no-untyped-def]
    event = ""
    data: list[str] = []
    for line in lines:
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data.append(line.split(":", 1)[1].lstrip())
        elif line == "":
            if event == "update":
                return json.loads("\n".join(data))
            event = ""
            data = []
    raise AssertionError("stream ended before an update event")


def test_run_events_stream_snapshot_and_changes(client: TestClient) -> None:
    _install_executor(client)
    site = _create_site(client, "run-stream")
    run = client.post("/api/v1/runs", json={"site_ids": [site["id"]]}).json()

    with client.stream("GET", f"/api/v1/runs/{run['id']}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        lines = response.iter_lines()
        first = _next_update(lines)
        assert first["id"] == run["id"]
        assert first["jobs"][0]["site_name"] == "run-stream"

        manager = ExecutionManager(
            client.app.state.session_factory,
            client.app.state.settings,
            client.app.state.crypto,
        )
        manager._persist_progress(
            run["jobs"][0]["id"],
            {
                "message": "执行中",
                "steps": [
                    {
                        "step_key": "open",
                        "feature_type": "flow.open",
                        "label": "访问站点",
                        "status": "success",
                        "message": "已打开",
                        "data": {"url": "https://pt.example.com"},
                    },
                    {
                        "step_key": "sign",
                        "feature_type": "sign.button",
                        "label": "签到",
                        "status": "running",
                        "message": "执行中",
                        "data": {},
                    },
                ],
            },
        )

        second = _next_update(lines)
        assert second["jobs"][0]["steps"][0]["status"] == "success"
        assert second["jobs"][0]["steps"][1]["status"] == "running"

        session = client.app.state.session_factory()
        try:
            row = session.get(Run, run["id"])
            row.status = "success"
            session.commit()
        finally:
            session.close()

        third = _next_update(lines)
        assert third["status"] == "success"


def test_progress_persists_and_replaces_steps(client: TestClient) -> None:
    _install_executor(client)
    site = _create_site(client, "run-progress")
    run = client.post("/api/v1/runs", json={"site_ids": [site["id"]]}).json()
    job_id = run["jobs"][0]["id"]
    manager = ExecutionManager(
        client.app.state.session_factory,
        client.app.state.settings,
        client.app.state.crypto,
    )

    manager._persist_progress(
        job_id,
        {
            "message": "执行中",
            "steps": [
                {"step_key": "open", "status": "success", "message": "已打开"},
                {"step_key": "sign", "status": "running", "message": "执行中"},
            ],
        },
    )
    detail = client.get(f"/api/v1/runs/{run['id']}").json()
    steps = detail["jobs"][0]["steps"]
    assert [step["status"] for step in steps] == ["success", "running"]
    assert detail["jobs"][0]["message"] == "执行中"

    manager._persist_progress(
        job_id,
        {
            "message": "签到成功",
            "steps": [
                {"step_key": "open", "status": "success", "message": "已打开"},
                {"step_key": "sign", "status": "success", "message": "签到成功"},
            ],
        },
    )
    detail = client.get(f"/api/v1/runs/{run['id']}").json()
    steps = detail["jobs"][0]["steps"]
    assert len(steps) == 2
    assert steps[1]["status"] == "success"
    assert detail["jobs"][0]["message"] == "签到成功"


def test_progress_persists_artifacts_and_serves_files(
    client: TestClient, settings
) -> None:
    _install_executor(client)
    site = _create_site(client, "run-artifacts")
    run = client.post("/api/v1/runs", json={"site_ids": [site["id"]]}).json()
    job_id = run["jobs"][0]["id"]
    root = Path(settings.artifact_root)
    log_path = root / f"run-{run['id']}" / f"job-{job_id}" / "job.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("line one\nline two\n", encoding="utf-8")
    data = log_path.read_bytes()

    manager = ExecutionManager(
        client.app.state.session_factory,
        client.app.state.settings,
        client.app.state.crypto,
    )
    manager._persist_progress(
        job_id,
        {
            "message": "执行中",
            "steps": [],
            "artifacts": [
                {
                    "kind": "log",
                    "label": "执行日志",
                    "relative_path": log_path.relative_to(root).as_posix(),
                    "content_type": "text/plain; charset=utf-8",
                    "size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            ],
        },
    )

    detail = client.get(f"/api/v1/runs/{run['id']}").json()
    artifacts = detail["jobs"][0]["artifacts"]
    assert len(artifacts) == 1
    assert artifacts[0]["kind"] == "log"
    artifact_id = artifacts[0]["id"]

    response = client.get(f"/api/v1/artifacts/{artifact_id}")
    assert response.status_code == 200
    assert response.text == "line one\nline two\n"
    assert response.headers["content-type"].startswith("text/plain")

    download = client.get(f"/api/v1/artifacts/{artifact_id}?download=1")
    assert download.status_code == 200
    assert download.headers["content-disposition"].startswith("attachment")


def test_progress_persists_and_serves_captcha_artifacts(
    client: TestClient, settings
) -> None:
    _install_executor(client)
    site = _create_site(client, "run-captcha-artifacts")
    run = client.post("/api/v1/runs", json={"site_ids": [site["id"]]}).json()
    job_id = run["jobs"][0]["id"]
    root = Path(settings.artifact_root)
    path = root / f"run-{run['id']}" / f"job-{job_id}" / "captcha-sign-1.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"captcha-png")
    data = path.read_bytes()

    manager = ExecutionManager(
        client.app.state.session_factory,
        client.app.state.settings,
        client.app.state.crypto,
    )
    manager._persist_progress(
        job_id,
        {
            "message": "执行中",
            "steps": [],
            "artifacts": [
                {
                    "kind": "captcha",
                    "label": "验证码截图 · 登录",
                    "step_key": "sign",
                    "relative_path": path.relative_to(root).as_posix(),
                    "content_type": "image/png",
                    "size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            ],
        },
    )

    detail = client.get(f"/api/v1/runs/{run['id']}").json()
    artifacts = detail["jobs"][0]["artifacts"]
    assert len(artifacts) == 1
    assert artifacts[0]["kind"] == "captcha"
    assert artifacts[0]["step_key"] == "sign"
    artifact_id = artifacts[0]["id"]

    response = client.get(f"/api/v1/artifacts/{artifact_id}")
    assert response.status_code == 200
    assert response.content == b"captcha-png"
    assert response.headers["content-type"].startswith("image/png")

    stats = client.get("/api/v1/artifacts/stats").json()
    assert stats["screenshot_count"] == 1
    assert stats["log_count"] == 0


def test_artifact_stats_and_cleanup(client: TestClient, settings) -> None:
    _install_executor(client)
    site = _create_site(client, "run-artifact-cleanup")
    run = client.post("/api/v1/runs", json={"site_ids": [site["id"]]}).json()
    job_id = run["jobs"][0]["id"]
    root = Path(settings.artifact_root)
    log_path = root / f"run-{run['id']}" / f"job-{job_id}" / "job.log"
    shot_path = root / f"run-{run['id']}" / f"job-{job_id}" / "failure.png"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_bytes(b"old-log")
    shot_path.write_bytes(b"new-shot")
    orphan_path = root / f"run-{run['id']}" / f"job-{job_id}" / "orphan.log"
    orphan_path.write_bytes(b"orphan")
    old_ts = (datetime.now(timezone.utc) - timedelta(days=40)).timestamp()
    os.utime(log_path, (old_ts, old_ts))
    os.utime(orphan_path, (old_ts, old_ts))

    manager = ExecutionManager(
        client.app.state.session_factory,
        client.app.state.settings,
        client.app.state.crypto,
    )
    manager._persist_progress(
        job_id,
        {
            "message": "执行中",
            "steps": [],
            "artifacts": [
                {
                    "kind": "log",
                    "label": "执行日志",
                    "relative_path": log_path.relative_to(root).as_posix(),
                    "content_type": "text/plain; charset=utf-8",
                    "size_bytes": log_path.stat().st_size,
                    "sha256": hashlib.sha256(log_path.read_bytes()).hexdigest(),
                },
                {
                    "kind": "screenshot",
                    "label": "失败截图",
                    "step_key": "sign",
                    "relative_path": shot_path.relative_to(root).as_posix(),
                    "content_type": "image/png",
                    "size_bytes": shot_path.stat().st_size,
                    "sha256": hashlib.sha256(shot_path.read_bytes()).hexdigest(),
                },
            ],
        },
    )
    session = client.app.state.session_factory()
    try:
        row = session.query(JobArtifact).filter_by(relative_path=log_path.relative_to(root).as_posix()).one()
        row.created_at = datetime.now(timezone.utc) - timedelta(days=40)
        session.commit()
    finally:
        session.close()

    stats = client.get("/api/v1/artifacts/stats")
    assert stats.status_code == 200, stats.text
    body = stats.json()
    assert body["log_count"] == 1
    assert body["screenshot_count"] == 1
    assert body["total_bytes"] == 7 + 8
    assert body["retention_days"] == 30

    cleanup = client.post("/api/v1/artifacts/cleanup")
    assert cleanup.status_code == 200, cleanup.text
    result = cleanup.json()
    assert result["removed_count"] == 1
    assert result["orphan_count"] == 1
    assert not log_path.exists()
    assert not orphan_path.exists()
    assert shot_path.exists()

    after = client.get("/api/v1/artifacts/stats").json()
    assert after["log_count"] == 0
    assert after["screenshot_count"] == 1


def test_run_events_require_auth_and_existing_run(
    anon_client: TestClient, client: TestClient
) -> None:
    assert anon_client.get("/api/v1/runs/1/events").status_code == 401
    assert anon_client.get("/api/v1/artifacts/1").status_code == 401
    assert client.get("/api/v1/runs/999999/events").status_code == 404


def test_run_all_enabled_sites_and_skip_disabled(client: TestClient) -> None:
    _install_executor(client)
    first = _create_site(client, "run-enabled")
    second = _create_site(client, "run-disabled")
    assert client.put(
        f"/api/v1/sites/{second['id']}",
        json={
            "name": "run-disabled",
            "base_url": "https://pt.example.com",
            "enabled": False,
            "steps": [
                {"step_key": "open", "feature_type": "flow.open"},
                {"step_key": "sign", "feature_type": "sign.button"},
            ],
        },
    ).status_code == 200

    response = client.post("/api/v1/runs", json={})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["requested_sites"] == [first["id"]]
    assert body["site_count"] == 1

    explicit = client.post("/api/v1/runs", json={"site_ids": [second["id"]]})
    assert explicit.status_code == 422
    assert "未启用" in explicit.json()["detail"]


def test_cancel_finished_run_conflicts(client: TestClient) -> None:
    _install_executor(client)
    site = _create_site(client, "run-conflict")
    run = client.post("/api/v1/runs", json={"site_ids": [site["id"]]}).json()
    session = client.app.state.session_factory()
    try:
        row = session.get(Run, run["id"])
        row.status = "success"
        session.commit()
    finally:
        session.close()
    response = client.post(f"/api/v1/runs/{run['id']}/cancel")
    assert response.status_code == 409


def test_recover_interrupted(client: TestClient) -> None:
    _install_executor(client)
    site = _create_site(client, "run-recover")
    run = client.post("/api/v1/runs", json={"site_ids": [site["id"]]}).json()
    session = client.app.state.session_factory()
    try:
        row = session.get(Run, run["id"])
        row.status = "running"
        job = session.query(SiteJob).filter_by(run_id=row.id).one()
        job.status = "running"
        session.commit()
        ExecutionService(
            session, client.app.state.settings, client.app.state.crypto
        ).recover_interrupted()
        session.refresh(row)
        session.refresh(job)
        assert row.status == "interrupted"
        assert job.status == "interrupted"
    finally:
        session.close()


def test_executor_persists_results_and_injects_credentials(client: TestClient) -> None:
    site_response = client.post(
        "/api/v1/sites",
        json={
            "name": "run-executor",
            "base_url": "https://pt.example.com",
            "steps": [
                {"step_key": "open", "feature_type": "flow.open"},
                {"step_key": "login", "feature_type": "auth.form_login"},
                {"step_key": "sign", "feature_type": "sign.button"},
            ],
        },
    )
    assert site_response.status_code == 201, site_response.text
    site = site_response.json()
    assert client.put(
        f"/api/v1/sites/{site['id']}/credential",
        json={"username": "executor-user", "password": "executor-pass"},
    ).status_code == 200
    assert client.put(
        f"/api/v1/sites/{site['id']}/cookies",
        json={"cookies": "uid=1; pass=abc"},
    ).status_code == 200

    app = client.app
    session = app.state.session_factory()
    try:
        service = ExecutionService(session, app.state.settings, app.state.crypto)
        run = service.create_run(RunCreate(site_ids=[site["id"]]), "admin")
        manager = ExecutionManager(
            session_factory=app.state.session_factory,
            settings=app.state.settings,
            crypto=app.state.crypto,
        )
        seen: dict = {}

        async def fake_execute(payload):
            seen.update(payload)
            return {
                "status": "success",
                "message": "完成",
                "cookies": [
                    {
                        "name": "uid",
                        "value": "2",
                        "domain": "pt.example.com",
                        "path": "/",
                        "secure": True,
                    }
                ],
                "steps": [
                    {
                        "step_key": "open",
                        "feature_type": "flow.open",
                        "label": "打开站点",
                        "status": "success",
                        "message": "ok",
                        "data": {"url": "https://pt.example.com"},
                    },
                    {
                        "step_key": "login",
                        "feature_type": "auth.form_login",
                        "label": "表单登录",
                        "status": "success",
                        "message": "ok",
                        "data": {},
                    },
                    {
                        "step_key": "sign",
                        "feature_type": "sign.button",
                        "label": "签到",
                        "status": "success",
                        "message": "ok",
                        "data": {},
                    },
                ],
            }

        manager._post_execute = fake_execute  # type: ignore[method-assign]
        asyncio.run(manager._execute_run(run.id))

        session.expire_all()
        row = session.get(Run, run.id)
        assert row is not None
        assert row.status == "success"
        job = session.query(SiteJob).filter_by(run_id=run.id).one()
        assert job.status == "success"
        steps = session.query(JobStep).filter_by(site_job_id=job.id).all()
        assert [step.step_key for step in steps] == ["open", "login", "sign"]
        assert "credentials" not in seen
        assert "executor-pass" not in str(seen)
        assert seen["secret_access"]["providers"]["login"] == "builtin.manual"
        assert seen["lane_key"] == "general"
        assert [signal["signal_key"] for signal in seen["signals"]] == [
            "sys_resign_pending"
        ]
        assert [(cookie["name"], cookie["value"]) for cookie in seen["cookies"]] == [
            ("uid", "1"),
            ("pass", "abc"),
        ]
        cookie_view = CredentialService(session, app.state.crypto).view(site["id"])
        assert cookie_view.cookies_configured is True
        assert cookie_view.cookie_count == 1
        saved_cookies = CredentialService(session, app.state.crypto).resolve_cookies(
            site["id"]
        )
        assert saved_cookies is not None
        assert saved_cookies[0]["name"] == "uid"
        assert saved_cookies[0]["value"] == "2"
    finally:
        session.close()


def test_executor_keeps_cookies_when_job_fails(client: TestClient) -> None:
    site = client.post(
        "/api/v1/sites",
        json={
            "name": "run-cookie-failure",
            "base_url": "https://pt.example.com",
            "steps": [{"step_key": "open", "feature_type": "flow.open"}],
        },
    ).json()
    assert client.put(
        f"/api/v1/sites/{site['id']}/cookies",
        json={"cookies": "uid=1; pass=abc"},
    ).status_code == 200

    app = client.app
    session = app.state.session_factory()
    try:
        run = ExecutionService(session, app.state.settings, app.state.crypto).create_run(
            RunCreate(site_ids=[site["id"]]), "admin"
        )
        manager = ExecutionManager(
            session_factory=app.state.session_factory,
            settings=app.state.settings,
            crypto=app.state.crypto,
        )

        async def fake_execute(_payload):
            return {"status": "fail", "message": "failed", "steps": []}

        manager._post_execute = fake_execute  # type: ignore[method-assign]
        asyncio.run(manager._execute_run(run.id))
        session.expire_all()
        assert (
            CredentialService(session, app.state.crypto)
            .view(site["id"])
            .cookies_configured
            is True
        )
    finally:
        session.close()


def test_executor_passes_selected_integrations(client: TestClient) -> None:
    assert client.put(
        "/api/v1/integrations/twofauth",
        json={"url": "https://2fa.example.com", "token": "dummy-2fauth-token"},
    ).status_code == 200
    site_response = client.post(
        "/api/v1/sites",
        json={
            "name": "run-integrations",
            "base_url": "https://pt.example.com",
            "steps": [
                {
                    "step_key": "totp",
                    "feature_type": "challenge.2fa",
                    "config": {"service_hint": "PT"},
                },
                {
                    "step_key": "turn",
                    "feature_type": "challenge.turnstile",
                },
                {"step_key": "sign", "feature_type": "sign.button"},
            ],
        },
    )
    assert site_response.status_code == 201, site_response.text
    site = site_response.json()

    app = client.app
    session = app.state.session_factory()
    try:
        run = ExecutionService(session, app.state.settings, app.state.crypto).create_run(
            RunCreate(site_ids=[site["id"]]), "admin"
        )
        manager = ExecutionManager(
            session_factory=app.state.session_factory,
            settings=app.state.settings,
            crypto=app.state.crypto,
        )
        seen: dict = {}

        async def fake_execute(payload):
            seen.update(payload)
            return {"status": "success", "message": "ok", "steps": []}

        manager._post_execute = fake_execute  # type: ignore[method-assign]
        asyncio.run(manager._execute_run(run.id))
        assert "twofauth" not in seen
        assert "dummy-2fauth-token" not in str(seen)
        assert seen["cfbox"]["url"] == "http://cfbox.example.com"
        assert seen["cfbox"]["api_key"] == "test-cfbox-key"
    finally:
        session.close()


def test_executor_passes_vision_for_image_quiz(client: TestClient) -> None:
    from app.features.base import IMAGE_QUIZ_DEFAULT_PROMPT

    assert client.put(
        "/api/v1/integrations/vision",
        json={
            "base_url": "https://vision.example/v1",
            "model": "vision-model",
            "api_key": "vision-secret",
        },
    ).status_code == 200
    site = client.post(
        "/api/v1/sites",
        json={
            "name": "run-image-quiz",
            "base_url": "https://pt.example.com",
            "steps": [
                {"step_key": "open", "feature_type": "flow.open"},
                {"step_key": "quiz", "feature_type": "sign.image_quiz"},
            ],
        },
    ).json()

    app = client.app
    session = app.state.session_factory()
    try:
        run = ExecutionService(session, app.state.settings, app.state.crypto).create_run(
            RunCreate(site_ids=[site["id"]]), "admin"
        )
        manager = ExecutionManager(
            session_factory=app.state.session_factory,
            settings=app.state.settings,
            crypto=app.state.crypto,
        )
        seen: dict = {}

        async def fake_execute(payload):
            seen.update(payload)
            return {"status": "success", "message": "ok", "steps": []}

        manager._post_execute = fake_execute  # type: ignore[method-assign]
        asyncio.run(manager._execute_run(run.id))
        assert seen["vision"] == {
            "base_url": "https://vision.example/v1",
            "model": "vision-model",
            "api_key": "vision-secret",
            "supports_vision": True,
            "timeout": app.state.settings.vision_timeout,
        }
        assert seen["feature_execution_settings"]["sign.image_quiz"][
            "llm_prompt"
        ] == IMAGE_QUIZ_DEFAULT_PROMPT
    finally:
        session.close()


def test_executor_runs_different_lanes_concurrently(client: TestClient) -> None:
    general = client.post(
        "/api/v1/sites",
        json={
            "name": "concurrent-general",
            "base_url": "https://pt.example.com",
            "lane_policy": "general",
            "steps": [
                {"step_key": "open", "feature_type": "flow.open"},
                {"step_key": "sign", "feature_type": "sign.button"},
            ],
        },
    ).json()
    interaction = client.post(
        "/api/v1/sites",
        json={
            "name": "concurrent-interaction",
            "base_url": "https://pt.example.com",
            "lane_policy": "interaction",
            "steps": [
                {"step_key": "open", "feature_type": "flow.open"},
                {"step_key": "sign", "feature_type": "sign.button"},
            ],
        },
    ).json()

    app = client.app
    session = app.state.session_factory()
    try:
        run = ExecutionService(session, app.state.settings, app.state.crypto).create_run(
            RunCreate(site_ids=[general["id"], interaction["id"]]), "admin"
        )
        manager = ExecutionManager(
            session_factory=app.state.session_factory,
            settings=app.state.settings,
            crypto=app.state.crypto,
        )
        state = {"active": 0, "max": 0}

        async def fake_execute(payload):
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
            try:
                await asyncio.sleep(0.2)
                return {"status": "success", "message": "ok", "steps": []}
            finally:
                state["active"] -= 1

        manager._post_execute = fake_execute  # type: ignore[method-assign]
        asyncio.run(manager._execute_run(run.id))
        session.expire_all()
        row = session.get(Run, run.id)
        assert row is not None and row.status == "success"
        assert state["max"] == 2
    finally:
        session.close()


def test_executor_retries_turnstile_on_interaction_lane(client: TestClient) -> None:
    site = client.post(
        "/api/v1/sites",
        json={
            "name": "fallback-site",
            "base_url": "https://pt.example.com",
            "lane_policy": "general",
            "steps": [
                {"step_key": "open", "feature_type": "flow.open"},
                {"step_key": "turn", "feature_type": "challenge.turnstile"},
                {"step_key": "sign", "feature_type": "sign.button"},
            ],
        },
    ).json()
    app = client.app
    session = app.state.session_factory()
    try:
        run = ExecutionService(session, app.state.settings, app.state.crypto).create_run(
            RunCreate(site_ids=[site["id"]]), "admin"
        )
        manager = ExecutionManager(
            session_factory=app.state.session_factory,
            settings=app.state.settings,
            crypto=app.state.crypto,
        )
        lanes: list[str] = []

        async def fake_execute(payload):
            lanes.append(payload["lane_key"])
            if len(lanes) == 1:
                return {
                    "status": "fallback_interaction",
                    "message": "Playwright 点击未生效，转交互车道重试",
                    "steps": [],
                }
            return {"status": "success", "message": "ok", "steps": []}

        manager._post_execute = fake_execute  # type: ignore[method-assign]
        asyncio.run(manager._execute_run(run.id))
        session.expire_all()
        job = session.query(SiteJob).filter_by(run_id=run.id).one()
        assert lanes == ["general", "interaction"]
        assert job.lane == "interaction"
        assert job.attempt == 2
        assert job.status == "success"
    finally:
        session.close()


def test_executor_defers_bitwarden_credentials_to_worker(client, monkeypatch) -> None:
    from app.services.bitwarden import BitwardenService

    provider = {
        "type": "bitwarden",
        "url": "http://bitwarden:8090",
        "token": "test-token",
        "mode": "domain",
        "host": "pt.example.com",
        "custom": None,
        "timeout": 30,
    }
    monkeypatch.setattr(
        BitwardenService,
        "lookup_runtime_for_site",
        lambda self, site: dict(provider),
    )
    site = client.post(
        "/api/v1/sites",
        json={
            "name": "run-bitwarden",
            "base_url": "https://pt.example.com",
            "bitwarden_enabled": True,
            "bitwarden_match_mode": "domain",
            "steps": [
                {"step_key": "open", "feature_type": "flow.open"},
                {"step_key": "login", "feature_type": "auth.form_login"},
            ],
        },
    ).json()

    app = client.app
    session = app.state.session_factory()
    try:
        run = ExecutionService(session, app.state.settings, app.state.crypto).create_run(
            RunCreate(site_ids=[site["id"]]), "admin"
        )
        manager = ExecutionManager(
            session_factory=app.state.session_factory,
            settings=app.state.settings,
            crypto=app.state.crypto,
        )
        seen: dict = {}

        async def fake_execute(payload):
            seen.update(payload)
            return {"status": "success", "message": "ok", "steps": []}

        manager._post_execute = fake_execute  # type: ignore[method-assign]
        asyncio.run(manager._execute_run(run.id))
        assert "credentials" not in seen
        assert "credential_provider" not in seen
        assert "test-token" not in str(seen)
        assert seen["secret_access"]["providers"]["login"] == "builtin.bitwarden"
    finally:
        session.close()


def test_executor_fails_when_bitwarden_unavailable(client, monkeypatch) -> None:
    from app.services.bitwarden import BitwardenError, BitwardenService

    def fail(self, site):
        raise BitwardenError("Bitwarden 凭据集成未启用")

    monkeypatch.setattr(BitwardenService, "resolve_for_site", fail)
    site = client.post(
        "/api/v1/sites",
        json={
            "name": "run-bitwarden-disabled",
            "base_url": "https://pt.example.com",
            "bitwarden_enabled": True,
            "steps": [
                {"step_key": "open", "feature_type": "flow.open"},
                {"step_key": "login", "feature_type": "auth.form_login"},
            ],
        },
    ).json()

    app = client.app
    session = app.state.session_factory()
    try:
        run = ExecutionService(session, app.state.settings, app.state.crypto).create_run(
            RunCreate(site_ids=[site["id"]]), "admin"
        )
        manager = ExecutionManager(
            session_factory=app.state.session_factory,
            settings=app.state.settings,
            crypto=app.state.crypto,
        )
        called = False

        async def fake_execute(_payload):
            nonlocal called
            called = True
            from app.services.secret_providers import SecretDenied
            access = _payload["secret_access"]
            args = (access["token"], _payload["job_id"], site["id"], "login", "auth.form_login", "builtin.bitwarden")
            lease = await manager.secret_providers.request(*args)
            try:
                await manager.secret_providers.request(*args, lease=lease["lease"])
            except SecretDenied:
                return {"status": "fail", "message": "Bitwarden provider unavailable", "steps": []}
            raise AssertionError("provider failure must be denied")

        manager._post_execute = fake_execute  # type: ignore[method-assign]
        asyncio.run(manager._execute_run(run.id))
        session.expire_all()
        job = session.query(SiteJob).filter_by(run_id=run.id).one()
        assert called is True
        assert job.status == "fail"
        assert "Bitwarden" in job.message
    finally:
        session.close()


def test_today_unsuccessful_skips_successful_and_active(client: TestClient) -> None:
    _install_executor(client)
    sites = [
        _create_site(client, "today-success"),
        _create_site(client, "today-running"),
        _create_site(client, "today-pending"),
    ]
    now = datetime.now(timezone.utc)
    app = client.app
    session = app.state.session_factory()
    try:
        finished_run = Run(
            mode="manual",
            trigger="manual",
            status="success",
            requested_sites_json=json.dumps([sites[0]["id"]]),
            created_by="admin",
            summary_json="{}",
        )
        active_run = Run(
            mode="manual",
            trigger="manual",
            status="running",
            requested_sites_json=json.dumps([sites[1]["id"]]),
            created_by="admin",
            summary_json="{}",
        )
        session.add_all([finished_run, active_run])
        session.flush()
        session.add_all(
            [
                SiteJob(
                    run_id=finished_run.id,
                    site_id=sites[0]["id"],
                    site_name="today-success",
                    lane="general",
                    status="success",
                    finished_at=now,
                ),
                SiteJob(
                    run_id=active_run.id,
                    site_id=sites[1]["id"],
                    site_name="today-running",
                    lane="general",
                    status="running",
                    started_at=now,
                ),
            ]
        )
        session.commit()
    finally:
        session.close()

    response = client.post("/api/v1/runs", json={"today_unsuccessful": True})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["requested_sites"] == [sites[2]["id"]]
    assert [job["site_name"] for job in body["jobs"]] == ["today-pending"]


def test_today_unsuccessful_rejects_when_all_succeeded(client: TestClient) -> None:
    _install_executor(client)
    site = _create_site(client, "today-all-success")
    now = datetime.now(timezone.utc)
    app = client.app
    session = app.state.session_factory()
    try:
        run = Run(
            mode="manual",
            trigger="manual",
            status="success",
            requested_sites_json=json.dumps([site["id"]]),
            created_by="admin",
            summary_json="{}",
        )
        session.add(run)
        session.flush()
        session.add(
            SiteJob(
                run_id=run.id,
                site_id=site["id"],
                site_name="today-all-success",
                lane="general",
                status="success",
                finished_at=now,
            )
        )
        session.commit()
    finally:
        session.close()

    response = client.post("/api/v1/runs", json={"today_unsuccessful": True})
    assert response.status_code == 422
    assert "今日没有未成功" in response.json()["detail"]


def test_executor_restarts_task_twice_on_captcha_restart(client: TestClient) -> None:
    site = client.post(
        "/api/v1/sites",
        json={
            "name": "run-captcha-restart",
            "base_url": "https://pt.example.com",
            "steps": [
                {"step_key": "open", "feature_type": "flow.open"},
                {"step_key": "captcha", "feature_type": "challenge.image_captcha"},
            ],
        },
    ).json()

    app = client.app
    session = app.state.session_factory()
    try:
        run = ExecutionService(session, app.state.settings, app.state.crypto).create_run(
            RunCreate(site_ids=[site["id"]]), "admin"
        )
        manager = ExecutionManager(
            session_factory=app.state.session_factory,
            settings=app.state.settings,
            crypto=app.state.crypto,
        )
        attempts: list[int] = []

        async def fake_execute(payload):
            attempts.append(int(payload["attempt"]))
            return {
                "status": "restart",
                "message": "图形验证码置信度不足，且没有可用的刷新方式",
                "steps": [],
            }

        manager._post_execute = fake_execute  # type: ignore[method-assign]
        asyncio.run(manager._execute_run(run.id))

        session.expire_all()
        job = session.query(SiteJob).filter_by(run_id=run.id).one()
        assert attempts == [1, 2, 3]
        assert job.attempt == 3
        assert job.status == "error"
        assert "已重新开始 2 次" in job.message
    finally:
        session.close()
