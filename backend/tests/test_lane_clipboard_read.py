import asyncio
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.lanes import LaneService
from tests.test_lane_clipboard import (
    AUTH, INTERNAL, LIMIT, PUBLIC, REAL_ASYNC_CLIENT, TEXT, TOKEN, Chunks,
    assert_private, relay, worker_client,  # noqa: F401
)
from worker.app import LaneBusyError
from worker.browser_backend import BrowserSession, ManagerBackend
from worker.desktop import X11Desktop


@pytest.fixture
def read_worker(worker_client, monkeypatch):
    async def get_clipboard(session):
        assert session is worker_client.lane.session
        assert worker_client.lane.viewers
        worker_client.calls.append(session.profile_id)
        return TEXT

    monkeypatch.setattr(worker_client.manager.backend, "get_clipboard", get_clipboard)
    return worker_client


@pytest.mark.parametrize("path", [PUBLIC, "/api/v1/lanes/unknown/clipboard"])
def test_read_public_auth_first(anon_client, monkeypatch, path):
    monkeypatch.setattr(LaneService, "get_clipboard", AsyncMock(side_effect=AssertionError))
    assert_private(anon_client.get(path), 401)
    LaneService.get_clipboard.assert_not_called()


@pytest.mark.parametrize("token,headers", [
    (TOKEN, {}), (TOKEN, {"Authorization": "Bearer wrong"}), ("", AUTH),
    (TOKEN, {**AUTH, "Origin": "http://testserver"}), (TOKEN, {**AUTH, "Origin": ""}),
    (TOKEN, [("Authorization", f"Bearer {TOKEN}"), ("Authorization", f"Bearer {TOKEN}")]),
])
@pytest.mark.parametrize("path", [INTERNAL, "/lanes/unknown/clipboard"])
def test_read_worker_auth_first(read_worker, monkeypatch, token, headers, path):
    monkeypatch.setenv("SITEFLOW_BROWSER_TOKEN", token)
    assert_private(read_worker.client.get(path, headers=headers), 403)
    assert not read_worker.calls
    assert not read_worker.lane.viewers


@pytest.mark.parametrize("text", ["", TEXT, "\U0001f680" * LIMIT])
def test_read_public_exact_text_without_csrf(client, relay, text):
    relay.install(lambda request: httpx.Response(200, json={"text": text, "private": TOKEN}))
    client.headers.pop("X-CSRF-Token")
    response = client.get(PUBLIC + "?profile_id=attacker", headers={"Origin": "http://testserver"})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {"text": text}
    request, = relay.calls
    assert str(request.url) == "http://fixed-worker:6300/lanes/general/clipboard"
    assert request.method == "GET" and request.content == b""
    assert request.headers["Authorization"] == AUTH["Authorization"]
    assert request.headers["Cache-Control"] == "no-store"
    assert "origin" not in request.headers and "cookie" not in request.headers
    assert relay.options == [{"timeout": 15, "follow_redirects": False, "trust_env": False}]


def test_read_public_unknown_and_missing_token(client, relay):
    assert_private(client.get("/api/v1/lanes/unknown/clipboard"), 404)
    client.app.state.settings.browser_token = None
    assert_private(client.get(PUBLIC), 503)
    assert not relay.calls


@pytest.mark.parametrize("state", [
    "stopped", "starting", "stopping", "disabled", "error", "disconnected", "no-session", "unknown", "shutdown",
])
def test_read_never_starts_lane(read_worker, monkeypatch, state):
    monkeypatch.setattr(read_worker.manager.backend, "start", AsyncMock(side_effect=AssertionError))
    path = INTERNAL
    if state == "disconnected":
        read_worker.lane.session.disconnected = True
    elif state == "no-session":
        read_worker.lane.session = None
    elif state == "unknown":
        path = "/lanes/unknown/clipboard"
    elif state == "shutdown":
        read_worker.manager._stopping = True
    else:
        read_worker.lane.status = state
    assert_private(read_worker.client.get(path, headers=AUTH), 404 if state == "unknown" else 409)
    assert not read_worker.calls and not read_worker.lane.viewers
    read_worker.manager.backend.start.assert_not_called()


@pytest.mark.parametrize("desktop", [None, SimpleNamespace()])
def test_read_non_x11_falls_back_to_manager(read_worker, desktop):
    read_worker.lane.session.desktop = desktop
    response = read_worker.client.get(INTERNAL, headers=AUTH)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {"text": TEXT}
    assert read_worker.calls == [read_worker.lane.session.profile_id]
    assert not read_worker.lane.viewers


