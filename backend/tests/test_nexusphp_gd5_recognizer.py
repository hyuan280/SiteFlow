from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytest.importorskip("numpy")
pytest.importorskip("PIL")

from worker.nexusphp_gd5 import NexusPhpGD5Generator, NexusPhpGD5Recognizer  # noqa: E402


def test_nexusphp_gd5_recognizer_roundtrip() -> None:
    generator = NexusPhpGD5Generator(seed=7)
    recognizer = NexusPhpGD5Recognizer()
    for _ in range(25):
        image, text = generator.render()
        predicted, margin = recognizer.recognize(image)
        assert predicted == text
        assert margin > 0


def test_nexusphp_gd5_recognizer_accepts_scaled_images() -> None:
    from io import BytesIO

    from PIL import Image

    generator = NexusPhpGD5Generator(seed=11)
    recognizer = NexusPhpGD5Recognizer()
    for size, resample in (
        ((151, 41), Image.Resampling.LANCZOS),
        ((300, 80), Image.Resampling.NEAREST),
    ):
        image, text = generator.render()
        scaled = image.resize(size, resample)
        predicted, margin = recognizer.recognize(scaled)
        assert len(predicted) == 6
        assert isinstance(margin, int)
        if size == (300, 80):
            assert predicted == text
            buffer = BytesIO()
            scaled.save(buffer, format="PNG")
            from worker.executor import _recognize_nexusphp_gd5

            assert _recognize_nexusphp_gd5(buffer.getvalue())[0] == text
