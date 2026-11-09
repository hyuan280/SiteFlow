from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from tests.test_assistant_browser import Manager, TOKEN, URL, public_dns  # noqa: F401
from tests.test_assistant_step import request
from worker.assistant_browser import AssistantBrowser, OpenRequest, _url
from worker.assistant_step import fingerprints, validate_secret_targets
from worker.assistant_urls import MAX_DECODE_ROUNDS, project_url


@pytest.mark.parametrize("url", [
    "https://example.com/login?tab=foo&next=%2fhome&tab=bar+one&empty=&flag#/login?tab=foo",
    "https://example.com/?return_to=https%3a%2f%2fother.example%2flogin%3Ftab%3Dfoo%26q%3Da%2520b#section",
    "https://example.com/?redirect=https://other.example/login?tab=foo#details",
    "https://example.com/?code_format=json&stateful=1&signature_version=2#tabs",
    "https://example.com/?q=hello%20world&encoded=%e2%98%83#/%E2%98%83?tab=foo",
    "https://EXAMPLE.com:443/path?b=2&a=%2f%2F&&empty=#",
    "https://example.com/?", "https://example.com/#", "https://example.com/?#",
])
def test_projection_preserves_every_non_sensitive_byte(url):
    assert project_url(url) == {"url": url, "url_redacted": False, "redacted_parameters": []}
    assert _url(url)[2].endswith(url[url.index("/", len("https://")):])


@pytest.mark.parametrize("name", [
    "token", "access_token", "Refresh-Token", "ID.Token", "password", "passwd",
    "secret", "api_key", "Authorization", "cookie", "session", "session_id", "sid",
    "code", "state", "oauth_code", "signature", "sig", "X-Amz-Signature", "csrf_token",
])
def test_sensitive_parameters_are_marked_without_reordering(name):
    raw_name = quote(name, safe="")
    url = f"https://example.com/?x=%2f&{raw_name}=PRIVATE%2BVALUE&x=one+two#tab"
    assert project_url(url) == {
        "url": f"https://example.com/?x=%2f&{raw_name}=[REDACTED]&x=one+two#tab",
        "url_redacted": True, "redacted_parameters": [name],
    }


@pytest.mark.parametrize("suffix,expected", [
    ("#access_token=PRIVATE&tab=foo", "#access_token=[REDACTED]&tab=foo"),
    ("#/login?tab=foo&access_token=PRIVATE", "#/login?tab=foo&access_token=[REDACTED]"),
    ("?%74oken=PRIVATE;tab=foo&token=SECOND", "?%74oken=[REDACTED];tab=foo&token=[REDACTED]"),
])
def test_fragment_and_duplicate_parameter_redaction(suffix, expected):
    result = project_url("https://example.com/" + suffix)
    assert result["url"] == "https://example.com/" + expected
    assert result["url_redacted"] is True
    assert len(result["redacted_parameters"]) == 1


def test_nested_redaction_preserves_other_raw_encoding():
    url = ("https://example.com/?return_to=https%3a%2f%2fother.example%2flogin"
           "%3ftab%3dfoo%26token%3dPRIVATE%26q%3da%2520b&next=%2fhome#/login?tab=foo")
    result = project_url(url)
    assert result["url"] == url.replace("PRIVATE", "%5BREDACTED%5D")
    assert result["redacted_parameters"] == ["token"]
    inner = "https://other.example/?tab=foo&access_token=PRIVATE"
    nested = "https://other.example/?return_to=" + quote(inner, safe="")
    url = "https://example.com/?redirect=" + quote(nested, safe="")
    result = project_url(url)
    assert result["url"] == url.replace("PRIVATE", "%255BREDACTED%255D")
    assert result["redacted_parameters"] == ["access_token"]


@pytest.mark.parametrize("rounds", [2, 3])
@pytest.mark.parametrize("suffix", ["?tab=foo&token=PRIVATE&q=a%20b", "#/login?tab=foo&access_token=PRIVATE"])
def test_multiply_encoded_nested_tokens_preserve_surrounding_bytes(rounds, suffix):
    nested = "https://other.example/login" + suffix
    marker = "[REDACTED]"
    for _ in range(rounds):
        nested = quote(nested, safe="").replace("%3A", "%3a").replace("%2F", "%2f")
        marker = quote(marker, safe="")
    url = "https://example.com/?tab=foo&next=" + nested + "&q=a+one#/login?tab=bar"
    result = project_url(url)
    assert result["url"] == url.replace("PRIVATE", marker)
    assert result["url_redacted"] is True
    assert result["redacted_parameters"] == ["access_token" if "access_token" in suffix else "token"]


