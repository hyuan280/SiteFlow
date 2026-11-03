import copy
import itertools

import pytest

from worker.cfbox_response import resolve_cfbox_prediction


STATES = ("absent", "verified", "unknown", "unchecked", "processing", "error")
CENTER = {"x": 42.77, "y": 92.99}


def test_real_multiple_widgets_response():
    body = {
        "state": "unknown",
        "confidence": None,
        "ambiguous": True,
        "reason": "multiple_widgets",
        "has_cf_challenge": True,
        "challenges": [
            {"state": "unchecked", "confidence": 0.9379,
             "checkbox_center_pct": CENTER, "box": [0, 0, 100, 100]},
            {"state": "unknown", "confidence": None, "checkbox_center_pct": None},
        ],
    }
    original = copy.deepcopy(body)
    assert resolve_cfbox_prediction(body) == {
        "state": "unchecked",
        "effective_confidence": 0.9379,
        "center": CENTER,
        "summary_state": "unknown",
        "challenge_states": ["unchecked", "unknown"],
        "challenge_confidences": [0.9379, None],
        "low_confidence_eligible": False,
        "multiple_unresolved": True,
        "invalid": False,
        "has_challenge_evidence": True,
        "allow_clear_override": False,
    }
    assert body == original


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("challenges", [{}, {"challenges": None}, {"challenges": []}])
def test_legacy_summary(state, challenges):
    result = resolve_cfbox_prediction({
        "state": state, "confidence": 0.3, "checkbox_center_pct": CENTER, **challenges,
    })
    assert result["state"] == result["summary_state"] == state
    assert result["effective_confidence"] == 0.3
    assert result["center"] == (CENTER if state == "unchecked" else None)
    assert result["challenge_states"] == []
    assert result["challenge_confidences"] == []
    assert result["low_confidence_eligible"] is False
    assert result["multiple_unresolved"] is False
    assert result["has_challenge_evidence"] is False
    assert result["allow_clear_override"] is (state not in {"unchecked", "processing"})


@pytest.mark.parametrize("summary", STATES)
@pytest.mark.parametrize("confidence", [None, 0.99])
@pytest.mark.parametrize("child", STATES)
def test_nonempty_children_always_checked(summary, confidence, child):
    result = resolve_cfbox_prediction({
        "state": summary, "confidence": confidence, "checkbox_center_pct": {"x": 1, "y": 1},
        "challenges": [{"state": child, "confidence": 0.4, "checkbox_center_pct": CENTER}],
    })
    assert result["state"] == child
    assert result["effective_confidence"] == 0.4
    assert result["center"] == (CENTER if child == "unchecked" else None)
    assert result["has_challenge_evidence"] is (child not in {"absent", "verified"})


@pytest.mark.parametrize("states", list(itertools.permutations(
    ["unknown", "error", "processing", "unchecked"], 2,
)))
def test_unresolved_priority_and_mixed_override_block(states):
    priority = ["unchecked", "processing", "error", "unknown"]
    result = resolve_cfbox_prediction({
        "state": "verified", "confidence": 1,
        "challenges": [{"state": state, "confidence": 0.3} for state in states],
    })
    assert result["state"] == min(states, key=priority.index)
    assert result["multiple_unresolved"] is True
    assert result["allow_clear_override"] is False
    assert result["center"] is None


@pytest.mark.parametrize("other", STATES)
def test_competing_target_blocks_click(other):
    result = resolve_cfbox_prediction({
        "state": "unknown",
        "challenges": [
            {"state": "unchecked", "checkbox_center_pct": CENTER},
            {"state": other, "checkbox_center_pct": {"x": 10, "y": 20}},
        ],
    })
    assert result["state"] == "unchecked"
    assert result["center"] is None
    assert result["multiple_unresolved"] is (other not in {"absent", "verified"})
    assert result["allow_clear_override"] is False


@pytest.mark.parametrize("other_center", [None, CENTER, {"x": 10, "y": 20}])
def test_multiple_unchecked_targets_are_not_deduplicated(other_center):
    result = resolve_cfbox_prediction({
        "state": "unchecked", "checkbox_center_pct": CENTER,
        "challenges": [
            {"state": "unchecked", "checkbox_center_pct": CENTER},
            {"state": "unchecked", "checkbox_center_pct": other_center},
        ],
    })
    assert result["center"] is None
    assert result["multiple_unresolved"] is True
    assert result["allow_clear_override"] is False


