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

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                self.model_requests.append(body)
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
