from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker.executor import (  # noqa: E402
    SYSTEM_RESIGN_SIGNAL_KEY,
    _condition_allows,
    _run_buy_card,
    _run_resign,
)


class FakeLocator:
    def __init__(self, page: "FakePage", selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> "FakeLocator":
        return self

    async def inner_text(self) -> str:
        return self.page.calendar_title

    async def click(self, **_kwargs) -> None:
        return None


class FakePage:
    def __init__(self) -> None:
        self.url = "https://pt.example.com/attendance.php"
        self.calendar_title = "2026年10月"
        self.cards = 3
        self.points = 5000
        self.missed: set[str] = set()
        self.bought = 0

    async def inner_text(self, _selector: str) -> str:
        return f"补签卡: {self.cards}\n魔力值: {self.points}"

    async def wait_for_load_state(self, *_args, **_kwargs) -> None:
        return None

    async def goto(self, url: str, **_kwargs) -> None:
        self.url = url

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    async def evaluate(self, script: str, arg=None):  # type: ignore[no-untyped-def]
        if "const days = Array.from" in script:
            return "missed" if arg["date"] in self.missed else "signed"
        if "const headings = Array.from" in script:
            return {"option": "12", "price": "1000"}
        if "dateParam" in script:
            return {
                "status": 200,
                "text": json.dumps({"ret": 0, "msg": "OK", "data": {"points": 1}}),
            }
        if "const body = new URLSearchParams" in script:
            self.bought += 1
            self.cards += 1
            self.points -= 1000
            return {"status": 200, "text": "ok"}
        raise AssertionError(f"unexpected evaluate: {script[:80]}")


def test_runtime_resign_signal() -> None:
    async def run() -> None:
        signals = {
            SYSTEM_RESIGN_SIGNAL_KEY: {
                "signal_key": SYSTEM_RESIGN_SIGNAL_KEY,
                "label": "断签",
                "enabled": True,
            }
        }
        assert (
            await _condition_allows(
                None,
                f"signal:{SYSTEM_RESIGN_SIGNAL_KEY}:true",
                signals,
                runtime_signals={SYSTEM_RESIGN_SIGNAL_KEY: (True, "待补签 2 天")},
            )
            == "true"
        )
        assert (
            await _condition_allows(
                None,
                f"signal:{SYSTEM_RESIGN_SIGNAL_KEY}:false",
                signals,
                runtime_signals={SYSTEM_RESIGN_SIGNAL_KEY: (False, "没有未签到日期")},
            )
            == "true"
        )
        assert (
            await _condition_allows(
                None,
                f"signal:{SYSTEM_RESIGN_SIGNAL_KEY}:true",
                signals,
                runtime_signals={SYSTEM_RESIGN_SIGNAL_KEY: (False, "没有未签到日期")},
            )
            == "false"
        )

    asyncio.run(run())


def test_runtime_compound_conditions() -> None:
    async def run() -> None:
        signals = {
            "sig_a": {"signal_key": "sig_a", "label": "A", "enabled": True},
            SYSTEM_RESIGN_SIGNAL_KEY: {
                "signal_key": SYSTEM_RESIGN_SIGNAL_KEY,
                "label": "断签",
                "enabled": True,
            },
        }
        runtime = {
            "sig_a": (True, "页面条件成立"),
            SYSTEM_RESIGN_SIGNAL_KEY: (False, "没有未签到日期"),
        }
        assert (
            await _condition_allows(
                None,
                "all(signal:sig_a:true,signal:sys_resign_pending:true)",
                signals,
                runtime_signals=runtime,
            )
            == "false"
        )
        assert (
            await _condition_allows(
                None,
                "any(signal:sig_a:true,signal:sys_resign_pending:true)",
                signals,
                runtime_signals=runtime,
            )
            == "true"
        )

    asyncio.run(run())


def test_run_resign_marks_missed_dates() -> None:
    async def run() -> None:
        page = FakePage()
        page.missed = {"2026-10-01", "2026-10-02"}
        messages: list[tuple[str, str]] = []
        status, message, data = await _run_resign(
            page,
            {"attendance_url": "https://pt.example.com/attendance.php"},
            ["2026-10-01", "2026-10-02"],
            lambda level, text: messages.append((level, text)),
        )
        assert status == "success", message
        assert [item["date"] for item in data["resign"]["success"]] == [
            "2026-10-01",
            "2026-10-02",
        ]
        assert any("补签成功" in text for _, text in messages)

    asyncio.run(run())


def test_run_resign_uses_available_cards_without_failing() -> None:
    async def run() -> None:
        page = FakePage()
        page.cards = 1
        page.missed = {"2026-10-01", "2026-10-02"}
        status, message, data = await _run_resign(
            page,
            {"attendance_url": "https://pt.example.com/attendance.php"},
            ["2026-10-01", "2026-10-02"],
            lambda _level, _text: None,
        )
        assert status == "success", message
        assert len(data["resign"]["success"]) == 1
        assert data["resign"]["remaining"] == ["2026-10-02"]

    asyncio.run(run())


def test_run_buy_card_with_insufficient_points_is_not_failure() -> None:
    async def run() -> None:
        page = FakePage()
        page.cards = 0
        page.points = 500
        status, message, data = await _run_buy_card(
            page,
            {"shop_url": "https://pt.example.com/mybonus.php", "purchase_interval_seconds": 0},
            ["2026-10-01", "2026-10-02"],
            lambda _level, _text: None,
        )
        assert status == "success", message
        assert data["buy_card"]["bought"] == 0
        assert "魔力值不足" in message

    asyncio.run(run())


def test_run_buy_card_tops_up_missing_cards() -> None:
    async def run() -> None:
        page = FakePage()
        page.cards = 1
        status, message, data = await _run_buy_card(
            page,
            {
                "shop_url": "https://pt.example.com/mybonus.php",
                "purchase_interval_seconds": 0,
            },
            ["2026-10-01", "2026-10-02"],
            lambda _level, _text: None,
        )
        assert status == "success", message
        assert data["buy_card"]["bought"] == 1
        assert page.cards == 2

    asyncio.run(run())
