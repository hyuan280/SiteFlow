import asyncio
import json
import zlib
from contextlib import asynccontextmanager, contextmanager
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import event, select

from app.models.auth import AuthSession
from app.models.integration import IntegrationConfig
from app.services import model_probe
from tests.test_model_registry import CONNECTION, PATH, extra, primary, registry, save


@pytest.fixture
def upstream(monkeypatch):
    calls = []
    options = []
    original = httpx.AsyncClient.__init__

    def init(self, *args, **kwargs):
        options.append(kwargs.copy())
        original(self, *args, **kwargs)

    async def send(self, request, **kwargs):
        calls.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(
            b'{"choices":[{"message":{"content":"OK"}}]}'), request=request)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)
    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    return calls, options


def snapshot(client):
    with client.app.state.session_factory() as session:
        rows = list(session.scalars(select(IntegrationConfig).order_by(IntegrationConfig.key)))
        return [(row.key, row.value_ciphertext, row.nonce, row.key_version, row.updated_at,
                 row.updated_by, row.fingerprint) for row in rows], session.scalar(select(AuthSession)).last_seen_at


@contextmanager
def readonly(client):
    with client.app.state.session_factory() as session:
        engine = session.get_bind()
    statements = []

    def guard(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")):
            pytest.fail("model test issued SQL write: " + statement)
        cursor.execute("PRAGMA query_only=ON")

    event.listen(engine, "before_cursor_execute", guard)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", guard)
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA query_only=OFF")


@pytest.mark.parametrize("target", ["primary", "secondary"])
@pytest.mark.parametrize("vision", [False, True])
@pytest.mark.parametrize("cap", [None, 0, 47, "omitted"])
def test_current_draft_exact_payload_and_no_writes(client, upstream, target, vision, cap):
    chosen = extra(enabled=False, usages=[], config={**CONNECTION, "max_tokens": 123})
    assert save(client, [extra(), primary(enabled=False, config={**CONNECTION, "max_tokens": 123}), chosen]).status_code == 200
    model_id = "primary" if target == "primary" else chosen["id"]
    config = {"base_url": "https://draft.example/v1/", "model": "draft-model", "supports_vision": vision}
    if cap != "omitted":
        config["max_tokens"] = cap
    before = snapshot(client)
    with readonly(client):
        response = client.request("POST", f"{PATH}/{model_id}/test", json={"expected_revision": 1, "config": config})
    assert response.status_code == 200, response.text
    assert response.json()["ok"]
    assert response.json()["data"]["mode"] == ("vision" if vision else "text")
    assert response.headers["cache-control"] == "no-store"
    assert snapshot(client) == before
    calls, options = upstream
    request, = calls
    assert str(request.url) == "https://draft.example/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer " + CONNECTION["api_key"]
    assert request.headers["accept-encoding"] == "identity"
    body = json.loads(request.content)
    content = ([{"type": "text", "text": "Describe this image in a few words."},
                {"type": "image_url", "image_url": {"url": model_probe._IMAGE}}]
               if vision else "Reply with the single word OK.")
    assert body == {"model": "draft-model", "messages": [{"role": "user", "content": content}],
                    **({"max_tokens": 123 if cap == "omitted" else cap} if cap else {})}
    assert options[0] == {"timeout": 30, "trust_env": False, "follow_redirects": False}
    assert CONNECTION["api_key"] not in response.text


@pytest.mark.parametrize("payload", [
    {}, {"config": {}}, {"expected_revision": True, "config": {}},
    {"expected_revision": -1, "config": {}}, {"expected_revision": 1},
    {"expected_revision": 1, "config": {"api_key": "private-injected-key"}},
    {"expected_revision": 1, "config": {"secret-unknown": "private-injected-key"}},
    {"expected_revision": 1, "config": {"max_tokens": True}},
    {"expected_revision": 1, "config": {"max_tokens": "4"}},
    {"expected_revision": 1, "config": {"max_tokens": -1}},
    {"expected_revision": 1, "config": {"base_url": "https://user:private-injected-key@example.com/v1"}},
    {"expected_revision": 1, "config": {"model": None}},
    {"expected_revision": 1, "config": {"model": " "}},
    {"expected_revision": 1, "config": {"supports_vision": None}},
    {"expected_revision": 1, "config": {"timeout": 300}},
])
def test_invalid_draft_static_no_network_or_write(client, upstream, payload):
    assert save(client, [primary(config=CONNECTION)]).status_code == 200
    before = snapshot(client)
    with readonly(client):
        response = client.request("POST", PATH + "/primary/test", json=payload)
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "invalid_model_registry"}}
    assert not upstream[0] and snapshot(client) == before


