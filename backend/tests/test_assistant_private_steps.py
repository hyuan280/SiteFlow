from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException
import httpx
import pytest
from pydantic import ValidationError

from tests.test_assistant_browser import Manager, TOKEN, URL, public_dns  # noqa: F401
from worker import executor, vision
from worker.app import create_app
from worker.assistant_browser import AssistantBrowser, READ_ONLY
from worker.assistant_step import ExecuteRequest, FEATURES, contains_secret, validate_step


def request(feature, config, origin=URL, runtime=None):
    return ExecuteRequest(token=TOKEN, operation_id=uuid.uuid4(), site_origin=origin,
        step={"step_key": "private", "feature_type": feature, "config": config, "timeout_seconds": 12},
        execution={"feature_types": [feature], "allow_sign": True}, private_runtime=runtime)


VISION = {"base_url": "http://internal-provider.invalid/v1", "api_key": "provider-private-key",
          "model": "fixture-model", "timeout": 10, "max_tokens": 0, "supports_vision": True}


@pytest.mark.parametrize("feature,config,runtime", [
    ("challenge.2fa", {"code_selector": ["#code"]}, {"totp_code": "123456"}),
    ("challenge.turnstile", {"wait_seconds": 1}, {"cfbox": {"url": "http://internal-cfbox", "api_key": "private"}}),
    ("challenge.image_captcha", {"image_selector": ["#image"], "input_selector": ["#code"], "length": 0, "min_confidence": 0}, None),
    ("challenge.grid_captcha", {"selected_selector": ".selected", "max_refresh": 0}, {"vision": VISION}),
    ("sign.iframe.image_captcha", {"iframe_selector": ["iframe"], "image_selector": ["#image"], "input_selector": ["#code"], "submit_selector": ["#submit"], "fields": {}}, None),
    ("sign.image_quiz", {"image_selector": ["#image"], "answer_selector": [".answer"], "message_text": "", "max_refresh": 0}, {"vision": VISION}),
    ("action.fill", {"selector": ["#public"], "value": "", "clear": False}, None),
    ("flow.result", {"output_value": False, "llm_enabled": False, "message": "", "llm_prompt": ""}, None),
])
def test_eighteen_builtin_allowlist(feature, config, runtime):
    assert len(FEATURES) == 18
    assert validate_step(request(feature, config, runtime=runtime))["feature_type"] == feature
    assert "flow.result" in READ_ONLY
    for denied in ["flow.jump", "post.buy_card", "post.resign", "plugin.custom"]:
        assert denied not in FEATURES


@pytest.mark.parametrize("runtime", [
    {"plugin_runtimes": {}}, {"broker": {}}, {"totp_code": 123456},
    {"vision": {**VISION, "extra": "PRIVATE"}},
    {"vision": {**VISION, "supports_vision": 1}},
    {"vision": {**VISION, "execution_settings": {"arbitrary": "PRIVATE"}}},
    {"cfbox": {"url": "http://internal", "secret_access": "PRIVATE"}},
    {"feature_execution_settings": {"flow.result": {"base_url": "PRIVATE"}}},
])
def test_private_runtime_strict_allowlist(runtime):
    with pytest.raises(ValidationError):
        request("flow.result", {}, runtime=runtime)


def test_runtime_repr_and_endpoint_validation_are_private():
    payload = request("challenge.2fa", {"code_selector": ["#code"]}, runtime={"totp_code": "PRIVATE"})
    assert "PRIVATE" not in repr(payload) and "PRIVATE" not in repr(payload.private_runtime)

    async def run():
        app = create_app()
        app.state.assistant_browser = SimpleNamespace(execute=AsyncMock())
        raw = payload.model_dump(mode="json")
        raw["private_runtime"]["vision"] = {**VISION, "arbitrary": "PRIVATE"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
            response = await client.post("/assistant-browser/id/execute", json=raw)
        assert response.status_code == 422
        assert "PRIVATE" not in response.text and VISION["api_key"] not in response.text
    asyncio.run(run())


@asynccontextmanager
async def chromium_fixture(html, extra=None, trusted_origins=None, same_origin_only=False):
    from playwright.async_api import async_playwright

    candidates = list(Path("/opt/opencode/.cache/ms-playwright").glob("chromium_headless_shell-*/**/headless_shell"))
    candidates += list(Path("/opt/opencode/.cache/ms-playwright").glob("chromium_headless_shell-*/**/chrome-headless-shell"))
    async with async_playwright() as playwright:
        try:
            chromium = await playwright.chromium.launch(headless=True,
                executable_path=str(candidates[0]) if candidates else None,
                args=["--host-resolver-rules=MAP fixture.example 127.0.0.1, MAP external.example 127.0.0.1",
                      "--no-proxy-server"])
        except Exception:
            pytest.skip("Local Chromium executable unavailable")
        context = await chromium.new_context(service_workers="block")
        browser = AssistantBrowser(Manager())
        browser.manager.lane.context = context
        seen = []

        async def serve(reader, writer):
            headers = await reader.readuntil(b"\r\n\r\n")
            method, path, _ = headers.split(b"\r\n")[0].decode().split(" ")
            length = next((int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                           if line.lower().startswith(b"content-length:")), 0)
            body = await reader.readexactly(length) if length else b""
            seen.append((method, path, body))
            content = (extra or {}).get(path, html).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\nContent-Length: "
                         + str(len(content)).encode() + b"\r\n\r\n" + content)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        origin = f"http://fixture.example:{server.sockets[0].getsockname()[1]}"
        try:
            session_id = (await browser.open(origin + "/", "interaction", TOKEN,
                trusted_origins=trusted_origins, same_origin_only=same_origin_only))["session_id"]
            yield browser, session_id, origin, browser.sessions[session_id].page, seen
        finally:
            await browser.stop()
            server.close()
            await server.wait_closed()
            await chromium.close()


