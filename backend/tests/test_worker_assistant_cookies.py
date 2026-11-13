from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from tests.test_assistant_browser import Manager, Page, TOKEN, public_dns  # noqa: F401
from tests.test_assistant_browser_queue import until
from worker.app import create_app
from worker import assistant_browser as module
from worker.assistant_browser import AssistantBrowser, OpenRequest
from worker.assistant_step import fingerprints

URL = "https://login.example.com/start"
SECRET = "private-cookie-value-938471"


def cookie(**changes):
    return {"name": "sid", "value": SECRET, "domain": "login.example.com", "path": "/", **changes}


def setup():
    manager = Manager()
    manager.context.add_cookies = AsyncMock()
    manager.context.clear_cookies = AsyncMock()
    return manager, AssistantBrowser(manager)


async def generation(browser, url=URL, lane="interaction"):
    return (await browser.cookie_generation(url, lane))["generation"]


def test_import_before_first_goto_private_and_idempotent(public_dns):
    async def run():
        manager, browser = setup()
        rows = [cookie(httpOnly=True, secure=True, sameSite="Lax", expires=-1),
                {"name": "other", "value": "second-private-value", "url": "https://login.example.com/path"}]
        sid, seed = str(uuid.uuid4()), await generation(browser)
        events = []

        async def add(values):
            events.append("add")
            assert values == rows
            assert fingerprints([SECRET]) <= browser.sessions[sid].cookie_fingerprints

        async def goto(url, **kwargs):
            events.append("goto")
            assert events == ["add", "goto"]
            manager.page.url = url

        manager.context.add_cookies.side_effect = add
        manager.page.goto = AsyncMock(side_effect=goto)
        kwargs = {"cookies": rows, "cookie_generation": seed}
        view = await browser.open(URL, "interaction", TOKEN, sid, **kwargs)
        assert SECRET not in json.dumps(view)
        session = browser.sessions[sid]
        assert session.cookies is None and session.cookie_payload_hash
        assert SECRET not in repr(session)
        original_send = manager.cdp.send.side_effect

        async def send(method, params=None):
            if method == "Runtime.callFunctionOn":
                return {"result": {"value": {"elements": [
                    {"node_id": "n0", "tag": "div", "text": "Visible " + SECRET},
                ]}}}
            return await original_send(method, params)

        manager.cdp.send.side_effect = send
        observation = await browser.observe(sid, TOKEN)
        assert SECRET not in json.dumps(observation)
        assert observation["elements"]
        assert await browser.open(URL, "interaction", TOKEN, sid, **kwargs) == view
        manager.context.add_cookies.assert_awaited_once()
        manager.page.goto.assert_awaited_once()
        for changed in ({"cookies": [cookie(value="different")], "cookie_generation": seed},
                        {"cookies": rows, "cookie_generation": str(uuid.uuid4())}, {}):
            with pytest.raises(HTTPException) as error:
                await browser.open(URL, "interaction", TOKEN, sid, **changed)
            assert error.value.status_code == 409
        request = OpenRequest(url=URL, token=TOKEN, **kwargs)
        assert SECRET not in repr(request)
        await browser.stop()
    asyncio.run(run())


@pytest.mark.parametrize("row", [
    cookie(domain="example.com"), cookie(domain=".example.com"),
    cookie(domain="sibling.example.com"), cookie(domain="child.login.example.com"),
    cookie(domain="login.example.com.evil"), cookie(domain="..login.example.com"),
    cookie(domain="LOGIN.example.com"), cookie(domain="login.example.com."),
    {"name": "sid", "value": SECRET, "url": "https://example.com/"},
    {"name": "sid", "value": SECRET, "url": "http://login.example.com/"},
    {"name": "sid", "value": SECRET, "url": "https://login.example.com:444/"},
    {"name": "sid", "value": SECRET, "url": "https://user:pass@login.example.com/"},
    cookie(url="https://login.example.com/"), cookie(path="relative"),
    cookie(domain=None), cookie(value=123), cookie(name="invalid name"),
    cookie(value="secret\nvalue"), cookie(secure=1), cookie(httpOnly="true"),
    cookie(sameSite="lax"), cookie(expires=True), cookie(expires=float("nan")),
    cookie(expires=float("inf")), cookie(expires=-2), cookie(partitionKey="unsupported"),
    {"value": SECRET}, cookie(value="\ud800"),
])
def test_unsafe_cookie_rows_fail_closed(public_dns, caplog, row):
    async def run():
        manager, browser = setup()
        with pytest.raises(HTTPException) as error:
            await browser.open(URL, "interaction", TOKEN, cookies=[row], cookie_generation=str(uuid.uuid4()))
        assert error.value.status_code == 422
        assert SECRET not in str(error.value) + caplog.text
        assert not browser.sessions
        manager.context.add_cookies.assert_not_awaited()
        manager.context.new_page.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("seed", [None, 123, "123", True, str(uuid.uuid1())])
