"""Run CF probes against Chromium DOM, without contacting real sites or CFbox."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from playwright.async_api import async_playwright

from .test_worker_turnstile import fake_clock  # noqa: F401
from worker import executor


CHROMIUM = Path(
    "/opt/opencode/.cache/ms-playwright/chromium_headless_shell-1223/"
    "chrome-headless-shell-linux64/chrome-headless-shell"
)
CF_SCRIPT = (
    '<script data-cf-turnstile '
    'src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit"></script>'
)
CF_FRAME = "https://challenges.cloudflare.com/turnstile/widget"
SITE_CAP = (
    CF_SCRIPT + '<div class="site-cap" role="dialog" aria-label="\u4eba\u673a\u9a8c\u8bc1">'
    '<div class="ts-box" style="width:300px;height:65px">'
    '<input type="checkbox"> <span>Verification</span></div></div>'
)


@asynccontextmanager
async def dom_page(html="", documents=None):
    if not CHROMIUM.is_file():
        pytest.skip("Local Chromium headless shell not installed")
    documents = documents or {}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=str(CHROMIUM), args=["--no-sandbox"])
        try:
            page = await browser.new_page(service_workers="block")

            async def route(request_route):
                url = request_route.request.url
                is_script = request_route.request.resource_type == "script"
                await request_route.fulfill(
                    content_type="application/javascript" if is_script else "text/html",
                    body="/* local mock */" if is_script else documents.get(url, "<body>Normal frame</body>"),
                )

            await page.route("**/*", route)
            await page.goto("https://site.test/")
            await page.set_content(html)
            yield page
        finally:
            await browser.close()


@pytest.mark.parametrize("html,expected", [
    ('<div class="cf-turnstile" style="width:300px;height:65px"></div>', True),
    ('<section id="cf-chl-widget-123" style="width:300px;height:65px"></section>', True),
    ('<span id="cf-chl-widget-inline" style="display:inline-block;width:300px;height:65px"></span>', True),
    (SITE_CAP, True),
    ('<div class="site-cap" role="dialog" aria-label="\u4eba\u673a\u9a8c\u8bc1">'
     '<div class="ts-box" style="width:300px;height:65px"></div></div>', False),
    (CF_SCRIPT + '<div class="site-cap" role="dialog">Other captcha</div>', False),
    (CF_SCRIPT + '<div class="ts-box" hidden>Hidden challenge</div>', False),
    (CF_SCRIPT, False),
    ('<script data-cf-turnstile src="https://untrusted.test/turnstile/v0/api.js"></script>'
     '<div class="ts-box" style="width:300px;height:65px"></div>', False),
    ('<script src="https://challenges.cloudflare.com.evil.test/turnstile/v0/api.js"></script>'
     '<div class="ts-box" style="width:300px;height:65px"></div>', False),
    ('<input type="hidden" id="cf-chl-widget-response" name="cf-turnstile-response" value="secret">', False),
    ('<textarea id="cf-chl-widget-response" name="cf-turnstile-response">secret</textarea>', False),
    ('<div style="opacity:0"><div class="cf-turnstile" style="width:300px;height:65px"></div></div>', False),
    ('<div class="cf-turnstile" style="width:0;height:0">'
     f'<iframe src="{CF_FRAME}" style="width:300px;height:65px"></iframe></div>', True),
])
def test_dom_marker_variants(html, expected):
    async def run():
        async with dom_page(html) as page:
            assert await page.evaluate(executor.CF_VISIBLE_MARKER_SCRIPT) is expected
            state = await executor._cf_page_state(page)
            assert state["marker"] is expected
            assert "urls" not in state
            assert "secret" not in json.dumps(state)

    asyncio.run(run())


@pytest.mark.parametrize("script_url,expected", [
    ("https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit", True),
    ("//challenges.cloudflare.com/turnstile/v0/api.js", True),
    ("https://CHALLENGES.CLOUDFLARE.COM/turnstile/v0/api.js", True),
    ("http://challenges.cloudflare.com/turnstile/v0/api.js", False),
    ("https://challenges.cloudflare.com.evil.test/turnstile/v0/api.js", False),
    ("https://challenges.cloudflare.com@evil.test/turnstile/v0/api.js", False),
    ("https://evil.test/?src=https://challenges.cloudflare.com/turnstile/v0/api.js", False),
    ("https://challenges.cloudflare.com/not-turnstile/api.js", False),
    ("https://challenges.cloudflare.com/turnstile-fake/api.js", False),
])
def test_open_shadow_ts_box_requires_trusted_script(script_url, expected):
    async def run():
        async with dom_page('<div id="host"></div>') as page:
            await page.evaluate("""(url) => {
                const root = document.querySelector('#host').attachShadow({mode: 'open'});
                root.innerHTML = '<div class="ts-box" style="width:300px;height:65px"></div>';
                const script = document.createElement('script');
                script.src = url;
                root.append(script);
            }""", script_url)
            assert await page.evaluate(executor.CF_VISIBLE_MARKER_SCRIPT) is expected
            state = await executor._cf_page_state(page)
            assert state["marker"] is expected
            assert state["dom_marker"] is expected
            assert state["text_readable"] is True

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["open", "closed"])
def test_site_cap_wrapper_detects_widget_with_inaccessible_internals(mode):
    async def run():
        async with dom_page(SITE_CAP) as page:
            await page.evaluate("""(mode) => {
                const box = document.querySelector('.ts-box');
                box.replaceChildren();
                const root = box.attachShadow({mode});
                root.innerHTML = '<label><input type="checkbox">Verification</label>';
            }""", mode)
            assert await page.evaluate("""() =>
                document.querySelector('.ts-box').shadowRoot !== null""") is (mode == "open")
            assert len(page.frames) == 1
            assert await page.evaluate(executor.CF_VISIBLE_MARKER_SCRIPT) is True
            state = await executor._cf_page_state(page)
            assert state["marker"] is True
            assert state["dom_marker"] is True
            assert state["frame_challenge"] is False
            assert state["token_length"] == 0

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["open", "closed"])
@pytest.mark.parametrize("hidden_style", ["", "display:none", "visibility:hidden", "opacity:0"])
@pytest.mark.parametrize("actual_url_only", [False, True])
def test_shadow_frame_actual_url_is_marker(mode, hidden_style, actual_url_only):
    async def run():
        async with dom_page('<div id="host"></div>') as page:
            await page.evaluate("""({mode, url}) => {
                const root = document.querySelector('#host').attachShadow({mode});
                const nested = document.createElement('div');
                root.append(nested);
                const inner = nested.attachShadow({mode});
                const frame = document.createElement('iframe');
                frame.src = url;
                inner.append(frame);
            }""", {"mode": mode, "url": "about:blank" if actual_url_only else CF_FRAME})
            if actual_url_only:
                await page.frames[1].goto(CF_FRAME)
                element = await page.frames[1].frame_element()
                try:
                    assert await element.get_attribute("src") == "about:blank"
                finally:
                    await element.dispose()
            elif not any(frame.url == CF_FRAME for frame in page.frames):
                await page.wait_for_event("framenavigated", predicate=lambda frame: frame.url == CF_FRAME)
            if hidden_style:
                await page.locator("#host").evaluate("(el, style) => el.style.cssText = style", hidden_style)
            state = await executor._cf_page_state(page)
            assert len(page.frames) == 2
            assert page.frames[1].url == CF_FRAME
            dom_marker = mode == "open" and not hidden_style and not actual_url_only
            assert await page.evaluate(executor.CF_VISIBLE_MARKER_SCRIPT) is dom_marker
            # Playwright can inspect the frame host even when page JS cannot enter its root.
            assert state["marker"] is (not hidden_style)
            assert state["dom_marker"] is dom_marker
            assert state["frame_challenge"] is (not hidden_style)
            assert state["frames_checked"] == (0 if hidden_style else 1)
            assert state["frames_hidden"] == (1 if hidden_style else 0)
            assert state["frames_unreadable"] == 0
            assert state["text_readable"] is True
            assert state["token_readable"] is True

    asyncio.run(run())


def test_open_shadow_component_and_token_length():
    async def run():
        async with dom_page(CF_SCRIPT + '<div id="host"></div>') as page:
            await page.evaluate("""() => {
                const root = document.querySelector('#host').attachShadow({mode: 'open'});
                root.innerHTML = '<div class="ts-box" style="width:300px;height:65px"></div>'
                    + '<input type="hidden" name="cf-turnstile-response" value="never-log-this-token">';
            }""")
            state = await executor._cf_page_state(page)
            assert state["marker"] is True
            assert state["token_length"] == len("never-log-this-token")
            assert state["token_readable"] is True
            assert "never-log" not in json.dumps(state)

    asyncio.run(run())


@pytest.mark.parametrize("hidden_style", [
    "", "display:none", "visibility:hidden", "opacity:0", "content-visibility:hidden",
])
def test_open_shadow_marker_respects_composed_hidden_ancestors(hidden_style):
    async def run():
        async with dom_page(CF_SCRIPT + '<div id="host"></div>') as page:
            await page.evaluate("""(style) => {
                const host = document.querySelector('#host');
                host.style.cssText = style;
                const root = host.attachShadow({mode: 'open'});
                const nested = document.createElement('div');
                root.append(nested);
                nested.attachShadow({mode: 'open'}).innerHTML =
                    '<section class="ts-box" style="width:300px;height:65px"></section>';
            }""", hidden_style)
            assert await page.evaluate(executor.CF_VISIBLE_MARKER_SCRIPT) is (not hidden_style)
            state = await executor._cf_page_state(page)
            assert state["marker"] is (not hidden_style)

    asyncio.run(run())


@pytest.mark.parametrize("ancestor_visibility", ["hidden", "collapse"])
@pytest.mark.parametrize("component", ["widget", "iframe"])
def test_visible_child_overrides_ancestor_visibility(ancestor_visibility, component):
    async def run():
        child = (
            '<section class="cf-turnstile" style="visibility:visible;width:300px;height:65px"></section>'
            if component == "widget" else
            '<iframe src="about:blank" style="visibility:visible;width:300px;height:65px"></iframe>'
        )
        async with dom_page(f'<div style="visibility:{ancestor_visibility}">{child}</div>') as page:
            if component == "iframe":
                await page.frames[1].goto(CF_FRAME)
            element = page.locator("section" if component == "widget" else "iframe")
            assert await element.evaluate("el => getComputedStyle(el).visibility") == "visible"
            assert await element.evaluate(executor.CF_ELEMENT_VISIBLE_SCRIPT) is True
            assert await page.evaluate(executor.CF_VISIBLE_MARKER_SCRIPT) is (component == "widget")
            state = await executor._cf_page_state(page)
            assert state["marker"] is True
            assert state["marker_readable"] is True
            assert state["frame_challenge"] is (component == "iframe")
            assert state["frames_hidden"] == 0
            assert state["frames_unreadable"] == 0

    asyncio.run(run())


@pytest.mark.parametrize("opacity", [0, 1])
@pytest.mark.parametrize("component", ["widget", "iframe"])
def test_slotted_component_respects_slot_ancestor_opacity(opacity, component):
    async def run():
        child = (
            '<section slot="challenge" class="cf-turnstile" style="width:300px;height:65px"></section>'
            if component == "widget" else
            f'<iframe slot="challenge" src="{CF_FRAME}" style="width:300px;height:65px"></iframe>'
        )
        async with dom_page(f'<div id="host">{child}</div>') as page:
            await page.evaluate("""(opacity) => {
                const root = document.querySelector('#host').attachShadow({mode: 'open'});
                root.innerHTML = `<div style="opacity:${opacity}"><slot name="challenge"></slot></div>`;
            }""", opacity)
            element = page.locator("section" if component == "widget" else "iframe")
            assert await element.evaluate("el => el.assignedSlot.name") == "challenge"
            assert await element.evaluate("el => el.getBoundingClientRect().width > 0") is True
            assert await element.evaluate(executor.CF_ELEMENT_VISIBLE_SCRIPT) is bool(opacity)
            assert await page.evaluate(executor.CF_VISIBLE_MARKER_SCRIPT) is bool(opacity)
            state = await executor._cf_page_state(page)
            assert state["marker"] is bool(opacity)
            assert state["marker_readable"] is True
            assert state["frame_challenge"] is (component == "iframe" and bool(opacity))
            assert state["frames_hidden"] == (1 if component == "iframe" and not opacity else 0)
            assert state["frames_unreadable"] == 0

    asyncio.run(run())


@pytest.mark.parametrize("hidden_style", ["", "display:none", "visibility:hidden", "opacity:0"])
@pytest.mark.parametrize("inner", ["https://inner.test/frame", CF_FRAME])
def test_nested_cross_origin_frame_marker_and_hidden_parent(hidden_style, inner):
    outer = "https://outer.test/frame"

    async def run():
        documents = {
            outer: f'<body>Outer<iframe src="{inner}"></iframe></body>',
            inner: '<body><section class="cf-turnstile" style="width:300px;height:65px"></section>'
                   '<input type="hidden" name="cf-turnstile-response" value="child-token"></body>',
        }
        async with dom_page(f'<div id="ancestor"><iframe src="{outer}"></iframe></div>', documents) as page:
            # set_content waits for these locally fulfilled nested documents to load.
            assert len(page.frames) == 3
            assert page.frames[-1].url == inner
            if hidden_style:
                await page.locator("#ancestor").evaluate("(el, style) => el.style.cssText = style", hidden_style)
            state = await executor._cf_page_state(page)
            assert state["marker"] is (not hidden_style)
            assert state["text_readable"] is True
            assert state["frame_challenge"] is (inner == CF_FRAME and not hidden_style)
            assert state["frames_checked"] == (0 if hidden_style else 2)
            assert state["frames_hidden"] == (2 if hidden_style else 0)
            assert state["frames_unreadable"] == 0
            assert state["token_readable"] is True
            assert state["token_length"] == (0 if hidden_style else len("child-token"))
            assert "child-token" not in json.dumps(state)

    asyncio.run(run())


@pytest.mark.parametrize("hidden", [False, True])
def test_actual_frame_url_detected_even_with_blank_src(hidden):
    async def run():
        async with dom_page('<iframe src="about:blank"></iframe>') as page:
            frame = page.frames[1]
            await frame.goto(CF_FRAME)
            assert await page.locator("iframe").get_attribute("src") == "about:blank"
            assert await page.evaluate(executor.CF_VISIBLE_MARKER_SCRIPT) is False
            if hidden:
                await page.locator("iframe").evaluate("el => el.style.visibility = 'hidden'")
            state = await executor._cf_page_state(page)
            assert state["marker"] is (not hidden)
            assert state["frame_challenge"] is (not hidden)

    asyncio.run(run())


@pytest.mark.parametrize("existing_marker", [False, True])
@pytest.mark.parametrize("failure", ["marker", "text", "visibility", "token"])
@pytest.mark.parametrize("failure_mode", ["raise", "null"])
def test_unreadable_visible_frame_never_proves_clear(existing_marker, failure, failure_mode):
    class Proxy:
        def __init__(self, target):
            self.target = target

        def __getattr__(self, name):
            return getattr(self.target, name)

    class Frame(Proxy):
        async def frame_element(self):
            if failure == "visibility" and failure_mode == "raise":
                raise RuntimeError("Frame host unavailable")
            element = await self.target.frame_element()
            if failure == "visibility":
                class Element(Proxy):
                    async def evaluate(self, script):
                        return None

                return Element(element)
            return element

        async def evaluate(self, script):
            if script == {
                "marker": executor.CF_VISIBLE_MARKER_SCRIPT,
                "text": executor.CF_PAGE_TEXT_SCRIPT,
                "token": executor.CF_TOKEN_LENGTH_SCRIPT,
            }.get(failure):
                if failure_mode == "null":
                    return None
                raise RuntimeError("Cross origin frame detached")
            return await self.target.evaluate(script)

    class Page(Proxy):
        @property
        def frames(self):
            return [self.target.main_frame, Frame(self.target.frames[1])]

    async def run():
        html = '<iframe src="https://child.test/frame"></iframe>'
        if existing_marker:
            html += '<div class="cf-turnstile" style="width:300px;height:65px"></div>'
        async with dom_page(html) as page:
            state = await executor._cf_page_state(Page(page))
            assert state["marker_readable"] is (failure not in {"marker", "visibility"})
            if failure == "text":
                assert state["text_readable"] is False
            elif failure == "token":
                assert state["token_readable"] is False
                assert state["token_length"] == 0
            else:
                assert state["marker"] is (True if existing_marker else None)
            if failure != "token":
                assert not (state["marker"] is False and state["text_readable"])
            if failure == "visibility":
                assert state["frames_unreadable"] == 1
                assert state["token_readable"] is False
                assert state["text_readable"] is False

    asyncio.run(run())


def test_bounded_shadow_scan_reports_unreadable():
    async def run():
        async with dom_page() as page:
            await page.evaluate("""() => {
                for (let index = 0; index < 257; index++) {
                    const host = document.createElement('div');
                    document.body.append(host);
                    host.attachShadow({mode: 'open'});
                }
            }""")
            state = await executor._cf_page_state(page)
            assert state["marker"] is None
            assert state["marker_readable"] is False
            assert state["token_readable"] is False

    asyncio.run(run())


@pytest.mark.parametrize("existing_marker", [False, True])
@pytest.mark.parametrize("element_count", [20000, 20001])
def test_element_scan_limit_cannot_hide_pending_shadow_root(existing_marker, element_count):
    async def run():
        async with dom_page('<div id="host"></div>') as page:
            await page.evaluate("""({existingMarker, elementCount}) => {
                const host = document.querySelector('#host');
                host.attachShadow({mode: 'open'}).innerHTML =
                    '<div class="cf-turnstile" style="width:300px;height:65px"></div>'
                    + '<input name="cf-turnstile-response" value="never-log-boundary-token">';
                if (existingMarker) host.className = 'cf-turnstile';
                host.style.cssText = 'width:300px;height:65px';
                const fragment = document.createDocumentFragment();
                const remaining = elementCount - document.querySelectorAll('*').length;
                for (let index = 0; index < remaining; index++) {
                    fragment.append(document.createElement('span'));
                }
                document.body.append(fragment);
            }""", {"existingMarker": existing_marker, "elementCount": element_count})
            assert await page.evaluate("document.querySelectorAll('*').length") == element_count
            state = await executor._cf_page_state(page)
            assert state["marker"] is (True if existing_marker else None)
            assert state["marker_readable"] is (existing_marker is True)
            assert state["token_readable"] is False
            assert "never-log" not in json.dumps(state)

    asyncio.run(run())


@pytest.fixture
def cfbox_mock(monkeypatch):
    calls, screenshots = [], []
    predictions = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            index = len(calls)
            prediction = predictions[min(index, len(predictions) - 1)]
            calls.append(prediction["state"])
            screenshots.append(kwargs["files"]["image"][1])
            return httpx.Response(200, json=prediction)

    monkeypatch.setattr(executor.httpx, "AsyncClient", Client)

    def configure(responses):
        predictions[:] = responses
        calls.clear()
        screenshots.clear()
        return calls, screenshots

    return configure


@pytest.mark.parametrize("failure", [
    "none", "main_marker", "frame_marker", "main_token", "frame_token",
    "frames", "visibility", "frame_url",
])
@pytest.mark.parametrize("failure_mode", ["raise", "null"])
def test_verified_old_token_requires_readable_marker_and_token(cfbox_mock, fake_clock, failure, failure_mode):
    calls, screenshots = cfbox_mock([{"state": "verified"}])

    def fail():
        if failure_mode == "raise":
            raise RuntimeError("CF probe unavailable")
        return None

    class Proxy:
        def __init__(self, target):
            self.target = target

        def __getattr__(self, name):
            return getattr(self.target, name)

        async def evaluate(self, script):
            scope = "main" if isinstance(self, Page) else "frame"
            if (failure == scope + "_marker" and script == executor.CF_VISIBLE_MARKER_SCRIPT
                    or failure == scope + "_token" and script == executor.CF_TOKEN_LENGTH_SCRIPT):
                return fail()
            return await self.target.evaluate(script)

    class Frame(Proxy):
        @property
        def url(self):
            if failure == "frame_url":
                # A missing URL is represented by a property read failure, not a URL string.
                raise RuntimeError("Frame URL unavailable")
            return self.target.url

        async def frame_element(self):
            element = await self.target.frame_element()
            if failure == "visibility":
                class Element(Proxy):
                    async def evaluate(self, script):
                        return fail()

                return Element(element)
            return element

    class Page(Proxy):
        @property
        def frames(self):
            if failure == "frames":
                return fail()
            return [self.target.main_frame, Frame(self.target.frames[1])]

    async def run():
        old_token = '<input type="hidden" name="cf-turnstile-response" value="old-secret-token">'
        html = '<section class="cf-turnstile" style="width:300px;height:65px"></section>'
        html += old_token + f'<iframe src="{CF_FRAME}"></iframe>'
        async with dom_page(html, {CF_FRAME: "<body>Normal frame" + old_token + "</body>"}) as page:
            state = await executor._cf_page_state(Page(page))
            assert state["marker"] is True
            assert state["token_length"] == len("old-secret-token")
            assert state["marker_readable"] is (failure not in {
                "main_marker", "frame_marker", "frames", "visibility", "frame_url",
            })
            assert state["token_readable"] is (failure not in {
                "main_token", "frame_token", "frames", "visibility",
            })
            assert "old-secret-token" not in json.dumps(state)
            if failure in {"main_marker", "frame_marker", "main_token", "frame_token", "none"}:
                assert state["frame_challenge"] is True
                assert state["text_readable"] is True
            result = await executor._run_turnstile(
                Page(page), {"wait_seconds": 5}, {"url": "http://cfbox.invalid"}, None, None, "general",
            )
            if failure == "none":
                assert result[0] == "success"
                assert result[2]["completion_reason"] == "verified_and_token"
                assert len(calls) == len(screenshots) == 3
            else:
                assert result[0] == "unknown"
                assert result[2]["unresolved_challenge"] is True
                assert result[2]["token_present"] is True
                assert result[2]["token_readable"] is state["token_readable"]
                assert "visible_widget" in result[2]["blockers"]
                assert "completion_reason" not in result[2]
                assert len(calls) == len(screenshots) == 5
            assert result[2]["clicked"] is False
            assert "old-secret-token" not in json.dumps(result)

    asyncio.run(run())


@pytest.mark.parametrize("component", [True, False])
@pytest.mark.parametrize("predictions", [["error", "error", "unknown", "unknown"], ["error"]])
def test_site_cap_error_then_unknown_never_passes(cfbox_mock, fake_clock, component, predictions):
    calls, screenshots = cfbox_mock([
        {"state": prediction, "confidence": 0.9223 if prediction == "error" else None}
        for prediction in predictions
    ])

    async def run():
        async with dom_page(SITE_CAP if component else "<body>Ordinary page</body>") as page:
            result = await executor._run_turnstile(
                page, {"wait_seconds": 5}, {"url": "http://cfbox.invalid"}, None, None, "general",
            )
            if component or predictions[-1] == "unknown":
                assert result[0] == "unknown"
                assert result[2]["unresolved_challenge"] is True
                assert "cfbox_unresolved" in result[2]["blockers"]
                assert len(calls) == len(screenshots) == 5
                if component:
                    assert "visible_widget" in result[2]["blockers"]
                    assert not await page.locator('input[type="checkbox"]').is_checked()
            else:
                assert result[0] == "success"
                assert result[2]["completion_reason"] == "low_confidence_and_clear_page"
            assert result[2]["clicked"] is False
            assert all(image.startswith(b"\x89PNG") for image in screenshots)
            # An unchanged screenshot must still be submitted on every poll.
            assert len(set(screenshots)) == 1

    asyncio.run(run())
