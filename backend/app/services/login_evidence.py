"""Strict, value-free projection of post-timeout login selector diagnostics."""

from __future__ import annotations

import json


def project_success_evidence(value) -> dict | None:
    """Drop unknown fields; omit malformed evidence rather than forwarding raw data."""
    def integer(item, maximum=10000):
        return type(item) is int and 0 <= item <= maximum

    if not isinstance(value, dict):
        return None
    elapsed = value.get("elapsed_ms")
    truncated = value.get("truncated", value.get("evidence_truncated"))
    reason = value.get("reason")
    selectors = value.get("selectors")
    if (value.get("selector_mode") != "visible"
            or value.get("frame_scope") != "main_and_visible_same_origin"
            or value.get("captured_after_timeout") is not True
            or not isinstance(reason, str) or reason not in {
                "selector_missing", "matches_hidden", "read_incomplete",
                "visible_after_timeout", "frame_scope_excluded",
            }
            or not integer(elapsed, 10_000_000)
            or not integer(value.get("configured_count"))
            or not isinstance(selectors, list)
            or type(truncated) is not bool
            or any(key in value and type(value[key]) is not bool for key in (
                "truncated", "evidence_truncated", "visible_after_timeout",
            ))
            or ("waited_ms" in value and not integer(value["waited_ms"], 10_000_000))
            or ("snapshot_consistency" in value
                and value["snapshot_consistency"] != "sampled_after_timeout")):
        return None
    projected = {
        "selector_mode": "visible", "frame_scope": "main_and_visible_same_origin",
        "captured_after_timeout": True, "reason": reason, "elapsed_ms": elapsed,
        "configured_count": value["configured_count"], "selectors": [],
        "truncated": truncated or len(selectors) > 10,
    }
    # Login wait time and post-timeout sampling duration are distinct measurements.
    if "waited_ms" in value:
        projected["waited_ms"] = value["waited_ms"]
    if "snapshot_consistency" in value:
        projected["snapshot_consistency"] = "sampled_after_timeout"
    if "visible_after_timeout" in value:
        projected["visible_after_timeout"] = value["visible_after_timeout"]
    for selector in selectors[:10]:
        if not isinstance(selector, dict):
            return None
        frames = selector.get("frames")
        if (any(not integer(selector.get(key)) for key in (
                "selector_index", "match_count", "visible_count", "read_errors"))
                or selector["visible_count"] > selector["match_count"]
                or not integer(selector.get("checked_count"), 100)
                or type(selector.get("truncated")) is not bool
                or not isinstance(frames, list)):
            return None
        item = {key: selector[key] for key in (
            "selector_index", "match_count", "visible_count", "checked_count", "read_errors",
        )}
        item.update(truncated=selector["truncated"] or len(frames) > 10, frames=[])
        if len(frames) > 10:
            projected["truncated"] = True
        for frame in frames[:10]:
            if not isinstance(frame, dict):
                return None
            scope = frame.get("scope")
            if (not integer(frame.get("frame_index"))
                    or not isinstance(scope, str) or scope not in {
                        "main", "same_origin", "cross_origin", "hidden_frame", "unreadable_frame",
                    }
                    or any(key not in frame or (
                        frame[key] is not None and not integer(frame[key])
                    ) for key in ("match_count", "visible_count"))
                    or (frame["match_count"] is not None and frame["visible_count"] is not None
                        and frame["visible_count"] > frame["match_count"])
                    or not integer(frame.get("checked_count"), 100)
                    or type(frame.get("truncated")) is not bool):
                return None
            entry = {key: frame[key] for key in (
                "frame_index", "scope", "match_count", "visible_count", "checked_count", "truncated",
            )}
            if "error" in frame:
                error = frame["error"]
                if not isinstance(error, str) or error not in {
                    "invalid_selector", "read_timeout", "frame_detached", "frame_not_eligible",
                }:
                    return None
                entry["error"] = error
            item["frames"].append(entry)
        projected["selectors"].append(item)

    # Even 100 valid frame entries can exceed the byte budget. Preserve selector
    # summaries while removing trailing frame details and flagging the omission.
    while len(json.dumps(projected).encode("utf-8")) > 16000:
        for selector in reversed(projected["selectors"]):
            if selector["frames"]:
                selector["frames"].pop()
                selector["truncated"] = projected["truncated"] = True
                break
        else:
            return None
    return projected
