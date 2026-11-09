from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
from io import BytesIO
import json
import threading
import time
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
from PIL import Image
import pytest
from sqlalchemy import select

from app.models.assistant import AssistantMessage, AssistantSession
from app.services.assistant import AssistantError, AssistantManager, BrowserScope
from app.services.assistant_images import image_message, omit_images, validate_image
from app.services.assistant_tokens import estimate_context_tokens
from app.services.assistant_tools import AssistantTools
from app.services.config_ai_generation import _json
from app.services.integrations import IntegrationService
from tests.test_assistant_api import (  # noqa: F401
    CONFIG, ROOT, assistant_client, create, provider, send, target, text, tool, wait,
)
from tests.test_assistant_cache_history import assert_complete
from tests.test_assistant_execution import response as execution_response
from tests.test_assistant_runtime_settings import save


def jpeg(width=80, height=60, **options):
    output = BytesIO()
    Image.new("RGB", (width, height), "white").save(output, "JPEG", **options)
    return {"mime_type": "image/jpeg", "image_base64": base64.b64encode(output.getvalue()).decode(),
            "width": width, "height": height, "masked": True}


def setup_browser(client, *, consent=True):
    manager = client.app.state.assistant_manager
    sid = create(client)
    target(client, sid, authorized=True, screenshot_consent=consent)
    scope = BrowserScope("admin", str(uuid4()), "private-scope-token", "interaction", "",
                         time.monotonic() + 900)
    manager.browsers[sid] = scope
    return manager, sid, scope


def raw_image(scope, image=None):
    return {"session_id": scope.session_id, "lane_key": scope.lane_key,
            "url": "https://example.com/account?token=private-url-value",
            **(image or jpeg()), "private_worker_metadata": "never-export"}


def vision(monkeypatch):
    monkeypatch.setattr(IntegrationService, "vision_runtime", lambda self: {**CONFIG, "supports_vision": True})


@pytest.mark.parametrize("auto_compact", [False, True])
def test_model_pixels_follow_complete_batch_and_encrypted_canonical_prefix(
    assistant_client, provider, monkeypatch, auto_compact,
):
    client = assistant_client
    vision(monkeypatch)
    save(client, auto_title=False, auto_compact=auto_compact)
    manager, sid, scope = setup_browser(client)
    original = raw_image(scope, jpeg(comment=b"private-jpeg-comment"))
    batch = tool("browser_screenshot", call_id="capture")
    batch["choices"][0]["message"]["tool_calls"].extend(
        tool("check_site", {"base_url": "https://example.com/"}, "read")["choices"][0]["message"]["tool_calls"])
    first_reply, followup_reply = text("Screenshot reviewed"), text("Follow-up reviewed")
    for output in (batch, first_reply, followup_reply):
        output["usage"] = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
    provider[0].extend([batch, original, first_reply])
    send(client, sid, content="Inspect the screenshot")
    detail = wait(client, sid)
    assert detail["messages"][-1]["content"] == "Screenshot reviewed"
    assert len(provider[1]) == 3  # two model calls and exactly one capture
    assert detail["usage"]["totals"]["request_count"] == 2
    assert detail["usage"]["totals"]["total_tokens"] == 24
    outbound = json.loads(provider[1][-1].content)["messages"]
    assert_complete(outbound)
    assert [item["role"] for item in outbound[-4:]] == ["assistant", "tool", "tool", "user"]
    multimodal = outbound[-1]
    assert multimodal["content"][1]["type"] == "image_url"
    part = multimodal["content"][1]["image_url"]
    assert part["url"].startswith("data:image/jpeg;base64,") and part["detail"] == "low"
    pixels = base64.b64decode(part["url"].split(",", 1)[1], validate=True)
    assert b"private-jpeg-comment" not in pixels
    for item in outbound:
        if item["role"] == "tool":
            assert "image_base64" not in item["content"] and "data:image" not in item["content"]
    public = _json(detail)
    for private in ("image_base64", "data:image", "private-url-value", "private_worker_metadata", scope.token):
        assert private not in public
    capture = next(item for item in detail["messages"] if item.get("tool_name") == "browser_screenshot")
    metadata = json.loads(capture["content"])
    assert metadata["url"] == "https://example.com/account?token=[REDACTED]"
    response = client.get(metadata["image_url"])
    assert response.status_code == 200 and response.content == pixels
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "no-store"
    with manager.session_factory() as db:
        rows = list(db.scalars(select(AssistantMessage).where(AssistantMessage.session_id == sid)))
        images = [row for row in rows if manager._decrypt(row).get("context_image")]
        assert len(images) == 1 and images[0].role == "system"
        assert images[0].uuid == metadata["image_id"]
        assert part["url"].encode() not in images[0].ciphertext
        assert manager._decrypt(images[0])["_model_message"] == multimodal
        assert manager._decrypt(images[0])["_target_revision"] == 1
    fresh = AssistantManager(manager.session_factory, manager.settings, manager.crypto)
    assert fresh.get_image("admin", sid, metadata["image_id"]) == pixels
    with pytest.raises(AssistantError):
        fresh.get_image("other", sid, metadata["image_id"])
    provider[0].append(followup_reply)
    send(client, sid, content="Continue with the same evidence")
    detail = wait(client, sid)
    assert detail["usage"]["totals"]["request_count"] == 3
    assert detail["usage"]["totals"]["total_tokens"] == 36
    following = json.loads(provider[1][-1].content)["messages"]
    assert following[:len(outbound)] == outbound
    assert len([item for item in following if isinstance(item.get("content"), list)]) == 1
    assert not client.app.state.summary_provider[1]


