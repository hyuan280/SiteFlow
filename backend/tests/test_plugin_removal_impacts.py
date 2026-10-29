import json
import pytest
from app.models.plugin import PluginRegistration
from app.models.setting import AppSetting
from app.models.workflow import Template, Site, WorkflowStep
from app.services.plugin_impacts import references, snapshot_change
from tests.test_plugin_runtime_api import plugin_client, register_payload, HEADERS
from tests.helpers import login_client
from tests.test_secret_providers import grant
from app.services.secret_providers import SecretDenied
from app.services.plugin_runtime import PluginRuntimeService


def test_tombstone_restore(tmp_path):
    client = plugin_client(tmp_path)
    path = "/api/v1/plugin-runtime/plugins/acme"
    client.post("/api/v1/plugin-runtime/register", json=register_payload(), headers=HEADERS)
    assert client.get(f"{path}/references").status_code == 401
    assert client.get(f"{path}/change-impacts").status_code == 401
    assert client.post(f"{path}/remove").status_code == 401
    login_client(client)
    assert client.post(f"{path}/remove", headers={"X-CSRF-Token": "bad"}).status_code == 403
    assert client.post(f"{path}/remove").json()["status"] == "removed"
    restarted = plugin_client(tmp_path)
    assert restarted.post("/api/v1/plugin-runtime/register", json=register_payload(), headers=HEADERS).status_code == 409
    assert client.post("/api/v1/plugin-runtime/heartbeat", json={"plugin_id": "acme"}, headers=HEADERS).json()["status"] == "removed"
    for action in ("enable", "approve", "disable", "check", "revoke"):
        assert client.post(f"{path}/{action}", json={}).status_code == 409
    assert client.post(f"{path}/restore").json()["status"] == "unapproved"
    assert client.post(f"{path}/approve", json={}).json()["status"] == "available"


@pytest.mark.parametrize("change", ["remove", "type", "required", "compatible"])
def test_effective_schema(tmp_path, change):
    client = plugin_client(tmp_path)
    feature = {"key": "plugin.acme.work", "name": "Work", "category": "action", "version": 1, "config_schema": {"type": "object", "properties": {"value": {"type": "string"}, "inherited": {"type": "string"}}, "required": ["value", "inherited"]}}
    with client.app.state.session_factory() as session:
        plugin = PluginRegistration(plugin_id="acme", name="Acme", version="1", protocol_version=1, endpoint="acme:7000", features_json=json.dumps([feature]))
        parent = Template(name="parent")
        session.add_all([plugin, parent])
        session.flush()
        child = Template(name="child", parent_id=parent.id)
        session.add(child)
        session.flush()
        site = Site(name="site", base_url="https://example.test", template_id=child.id, credential_provider_id="plugin.acme")
        session.add(site)
        step = WorkflowStep(owner_type="template", owner_id=parent.id, step_key="work", feature_type=feature["key"], feature_version=1, category="action", enabled=False, config_json='{"inherited":"private-password"}')
        session.add_all([step, AppSetting(key="feature.plugin.acme.work", value_json='{"defaults":{"value":"private-default"},"required_fields":["value","inherited"]}')])
        session.commit()
        if change == "type":
            feature["config_schema"]["properties"]["value"]["type"] = "integer"
        if change == "required":
            feature["config_schema"]["required"].append("new")
        if change == "compatible":
            feature["config_schema"]["properties"]["optional"] = {"type": "boolean"}
        report = references(session, plugin, [] if change == "remove" else [feature], ["secret.credentials"], [])
        work = [item for item in report["items"] if item["step_key"] == "work"]
        assert len(work) == 3 and all(not item["enabled"] for item in work)
        assert all(bool(item["errors"]) == (change != "compatible") for item in work)
        assert report["reference_count"] == 4
        assert "private-" not in json.dumps(report)
        snapshot_change(session, plugin, [] if change == "remove" else [feature], ["secret.credentials"], [])
        session.commit()
        assert json.loads(plugin.previous_metadata_json)["features"]
        assert "private-" not in plugin.change_impacts_json
        assert step.config_json == '{"inherited":"private-password"}'


def test_inherited_totp_and_bitwarden_fallback(tmp_path):
    client = plugin_client(tmp_path)
    with client.app.state.session_factory() as session:
        plugin = PluginRegistration(plugin_id="acme", name="Acme", version="1", protocol_version=1, endpoint="acme:7000")
        parent = Template(name="totp")
        session.add_all([plugin, parent])
        session.flush()
        session.add_all([Site(name="fallback", base_url="https://example.test", template_id=parent.id, bitwarden_enabled=True), WorkflowStep(owner_type="template", owner_id=parent.id, step_key="otp", feature_type="challenge.2fa", feature_version=1, category="challenge", config_json='{}'), AppSetting(key="feature.challenge.2fa", value_json='{"defaults":{"totp_provider_id":"plugin.acme","service_hint":"private-service"},"required_fields":["service_hint"]}')])
        session.commit()
        report = references(session, plugin)
        assert len(report["items"]) == 2
        assert all(item["reference"] == "totp" and item["errors"] == ["capability_removed"] for item in report["items"])
        assert "private-service" not in json.dumps(report)


def test_removed_plugin_denies_stale_runtime_grant(grant, client, monkeypatch):
    from app.models.execution import SiteJob
    from app.models.plugin import utcnow
    providers, args = grant
    monkeypatch.setattr(PluginRuntimeService, "_tokens", lambda self: {"acme": "private"})
    with client.app.state.session_factory() as session:
        session.add(PluginRegistration(plugin_id="acme", name="Acme", version="1", protocol_version=1, endpoint="acme:7000", capabilities_json='["features"]', approved_capabilities_json='["features"]', approved_at=utcnow()))
        session.commit()
        service = PluginRuntimeService(session, client.app.state.settings)
        runtime = service.runtime_for_plugin("acme")
        token = providers.register_plugins(session.get(SiteJob, args[1]), {"acme": runtime})
        service.remove("acme", "admin")
        with pytest.raises(SecretDenied):
            providers.authorize_plugin(token, args[1], "acme", runtime["generation"], "features")
        service.restore("acme", "admin")
        service.approve("acme", None, "admin")
        with pytest.raises(SecretDenied):
            providers.authorize_plugin(token, args[1], "acme", runtime["generation"], "features")
