from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker import executor  # noqa: E402


def test_nexusphp_gd5_method_returns_text_and_margin(monkeypatch) -> None:
    monkeypatch.setattr(
        executor,
        "_recognize_nexusphp_gd5",
        lambda _image: ("AB12CD", 14),
    )
    monkeypatch.setattr(
        executor,
        "_classify_captcha",
        lambda _image: (_ for _ in ()).throw(
            AssertionError("ddddocr must be skipped")
        ),
    )
    image = AsyncMock()
    image.screenshot.return_value = b"cropped-png"

    status, text, message, data = asyncio.run(
        executor._read_captcha(
            None,
            image,
            {
                "captcha_method": "nexusphp_gd5",
                "charset": "alnum",
                "length": 6,
            },
            lambda *args: None,
        )
    )

    assert (status, text, message) == ("ok", "AB12CD", "")
    assert data["provider"] == "nexusphp_gd5"
    assert data["match_margin"] == 14
    assert "confidence" not in data


def test_nexusphp_gd5_method_restarts_when_match_is_ambiguous(monkeypatch) -> None:
    monkeypatch.setattr(
        executor,
        "_recognize_nexusphp_gd5",
        lambda _image: ("AB12CD", 0),
    )

    async def no_refresh(*_args):
        return None

    monkeypatch.setattr(executor, "_refresh_captcha_image", no_refresh)
    image = AsyncMock()
    image.screenshot.return_value = b"cropped-png"

    status, text, _message, data = asyncio.run(
        executor._read_captcha(
            None,
            image,
            {
                "captcha_method": "nexusphp_gd5",
                "charset": "alnum",
                "length": 6,
            },
            lambda *args: None,
        )
    )

    assert status == "restart"
    assert text == ""
    assert data["provider"] == "nexusphp_gd5"
    assert "模板匹配不唯一" in data["reason"]


def test_unknown_captcha_method_fails_without_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        executor,
        "_classify_captcha",
        lambda _image: (_ for _ in ()).throw(
            AssertionError("ddddocr must be skipped")
        ),
    )
    image = AsyncMock()
    image.screenshot.return_value = b"cropped-png"

    status, _text, message, data = asyncio.run(
        executor._read_captcha(
            None,
            image,
            {"captcha_method": "missing"},
            lambda *args: None,
        )
    )

    assert status == "fail"
    assert "未知的验证码识别方式" in message
    assert data["reason"] == "unknown_method"