@pytest.mark.parametrize("consent,support,code", [
    (False, True, "interaction_denied"), (True, False, "model_vision_required"),
])
def test_model_screenshot_denied_before_capture(assistant_client, provider, monkeypatch, consent, support, code):
    client = assistant_client
    if support:
        vision(monkeypatch)
    save(client, auto_title=False)
    manager, sid, scope = setup_browser(client, consent=consent)
    worker = AsyncMock()
    monkeypatch.setattr(manager, "_worker", worker)
    provider[0].extend([tool("browser_screenshot"), text("Permission or vision required")])
    send(client, sid)
    if not consent:
        from tests.test_assistant_interactions import pending, url

        review = pending(client, sid)
        assert review["kind"] == "authorization" and review["purpose"] == "authorization"
        assert review["review"]["capability"] == "screenshot"
        worker.assert_not_awaited()
        response = client.post(url(sid, review["id"]), json={"decision": "deny"})
        assert response.status_code == 200
    detail = wait(client, sid)
    log = next(item for item in detail["messages"] if item["role"] == "tool")
    result = json.loads(log["content"])
    if not consent:
        assert result["ok"] is False and result["code"] == code and result["terminal"] is True
        assert detail["target"]["screenshot_consent"] is False
    else:
        assert result == {"ok": False, "code": code}
    assert log["status"] == "error"
    assert detail["pending_interaction"] is None
    assert detail["messages"][-1]["content"] == "Permission or vision required"
    worker.assert_not_awaited()
    with manager.session_factory() as db:
        assert not any(manager._decrypt(row).get("context_image") for row in db.scalars(
            select(AssistantMessage).where(AssistantMessage.session_id == sid)))


@pytest.mark.parametrize("field,value", [
    ("image_base64", "not base64!"), ("image_base64", base64.b64encode(b"not a JPEG").decode()),
    ("width", 81), ("width", 1601), ("height", 1201), ("width", True),
    ("masked", False), ("mime_type", "image/png"),
])
def test_screenshot_validation_is_independent(field, value):
    with pytest.raises(ValueError, match="invalid screenshot"):
        validate_image({**jpeg(), field: value})


