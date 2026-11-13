from __future__ import annotations

import asyncio
import json
from pathlib import Path
import socket
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from app.services.assistant_tools import AssistantTools
from app.services.assistant_retry import PENDING
from app.services.assistant_urls import trust_required
from tests.test_assistant_execution import (  # noqa: F401
    BASE, OWNER, PASSWORD, TOKEN, USERNAME, arguments, bind, execution, save_target,
)
from tests.test_assistant_retry import load


@pytest.mark.parametrize("reason", ["browser_url_denied", "browser_credential_endpoint_not_approved"])
@pytest.mark.parametrize("missing_evidence", [False, True])
@pytest.mark.parametrize("trust_warning", [False, True])
def test_uncertain_login_keeps_safe_reads_but_unsafe_current_url_closes(
    execution, monkeypatch, reason, missing_evidence, trust_warning,
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    from tests.test_assistant_browser import Manager
    from worker import executor
    from worker.app import create_app
    from worker.assistant_browser import AssistantBrowser

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", AsyncMock(return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
    ]))
    bind(execution)
    save_target(execution, {**execution.target, "browser_consent": True})
    manager = execution.manager
    manager.browsers.clear()
    lane_manager = Manager()
    browser = AssistantBrowser(lane_manager)
    app = create_app()
    app.state.assistant_browser = browser
    client_type = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_type(
        transport=httpx.ASGITransport(app=app), **kwargs,
    ))
    monkeypatch.setattr(manager, "_worker", type(manager)._worker.__get__(manager))
    close = AsyncMock(wraps=type(manager)._close_browser.__get__(manager))
    monkeypatch.setattr(manager, "_close_browser", close)
    send = lane_manager.cdp.send.side_effect

    async def cdp(method, params=None):
        if method == "Runtime.callFunctionOn":
            return {"result": {"value": {"elements": [{
                "node_id": "n0", "parent_id": None, "tag": "h1", "visible": True,
                "attributes": {"id": "login"},
                "text": "Login page",
            }]}}}
        return await send(method, params)

    lane_manager.cdp.send.side_effect = cdp
    tools = AssistantTools(manager, OWNER, execution.session_id, execution.target)

    async def scenario():
        try:
            assert (await tools.dispatch("browser_open", {}))["ok"]
            scope = manager.browsers[execution.session_id]
            session = browser.sessions[scope.session_id]
            page = session.page
            original_collect = browser._collect
            collections = 0
            delayed_failure = {"code": "browser_dns_denied"}
            if trust_warning:
                delayed_failure = {
                    "code": "browser_trust_required",
                    "source_url": BASE + "login?token=PRIVATE&tab=one",
                    "target_url": "https://sso.example/login?state=PRIVATE&redirect=%2F",
                }

            async def collect(*args, **kwargs):
                nonlocal collections
                collections += 1
                if collections == 2:
                    # A later blocked request must survive the operation's cleanup
                    # and become a one-shot warning only after a safe observation.
                    session.failure = delayed_failure
                    if missing_evidence:
                        raise RuntimeError("DO-NOT-EXPORT-evidence-error")
                return await original_collect(*args, **kwargs)

            monkeypatch.setattr(browser, "_collect", collect)

            async def step(actual_page, normalized, **kwargs):
                assert actual_page is page
                assert normalized["feature_type"] == "auth.form_login"
                context = kwargs["secret_payload"]["_assistant_secret_context"]
                assert context.credentials == {"username": USERNAME, "password": PASSWORD}
                context.action_started = True
                session.failure = {"code": reason}
                raise HTTPException(403, {"code": reason, "message": "DO-NOT-EXPORT-step-error"})

            run_step = AsyncMock(side_effect=step)
            monkeypatch.setattr(executor, "_run_step", run_step)
            args = arguments("auth.form_login")
            result = await tools.dispatch("execute_step", args)
            assert result["ok"] is False and result["status"] == "unknown"
            assert result["code"] == "outcome_uncertain"
            assert result["diagnostics"]["reason"] == reason
            assert result["before"]["elements"][0]["text"] == "Login page"
            if missing_evidence:
                assert result["after"] == {"elements": []}
                assert result["diagnostics"]["evidence_errorcode"] == "browser_snapshot_unavailable"
            else:
                assert result["after"]["elements"][0]["text"] == "Login page"
            assert manager.browsers[execution.session_id] is scope
            assert session.uncertain and session.failure is delayed_failure
            assert session.page is page and not session.closed
            close.assert_not_awaited()

            observed = await tools.dispatch("browser_observe", {})
            assert observed["ok"] and observed["url"] == BASE
            assert observed["elements"][0]["text"] == "Login page"
            if trust_warning:
                expected = trust_required(delayed_failure["source_url"], delayed_failure["target_url"])
                assert observed["warnings"] == [{
                    **{key: value for key, value in expected.items() if key != "ok"},
                    "source": "blocked_request",
                }]
                assert "PRIVATE" not in json.dumps(observed)
                assert "https://sso.example" not in execution.target.get("trusted_origins", [])
            else:
                assert observed["warnings"] == [{"code": "browser_dns_denied", "source": "blocked_request"}]
            assert session.failure is None
            refreshed = await tools.dispatch("browser_observe", {})
            assert refreshed["ok"] and "warnings" not in refreshed
            inspected = await tools.dispatch("browser_inspect", {
                "snapshot_id": refreshed["snapshot_id"], "node_id": refreshed["elements"][0]["node_id"],
            })
            assert inspected["ok"] and inspected["elements"] == refreshed["elements"]
            checked = await tools.dispatch("browser_check_selector", {"selector": "#login"})
            assert checked["ok"]
            assert (await tools.dispatch("execute_step", args))["code"] == "operation_already_used"
            assert not load(execution).get(PENDING)
            run_step.assert_awaited_once()
            assert manager.browsers[execution.session_id] is scope
            close.assert_not_awaited()
            lane_manager.page.close.assert_not_awaited()
            lane_manager.release_lane.assert_not_awaited()
            lane_manager.context.new_page.assert_awaited_once()
            exposed = json.dumps([result, observed, refreshed, inspected, checked])
            assert all(secret not in exposed for secret in [USERNAME, PASSWORD, TOKEN, scope.token, "DO-NOT-EXPORT"])

            page.url = "javascript:void(0)"
            session.failure = {"code": "browser_url_denied"}
            denied = await tools.dispatch("browser_observe", {})
            assert denied["ok"] is False and denied["code"] == "browser_unavailable"
            assert denied["reason"] == "browser_url_denied" and "warnings" not in denied
            close.assert_awaited_once_with(execution.session_id)
            assert execution.session_id not in manager.browsers
            assert scope.session_id not in browser.sessions
            lane_manager.page.close.assert_awaited_once()
            lane_manager.release_lane.assert_awaited_once()
        finally:
            await browser.stop()

    asyncio.run(scenario())