def test_cookie_snapshot_requires_uuid4_generation(public_dns, seed):
    async def run():
        manager, browser = setup()
        with pytest.raises(HTTPException):
            await browser.open(URL, "interaction", TOKEN, cookies=[cookie()], cookie_generation=seed)
        manager.lease_lane.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("rows", [[cookie()] * 301, [cookie(value="x" * (256 * 1024))]])
def test_cookie_transport_bounds(rows):
    with pytest.raises(ValidationError) as error:
        OpenRequest(url=URL, token=TOKEN, cookies=rows, cookie_generation=str(uuid.uuid4()))
    assert SECRET not in str(error.value)


def test_import_error_static_and_releases_admission(public_dns, caplog):
    async def run():
        manager, browser = setup()
        manager.context.add_cookies.side_effect = RuntimeError(SECRET)
        manager.page.goto = AsyncMock()
        with pytest.raises(HTTPException) as error:
            await browser.open(URL, "interaction", TOKEN, cookies=[cookie()], cookie_generation=await generation(browser))
        assert error.value.detail == {"code": "browser_cookie_import_failed"}
        assert SECRET not in str(error.value) + caplog.text
        manager.page.goto.assert_not_awaited()
        assert not browser.sessions and manager.lane.execution_semaphore.active == 0
        manager.release_lane.assert_awaited_once()
    asyncio.run(run())


def test_cancelled_import_retains_mutation_ownership_and_admission(public_dns):
    async def run():
        manager, browser = setup()
        entered, finish = asyncio.Event(), asyncio.Event()
        sid = str(uuid.uuid4())

        async def add(rows):
            entered.set()
            await finish.wait()

        manager.context.add_cookies.side_effect = add
        manager.page.goto = AsyncMock()
        opening = asyncio.create_task(browser.open(URL, "interaction", TOKEN, sid,
                                                  cookies=[cookie()], cookie_generation=await generation(browser)))
        await entered.wait()
        opening.cancel()
        await until(lambda: browser.sessions[sid].closed)
        assert browser.sessions[sid].provisioning
        assert manager.lane.execution_semaphore.active == 1
        manager.release_lane.assert_not_awaited()
        assert not opening.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await opening
        manager.page.goto.assert_not_awaited()
        manager.release_lane.assert_awaited_once()
        assert not browser.sessions and manager.lane.execution_semaphore.active == 0
    asyncio.run(run())