@pytest.mark.parametrize("others", [
    ["error"], ["processing"], ["unknown"], ["unknown", "error"],
])
@pytest.mark.parametrize("confidence", [None, 0, 0.3, 0.95, 1])
def test_unique_unchecked_with_centerless_children_is_clickable(others, confidence):
    body = {
        "state": "unknown", "confidence": None, "ambiguous": True,
        "reason": "multiple_widgets", "has_cf_challenge": True,
        "challenges": [
            {"state": "unchecked", "confidence": 0.9259, "checkbox_center_pct": CENTER},
            *[{"state": state, "confidence": confidence, "checkbox_center_pct": None}
              for state in others],
        ],
    }
    original = copy.deepcopy(body)
    assert resolve_cfbox_prediction(body) == {
        "state": "unchecked",
        "effective_confidence": 0.9259,
        "center": CENTER,
        "summary_state": "unknown",
        "challenge_states": ["unchecked", *others],
        "challenge_confidences": [0.9259, *[confidence for _ in others]],
        "low_confidence_eligible": False,
        "multiple_unresolved": True,
        "invalid": False,
        "has_challenge_evidence": True,
        "allow_clear_override": False,
    }
    assert body == original


@pytest.mark.parametrize("other", ["absent", "verified", "unknown", "processing", "error"])
def test_same_center_in_other_state_is_not_a_second_target(other):
    result = resolve_cfbox_prediction({
        "state": "unknown",
        "challenges": [
            {"state": "unchecked", "checkbox_center_pct": CENTER},
            {"state": other, "checkbox_center_pct": CENTER},
        ],
    })
    assert result["state"] == "unchecked"
    assert result["center"] == CENTER
    assert result["multiple_unresolved"] is (other not in {"absent", "verified"})
    assert result["allow_clear_override"] is False


@pytest.mark.parametrize("states,expected", [
    (["verified", "verified"], "verified"),
    (["verified", "absent"], "verified"),
    (["absent", "absent"], "absent"),
    (["verified", "unknown"], "unknown"),
    (["absent", "unknown"], "unknown"),
])
def test_only_resolved_children_can_verify(states, expected):
    result = resolve_cfbox_prediction({
        "state": "verified", "challenges": [{"state": state} for state in states],
    })
    assert result["state"] == expected


@pytest.mark.parametrize("field", ["has_cf_challenge", "ambiguous"])
@pytest.mark.parametrize("challenges", [{}, {"challenges": None}, {"challenges": []}])
def test_flagged_unknown_is_not_initial_plain_unknown(field, challenges):
    result = resolve_cfbox_prediction({"state": "unknown", field: True, **challenges})
    assert result["state"] == "unknown"
    assert result["has_challenge_evidence"] is True
    assert result["multiple_unresolved"] is True
    assert result["allow_clear_override"] is False


@pytest.mark.parametrize("value", [False, None, 1, "true"])
def test_summary_flags_require_literal_true(value):
    result = resolve_cfbox_prediction({
        "state": "unknown", "has_cf_challenge": value, "ambiguous": value,
    })
    assert result["has_challenge_evidence"] is False
    assert result["multiple_unresolved"] is False
    assert result["allow_clear_override"] is True


@pytest.mark.parametrize("value,expected", [
    (None, None), (False, None), (True, None), ("0.3", None), ({}, None), ([], None),
    (-0.1, None), (1.1, None), (float("nan"), None), (float("inf"), None),
    (float("-inf"), None), (10 ** 1000, None), (0, 0.0), (1, 1.0), (0.9379, 0.9379),
])
@pytest.mark.parametrize("child", [False, True])
def test_confidence_validation(value, expected, child):
    prediction = {"state": "unknown", "confidence": value}
    body = {"state": "verified", "confidence": 1, "challenges": [prediction]} if child else prediction
    result = resolve_cfbox_prediction(body)
    assert result["effective_confidence"] == expected
    assert result["challenge_confidences"] == ([expected] if child else [])
    assert result["low_confidence_eligible"] is (expected is not None and expected < 0.95)


def test_missing_child_confidence_does_not_inherit_or_promote_summary():
    result = resolve_cfbox_prediction({
        "state": "verified", "confidence": 1, "challenges": [{"state": "unknown"}],
    })
    assert result["state"] == "unknown"
    assert result["effective_confidence"] is None


