from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import threading
import time
from uuid import UUID, uuid4

import httpx
from httpx import AsyncClient as RealAsyncClient
import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app.api.deps import get_db
from app.api.v1 import assistant as api
from app.core.auth import SESSION_COOKIE
from app.main import create_app
from app.models.assistant import AssistantMessage, AssistantSession
from app.models.auth import AuthSession
from app.models.workflow import Site, SiteCredential, WorkflowStep
from app.schemas.assistant import CreateSession, SendMessage, SetTarget
from app.services import assistant as service
from app.services.assistant import AssistantError, AssistantManager, BrowserScope
from app.services.assistant_tools import AssistantTools
from app.services.config_ai_generation import GenerationError
from app.services.integrations import IntegrationService
from tests.helpers import login_client, make_settings
from tests.test_assistant_snapshot import node, selector_result, snapshot


ROOT = "/api/v1/assistant/sessions"
KEY = "fake-assistant-runtime-key-never-export"
PRIVATE = "private-provider-exception-never-export"
CONFIG = {"base_url": "https://provider.example/v1", "model": "test-model",
          "api_key": KEY, "timeout": 30, "supports_vision": False}
TARGET = {"name": "Example", "base_url": "https://example.com/", "credential_mode": "manual",
          "confirmed": True, "proxy": False, "browser_consent": True}
EMPTY = {"steps": [], "groups": [], "signals": []}


@pytest.fixture
def assistant_client(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, config_ai_enabled=True)
    app = create_app(settings)
    manager = app.state.assistant_manager

    @asynccontextmanager
    async def lifespan(app):
        manager.recover()
        yield
        await manager.shutdown()

    app.router.lifespan_context = lifespan
    monkeypatch.setattr(IntegrationService, "vision_runtime", lambda self: dict(CONFIG))
    with TestClient(app) as client:
        login_client(client)
        yield client


@pytest.fixture
def provider(assistant_client, monkeypatch):
    responses, requests, options = [], [], []
    title_responses, title_requests = [], []
    assistant_client.app.state.title_provider = (title_responses, title_requests)
    summary_responses, summary_requests = [], []
    assistant_client.app.state.summary_provider = (summary_responses, summary_requests)

    async def handler(request):
        if ("chat/completions" in str(request.url)
                and "tools" not in json.loads(request.content)):
            is_summary = "SiteFlow context compaction" in json.loads(request.content)["messages"][0]["content"]
            queue, captured = ((summary_responses, summary_requests) if is_summary
                               else (title_responses, title_requests))
            captured.append(request)
            # Existing provider scenarios remain independent of optional title requests.
            response = queue.pop(0) if queue else httpx.Response(200, text="invalid")
            if callable(response):
                response = await response(request)
            if isinstance(response, BaseException):
                raise response
            return response if isinstance(response, httpx.Response) else httpx.Response(200, json=response)
        requests.append(request)
        assert responses, "Unexpected provider/worker request"
        response = responses.pop(0)
        if callable(response):
            response = await response(request)
        if isinstance(response, BaseException):
            raise response
        return response if isinstance(response, httpx.Response) else httpx.Response(200, json=response)

    def factory(**kwargs):
        options.append(kwargs)
        return RealAsyncClient(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(service.httpx, "AsyncClient", factory)
    return responses, requests, options


@pytest.fixture
def title_provider(assistant_client, provider):
    return assistant_client.app.state.title_provider


def text(content="Which website and account sourcing would you like?"):
    return {"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": content, "reasoning_content": "private-reasoning-not-history",
    }}]}


def tool(name, arguments=None, call_id="call"):
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "tool_calls": [{"id": call_id, "type": "function", "function": {
            "name": name, "arguments": json.dumps({"title": "Inspect safely", **(arguments or {})}),
        }}],
    }}]}


def browser_open_response(worker_id, lane_key, expires_at):
    async def response(request):
        payload = json.loads(request.content)
        worker_id.append(payload["session_id"])
        return {"session_id": payload["session_id"], "lane_key": lane_key, "expires_at": expires_at}
    return response


def create(client):
    response = client.post(ROOT, json={"consent_to_send": True})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def target(client, identifier, **changes):
    response = client.put(f"{ROOT}/{identifier}/target", json={**TARGET, **changes})
    assert response.status_code == 200, response.text
    return response.json()


def send(client, identifier, *, content="Help me configure this site", request_id=None, status=202):
    response = client.post(f"{ROOT}/{identifier}/messages", json={
        "content": content, "consent_to_send": True, "request_id": str(request_id or uuid4()),
    })
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    return response


def wait(client, identifier):
    for _ in range(300):
        response = client.get(f"{ROOT}/{identifier}")
        assert response.status_code == 200, response.text
        if response.json()["status"] == "idle":
            return response.json()
        time.sleep(0.01)
    pytest.fail("Assistant did not finish")


def counts(client):
    with client.app.state.session_factory() as db:
        return tuple(db.scalar(select(func.count()).select_from(model))
                     for model in (Site, SiteCredential, WorkflowStep))


def test_clarification_and_exact_shape_persistent_encrypted_history(assistant_client, provider):
    client = assistant_client
    responses, requests, options = provider
    identifier = create(client)
    detail = client.get(f"{ROOT}/{identifier}").json()
    assert detail == {"id": identifier, "title": "\u65b0\u4f1a\u8bdd", "status": "idle",
                       "title_locked": False, "title_revision": 0,
                        "retry_authorizations": [], "unresolved_operations": [], "pending_approval": None,
                        "pending_interaction": None,
                      "target": None, "browser": None, "messages": detail["messages"]}
    assert len(detail["messages"]) == 1
    assert detail["messages"][0]["role"] == "assistant"
    assert detail["messages"][0]["content"] == service.GREETING
    before = counts(client)
    responses.append(text())
    assert send(client, identifier).json()["status"] == "running"
    detail = wait(client, identifier)
    assert set(detail) == {"id", "title", "title_locked", "title_revision", "status", "target", "browser", "messages", "usage", "retry_authorizations", "unresolved_operations", "pending_approval", "pending_interaction"}
    assert [message["role"] for message in detail["messages"]] == ["assistant", "user", "assistant"]
    assert set(detail["messages"][0]) == {"id", "role", "content", "created_at"}
    assert "private-reasoning" not in json.dumps(detail)
    assert client.get(ROOT).json()["items"][0]["id"] == identifier
    assert options[0]["trust_env"] is False and options[0]["follow_redirects"] is False
    request = json.loads(requests[0].content)
    assert request["stream"] is False and request["tool_choice"] == "auto"
    assert KEY not in requests[0].content.decode()
    assert counts(client) == before
    manager = client.app.state.assistant_manager
    with manager.session_factory() as db:
        session = db.get(AssistantSession, identifier)
        assert b"Help me" not in session.ciphertext
        assert manager.crypto.decrypt(f"assistant:session:{identifier}", session.ciphertext,
                                      session.nonce, session.key_version)
        messages = list(db.scalars(select(AssistantMessage)))
        for message in messages:
            assert b"Help me" not in message.ciphertext
            plaintext = manager.crypto.decrypt(f"assistant:message:{message.uuid}", message.ciphertext,
                                               message.nonce, message.key_version)
            assert "private-reasoning" not in plaintext
        with pytest.raises(Exception):
            manager.crypto.decrypt(f"assistant:message:{uuid4()}", messages[0].ciphertext,
                                   messages[0].nonce, messages[0].key_version)
    fresh = AssistantManager(manager.session_factory, manager.settings, manager.crypto)
    assert fresh.detail("admin", identifier) == detail


