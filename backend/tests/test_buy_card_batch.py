"""Quantity batches, isolated DOM mocks and disposable localhost browser only."""
from __future__ import annotations

import asyncio
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker import executor


SHOP = "https://shop.example/shop"
CONFIG = {"shop_url": SHOP, "purchase_quantity": 5, "purchase_interval_seconds": 0,
          "verification_timeout_seconds": 1, "quantity_selectors": [".quantity"]}
BROWSER = Path("/opt/opencode/.cache/ms-playwright/chromium_headless_shell-1223/"
               "chrome-headless-shell-linux64/chrome-headless-shell")


class Element:
    def __init__(self, page, *, attrs=None, tag="input", kind="number", visible=True,
                 enabled=True, readonly=False, valid=True):
        self.page = page
        self.attrs = attrs or {}
        self.tag, self.kind = tag, kind
        self.visible, self.enabled, self.readonly = visible, enabled, readonly
        self.valid, self.value = valid, "1"
        self.fills, self.value_reads = [], 0
        self.on_fill = None
        self.on_value = None
        self.on_blur = None
        self.connected = True
        self.presses = []

    async def is_visible(self):
        return self.visible

    async def is_enabled(self):
        return self.enabled

    async def is_editable(self):
        return not self.readonly

    async def evaluate(self, script):
        if "checkValidity" in script:
            return self.valid
        if "HTMLInputElement" in script:
            editable = self.tag == "input" and self.kind in {"number", "text", "tel"} and not self.readonly
            if "getAttribute" not in script:
                return editable
            return {"input": self.connected and self.tag == "input" and self.kind in {"number", "text", "tel"},
                    **{key: self.attrs.get(key) for key in ("min", "max", "step", "value")}}
        assert "el.form" in script
        return {"action": self.page.action, "override": None, "href": None, "target": ""}

    async def fill(self, value, **_kwargs):
        assert self.kind != "password"
        self.fills.append(value)
        self.value = value
        self.page.quantity = int(value)
        if self.on_fill:
            self.on_fill()

    async def input_value(self):
        assert self.kind != "password"
        self.value_reads += 1
        if self.on_value:
            self.on_value()
        return self.value

    async def press(self, key, **_kwargs):
        assert key == "Tab"
        self.presses.append(key)
        if self.on_blur:
            self.on_blur()

    async def text_content(self):
        return await self.inner_text()

    async def inner_text(self):
        return f"补签卡: {self.page.cards}\n魔力值: {self.page.points}"

    async def click(self, *, trial=False, **_kwargs):
        if trial:
            if self.page.on_trial:
                self.page.on_trial()
            return
        self.page.clicked.append(self.page.quantity)
        self.page.cards += self.page.delta if self.page.delta is not None else self.page.quantity
        self.page.points = Decimal(str(self.page.points)) - Decimal(str(self.page.price)) * self.page.quantity
        if self.page.after_pay:
            self.page.after_pay()
        if self.page.click_error:
            raise RuntimeError("committed then navigation failed")

    async def dispose(self):
        pass


class Product(Element):
    async def evaluate(self, script, args=None):
        assert "el.isConnected" in script
        return self.connected

    def as_element(self):
        return self

    async def query_selector_all(self, selector):
        if selector == "td":
            price = Element(self.page)

            async def text():
                return str(self.page.display_price)

            price.inner_text = text
            return [Element(self.page), Element(self.page), price]
        if selector == "button[type='submit']":
            return [self.page.button]
        return self.page.inputs.get(selector, [])