@pytest.mark.parametrize("rounds", [2, 3, MAX_DECODE_ROUNDS])
def test_multiply_encoded_public_nested_url_is_unchanged(rounds):
    nested = "https://other.example/login?tab=foo&q=a%20b&code_format=json#/login?tab=bar"
    for _ in range(rounds):
        nested = quote(nested, safe="")
    url = "https://example.com/?next=" + nested + "&tab=foo"
    assert project_url(url) == {"url": url, "url_redacted": False, "redacted_parameters": []}


@pytest.mark.parametrize("rounds", [2, 3])
def test_visible_nested_scheme_does_not_hide_encoded_query(rounds):
    query, marker = "?tab=foo&token=PRIVATE&q=a%20b", "[REDACTED]"
    for _ in range(rounds):
        query = quote(query, safe="")
        marker = quote(marker, safe="")
    url = "https://example.com/?next=https://other.example/login" + query + "&tab=bar"
    assert project_url(url) == {
        "url": url.replace("PRIVATE", marker), "url_redacted": True,
        "redacted_parameters": ["token"],
    }


@pytest.mark.parametrize("url", [
    "https://user:password@example.com/", "file:///etc/passwd", "https://example.com/\\foo",
    "https://example.com/%5cfoo", "https://example.com/?q=%0a", "https://example.com/\n",
    "https://example.com/#\x7f", "https://example.com./", "https:///example.com/",
    "https://example.com/" + "x" * 4096,
])
def test_invalid_projection_rejects_without_echoing_input(url):
    with pytest.raises(ValueError, match="^Invalid URL$"):
        project_url(url)


def test_nested_userinfo_is_never_exposed():
    result = project_url("https://example.com/?return_to=https%3A%2F%2Fu%3Ap%40other.example%2F")
    assert result["url"] == "https://example.com/?return_to=[REDACTED]"
    assert result["redacted_parameters"] == ["return_to"]


@pytest.mark.parametrize("origins", [
    ["https://other.example/"], ["https://other.example/path"], ["https://other.example?tab=foo"],
    ["https://other.example#"], ["https://u:p@other.example"], ["HTTPS://other.example"],
    ["https://other.example:443"], ["https://other.example"] * 21, "https://other.example",
])
def test_trusted_origins_are_strict_origin_only(origins):
    with pytest.raises(ValidationError):
        OpenRequest(url=URL, token=TOKEN, trusted_origins=origins)


def test_full_query_open_navigation_and_observation(public_dns):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        url = URL + "?tab=foo&next=%2fhome#/login?tab=foo"
        view = await browser.open(url, "interaction", TOKEN)
        assert view["url"] == url and view["url_redacted"] is False
        observed = await browser.observe(view["session_id"], TOKEN)
        assert observed["url"] == url and observed["redacted_parameters"] == []
        target = URL + "?tab=bar&access_token=PRIVATE#/login?tab=foo"
        navigated = await browser.navigate(view["session_id"], TOKEN, target)
        assert manager.page.url == target
        assert navigated["url"] == target.replace("PRIVATE", "[REDACTED]")
        assert navigated["url_redacted"] is True
        await browser.stop()
    asyncio.run(run())