@pytest.mark.parametrize("method,suffix", [
    ("get", ""), ("get", "/events"), ("put", "/target"), ("put", "/title"),
    ("post", "/messages"), ("post", "/cancel"), ("delete", ""),
])
def test_all_routes_enforce_owner(assistant_client, method, suffix):
    client = assistant_client
    identifier = create(client)
    with client.app.state.session_factory() as db:
        db.execute(update(AssistantSession).values(owner="other-admin"))
        db.commit()
    kwargs = {}
    if suffix == "/target":
        kwargs["json"] = TARGET
    elif suffix == "/title":
        kwargs["json"] = {"title": "Renamed"}
    elif suffix == "/messages":
        kwargs["json"] = {"content": "hello", "consent_to_send": True, "request_id": str(uuid4())}
    response = getattr(client, method)(f"{ROOT}/{identifier}{suffix}", **kwargs)
    assert response.status_code == 404, response.text
    assert client.get(ROOT).json() == {"items": []}


@pytest.mark.parametrize("suffix,method", [("", "post"), ("/target", "put"),
                                          ("/title", "put"),
                                          ("/messages", "post"), ("/cancel", "post"), ("", "delete")])
def test_mutations_require_csrf_without_touching_auth(assistant_client, suffix, method):
    client = assistant_client
    identifier = create(client)
    with client.app.state.session_factory() as db:
        auth = db.scalar(select(AuthSession))
        auth.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        db.commit()
        original = auth.last_seen_at
    client.headers.pop("X-CSRF-Token")
    if method == "post" and suffix == "":
        url, body = ROOT, {"consent_to_send": True}
    else:
        url = f"{ROOT}/{identifier}{suffix}"
        body = {"title": "Renamed"} if suffix == "/title" else TARGET if suffix == "/target" else {
            "content": "hello", "consent_to_send": True, "request_id": str(uuid4()),
        } if suffix == "/messages" else None
    response = getattr(client, method)(url, **({"json": body} if body else {}))
    assert response.status_code == 403
    with client.app.state.session_factory() as db:
        assert db.scalar(select(AuthSession)).last_seen_at.replace(tzinfo=None) == original.replace(tzinfo=None)


def test_anonymous_and_revoked_auth(assistant_client):
    client = assistant_client
    identifier = create(client)
    client.cookies.clear()
    for path in (ROOT, f"{ROOT}/{identifier}", f"{ROOT}/{identifier}/events"):
        assert client.get(path).status_code == 401
    assert client.post(ROOT, json={"consent_to_send": True}).status_code == 401


@pytest.mark.parametrize("payload", [
    {"consent_to_send": 1}, {"consent_to_send": "true"},
    {"title": "x" * 129}, {"consent_to_send": True, "credentials": PRIVATE},
])
def test_static_validation_and_body_limit(assistant_client, payload):
    response = assistant_client.post(ROOT, json=payload)
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "invalid_request"}}
    assert PRIVATE not in response.text
    response = assistant_client.post(ROOT, content=b" " * (128 * 1024 + 1))
    assert response.status_code == 413
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("url", [
    "http://localhost/", "http://127.0.0.1/", "http://10.0.0.1/", "http://[::1]/",
    "http://2130706433/", "http://0x7f000001/", "https://user:password@example.com/",
    "file:///etc/passwd",
    "https://example.com/\\evil", "https://example.com/%0aevil", "https://example.com:99999/",
])
def test_target_rejects_unsafe_urls(assistant_client, url):
    identifier = create(assistant_client)
    response = assistant_client.put(f"{ROOT}/{identifier}/target", json={**TARGET, "base_url": url})
    assert response.status_code == 422
    assert url not in response.text


@pytest.mark.parametrize("field,value", [("confirmed", False), ("confirmed", 1),
                                         ("browser_consent", "true"), ("browser_consent", False),
                                         ("proxy", "false"), ("credential_mode", "provider-id")])
def test_target_requires_host_confirmation(assistant_client, field, value):
    identifier = create(assistant_client)
    response = assistant_client.put(f"{ROOT}/{identifier}/target", json={**TARGET, field: value})
    assert response.status_code == 422


@pytest.mark.parametrize("grant", [
    {}, {"confirmed": True}, {"browser_consent": True}, {"authorized": None},
    *[{"authorized": value} for value in (False, 0, 1, "true", "false")],
    {"authorized": True, "confirmed": False},
    {"authorized": True, "browser_consent": False},
])
def test_target_requires_explicit_overall_or_complete_legacy_grant(assistant_client, grant):
    client = assistant_client
    identifier = create(client)
    response = client.put(f"{ROOT}/{identifier}/target", json={
        **{key: value for key, value in TARGET.items()
           if key not in {"confirmed", "browser_consent"}}, **grant,
    })
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "invalid_request"}}
    detail = client.get(f"{ROOT}/{identifier}").json()
    assert detail["target"] is None and detail["status"] == "idle"


def test_disabled_and_unconfigured_runtime_are_static(assistant_client, monkeypatch):
    client = assistant_client
    identifier = create(client)
    client.app.state.settings.config_ai_enabled = False
    assert client.post(ROOT, json={}).status_code == 200
    send(client, identifier, status=503)
    client.app.state.settings.config_ai_enabled = True
    monkeypatch.setattr(IntegrationService, "vision_runtime", lambda self: None)
    assert send(client, identifier, status=503).json() == {"detail": {"code": "model_unavailable"}}


@pytest.mark.parametrize("payload,locked,title", [
    ({}, False, "\u65b0\u4f1a\u8bdd"),
    ({"consent_to_send": False}, False, "\u65b0\u4f1a\u8bdd"),
    ({"consent_to_send": True}, False, "\u65b0\u4f1a\u8bdd"),
    ({"title": "  "}, False, "\u65b0\u4f1a\u8bdd"),
    ({"title": None}, False, "\u65b0\u4f1a\u8bdd"),
    ({"title": "  Explicit  "}, True, "Explicit"),
    ({"title": "\u65b0\u4f1a\u8bdd"}, True, "\u65b0\u4f1a\u8bdd"),
])
def test_creation_is_local_and_persists_greeting(assistant_client, monkeypatch, payload, locked, title):
    client = assistant_client
    manager = client.app.state.assistant_manager
    client.app.state.settings.config_ai_enabled = False

    def forbidden(*args, **kwargs):
        pytest.fail("Creating a session must not consult runtime or contact a provider")

    monkeypatch.setattr(manager, "_runtime", forbidden)
    monkeypatch.setattr(service.httpx, "AsyncClient", forbidden)
    response = client.post(ROOT, json=payload)
    assert response.status_code == 200
    detail = response.json()
    assert detail["title"] == title
    assert detail["title_locked"] is locked
    assert detail["title_revision"] == 0
    assert detail["status"] == "idle"
    assert [item["role"] for item in detail["messages"]] == ["assistant"]
    assert detail["messages"][0]["content"] == service.GREETING
    assert client.get(f"{ROOT}/{detail['id']}").json() == detail
    item = client.get(ROOT).json()["items"][0]
    assert (item["title"], item["title_locked"], item["title_revision"]) == (title, locked, 0)
    with manager.session_factory() as db:
        assert manager._decrypt(db.get(AssistantSession, detail["id"]))["title_locked"] is locked


def test_send_still_requires_explicit_consent(assistant_client):
    identifier = create(assistant_client)
    for consent in ({}, {"consent_to_send": False}, {"consent_to_send": 1}):
        response = assistant_client.post(f"{ROOT}/{identifier}/messages", json={
            "content": "hello", "request_id": str(uuid4()), **consent,
        })
        assert response.status_code == 422


def test_rename_locks_even_same_title_without_runtime(assistant_client, monkeypatch):
    client = assistant_client
    identifier = create(client)
    manager = client.app.state.assistant_manager
    client.app.state.settings.config_ai_enabled = False

    def forbidden():
        pytest.fail("Rename must not require runtime")

    monkeypatch.setattr(manager, "_runtime", forbidden)
    for revision, title in enumerate(["\u65b0\u4f1a\u8bdd", "Renamed", "Renamed"], 1):
        response = client.put(f"{ROOT}/{identifier}/title", json={"title": f"  {title}  "})
        assert response.status_code == 200
        detail = response.json()
        assert detail["title"] == title
        assert detail["title_locked"] is True
        assert detail["title_revision"] == revision
        assert detail["status"] == "idle"
        assert len(detail["messages"]) == 1
    for title in ("", "   ", "x" * 129, None, 123):
        assert client.put(f"{ROOT}/{identifier}/title", json={"title": title}).status_code == 422
    assert client.get(f"{ROOT}/{identifier}").json() == detail