class Page:
    def __init__(self, attrs=None):
        self.url, self.action = SHOP, SHOP
        self.cards, self.points, self.price = 0, 100, 1
        self.display_price = 1
        self.quantity = 1
        self.delta = None
        self.ready = True
        self.click_error = False
        self.after_pay = self.on_trial = None
        self.clicked, self.gotos, self.handlers = [], [], {}
        self.input = Element(self, attrs=attrs)
        self.inputs = {".quantity": [self.input]}
        self.button = Element(self)

    def on(self, event, callback):
        self.handlers[event] = callback

    def remove_listener(self, event, callback):
        assert self.handlers.pop(event) == callback

    async def goto(self, url, **_kwargs):
        self.url = url
        self.gotos.append(url)

    async def evaluate_handle(self, script, _args):
        assert "closest" in script
        return Product(self)

    async def query_selector_all(self, selector):
        assert selector == "body", "quantity selectors must never search the page"
        return [Element(self)]

    async def evaluate(self, script):
        assert "document.readyState" in script
        return self.ready

    async def wait_for_load_state(self, *_args, **_kwargs):
        pass


def buy(page=None, config=None, pending=None, logs=None):
    page = page or Page()
    status, message, data = asyncio.run(executor._run_buy_card(
        page, {**CONFIG, **(config or {})}, pending or [],
        lambda *entry: logs.append(entry) if logs is not None else None))
    assert not page.handlers
    return status, message, data["buy_card"]


@pytest.mark.parametrize("kind", ["number", "text", "tel"])
@pytest.mark.parametrize("separate", [False, True])
def test_batch_fills_once_and_verifies_n(kind, separate):
    page = Page()
    page.cards = 8
    page.input.kind = kind
    logs = []
    status, message, result = buy(page, {"card_count_url": "/inventory" if separate else ""}, logs=logs)
    assert status == "success", message
    assert page.input.fills == ["5"] and page.clicked == [5]
    assert page.input.presses == ["Tab"]
    assert result["bought"] == 5 and result["cards_after"] == 13
    item = result["rounds"][0]
    assert (item["qty"], item["requested"], item["expected_cards"]) == (5, 5, 13)
    assert item["price"] == 1 and item["dispatched"]
    assert any("期望 13 张" in text for _, text in logs)


@pytest.mark.parametrize("attrs,quantity,expected", [
    ({"max": "2"}, 5, [2, 2, 1]),
    ({"min": "0", "step": "2"}, 6, [6]),
    ({"min": "2", "step": "3", "max": "7"}, 7, [5, 2]),
    ({"step": "any"}, 5, [5]),
    ({"max": "5.0", "step": "1e0"}, 5, [5]),
])
def test_limits_floor_to_lattice_and_continue(attrs, quantity, expected):
    page = Page(attrs)
    status, message, result = buy(page, {"purchase_quantity": quantity})
    assert status == "success", message
    assert page.clicked == expected
    assert result["bought"] == quantity
    assert [r["qty"] for r in result["rounds"]] == expected


def test_limits_and_price_are_reread_each_batch():
    page = Page({"max": "2"})

    def update():
        page.input.attrs["max"] = "1"
        page.price = page.display_price = 2

    page.after_pay = update
    status, message, result = buy(page)
    assert status == "success", message
    assert page.clicked == [2, 1, 1, 1]
    assert [r["price"] for r in result["rounds"]] == [1, 2, 2, 2]


@pytest.mark.parametrize("points,price,expected", [(0.3, 0.1, [3]), (2.9, 1, [2]), (0, 1, [])])
def test_affordability_is_decimal_and_partial_success(points, price, expected):
    page = Page()
    page.points, page.price, page.display_price = points, price, price
    status, message, result = buy(page)
    assert status == "success" and "魔力值不足" in message
    assert page.clicked == expected and result["bought"] == sum(expected)


def test_price_after_fill_is_total_rejects_changed_unit_quote():
    page = Page()
    page.points = 5
    page.input.on_fill = lambda: setattr(page, "display_price", 5)
    status, message, result = buy(page, {"max_price": 1})
    assert status == "fail", message
    assert not page.clicked and not result["payment_attempted"]


