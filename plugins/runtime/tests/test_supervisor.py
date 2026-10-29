from __future__ import annotations

import json
from pathlib import Path

import grpc
import httpx
import pytest

from siteflow_proto import plugin_pb2, plugin_pb2_grpc
from siteflow_plugin_runtime.config import Config
from siteflow_plugin_runtime.control import PluginState
from siteflow_plugin_runtime.manifest import ManifestError, load_manifest
from siteflow_plugin_runtime.registration import RegistrationClient
from siteflow_plugin_runtime.server import build_server

MANIFEST = {
    "id": "acme",
    "name": "Acme Plugin",
    "version": "1.0.0",
    "protocol_version": 1,
    "capabilities": ["features", "captcha_methods"],
}
TOKEN_METADATA = (("x-plugin-token", "acme-token"),)


def test_secret_rpc_authorization_and_redaction(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from siteflow_plugin_runtime.secrets import SecretProviderServicer
    config = make_config(tmp_path)
    state = PluginState.load(config)
    calls = []
    def handler(request):
        calls.append(request)
        raise RuntimeError("private-provider-secret")
    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    plugin_pb2_grpc.add_SecretProviderServicer_to_server(SecretProviderServicer(state, handler, handler), server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = plugin_pb2_grpc.SecretProviderStub(channel)
            request = plugin_pb2.SecretProviderRequest(job_id=1, site_id=1, step_key="login", site_origin="http://demo.test")
            for metadata in [(), TOKEN_METADATA]:
                with pytest.raises(grpc.RpcError):
                    stub.ResolveCredential(request, metadata=metadata, timeout=2)
            assert calls == []
            state.manifest["capabilities"].append("secret.credentials")
            with pytest.raises(grpc.RpcError) as error:
                stub.ResolveCredential(request, metadata=TOKEN_METADATA, timeout=2)
            assert error.value.details() == "Secret provider failed"
            assert len(calls) == 1
            with pytest.raises(grpc.RpcError):
                stub.GenerateTotp(request, metadata=TOKEN_METADATA, timeout=2)
            assert len(calls) == 1
    finally:
        server.stop(0).wait()


def make_config(tmp_path: Path, *, register_enabled: bool = False) -> Config:
    token_file = tmp_path / "plugin-token"
    token_file.write_text("acme-token", encoding="utf-8")
    manifest_path = tmp_path / "plugin.json"
    manifest_path.write_text(json.dumps(MANIFEST), encoding="utf-8")
    features_file = tmp_path / "features.json"
    features_file.write_text(
        json.dumps(
            {
                "features": [
                    {
                        "key": "plugin.acme.checkin",
                        "name": "Acme Checkin",
                        "category": "sign",
                        "version": 1,
                        "config_schema": {"type": "object"},
                        "requires": ["auth.form_login"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    captcha_file = tmp_path / "captcha.json"
    captcha_file.write_text(
        json.dumps(
            {
                "methods": [
                    {
                        "key": "plugin.acme.gd5",
                        "label": "Acme GD5",
                        "description": "Acme captcha",
                        "supports_vision": False,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return Config(
        control_url="http://api:8000",
        plugin_id="acme",
        endpoint="plugin-acme:7000",
        token_file=str(token_file),
        manifest_path=str(manifest_path),
        heartbeat_seconds=20,
        bind="127.0.0.1",
        port=0,
        features_file=str(features_file),
        captcha_methods_file=str(captcha_file),
        register_enabled=register_enabled,
    )


@pytest.fixture
def grpc_client(tmp_path: Path):
    config = make_config(tmp_path)
    state = PluginState.load(config)
    server, port = build_server(config, state, port=0)
    server.start()
    channel = grpc.insecure_channel(
        f"127.0.0.1:{port}",
        options=(("grpc.enable_http_proxy", 0),),
    )
    try:
        yield plugin_pb2_grpc.PluginControlStub(channel)
    finally:
        channel.close()
        server.stop(0).wait()


def test_health_and_metadata(grpc_client) -> None:
    health = grpc_client.Health(plugin_pb2.Empty(), metadata=TOKEN_METADATA)
    assert health.ok is True
    assert health.plugin_id == "acme"
    assert health.version == "1.0.0"
    assert health.protocol_version == 1

    manifest = grpc_client.GetManifest(
        plugin_pb2.Empty(), metadata=TOKEN_METADATA
    )
    assert manifest.id == "acme"
    assert list(manifest.capabilities) == ["features", "captcha_methods"]

    features = grpc_client.ListFeatures(
        plugin_pb2.Empty(), metadata=TOKEN_METADATA
    )
    assert [item.key for item in features.features] == ["plugin.acme.checkin"]
    assert features.features[0].requires == ["auth.form_login"]
    assert json.loads(features.features[0].config_schema_json) == {"type": "object"}

    methods = grpc_client.ListCaptchaMethods(
        plugin_pb2.Empty(), metadata=TOKEN_METADATA
    )
    assert [item.key for item in methods.methods] == ["plugin.acme.gd5"]


def test_control_requires_plugin_token(grpc_client) -> None:
    with pytest.raises(grpc.RpcError) as missing:
        grpc_client.Health(plugin_pb2.Empty())
    assert missing.value.code() == grpc.StatusCode.UNAUTHENTICATED

    with pytest.raises(grpc.RpcError) as wrong:
        grpc_client.Health(
            plugin_pb2.Empty(), metadata=(("x-plugin-token", "wrong"),)
        )
    assert wrong.value.code() == grpc.StatusCode.UNAUTHENTICATED


def test_reload_reads_updated_manifest(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    state = PluginState.load(config)
    server, port = build_server(config, state, port=0)
    server.start()
    channel = grpc.insecure_channel(
        f"127.0.0.1:{port}",
        options=(("grpc.enable_http_proxy", 0),),
    )
    try:
        stub = plugin_pb2_grpc.PluginControlStub(channel)
        updated = {**MANIFEST, "version": "1.1.0"}
        Path(config.manifest_path).write_text(json.dumps(updated), encoding="utf-8")
        result = stub.Reload(
            plugin_pb2.ReloadRequest(reason="test"), metadata=TOKEN_METADATA
        )
        assert result.ok is True
        assert result.version == "1.1.0"
        assert stub.Health(plugin_pb2.Empty(), metadata=TOKEN_METADATA).version == "1.1.0"
    finally:
        channel.close()
        server.stop(0).wait()


def test_manifest_id_must_match(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    with pytest.raises(ManifestError) as exc:
        load_manifest(config.manifest_path, "other")
    assert "does not match" in str(exc.value)


def test_registration_lifecycle(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    requests: list[tuple[str, dict, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append((request.url.path, payload, request.headers["x-plugin-token"]))
        if request.url.path.endswith("/register"):
            return httpx.Response(201, json={"generation": 3})
        if request.url.path.endswith("/heartbeat"):
            return httpx.Response(200, json={"plugin_id": "acme", "generation": 3})
        if request.url.path.endswith("/unregister"):
            return httpx.Response(204)
        return httpx.Response(404)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    registrar = RegistrationClient(config, MANIFEST, http_client=http_client)
    assert registrar.register() == 3
    assert registrar.heartbeat(3) is True
    registrar.unregister(3)
    http_client.close()

    assert [item[0] for item in requests] == [
        "/api/v1/plugin-runtime/register",
        "/api/v1/plugin-runtime/heartbeat",
        "/api/v1/plugin-runtime/unregister",
    ]
    assert all(item[2] == "acme-token" for item in requests)
    assert requests[0][1]["endpoint"] == "plugin-acme:7000"
    assert requests[0][1]["manifest"]["id"] == "acme"
    assert requests[1][1]["generation"] == 3
    assert requests[2][1]["generation"] == 3


def test_heartbeat_rejection_requests_reregistration(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    registrar = RegistrationClient(config, MANIFEST, http_client=http_client)
    assert registrar.heartbeat(1) is False
    http_client.close()


def test_execution_and_captcha_handlers(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    state = PluginState.load(config)
    calls: list[dict] = []

    def execute_handler(request, broker):
        calls.append(request)
        assert broker.url == "http://worker:6300/plugin-broker"
        assert broker.token == "broker-token"
        return {
            "status": "success",
            "message": "step ok",
            "data": {"feature": request["feature_key"]},
        }

    def captcha_handler(request):
        calls.append(request)
        return {"text": "AB12", "confidence": 0.9, "margin": 3, "meta": {"x": 1}}

    server, port = build_server(
        config,
        state,
        port=0,
        execute_handler=execute_handler,
        captcha_handler=captcha_handler,
    )
    server.start()
    channel = grpc.insecure_channel(
        f"127.0.0.1:{port}",
        options=(("grpc.enable_http_proxy", 0),),
    )
    try:
        stub = plugin_pb2_grpc.PluginExecutionStub(channel)
        result = stub.ExecuteStep(
            plugin_pb2.StepRequest(
                execution_id="exec-1",
                plugin_id="acme",
                feature_key="plugin.acme.checkin",
                config_json='{"selector":["#sign"]}',
                site_origin="https://pt.example.com",
                current_url="https://pt.example.com/",
                deadline_ms=5000,
                broker=plugin_pb2.BrokerInfo(
                    url="http://worker:6300/plugin-broker",
                    token="broker-token",
                ),
            ),
            metadata=TOKEN_METADATA,
        )
        assert result.status == "success"
        assert json.loads(result.data_json) == {"feature": "plugin.acme.checkin"}
        captcha = stub.RecognizeCaptcha(
            plugin_pb2.CaptchaRequest(
                method_key="plugin.acme.gd5",
                image=b"image",
                charset="alnum",
                length=4,
            ),
            metadata=TOKEN_METADATA,
        )
        assert captcha.text == "AB12"
        assert captcha.confidence == pytest.approx(0.9)
        assert json.loads(captcha.meta_json) == {"x": 1}
    finally:
        channel.close()
        server.stop(0).wait()

    assert calls[0]["feature_key"] == "plugin.acme.checkin"
    assert calls[0]["config"] == {"selector": ["#sign"]}
    assert calls[1]["method_key"] == "plugin.acme.gd5"
    assert calls[1]["length"] == 4


def test_demo_handler_uses_broker() -> None:
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "examples" / "demo" / "demo_plugin.py"
    spec = importlib.util.spec_from_file_location("demo_plugin", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    class FakeBroker:
        def __init__(self) -> None:
            self.calls = []

        def action(self, op, **payload):
            self.calls.append((op, payload))
            if op == "click":
                return {"ok": True}
            return {"ok": True, "text": "签到成功"}

    broker = FakeBroker()
    result = module.execute_step(
        {
            "feature_key": "plugin.demo.demo_checkin",
            "config": {
                "button_selector": ["#sign"],
                "success_selector": ["#success"],
            },
        },
        broker,
    )
    assert result["status"] == "success"
    assert broker.calls[0] == ("click", {"selectors": ["#sign"]})
    assert broker.calls[1] == ("read_text", {"selectors": ["#success"]})
    assert result["data"]["text"] == "签到成功"
