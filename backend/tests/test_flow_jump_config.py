from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.features.catalog import FlowJumpConfig


@pytest.mark.parametrize("mode", [{}, {"match_mode": "page"}])
@pytest.mark.parametrize("evidence", [{"texts": ["验证失败"]}, {"selectors": [".error"]}])
def test_page_mode_accepts_either_evidence(mode, evidence):
    config = FlowJumpConfig(target_step_key="open", **mode, **evidence)
    assert config.match_mode == "page"


@pytest.mark.parametrize("mode", [{}, {"match_mode": "page"}])
def test_page_mode_requires_evidence(mode):
    with pytest.raises(ValidationError) as caught:
        FlowJumpConfig(target_step_key="open", **mode)
    assert caught.value.errors()[0]["type"] == "flow_jump_evidence_required"


@pytest.mark.parametrize("action", ["jump", "restart"])
def test_always_mode_accepts_no_page_evidence(action):
    config = FlowJumpConfig(
        match_mode="always", action=action, target_step_key=" open " if action == "jump" else "",
    )
    assert config.texts == config.selectors == []
    assert config.target_step_key == ("open" if action == "jump" else "")


@pytest.mark.parametrize("target,code", [
    ("", "flow_jump_target_required"),
    ("   ", "flow_jump_target_required"),
    ("bad:key", "flow_jump_target_invalid"),
])
def test_always_mode_preserves_target_validation(target, code):
    with pytest.raises(ValidationError) as caught:
        FlowJumpConfig(match_mode="always", target_step_key=target)
    assert caught.value.errors()[0]["type"] == code


@pytest.mark.parametrize("max_jumps", [0, 100])
def test_always_mode_accepts_jump_limit_boundaries(max_jumps):
    config = FlowJumpConfig(match_mode="always", target_step_key="open", max_jumps=max_jumps)
    assert config.max_jumps == max_jumps


@pytest.mark.parametrize("overrides", [
    {"max_jumps": -1}, {"max_jumps": 101}, {"match_mode": "invalid"}, {"match_mode": None},
])
def test_invalid_mode_or_jump_limit_is_rejected(overrides):
    with pytest.raises(ValidationError):
        FlowJumpConfig.model_validate({
            "match_mode": "always", "target_step_key": "open", **overrides,
        })


def test_jump_schema_exposes_mode_and_unchanged_jump_limit():
    properties = FlowJumpConfig.model_json_schema()["properties"]
    mode = properties["match_mode"]
    assert mode["enum"] == ["page", "always"]
    assert mode["default"] == "page"
    assert mode["title"] == "跳转匹配方式"
    assert properties["max_jumps"]["default"] == 10
    assert properties["max_jumps"]["maximum"] == 100
