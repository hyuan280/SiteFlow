"""Independent backend URL projection; parameter redaction is not general DLP."""

from __future__ import annotations

import codecs
import re
from urllib.parse import quote, unquote, unquote_plus, urljoin, urlsplit

from app.schemas.assistant import public_url


REDACTED = "[REDACTED]"
_SENSITIVE = frozenset({
    "token", "accesstoken", "refreshtoken", "idtoken", "password", "passwd",
    "secret", "apikey", "authorization", "cookie", "session", "sessionid",
    "sid", "code", "state", "oauthcode", "signature", "sig", "credential",
    "credentials", "clientsecret", "auth", "jwt", "bearer", "csrf", "csrftoken",
    "xsrf", "xsrftoken", "assertion", "samlresponse", "samlrequest",
})
URL_NOTICE = (
    "Redacted URLs are evidence only, never working URLs. Do not save or execute a URL "
    "containing [REDACTED]; required sensitive parameters must be supplied outside chat. "
    "For browser_trust_required, ask the user to confirm target_origin in UI trusted_origins. "
    "Never approve origins through model arguments or silently change saved site settings. "
    "Preserve public query parameters and fragments exactly. Browser navigation accepts a "
    "relative path (including query/fragment) or an absolute url, exactly one; absolute URLs "
    "must be on the original or explicitly user-trusted origin. Execution and credentials "
    "remain limited to the original origin, not additional trusted origins. "
    "trusted_origins is observation only and does not authorize credential trusted_urls. "
    "trusted_url_pattern is an informational candidate for separate user review, not "
    "approval to send credentials."
)


def origin(url: str) -> str:
    parsed = urlsplit(public_url(url))
    return f"{parsed.scheme}://{parsed.netloc}"


def resolve_url(base: str, value: str) -> str:
    """Resolve a reference without urljoin dropping empty query/fragment markers."""
    if (not isinstance(value, str) or not value
            or any(c.isspace() or ord(c) <= 32 or 127 <= ord(c) <= 159 or c == "\\" for c in value)
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 or c == "\\" for c in unquote(value))):
        raise ValueError("Invalid public URL")
    joined = urljoin(base, value)
    before, marker, fragment = value.partition("#")
    if "?" in before:
        joined = joined.split("#", 1)[0].split("?", 1)[0] + "?" + before.split("?", 1)[1]
    if marker:
        joined = joined.split("#", 1)[0] + "#" + fragment
    return public_url(joined)


def trusted_url_pattern(url: str) -> str:
    return "^" + re.escape(origin(url)) + r"(?:[/?#].*)?$"


def approved_url(url: str, target: dict) -> bool:
    return origin(url) in {origin(target["base_url"]), *target.get("trusted_origins", [])}


def contains_redaction(value) -> bool:
    if isinstance(value, str):
        for _ in range(10):
            if REDACTED.lower() in value.lower():
                return True
            decoded = unquote(value)
            if decoded == value:
                break
            value = decoded
    elif isinstance(value, dict):
        return any(contains_redaction(item) for item in value.values())
    elif isinstance(value, list):
        return any(contains_redaction(item) for item in value)
    return False


def _decoded_offsets(raw: str) -> list[int]:
    # Replace only sensitive spans in nested encoded URLs, not their public bytes.
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    offsets = [0]
    for match in re.finditer(r"%[0-9a-fA-F]{2}|.", raw):
        token = match.group()
        data = (bytes([int(token[1:], 16)]) if token.startswith("%") and len(token) == 3
                else token.replace("+", " ").encode())
        offsets.extend([match.end()] * len(decoder.decode(data)))
    offsets.extend([len(raw)] * len(decoder.decode(b"", final=True)))
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
            if normalized in _SENSITIVE or normalized.endswith((
                "token", "password", "passwd", "secret", "apikey", "signature", "sessionid",
            )):
                replacements.append((offset, offset + len(raw), REDACTED))
                names.append(name)
                continue
            decoded = raw
            boundaries = list(range(len(raw) + 1))
            for layer in range(1, 9 - depth):
                value = unquote_plus(decoded)
                offsets = _decoded_offsets(decoded)
                boundaries = [boundaries[index] for index in offsets]
                unchanged = value == decoded
                decoded = value
                if (re.match(r"(?:https?:)?//", decoded, re.I)
                        and urlsplit(decoded).username is not None):
                    replacements.append((offset, offset + len(raw), REDACTED))
                    names.append(name)
                    break
                if "?" in decoded or "#" in decoded:
                    inner, inner_names = _scrub(decoded, depth + layer)
                    for left, right, replacement in inner:
                        for _ in range(layer):
                            replacement = quote(replacement, safe="")
                        replacements.append((offset + boundaries[left], offset + boundaries[right],
                                             replacement))
                    names.extend(inner_names)
                    break
                if unchanged:
                    if contains_redaction(decoded):
                        names.append(name)
                    break
            else:
                # An opaque value beyond the decoding budget may still hide a URL.
                replacements.append((offset, offset + len(raw), REDACTED))
                names.append(name)
    return replacements, names


def project_url(url: str) -> dict:
    url = public_url(url)
    try:
        replacements, names = _scrub(url)
    except (ValueError, UnicodeError, IndexError):
        raise ValueError("Invalid public URL") from None
    for start, end, value in sorted(replacements, reverse=True):
        url = url[:start] + value + url[end:]
    if len(url) > 4096:
        raise ValueError("Invalid public URL")
    return {"url": url, "url_redacted": bool(replacements) or contains_redaction(url),
            "redacted_parameters": list(dict.fromkeys(names))}


def trust_required(source_url: str, target_url: str) -> dict:
    source, target = project_url(source_url), project_url(target_url)
    return {
        "ok": False, "code": "browser_trust_required",
        "source_url": source["url"], "target_url": target["url"],
        "source_url_redacted": source["url_redacted"],
        "target_url_redacted": target["url_redacted"],
        "redacted_parameters": list(dict.fromkeys(
            source["redacted_parameters"] + target["redacted_parameters"],
        )),
        "source_origin": origin(source_url), "target_origin": origin(target_url),
        "user_action": "confirm_trusted_origin",
        "trusted_url_pattern": trusted_url_pattern(target_url),
        "notice": URL_NOTICE,
    }
