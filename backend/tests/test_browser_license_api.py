from __future__ import annotations

import asyncio
import gzip
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import select

from app.core.auth import CSRF_COOKIE
from app.core.config import Settings
from app.models.integration import IntegrationConfig
from app.models.setting import AppSetting
from app.schemas.lane import BROWSER_BACKEND_UNAVAILABLE, BrowserSettingsView
from app.services.assistant_operations import _known_keys, _sanitize_log
from app.services.lanes import LaneService, WorkerRequestError


PATH = "/api/v1/lanes/browser-settings"
PRIVATE = "private-license-never-public"
TOKEN = "private-worker-bearer-token"


def public_status(**overrides):
    return {
        "backend": "cloak-manager", "connected": True,
        "license": {"configured": False, "source": "unset", "editable": True},
        "license_tier": "keyless", "binary_version": "146.0.7680.177.5", "error": None,
        **overrides,
    }


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk


@pytest.fixture
def worker(client, monkeypatch):
    settings = client.app.state.settings
    settings.browser_token = SecretStr(TOKEN)
    settings.cloakbrowser_license_key = None
    settings.worker_url = "http://fixed-worker:6300"
    real_client = httpx.AsyncClient
    requests, options = [], []

    def install(responder):
        def handle(request):
            requests.append(request)
            response = responder(request)
            if isinstance(response, dict):
                return httpx.Response(200, stream=Chunks([json.dumps(response).encode()]))
            return response

        def factory(**kwargs):
            options.append(kwargs)
            return real_client(transport=httpx.MockTransport(handle), **kwargs)

        monkeypatch.setattr("app.services.lanes.httpx.AsyncClient", factory)

    install(lambda request: public_status())
    return SimpleNamespace(
        install=install, requests=requests, options=options, settings=settings,
    )


@pytest.mark.parametrize("method", ["GET", "PUT"])
@pytest.mark.parametrize("key", [PRIVATE, ""])
def test_authentication_and_no_store(anon_client, monkeypatch, method, key):
    def unexpected(*args, **kwargs):
        pytest.fail("unauthenticated request contacted worker")

    monkeypatch.setattr("app.services.lanes.httpx.AsyncClient", unexpected)
    response = anon_client.request(method, PATH, json={"license_key": key})
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"
    assert PRIVATE not in response.text


@pytest.mark.parametrize("csrf", ["missing", "wrong", "wrong-cookie"])
@pytest.mark.parametrize("key", [PRIVATE, ""])
def test_put_requires_csrf(client, worker, csrf, key):
    if csrf == "missing":
        client.headers.pop("X-CSRF-Token")
    elif csrf == "wrong":
        client.headers["X-CSRF-Token"] = "wrong"
    else:
        client.cookies.set(CSRF_COOKIE, "wrong")
    response = client.put(PATH, json={"license_key": key})
    assert response.status_code == 403
    assert response.headers["Cache-Control"] == "no-store"
    assert worker.requests == []


@pytest.mark.parametrize("method,key,deadline", [
    ("GET", PRIVATE, 30.0), ("PUT", f"  {PRIVATE}  ", 360.0), ("PUT", "", 360.0),
])
def test_fixed_worker_request_and_public_projection(client, worker, method, key, deadline):
    status = public_status()
    worker.install(lambda request: {
        **status, "license_key": PRIVATE, "manager_url": "http://private-manager",
        "mask": PRIVATE, "license": {**status["license"], "mask": PRIVATE},
    })
    response = client.request(
        method, PATH + "?manager_url=http://attacker/", json={"license_key": key},
    )
    assert response.status_code == 200, response.text
    assert response.json() == status
    assert response.headers["Cache-Control"] == "no-store"
    assert PRIVATE not in response.text and TOKEN not in response.text
    assert len(worker.requests) == 1
    request = worker.requests[0]
    assert str(request.url) == "http://fixed-worker:6300/browser/settings"
    assert request.method == method
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["Accept-Encoding"] == "identity"
    assert "origin" not in request.headers and "cookie" not in request.headers
    if method == "PUT":
        assert json.loads(request.content) == {"license_key": key.strip()}
    else:
        assert not request.content
    assert worker.options == [{"timeout": deadline, "follow_redirects": False, "trust_env": False}]


@pytest.mark.parametrize("key", [PRIVATE, " ", "\t"])
@pytest.mark.parametrize("body", ["invalid-json", json.dumps({"license_key": PRIVATE}),
                                  json.dumps({"license_key": ""})])
