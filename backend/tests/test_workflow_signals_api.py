from __future__ import annotations

from fastapi.testclient import TestClient


def _login_signal() -> dict:
    return {
        "signal_key": "sig_login",
        "label": "登录状态",
        "description": "判断是否已经登录",
        "include_texts": ["退出登录"],
        "include_selectors": ["a[href*='logout']"],
        "exclude_texts": ["请登录"],
        "exclude_selectors": [],
        "match_mode": "any",
        "enabled": True,
    }


def _steps(condition: str | None = None) -> list[dict]:
    return [
        {"step_key": "open", "feature_type": "flow.open", "config": {}},
        {
            "step_key": "login",
            "feature_type": "auth.form_login",
            "condition": condition,
            "config": {
                "username_selector": ["#username"],
                "password_selector": ["#password"],
                "submit_selector": ["button"],
            },
        },
        {"step_key": "sign", "feature_type": "sign.button", "config": {}},
    ]


def test_condition_requires_defined_signal(client: TestClient) -> None:
    response = client.post(
        "/api/v1/templates",
        json={
            "name": "no-signal",
            "steps": _steps("signal:sig_login:true"),
        },
    )
    assert response.status_code == 422
    assert "不存在的信号" in response.json()["detail"]


def test_signal_and_condition_resolution(client: TestClient) -> None:
    created = client.post(
        "/api/v1/templates",
        json={
            "name": "signal-template",
            "signals": [_login_signal()],
            "steps": _steps("signal:sig_login:true"),
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["signal_count"] == 1
    assert body["resolved_signals"][0]["signal_key"] == "sig_login"
    assert body["resolved_signals"][0]["source"] == "template:signal-template"
    assert body["resolved"]["steps"][1]["condition"] == "signal:sig_login:true"
    assert body["resolved"]["steps"][1]["condition_unknown_action"] == "needs_human"

    updated = client.put(
        f"/api/v1/templates/{body['id']}",
        json={
            "name": "signal-template",
            "signals": [
                {**_login_signal(), "include_texts": ["个人中心", "退出"]},
            ],
            "steps": _steps("signal:sig_login:true"),
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["resolved_signals"][0]["include_texts"] == ["个人中心", "退出"]


def test_used_signal_cannot_be_deleted(client: TestClient) -> None:
    created = client.post(
        "/api/v1/templates",
        json={
            "name": "used-signal",
            "signals": [_login_signal()],
            "steps": _steps("signal:sig_login:true"),
        },
    )
    assert created.status_code == 201
    template_id = created.json()["id"]

    response = client.put(
        f"/api/v1/templates/{template_id}",
        json={
            "name": "used-signal",
            "signals": [],
            "steps": _steps("signal:sig_login:true"),
        },
    )
    assert response.status_code == 409
    assert "不能删除" in response.json()["detail"]

    response = client.put(
        f"/api/v1/templates/{template_id}",
        json={
            "name": "used-signal",
            "signals": [_login_signal()],
            "steps": _steps("always"),
        },
    )
    assert response.status_code == 200
    response = client.put(
        f"/api/v1/templates/{template_id}",
        json={"name": "used-signal", "signals": [], "steps": _steps("always")},
    )
    assert response.status_code == 200


def test_signal_inheritance_and_site_override(client: TestClient) -> None:
    parent = client.post(
        "/api/v1/templates",
        json={
            "name": "parent-signal",
            "signals": [_login_signal()],
            "steps": _steps("signal:sig_login:true"),
        },
    )
    assert parent.status_code == 201, parent.text
    parent_id = parent.json()["id"]

    child = client.post(
        "/api/v1/templates",
        json={"name": "child-signal", "parent_id": parent_id, "steps": []},
    )
    assert child.status_code == 201, child.text
    assert child.json()["resolved_signals"][0]["source"] == "template:parent-signal"

    site = client.post(
        "/api/v1/sites",
        json={
            "name": "signal-site",
            "base_url": "https://signal.example.com",
            "template_id": parent_id,
            "steps": [],
        },
    )
    assert site.status_code == 201, site.text
    assert site.json()["resolved_signals"][0]["source"] == "template:parent-signal"

    override = client.put(
        f"/api/v1/sites/{site.json()['id']}",
        json={
            "name": "signal-site",
            "base_url": "https://signal.example.com",
            "template_id": parent_id,
            "signals": [
                {**_login_signal(), "label": "站点登录状态", "include_texts": ["站点专属"]},
            ],
            "steps": [],
        },
    )
    assert override.status_code == 200, override.text
    assert override.json()["resolved_signals"][0]["source"] == "site"
    assert override.json()["resolved_signals"][0]["label"] == "站点登录状态"


def test_signal_requires_include_rule_and_valid_condition(client: TestClient) -> None:
    response = client.post(
        "/api/v1/templates",
        json={
            "name": "bad-signal",
            "signals": [{"signal_key": "sig_empty", "label": "空信号"}],
            "steps": [],
        },
    )
    assert response.status_code == 422
    assert "至少需要一个包含" in response.json()["detail"]

    response = client.post(
        "/api/v1/templates",
        json={
            "name": "bad-condition",
            "steps": _steps("if_logged_in"),
        },
    )
    assert response.status_code == 422
    assert "执行条件无效" in response.json()["detail"]