@pytest.mark.parametrize("attrs,quantity,expected", [
    ({"step": "2"}, 4, 4),
    ({"step": "2", "value": "1"}, 5, 5),
    ({"step": "2", "value": "2"}, 4, 4),
    ({"step": "2", "value": "bad"}, 4, 4),
    ({"step": "2", "value": "+1"}, 4, 4),
    ({"min": "0", "step": "2", "value": "1"}, 4, 4),
    ({"step": "any", "value": "0.5"}, 5, 5),
])
def test_html_step_base(attrs, quantity, expected):
    page = Page(attrs)
    status, message, result = buy(page, {"purchase_quantity": quantity})
    assert status == "success", message
    assert page.clicked == [expected] and result["bought"] == expected


def test_fractional_initial_step_base_has_no_integer_lattice():
    page = Page({"step": "2", "value": "0.5"})
    status, _, result = buy(page)
    assert status == "fail" and not page.input.fills and not result["payment_attempted"]


def test_real_tab_change_is_checked_before_click():
    page = Page()
    page.input.on_blur = lambda: setattr(page.input, "value", "9")
    status, _, result = buy(page)
    assert status == "fail" and not page.clicked and not result["payment_attempted"]


@pytest.mark.parametrize("when", ["fill", "trial", "preclick"])
@pytest.mark.parametrize("change", ["price", "balance"])
def test_quote_and_balance_are_rechecked_before_every_click(when, change):
    page = Page()

    def mutate():
        if change == "price":
            page.display_price = 2
        else:
            page.points = 4

    config = {}
    if when == "fill":
        page.input.on_fill = mutate
    elif when == "trial":
        page.on_trial = mutate
    else:
        page.delta = 0
        page.after_pay = mutate
        config["purchase_clicks"] = [
            {"scope": "product", "selectors": ["button[type='submit']"]},
            {"scope": "product", "selectors": ["button[type='submit']"]},
        ]
    status, _, result = buy(page, config)
    assert status == ("waiting_human" if when == "preclick" else "fail")
    assert page.clicked == ([5] if when == "preclick" else [])
    assert result["bought"] == 0


def test_hidden_input_may_be_readonly_but_detached_is_rejected():
    page = Page()
    page.input.visible = False
    page.input.readonly = True
    assert asyncio.run(executor._buy_quantity(page, page.input, executor._buy_origin(SHOP), 5, 1)) == 1
    page.input.connected = False
    with pytest.raises(ValueError):
        asyncio.run(executor._buy_quantity(page, page.input, executor._buy_origin(SHOP), 5, 1))


def test_hidden_readonly_input_must_still_be_enabled():
    page = Page()
    page.input.visible = False
    page.input.readonly = True
    page.input.enabled = False
    with pytest.raises(ValueError):
        asyncio.run(executor._buy_quantity(page, page.input, executor._buy_origin(SHOP), 5, 1))


@pytest.mark.parametrize("attrs", [
    {"max": "0"}, {"min": "6"}, {"min": "3", "max": "2"},
    {"max": "-1"}, {"min": "-1"}, {"step": "0"}, {"step": "-1"},
    {"min": "1.5"}, {"max": "2.5"}, {"step": "0.5"},
    {"max": "NaN"}, {"min": "Infinity"}, {"step": "bad"},
    {"min": ""}, {"max": " "}, {"step": "ANY"},
    {"min": "0", "step": "6"},
])
def test_invalid_or_unavailable_limits_never_fill_or_pay(attrs):
    page = Page(attrs)
    backup = Element(page)
    page.inputs[".quantity"].append(backup)
    status, _, result = buy(page)
    assert status == "fail"
    assert not page.input.fills and not page.clicked and not result["payment_attempted"]
    assert not backup.fills


