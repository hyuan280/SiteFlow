from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import re
from unittest.mock import AsyncMock
from urllib.parse import quote

import httpx
import pytest
from pydantic import ValidationError

from app.schemas.assistant import SetTarget, public_url
from app.services.assistant import AssistantError
from app.services.assistant_tools import AssistantTools, safe_snapshot
from app.services.assistant_urls import project_url, resolve_url, trust_required, trusted_url_pattern
from app.services.workflows import WorkflowService
from tests.test_assistant_api import ROOT, TARGET, create, send, target, text, tool, wait
from tests.test_assistant_api import assistant_client, provider  # noqa: F401
from tests.test_assistant_snapshot import node, snapshot
from tests.test_assistant_execution import (
    arguments, execution, response, run, save_target,  # noqa: F401
)


@pytest.mark.parametrize("suffix", [
    "/login?tab=foo&next=%2fhome&tab=bar+one&empty=&flag#/login?tab=foo",
    "/?return_to=https%3a%2f%2fother.example%2flogin%3Ftab%3Dfoo%26q%3Da%2520b#section",
    "/?redirect=https://other.example/login?tab=foo#details",
    "/?code_format=json&stateful=1&signature_version=2#tabs",
    "/?q=hello%20world&encoded=%e2%98%83#/%E2%98%83?tab=foo",
    "/path?b=2&a=%2f%2F&&empty=#", "/?", "/#", "/?#",
])
def test_full_public_url_preserves_raw_suffix(suffix):
    url = "https://example.com" + suffix
    assert public_url("https://EXAMPLE.com:443" + suffix) == url
    assert project_url(url) == {"url": url, "url_redacted": False, "redacted_parameters": []}


@pytest.mark.parametrize("name", [
    "token", "access_token", "Refresh-Token", "ID.Token", "password", "passwd",
    "secret", "api_key", "Authorization", "cookie", "session", "session_id", "sid",
    "code", "state", "oauth_code", "signature", "sig", "X-Amz-Signature", "csrf_token",
])
def test_sensitive_values_only_are_redacted(name):
    key = quote(name, safe="")
    raw = f"https://example.com/?x=%2f&{key}=PRIVATE%2BVALUE&x=one+two#tab"
    assert project_url(raw) == {
        "url": raw.replace("PRIVATE%2BVALUE", "[REDACTED]"),
        "url_redacted": True, "redacted_parameters": [name],
    }


@pytest.mark.parametrize("suffix,expected,names", [
    ("#access_token=PRIVATE&tab=foo", "#access_token=[REDACTED]&tab=foo", ["access_token"]),
    ("#/login?tab=foo&code=PRIVATE", "#/login?tab=foo&code=[REDACTED]", ["code"]),
    ("?%74oken=PRIVATE;tab=foo&token=SECOND", "?%74oken=[REDACTED];tab=foo&token=[REDACTED]", ["token"]),
    ("?return_to=https%3a%2f%2fother.example%2flogin%3ftab%3dfoo%26token%3dPRIVATE%26q%3da%2520b",
     "?return_to=https%3a%2f%2fother.example%2flogin%3ftab%3dfoo%26token%3d%5BREDACTED%5D%26q%3da%2520b", ["token"]),
    ("?return_to=https%3A%2F%2Fu%3Ap%40other.example%2F", "?return_to=[REDACTED]", ["return_to"]),
])
def test_fragment_and_nested_url_redaction(suffix, expected, names):
    assert project_url("https://example.com/" + suffix) == {
        "url": "https://example.com/" + expected,
        "url_redacted": True, "redacted_parameters": names,
    }


@pytest.mark.parametrize("layers", [2, 3])
@pytest.mark.parametrize("suffix", ["?tab=foo&token=PRIVATE&q=a%20b", "#/login?tab=foo&state=PRIVATE&q=a%20b"])
def test_multilayer_nested_url_redaction_preserves_public_bytes(layers, suffix):
    nested = "https://other.example/%E2%98%83" + suffix
    marker = "[REDACTED]"
    for _ in range(layers):
        nested = quote(nested, safe="")
        marker = quote(marker, safe="")
    raw = "https://example.com/?x=%2f&return_to=" + nested + "&x=one+two#overview"
    result = project_url(raw)
    assert result == {
        "url": raw.replace("PRIVATE", marker), "url_redacted": True,
        "redacted_parameters": ["token" if suffix.startswith("?") else "state"],
    }
    assert project_url(result["url"]) == result