def test_api_env_lock_precedes_worker_and_body_validation(client, worker, key, body):
    worker.settings.cloakbrowser_license_key = SecretStr(key)
    worker.settings.browser_token = None
    response = client.put(PATH, content=body)
    assert response.status_code == 409
    assert response.headers["Cache-Control"] == "no-store"
    assert PRIVATE not in response.text
    assert worker.requests == [] and worker.options == []


@pytest.mark.parametrize("backend", ["cloak-manager", "unsupported"])
@pytest.mark.parametrize("env_value", [PRIVATE, " ", ""])
def test_get_env_overlay_only_for_cloak(client, worker, backend, env_value):
    worker.settings.cloakbrowser_license_key = SecretStr(env_value)
    status = public_status(backend=backend)
    worker.install(lambda request: status)
    response = client.get(PATH)
    assert response.status_code == 200
    expected = ({"configured": True, "source": "env", "editable": False}
                if env_value and backend == "cloak-manager" else status["license"])
    assert response.json()["license"] == expected
    assert PRIVATE not in response.text


def test_empty_api_env_allows_put_and_no_database_storage(client, worker):
    worker.settings.cloakbrowser_license_key = SecretStr("")
    with client.app.state.session_factory() as db:
        before = [list(db.scalars(select(model))) for model in (AppSetting, IntegrationConfig)]
    response = client.put(PATH, json={"license_key": PRIVATE})
    assert response.status_code == 200
    assert len(worker.requests) == 1
    with client.app.state.session_factory() as db:
        after = [list(db.scalars(select(model))) for model in (AppSetting, IntegrationConfig)]
    assert [len(rows) for rows in after] == [len(rows) for rows in before]
    assert PRIVATE not in response.text


@pytest.mark.parametrize("method", ["GET", "PUT"])
@pytest.mark.parametrize("status,expected", [
    (409, 409), (422, 422), (502, 502), (503, 503), (504, 504),
    (400, 502), (401, 502), (403, 502), (404, 502), (500, 502), (307, 502), (201, 502),
])
def test_http_errors_are_static_and_never_read_or_retried(client, worker, method, status, expected):
    stream = Chunks([PRIVATE.encode()])
    worker.install(lambda request: httpx.Response(
        status, stream=stream, headers={"location": "http://attacker/" + PRIVATE},
    ))
    response = client.request(method, PATH, json={"license_key": PRIVATE})
    assert response.status_code == expected
    assert response.headers["Cache-Control"] == "no-store"
    assert PRIVATE not in response.text and TOKEN not in response.text
    assert stream.reads == 0
    assert len(worker.requests) == 1
    assert set(response.json()) == {"detail"}


@pytest.mark.parametrize("method", ["GET", "PUT"])
@pytest.mark.parametrize("failure,expected", [(httpx.ReadTimeout, 504), (httpx.ConnectError, 503)])
def test_transport_errors_are_secret_safe_without_retries(client, worker, method, failure, expected):
    def fail(request):
        raise failure(PRIVATE + TOKEN, request=request)

    worker.install(fail)
    response = client.request(method, PATH, json={"license_key": PRIVATE})
    assert response.status_code == expected
    assert response.headers["Cache-Control"] == "no-store"
    assert PRIVATE not in response.text and TOKEN not in response.text
    assert len(worker.requests) == 1


@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_missing_worker_token_fails_closed(client, worker, method):
    worker.settings.browser_token = None
    response = client.request(method, PATH, json={"license_key": PRIVATE})
    assert response.status_code == 503
    assert response.headers["Cache-Control"] == "no-store"
    assert worker.requests == [] and worker.options == []


@pytest.mark.parametrize("body", [
    b"not-json", b"[]", b"null", b'"private-license-never-public"',
    b"{}", b'{"license_key":null}', b'{"license_key":true}', b'{"license_key":123}',
    b'{"license_key":[]}', b'{"license_key":"   "}', b'{"license_key":"\\u00a0"}',
    b'{"license_key":"private-license-never-public","manager_url":"http://attacker"}',
    b'{"license_key":"one","license_key":"private-license-never-public"}',
    b'{"license_key":"private-license-never-public","license_key":""}',
    b'{"license_key":"private-license-never-public\\n"}',
    b'{"license_key":"private-license-never-public\\u007f"}',
    b'{"license_key":"private-license-never-public\\u200b"}',
    b'{"license_key":"private-license-never-public\\ud800"}',
    json.dumps({"license_key": "x" * 4097}).encode(),
    b'{"license_key":"\xff"}',
])
def test_manual_validation_is_static_and_does_not_call_worker(client, worker, body):
    response = client.put(PATH, content=body)
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid browser license key"}
    assert response.headers["Cache-Control"] == "no-store"
    assert PRIVATE not in response.text
    assert worker.requests == []