def test_low_confidence_error_does_not_erase_unknown_child():
    result = resolve_cfbox_prediction({
        "state": "absent", "confidence": 1,
        "challenges": [{"state": "error", "confidence": 0.3}, {"state": "unknown"}],
    })
    assert result["state"] == "error"
    assert result["effective_confidence"] == 0.3
    assert result["has_challenge_evidence"] is True
    assert result["multiple_unresolved"] is True
    assert result["allow_clear_override"] is False


@pytest.mark.parametrize("states,confidences", [
    (["error", "error"], [0.7024, 0.92]),
    (["error", "unknown"], [0.8, 0.9]),
])
@pytest.mark.parametrize("summary", STATES)
@pytest.mark.parametrize("resolved", [[], [
    {"state": "verified", "confidence": 1}, {"state": "absent", "confidence": None},
]])
def test_all_low_unresolved_children_are_eligible(states, confidences, summary, resolved):
    body = {
        "state": summary, "confidence": 1, "checkbox_center_pct": CENTER,
        "ambiguous": True, "has_cf_challenge": True,
        "challenges": [
            *resolved,
            *[{"state": state, "confidence": confidence, "checkbox_center_pct": None}
              for state, confidence in zip(states, confidences)],
        ],
    }
    original = copy.deepcopy(body)
    result = resolve_cfbox_prediction(body)
    assert result["state"] == "error"
    assert result["effective_confidence"] == min(confidences)
    assert result["challenge_confidences"] == [
        *[child["confidence"] for child in resolved], *confidences,
    ]
    assert result["low_confidence_eligible"] is True
    assert result["multiple_unresolved"] is True
    assert result["allow_clear_override"] is False
    assert body == original


@pytest.mark.parametrize("other", ["error", "unknown"])
@pytest.mark.parametrize("confidence", [
    0.95, 1, None, "0.8", False, True, -0.1, 1.1, float("nan"), float("inf"), {}, [],
])
@pytest.mark.parametrize("reverse", [False, True])
def test_aggregated_min_does_not_make_every_child_low(other, confidence, reverse):
    children = [{"state": "error", "confidence": 0.7024},
                {"state": other, "confidence": confidence}]
    if reverse:
        children.reverse()
    result = resolve_cfbox_prediction({"state": "error", "confidence": 0.7024,
                                       "challenges": children})
    assert result["low_confidence_eligible"] is False
    assert result["allow_clear_override"] is False
    if other == "unknown" or type(confidence) in (int, float) and confidence in (0.95, 1):
        assert result["effective_confidence"] == 0.7024


@pytest.mark.parametrize("active", ["unchecked", "processing"])
@pytest.mark.parametrize("center", [None, CENTER])
def test_active_child_blocks_low_confidence_eligibility(active, center):
    result = resolve_cfbox_prediction({
        "state": "error", "confidence": 0.7024,
        "challenges": [{"state": "error", "confidence": 0.7024},
                       {"state": active, "confidence": 0.1, "checkbox_center_pct": center}],
    })
    assert result["low_confidence_eligible"] is False
    assert result["allow_clear_override"] is False


@pytest.mark.parametrize("state", STATES)
def test_center_on_any_child_blocks_low_confidence_eligibility(state):
    result = resolve_cfbox_prediction({
        "state": "error", "confidence": 0.7024,
        "challenges": [{"state": "error", "confidence": 0.7024},
                       {"state": state, "confidence": 0.1, "checkbox_center_pct": CENTER}],
    })
    assert result["low_confidence_eligible"] is False


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("center", [None, CENTER])
@pytest.mark.parametrize("challenges", [{}, {"challenges": None}, {"challenges": []}])
def test_summary_low_confidence_eligibility(state, center, challenges):
    result = resolve_cfbox_prediction({
        "state": state, "confidence": 0.7024, "checkbox_center_pct": center,
        "ambiguous": True, **challenges,
    })
    assert result["low_confidence_eligible"] is (
        state in {"error", "unknown"} and center is None
    )


@pytest.mark.parametrize("states", [["verified"], ["absent"], ["absent", "verified"]])
def test_resolved_children_do_not_fall_back_to_low_summary(states):
    result = resolve_cfbox_prediction({
        "state": "error", "confidence": 0.7024,
        "challenges": [{"state": state, "confidence": 0.1} for state in states],
    })
    assert result["low_confidence_eligible"] is False


