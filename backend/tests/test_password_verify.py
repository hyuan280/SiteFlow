import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.features.catalog import AuthFormLoginConfig, AuthPasswordVerifyConfig
from app.models.execution import Run, SiteJob
from app.models.plugin import PluginRegistration, utcnow
from app.models.workflow import Site
from app.schemas.execution import RunCreate
from app.services.execution import ExecutionService
from app.services.plugin_runtime import PluginRuntimeService
from app.services.secret_providers import SecretDenied, step_provider


pytestmark = pytest.mark.usefixtures("fake_dispatch")

PURPOSE = "auth.password_verify"
DEFAULTS = {
    "password_selector": ["input[name='password']"],
    "submit_selector": ["button[type='submit']"],
    "success_selector": [],
}


def test_password_verify_catalog(client):
    features = {item["key"]: item for item in client.get("/api/v1/features").json()}
    feature = features[PURPOSE]
    assert feature["name"] == "密码验证"
    assert feature["category"] == "auth"
    assert feature["cardinality"] == "multiple"
    assert feature["requires"] == []
    assert feature["defaults"] == DEFAULTS
    schema = feature["config_schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == set(DEFAULTS)
    assert "不证明验证成功" in feature["description"]
    assert "留空仅提交，不证明验证成功" in schema["properties"]["success_selector"]["description"]
    for name, field in schema["properties"].items():
        assert any("\u4e00" <= char <= "\u9fff" for char in field["title"])
        assert any("\u4e00" <= char <= "\u9fff" for char in field["description"])
        assert field["type"] == "array"
        assert field["items"]["minLength"] == 1
        if name != "success_selector":
            assert field["minItems"] == 1
    assert AuthPasswordVerifyConfig().model_dump() == DEFAULTS
    assert not issubclass(AuthPasswordVerifyConfig, AuthFormLoginConfig)
    assert features["auth.form_login"]["cardinality"] == "single"
    assert features["auth.form_login"]["defaults"] == AuthFormLoginConfig().model_dump()


@pytest.mark.parametrize("config", [
    {"password": "plaintext"},
    {"username": "plaintext"},
    {"username_selector": ["#username"]},
    {"username_field": "username"},
    {"password_field": "password"},
    {"totp_provider_id": "builtin.twofauth"},
    {"password_selector": []},
    {"submit_selector": []},
    {"password_selector": [" "]},
    {"submit_selector": [""]},
    {"success_selector": [" "]},
    {"password_selector": "#password"},
])
def test_password_verify_rejects_invalid_config(client, config):
    with pytest.raises(ValidationError):
        AuthPasswordVerifyConfig.model_validate(config)
    response = client.post("/api/v1/sites", json={
        "name": "invalid-password-verify", "base_url": "https://example.test",
        "steps": [{"step_key": "verify", "feature_type": PURPOSE, "config": config}],
    })
    assert response.status_code == 422, response.text


def test_password_verify_allows_multiple_steps_and_candidates(client):
    response = client.post("/api/v1/sites", json={
        "name": "multiple-password-verify", "base_url": "https://example.test",
        "steps": [
            {"step_key": "first", "feature_type": PURPOSE},
            {"step_key": "second", "feature_type": PURPOSE, "config": {
                "password_selector": [" #password ", "input[type='password']"],
                "submit_selector": ["#verify", "button"],
                "success_selector": ["#verified"],
            }},
        ],
    })
    assert response.status_code == 201, response.text
    steps = response.json()["resolved"]["steps"]
    assert [step["step_key"] for step in steps] == ["first", "second"]
    assert steps[0]["config"] == DEFAULTS
    assert steps[1]["config"] == {
        "password_selector": ["#password", "input[type='password']"],
        "submit_selector": ["#verify", "button"],
        "success_selector": ["#verified"],
    }


@pytest.mark.parametrize("provider", ["builtin.manual", "builtin.bitwarden", "plugin.vault"])
def test_password_verify_executor_payload(client, monkeypatch, provider):
    monkeypatch.setattr(PluginRuntimeService, "_tokens", lambda self: {"vault": "private-plugin-token"})
    response = client.post("/api/v1/sites", json={
        "name": "password-verify-payload", "base_url": "https://example.test",
        "credential_provider_id": provider,
        "steps": [
            {"step_key": "verify", "feature_type": PURPOSE},
            {"step_key": "disabled", "feature_type": PURPOSE, "enabled": False},
        ],
    })
    assert response.status_code == 201, response.text
    site = response.json()
    assert client.put(f"/api/v1/sites/{site['id']}/credential", json={
        "username": "private-user", "password": "private-password",
    }).status_code == 200
    app = client.app
    manager = app.state.executor
    with app.state.session_factory() as session:
        if provider == "plugin.vault":
            session.add(PluginRegistration(
                plugin_id="vault", name="Vault", version="1", protocol_version=1,
                endpoint="plugin-vault:7000", capabilities_json='["secret.credentials"]',
                approved_capabilities_json='["secret.credentials"]', approved_at=utcnow(),
            ))
            session.commit()
        run = ExecutionService(session, app.state.settings, app.state.crypto).create_run(
            RunCreate(site_ids=[site["id"]]), "admin",
        )
        run_id = run.id
    seen = {}

    async def execute(payload):
        seen.update(payload)
        assert payload["secret_access"]["providers"] == {"verify": provider, "disabled": provider}
        assert payload["steps"][0]["config"] == DEFAULTS
        assert payload["steps"][1]["enabled"] is False
        assert payload["plugin_runtimes"] == {}
        assert "credentials" not in payload
        assert all(value not in str(payload) for value in (
            "private-user", "private-password", "private-plugin-token",
        ))
        args = (payload["secret_access"]["token"], payload["job_id"], site["id"],
                "verify", PURPOSE, provider)
        assert "lease" in await manager.secret_providers.request(*args)
        with pytest.raises(SecretDenied):
            await manager.secret_providers.request(*args[:3], "disabled", PURPOSE, provider)
        return {"status": "success", "message": "ok", "steps": []}

    monkeypatch.setattr(manager, "_post_execute", execute)
    asyncio.run(manager._execute_run(run_id))
    assert seen
    with app.state.session_factory() as session:
        assert session.get(Run, run_id).status == "success"
    with pytest.raises(SecretDenied):
        asyncio.run(manager.secret_providers.request(
            seen["secret_access"]["token"], seen["job_id"], site["id"], "verify", PURPOSE, provider,
        ))


@pytest.mark.parametrize("capabilities", [["secret.credentials"], ["secret.totp"]])
def test_password_verify_plugin_references_require_credentials(client, capabilities):
    response = client.post("/api/v1/sites", json={
        "name": "password-verify-references", "base_url": "https://example.test",
        "credential_provider_id": "plugin.vault",
        "steps": [{"step_key": "verify", "feature_type": PURPOSE}],
    })
    assert response.status_code == 201, response.text
    with client.app.state.session_factory() as session:
        session.add(PluginRegistration(
            plugin_id="vault", name="Vault", version="1", protocol_version=1,
            endpoint="plugin-vault:7000", capabilities_json=json.dumps(capabilities),
        ))
        session.commit()
        report = PluginRuntimeService(session, client.app.state.settings).references("vault")
    assert len(report["items"]) == 1
    item = report["items"][0]
    assert item["owner_type"] == "site" and item["owner_id"] == response.json()["id"]
    assert item["enabled"] is True
    assert item["reference"] == "credentials"
    assert item["errors"] == ([] if "secret.credentials" in capabilities else ["capability_removed"])


def test_password_verify_never_uses_totp_provider(client, monkeypatch):
    response = client.post("/api/v1/sites", json={
        "name": "password-verify-no-totp", "base_url": "https://example.test",
        "steps": [{"step_key": "verify", "feature_type": PURPOSE}],
    })
    assert response.status_code == 201, response.text
    app = client.app
    providers = app.state.executor.secret_providers
    step = SimpleNamespace(enabled=True, step_key="verify", feature_type=PURPOSE,
                           config={"totp_provider_id": "builtin.twofauth"})
    with app.state.session_factory() as session:
        site = session.get(Site, response.json()["id"])
        assert step_provider(site, step) == "builtin.manual"
        run = ExecutionService(session, app.state.settings, app.state.crypto).create_run(
            RunCreate(site_ids=[site.id]), "admin",
        )
        job = session.query(SiteJob).filter_by(run_id=run.id).one()
        # Even an invalid persisted provider must not enter the TOTP resolver.
        site.credential_provider_id = "builtin.twofauth"
        session.commit()
        token = providers.register(job, site, [step])
        args = (token, job.id, site.id, "verify", PURPOSE, "builtin.twofauth")

    def fail(*args, **kwargs):
        pytest.fail("password verification must not call TOTP")

    monkeypatch.setattr("app.services.secret_providers.httpx.AsyncClient", fail)
    with pytest.raises(SecretDenied):
        asyncio.run(providers.request(*args))