def test_scoped_clear_initial_host_all_paths_preserves_other_state(public_dns):
    async def run():
        manager, browser = setup()
        rows = [cookie(), cookie(domain=".login.example.com", path="/account"),
                cookie(domain="example.com"), cookie(domain=".example.com"),
                cookie(domain="sibling.example.com"), cookie(domain="child.login.example.com"),
                cookie(domain="loginXexample.com"), cookie(domain="login.example.com.evil")]
        manager.context.saved_passwords = ["password"]
        manager.context.storage = {"local": "keep", "session": "keep"}
        view = await browser.open(URL, "interaction", TOKEN, cookies=[cookie()], cookie_generation=await generation(browser),
                                  trusted_origins=["https://sibling.example.com"])
        session = browser.sessions[view["session_id"]]
        manager.page.url = "https://sibling.example.com/current"
        session.snapshots["old"] = {"secret": SECRET}
        session.latest_snapshots["main"] = "old"
        session.document_versions["main"] = ("loader", URL)
        known = set(session.cookie_fingerprints)

        async def clear(**kwargs):
            assert set(kwargs) == {"domain"}
            pattern = kwargs["domain"]
            assert isinstance(pattern, re.Pattern)
            rows[:] = [row for row in rows if not pattern.search(row["domain"])]

        manager.context.clear_cookies.side_effect = clear
        operation = str(uuid.uuid4())
        result = await browser.clear_cookies(session.session_id, TOKEN, operation)
        assert result == {"session_id": session.session_id, "lane_key": "interaction", "cleared": True}
        assert await browser.clear_cookies(session.session_id, TOKEN, operation) == result
        manager.context.clear_cookies.assert_awaited_once()
        assert len(rows) == 6
        assert all(row["domain"] not in {"login.example.com", ".login.example.com"} for row in rows)
        assert manager.context.saved_passwords == ["password"]
        assert manager.context.storage == {"local": "keep", "session": "keep"}
        assert not session.snapshots and not session.latest_snapshots and not session.document_versions
        assert session.cookie_fingerprints == known
        assert manager.page.url == "https://sibling.example.com/current"
        manager.page.evaluate.assert_not_awaited()
        manager.page.close.assert_not_awaited()
        manager.home.close.assert_not_awaited()
        manager.other.close.assert_not_awaited()
        await browser.stop()
    asyncio.run(run())


def test_clear_invalidates_queued_and_late_old_snapshots_but_allows_fresh(public_dns):
    async def run():
        manager, browser = setup()
        human = await browser.open(URL, "interaction", TOKEN)
        seed = await generation(browser)
        kwargs = {"cookies": [cookie()], "cookie_generation": seed, "wait_for_lane": True}
        queued = await browser.open(URL, "interaction", TOKEN, **kwargs)
        waiting = browser.sessions[queued["session_id"]]
        waiting.snapshots["old"] = {}
        await browser.clear_cookies(human["session_id"], TOKEN, str(uuid.uuid4()))
        assert waiting.cookies is None and not waiting.snapshots
        assert not waiting.cookie_fingerprints  # The stale payload was never imported.
        assert (await browser.open(URL, "interaction", TOKEN, queued["session_id"], **kwargs))["status"] == "queued"
        # This request was not even registered when the clear occurred.
        late = await browser.open(URL, "interaction", TOKEN, **kwargs)
        fresh = await browser.open(URL, "interaction", TOKEN, cookies=[cookie(value="fresh-private")],
                                   cookie_generation=await generation(browser), wait_for_lane=True)
        for previous, following in [(human, queued), (queued, late), (late, fresh)]:
            manager.page = Page()
            manager.context.new_page.return_value = manager.page
            await browser.close(previous["session_id"], TOKEN)
            await until(lambda: browser.sessions[following["session_id"]].ready)
        manager.context.add_cookies.assert_awaited_once_with([cookie(value="fresh-private")])
        await browser.stop()
    asyncio.run(run())


def test_clear_failure_is_cached_static_no_retry_and_invalidates(public_dns, caplog):
    async def run():
        manager, browser = setup()
        view = await browser.open(URL, "interaction", TOKEN)
        sid, operation = view["session_id"], str(uuid.uuid4())
        pending = await browser.open(URL, "interaction", TOKEN, cookies=[cookie()],
                                     cookie_generation=await generation(browser), wait_for_lane=True)
        old_generation = browser.sessions[pending["session_id"]].cookie_generation
        manager.context.clear_cookies.side_effect = RuntimeError(SECRET)
        for _ in range(2):
            browser.sessions[sid].snapshots["old"] = {}
            with pytest.raises(HTTPException) as error:
                await browser.clear_cookies(sid, TOKEN, operation)
            assert error.value.detail == {"code": "browser_cookie_clear_uncertain"}
            assert SECRET not in str(error.value) + caplog.text
        manager.context.clear_cookies.assert_awaited_once()
        assert browser.sessions[pending["session_id"]].cookies is None
        assert await generation(browser) != old_generation
        await browser.stop()
    asyncio.run(run())