def test_body_limit_including_streamed_request(client, worker):
    response = client.put(PATH, content=iter([b" " * 4096, b" " * 4096, PRIVATE.encode()]))
    assert response.status_code == 413
    assert response.headers["Cache-Control"] == "no-store"
    assert PRIVATE not in response.text
    assert worker.requests == []


def test_maximum_key_and_body_are_accepted(client, worker):
    key = "x" * 4096
    body = json.dumps({"license_key": " " + key + " "}).encode()
    response = client.put(PATH, content=body + b" " * (8192 - len(body)))
    assert response.status_code == 200
    assert json.loads(worker.requests[0].content) == {"license_key": key}


@pytest.mark.parametrize("override", [
    {"backend": PRIVATE}, {"backend": ["cloak-manager"]}, {"connected": "true"},
    {"license_tier": PRIVATE}, {"license_tier": ["free"]},
    {"binary_version": PRIVATE}, {"binary_version": "********"}, {"binary_version": "1.2\n"},
    {"binary_version": "1" * 65}, {"binary_version": 146},
    {"license": {"configured": 1, "source": "manager", "editable": True}},
    {"license": {"configured": True, "source": PRIVATE, "editable": True}},
    {"license": {"configured": True, "source": "manager", "editable": "false"}},
    {"license": []}, {"license": {}},
])
def test_worker_projection_is_strict(client, worker, override):
    worker.install(lambda request: public_status(**override))
    response = client.get(PATH)
    assert response.status_code == 502
    assert response.headers["Cache-Control"] == "no-store"
    assert PRIVATE not in response.text


@pytest.mark.parametrize("error", [PRIVATE, {"secret": PRIVATE}, [PRIVATE], "", False])
def test_worker_status_error_is_always_sanitized(client, worker, error):
    worker.install(lambda request: public_status(error=error))
    response = client.get(PATH)
    assert response.status_code == 200
    assert response.json()["error"] == BROWSER_BACKEND_UNAVAILABLE
    assert response.json()["license"]["editable"] is False
    assert PRIVATE not in response.text


@pytest.mark.parametrize("configured,source", [(True, "manager"), (False, "unset"), (False, "unknown")])
def test_get_after_unknown_save_keeps_editing_locked(client, worker, configured, source):
    worker.install(lambda request: public_status(
        license={"configured": configured, "source": source, "editable": False},
        error=PRIVATE + " unknown save guard",
    ))
    response = client.get(PATH)
    assert response.status_code == 200
    assert response.json()["license"] == {
        "configured": configured, "source": source, "editable": False,
    }
    assert response.json()["error"] == BROWSER_BACKEND_UNAVAILABLE
    assert "刷新确认" in response.json()["error"]
    assert PRIVATE not in response.text and "unknown save guard" not in response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert len(worker.requests) == 1


@pytest.mark.parametrize("license", [
    {"configured": False, "source": "manager", "editable": True},
    {"configured": True, "source": "unset", "editable": True},
    {"configured": True, "source": "unknown", "editable": False},
    {"configured": False, "source": "env", "editable": False},
    {"configured": True, "source": "env", "editable": True},
    {"configured": True, "source": "manager", "editable": False},
    {"configured": False, "source": "unset", "editable": False},
])
def test_incoherent_cloak_license_state_is_rejected(client, worker, license):
    worker.install(lambda request: public_status(license=license))
    response = client.get(PATH)
    assert response.status_code == 502
    assert response.json()["detail"] == BROWSER_BACKEND_UNAVAILABLE
    assert response.headers["Cache-Control"] == "no-store"


def test_env_overlay_precedes_license_coherence_validation(client, worker):
    worker.settings.cloakbrowser_license_key = SecretStr(PRIVATE)
    worker.install(lambda request: public_status(
        license={"configured": False, "source": "manager", "editable": False},
    ))
    response = client.get(PATH)
    assert response.status_code == 200
    assert response.json()["license"] == {"configured": True, "source": "env", "editable": False}


@pytest.mark.parametrize("body", [b"not-json-" + PRIVATE.encode(), b"[]", b"{}", b"\xff", b"[" * 2000])
def test_malformed_worker_json_is_private(client, worker, body):
    worker.install(lambda request: httpx.Response(200, stream=Chunks([body])))
    response = client.get(PATH)
    assert response.status_code == 502
    assert response.headers["Cache-Control"] == "no-store"
    assert PRIVATE not in response.text


