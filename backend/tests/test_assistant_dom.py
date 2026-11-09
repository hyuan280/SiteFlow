from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from tests.test_assistant_browser import Manager, TOKEN, URL, public_dns  # noqa: F401
from worker.app import create_app
from worker.assistant_browser import AssistantBrowser
from worker.assistant_snapshot import LIMITS, MAX_SNAPSHOT_BYTES, SNAPSHOT_SCRIPT, _snapshot, paginate


def test_wire_pagination_fits_and_advances():
    raw = {"elements": [{"node_id": f"n{i}", "parent_id": None, "tag": "custom-widget",
                         "attributes": {f"data-part-{j}": "\u1234" * 512 for j in range(4)},
                         "text": "\u1234" * 300} for i in range(100)]}
    nodes = _snapshot(raw)["elements"]
    offset, seen = 0, []
    while True:
        page = paginate({"snapshot_id": str(uuid.uuid4()), "frames": []}, nodes, offset, 100, False)
        assert len(json.dumps(page, ensure_ascii=True).encode()) <= MAX_SNAPSHOT_BYTES
        seen.extend(node["node_id"] for node in page["elements"])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert seen == [node["node_id"] for node in nodes]


def test_projection_checks_secrets_before_truncation_and_css_escaping():
    from worker.assistant_step import fingerprints

    punctuation = "private:login!"
    long_secret = "long-private-" + "abc!" * 150
    raw = {"elements": [
        {"node_id": "n0", "tag": "div", "text": " " * 200 + long_secret,
         "attributes": {"id": punctuation, "data-part": "prefix " * 20 + long_secret},
         "selector": r"[id=private\:login\!]", "selector_source": "id", "selector_matches": 1},
        {"node_id": "n1", "tag": "meta", "attributes": {"property": "security:token", "content": "META_SECRET"}},
        {"node_id": "n2", "tag": "object", "attributes": {"data": "javascript:OBJECT_SECRET"}},
        {"node_id": "n3", "tag": "div", "text": long_secret + "x" * 4096,
         "attributes": {"data-part": long_secret + "x" * 4096}},
        {"node_id": "n4", "tag": "div", "text": "public " * 100,
         "attributes": {"data-part": "public " * 100}},
    ]}
    nodes = _snapshot(raw, known=fingerprints([punctuation, long_secret]))["elements"]
    assert nodes[0]["text"] == "[redacted]" and nodes[0]["attributes"]["data-part"] == "[redacted]"
    assert nodes[0]["attributes"]["id"] == "[redacted]" and "selector" not in nodes[0]
    assert nodes[1]["attributes"] == {"property": "security:token"}
    assert nodes[2]["attributes"] == {}
    assert "text" not in nodes[3] and nodes[3]["attributes"] == {}
    assert len(nodes[4]["text"]) == 300 and len(nodes[4]["attributes"]["data-part"]) == 512
    assert long_secret[:300] not in json.dumps(nodes) and "META_SECRET" not in json.dumps(nodes)


