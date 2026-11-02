from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker import executor
from app.features.catalog import PostBuyCardConfig


SHOP = "https://shop.example/mybonus.php"
CONFIG = {"shop_url": SHOP, "purchase_interval_seconds": 0,
          "verification_timeout_seconds": 1}


class Element:
    def __init__(self, page, text="", *, visible=True, enabled=True, available=True,
                 action=None, override=None, effect=None):
        self.page, self.text = page, text
        self.visible, self.enabled, self.available = visible, enabled, available
        self.action, self.override, self.effect = action, override, effect

    async def is_visible(self):
        return self.visible

    async def is_enabled(self):
        return self.enabled

    async def inner_text(self):
        return self.text() if callable(self.text) else self.text

    async def evaluate(self, _script):
        return {"action": self.action, "override": self.override, "href": None, "target": ""}

    async def click(self, *, trial=False, **_kwargs):
        if trial:
            if not self.available:
                raise RuntimeError("covered")
            return
        self.page.clicked.append(self.text)
        if self.effect:
            await self.effect()

    async def dispose(self):
        pass


class Product(Element):
    def as_element(self):
        return self

    async def query_selector_all(self, selector):
        if selector == "td":
            return [Element(self.page), Element(self.page),
                    Element(self.page, lambda: self.page.price)]
        return self.page.product_elements.get(selector, [])


class BuyPage:
    def __init__(self):
        self.url = SHOP
        self.cards, self.points, self.price = 0, 5000, "1,000.25"
        self.delta = 1
        self.gotos, self.clicked, self.handlers = [], [], {}
        self.after_click_error = False
        self.dialog = False
        self.dismissed = False
        self.unreadable = False
        self.stale = False
        self.persisted_cards = None
        self.settled = True
        self.ready = True
        self.product_elements = {
            "button[type='submit']": [Element(self, "pay", action=SHOP, effect=self.pay)],
        }
        self.page_elements = {}
        self.product = Product(self)

    async def pay(self):
        if self.dialog:
            async def dismiss():
                self.dismissed = True
            await self.handlers["dialog"](SimpleNamespace(dismiss=dismiss))
            return
        if self.stale:
            self.persisted_cards = self.cards + self.delta
        else:
            self.cards += self.delta
        self.points -= 1000.25
        if self.after_click_error:
            raise RuntimeError("navigation failed after payment")

    def on(self, event, callback):
        self.handlers[event] = callback

    def remove_listener(self, event, callback):
        assert self.handlers[event] == callback
        del self.handlers[event]

    async def goto(self, url, **_kwargs):
        self.gotos.append(url)
        self.url = url
        if self.persisted_cards is not None:
            self.cards, self.persisted_cards = self.persisted_cards, None

    async def query_selector_all(self, selector):
        if selector == "body":
            return [Element(self, lambda: f"补签卡: {self.cards}\n魔力值: {self.points}"
                            if not (self.unreadable and self.clicked) else "unknown")]
        return self.page_elements.get(selector, [])

    async def evaluate_handle(self, script, args):
        assert "closest" in script
        assert args["titles"] == ["购买补签卡"]
        return self.product

    async def evaluate(self, script):
        assert "document.readyState" in script
        return self.ready

    async def wait_for_load_state(self, *_args, **_kwargs):
        if not self.settled:
            await asyncio.sleep(2)


def run_buy(page=None, config=None, pending=None, log=None):
    page = page or BuyPage()
    return asyncio.run(executor._run_buy_card(
        page, {**CONFIG, **(config or {})}, pending if pending is not None else ["a", "b"],
        log or (lambda *_: None),
    ))


def test_candidates_skip_hidden_disabled_covered_and_click_only_first():
    page = BuyPage()
    page.product_elements[".candidates"] = [
        Element(page, "hidden", visible=False), Element(page, "disabled", enabled=False),
        Element(page, "covered", available=False), Element(page, "first", effect=page.pay),
        Element(page, "never", effect=page.pay),
    ]
    status, message, data = run_buy(page, {"purchase_quantity": 1, "purchase_clicks": [
        {"scope": "product", "selectors": [".missing", ".candidates", "button[type='submit']"]},
    ]})
    assert status == "success", message
    assert page.clicked == ["first"]
    assert data["buy_card"]["bought"] == 1
    assert data["buy_card"]["payment_attempted"] is True
    assert data["buy_card"]["rounds"][0]["dispatched"] is True


def test_click_array_order_with_page_modal_and_product_reselection():
    page = BuyPage()
    page.product_elements[".open"] = [Element(page, "open")]
    page.product_elements[".choose"] = [Element(page, "choose")]
    page.page_elements[".confirm"] = [Element(page, "confirm", effect=page.pay)]
    status, message, data = run_buy(page, {"purchase_quantity": 2, "purchase_clicks": [
        {"scope": "product", "selectors": [".open"]},
        {"scope": "product", "selectors": [".choose"]},
        {"scope": "page", "selectors": [".confirm"]},
    ]})
    assert status == "success", message
    assert page.clicked == ["open", "choose", "confirm"] * 2
    assert [r["clicks"] for r in data["buy_card"]["rounds"]] == [3, 3]


@pytest.mark.parametrize("delta,unreadable", [(0, False), (2, False), (-1, False), (1, True)])
def test_unverified_inventory_stops_without_replay(delta, unreadable):
    page = BuyPage()
    page.cards = 5
    page.delta, page.unreadable = delta, unreadable
    started = time.monotonic()
    status, _, data = run_buy(page, {"purchase_quantity": 3})
    elapsed = time.monotonic() - started
    assert status == "waiting_human"
    assert page.clicked == ["pay"]
    assert data["buy_card"]["bought"] == 0
    assert len(data["buy_card"]["rounds"]) == 1
    round_result = data["buy_card"]["rounds"][0]
    assert round_result["cards_after"] == (None if unreadable else 5 + delta)
    assert round_result["status"] != "success"
    assert round_result["verification_checks"] >= 2
    assert 0.9 <= round_result["verification_elapsed_seconds"] <= elapsed + 0.05
    assert elapsed < 2.5


