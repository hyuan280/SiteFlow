"""Template recognizer for the NexusPHP GD font-5 captcha.

The text is pure black, un-antialiased, and placed on a fixed grid:
  x = 25 + 18 * i, y = 12, cell = 9x15
so the exact font bitmaps give a reliable match without any model.
"""

import argparse
import csv
from pathlib import Path

import numpy as np
from PIL import Image

from . import font5
from .generator import (
    CELL_HEIGHT,
    CELL_WIDTH,
    CHARSET,
    HEIGHT,
    LENGTH,
    TEXT_X,
    TEXT_Y,
    WIDTH,
)


class NexusPhpGD5Recognizer:
    def __init__(self):
        self.templates = {
            ch: (font5.GLYPHS[ch] > 0) for ch in CHARSET
        }

    def _recognize_normalized(self, image):
        rgb = np.asarray(image.convert("RGB"))
        black = rgb.max(axis=2) < 60
        chars = []
        margins = []
        for i in range(LENGTH):
            x = TEXT_X + i * CELL_WIDTH * 2
            cell = black[TEXT_Y : TEXT_Y + CELL_HEIGHT, x : x + CELL_WIDTH]
            scores = []
            for ch, template in self.templates.items():
                score = int(np.count_nonzero(cell & ~template))
                score += int(np.count_nonzero(~cell & template))
                scores.append((score, ch))
            scores.sort()
            best_score, best_char = scores[0]
            second_score = scores[1][0]
            chars.append(best_char)
            margins.append(second_score - best_score)
        return "".join(chars), min(margins)

    def recognize(self, image):
        image = image.convert("RGB")
        if image.size == (WIDTH, HEIGHT):
            return self._recognize_normalized(image)
        candidates = []
        for resample in (Image.Resampling.NEAREST, Image.Resampling.LANCZOS):
            normalized = image.resize((WIDTH, HEIGHT), resample)
            candidates.append(self._recognize_normalized(normalized))
        return max(candidates, key=lambda item: item[1])

    def recognize_file(self, path):
        return self.recognize(Image.open(path))

    def recognize_bytes(self, data):
        from io import BytesIO

        return self.recognize(Image.open(BytesIO(data)))


def evaluate_csv(dataset_dir, split="test"):
    split_dir = Path(dataset_dir) / split
    labels_path = split_dir / "labels.csv"
    if not labels_path.is_file():
        raise SystemExit(f"missing labels: {labels_path}")
    recognizer = NexusPhpGD5Recognizer()
    total = 0
    correct = 0
    min_margin = None
    with labels_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            path = split_dir / "images" / row["filename"]
            text, margin = recognizer.recognize_file(path)
            total += 1
            correct += int(text == row["text"])
            min_margin = margin if min_margin is None else min(min_margin, margin)
    accuracy = correct / total if total else 0.0
    print(f"{split}: {correct}/{total} correct = {accuracy:.6f}")
    print(f"{split}: min margin = {min_margin}")
    return accuracy


def evaluate_real():
    recognizer = NexusPhpGD5Recognizer()
    cases = {
        "real_001.png": "58N7NH",
        "real_002.png": "MB9R8P",
    }
    root = Path(__file__).resolve().parents[2] / "images"
    for filename, expected in cases.items():
        text, margin = recognizer.recognize_file(root / filename)
        print(f"{filename}: predicted={text} expected={expected} margin={margin} ok={text == expected}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, help="recognize one image")
    parser.add_argument("--dataset", type=Path, help="dataset root with labels.csv splits")
    parser.add_argument("--split", default="test")
    parser.add_argument("--real", action="store_true", help="check the two real samples")
    args = parser.parse_args(argv)

    if args.image is not None:
        text, margin = NexusPhpGD5Recognizer().recognize_file(args.image)
        print(f"{text}\tmargin={margin}")
    if args.dataset is not None:
        evaluate_csv(args.dataset, args.split)
    if args.real:
        evaluate_real()
    if args.image is None and args.dataset is None and not args.real:
        parser.error("use --image, --dataset or --real")


if __name__ == "__main__":
    main()
