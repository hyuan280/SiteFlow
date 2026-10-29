"""Generator for the NexusPHP GD font-5 captcha.

Rules reproduced from:
  app/Services/Captcha/Drivers/ImageCaptchaDriver.php
  include/functions.php::random_str()

  - background: random reg1.png ... reg5.png (150x40)
  - dots: floor(150*40/35) = 171 random RGB pixels
  - text: 6 chars, each separated by one blank cell
  - font: GD built-in font 5 (gdFontGiant, 9x15)
  - text origin: x=25, y=12; black text drawn after the dots
"""

import argparse
import csv
import json
import random
from pathlib import Path

import numpy as np
from PIL import Image

from . import font5

NAME = "nexusphp_gd5"
DESCRIPTION = "NexusPHP GD font-5 captcha"

WIDTH = 150
HEIGHT = 40
LENGTH = 6
CELL_WIDTH = 9
CELL_HEIGHT = 15
TEXT_X = (WIDTH - CELL_WIDTH * (LENGTH * 2 - 1)) // 2
TEXT_Y = (HEIGHT - CELL_HEIGHT) // 2
DOT_COUNT = (WIDTH * HEIGHT) // 35

ASSETS = Path(__file__).resolve().parent / "assets"
CHARSET = font5.CHARSET


class NexusPhpGD5Generator:
    def __init__(self, seed=None, assets_dir=None):
        self.rng = random.Random(seed)
        self.assets_dir = Path(assets_dir) if assets_dir else ASSETS
        self.backgrounds = [
            np.asarray(
                Image.open(self.assets_dir / f"reg{i}.png").convert("RGB"),
                dtype=np.uint8,
            )
            for i in range(1, 6)
        ]

    def random_text(self):
        return "".join(self.rng.choice(CHARSET) for _ in range(LENGTH))

    def render(self, text=None, background_index=None):
        if text is None:
            text = self.random_text()
        if len(text) != LENGTH:
            raise ValueError(f"text must be exactly {LENGTH} chars, got {len(text)}")
        if any(ch not in CHARSET for ch in text):
            bad = sorted(set(text) - set(CHARSET))
            raise ValueError(f"unsupported chars: {bad}")

        if background_index is None:
            background_index = self.rng.randrange(len(self.backgrounds))
        arr = self.backgrounds[background_index].copy()

        for _ in range(DOT_COUNT):
            x = self.rng.randrange(WIDTH)
            y = self.rng.randrange(HEIGHT)
            color = (
                self.rng.randrange(256),
                self.rng.randrange(256),
                self.rng.randrange(256),
            )
            arr[y, x] = color

        spaced = " ".join(text)
        for i, ch in enumerate(spaced):
            glyph = font5.GLYPHS[ch]
            glyph_y, glyph_x = np.nonzero(glyph)
            x = TEXT_X + i * CELL_WIDTH
            arr[TEXT_Y + glyph_y, x + glyph_x] = 0

        return Image.fromarray(arr, "RGB"), text


def _meta(seed=None, split_sizes=None):
    return {
        "name": NAME,
        "description": DESCRIPTION,
        "width": WIDTH,
        "height": HEIGHT,
        "length": LENGTH,
        "charset": CHARSET,
        "font": "gdFontGiant (GD font 5, 9x15)",
        "backgrounds": [f"reg{i}.png" for i in range(1, 6)],
        "dot_count": DOT_COUNT,
        "text_layout": {
            "origin_x": TEXT_X,
            "origin_y": TEXT_Y,
            "cell_width": CELL_WIDTH,
            "cell_height": CELL_HEIGHT,
            "spaced": True,
        },
        "seed": seed,
        "split_sizes": split_sizes or {},
    }


def build_split(generator, out_dir, split, count, start_index=0):
    image_dir = out_dir / split / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    labels_path = out_dir / split / "labels.csv"
    with labels_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["filename", "text"])
        for i in range(count):
            image, text = generator.render()
            filename = f"{start_index + i:06d}.png"
            image.save(image_dir / filename)
            writer.writerow([filename, text])
    return count


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="dataset output root")
    parser.add_argument("--train", type=int, default=0)
    parser.add_argument("--val", type=int, default=0)
    parser.add_argument("--test", type=int, default=0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--text", default=None, help="render one specific text")
    parser.add_argument("--image", type=Path, default=None, help="single image output")
    parser.add_argument("--background", type=int, default=None, help="1..5")
    args = parser.parse_args(argv)

    generator = NexusPhpGD5Generator(seed=args.seed)

    if args.image is not None:
        background_index = None
        if args.background is not None:
            if not 1 <= args.background <= 5:
                parser.error("--background must be 1..5")
            background_index = args.background - 1
        image, text = generator.render(args.text, background_index=background_index)
        args.image.parent.mkdir(parents=True, exist_ok=True)
        image.save(args.image)
        print(f"{args.image}\t{text}")
        return

    if args.out is None:
        parser.error("--out is required unless --image is used")

    args.out.mkdir(parents=True, exist_ok=True)
    split_sizes = {"train": args.train, "val": args.val, "test": args.test}
    (args.out / "meta.json").write_text(
        json.dumps(
            _meta(seed=args.seed, split_sizes=split_sizes),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    total = 0
    for split, count in (("train", args.train), ("val", args.val), ("test", args.test)):
        if count:
            build_split(generator, args.out, split, count)
            total += count
    print(f"{NAME}: generated {total} images under {args.out}")


if __name__ == "__main__":
    main()
