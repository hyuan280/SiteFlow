import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import inspect

from tests.test_plugin_runtime_api import plugin_client, register_payload, HEADERS
from tests.helpers import login_client
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from worker.plugin_broker import PluginBroker, BrokerDenied, BrokerNotFound
from worker.executor import _run_plugin_step
from tests.test_worker_plugin_broker import FakePage
from app.services.secret_providers import SecretDenied
from app.services.plugin_runtime import PluginRuntimeService
from tests.test_secret_providers import grant
from app.models.plugin import PluginAuditEvent
from sqlalchemy import select


def test_audit_auth_csrf_pagination_redaction_and_revoke(tmp_path):
    client = plugin_client(tmp_path)
    assert client.get("/api/v1/plugin-runtime/audit").status_code == 401
    client.post("/api/v1/plugin-runtime/register", json=register_payload(), headers=HEADERS)
    login_client(client)
    client.post("/api/v1/plugin-runtime/plugins/acme/approve", json={})
    assert client.post("/api/v1/plugin-runtime/plugins/acme/revoke", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    response = client.post("/api/v1/plugin-runtime/plugins/acme/revoke")
    assert response.status_code == 200
    assert response.json()["approved_capabilities"] == []
    assert response.json()["feature_count"] == 0
    data = client.get("/api/v1/plugin-runtime/audit?plugin_id=acme&page_size=1").json()
    assert data["total"] == 3 and len(data["items"]) == 1
    assert data["items"][0]["event"] == "revoke"
    assert "acme-token" not in json.dumps(data)
    assert client.get("/api/v1/plugin-runtime/audit?page_size=101").status_code == 422
    assert "plugin_audit_events" in inspect(client.app.state.engine).get_table_names()


def test_broker_denies_missing_capability_and_active_revocation():
    async def run():
        broker = PluginBroker("http://worker/plugin-broker")
        page = FakePage()
        with pytest.raises(BrokerDenied):
            broker.register(page, site_origin="https://pt.example.com", approved_capabilities=["features"], authorize=AsyncMock())
        check = AsyncMock()
        token = broker.register(page, site_origin="https://pt.example.com", approved_capabilities=["browser_actions"], authorize=check)
        await broker.execute({"token": token, "op": "query_selector", "selector": "#success"})
        check.side_effect = RuntimeError("revoked")
        with pytest.raises(BrokerDenied):
            await broker.execute({"token": token, "op": "click", "selector": "#sign"})
        assert page.clicked == []
        with pytest.raises(BrokerNotFound):
            await broker.execute({"token": token, "op": "click", "selector": "#sign"})
    asyncio.run(run())


def test_non_browser_step_has_no_broker(monkeypatch):
    monkeypatch.setattr("worker.plugin_authorization.authorize", AsyncMock())
    def execute(*args, **kwargs):
        assert kwargs["broker_token"] == kwargs["broker_url"] == ""
        assert "authorization" not in kwargs
        return {"status": "success"}
    monkeypatch.setattr("worker.plugin_client.execute_step", execute)
    result = asyncio.run(_run_plugin_step(FakePage(), {}, {}, "plugin.acme.work", {"acme": {"approved_capabilities": ["features"]}}, None, lambda *args: None))
    assert result[0] == "success"


def test_scoped_authorization_stop_enable_and_revoke(grant, client, monkeypatch):
    providers, args = grant
    from app.models.plugin import PluginRegistration, utcnow
    from app.models.execution import SiteJob
    monkeypatch.setattr(PluginRuntimeService, "_tokens", lambda self: {"acme": "private-plugin-token"})
    with client.app.state.session_factory() as session:
        session.add(PluginRegistration(plugin_id="acme", name="Acme", version="1", protocol_version=1, endpoint="acme:7000", capabilities_json='["features", "browser_actions"]', approved_capabilities_json='["features", "browser_actions"]', approved_at=utcnow()))
        session.commit()
        service = PluginRuntimeService(session, client.app.state.settings)
        runtime = service.runtime_for_plugin("acme")
        token = providers.register_plugins(session.get(SiteJob, args[1]), {"acme": runtime})
        providers.authorize_plugin(token, args[1], "acme", runtime["generation"], "browser_actions")
        service.disable("acme", "admin")
        with pytest.raises(SecretDenied):
            providers.authorize_plugin(token, args[1], "acme", runtime["generation"], "browser_actions")
        service.enable("acme", "admin")
        with pytest.raises(SecretDenied):
            providers.authorize_plugin(token, args[1], "acme", runtime["generation"], "browser_actions")
        service.revoke("acme", "admin")


def test_persistent_audit_redacts_rejected_scope(grant, client):
    providers, args = grant
    with pytest.raises(SecretDenied):
        asyncio.run(providers.request("private-token", args[1], args[2], "private-password", "private-otp", "private-provider"))
    with client.app.state.session_factory() as session:
        rows = session.scalars(select(PluginAuditEvent)).all()
        serialized = json.dumps([{c.name: str(getattr(row, c.name)) for c in PluginAuditEvent.__table__.columns} for row in rows])
        assert "lease_reject" in serialized
        assert "private-" not in serialized


def test_active_step_revokes_broker_and_cancels_rpc(monkeypatch):
    import threading
    entered = threading.Event()
    cancelled = threading.Event()
    checks = [0]
    async def authorize(*args):
        checks[0] += 1
        if checks[0] > 1:
            raise RuntimeError("revoked")
    def execute(*args, cancel_event, **kwargs):
        entered.set()
        if cancel_event.wait(3):
            cancelled.set()
        return {"status": "success"}
    monkeypatch.setattr("worker.plugin_authorization.authorize", authorize)
    monkeypatch.setattr("worker.plugin_client.execute_step", execute)
    broker = PluginBroker("http://worker/plugin-broker")
    result = asyncio.run(_run_plugin_step(FakePage(), {}, {}, "plugin.acme.work", {"acme": {"approved_capabilities": ["features", "browser_actions"]}}, broker, lambda *args: None))
    assert entered.is_set() and cancelled.is_set()
    assert result[0] == "fail" and broker._sessions == {}
