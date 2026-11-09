"""Independent, bounded projection of the worker's generic DOM evidence."""

from __future__ import annotations

import json
import re
from uuid import UUID

from app.services.assistant_urls import URL_NOTICE, approved_url, project_url, trust_required


SENSITIVE_KEY = re.compile(
    r"token|password|passwd|secret|auth|nonce|cookie|storage|session|credential|csrf|xsrf|apikey|signature",
    re.I,
)
URL_ATTRIBUTES = {"href", "src", "action", "formaction", "poster", "cite", "background", "longdesc", "xlink:href"}
URL_KEY = re.compile(r"(?:^|[-_:])(?:url|href|src|action)$", re.I)
OMIT_ATTRIBUTES = {"style", "srcdoc", "srcset", "imagesrcset", "ping", "selected", "checked"}
NO_TEXT = {"input", "textarea", "select", "option", "script", "style", "template", "noscript"}
POLICY_CODES = frozenset({
    "browser_url_denied", "browser_dns_denied", "browser_redirect_denied",
    "browser_trust_required", "browser_credential_endpoint_not_approved",
})
FRAME_REASONS = {
    "browser_trust_required", "browser_frame_unavailable", "browser_url_denied",
    "browser_dns_denied", "browser_redirect_denied", "browser_frame_unreadable",
}
LIMIT_BOUNDS = {
    "page_bytes": 65536, "page_nodes": 100, "scan_nodes": 5000, "scan_depth": 64,
    "snapshot_bytes": 2 * 1024 * 1024, "retained_snapshots": 4, "text_chars": 300,
    "attributes_per_node": 24, "attribute_chars": 512, "selector_chars": 2048,
    "url_attribute_chars": 4096, "selector_bytes": 4096, "frames": 64,
    "frame_bytes": 12 * 1024, "node_bytes": 8 * 1024,
    "selector_matches": 10000, "selector_samples": 10,
}


def _bounded_int(value, maximum):
    return type(value) is int and 0 <= value <= maximum


def _identifier(value, maximum=256):
    return isinstance(value, str) and 1 <= len(value) <= maximum and not any(
        ord(char) < 32 or ord(char) == 127 for char in value
    )


def _url(raw):
    if raw.get("url") == "[redacted]" and raw.get("url_redacted") is True:
        return {"url": "[redacted]", "url_redacted": True, "redacted_parameters": []}
    return project_url(raw["url"])


def _page(raw, target):
    projected = _url(raw)
    if projected["url"] != "[redacted]" and not approved_url(projected["url"], target):
        raise ValueError
    result = {"ok": True, **projected}
    if projected["url_redacted"]:
        result["notice"] = URL_NOTICE
    return result


def _nodes(sources, frame_id, maximum):
    if not isinstance(sources, list) or len(sources) > maximum:
        raise ValueError
    result, seen = [], set()
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError
        tag, node_id, parent_id = source.get("tag"), source.get("node_id"), source.get("parent_id")
        if (not isinstance(tag, str) or not 1 <= len(tag) <= 128
                or not isinstance(node_id, str) or not re.fullmatch(r"[nm][0-9]{1,5}", node_id)
                or node_id in seen or source.get("frame_id") != frame_id
                or (parent_id is not None and (not isinstance(parent_id, str)
                    or not re.fullmatch(r"n[0-9]{1,5}", parent_id)))
                or type(source.get("visible")) is not bool
                or not isinstance(source.get("attributes"), dict)):
            raise ValueError
        seen.add(node_id)
        # Parents can be on an earlier page or outside the inspected subtree.
        item = {"node_id": node_id, "parent_id": parent_id, "frame_id": frame_id,
                "tag": tag, "visible": source["visible"], "attributes": {}}
        if tag.lower() not in NO_TEXT and isinstance(source.get("text"), str):
            item["text"] = " ".join(source["text"].split())[:300]
        selector = source.get("selector")
        if (isinstance(selector, str) and 1 <= len(selector) <= 2048
                and len(json.dumps(selector, ensure_ascii=True).encode()) <= 4096
                and type(source.get("selector_matches")) is int and source["selector_matches"] == 1
                and source.get("selector_source") in {"id", "name", "dom_path"}):
            item.update(selector=selector, selector_matches=1, selector_source=source["selector_source"])
        if type(source.get("has_shadow_root")) is bool:
            item["has_shadow_root"] = source["has_shadow_root"]
        # Meta content inherits sensitivity from its semantic name/property.
        sensitive_meta = tag.lower() == "meta" and any(
            isinstance(key, str) and key.lower() in {"name", "property"}
            and isinstance(value, str)
            and SENSITIVE_KEY.search(re.sub(r"[^a-z0-9]", "", value.lower()))
            for key, value in source["attributes"].items()
        )
        for key, value in source["attributes"].items():
            if not isinstance(key, str) or not isinstance(value, str) or not 1 <= len(key) <= 128:
                continue
            lower = key.lower()
            if (lower.startswith("on") or "value" in lower or lower in OMIT_ATTRIBUTES
                    or (sensitive_meta and lower == "content")
                    or SENSITIVE_KEY.search(re.sub(r"[^a-z0-9]", "", lower))):
                continue
            if len(item["attributes"]) >= 24:
                break
            if (lower in URL_ATTRIBUTES or (tag.lower() == "object" and lower == "data")
                    or URL_KEY.search(key) or re.match(r"https?://", value, re.I)):
                try:
                    value = value if value == "[redacted]" else project_url(value)["url"]
                except ValueError:
                    continue
                if len(value) > 4096:
                    continue
            else:
                value = value[:512]
            item["attributes"][key] = value
        result.append(item)
    return result