def test_read_does_not_cache_desktop_content(read_worker):
    getter = AsyncMock(side_effect=[TEXT, "", "new\u4e2d\u6587"])
    read_worker.lane.session.desktop = SimpleNamespace(read_clipboard=getter)
    for text in [TEXT, "", "new\u4e2d\u6587"]:
        response = read_worker.client.get(INTERNAL, headers=AUTH)
        assert response.status_code == 200
        assert response.json() == {"text": text}
        assert response.headers["Cache-Control"] == "no-store"
    assert getter.await_count == 3
    assert not read_worker.calls and not read_worker.lane.viewers


@pytest.mark.parametrize("text", ["", TEXT, "\U0001f680" * LIMIT])
def test_read_desktop_wins_over_stale_manager(read_worker, monkeypatch, text):
    desktop = X11Desktop(":123")
    getter = AsyncMock(return_value=text)
    monkeypatch.setattr(desktop, "read_clipboard", getter)
    read_worker.lane.session.desktop = desktop
    response = read_worker.client.get(INTERNAL + "?profile_id=other", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"text": text}
    getter.assert_awaited_once_with()
    assert not read_worker.calls and not read_worker.lane.viewers


@pytest.mark.parametrize("failure,status", [(RuntimeError, 502), (TimeoutError, 504)])
@pytest.mark.parametrize("desktop", [False, True])
def test_read_worker_errors_do_not_leak_or_fall_back(read_worker, monkeypatch, failure, status, desktop):
    getter = AsyncMock(side_effect=failure(TEXT))
    if desktop:
        read_worker.lane.session.desktop = SimpleNamespace(read_clipboard=getter)
    else:
        monkeypatch.setattr(read_worker.manager.backend, "get_clipboard", getter)
    assert_private(read_worker.client.get(INTERNAL, headers=AUTH), status)
    assert not read_worker.calls and not read_worker.lane.viewers


@pytest.mark.parametrize("text", [None, 123, "a" * (LIMIT + 1), "\ud800"])
def test_read_worker_revalidates_provider_text(read_worker, monkeypatch, text):
    monkeypatch.setattr(read_worker.manager.backend, "get_clipboard", AsyncMock(return_value=text))
    assert_private(read_worker.client.get(INTERNAL, headers=AUTH), 502)
    assert not read_worker.lane.viewers


def test_read_pin_prevents_stop_and_releases_after_cancel(read_worker, monkeypatch):
    async def run():
        entered = asyncio.Event()

        async def wait():
            assert read_worker.lane.viewers
            entered.set()
            await asyncio.Event().wait()

        read_worker.lane.session.desktop = SimpleNamespace(read_clipboard=wait)
        task = asyncio.create_task(read_worker.manager.get_clipboard("general"))
        await entered.wait()
        with pytest.raises(LaneBusyError):
            await read_worker.manager.stop_lane("general")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not read_worker.lane.viewers and not read_worker.calls

    asyncio.run(run())


@pytest.mark.parametrize("status,expected", [
    (404, 404), (409, 409), (413, 413), (422, 422), (502, 502), (503, 503), (504, 504),
    (401, 502), (403, 502), (500, 502), (307, 502),
])
def test_read_public_errors_never_read_body(client, relay, status, expected):
    stream = Chunks([TEXT.encode()])
    relay.install(lambda request: httpx.Response(status, stream=stream))
    assert_private(client.get(PUBLIC), expected)
    assert stream.reads == 0 and len(relay.calls) == 1


INVALID_BODIES = [
    b'{}', b'[]', b'null', b'{"text":null}', b'{"text":1}', b'{"text":[]}',
    b'{"text":"\xff"}', b'{"text":"\\ud800"}', b'private-clipboard',
    json.dumps({"text": "a" * (LIMIT + 1)}).encode(),
    b" " * (12 * LIMIT + 1025),
]


@pytest.mark.parametrize("body", INVALID_BODIES, ids=[
    "missing", "array", "null", "null-text", "number", "array-text", "utf8", "surrogate", "not-json",
    "codepoint-limit", "byte-limit",
])
def test_read_public_rejects_bad_worker_body(client, relay, body):
    stream = Chunks([body, b"unread"])
    relay.install(lambda request: httpx.Response(200, stream=stream))
    assert_private(client.get(PUBLIC), 502)
    if len(body) > 12 * LIMIT + 1024:
        assert stream.reads == 1


def test_read_public_rejects_compression(client, relay):
    stream = Chunks([TEXT.encode()])
    relay.install(lambda request: httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=stream))
    assert_private(client.get(PUBLIC), 502)
    assert stream.reads == 0


