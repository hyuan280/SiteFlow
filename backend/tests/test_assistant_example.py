from __future__ import annotations

import json
from pathlib import Path
import re
import socket

from sqlalchemy import event, select

from app.core.crypto import CryptoService
from app.core.db import Base
from app.schemas.workflow import TemplateWrite
from app.services.workflows import WorkflowService


EXAMPLE = Path(__file__).resolve().parents[2] / "docs/examples/nexusphp-assistant-workflow.json"


def test_nexusphp_example_is_standalone_and_preserves_observed_candidates():
    raw = json.loads(EXAMPLE.read_text())
    payload = TemplateWrite.model_validate(raw)
    assert payload.name == "NexusPHP 登录签到示例"
    assert payload.parent_id is None
    assert payload.enabled is False
    assert not {"id", "template_id", "history_key", "expected_revision"} & raw.keys()
    assert {group.group_key for group in payload.groups} == {"login", "sign"}
    assert len({step.step_key for step in payload.steps}) == len(payload.steps)
    assert all(step.on_error == "stop" for step in payload.steps)
    assert all(not step.feature_type.startswith("post.") for step in payload.steps)
    assert not any("resign" in step.condition for step in payload.steps)
    assert all(step.feature_type != "challenge.turnstile" for step in payload.steps)

    signals = {signal.signal_key: signal for signal in payload.signals}
    assert signals["is_logged_in"].include_texts == ["欢迎回来", "歡迎回來", "控制面板"]
    assert signals["is_logged_in"].exclude_texts == ["请登录", "限制只能使用本IP登录", "Sign in"]
    assert signals["has_image_captcha"].include_selectors == [
        "img[alt='CAPTCHA']", "img[alt='图片验证码']", "img[src*='imagehash']",
    ]
    checked_in = signals["is_checked_in"]
    assert checked_in.include_texts == [
        "今天已经签到", "今天已经簽到", "签到成功", "簽到成功", "签到已得", "簽到已得", "已签到",
    ]
    assert checked_in.exclude_texts == ["每日签到", "签到得魔力", "簽到得魔力"]
    steps = {step.step_key: step for step in payload.steps}
    assert steps["submit_login"].config == {
        "username_selector": ["input[name='username']", "input[placeholder*='用户名']", "#username"],
        "password_selector": ["input[type='password']", "input[name='password']"],
        "submit_selector": [
            "input[value='登录']", "input[value='登錄']", "button:has-text('登录')",
            "input[id='submit-btn']", "input[type='submit']", "button[type='submit']",
        ],
        "username_field": "username",
        "password_field": "password",
        "success_selector": ["a[href*='logout.php']", "a[href*='userdetails.php']", "#info_block"],
    }
    assert steps["submit_sign"].config["button_selector"] == [
        "input[value='立即簽到']", "input[value='立即签到']",
    ]
    assert steps["submit_sign"].config["success_signals"] == checked_in.include_texts
    assert steps["open_login"].condition == "signal:is_logged_in:false"
    assert steps["open_attendance"].condition == (
        "all(signal:is_logged_in:true,signal:is_checked_in:false)"
    )
    assert steps["open_login"].config["url"] == "{base_url}/login.php"
    assert steps["open_attendance"].config["url"] == "{base_url}/attendance.php"
    assert "signal:has_image_captcha:false" in steps["submit_login"].condition
    assert "signal:has_image_captcha:false" in steps["submit_sign"].condition
    assert steps["sign_confirmed"].condition == (
        "all(signal:is_logged_in:true,signal:is_checked_in:true)"
    )
    assert steps["sign_not_confirmed"].condition == "always"
    assert steps["sign_not_confirmed"].config["output_value"] is False

    # Locator/field names are allowed; literal account values and static input are not.
    allowed_sensitive_keys = {
        "username_selector", "password_selector", "username_field", "password_field",
    }
    for step in payload.steps:
        for key in step.config:
            if key not in allowed_sensitive_keys:
                assert not re.search(
                    r"password|username|token|cookie|secret|credential|inline|static|value|fields",
                    key, re.I,
                ) or key == "output_value"
        if step.feature_type in {"flow.open", "action.navigate"}:
            assert step.config["url"].startswith("{base_url}")
            assert step.config["retry_count"] == 0
        if step.feature_type == "flow.result":
            assert step.config["llm_enabled"] is False
    assert not re.search(r"https?://", EXAMPLE.read_text())


def test_nexusphp_example_full_draft_is_read_only_and_offline(client, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("Example validation must not use the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    settings = client.app.state.settings.model_copy(update={
        "config_ai_enabled": False, "vision_base_url": None, "vision_api_key": None,
        "vision_model": None, "cfbox_url": None, "cfbox_api_key": None,
    })
    payload = TemplateWrite.model_validate_json(EXAMPLE.read_text())
    original = payload.model_dump()
    with client.app.state.session_factory() as session:
        before = {
            table.name: session.execute(select(table)).all()
            for table in Base.metadata.sorted_tables
        }

        def no_dml(conn, cursor, statement, parameters, context, executemany):
            assert not (context.isinsert or context.isupdate or context.isdelete)
            assert not re.match(r"\s*(INSERT|UPDATE|DELETE|REPLACE)\b", statement, re.I)

        engine = client.app.state.engine
        event.listen(engine, "before_cursor_execute", no_dml)
        try:
            result = WorkflowService(
                session, settings, CryptoService(settings.master_key_bytes()),
            ).validate_draft("template", payload)
        finally:
            event.remove(engine, "before_cursor_execute", no_dml)
        assert result["valid"] is True
        assert result["owner_id"] is None
        assert result["resolved"]["owner_id"] is None
        assert [step["step_key"] for step in result["resolved"]["steps"]] == [
            step.step_key for step in payload.steps
        ]
        assert all(
            step["condition_unknown_action"] == "needs_human"
            for step in result["resolved"]["steps"]
        )
        assert payload.model_dump() == original
        assert before == {
            table.name: session.execute(select(table)).all()
            for table in Base.metadata.sorted_tables
        }