@pytest.mark.parametrize("stage", ["route", "redirect", "navigate"])
def test_cross_origin_candidate_has_full_projected_urls(public_dns, caplog, stage):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        source = URL + "?tab=foo&state=SOURCE_PRIVATE#overview"
        view = await browser.open(source, "interaction", TOKEN)
        session = browser.sessions[view["session_id"]]
        target = "https://other.example/login?tab=foo&token=TARGET_PRIVATE&next=%2Fhome#/login?tab=bar"
        if stage == "route":
            route = SimpleNamespace(request=SimpleNamespace(
                url=target, method="GET", resource_type="document", frame=manager.page.main_frame,
                is_navigation_request=lambda: True), abort=AsyncMock(), continue_=AsyncMock())
            await browser._route(session, route)
            route.abort.assert_awaited_once()
            route.continue_.assert_not_awaited()
        elif stage == "redirect":
            await browser._response(session, {
                "requestId": "r", "frameId": "main", "resourceType": "Document",
                "request": {"url": source, "method": "GET"}, "responseStatusCode": 302,
                "responseHeaders": [{"name": "Location", "value": target}],
            })
            assert manager.cdp.send.call_args.args[0] == "Fetch.failRequest"
        else:
            with pytest.raises(HTTPException) as exc:
                await browser.navigate(session.session_id, TOKEN, target)
            assert exc.value.status_code == 403
            assert exc.value.detail == session.failure
        assert session.failure == {
            "code": "browser_trust_required", "source_url": source.replace("SOURCE_PRIVATE", "[REDACTED]"),
            "target_url": target.replace("TARGET_PRIVATE", "[REDACTED]"),
            "source_url_redacted": True, "target_url_redacted": True,
            "redacted_parameters": ["state", "token"],
        }
        assert manager.page.url == source
        assert all(value not in caplog.text for value in [source, target, "SOURCE_PRIVATE", "TARGET_PRIVATE"])
        await browser.stop()
    asyncio.run(run())


def test_trusted_navigation_and_redirect_preserve_query_but_execution_denied(public_dns, monkeypatch):
    from worker import executor

    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        session_id = str(uuid.uuid4())
        view = await browser.open(URL, "interaction", TOKEN, session_id,
                                  trusted_origins=["https://other.example"])
        assert await browser.open(URL, "interaction", TOKEN, session_id,
                                  trusted_origins=["https://other.example"]) == view
        with pytest.raises(HTTPException) as exc:
            await browser.open(URL, "interaction", TOKEN, session_id)
        assert exc.value.status_code == 409
        target = "https://other.example/login?tab=foo#/login?tab=bar"
        assert (await browser.navigate(session_id, TOKEN, target))["url"] == target
        session = browser.sessions[session_id]
        route = SimpleNamespace(request=SimpleNamespace(
            url=target, method="GET", resource_type="document", frame=manager.page.main_frame,
            is_navigation_request=lambda: True), abort=AsyncMock(), continue_=AsyncMock())
        await browser._route(session, route)
        route.continue_.assert_awaited_once()
        await browser._response(session, {
            "requestId": "r", "frameId": "main", "resourceType": "Document",
            "request": {"url": target, "method": "GET"}, "responseStatusCode": 302,
            "responseHeaders": [{"name": "Location", "value": "?tab=baz#done"}],
        })
        assert manager.cdp.send.call_args.args[0] == "Fetch.continueResponse"
        assert (await browser.observe(session_id, TOKEN))["url"] == target
        execute = AsyncMock()
        monkeypatch.setattr(executor, "_run_step", execute)
        payload = request("auth.form_login", {
            "username_selector": ["#u"], "password_selector": ["#p"], "submit_selector": ["#s"],
        }, credentials={"username": "PRIVATE_USER", "password": "PRIVATE_PASSWORD"})
        result = await browser.execute(session_id, payload)
        assert result["status"] == "error"
        execute.assert_not_awaited()
        assert not session.secret_fingerprints
        with pytest.raises(RuntimeError):
            await validate_secret_targets(manager.page, payload.step, session.origin)
        with pytest.raises(HTTPException) as exc:
            await browser.execute(session_id, request("action.navigate", {"url": target}))
        assert exc.value.status_code == 422
        await browser.stop()
    asyncio.run(run())


def test_private_trust_candidate_is_never_reported(public_dns):
    async def run():
        browser = AssistantBrowser(Manager())
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        with pytest.raises(HTTPException) as exc:
            await browser.navigate(session_id, TOKEN, "http://127.0.0.1/private?tab=foo")
        assert exc.value.detail == {"code": "browser_dns_denied"}
        await browser.stop()
    asyncio.run(run())


