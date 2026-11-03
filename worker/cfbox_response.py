"""Pure, fail-closed CFbox response resolution; no page or click side effects."""

import math


_STATES = {"absent", "verified", "unknown", "unchecked", "processing", "error"}
_UNRESOLVED = ("unchecked", "processing", "error", "unknown")


def _confidence(value):
    if type(value) in (int, float) and 0 <= value <= 1 and math.isfinite(value):
        return float(value)
    return None


def _center(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("CFbox checkbox_center_pct must be an object or null")
    for key in ("x", "y"):
        coordinate = value.get(key)
        if not (
            type(coordinate) in (int, float)
            and 0 < coordinate <= 100
            and math.isfinite(coordinate)
        ):
            raise ValueError("CFbox center coordinates must be finite numbers in (0, 100]")
    # Fractions remain unchanged: the click layer owns legacy normalization.
    return {"x": value["x"], "y": value["y"]}


def resolve_cfbox_prediction(body) -> dict:
    """Resolve summary/children without treating uncertainty as verification.

    Missing/null/empty challenges use the legacy summary. Nonempty challenges
    are authoritative, including when the summary has a confidence or is absent.
    Invalid structure/state/center raises ValueError; invalid confidence is None.
    ``center`` is a raw coordinate dict, only exposed for a unique safe unchecked
    target. ``multiple_unresolved`` blocks completion, not that target's click.
    ``allow_clear_override`` only permits a caller's separate page-clear checks;
    it is not itself evidence of success. Every returned result has invalid=False.
    """
    if not isinstance(body, dict):
        raise ValueError("CFbox response must be an object")
    summary_state = body.get("state")
    if not isinstance(summary_state, str) or summary_state not in _STATES:
        raise ValueError("CFbox summary state is invalid")
    challenges = body.get("challenges")
    if challenges is None:
        challenges = []
    if not isinstance(challenges, list) or len(challenges) > 100:
        raise ValueError("CFbox challenges must be an array of at most 100 objects")

    summary_confidence = _confidence(body.get("confidence"))
    summary_center = _center(body.get("checkbox_center_pct"))
    children = []
    for child in challenges:
        if not isinstance(child, dict):
            raise ValueError("CFbox challenge must be an object")
        state = child.get("state")
        if not isinstance(state, str) or state not in _STATES:
            raise ValueError("CFbox challenge state is invalid")
        children.append({
            "state": state,
            "confidence": _confidence(child.get("confidence")),
            "center": _center(child.get("checkbox_center_pct")),
        })

    unresolved = [child for child in children if child["state"] in _UNRESOLVED]
    summary_flag = body.get("has_cf_challenge") is True or body.get("ambiguous") is True
    multiple_unresolved = len(unresolved) > 1
    center = None
    if not children:
        state = summary_state
        confidence = summary_confidence
        if state == "unchecked":
            center = summary_center
        if state == "unknown" and summary_flag:
            multiple_unresolved = True
    elif unresolved:
        state = next(state for state in _UNRESOLVED
                     if any(child["state"] == state for child in unresolved))
        candidates = [child for child in unresolved if child["state"] == state]
        confidences = [child["confidence"] for child in candidates]
        confidence = min(confidences) if all(c is not None for c in confidences) else None
        # Centerless children do not compete; distinct centers in any state do.
        if (
            state == "unchecked" and len(candidates) == 1
            and candidates[0]["center"] is not None
            and all(
                child["center"] is None or child["center"] == candidates[0]["center"]
                for child in children
            )
        ):
            center = candidates[0]["center"]
        if state == "unknown" and summary_flag:
            multiple_unresolved = True
    else:
        state = "verified" if any(c["state"] == "verified" for c in children) else "absent"
        confidences = [child["confidence"] for child in children]
        confidence = min(confidences) if all(c is not None for c in confidences) else None

    # Multi-widget ambiguity is not an active checkbox when every unresolved
    # detection is centerless and individually low-confidence error/unknown.
    low_candidates = unresolved if children else [{
        "state": summary_state, "confidence": summary_confidence, "center": summary_center,
    }]
    low_confidence_eligible = bool(low_candidates) and all(
        child["state"] in {"error", "unknown"}
        and child["confidence"] is not None and child["confidence"] < 0.95
        and child["center"] is None
        for child in low_candidates
    ) and all(child["center"] is None for child in children)

    return {
        "state": state,
        "effective_confidence": confidence,
        "center": center,
        "summary_state": summary_state,
        "challenge_states": [child["state"] for child in children],
        "challenge_confidences": [child["confidence"] for child in children],
        "low_confidence_eligible": low_confidence_eligible,
        "multiple_unresolved": multiple_unresolved,
        "invalid": False,
        "has_challenge_evidence": summary_flag or bool(unresolved),
        "allow_clear_override": state not in {"unchecked", "processing"}
        and not multiple_unresolved
        and not (body.get("ambiguous") is True and state in _UNRESOLVED),
    }