def test_first_eligible_candidate_only_never_reads_password():
    page = Page()
    skipped = [Element(page, visible=False), Element(page, enabled=False),
               Element(page, readonly=True), Element(page, kind="password"),
               Element(page, tag="select"), Element(page, kind="email")]
    backup = Element(page)
    page.inputs = {".skipped": skipped, ".quantity": [page.input, backup]}
    status, message, _ = buy(page, {"quantity_selectors": [".missing", ".skipped", ".quantity"]})
    assert status == "success", message
    assert page.input.fills == ["5"]
    assert all(not el.fills and not el.value_reads for el in [*skipped, backup])


@pytest.mark.parametrize("failure", ["fill", "value", "validity", "read_attrs", "max", "min", "step", "origin", "ready"])
def test_fill_failures_never_switch_candidate_or_claim_dispatch(failure):
    page = Page()
    backup = Element(page)
    page.inputs[".quantity"].append(backup)

    def mutate():
        if failure in {"fill", "read_attrs"}:
            if failure == "read_attrs":
                async def unreadable(_script):
                    raise RuntimeError("attributes unreadable")
                page.input.evaluate = unreadable
            else:
                raise RuntimeError("fill result unknown")
        elif failure == "value":
            page.input.value = "1"
        elif failure == "validity":
            page.input.valid = False
        elif failure in {"max", "min", "step"}:
            page.input.attrs[failure] = {"max": "2", "min": "6", "step": "3"}[failure]
        elif failure == "origin":
            page.url = "https://evil.example/"
        else:
            page.ready = False

    page.input.on_fill = mutate
    status, _, result = buy(page)
    assert status == "fail" and page.input.fills == ["5"]
    assert not backup.fills and not page.clicked and not result["payment_attempted"]
    assert not result["rounds"][0]["dispatched"]


@pytest.mark.parametrize("failure", ["max", "min", "step", "validity", "value", "origin", "ready", "form", "blocked"])
def test_changed_limits_after_button_selection_are_rechecked(failure):
    page = Page()

    def change():
        if failure in {"max", "min", "step"}:
            page.input.attrs[failure] = {"max": "1", "min": "6", "step": "3"}[failure]
        elif failure == "validity":
            page.input.valid = False
        elif failure == "value":
            page.input.value = "4"
        elif failure == "origin":
            page.url = "https://evil.example/"
        elif failure == "form":
            page.action = "https://evil.example/pay"
        elif failure == "blocked":
            async def blocked(_script):
                await asyncio.Event().wait()
            page.input.evaluate = blocked
        else:
            page.ready = False

    page.on_trial = change
    if failure == "blocked":
        # The per-click quantity recheck has its own bounded operation budget.
        async def bounded_buy():
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(executor, "SELECTOR_WAIT_MS", 20)
                return await executor._run_buy_card(page, CONFIG, [], lambda *_: None)

        status, _, data = asyncio.run(bounded_buy())
        result = data["buy_card"]
    else:
        status, _, result = buy(page)
    assert status == "fail" and not page.clicked
    assert not result["payment_attempted"]


@pytest.mark.parametrize("delta", [0, 1, 4, 6, -1])
def test_wrong_increment_waits_full_deadline_without_repayment(delta):
    page = Page()
    page.delta = delta
    started = time.monotonic()
    status, _, result = buy(page)
    assert 0.9 <= time.monotonic() - started < 2.5
    assert status == "waiting_human" and page.clicked == [5]
    assert result["bought"] == 0 and result["payment_attempted"]
    assert result["rounds"][0]["verification_checks"] >= 2


def test_committed_click_exception_verifies_exact_batch():
    page = Page()
    page.click_error = True
    status, message, result = buy(page)
    assert status == "success", message
    assert page.clicked == [5] and result["bought"] == 5
    assert result["rounds"][0]["error"]


@pytest.mark.parametrize("observation", [1, 6, None])
def test_transient_anomalies_wait_for_exact_batch(observation):
    page = Page()
    original = page.query_selector_all
    checks = []

    async def read(selector):
        if page.clicked and not checks:
            checks.append(True)
            el = Element(page)

            async def text():
                return "unknown" if observation is None else f"补签卡: {observation}"

            el.inner_text = text
            return [el]
        return await original(selector)

    page.query_selector_all = read
    status, message, result = buy(page)
    assert status == "success", message
    assert page.clicked == [5] and result["bought"] == 5
    assert result["rounds"][0]["verification_checks"] == 2


