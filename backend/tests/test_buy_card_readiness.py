from __future__ import annotations

import asyncio
import time

import pytest

from tests.test_buy_card_clicks import BuyPage, Element, SHOP, run_buy
from worker import executor


@pytest.mark.parametrize("stock", [0, 3])
@pytest.mark.parametrize("state", ["ready", "delayed", "always_busy", "evaluate_error"])
def test_piggo_initial_placeholder_zero_requires_readiness(stock, state):
    page = BuyPage()
    original_read = page.query_selector_all
    readiness, observations = [], []

    async def evaluate(script):
        assert "document.readyState" in script
        readiness.append(True)
        if state == "evaluate_error":
            raise RuntimeError("Execution context was destroyed")
        ready = state == "ready" or (state == "delayed" and len(readiness) > 4)
        page.cards = stock if ready else 0
        return ready

    async def read(selector):
        if selector == ".cal-help":
            observations.append(page.cards)
            return [Element(page, f"补签卡（当前 {page.cards} 张）")]
        return await original_read(selector)

    page.evaluate, page.query_selector_all = evaluate, read
    started = time.monotonic()
    status, message, data = run_buy(page, {
        "card_count_url": "/calendar", "card_count_selectors": [".cal-help"],
        "card_count_regex": r"补签卡（当前\s*(\d+)\s*张",
    }, pending=["a"] * stock)
    result = data["buy_card"]
    assert not page.clicked and not result["payment_attempted"]
    assert page.gotos == ["https://shop.example/calendar"]
    if state in {"always_busy", "evaluate_error"}:
        assert status == "fail", message
        assert result["cards_before"] is None
        assert 0.9 <= time.monotonic() - started < 2.5
        assert observations == [0] * len(observations) and len(observations) >= 4
    else:
        assert status == "success", message
        assert result["cards_before"] == stock
        assert observations == ([0, 0, stock] if state == "delayed" else [stock])


@pytest.mark.parametrize("phase", ["inventory", "shop", "verification"])
def test_readiness_becoming_busy_during_text_read_never_accepts_it(phase):
    page = BuyPage()
    original_read = page.query_selector_all
    count_reads = 0

    class BusyText(Element):
        async def inner_text(self):
            page.ready = False
            return await super().inner_text()

    async def read(selector):
        nonlocal count_reads
        if selector == "body":
            count_reads += 1
            if phase == "inventory" or (phase == "shop" and count_reads > 1) or page.clicked:
                return [BusyText(page, f"补签卡: {page.cards}\n魔力值: {page.points}")]
        return await original_read(selector)

    page.query_selector_all = read
    status, _, data = run_buy(page, {"purchase_quantity": 2})
    assert status == ("waiting_human" if phase == "verification" else "fail")
    assert page.clicked == (["pay"] if phase == "verification" else [])
    assert data["buy_card"]["bought"] == 0


@pytest.mark.parametrize("delay", ["product", "price", "balance", "readiness", "parse_error"])
def test_shop_wait_retries_each_round_and_preserves_tab_query(monkeypatch, delay):
    page = BuyPage()
    shop = "https://shop.example/mybonus?tab=shop"
    original_goto, original_extract = page.goto, executor._extract_card_info
    attempts, navigations = [], []

    async def goto(url, **kwargs):
        await original_goto(url, **kwargs)
        if url == shop:
            navigations.append(url)
            attempts.append(0)

    async def evaluate(script):
        assert "document.readyState" in script
        if page.url == shop and delay == "readiness":
            attempts[-1] += 1
            return attempts[-1] > 2
        return True

    async def extract(config_page, config, log):
        attempts[-1] += 1
        if attempts[-1] <= 2:
            if delay == "parse_error":
                raise RuntimeError("DOM temporarily rebuilding")
            if delay in {"product", "price"}:
                return None
        return await original_extract(config_page, config, log)

    original_read = page.query_selector_all

    async def read(selector):
        if selector == ".balance":
            return [Element(page, "加载中" if delay == "balance" and attempts[-1] <= 2
                            else f"余额: {page.points}")]
        return await original_read(selector)

    page.goto, page.evaluate, page.query_selector_all = goto, evaluate, read
    monkeypatch.setattr(executor, "_extract_card_info", extract)
    status, message, data = run_buy(page, {
        "shop_url": shop, "card_count_url": "/calendar", "purchase_quantity": 2,
        "points_selectors": [".balance"], "points_regex": r"余额:\s*([\d.]+)",
    })
    assert status == "success", (message, data)
    assert data["buy_card"]["bought"] == 2 and page.clicked == ["pay", "pay"]
    assert len(navigations) >= 2 and all(value == shop for value in navigations)
    assert all(url == shop or url == "https://shop.example/calendar" for url in page.gotos)
    assert len([count for count in attempts if count >= 3]) == 2


@pytest.mark.parametrize("separate", [False, True])
def test_post_payment_busy_exact_increment_is_not_verified_or_refreshed(separate):
    page = BuyPage()

    async def pay():
        await page.pay()
        page.ready = False

    page.product_elements["button[type='submit']"][0].effect = pay
    status, _, data = run_buy(page, {"purchase_quantity": 3,
                                    "card_count_url": "/calendar" if separate else ""})
    assert status == "waiting_human"
    assert page.clicked == ["pay"] and data["buy_card"]["bought"] == 0
    assert page.gotos == (["https://shop.example/calendar", SHOP] if separate else [SHOP])
    assert data["buy_card"]["rounds"][0]["status"] != "success"


def test_readiness_evaluate_exception_is_fail_closed():
    class BrokenPage:
        async def evaluate(self, _script):
            raise RuntimeError("temporary context destruction")

    assert asyncio.run(executor._buy_page_ready(BrokenPage())) is False


@pytest.mark.parametrize("failure", ["busy", "evaluate_error", "parse_error", "blocked_parse"])
def test_shop_readiness_has_bounded_deadline_without_payment(monkeypatch, failure):
    page = BuyPage()
    attempts = []

    async def evaluate(_script):
        if page.url.endswith("/calendar"):
            return True
        attempts.append("ready")
        if failure == "evaluate_error":
            raise RuntimeError("Execution context was destroyed")
        return failure != "busy"

    async def extract(*_args):
        attempts.append("parse")
        if failure == "blocked_parse":
            await asyncio.Event().wait()
        raise RuntimeError("DOM temporarily rebuilding")

    page.evaluate = evaluate
    monkeypatch.setattr(executor, "_extract_card_info", extract)
    started = time.monotonic()
    status, _, data = run_buy(page, {"card_count_url": "/calendar", "purchase_quantity": 1})
    assert status == "fail"
    assert 0.9 <= time.monotonic() - started < 2.5
    assert not page.clicked and not data["buy_card"]["payment_attempted"]
    assert data["buy_card"]["rounds"] == []
    assert page.gotos == ["https://shop.example/calendar", SHOP]
    assert len(attempts) >= (2 if failure == "blocked_parse" else 4)