def test_screenshot_rejects_truncated_oversized_and_noncanonical_base64():
    image = jpeg()
    binary = base64.b64decode(image["image_base64"])
    for value in (binary[:-2], binary + b"trailing", binary + b"x" * (384 * 1024)):
        with pytest.raises(ValueError):
            validate_image({**image, "image_base64": base64.b64encode(value).decode()})
    with pytest.raises(ValueError):
        validate_image({**image, "image_base64": image["image_base64"] + "\n"})


def test_ui_screenshot_is_ephemeral_and_refresh_returns_detail(assistant_client, provider):
    client = assistant_client
    manager, sid, scope = setup_browser(client)
    before = client.get(f"{ROOT}/{sid}").json()
    provider[0].extend([raw_image(scope), {"session_id": scope.session_id, "lane_key": scope.lane_key,
                                            "refreshed": True}])
    result = client.post(f"{ROOT}/{sid}/browser/screenshot", json={})
    assert result.status_code == 200 and result.json()["masked"] is True
    assert result.json()["mime_type"] == "image/jpeg"
    assert "_image" not in result.json()
    assert client.get(f"{ROOT}/{sid}").json() == before
    with manager.session_factory() as db:
        assert not any(manager._decrypt(row).get("context_image") for row in db.scalars(select(AssistantMessage)))
    result = client.post(f"{ROOT}/{sid}/browser/refresh", json={})
    assert result.status_code == 200 and result.json()["id"] == sid and result.json()["status"] == "idle"
    assert [str(request.url).rsplit("/", 1)[-1] for request in provider[1]] == ["screenshot", "refresh"]
    assert all(set(json.loads(request.content)) == {"token"} for request in provider[1])


@pytest.mark.parametrize("operation", ["screenshot", "refresh"])
def test_ui_recovery_is_idle_owner_csrf_and_strict(assistant_client, provider, operation):
    client = assistant_client
    manager, sid, scope = setup_browser(client)
    path = f"{ROOT}/{sid}/browser/{operation}"
    token = client.headers.pop("X-CSRF-Token")
    assert client.post(path, json={}).status_code == 403
    client.headers["X-CSRF-Token"] = token
    assert client.post(path, json={"url": "private-value"}).status_code == 422
    with manager.session_factory() as db:
        db.get(AssistantSession, sid).status = "running"
        db.commit()
    assert client.post(path, json={}).json() == {"detail": {"code": "session_running"}}
    with manager.session_factory() as db:
        row = db.get(AssistantSession, sid)
        row.status, row.owner = "idle", "other"
        db.commit()
    assert client.post(path, json={}).status_code == 404
    assert not provider[1]


@pytest.mark.parametrize("name", ["browser_observe", "browser_inspect", "browser_check_selector", "browser_screenshot"])
@pytest.mark.parametrize("reason", ["browser_dns_denied", "browser_redirect_denied"])
def test_read_policy_denial_retains_page_without_evidence(assistant_client, monkeypatch, name, reason):
    manager, sid, scope = setup_browser(assistant_client)
    worker = AsyncMock(side_effect=AssistantError("browser_unavailable", reason=reason))
    monkeypatch.setattr(manager, "_worker", worker)
    result = asyncio.run(manager.browser_tool("admin", sid, manager.detail("admin", sid)["target"], name, None))
    assert result["ok"] is False and result["code"] == reason
    assert result["page_retained"] is True and "evidence" not in result and "_image" not in result
    assert manager.browsers[sid] is scope and worker.await_count == 1
    assert "not proof a proxy is needed" in result["notice"]


def test_secret_url_denial_is_still_fatal(assistant_client, monkeypatch):
    manager, sid, scope = setup_browser(assistant_client)
    worker = AsyncMock(side_effect=[AssistantError("browser_unavailable", reason="browser_url_denied"), {"closed": True}])
    monkeypatch.setattr(manager, "_worker", worker)
    result = asyncio.run(manager.browser_tool("admin", sid, manager.detail("admin", sid)["target"], "browser_screenshot", None))
    assert result["ok"] is False and not result.get("page_retained")
    assert sid not in manager.browsers and worker.await_count == 2