def test_known_credentials_cannot_return_in_any_snapshot_field_or_control_url(public_dns, monkeypatch):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        session = browser.sessions[session_id]
        session.secret_fingerprints = fingerprints(["private-login", "private-password"])
        manager.page.url = URL + "?tab=foo#overview"
        heading_selector = "html:nth-of-type(1) > body:nth-of-type(1) > h1:nth-of-type(1)"
        link_selector = "html:nth-of-type(1) > body:nth-of-type(1) > a:nth-of-type(1)"

        async def snapshot(session, frame, args=None):
            return {"elements": [
                {"node_id": "n0", "parent_id": None, "tag": "h1", "visible": True,
                 "text": "Welcome private-login", "attributes": {"id": "private-password"},
                 "selector": heading_selector, "selector_matches": 1, "selector_source": "dom_path"},
                {"node_id": "n1", "parent_id": None, "tag": "a", "visible": True,
                 "attributes": {"href": URL + "?tab=private%252Dpassword"}, "text": "Login",
                 "selector": link_selector, "selector_matches": 1, "selector_source": "dom_path"},
            ]}

        monkeypatch.setattr(browser, "_isolated_snapshot", snapshot)
        result = await browser.observe(session_id, TOKEN)
        assert result["url"] == manager.page.url and result["url_redacted"] is False
        assert result["redacted_parameters"] == []
        assert len(result["elements"]) == 2
        assert result["elements"][0]["text"] == "[redacted]"
        assert result["elements"][0]["attributes"]["id"] == "[redacted]"
        assert result["elements"][0]["selector"] == heading_selector
        assert result["elements"][1]["selector"] == link_selector
        assert all(node["selector_matches"] == 1 and node["selector_source"] == "dom_path"
                   for node in result["elements"])
        assert result["elements"][1]["attributes"]["href"] == "[redacted]"
        assert result["frames"][0]["url"] == manager.page.url
        assert result["frames"][0]["url_redacted"] is False
        assert result["frames"][0]["redacted_parameters"] == []
        assert "private" not in json.dumps(result)
        manager.page.url = URL + "?tab=private%2Dpassword#overview"
        with pytest.raises(HTTPException) as exc:
            await browser.observe(session_id, TOKEN)
        assert exc.value.status_code == 403 and exc.value.detail == {"code": "browser_url_denied"}
        for url in [URL + "?tab=private%2Dpassword", "https://other.example/?tab=private-password"]:
            with pytest.raises(HTTPException) as exc:
                await browser.navigate(session_id, TOKEN, url)
            assert exc.value.detail == {"code": "browser_url_denied"}
        await browser.stop()
    asyncio.run(run())


@pytest.mark.parametrize("stage", ["route", "redirect"])
def test_known_credentials_suppress_trust_candidates(public_dns, stage):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        session = browser.sessions[session_id]
        session.secret_fingerprints = fingerprints(["known-password"])
        target = "https://other.example/login?tab=known%252Dpassword"
        if stage == "route":
            route = SimpleNamespace(request=SimpleNamespace(
                url=target, method="GET", resource_type="document", frame=manager.page.main_frame,
                is_navigation_request=lambda: True), abort=AsyncMock(), continue_=AsyncMock())
            await browser._route(session, route)
            route.abort.assert_awaited_once()
            assert session.failure == {"code": "browser_url_denied"}
        else:
            await browser._response(session, {
                "requestId": "r", "frameId": "main", "resourceType": "Document",
                "request": {"url": URL, "method": "GET"}, "responseStatusCode": 302,
                "responseHeaders": [{"name": "Location", "value": target}],
            })
            assert session.failure == {"code": "browser_url_denied"}
        await browser.stop()
    asyncio.run(run())


def test_approved_origins_still_require_public_dns(public_dns):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        with pytest.raises(HTTPException) as exc:
            await browser.open(URL, "interaction", TOKEN, trusted_origins=["http://127.0.0.1"])
        assert exc.value.detail == {"code": "browser_dns_denied"}
        manager.lease_lane.assert_not_awaited()
        await browser.stop()
    asyncio.run(run())