def test_snapshot_paging_inspect_navigation_and_eviction(public_dns):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        original = manager.cdp.send.side_effect
        loader = "one"
        calls = 0

        async def send(method, params=None):
            nonlocal calls
            if method == "Page.getFrameTree":
                return {"frameTree": {"frame": {"id": "main", "url": URL, "loaderId": loader}}}
            if method == "Runtime.callFunctionOn":
                calls += 1
                return {"result": {"value": {"elements": [
                    {"node_id": f"n{i}", "parent_id": f"n{i-1}" if i else None,
                     "tag": "div", "visible": False, "attributes": {"data-part": str(i)}} for i in range(12)]}}}
            return await original(method, params)

        manager.cdp.send.side_effect = send
        first = await browser.observe(session_id, TOKEN, limit=2)
        assert first["offset"] == 0 and first["next_offset"] == 2 and first["total"] == 12
        assert first["limits"] == LIMITS
        second = await browser.observe(session_id, TOKEN, offset=2, limit=2)
        assert first["snapshot_id"] == second["snapshot_id"] and calls == 1
        assert second["elements"][0]["node_id"] == "n2"
        inspected = await browser.inspect(session_id, TOKEN, first["snapshot_id"], "n5", limit=3)
        assert inspected["total"] == 7 and inspected["elements"][0]["node_id"] == "n5"
        assert inspected["next_offset"] == 3 and calls == 1
        with pytest.raises(HTTPException) as error:
            await browser.inspect(session_id, TOKEN, first["snapshot_id"], "unknown")
        assert error.value.status_code == 409
        loader = "two"  # Reload to exactly the same URL must invalidate too.
        with pytest.raises(HTTPException) as error:
            await browser.inspect(session_id, TOKEN, first["snapshot_id"], "n0")
        assert error.value.detail == {"code": "browser_snapshot_stale"}
        with pytest.raises(HTTPException):
            await browser.observe(session_id, TOKEN, offset=2)
        fresh = await browser.observe(session_id, TOKEN)
        for _ in range(LIMITS["retained_snapshots"]):
            await browser.observe(session_id, TOKEN)
        with pytest.raises(HTTPException):
            await browser.inspect(session_id, TOKEN, fresh["snapshot_id"], "n0")
        session = browser.sessions[session_id]
        assert len(session.snapshots) == LIMITS["retained_snapshots"]
        await browser.close(session_id, TOKEN)
        assert session.snapshots == {}
    asyncio.run(run())


def test_frame_registry_authorization_and_dns_before_read(public_dns):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        session_id = (await browser.open(URL, "interaction", TOKEN,
                                        trusted_origins=["https://approved.example"]))["session_id"]
        original = manager.cdp.send.side_effect

        async def send(method, params=None):
            if method == "Page.getFrameTree":
                return {"frameTree": {"frame": {"id": "main", "url": URL, "loaderId": "one"},
                    "childFrames": [{"frame": {"id": frame_id, "url": url, "loaderId": "child"}} for frame_id, url in [
                        ("approved", "https://approved.example/path?token=FRAME_SECRET"),
                        ("untrusted", "https://untrusted.example/path"),
                        ("private", "http://127.0.0.1/private"),
                        ("opaque", "about:blank"),
                    ]]}}
            return await original(method, params)

        manager.cdp.send.side_effect = send
        result = await browser.observe(session_id, TOKEN)
        frames = {frame["frame_id"]: frame for frame in result["frames"]}
        assert frames["approved"]["readable"] is True
        assert "FRAME_SECRET" not in json.dumps(result)
        assert frames["untrusted"]["reason"] == "browser_trust_required"
        assert frames["private"]["reason"] == "browser_dns_denied"
        assert frames["opaque"]["reason"] == "browser_url_denied"
        for frame_id, status in [("untrusted", 403), ("private", 403), ("opaque", 403), ("invented", 404)]:
            with pytest.raises(HTTPException) as error:
                await browser.observe(session_id, TOKEN, frame_id=frame_id)
            assert error.value.status_code == status
            if status == 403:
                assert error.value.detail == {"code": "browser_frame_unreadable"}
                with pytest.raises(HTTPException) as error:
                    await browser.check_selector(session_id, TOKEN, "button", frame_id)
                assert error.value.status_code == 403
                assert error.value.detail == {"code": "browser_frame_unreadable"}
        await browser.observe(session_id, TOKEN, frame_id="approved")
        worlds = [call.args[1]["frameId"] for call in manager.cdp.send.call_args_list
                  if call.args[0] == "Page.createIsolatedWorld"]
        assert "approved" in worlds and not set(worlds) & {"untrusted", "private", "opaque", "invented"}
        manager.page.evaluate.assert_not_awaited()
        await browser.stop()
    asyncio.run(run())


