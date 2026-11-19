import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.auth import CSRF_COOKIE
from app.services.lanes import LaneService

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker.app import LaneBusyError, LaneManager, create_app  # noqa: E402
from worker.browser_backend import BrowserSession, ManagerBackend  # noqa: E402


PUBLIC = "/api/v1/lanes/general/clipboard"
INTERNAL = "/lanes/general/clipboard"
TOKEN = "clipboard-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
TEXT = "private-clipboard\n\u4e2d\u6587\r\n\U0001f680 e\u0301\t "
LIMIT = 1_048_576
BODY_LIMIT = 12 * LIMIT + 1024
REAL_ASYNC_CLIENT = httpx.AsyncClient


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk


def assert_private(response, status):
    assert response.status_code == status, response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert "private-clipboard" not in response.text
    assert TOKEN not in response.text


def test_public_requires_auth(anon_client, monkeypatch):
    async def unexpected(*args):
        pytest.fail("Unauthenticated request reached worker")

    monkeypatch.setattr(LaneService, "set_clipboard", unexpected)
    assert_private(anon_client.post(PUBLIC, json={"text": TEXT}), 401)


@pytest.mark.parametrize("csrf", ["missing", "wrong", "wrong-cookie"])
def test_public_requires_csrf(client, monkeypatch, csrf):
    async def unexpected(*args):
        pytest.fail("Invalid CSRF reached worker")

    monkeypatch.setattr(LaneService, "set_clipboard", unexpected)
    if csrf == "missing":
        client.headers.pop("X-CSRF-Token")
    elif csrf == "wrong":
        client.headers["X-CSRF-Token"] = "wrong"
    else:
        client.cookies.set(CSRF_COOKIE, "wrong")
    assert_private(client.post(PUBLIC, json={"text": TEXT}), 403)


@pytest.fixture
def worker_client(monkeypatch, tmp_path):
    monkeypatch.setenv("SITEFLOW_BROWSER_TOKEN", TOKEN)
    monkeypatch.setattr("worker.app.DATA_DIR", tmp_path)
    manager = LaneManager()
    lane = manager.get("general")
    lane.status = "running"
    lane.session = BrowserSession(None, None, "private/profile?\u4e2d", None, None)
    calls = []

    async def set_clipboard(session, text):
        assert session is lane.session
        assert lane.viewers
        calls.append(text)

    monkeypatch.setattr(manager.backend, "set_clipboard", set_clipboard)
    app = create_app()
    app.state.manager = manager
    # Deliberately do not enter lifespan: no live Manager/profile operations.
    yield SimpleNamespace(client=TestClient(app), manager=manager, lane=lane, calls=calls)
    asyncio.run(manager.backend.close())


@pytest.mark.parametrize("token,headers", [
    (TOKEN, {}), (TOKEN, {"Authorization": "Bearer incorrect"}),
    ("", AUTH), (TOKEN, {**AUTH, "Origin": "https://attacker.test"}),
    (TOKEN, {**AUTH, "Origin": ""}),
    (TOKEN, [("Authorization", f"Bearer {TOKEN}"), ("Authorization", f"Bearer {TOKEN}")]),
])
def test_worker_auth_before_body_or_lane(worker_client, monkeypatch, token, headers):
    monkeypatch.setenv("SITEFLOW_BROWSER_TOKEN", token)
    assert_private(worker_client.client.post(INTERNAL, headers=headers, content=TEXT), 403)
    assert not worker_client.calls
    assert not worker_client.lane.viewers


@pytest.mark.parametrize("text", ["", TEXT, "\U0001f680" * LIMIT])
@pytest.mark.parametrize("target", ["public", "worker"])
def test_exact_text_and_limit(client, monkeypatch, worker_client, text, target):
    calls = []

    async def set_clipboard(self, key, value):
        assert key == "general"
        calls.append(value)

    if target == "public":
        monkeypatch.setattr(LaneService, "set_clipboard", set_clipboard)
        response = client.post(PUBLIC, content=json.dumps({"text": text}))
        assert calls == [text]
    else:
        response = worker_client.client.post(INTERNAL, headers=AUTH, content=json.dumps({"text": text}))
        assert worker_client.calls == [text]
        assert not worker_client.lane.viewers
    assert_private(response, 200)
    assert response.json() == {"ok": True}


@pytest.mark.parametrize("body,status", [
    (b'{"text": "private-clipboard",', 422),
    (b'{"text": null}', 422), (b'{"text": 123}', 422),
    (b'{"text": ["private-clipboard"]}', 422), (b'{}', 422),
    (b'{"text": "private-clipboard", "text": ""}', 422),
    (b'{"text": "private-clipboard", "profile_id": "other"}', 422),
    (b'["private-clipboard"]', 422), (b'{"text": "\\ud800"}', 422),
    (b'{"text": "\xff"}', 422),
    (json.dumps({"text": "a" * (LIMIT + 1)}).encode(), 413),
    (b" " * (BODY_LIMIT + 1), 413),
])
@pytest.mark.parametrize("target", ["public", "worker"])
def test_validation_is_bounded_and_private(client, monkeypatch, worker_client, body, status, target):
    async def unexpected(*args):
        pytest.fail("Invalid clipboard text was forwarded")

    if target == "public":
        monkeypatch.setattr(LaneService, "set_clipboard", unexpected)
        response = client.post(PUBLIC, content=body)
    else:
        response = worker_client.client.post(INTERNAL, content=body, headers=AUTH)
        assert not worker_client.calls
    assert_private(response, status)