def test_clear_token_ready_validation_and_cancelled_operation_ownership(public_dns):
    async def run():
        manager, browser = setup()
        view = await browser.open(URL, "interaction", TOKEN)
        sid, operation = view["session_id"], str(uuid.uuid4())
        with pytest.raises(HTTPException) as error:
            await browser.clear_cookies(sid, "wrong", operation)
        assert error.value.status_code == 404
        pending = await browser.open(URL, "interaction", TOKEN, wait_for_lane=True)
        with pytest.raises(HTTPException) as error:
            await browser.clear_cookies(pending["session_id"], TOKEN, operation)
        assert error.value.status_code == 409
        entered, finish = asyncio.Event(), asyncio.Event()

        async def clear(**kwargs):
            entered.set()
            await finish.wait()

        manager.context.clear_cookies.side_effect = clear
        clearing = asyncio.create_task(browser.clear_cookies(sid, TOKEN, operation))
        await entered.wait()
        clearing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await clearing
        retry = asyncio.create_task(browser.clear_cookies(sid, TOKEN, operation))
        await asyncio.sleep(0)
        assert not retry.done()
        finish.set()
        assert (await retry)["cleared"] is True
        manager.context.clear_cookies.assert_awaited_once()
        await browser.stop()
    asyncio.run(run())


def test_import_and_clear_serialize_by_lane_host(public_dns):
    async def run():
        manager, browser = setup()
        manager.lane.execution_semaphore.resize(2)
        first = await browser.open(URL, "interaction", TOKEN)
        entered, finish = asyncio.Event(), asyncio.Event()
        events = []

        async def add(rows):
            events.append("import-start")
            entered.set()
            await finish.wait()
            events.append("import-end")

        async def clear(**kwargs):
            events.append("clear")

        manager.context.add_cookies.side_effect = add
        manager.context.clear_cookies.side_effect = clear
        manager.page = Page()
        manager.context.new_page.return_value = manager.page
        importing = asyncio.create_task(browser.open(URL, "interaction", TOKEN,
                                                     cookies=[cookie()], cookie_generation=await generation(browser)))
        await entered.wait()
        clearing = asyncio.create_task(browser.clear_cookies(first["session_id"], TOKEN, str(uuid.uuid4())))
        await asyncio.sleep(0.01)
        manager.context.clear_cookies.assert_not_awaited()
        finish.set()
        await asyncio.gather(importing, clearing)
        assert events == ["import-start", "import-end", "clear"]
        await browser.stop()
    asyncio.run(run())


def test_generation_lane_host_isolation_and_unknown_generations(public_dns):
    async def run():
        manager, browser = setup()
        view = await browser.open(URL, "interaction", TOKEN)
        old = await generation(browser)
        proxy = await generation(browser, lane="proxy_interaction")
        sibling = await generation(browser, "https://sibling.example.com/start")
        await browser.clear_cookies(view["session_id"], TOKEN, str(uuid.uuid4()))
        await browser.close(view["session_id"], TOKEN)
        for seed in (old, str(uuid.uuid4()), proxy, sibling):
            stale = await browser.open(URL, "interaction", TOKEN, cookies=[cookie()], cookie_generation=seed)
            manager.context.add_cookies.assert_not_awaited()
            await browser.close(stale["session_id"], TOKEN)
        for url, lane, rows, seed in [
            (URL, "proxy_interaction", [cookie()], proxy),
            ("https://sibling.example.com/start", "interaction", [cookie(domain="sibling.example.com")], sibling),
        ]:
            current = await browser.open(url, lane, TOKEN, cookies=rows, cookie_generation=seed)
            await browser.close(current["session_id"], TOKEN)
        assert manager.context.add_cookies.await_count == 2
        await browser.stop()
    asyncio.run(run())