@pytest.mark.parametrize("failure", ["read", "navigate"])
@pytest.mark.parametrize("separate", [False, True])
def test_transient_verification_errors_recover_each_round_without_repayment(failure, separate):
    page = BuyPage()
    page.stale = True
    original_read, original_goto = page.query_selector_all, page.goto
    failed_rounds, logs = [], []

    def fail_once():
        if page.clicked and len(page.clicked) not in failed_rounds:
            assert sum("开始库存核验" in message for _, message in logs) == len(page.clicked)
            failed_rounds.append(len(page.clicked))
            raise RuntimeError("temporary same-origin navigation/context destruction")

    async def read(selector):
        if failure == "read" and selector == "body":
            fail_once()
        return await original_read(selector)

    async def goto(url, **kwargs):
        if failure == "navigate":
            fail_once()
        await original_goto(url, **kwargs)

    page.query_selector_all, page.goto = read, goto
    status, message, data = run_buy(page, {
        "purchase_quantity": 3, "verification_timeout_seconds": 2,
        "card_count_url": "/inventory" if separate else "",
    }, log=lambda *entry: logs.append(entry))
    assert status == "success", (message, data)
    assert failed_rounds == [1, 2, 3]
    assert page.clicked == ["pay"] * 3
    assert data["buy_card"]["bought"] == 3
    rounds = data["buy_card"]["rounds"]
    assert [item["cards_before"] for item in rounds] == [0, 1, 2]
    assert [item["cards_after"] for item in rounds] == [1, 2, 3]
    for item in rounds:
        assert item["status"] == "success" and item["clicks"] == 1 and item["dispatched"]
        assert item["verification_error"]
        assert item["verification_checks"] >= 1
        assert 0.09 <= item["verification_elapsed_seconds"] < 2


@pytest.mark.parametrize("observed_delta", [0, 2, -1, None])
def test_transient_inventory_anomalies_then_exact_increment_succeed(observed_delta):
    page = BuyPage()
    page.cards = 5
    original_read = page.query_selector_all
    observations = []

    async def read(selector):
        if page.clicked and selector == "body":
            delta = observed_delta if not observations else 1
            observations.append(delta)
            text = "unknown" if delta is None else f"补签卡: {5 + delta}"
            return [Element(page, text)]
        return await original_read(selector)

    page.query_selector_all = read
    status, message, data = run_buy(page, {"purchase_quantity": 1})
    assert status == "success", (message, data)
    assert observations == [observed_delta, 1]
    assert page.clicked == ["pay"]
    assert data["buy_card"]["bought"] == 1
    round_result = data["buy_card"]["rounds"][0]
    assert round_result["cards_after"] == 6
    assert round_result["verification_checks"] == 2
    assert page.gotos == [SHOP, SHOP]
    assert 0 <= round_result["verification_elapsed_seconds"] < 1


@pytest.mark.parametrize("failure", ["read", "navigate", "blocked_read"])
def test_persistent_verification_errors_wait_for_independent_deadline(failure):
    page = BuyPage()
    page.stale = True
    original_read, original_goto = page.query_selector_all, page.goto
    attempts = []
    logs = []

    async def read(selector):
        if page.clicked and selector == "body" and failure != "navigate":
            assert any("开始库存核验" in message for _, message in logs)
            attempts.append("read")
            if failure == "blocked_read":
                await asyncio.sleep(10)
            raise RuntimeError("inventory temporarily unavailable")
        return await original_read(selector)

    async def goto(url, **kwargs):
        if page.clicked and failure == "navigate":
            attempts.append("navigate")
            raise RuntimeError("same-origin GET temporarily unavailable")
        await original_goto(url, **kwargs)

    page.query_selector_all, page.goto = read, goto
    started = time.monotonic()
    status, _, data = run_buy(page, {
        "purchase_quantity": 3, "verification_timeout_seconds": 2,
    }, log=lambda *entry: logs.append(entry))
    elapsed = time.monotonic() - started
    assert status == "waiting_human"
    assert page.clicked == ["pay"]
    assert data["buy_card"]["bought"] == 0
    assert len(data["buy_card"]["rounds"]) == 1
    round_result = data["buy_card"]["rounds"][0]
    assert round_result["status"] != "success"
    assert round_result["verification_error"]
    assert 1.9 <= round_result["verification_elapsed_seconds"] <= elapsed + 0.05
    assert elapsed < 3.5
    if failure == "blocked_read":
        assert len(attempts) == 1
    else:
        assert len(attempts) >= 2
    assert round_result["verification_checks"] == (len(attempts) if failure == "navigate" else 0)


def test_timeout_last_exact_increment_without_settlement_is_not_success():
    page = BuyPage()
    page.delta = 0
    settlement_checks = []

    async def unsettled_after_first_check(*_args, **_kwargs):
        settlement_checks.append(True)
        if len(settlement_checks) > 1:
            page.cards = 1
            raise RuntimeError("networkidle timeout; submission still in flight")

    page.wait_for_load_state = unsettled_after_first_check
    status, _, data = run_buy(page, {"purchase_quantity": 3})
    assert status == "waiting_human"
    assert page.clicked == ["pay"]
    assert data["buy_card"]["bought"] == 0
    assert len(data["buy_card"]["rounds"]) == 1
    round_result = data["buy_card"]["rounds"][0]
    assert round_result["cards_after"] == 1
    assert round_result["verification_checks"] >= 2
    assert round_result["status"] != "success"
    assert 0.9 <= round_result["verification_elapsed_seconds"] < 2.5


