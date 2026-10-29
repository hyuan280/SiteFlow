from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker import executor, vision  # noqa: E402


class FakePage:
    url = "https://pt.example.com/showup.php"

    async def wait_for_load_state(self, *_args, **_kwargs) -> None:
        return None


class FakeButton:
    def __init__(self) -> None:
        self.clicked = False

    async def click(self, **_kwargs) -> None:
        self.clicked = True


class FakeInput:
    def __init__(self) -> None:
        self.value = ""

    async def fill(self, value: str, **_kwargs) -> None:
        self.value = value


def test_image_quiz_without_vision_requires_human() -> None:
    for vision_config in (None, {"execution_settings": {}}):
        logs: list[str] = []
        result = asyncio.run(
            executor._run_image_quiz(
                FakePage(),
                {},
                lambda _level, message: logs.append(message),
                vision_config,
            )
        )

        assert result[0] == "waiting_human"
        assert result[2]["reason"] == "vision_unavailable"
        assert "视觉模型" in result[1]


def test_image_quiz_selects_option_and_fills_message(monkeypatch) -> None:
    option = FakeButton()
    message = FakeInput()
    captured: list[tuple[bytes, str]] = []
    logs: list[str] = []
    seen_prompt: list[str | None] = []
    form = {
        "question": "图中是哪部作品？",
        "options": ["作品 A", "作品 B"],
        "option_rows": [
            ("作品 A", FakeButton(), False),
            ("作品 B", option, True),
        ],
        "answer_selector": "input[name^='captcha_']",
        "image": b"quiz-image",
    }

    async def fake_read_form(*_args):
        return form, ""

    async def fake_find(page, selectors, **_kwargs):
        if selectors == ["textarea[name='message']"]:
            return message
        return None

    async def fake_success(*_args):
        return True, "missing:input[name^='captcha_']"

    async def capture(image: bytes, label: str) -> None:
        captured.append((image, label))

    def fake_answer(*args):
        seen_prompt.append(args[4])
        return 2, "标记在作品 B"

    monkeypatch.setattr(executor, "_read_quiz_form", fake_read_form)
    monkeypatch.setattr(executor, "_find_locator", fake_find)
    monkeypatch.setattr(executor, "_wait_quiz_success", fake_success)
    monkeypatch.setattr(vision, "answer_image_quiz", fake_answer)

    status, text, data = asyncio.run(
        executor._run_image_quiz(
            FakePage(),
            {
                "message_selector": ["textarea[name='message']"],
                "message_text": "每天长知识！",
                "submit_selector": [],
            },
            lambda _level, value: logs.append(value),
            {
                "base_url": "https://vision.example/v1",
                "model": "vision",
                "api_key": "secret",
                "execution_settings": {"llm_prompt": "自定义图片答题提示词"},
            },
            capture,
        )
    )

    assert status == "success"
    assert text == "图片答题签到已提交"
    assert data["selected_index"] == 2
    assert data["selected_option"] == "作品 B"
    assert option.clicked is True
    assert message.value == "每天长知识！"
    assert captured == [(b"quiz-image", "图片答题截图")]
    assert seen_prompt == ["自定义图片答题提示词"]
    assert any("作品 B" in line for line in logs)