@pytest.mark.parametrize("child", [False, True])
def test_explicit_ambiguity_blocks_error_clear_override(child):
    prediction = {"state": "error", "confidence": 0.3}
    body = {"state": "unknown", "challenges": [prediction]} if child else prediction
    result = resolve_cfbox_prediction({**body, "ambiguous": True})
    assert result["allow_clear_override"] is False
    assert result["low_confidence_eligible"] is True


@pytest.mark.parametrize("confidence", [None, 0, 0.3])
def test_unique_unchecked_target_is_not_confidence_gated(confidence):
    result = resolve_cfbox_prediction({
        "state": "unknown",
        "challenges": [
            {"state": "unchecked", "confidence": confidence, "checkbox_center_pct": CENTER},
            {"state": "unknown"}, {"state": "verified"}, {"state": "absent"},
        ],
    })
    assert result["state"] == "unchecked"
    assert result["center"] == CENTER
    assert result["effective_confidence"] == confidence
    assert result["allow_clear_override"] is False


@pytest.mark.parametrize("confidences,expected", [
    ([0.3, 0.8], 0.3), ([0.3, None], None), ([None, None], None),
])
def test_same_priority_confidence_is_conservative(confidences, expected):
    result = resolve_cfbox_prediction({
        "state": "unknown",
        "challenges": [{"state": "error", "confidence": c} for c in confidences],
    })
    assert result["effective_confidence"] == expected
    assert result["multiple_unresolved"] is True
    assert result["allow_clear_override"] is False


@pytest.mark.parametrize("center", [CENTER, {"x": 0.42, "y": 0.93}, {"x": 1, "y": 100}])
@pytest.mark.parametrize("child", [False, True])
def test_center_preserves_raw_coordinates(center, child):
    prediction = {"state": "unchecked", "checkbox_center_pct": center}
    body = {"state": "unknown", "challenges": [prediction]} if child else prediction
    result = resolve_cfbox_prediction(body)
    assert result["center"] == center
    assert result["center"] is not center


@pytest.mark.parametrize("center", [None, ...])
@pytest.mark.parametrize("child", [False, True])
def test_missing_center_never_produces_target(center, child):
    body = {"state": "unchecked"}
    if center is not ...:
        body["checkbox_center_pct"] = center
    if child:
        body = {"state": "unknown", "checkbox_center_pct": CENTER,
                "challenges": [body, {"state": "error"}]}
    assert resolve_cfbox_prediction(body)["center"] is None


@pytest.mark.parametrize("coordinate", [
    None, False, True, "42", 0, -1, 100.01, float("nan"), float("inf"),
    float("-inf"), 10 ** 1000, [], {},
])
@pytest.mark.parametrize("key", ["x", "y"])
@pytest.mark.parametrize("child", [False, True])
def test_invalid_center_coordinate_fails_closed(coordinate, key, child):
    prediction = {"state": "unchecked", "checkbox_center_pct": {**CENTER, key: coordinate}}
    body = {"state": "verified", "challenges": [prediction]} if child else prediction
    with pytest.raises(ValueError):
        resolve_cfbox_prediction(body)


@pytest.mark.parametrize("body", [
    None, [], "unknown", 1, {}, {"state": None}, {"state": []}, {"state": True},
    {"state": "invalid"}, {"state": "VERIFIED"},
    {"state": "unknown", "checkbox_center_pct": []},
    {"state": "unknown", "checkbox_center_pct": {}},
    *[{"state": "verified", "challenges": value} for value in ({}, "", 0, False, ())],
    *[{"state": "verified", "challenges": [value]}
      for value in (None, [], "verified", {}, {"state": []}, {"state": True},
                    {"state": "invalid"}, {"state": "Verified"})],
])
def test_invalid_response_fails_closed(body):
    with pytest.raises(ValueError):
        resolve_cfbox_prediction(body)


def test_all_children_validated_even_after_clickable_target():
    with pytest.raises(ValueError):
        resolve_cfbox_prediction({
            "state": "unchecked", "confidence": 1,
            "challenges": [
                {"state": "unchecked", "checkbox_center_pct": CENTER}, {"state": "invalid"},
            ],
        })


def test_challenge_count_bound():
    body = {"state": "verified", "challenges": [{"state": "verified"}] * 100}
    assert resolve_cfbox_prediction(body)["state"] == "verified"
    body["challenges"].append({"state": "verified"})
    with pytest.raises(ValueError):
        resolve_cfbox_prediction(body)
