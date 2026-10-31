"""Executed inside the isolated worker by test_demo_e2e, never on live profiles."""
import asyncio
import io
import json
import shutil
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

from worker.browser_backend import ManagerBackend
from worker.app import _inject_picker
from worker.executor import _goto_if_needed, _run_step
from cloakbrowser.human.engine import Human
from cloakbrowser.human.patch import original
from playwright import async_api


async def main():
    async def forbidden_read(*args, **kwargs):
        raise AssertionError("Observation entered SDK humanized read pipeline")
    Human.read = forbidden_read
    actions = []
    human_click, human_fill = Human.click, Human.fill
    async def click(self, *args, **kwargs):
        actions.append("click")
        return await human_click(self, *args, **kwargs)
    async def fill(self, *args, **kwargs):
        actions.append("fill")
        return await human_fill(self, *args, **kwargs)
    Human.click, Human.fill = click, fill
    assert shutil.which("Xvnc") is None
    assert not Path("/root/.cloakbrowser").exists()
    backend = ManagerBackend(Path("/tmp/remote-smoke"))
    session = None
    try:
        session = await backend.start("desktop-smoke", None)
        profile_id = session.profile_id
        page = await session.context.new_page()
        await page.goto("about:blank")
        await page.evaluate("""() => {
            document.body.innerHTML = '<input id="input"><button id="click">Test</button>';
            const button = document.querySelector('button');
            button.style = 'position:fixed;left:100px;top:100px;width:200px;height:100px;background:rgb(255,0,255);border:0';
            button.onclick = () => document.body.dataset.clicked = 'yes';
        }""")
        await page.bring_to_front()
        await page.locator("#input").fill("remote")
        assert await page.locator("#input").input_value() == "remote"
        assert await page.input_value("#input") == "remote"
        assert await page.main_frame.input_value("#input") == "remote"
        handle = await page.query_selector("#input")
        assert await handle.input_value() == "remote"
        assert await handle.get_attribute("id") == "input"
        for cls in (async_api.Page, async_api.Frame, async_api.Locator, async_api.ElementHandle):
            for name in ("input_value", "text_content", "inner_text", "inner_html", "get_attribute"):
                assert getattr(cls, name) is original(cls, name)
            assert cls.click is not original(cls, "click")
        await page.evaluate("""() => {
            const frame = document.createElement('iframe');
            frame.srcdoc = '<body><span id="child">Child text</span></body>';
            document.body.append(frame);
            const hidden = document.createElement('span');
            hidden.id = 'hidden'; hidden.hidden = true; hidden.textContent = 'Hidden text';
            document.body.append(hidden);
        }""")
        assert await page.frame_locator("iframe").locator("#child").inner_text(timeout=2000) == "Child text"
        assert await page.locator("#hidden").text_content() == "Hidden text"
        await page.locator("#hidden").wait_for(state="hidden", timeout=1000)
        assert not await page.locator("#hidden").is_visible()
        assert await page.locator("#hidden").count() == 1
        try:
            await page.locator("#missing").inner_text(timeout=100)
        except async_api.TimeoutError:
            pass
        else:
            raise AssertionError("Native read did not honor its timeout")
        async with page.expect_popup() as opened:
            await page.evaluate("window.open('about:blank')")
        popup = await opened.value
        await popup.evaluate("document.body.textContent = 'Popup text'")
        assert await popup.locator("body").inner_text() == "Popup text"
        await popup.close()
        await page.bring_to_front()
        # CDP focus completes before the X11 framebuffer has necessarily repainted.
        await asyncio.sleep(.5)
        png = await session.desktop.screenshot()
        image = Image.open(io.BytesIO(png)).convert("RGB")
        assert image.size == (1920, 1080)
        target = next((x, y) for y in range(image.height) for x in range(image.width)
                      if image.getpixel((x, y)) == (255, 0, 255))
        await session.desktop.click(target[0] + 20, target[1] + 20)
        await page.wait_for_function("document.body.dataset.clicked === 'yes'")
        await _inject_picker(page)
        await page.keyboard.down("Alt")
        try:
            await page.locator("#click").click()
        finally:
            await page.keyboard.up("Alt")
        picked = await page.evaluate("window.__siteflowPicker.picked")
        assert picked["selector"] == "#click"
        assert "fill" in actions and "click" in actions
        # A curved path from above crosses a navigation hover menu, which then
        # covers the intended link. Recovery must close it without clicking it.
        await page.evaluate("""() => {
            document.body.innerHTML = `<style>
                body { margin:0; min-height:1500px }
                #nav { position:absolute; left:610px; top:135px; width:85px;
                    height:27px; z-index:2; background:gray }
                #menu { display:none; position:absolute; top:27px; width:85px;
                    height:300px; background:yellow }
                #nav:hover #menu { display:block }
                #attendance { position:absolute; left:620px; top:185px;
                    width:65px; height:20px }
            </style><div id="nav">Dropdown<div id="menu">Menu</div></div>
            <a id="attendance" href="attendance.php">Sign in</a>`;
            window.hoverEntries = 0;
            window.pressedTargets = [];
            document.querySelector('#nav').onmouseenter = () => hoverEntries++;
            document.onmousedown = e => pressedTargets.push(e.target.id);
            document.querySelector('a').onclick = e => e.preventDefault();
        }""")
        human = page._impl_obj._cloak_human
        human.cursor.x, human.cursor.y, human.cursor.initialized = 650, 50, True
        await human.mouse.move(650, 50)
        await page.locator("#attendance").click(timeout=10000)
        assert await page.evaluate("hoverEntries > 0")
        assert await page.evaluate("pressedTargets") == ["attendance"]
        # A permanent overlay must still time out, never receive a forced click.
        await page.evaluate("""() => {
            const blocker = document.createElement('div');
            blocker.id = 'blocker';
            blocker.style = 'position:fixed;inset:0;z-index:10;background:white';
            document.body.append(blocker);
            pressedTargets = [];
        }""")
        try:
            await page.locator("#attendance").click(timeout=500)
        except async_api.TimeoutError:
            pass
        else:
            raise AssertionError("Permanent overlay should prevent clicking")
        assert await page.evaluate("pressedTargets") == []
        # Opening another task tab during input must wait for the full fill;
        # the next fill must foreground its own page rather than stall in CDP.
        await page.evaluate("""() => {
            document.body.innerHTML = '<input id="username"><input id="password" type="password"><button id="submit">Login</button>';
            window.accepted = false;
            document.querySelector('#submit').onclick = () => {
                accepted = document.querySelector('#username').value === 'fixture-user'
                    && document.querySelector('#password').value === 'Fixture-password-217!';
            };
        }""")
        gate = session.context._impl_obj._siteflow_input_guard
        filling = asyncio.create_task(page.locator("#username").fill("fixture-user"))
        for _ in range(100):
            if gate.owner is filling:
                break
            await asyncio.sleep(.01)
        assert gate.owner is filling
        other_opening = asyncio.create_task(session.context.new_page())
        await asyncio.sleep(.05)
        assert not other_opening.done()
        await asyncio.wait_for(filling, 15)
        other = await other_opening
        await other.goto("about:blank")
        await other.bring_to_front()
        await asyncio.wait_for(page.locator("#password").fill("Fixture-password-217!"), 15)
        await page.locator("#submit").click()
        assert await page.evaluate("accepted")
        await other.close()
        print("PASS concurrent tab creation waits for input; background login fill foregrounds safely")
        print("PASS hover-menu recovery clicks only the target; permanent overlays still block")
        class CacheFixture(BaseHTTPRequestHandler):
            stopped = True
            requests = 0
            model_requests = []
            grid_calls = 0

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                self.model_requests.append(body)
                if body.get('tools'):
                    type(self).grid_calls += 1
                    result = {'is_clear': self.grid_calls > 1,
                              'selected_indices': ([1, 5, 9] if self.grid_calls > 2 else [2]) if self.grid_calls > 1 else [],
                              'need_refresh': self.grid_calls == 1}
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({'choices': [{'message': {'tool_calls': [{
                        'type': 'function', 'function': {'name': 'submit_geetest_result',
                        'arguments': json.dumps(result)},
                    }]}, 'finish_reason': 'tool_calls'}]}).encode())
                    return
                content = body['messages'][1]['content']
                is_image = any(part['type'] == 'image_url' for part in content)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'choices': [{'message': {
                    'content': 'False' if is_image else 'True'}, 'finish_reason': 'stop'}]}).encode())

            def do_GET(self):
                if self.path != "/":
                    self.send_error(404)
                    return
                type(self).requests += 1
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                self.wfile.write(b"<title>The website has been stopped</title>" if self.stopped
                                 else b"<title>Recovered</title>Welcome")

            def log_message(self, *_args):
                pass

        fixture = ThreadingHTTPServer(("127.0.0.1", 0), CacheFixture)
        thread = threading.Thread(target=fixture.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{fixture.server_port}/"
            await page.goto(url)
            CacheFixture.stopped = False
            await page.goto("about:blank")
            await page.goto(url)
            assert "stopped" in await page.title()
            assert CacheFixture.requests == 1, "Expected cached HTTP-200 error page"
            messages = []
            await _goto_if_needed(page, url, "domcontentloaded",
                                  lambda level, message: messages.append(message), "page",
                                  retry_count=1, retry_interval_seconds=0, check_error_page=True)
            assert await page.title() == "Recovered"
            assert CacheFixture.requests == 2
            assert any("禁用" in message for message in messages)
            await page.goto("about:blank")
            await page.goto(url)
            assert CacheFixture.requests == 2, "Cache must be restored after recovery"
            print("PASS cached HTTP-200 error page recovers within retry budget; cache restored")
            for visual in (True, False):
                status, _, data = await _run_step(page, {
                    'feature_type': 'flow.result', 'config': {'output_value': True, 'llm_enabled': True},
                }, None, None, None, None, None, 'general', vision={
                    'base_url': url.rstrip('/'), 'model': 'fixture', 'api_key': 'fixture-token',
                    'supports_vision': True, 'execution_settings': {
                        'llm_enabled': True, 'use_vision': visual, 'judgment_scope': 'all',
                    },
                })
                assert status == 'success'
                assert data['value'] is (not visual)
            image_content = CacheFixture.model_requests[0]['messages'][1]['content']
            assert 'Welcome' not in image_content[0]['text']
            assert image_content[1]['image_url']['url'].startswith('data:image/png;base64,')
            text_content = CacheFixture.model_requests[1]['messages'][1]['content']
            assert len(text_content) == 1 and 'Welcome' in text_content[0]['text']
            print('PASS result judgement with real page screenshot/text and isolated model HTTP responses')
            from worker import secret_client
            import re
            await page.evaluate('''() => {
                document.body.innerHTML = '<input id="trusted-user"><input id="trusted-pass" type="password"><button id="trusted-submit">Login</button>';
                window.trustedSubmitted = false;
                document.querySelector('button').onclick=()=>trustedSubmitted=true;
            }''')
            secret_calls = []
            async def fixture_secret(*args):
                secret_calls.append(True)
                return {'username': 'fixture-user', 'password': 'fixture-password'}
            original_secret = secret_client.resolve_secret
            secret_client.resolve_secret = fixture_secret
            try:
                login = {'feature_type': 'auth.form_login', 'step_key': 'trusted-login', 'config': {
                    'username_selector': ['#trusted-user'], 'password_selector': ['#trusted-pass'],
                    'submit_selector': ['#trusted-submit'],
                }}
                scope = {'base_url': 'https://unrelated.example', 'trusted_urls': []}
                try:
                    await _run_step(page, login, None, None, None, None, None, 'general', secret_payload=scope)
                except RuntimeError as exc:
                    assert 'Secret origin denied' in str(exc)
                else:
                    raise AssertionError('Cross-origin credentials were not blocked')
                assert not secret_calls
                scope['trusted_urls'] = [re.escape(url)]
                status, _, _ = await _run_step(page, login, None, None, None, None, None,
                                                'general', secret_payload=scope)
                assert status == 'success' and len(secret_calls) == 1
                assert await page.evaluate('trustedSubmitted')
                print('PASS trusted URL full-match allows explicit cross-origin login; default remains denied')
            finally:
                secret_client.resolve_secret = original_secret
            await page.evaluate('''() => {
                document.body.innerHTML = `<style>
                .geetest_panel_box {width:330px;padding:12px;margin:20px;background:white}
                .grid {display:grid;grid-template-columns:repeat(3,100px);gap:5px}
                .geetest_item {width:100px;height:70px;background:lightblue;cursor:pointer}
                .geetest_selected {outline:3px solid green}
                </style><div class="geetest_panel_box">
                <div class="target">Target: cat<img id="target-image" width="1" height="1"
                    src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7#first"></div><div class="grid"></div>
                <button class="geetest_refresh">Refresh</button>
                <button class="geetest_commit">Submit</button></div><p id="status"></p>`;
                window.submissions = 0; window.refreshes = 0;
                // Two equal-sized sibling loading layers cannot be identified
                // by the humanizer's ElementHandle bounding-box lookup.
                const loading = document.createElement('div');
                loading.className = 'geetest_panel_box';
                loading.style = 'position:absolute;left:500px;top:20px;width:200px;height:200px';
                const cover = document.createElement('div'); cover.style=loading.style.cssText;
                document.body.prepend(cover);document.body.prepend(loading);
                setTimeout(()=>{loading.remove();cover.remove()},1500);
                const grid = document.querySelector('.grid');
                for(let n=1;n<=9;n++) {
                    const cell=document.createElement('div');cell.className='geetest_item';
                    cell.textContent=[1,5,9].includes(n)?'cat':'dog';
                    cell.onclick=()=>cell.classList.toggle('geetest_selected');grid.append(cell);
                }
                document.querySelector('.geetest_refresh').onclick=()=>{
                    refreshes++; document.querySelector('.target').textContent='Target: cat (new)';
                    const image=document.createElement('img');image.width=1;image.height=1;
                    image.src='data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7#refresh';
                    document.querySelector('.target').append(image);
                };
                document.querySelector('.geetest_commit').onclick=()=>{
                    submissions++;
                    const selected=Array.from(document.querySelectorAll('.geetest_item'))
                        .flatMap((el,i)=>el.classList.contains('geetest_selected')?[i+1]:[]);
                    if(JSON.stringify(selected)==='[1,5,9]') document.querySelector('#status').textContent='Grid accepted';
                    else {
                        document.querySelectorAll('.geetest_selected').forEach(el=>el.classList.remove('geetest_selected'));
                        document.querySelector('.target img').src += '-rejected';
                    }
                };
            }''')
            grid_images = []
            async def capture_grid(image, caption):
                grid_images.append(image)
            status, message, data = await _run_step(page, {
                'feature_type': 'challenge.grid_captcha', 'config': {
                    'success_signals': ['Grid accepted'], 'max_refresh': 2,
                },
            }, None, None, None, None, None, 'general', vision={
                'base_url': url.rstrip('/'), 'model': 'fixture', 'api_key': 'fixture-token',
                'supports_vision': True,
            }, capture_image=capture_grid)
            assert status == 'success', (status, message, data)
            assert data['selected_indices'] == [1, 5, 9]
            assert len(grid_images) == 3 and CacheFixture.grid_calls == 3
            assert data['verification_attempts'] == 3 and data['retries'] == 2
            assert await page.evaluate('({submissions,refreshes})') == {'submissions': 2, 'refreshes': 1}
            print('PASS grid CAPTCHA: loading stability, unclear refresh, rejected answer regeneration and successful retry')
        finally:
            fixture.shutdown()
            fixture.server_close()
            thread.join()
        await page.close()
        await backend.stop(session)
        session = await backend.start("desktop-smoke", None)
        assert session.profile_id == profile_id
        assert session.context.pages
        await session.context.add_cookies([{"name": "persistent", "value": "fixture", "domain": "example.test", "path": "/", "expires": time.time() + 3600}])
        await backend.stop(session)
        session = None
        await backend.close()
        backend = ManagerBackend(Path("/tmp/remote-smoke"))
        session = await backend.start("desktop-smoke", None)
        assert session.profile_id == profile_id
        assert any(cookie["name"] == "persistent" for cookie in await session.context.cookies())
        # Simulate lost worker state without stopping the remotely running profile.
        await session.browser.close()
        session = None
        await backend.close()
        backend = ManagerBackend(Path("/tmp/remote-smoke"))
        await backend.prepare()
        status = await backend.request("GET", f"/api/profiles/{profile_id}/status")
        assert status["status"] == "stopped"
        profile = await backend.request("GET", f"/api/profiles/{profile_id}")
        assert profile["auto_launch"] is False
        session = await backend.start("desktop-smoke", None)
        assert session.profile_id == profile_id
        print("PASS remote CDP, humanized input, shared-X11 desktop PNG/click, profile reuse; no bundled browser")
    finally:
        if session is not None:
            await backend.stop(session)
        await backend.close()


asyncio.run(main())