def test_legacy_title_defaults_locked_and_can_be_renamed(assistant_client, provider, title_provider):
    client = assistant_client
    identifier = create(client)
    manager = client.app.state.assistant_manager
    with manager.session_factory() as db:
        row = db.get(AssistantSession, identifier)
        manager._encrypt(row, {"title": "Legacy", "target": None})
        db.commit()
    provider[0].append(text("Legacy reply"))
    send(client, identifier)
    detail = wait(client, identifier)
    assert detail["title_locked"] is True and detail["title_revision"] == 0
    assert client.get(ROOT).json()["items"][0]["title_locked"] is True
    assert not title_provider[1]
    renamed = client.put(f"{ROOT}/{identifier}/title", json={"title": "New"}).json()
    assert renamed["title_locked"] is True and renamed["title_revision"] == 1


def test_auto_titles_only_first_three_user_ordinals_and_replay(assistant_client, provider, title_provider):
    client = assistant_client
    identifier = create(client)
    replies, requests = title_provider
    for ordinal in range(1, 5):
        request_id = uuid4()
        provider[0].append(text(f"Reply {ordinal}"))
        if ordinal <= 3:
            replies.append(text(json.dumps({"title": f"Title {ordinal}"})))
        send(client, identifier, content=f"Question {ordinal}", request_id=request_id)
        detail = wait(client, identifier)
        assert detail["title"] == f"Title {min(ordinal, 3)}"
        assert detail["title_revision"] == min(ordinal, 3)
        assert detail["title_locked"] is False
        assert send(client, identifier, request_id=request_id).json() == detail
    assert len(requests) == 3
    for ordinal, request in enumerate(requests, 1):
        payload = json.loads(request.content)
        assert set(payload) == {"model", "stream", "messages"}
        assert payload["model"] == CONFIG["model"]
        assert [message["role"] for message in payload["messages"]] == ["system", "user"]
        assert json.loads(payload["messages"][1]["content"]) == [
            f"Question {index}" for index in range(1, ordinal + 1)
        ]
        assert request.headers["authorization"] == f"Bearer {KEY}"
        assert "Reply" not in request.content.decode()
        assert service.GREETING not in request.content.decode()
    assert not replies


def test_explicit_or_manual_title_never_auto_updates(assistant_client, provider, title_provider):
    client = assistant_client
    explicit = client.post(ROOT, json={"title": "Explicit"}).json()["id"]
    manual = create(client)
    assert client.put(f"{ROOT}/{manual}/title", json={"title": "Manual"}).status_code == 200
    for identifier, title in ((explicit, "Explicit"), (manual, "Manual")):
        provider[0].append(text("Reply"))
        send(client, identifier)
        assert wait(client, identifier)["title"] == title
    assert not title_provider[1]


def test_failed_and_cancelled_ordinals_do_not_generate_titles(assistant_client, provider, title_provider):
    client = assistant_client
    identifier = create(client)
    provider[0].append(httpx.Response(500))
    first = uuid4()
    send(client, identifier, request_id=first)
    wait(client, identifier)
    assert not title_provider[1]
    started = threading.Event()

    async def blocked(request):
        started.set()
        await asyncio.Event().wait()

    provider[0].append(blocked)
    send(client, identifier, content="Cancelled second")
    assert started.wait(2)
    assert client.post(f"{ROOT}/{identifier}/cancel").status_code == 200
    assert not title_provider[1]
    title_provider[0].append(text('{"title":"Third turn"}'))
    provider[0].append(text("Successful third"))
    send(client, identifier, content="Third")
    assert wait(client, identifier)["title"] == "Third turn"
    assert len(title_provider[1]) == 1
    assert len(json.loads(json.loads(title_provider[1][0].content)["messages"][1]["content"])) == 3
    send(client, identifier, request_id=first)
    provider[0].append(text("Fourth successful"))
    send(client, identifier)
    wait(client, identifier)
    assert len(title_provider[1]) == 1


def test_auto_title_counts_entire_history_not_context_window(assistant_client, provider, title_provider):
    client = assistant_client
    identifier = create(client)
    manager = client.app.state.assistant_manager
    with manager.session_factory() as db:
        for index in range(3):
            manager._message(db, identifier, "user", f"Old user {index}")
        for index in range(110):
            manager._message(db, identifier, "tool", "Untrusted DOM", title="Inspect", tool_name="browser_observe")
        db.commit()
    provider[0].append(text("Fourth user reply"))
    send(client, identifier)
    wait(client, identifier)
    assert not title_provider[1]


def test_auto_title_input_bounded_and_excludes_non_user_data(assistant_client, provider, title_provider):
    client = assistant_client
    identifier = create(client)
    manager = client.app.state.assistant_manager
    with manager.session_factory() as db:
        manager._message(db, identifier, "tool", "PRIVATE DOM", title="Inspect", tool_name="browser_observe")
        manager._message(db, identifier, "assistant", "PRIVATE ASSISTANT")
        db.commit()
    provider[0].append(text("Reply"))
    title_provider[0].append(text('{"title":"Bounded"}'))
    send(client, identifier, content="\u6d4b" * 12000)
    assert wait(client, identifier)["title"] == "Bounded"
    payload = json.loads(title_provider[1][0].content)
    assert len(payload["messages"][1]["content"].encode()) <= 8192
    assert "PRIVATE" not in title_provider[1][0].content.decode()


@pytest.mark.parametrize("failure", [
    httpx.Response(500, text=PRIVATE), httpx.ConnectError(PRIVATE), httpx.ReadTimeout(PRIVATE),
    httpx.Response(200, text=PRIVATE), httpx.Response(200, content=b" " * (16 * 1024 + 1)),
    httpx.Response(200, content=b"{}", headers={"Content-Encoding": "unsupported"}),
    text('{"title":""}'), text('{"title":"   "}'), text('{"title":123}'),
    text(json.dumps({"title": "x" * 129})), text('{"title":"A","extra":"B"}'),
    text('{"title":"A","title":"B"}'), text('{"title":NaN}'),
    text('```json\n{"title":"A"}\n```'), text(json.dumps({"title": KEY})),
    {"choices": [{"finish_reason": "length", "message": {"role": "assistant", "content": "{}"}}]},
    tool("list_features"),
])
def test_title_failure_is_silent_preserves_success_and_is_not_retried(
    assistant_client, provider, title_provider, failure,
):
    client = assistant_client
    identifier = create(client)
    provider[0].append(text("Completed reply"))
    title_provider[0].append(failure)
    request_id = uuid4()
    send(client, identifier, request_id=request_id)
    detail = wait(client, identifier)
    assert detail["title"] == "\u65b0\u4f1a\u8bdd" and detail["title_revision"] == 0
    assert detail["messages"][-1]["content"] == "Completed reply"
    assert [item["role"] for item in detail["messages"]] == ["assistant", "user", "assistant"]
    assert PRIVATE not in json.dumps(detail) and KEY not in json.dumps(detail)
    assert send(client, identifier, request_id=request_id).json() == detail
    assert len(title_provider[1]) == 1
    manager = client.app.state.assistant_manager
    with manager.session_factory() as db:
        row = db.get(AssistantSession, identifier)
        row.status = "running"
        db.commit()

    async def retry():
        await manager._auto_title("admin", identifier, CONFIG, asyncio.get_running_loop().time() + 1)

    asyncio.run(retry())
    manager._idle(identifier)
    assert len(title_provider[1]) == 1