def test_execution_cannot_follow_preapproved_cross_origin_requests(public_dns):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        session_id = (await browser.open(URL, "interaction", TOKEN,
                      trusted_origins=["https://other.example"]))["session_id"]
        session = browser.sessions[session_id]
        session.executing = session.credential_execution = True
        session.credential_post_urls = {URL}
        target = "https://other.example/login?tab=foo"
        for resource in ["document", "fetch", "image"]:
            route = SimpleNamespace(request=SimpleNamespace(
                url=target, method="GET", resource_type=resource, frame=manager.page.main_frame,
                is_navigation_request=lambda: resource == "document"), abort=AsyncMock(), continue_=AsyncMock())
            await browser._route(session, route)
            route.abort.assert_awaited_once()
            route.continue_.assert_not_awaited()
        await browser._response(session, {
            "requestId": "r", "frameId": "main", "resourceType": "Document",
            "request": {"url": URL, "method": "POST"}, "responseStatusCode": 303,
            "responseHeaders": [{"name": "Location", "value": target}],
        })
        assert session.failure == {"code": "browser_redirect_denied"}
        await browser.stop()
    asyncio.run(run())


def test_snapshot_url_change_in_query_is_not_hidden_by_projection(public_dns, monkeypatch):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        session_id = (await browser.open(URL + "?token=FIRST", "interaction", TOKEN))["session_id"]

        async def snapshot(session, frame, args=None):
            manager.page.url = URL + "?token=SECOND"
            return {"elements": []}

        monkeypatch.setattr(browser, "_isolated_snapshot", snapshot)
        with pytest.raises(HTTPException) as exc:
            await browser.observe(session_id, TOKEN)
        assert exc.value.status_code == 409
        assert exc.value.detail == {"code": "browser_snapshot_stale"}
        assert not browser.sessions[session_id].snapshots
        await browser.stop()
    asyncio.run(run())


def test_execution_projects_full_urls_and_cached_snapshots_use_new_fingerprints(public_dns, monkeypatch):
    from worker import executor

    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]

        async def execute(page, step, **kwargs):
            await page.goto(step["config"]["url"])
            return "success", "ignored", {}

        monkeypatch.setattr(executor, "_run_step", execute)
        target = "https://example.com/next?tab=private-login&token=PRIVATE#/login?tab=foo"
        payload = request("action.navigate", {"url": target, "retry_count": 0})
        result = await browser.execute(session_id, payload)
        assert result["status"] == "success"
        assert result["after"]["url"] == target.replace("PRIVATE", "[REDACTED]")
        assert result["after"]["url_redacted"] is True
        assert result["after"]["redacted_parameters"] == ["token"]
        assert manager.page.url == target
        browser.sessions[session_id].secret_fingerprints = fingerprints(["private-login"])
        cached = await browser.execute(session_id, payload)
        assert cached["after"]["url"] == "[redacted]"
        assert cached["after"]["url_redacted"] is True
        assert "private-login" not in json.dumps(cached)
        await browser.stop()
    asyncio.run(run())


@pytest.mark.parametrize("stage", ["route", "redirect"])
def test_observation_surfaces_native_trust_failure_once(public_dns, monkeypatch, stage):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        session = browser.sessions[session_id]
        target = "https://other.example/login?tab=foo&token=PRIVATE#/login?tab=bar"
        if stage == "route":
            route = SimpleNamespace(request=SimpleNamespace(
                url=target, method="GET", resource_type="document", frame=manager.page.main_frame,
                is_navigation_request=lambda: True), abort=AsyncMock(), continue_=AsyncMock())
            await browser._route(session, route)
        else:
            await browser._response(session, {
                "requestId": "r", "frameId": "main", "resourceType": "Document",
                "request": {"url": URL, "method": "GET"}, "responseStatusCode": 302,
                "responseHeaders": [{"name": "Location", "value": target}],
            })
        pending = session.failure
        assert pending["code"] == "browser_trust_required"
        snapshot = AsyncMock(return_value={"elements": []})
        monkeypatch.setattr(browser, "_isolated_snapshot", snapshot)
        observed = await browser.observe(session_id, TOKEN)
        assert observed["url"] == URL and observed["snapshot_id"]
        warning = observed["warnings"][0]
        assert warning == {**pending, "source": "blocked_request",
                           "source_origin": "https://example.com", "target_origin": "https://other.example"}
        assert warning["target_url"] == target.replace("PRIVATE", "[REDACTED]")
        assert "PRIVATE" not in json.dumps(observed)
        assert session.failure is None
        again = await browser.observe(session_id, TOKEN)
        assert again["url"] == URL and "warnings" not in again
        assert snapshot.await_count == 2
        await browser.stop()
    asyncio.run(run())


