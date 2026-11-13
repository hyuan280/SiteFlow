"""Non-post retries no longer open dialogs; explicit approval APIs remain shipped."""

import json
import time
from uuid import uuid4

import pytest

from app.models.assistant import AssistantSession
from app.services.assistant import BrowserScope
from app.services.assistant_retry import GRANTS, PENDING, RECORDS
from tests.test_assistant_api import (  # noqa: F401
    ROOT, assistant_client, create, provider, send, target, text, tool, wait,
)
from tests.test_assistant_execution import TOKEN, arguments, response
from tests.test_assistant_runtime_settings import save


@pytest.mark.parametrize("retry_tool,reuse_original", [
    ("retry_step", True), ("execute_step", True), ("execute_step", False),
])
def test_nonpost_retry_policy_without_dialog(assistant_client, provider, retry_tool, reuse_original):
    client = assistant_client
    save(client, auto_title=False)
    sid = create(client)
    target(client, sid, authorized=True)
    manager = client.app.state.assistant_manager
    scope = BrowserScope("admin", str(uuid4()), TOKEN, "interaction", "", time.monotonic() + 900)
    manager.browsers[sid] = scope
    args = arguments("action.click", {"selector": ["#approved"]})
    provider[0].extend([
        tool("execute_step", args, "original"),
        response(scope, args["operation_id"], status="unknown", code="outcome_uncertain"),
        text("Inspect before retrying"),
    ])
    send(client, sid)
    assert wait(client, sid)["pending_approval"] is None
    captured = []

    async def executed(request):
        payload = json.loads(request.content)
        captured.append(payload)
        return response(scope, payload["operation_id"])

    retry_args = ({"operation_id": args["operation_id"]} if retry_tool == "retry_step"
                  else {**args, "operation_id": args["operation_id"] if reuse_original else str(uuid4())})
    duplicate = retry_tool == "execute_step" and reuse_original
    provider[0].append(tool(retry_tool, retry_args, "retry"))
    if not duplicate:
        provider[0].append(executed)
    provider[0].append(text("Finished"))
    send(client, sid, content="Retry the uncertain action")
    detail = wait(client, sid)
    assert detail["pending_approval"] is None and not manager._approval_waiters
    result = json.loads([item["content"] for item in detail["messages"]
                         if item.get("tool_name") == retry_tool][-1])
    if duplicate:
        assert result["code"] == "operation_already_used" and not captured
        return
    assert result["ok"] and len(captured) == 1
    assert captured[0]["operation_id"] != args["operation_id"]
    with manager.session_factory() as db:
        stored = manager._decrypt(db.get(AssistantSession, sid))
    assert not stored.get(PENDING)
    original = stored[RECORDS][args["operation_id"]]
    assert original["status"] == "unknown"
    if retry_tool == "retry_step":
        grant = stored[GRANTS][args["operation_id"]]
        assert grant["automatic"] and grant["status"] == "used"
        assert grant["invocation"]["tool_call_id"] == "retry"
        assert captured[0]["step"] == original["step"]
        assert captured[0]["retry_of"] == args["operation_id"]
        assert original["resolution"]["status"] == "success"
        assert grant["id"] not in json.dumps(detail)
    else:
        assert "resolution" not in original and "retry_of" not in captured[0]


def test_legacy_decision_endpoint_requires_auth_csrf_and_strict_body(assistant_client):
    client = assistant_client
    sid = create(client)
    url = f"{ROOT}/{sid}/retry-approvals/{uuid4()}/decision"
    token = client.headers.pop("X-CSRF-Token")
    assert client.post(url, json={"decision": "approve"}).status_code == 403
    client.headers["X-CSRF-Token"] = token
    for body in ({}, {"decision": True}, {"decision": "approved"},
                 {"decision": "approve", "step": {}}):
        assert client.post(url, json=body).status_code == 422
    assert client.post(url, json={"decision": "approve"}).status_code == 404
    client.cookies.clear()
    assert client.post(url, json={"decision": "approve"}).status_code == 401