@pytest.mark.parametrize("change", ["lock", "revision"])
def test_title_rechecks_encrypted_lock_and_revision(assistant_client, provider, title_provider, change):
    client = assistant_client
    identifier = create(client)
    manager = client.app.state.assistant_manager

    async def race(request):
        # Simulate an external writer, bypassing the idle-only rename endpoint.
        with manager.session_factory() as db:
            row = db.get(AssistantSession, identifier)
            data = manager._decrypt(row)
            data["title"] = "Newer title"
            if change == "lock":
                data["title_locked"] = True
            else:
                data["title_revision"] += 1
            manager._encrypt(row, data)
            db.commit()
        return text('{"title":"Stale generated title"}')

    provider[0].append(text("Completed reply"))
    title_provider[0].append(race)
    send(client, identifier)
    detail = wait(client, identifier)
    assert detail["title"] == "Newer title"
    assert detail["messages"][-1]["content"] == "Completed reply"


def test_title_timeout_uses_remaining_budget_without_failing_turn(assistant_client, monkeypatch):
    manager = assistant_client.app.state.assistant_manager
    identifier = create(assistant_client)
    seen = []
    real_timeout = asyncio.timeout

    async def conversation(*args):
        with manager.session_factory() as db:
            manager._message(db, identifier, "assistant", "Completed reply")
            db.commit()

    async def blocked(*args):
        seen.append("title")
        await asyncio.Event().wait()

    def shortened(delay):
        assert delay <= 10
        seen.append(delay)
        return real_timeout(0.01)

    monkeypatch.setattr(manager, "_conversation", conversation)
    monkeypatch.setattr(service, "generate_title", blocked)
    monkeypatch.setattr(service.asyncio, "timeout", shortened)

    async def run():
        await manager.send("admin", identifier, SendMessage(
            content="First user", consent_to_send=True, request_id=uuid4(),
        ))
        await manager.tasks[identifier]
        detail = manager.detail("admin", identifier)
        assert detail["status"] == "idle"
        assert detail["messages"][-1]["content"] == "Completed reply"
        assert detail["title_revision"] == 0
        assert not manager.tasks
        with manager.session_factory() as db:
            manager._message(db, identifier, "user", "Second user")
            db.get(AssistantSession, identifier).status = "running"
            db.commit()
        await manager._auto_title("admin", identifier, CONFIG, asyncio.get_running_loop().time() + 0.05)
        assert 0 < seen[-2] <= 0.05
        before = list(seen)
        await manager._auto_title("admin", identifier, CONFIG, asyncio.get_running_loop().time() - 1)
        assert seen == before
        manager._idle(identifier)

    asyncio.run(run())


def test_cancel_during_title_propagates_and_prevents_title_write(assistant_client, provider, title_provider):
    client = assistant_client
    identifier = create(client)
    started = threading.Event()
    cancelled = threading.Event()

    async def blocked(request):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    provider[0].append(text("Completed reply"))
    title_provider[0].append(blocked)
    send(client, identifier)
    assert started.wait(2)
    assert client.put(f"{ROOT}/{identifier}/title", json={"title": "Cannot race"}).status_code == 409
    detail = client.post(f"{ROOT}/{identifier}/cancel").json()
    assert cancelled.is_set()
    assert detail["status"] == "idle" and detail["title_revision"] == 0
    assert detail["messages"][-2]["content"] == "Completed reply"
    assert "cancelled" in detail["messages"][-1]["content"]
    assert not client.app.state.assistant_manager.tasks


def test_title_request_preserves_configured_token_budget(assistant_client, provider, title_provider, monkeypatch):
    config = {**CONFIG, "max_tokens": 1777}
    monkeypatch.setattr(IntegrationService, "vision_runtime", lambda self: config)
    identifier = create(assistant_client)
    provider[0].append(text("Reply"))
    title_provider[0].append(text('{"title":"Generated"}'))
    send(assistant_client, identifier)
    wait(assistant_client, identifier)
    assert json.loads(title_provider[1][0].content)["max_tokens"] == 1777
    assert json.loads(provider[1][0].content)["max_tokens"] == 1777


def test_idempotency_overlap_global_limit_cancel_delete(assistant_client, provider):
    client = assistant_client
    responses, requests, _ = provider
    started = threading.Event()

    async def blocked(request):
        started.set()
        await asyncio.Event().wait()

    first, second, third = (create(client) for _ in range(3))
    request_id = uuid4()
    responses.append(blocked)
    send(client, first, request_id=request_id)
    assert started.wait(2)
    replay = send(client, first, request_id=request_id, content="Changed replay must not append")
    assert len(replay.json()["messages"]) == 2
    assert send(client, first, status=409).json()["detail"]["code"] == "session_running"
    assert client.delete(f"{ROOT}/{first}").status_code == 409
    assert client.put(f"{ROOT}/{first}/target", json=TARGET).status_code == 409
    assert client.put(f"{ROOT}/{first}/title", json={"title": "Busy"}).status_code == 409
    responses.append(blocked)
    send(client, second)
    assert send(client, third, status=429).json()["detail"]["code"] == "assistant_busy"
    for identifier in (first, second):
        response = client.post(f"{ROOT}/{identifier}/cancel")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "idle"
    assert not client.app.state.assistant_manager.tasks
    assert send(client, first, request_id=request_id).json()["status"] == "idle"
    assert len(requests) == 2
    assert client.delete(f"{ROOT}/{first}").status_code == 204
    with client.app.state.session_factory() as db:
        assert db.scalar(select(AssistantMessage).where(AssistantMessage.session_id == first)) is None


def test_restart_finalizes_pending_tool_logs_once(assistant_client):
    client = assistant_client
    identifier = create(client)
    manager = client.app.state.assistant_manager
    request_id = str(uuid4())
    with manager.session_factory() as db:
        row = db.get(AssistantSession, identifier)
        row.status = "running"
        manager._message(db, identifier, "user", "persist me", request_id=request_id)
        manager._message(db, identifier, "tool", "", status="running", title="Observe", tool_name="browser_observe")
        db.commit()
    fresh = AssistantManager(manager.session_factory, manager.settings, manager.crypto)
    fresh.recover()
    detail = fresh.detail("admin", identifier)
    assert detail["status"] == "idle" and detail["browser"] is None
    assert detail["messages"][2]["status"] == "error"
    assert "interrupted" in detail["messages"][3]["content"]
    fresh.recover()
    assert fresh.detail("admin", identifier) == detail
    assert send(client, identifier, request_id=request_id).json() == detail