@pytest.mark.parametrize("raw,status", [
    ('{"expected_revision":1,"expected_revision":1,"config":{}}', 422),
    ('{"expected_revision":1,"config":{"model":"x","model":"private-injected-key"}}', 422),
    ('{"expected_revision":NaN,"config":{}}', 422),
    ('{"expected_revision":1,"config":{}} trailing-private-injected-key', 422),
    ('null', 422), ('[]', 422), (' ', 422), ('x' * (16 * 1024 + 1), 413),
])
def test_strict_and_bounded_body(client, upstream, raw, status):
    response = client.request("POST", PATH + "/primary/test", content=raw,
                              headers={"Content-Type": "application/json"})
    assert response.status_code == status
    assert "private-injected-key" not in response.text
    assert not upstream[0]


def test_stale_missing_incomplete_and_revision_zero(client, upstream):
    for revision in (0, 1):
        if revision:
            assert save(client, [primary()]).status_code == 200
        with readonly(client):
            response = client.request("POST", PATH + "/primary/test",
                                      json={"expected_revision": revision, "config": {"model": "draft"}})
        assert response.status_code == (422 if revision else 404)
    assert client.request("POST", PATH + "/primary/test", json={"expected_revision": 0, "config": {}}).status_code == 409
    assert client.request("POST", PATH + f"/{uuid4()}/test", json={"expected_revision": 1, "config": {}}).status_code == 404
    assert not upstream[0]


@pytest.mark.parametrize("field,value", [
    ("base_url", CONNECTION["base_url"]), ("model", CONNECTION["model"]),
    ("supports_vision", False), ("max_tokens", 0),
])
def test_env_locks_reject_even_same_value_only_for_primary(client, upstream, field, value):
    chosen = extra()
    assert save(client, [primary(config=CONNECTION), chosen]).status_code == 200
    setattr(client.app.state.settings, "vision_" + field, value)
    before = snapshot(client)
    with readonly(client):
        response = client.request("POST", PATH + "/primary/test", json={"expected_revision": 1, "config": {field: value}})
    assert response.status_code == 409 and not upstream[0]
    assert client.request("POST", f"{PATH}/{chosen['id']}/test", json={"expected_revision": 1, "config": {field: value}}).json()["ok"]
    assert snapshot(client) == before


def test_revision_zero_saved_env_key_and_empty_body_compatibility(client, upstream):
    settings = client.app.state.settings
    settings.vision_api_key = SecretStr(CONNECTION["api_key"])
    settings.vision_base_url = CONNECTION["base_url"]
    settings.vision_model = CONNECTION["model"]
    before = snapshot(client)
    with readonly(client):
        for kwargs in ({}, {"content": b""}, {"json": {"expected_revision": 0, "config": {}}}):
            assert client.request("POST", PATH + "/primary/test", **kwargs).json()["ok"]
    assert snapshot(client) == before
    assert len(upstream[0]) == 3