CAPTCHA = '''<form method="post"><canvas id="image" width="90" height="35"></canvas>
<input id="code" name="captcha"><button id="submit">Submit</button></form>
<div id="reflected"></div><input id="public" name="comment">
<script>code.oninput=()=>{reflected.textContent=code.value.split('').reverse().join('');
reflected.className='answer-'+btoa(code.value)};
document.querySelector('form').onsubmit=e=>{e.preventDefault(); document.body.dataset.solved=code.value};
</script>'''


CF_SOURCE = 'https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/turnstile/test'


async def install_cf_frame(page):
    async def serve(route):
        await route.fulfill(content_type='text/html', body='''<style>body{margin:0}</style>
            <input type="checkbox" style="width:30px;height:30px"
            onclick="parent.postMessage('challenge-clicked','*')">''')

    await page.route('https://challenges.cloudflare.com/**', serve)
    await page.evaluate("""source => {
        const frame = document.createElement('iframe'); frame.id='cf'; frame.src=source;
        frame.style='position:fixed;left:100px;top:100px;width:300px;height:70px;border:0';
        document.body.append(frame);
        window.addEventListener('message', e => {
            if(e.origin==='https://challenges.cloudflare.com') document.body.dataset.challengeClicked='yes';
        });
    }""", CF_SOURCE)
    await page.frame_locator('#cf').locator('input').wait_for()


async def assert_tools_available(browser, session_id, origin, previous):
    from app.services.assistant_snapshot import safe_snapshot

    session = browser.sessions[session_id]
    assert previous["snapshot_id"] in session.snapshots
    observed = await browser.observe(session_id, TOKEN)
    assert observed["elements"]
    assert safe_snapshot(observed, {"base_url": origin + '/'})['ok'] is True
    assert (await browser.inspect(session_id, TOKEN, previous["snapshot_id"], "n1"))["elements"]
    checked = await browser.check_selector(session_id, TOKEN, '#code')
    assert checked['match_count'] == 1
    assert 'value' not in checked['matches'][0]['attributes']
    assert 'text' not in checked['matches'][0]
    assert (await browser.screenshot(session_id, TOKEN))['masked'] is True
    await browser.navigate(session_id, TOKEN, origin + "/next")
    await browser.refresh(session_id, TOKEN)
    assert (await browser.observe(session_id, TOKEN))["elements"]
    assert (await browser.screenshot(session_id, TOKEN))['masked'] is True


@pytest.mark.parametrize("method", ["ddddocr", "nexusphp_gd5", "vision"])
def test_chromium_captcha_answers_leave_browser_tools_available(public_dns, monkeypatch, method):
    answer = "solverA9"
    monkeypatch.setattr(executor, "_classify_captcha", lambda _: (answer, 1.0))
    monkeypatch.setattr(executor, "_recognize_nexusphp_gd5", lambda _: (answer, 50))
    monkeypatch.setattr(vision, "recognize", lambda *_: answer)

    async def run():
        async with chromium_fixture(CAPTCHA) as (browser, sid, origin, page, _):
            previous = await browser.observe(sid, TOKEN)
            assert previous["elements"]
            payload = request("challenge.image_captcha", {"image_selector": ["#image"],
                "input_selector": ["#code"], "submit_selector": ["#submit"], "captcha_method": method},
                origin, {"vision": VISION} if method == "vision" else None)
            response = await browser.execute(sid, payload)
            assert response["status"] == "success", response
            assert await page.locator("#code").input_value() == answer
            assert await page.locator("#reflected").inner_text() == answer[::-1]
            assert not contains_secret(answer, browser.sessions[sid].secret_fingerprints)
            assert response["before"]["elements"] and response["after"]["elements"]
            assert all('value' not in node['attributes'] for snapshot in ('before', 'after')
                       for node in response[snapshot]['elements'] if node['tag'] == 'input')
            assert response["result"] == {"submitted": True}
            assert answer[::-1] in json.dumps(response)
            assert "provider-private" not in json.dumps(response)
            assert response == await browser.execute(sid, payload)
            await assert_tools_available(browser, sid, origin, previous)
            await browser.close(sid, TOKEN)
            replacement = (await browser.open(origin + "/", "interaction", TOKEN))["session_id"]
            assert (await browser.observe(replacement, TOKEN))["elements"]
            fresh = await browser.execute(replacement, request('action.fill', {
                'selector': ['#public'], 'value': 'fresh session'}, origin))
            assert fresh['status'] == 'success', fresh
    asyncio.run(run())


@pytest.mark.parametrize("script", [
    "code.type='password'", "code.readOnly=true", "code.after(code.cloneNode())",
    "document.querySelector('form').action='https://other.example/'",
    "document.querySelector('form').target='_blank'",
])
def test_chromium_totp_targets_fail_before_fill(public_dns, script):
    async def run():
        async with chromium_fixture(CAPTCHA) as (browser, sid, origin, page, seen):
            await page.evaluate(script)
            response = await browser.execute(sid, request("challenge.2fa", {
                "code_selector": ["#code"], "submit_selector": ["#submit"]}, origin, {"totp_code": "123456"}))
            assert response["status"] == "error", response
            assert await page.locator("#code").first.input_value() == ""
            assert not any(row[0] == "POST" for row in seen)
            assert "123456" not in json.dumps(response)
            assert contains_secret('123456', browser.sessions[sid].secret_fingerprints)
    asyncio.run(run())


