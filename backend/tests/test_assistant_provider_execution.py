import json
from copy import deepcopy
from types import SimpleNamespace

import grpc
import pytest

from app.models.assistant import AssistantSession
from app.models.plugin import PluginRegistration, utcnow
from app.models.setting import AppSetting
from app.models.workflow import Site, WorkflowStep
from app.services.assistant_provider_policy import authorized_providers, provider_binding, provider_policy_key
from app.services.plugin_runtime import PluginRuntimeService
from tests.test_assistant_execution import (
    BASE, PASSWORD, USERNAME, arguments, bind, execution, response, run, save_target,
)


def configure(helper, capability="secret.credentials"):
    site_id = bind(helper)
    key = provider_policy_key("plugin.vault", capability)
    with helper.manager.session_factory() as db:
        db.add(PluginRegistration(plugin_id="vault", name="Vault", version="1", protocol_version=1,
            endpoint="vault:7000", capabilities_json=json.dumps([capability]), approved_at=utcnow(),
            approved_capabilities_json=json.dumps([capability])))
        db.add(AppSetting(key=f"assistant.feature_policy.{key}",
                          value_json='{"allowed":true,"revision":1}'))
        if capability == "secret.credentials":
            db.get(Site, site_id).credential_provider_id = "plugin.vault"
        db.commit()
    save_target(helper, {**helper.target, "credential_mode": "site"})
    return site_id, key


def mock_rpc(monkeypatch, helper, *, pending=None, code="193827"):
    from app.services import secret_providers as module

    captured = []

    class Call:
        def __init__(self):
            self.waited = False
            self.canceled = False

        def result(self, timeout):
            if pending and not self.waited:
                self.waited = True
                pending()
                raise grpc.FutureTimeoutError()
            return SimpleNamespace(username=USERNAME, password=PASSWORD, code=code)

        def cancel(self):
            self.canceled = True

    call = Call()

    class RPC:
        def future(self, request, **kwargs):
            captured.append(request)
            return call

    class Channel:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(PluginRuntimeService, "_tokens", lambda self: {"vault": "private-plugin-token"})
    monkeypatch.setattr(module, "validate_plugin_endpoint", lambda *args, **kwargs: "vault:7000")
    monkeypatch.setattr(module.grpc, "insecure_channel", lambda *args, **kwargs: Channel())
    monkeypatch.setattr(module.plugin_pb2_grpc, "SecretProviderStub", lambda channel: SimpleNamespace(
        ResolveCredential=RPC(), GenerateTotp=RPC()))
    return captured, call


def test_site_plugin_credentials_private_rpc_provenance_and_auth_budget(execution, monkeypatch):
    site_id, key = configure(execution)
    captured, call = mock_rpc(monkeypatch, execution)
    payloads = []

    async def worker(path, payload):
        payloads.append(deepcopy(payload))
        return response(execution.manager.browsers[execution.session_id], payload["operation_id"])

    execution.manager._worker.side_effect = worker
    args = arguments("auth.form_login")
    result = run(execution, args)
    assert result["ok"], result
    assert payloads[0]["credentials"] == {"username": USERNAME, "password": PASSWORD}
    assert captured[0].job_id == 0 and captured[0].site_id == site_id
    assert captured[0].step_key == f"assistant-operation:{args['operation_id']}"
    assert captured[0].site_origin == BASE
    assert call.canceled
    assert USERNAME not in json.dumps(result) and PASSWORD not in json.dumps(result)
    with execution.manager.session_factory() as db:
        data = execution.manager._decrypt(db.get(AssistantSession, execution.session_id))
        records = data["_execution_records"]
        assert len(records) == 1 and records[args["operation_id"]]["category"] == "auth"
        assert records[args["operation_id"]]["feature_bindings"][key]["policy"]["revision"] == 1
        assert USERNAME not in json.dumps(data) and PASSWORD not in json.dumps(data)


def test_plugin_totp_only_private_code(execution, monkeypatch):
    configure(execution, "secret.totp")
    captured, _ = mock_rpc(monkeypatch, execution)
    payloads = []

    async def worker(path, payload):
        payloads.append(deepcopy(payload))
        return response(execution.manager.browsers[execution.session_id], payload["operation_id"])

    execution.manager._worker.side_effect = worker
    result = run(execution, arguments("challenge.2fa", {
        "totp_provider_id": "plugin.vault", "service_hint": "Example"}))
    assert result["ok"], result
    assert payloads[0]["private_runtime"] == {"totp_code": "193827"}
    assert "credentials" not in payloads[0]
    assert captured[0].service_hint == "Example"
    assert "193827" not in json.dumps(result)