def test_shared_cookie_protection_survives_close_clear_and_new_unseeded_sessions(public_dns):
    async def run():
        manager, browser = setup()
        manager.lane.execution_semaphore.resize(2)
        first = await browser.open(URL, "interaction", TOKEN)
        first_session = browser.sessions[first["session_id"]]
        # Password fingerprints remain local, unlike imported cookie fingerprints.
        first_session.secret_fingerprints.update(fingerprints(["local-password-938471"]))
        seeded = await browser.open(URL, "interaction", TOKEN, cookies=[cookie()],
                                    cookie_generation=await generation(browser))
        seeded_session = browser.sessions[seeded["session_id"]]
        assert first_session.cookie_fingerprints is seeded_session.cookie_fingerprints
        assert fingerprints([SECRET]) <= first_session.known_fingerprints
        assert not seeded_session.secret_fingerprints
        # Clear must retain protection for old DOM/snapshots.
        await browser.clear_cookies(seeded["session_id"], TOKEN, str(uuid.uuid4()))
        await browser.close(seeded["session_id"], TOKEN)
        await browser.close(first["session_id"], TOKEN)
        unseeded = await browser.open(URL, "interaction", TOKEN)
        session = browser.sessions[unseeded["session_id"]]
        assert session.cookie_fingerprints is first_session.cookie_fingerprints
        assert not session.secret_fingerprints
        original = manager.cdp.send.side_effect

        async def send(method, params=None):
            if method == "Runtime.callFunctionOn":
                return {"result": {"value": {"elements": [
                    {"node_id": "n0", "tag": "div", "text": "Old cookie: " + SECRET},
                ]}}}
            return await original(method, params)

        manager.cdp.send.side_effect = send
        assert SECRET not in json.dumps(await browser.observe(session.session_id, TOKEN))
        manager.page.url = URL + "/" + SECRET
        assert SECRET not in json.dumps(session.view())
        with pytest.raises(HTTPException):
            await browser._current(session)
        await browser.stop()
    asyncio.run(run())


@pytest.mark.parametrize("value", ["1", "en", "example", "aaaaaaaaaaaaaaaaaaaa", "abcabcabcabcabcabc"])
def test_preference_cookies_do_not_block_hosts_paths_or_corrupt_envelope(public_dns, value):
    async def run():
        manager, browser = setup()
        url = "https://login.example.com/123/example/en"
        result = await browser.open(url, "interaction", TOKEN, cookies=[cookie(value=value)],
                                    cookie_generation=await generation(browser))
        session = browser.sessions[result["session_id"]]
        assert not session.cookie_fingerprints
        assert result["url"] == url and result["url_redacted"] is False
        assert await browser._current(session) == url
        observation = await browser.observe(session.session_id, TOKEN)
        assert observation["session_id"] == session.session_id
        assert observation["lane_key"] == "interaction" and observation["url"] == url
        await browser.stop()
    asyncio.run(run())


def test_generation_survives_context_reconnect_but_not_clear_or_worker_restart(public_dns):
    async def run():
        manager, browser = setup()
        manager.lane.context = None
        seed = await generation(browser)
        manager.lease_lane.assert_not_awaited()
        public_dns.assert_not_awaited()

        async def lease(key):
            manager.lane.context = manager.context
            return "started", {}

        manager.lease_lane.side_effect = lease
        first = await browser.open(URL, "interaction", TOKEN, cookies=[cookie()], cookie_generation=seed)
        known = browser.sessions[first["session_id"]].cookie_fingerprints
        assert fingerprints([SECRET]) <= known
        assert await generation(browser) == seed
        replacement = Manager()
        replacement.context.add_cookies = AsyncMock()
        replacement.context.clear_cookies = AsyncMock()
        manager.context = replacement.context
        manager.lane.context = replacement.context
        fresh = await generation(browser)
        assert fresh == seed
        assert browser.sessions[first["session_id"]].cookie_fingerprints is known
        with pytest.raises(HTTPException):
            await browser.clear_cookies(first["session_id"], TOKEN, str(uuid.uuid4()))
        await browser.close(first["session_id"], TOKEN)
        # A context replacement after the handshake must not discard its seed.
        reconnected = Manager()
        reconnected.context.add_cookies = AsyncMock()
        reconnected.context.clear_cookies = AsyncMock()
        manager.context = reconnected.context
        manager.lane.context = reconnected.context
        accepted = await browser.open(URL, "interaction", TOKEN, cookies=[cookie()], cookie_generation=fresh)
        reconnected.context.add_cookies.assert_awaited_once()
        assert browser.sessions[accepted["session_id"]].cookie_fingerprints is known
        await browser.clear_cookies(accepted["session_id"], TOKEN, str(uuid.uuid4()))
        current = await generation(browser)
        assert current != seed
        await browser.close(accepted["session_id"], TOKEN)
        manager.context = replacement.context
        manager.lane.context = replacement.context
        assert await generation(browser) == current
        stale = await browser.open(URL, "interaction", TOKEN, cookies=[cookie()], cookie_generation=seed)
        replacement.context.add_cookies.assert_not_awaited()
        assert browser.sessions[stale["session_id"]].cookie_fingerprints is known
        await browser.stop()
        restarted = AssistantBrowser(manager)
        stale = await restarted.open(URL, "interaction", TOKEN, cookies=[cookie()], cookie_generation=current)
        replacement.context.add_cookies.assert_not_awaited()
        assert not restarted.sessions[stale["session_id"]].cookie_fingerprints
        assert await generation(restarted) != current
        await restarted.stop()
    asyncio.run(run())