@pytest.mark.parametrize("during", ["wait", "wait_timeout", "read"])
@pytest.mark.parametrize("separate", [False, True])
def test_cross_origin_during_verification_await_stops_without_masking_get(during, separate):
    page = BuyPage()
    original_read = page.query_selector_all
    gotos_at_crossing = []
    reads_after_payment = []

    async def cross_origin():
        await asyncio.sleep(0)
        gotos_at_crossing.extend(page.gotos)
        page.url = "https://evil.example/inventory"

    async def wait(*_args, **_kwargs):
        if during != "read":
            await cross_origin()
            if during == "wait_timeout":
                raise TimeoutError("networkidle timed out after cross-origin navigation")

    class NavigatingText(Element):
        async def inner_text(self):
            await cross_origin()
            # An exact increment must not bypass the post-read origin check.
            return "补签卡: 1"

    async def read(selector):
        if page.clicked and selector == "body":
            reads_after_payment.append(page.url)
            return [NavigatingText(page)]
        return await original_read(selector)

    page.wait_for_load_state, page.query_selector_all = wait, read
    started = time.monotonic()
    status, _, data = run_buy(page, {
        "purchase_quantity": 3, "verification_timeout_seconds": 5,
        "card_count_url": "/inventory" if separate else "",
    })
    assert time.monotonic() - started < 0.5
    assert status == "waiting_human"
    assert page.clicked == ["pay"]
    assert page.url == "https://evil.example/inventory"
    assert gotos_at_crossing and page.gotos == gotos_at_crossing
    expected_gotos = ["https://shop.example/inventory", SHOP] if separate else [SHOP]
    if separate and during == "read":
        expected_gotos.append("https://shop.example/inventory")
    assert page.gotos == expected_gotos
    assert len(reads_after_payment) == (1 if during == "read" else 0)
    assert not page.handlers
    assert data["buy_card"]["bought"] == 0
    assert len(data["buy_card"]["rounds"]) == 1
    round_result = data["buy_card"]["rounds"][0]
    assert round_result["cards_after"] is None
    assert round_result["verification_checks"] == 0
    assert round_result["verification_elapsed_seconds"] < 0.5
    assert "跨源" in round_result["verification_error"]
    assert round_result["status"] != "success"


@pytest.mark.parametrize("outcome", ["settles_exact", "drifts_to_two", "never_settles"])
def test_separate_inventory_get_requires_its_own_settlement_before_success(outcome):
    page = BuyPage()
    original_goto, original_read = page.goto, page.query_selector_all
    events, observations = [], []
    inventory_waits = 0

    async def goto(url, **kwargs):
        await original_goto(url, **kwargs)
        if page.clicked:
            events.append("get")

    async def wait(*_args, **_kwargs):
        nonlocal inventory_waits
        if page.url == SHOP:
            events.append("wait_shop")
            return
        inventory_waits += 1
        events.append("wait_inventory")
        await asyncio.sleep(0.01)
        if inventory_waits == 1 or outcome == "never_settles":
            raise TimeoutError("inventory document is not networkidle yet")
        if outcome == "drifts_to_two":
            page.cards = 2

    async def read(selector):
        if page.clicked and selector == "body":
            events.append("read_inventory")
            observations.append(page.cards)
        return await original_read(selector)

    page.goto, page.wait_for_load_state, page.query_selector_all = goto, wait, read
    status, message, data = run_buy(page, {
        "purchase_quantity": 1 if outcome == "settles_exact" else 3,
        "card_count_url": "/inventory",
    })
    assert events[:4] == ["wait_shop", "get", "wait_inventory", "read_inventory"]
    assert observations[0] == 1 and len(observations) >= 2
    assert page.clicked == ["pay"] and not page.handlers
    assert page.gotos == ["https://shop.example/inventory", SHOP, "https://shop.example/inventory"]
    assert len(data["buy_card"]["rounds"]) == 1
    round_result = data["buy_card"]["rounds"][0]
    assert round_result["verification_checks"] == len(observations)
    assert round_result["dispatched"] and round_result["clicks"] == 1
    if outcome == "settles_exact":
        assert status == "success", (message, data)
        assert data["buy_card"]["bought"] == 1
        assert round_result["status"] == "success"
        assert observations == [1, 1]
        assert 0.1 <= round_result["verification_elapsed_seconds"] < 1
    else:
        assert status == "waiting_human", (message, data)
        assert data["buy_card"]["bought"] == 0
        assert round_result["status"] != "success"
        assert round_result["cards_after"] == (2 if outcome == "drifts_to_two" else 1)
        assert observations[1:] == [round_result["cards_after"]] * (len(observations) - 1)
        assert 0.9 <= round_result["verification_elapsed_seconds"] < 2.5


