import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.models.execution import SiteJob
from app.models.workflow import Site
from app.schemas.execution import RunCreate
from app.services.execution import ExecutionService
from app.services.secret_providers import SecretDenied


@pytest.fixture
def grant(client):
    site = client.post("/api/v1/sites", json={"name": "lease-test", "base_url": "https://example.test", "steps": [{"step_key": "login", "feature_type": "auth.form_login"}]}).json()
    client.put(f"/api/v1/sites/{site['id']}/credential", json={"username": "private-user", "password": "private-password"})
    app = client.app
    providers = app.state.executor.secret_providers
    with app.state.session_factory() as session:
        run = ExecutionService(session, app.state.settings, app.state.crypto).create_run(RunCreate(site_ids=[site["id"]]), "admin")
        job = session.query(SiteJob).filter_by(run_id=run.id).one()
        token = providers.register(job, session.get(Site, site["id"]), [SimpleNamespace(enabled=True, feature_type="auth.form_login", step_key="login", config={})])
        args = (token, job.id, site["id"], "login", "auth.form_login", "builtin.manual")
    return providers, args


def call(providers, args, **kwargs):
    return asyncio.run(providers.request(*args, **kwargs))


def test_one_use_and_no_plaintext_storage(grant):
    providers, args = grant
    lease = call(providers, args)["lease"]
    assert lease not in str(providers.leases)
    assert args[0] not in str(providers.jobs)
    assert call(providers, args, lease=lease) == {"username": "private-user", "password": "private-password"}
    assert "private-password" not in str(vars(providers))
    with pytest.raises(SecretDenied):
        call(providers, args, lease=lease)


@pytest.mark.parametrize("index,value", [(0, "invalid"), (1, 999), (2, 999), (3, "other"), (4, "challenge.2fa"), (5, "builtin.bitwarden")])
def test_wrong_scope(grant, index, value):
    providers, args = grant
    lease = call(providers, args)["lease"]
    wrong = list(args)
    wrong[index] = value
    with pytest.raises(SecretDenied):
        call(providers, wrong, lease=lease)
    assert call(providers, args, lease=lease)["password"] == "private-password"


def test_expired_and_revoked(grant, monkeypatch):
    import app.services.secret_providers as module
    providers, args = grant
    lease = call(providers, args)["lease"]
    now = module.time.monotonic()
    monkeypatch.setattr(module.time, "monotonic", lambda: now + 31)
    with pytest.raises(SecretDenied):
        call(providers, args, lease=lease)
    providers.revoke(args[1])
    with pytest.raises(SecretDenied):
        call(providers, args)


def test_atomic_consume(grant):
    providers, args = grant
    lease = call(providers, args)["lease"]
    def consume(_):
        try:
            call(providers, args, lease=lease)
            return True
        except SecretDenied:
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(consume, range(4))) == 1


def test_old_lease_cannot_cross_attempt_registration(grant, client):
    providers, args = grant
    lease = call(providers, args)["lease"]
    with client.app.state.session_factory() as session:
        token = providers.register(session.get(SiteJob, args[1]), session.get(Site, args[2]), [SimpleNamespace(enabled=True, feature_type="auth.form_login", step_key="login", config={})])
    with pytest.raises(SecretDenied):
        call(providers, (token, *args[1:]), lease=lease)


