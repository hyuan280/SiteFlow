from __future__ import annotations

import asyncio
import json
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException
import pytest
from pydantic import ValidationError

from tests.test_assistant_browser import Manager, TOKEN, URL, public_dns  # noqa: F401
from tests.test_assistant_private_steps import chromium_fixture
from worker.assistant_browser import AssistantBrowser, OpenRequest


@pytest.mark.parametrize("policy", [False, True])
def test_open_policy_is_strict_boolean(policy):
    assert OpenRequest(url=URL, token=TOKEN).same_origin_only is False
    assert OpenRequest(url=URL, token=TOKEN, same_origin_only=policy).same_origin_only is policy


@pytest.mark.parametrize("policy", [0, 1, "true", "false", "1", "0", None, [], {}])
def test_open_policy_rejects_coercion(policy):
    with pytest.raises(ValidationError):
        OpenRequest(url=URL, token=TOKEN, same_origin_only=policy)


@pytest.mark.parametrize("policy", [False, True])
def test_open_retries_match_original_policy_before_and_after_execution(public_dns, policy):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        sid = str(uuid.uuid4())
        entered, finish = asyncio.Event(), asyncio.Event()

        async def lease(_key):
            entered.set()
            await finish.wait()
            return "owned-lease", {}

        manager.lease_lane.side_effect = lease
        opening = asyncio.create_task(browser.open(URL, "interaction", TOKEN, sid,
                                                   same_origin_only=policy))
        await entered.wait()
        try:
            session = browser.sessions[sid]
            assert session.same_origin_only is policy
            for ready in [False, True]:
                if ready:
                    finish.set()
                    view = await opening
                    assert await browser.open(URL, "interaction", TOKEN, sid,
                                              same_origin_only=policy) == view
                    # Execution makes routing sticky-strict, not the original open contract.
                    session.same_origin_only = True
                    assert await browser.open(URL, "interaction", TOKEN, sid,
                                              same_origin_only=policy) == view
                with pytest.raises(HTTPException) as error:
                    await browser.open(URL, "interaction", TOKEN, sid, same_origin_only=not policy)
                assert error.value.status_code == 409
            manager.context.new_page.assert_awaited_once()
        finally:
            finish.set()
            await browser.stop()
            await asyncio.gather(opening, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize("strict", [False, True])
def test_initial_open_routes_resources_before_any_step(public_dns, strict):
    html = '''<h1>Initial page</h1><script>
    const external = 'http://external.example:' + location.port;
    const requests = [];
    for (const [base, prefix] of [[location.origin, 'same'], [external, 'external']]) {
      requests.push(fetch(base + '/' + prefix + '-fetch?token=RESOURCE_SECRET').catch(() => {}));
      for (const kind of ['script', 'image']) {
        requests.push(new Promise(resolve => {
          const el = document.createElement(kind === 'script' ? 'script' : 'img');
          el.onload = el.onerror = resolve;
          el.src = base + '/' + prefix + '-' + kind + '?token=RESOURCE_SECRET';
          document.body.append(el);
        }));
      }
    }
    Promise.all(requests).then(() => { window.initialDone = true; });
    </script>'''

    async def run():
        async with chromium_fixture(html, same_origin_only=strict) as (browser, sid, origin, page, seen):
            await page.wait_for_function("window.initialDone === true")
            session = browser.sessions[sid]
            assert session.same_origin_only is strict
            assert not session.executing and not session.operations
            assert session.approved_origins == {origin}
            paths = [path for _, path, _ in seen]
            for kind in ["script", "fetch", "image"]:
                assert f"/same-{kind}?token=RESOURCE_SECRET" in paths
                assert (f"/external-{kind}?token=RESOURCE_SECRET" in paths) is not strict
            if strict:
                assert len(session.blocked_requests) == 3
                assert {item["code"] for item in session.blocked_requests} == {"browser_url_denied"}
                assert "RESOURCE_SECRET" not in json.dumps(session.blocked_requests)
            else:
                assert not session.blocked_requests
            await browser.navigate(sid, TOKEN, origin + "/next")
            assert page.url == origin + "/next"
    asyncio.run(run())


@pytest.mark.parametrize("resource,child", [("document", False), ("document", True),
                                           ("script", False), ("fetch", False), ("image", False)])
def test_strict_policy_does_not_delegate_to_observation_trust(public_dns, resource, child):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        try:
            sid = (await browser.open(URL, "interaction", TOKEN, same_origin_only=True,
                                      trusted_origins=["https://external.example"]))["session_id"]
            route = SimpleNamespace(request=SimpleNamespace(
                url="https://external.example/private?token=SECRET_URL", method="GET",
                resource_type=resource, frame=object() if child else manager.page.main_frame,
                is_navigation_request=lambda: resource == "document"),
                abort=AsyncMock(), continue_=AsyncMock())
            await manager.page.route.call_args.args[1](route)
            route.abort.assert_awaited_once_with("blockedbyclient")
            route.continue_.assert_not_awaited()
            observed = await browser.observe(sid, TOKEN)
            if resource == "document" and not child:
                assert observed["warnings"] == [{"code": "browser_url_denied", "source": "blocked_request"}]
                assert "warnings" not in await browser.observe(sid, TOKEN)
            assert "SECRET_URL" not in json.dumps(observed)
            assert "external.example" not in json.dumps(observed)
        finally:
            await browser.stop()
    asyncio.run(run())


def test_strict_same_origin_dns_denial_remains_static(public_dns, caplog):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        try:
            sid = (await browser.open(URL, "interaction", TOKEN, same_origin_only=True))["session_id"]
            session = browser.sessions[sid]
            session.dns.cache.clear()
            public_dns.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 0))]
            route = SimpleNamespace(request=SimpleNamespace(
                url=URL + "?token=DNS_SECRET", method="GET", resource_type="document",
                frame=manager.page.main_frame, is_navigation_request=lambda: True),
                abort=AsyncMock(), continue_=AsyncMock())
            await manager.page.route.call_args.args[1](route)
            route.abort.assert_awaited_once()
            route.continue_.assert_not_awaited()
            assert session.failure == {"code": "browser_dns_denied"}
            public_dns.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
            observed = await browser.observe(sid, TOKEN)
            assert observed["warnings"] == [{"code": "browser_dns_denied", "source": "blocked_request"}]
            assert all(value not in json.dumps(observed) + caplog.text
                       for value in ["DNS_SECRET", TOKEN, "10.0.0.1"])
        finally:
            await browser.stop()
    asyncio.run(run())