@pytest.mark.parametrize("stop", ["cancel", "cross_origin", "page_closed"])
@pytest.mark.parametrize("completed_rounds", [0, 1])
def test_verification_unsafe_or_cancelled_stops_immediately_without_next_round(stop, completed_rounds):
    page = BuyPage()
    original_read = page.query_selector_all
    entered = asyncio.Event()
    attempts = []

    async def read(selector):
        if len(page.clicked) > completed_rounds and selector == "body":
            attempts.append(True)
            entered.set()
            if stop == "cancel":
                await asyncio.Event().wait()
            elif stop == "cross_origin":
                page.url = "https://evil.example/inventory"
            else:
                page.is_closed = lambda: True
            raise RuntimeError("verification interrupted")
        return await original_read(selector)

    page.query_selector_all = read

    async def run():
        task = asyncio.create_task(executor._run_buy_card(page, {
            **CONFIG, "purchase_quantity": 3, "verification_timeout_seconds": 5,
        }, [], lambda *_: None))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            if stop == "cancel":
                task.cancel()
            return await asyncio.wait_for(task, timeout=1)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    started = time.monotonic()
    status, _, data = asyncio.run(run())
    assert time.monotonic() - started < 0.5
    assert status == "waiting_human"
    assert page.clicked == ["pay"] * (completed_rounds + 1) and len(attempts) == 1
    assert not page.handlers
    assert data["buy_card"]["bought"] == completed_rounds
    assert len(data["buy_card"]["rounds"]) == completed_rounds + 1
    assert all(item["status"] == "success" for item in data["buy_card"]["rounds"][:-1])
    round_result = data["buy_card"]["rounds"][-1]
    assert round_result["cards_before"] == completed_rounds
    assert round_result["status"] != "success"
    assert round_result["verification_checks"] == 0
    assert round_result["verification_elapsed_seconds"] < 0.5
    assert round_result["verification_error"]
    if stop == "cancel":
        assert "CancelledError" in round_result["verification_error"]


def test_click_committed_then_throws_is_verified_not_replayed():
    page = BuyPage()
    page.after_click_error = True
    status, message, data = run_buy(page, {"purchase_quantity": 1})
    assert status == "success", message
    assert page.clicked == ["pay"]
    assert data["buy_card"]["bought"] == 1
    assert "error" in data["buy_card"]["rounds"][0]
    assert data["buy_card"]["payment_attempted"] is True
    assert data["buy_card"]["rounds"][0]["dispatched"] is True


def test_failed_click_does_not_fallback_to_another_candidate():
    page = BuyPage()
    page.delta, page.after_click_error = 0, True
    page.product_elements["button[type='submit']"].append(Element(page, "duplicate", effect=page.pay))
    status, _, _ = run_buy(page, {"purchase_quantity": 1})
    assert status == "waiting_human"
    assert page.clicked == ["pay"]


def test_fixed_quantity_buys_n_with_empty_pending_and_existing_stock():
    page = BuyPage()
    page.cards = 20
    status, message, data = run_buy(page, {"purchase_quantity": 2}, [])
    assert status == "success", message
    result = data["buy_card"]
    assert (result["mode"], result["requested_quantity"], result["bought"]) == ("fixed", 2, 2)
    assert (result["cards_before"], result["cards_after"]) == (20, 22)


@pytest.mark.parametrize("stock,pending,bought", [(1, ["a", "b", "c"], 2), (3, ["a"], 0), (0, [], 0)])
def test_auto_deducts_stock(stock, pending, bought):
    page = BuyPage()
    page.cards = stock
    status, message, data = run_buy(page, pending=pending)
    assert status == "success", message
    assert data["buy_card"]["mode"] == "auto"
    assert data["buy_card"]["requested_quantity"] == bought
    assert len(page.clicked) == bought


@pytest.mark.parametrize("config", [{"max_price": 1000}, {"max_price": "nan"}])
def test_price_limit(config):
    page = BuyPage()
    status, _, _ = run_buy(page, config)
    assert status == "fail"
    assert not page.clicked


def test_price_rechecked_each_round():
    page = BuyPage()

    async def pay_then_raise_price():
        await page.pay()
        page.price = "2000"

    page.product_elements["button[type='submit']"][0].effect = pay_then_raise_price
    status, _, data = run_buy(page, {"purchase_quantity": 3, "max_price": 1500})
    assert status == "waiting_human"
    assert data["buy_card"]["bought"] == 1
    assert page.clicked == ["pay"]


def test_inventory_drift_between_rounds_stops():
    page = BuyPage()
    original_goto = page.goto

    async def drift(url, **kwargs):
        await original_goto(url, **kwargs)
        if page.clicked:
            page.cards += 1

    page.goto = drift
    status, _, data = run_buy(page, {"purchase_quantity": 3})
    assert status == "waiting_human"
    assert data["buy_card"]["bought"] == 1
    assert page.clicked == ["pay"]


def test_missing_button_times_out_before_payment(monkeypatch):
    monkeypatch.setattr(executor, "SELECTOR_WAIT_MS", 20)
    page = BuyPage()
    page.product_elements = {}
    status, _, data = run_buy(page)
    assert status == "fail"
    assert data["buy_card"]["bought"] == 0
    assert data["buy_card"]["payment_attempted"] is False
    assert data["buy_card"]["rounds"][0]["dispatched"] is False
    assert not page.clicked


@pytest.mark.parametrize("price", ["0", "-100", "garbage", "9" * 400])
def test_invalid_prices(price):
    page = BuyPage()
    page.price = price
    status, _, _ = run_buy(page)
    assert status == "fail"
    assert not page.clicked


@pytest.mark.parametrize("points", ["unreadable", "nan", "inf"])
def test_unreadable_points_is_failure_not_insufficient(points):
    page = BuyPage()
    page.points = points
    status, message, _ = run_buy(page)
    assert status == "fail"
    assert "余额" in message
    assert not page.clicked


@pytest.mark.parametrize("filled", [False, True])
@pytest.mark.parametrize("price,points,expected", [
    ("-1,000.25", 5000, "fail"), ("+1,000.25", -5000, "fail"),
    ("+1,000.25", "+5000.25", "success"), ("+1,000.25", 0, "success"),
])
def test_signed_prices_and_balances_with_actual_filled_defaults(filled, price, points, expected):
    page = BuyPage()
    page.price, page.points = price, points
    config = PostBuyCardConfig().model_dump() if filled else {}
    if isinstance(points, str):
        # Keep the rendered signed balance, but use numeric state for the fake payment.
        page.page_elements[".balance"] = [Element(page, f"魔力值: {points}")]
        page.points = float(points)
        config["points_selectors"] = [".balance"]
    status, message, data = run_buy(page, {**config, **CONFIG, "purchase_quantity": 1})
    assert status == expected, message
    should_pay = expected == "success" and points != 0
    assert bool(page.clicked) == should_pay
    assert data["buy_card"]["payment_attempted"] is should_pay


