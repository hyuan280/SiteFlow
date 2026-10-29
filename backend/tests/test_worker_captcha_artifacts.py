from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker import executor  # noqa: E402


def test_job_artifacts_saves_captcha_image(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(executor, "ARTIFACT_ROOT", tmp_path)
    artifacts = executor.JobArtifacts({"run_id": 1, "job_id": 2})

    assert artifacts.save_image(
        b"captcha-png",
        prefix="captcha",
        label="验证码截图 · 登录",
        step_key="sign",
    )
    captcha = [item for item in artifacts.snapshot() if item["kind"] == "captcha"]
    assert len(captcha) == 1
    assert captcha[0]["step_key"] == "sign"
    assert captcha[0]["content_type"] == "image/png"
    assert (tmp_path / captcha[0]["relative_path"]).read_bytes() == b"captcha-png"


def test_read_captcha_saves_initial_and_refreshed_images_and_logs_text(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(executor, "ARTIFACT_ROOT", tmp_path)
    logs: list[str] = []
    captured: list[tuple[bytes, str]] = []
    results = iter([("bad", 0.1), ("ab12", 0.99)])
    monkeypatch.setattr(executor, "_classify_captcha", lambda _image: next(results))
    refreshed = AsyncMock()
    refreshed.screenshot.return_value = b"refreshed-png"

    async def fake_refresh(*_args):
        return refreshed, b"refreshed-png"

    monkeypatch.setattr(executor, "_refresh_captcha_image", fake_refresh)
    image = AsyncMock()
    image.screenshot.return_value = b"initial-png"

    async def capture(image_bytes: bytes, label: str) -> None:
        captured.append((image_bytes, label))

    status, text, message, data = asyncio.run(
        executor._read_captcha(
            None,
            image,
            {"charset": "alnum", "length": 4},
            lambda _level, value: logs.append(value),
            None,
            capture,
        )
    )

    assert (status, text, message) == ("ok", "ab12", "")
    assert data["refreshed"] is True
    assert captured == [
        (b"initial-png", "验证码截图"),
        (b"refreshed-png", "验证码截图（刷新后）"),
    ]
    assert any("bad" in line for line in logs)
    assert any("ab12" in line for line in logs)