def test_new_navigation_replaces_pending_failure_without_losing_new_failure(public_dns):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        session = browser.sessions[session_id]
        session.failure = {"code": "browser_dns_denied"}
        target = URL + "?tab=foo"
        assert (await browser.navigate(session_id, TOKEN, target))["url"] == target
        assert session.failure is None

        async def goto(url, **kwargs):
            session.failure = {"code": "browser_url_denied"}
            raise RuntimeError("navigation failed")

        manager.page.goto = goto
        with pytest.raises(HTTPException) as exc:
            await browser.navigate(session_id, TOKEN, URL)
        assert exc.value.detail == {"code": "browser_url_denied"}
        observed = await browser.observe(session_id, TOKEN)
        assert observed["url"] == target
        assert observed["warnings"] == [{"code": "browser_url_denied", "source": "blocked_request"}]
        assert session.failure is None
        await browser.stop()
    asyncio.run(run())


def test_trust_warning_reprojects_only_bounded_allowlisted_details(public_dns):
    async def run():
        browser = AssistantBrowser(Manager())
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        session = browser.sessions[session_id]
        session.secret_fingerprints = fingerprints(["new-password"])
        session.failure = {
            "code": "browser_trust_required", "source_url": URL + "?token=RAW-TOKEN",
            "target_url": "https://other.example/login?tab=new%252Dpassword",
            "source_url_redacted": False, "target_url_redacted": False,
            "target_origin": "https://injected.example/", "source_origin": "private-blob",
            "redacted_parameters": ["token", "new-password", "x" * 129, {}, 123] + [f"p{i}" for i in range(50)],
            "credentials": {"password": "new-password"}, "arbitrary": "RAW-TOKEN",
        }
        observed = await browser.observe(session_id, TOKEN)
        warning = observed["warnings"][0]
        assert warning["source_url"] == URL + "?token=[REDACTED]"
        assert warning["source_url_redacted"] is True
        assert warning["source_origin"] == "https://example.com"
        assert warning["target_url"] == "[redacted]" and warning["target_url_redacted"] is True
        assert "target_origin" not in warning
        assert "credentials" not in warning and "arbitrary" not in warning
        assert "known_credential" in warning["redacted_parameters"]
        assert len(warning["redacted_parameters"]) <= 32
        assert all(isinstance(name, str) and len(name) <= 128 for name in warning["redacted_parameters"])
        assert "new-password" not in json.dumps(observed) and "RAW-TOKEN" not in json.dumps(observed)
        for value in ["https://other.example/" + "x" * 4096, {"url": "private-blob"}, 123]:
            session.failure = {"code": "browser_trust_required", "target_url": value,
                               "target_url_redacted": "private-blob", "redacted_parameters": {"private": "blob"}}
            warning = (await browser.observe(session_id, TOKEN))["warnings"][0]
            assert warning == {"code": "browser_trust_required", "source": "blocked_request", "redacted_parameters": []}
        session.failure = {"code": "browser_url_denied", "target_url": URL, "arbitrary": "private-blob"}
        assert (await browser.observe(session_id, TOKEN))["warnings"] == [
            {"code": "browser_url_denied", "source": "blocked_request"}]
        await browser.stop()
    asyncio.run(run())


def test_trust_warning_retains_newer_failure_arriving_during_snapshot(public_dns, monkeypatch):
    async def run():
        browser = AssistantBrowser(Manager())
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        session = browser.sessions[session_id]
        target = "https://other.example/login?tab=foo&token=PRIVATE"
        session.failure = (await browser._navigation_error(session, target)).detail
        newer = {"code": "browser_dns_denied"}

        async def snapshot(*args):
            session.failure = newer
            return {"elements": []}

        monkeypatch.setattr(browser, "_isolated_snapshot", snapshot)
        warning = (await browser.observe(session_id, TOKEN))["warnings"][0]
        assert warning["target_url"] == target.replace("PRIVATE", "[REDACTED]")
        assert warning["target_origin"] == "https://other.example"
        assert session.failure is newer
        monkeypatch.setattr(browser, "_isolated_snapshot", AsyncMock(return_value={"elements": []}))
        assert (await browser.observe(session_id, TOKEN))["warnings"] == [
            {"code": "browser_dns_denied", "source": "blocked_request"}]
        assert session.failure is None
        await browser.stop()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["paginate", "warning_limit", "envelope_limit"])
