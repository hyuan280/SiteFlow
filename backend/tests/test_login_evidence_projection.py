import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services.login_evidence import project_success_evidence


def evidence():
    return {
        "selector_mode": "visible", "frame_scope": "main_and_visible_same_origin",
        "captured_after_timeout": True, "reason": "matches_hidden", "elapsed_ms": 30001,
        "configured_count": 1, "selectors": [{
            "selector_index": 0, "match_count": 2, "visible_count": 0, "checked_count": 2,
            "truncated": False, "read_errors": 0, "frames": [{
                "frame_index": 0, "scope": "main", "match_count": 2, "visible_count": 0,
                "checked_count": 2, "truncated": False,
            }],
        }], "truncated": False,
    }


def test_projection_is_independent_value_free_and_does_not_mutate_input():
    raw = evidence()
    raw.update(url="https://private/?token=secret", text="private", selector="private")
    raw["selectors"][0].update(selector="private", value="private")
    raw["selectors"][0]["frames"][0].update(url="private", message="private")
    original = deepcopy(raw)
    projected = project_success_evidence(raw)
    assert projected == evidence()
    assert raw == original
    assert "private" not in json.dumps(projected)
    projected["selectors"][0]["frames"].clear()
    assert raw == original


def test_wait_and_sampling_durations_are_independent():
    raw = evidence()
    raw.update(waited_ms=10000, elapsed_ms=42)
    assert project_success_evidence(raw) == raw
    raw.pop("elapsed_ms")
    assert project_success_evidence(raw) is None


@pytest.mark.parametrize("truncated", [False, True])
def test_worker_truncation_alias_is_canonicalized(truncated):
    raw = evidence()
    raw["evidence_truncated"] = truncated
    raw.pop("truncated")
    assert project_success_evidence(raw) == {**evidence(), "truncated": truncated}
    assert "evidence_truncated" not in project_success_evidence(raw)
    raw["truncated"] = False
    assert project_success_evidence(raw)["truncated"] is False


@pytest.mark.parametrize("field,invalid", [
    ("waited_ms", [None, True, -1, 10_000_001, 1.0, "1", [], {}]),
    ("evidence_truncated", [None, 0, 1, "false", [], {}]),
    ("visible_after_timeout", [None, 0, 1, "false", [], {}]),
    ("snapshot_consistency", [None, True, "private-provider-message", [], {}]),
])
def test_optional_worker_metadata_is_strictly_validated(field, invalid):
    for value in invalid:
        raw = evidence()
        raw[field] = value
        assert project_success_evidence(raw) is None, (field, value)


@pytest.mark.parametrize("waited_ms", [0, 10_000_000])
@pytest.mark.parametrize("visible", [False, True])
def test_reviewed_worker_metadata_is_preserved(waited_ms, visible):
    raw = evidence()
    raw.update(waited_ms=waited_ms, visible_after_timeout=visible,
               snapshot_consistency="sampled_after_timeout")
    assert project_success_evidence(raw) == raw