def test_stopped_lane_handshake_imports_after_restart_and_retains_old_fingerprints(public_dns):
    async def run():
        manager, browser = setup()
        seed = await generation(browser)
        first = await browser.open(URL, "interaction", TOKEN, cookies=[cookie()], cookie_generation=seed)
        known = browser.sessions[first["session_id"]].cookie_fingerprints
        await browser.close(first["session_id"], TOKEN)
        manager.lane.context = None
        handshake = await generation(browser)
        assert handshake == seed
        # Saved cookies are resolved after the stopped-lane handshake.
        rows = [cookie(value="fresh-opaque-cookie-984713")]
        replacement = Manager()
        replacement.context.add_cookies = AsyncMock()
        replacement.context.clear_cookies = AsyncMock()
        events = []

        async def lease(key):
            manager.context = replacement.context
            manager.lane.context = replacement.context
            return "reconnected", {}

        async def add(values):
            events.append("add")
            assert values == rows

        async def goto(url, **kwargs):
            events.append("goto")
            assert events == ["add", "goto"]
            replacement.page.url = url

        manager.lease_lane.side_effect = lease
        replacement.context.add_cookies.side_effect = add
        replacement.page.goto = AsyncMock(side_effect=goto)
        seeded = await browser.open(URL, "interaction", TOKEN, cookies=rows, cookie_generation=handshake)
        replacement.context.add_cookies.assert_awaited_once_with(rows)
        assert events == ["add", "goto"]
        assert browser.sessions[seeded["session_id"]].cookie_fingerprints is known
        assert fingerprints([SECRET, rows[0]["value"]]) <= known
        await browser.close(seeded["session_id"], TOKEN)
        replacement.page = Page()
        replacement.context.new_page.return_value = replacement.page
        unseeded = await browser.open(URL, "interaction", TOKEN)
        session = browser.sessions[unseeded["session_id"]]
        assert session.cookie_fingerprints is known
        original = replacement.cdp.send.side_effect

        async def send(method, params=None):
            if method == "Runtime.callFunctionOn":
                return {"result": {"value": {"elements": [
                    {"node_id": "n0", "tag": "div", "text": "Reflected " + SECRET},
                ]}}}
            return await original(method, params)

        replacement.cdp.send.side_effect = send
        observed = await browser.observe(session.session_id, TOKEN)
        assert observed["elements"] and SECRET not in json.dumps(observed)
        replacement.context.add_cookies.assert_awaited_once()
        await browser.stop()
    asyncio.run(run())