def test_browser_and_proposal_flow_no_workflow_writes(assistant_client, provider):
    client = assistant_client
    responses, requests, options = provider
    identifier = create(client)
    confirmed = target(client, identifier, base_url="https://EXAMPLE.com:443")
    assert confirmed["target"] == {
        **{k: v for k, v in TARGET.items() if k != "browser_consent"},
        "run_lane_policy": "auto", "workflow_site_id": None, "workflow_consent": False,
    }
    before = counts(client)
    worker_ids = []
    expires = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
    responses.extend([
        tool("browser_open", call_id="open"),
        browser_open_response(worker_ids, "interaction", expires),
        text("Please log in manually in BrowserView; visible page text may be sent to the model."),
    ])
    send(client, identifier)
    detail = wait(client, identifier)
    worker_id = worker_ids[0]
    assert detail["browser"] == {"lane_key": "interaction", "status": "ready", "expires_at": expires}
    assert worker_id not in json.dumps(detail)
    worker_request = next(request for request in requests if "/assistant-browser/open" in str(request.url))
    token = json.loads(worker_request.content)["token"]
    assert json.loads(worker_request.content)["session_id"] == worker_id
    assert next(option for option in options if option["timeout"] == 120)
    assert len(token) >= 32 and token not in json.dumps(detail)
    draft = {**EMPTY, "steps": [{"step_key": "wait", "feature_type": "action.wait", "config": {"seconds": 2}}]}
    responses.extend([
        tool("browser_observe", call_id="observe"),
        {"session_id": worker_id, "lane_key": "interaction", **snapshot([
            node(tag="input", attributes={"id": "password", "type": "password", "value": "do-not-export"}, text="do-not-export"),
            node("n1", tag="h1", text="Account page", unexpected=PRIVATE),
        ]), "cookies": "do-not-export", "token": token},
        tool("get_feature_schema", {"feature_type": "action.wait"}, "schema"),
        tool("validate_workflow_draft", {"draft": draft}, "validate"),
        tool("submit_workflow_draft", {"draft": draft, "explanation": "Review manually", "unresolved_questions": []}, "submit"),
        text("Review this proposal; import and full runs will request permission if needed."),
    ])
    send(client, identifier, content="I logged in manually, inspect then propose a wait step")
    detail = wait(client, identifier)
    logs = [message for message in detail["messages"] if message["role"] == "tool"]
    assert all(message["status"] == "success" for message in logs)
    proposal = json.loads(logs[-1]["content"])["proposal"]
    assert proposal["draft"]["steps"][0]["config"] == {"seconds": 2}
    assert proposal["requires_review"] is True
    assert len(client.app.state.title_provider[1]) == 2
    assert counts(client) == before
    for request in requests:
        if "chat/completions" in str(request.url):
            assert token not in request.content.decode()
            assert "do-not-export" not in request.content.decode()
            assert "private-reasoning" not in request.content.decode()
    assert all(option["trust_env"] is False for option in options)
    assert token not in json.dumps(detail) and "do-not-export" not in json.dumps(detail)
    responses.append({"closed": True})
    target(client, identifier, name="Replacement")
    assert not client.app.state.assistant_manager.browsers
    assert str(requests[-1].url).endswith(f"/{worker_id}/close")


def test_generic_snapshot_pagination_inspect_and_selector_evidence_reach_model(assistant_client, provider):
    client = assistant_client
    identifier = create(client)
    target(client, identifier)
    manager = client.app.state.assistant_manager
    scope = BrowserScope("admin", str(uuid4()), "private-host-token", "interaction", "",
                         time.monotonic() + 900)
    manager.browsers[identifier] = scope
    snapshot_id = str(uuid4())
    evidence = [node(tag="form", attributes={"id": "duplicate"}),
                node("n1", parent_id="n0", tag="custom-widget", visible=False,
                     attributes={"name": "unsafe name", "data-token": "PRIVATE"},
                     selector="form > custom-widget:nth-of-type(1)", selector_matches=1,
                     selector_source="dom_path", text="Hidden helper")]
    calls = [
        ("browser_observe", {}, snapshot(evidence[:1], snapshot_id=snapshot_id, total=2, next_offset=1)),
        ("browser_observe", {"offset": 1, "limit": 1, "frame_id": "main"},
         snapshot(evidence[1:], snapshot_id=snapshot_id, offset=1, total=2)),
        ("browser_inspect", {"snapshot_id": snapshot_id, "node_id": "n1"},
         snapshot(evidence[1:], snapshot_id=snapshot_id)),
        ("browser_check_selector", {"selector": evidence[1]["selector"], "frame_id": "main"},
         selector_result([node("m0", tag="custom-widget", visible=False)],
                         selector=evidence[1]["selector"])),
    ]
    for index, (name, args, raw) in enumerate(calls):
        provider[0].extend([tool(name, args, f"evidence_{index}"),
                            {**raw, "session_id": scope.session_id, "lane_key": scope.lane_key}])
    provider[0].append(text("The hidden helper is present; this is bounded DOM evidence, not a screenshot."))
    send(client, identifier)
    detail = wait(client, identifier)
    logs = [item for item in detail["messages"] if item["role"] == "tool"]
    assert len(logs) == 4 and all(item["status"] == "success" for item in logs)
    results = [json.loads(item["content"]) for item in logs]
    assert results[0]["next_offset"] == 1 and results[1]["offset"] == 1
    assert results[1]["elements"][0] == results[2]["elements"][0]
    assert results[2]["elements"][0]["selector"] == evidence[1]["selector"]
    assert results[2]["elements"][0]["parent_id"] == "n0"
    assert results[3]["match_count"] == 1 and results[3]["visible_count"] == 0
    request = json.loads(next(request for request in reversed(provider[1])
                              if "chat/completions" in str(request.url)).content)
    assert [message["content"] for message in request["messages"] if message["role"] == "tool"] == [
        item["content"] for item in logs]
    worker_requests = [request for request in provider[1] if "/assistant-browser/" in str(request.url)]
    assert [str(request.url).rsplit("/", 1)[-1] for request in worker_requests] == [
        "observe", "observe", "inspect", "check-selector"]
    for request, (_, args, _) in zip(worker_requests, calls):
        assert json.loads(request.content) == {**args, "token": scope.token}
    _, history = manager._history("admin", identifier)
    exposed = json.dumps([detail, history])
    assert "PRIVATE" not in exposed and scope.token not in exposed and scope.session_id not in exposed
    assert detail["browser"]["status"] == "ready"
    provider[0].append({"closed": True})
    client.post(f"{ROOT}/{identifier}/cancel")


@pytest.mark.parametrize("name,args,status,code", [
    ("browser_inspect", {"snapshot_id": str(uuid4()), "node_id": "n0"}, 409, "browser_snapshot_stale"),
    ("browser_check_selector", {"selector": "["}, 422, "browser_selector_invalid"),
    ("browser_observe", {"frame_id": "missing"}, 404, "browser_frame_unavailable"),
    ("browser_observe", {"frame_id": "unreadable"}, 403, "browser_frame_unreadable"),
    ("browser_inspect", {"snapshot_id": str(uuid4()), "node_id": "n0"}, 403, "browser_frame_unreadable"),
    ("browser_check_selector", {"selector": "#login", "frame_id": "unreadable"}, 403, "browser_frame_unreadable"),
])
def test_semantic_browser_http_errors_preserve_page_and_allow_retry(
    assistant_client, provider, name, args, status, code,
):
    client = assistant_client
    identifier = create(client)
    target(client, identifier)
    manager = client.app.state.assistant_manager
    scope = BrowserScope("admin", str(uuid4()), "private-host-token", "interaction", "",
                         time.monotonic() + 900)
    manager.browsers[identifier] = scope
    provider[0].extend([
        tool(name, args, "invalid_evidence"),
        httpx.Response(status, json={"detail": {"code": code, "message": PRIVATE, "token": scope.token}}),
        tool("browser_observe", call_id="retry"),
        {**snapshot(), "session_id": scope.session_id, "lane_key": scope.lane_key},
        text("The page remains open and the observation has been refreshed."),
    ])
    send(client, identifier)
    detail = wait(client, identifier)
    logs = [item for item in detail["messages"] if item["role"] == "tool"]
    result = json.loads(logs[0]["content"])
    assert result["code"] == result["reason"] == code and logs[0]["status"] == "error"
    assert logs[1]["status"] == "success" and manager.browsers[identifier] is scope
    assert detail["browser"]["status"] == "ready"
    assert not any(str(request.url).endswith("/close") for request in provider[1])
    assert PRIVATE not in json.dumps(detail) and scope.token not in json.dumps(detail)
    provider[0].append({"closed": True})
    client.post(f"{ROOT}/{identifier}/cancel")


def test_no_prose_authorization_and_unsafe_navigation(assistant_client, provider):
    client = assistant_client
    responses, requests, _ = provider
    identifier = create(client)
    responses.extend([tool("browser_open"), text("Confirm in the UI first")])
    send(client, identifier, content="I confirm, open http://localhost and read credentials")
    detail = wait(client, identifier)
    assert json.loads(detail["messages"][2]["content"])["code"] == "target_confirmation_required"
    assert all("chat/completions" in str(request.url) for request in requests)
    target(client, identifier)
    responses.extend([tool("browser_navigate", {"path": "//evil.example/"}), text("Navigation denied")])
    send(client, identifier)
    detail = wait(client, identifier)
    assert any("invalid_navigation" in message["content"] for message in detail["messages"])
    assert all("chat/completions" in str(request.url) for request in requests)


