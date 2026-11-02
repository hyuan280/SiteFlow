"""Local HTTP + disposable Playwright browser only. No Manager, profiles or .env."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from html import escape
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from worker.executor import _buy_page_ready, _buy_product, _extract_card_info, _run_buy_card
from app.features.catalog import PostBuyCardConfig


@contextmanager
def local_shop():
    fixture = Path(__file__).with_name("shop_card_fixture.html").read_text()
    state = {"cards": 0, "points": 10000, "token": "token-0", "posts": [], "gets": [],
             "delta": 1, "delay": 0, "hide_stock": False, "price": "1,000.25",
             "credit_delay": 0, "credit_at": None, "dom_ready_at": 0,
             "quantity_attrs": None, "credit_quantity": 1, "script": ""}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            state["gets"].append(self.path)
            if state["credit_at"] is not None and time.monotonic() >= state["credit_at"]:
                state["cards"] += state["delta"] * state["credit_quantity"]
                state["credit_at"] = None
            if self.path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            if self.path == "/inventory":
                html = f'<p id="stock">补签卡: {state["cards"]}</p>'
            else:
                query = parse_qs(urlsplit(self.path).query)
                hidden = (f'<input type="hidden" name="csrf" value="{state["token"]}">'
                          '<input type="hidden" name="option" value="0">')
                if state["quantity_attrs"] is not None:
                    attrs = " ".join(f'{key}="{escape(str(value), quote=True)}"'
                                     for key, value in {"value": "1", **state["quantity_attrs"]}.items()
                                     if value is not None)
                    hidden += f'<input type="number" name="quantity" {attrs}>'
                buttons = ('<input type="submit" hidden value="hidden">'
                           '<input type="submit" disabled value="disabled">'
                           '<button type="submit" name="redeem" value="yes">交换</button>')
                if query.get("input") == ["1"]:
                    buttons = '<input type="submit" name="redeem" value="yes">'
                action = f'/pay?{urlsplit(self.path).query}'
                if query.get("layout") == ["grid"]:
                    products = ('<form class="magic-card"><h2>上传量</h2><span class="price">99999</span></form>'
                                f'<form class="magic-card" method="post" action="{action}">'
                                f'{hidden}<div class="card-title">购买补签卡</div>'
                                f'<span class="price">{state["price"]}</span>{buttons}</form>')
                else:
                    # Invalid legacy tr > form is intentional: button.form survives parser repair.
                    products = ('<table><tr><td><h2>上传量</h2></td><td>999999</td><td>500</td></tr>'
                                f'<tr><form method="post" action="{action}">'
                                '<td><h2>购买补签卡</h2></td><td>999999</td>'
                                f'<td>{state["price"]}</td><td>{hidden}{buttons}</td></form></tr></table>')
                html = fixture.replace("PRODUCTS", products).replace("CARDS", str(state["cards"]))
                html = html.replace("POINTS", str(state["points"]))
                dom_delay_ms = max(0, int((state["dom_ready_at"] - time.monotonic()) * 1000))
                html = html.replace("DOM_DELAY_MS", str(dom_delay_ms))
                if state["hide_stock"]:
                    html = html.replace(f'补签卡: {state["cards"]}', "库存见独立页面")
                if state["script"]:
                    html += f'<script>{state["script"]}</script>'
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())

        def do_POST(self):
            fields = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            state["posts"].append((self.path, fields))
            if fields.get("csrf") != [state["token"]] or fields.get("option") != ["0"]:
                self.send_response(403)
                self.end_headers()
                return
            time.sleep(state["delay"])
            quantity = int(fields.get("quantity", ["1"])[0])
            if state["credit_delay"]:
                state["credit_at"] = time.monotonic() + state["credit_delay"]
                state["credit_quantity"] = quantity
            else:
                state["cards"] += state["delta"] * quantity
            state["points"] -= float(state["price"].replace(",", "")) * quantity
            state["token"] = f'token-{len(state["posts"])}'
            query = urlsplit(self.path).query
            if parse_qs(query).get("mode") == ["rebuild"]:
                state["dom_ready_at"] = time.monotonic() + 1.2
            if parse_qs(query).get("mode", ["prg"]) == ["prg"]:
                self.send_response(303)
                self.send_header("Location", "/shop?" + query)
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({k: state[k] for k in ("cards", "points", "token")}).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


async def check_browser(context):
    with local_shop() as (base, state):
        page = await context.new_page()
        log = lambda *_args: None
        for layout in ("table", "grid"):
            for mode in ("prg", "ajax", "modal"):
                state.update(cards=0, points=10000, token="token-0", posts=[], gets=[])
                url = f"{base}/shop?layout={layout}&mode={mode}"
                config = {**PostBuyCardConfig().model_dump(),
                          "shop_url": url, "layout": layout, "purchase_quantity": 2,
                          "purchase_interval_seconds": 0, "verification_timeout_seconds": 3}
                if layout == "grid":
                    config["price_selectors"] = [".price"]
                if mode == "modal":
                    config["purchase_clicks"] = [
                        {"scope": "product", "selectors": ["input[type='submit']", "button[type='submit']"]},
                        {"scope": "page", "selectors": ["#confirm-pay"]},
                    ]
                status, message, data = await _run_buy_card(page, config, [], log)
                assert status == "success", (layout, mode, message, data)
                assert data["buy_card"]["bought"] == 2 and state["cards"] == 2, data
                assert data["buy_card"]["payment_attempted"] is True
                assert all(item["dispatched"] is True for item in data["buy_card"]["rounds"])
                assert len(state["posts"]) == 2, state
                assert [fields["csrf"] for _, fields in state["posts"]] == [["token-0"], ["token-1"]]
                if mode == "prg":
                    assert all(fields["redeem"] == ["yes"] for _, fields in state["posts"])
                print(f"PASS: {layout} {mode}, real clicks, inventory +1, refreshed CSRF")

        url = f"{base}/shop?layout=grid&mode=ajax"
        config = {**PostBuyCardConfig().model_dump(),
                  "shop_url": url, "layout": "grid", "price_selectors": [".price"],
                  "purchase_quantity": 1, "purchase_interval_seconds": 0, "verification_timeout_seconds": 2}
        state.update(cards=0, points=10000, token="token-0", posts=[])
        status, message, data = await _run_buy_card(page, {
            **config, "shop_url": f"{base}/shop?input=1&mode=prg", "layout": "table",
            "price_selectors": [],
        }, [], log)
        assert status == "success" and state["posts"][0][1]["redeem"] == ["yes"], (message, data)
        for delta in (0, 2):
            state.update(cards=0, points=10000, token="token-0", posts=[], delta=delta)
            status, _, data = await _run_buy_card(page, config, [], log)
            assert status == "waiting_human" and data["buy_card"]["bought"] == 0, data
            assert len(state["posts"]) == 1
        state.update(cards=0, points=10000, token="token-0", posts=[], delta=1, delay=0.7)
        status, message, data = await _run_buy_card(page, config, [], log)
        assert status == "success", (message, data)
        assert len(state["posts"]) == 1
        state["delay"] = 0

        state.update(cards=0, points=10000, token="token-0", posts=[], hide_stock=True)
        status, message, data = await _run_buy_card(page, {**config, "card_count_url": "/inventory"}, [], log)
        assert status == "success", (message, data)
        assert all("attendance" not in path for path in state["gets"])
        state["hide_stock"] = False

        state.update(cards=3, points=10000, token="token-0", posts=[], gets=[], hide_stock=True)
        initial_inventory_requests, initial_navigations, initial_logs = [], [], []

        def record_initial_navigation(request):
            if request.is_navigation_request() and request.frame == page.main_frame:
                initial_navigations.append((request.url, time.monotonic()))

        async def delayed_initial_inventory(route):
            initial_inventory_requests.append(route.request.url)
            if len(initial_inventory_requests) == 1:
                assert not state["posts"]
                await route.fulfill(content_type="text/html; charset=utf-8", body='''
                    <meta charset="utf-8"><p>库存加载中</p>
                    <script>
                    setTimeout(() => {
                      const stock = document.createElement('p');
                      stock.textContent = '补签卡: 3';
                      document.body.append(stock);
                    }, 650);
                    </script>
                ''')
            else:
                await route.continue_()

        page.on("request", record_initial_navigation)
        await page.route(f"{base}/inventory", delayed_initial_inventory)
        try:
            status, message, data = await asyncio.wait_for(_run_buy_card(page, {
                **config, "card_count_url": "/inventory", "verification_timeout_seconds": 3,
            }, [], lambda *entry: initial_logs.append(entry)), timeout=10)
        finally:
            await page.unroute(f"{base}/inventory", delayed_initial_inventory)
            page.remove_listener("request", record_initial_navigation)
        assert status == "success", (message, data, initial_logs)
        assert data["buy_card"]["cards_before"] == 3 and data["buy_card"]["cards_after"] == 4
        assert data["buy_card"]["bought"] == len(state["posts"]) == 1
        assert [entry[0] for entry in initial_navigations[:2]] == [f"{base}/inventory", url]
        assert 0.6 <= initial_navigations[1][1] - initial_navigations[0][1] < 3
        assert len(initial_inventory_requests) == 2
        assert any("来源=独立库存地址" in text for _, text in initial_logs)
        print("PASS: first inventory DOM delayed 650ms, polled without reload, inventory before shop, one POST")
        state["hide_stock"] = False

        for separate in (False, True):
            state.update(cards=0, points=10000, token="token-0", posts=[], gets=[],
                         credit_delay=1.3, credit_at=None, hide_stock=separate)
            status, message, data = await _run_buy_card(page, {
                **config, "card_count_url": "/inventory" if separate else "",
                "verification_timeout_seconds": 5,
            }, [], log)
            assert status == "success" and data["buy_card"]["bought"] == 1, (message, data)
            assert len(state["posts"]) == 1
            count_gets = [path for path in state["gets"] if path == "/inventory" or not separate]
            assert len(count_gets) >= 3, state["gets"]
        state.update(credit_delay=0, credit_at=None, hide_stock=False)

        state.update(cards=0, points=10000, token="token-0", posts=[], gets=[])
        inventory_documents, pending_requests, inventory_logs = [], [], []

        async def inventory_route(route):
            if state["posts"] and not inventory_documents:
                inventory_documents.append(route.request.url)
                await route.fulfill(content_type="text/html; charset=utf-8", body='''
                    <meta charset="utf-8"><p id="stock">补签卡: 1</p>
                    <script>
                    fetch('/inventory-pending');
                    setTimeout(() => {
                      document.querySelector('#stock').textContent = '补签卡: 2';
                    }, 1800);
                    </script>
                ''')
            else:
                await route.continue_()

        async def pending_route(route):
            pending_requests.append(route.request.url)
            await asyncio.sleep(2)
            state["cards"] = 2
            await route.fulfill(body="done")

        await page.route(f"{base}/inventory", inventory_route)
        await page.route(f"{base}/inventory-pending", pending_route)
        try:
            status, message, data = await asyncio.wait_for(_run_buy_card(page, {
                **config, "card_count_url": "/inventory", "purchase_quantity": 3,
                "verification_timeout_seconds": 5,
            }, [], lambda *entry: inventory_logs.append(entry)), timeout=15)
        finally:
            await page.unroute(f"{base}/inventory", inventory_route)
            await page.unroute(f"{base}/inventory-pending", pending_route)
        assert status == "waiting_human" and data["buy_card"]["bought"] == 0, (message, data, inventory_logs)
        assert len(state["posts"]) == 1 and len(data["buy_card"]["rounds"]) == 1
        assert len(inventory_documents) == len(pending_requests) == 1
        round_result = data["buy_card"]["rounds"][0]
        assert round_result["cards_after"] == 2 and round_result["status"] != "success"
        assert round_result["verification_checks"] >= 2
        assert 4.9 <= round_result["verification_elapsed_seconds"] < 6.5
        assert any("当前 1 张" in message and "页面就绪=False" in message
                   for _, message in inventory_logs), inventory_logs
        print("PASS: separate inventory GET, unsettled +1 then settled +2, "
              f"one POST, full 5s window, elapsed={round_result['verification_elapsed_seconds']}s")

        for interrupt_get in (False, True):
            state.update(cards=0, points=10000, token="token-0", posts=[], gets=[], dom_ready_at=0)
            rebuild_url = f"{base}/shop?layout=grid&mode=rebuild"
            aborted_gets, verification_logs = [], []

            async def verification_route(route):
                if (interrupt_get and state["posts"] and not aborted_gets
                        and route.request.method == "GET" and route.request.is_navigation_request()):
                    aborted_gets.append(route.request.url)
                    await route.abort("aborted")
                else:
                    await route.continue_()

            await page.route(rebuild_url, verification_route)
            try:
                status, message, data = await asyncio.wait_for(_run_buy_card(page, {
                    **config, "shop_url": rebuild_url, "purchase_quantity": 1,
                    "verification_timeout_seconds": 5,
                }, [], lambda *entry: verification_logs.append(entry)), timeout=15)
            finally:
                await page.unroute(rebuild_url, verification_route)
            assert status == "success" and data["buy_card"]["bought"] == 1, (message, data, verification_logs)
            assert len(state["posts"]) == 1, state
            assert state["cards"] == 1
            assert aborted_gets == ([rebuild_url] if interrupt_get else [])
            round_result = data["buy_card"]["rounds"][0]
            assert round_result["status"] == "success" and round_result["dispatched"]
            assert round_result["clicks"] == 1 and round_result["verification_checks"] >= 2
            assert 1 <= round_result["verification_elapsed_seconds"] < 5
            assert await page.locator('#balance[data-rebuilt="true"]').count() == 1
            assert any("开始库存核验" in message for _, message in verification_logs)
            if interrupt_get:
                assert round_result["verification_error"]
                assert any("暂时失败" in message for _, message in verification_logs)
            print(f"PASS: delayed DOM rebuild, interrupted verification GET={interrupt_get}, "
                  f"5s window, one POST, checks={round_result['verification_checks']}, "
                  f"elapsed={round_result['verification_elapsed_seconds']}s")
        state["dom_ready_at"] = 0

        before = len(state["posts"])
        status, _, _ = await _run_buy_card(page, {**config, "max_price": 1000}, [], log)
        assert status == "fail" and len(state["posts"]) == before
        status, _, _ = await _run_buy_card(page, {**config, "shop_url": url.replace("ajax", "native")}, [], log)
        assert status == "waiting_human" and len(state["posts"]) == before

        await page.goto(url)
        for attribute in ("action", "formaction"):
            await page.goto(url)
            target = "form.magic-card" if attribute == "action" else "button[type='submit']"
            await page.locator(target).last.evaluate(
                "(el, attr) => el.setAttribute(attr, 'https://invalid.example/pay')", attribute)
            from worker.executor import _purchase_cards
            # Preserve mutated DOM for this check; the normal entry performs a fresh GET.
            original_goto = page.goto
            async def skip_initial_goto(*_args, **_kwargs):
                pass
            page.goto = skip_initial_goto
            try:
                result = await _purchase_cards(page, config, url, 1, log)
            finally:
                page.goto = original_goto
            assert result["status"] == "fail" and len(state["posts"]) == before, result

        await page.goto(url)
        info = await _extract_card_info(page, config, log)
        assert info == {"price": 1000.25}, info
        await page.locator("form.magic-card").last.evaluate("el => el.after(el.cloneNode(true))")
        assert await _extract_card_info(page, config, log) is None
        await page.set_content('<section><article><h2>购买补签卡</h2><span class="price">10</span>'
                               '</article><article><div class="card-title">购买补签卡</div>'
                               '<span class="price">20</span></article></section>')
        assert await _extract_card_info(page, config, log) == {"price": 10}
        await page.set_content('<div class="product"><h2>备用商品</h2><span class="price">20.5</span></div>'
                               '<div class="product"><h2>首选商品</h2><span class="price">10.5</span></div>')
        custom = {"layout": "custom", "product_selectors": [".product"],
                  "card_title": ["首选商品", "备用商品"], "price_selectors": [".price"]}
        assert await _extract_card_info(page, custom, log) == {"price": 10.5}
        assert await _extract_card_info(page, {**custom, "price_selectors": []}, log) is None
        await page.set_content('<section class="preferred">'
                               '<span class="price">10</span><article><h2>购买补签卡</h2>'
                               '<span class="nested-price">99</span></article></section>'
                               '<article><div class="card-title">购买补签卡</div>'
                               '<span class="nested-price">30</span></article>')
        ordered = {**config, "product_selectors": [".missing", ".preferred", "article"],
                   "title_selectors": [".missing", "h2", "h3", ".card-title"]}
        product = await _buy_product(page, ordered)
        assert product is not None
        assert await product.get_attribute("class") == "preferred"
        await product.dispose()
        assert await _extract_card_info(page, ordered, log) == {"price": 10}
        # First selector ambiguity must fail, not fall through to a unique backup.
        await page.locator("section.preferred").evaluate("el => el.after(el.cloneNode(true))")
        assert await _extract_card_info(page, ordered, log) is None
        await page.set_content('<article><h2>购买补签卡</h2><span class="price">10</span></article>'
                               '<article><h2>购买补签卡</h2><span class="price">20</span></article>'
                               '<article><h3>购买补签卡</h3><span class="price">30</span></article>')
        assert await _extract_card_info(page, config, log) is None
        print("PASS: no/multiple inventory increments, delayed AJAX, explicit inventory URL, limits, native dialog dismissed, same-origin guards, ambiguity")
        print("PASS: ordered product/title candidates, delayed server credit via bounded GET polling, dispatch evidence")
        for price, points in (("-1,000.25", 10000), ("1,000.25", -10000)):
            state.update(cards=0, points=points, price=price, token="token-0", posts=[])
            status, _, data = await _run_buy_card(page, config, [], log)
            assert status == "fail" and not state["posts"], data
            assert data["buy_card"]["payment_attempted"] is False
        print("PASS: negative price/balance rejected with actual filled catalog defaults")
        await page.close()


async def check_piggo_readiness(context):
    fixture = Path(__file__).with_name("piggo_card_fixture.html").read_text()
    with local_shop() as (base, _server_state):
        page = await context.new_page()
        # No remote requests, shared profiles, cookies or live site configuration.
        state = {"cards": 3, "points": 10000, "shellDelay": 400, "stockDelay": 1100,
                 "shopDelay": 3000, "permanentBusy": False, "postBusy": False}
        posts, navigations = [], []
        first_shop_at = []

        async def route_request(route):
            request = route.request
            assert request.url.startswith(base + "/"), request.url
            path = urlsplit(request.url).path
            if request.is_navigation_request():
                navigations.append(request.url)
            if path == "/pay":
                assert request.method == "POST"
                posts.append(time.monotonic())
                state["cards"] += 1
                state["points"] -= 2000
                await route.fulfill(content_type="application/json", body=json.dumps(state))
                return
            assert path in {"/calendar", "/mybonus"}, request.url
            if path == "/mybonus":
                assert request.url == f"{base}/mybonus?tab=shop", request.url
                first_shop_at.append(time.monotonic())
            html = fixture.replace("FIXTURE_STATE", json.dumps({
                **state, "view": "calendar" if path == "/calendar" else "shop",
            }))
            await route.fulfill(content_type="text/html; charset=utf-8", body=html)

        await page.route("**/*", route_request)
        config = {**PostBuyCardConfig().model_dump(),
                  "shop_url": f"{base}/mybonus?tab=shop", "card_count_url": f"{base}/calendar",
                  "card_count_selectors": [], "card_count_regex": r"补签卡（当前\s*(\d+)\s*张",
                  "layout": "grid", "product_selectors": [], "card_title": ["补签卡"],
                  "price_selectors": [".good-cost"], "points_regex": r"余额:\s*([\d,]+)",
                  "purchase_quantity": 2, "purchase_interval_seconds": 0,
                  "verification_timeout_seconds": 5, "max_price": 2000}
        started = time.monotonic()
        status, message, data = await asyncio.wait_for(
            _run_buy_card(page, config, [], lambda *_: None), timeout=35)
        result = data["buy_card"]
        assert status == "success", (message, data)
        assert (result["cards_before"], result["cards_after"], result["bought"]) == (3, 5, 2)
        assert [item["price"] for item in result["rounds"]] == [2000, 2000]
        assert len(posts) == 2 and first_shop_at[0] - started >= 1.4
        assert posts[0] - first_shop_at[0] >= 2.9
        assert posts[1] - first_shop_at[-1] >= 2.9
        assert all(url in {config["shop_url"], config["card_count_url"]} for url in navigations)
        print("PASS: Piggo DOM, initial busy 0 -> real 3 at 1.5s, h3/article.good/.good-cost "
              "after 3s each round, price 2000, tab=shop retained, exactly two POSTs")

        state.update(cards=0, points=10000, shellDelay=0, stockDelay=0, shopDelay=0)
        posts.clear()
        status, message, data = await _run_buy_card(page, {**config, "purchase_quantity": 1}, [], lambda *_: None)
        assert status == "success", (message, data)
        assert data["buy_card"]["cards_before"] == 0 and len(posts) == 1
        print("PASS: Piggo genuine ready initial 0 is accepted and purchased once")

        state.update(cards=3, points=10000, permanentBusy=True)
        posts.clear()
        navigations.clear()
        status, message, data = await _run_buy_card(page, {
            **config, "verification_timeout_seconds": 1,
        }, [], lambda *_: None)
        assert status == "fail" and not posts, (message, data)
        assert data["buy_card"]["cards_before"] is None
        assert navigations == [config["card_count_url"]]
        print("PASS: Piggo permanent initial busy + placeholder 0 times out without shop or POST")

        state.update(cards=3, permanentBusy=False, postBusy=True)
        posts.clear()
        navigations.clear()
        status, message, data = await _run_buy_card(page, {
            **config, "verification_timeout_seconds": 2,
        }, [], lambda *_: None)
        assert status == "waiting_human" and len(posts) == 1, (message, data)
        assert data["buy_card"]["bought"] == 0
        assert navigations == [config["card_count_url"], config["shop_url"]]
        assert await page.locator('.cal-help').inner_text() == "补签卡（当前 4 张）"
        assert await _buy_page_ready(page) is False
        print("PASS: Piggo post-payment networkidle + busy + exact +1 is not verified, no GET/replay")

        for selector in ('aria-busy="true"', 'class="loading"', 'class="spinner"'):
            for style, expected in (("", False), ("display:none", True),
                                    ("visibility:hidden", True), ("visibility:collapse", True)):
                await page.set_content(f'<body><b {selector} style="{style}">Loading</b></body>')
                assert await _buy_page_ready(page) is expected, (selector, style)
        await page.set_content('<div style="display:none"><b class="loading">Loading</b></div>')
        assert await _buy_page_ready(page) is True
        print("PASS: readiness uses CSS visibility/geometry, ignores hidden loaders and hidden ancestors")
        await page.close()


async def main(executable=None):
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, executable_path=executable)
        try:
            async with await browser.new_context() as context:
                await check_browser(context)
                await check_piggo_readiness(context)
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-executable")
    asyncio.run(main(parser.parse_args().browser_executable))