@pytest.mark.parametrize("level,field,invalid", [
    ("root", "selector_mode", [None, True, "attached", [], {}]),
    ("root", "frame_scope", [None, "all", [], {}]),
    ("root", "captured_after_timeout", [None, False, 1, "true"]),
    ("root", "reason", [None, [], {}, "private-provider-message"]),
    ("root", "elapsed_ms", [None, True, -1, 10_000_001, 1.0, "1"]),
    ("root", "configured_count", [None, True, -1, 10001, 1.0, "1"]),
    ("root", "selectors", [None, {}, "private"]),
    ("root", "truncated", [None, 0, 1, "false"]),
    ("selector", "selector_index", [None, True, -1, 10001, 1.0, "1"]),
    ("selector", "match_count", [None, True, -1, 10001, 1.0, "1"]),
    ("selector", "visible_count", [None, True, -1, 3, 10001, 1.0, "1"]),
    ("selector", "checked_count", [None, True, -1, 101, 1.0, "1"]),
    ("selector", "read_errors", [None, True, -1, 10001, 1.0, "1"]),
    ("selector", "truncated", [None, 0, 1, "false"]),
    ("selector", "frames", [None, {}, "private"]),
    ("frame", "frame_index", [None, True, -1, 10001, 1.0, "1"]),
    ("frame", "scope", [None, [], {}, "private-provider-message"]),
    ("frame", "match_count", [True, -1, 10001, 1.0, "1"]),
    ("frame", "visible_count", [True, -1, 3, 10001, 1.0, "1"]),
    ("frame", "checked_count", [None, True, -1, 101, 1.0, "1"]),
    ("frame", "truncated", [None, 0, 1, "false"]),
    ("frame", "error", [None, [], {}, "private-provider-message"]),
])
def test_invalid_typed_fields_omit_entire_evidence(level, field, invalid):
    for value in invalid:
        raw = evidence()
        item = raw if level == "root" else raw["selectors"][0]
        if level == "frame":
            item = item["frames"][0]
        item[field] = value
        assert project_success_evidence(raw) is None, (level, field, value)


@pytest.mark.parametrize("level", ["root", "selector", "frame"])
def test_all_contract_fields_are_required(level):
    raw = evidence()
    item = raw if level == "root" else raw["selectors"][0]
    if level == "frame":
        item = item["frames"][0]
    for field in list(item):
        value = item.pop(field)
        assert project_success_evidence(raw) is None, (level, field)
        item[field] = value


@pytest.mark.parametrize("value", [None, [], True, 1, "private", {}, {"selectors": [None]}])
def test_invalid_root(value):
    assert project_success_evidence(value) is None


@pytest.mark.parametrize("reason", [
    "selector_missing", "matches_hidden", "read_incomplete",
    "visible_after_timeout", "frame_scope_excluded",
])
@pytest.mark.parametrize("scope", [
    "main", "same_origin", "cross_origin", "hidden_frame", "unreadable_frame",
])
@pytest.mark.parametrize("error", [
    "invalid_selector", "read_timeout", "frame_detached", "frame_not_eligible",
])
def test_all_reviewed_enums_and_nullable_frame_counts(reason, scope, error):
    raw = evidence()
    raw["reason"] = reason
    raw["selectors"][0]["frames"][0].update(
        scope=scope, error=error, match_count=None, visible_count=None)
    assert project_success_evidence(raw) == raw


def test_bounds_and_byte_budget_are_enforced_with_explicit_truncation():
    raw = evidence()
    raw.update(elapsed_ms=10_000_000, configured_count=10000)
    selector = raw["selectors"][0]
    selector.update(selector_index=10000, match_count=10000, visible_count=10000,
                    checked_count=100, read_errors=10000)
    selector["frames"][0].update(frame_index=10000, scope="unreadable_frame", match_count=10000,
                                 visible_count=10000, checked_count=100, error="frame_not_eligible")
    assert project_success_evidence(raw) == raw
    selector["frames"] *= 20
    raw["selectors"] *= 20
    original = deepcopy(raw)
    projected = project_success_evidence(raw)
    assert projected["truncated"] is True
    assert len(projected["selectors"]) == 10
    assert all(item["truncated"] and len(item["frames"]) <= 10 for item in projected["selectors"])
    assert len(json.dumps(projected).encode()) <= 16000
    assert raw == original
    assert project_success_evidence(projected) == projected


def test_byte_budget_marks_truncation_even_when_array_limits_are_not_exceeded():
    raw = evidence()
    frame = raw["selectors"][0]["frames"][0]
    frame.update(frame_index=10000, scope="unreadable_frame", match_count=10000,
                 visible_count=10000, checked_count=100, error="frame_not_eligible")
    raw["selectors"][0]["frames"] *= 10
    raw["selectors"] *= 10
    assert len(json.dumps(raw).encode()) > 16000
    projected = project_success_evidence(raw)
    assert projected["truncated"] is True
    assert any(item["truncated"] for item in projected["selectors"])
    assert sum(len(item["frames"]) for item in projected["selectors"]) < 100
    assert len(json.dumps(projected).encode()) <= 16000