@pytest.mark.parametrize("source", ["db", "env"])
@pytest.mark.parametrize("cap", [2**31, 2**53 - 1])
def test_unchanged_legacy_primary_large_cap_readonly(client, upstream, source, cap):
    response = client.put("/api/v1/integrations/vision", json={**CONNECTION, "max_tokens": cap})
    assert response.status_code == 200
    if source == "env":
        client.app.state.settings.vision_max_tokens = cap
    before = snapshot(client)
    with readonly(client):
        view = client.get(PATH).json()
        assert view["items"][0]["config"]["max_tokens"]["value"] == cap
        assert view["items"][0]["config"]["max_tokens"]["source"] == source
        count = 0
        for config in (None, {}, {"model": "draft-model"}):
            kwargs = {} if config is None else {"json": {
                "expected_revision": view["revision"], "config": config,
            }}
            response = client.request("POST", PATH + "/primary/test", **kwargs)
            if source == "env" and config:
                assert response.status_code == 409
                continue
            count += 1
            assert response.status_code == 200 and response.json()["ok"], response.text
            body = json.loads(upstream[0][-1].content)
            assert body["max_tokens"] == cap
            assert body["model"] == ("draft-model" if config else CONNECTION["model"])
        assert len(upstream[0]) == count
        response = client.request("POST", PATH + "/primary/test", json={
            "expected_revision": view["revision"], "config": {"max_tokens": cap},
        })
        assert response.status_code == 422
        assert response.json() == {"detail": {"code": "invalid_model_registry"}}
        assert len(upstream[0]) == count
        for reset in (None, 0):
            response = client.request("POST", PATH + "/primary/test", json={
                "expected_revision": view["revision"], "config": {"max_tokens": reset},
            })
            if source == "env":
                assert response.status_code == 409
                assert response.json() == {"detail": {"code": "model_config_locked"}}
                assert len(upstream[0]) == count
            else:
                assert response.status_code == 200 and response.json()["ok"]
                assert "max_tokens" not in json.loads(upstream[0][-1].content)
    assert snapshot(client) == before
    assert save(client, [primary(config={"max_tokens": cap})], view["revision"]).status_code == 422
    assert snapshot(client) == before


@pytest.mark.parametrize("status,body,stage", [
    (401, {"error": {"code": "invalid_api_key", "message": "private-response"}}, "http"),
    (429, {"error": {"code": "insufficient_quota", "type": "rate_limit_error", "message": "private-response"}}, "http"),
    (404, {"error": {"code": "model_not_found"}}, "http"),
    (500, "<html>private-response</html>", "http"),
    (302, "private-response", "http"),
    (200, "<html>private-response</html>", "invalid_json"),
    (200, "{bad-private-response", "invalid_json"),
    (200, [], "invalid_response"),
    (200, {"error": {"message": "private-response"}}, "invalid_response"),
    (200, {"choices": []}, "invalid_response"),
    (200, {"choices": [{"message": {"reasoning_content": "private-response"}}]}, "invalid_response"),
    (200, {"choices": [{"message": {"content": "<think>private-response</think>"}}]}, "invalid_response"),
    (200, {"choices": [{"message": {"content": "<think>private-response"}}]}, "invalid_response"),
    (200, {"choices": [{"finish_reason": "length", "message": {"content": "private-response"}}]}, "output_length"),
    (200, {"choices": [{"finish_reason": "content_filter", "message": {"content": "private-response"}}]}, "content_filter"),
    (200, {"choices": [{"message": {"refusal": "private-response", "content": "OK"}}]}, "refusal"),
    (200, {"choices": [{"message": {"content": "private-response\u0000"}}]}, "invalid_response"),
])
def test_safe_detailed_failures_no_retry(client, upstream, monkeypatch, status, body, stage):
    assert save(client, [primary(config=CONNECTION)]).status_code == 200
    calls = []

    async def send(self, request, **kwargs):
        calls.append(request)
        raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        return httpx.Response(status, headers={"private-header": CONNECTION["api_key"],
                                              "Location": "https://redirect.example/v1"},
                              request=request, stream=httpx.ByteStream(raw))

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    before = snapshot(client)
    with readonly(client):
        response = client.request("POST", PATH + "/primary/test", json={"expected_revision": 1, "config": {}})
    result = response.json()
    assert response.status_code == 200 and not result["ok"]
    assert result["data"]["stage"] == stage and result["data"]["http_status"] == status
    assert len(calls) == 1 and snapshot(client) == before
    for secret in (*CONNECTION.values(), "private-response", "private-header", "redirect.example", model_probe._IMAGE):
        assert secret not in response.text
    if status in {401, 429, 404}:
        assert result["data"]["provider_error"]["code"] == body["error"]["code"]
    assert len(result["message"]) <= 500 and len(response.text) <= 1500


