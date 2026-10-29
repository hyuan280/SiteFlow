from fastapi.testclient import TestClient
from sqlalchemy import inspect, select

from app.main import create_app
from app.models.plugin import PluginRegistration
from app.models.setting import AppSetting
from app.models.workflow import Site, SiteCredential, WorkflowGroup, WorkflowStep
from tests.helpers import login_client


def test_empty_database_startup_creates_current_schema_and_defaults(settings, monkeypatch):
    async def no_background_work(*args):
        pass

    # Exercise the real lifespan without contacting a worker or running retention jobs.
    monkeypatch.setattr("app.services.scheduler.Scheduler.start", no_background_work)
    monkeypatch.setattr("app.services.scheduler.Scheduler.stop", no_background_work)
    monkeypatch.setattr("app.services.artifacts.ArtifactCleanupRunner.start", no_background_work)
    monkeypatch.setattr("app.services.artifacts.ArtifactCleanupRunner.stop", no_background_work)
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/api/v1/sites").status_code == 401
        login_client(client)
        features = client.get("/api/v1/features").json()
        captcha = next(item for item in features if item["key"] == "challenge.image_captcha")
        assert captcha["defaults"]["captcha_method"] == "ddddocr"
        assert captcha["execution_settings"]["llm_prompt"]
        response = client.post("/api/v1/sites", json={
            "name": "Fresh site", "base_url": "https://example.com",
            "groups": [{"group_key": "sign", "name": "Sign", "position": 1000000,
                        "step_anchor": 1000}],
            "steps": [{"step_key": "open", "feature_type": "flow.open",
                       "group_key": "sign", "position": 1000}],
        })
        assert response.status_code == 201, response.text
        site_id = response.json()["id"]
        with app.state.session_factory() as session:
            site = session.get(Site, site_id)
            assert site.support_resign is True
            assert site.resign_max_days == 30
            assert site.credential_provider_id is None
            assert site.bitwarden_enabled is False
            assert site.bitwarden_match_mode == "domain"
            group = session.scalars(select(WorkflowGroup)).one()
            assert (group.position, group.step_anchor) == (1000000, 1000)
            assert session.scalars(select(WorkflowStep)).one().group_key == "sign"
            session.add(SiteCredential(site_id=site_id, username_ciphertext=b"", username_nonce=b"",
                                       password_ciphertext=b"", password_nonce=b""))
            plugin = PluginRegistration(plugin_id="fresh", name="Fresh", version="1",
                                        protocol_version=1, endpoint="fresh:7000")
            session.add(plugin)
            session.commit()
            assert plugin.status == "unapproved"
            assert plugin.approved_capabilities_json == "[]"
            assert plugin.approved_at is None
            assert plugin.permission_epoch == 1
            assert plugin.previous_metadata_json == plugin.change_impacts_json == "{}"
            assert session.get(SiteCredential, site_id).cookie_count == 0
            assert not session.scalars(select(AppSetting).where(AppSetting.key.like("migration.%"))).all()
        columns = {column["name"] for column in inspect(app.state.engine).get_columns("site_credentials")}
        assert {"cookies_ciphertext", "cookies_nonce", "cookies_updated_at"} <= columns
    # Reopening the same current database must not rewrite workflow ordering or approval.
    with TestClient(create_app(settings)) as client:
        login_client(client)
        assert client.get(f"/api/v1/sites/{site_id}").status_code == 200
        with client.app.state.session_factory() as session:
            assert session.scalars(select(WorkflowGroup)).one().step_anchor == 1000
            assert session.get(PluginRegistration, "fresh").approved_at is None
