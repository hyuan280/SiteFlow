"""GD built-in font 5 (gdFontGiant, 9x15) glyphs.

Source: libgd gdfontg.c
  -Misc-Fixed-Bold-R-Normal-Sans-15-140-75-75-C-90-ISO8859-2

Only the NexusPHP captcha charset plus space is embedded.
"""

import base64
import zlib

import numpy as np

CHARSET = "ABCDEFGHPRMN123456789"
SEQUENCE = CHARSET + " "
WIDTH = 9
HEIGHT = 15

_DATA = (
    "eNq9VQkSwyAIhP9/utMqERERgZTJwUQjC8sBYAkivZv2fdL9e2pKE2vPlfTj+o+r9fbF3qNvhgAQZqOfgcOjs4IMIkRwuD31KPFo"
    "EMm2yxzqMT7xaCAWwoCQbHJja0IvkHRueHg/l2dJwbq6xJukXMJAnQXIR2PYIt4HBcZVTQqHQb5LRYMxsmVVItSwmTLNltsKZb+H"
    "UGwmmgTpfcf7F0829XRcV2aoubZh0zLPCpCDQwYiUbV2T8An86wgLHxGB71yloln08zjMERGJTKzYLTplbI6+Fo0LkzMDYqWqmD8"
    "Tz5zdwMk"
)

_RAW = zlib.decompress(base64.b64decode(_DATA))
_EXPECTED = len(SEQUENCE) * WIDTH * HEIGHT
if len(_RAW) != _EXPECTED:
    raise RuntimeError(f"font5 data size mismatch: {len(_RAW)} != {_EXPECTED}")

GLYPHS = {
    ch: np.frombuffer(
        _RAW,
        dtype=np.uint8,
        count=WIDTH * HEIGHT,
        offset=i * WIDTH * HEIGHT,
    )
    .reshape(HEIGHT, WIDTH)
    .copy()
    for i, ch in enumerate(SEQUENCE)
}