@pytest.mark.parametrize("name,args", [
    ("browser_observe", {}), ("browser_inspect", {"snapshot_id": str(uuid4()), "node_id": "n1"}),
    ("browser_check_selector", {"selector": "#approved"}), ("browser_screenshot", {}),
])
def test_dns_denial_reaches_model_without_refresh_or_evidence(
    assistant_client, provider, monkeypatch, name, args,
):
    client = assistant_client
    vision(monkeypatch)
    save(client, auto_title=False)
    manager, sid, scope = setup_browser(client)
    provider[0].extend([tool(name, args),
                        httpx.Response(403, json={"detail": {"code": "browser_dns_denied"}}),
                        text("DNS policy denied this read; outcome remains unknown")])
    send(client, sid)
    detail = wait(client, sid)
    log = next(item for item in detail["messages"] if item["role"] == "tool")
    result = json.loads(log["content"])
    assert result["code"] == result["reason"] == "browser_dns_denied"
    assert result["page_retained"] is True and result["ok"] is False
    assert "_image" not in result and "elements" not in result
    assert manager.browsers[sid] is scope
    worker_requests = [request for request in provider[1] if "/assistant-browser/" in str(request.url)]
    assert len(worker_requests) == 1
    assert not str(worker_requests[0].url).endswith("/refresh")
    assert "data:image" not in provider[1][-1].content.decode()


def test_worker_read_cap_is_operation_specific(assistant_client, provider):
    manager = assistant_client.app.state.assistant_manager
    oversized = {"padding": "x" * (65 * 1024)}
    provider[0].extend([oversized, oversized, {"padding": "x" * (544 * 1024)}])
    assert asyncio.run(manager._worker("id/screenshot", {})) == oversized
    for path in ("id/observe", "id/screenshot"):
        with pytest.raises(AssistantError):
            asyncio.run(manager._worker(path, {}))


def test_full_size_screenshot_passes_worker_cap_without_binary_tool_text(assistant_client, provider, monkeypatch):
    import random

    vision(monkeypatch)
    save(assistant_client, auto_title=False)
    manager, sid, scope = setup_browser(assistant_client)
    output = BytesIO()
    Image.frombytes("RGB", (640, 480), random.Random(0).randbytes(640 * 480 * 3)).save(
        output, "JPEG", quality=60)
    image = {"mime_type": "image/jpeg", "image_base64": base64.b64encode(output.getvalue()).decode(),
             "width": 640, "height": 480, "masked": True}
    assert 64 * 1024 < len(output.getvalue()) < 384 * 1024
    provider[0].extend([tool("browser_screenshot"), raw_image(scope, image), text("Actual pixels received")])
    send(assistant_client, sid)
    detail = wait(assistant_client, sid)
    assert detail["messages"][-1]["content"] == "Actual pixels received"
    outbound = json.loads(provider[1][-1].content)["messages"]
    assert isinstance(outbound[-1]["content"], list)
    assert "image_base64" not in _json(detail)


def test_model_refresh_uses_owned_worker_get_operation(assistant_client, provider):
    save(assistant_client, auto_title=False)
    manager, sid, scope = setup_browser(assistant_client)
    provider[0].extend([tool("browser_refresh"),
                        {"session_id": scope.session_id, "lane_key": scope.lane_key, "refreshed": True},
                        text("Refreshed without retrying")])
    send(assistant_client, sid)
    detail = wait(assistant_client, sid)
    assert detail["messages"][-1]["content"] == "Refreshed without retrying"
    assert str(provider[1][1].url).endswith(f"/{scope.session_id}/refresh")
    assert json.loads(provider[1][1].content) == {"token": scope.token}