@pytest.mark.parametrize("layers", [2, 3])
def test_multilayer_public_url_is_unchanged(layers):
    nested = "https://other.example/login?tab=foo&q=a%20b&next=%2fhome#details"
    for _ in range(layers):
        nested = quote(nested, safe="")
    raw = "https://example.com/?return_to=" + nested + "&tab=one+two"
    assert project_url(raw) == {"url": raw, "url_redacted": False, "redacted_parameters": []}


@pytest.mark.parametrize("layers", [2, 3])
def test_multilayer_userinfo_is_redacted(layers):
    nested = "https://user:PRIVATE@other.example/login?tab=foo"
    for _ in range(layers):
        nested = quote(nested, safe="")
    assert project_url("https://example.com/?return_to=" + nested) == {
        "url": "https://example.com/?return_to=[REDACTED]",
        "url_redacted": True, "redacted_parameters": ["return_to"],
    }


def test_nested_decoding_budget_fails_closed():
    nested = "https://other.example/login?token=PRIVATE"
    for _ in range(12):
        nested = quote(nested, safe="")
    assert project_url("https://example.com/?return_to=" + nested) == {
        "url": "https://example.com/?return_to=[REDACTED]",
        "url_redacted": True, "redacted_parameters": ["return_to"],
    }


def test_nested_redirects_compose_multilayer_raw_spans_and_trust_flags():
    nested = "https://sso.example/login?tab=foo&token=PRIVATE&q=a%20b"
    marker = "[REDACTED]"
    for _ in range(2):
        nested = quote(nested, safe="")
        marker = quote(marker, safe="")
    nested = "https://other.example/?next=" + nested + "&tab=bar#details"
    for _ in range(3):
        nested = quote(nested, safe="")
        marker = quote(marker, safe="")
    raw = "https://example.com/?return_to=" + nested + "&q=one+two#overview"
    result = trust_required("https://source.example/?tab=foo", raw)
    assert result["target_url"] == raw.replace("PRIVATE", marker)
    assert result["target_url_redacted"] is True
    assert result["source_url_redacted"] is False
    assert result["redacted_parameters"] == ["token"]
    assert "informational candidate for separate user review" in result["notice"]
    assert "not approval to send credentials" in result["notice"]


@pytest.mark.parametrize("value", [
    "https://user:password@example.com/", "http://127.0.0.1/", "https://localhost/",
    "https://example.com/%0a", "https://example.com/?q=%5c", "https://example.com/\n",
    "https://example.com./", "https:///example.com/", "https://example.com:99999/",
])
def test_unsafe_urls_still_rejected(value):
    with pytest.raises(ValueError):
        public_url(value)


@pytest.mark.parametrize("origins", [
    ["https://other.example/"], ["https://other.example/path"], ["https://other.example?"],
    ["https://other.example#"], ["https://u:p@other.example"], ["HTTPS://other.example"],
    ["https://other.example:443"], ["http://127.0.0.1"], ["https://other.example"] * 21,
    "https://other.example", [True],
])
def test_trust_input_is_strict_public_origin_only(origins):
    with pytest.raises(ValidationError):
        SetTarget(**TARGET, trusted_origins=origins)


@pytest.mark.parametrize("origins", [[], ["https://sso.example", "https://sso.example:8443"]])
def test_target_keeps_explicit_trust_and_full_url_without_leaking_parameters(assistant_client, origins):
    identifier = create(assistant_client)
    full = "https://EXAMPLE.com:443/login?tab=foo&next=%2fhome#/login?tab=foo"
    detail = target(assistant_client, identifier, base_url=full, trusted_origins=origins)
    assert detail["target"]["base_url"] == full.replace("EXAMPLE.com:443", "example.com")
    assert detail["target"]["trusted_origins"] == origins
    secret = target(assistant_client, identifier, base_url=full + "&token=PRIVATE")
    assert "PRIVATE" not in json.dumps(secret)
    assert secret["target"]["base_url_redacted"] is True
    manager = assistant_client.app.state.assistant_manager
    private, history = manager._history("admin", identifier)
    assert "PRIVATE" in private["base_url"]
    assert "PRIVATE" not in json.dumps(history)


