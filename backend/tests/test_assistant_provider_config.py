import asyncio
import json

import pytest

from app.models.plugin import PluginRegistration, utcnow
from app.models.workflow import Site
from app.services.assistant_feature_policy import set_assistant_allowed
from app.services.assistant_interactions import PENDING
from tests.test_assistant_workflows import context, configure, make_site, call  # noqa: F401
from tests.test_assistant_site_config import args, reviewed, state


@pytest.fixture
def providers(context):
    manager, _ = context
    with manager.session_factory() as db:
        for identifier in ("vault", "hidden"):
            db.add(PluginRegistration(
                plugin_id=identifier, name=identifier.title(), version="1", protocol_version=1,
                endpoint="private-provider:7000", approved_at=utcnow(),
                capabilities_json='["secret.credentials", "secret.totp"]',
                approved_capabilities_json='["secret.credentials", "secret.totp"]',
                manifest_json='{"private_key":"never-expose"}'))
        db.flush()
        for capability in ("secret.credentials", "secret.totp"):
            set_assistant_allowed(db, f"plugin.vault.{capability}", True)
        db.commit()
    return manager


def test_provider_discovery_and_totp_schema_are_public_and_current(context, providers):
    tools = configure(context)
    result = call(tools, "list_secret_providers")
    assert result["ok"]
    assert {item["id"] for item in result["providers"]} == {
        "builtin.manual", "builtin.bitwarden", "builtin.twofauth", "plugin.vault"}
    assert all(set(item) == {"id", "name", "capability"} for item in result["providers"])
    assert "private-provider" not in json.dumps(result) and "never-expose" not in json.dumps(result)
    schema = call(tools, "get_feature_schema", {"feature_type": "challenge.2fa"})
    assert schema["config_schema"]["properties"]["totp_provider_id"]["enum"] == [
        "builtin.twofauth", "plugin.vault"]
    assert "password" not in call(tools, "get_feature_schema", {
        "feature_type": "auth.form_login"})["config_schema"]["properties"]
    with providers.session_factory() as db:
        set_assistant_allowed(db, "plugin.vault.secret.totp", False)
        db.commit()
    assert call(tools, "get_feature_schema", {"feature_type": "challenge.2fa"})[
        "config_schema"]["properties"]["totp_provider_id"]["enum"] == ["builtin.twofauth"]


@pytest.mark.parametrize("provider", ["plugin.vault", "builtin.manual", "builtin.bitwarden"])
def test_reviewed_provider_switch_rebinds_without_granting_consent(context, providers, provider):
    site = make_site(context)
    tools = configure(context, workflow_site_id=site.id, workflow_consent=False)
    before = state(tools)["target"]
    request = args({"credential_provider_id": provider})
    result, item = asyncio.run(reviewed(tools, request))
    assert result["ok"], result
    assert item["review"]["changes"] == [{
        "field": "credential_provider_id", "label": "Credential provider", "before": None,
        "after": provider}]
    after = state(tools)["target"]
    assert after["credential_mode"] == "site" and after["site_id"] == site.id
    assert after["_credential_binding"] == after["_workflow_binding"]
    for field in ("execution_consent", "credential_consent", "execution_features", "workflow_consent"):
        assert after.get(field) == before.get(field)
    with providers.session_factory() as db:
        saved = db.get(Site, site.id)
        assert saved.credential_provider_id == provider
        assert saved.bitwarden_enabled == (provider == "builtin.bitwarden")
        assert saved.config_revision == 2
    assert call(tools, "update_site_config", request)["replayed"]


@pytest.mark.parametrize("change", ["revoke", "reapprove", "generation", "policy_cycle", "site"])
def test_provider_review_rejects_revocation_and_stale_bindings(context, providers, change):
    site = make_site(context)
    tools = configure(context, workflow_site_id=site.id)

    def concurrent(_):
        with providers.session_factory() as db:
            row = db.get(PluginRegistration, "vault")
            if change == "revoke":
                row.approved_at = None
            elif change == "reapprove":
                row.permission_epoch += 2
                row.approved_at = utcnow()
            elif change == "generation":
                row.generation += 1
            elif change == "policy_cycle":
                set_assistant_allowed(db, "plugin.vault.secret.credentials", False)
                set_assistant_allowed(db, "plugin.vault.secret.credentials", True)
            else:
                db.get(Site, site.id).config_revision += 1
            db.commit()

    result, _ = asyncio.run(reviewed(tools, args({"credential_provider_id": "plugin.vault"}),
                                     before=concurrent))
    assert not result["ok"]
    assert result["code"] in {"credentials_unavailable", "provider_binding_changed", "workflow_binding_changed"}
    with providers.session_factory() as db:
        assert db.get(Site, site.id).credential_provider_id is None


def test_unauthorized_provider_and_raw_secrets_never_reach_review(context, providers):
    site = make_site(context)
    tools = configure(context, workflow_site_id=site.id)
    assert call(tools, "update_site_config", args({"credential_provider_id": "plugin.hidden"}))[
        "code"] == "credentials_unavailable"
    for patch in ({"credential_provider_id": None}, {"credential_provider_id": "builtin.twofauth"},
                  {"password": "private"}, {"credential_provider_id": "plugin.vault", "password": "private"}):
        assert call(tools, "update_site_config", args(patch))["code"] == "invalid_arguments"
    assert PENDING not in state(tools)


def test_denied_provider_switch_changes_neither_site_nor_target(context, providers):
    site = make_site(context)
    tools = configure(context, workflow_site_id=site.id)
    before = state(tools)["target"]
    tools.config["_assistant_settings"] = {"auto_authorize_workflow": True}
    result, _ = asyncio.run(reviewed(tools, args({"credential_provider_id": "plugin.vault"}),
                                     decision="deny"))
    assert result["code"] == "interaction_denied"
    assert state(tools)["target"] == before
    with providers.session_factory() as db:
        saved = db.get(Site, site.id)
        assert saved.credential_provider_id is None and saved.config_revision == 1