@pytest.mark.parametrize("quantity,stock,pending,expected", [(0, 2, 7, 5), (5, 9, 0, 5), (0, 9, 7, 0)])
def test_fixed_vs_auto_quantity(quantity, stock, pending, expected):
    page = Page()
    page.cards = stock
    status, message, result = buy(page, {"purchase_quantity": quantity}, ["day"] * pending)
    assert status == "success", message
    assert result["requested_quantity"] == result["bought"] == expected
    assert page.cards == stock + expected


@pytest.mark.parametrize("absent", [False, True])
def test_unconfigured_keeps_single_card_behavior(absent):
    page = Page()
    config = {**CONFIG, "quantity_selectors": []}
    if absent:
        del config["quantity_selectors"]
    status, message, data = asyncio.run(executor._run_buy_card(page, config, [], lambda *_: None))
    result = data["buy_card"]
    assert status == "success", message
    assert page.clicked == [1] * 5 and not page.input.fills
    assert [r["qty"] for r in result["rounds"]] == [1] * 5


def test_no_product_match_never_uses_page_fallback(monkeypatch):
    monkeypatch.setattr(executor, "SELECTOR_WAIT_MS", 20)
    page = Page()
    page.inputs = {}
    status, _, result = buy(page)
    assert status == "fail" and not result["payment_attempted"] and not page.clicked


@pytest.mark.parametrize("selectors", [[".q"] * 21, "input", [""], [1], None])
def test_worker_rejects_invalid_selector_contract(selectors):
    page = Page()
    status, _, result = buy(page, {"quantity_selectors": selectors})
    assert status == "fail" and not page.gotos and not result["payment_attempted"]


def test_quantity_timeout_only_adds_budget():
    plain = {"purchase_quantity": 365}
    assert executor._buy_timeout({**plain, "quantity_selectors": [".q"]}) > executor._buy_timeout(plain)


def test_minimum_increases_above_already_filled_lattice_point():
    page = Page({"step": "3"})
    page.input.on_fill = lambda: page.input.attrs.update(min="5", step="1")
    status, _, result = buy(page)
    assert page.input.fills == ["3"]
    assert status == "fail" and not page.clicked and not result["payment_attempted"]


def test_multi_click_rechecks_quantity_before_modal_confirmation():
    page = Page()
    page.delta = 0
    page.after_pay = lambda: page.input.attrs.update(max="2")
    status, _, result = buy(page, {"purchase_clicks": [
        {"scope": "product", "selectors": ["button[type='submit']"]},
        {"scope": "product", "selectors": ["button[type='submit']"]},
    ]})
    assert status == "waiting_human" and page.clicked == [5]
    assert result["bought"] == 0 and result["payment_attempted"]
    assert result["rounds"][0]["clicks"] == 1


def test_cancelled_committed_batch_is_counted_but_stops_next_batch():
    page = Page({"max": "2"})

    async def slow_click(*, trial=False, **_kwargs):
        if trial:
            return
        page.clicked.append(page.quantity)
        page.cards += page.quantity
        await asyncio.Event().wait()

    page.button.click = slow_click

    async def run():
        return await asyncio.wait_for(executor._run_buy_card(page, CONFIG, [], lambda *_: None), 0.02)

    status, _, data = asyncio.run(run())
    result = data["buy_card"]
    assert status == "waiting_human" and result["bought"] == 2
    assert page.clicked == [2] and result["rounds"][0]["status"] == "success"