@pytest.mark.parametrize("value", ["/login?tab=foo&next=%2fhome#/login?tab=foo", "?tab=foo#", "#", "/login?", "/login?#"])
def test_navigation_reference_keeps_empty_and_full_suffix(value):
    resolved = resolve_url("https://example.com/old?before=1#old", value)
    if value.startswith("/"):
        assert resolved == "https://example.com" + value
    elif value.startswith("?"):
        assert resolved == "https://example.com/old" + value
    else:
        assert resolved == "https://example.com/old?before=1#"


def test_model_navigation_requires_one_argument_and_never_approves_origins(assistant_client):
    manager = assistant_client.app.state.assistant_manager
    identifier = create(assistant_client)
    confirmed = target(assistant_client, identifier, trusted_origins=["https://sso.example"])["target"]
    manager.browser_tool = AsyncMock(return_value={"ok": True})
    tools = AssistantTools(manager, "admin", identifier, confirmed)

    async def scenario():
        for args in ({}, {"path": "/", "url": "https://sso.example"},
                     {"path": "/", "trusted_origins": ["https://evil.example"]}):
            assert (await tools.dispatch("browser_navigate", args))["code"] == "invalid_arguments"
        for args, expected in [
            ({"path": "/login?tab=foo#details"}, "https://example.com/login?tab=foo#details"),
            ({"url": "https://sso.example/login?tab=foo#details"}, "https://sso.example/login?tab=foo#details"),
        ]:
            assert (await tools.dispatch("browser_navigate", args))["ok"]
            assert manager.browser_tool.call_args.args[-1] == expected
        manager.browser_tool.reset_mock()
        result = await tools.dispatch("browser_navigate", {
            "url": "https://other.example/login?tab=foo&token=PRIVATE#details",
        })
        assert result["code"] == "browser_trust_required"
        assert result["user_action"] == "confirm_trusted_origin"
        assert result["target_origin"] == "https://other.example"
        assert result["target_url"] == "https://other.example/login?tab=foo&token=[REDACTED]#details"
        assert "PRIVATE" not in json.dumps(result)
        manager.browser_tool.assert_not_awaited()
    asyncio.run(scenario())


def test_worker_trust_error_is_bounded_and_projected_again(assistant_client, provider):
    manager = assistant_client.app.state.assistant_manager
    raw = {
        "code": "browser_trust_required", "source_url": "https://example.com/?state=SOURCE#overview",
        "target_url": "https://sso.example:8443/login?tab=foo&token=TARGET#/login?tab=bar",
        "source_url_redacted": False, "target_url_redacted": False,
        "redacted_parameters": ["untrusted metadata"], "message": "PRIVATE", "token": "PRIVATE",
        "trusted_url_pattern": ".*",
    }
    provider[0].append(httpx.Response(403, json={"detail": raw}))

    raw_expected = trust_required(raw["source_url"], raw["target_url"])
    assert raw_expected["trusted_url_pattern"] == r"^https://sso\.example:8443(?:[/?#].*)?$"
    async def bounded():
        with pytest.raises(AssistantError) as exc:
            await manager._worker("open", {})
        assert exc.value.code == "browser_trust_required"
        assert exc.value.details == raw_expected
        assert "PRIVATE" not in json.dumps(exc.value.details)
        assert exc.value.details["redacted_parameters"] == ["state", "token"]
    asyncio.run(bounded())


@pytest.mark.parametrize("case", ["oversized", "private", "userinfo", "missing", "invalid"])
def test_unsafe_worker_trust_details_fail_closed(assistant_client, provider, case):
    raw = {"code": "browser_trust_required", "source_url": TARGET["base_url"],
           "target_url": "https://sso.example/login?token=PRIVATE"}
    if case == "private":
        raw["target_url"] = "http://127.0.0.1/private"
    elif case == "userinfo":
        raw["target_url"] = "https://u:PRIVATE@sso.example/login"
    elif case == "missing":
        del raw["target_url"]
    elif case == "invalid":
        raw["source_url"] = 123
    body = json.dumps({"detail": raw})
    if case == "oversized":
        body += " " * (16 * 1024)
    provider[0].append(httpx.Response(403, content=body.encode()))
    manager = assistant_client.app.state.assistant_manager

    async def scenario():
        with pytest.raises(AssistantError) as exc:
            await manager._worker("open", {})
        assert exc.value.code == "browser_unavailable"
        assert exc.value.details == {}
    asyncio.run(scenario())


