"""Exercise sequential tour dismissal through real action.click steps offline."""

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from playwright.async_api import async_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker import executor


CHROMIUM = Path(
    "/opt/opencode/.cache/ms-playwright/chromium_headless_shell-1223/"
    "chrome-headless-shell-linux64/chrome-headless-shell"
)
TOUR_HTML = """<!doctype html>
<meta charset="utf-8">
<style>
  .shell-tour { position: fixed; inset: 0; display: grid; place-items: center; }
  .shell-tour-card { padding: 24px; background: white; border: 1px solid; }
  .shell-tour-acts, .shell-tour-right { display: flex; gap: 12px; }
</style>
<script>
  window.tourEvents = [];
  window.tourLifecycle = [];
  for (const type of ['pointerdown', 'pointerup', 'click']) {
    document.addEventListener(type, event => {
      const button = event.target.closest('button');
      if (!button) return;
      window.tourEvents.push({
        root: button.closest('[data-tour-root]').dataset.tourRoot,
        type: event.type,
        text: button.textContent,
        trusted: event.isTrusted,
      });
    }, true);
  }
  function renderTour(name) {
    const root = document.createElement('div');
    root.className = 'shell-tour';
    root.dataset.tourRoot = name;
    root.innerHTML = `
      <div class="shell-tour-card">
        <h2>${name} tour</h2>
        <div class="shell-tour-acts">
          <div class="shell-tour-right">
            <button class="btn ghost">&#19978;&#19968;&#27493;</button>
          </div>
          <button class="btn ghost">&#36339;&#36807;</button>
        </div>
      </div>`;
    root.querySelector('.shell-tour-acts > button.ghost').onclick = () => {
      window.tourLifecycle.push('skip:' + name);
      root.remove();
      if (name === 'shell') setTimeout(() => renderTour('home'), 100);
    };
    document.body.append(root);
    window.tourLifecycle.push('render:' + name);
  }
  document.addEventListener('DOMContentLoaded', () => renderTour('shell'));
</script>
<body></body>
"""


@asynccontextmanager
async def tour_page():
    if not CHROMIUM.is_file():
        pytest.skip("Local Chromium headless shell not installed")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=str(CHROMIUM), args=["--no-sandbox"]
        )
        try:
            page = await browser.new_page(service_workers="block")

            async def route(request_route):
                await request_route.fulfill(content_type="text/html", body=TOUR_HTML)

            await page.route("**/*", route)
            await page.goto("https://tour.test/")
            yield page
        finally:
            await browser.close()


def test_action_click_dismisses_shell_then_delayed_home_tour():
    async def run():
        async with tour_page() as page:
            for index, name in enumerate(("shell", "home"), start=1):
                root_selector = f'.shell-tour[data-tour-root="{name}"]'
                root = page.locator(root_selector)
                await expect(root).to_be_visible(timeout=3000)
                selector = (
                    f'{root_selector} .shell-tour-acts > button.ghost'
                    ':text-is("\u8df3\u8fc7")'
                )
                await expect(page.locator(selector)).to_have_count(1)
                status, message, data = await executor._run_step(
                    page,
                    {"feature_type": "action.click", "config": {"selector": [selector]}},
                    credentials=None,
                    twofauth=None,
                    cfbox=None,
                    cancel=None,
                    xdisplay=None,
                    lane_key="general",
                )
                assert status == "success", message
                assert data == {"url": "https://tour.test/"}
                await expect(root).to_have_count(0)
                assert await page.evaluate("window.tourEvents") == [
                    {"root": tour, "type": event, "text": "\u8df3\u8fc7", "trusted": True}
                    for tour in ("shell", "home")[:index]
                    for event in ("pointerdown", "pointerup", "click")
                ]

            await expect(page.locator(".shell-tour")).to_have_count(0)
            assert await page.evaluate("window.tourLifecycle") == [
                "render:shell", "skip:shell", "render:home", "skip:home",
            ]

    asyncio.run(run())