def test_internal_route(grant, client):
    providers, args = grant
    body = dict(zip(("job_id", "site_id", "step_key", "purpose", "provider"), args[1:]))
    path = "/api/v1/internal/secrets/resolve"
    assert client.post(path, json=body).status_code == 403
    headers = {"Authorization": f"Bearer {args[0]}"}
    response = client.post(path, json=body, headers=headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body["lease"] = response.json()["lease"]
    assert client.post(path, json=body, headers=headers).json()["password"] == "private-password"
    assert client.post(path, json=body, headers=headers).status_code == 403


@pytest.mark.parametrize("change", ["cancel", "attempt", "finished", "shutdown"])
def test_lifecycle_denied(grant, client, change):
    from app.models.execution import Run
    providers, args = grant
    lease = call(providers, args)["lease"]
    if change == "shutdown":
        providers.clear()
    else:
        with client.app.state.session_factory() as session:
            job = session.get(SiteJob, args[1])
            if change == "cancel":
                session.get(Run, job.run_id).cancel_requested = True
            elif change == "attempt":
                job.attempt += 1
            else:
                job.status = "success"
            session.commit()
    with pytest.raises(SecretDenied):
        call(providers, args, lease=lease)


def test_provider_failure_redacted_and_consumed(grant, monkeypatch):
    from app.services.credentials import CredentialService
    providers, args = grant
    def fail(*args):
        raise RuntimeError("private-password")
    monkeypatch.setattr(CredentialService, "resolve", fail)
    lease = call(providers, args)["lease"]
    with pytest.raises(SecretDenied) as error:
        call(providers, args, lease=lease)
    assert "private-password" not in str(error.value)
    with pytest.raises(SecretDenied):
        call(providers, args, lease=lease)


@pytest.mark.parametrize("ambiguous,username", [(False, ""), (True, ""), (True, "test")])
def test_twofauth_builtin_local_fake(grant, client, monkeypatch, ambiguous, username):
    import httpx
    from app.services.integrations import IntegrationService
    providers, args = grant
    monkeypatch.setattr(IntegrationService, "resolve_twofauth_url", lambda self: SimpleNamespace(value="https://fake.test"))
    monkeypatch.setattr(IntegrationService, "resolve_twofauth_token", lambda self: SimpleNamespace(value="provider-private-token"))
    calls = []
    original = httpx.AsyncClient
    def handle(request):
        calls.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer provider-private-token"
        if request.url.path.endswith("/otp"):
            return httpx.Response(200, json={"password": "123456"})
        accounts = [{"id": 1, "service": "PT", "account": "test"}]
        return httpx.Response(200, json=accounts + ([{"id": 2, "service": "PT", "account": "other"}] if ambiguous else []))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    with client.app.state.session_factory() as session:
        job = session.get(SiteJob, args[1])
        site = session.get(Site, args[2])
        token = providers.register(job, site, [SimpleNamespace(enabled=True, feature_type="challenge.2fa", step_key="totp", config={"service_hint": "PT", "username_hint": username})])
    args = (token, args[1], args[2], "totp", "challenge.2fa", "builtin.twofauth")
    lease = call(providers, args)["lease"]
    if ambiguous and not username:
        with pytest.raises(SecretDenied):
            call(providers, args, lease=lease)
        assert len(calls) == 1
    else:
        assert call(providers, args, lease=lease) == {"code": "123456"}
        assert len(calls) == 2


def test_bitwarden_legacy_options_snapshot(grant, client, monkeypatch):
    from app.services.bitwarden import BitwardenService
    providers, args = grant
    def resolve(self, site):
        assert site.base_url == "https://example.test"
        assert site.bitwarden_match_mode == "custom"
        assert site.bitwarden_custom_match == "saved-legacy-match"
        return SimpleNamespace(username="vault-user", password="vault-password")
    monkeypatch.setattr(BitwardenService, "resolve_for_site", resolve)
    with client.app.state.session_factory() as session:
        site = session.get(Site, args[2])
        site.bitwarden_enabled = True
        site.bitwarden_match_mode = "custom"
        site.bitwarden_custom_match = "saved-legacy-match"
        session.commit()
        token = providers.register(session.get(SiteJob, args[1]), site, [SimpleNamespace(enabled=True, feature_type="auth.form_login", step_key="login", config={})])
    args = (token, *args[1:5], "builtin.bitwarden")
    lease = call(providers, args)["lease"]
    assert call(providers, args, lease=lease) == {"username": "vault-user", "password": "vault-password"}


@pytest.mark.parametrize("state", ["approved", "unapproved", "stopped", "wrong-capability", "rpc-failure", "changed"])
def test_remote_security_and_redaction(grant, client, monkeypatch, caplog, state):
    import json
    from app.models.plugin import PluginRegistration, utcnow
    from app.services.plugin_runtime import PluginRuntimeService
    import app.services.secret_providers as module
    providers, args = grant
    monkeypatch.setattr(PluginRuntimeService, "_tokens", lambda self: {"vault": "private-provider-token"})
    monkeypatch.setattr(module, "validate_plugin_endpoint", lambda endpoint, *args, **kwargs: endpoint)
    calls = []
    with client.app.state.session_factory() as session:
        caps = ["secret.totp"] if state == "wrong-capability" else ["secret.credentials"]
        session.add(PluginRegistration(plugin_id="vault", name="Vault", version="1", protocol_version=1,
            endpoint="plugin-vault:7000", capabilities_json=json.dumps(caps),
            approved_capabilities_json=json.dumps(caps) if state != "unapproved" else "[]",
            approved_at=utcnow() if state != "unapproved" else None, enabled=state != "stopped"))
        site = session.get(Site, args[2])
        site.credential_provider_id = "plugin.vault"
        site.bitwarden_match_mode = "custom"
        site.bitwarden_custom_match = "saved-plugin-account"
        session.commit()
        token = providers.register(session.get(SiteJob, args[1]), site,
            [SimpleNamespace(enabled=True, feature_type="auth.form_login", step_key="login", config={"endpoint": "evil:7000"})])
    args = (token, *args[1:5], "plugin.vault")

    class Channel:
        def __enter__(self): return self
        def __exit__(self, *args): pass

    def channel(endpoint, **kwargs):
        assert endpoint == "plugin-vault:7000"
        return Channel()

    class Stub:
        def __init__(self, channel):
            resolve = self.ResolveCredential
            self.ResolveCredential = SimpleNamespace(future=lambda *a, **kw: SimpleNamespace(
                result=lambda timeout: resolve(*a, **kw), cancel=lambda: None))
        def ResolveCredential(self, request, timeout, metadata):
            calls.append(request)
            assert request.match_mode == "custom"
            assert request.custom_match == "saved-plugin-account"
            assert timeout == 20
            assert metadata == (("x-plugin-token", "private-provider-token"),)
            assert not hasattr(request, "lease") and not hasattr(request, "token")
            if state == "rpc-failure":
                raise RuntimeError("private-provider-token private-remote-password")
            if state == "changed":
                with client.app.state.session_factory() as session:
                    session.get(PluginRegistration, "vault").generation += 1
                    session.commit()
            return SimpleNamespace(username="remote-user", password="private-remote-password")

    monkeypatch.setattr(module.grpc, "insecure_channel", channel)
    monkeypatch.setattr(module.plugin_pb2_grpc, "SecretProviderStub", Stub)
    if state in {"unapproved", "stopped", "wrong-capability"}:
        with pytest.raises(SecretDenied):
            call(providers, args)
        assert calls == []
        return
    lease = call(providers, args)["lease"]
    if state == "approved":
        assert call(providers, args, lease=lease)["password"] == "private-remote-password"
    else:
        with pytest.raises(SecretDenied) as error:
            call(providers, args, lease=lease)
        assert not str(error.value)
    assert len(calls) == (1 if state in {"approved", "rpc-failure", "changed"} else 0)
    assert "private-provider-token" not in caplog.text
    assert "private-remote-password" not in caplog.text
    with pytest.raises(SecretDenied):
        call(providers, args, lease=lease)


def test_legacy_twofa_policy_and_site_provider_roundtrip(client):
    import json
    from app.models.setting import AppSetting
    with client.app.state.session_factory() as session:
        session.add(AppSetting(key="feature.challenge.2fa", value_json=json.dumps({
            "defaults": {"service_hint": "legacy-account"}, "required_fields": ["service_hint"]})))
        session.commit()
    response = client.post("/api/v1/sites", json={"name": "provider-roundtrip", "base_url": "https://example.test",
        "credential_provider_id": "plugin.demo", "steps": [{"step_key": "otp", "feature_type": "challenge.2fa",
        "config": {"totp_provider_id": "plugin.demo", "service_hint": "Demo", "username_hint": "admin"}}]})
    assert response.status_code == 201, response.text
    assert response.json()["credential_provider_id"] == "plugin.demo"
    assert client.get(f"/api/v1/sites/{response.json()['id']}").json()["credential_provider_id"] == "plugin.demo"
    feature = next(f for f in client.get("/api/v1/features").json() if f["key"] == "challenge.2fa")
    assert feature["defaults"]["service_hint"] == "legacy-account"
    assert "service_hint" in feature["required_fields"]
    assert feature["config_schema"]["properties"]["service_hint"]["title"] == "2FA 服务名（必须）"
    assert feature["config_schema"]["properties"]["username_hint"]["title"] == "2FA 用户名（可选）"
    assert feature["config_schema"]["properties"]["totp_provider_id"]["title"] == "2FA 提供商"


@pytest.mark.parametrize("provider", ["builtin.twofauth", "plugin.demo"])
def test_all_totp_providers_require_service(client, provider):
    response = client.post("/api/v1/sites", json={"name": "required", "base_url": "https://example.test",
        "steps": [{"step_key": "otp", "feature_type": "challenge.2fa", "config": {"totp_provider_id": provider, "service_hint": "  "}}]})
    assert response.status_code == 422


def test_plugin_custom_matching_roundtrip(client):
    payload = {"name": "plugin-match", "base_url": "https://example.test", "credential_provider_id": "plugin.demo",
               "bitwarden_match_mode": "custom", "bitwarden_custom_match": " vault-account "}
    response = client.post("/api/v1/sites", json=payload)
    assert response.status_code == 201
    detail = client.get(f"/api/v1/sites/{response.json()['id']}").json()
    assert detail["bitwarden_custom_match"] == "vault-account"
    assert detail["bitwarden_match_mode"] == "custom"
    payload["bitwarden_custom_match"] = " "
    assert client.post("/api/v1/sites", json=payload).status_code == 422