def test_chromium_totp_and_rebinding_after_fill(public_dns):
    async def run():
        async with chromium_fixture(CAPTCHA) as (browser, sid, origin, page, seen):
            await page.evaluate("code.oninput=()=>reflected.textContent=code.value; void 0")
            payload = request("challenge.2fa", {"code_selector": ["#code"]}, origin, {"totp_code": "123456"})
            response = await browser.execute(sid, payload)
            assert response["status"] == "success" and response["result"] == {"submitted": False}
            assert await page.locator("#code").input_value() == "123456"
            assert contains_secret('123456', browser.sessions[sid].secret_fingerprints)
            assert response['before']['elements'] and response['after']['elements']
            observed = await browser.observe(sid, TOKEN)
            assert '123456' not in json.dumps([response, observed])
            assert any(node.get('text') == '[redacted]' for node in observed['elements'])
            checked = await browser.check_selector(sid, TOKEN, '#code')
            assert 'value' not in checked['matches'][0]['attributes'] and 'text' not in checked['matches'][0]
            assert (await browser.screenshot(sid, TOKEN))['masked']
            with pytest.raises(HTTPException) as blocked:
                await browser.navigate(sid, TOKEN, origin + '/next?answer=123456')
            assert blocked.value.detail == {'code': 'browser_url_denied'}
            assert await page.locator('#code').input_value() == '123456'
            await browser.close(sid, TOKEN)
            sid = (await browser.open(origin + "/", "interaction", TOKEN))["session_id"]
            page = browser.sessions[sid].page
            await page.evaluate("code.oninput=()=>submit.replaceWith(submit.cloneNode(true)); void 0")
            payload.operation_id = uuid.uuid4()
            payload.step["config"]["submit_selector"] = ["#submit"]
            response = await browser.execute(sid, payload)
            assert response["status"] == "unknown", response
            assert not any(row[0] == "POST" for row in seen)
            assert "123456" not in json.dumps(response)
    asyncio.run(run())


@pytest.mark.parametrize("script", ["", "document.querySelector('iframe').src='https://other.example/'", "document.body.append(document.querySelector('iframe').cloneNode())"])
def test_chromium_private_iframe_same_origin_unique(public_dns, monkeypatch, script):
    monkeypatch.setattr(executor, "_classify_captcha", lambda _: ("answer99", 1.0))

    async def run():
        async with chromium_fixture('<iframe src="/frame"></iframe>', {"/frame": CAPTCHA, "/replacement": CAPTCHA}) as (browser, sid, origin, page, _):
            await page.locator("iframe").first.wait_for()
            await page.frame_locator("iframe").locator("#code").wait_for()
            if script:
                await page.evaluate(script)
            response = await browser.execute(sid, request("sign.iframe.image_captcha", {
                "iframe_selector": ["iframe"], "image_selector": ["#image"], "input_selector": ["#code"],
                "submit_selector": ["#submit"], "fields": {}}, origin))
            assert response["status"] == ("error" if script else "success"), response
            assert "answer99" not in json.dumps(response)
    asyncio.run(run())


@pytest.mark.parametrize('race', [None, 'navigate', 'replace'])
def test_screenshot_shows_same_origin_frame_images_masks_inputs_and_unreadable_frames(public_dns, race):
    from PIL import Image

    controls = '''<style>body{margin:0}canvas,input{position:fixed;top:0;width:90px;height:30px;
        border:0;padding:0}canvas{left:0;background:rgb(0,180,0)}input{left:120px}</style>
        <canvas></canvas><input value="private-input">'''
    html = controls + '''<iframe id="readable" src="/frame" style="position:fixed;left:20px;top:100px;width:300px;height:200px;border:0"></iframe>
        <iframe srcdoc="private-frame" style="position:fixed;left:20px;top:330px;width:300px;height:40px;border:0"></iframe>'''
    frame = controls + '<iframe src="/nested" style="position:fixed;left:0;top:80px;width:260px;height:60px;border:0"></iframe>'

    async def run():
        async with chromium_fixture(html, {'/frame': frame, '/nested': controls},
                trusted_origins=['https://other.example']) as (browser, sid, origin, page, _):
            await page.frame_locator('#readable').frame_locator('iframe').locator('input').wait_for()
            await page.route('https://other.example/**', lambda route: route.fulfill(
                content_type='text/html', body=controls))
            await page.evaluate("""() => {
                const frame = document.createElement('iframe'); frame.src='https://other.example/frame';
                frame.id='cross'; frame.style='position:fixed;left:20px;top:430px;width:300px;height:40px;border:0';
                document.body.append(frame);
            }""")
            await page.frame_locator('#cross').locator('input').wait_for()
            before = await page.content()
            if race:
                capture = page.screenshot

                async def screenshot(**kwargs):
                    image = await capture(**kwargs)
                    if race == 'navigate':
                        await page.frame_locator('#readable').locator('canvas').evaluate("() => location.href='/nested'")
                        await page.frame_locator('#readable').locator('input').wait_for()
                    else:
                        await page.locator('#readable').evaluate('el => el.replaceWith(el.cloneNode())')
                        await page.frame_locator('#readable').locator('input').wait_for()
                    return image

                page.screenshot = screenshot
                with pytest.raises(HTTPException) as error:
                    await browser.screenshot(sid, TOKEN)
                assert error.value.detail == {'code': 'browser_snapshot_stale'}
                return
            result = await browser.screenshot(sid, TOKEN)
            with Image.open(BytesIO(base64.b64decode(result['image_base64']))) as image:
                for y in (15, 115, 195):
                    red, green, blue = image.getpixel((50, y))
                    assert red < 30 and green > 150 and blue < 30, (y, red, green, blue)
                    red, green, blue = image.getpixel((170, y))
                    assert red > 220 and green < 30 and blue > 220, (y, red, green, blue)
                for y in (345, 445):
                    red, green, blue = image.getpixel((50, y))
                    assert red > 220 and green < 30 and blue > 220, (y, red, green, blue)
            assert await page.content() == before
            assert await page.frame_locator('#readable').locator('input').input_value() == 'private-input'
            assert await page.frame_locator('#readable').frame_locator('iframe').locator('input').input_value() == 'private-input'
    asyncio.run(run())