def test_insufficient_points_keeps_affordable_success():
    page = BuyPage()
    page.points = 1500
    status, message, data = run_buy(page, {"purchase_quantity": 3})
    assert status == "success", message
    assert "魔力值不足" in message
    assert data["buy_card"]["bought"] == 1


@pytest.mark.parametrize("amount", ["- 100", "-\n100"])
@pytest.mark.parametrize("field", ["price", "points"])
def test_spaced_negative_amounts_never_dispatch_payment(field, amount):
    page = BuyPage()
    config = {**PostBuyCardConfig().model_dump(), **CONFIG, "purchase_quantity": 1}
    if field == "price":
        page.price = f"价格: {amount}"
    else:
        page.page_elements[".balance"] = [Element(page, f"魔力值: {amount}")]
        config["points_selectors"] = [".balance"]
    status, _, data = run_buy(page, config)
    assert status == "fail"
    assert not page.clicked
    assert data["buy_card"]["payment_attempted"] is False


@pytest.mark.parametrize("attribute", ["action", "override"])
def test_cross_origin_form_owner_rejected_even_when_trusted(attribute):
    page = BuyPage()
    setattr(page.product_elements["button[type='submit']"][0], attribute, "https://evil.example/pay")
    status, _, _ = run_buy(page, {"trusted_urls": ["https://evil.example"]})
    assert status == "fail"
    assert not page.clicked


def test_cross_origin_inventory_rejected_without_navigation():
    page = BuyPage()
    status, _, _ = run_buy(page, {"card_count_url": "https://evil.example/cards"})
    assert status == "fail"
    assert not page.gotos


def test_native_dialog_dismissed_and_unknown():
    page = BuyPage()
    page.dialog = True
    status, _, data = run_buy(page, {"purchase_quantity": 3})
    assert status == "waiting_human"
    assert page.dismissed and page.clicked == ["pay"]
    assert data["buy_card"]["bought"] == 0
    assert not page.handlers


def test_legacy_fields_are_never_read():
    class PoisonConfig(dict):
        def get(self, key, *args):
            assert key not in {"option_selector", "exchange_action", "submit_value"}
            return super().get(key, *args)

        def __getitem__(self, key):
            assert key not in {"option_selector", "exchange_action", "submit_value"}
            return super().__getitem__(key)

    page = BuyPage()
    config = PoisonConfig({**CONFIG, "purchase_quantity": 1,
                           "option_selector": "bad", "exchange_action": "bad", "submit_value": "bad"})
    status, message, _ = asyncio.run(executor._run_buy_card(page, config, [], lambda *_: None))
    assert status == "success", message


def test_safe_get_refreshes_stale_body_and_separate_inventory_is_explicit():
    for extra in ({}, {"card_count_url": "/inventory"}):
        page = BuyPage()
        page.stale = True
        status, message, data = run_buy(page, {"purchase_quantity": 2, **extra})
        assert status == "success", message
        assert data["buy_card"]["bought"] == 2
        assert len(page.clicked) == 2
        assert all("attendance" not in url for url in page.gotos)
        assert SHOP in page.gotos
        if extra:
            assert "https://shop.example/inventory" in page.gotos


@pytest.mark.parametrize("separate", [False, True])
def test_unsettled_submission_is_not_interrupted_by_refresh(separate):
    page = BuyPage()
    page.settled, page.stale = False, True
    status, _, _ = run_buy(page, {"purchase_quantity": 2,
                                 "card_count_url": "/inventory" if separate else ""})
    assert status == "waiting_human"
    assert page.gotos == (["https://shop.example/inventory", SHOP] if separate else [SHOP])
    assert page.clicked == ["pay"]


@pytest.mark.parametrize("separate", [False, True])
def test_delayed_server_inventory_requires_multiple_safe_gets(separate):
    page = BuyPage()
    original_goto = page.goto
    verification_gets = []

    async def delayed_inventory(url, **kwargs):
        await original_goto(url, **kwargs)
        if page.clicked and (not separate or url.endswith("/inventory")):
            verification_gets.append(url)
            page.cards = 1 if len(verification_gets) >= 2 else 0

    async def payment_pending_settlement():
        page.points -= 1000.25

    page.product_elements["button[type='submit']"][0].effect = payment_pending_settlement
    page.goto = delayed_inventory
    status, message, data = run_buy(page, {"purchase_quantity": 1,
        "verification_timeout_seconds": 3, "card_count_url": "/inventory" if separate else ""})
    assert status == "success", message
    assert data["buy_card"]["bought"] == 1
    assert len(verification_gets) == 2
    assert page.clicked == ["pay"]


def test_dispatch_evidence_is_set_before_click_exception():
    page = BuyPage()

    async def fail_before_receipt():
        raise RuntimeError("transport lost; dispatch outcome unknown")

    page.product_elements["button[type='submit']"][0].effect = fail_before_receipt
    status, _, data = run_buy(page, {"purchase_quantity": 1})
    result = data["buy_card"]
    assert status == "waiting_human"
    assert result["bought"] == 0 and result["payment_attempted"] is True
    assert result["rounds"][0]["clicks"] == 0
    assert result["rounds"][0]["dispatched"] is True