def test_trust_failure_is_projected_in_persisted_history_and_model_context(assistant_client, provider):
    identifier = create(assistant_client)
    target(assistant_client, identifier)
    provider[0].extend([
        tool("browser_open"), httpx.Response(403, json={"detail": {
            "code": "browser_trust_required", "source_url": "https://example.com/?state=SOURCE_PRIVATE#overview",
            "target_url": "https://sso.example/login?tab=foo&code=TARGET_PRIVATE#details",
            "message": "RAW_PRIVATE", "trusted_url_pattern": ".*",
        }}), {"closed": True}, text("Confirm the SSO origin in target settings."),
    ])
    send(assistant_client, identifier)
    detail = wait(assistant_client, identifier)
    log = next(item for item in detail["messages"] if item["role"] == "tool")
    result = json.loads(log["content"])
    assert log["status"] == "error" and result["code"] == "browser_trust_required"
    assert result["target_url"] == "https://sso.example/login?tab=foo&code=[REDACTED]#details"
    assert result["source_url"] == "https://example.com/?state=[REDACTED]#overview"
    assert result["user_action"] == "confirm_trusted_origin"
    assert detail["target"].get("trusted_origins", []) == []
    manager = assistant_client.app.state.assistant_manager
    _, history = manager._history("admin", identifier)
    exposed = json.dumps([detail, history])
    for secret in ("SOURCE_PRIVATE", "TARGET_PRIVATE", "RAW_PRIVATE"):
        assert secret not in exposed
        assert all(secret not in request.content.decode() for request in provider[1]
                   if "chat/completions" in str(request.url))


@pytest.mark.parametrize("proxy", [False, True])
def test_browser_open_passes_host_trust_and_reports_actionable_failure(assistant_client, proxy):
    manager = assistant_client.app.state.assistant_manager
    identifier = create(assistant_client)
    confirmed = target(assistant_client, identifier, trusted_origins=["https://sso.example"], proxy=proxy)["target"]
    details = trust_required(confirmed["base_url"], "https://other.example/login?tab=foo&code=PRIVATE#details")
    manager._worker = AsyncMock(side_effect=AssistantError("browser_trust_required", 403, details=details))
    manager._close_browser = AsyncMock()
    result = asyncio.run(manager.browser_tool("admin", identifier, confirmed, "browser_open", None))
    assert result == {**details, "lane_key": "proxy_interaction" if proxy else "interaction"}
    assert manager._worker.call_args.args[1]["trusted_origins"] == ["https://sso.example"]
    manager.browsers.clear()


@pytest.mark.parametrize("host", ["https://sso.example", "https://sso.example:8443", "https://[2606:4700:4700::1111]"])
def test_trust_pattern_is_exact_and_boundary_safe(host):
    pattern = trusted_url_pattern(host)
    for suffix in ("", "/login", "?tab=foo", "#details", "/login?tab=foo#details"):
        assert re.fullmatch(pattern, host + suffix)
    for suffix in (".evil/", "@evil.example/", ":9999/", "evil/"):
        assert not re.fullmatch(pattern, host + suffix)


def test_snapshot_keeps_trusted_full_url_and_independent_link_redaction():
    raw = snapshot([node(tag="a", attributes={
        "href": "https://other.example/next?tab=bar&token=SECRET#overview"}, text="Next")],
        url="https://sso.example/login?tab=foo&state=PRIVATE#details", cookies="PRIVATE")
    assert not safe_snapshot(raw, TARGET)["ok"]
    result = safe_snapshot(raw, {**TARGET, "trusted_origins": ["https://sso.example"]})
    assert result["ok"] and result["url_redacted"]
    assert result["url"] == raw["url"].replace("PRIVATE", "[REDACTED]")
    assert result["redacted_parameters"] == ["state"]
    assert result["elements"][0]["attributes"]["href"] == raw["elements"][0]["attributes"]["href"].replace("SECRET", "[REDACTED]")
    assert "PRIVATE" not in json.dumps(result) and "SECRET" not in json.dumps(result)


def test_snapshot_preserves_worker_secret_scrubbed_urls():
    result = safe_snapshot(snapshot([node(tag="a", attributes={"href": "[redacted]"}, text="Next")],
        url="[redacted]", url_redacted=True), TARGET)
    assert result["ok"] and result["url_redacted"]
    assert result["url"] == "[redacted]"
    assert result["elements"][0]["attributes"]["href"] == "[redacted]"