@pytest.mark.parametrize("selector", ["#public", "#code", "#password", "#cookie", "#duplicate"])
def test_chromium_public_fill_rejects_sensitive_targets(public_dns, selector):
    html = CAPTCHA + '<input id="password" type="password"><input id="cookie" name="cookie"><input id="duplicate"><input id="duplicate">'

    async def run():
        async with chromium_fixture(html) as (browser, sid, origin, page, _):
            response = await browser.execute(sid, request("action.fill", {"selector": [selector], "value": "public comment"}, origin))
            assert response["status"] == ("success" if selector == "#public" else "error"), response
            if selector == "#public":
                assert await page.locator(selector).input_value() == "public comment"
            assert not browser.sessions[sid].secret_fingerprints
    asyncio.run(run())


def test_chromium_fill_revalidates_original_after_clear(public_dns):
    async def run():
        async with chromium_fixture(CAPTCHA) as (browser, sid, origin, page, _):
            await page.evaluate("public.value='existing'; public.oninput=()=>public.replaceWith(public.cloneNode()); void 0")
            assert await page.locator('#public').input_value() == 'existing'
            response = await browser.execute(sid, request("action.fill", {"selector": ["#public"], "value": "do not fill replacement"}, origin))
            assert response["status"] == "unknown", (response, await page.locator('#public').input_value())
            assert await page.locator("#public").input_value() == ""
    asyncio.run(run())


def test_flow_result_only_boolean_and_host_settings(public_dns, monkeypatch):
    judgment = AsyncMock(return_value={"result": False, "reason": "PRIVATE-answer-from-page"})
    monkeypatch.setattr(vision, "judge_result", judgment)

    async def run():
        async with chromium_fixture(CAPTCHA) as (browser, sid, origin, page, _):
            response = await browser.execute(sid, request("flow.result", {"output_value": False}, origin))
            assert response["result"] == {"value": False}
            judgment.assert_not_awaited()
            response = await browser.execute(sid, request("flow.result", {"output_value": False, "llm_enabled": True}, origin,
                {"vision": VISION, "feature_execution_settings": {"flow.result": {
                    "llm_enabled": True, "judgment_scope": "all", "use_vision": False}}}))
            assert response["status"] == "success", response
            assert response["result"] == {"value": False}
            assert "PRIVATE" not in json.dumps(response)
            assert not browser.sessions[sid].writes_authorized
            judgment.assert_awaited_once()
    asyncio.run(run())


def test_chromium_challenge_allows_followup_on_same_page_and_exact_replay(public_dns, monkeypatch):
    monkeypatch.setattr(executor, '_classify_captcha', lambda _: ('solverA9', 1.0))

    async def run():
        async with chromium_fixture(CAPTCHA) as (browser, sid, origin, page, _):
            payload = request('challenge.image_captcha', {
                'image_selector': ['#image'], 'input_selector': ['#code']}, origin)
            response = await browser.execute(sid, payload)
            assert response['status'] == 'success', response
            # The reversed reflection is deliberately outside exact-secret filtering.
            assert await page.locator('#reflected').inner_text() == '9Arevlos'
            await page.evaluate("reflected.onclick=()=>document.body.dataset.clicked='yes'; void 0")
            session = browser.sessions[sid]
            judge = AsyncMock(return_value={'result': True, 'reason': 'solver reasoning'})
            monkeypatch.setattr(vision, 'judge_result', judge)
            for selector in ['#reflected:has-text("9A")', '#reflected:text-matches("^9A")']:
                clicked = await browser.execute(sid, request('action.click', {'selector': [selector]}, origin))
                assert clicked['status'] == 'success', clicked
            assert await page.locator('body').get_attribute('data-clicked') == 'yes'
            checked = await browser.check_selector(sid, TOKEN, '[class^="answer-"]')
            assert checked['match_count'] == 1 and checked['matches'][0]['text'] == '9Arevlos'
            assert (await browser.screenshot(sid, TOKEN))['masked']
            submitted = await browser.execute(sid, request('action.click', {'selector': ['#submit']}, origin))
            assert submitted['status'] == 'success', submitted
            assert await page.locator('body').get_attribute('data-solved') == 'solverA9'
            assert (await browser.execute(sid, request('action.wait', {'seconds': 1}, origin)))['status'] == 'success'
            result = await browser.execute(sid, request('flow.result', {'llm_enabled': True}, origin,
                {'vision': VISION, 'feature_execution_settings': {'flow.result': {
                    'llm_enabled': True, 'judgment_scope': 'all', 'use_vision': False}}}))
            assert result['status'] == 'success' and result['result'] == {'value': True}
            assert 'solver reasoning' not in json.dumps(result)
            judge.assert_awaited_once()
            assert session.page is page
            assert await browser.execute(sid, payload) == response
            changed = payload.model_copy(deep=True)
            changed.step['config']['input_selector'] = ['#public']
            with pytest.raises(HTTPException) as conflict:
                await browser.execute(sid, changed)
            assert conflict.value.status_code == 409
            assert conflict.value.detail == 'Browser operation conflict'
            with pytest.raises(HTTPException) as unauthorized:
                await browser.execute(sid, request('action.wait', {'seconds': 1}, origin).model_copy(update={'token': 'wrong'}))
            assert unauthorized.value.status_code == 404

            await browser.navigate(sid, TOKEN, origin + '/next')
            await browser.refresh(sid, TOKEN)
            assert (await browser.execute(sid, request('action.fill', {
                'selector': ['#public'], 'value': 'next comment'}, origin)))['status'] == 'success'
            assert await browser.execute(sid, payload) == response

    asyncio.run(run())