def test_grid_and_custom_require_explicit_price():
    for layout in ("grid", "custom"):
        page = BuyPage()
        status, _, _ = run_buy(page, {"layout": layout, "product_selectors": [".product"]})
        assert status == "fail"
        assert not page.clicked
        page.product_elements[".price"] = [Element(page, "1,000.25")]
        status, message, _ = run_buy(page, {"layout": layout, "product_selectors": [".product"],
                                             "price_selectors": [".price"], "purchase_quantity": 1})
        assert status == "success", message


def test_price_selectors_require_unique_visible_match():
    page = BuyPage()
    page.product_elements[".price"] = [Element(page, "100"), Element(page, "200")]
    status, _, _ = run_buy(page, {"price_selectors": [".price"]})
    assert status == "fail"
    page.product_elements[".price"][0].visible = False
    status, message, _ = run_buy(page, {"price_selectors": [".missing", ".price"], "purchase_quantity": 1})
    assert status == "success", message


def test_points_and_inventory_selector_candidates():
    page = BuyPage()
    page.page_elements[".cards"] = [Element(page, lambda: f"cards={page.cards}")]
    page.page_elements[".points"] = [Element(page, lambda: f"balance={page.points}")]
    status, message, data = run_buy(page, {"purchase_quantity": 1,
        "card_count_selectors": [".missing", ".cards"], "card_count_regex": r"cards=(\d+)",
        "points_selectors": [".missing", ".points"], "points_regex": r"balance=([\d.]+)"})
    assert status == "success", message
    assert data["buy_card"]["cards_after"] == 1


@pytest.mark.parametrize("separate", [False, True])
@pytest.mark.parametrize("readiness", ["delayed", "read_error"])
def test_initial_inventory_polls_current_document_before_opening_shop(separate, readiness):
    page = BuyPage()
    shop = SHOP + "?token=shop-secret"
    inventory = "https://shop.example/inventory?token=inventory-secret"
    original_read = page.query_selector_all
    attempts, logs, gotos_at_payment = [], [], []
    reading_inventory = False

    def log(level, message):
        nonlocal reading_inventory
        logs.append((level, message))
        if "读取购卡库存：" in message:
            reading_inventory = True
        elif "已读取补签卡数量：" in message:
            reading_inventory = False

    async def read(selector):
        if selector == "body" and reading_inventory:
            attempts.append(time.monotonic())
            assert page.gotos == [inventory if separate else shop]
            if readiness == "read_error" and len(attempts) == 1:
                raise RuntimeError("Execution context was destroyed")
            if readiness == "delayed" and len(attempts) <= 2:
                return [Element(page, "库存加载中")]
        if separate and page.url != inventory:
            return [Element(page, lambda: f"魔力值: {page.points}")]
        return await original_read(selector)

    async def pay():
        gotos_at_payment.extend(page.gotos)
        await page.pay()

    page.query_selector_all = read
    page.product_elements["button[type='submit']"][0].effect = pay
    status, message, data = run_buy(page, {
        "shop_url": shop, "card_count_url": inventory if separate else "",
        "purchase_quantity": 1,
    }, log=log)
    assert status == "success", (message, data, logs)
    assert len(attempts) == (3 if readiness == "delayed" else 2)
    assert all(0.18 <= later - earlier < 0.6 for earlier, later in zip(attempts, attempts[1:]))
    assert gotos_at_payment == ([inventory, shop] if separate else [shop])
    assert page.clicked == ["pay"] and not page.handlers
    assert data["buy_card"]["cards_before"] == 0
    assert data["buy_card"]["bought"] == 1
    assert any(f"来源={'独立库存地址' if separate else '商店页面'}" in text for _, text in logs)
    navigation_logs = [text for _, text in logs if "购卡页面导航" in text]
    assert navigation_logs and all("查询参数仅在日志中隐藏，导航保留" in text for text in navigation_logs)
    assert all("?" not in text and "secret" not in text for _, text in logs)


@pytest.mark.parametrize("failure", [
    "invalid_regex", "unmatched_regex", "missing_group", "nonnumeric_group",
    "missing_selector", "invalid_selector", "no_stock", "blocked_read",
])
def test_initial_inventory_failure_uses_full_verification_deadline_without_payment(failure):
    page = BuyPage()
    inventory = "https://shop.example/inventory"
    original_read = page.query_selector_all
    attempts, logs = [], []
    config = {"card_count_url": "/inventory", "purchase_quantity": 1}
    patterns = {
        "invalid_regex": "[", "unmatched_regex": r"不存在的库存: (\d+)",
        "missing_group": r"补签卡: \d+", "nonnumeric_group": r"(补签卡)",
    }
    if failure in patterns:
        config["card_count_regex"] = patterns[failure]
    if failure.endswith("selector"):
        config["card_count_selectors"] = ["[" if failure == "invalid_selector" else ".missing"]

    async def read(selector):
        attempts.append(time.monotonic())
        assert page.gotos == [inventory] and not page.clicked
        if failure == "invalid_selector":
            raise RuntimeError("Invalid selector")
        if failure == "blocked_read":
            await asyncio.Event().wait()
        if failure == "no_stock":
            return [Element(page, "库存加载中")]
        return await original_read(selector)

    page.query_selector_all = read
    started = time.monotonic()
    status, message, data = run_buy(page, config, log=lambda *entry: logs.append(entry))
    elapsed = time.monotonic() - started
    assert status == "fail", (message, data)
    assert 0.9 <= elapsed < 2.5
    assert (len(attempts) == 1) if failure == "blocked_read" else (len(attempts) >= 4)
    assert page.gotos == [inventory] and not page.clicked and not page.handlers
    result = data["buy_card"]
    assert result["payment_attempted"] is False and result["bought"] == 0
    assert result["cards_before"] is None and result["rounds"] == []
    assert "1s" in message and "第一捕获组" in message
    assert any("库存读取等待结束" in text and "第一捕获组" in text for _, text in logs)