def _frames(raw, target):
    sources = raw.get("frames")
    if not isinstance(sources, list) or type(raw.get("frames_truncated")) is not bool:
        raise ValueError
    frames, seen = [], set()
    for source in sources[:64]:
        if not isinstance(source, dict) or not _identifier(source.get("frame_id")):
            raise ValueError
        frame_id = source["frame_id"]
        if frame_id in seen:
            raise ValueError
        seen.add(frame_id)
        item = {"frame_id": frame_id, "readable": False}
        if _identifier(source.get("parent_frame_id")):
            item["parent_frame_id"] = source["parent_frame_id"]
        try:
            projected = _url(source)
            item.update(projected)
            approved = projected["url"] != "[redacted]" and approved_url(projected["url"], target)
            item["readable"] = approved and source.get("readable") is True
            if not approved:
                item["reason"] = "browser_trust_required"
        except (ValueError, KeyError, TypeError):
            item.update(url="[redacted]", url_redacted=True, redacted_parameters=[],
                        reason="browser_url_denied")
        if not item["readable"] and "reason" not in item:
            reason = source.get("reason")
            item["reason"] = reason if isinstance(reason, str) and reason in FRAME_REASONS else "browser_frame_unavailable"
        frames.append(item)
    return frames, raw["frames_truncated"] or len(sources) > 64


def safe_snapshot(raw: dict, target: dict) -> dict:
    """Retain generic hierarchy and proof, never infer selectors or rescan a DOM."""
    try:
        result = _page(raw, target)
        snapshot_id, frame_id = raw["snapshot_id"], raw["frame_id"]
        if not isinstance(snapshot_id, str) or str(UUID(snapshot_id)) != snapshot_id or not _identifier(frame_id):
            raise ValueError
        offset, total, next_offset = raw["offset"], raw["total"], raw["next_offset"]
        if (not _bounded_int(offset, 5000) or not _bounded_int(total, 5000)
                or type(raw.get("truncated")) is not bool):
            raise ValueError
        elements = _nodes(raw["elements"], frame_id, 100)
        end = offset + len(elements)
        if end > total and elements:
            raise ValueError
        expected_next = end if end < total else None
        if ((next_offset is not None and not _bounded_int(next_offset, 5000))
                or next_offset != expected_next or (next_offset is not None and next_offset <= offset)):
            raise ValueError
        frames, frames_truncated = _frames(raw, target)
        if not any(frame["frame_id"] == frame_id and frame["readable"] for frame in frames):
            raise ValueError
        source_limits = raw.get("limits")
        if not isinstance(source_limits, dict):
            raise ValueError
        limits = {key: value for key, value in source_limits.items()
                  if key in LIMIT_BOUNDS and _bounded_int(value, LIMIT_BOUNDS[key])}
        if source_limits.get("inspect_root_included") is True:
            limits["inspect_root_included"] = True
        if source_limits.get("inspect_scope") == "captured_subtree":
            limits["inspect_scope"] = "captured_subtree"
        result.update(snapshot_id=snapshot_id, frame_id=frame_id, elements=elements,
                      offset=offset, next_offset=next_offset, total=total, truncated=raw["truncated"],
                      frames=frames, frames_truncated=frames_truncated, limits=limits)
        sources = raw.get("warnings")
        if isinstance(sources, list):
            warnings = []
            for source in sources[:4]:
                if (not isinstance(source, dict) or source.get("source") != "blocked_request"
                        or not isinstance(source.get("code"), str) or source["code"] not in POLICY_CODES):
                    continue
                warning = {"code": source["code"], "source": "blocked_request"}
                if source["code"] == "browser_trust_required":
                    try:
                        # Historical evidence does not grant navigation or frame authority.
                        details = trust_required(source["source_url"], source["target_url"])
                        warning.update({key: value for key, value in details.items()
                                        if key not in {"ok", "code"}})
                    except (ValueError, KeyError, TypeError):
                        pass
                if source.get("details_truncated") is True:
                    warning["details_truncated"] = True
                    if source.get("reason") == "browser_warning_details_limit":
                        warning["reason"] = "browser_warning_details_limit"
                warnings.append(warning)
            if warnings:
                result["warnings"] = warnings
        if len(json.dumps(result, ensure_ascii=True).encode()) > 128 * 1024:
            raise ValueError
        return result
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return {"ok": False, "code": "browser_unavailable"}


def safe_selector_result(raw: dict, target: dict) -> dict:
    try:
        result = _page(raw, target)
        frame_id, selector = raw["frame_id"], raw["selector"]
        count, visible = raw["match_count"], raw["visible_count"]
        if (not _identifier(frame_id) or not isinstance(selector, str) or not 1 <= len(selector) <= 2048
                or not _bounded_int(count, 10000) or not _bounded_int(visible, count)
                or type(raw.get("truncated")) is not bool):
            raise ValueError
        matches = _nodes(raw["matches"], frame_id, 10)
        if len(matches) > count:
            raise ValueError
        result.update(frame_id=frame_id, selector=selector, match_count=count, visible_count=visible,
                      matches=matches, truncated=raw["truncated"] or count > len(matches))
        if len(json.dumps(result, ensure_ascii=True).encode()) > 128 * 1024:
            raise ValueError
        return result
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return {"ok": False, "code": "browser_unavailable"}