def test_ui_approval_then_normal_message_retries_exact_stored_step(assistant_client, provider):
    client = assistant_client
    save(client, auto_title=False)
    manager, sid, scope = setup_browser(client)
    operation = str(uuid4())
    args = {"operation_id": operation, "step": {"step_key": "click", "feature_type": "action.click",
                                               "config": {"selector": ["#approved"]}}}
    provider[0].extend([tool("execute_step", args, "original"),
                        execution_response(scope, operation, status="unknown", code="outcome_uncertain"),
                        text("Review the uncertain operation before retrying")])
    send(client, sid)
    detail = wait(client, sid)
    assert detail["unresolved_operations"][0]["operation_id"] == operation
    assert detail["unresolved_operations"][0]["can_authorize"] is True
    with manager.session_factory() as db:
        data = manager._decrypt(db.get(AssistantSession, sid))
        stored = deepcopy(data["_execution_records"][operation]["step"])
    before = len(provider[1])
    body = {"operation_id": operation, "confirmed": True, "request_id": str(uuid4())}
    approval = client.post(f"{ROOT}/{sid}/retry-authorizations", json=body)
    assert approval.status_code == 200 and len(provider[1]) == before
    assert approval.json()["retry_authorizations"][0]["status"] == "approved"
    assert client.post(f"{ROOT}/{sid}/retry-authorizations", json=body).json() == approval.json()
    assert client.get(f"{ROOT}/{sid}").json()["retry_authorizations"] == approval.json()["retry_authorizations"]
    captured = []

    async def executed(request):
        payload = json.loads(request.content)
        captured.append(payload)
        return execution_response(scope, payload["operation_id"])

    provider[0].extend([tool("retry_step", {"operation_id": operation}, "retry"), executed,
                        text("Explicitly approved retry completed")])
    send(client, sid, content="Perform my approved retry")
    detail = wait(client, sid)
    assert detail["messages"][-1]["content"] == "Explicitly approved retry completed"
    assert captured[0]["step"] == stored and captured[0]["retry_of"] == operation
    assert detail["retry_authorizations"][0]["status"] == "used"
    assert detail["unresolved_operations"] == []
    assert captured[0]["retry_authorization"] not in _json(detail)
    retry_log = next(item for item in detail["messages"] if item.get("tool_name") == "retry_step")
    assert json.loads(retry_log["content"])["retry_of"] == operation
    provider[0].append(text("Host resolution acknowledged"))
    send(client, sid, content="Continue")
    wait(client, sid)
    head = json.loads(provider[1][-1].content)["messages"][1]
    assert '"resolution"' in head["content"] and '"status":"success"' in head["content"]


def test_cancel_screenshot_closes_once_and_does_not_resubmit(assistant_client, provider, monkeypatch):
    client = assistant_client
    vision(monkeypatch)
    save(client, auto_title=False)
    manager, sid, scope = setup_browser(client)
    entered = threading.Event()
    paths = []

    async def worker(path, payload):
        paths.append(path)
        if path.endswith("/screenshot"):
            entered.set()
            await asyncio.Event().wait()
        return {"closed": True}

    monkeypatch.setattr(manager, "_worker", worker)
    provider[0].append(tool("browser_screenshot", call_id="capture"))
    send(client, sid)
    assert entered.wait(2)
    result = client.post(f"{ROOT}/{sid}/cancel")
    assert result.status_code == 200 and result.json()["status"] == "idle"
    assert paths == [f"{scope.session_id}/screenshot", f"{scope.session_id}/close"]
    assert sid not in manager.browsers
    with manager.session_factory() as db:
        assert not any(manager._decrypt(row).get("context_image") for row in db.scalars(select(AssistantMessage)))
    provider[0].append(text("Cancelled capture was not replayed"))
    send(client, sid)
    wait(client, sid)
    assert len(paths) == 2