@pytest.mark.parametrize('terminal', ['success', 'unknown'])
def test_challenge_execution_preserves_pending_busy_and_unknown_history(public_dns, monkeypatch, terminal):
    async def run():
        browser = AssistantBrowser(Manager())
        sid = (await browser.open(URL, 'interaction', TOKEN))['session_id']
        entered, release = asyncio.Event(), asyncio.Event()

        async def step(*args, **kwargs):
            entered.set()
            await release.wait()
            return terminal, '', {'submitted': False}

        run_step = AsyncMock(side_effect=step)
        monkeypatch.setattr(executor, '_run_step', run_step)
        payload = request('challenge.image_captcha', {
            'image_selector': ['#image'], 'input_selector': ['#code']})
        original = asyncio.create_task(browser.execute(sid, payload))
        try:
            await entered.wait()
            with pytest.raises(HTTPException) as pending:
                await browser.execute(sid, payload)
            assert pending.value.detail == 'Browser operation in progress'
            with pytest.raises(HTTPException) as denied:
                await browser.execute(sid, request('action.click', {'selector': ['#guess']}))
            assert denied.value.detail == 'Browser session busy'
            release.set()
            response = await original
            assert response['status'] == terminal
            assert await browser.execute(sid, payload) == response
            run_step.assert_awaited_once()
            if terminal == 'unknown':
                run_step.side_effect = None
                run_step.return_value = ('success', '', {})
                assert (await browser.execute(sid, request('action.click', {'selector': ['#next']})))['status'] == 'success'
                await browser.execute(sid, request('flow.result', {'output_value': False}))
                assert browser.sessions[sid].uncertain
                assert browser.sessions[sid].unresolved_operations == {str(payload.operation_id)}
                assert await browser.execute(sid, payload) == response
            else:
                assert (await browser.execute(sid, request('action.click', {'selector': ['#next']})))['status'] == 'success'
        finally:
            release.set()
            await original
            await browser.stop()
    asyncio.run(run())


def test_chromium_quiz_choice_visible_without_raw_solver_response(public_dns, monkeypatch):
    monkeypatch.setattr(vision, "answer_image_quiz", lambda *_: (2, "private reasoning"))
    html = '''<canvas id="image" width="60" height="30"></canvas>
    <button class="answer" type="button" onclick="this.className+=' selected'">first</button>
    <button class="answer" type="button" onclick="this.className+=' selected'; document.body.dataset.answer='second'">second</button>
    <div id="ok">Success</div>'''

    async def run():
        async with chromium_fixture(html) as (browser, sid, origin, page, _):
            response = await browser.execute(sid, request("sign.image_quiz", {"image_selector": ["#image"],
                "answer_selector": [".answer"], "success_selector": ["#ok"], "max_refresh": 0}, origin, {"vision": VISION}))
            assert response["status"] == "success", response
            assert await page.locator(".selected").inner_text() == "second"
            assert not any(value in json.dumps(response) for value in ["selected_index", "private reasoning"])
            assert 'second' in json.dumps(response['after'])
            assert (await browser.observe(sid, TOKEN))["elements"]
            assert (await browser.check_selector(sid, TOKEN, '.selected'))['match_count'] == 1
            assert not contains_secret('second', browser.sessions[sid].secret_fingerprints)
            assert (await browser.screenshot(sid, TOKEN))['masked']
    asyncio.run(run())


def test_chromium_grid_state_visible_without_raw_solver_response(public_dns, monkeypatch):
    monkeypatch.setattr(vision, "answer_grid_captcha", AsyncMock(return_value=[2, 5, 9]))
    html = '<div id="grid">' + ''.join('<button class="cell" onclick="this.classList.toggle(\'selected\')">x</button>' for _ in range(9)) + '''</div>
    <button id="submit" onclick="document.querySelector('#ok').hidden=false">Submit</button><div id="ok" hidden>Success</div>'''

    async def run():
        async with chromium_fixture(html) as (browser, sid, origin, page, _):
            response = await browser.execute(sid, request("challenge.grid_captcha", {"image_selector": ["#grid"],
                "cell_selector": [".cell"], "selected_selector": ".selected", "submit_selector": ["#submit"],
                "success_selector": ["#ok"], "max_refresh": 0}, origin, {"vision": VISION}))
            assert response["status"] == "success", response
            assert await page.locator(".selected").count() == 3
            assert response["result"] == {"submitted": True, "verified": True}
            assert "selected_indices" not in json.dumps(response)
            assert (await browser.observe(sid, TOKEN))["elements"]
            assert (await browser.check_selector(sid, TOKEN, '.selected'))['match_count'] == 3
            assert not contains_secret('[2,5,9]', browser.sessions[sid].secret_fingerprints)
            assert (await browser.screenshot(sid, TOKEN))['masked']
    asyncio.run(run())


