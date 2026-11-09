"""Bounded URL validation and parameter redaction, not general-purpose DLP."""
from __future__ import annotations

import codecs
import re
from typing import TypedDict
from urllib.parse import quote, unquote, unquote_plus, urlsplit

MAX_URL_LENGTH = 4096
MAX_DECODE_ROUNDS = 8
REDACTED = "[REDACTED]"
SENSITIVE = frozenset({
    "token", "accesstoken", "refreshtoken", "idtoken", "password", "passwd",
    "secret", "apikey", "authorization", "cookie", "session", "sessionid",
    "sid", "code", "state", "oauthcode", "signature", "sig", "credential",
    "credentials", "clientsecret", "auth", "jwt", "bearer", "csrf", "csrftoken",
    "xsrf", "xsrftoken", "assertion", "samlresponse", "samlrequest",
})


class ProjectedURL(TypedDict):
    url: str
    url_redacted: bool
    redacted_parameters: list[str]


def parse_url(url: str) -> tuple[str, str, str]:
    """Validate before urlsplit can discard controls; retain the full raw suffix."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH:
        raise ValueError("Invalid URL")
    if (any(ord(c) <= 32 or 127 <= ord(c) <= 159 or c.isspace() or c == "\\" for c in url)
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 or c == "\\" for c in unquote(url))):
        raise ValueError("Invalid URL")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or not re.match(r"https?://", url, re.I)):
            raise ValueError()
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        if "%" in host or host.endswith(".") or not re.fullmatch(r"[a-z0-9.:-]+", host):
            raise ValueError()
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        if not 1 <= port <= 65535:
            raise ValueError()
        authority = f"[{host}]" if ":" in host else host
        if port != (443 if parsed.scheme == "https" else 80):
            authority += f":{port}"
        origin = f"{parsed.scheme}://{authority}"
        suffix = url[len(parsed.scheme) + 3 + len(parsed.netloc):]
        if not suffix.startswith("/"):
            suffix = "/" + suffix
        return host, origin, origin + suffix
    except (ValueError, UnicodeError):
        raise ValueError("Invalid URL") from None


def _decoded_offsets(raw: str) -> list[int]:
    """Map form-decoded character boundaries back to their original raw bytes."""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    offsets = [0]
    for match in re.finditer(r"%[0-9a-fA-F]{2}|.", raw):
        token = match.group()
        data = (bytes([int(token[1:], 16)]) if token.startswith("%") and len(token) == 3
                else token.replace("+", " ").encode())
        chunk = decoder.decode(data)
        offsets.extend([match.end()] * len(chunk))
    chunk = decoder.decode(b"", final=True)
    offsets.extend([len(raw)] * len(chunk))
    return offsets


def _scrub(url: str, depth: int = 0) -> tuple[list[tuple[int, int, str]], list[str]]:
    replacements, names = [], []
    before, marker, fragment = url.partition("#")
    sections = []
    if "?" in before:
        start = before.index("?") + 1
        sections.append((start, before[start:]))
    if marker:
        start = len(before) + 1
        if "?" in fragment and "=" not in fragment.split("?", 1)[0]:
            start += fragment.index("?") + 1
        sections.append((start, url[start:]))
    for start, section in sections:
        for match in re.finditer(r"[^&;]+", section):
            key, equals, raw = match.group().partition("=")
            if not equals:
                continue
            name = unquote_plus(key)
            normalized = re.sub(r"[^a-z0-9]", "", name.lower())
            offset = start + match.start() + len(key) + 1
            if normalized in SENSITIVE or normalized.endswith((
                "token", "password", "passwd", "secret", "apikey", "signature", "sessionid",
            )):
                replacements.append((offset, offset + len(raw), REDACTED))
                names.append(name)
                continue
            decoded = raw
            layers = []
            for _ in range(max(1, MAX_DECODE_ROUNDS - depth)):
                layers.append(decoded)
                previous = decoded
                decoded = unquote_plus(decoded)
                if "?" in decoded or "#" in decoded or decoded == previous:
                    break
            if re.match(r"(?:https?:)?//", decoded, re.I) and urlsplit(decoded).username is not None:
                replacements.append((offset, offset + len(raw), REDACTED))
                names.append(name)
                continue
            if "?" not in decoded and "#" not in decoded:
                continue
            # Nested return/redirect URLs may themselves contain encoded URLs.
            if depth >= MAX_DECODE_ROUNDS:
                replacements.append((offset, offset + len(raw), REDACTED))
                names.append(name)
                continue
            inner, inner_names = _scrub(decoded, depth + len(layers))
            if inner:
                # Lift only recognized value spans through every encoding layer.
                for layer in reversed(layers):
                    boundaries = _decoded_offsets(layer)
                    inner = [(boundaries[left], boundaries[right], quote(replacement, safe=""))
                             for left, right, replacement in inner]
                for left, right, replacement in inner:
                    replacements.append((offset + left, offset + right, replacement))
                names.extend(inner_names)
    return replacements, names


def project_url(url: str) -> ProjectedURL:
    """Preserve the observed URL verbatim except recognized sensitive values.

    Userinfo is rejected rather than exposed. Callers must additionally guard
    known credential fingerprints, including credentials in paths or DOM text.
    """
    parse_url(url)
    try:
        replacements, names = _scrub(url)
    except (ValueError, UnicodeError):
        raise ValueError("Invalid URL") from None
    for start, end, value in sorted(replacements, reverse=True):
        url = url[:start] + value + url[end:]
    if len(url) > MAX_URL_LENGTH:
        raise ValueError("Invalid URL")
    return {"url": url, "url_redacted": bool(replacements),
            "redacted_parameters": list(dict.fromkeys(names))}