@pytest.mark.parametrize("state", ["stopped", "starting", "stopping", "disabled", "error", "disconnected", "no-session", "unknown", "shutdown"])
def test_worker_never_starts_unavailable_lane(worker_client, state):
    path = INTERNAL
    if state == "disconnected":
        worker_client.lane.session.disconnected = True
    elif state == "no-session":
        worker_client.lane.session = None
    elif state == "unknown":
        path = "/lanes/unknown/clipboard"
    elif state == "shutdown":
        worker_client.manager._stopping = True
    else:
        worker_client.lane.status = state
    assert_private(worker_client.client.post(path, json={"text": TEXT}, headers=AUTH),
                   404 if state == "unknown" else 409)
    assert not worker_client.calls
    assert not worker_client.lane.viewers


@pytest.mark.parametrize("failure,status", [(RuntimeError, 502), (TimeoutError, 504)])
def test_worker_errors_release_pin(worker_client, monkeypatch, failure, status):
    async def fail(*args):
        raise failure(TEXT)

    monkeypatch.setattr(worker_client.manager.backend, "set_clipboard", fail)
    assert_private(worker_client.client.post(INTERNAL, json={"text": TEXT}, headers=AUTH), status)
    assert not worker_client.lane.viewers


def test_clipboard_pin_blocks_stop_and_releases_on_cancellation(worker_client, monkeypatch):
    async def run():
        entered = asyncio.Event()

        async def wait(*args):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(worker_client.manager.backend, "set_clipboard", wait)
        task = asyncio.create_task(worker_client.manager.set_clipboard("general", TEXT))
        await entered.wait()
        with pytest.raises(LaneBusyError):
            await worker_client.manager.stop_lane("general")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not worker_client.lane.viewers

    asyncio.run(run())


@pytest.fixture
def relay(client, monkeypatch):
    client.app.state.settings.browser_token = SecretStr(TOKEN)
    client.app.state.settings.worker_url = "http://fixed-worker:6300"
    real_client = httpx.AsyncClient
    calls = []
    options = []

    def install(responder):
        def handle(request):
            calls.append(request)
            return responder(request)

        def factory(**kwargs):
            options.append(kwargs)
            return real_client(transport=httpx.MockTransport(handle), **kwargs)

        monkeypatch.setattr("app.services.lanes.httpx.AsyncClient", factory)

    install(lambda request: httpx.Response(200, json={"ok": True, "private": TEXT}))
    return SimpleNamespace(install=install, calls=calls, options=options)


def test_public_relay_is_authenticated_and_projects_success(client, relay):
    response = client.post(PUBLIC + "?profile_id=attacker", json={"text": TEXT}, headers={"Origin": "http://testserver"})
    assert_private(response, 200)
    assert response.json() == {"ok": True}
    request, = relay.calls
    assert str(request.url) == "http://fixed-worker:6300/lanes/general/clipboard"
    assert request.method == "POST"
    assert request.headers["Authorization"] == AUTH["Authorization"]
    assert "origin" not in request.headers and "cookie" not in request.headers
    assert json.loads(request.content) == {"text": TEXT}
    assert relay.options == [{"timeout": 15, "follow_redirects": False, "trust_env": False}]


@pytest.mark.parametrize("status,expected", [(404, 404), (409, 409), (413, 413), (422, 422), (504, 504), (401, 502), (403, 502), (500, 502), (307, 502)])
def test_public_worker_errors_are_not_read_or_leaked(client, relay, status, expected):
    stream = Chunks([TEXT.encode()])
    relay.install(lambda request: httpx.Response(status, stream=stream))
    assert_private(client.post(PUBLIC, json={"text": TEXT}), expected)
    assert stream.reads == 0
    assert len(relay.calls) == 1


@pytest.mark.parametrize("body", [b'{}', b'{"ok": false}', b'private-clipboard', b'x' * 4097])
def test_public_invalid_worker_success(client, relay, body):
    relay.install(lambda request: httpx.Response(200, stream=Chunks([body])))
    assert_private(client.post(PUBLIC, json={"text": TEXT}), 502)


@pytest.mark.parametrize("failure,status", [(httpx.ReadTimeout, 504), (httpx.ConnectError, 502)])
def test_public_transport_errors(client, relay, failure, status):
    def fail(request):
        raise failure(TEXT, request=request)

    relay.install(fail)
    assert_private(client.post(PUBLIC, json={"text": TEXT}), status)
    assert len(relay.calls) == 1