@pytest.mark.parametrize("approved,credential,method,main,allowed", [
    (True, False, "GET", False, True), (True, False, "HEAD", False, True),
    (False, False, "GET", False, False), (True, True, "GET", False, False),
    (True, False, "POST", False, False), (True, False, "GET", True, False),
])
@pytest.mark.parametrize("transport", ["route", "redirect"])
def test_turnstile_resources_require_explicit_trust_no_credential_authority(
    public_dns, approved, credential, method, main, allowed, transport
):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        sid = (await browser.open(URL, "interaction", TOKEN,
            trusted_origins=["https://challenge.example"] if approved else []))["session_id"]
        session = browser.sessions[sid]
        session.executing = session.same_origin_only = session.challenge_resources = True
        session.writes_authorized = True
        session.credential_execution = credential
        target = "https://challenge.example/frame"
        if transport == "route":
            route = SimpleNamespace(request=SimpleNamespace(url=target, method=method,
                resource_type="document", frame=manager.page.main_frame if main else object(),
                is_navigation_request=lambda: True), abort=AsyncMock(), continue_=AsyncMock())
            await browser._route(session, route)
            assert route.continue_.await_count == int(allowed)
        else:
            await browser._response(session, {"requestId": "r", "frameId": "main" if main else "child",
                "resourceType": "Document", "request": {"url": URL, "method": method},
                "responseStatusCode": 307, "responseHeaders": [{"name": "Location", "value": target}]})
            assert manager.cdp.send.call_args.args[0] == ("Fetch.continueResponse" if allowed else "Fetch.failRequest")
        await browser.stop()
    asyncio.run(run())


def test_chromium_turnstile_private_page_only_and_static_http_failure(public_dns, monkeypatch):
    calls = []

    class Provider:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def post(self, *args, **kwargs):
            calls.append((args, kwargs))
            raise httpx.ConnectError("PRIVATE provider URL and secret response")

    monkeypatch.setattr(executor.httpx, "AsyncClient", lambda **_: Provider())

    async def run():
        async with chromium_fixture(CAPTCHA, trusted_origins=['https://challenges.cloudflare.com']) as (browser, sid, origin, page, _):
            await install_cf_frame(page)
            previous = await browser.observe(sid, TOKEN)
            response = await browser.execute(sid, request("challenge.turnstile", {"wait_seconds": 5}, origin,
                {"cfbox": {"url": "http://private-cfbox.invalid", "api_key": "cfbox-private-key"}}))
            assert response["status"] == "failed", response
            assert len(calls) == 1 and calls[0][1]["files"]["image"][1]
            assert not any(value in json.dumps(response) for value in ["PRIVATE", "private-cfbox", "cfbox-private-key"])
            await assert_tools_available(browser, sid, origin, previous)
    asyncio.run(run())


def test_chromium_provider_rebinds_answer_input_before_fill(public_dns, monkeypatch):
    async def run():
        async with chromium_fixture(CAPTCHA) as (browser, sid, origin, page, _):
            async def replace(*args, **kwargs):
                await page.evaluate("code.replaceWith(code.cloneNode())")
                return "ok", "PRIVATEanswer", "", {}

            monkeypatch.setattr(executor, "_read_captcha", replace)
            response = await browser.execute(sid, request("challenge.image_captcha", {
                "image_selector": ["#image"], "input_selector": ["#code"]}, origin))
            assert response["status"] == "error", response
            assert await page.locator("#code").input_value() == ""
            assert not contains_secret("PRIVATEanswer", browser.sessions[sid].secret_fingerprints)
            assert "PRIVATEanswer" not in json.dumps(response)
    asyncio.run(run())


def test_chromium_private_iframe_rechecks_origin_after_provider(public_dns, monkeypatch):
    async def run():
        async with chromium_fixture('<iframe src="/frame"></iframe>', {"/frame": CAPTCHA, "/replacement": CAPTCHA}) as (browser, sid, origin, page, _):
            await page.frame_locator("iframe").locator("#code").wait_for()

            async def replace(*args, **kwargs):
                await page.locator('iframe').evaluate("el => el.src='/replacement'")
                await page.frame_locator('iframe').locator('#code').wait_for()
                return "ok", "PRIVATEanswer", "", {}

            monkeypatch.setattr(executor, "_read_captcha", replace)
            response = await browser.execute(sid, request("sign.iframe.image_captcha", {
                "iframe_selector": ["iframe"], "image_selector": ["#image"], "input_selector": ["#code"],
                "submit_selector": ["#submit"]}, origin))
            assert response["status"] == "error", response
            assert await page.frame_locator('iframe').locator('#code').input_value() == ''
    asyncio.run(run())


@pytest.mark.parametrize("value", ["{password}", "Bearer private-token", "cookie=session", "known-secret"])
def test_public_fill_rejects_secret_values(public_dns, value):
    async def run():
        async with chromium_fixture(CAPTCHA) as (browser, sid, origin, page, _):
            from worker.assistant_step import fingerprints

            browser.sessions[sid].secret_fingerprints.update(fingerprints(["known-secret"]))
            response = await browser.execute(sid, request('action.fill', {'selector': ['#public'], 'value': value}, origin))
            assert response['status'] == 'error', response
            assert await page.locator('#public').input_value() == ''
    asyncio.run(run())


