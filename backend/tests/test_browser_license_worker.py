import asyncio
import gzip
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker.app import create_app  # noqa: E402
from worker.browser_backend import (  # noqa: E402
    BROWSER_SETTINGS_WRITE_UNKNOWN, BrowserSettingsError, ManagerBackend,
)


TOKEN = "worker-private-token"
SECRET = "private-license-must-not-leak"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
STATUS = {"license_tier": "free", "binary_version": "156.0.8078.47.1"}


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    monkeypatch.setenv("SITEFLOW_BROWSER_TOKEN", TOKEN)
    monkeypatch.setenv("SITEFLOW_BROWSER_URL", "https://configured-browser.example/base")
    monkeypatch.delenv("CLOAKBROWSER_LICENSE_KEY", raising=False)


@asynccontextmanager
async def worker(tmp_path, respond=None):
    backend = ManagerBackend(tmp_path)
    await backend.settings_client.aclose()
    calls = []

    async def dispatch(request):
        calls.append(request)
        assert request.url.host == "configured-browser.example"
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert request.headers["accept-encoding"] == "identity"
        assert request.url.path in {"/base/api/status", "/base/api/settings"}
        expected_timeout = 300 if request.method == "PUT" else 10
        assert set(request.extensions["timeout"].values()) == {expected_timeout}
        if respond is not None:
            return await respond(request)
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json=STATUS)
        return httpx.Response(200, json={"license_key_set": False, "license_key": SECRET,
                                         "release_channel": "stable"})

    backend.settings_client = httpx.AsyncClient(
        base_url=backend.url, headers={**backend.headers, "Accept-Encoding": "identity"},
        transport=httpx.MockTransport(dispatch), trust_env=False, follow_redirects=False,
    )
    app = create_app()
    app.state.manager = SimpleNamespace(backend=backend)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
            yield client, backend, calls
        assert not backend.profiles
        assert not backend.state_path.exists()
        assert all(r.url.path in {"/base/api/status", "/base/api/settings"} for r in calls)
    finally:
        await backend.close()


@pytest.mark.parametrize("method", ["GET", "PUT"])
@pytest.mark.parametrize("key", [SECRET, ""])
@pytest.mark.parametrize("headers,token", [
    ({}, TOKEN),
    ({"Authorization": "Bearer wrong"}, TOKEN),
    ({"Authorization": "Basic worker-private-token"}, TOKEN),
    ({**AUTH, "Origin": "https://siteflow.example"}, TOKEN),
    ({**AUTH, "Origin": ""}, TOKEN),
    (AUTH, ""),
])
def test_requires_internal_bearer_without_any_origin(tmp_path, monkeypatch, method, key, headers, token):
    monkeypatch.setenv("SITEFLOW_BROWSER_TOKEN", token)

    async def run():
        async with worker(tmp_path) as (client, _, calls):
            response = await client.request(method, "/browser/settings", headers=headers,
                                            json={"license_key": key})
            assert response.status_code == 403
            assert SECRET not in response.text
            assert calls == []
    asyncio.run(run())


@pytest.mark.parametrize("configured,env,expected", [
    (True, "", {"configured": True, "source": "manager", "editable": True}),
    (False, "", {"configured": False, "source": "unset", "editable": True}),
    (False, SECRET, {"configured": True, "source": "env", "editable": False}),
    (True, SECRET, {"configured": True, "source": "env", "editable": False}),
])
def test_get_projects_only_safe_metadata(tmp_path, monkeypatch, configured, env, expected):
    monkeypatch.setenv("CLOAKBROWSER_LICENSE_KEY", env)
    # Backend identity is the instantiated provider, not the current environment.
    monkeypatch.setenv("SITEFLOW_BROWSER_BACKEND", "not-the-instantiated-backend")

    async def respond(request):
        return httpx.Response(200, json=STATUS if request.url.path.endswith("/status") else {
            "license_key_set": configured, "license_key": SECRET, "error": SECRET,
            "source": "env", "editable": False,
        })

    async def run():
        async with worker(tmp_path, respond) as (client, _, calls):
            response = await client.get("/browser/settings", headers=AUTH)
            assert response.status_code == 200
            assert response.json() == {
                "backend": "cloak-manager", "connected": True, "license": expected,
                "license_tier": "free", "binary_version": STATUS["binary_version"], "error": None,
            }
            assert SECRET not in response.text
            assert [r.method for r in calls] == ["GET", "GET"]
    asyncio.run(run())