@pytest.mark.parametrize("clock_skew", [-30, 30])
def test_generations_ignore_cross_host_clock_skew(public_dns, monkeypatch, clock_skew):
    async def run():
        manager, browser = setup()
        seed = await generation(browser)
        current = await browser.open(URL, "interaction", TOKEN)
        now = time.time()
        monkeypatch.setattr(module.time, "time", lambda: now + clock_skew)
        await browser.clear_cookies(current["session_id"], TOKEN, str(uuid.uuid4()))
        await browser.close(current["session_id"], TOKEN)
        old = await browser.open(URL, "interaction", TOKEN, cookies=[cookie()], cookie_generation=seed)
        manager.context.add_cookies.assert_not_awaited()
        await browser.close(old["session_id"], TOKEN)
        fresh = await generation(browser)
        assert fresh != seed
        await browser.open(URL, "interaction", TOKEN, cookies=[cookie()], cookie_generation=fresh)
        manager.context.add_cookies.assert_awaited_once()
        await browser.stop()
    asyncio.run(run())


def test_worker_routes_private_forwarding_validation_and_body_limits(public_dns):
    async def run():
        manager, browser = setup()
        app = create_app()
        app.state.assistant_browser = browser
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
            handshake = await client.post("/assistant-browser/cookie-generation", json={"url": URL, "lane_key": "interaction"})
            assert handshake.status_code == 200
            assert set(handshake.json()) == {"generation"}
            assert uuid.UUID(handshake.json()["generation"]).version == 4
            manager.lease_lane.assert_not_awaited()
            public_dns.assert_not_awaited()
            for extra in ({"token": TOKEN}, {"cookies": [cookie()]}, {"host": "example.com"}):
                invalid = await client.post("/assistant-browser/cookie-generation", json={"url": URL, **extra})
                assert invalid.status_code == 422 and SECRET not in invalid.text
            assert (await client.post("/assistant-browser/cookie-generation", content=b"x" * 4097)).status_code == 413
            payload = {"url": URL, "token": TOKEN, "cookies": [cookie()], "cookie_generation": handshake.json()["generation"]}
            response = await client.post("/assistant-browser/open", json=payload)
            assert response.status_code == 200
            assert SECRET not in response.text
            sid = response.json()["session_id"]
            endpoint = f"/assistant-browser/{sid}/clear-cookies"
            operation = str(uuid.uuid4())
            body = {"token": TOKEN, "operation_id": operation}
            assert (await client.post(endpoint, json={**body, "token": "wrong"})).status_code == 404
            for extra in ({"domain": "example.com"}, {"host": "example.com"}, {"url": URL},
                          {"operation_id": str(uuid.uuid1())}, {"operation_id": SECRET}):
                invalid = await client.post(endpoint, json={**body, **extra})
                assert invalid.status_code == 422 and SECRET not in invalid.text
            first = await client.post(endpoint, json=body)
            assert first.status_code == 200 and first.json()["cleared"] is True
            assert (await client.post(endpoint, json=body)).json() == first.json()
            manager.context.clear_cookies.assert_awaited_once()
            invalid = await client.post("/assistant-browser/open", json={**payload, "cookies": [cookie(secure=SECRET)]})
            assert invalid.status_code == 422 and SECRET not in invalid.text
            invalid = await client.post("/assistant-browser/open", json={**payload, "cookie_seed_at": time.time()})
            assert invalid.status_code == 422
            assert (await client.post(endpoint, content=b"x" * 4097)).status_code == 413
            assert (await client.post("/assistant-browser/open", content=b"x" * (352 * 1024 + 1))).status_code == 413
            # The cookie payload may exceed the old recovery request limit.
            await browser.close(sid, TOKEN)
            payload["cookie_generation"] = await generation(browser)
            large = await client.post("/assistant-browser/open", json={
                **payload, "cookies": [cookie(value="z" * (200 * 1024))]})
            assert large.status_code == 200
            await browser.stop()
        mock = AsyncMock(return_value={"session_id": "mock"})
        app.state.assistant_browser = type("Browser", (), {"open": mock})()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
            assert (await client.post("/assistant-browser/open", json={"url": URL, "token": TOKEN})).status_code == 200
        assert "cookies" not in mock.call_args.kwargs and "cookie_generation" not in mock.call_args.kwargs
    asyncio.run(run())