def test_recognized_captcha_answer_allowed_in_ordinary_query(public_dns, monkeypatch):
    monkeypatch.setattr(executor, '_classify_captcha', lambda _: ('PRIVATEanswer', 1.0))

    async def run():
        async with chromium_fixture(CAPTCHA) as (browser, sid, origin, page, seen):
            await page.evaluate("code.oninput=()=>fetch('/leak?answer='+encodeURIComponent(code.value)); void 0")
            response = await browser.execute(sid, request('challenge.image_captcha', {
                'image_selector': ['#image'], 'input_selector': ['#code']}, origin))
            await page.wait_for_timeout(100)
            assert response['status'] == 'success', response
            assert any('PRIVATEanswer' in row[1] for row in seen)
            assert not contains_secret('PRIVATEanswer', browser.sessions[sid].secret_fingerprints)
            assert 'blocked_requests' not in response['diagnostics']
            await browser.navigate(sid, TOKEN, origin + '/next?answer=PRIVATEanswer')
            assert browser.sessions[sid].view()['url'] == origin + '/next?answer=PRIVATEanswer'
            await browser.navigate(sid, TOKEN, origin + '/next?token=PRIVATEanswer')
            assert browser.sessions[sid].view()['url'] == origin + '/next?token=[REDACTED]'
            session = browser.sessions[sid]
            browser._record_blocked(session, origin + '/next?answer=PRIVATEanswer', 'POST',
                HTTPException(403, {'code': 'browser_url_denied'}))
            assert session.blocked_requests[-1]['url'] == origin + '/next?answer=PRIVATEanswer'
    asyncio.run(run())


def test_private_hooks_are_capabilities_not_json():
    from worker.assistant_private import private_context

    with pytest.raises(RuntimeError, match='Private capability denied'):
        private_context({'_assistant_private_context': {'register_secret': 'not callable'}})
    assert private_context(None) is None
    assert private_context({'secret_access': {}}) is None


def test_private_result_filter_count_is_strict(public_dns, monkeypatch):
    async def run():
        browser = AssistantBrowser(Manager())
        sid = (await browser.open(URL, 'interaction', TOKEN))['session_id']
        monkeypatch.setattr(executor, '_run_step', AsyncMock(return_value=('success', 'PRIVATE', {
            'count': True, 'submitted': 1, 'verified': False, 'selected_index': 2, 'text': 'PRIVATE'})))
        response = await browser.execute(sid, request('action.wait', {'seconds': 1}))
        assert response['result'] == {'verified': False}
        assert 'PRIVATE' not in json.dumps(response)
        await browser.stop()
    asyncio.run(run())


@pytest.mark.parametrize('echo', ['ProviderPrivateKey991', 'prefixProviderPrivateKey991suffix'])
def test_vision_cannot_echo_provider_key_to_page(public_dns, monkeypatch, caplog, echo):
    calls = []

    def respond(request, **kwargs):
        calls.append(request)
        return BytesIO(json.dumps({'choices': [{'message': {'content': echo}, 'finish_reason': 'stop'}]}).encode())

    monkeypatch.setattr(vision, 'urlopen', respond)

    async def run():
        async with chromium_fixture(CAPTCHA) as (browser, sid, origin, page, seen):
            await page.evaluate("code.onfocus=()=>document.body.dataset.focused='yes'; void 0")
            response = await browser.execute(sid, request('challenge.image_captcha', {
                'image_selector': ['#image'], 'input_selector': ['#code'], 'captcha_method': 'vision'}, origin,
                {'vision': {**VISION, 'api_key': 'ProviderPrivateKey991'}}))
            assert len(calls) == 1
            assert response['status'] == 'failed', response
            assert await page.locator('#code').input_value() == ''
            assert await page.locator('body').get_attribute('data-focused') is None
            assert not any(row[0] == 'POST' for row in seen)
            assert 'ProviderPrivateKey991' not in json.dumps(response)
            assert 'ProviderPrivateKey991' not in caplog.text
            with pytest.raises(HTTPException) as blocked:
                await browser.navigate(sid, TOKEN, origin + '/next?answer=ProviderPrivateKey991')
            assert blocked.value.detail == {'code': 'browser_url_denied'}
            session = browser.sessions[sid]
            browser._record_blocked(session, origin + '/next?answer=ProviderPrivateKey991', 'GET', blocked.value)
            assert session.blocked_requests[-1]['url'] == '[redacted]'
            assert 'ProviderPrivateKey991' not in json.dumps(session.blocked_requests)
    asyncio.run(run())


@pytest.mark.parametrize('secret', ['ProviderPrivateKey991', VISION['base_url']])
def test_quiz_cannot_submit_provider_secret_option(public_dns, monkeypatch, caplog, secret):
    monkeypatch.setattr(vision, 'answer_image_quiz', lambda *_: (1, ''))
    html = '<canvas id="image" width="90" height="35"></canvas><form method="post">' + (
        '<input type="submit" class="answer" name="answer" value="' + secret + '"></form>')

    async def run():
        async with chromium_fixture(html) as (browser, sid, origin, page, seen):
            await page.evaluate("document.querySelector('.answer').onfocus=()=>document.body.dataset.focused='yes'; void 0")
            response = await browser.execute(sid, request('sign.image_quiz', {
                'image_selector': ['#image'], 'answer_selector': ['.answer'], 'max_refresh': 0}, origin,
                {'vision': {**VISION, 'api_key': 'ProviderPrivateKey991'}}))
            assert response['status'] == 'error', response
            assert await page.locator('body').get_attribute('data-focused') is None
            assert not any(row[0] == 'POST' for row in seen)
            assert secret not in json.dumps(response) and secret not in caplog.text
    asyncio.run(run())