@pytest.mark.parametrize("failure,status", [(httpx.ReadTimeout, 504), (httpx.ConnectError, 502)])
def test_read_public_transport_errors(client, relay, failure, status):
    def fail(request):
        raise failure(TEXT, request=request)

    relay.install(fail)
    assert_private(client.get(PUBLIC), status)


@pytest.fixture
def manager_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("SITEFLOW_BROWSER_TOKEN", TOKEN)
    backend = ManagerBackend(tmp_path)
    asyncio.run(backend.client.aclose())
    calls = []

    def install(responder):
        def handle(request):
            calls.append(request)
            return responder(request)

        backend.client = REAL_ASYNC_CLIENT(base_url="http://manager", headers=backend.headers,
                                          transport=httpx.MockTransport(handle))

    session = BrowserSession(None, None, "id/123?\u4e2d", None, None)
    yield SimpleNamespace(backend=backend, session=session, install=install, calls=calls)
    asyncio.run(backend.close())


@pytest.mark.parametrize("text", ["", TEXT, "\U0001f680" * LIMIT])
def test_read_manager_unicode_profile_projection_and_no_cache(manager_adapter, text):
    adapter = manager_adapter
    adapter.install(lambda request: httpx.Response(200, json={"text": text, "secret": TOKEN}))
    assert asyncio.run(adapter.backend.get_clipboard(adapter.session)) == text
    request, = adapter.calls
    assert request.url.raw_path == b"/api/profiles/id%2F123%3F%E4%B8%AD/clipboard"
    assert request.method == "GET" and request.content == b""
    assert request.headers["Authorization"] == AUTH["Authorization"]
    assert request.headers["Cache-Control"] == "no-store"
    assert request.headers["Accept-Encoding"] == "identity"
    assert request.extensions["timeout"]["read"] == 10


@pytest.mark.parametrize("status", [204, 400, 401, 404, 409, 500, 307])
def test_read_manager_errors_do_not_read_or_redirect(manager_adapter, status):
    adapter = manager_adapter
    stream = Chunks([TEXT.encode()])
    adapter.install(lambda request: httpx.Response(status, headers={"Location": "http://other"}, stream=stream))
    with pytest.raises(RuntimeError, match="^Clipboard unavailable$"):
        asyncio.run(adapter.backend.get_clipboard(adapter.session))
    assert stream.reads == 0 and len(adapter.calls) == 1


@pytest.mark.parametrize("body", INVALID_BODIES, ids=[
    "missing", "array", "null", "null-text", "number", "array-text", "utf8", "surrogate", "not-json",
    "codepoint-limit", "byte-limit",
])
def test_read_manager_rejects_bad_bounded_body(manager_adapter, body):
    adapter = manager_adapter
    stream = Chunks([body, b"unread"])
    adapter.install(lambda request: httpx.Response(200, stream=stream))
    with pytest.raises(RuntimeError, match="^Clipboard unavailable$"):
        asyncio.run(adapter.backend.get_clipboard(adapter.session))
    if len(body) > 12 * LIMIT + 1024:
        assert stream.reads == 1


def test_read_manager_rejects_compression(manager_adapter):
    adapter = manager_adapter
    stream = Chunks([TEXT.encode()])
    adapter.install(lambda request: httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=stream))
    with pytest.raises(RuntimeError, match="^Clipboard unavailable$"):
        asyncio.run(adapter.backend.get_clipboard(adapter.session))
    assert stream.reads == 0


def test_read_manager_disconnected_does_not_request(manager_adapter):
    adapter = manager_adapter
    adapter.session.disconnected = True
    with pytest.raises(RuntimeError, match="^Clipboard session unavailable$"):
        asyncio.run(adapter.backend.get_clipboard(adapter.session))
    assert not adapter.calls


@pytest.mark.parametrize("failure,expected", [(httpx.ReadTimeout, TimeoutError), (httpx.ConnectError, RuntimeError)])
def test_read_manager_transport_errors(manager_adapter, failure, expected):
    adapter = manager_adapter

    def fail(request):
        raise failure(TEXT, request=request)

    adapter.install(fail)
    with pytest.raises(expected) as error:
        asyncio.run(adapter.backend.get_clipboard(adapter.session))
    assert TEXT not in str(error.value)