@pytest.mark.skipif(not BROWSER.is_file(), reason="isolated Chromium executable unavailable")
@pytest.mark.parametrize("layout", ["table", "grid"])
@pytest.mark.parametrize("mode", ["prg", "ajax", "modal"])
def test_real_browser_quantity_max_and_total_post(layout, mode):
    from playwright.async_api import async_playwright
    from tests.shop_card_browser import local_shop

    async def run():
        with local_shop() as (base, state):
            state["quantity_attrs"] = {"min": "1", "max": "2", "step": "1"}
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True, executable_path=str(BROWSER))
                try:
                    context = await browser.new_context()
                    page = await context.new_page()

                    async def local_only(route):
                        assert route.request.url.startswith(base + "/")
                        await route.continue_()

                    await page.route("**/*", local_only)
                    config = {**CONFIG, "shop_url": f"{base}/shop?layout={layout}&mode={mode}",
                              "quantity_selectors": ['input[name="quantity"]'],
                              "layout": layout, "verification_timeout_seconds": 3}
                    if layout == "grid":
                        config["price_selectors"] = [".price"]
                    if mode == "modal":
                        config["purchase_clicks"] = [
                            {"scope": "product", "selectors": ["button[type='submit']"]},
                            {"scope": "page", "selectors": ["#confirm-pay"]},
                        ]
                    status, message, data = await executor._run_buy_card(page, config, [], lambda *_: None)
                    result = data["buy_card"]
                    assert status == "success", (message, data)
                    assert state["cards"] == result["bought"] == 5
                    assert len(state["posts"]) == 3
                    assert [fields["quantity"] for _, fields in state["posts"]] == [["2"], ["2"], ["1"]]
                    assert [fields["csrf"] for _, fields in state["posts"]] == [["token-0"], ["token-1"], ["token-2"]]
                    assert [r["expected_cards"] for r in result["rounds"]] == [2, 4, 5]
                    assert state["points"] == 10000 - 5 * 1000.25
                    await context.close()
                finally:
                    await browser.close()

    asyncio.run(run())