@pytest.mark.parametrize("change", ["policy", "revision", "generation", "epoch", "site", "target"])
def test_pending_rpc_revocation_discards_secret_without_fallback(execution, monkeypatch, change):
    site_id, key = configure(execution)

    def revoke():
        with execution.manager.session_factory() as db:
            if change in {"policy", "revision"}:
                db.get(AppSetting, f"assistant.feature_policy.{key}").value_json = json.dumps({
                    "allowed": change != "policy", "revision": 2})
            elif change in {"generation", "epoch"}:
                row = db.get(PluginRegistration, "vault")
                if change == "generation":
                    row.generation += 1
                else:
                    row.permission_epoch += 1
            elif change == "site":
                db.get(Site, site_id).config_revision += 1
            else:
                row = db.get(AssistantSession, execution.session_id)
                data = execution.manager._decrypt(row)
                data["target"]["credential_consent"] = False
                execution.manager._encrypt(row, data)
            db.commit()

    _, call = mock_rpc(monkeypatch, execution, pending=revoke)
    result = run(execution, arguments("auth.form_login"))
    assert not result["ok"] and PASSWORD not in json.dumps(result)
    execution.manager._worker.assert_not_awaited()
    assert call.canceled


def test_provider_catalog_requires_both_approvals(execution):
    _, key = configure(execution)
    with execution.manager.session_factory() as db:
        assert authorized_providers(db, "secret.credentials") == [
            {"id": "plugin.vault", "name": "Vault", "capability": "secret.credentials"}]
        assert provider_binding(db, "plugin.vault", "secret.credentials")["generation"] == 1
        db.get(AppSetting, f"assistant.feature_policy.{key}").value_json = '{"allowed":false,"revision":2}'
        db.flush()
        assert authorized_providers(db, "secret.credentials") == []


def test_plugin_totp_output_leak_is_rejected(execution, monkeypatch):
    configure(execution, "secret.totp")
    mock_rpc(monkeypatch, execution)

    async def worker(path, payload):
        raw = response(execution.manager.browsers[execution.session_id], payload["operation_id"])
        raw["logs"] = ["193827"]
        return raw

    execution.manager._worker.side_effect = worker
    assert run(execution, arguments("challenge.2fa", {
        "totp_provider_id": "plugin.vault", "service_hint": "Example"}))["code"] == "secret_output"


def test_site_plugin_workflow_accepted_and_guard_revoked(execution, monkeypatch):
    import asyncio
    from unittest.mock import Mock
    from app.services.assistant_run_guard import validate_guard
    from app.services.assistant_workflows import AssistantWorkflows
    from app.models.execution import Run, SiteJob
    from app.services.secret_providers import SecretProviders, SecretDenied
    from app.services.workflows import WorkflowService

    site_id, key = configure(execution)
    monkeypatch.setattr(PluginRuntimeService, "_tokens", lambda self: {"vault": "private-plugin-token"})
    with execution.manager.session_factory() as db:
        db.add_all([
            WorkflowStep(owner_type="site", owner_id=site_id, step_key="login",
                         feature_type="auth.form_login", category="auth", position=1),
            WorkflowStep(owner_type="site", owner_id=site_id, step_key="result",
                         feature_type="flow.result", category="flow", position=2),
        ])
        db.commit()
    execution.manager.browsers.clear()
    execution.manager.executor = Mock()
    save_target(execution, {**execution.target, "workflow_consent": True,
        "workflow_site_id": site_id, "_workflow_binding": execution.target["_credential_binding"]})
    result = asyncio.run(AssistantWorkflows(execution.manager, execution.owner,
        execution.session_id, execution.target).run_site())
    assert result["ok"], result
    providers = SecretProviders(execution.manager.session_factory, execution.manager.settings,
                                execution.manager.crypto)
    with execution.manager.session_factory() as db:
        site = db.get(Site, site_id)
        assert db.get(Run, result["run_id"]).trigger == "assistant"
        assert validate_guard(db, result["run_id"], site, execution.manager.settings, execution.manager.crypto)
        job = db.query(SiteJob).filter_by(run_id=result["run_id"]).one()
        steps = WorkflowService(db, execution.manager.settings, execution.manager.crypto).resolve_site(site_id).steps
        token = providers.register(job, site, steps, reserve_auth=lambda *args: None)
        job_id = job.id
    lease = asyncio.run(providers.request(token, job_id, site_id, "login", "auth.form_login", "plugin.vault"))
    with execution.manager.session_factory() as db:
        site = db.get(Site, site_id)
        db.get(AppSetting, f"assistant.feature_policy.{key}").value_json = '{"allowed":true,"revision":2}'
        db.commit()
        assert not validate_guard(db, result["run_id"], site, execution.manager.settings, execution.manager.crypto)
    with pytest.raises(SecretDenied):
        asyncio.run(providers.request(token, job_id, site_id, "login", "auth.form_login", "plugin.vault", lease["lease"]))
