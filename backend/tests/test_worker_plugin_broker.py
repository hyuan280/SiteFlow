from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker.executor import _read_captcha, _run_plugin_step  # noqa: E402
from worker.plugin_broker import (
    BrokerActionError,
    BrokerDenied,
    BrokerNotFound,
    PluginBroker,
)


class FakeLocator:
    def __init__(self, page: "FakePage", selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> "FakeLocator":
        return self

    async def wait_for(self, *, state: str, timeout: int) -> None:
        assert timeout > 0
        if self.selector == "#missing":
            raise RuntimeError("not found")

    async def click(self, *, timeout: int) -> None:
        self.page.clicked.append(self.selector)
        await self.page.on_click(self.selector)

    async def fill(self, value: str, *, timeout: int) -> None:
        self.page.filled.append((self.selector, value))

    async def inner_text(self, *, timeout: int) -> str:
        return self.page.texts.get(self.selector, "")

    async def count(self) -> int:
        return 1 if self.selector in self.page.texts else 0


class FakePage:
    def __init__(self, url: str = "https://pt.example.com/") -> None:
        self.url = url
        self.clicked: list[str] = []
        self.filled: list[tuple[str, str]] = []
        self.texts = {"#success": "签到成功"}
        self.context = None

    async def on_click(self, selector: str) -> None:
        return None

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.url = url

    async def screenshot(self, *, type: str) -> bytes:
        return b"image"


def test_broker_actions_and_scope() -> None:
    async def run() -> None:
        page = FakePage()
        broker = PluginBroker("http://worker:6300/plugin-broker")
        token = broker.register(
            page,
            site_origin="https://pt.example.com",
            step_key="checkin",
            approved_capabilities=["browser_actions"],
            authorize=AsyncMock(),
        )
        try:
            clicked = await broker.execute(
                {"token": token, "op": "click", "selectors": ["#sign"]}
            )
            assert clicked["ok"] is True
            assert page.clicked == ["#sign"]

            filled = await broker.execute(
                {
                    "token": token,
                    "op": "fill",
                    "selector": "#message",
                    "value": "hello",
                }
            )
            assert filled["ok"] is True
            assert page.filled == [("#message", ""), ("#message", "hello")]

            text = await broker.execute(
                {"token": token, "op": "read_text", "selector": "#success"}
            )
            assert text["text"] == "签到成功"

            navigated = await broker.execute(
                {
                    "token": token,
                    "op": "goto",
                    "url": "https://pt.example.com/index.php",
                }
            )
            assert navigated["ok"] is True

            with pytest.raises(BrokerDenied):
                await broker.execute(
                    {
                        "token": token,
                        "op": "goto",
                        "url": "https://evil.example.com/",
                    }
                )
            with pytest.raises(BrokerDenied):
                await broker.execute(
                    {"token": token, "op": "evaluate", "script": "1"}
                )
            with pytest.raises(BrokerActionError):
                await broker.execute(
                    {"token": token, "op": "click", "selector": "#missing"}
                )
        finally:
            broker.release(token)
        with pytest.raises(BrokerNotFound):
            await broker.execute({"token": token, "op": "click", "selector": "#sign"})

    asyncio.run(run())


def test_plugin_step_uses_broker_and_grpc_client(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_execute_step(endpoint: str, token: str, **kwargs):
        calls.append({"endpoint": endpoint, "token": token, **kwargs})
        return {"status": "success", "message": "remote ok", "data": {"remote": True}}

    monkeypatch.setattr("worker.plugin_client.execute_step", fake_execute_step)
    monkeypatch.setattr("worker.plugin_authorization.authorize", AsyncMock())

    class FakeBroker:
        public_url = "http://worker:6300/plugin-broker"

        def __init__(self) -> None:
            self.registered: dict[str, Any] = {}
            self.released = ""

        def register(self, page, **kwargs):
            self.registered = kwargs
            return "broker-token"

        def release(self, token: str) -> None:
            self.released = token

        async def release_and_wait(self, token: str) -> None:
            self.release(token)

    async def run() -> tuple[str, str, dict[str, Any]]:
        page = FakePage()
        broker = FakeBroker()
        return await _run_plugin_step(
            page,
            {"step_key": "checkin", "timeout_seconds": 15},
            {"button_selector": ["#sign"]},
            "plugin.acme.checkin",
            {"acme": {"endpoint": "plugin-acme:7000", "token": "plugin-token", "approved_capabilities": ["features", "browser_actions"]}},
            broker,
            lambda _level, _message: None,
        )

    status, message, data = asyncio.run(run())
    assert status == "success"
    assert message == "remote ok"
    assert data == {"remote": True}
    assert calls[0]["endpoint"] == "plugin-acme:7000"
    assert calls[0]["token"] == "plugin-token"
    assert calls[0]["feature_key"] == "plugin.acme.checkin"
    assert calls[0]["broker_url"] == "http://worker:6300/plugin-broker"
    assert calls[0]["broker_token"] == "broker-token"
    assert calls[0]["site_origin"] == "https://pt.example.com"


def test_plugin_step_requires_active_runtime() -> None:
    async def run() -> tuple[str, str, dict[str, Any]]:
        return await _run_plugin_step(
            FakePage(),
            {"step_key": "checkin"},
            {},
            "plugin.acme.checkin",
            {},
            None,
            lambda _level, _message: None,
        )

    status, message, _ = asyncio.run(run())
    assert status == "fail"
    assert "未启用" in message


def test_plugin_captcha_uses_grpc_client(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_recognize(endpoint: str, token: str, **kwargs):
        calls.append({"endpoint": endpoint, "token": token, **kwargs})
        return {"text": "AB12", "confidence": 0.9, "margin": 3, "meta": {}}

    monkeypatch.setattr("worker.plugin_client.recognize_captcha", fake_recognize)
    monkeypatch.setattr("worker.plugin_authorization.authorize", AsyncMock())

    class FakeImage:
        async def screenshot(self, *, type: str, timeout: int) -> bytes:
            return b"image"

    async def run() -> tuple[str, str, str, dict[str, Any]]:
        return await _read_captcha(
            FakePage(),
            FakeImage(),
            {
                "captcha_method": "plugin.acme.gd5",
                "charset": "alnum",
                "length": 4,
            },
            lambda _level, _message: None,
            plugin_runtimes={
                "acme": {
                    "endpoint": "plugin-acme:7000",
                    "token": "plugin-token",
                }
            },
        )

    status, text, message, data = asyncio.run(run())
    assert status == "ok"
    assert text == "AB12"
    assert message == ""
    assert data["provider"] == "plugin.acme.gd5"
    assert calls[0]["method_key"] == "plugin.acme.gd5"
    assert calls[0]["charset"] == "alnum"
    assert calls[0]["length"] == 4
    assert calls[0]["image"] == b"image"


def test_broker_click_switches_to_same_origin_popup() -> None:
    class FakeContext:
        def __init__(self) -> None:
            self.pages = []

    class PopupPage(FakePage):
        def __init__(self, wanted: bool = False) -> None:
            super().__init__()
            self.wanted = wanted
            self.context = FakeContext()
            self.context.pages.append(self)

        async def on_click(self, selector: str) -> None:
            if not self.wanted:
                return
            popup = PopupPage()
            popup.url = "https://pt.example.com/lucky"
            popup.texts = {"#result": "抽奖成功"}
            self.context.pages.append(popup)

        async def wait_for_load_state(self, state: str, *, timeout: int) -> None:
            return None

    async def run() -> None:
        page = PopupPage(wanted=True)
        broker = PluginBroker("http://worker:6300/plugin-broker")
        token = broker.register(page, site_origin="https://pt.example.com", approved_capabilities=["browser_actions"], authorize=AsyncMock())
        try:
            result = await broker.execute(
                {"token": token, "op": "click", "selector": "a[target=_blank]"}
            )
            assert result["switched_page"] is True
            assert result["url"] == "https://pt.example.com/lucky"
            text = await broker.execute(
                {"token": token, "op": "read_text", "selector": "#result"}
            )
            assert text["text"] == "抽奖成功"
        finally:
            broker.release(token)

    asyncio.run(run())