def test_worker_response_is_bounded(client, worker):
    stream = Chunks([b" " * 8192] * 10)
    worker.install(lambda request: httpx.Response(200, stream=stream))
    response = client.get(PATH)
    assert response.status_code == 502
    assert stream.reads == 9
    assert response.headers["Cache-Control"] == "no-store"


def test_maximum_worker_response_is_accepted(client, worker):
    body = json.dumps(public_status()).encode()
    body += b" " * (65536 - len(body))
    worker.install(lambda request: httpx.Response(200, stream=Chunks([body])))
    response = client.get(PATH)
    assert response.status_code == 200
    assert response.json() == public_status()


def test_compressed_response_is_rejected_without_decoding(client, worker):
    stream = Chunks([gzip.compress(b" " * 1000000)])
    worker.install(lambda request: httpx.Response(
        200, stream=stream, headers={"Content-Encoding": "gzip"},
    ))
    response = client.get(PATH)
    assert response.status_code == 502
    assert stream.reads == 0
    assert response.headers["Cache-Control"] == "no-store"


def test_unexpected_failure_is_private_and_not_cacheable(client, worker, monkeypatch):
    async def fail(*args):
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(LaneService, "browser_settings", fail)
    response = client.get(PATH)
    assert response.status_code == 500
    assert response.headers["Cache-Control"] == "no-store"
    assert PRIVATE not in response.text


@pytest.mark.parametrize("license_key,deadline", [(None, 30.0), (PRIVATE, 360.0), ("", 360.0)])
def test_total_deadline_has_no_retry(client, worker, monkeypatch, license_key, deadline):
    deadlines = []

    class Expired:
        async def __aenter__(self):
            raise TimeoutError(PRIVATE)

        async def __aexit__(self, *args):
            pass

    def timeout(value):
        deadlines.append(value)
        return Expired()

    monkeypatch.setattr("app.services.lanes.asyncio.timeout", timeout)
    with pytest.raises(WorkerRequestError) as error:
        asyncio.run(LaneService(worker.settings).browser_settings(license_key))
    assert error.value.status_code == 504
    assert PRIVATE not in str(error.value)
    assert deadlines == [deadline]
    assert worker.requests == []


@pytest.mark.parametrize("license_key,deadline", [(None, 30.0), (PRIVATE, 360.0), ("", 360.0)])
def test_total_deadline_includes_response_stream(client, worker, monkeypatch, license_key, deadline):
    real_timeout = asyncio.timeout
    deadlines = []

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(1)
            yield json.dumps(public_status()).encode()

    def timeout(value):
        deadlines.append(value)
        return real_timeout(0.02)

    worker.install(lambda request: httpx.Response(200, stream=SlowStream()))
    monkeypatch.setattr("app.services.lanes.asyncio.timeout", timeout)
    with pytest.raises(WorkerRequestError) as error:
        asyncio.run(LaneService(worker.settings).browser_settings(license_key))
    assert error.value.status_code == 504
    assert deadlines == [deadline]
    assert len(worker.requests) == 1


def test_public_schema_rejects_extra_fields_and_private_errors():
    with pytest.raises(ValidationError):
        BrowserSettingsView.model_validate(public_status(license_key=PRIVATE))
    with pytest.raises(ValidationError):
        BrowserSettingsView.model_validate(public_status(error=PRIVATE))


def test_environment_alias_is_secret(monkeypatch):
    monkeypatch.setenv("CLOAKBROWSER_LICENSE_KEY", " ")
    settings = Settings(_env_file=None)
    assert isinstance(settings.cloakbrowser_license_key, SecretStr)
    assert settings.cloakbrowser_license_key.get_secret_value() == " "
    assert LaneService(settings).browser_license_env_locked()
    monkeypatch.setenv("CLOAKBROWSER_LICENSE_KEY", PRIVATE)
    settings = Settings(_env_file=None)
    assert PRIVATE not in repr(settings)
    assert PRIVATE not in settings.model_dump_json()


def test_license_and_worker_token_are_known_redaction_keys(client, worker):
    worker.settings.cloakbrowser_license_key = SecretStr(PRIVATE)
    tools = SimpleNamespace(config={}, manager=SimpleNamespace(
        settings=worker.settings, crypto=client.app.state.crypto,
    ))
    with client.app.state.session_factory() as db:
        keys = _known_keys(db, tools)
    assert PRIVATE in keys and TOKEN in keys
    sanitized = _sanitize_log(f"diagnostic {PRIVATE} {TOKEN}", keys)
    assert PRIVATE not in sanitized and TOKEN not in sanitized
    assert "diagnostic" in sanitized