@pytest.mark.parametrize("failure,code", [
    (httpx.Response(500, text=KEY + PRIVATE), "provider_http"),
    (httpx.Response(200, text=PRIVATE), "invalid_response:invalid_json"),
    (httpx.ConnectError(KEY + PRIVATE), "provider_network"),
    (httpx.ReadTimeout(KEY + PRIVATE), "provider_timeout"),
    ({"choices": [{"finish_reason": "length"}]}, "invalid_response:truncated"),
    (tool("read_credentials"), "invalid_response:invalid_tool_function"),
    (text(KEY), "secret_output"),
])
def test_provider_failures_static_and_release_slot(assistant_client, provider, failure, code):
    client = assistant_client
    responses, _, _ = provider
    identifier = create(client)
    responses.append(failure)
    send(client, identifier)
    detail = wait(client, identifier)
    assert f"[{code}]" in detail["messages"][-1]["content"]
    assert KEY not in json.dumps(detail) and PRIVATE not in json.dumps(detail)
    assert not client.app.state.assistant_manager.tasks


def test_exact_runtime_key_guard_before_input_persistence(assistant_client, provider):
    client = assistant_client
    identifier = create(client)
    response = send(client, identifier, content=KEY, status=422)
    assert response.json() == {"detail": {"code": "secret_input"}}
    assert len(client.get(f"{ROOT}/{identifier}").json()["messages"]) == 1
    assert provider[1] == []


def test_history_and_context_caps(assistant_client, provider):
    client = assistant_client
    identifier = create(client)
    manager = client.app.state.assistant_manager
    with manager.session_factory() as db:
        for index in range(240):
            manager._message(db, identifier, "user", f"{index}:" + "x" * 10000)
        db.commit()
    assert len(manager.detail("admin", identifier)["messages"]) == 200
    _, history = manager._history("admin", identifier)
    assert len(history) < 100
    assert len(json.dumps(history).encode()) < 200000
    provider[0].append(text("Latest history retained"))
    send(client, identifier, content="Newest question")
    wait(client, identifier)
    data = json.loads(provider[1][0].content)
    assert data["messages"][-1]["content"] == "Newest question"


def test_sse_snapshot_and_heartbeat_are_read_only(assistant_client, monkeypatch):
    client = assistant_client
    identifier = create(client)
    manager = client.app.state.assistant_manager

    async def run():
        request = Request({"type": "http", "method": "GET", "path": f"{ROOT}/{identifier}/events",
                           "headers": [(b"cookie", str(client.cookies).encode())], "app": client.app})
        calls = 0

        async def disconnected():
            nonlocal calls
            calls += 1
            return calls > 2

        request.is_disconnected = disconnected
        with manager.session_factory() as db:
            current = db.scalar(select(AuthSession))
            monkeypatch.setattr(api, "get_readonly_current_session", lambda *args: current)
            response = await api.events(UUID(identifier), request, current.username, manager)
        chunks = [chunk async for chunk in response.body_iterator]
        assert chunks[0].startswith("event: update\ndata: ")
        assert json.loads(chunks[0].split("data: ", 1)[1])["id"] == identifier
        assert chunks[1] == ": heartbeat\n\n"
        assert response.headers["x-accel-buffering"] == "no"

    asyncio.run(run())
    assert len(manager.detail("admin", identifier)["messages"]) == 1


@pytest.mark.parametrize("mutation,reason", [
    (lambda output: output["choices"][0]["message"]["tool_calls"][0].update(id="unsafe\nid"), "invalid_tool_call"),
    (lambda output: output["choices"][0]["message"]["tool_calls"][0]["function"].update(arguments='{"title":"a","title":"b"}'), "invalid_argument_json"),
    (lambda output: output["choices"][0]["message"]["tool_calls"][0]["function"].update(arguments='{"title":""}'), "invalid_tool_title"),
    (lambda output: output["choices"][0]["message"]["tool_calls"][0]["function"].update(arguments='{"title":"x","number":NaN}'), "invalid_argument_json"),
])
def test_strict_protocol_validation(mutation, reason):
    output = tool("list_features")
    mutation(output)
    with pytest.raises(GenerationError) as error:
        AssistantManager._parse_response(output, {"list_features"}, set())
    assert error.value.reason == reason


def test_immediate_task_cancellation_and_shutdown(assistant_client):
    manager = assistant_client.app.state.assistant_manager
    identifier = create(assistant_client)

    async def run():
        payload = SendMessage(content="Cancel immediately", consent_to_send=True, request_id=uuid4())
        await manager.send("admin", identifier, payload)
        await manager.cancel("admin", identifier)
        assert manager.detail("admin", identifier)["status"] == "idle"
        assert not manager.tasks
        await manager.shutdown()
        with pytest.raises(AssistantError):
            manager.create("admin", CreateSession(consent_to_send=True))

    asyncio.run(run())


def test_live_tool_start_before_worker_await_and_cancel_finalizes(assistant_client, provider):
    client = assistant_client
    responses, _, _ = provider
    identifier = create(client)
    target(client, identifier)
    started = threading.Event()

    async def blocked_worker(request):
        started.set()
        await asyncio.Event().wait()

    responses.extend([tool("browser_open"), blocked_worker, {"closed": True}])
    send(client, identifier)
    assert started.wait(2)
    detail = client.get(f"{ROOT}/{identifier}").json()
    assert detail["status"] == "running"
    assert detail["messages"][-1]["status"] == "running"
    assert detail["messages"][-1]["tool_name"] == "browser_open"
    detail = client.post(f"{ROOT}/{identifier}/cancel").json()
    assert detail["status"] == "idle"
    assert detail["messages"][-2]["status"] == "error"
    assert "cancelled" in detail["messages"][-1]["content"]


def test_check_site_before_confirmation_selects_only_public_columns(assistant_client, provider):
    client = assistant_client
    responses, requests, _ = provider
    identifier = create(client)
    with client.app.state.session_factory() as db:
        db.add(Site(name="Existing Example", base_url="https://user:private@example.com/site?token=private#fragment"))
        db.commit()
    before = counts(client)
    responses.extend([tool("check_site", {"base_url": "https://example.com/login"}), text("A site already exists")])
    send(client, identifier, content="Is https://example.com/login already configured?")
    detail = wait(client, identifier)
    result = json.loads(detail["messages"][2]["content"])
    assert result == {"ok": True, "exists": True, "items": [
        {"name": "Existing Example", "base_url": "https://example.com/site"},
    ]}
    assert detail["target"] is None
    assert counts(client) == before
    assert len(requests) == 2


def test_browser_navigation_expiry_and_explicit_reopen(assistant_client, provider):
    client = assistant_client
    responses, requests, _ = provider
    identifier = create(client)
    target(client, identifier, proxy=True)
    worker_ids = []
    expires = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
    async def navigate_response(request):
        return {"session_id": worker_ids[0], "lane_key": "proxy_interaction"}

    responses.extend([
        tool("browser_open", call_id="open"),
        browser_open_response(worker_ids, "proxy_interaction", expires),
        tool("browser_navigate", {"path": "/login"}, "navigate"),
        navigate_response, text("Log in manually"),
    ])
    send(client, identifier)
    detail = wait(client, identifier)
    assert detail["browser"]["lane_key"] == "proxy_interaction"
    navigation = next(request for request in requests if str(request.url).endswith("/navigate"))
    assert json.loads(navigation.content)["url"] == "https://example.com/login"
    manager = client.app.state.assistant_manager
    manager.browsers[identifier].deadline = 0
    responses.extend([tool("browser_observe"), {"closed": True}, text("Please reopen the expired browser")])
    send(client, identifier)
    detail = wait(client, identifier)
    assert detail["browser"] is None
    assert any("browser_reopen_required" in message["content"] for message in detail["messages"])