def test_summary_and_extract_never_receive_binary(assistant_client, provider, monkeypatch):
    client = assistant_client
    vision(monkeypatch)
    save(client, auto_title=False)
    manager, sid, scope = setup_browser(client)
    source_image = image_message(validate_image(jpeg()), 'image_id=fixture-image source_message_id=123')
    with manager.session_factory() as db:
        manager._message(db, sid, "user", "Old user request")
        manager._message(db, sid, "system", "Screenshot evidence", context_image=True,
                         _image=jpeg(), _model_message=source_image)
        for index in range(50):
            manager._message(db, sid, "user", f"Historical request {index} " + "x" * 1000)
        db.commit()
    sources = []

    async def summarize(config, messages):
        sources.extend(messages)
        assert "data:image" not in _json(messages) and "image_base64" not in _json(messages)
        return "Historical screenshot evidence omitted; image ID fixture-image"

    monkeypatch.setattr("app.services.assistant.summarize_context", summarize)
    provider[0].append(text("Summary used"))
    send(client, sid)
    detail = wait(client, sid)
    assert detail["messages"][-1]["content"] == "Summary used" and sources
    assert "fixture-image" in _json(sources) and "image evidence omitted" in _json(sources)
    extracted = manager._context_extract([source_image])
    assert "data:image" not in extracted and "fixture-image" in extracted
    assert "source_message_id=123" in extracted
    _, _, _, _, _, protected, _ = manager._context_history("admin", sid)
    assert all(isinstance(item["content"], str) for item in protected)


def test_image_compaction_preserves_latest_and_never_slices_data_urls(assistant_client):
    manager = assistant_client.app.state.assistant_manager
    old = image_message(validate_image(jpeg()), "image_id=old")
    latest = image_message(validate_image(jpeg()), "image_id=latest")
    users = [{"role": "user", "content": "actual host user: " + "Keep the original goal. " * 40
              + "EXACT_CONSTRAINT_TAIL_DO_NOT_PURCHASE"}]
    messages = [{"role": "system", "content": "system"}] * 2 + users + [old, latest]
    original = deepcopy(messages)
    projected = messages[:-2] + [{**old, "content": omit_images(old["content"])}, latest]
    full_tokens = estimate_context_tokens(messages)
    projected_tokens = estimate_context_tokens(projected)
    limit = (full_tokens + projected_tokens) // 2
    assert projected_tokens <= limit < full_tokens
    fitted = manager._fit_context(messages, protected_users=users, limit=limit, cap=64000)
    assert fitted == projected and estimate_context_tokens(fitted) <= limit
    assert messages == original
    assert latest in fitted
    assert any(isinstance(item.get("content"), str) and "image_id=old" in item["content"] for item in fitted)
    assert old not in fitted and users[0] in fitted


def test_auto_compact_off_enforces_request_cap_without_slicing_images(assistant_client, provider, monkeypatch):
    import random

    client = assistant_client
    vision(monkeypatch)
    save(client, auto_title=False, auto_compact=False)
    manager, sid, scope = setup_browser(client)
    output = BytesIO()
    Image.frombytes("RGB", (640, 480), random.Random(1).randbytes(640 * 480 * 3)).save(
        output, "JPEG", quality=60)
    image = {"mime_type": "image/jpeg", "image_base64": base64.b64encode(output.getvalue()).decode(),
             "width": 640, "height": 480, "masked": True}
    canonical = image_message(validate_image(image), "image_id=large-preserved")
    with manager.session_factory() as db:
        for _ in range(12):
            manager._message(db, sid, "system", "Screenshot evidence", context_image=True,
                             _image=image, _model_message=canonical, _target_revision=1)
        db.commit()
    send(client, sid)
    detail = wait(client, sid)
    assert "[request_limit]" in detail["messages"][-1]["content"]
    assert not provider[1]
    _, _, records, _, _, _, _ = manager._context_history("admin", sid, auto_compact=False)
    images = [item for _, item in records if isinstance(item.get("content"), list)]
    assert images == [canonical] * 12