@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_unsupported_backend_is_normalized(tmp_path, monkeypatch, method):
    monkeypatch.setenv("SITEFLOW_BROWSER_BACKEND", "cloak-manager")

    async def run():
        async with worker(tmp_path) as (client, _, calls):
            client._transport.app.state.manager.backend = SimpleNamespace()
            response = await client.request(method, "/browser/settings", headers=AUTH, content=SECRET)
            assert response.status_code == 200
            assert response.json() == {
                "backend": "unsupported", "connected": False,
                "license": {"configured": False, "source": "unknown", "editable": False},
                "license_tier": "unknown", "binary_version": None,
                "error": "当前浏览器后端不支持授权设置。",
            }
            assert not calls
    asyncio.run(run())


@pytest.mark.parametrize("env", [SECRET, " "])
@pytest.mark.parametrize("key", [SECRET, ""])
def test_env_lock_rejects_before_read_or_put(tmp_path, monkeypatch, env, key):
    monkeypatch.setenv("CLOAKBROWSER_LICENSE_KEY", env)

    async def run():
        async with worker(tmp_path) as (client, backend, calls):
            response = await client.put("/browser/settings", headers=AUTH, json={"license_key": key})
            assert response.status_code == 409
            assert SECRET not in response.text
            with pytest.raises(BrowserSettingsError) as error:
                await backend.update_browser_settings(key)
            assert error.value.status_code == 409
            assert not calls
    asyncio.run(run())


@pytest.mark.parametrize("body", [
    SECRET, '{"license_key":"' + SECRET, '"' + SECRET + '"',
    json.dumps([SECRET]), json.dumps({"license_key": SECRET, "extra": SECRET}),
    json.dumps({"license_key": {"secret": SECRET}}), json.dumps({"license_key": 123}),
    json.dumps({"license_key": None}), json.dumps({"license_key": "\u00a0"}),
    json.dumps({"license_key": "   "}), json.dumps({"license_key": "a" * 4097}),
    json.dumps({"license_key": SECRET + "\n"}), json.dumps({"license_key": "\x7f" + SECRET}),
    json.dumps({"license_key": SECRET + "\x85"}), json.dumps({"license_key": "\ud800"}),
    '{"license_key":"' + SECRET + '","license_key":"other"}',
    '{"license_key":"' + SECRET + '","license_key":""}',
    b'\xff' + SECRET.encode(), b" " * 8193,
])
def test_sensitive_malformed_input_has_static_422(tmp_path, body):
    async def run():
        async with worker(tmp_path) as (client, backend, calls):
            response = await client.put("/browser/settings", headers=AUTH, content=body)
            assert response.status_code == 422
            assert response.json() == {"detail": "授权设置请求无效。"}
            assert not calls
            assert backend.settings_write_unknown is False
    asyncio.run(run())


def test_streamed_body_is_bounded_without_content_length(tmp_path):
    async def chunks():
        yield b'{"license_key":"'
        yield b"a" * 8192
        raise AssertionError("must stop reading after the limit")

    async def run():
        async with worker(tmp_path) as (client, _, calls):
            response = await client.put("/browser/settings", headers=AUTH, content=chunks())
            assert response.status_code == 422
            assert not calls
    asyncio.run(run())


@pytest.mark.parametrize("key", ["  " + SECRET + "  ", "x" * 4096, ""])
def test_save_or_clear_sends_only_key_then_refreshes_metadata(tmp_path, monkeypatch, key):
    saved = key == ""
    release_channel = "beta"
    monkeypatch.setenv("HOME", str(tmp_path))
    license_file = tmp_path / ".cloakbrowser" / "license.key"
    license_file.parent.mkdir()
    license_file.write_text(SECRET)

    async def respond(request):
        nonlocal saved, release_channel
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={**STATUS, "license_tier": "none"})
        if request.method == "PUT":
            assert json.loads(request.content) == {"license_key": key.strip()}
            saved = bool(json.loads(request.content)["license_key"])
            release_channel = json.loads(request.content).get("release_channel", release_channel)
            return httpx.Response(200, json={"license_key": SECRET, "saved": True})
        return httpx.Response(200, json={"license_key_set": saved, "license_key": SECRET,
                                       "release_channel": release_channel})

    async def run():
        async with worker(tmp_path, respond) as (client, backend, calls):
            before = await client.get("/browser/settings", headers=AUTH)
            assert before.json()["license"]["configured"] is (key == "")
            calls.clear()
            response = await client.put("/browser/settings", headers=AUTH, json={"license_key": key})
            assert response.status_code == 200
            assert response.json()["license"] == {
                "configured": bool(key), "source": "manager" if key else "unset", "editable": True,
            }
            assert response.json()["license_tier"] == "none"
            assert SECRET not in response.text
            assert [r.method for r in calls] == ["GET", "GET", "PUT", "GET", "GET"]
            assert backend.settings_write_unknown is False
            assert saved is bool(key)
            assert release_channel == "beta"
            assert license_file.read_text() == SECRET
    asyncio.run(run())