@pytest.mark.parametrize("target", ["public", "worker", "manager"])
def test_read_total_deadline(client, relay, read_worker, manager_adapter, monkeypatch, target):
    real_timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda delay: real_timeout(0.02))

    async def wait(*args):
        await asyncio.Event().wait()

    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"text":"'
            await wait()

    if target == "public":
        relay.install(lambda request: httpx.Response(200, stream=SlowBody()))
        assert_private(client.get(PUBLIC), 504)
    elif target == "manager":
        adapter = manager_adapter
        adapter.install(lambda request: httpx.Response(200, stream=SlowBody()))
        with pytest.raises(TimeoutError, match="^Clipboard request timed out$"):
            asyncio.run(adapter.backend.get_clipboard(adapter.session))
    else:
        monkeypatch.setattr(read_worker.manager.backend, "get_clipboard", wait)
        assert_private(read_worker.client.get(INTERNAL, headers=AUTH), 504)
        assert not read_worker.lane.viewers


class ClipboardProcess:
    def __init__(self, chunks, *, code=0, slow_read=False, slow_wait=False):
        self.chunks = iter(chunks)
        self.code = code
        self.returncode = None
        self.stdout = self
        self.slow_read = slow_read
        self.slow_wait = slow_wait
        self.killed = False
        self.killed_at_read = None
        self.reaped = False
        self.reads = 0

    async def read(self, size):
        assert size == 65536
        self.reads += 1
        if self.slow_read and not self.killed:
            await asyncio.Event().wait()
        return next(self.chunks, b"")

    async def wait(self):
        if self.slow_wait and not self.killed:
            await asyncio.Event().wait()
        self.returncode = -9 if self.killed else self.code
        self.reaped = True
        return self.returncode

    def kill(self):
        self.killed = True
        self.killed_at_read = self.reads


@pytest.fixture
def xclip_mock(monkeypatch):
    def install(process):
        spawn = AsyncMock(return_value=process)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        return spawn

    # Even an incorrectly wired test must never spawn a real process.
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(side_effect=AssertionError))
    return install


@pytest.mark.parametrize("text", ["", TEXT, "\U0001f680" * LIMIT])
def test_x11_reads_utf8_exactly_with_bounded_stdout(xclip_mock, text):
    data = text.encode()
    process = ClipboardProcess([data[i:i + 65536] for i in range(0, len(data), 65536)])
    spawn = xclip_mock(process)
    assert asyncio.run(X11Desktop(":456").read_clipboard()) == text
    args, kwargs = spawn.call_args
    assert args == ("xclip", "-selection", "clipboard", "-o", "-target", "UTF8_STRING")
    assert kwargs["env"]["DISPLAY"] == ":456"
    assert kwargs["stdout"] == subprocess.PIPE
    assert kwargs["stderr"] == kwargs["stdin"] == subprocess.DEVNULL
    assert process.reaped and not process.killed


@pytest.mark.parametrize("failure", ["bytes", "codepoints", "invalid-utf8", "surrogate", "exit"])
def test_x11_rejects_invalid_or_large_clipboard_and_reaps(xclip_mock, failure):
    chunks = {
        "bytes": [b"x" * 65536] * 64 + [b"x", b"never read"],
        "codepoints": [b"x" * 65536] * 16 + [b"x"],
        "invalid-utf8": [b"\xff"], "surrogate": [b"\xed\xa0\x80"], "exit": [TEXT.encode()],
    }[failure]
    process = ClipboardProcess(chunks, code=1 if failure == "exit" else 0)
    xclip_mock(process)
    with pytest.raises(RuntimeError, match="^Clipboard unavailable$"):
        asyncio.run(X11Desktop(":456").read_clipboard())
    assert process.reaped
    if failure == "bytes":
        assert process.killed and process.killed_at_read == 65


@pytest.mark.parametrize("failure", ["spawn", "read", "wait", "cancel"])
def test_x11_deadline_and_cancellation_kill_and_reap(xclip_mock, monkeypatch, failure):
    real_timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda delay: real_timeout(0.02))
    process = ClipboardProcess([], slow_read=failure in {"read", "cancel"}, slow_wait=failure == "wait")
    spawn = xclip_mock(process)

    async def run():
        if failure == "spawn":
            spawn.side_effect = OSError(TEXT)
            with pytest.raises(RuntimeError, match="^Clipboard unavailable$"):
                await X11Desktop(":456").read_clipboard()
        elif failure == "cancel":
            task = asyncio.create_task(X11Desktop(":456").read_clipboard())
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(TimeoutError, match="^Clipboard request timed out$"):
                await X11Desktop(":456").read_clipboard()

    asyncio.run(run())
    if failure != "spawn":
        assert process.killed and process.reaped