@pytest.mark.parametrize("auto_compact", [False, True])
def test_revoked_and_renewed_consent_never_replays_old_target_pixels(
    assistant_client, provider, monkeypatch, auto_compact,
):
    client = assistant_client
    vision(monkeypatch)
    save(client, auto_title=False, auto_compact=auto_compact)
    manager, sid, scope = setup_browser(client)
    provider[0].extend([tool("browser_screenshot", call_id="old_capture"), raw_image(scope),
                        text("Original screenshot reviewed")])
    send(client, sid)
    detail = wait(client, sid)
    capture = next(item for item in detail["messages"] if item.get("tool_name") == "browser_screenshot")
    metadata = json.loads(capture["content"])
    original_pixels = client.get(metadata["image_url"]).content
    with manager.session_factory() as db:
        original = db.scalar(select(AssistantMessage).where(AssistantMessage.uuid == metadata["image_id"]))
        encrypted = (original.ciphertext, original.nonce, original.key_version)

    provider[0].append({"closed": True})
    target(client, sid, authorized=True, screenshot_consent=False)
    provider[0].append(text("Consent revoked; pixels omitted"))
    send(client, sid)
    wait(client, sid)
    revoked = json.loads(provider[1][-1].content)["messages"]
    assert "data:image" not in _json(revoked)
    assert metadata["image_id"] in _json(revoked) and "image evidence omitted" in _json(revoked)
    assert client.get(metadata["image_url"]).content == original_pixels

    target(client, sid, authorized=True, screenshot_consent=True)
    scope = BrowserScope("admin", str(uuid4()), "replacement-token", "interaction", "",
                         time.monotonic() + 900)
    manager.browsers[sid] = scope
    provider[0].extend([tool("browser_screenshot", call_id="new_capture"), raw_image(scope, jpeg(40, 30)),
                        text("Only fresh pixels reviewed")])
    send(client, sid)
    detail = wait(client, sid)
    # The first request after renewal must not resurrect even same-origin images.
    renewed = json.loads(provider[1][-3].content)["messages"]
    assert "data:image" not in _json(renewed)
    fresh = json.loads(provider[1][-1].content)["messages"]
    image_messages = [item for item in fresh if isinstance(item.get("content"), list)]
    assert len(image_messages) == 1 and metadata["image_id"] not in _json(image_messages)
    assert metadata["image_id"] in _json(fresh) and "image evidence omitted" in _json(fresh)
    with manager.session_factory() as db:
        old = db.scalar(select(AssistantMessage).where(AssistantMessage.uuid == metadata["image_id"]))
        assert (old.ciphertext, old.nonce, old.key_version) == encrypted
        images = [manager._decrypt(row) for row in db.scalars(select(AssistantMessage).where(
            AssistantMessage.session_id == sid, AssistantMessage.role == "system"))
                  if manager._decrypt(row).get("context_image")]
        assert [item["_target_revision"] for item in images] == [1, 3]


@pytest.mark.parametrize("auto_compact", [False, True])
@pytest.mark.parametrize("revision", [None, 0, True])
def test_legacy_or_mismatched_revision_images_are_text_only(
    assistant_client, provider, monkeypatch, auto_compact, revision,
):
    client = assistant_client
    vision(monkeypatch)
    save(client, auto_title=False, auto_compact=auto_compact)
    manager, sid, scope = setup_browser(client)
    image = validate_image(jpeg())
    canonical = image_message(image, "image_id=legacy-image source_message_id=123")
    with manager.session_factory() as db:
        record = manager._message(
            db, sid, "system", "Screenshot evidence", context_image=True, _image=image,
            _model_message=canonical, **({"_target_revision": revision} if revision is not None else {}))
        db.commit()
        image_id = record.uuid
        ciphertext = record.ciphertext
    provider[0].append(text("Legacy evidence is metadata only"))
    send(client, sid)
    wait(client, sid)
    outbound = json.loads(provider[1][-1].content)["messages"]
    assert "data:image" not in _json(outbound)
    assert "legacy-image" in _json(outbound) and "image evidence omitted" in _json(outbound)
    assert client.get(f"{ROOT}/{sid}/images/{image_id}").status_code == 200
    with manager.session_factory() as db:
        row = db.scalar(select(AssistantMessage).where(AssistantMessage.uuid == image_id))
        assert row.ciphertext == ciphertext
        assert manager._decrypt(row)["_model_message"] == canonical