@pytest.mark.parametrize('feature', ['action.fill', 'challenge.image_captcha', 'challenge.2fa'])
@pytest.mark.parametrize('mutation', ['password', 'form', 'replacement', 'classification', 'hidden'])
def test_focus_handlers_cannot_retarget_private_or_public_fill(public_dns, monkeypatch, feature, mutation):
    monkeypatch.setattr(executor, '_classify_captcha', lambda _: ('Answer991', 1.0))

    async def run():
        async with chromium_fixture(CAPTCHA) as (browser, sid, origin, page, _):
            selector = '#public' if feature == 'action.fill' else '#code'
            await page.locator(selector).evaluate("""(el, mutation) => {
                const form = document.createElement('form'); form.id='other'; document.body.append(form);
                window.originalTarget=el; window.inputEvents=0;
                el.addEventListener('input',()=>window.inputEvents++);
                el.onfocus=()=>{
                    document.body.dataset.focused='yes';
                    if(mutation==='password') el.type='password';
                    if(mutation==='form') form.append(el);
                    if(mutation==='replacement') el.replaceWith(el.cloneNode());
                    if(mutation==='classification') {
                        if(el.id==='public') el.autocomplete='current-password';
                        else el.readOnly=true;
                    }
                    if(mutation==='hidden') el.style.display='none';
                };
            }""", mutation)
            config = {'selector': [selector], 'value': 'public comment'} if feature == 'action.fill' else (
                {'code_selector': [selector]} if feature == 'challenge.2fa' else
                {'image_selector': ['#image'], 'input_selector': [selector]})
            response = await browser.execute(sid, request(feature, config, origin,
                {'totp_code': '123456'} if feature == 'challenge.2fa' else None))
            assert response['status'] == 'unknown', response
            assert await page.locator('body').get_attribute('data-focused') == 'yes'
            assert await page.locator(selector).input_value() == ''
            assert await page.evaluate('originalTarget.value') == ''
            assert await page.evaluate('inputEvents') == 0
            assert 'Answer991' not in json.dumps(response) and '123456' not in json.dumps(response)
    asyncio.run(run())


@pytest.mark.parametrize('case', ['no_challenge', 'purchase', 'stale_node', 'reloaded', 'moved', 'overlay', 'untrusted', 'valid'])
def test_turnstile_provider_coordinates_bound_to_pinned_challenge(public_dns, monkeypatch, case):
    calls = []

    async def run():
        trusted = [] if case == 'untrusted' else ['https://challenges.cloudflare.com']
        async with chromium_fixture('<button id="buy" style="position:fixed;left:500px;top:300px;width:100px;height:40px" '
                'onclick="document.body.dataset.purchased=\'yes\'">Buy</button>',
                trusted_origins=trusted) as (browser, sid, origin, page, _):
            if case != 'no_challenge':
                await install_cf_frame(page)
            viewport = page.viewport_size
            point = {'x': 115, 'y': 115} if case not in {'purchase', 'no_challenge'} else {'x': 550, 'y': 320}

            class Provider:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_):
                    pass

                async def post(self, *args, **kwargs):
                    calls.append(args)
                    if case == 'stale_node':
                        await page.evaluate("cf.replaceWith(cf.cloneNode()); void 0")
                        await page.frame_locator('#cf').locator('input').wait_for()
                    if case == 'reloaded':
                        frame = next(frame for frame in page.frames if frame.url == CF_SOURCE)
                        await frame.goto(CF_SOURCE)
                    if case == 'moved':
                        await page.evaluate("cf.style.left='101px'")
                    if case == 'overlay':
                        await page.evaluate("buy.style='position:fixed;left:100px;top:100px;width:300px;height:70px;z-index:999'")
                    state = 'verified' if len(calls) > 1 and case == 'valid' else 'unchecked'
                    if state == 'verified':
                        await page.evaluate("""() => {
                            document.querySelector('#cf').style.display='none';
                            if(!document.querySelector('[name="cf-turnstile-response"]')) {
                                const token=document.createElement('input'); token.type='hidden';
                                token.name='cf-turnstile-response'; token.value='token'; document.body.append(token);
                            }
                        }""")
                    return SimpleNamespace(status_code=200, json=lambda: {'state': state, 'confidence': 1,
                        'checkbox_center_pct': {key: point[key] / viewport['width' if key == 'x' else 'height'] * 100
                            for key in ('x', 'y')} if state == 'unchecked' else None})

            monkeypatch.setattr(executor.httpx, 'AsyncClient', lambda **_: Provider())
            if case == 'valid':
                # Stop after the guarded click; completion polling isn't the click authority under test.
                async def stop_poll(*args):
                    return False
                monkeypatch.setattr(executor, '_sleep', stop_poll)
            response = await browser.execute(sid, request('challenge.turnstile', {'wait_seconds': 5}, origin,
                {'cfbox': {'url': 'http://private-cfbox.invalid', 'api_key': 'CFPrivate991'}}))
            assert response['status'] != 'success', response
            assert await page.locator('body').get_attribute('data-purchased') is None
            if case in {'no_challenge', 'untrusted'}:
                assert calls == []
            else:
                assert len(calls) == 1
            if case == 'valid':
                await page.wait_for_function("document.body.dataset.challengeClicked === 'yes'")
                assert await page.locator('body').get_attribute('data-challenge-clicked') == 'yes'
            else:
                assert await page.locator('body').get_attribute('data-challenge-clicked') is None
            assert 'CFPrivate991' not in json.dumps(response)
    asyncio.run(run())