def test_public_unknown_lane_and_missing_token(client, relay):
    assert_private(client.post("/api/v1/lanes/unknown/clipboard", json={"text": TEXT}), 404)
    client.app.state.settings.browser_token = None
    assert_private(client.post(PUBLIC, json={"text": TEXT}), 503)
    assert not relay.calls


@pytest.mark.parametrize("text", ["", TEXT])
@pytest.mark.parametrize("status", [200, 204, 400, 500, 307])
def test_manager_adapter_exact_unicode_encoded_profile_and_private_errors(tmp_path, monkeypatch, text, status):
    monkeypatch.setenv("SITEFLOW_BROWSER_TOKEN", TOKEN)

    async def run():
        backend = ManagerBackend(tmp_path)
        await backend.client.aclose()
        calls = []
        stream = Chunks([TEXT.encode()])

        def handle(request):
            calls.append(request)
            return httpx.Response(status, stream=stream)

        backend.client = httpx.AsyncClient(base_url="http://manager", headers=backend.headers,
                                          transport=httpx.MockTransport(handle))
        try:
            session = BrowserSession(None, None, "id/123?\u4e2d", None, None)
            if status < 300:
                await backend.set_clipboard(session, text)
            else:
                with pytest.raises(RuntimeError, match="^Clipboard unavailable$"):
                    await backend.set_clipboard(session, text)
            request, = calls
            assert request.url.raw_path == b"/api/profiles/id%2F123%3F%E4%B8%AD/clipboard"
            assert request.method == "POST"
            assert json.loads(request.content) == {"text": text}
            assert request.headers["Authorization"] == AUTH["Authorization"]
            assert request.extensions["timeout"]["read"] == 10
            assert stream.reads == 0
        finally:
            await backend.close()

    asyncio.run(run())


@pytest.mark.parametrize("value", [None, 123, "a" * (LIMIT + 1), "\ud800"])
def test_manager_adapter_validates_before_request(tmp_path, value):
    async def run():
        backend = ManagerBackend(tmp_path)
        try:
            with pytest.raises(ValueError, match="^Invalid clipboard text$"):
                await backend.set_clipboard(BrowserSession(None, None, "id", None, None), value)
        finally:
            await backend.close()

    asyncio.run(run())


@pytest.mark.parametrize("target", ["public", "worker"])
@pytest.mark.parametrize("failure", ["oversize", "slow-body", "slow-write"])
def test_streaming_limits_and_deadlines(client, worker_client, monkeypatch, target, failure):
    real_timeout = asyncio.timeout

    def short_timeout(delay):
        return real_timeout(0.02 if failure != "oversize" else delay)

    async def run():
        reads = []

        async def body():
            reads.append(1)
            if failure == "oversize":
                yield b" " * BODY_LIMIT
                reads.append(2)
                yield b"x"
                pytest.fail("Read beyond body limit")
            elif failure == "slow-body":
                yield b'{"text":"private-clipboard'
                await asyncio.Event().wait()
            else:
                yield json.dumps({"text": TEXT}).encode()

        async def wait(*args):
            await asyncio.Event().wait()

        app = client.app if target == "public" else worker_client.client.app
        if failure == "slow-write":
            if target == "public":
                client.app.state.settings.browser_token = SecretStr(TOKEN)
                real_client = httpx.AsyncClient

                async def respond(request):
                    await wait()

                monkeypatch.setattr("app.services.lanes.httpx.AsyncClient",
                                    lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
            else:
                monkeypatch.setattr(worker_client.manager.backend, "set_clipboard", wait)
        monkeypatch.setattr(asyncio, "timeout", short_timeout)
        # The transport below must not use the patched worker-client factory.
        async with REAL_ASYNC_CLIENT(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                                     cookies=client.cookies, headers=client.headers if target == "public" else AUTH) as session:
            response = await session.post(PUBLIC if target == "public" else INTERNAL, content=body())
        assert_private(response, {"oversize": 413, "slow-body": 408, "slow-write": 504}[failure])
        assert reads == ([1, 2] if failure == "oversize" else [1])
        assert not worker_client.lane.viewers

    asyncio.run(run())


@pytest.mark.parametrize("failure,expected", [(httpx.ReadTimeout, TimeoutError), (httpx.ConnectError, RuntimeError)])
def test_manager_adapter_transport_errors_are_static(tmp_path, failure, expected):
    async def run():
        backend = ManagerBackend(tmp_path)
        await backend.client.aclose()

        def fail(request):
            raise failure(TEXT, request=request)

        backend.client = httpx.AsyncClient(base_url="http://manager", transport=httpx.MockTransport(fail))
        try:
            with pytest.raises(expected) as error:
                await backend.set_clipboard(BrowserSession(None, None, "id", None, None), TEXT)
            assert "private-clipboard" not in str(error.value)
        finally:
            await backend.close()

    asyncio.run(run())