@pytest.mark.parametrize("resource", ["Script", "Fetch", "Image", "Document"])
def test_strict_redirects_and_untrusted_challenge_resources_fail_closed(public_dns, resource):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        try:
            sid = (await browser.open(URL, "interaction", TOKEN, same_origin_only=True))["session_id"]
            session = browser.sessions[sid]
            for executing in [False, True]:
                session.executing = session.challenge_resources = executing
                route = SimpleNamespace(request=SimpleNamespace(
                    url="https://external.example/resource?token=CHALLENGE_SECRET", method="GET",
                    resource_type=resource.lower(), frame=object(),
                    is_navigation_request=lambda: resource == "Document"),
                    abort=AsyncMock(), continue_=AsyncMock())
                await manager.page.route.call_args.args[1](route)
                route.abort.assert_awaited_once()
                route.continue_.assert_not_awaited()
                await browser._response(session, {
                    "requestId": "redirect", "frameId": "child", "resourceType": resource,
                    "request": {"url": URL, "method": "GET"}, "responseStatusCode": 302,
                    "responseHeaders": [{"name": "Location", "value": route.request.url}],
                })
                assert manager.cdp.send.call_args.args[0] == "Fetch.failRequest"
                assert session.failure == {"code": "browser_redirect_denied"}
                assert "CHALLENGE_SECRET" not in json.dumps(session.blocked_requests)
        finally:
            await browser.stop()
    asyncio.run(run())