def test_generic_snapshots_redact_known_credentials_and_detect_racing_navigation(public_dns):
    from worker.assistant_step import fingerprints

    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        session = browser.sessions[session_id]
        session.secret_fingerprints = fingerprints(["private-login"])
        original = manager.cdp.send.side_effect
        racing = False

        async def send(method, params=None):
            if method == "Runtime.callFunctionOn":
                if racing:
                    manager.page.url = URL + "?token=SECOND"
                return {"result": {"value": {"elements": [{
                    "node_id": "n0", "parent_id": None, "tag": "h1", "visible": True,
                    "attributes": {"id": "private-login"}, "text": "Welcome private-login",
                    "selector": "#private-login", "selector_matches": 1, "selector_source": "id",
                }]}}}
            return await original(method, params)

        manager.cdp.send.side_effect = send
        result = await browser.observe(session_id, TOKEN)
        node = result["elements"][0]
        assert node["text"] == "[redacted]" and node["attributes"]["id"] == "[redacted]"
        assert "selector" not in node and "selector_matches" not in node
        assert "private-login" not in json.dumps(result)
        assert "private-login" not in json.dumps(session.snapshots)
        racing = True
        with pytest.raises(HTTPException) as error:
            await browser.observe(session_id, TOKEN)
        assert error.value.status_code == 409 and error.value.detail == {"code": "browser_snapshot_stale"}
        await browser.stop()
    asyncio.run(run())


def test_dom_routes_strict_requests_and_static_errors():
    async def run():
        app = create_app()
        browser = AsyncMock()
        browser.observe.return_value = {"elements": []}
        browser.inspect.return_value = {"elements": []}
        browser.check_selector.return_value = {"match_count": 0}
        app.state.assistant_browser = browser
        snapshot_id = str(uuid.uuid4())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
            base = "/assistant-browser/session"
            assert (await client.post(base + "/observe", json={"token": TOKEN})).status_code == 200
            browser.observe.assert_awaited_with("session", TOKEN, 0, 100, None)
            assert (await client.post(base + "/inspect", json={"token": TOKEN, "snapshot_id": snapshot_id,
                                                              "node_id": "n0"})).status_code == 200
            browser.inspect.assert_awaited_with("session", TOKEN, snapshot_id, "n0", 0, 100)
            assert (await client.post(base + "/check-selector", json={"token": TOKEN,
                                                                     "selector": "button"})).status_code == 200
            browser.check_selector.assert_awaited_with("session", TOKEN, "button", None)
            for endpoint, extra in [("observe", {"offset": -1}), ("observe", {"limit": 101}),
                                    ("observe", {"limit": True}), ("observe", {"offset": "1"}),
                                    ("observe", {"javascript": "secret"}),
                                    ("check-selector", {"selector": "a" * 2049}),
                                    ("inspect", {"snapshot_id": "bad", "node_id": "n0"})]:
                response = await client.post(base + "/" + endpoint, json={"token": TOKEN, **extra})
                assert response.status_code == 422
                assert TOKEN not in response.text and "secret" not in response.text
    asyncio.run(run())