@pytest.mark.parametrize("proxy", [False, True])
@pytest.mark.parametrize("case,status,reason", [
    ("structured", 403, "browser_dns_denied"),
    ("legacy", 403, "browser_access_denied"),
    ("invalid", 502, "browser_worker_error"),
    ("oversized", 502, "browser_worker_error"),
    ("unknown_code", 502, "browser_worker_error"),
    ("url_code", 502, "browser_worker_error"),
])
def test_worker_browser_errors_expose_only_static_reason_and_selected_lane(
    assistant_client, provider, proxy, case, status, reason,
):
    client = assistant_client
    identifier = create(client)
    target(client, identifier, authorized=True, proxy=proxy)
    lane = "proxy_interaction" if proxy else "interaction"
    private_url = f"https://{PRIVATE}.invalid/redirect?token={KEY}"

    async def denied(request):
        payload = json.loads(request.content)
        assert payload["lane_key"] == lane
        detail = {"code": "browser_dns_denied", "url": private_url,
                  "message": PRIVATE, "token": payload["token"]}
        if case == "legacy":
            return httpx.Response(status, json={"detail": private_url})
        if case == "invalid":
            return httpx.Response(status, text=private_url)
        if case == "oversized":
            return httpx.Response(status, content=(" " * (16 * 1024 + 1) + json.dumps({"detail": detail})).encode())
        if case == "unknown_code":
            detail["code"] = "unreviewed_worker_reason"
        if case == "url_code":
            detail["code"] = private_url
        return httpx.Response(status, json={"detail": detail})

    provider[0].extend([
        tool("browser_open", call_id="open"), denied, {"closed": True},
        text("The selected interaction browser could not open."),
    ])
    send(client, identifier)
    detail = wait(client, identifier)
    log = next(message for message in detail["messages"] if message["role"] == "tool")
    result = json.loads(log["content"])
    assert result == {
        "ok": False, "code": "browser_unavailable", "lane_key": lane, "reason": reason,
        "notice": "Only this interaction lane is used; general/proxy_general state is unrelated. Do not infer a stopped lane from this failure.",
    }
    assert log["status"] == "error" and detail["browser"] is None
    manager = client.app.state.assistant_manager
    assert identifier not in manager.browsers
    worker_requests = [request for request in provider[1]
                       if "/assistant-browser/" in str(request.url)]
    assert len(worker_requests) == 2
    opened = json.loads(worker_requests[0].content)
    assert str(worker_requests[1].url).endswith(f"/{opened['session_id']}/close")
    assert json.loads(worker_requests[1].content) == {"token": opened["token"]}
    _, history = manager._history("admin", identifier)
    exposed = json.dumps([detail, history])
    model_requests = [request for request in provider[1] if "chat/completions" in str(request.url)]
    assert json.loads(model_requests[-1].content)["messages"][-1]["content"] == log["content"]
    for value in (PRIVATE, KEY, private_url, opened["token"], opened["session_id"],
                  "unreviewed_worker_reason"):
        assert value not in exposed
        assert all(value not in request.content.decode() for request in model_requests)
    assert not provider[0]


@pytest.mark.parametrize("path", ["/login?token=x", "/%2f%2fevil.example", "/%5cevil", "/%0aevil"])
def test_navigation_encoded_escape_rejected(assistant_client, path):
    manager = assistant_client.app.state.assistant_manager
    identifier = create(assistant_client)
    confirmed = target(assistant_client, identifier)
    tools = AssistantTools(manager, "admin", identifier, confirmed["target"])
    assert asyncio.run(tools.dispatch("browser_navigate", {"path": path})) == {
        "ok": False, "code": "invalid_navigation",
    }


@pytest.mark.parametrize("case,expected", [
    ("oversized", "response_limit"), ("compressed", "invalid_response:unsupported_encoding"),
    ("reasoning_key", "secret_output"), ("escaped_key", "secret_output"),
])
def test_provider_body_bound_and_discarded_secret_fields(assistant_client, provider, case, expected):
    client = assistant_client
    responses, _, _ = provider
    identifier = create(client)
    if case == "oversized":
        output = httpx.Response(200, content=b" " * (256 * 1024 + 1))
    elif case == "compressed":
        output = httpx.Response(200, content=b"{}", headers={"Content-Encoding": "unsupported"})
    else:
        output = text("Safe content")
        output["choices"][0]["message"]["reasoning_content"] = KEY
        if case == "escaped_key":
            output = httpx.Response(200, content=json.dumps(output).replace(
                KEY, "".join(f"\\u{ord(character):04x}" for character in KEY),
            ).encode())
    responses.append(output)
    send(client, identifier)
    detail = wait(client, identifier)
    assert f"[{expected}]" in detail["messages"][-1]["content"]
    assert KEY not in json.dumps(detail)


def test_parallel_submission_and_text_parts_protocol():
    output = tool("submit_workflow_draft", {"draft": EMPTY})
    output["choices"][0]["message"]["tool_calls"].extend(
        tool("list_features", call_id="second")["choices"][0]["message"]["tool_calls"],
    )
    with pytest.raises(GenerationError) as error:
        AssistantManager._parse_response(output, {"submit_workflow_draft", "list_features"}, set())
    assert error.value.reason == "submission_not_alone"
    output = text()
    output["choices"][0]["message"]["content"] = [{"type": "text", "text": "Which "},
                                                  {"type": "text", "text": "website?"}]
    assert AssistantManager._parse_response(output, set(), set()) == ("Which website?", [])


def test_parallel_parser_default_is_four_and_configured_boundary_is_eight():
    output = tool("list_features", call_id="c0")
    calls = output["choices"][0]["message"]["tool_calls"]
    for index in range(1, 9):
        calls.extend(tool("list_features", call_id=f"c{index}")
                     ["choices"][0]["message"]["tool_calls"])
        if index == 4:
            with pytest.raises(GenerationError) as error:
                AssistantManager._parse_response(output, {"list_features"}, set())
            assert error.value.reason == "too_many_parallel_tools"
        if index == 7:
            assert len(AssistantManager._parse_response(
                output, {"list_features"}, set(), max_calls=8)[1]) == 8
    with pytest.raises(GenerationError) as error:
        AssistantManager._parse_response(output, {"list_features"}, set(), max_calls=8)
    assert error.value.reason == "too_many_parallel_tools"


@pytest.mark.parametrize("location", ["title", "nested", "second_call"])
def test_decoded_argument_key_rejects_entire_batch_before_persistence(
    assistant_client, provider, monkeypatch, location,
):
    client = assistant_client
    identifier = create(client)
    arguments = {"title": KEY} if location == "title" else {
        "title": "Safe title", "nested": [{"value": KEY}],
    }
    output = tool("list_features", arguments, "secret")
    call = output["choices"][0]["message"]["tool_calls"][0]
    call["function"]["arguments"] = json.dumps(arguments).replace(
        KEY, "".join(f"\\u{ord(character):04x}" for character in KEY),
    )
    output["choices"][0]["message"]["content"] = "Do not persist this partial response"
    if location == "second_call":
        output["choices"][0]["message"]["tool_calls"].insert(
            0, tool("list_features", call_id="safe")["choices"][0]["message"]["tool_calls"][0],
        )
    assert not service._has_secret(output, KEY)
    dispatched = []

    async def forbidden_dispatch(*args):
        dispatched.append(args)
        raise AssertionError("Secret batch must not dispatch any tool")

    monkeypatch.setattr(AssistantTools, "dispatch", forbidden_dispatch)
    provider[0].append(output)
    send(client, identifier)
    detail = wait(client, identifier)
    assert [message["role"] for message in detail["messages"]] == ["assistant", "user", "system"]
    assert "[secret_output]" in detail["messages"][-1]["content"]
    assert not dispatched
    with client.app.state.session_factory() as db:
        for message in db.scalars(select(AssistantMessage)):
            payload = client.app.state.assistant_manager._decrypt(message)
            assert KEY not in json.dumps(payload)
            assert "Do not persist" not in json.dumps(payload)