def test_trust_warning_fits_complete_snapshot_budget(public_dns, monkeypatch, mode):
    from worker import assistant_browser as module

    async def run():
        browser = AssistantBrowser(Manager())
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        session = browser.sessions[session_id]
        nodes = [{"node_id": f"n{i}", "tag": "div", "text": "Public text " * 25,
                  "attributes": {f"data-part-{j}": "x" * 500 for j in range(6)}} for i in range(40)]
        monkeypatch.setattr(browser, "_isolated_snapshot", AsyncMock(return_value={"elements": nodes}))
        target = "https://other.example/" + ("\u1234" * 1500 if mode == "warning_limit" else "x" * 1000)
        if mode == "envelope_limit":
            baseline = await browser.observe(session_id, TOKEN, limit=1)
            monkeypatch.setattr(module, "MAX_SNAPSHOT_BYTES", len(json.dumps(baseline, ensure_ascii=True).encode()) + 250)
        session.failure = (await browser._navigation_error(session, target)).detail
        result = await browser.observe(session_id, TOKEN, limit=1 if mode == "envelope_limit" else 100)
        warning = result["warnings"][0]
        assert len(json.dumps(result, ensure_ascii=True).encode()) <= module.MAX_SNAPSHOT_BYTES
        assert result["elements"] and result["next_offset"] > 0
        if mode == "paginate":
            assert warning["target_url"] == target and warning["target_origin"] == "https://other.example"
            assert len(result["elements"]) < 40
        else:
            assert warning == {"code": "browser_trust_required", "source": "blocked_request",
                               "details_truncated": True, "reason": "browser_warning_details_limit"}
        assert session.failure is None
        await browser.stop()
    asyncio.run(run())


@pytest.mark.parametrize("rounds", [0, 1, 2, 3, MAX_DECODE_ROUNDS])
@pytest.mark.parametrize("stage", ["open", "navigate", "route", "redirect"])
def test_redaction_markers_are_not_navigation_targets(public_dns, caplog, rounds, stage):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        marker = "[ReDaCtEd]"
        for _ in range(rounds):
            marker = quote(marker, safe="")
        target = URL + "?tab=foo&next=" + marker + "#overview"
        # Projection remains idempotent; only control URLs reject placeholders.
        assert project_url(target)["url"] == target
        if stage == "open":
            with pytest.raises(HTTPException) as exc:
                await browser.open(target, "interaction", TOKEN)
            assert exc.value.detail == {"code": "browser_url_denied"}
            manager.context.new_page.assert_not_awaited()
        else:
            session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
            session = browser.sessions[session_id]
            if stage == "navigate":
                with pytest.raises(HTTPException) as exc:
                    await browser.navigate(session_id, TOKEN, target)
                assert exc.value.detail == {"code": "browser_url_denied"}
            elif stage == "route":
                route = SimpleNamespace(request=SimpleNamespace(
                    url=target, method="GET", resource_type="document", frame=manager.page.main_frame,
                    is_navigation_request=lambda: True), abort=AsyncMock(), continue_=AsyncMock())
                await browser._route(session, route)
                route.abort.assert_awaited_once()
                route.continue_.assert_not_awaited()
                assert session.failure == {"code": "browser_url_denied"}
            else:
                await browser._response(session, {
                    "requestId": "r", "frameId": "main", "resourceType": "Document",
                    "request": {"url": URL, "method": "GET"}, "responseStatusCode": 302,
                    "responseHeaders": [{"name": "Location", "value": target}],
                })
                assert manager.cdp.send.call_args.args[0] == "Fetch.failRequest"
                assert session.failure == {"code": "browser_url_denied"}
            assert manager.page.url == URL
        assert target not in caplog.text
        await browser.stop()
    asyncio.run(run())