@pytest.mark.parametrize("level", ["selector", "frame"])
def test_malformed_list_entries_are_omitted(level):
    raw = evidence()
    if level == "selector":
        raw["selectors"] = [None]
    else:
        raw["selectors"][0]["frames"] = [None]
    assert project_success_evidence(raw) is None


@pytest.mark.parametrize("visibility,reason", [
    ([], "frame_scope_excluded"),
    ([False, False], "matches_hidden"),
    ([False, True], "visible_after_timeout"),
])
def test_real_worker_evidence_survives_projection_with_excluded_frames(visibility, reason):
    from tests.test_login_evidence_diagnostics import (
        PRIVATE, child_frame, executor, matches, page_with,
    )

    main = SimpleNamespace(locator=Mock(return_value=matches(*visibility)))
    hidden = child_frame(main, visible=False)
    cross = child_frame(main, url=f"https://other.test/?token={PRIVATE}")
    raw = asyncio.run(executor._login_success_evidence(
        page_with(main, hidden, cross), [f'[data-token="{PRIVATE}"]'], waited_ms=10000,
    ))
    projected = project_success_evidence(raw)
    assert projected is not None
    assert projected == {
        **{key: value for key, value in raw.items() if key != "evidence_truncated"},
        "truncated": raw["evidence_truncated"],
    }
    assert projected["reason"] == reason
    assert projected["waited_ms"] == 10000
    assert projected["elapsed_ms"] < 3000
    assert projected["visible_after_timeout"] is any(visibility)
    summary = projected["selectors"][0]
    assert summary["match_count"] == len(visibility)
    assert summary["visible_count"] == sum(visibility)
    assert summary["checked_count"] == len(visibility)
    assert [frame["scope"] for frame in summary["frames"]] == [
        "main", "hidden_frame", "cross_origin",
    ]
    assert all(frame["match_count"] is None and frame["visible_count"] is None
               and frame["error"] == "frame_not_eligible" for frame in summary["frames"][1:])
    hidden.locator.assert_not_called()
    cross.locator.assert_not_called()
    exposed = json.dumps(projected)
    assert PRIVATE not in exposed and "https://" not in exposed and "data-token" not in exposed


def test_real_worker_bounded_evidence_survives_projection():
    from tests.test_login_evidence_diagnostics import (
        PRIVATE, child_frame, executor, matches, page_with,
    )

    main = SimpleNamespace(locator=Mock(return_value=matches(False, count=1000)))
    children = [child_frame(main) for _ in range(12)]
    for child in children:
        child.locator.return_value = matches(False, count=1000)
    raw = asyncio.run(executor._login_success_evidence(
        page_with(main, *children), [f".private-{PRIVATE}-{index}" for index in range(15)],
        waited_ms=10000,
    ))
    original = deepcopy(raw)
    projected = project_success_evidence(raw)
    assert projected is not None
    assert projected["reason"] == "read_incomplete"
    assert projected["truncated"] is True
    assert projected["configured_count"] == 15
    assert projected["waited_ms"] == 10000
    assert projected["elapsed_ms"] == raw["elapsed_ms"]
    assert len(projected["selectors"]) == 10
    for summary in projected["selectors"]:
        assert summary["match_count"] == 10000
        assert summary["checked_count"] == 100
        assert summary["visible_count"] == 0
        assert summary["truncated"] is True
        assert len(summary["frames"]) <= 10
    assert len(json.dumps(projected).encode()) <= 16000
    assert PRIVATE not in json.dumps(projected)
    assert raw == original
    assert project_success_evidence(projected) == projected