@pytest.mark.skipif(not BROWSER.is_file(), reason="isolated Chromium executable unavailable")
@pytest.mark.parametrize("case", [
    "blur_quantity", "async_change_balance", "fill_price", "fill_balance",
    "modal_price", "modal_balance", "step_zero_base", "step_value_base",
    "step_invalid_value", "step_min_zero", "modal_hidden", "modal_detached",
    "modal_hidden_changed_quantity",
    "modal_new_visible_quote", "modal_new_visible_ambiguous", "modal_new_visible_no_scope",
    "modal_disabled_quantity", "visible_product_hidden_price",
])
def test_real_browser_review4_guards(case):
    from playwright.async_api import async_playwright
    from tests.shop_card_browser import local_shop

    async def run():
        with local_shop() as (base, state):
            state["quantity_attrs"] = {"min": "1", "max": "10"}
            mode = "modal" if case.startswith("modal_") else "ajax"
            target = 5
            script = """
                const qty = document.querySelector('[name="quantity"]');
                const product = qty.closest('form');
                window.tabChanges = 0;
                qty.addEventListener('change', () => window.tabChanges++);
            """
            if case == "blur_quantity":
                script += "qty.addEventListener('blur', () => { qty.value = '9'; });"
            elif case == "async_change_balance":
                script += """
                    qty.addEventListener('change', () => {
                        document.querySelector('#balance').setAttribute('aria-busy', 'true');
                        setTimeout(() => {
                            document.querySelector('#balance').textContent = '补签卡: 0 魔力值: 100';
                            document.querySelector('#balance').removeAttribute('aria-busy');
                        }, 100);
                    });
                """
            elif case == "fill_price":
                script += "qty.addEventListener('input', () => { product.querySelector('.price').textContent = '5001.25'; });"
            elif case == "fill_balance":
                script += "qty.addEventListener('input', () => { document.querySelector('#balance').textContent = '补签卡: 0 魔力值: 100'; });"
            elif case == "modal_price":
                script += "product.addEventListener('submit', () => { product.querySelector('.price').textContent = '1200.5'; product.hidden = true; });"
            elif case == "modal_balance":
                script += "product.addEventListener('submit', () => { document.querySelector('#balance').textContent = '补签卡: 0 魔力值: 100'; });"
            elif case.startswith("step_"):
                state["quantity_attrs"] = {"step": "2", "max": "5", "value": None}
                target = 4
                if case == "step_value_base":
                    state["quantity_attrs"]["value"] = "1"
                    target = 5
                elif case == "step_invalid_value":
                    state["quantity_attrs"]["value"] = "bad"
                elif case == "step_min_zero":
                    state["quantity_attrs"].update(min="0", value="1")
            elif case == "modal_hidden":
                script += "product.addEventListener('submit', () => { product.hidden = true; qty.readOnly = true; });"
            elif case == "modal_detached":
                script += "product.addEventListener('submit', () => { qty.remove(); });"
            elif case == "modal_hidden_changed_quantity":
                script += "product.addEventListener('submit', () => { product.hidden = true; qty.value = '9'; });"
            elif case.startswith("modal_new_visible_"):
                tag = "div" if case == "modal_new_visible_no_scope" else "form"
                price = "1000.25" if case == "modal_new_visible_ambiguous" else "1200.5"
                count = 2 if case == "modal_new_visible_ambiguous" else 1
                script += f"""
                    product.addEventListener('submit', () => {{
                        product.hidden = true;
                        for (let i = 0; i < {count}; i++) {{
                            const replacement = document.createElement('{tag}');
                            replacement.className = 'magic-card';
                            replacement.innerHTML = '<h2>购买补签卡</h2><span class="price">{price}</span>';
                            document.querySelector('#confirm-modal').append(replacement);
                        }}
                    }});
                """
            elif case == "modal_disabled_quantity":
                script += "product.addEventListener('submit', () => { product.hidden = true; qty.disabled = true; });"
            elif case == "visible_product_hidden_price":
                script += """
                    const backup = document.createElement('span');
                    backup.className = 'price';
                    backup.hidden = true;
                    backup.textContent = '99999';
                    product.append(backup);
                """
            state["script"] = script
            success = case.startswith("step_") or case in {"modal_hidden", "visible_product_hidden_price"}
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True, executable_path=str(BROWSER))
                try:
                    context = await browser.new_context()
                    page = await context.new_page()

                    async def local_only(route):
                        assert route.request.url.startswith(base + "/")
                        await route.continue_()

                    await page.route("**/*", local_only)
                    config = {**CONFIG, "shop_url": f"{base}/shop?layout=grid&mode={mode}",
                              "quantity_selectors": ['input[name="quantity"]'],
                              "layout": "grid", "price_selectors": [".price"],
                              "purchase_quantity": target, "max_price": 1500}
                    if mode == "modal":
                        config["purchase_clicks"] = [
                            {"scope": "product", "selectors": ["button[type='submit']"]},
                            {"scope": "page", "selectors": ["#confirm-pay"]},
                        ]
                    status, message, data = await executor._run_buy_card(page, config, [], lambda *_: None)
                    result = data["buy_card"]
                    assert status == ("success" if success else "waiting_human" if mode == "modal" else "fail"), (case, message, data)
                    assert result["bought"] == (target if success else 0)
                    assert len(state["posts"]) == (1 if success else 0)
                    assert result["payment_attempted"] is (success or mode == "modal")
                    if success:
                        assert state["posts"][0][1]["quantity"] == [str(target)]
                        assert state["cards"] == target
                    elif mode == "modal":
                        assert result["rounds"][0]["clicks"] == 1
                    else:
                        assert result["rounds"][0]["clicks"] == 0
                        assert await page.evaluate("window.tabChanges") == 1
                    await context.close()
                finally:
                    await browser.close()

    asyncio.run(run())