@pytest.mark.parametrize("completed_rounds", [0, 1])
@pytest.mark.parametrize("stop", ["cross_origin", "cross_origin_text", "page_closed", "cancel"])
def test_inventory_readiness_unsafe_or_cancelled_never_dispatches_next_payment(stop, completed_rounds):
    page = BuyPage()
    original_read = page.query_selector_all
    entered = asyncio.Event()
    readiness_calls = 0
    attempts, gotos_at_stop = [], []

    def log(_level, message):
        nonlocal readiness_calls
        if "读取购卡库存：" in message:
            readiness_calls += 1

    async def interrupt():
        attempts.append(True)
        gotos_at_stop.extend(page.gotos)
        entered.set()
        if stop == "cancel":
            await asyncio.Event().wait()
        if stop.startswith("cross_origin"):
            page.url = "https://evil.example/inventory"
        else:
            page.is_closed = lambda: True
        if stop != "cross_origin_text":
            raise RuntimeError("inventory read interrupted")
        return f"补签卡: {page.cards}"

    class InterruptedText(Element):
        async def inner_text(self):
            return await interrupt()

    async def read(selector):
        if selector == "body" and readiness_calls == completed_rounds + 1:
            if stop == "cross_origin_text":
                return [InterruptedText(page)]
            await interrupt()
        return await original_read(selector)

    page.query_selector_all = read

    async def run():
        task = asyncio.create_task(executor._run_buy_card(page, {
            **CONFIG, "purchase_quantity": 3, "card_count_url": "/inventory",
            "verification_timeout_seconds": 5,
        }, [], log))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            if stop == "cancel":
                task.cancel()
            if stop == "cancel" and not completed_rounds:
                with pytest.raises(asyncio.CancelledError):
                    await task
                return None
            return await asyncio.wait_for(task, timeout=1)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    started = time.monotonic()
    outcome = asyncio.run(run())
    assert time.monotonic() - started < 0.5
    assert attempts == [True] and not page.handlers
    assert page.gotos == gotos_at_stop
    assert page.clicked == ["pay"] * completed_rounds
    if outcome is not None:
        status, _, data = outcome
        assert status == ("waiting_human" if completed_rounds else "fail")
        result = data["buy_card"]
        assert result["payment_attempted"] is bool(completed_rounds)
        assert result["bought"] == len(result["rounds"]) == completed_rounds
        assert all(item["status"] == "success" for item in result["rounds"])


@pytest.mark.parametrize("separate", [False, True])
@pytest.mark.parametrize("observation", [None, 0, 2, "read_error"])
def test_between_rounds_waits_for_expected_inventory_without_rebuying(separate, observation):
    page = BuyPage()
    original_read = page.query_selector_all
    readiness_calls = 0
    reading_inventory = False
    attempts, gotos_at_wait = [], []

    def log(_level, message):
        nonlocal readiness_calls, reading_inventory
        if "读取购卡库存：" in message:
            readiness_calls += 1
            reading_inventory = True
        elif "已读取补签卡数量：" in message:
            reading_inventory = False

    async def read(selector):
        if selector == "body" and readiness_calls == 2 and reading_inventory:
            attempts.append(time.monotonic())
            assert page.clicked == ["pay"]
            if not gotos_at_wait:
                gotos_at_wait.extend(page.gotos)
            assert page.gotos == gotos_at_wait
            if len(attempts) <= 2:
                if observation == "read_error":
                    raise RuntimeError("inventory DOM rebuilding")
                return [Element(page, "加载中" if observation is None else f"补签卡: {observation}")]
        return await original_read(selector)

    page.query_selector_all = read
    status, message, data = run_buy(page, {
        "purchase_quantity": 2, "card_count_url": "/inventory" if separate else "",
    }, log=log)
    assert status == "success", (message, data)
    assert len(attempts) == 3
    assert all(0.18 <= later - earlier < 0.6 for earlier, later in zip(attempts, attempts[1:]))
    assert page.clicked == ["pay", "pay"] and not page.handlers
    result = data["buy_card"]
    assert result["bought"] == 2 and result["cards_after"] == 2
    assert [item["cards_before"] for item in result["rounds"]] == [0, 1]
    assert all(item["clicks"] == 1 and item["status"] == "success" for item in result["rounds"])


@pytest.mark.parametrize("separate", [False, True])
@pytest.mark.parametrize("observation", [None, 0, 2])
def test_between_rounds_inventory_mismatch_exhausts_deadline_without_next_payment(separate, observation):
    page = BuyPage()
    original_read = page.query_selector_all
    readiness_calls = 0
    attempts, gotos_at_wait = [], []

    def log(_level, message):
        nonlocal readiness_calls
        if "读取购卡库存：" in message:
            readiness_calls += 1

    async def read(selector):
        if selector == "body" and readiness_calls == 2:
            attempts.append(time.monotonic())
            if not gotos_at_wait:
                gotos_at_wait.extend(page.gotos)
            assert page.gotos == gotos_at_wait and page.clicked == ["pay"]
            return [Element(page, "加载中" if observation is None else f"补签卡: {observation}")]
        return await original_read(selector)

    page.query_selector_all = read
    started = time.monotonic()
    status, message, data = run_buy(page, {
        "purchase_quantity": 3, "card_count_url": "/inventory" if separate else "",
    }, log=log)
    assert 0.9 <= time.monotonic() - started < 2.5
    assert status == "waiting_human" and "轮次之间库存异常" in message
    assert len(attempts) >= 4 and page.gotos == gotos_at_wait
    assert page.clicked == ["pay"] and not page.handlers
    result = data["buy_card"]
    assert result["bought"] == 1 and result["cards_after"] == observation
    assert len(result["rounds"]) == 1 and result["rounds"][0]["status"] == "success"