def test_real_chromium_generic_dom_shadow_frames_and_no_effects(public_dns):
    async def run():
        from playwright.async_api import async_playwright

        candidates = list(Path("/opt/opencode/.cache/ms-playwright").glob("chromium_headless_shell-*/chrome-linux/headless_shell"))
        candidates += list(Path("/opt/opencode/.cache/ms-playwright").glob("chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"))
        async with async_playwright() as playwright:
            try:
                chromium = await playwright.chromium.launch(
                    headless=True, executable_path=str(candidates[0]) if candidates else None,
                    args=["--host-resolver-rules=MAP *.example 127.0.0.1", "--no-proxy-server"],
                )
            except Exception:
                pytest.skip("Local Chromium executable unavailable")
            browser = None
            server = None
            try:
                context = await chromium.new_context(service_workers="block")
                seen = []
                html = b'''<!doctype html><html><head><style>.gone{display:none}</style>
<meta name="csrf-token" content="META_SECRET"><meta property="security:nonce" content="META_SECRET">
<meta name="description" content="Public description"></head><body>
<object type="application/x-siteflow-test" data="/frame?token=URL_SECRET"></object>
<object type="application/x-siteflow-test" data="//example.com/frame?token=URL_SECRET"></object>
<object type="application/x-siteflow-test" data="data:text/html,OBJECT_SECRET"></object>
<object type="application/x-siteflow-test" data="javascript:OBJECT_SECRET"></object>
<form action="/login?token=URL_SECRET" method="post" data-purpose="login" aria-label="Login form">
<label>Username<input name='user name' required value="FIELD_SECRET"></label>
<input type="password" value="FIELD_SECRET"><input type="hidden" name="csrf" value="FIELD_SECRET">
<textarea>FIELD_SECRET</textarea><select><option selected value="FIELD_SECRET">FIELD_SECRET</option></select>
<button onclick="window.effects++">Log in</button><button disabled>Disabled</button></form>
<div hidden data-kind="hidden">Hidden helper</div><section class="gone">Hidden section</section>
<div contenteditable="true">EDIT_SECRET<span>EDIT_SECRET</span><input value="FIELD_SECRET"></div>
<script data-secret="ATTR_SECRET" nonce="ATTR_SECRET">window.effects=0;window.s= 'SCRIPT_SECRET';
Document.prototype.createTreeWalker = () => {throw new Error('patched')};
window.getComputedStyle = () => {throw new Error('patched')};
customElements.define('login-panel', class extends HTMLElement {connectedCallback() {
this.attachShadow({mode:'open'}).innerHTML='<div><button>Shadow login</button><input value="FIELD_SECRET"></div>';
}});</script><login-panel data-mode="login" data-auth-token="ATTR_SECRET"></login-panel>
<template><p>TEMPLATE_SECRET</p></template><noscript>NOSCRIPT_SECRET</noscript>
<div id='1 odd &quot; \\ >>> id' class='any class: []' data-part="layout"
 data-url="/login?token=URL_SECRET" data-target="https://example.com/?token=URL_SECRET">Odd ID</div>
<div id="duplicate">First</div><div id="duplicate">Second</div>
<svg viewBox="0 0 10 10"><path d="M0 0L1 1"></path></svg>
<iframe hidden src="/frame?token=URL_SECRET"></iframe>
<iframe hidden src="http://untrusted.example/frame?token=URL_SECRET"></iframe>
<iframe src="http://approved.example:PORT/frame"></iframe>
<main>''' + b"<p data-index='part'>Item</p>" * 140 + b"</main></body></html>"

                async def serve(reader, writer):
                    try:
                        request = await reader.readuntil(b"\r\n\r\n")
                        path = request.split(b" ")[1]
                        seen.append(path)
                        body = b"<article><button>Frame login</button><input value='FIELD_SECRET'></article>" if path.startswith(b"/frame") else html.replace(b"PORT", str(port).encode())
                        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
                        await writer.drain()
                    finally:
                        writer.close()
                        await writer.wait_closed()

                server = await asyncio.start_server(serve, "127.0.0.1", 0)
                port = server.sockets[0].getsockname()[1]
                manager = Manager()
                manager.lane.context = context
                browser = AssistantBrowser(manager)
                session_id = (await browser.open(f"http://fixture.example:{port}/", "interaction", TOKEN,
                    trusted_origins=[f"http://approved.example:{port}"]))["session_id"]
                page = browser.sessions[session_id].page
                await page.wait_for_function("document.querySelector('login-panel').shadowRoot && frames.length === 3")
                await page.wait_for_timeout(100)
                from worker.assistant_step import fingerprints

                punct_secret = 'user :"\\[private]>!'
                long_secret = "long-private-" + "x7!" * 170
                browser.sessions[session_id].secret_fingerprints = fingerprints([punct_secret, long_secret])
                await page.evaluate("""({punct, long}) => {
                    const section = document.createElement('section');
                    section.hidden = true;
                    for (const [kind, secret] of [['punct',punct],['long',long]]) {
                        const div = document.createElement('div');
                        div.id = secret; div.setAttribute('name',secret);
                        div.setAttribute('data-kind',kind);
                        div.setAttribute('data-reflection','prefix '.repeat(20) + secret);
                        div.textContent = 'prefix '.repeat(20) + secret;
                        section.append(div);
                    }
                    const oversized = document.createElement('div');
                    oversized.setAttribute('data-kind','oversized');
                    oversized.setAttribute('data-reflection','prefix ' + long + 'z'.repeat(5000));
                    oversized.textContent = 'prefix ' + long + 'z'.repeat(5000);
                    section.append(oversized);
                    const input = document.createElement('input');
                    input.id = 'retained-credential'; input.value = long;
                    section.append(input);
                    document.body.append(section);
                }""", {"punct": punct_secret, "long": long_secret})
                first = await browser.observe(session_id, TOKEN)
                nodes = list(first["elements"])
                next_offset = first["next_offset"]
                while next_offset is not None:
                    part = await browser.observe(session_id, TOKEN, offset=next_offset)
                    assert part["snapshot_id"] == first["snapshot_id"]
                    assert len(json.dumps(part, ensure_ascii=True).encode()) <= MAX_SNAPSHOT_BYTES
                    nodes.extend(part["elements"])
                    next_offset = part["next_offset"]
                assert len(nodes) == first["total"] and len(nodes) > 100 and not first["truncated"]
                encoded = json.dumps({"nodes": nodes, "frames": first["frames"]})
                for secret in ["FIELD_SECRET", "EDIT_SECRET", "SCRIPT_SECRET", "TEMPLATE_SECRET", "NOSCRIPT_SECRET", "ATTR_SECRET", "URL_SECRET", "META_SECRET", "OBJECT_SECRET", punct_secret, long_secret, long_secret[:300]]:
                    assert secret not in encoded
                metas = [node for node in nodes if node["tag"] == "meta"]
                assert len(metas) == 3
                assert metas[0]["attributes"]["name"] == "csrf-token" and "content" not in metas[0]["attributes"]
                assert metas[1]["attributes"]["property"] == "security:nonce" and "content" not in metas[1]["attributes"]
                assert metas[2]["attributes"]["content"] == "Public description"
                objects = [node for node in nodes if node["tag"] == "object"]
                assert len(objects) == 4
                assert objects[0]["attributes"]["data"].endswith("/frame?token=[REDACTED]")
                assert objects[1]["attributes"]["data"] == "http://example.com/frame?token=[REDACTED]"
                assert all("data" not in node["attributes"] for node in objects[2:])
                for kind in ("punct", "long"):
                    reflection = next(node for node in nodes if node["attributes"].get("data-kind") == kind)
                    assert reflection["attributes"]["id"] == "[redacted]"
                    assert reflection["attributes"]["name"] == "[redacted]"
                    assert reflection["attributes"]["data-reflection"] == "[redacted]"
                    assert reflection["text"] == "[redacted]"
                    assert reflection["selector_source"] == "dom_path"
                    checked = await browser.check_selector(session_id, TOKEN, reflection["selector"])
                    assert checked["match_count"] == 1 and checked["matches"][0]["text"] == "[redacted]"
                oversized = next(node for node in nodes if node["attributes"].get("data-kind") == "oversized")
                assert "data-reflection" not in oversized["attributes"] and "text" not in oversized
                assert {"form", "html", "body", "input", "textarea", "select", "option", "script", "style", "template", "noscript", "login-panel", "iframe", "svg", "path"} <= {node["tag"] for node in nodes}
                assert any(node["attributes"].get("type") == "hidden" and node["visible"] is False for node in nodes)
                assert any(node.get("text") == "Hidden helper" and node["visible"] is False for node in nodes)
                assert any(node.get("text") == "Hidden section" and node["visible"] is False for node in nodes)
                form = next(node for node in nodes if node["tag"] == "form")
                assert form["attributes"]["action"].endswith("/login?token=[REDACTED]")
                assert form["attributes"]["data-purpose"] == "login" and form["attributes"]["aria-label"] == "Login form"
                host = next(node for node in nodes if node["tag"] == "login-panel")
                assert host["has_shadow_root"] is True
                shadow = next(node for node in nodes if node.get("text") == "Shadow login")
                assert " >>> " in shadow["selector"]
                login = next(node for node in nodes if node.get("text") == "Log in")
                assert login["selector_source"] == "dom_path" and login["selector_matches"] == 1
                # Every emitted selector is proven unique against the live frame.
                for node in nodes:
                    if node.get("selector"):
                        checked = await browser.check_selector(session_id, TOKEN, node["selector"])
                        assert checked["match_count"] == 1
                hidden = await browser.check_selector(session_id, TOKEN, "input[type=hidden]")
                assert hidden["match_count"] == 1 and hidden["visible_count"] == 0
                assert hidden["matches"][0]["visible"] is False
                paragraphs = await browser.check_selector(session_id, TOKEN, "main > p")
                assert paragraphs["match_count"] == 140 and len(paragraphs["matches"]) == 10
                assert paragraphs["truncated"] is True
                assert (await browser.check_selector(session_id, TOKEN, "nothing-here"))["match_count"] == 0
                with pytest.raises(HTTPException) as error:
                    await browser.check_selector(session_id, TOKEN, "button[")
                assert error.value.status_code == 422 and error.value.detail == {"code": "browser_selector_invalid"}
                with pytest.raises(HTTPException) as error:
                    await browser.check_selector(session_id, TOKEN, "nothing-here >>> button[")
                assert error.value.status_code == 422
                subtree = await browser.inspect(session_id, TOKEN, first["snapshot_id"], form["node_id"])
                assert subtree["elements"][0] == form and subtree["total"] < first["total"]
                assert len([node for node in nodes if node["tag"] == "iframe"]) == 3
                frames = first["frames"]
                assert any(not frame["readable"] and frame["reason"] == "browser_trust_required" for frame in frames)
                child = next(frame for frame in frames if "approved.example" in frame["url"])
                assert child["readable"]
                child_snapshot = await browser.observe(session_id, TOKEN, frame_id=child["frame_id"])
                assert "Frame login" in json.dumps(child_snapshot["elements"])
                assert (await browser.check_selector(session_id, TOKEN, "button", child["frame_id"]))["match_count"] == 1
                # DOM reads did not submit, click, clear values, or rewrite markup.
                assert await page.evaluate("window.effects") == 0
                assert await page.locator("input[name='user name']").input_value() == "FIELD_SECRET"
                assert await page.locator("textarea").input_value() == "FIELD_SECRET"
                assert await page.locator("#retained-credential").input_value() == long_secret
                assert not any(path.startswith(b"/login") for path in seen)
                # Inspect must never reinterpret a node ID as a newer live node.
                await page.locator("form > button").first.evaluate("el => el.remove()")
                captured = await browser.inspect(session_id, TOKEN, first["snapshot_id"], login["node_id"])
                assert captured["elements"][0] == login
                await page.reload(wait_until="domcontentloaded")
                with pytest.raises(HTTPException) as error:
                    await browser.inspect(session_id, TOKEN, first["snapshot_id"], form["node_id"])
                assert error.value.status_code == 409
                await page.evaluate("""() => {
                    const main = document.querySelector('main');
                    main.innerHTML = '<p>Many nodes</p>'.repeat(10020);
                }""")
                bounded = await browser.observe(session_id, TOKEN)
                assert bounded["truncated"] and bounded["total"] <= LIMITS["scan_nodes"]
                capped = await browser.check_selector(session_id, TOKEN, "main > p")
                assert capped["match_count"] == 10000 and capped["visible_count"] == 10000
                assert len(capped["matches"]) == 10 and capped["truncated"]
            finally:
                if browser:
                    await browser.stop()
                if server:
                    server.close()
                    await server.wait_closed()
                await chromium.close()
    asyncio.run(run())