@pytest.mark.parametrize("kind,phase", [(httpx.ConnectTimeout, "connect"), (httpx.ReadTimeout, "read"),
                                        (httpx.WriteTimeout, "write"), (httpx.PoolTimeout, "pool"),
                                        (TimeoutError, "total"), (httpx.ConnectError, None)])
def test_timeout_and_connection_details(client, upstream, monkeypatch, kind, phase):
    assert save(client, [primary(config=CONNECTION)]).status_code == 200

    async def send(*args, **kwargs):
        raise kind("private-response " + CONNECTION["api_key"])

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    response = client.request("POST", PATH + "/primary/test")
    data = response.json()["data"]
    assert not response.json()["ok"]
    if phase:
        assert data["timeout_phase"] == phase and data["timeout_seconds"] == 30
    else:
        assert data["stage"] == "connection"
    assert "private-response" not in response.text and CONNECTION["api_key"] not in response.text


def test_total_deadline_and_response_limit(client, upstream, monkeypatch):
    assert save(client, [primary(config=CONNECTION)]).status_code == 200
    client.app.state.settings.vision_timeout = 0.01

    async def slow(*args, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(httpx.AsyncClient, "send", slow)
    response = client.request("POST", PATH + "/primary/test")
    assert response.json()["data"]["timeout_phase"] == "total"
    assert response.json()["data"]["timeout_seconds"] == 0.01

    async def huge(self, request, **kwargs):
        return httpx.Response(200, stream=httpx.ByteStream(b"x" * (64 * 1024 + 1)), request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", huge)
    assert client.request("POST", PATH + "/primary/test").json()["data"]["stage"] == "response_limit"


@pytest.mark.parametrize("content", ["private-completion", [{"type": "text", "text": "private-completion"}]])
def test_completion_never_returned(client, upstream, monkeypatch, content):
    assert save(client, [primary(config=CONNECTION)]).status_code == 200

    async def send(self, request, **kwargs):
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(
            {"choices": [{"message": {"content": content}}]}).encode()), request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    response = client.request("POST", PATH + "/primary/test")
    assert response.json()["ok"] and "private-completion" not in response.text


def test_draft_auth_and_csrf(client, anon_client, upstream):
    payload = {"expected_revision": 0, "config": {}}
    assert anon_client.request("POST", PATH + "/primary/test", json=payload).status_code == 401
    client.headers.pop("X-CSRF-Token")
    assert client.request("POST", PATH + "/primary/test", json=payload).status_code == 403
    assert not upstream[0]


def test_request_body_timeout_static(client, upstream, monkeypatch):
    @asynccontextmanager
    async def timeout(seconds):
        assert seconds == 10
        raise TimeoutError("private-injected-key")
        yield

    monkeypatch.setattr(asyncio, "timeout", timeout)
    response = client.request("POST", PATH + "/primary/test", content=b'{"expected_revision":0}')
    assert response.status_code == 408
    assert response.json() == {"detail": {"code": "request_timeout"}}
    assert not upstream[0]


def test_stream_limit_closes_without_consuming_rest(client, upstream, monkeypatch):
    assert save(client, [primary(config=CONNECTION)]).status_code == 200
    chunks = []
    closed = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for index in range(20):
                chunks.append(index)
                yield b"x" * 8192

        async def aclose(self):
            closed.append(True)

    async def send(self, request, **kwargs):
        assert kwargs["stream"] is True
        return httpx.Response(200, stream=Stream(), request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    with readonly(client):
        response = client.request("POST", PATH + "/primary/test")
    assert response.json()["data"]["stage"] == "response_limit"
    assert len(chunks) == 9 and closed == [True]


def test_slow_trickle_has_total_deadline_and_closes(client, upstream, monkeypatch):
    assert save(client, [primary(config=CONNECTION)]).status_code == 200
    client.app.state.settings.vision_timeout = 0.03
    closed = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.005)
                yield b"x"

        async def aclose(self):
            closed.append(True)

    async def send(self, request, **kwargs):
        return httpx.Response(200, stream=Stream(), request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    response = client.request("POST", PATH + "/primary/test")
    assert response.json()["data"]["timeout_phase"] == "total"
    assert closed == [True]


@pytest.mark.parametrize("field,bad,repair", [
    ("base_url", "invalid-private-endpoint", CONNECTION["base_url"]),
    ("max_tokens", "invalid-private-cap", None),
    ("supports_vision", "invalid-private-bool", False),
])
def test_primary_draft_repairs_nonkey_field_without_saving(client, upstream, field, bad, repair):
    assert save(client, [primary(config=CONNECTION)]).status_code == 200
    with client.app.state.session_factory() as session:
        registry(client, session).primary._upsert("vision." + field, bad, "test")
        session.commit()
    before = snapshot(client)
    with readonly(client):
        response = client.request("POST", PATH + "/primary/test",
                                  json={"expected_revision": 1, "config": {field: repair}})
    assert response.status_code == 200 and response.json()["ok"]
    assert snapshot(client) == before and len(upstream[0]) == 1


@pytest.mark.parametrize("encoding", ["gzip", "GZip", "deflate", "br", "identity, gzip", "", "unknown-private-encoding"])
@pytest.mark.parametrize("status", [200, 429])
def test_encoded_response_rejected_before_reading_or_decoding(client, upstream, monkeypatch, encoding, status):
    assert save(client, [primary(config=CONNECTION)]).status_code == 200
    # Represent a 32 MiB gzip bomb without ever allocating its expanded body.
    compressor = zlib.compressobj(wbits=31)
    compressed = b"".join(compressor.compress(b"x" * 8192) for _ in range(4096)) + compressor.flush()
    assert len(compressed) < 64 * 1024
    closed = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            pytest.fail("encoded response body must not be read")
            yield compressed

        async def aclose(self):
            closed.append(True)

    def decode():
        pytest.fail("response must not instantiate a decompressor")

    async def send(self, request, **kwargs):
        assert request.headers["accept-encoding"] == "identity"
        response = httpx.Response(status, headers={"Content-Encoding": encoding},
                                  stream=Stream(), request=request)
        monkeypatch.setattr(response, "_get_content_decoder", decode)
        return response

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    before = snapshot(client)
    with readonly(client):
        response = client.request("POST", PATH + "/primary/test")
    result = response.json()
    assert response.status_code == 200 and not result["ok"]
    assert result["data"]["stage"] == "invalid_response"
    assert result["data"]["http_status"] == status
    assert "unknown-private-encoding" not in response.text
    assert CONNECTION["api_key"] not in response.text
    assert closed == [True] and snapshot(client) == before


@pytest.mark.parametrize("encoding", [None, "identity", "IDENTITY", " identity "])
def test_identity_response_reads_raw_without_decoder(client, upstream, monkeypatch, encoding):
    assert save(client, [primary(config=CONNECTION)]).status_code == 200

    def decode():
        pytest.fail("identity response must also bypass transparent decoding")

    async def send(self, request, **kwargs):
        response = httpx.Response(200, headers={"Content-Encoding": encoding} if encoding else {},
                                  stream=httpx.ByteStream(b'{"choices":[{"message":{"content":"OK"}}]}'),
                                  request=request)
        monkeypatch.setattr(response, "_get_content_decoder", decode)
        return response

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    assert client.request("POST", PATH + "/primary/test").json()["ok"]