@pytest.mark.parametrize("separate", [False, True])
def test_body_text_only_inventory_uses_custom_regex_first_group(separate):
    page = BuyPage()
    page.cards = 3

    async def read(selector):
        assert selector == "body"
        if separate and page.url == SHOP:
            return [Element(page, lambda: f"魔力值: {page.points}")]
        return [Element(page, lambda: f"你目前拥有补签卡 {page.cards} 张\n魔力值: {page.points}\n编号 99")]

    page.query_selector_all = read
    status, message, data = run_buy(page, {
        "purchase_quantity": 2, "card_count_url": "/inventory" if separate else "",
        "card_count_selectors": [], "card_count_regex": r"你目前拥有补签卡\s+(\d+)\s+(张)",
    })
    assert status == "success", (message, data)
    result = data["buy_card"]
    assert (result["cards_before"], result["cards_after"], result["bought"]) == (3, 5, 2)
    assert page.clicked == ["pay", "pay"]
    assert page.gotos[:2] == (["https://shop.example/inventory", SHOP] if separate else [SHOP, SHOP])


def test_default_timeout_scales_by_quantity_clicks_and_verification():
    one = executor._buy_timeout({"purchase_quantity": 1})
    many = executor._buy_timeout({"purchase_quantity": 365})
    assert many > one
    assert executor._buy_timeout({}) == many
    assert executor._buy_timeout({"purchase_quantity": 1, "purchase_clicks": [
        {"scope": "product", "selectors": [".a", ".b"]},
        {"scope": "page", "selectors": [".c"]},
    ], "verification_timeout_seconds": 60}) > one


def test_timeout_during_payment_verifies_then_stops():
    page = BuyPage()

    async def slow_pay():
        await page.pay()
        await asyncio.sleep(10)

    page.product_elements["button[type='submit']"][0].effect = slow_pay

    async def run():
        return await asyncio.wait_for(executor._run_buy_card(
            page, {**CONFIG, "purchase_quantity": 3}, [], lambda *_: None), timeout=0.02)

    status, _, data = asyncio.run(run())
    assert status == "waiting_human"
    assert page.clicked == ["pay"]
    assert data["buy_card"]["bought"] == 1


@pytest.mark.parametrize("explicit", [None, 7])
@pytest.mark.parametrize("times_out", [False, True])
def test_job_purchase_timeout_and_waiting_human_override_continue(monkeypatch, tmp_path, explicit, times_out):
    from backend.tests.test_worker_navigation import _JumpLane, _JumpManager, _JumpPage

    monkeypatch.setattr(executor, "ARTIFACT_ROOT", tmp_path)
    ran, timeouts = [], []

    async def capture(artifacts, *_args, **_kwargs):
        artifacts.screenshots.append({"kind": "screenshot"})

    async def step(_page, step, *_args, **_kwargs):
        ran.append(step["step_key"])
        if times_out:
            await asyncio.Event().wait()
        return "waiting_human", "unknown payment", {}

    original_wait_for = asyncio.wait_for

    async def record(awaitable, timeout):
        timeouts.append(timeout)
        return await original_wait_for(awaitable, 0.01 if times_out else timeout)

    monkeypatch.setattr(executor.JobArtifacts, "capture_failure", capture)
    monkeypatch.setattr(executor, "_run_step", step)
    monkeypatch.setattr(executor.asyncio, "wait_for", record)
    config = {"purchase_quantity": 9}
    manager = _JumpManager(_JumpLane(_JumpPage()))
    result = asyncio.run(executor.execute_job(manager, {
        "job_id": 1, "run_id": 1, "lane_key": "general", "resign_max_days": 1,
        "steps": [{"step_key": "buy", "feature_type": "post.buy_card", "config": config,
                   "timeout_seconds": explicit, "on_error": "continue"},
                  {"step_key": "never", "feature_type": "post.resign"}],
    }))
    assert result["status"] == "waiting_human"
    assert ran == ["buy"]
    assert timeouts == [explicit if explicit is not None else int(executor._buy_timeout(config))]


@pytest.mark.parametrize("later_status", ["restart", "fallback_interaction"])
def test_job_exposes_purchase_dispatch_evidence_after_later_step(monkeypatch, tmp_path, later_status):
    from backend.tests.test_worker_navigation import _JumpLane, _JumpManager

    class JobBuyPage(BuyPage):
        async def bring_to_front(self):
            pass

        async def close(self):
            pass

    monkeypatch.setattr(executor, "ARTIFACT_ROOT", tmp_path)
    original_step = executor._run_step

    async def step(page, configured_step, *args, **kwargs):
        if configured_step["step_key"] == "later":
            return later_status, "later step asks for replay", {}
        return await original_step(page, configured_step, *args, **kwargs)

    async def capture(artifacts, *_args, **_kwargs):
        artifacts.screenshots.append({"kind": "screenshot"})

    monkeypatch.setattr(executor, "_run_step", step)
    monkeypatch.setattr(executor.JobArtifacts, "capture_failure", capture)
    page = JobBuyPage()
    manager = _JumpManager(_JumpLane(page))
    result = asyncio.run(executor.execute_job(manager, {
        "job_id": 1, "run_id": 1, "lane_key": "general",
        "steps": [{"step_key": "buy", "feature_type": "post.buy_card",
                   "config": {**CONFIG, "purchase_quantity": 1}},
                  {"step_key": "later", "feature_type": "flow.jump"}],
    }))
    purchase = result["steps"][0]["data"]["buy_card"]
    assert purchase["payment_attempted"] is True
    assert purchase["rounds"][0]["dispatched"] is True
    assert purchase["bought"] == 1
    assert page.clicked == ["pay"]
