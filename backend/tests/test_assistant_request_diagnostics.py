from __future__ import annotations

from copy import deepcopy
import json

import pytest

from app.services.assistant_snapshot import POLICY_CODES
from tests.test_assistant_execution import (
    BASE, execution, response, run,  # noqa: F401
)
from tests.test_assistant_retry import approve, retry, unknown


def blocked(**fields):
    return {"code": "browser_url_denied", "method": "POST", "url": BASE + "login", **fields}


def respond(helper, diagnostics, *, status="error", code="execution_error"):
    async def worker(path, payload):
        raw = response(helper.manager.browsers[helper.session_id], payload["operation_id"],
                       status=status, code=code)
        raw.update(diagnostics=deepcopy(diagnostics), after={"elements": []}, result={})
        return raw

    helper.manager._worker.side_effect = worker


@pytest.mark.parametrize("status,code", [
    ("error", "execution_error"), ("error", "execution_timeout"),
    ("unknown", "outcome_uncertain"),
])
def test_blocked_requests_project_independently_of_generic_reason(execution, status, code):
    scope = execution.manager.browsers[execution.session_id]
    respond(execution, {
        "reason": "execution_error", "evidence_errorcode": "browser_snapshot_unavailable",
        "blocked_requests": [blocked(
            url=BASE + "login?next=%2Fhome&token=private-query#state=private-fragment&tab=1",
            url_redacted=False, redacted_parameters=["DO-NOT-EXPORT"],
            body="raw-POST-body", post_data="raw-POST-data", headers={"Cookie": "raw-header"},
            message="raw-message", credentials={"password": "unknown-credential"},
        )],
    }, status=status, code=code)
    result = run(execution)
    assert result["status"] == status and result["code"] == code and result["ok"] is False
    assert result["after"] == {"elements": []} and result["result"] == {}
    assert result["diagnostics"] == {
        "reason": "execution_error", "evidence_errorcode": "browser_snapshot_unavailable",
        "blocked_requests": [{
            "code": "browser_url_denied", "method": "POST",
            "url": BASE + "login?next=%2Fhome&token=[REDACTED]#state=[REDACTED]&tab=1",
            "url_redacted": True, "redacted_parameters": ["token", "state"],
        }],
    }
    assert all(value not in json.dumps(result) for value in [
        "private-query", "private-fragment", "DO-NOT-EXPORT", "raw-", "unknown-credential",
    ])
    assert execution.manager.browsers[execution.session_id] is scope
    execution.manager._close_browser.assert_not_awaited()


@pytest.mark.parametrize("method", [
    "GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "CONNECT", "TRACE", "OTHER",
])
def test_known_methods_and_policy_codes_are_retained(execution, method):
    for code in sorted(POLICY_CODES):
        respond(execution, {"reason": "unreviewed-reason", "blocked_requests": [blocked(
            code=code, method=method, url="https://other.example/endpoint?public=%2f#section",
            url_redacted=True, redacted_parameters=["untrusted-metadata"],
        )]})
        result = run(execution)
        assert result["diagnostics"] == {"blocked_requests": [{
            "code": code, "method": method,
            "url": "https://other.example/endpoint?public=%2f#section",
            "url_redacted": False, "redacted_parameters": [],
        }]}
    execution.manager._close_browser.assert_not_awaited()


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/private", "http://10.0.0.1/private", "http://[::1]/private",
    "http://localhost/private", "https://user:private-password@example.com/private",
    "javascript:private-code", "/relative-private", "https://example.com/" + "x" * 4096,
    "https://example.com/\nprivate", "[redacted]", None, [], {}, 1,
])
def test_invalid_urls_are_clipped_without_losing_static_diagnostics_or_browser(execution, url):
    scope = execution.manager.browsers[execution.session_id]
    respond(execution, {"blocked_requests": [blocked(url=url)]})
    result = run(execution)
    assert result["diagnostics"] == {"blocked_requests": [{
        "code": "browser_url_denied", "method": "POST",
        "url": "[redacted]", "url_redacted": True, "redacted_parameters": [],
    }]}
    assert "private" not in json.dumps(result)
    assert execution.manager.browsers[execution.session_id] is scope
    execution.manager._close_browser.assert_not_awaited()


@pytest.mark.parametrize("entry", [
    None, [], "DO-NOT-EXPORT", {},
    blocked(code="browser_method_denied"), blocked(code="browser_url_denied: DO-NOT-EXPORT"),
    blocked(code=[]), blocked(code=True), blocked(method="post"),
    blocked(method="DO-NOT-EXPORT"), blocked(method={}), blocked(method=None),
])
def test_invalid_entries_do_not_discard_sibling_requests_or_reason(execution, entry):
    respond(execution, {"reason": "browser_url_denied", "blocked_requests": [entry, blocked()]})
    result = run(execution)
    assert result["diagnostics"] == {
        "reason": "browser_url_denied", "blocked_requests": [{
            **blocked(), "url_redacted": False, "redacted_parameters": [],
        }],
    }
    execution.manager._close_browser.assert_not_awaited()


@pytest.mark.parametrize("requests", [None, {}, "DO-NOT-EXPORT", [], [blocked(code="unknown")]])
@pytest.mark.parametrize("reason", [None, "execution_error"])
def test_empty_or_invalid_lists_are_omitted_independently(execution, requests, reason):
    respond(execution, {"reason": reason, "blocked_requests": requests})
    result = run(execution)
    if reason:
        assert result["diagnostics"] == {"reason": reason}
    else:
        assert "diagnostics" not in result
    execution.manager._close_browser.assert_not_awaited()


@pytest.mark.parametrize("count", [1, 5, 6, 100])
def test_only_first_five_blocked_requests_are_projected(execution, count):
    respond(execution, {"blocked_requests": [blocked(url=BASE + f"blocked/{i}") for i in range(count)]})
    result = run(execution)
    requests = result["diagnostics"]["blocked_requests"]
    assert len(requests) == min(count, 5)
    assert [request["url"] for request in requests] == [
        BASE + f"blocked/{i}" for i in range(min(count, 5))
    ]
    execution.manager._close_browser.assert_not_awaited()


def test_retry_keeps_host_retry_fields_and_projects_request_diagnostics(execution):
    operation = unknown(execution)["operation_id"]
    approve(execution, operation)
    respond(execution, {"blocked_requests": [blocked(url=BASE + "login?password=private-query")]})
    result = retry(execution, operation)
    assert result["retry_of"] == operation and result["operation_id"] != operation
    assert result["status"] == "error" and result["code"] == "execution_error"
    assert result["diagnostics"]["blocked_requests"] == [{
        "code": "browser_url_denied", "method": "POST", "url": BASE + "login?password=[REDACTED]",
        "url_redacted": True, "redacted_parameters": ["password"],
    }]
    assert "private-query" not in json.dumps(result)
    execution.manager._close_browser.assert_not_awaited()