@pytest.mark.parametrize("tier,version,expected_tier,expected_version", [
    ("keyless", "146.0.7680.177.5", "keyless", "146.0.7680.177.5"),
    ("pro", "1", "pro", "1"), ("none", "1.2", "none", "1.2"),
    (SECRET, SECRET, "unknown", None), (["free"], 123, "unknown", None),
    (None, "1" * 65, "unknown", None), ("free", "1..2", "free", None),
    ("free", "1.2\n", "free", None), ("free", "１.２", "free", None),
])
def test_status_metadata_is_allowlisted(tmp_path, tier, version, expected_tier, expected_version):
    async def respond(request):
        return httpx.Response(200, json={"license_tier": tier, "binary_version": version}
                              if request.url.path.endswith("/status") else {"license_key_set": False})

    async def run():
        async with worker(tmp_path, respond) as (client, _, _calls):
            response = await client.get("/browser/settings", headers=AUTH)
            assert response.json()["license_tier"] == expected_tier
            assert response.json()["binary_version"] == expected_version
            assert SECRET not in response.text
    asyncio.run(run())


class RawChunks(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b" " * 40000
        yield b" " * 25537
        raise AssertionError("must stop at 64 KiB")


@pytest.mark.parametrize("failure", ["redirect", "compression", "oversize", "malformed", "list", "http", "timeout"])
@pytest.mark.parametrize("env", ["", SECRET])
def test_provider_failures_are_static_and_preserve_env_lock(tmp_path, monkeypatch, failure, env):
    monkeypatch.setenv("CLOAKBROWSER_LICENSE_KEY", env)

    async def respond(request):
        if failure == "redirect":
            return httpx.Response(307, headers={"Location": "https://external.example/" + SECRET})
        if failure == "compression":
            return httpx.Response(200, headers={"Content-Encoding": "gzip"},
                                  stream=httpx.ByteStream(gzip.compress(SECRET.encode())))
        if failure == "oversize":
            return httpx.Response(200, stream=RawChunks())
        if failure == "malformed":
            return httpx.Response(200, content=SECRET)
        if failure == "list":
            return httpx.Response(200, json=[SECRET])
        if failure == "timeout":
            raise httpx.ReadTimeout(SECRET, request=request)
        return httpx.Response(500, json={"detail": SECRET})

    async def run():
        async with worker(tmp_path, respond) as (client, _, calls):
            response = await client.get("/browser/settings", headers=AUTH)
            assert response.status_code == 200
            result = response.json()
            assert result["connected"] is False
            assert result["license"] == {
                "configured": bool(env), "source": "env" if env else "unknown", "editable": False,
            }
            assert result["error"] == "无法读取浏览器管理器授权设置，请稍后刷新。"
            assert SECRET not in response.text
            assert len(calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize("settings", [404, 405, 501, {"license_key": SECRET},
                                       {"license_key_set": "true"}, {"license_key_set": 1}])
@pytest.mark.parametrize("key", [SECRET, ""])
def test_old_or_malformed_settings_are_readonly_and_block_put(tmp_path, settings, key):
    async def respond(request):
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json=STATUS)
        return (httpx.Response(settings, content=SECRET) if isinstance(settings, int)
                else httpx.Response(200, json=settings))

    async def run():
        async with worker(tmp_path, respond) as (client, _, calls):
            response = await client.get("/browser/settings", headers=AUTH)
            result = response.json()
            assert response.status_code == 200
            assert result["license"] == {"configured": False, "source": "unknown", "editable": False}
            assert result["error"]
            assert result["connected"] is isinstance(settings, int)
            assert SECRET not in response.text
            response = await client.put("/browser/settings", headers=AUTH, json={"license_key": key})
            assert response.status_code in (409, 502)
            assert SECRET not in response.text
            assert all(r.method == "GET" for r in calls)
            assert client._transport.app.state.manager.backend.settings_write_unknown is False
    asyncio.run(run())


@pytest.mark.parametrize("failure,code", [("timeout", 504), ("http", 502), ("redirect", 502),
                                         ("compression", 502), ("malformed", 502), ("oversize", 502),
                                         ("connect", 502), ("exception", 502), ("unsupported", 409)])
@pytest.mark.parametrize("key", [SECRET, ""])
def test_put_failure_never_retries_or_exposes_provider_data(tmp_path, failure, code, key):
    async def respond(request):
        if request.method != "PUT":
            return httpx.Response(200, json=STATUS if request.url.path.endswith("/status")
                                  else {"license_key_set": False})
        if failure == "timeout":
            raise httpx.ReadTimeout(SECRET, request=request)
        if failure == "connect":
            raise httpx.ConnectError(SECRET, request=request)
        if failure == "exception":
            raise RuntimeError(SECRET)
        if failure == "unsupported":
            return httpx.Response(405, content=SECRET)
        if failure == "redirect":
            return httpx.Response(302, headers={"Location": "https://external.example/" + SECRET})
        if failure == "compression":
            return httpx.Response(200, headers={"Content-Encoding": "gzip"},
                                  stream=httpx.ByteStream(gzip.compress(SECRET.encode())))
        if failure == "oversize":
            return httpx.Response(200, stream=RawChunks())
        return httpx.Response(200 if failure == "malformed" else 403, content=SECRET)

    async def run():
        async with worker(tmp_path, respond) as (client, backend, calls):
            response = await client.put("/browser/settings", headers=AUTH, json={"license_key": key})
            assert response.status_code == code
            assert SECRET not in response.text
            if failure != "unsupported":
                assert response.json()["detail"] == BROWSER_SETTINGS_WRITE_UNKNOWN
            assert [r.method for r in calls] == ["GET", "GET", "PUT"]
            assert json.loads(calls[2].content) == {"license_key": key}
            assert not backend.settings_lock.locked()
            assert backend.settings_write_unknown is True
            for _ in range(2):
                before = len(calls)
                retry = await client.put("/browser/settings", headers=AUTH, json={"license_key": key})
                assert retry.status_code == 409
                assert retry.json()["detail"] == BROWSER_SETTINGS_WRITE_UNKNOWN
                assert len(calls) == before
                with pytest.raises(BrowserSettingsError) as error:
                    await backend.update_browser_settings(key)
                assert error.value.status_code == 409
                assert len(calls) == before
                read = await client.get("/browser/settings", headers=AUTH)
                assert read.json()["license"] == {"configured": False, "source": "unset", "editable": False}
                assert read.json()["connected"] is True
                assert read.json()["error"] == BROWSER_SETTINGS_WRITE_UNKNOWN
                assert backend.settings_write_unknown is True
    asyncio.run(run())


@pytest.mark.parametrize("key", [SECRET, ""])
def test_mutations_reject_busy_but_get_does_not_take_start_or_settings_lock(tmp_path, key):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def respond(request):
        if request.method == "PUT":
            entered.set()
            await release.wait()
        return httpx.Response(200, json=STATUS if request.url.path.endswith("/status")
                              else {"license_key_set": False})

    async def run():
        async with worker(tmp_path, respond) as (client, backend, calls):
            async with backend.lock:
                first = asyncio.create_task(client.put("/browser/settings", headers=AUTH,
                                                       json={"license_key": SECRET}))
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    busy = await asyncio.wait_for(client.put("/browser/settings", headers=AUTH,
                                                            json={"license_key": key}), 2)
                    assert busy.status_code == 409
                    read = await asyncio.wait_for(client.get("/browser/settings", headers=AUTH), 2)
                    assert read.status_code == 200
                    assert read.json()["connected"] is True
                    assert read.json()["license"]["editable"] is False
                    assert read.json()["error"] == BROWSER_SETTINGS_WRITE_UNKNOWN
                    assert backend.settings_write_unknown is True
                finally:
                    release.set()
                    await first
            assert sum(r.method == "PUT" for r in calls) == 1
            assert backend.settings_write_unknown is False
    asyncio.run(run())


def test_ambiguous_clear_stays_locked_even_when_manager_reports_key_unset(tmp_path):
    saved = True

    async def respond(request):
        nonlocal saved
        if request.method == "PUT":
            assert json.loads(request.content) == {"license_key": ""}
            saved = False
            raise httpx.ReadTimeout(SECRET, request=request)
        return httpx.Response(200, json=STATUS if request.url.path.endswith("/status")
                              else {"license_key_set": saved, "license_key": SECRET})

    async def run():
        async with worker(tmp_path, respond) as (client, backend, calls):
            response = await client.put("/browser/settings", headers=AUTH, json={"license_key": ""})
            assert response.status_code == 504
            assert response.json() == {"detail": BROWSER_SETTINGS_WRITE_UNKNOWN}
            assert SECRET not in response.text
            for key in ("", SECRET):
                read = await client.get("/browser/settings", headers=AUTH)
                assert read.json()["license"] == {
                    "configured": False, "source": "unset", "editable": False,
                }
                assert read.json()["error"] == BROWSER_SETTINGS_WRITE_UNKNOWN
                assert SECRET not in read.text
                before = len(calls)
                retry = await client.put("/browser/settings", headers=AUTH, json={"license_key": key})
                assert retry.status_code == 409
                assert retry.json() == {"detail": BROWSER_SETTINGS_WRITE_UNKNOWN}
                with pytest.raises(BrowserSettingsError) as error:
                    await backend.update_browser_settings(key)
                assert error.value.status_code == 409
                assert len(calls) == before
                assert backend.settings_write_unknown is True
            assert sum(r.method == "PUT" for r in calls) == 1
            assert not backend.settings_lock.locked()
    asyncio.run(run())


def test_settings_http_client_is_hardened(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid")

    async def run():
        backend = ManagerBackend(tmp_path)
        try:
            assert backend.settings_client.trust_env is False
            assert backend.settings_client.follow_redirects is False
            assert backend.settings_client.headers["accept-encoding"] == "identity"
            assert backend.settings_client.timeout == httpx.Timeout(10)
        finally:
            await backend.close()
    asyncio.run(run())


@pytest.mark.parametrize("key", [SECRET, ""])
def test_env_lock_is_rechecked_immediately_before_mutation(tmp_path, monkeypatch, key):
    async def respond(request):
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json=STATUS)
        monkeypatch.setenv("CLOAKBROWSER_LICENSE_KEY", SECRET)
        return httpx.Response(200, json={"license_key_set": False})

    async def run():
        async with worker(tmp_path, respond) as (client, backend, calls):
            response = await client.put("/browser/settings", headers=AUTH, json={"license_key": key})
            assert response.status_code == 409
            assert SECRET not in response.text
            assert all(r.method == "GET" for r in calls)
            assert backend.settings_write_unknown is False
    asyncio.run(run())


@pytest.mark.parametrize("env", ["", SECRET])
def test_settings_unavailable_after_successful_status_preserves_readonly_env(tmp_path, monkeypatch, env):
    monkeypatch.setenv("CLOAKBROWSER_LICENSE_KEY", env)

    async def respond(request):
        return httpx.Response(200, json=STATUS) if request.url.path.endswith("/status") else httpx.Response(
            503, content=SECRET)

    async def run():
        async with worker(tmp_path, respond) as (client, _, _calls):
            response = await client.get("/browser/settings", headers=AUTH)
            result = response.json()
            assert result["connected"] is False
            assert result["license"] == {"configured": bool(env), "source": "env" if env else "unknown",
                                         "editable": False}
            assert result["license_tier"] == "free"
            assert SECRET not in response.text
    asyncio.run(run())


def test_post_save_refresh_failure_returns_normalized_readonly_metadata(tmp_path):
    saved = False

    async def respond(request):
        nonlocal saved
        if request.method == "PUT":
            saved = True
            return httpx.Response(200, json={"license_key": SECRET})
        if saved:
            raise httpx.ConnectError(SECRET, request=request)
        return httpx.Response(200, json=STATUS if request.url.path.endswith("/status")
                              else {"license_key_set": False})

    async def run():
        async with worker(tmp_path, respond) as (client, backend, calls):
            response = await client.put("/browser/settings", headers=AUTH, json={"license_key": SECRET})
            assert response.status_code == 200
            result = response.json()
            assert result["connected"] is False
            assert result["license"] == {"configured": False, "source": "unknown", "editable": False}
            assert result["error"]
            assert SECRET not in response.text
            assert sum(r.method == "PUT" for r in calls) == 1
            assert backend.settings_write_unknown is False
    asyncio.run(run())


@pytest.mark.parametrize("configured,env", [(False, ""), (True, ""), (False, SECRET), (True, SECRET)])
def test_unknown_write_refresh_preserves_source_and_env_lock(tmp_path, monkeypatch, configured, env):
    monkeypatch.setenv("CLOAKBROWSER_LICENSE_KEY", env)

    async def respond(request):
        return httpx.Response(200, json=STATUS if request.url.path.endswith("/status")
                              else {"license_key_set": configured})

    async def run():
        async with worker(tmp_path, respond) as (client, backend, calls):
            backend.settings_write_unknown = True
            for _ in range(2):
                read = await client.get("/browser/settings", headers=AUTH)
                assert read.json()["license"] == {
                    "configured": bool(env) or configured,
                    "source": "env" if env else "manager" if configured else "unset", "editable": False,
                }
                assert read.json()["error"] == BROWSER_SETTINGS_WRITE_UNKNOWN
                assert backend.settings_write_unknown is True
                before = len(calls)
                retry = await client.put("/browser/settings", headers=AUTH, json={"license_key": SECRET})
                assert retry.status_code == 409
                assert len(calls) == before
    asyncio.run(run())


@pytest.mark.parametrize("route", [False, True])
@pytest.mark.parametrize("key", [SECRET, ""])
def test_cancellation_during_dispatched_write_is_sticky(tmp_path, route, key):
    async def run():
        entered = asyncio.Event()

        async def respond(request):
            if request.method == "PUT":
                entered.set()
                await asyncio.Event().wait()
            return httpx.Response(200, json=STATUS if request.url.path.endswith("/status")
                                  else {"license_key_set": True})

        async with worker(tmp_path, respond) as (client, backend, calls):
            write = asyncio.create_task(
                client.put("/browser/settings", headers=AUTH, json={"license_key": key})
                if route else backend.update_browser_settings(key))
            await asyncio.wait_for(entered.wait(), 2)
            write.cancel()
            if route:
                response = await write
                assert response.status_code == 504
                assert response.json()["detail"] == BROWSER_SETTINGS_WRITE_UNKNOWN
            else:
                with pytest.raises(asyncio.CancelledError):
                    await write
            assert not backend.settings_lock.locked()
            assert backend.settings_write_unknown is True
            read = await client.get("/browser/settings", headers=AUTH)
            assert read.json()["license"] == {"configured": True, "source": "manager", "editable": False}
            assert read.json()["error"] == BROWSER_SETTINGS_WRITE_UNKNOWN
            before = len(calls)
            retry = await client.put("/browser/settings", headers=AUTH, json={"license_key": key})
            assert retry.status_code == 409
            assert len(calls) == before
            assert backend.settings_write_unknown is True
    asyncio.run(run())


@pytest.mark.parametrize("phase", ["get", "preflight", "write", "postflight", "body"])
def test_route_total_and_body_deadlines(tmp_path, monkeypatch, phase):
    real_timeout = asyncio.timeout
    timeouts = []

    def short_timeout(delay):
        timeouts.append(delay)
        return real_timeout(0.02 if (phase == "body" and delay == 10)
                            or (phase != "body" and delay in (25, 350)) else delay)

    async def run():
        saved = False

        async def respond(request):
            nonlocal saved
            if phase in ("get", "preflight") or (phase == "write" and request.method == "PUT") or saved:
                await asyncio.Event().wait()
            if request.method == "PUT":
                saved = True
            return httpx.Response(200, json=STATUS if request.url.path.endswith("/status")
                                  else {"license_key_set": False})

        async def slow_body():
            yield b'{"license_key":"'
            await asyncio.Event().wait()

        async with worker(tmp_path, respond) as (client, backend, calls):
            monkeypatch.setattr("worker.app.asyncio.timeout", short_timeout)
            if phase == "get":
                response = await client.get("/browser/settings", headers=AUTH)
            elif phase == "body":
                response = await client.put("/browser/settings", headers=AUTH, content=slow_body())
            else:
                response = await client.put("/browser/settings", headers=AUTH, json={"license_key": SECRET})
            assert response.status_code == (408 if phase == "body" else 504)
            assert SECRET not in response.text
            assert timeouts == ([25] if phase == "get" else [350, 10])
            assert backend.settings_write_unknown is (phase == "write")
            assert not backend.settings_lock.locked()
            assert sum(r.method == "PUT" for r in calls) == int(phase in ("write", "postflight"))
            if phase == "write":
                assert response.json()["detail"] == BROWSER_SETTINGS_WRITE_UNKNOWN
            if phase == "body":
                assert calls == []
    asyncio.run(run())