def test_reprojection_keeps_nested_userinfo_redaction_flag():
    projected = project_url("https://example.com/?return_to=[REDACTED]&tab=foo")
    assert projected["url_redacted"] is True
    assert projected["url"] == "https://example.com/?return_to=[REDACTED]&tab=foo"
    assert projected["redacted_parameters"] == ["return_to"]


def test_draft_trust_settings_are_host_owned_and_never_saved(assistant_client, monkeypatch):
    manager = assistant_client.app.state.assistant_manager
    identifier = create(assistant_client)
    full = "https://example.com/login?tab=foo#/login?tab=foo/"
    confirmed = target(assistant_client, identifier, base_url=full,
                       trusted_origins=["https://sso.example:8443"])["target"]
    tools = AssistantTools(manager, "admin", identifier, confirmed)
    captured = []
    validate = WorkflowService.validate_draft

    def inspect(service, owner_type, payload, owner_id=None, *, replace_local):
        assert replace_local is True
        captured.append(payload)
        return validate(service, owner_type, payload, owner_id, replace_local=replace_local)
    monkeypatch.setattr(WorkflowService, "validate_draft", inspect)

    async def scenario():
        await tools.dispatch("get_feature_schema", {"feature_type": "action.wait"})
        result = await tools.dispatch("validate_workflow_draft", {"draft": {
            "steps": [{"step_key": "wait", "feature_type": "action.wait", "config": {}}],
            "groups": [], "signals": [],
        }})
        assert result["ok"], result
        assert result["required_site_settings"]["trusted_origins"] == ["https://sso.example:8443"]
        assert "trusted_urls" not in result["required_site_settings"]
        assert "observation only" in result["required_site_settings"]["notice"]
        assert "does not authorize credential trusted_urls" in result["required_site_settings"]["notice"]
        assert captured[0].trusted_urls == []
        assert captured[0].base_url == full
        result = await tools.dispatch("validate_workflow_draft", {"draft": {
            "steps": [{"step_key": "open", "feature_type": "flow.open", "config": {
                "url": "https://example.com/?token=[REDACTED]",
            }}], "groups": [], "signals": [],
        }})
        assert result["code"] == "redacted_url_not_usable"
    asyncio.run(scenario())
    assert assistant_client.get(f"{ROOT}/{identifier}").json()["target"] == confirmed


@pytest.mark.parametrize("url", ["/next?tab=foo&next=%2fhome#/login?tab=foo", "/next?", "/next#", "/next?#"])
def test_execution_keeps_full_nonsecret_url(execution, url):
    assert run(execution, arguments("action.navigate", {"url": url}))["ok"]
    payload = execution.manager._worker.call_args.args[1]
    assert payload["step"]["config"]["url"] == "https://example.com" + url


def test_execution_placeholder_keeps_full_host_target_suffix(execution):
    full = "https://example.com/login?tab=foo&next=%2fhome#/login?tab=foo/"
    save_target(execution, {**execution.target, "base_url": full})
    assert run(execution, arguments("flow.open"))["ok"]
    assert execution.manager._worker.call_args.args[1]["step"]["config"]["url"] == full


def test_execution_trust_does_not_expand_original_origin_authority(execution):
    save_target(execution, {**execution.target, "trusted_origins": ["https://sso.example"]})
    assert run(execution, arguments("action.navigate", {"url": "https://sso.example/login?tab=foo"})) == {
        "ok": False, "code": "invalid_step",
    }
    execution.manager._worker.assert_not_awaited()


def test_execution_projects_snapshot_full_url_metadata(execution):
    scope = execution.manager.browsers[execution.session_id]

    async def worker(path, payload):
        raw = response(scope, payload["operation_id"])
        raw["before"]["url"] = "https://example.com/?tab=foo&state=PRIVATE#overview"
        raw["after"]["url"] = "https://example.com/next?next=%2fhome&token=SECRET#details"
        return deepcopy(raw)
    execution.manager._worker.side_effect = worker
    result = run(execution)
    assert result["ok"]
    assert result["before"]["redacted_parameters"] == ["state"]
    assert result["after"]["redacted_parameters"] == ["token"]
    assert result["before"]["url_redacted"] and result["after"]["url_redacted"]
    assert "PRIVATE" not in json.dumps(result) and "SECRET" not in json.dumps(result)