@pytest.mark.parametrize("restriction", ["no_consent", "no_vision", "different_target"])
@pytest.mark.parametrize("auto_compact", [False, True])
def test_current_authority_always_gates_historical_image_pixels(
    assistant_client, provider, monkeypatch, restriction, auto_compact,
):
    client = assistant_client
    if restriction != "no_vision":
        vision(monkeypatch)
    save(client, auto_title=False, auto_compact=auto_compact)
    manager, sid, scope = setup_browser(client, consent=restriction != "no_consent")
    image = validate_image(jpeg())
    canonical = image_message(image, "image_id=historical-image source_message_id=123")
    with manager.session_factory() as db:
        manager._message(db, sid, "system", "Screenshot evidence", context_image=True,
                         _image=image, _model_message=canonical, _target_revision=1)
        db.commit()
    if restriction == "different_target":
        provider[0].append({"closed": True})
        target(client, sid, authorized=True, screenshot_consent=True, base_url="https://other.example/")
    provider[0].append(text("Only historical image metadata is available"))
    send(client, sid)
    wait(client, sid)
    outbound = json.loads(provider[1][-1].content)["messages"]
    assert "data:image" not in _json(outbound)
    assert "historical-image" in _json(outbound) and "image evidence omitted" in _json(outbound)


def test_empty_recovery_status_is_authoritative(assistant_client):
    sid = create(assistant_client)
    detail = assistant_client.get(f"{ROOT}/{sid}").json()
    assert detail["unresolved_operations"] == [] and detail["retry_authorizations"] == []


@pytest.mark.parametrize("replacement", ["changed", "missing"])
def test_browser_invalidated_grant_is_expired_in_detail_and_model_context(
    assistant_client, provider, replacement,
):
    client = assistant_client
    save(client, auto_title=False)
    manager, sid, scope = setup_browser(client)
    operation = str(uuid4())
    args = {"operation_id": operation, "step": {"step_key": "click", "feature_type": "action.click",
                                               "config": {"selector": ["#approved"]}}}
    provider[0].extend([tool("execute_step", args, "original"),
                        execution_response(scope, operation, status="unknown", code="outcome_uncertain"),
                        text("Await explicit approval")])
    send(client, sid)
    wait(client, sid)
    body = {"operation_id": operation, "confirmed": True, "request_id": str(uuid4())}
    approval = client.post(f"{ROOT}/{sid}/retry-authorizations", json=body)
    assert approval.status_code == 200
    assert approval.json()["retry_authorizations"][0]["status"] == "approved"
    if replacement == "changed":
        manager.browsers[sid] = BrowserScope(
            "admin", str(uuid4()), "different-token", "interaction", "", time.monotonic() + 900)
    else:
        manager.browsers.pop(sid)
    detail = client.get(f"{ROOT}/{sid}").json()
    assert detail["retry_authorizations"][0]["status"] == "expired"
    assert detail["unresolved_operations"][0]["operation_id"] == operation
    provider[0].append(text("The browser-bound grant has expired"))
    send(client, sid)
    wait(client, sid)
    head = json.loads(provider[1][-1].content)["messages"][1]["content"]
    assert '"status":"expired"' in head and '"status":"approved"' not in head