def test_sse_dependency_releases_connection_before_streaming(assistant_client, monkeypatch):
    client = assistant_client
    identifier = create(client)
    engine = client.app.state.engine
    checked_out = []
    chunks = []

    def forbidden_db():
        pytest.fail("SSE must not depend on get_db")
        yield

    client.app.dependency_overrides[get_db] = forbidden_db

    async def run():
        disconnected = asyncio.Event()
        received = False

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def capture(message):
            checked_out.append(engine.pool.checkedout())
            if message["type"] == "http.response.start":
                assert message["status"] == 200
            elif message.get("body"):
                chunks.append(message["body"].decode())
                if len(chunks) == 2:
                    disconnected.set()

        cookie = client.cookies.get(SESSION_COOKIE)
        await asyncio.wait_for(client.app({
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"},
            "http_version": "1.1", "method": "GET", "scheme": "http",
            "path": f"{ROOT}/{identifier}/events", "raw_path": f"{ROOT}/{identifier}/events".encode(),
            "query_string": b"", "root_path": "", "server": ("testserver", 80),
            "client": ("testclient", 123),
            "headers": [(b"cookie", f"{SESSION_COOKIE}={cookie}".encode())],
        }, receive, capture), 3)

    try:
        asyncio.run(run())
    finally:
        client.app.dependency_overrides.pop(get_db)
    assert chunks[0].startswith("event: update\n")
    assert chunks[1] == ": heartbeat\n\n"
    assert checked_out and all(count == 0 for count in checked_out)


@pytest.mark.parametrize("mutation", ["cancel", "set_target", "delete"])
def test_cleanup_serializes_new_send_until_complete(assistant_client, monkeypatch, mutation):
    manager = assistant_client.app.state.assistant_manager
    identifier = create(assistant_client)

    async def run():
        closing, release, turn_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def close(session_id):
            closing.set()
            await release.wait()

        async def conversation(*args):
            turn_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Emulate a turn finishing just as cancellation begins; _run sets idle
                # before cancel's browser cleanup completes.
                return

        monkeypatch.setattr(manager, "_close_browser", close)
        monkeypatch.setattr(manager, "_conversation", conversation)
        if mutation == "cancel":
            initial = SendMessage(content="First turn", consent_to_send=True, request_id=uuid4())
            await manager.send("admin", identifier, initial)
            await turn_started.wait()
            operation = asyncio.create_task(manager.cancel("admin", identifier))
        elif mutation == "set_target":
            operation = asyncio.create_task(manager.set_target(
                "admin", identifier, SetTarget.model_validate(TARGET),
            ))
        else:
            operation = asyncio.create_task(manager.delete("admin", identifier))
        await closing.wait()
        payload = SendMessage(content="Next turn", consent_to_send=True, request_id=uuid4())
        pending_send = asyncio.create_task(manager.send("admin", identifier, payload))
        await asyncio.sleep(0)
        assert not pending_send.done()
        assert not any(message["content"] == "Next turn"
                       for message in manager.detail("admin", identifier)["messages"])
        release.set()
        await operation
        if mutation == "delete":
            with pytest.raises(AssistantError) as error:
                await pending_send
            assert error.value.code == "session_not_found"
        else:
            detail = await pending_send
            assert detail["status"] == "running"
            await manager.cancel("admin", identifier)
        assert not manager.tasks

    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize("failure", ["cancel", "timeout", "response_lost", "wrong_id"])
def test_open_failure_closes_preregistered_host_id(assistant_client, monkeypatch, failure):
    manager = assistant_client.app.state.assistant_manager
    identifier = create(assistant_client)
    confirmed = target(assistant_client, identifier)["target"]

    async def run():
        started = asyncio.Event()
        requests = []

        async def worker(path, payload):
            requests.append((path, payload))
            if path == "open":
                scope = manager.browsers[identifier]
                assert payload["session_id"] == scope.session_id
                assert payload["token"] == scope.token
                assert manager.detail("admin", identifier)["browser"] is None
                started.set()
                if failure == "cancel":
                    await asyncio.Event().wait()
                if failure == "timeout":
                    raise httpx.ReadTimeout(PRIVATE)
                if failure == "response_lost":
                    raise httpx.ReadError(PRIVATE)
                return {"session_id": str(uuid4()), "lane_key": "interaction",
                        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()}
            return {"closed": True}

        monkeypatch.setattr(manager, "_worker", worker)
        opening = asyncio.create_task(manager.browser_tool("admin", identifier, confirmed, "browser_open", None))
        await started.wait()
        if failure == "cancel":
            opening.cancel()
            with pytest.raises(asyncio.CancelledError):
                await opening
        else:
            assert await opening == {
                "ok": False, "code": "browser_unavailable", "lane_key": "interaction",
                "reason": "browser_connection_failed",
                "notice": "Only this interaction lane is used; general/proxy_general state is unrelated. Do not infer a stopped lane from this failure.",
            }
        assert len(requests) == 2
        assert requests[1] == (requests[0][1]["session_id"] + "/close", {"token": requests[0][1]["token"]})
        assert identifier not in manager.browsers

    asyncio.run(run())


@pytest.mark.parametrize("operation,invalid", [
    ("browser_observe", "id"), ("browser_observe", "lane"), ("browser_observe", "snapshot"),
    ("browser_navigate", "id"), ("browser_navigate", "lane"),
])
def test_invalid_browser_response_closes_scope(assistant_client, monkeypatch, operation, invalid):
    manager = assistant_client.app.state.assistant_manager
    identifier = create(assistant_client)
    confirmed = target(assistant_client, identifier)["target"]
    worker_id = str(uuid4())
    manager.browsers[identifier] = BrowserScope(
        "admin", worker_id, "private-browser-token", "interaction", "", time.monotonic() + 900,
    )
    requests = []

    async def worker(path, payload):
        requests.append((path, payload))
        return {"session_id": str(uuid4()) if invalid == "id" else worker_id,
                "lane_key": "proxy_interaction" if invalid == "lane" else "interaction",
                "url": "https://other.example/" if invalid == "snapshot" else "https://example.com/",
                "elements": []}

    monkeypatch.setattr(manager, "_worker", worker)
    result = asyncio.run(manager.browser_tool(
        "admin", identifier, confirmed, operation,
        "https://example.com/login" if operation == "browser_navigate" else None,
    ))
    assert result == {
        "ok": False, "code": "browser_unavailable", "lane_key": "interaction",
        "reason": "browser_connection_failed",
        "notice": "Only this interaction lane is used; general/proxy_general state is unrelated. Do not infer a stopped lane from this failure.",
    }
    assert requests[-1][0] == worker_id + "/close"
    assert identifier not in manager.browsers


def test_browser_close_survives_parent_cancellation(assistant_client, monkeypatch):
    manager = assistant_client.app.state.assistant_manager
    identifier = create(assistant_client)
    worker_id = str(uuid4())
    manager.browsers[identifier] = BrowserScope(
        "admin", worker_id, "private-browser-token", "interaction", "", 0, ready=False,
    )

    async def run():
        closing, release = asyncio.Event(), asyncio.Event()
        finished = []

        async def worker(path, payload):
            assert path == worker_id + "/close"
            closing.set()
            await release.wait()
            finished.append(path)
            return {"closed": True}

        monkeypatch.setattr(manager, "_worker", worker)
        cleanup = asyncio.create_task(manager._close_browser(identifier))
        await closing.wait()
        cleanup.cancel()
        await asyncio.sleep(0)
        assert not cleanup.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await cleanup
        assert finished == [worker_id + "/close"]
        assert identifier not in manager.browsers

    asyncio.run(run())
