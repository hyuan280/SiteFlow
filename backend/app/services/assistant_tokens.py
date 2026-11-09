"""Local preflight estimates, never provider usage or a model capacity claim.

ASCII runs use ceil(chars / 3), other Unicode uses its UTF-8 byte length
(conservative for CJK, emoji and combining characters). JSON structure is counted
without ASCII escaping; escaped Unicode inside JSON chat text is decoded before
counting. Images use the low-detail estimate of 85 plus protocol markers, not
their base64 transport size. Actual usage is known only after a provider response.
"""

import json
import re


_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")


def estimate_tokens(value) -> int:
    if isinstance(value, str):
        value = _UNICODE_ESCAPE.sub(lambda match: chr(int(match[1], 16)), value)
        ascii_chars = sum(ord(char) < 128 for char in value)
        unicode_bytes = len(value.encode("utf-8", errors="surrogatepass")) - ascii_chars
        return (ascii_chars + 2) // 3 + unicode_bytes
    if isinstance(value, list):
        return 2 + sum(estimate_tokens(item) + 1 for item in value)
    if isinstance(value, dict):
        if value.get("type") == "image_url":
            return 85 + 12
        return 2 + sum(estimate_tokens(key) + estimate_tokens(item) + 2
                       for key, item in value.items())
    return estimate_tokens(json.dumps(value, ensure_ascii=False, allow_nan=False))


def estimate_context_tokens(messages, tools=()) -> int:
    """Include message framing, system text, tool definitions and image markers."""
    return 3 + estimate_tokens(messages) + 4 * len(messages) + estimate_tokens(tools)
