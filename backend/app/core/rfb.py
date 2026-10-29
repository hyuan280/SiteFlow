"""Minimal RFB compatibility helpers for KasmVNC 1.3.3.

noVNC 1.7 may batch client messages and send extension message types that
KasmVNC 1.3.3 does not understand. These helpers keep only standard messages,
rewrite pointer events to KasmVNC's extended format, and convert KasmVNC's
binary clipboard message to standard ServerCutText.
"""

from __future__ import annotations

import logging
import struct

logger = logging.getLogger("siteflow.vnc")

_RFB_MSG_SIZE: dict[int, int | None] = {
    0: 20,    # SetPixelFormat
    2: None,  # SetEncodings
    3: 10,    # FramebufferUpdateRequest
    4: 8,     # KeyEvent
    5: 6,     # PointerEvent
    6: None,  # ClientCutText
}

_RFB_EXTENSION_SIZE: dict[int, int] = {
    150: 10,
    248: 10,
    252: 4,
    255: 4,
}

_ALLOWED_ENCODINGS: set[int] = {
    0, 1, 2, 5, 7, 16,
    -239, -224,
    *range(-32, -22),
    *range(-256, -246),
}


def _rfb_msg_length(data: bytes, offset: int) -> int | None:
    if offset >= len(data):
        return None
    msg_type = data[offset]
    fixed = _RFB_MSG_SIZE.get(msg_type)
    if fixed is not None:
        return fixed
    remaining = len(data) - offset
    if msg_type == 2 and remaining >= 4:
        num_enc = struct.unpack_from(">H", data, offset + 2)[0]
        return 4 + num_enc * 4
    if msg_type == 6 and remaining >= 8:
        length = struct.unpack_from(">I", data, offset + 4)[0]
        return 8 + length
    ext_size = _RFB_EXTENSION_SIZE.get(msg_type)
    if ext_size is not None:
        return ext_size
    return None


def _rewrite_set_encodings(data: bytes, offset: int, msg_len: int) -> bytes:
    num_enc = struct.unpack_from(">H", data, offset + 2)[0]
    kept: list[int] = []
    for index in range(num_enc):
        enc = struct.unpack_from(">i", data, offset + 4 + index * 4)[0]
        if enc in _ALLOWED_ENCODINGS:
            kept.append(enc)
    result = struct.pack(">BxH", 2, len(kept))
    for enc in kept:
        result += struct.pack(">i", enc)
    return result


def _rewrite_pointer_event(data: bytes, offset: int) -> bytes:
    mask = data[offset + 1]
    x = struct.unpack_from(">H", data, offset + 2)[0]
    y = struct.unpack_from(">H", data, offset + 4)[0]
    return struct.pack(">BHHHhh", 5, mask, x, y, 0, 0)


def filter_client_messages(data: bytes) -> bytes:
    result = bytearray()
    offset = 0
    while offset < len(data):
        msg_type = data[offset]
        msg_len = _rfb_msg_length(data, offset)
        if msg_len is None or offset + msg_len > len(data):
            logger.debug("dropping unknown/incomplete RFB type=%s at %s", msg_type, offset)
            break
        if msg_type in _RFB_MSG_SIZE:
            if msg_type == 2:
                result.extend(_rewrite_set_encodings(data, offset, msg_len))
            elif msg_type == 5:
                result.extend(_rewrite_pointer_event(data, offset))
            else:
                result.extend(data[offset:offset + msg_len])
        offset += msg_len
    if len(result) != len(data):
        logger.debug("RFB filter input=%d output=%d", len(data), len(result))
    return bytes(result)


def parse_kasmvnc_clipboard(data: bytes) -> str | None:
    if len(data) < 7:
        return None
    offset = 6
    while offset < len(data):
        mime_len = data[offset]
        offset += 1
        if offset + mime_len > len(data):
            break
        mime_type = data[offset:offset + mime_len]
        offset += mime_len
        if offset + 4 > len(data):
            break
        data_len = struct.unpack_from(">I", data, offset)[0]
        offset += 4
        if mime_type == b"text/plain":
            end = min(offset + data_len, len(data))
            return data[offset:end].decode("utf-8", errors="replace")
        offset += data_len
    return None


def build_server_cut_text(text: str) -> bytes:
    text_bytes = text.encode("latin-1", errors="replace")
    return struct.pack(">BxxxI", 3, len(text_bytes)) + text_bytes
