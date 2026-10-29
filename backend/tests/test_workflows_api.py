from __future__ import annotations

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event

from app.main import create_app
from app.services.workflows import WorkflowService
from tests.helpers import login_client, make_settings


def _login_step(**config):
    base = {
        "username_selector": ["#username"],
        "password_selector": ["#password"],
        "submit_selector": ["button[type=submit]"],
    }
    base.update(config)
    return {
        "step_key": "login",
        "feature_type": "auth.form_login",
        "config": base,
    }


@pytest.mark.parametrize("kind", ["sites", "templates"])
def test_ambiguous_ungrouped_positions_rejected_on_write(client, kind):
    parent = _create_template(client, {
        "name": "collision-parent",
        "steps": [{"step_key": "base", "feature_type": "action.wait", "position": 1000}],
    })
    payload = {
        "name": "collision-child",
        **({"base_url": "https://example.com", "template_id": parent["id"]}
           if kind == "sites" else {"parent_id": parent["id"]}),
        "steps": [{"step_key": "local", "feature_type": "action.wait", "position": 1000}],
    }
    response = client.post(f"/api/v1/{kind}", json=payload)
    assert response.status_code == 422, response.text
    assert "Ambiguous ungrouped" in response.json()["detail"]
    payload["steps"][0]["position"] = 3000
    created = client.post(f"/api/v1/{kind}", json=payload)
    assert created.status_code == 201, created.text
    payload["steps"].append({"step_key": "other", "feature_type": "action.wait", "position": 3000})
    response = client.put(f"/api/v1/{kind}/{created.json()['id']}", json=payload)
    assert response.status_code == 422, response.text
    assert len(client.get(f"/api/v1/{kind}/{created.json()['id']}").json()["steps"]) == 1
    payload["steps"] = [{"step_key": "base", "feature_type": "action.wait", "position": 1000}]
    response = client.put(f"/api/v1/{kind}/{created.json()['id']}", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["resolved"]["steps"][0]["position"] == 1001
    payload["groups"] = [
        {"group_key": "g1", "name": "One", "position": 1000000},
        {"group_key": "g2", "name": "Two", "position": 2000000},
    ]
    payload["steps"] += [
        {"step_key": "one", "feature_type": "action.wait", "position": 1001, "group_key": "g1"},
        {"step_key": "two", "feature_type": "action.wait", "position": 1001, "group_key": "g2"},
    ]
    response = client.put(f"/api/v1/{kind}/{created.json()['id']}", json=payload)
    assert response.status_code == 200, response.text


def _base_template() -> dict:
    return {
        "name": "nexusphp-base",
        "description": "基础模板",
        "parent_id": None,
        "enabled": True,
        "steps": [
            {"step_key": "open", "feature_type": "flow.open", "config": {}},
            _login_step(success_selector=["#ok"]),
            {"step_key": "sign", "feature_type": "sign.button", "config": {}},
        ],
    }


def _create_template(client: TestClient, payload: dict) -> dict:
    response = client.post("/api/v1/templates", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _create_site(client: TestClient, payload: dict) -> dict:
    response = client.post("/api/v1/sites", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_turnstile_requires_cfbox(tmp_path) -> None:
    settings = make_settings(tmp_path, cfbox_url=None, cfbox_api_key=None)
    client = TestClient(create_app(settings))
    login_client(client)
    response = client.post(
        "/api/v1/templates",
        json={
            "name": "turnstile-without-cfbox",
            "steps": [
                {"step_key": "turn", "feature_type": "challenge.turnstile"},
            ],
        },
    )
    assert response.status_code == 422
    assert "CFbox" in response.json()["detail"]


def test_features_catalog_requires_auth(anon_client: TestClient, client: TestClient) -> None:
    assert anon_client.get("/api/v1/features").status_code == 401

    response = client.get("/api/v1/features")
    assert response.status_code == 200
    features = {item["key"]: item for item in response.json()}
    assert "auth.form_login" in features
    assert features["auth.form_login"]["cardinality"] == "single"
    assert "cfbox" in features["challenge.turnstile"]["requires"]
    assert features["auth.form_login"]["config_schema"]["additionalProperties"] is False
    selector_schema = features["auth.form_login"]["config_schema"]["properties"]["username_selector"]
    assert selector_schema["type"] == "array"
    assert selector_schema["items"]["type"] == "string"
    assert features["challenge.2fa"]["config_schema"]["properties"]["totp_provider_id"]["default"] == "builtin.twofauth"
    assert features["challenge.2fa"]["category"] == "challenge"
    assert features["challenge.2fa"]["cardinality"] == "multiple"
    assert "sign.iframe.image_captcha" in features
    assert "sign.iframe_form" not in features
    assert "flow.page_signals" not in features
    assert features["flow.result"]["category"] == "flow"
    assert features["flow.result"]["cardinality"] == "multiple"
    assert "output_value" in features["flow.result"]["config_schema"]["properties"]


def test_template_crud_and_resolution(client: TestClient) -> None:
    created = _create_template(client, _base_template())
    assert created["valid"] is True
    assert created["lane"] is None
    assert created["step_count"] == 3
    assert created["resolved"]["steps"][0]["config"]["url"] == "{base_url}"

    template_id = created["id"]
    updated_payload = _base_template()
    updated_payload["description"] = "更新后的模板"
    updated_payload["steps"][2]["config"] = {"success_signals": ["签到成功"]}
    response = client.put(f"/api/v1/templates/{template_id}", json=updated_payload)
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 2
    assert response.json()["description"] == "更新后的模板"

    listing = client.get("/api/v1/templates")
    assert listing.status_code == 200
    assert listing.json()[0]["id"] == template_id

    resolved = client.get(f"/api/v1/templates/{template_id}/resolved")
    assert resolved.status_code == 200
    assert resolved.json()["steps"][-1]["config"]["success_signals"] == ["签到成功"]

    assert client.delete(f"/api/v1/templates/{template_id}").status_code == 204
    assert client.get(f"/api/v1/templates/{template_id}").status_code == 404


def test_template_inheritance_override_disable_and_null(client: TestClient) -> None:
    parent = _create_template(client, _base_template())
    child = _create_template(
        client,
        {
            "name": "nexusphp-child",
            "parent_id": parent["id"],
            "steps": [
                {
                    "step_key": "open",
                    "feature_type": "flow.open",
                    "enabled": False,
                },
                {
                    "step_key": "login",
                    "feature_type": "auth.form_login",
                    "config": {"success_selector": None},
                },
                {
                    "step_key": "sign",
                    "feature_type": "sign.button",
                    "config": {
                        "button_selector": ["#sign"],
                        "success_signals": ["已签到"],
                    },
                },
                {
                    "step_key": "wait",
                    "feature_type": "action.wait",
                    "config": {"seconds": 3},
                },
            ],
        },
    )

    steps = {item["step_key"]: item for item in child["resolved"]["steps"]}
    assert child["resolved"]["lane"] is None
    assert steps["open"]["enabled"] is False
    assert steps["login"]["config"]["success_selector"] == []
    assert steps["login"]["source"] == "template:nexusphp-child"
    assert steps["sign"]["config"]["success_signals"] == ["已签到"]
    assert [item["step_key"] for item in child["resolved"]["steps"]] == [
        "open",
        "login",
        "sign",
        "wait",
    ]


def test_required_feature_field_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/templates",
        json={
            "name": "totp-without-hint",
            "steps": [
                {
                    "step_key": "totp",
                    "feature_type": "challenge.2fa",
                    "config": {
                        "code_selector": ["#code"],
                        "submit_selector": ["button"],
                    },
                }
            ],
        },
    )
    assert response.status_code == 422
    assert "service_hint" in response.json()["detail"]


def test_step_order_follows_list_order(client: TestClient) -> None:
    created = _create_template(
        client,
        {
            "name": "ordered-template",
            "steps": [
                {"step_key": "sign", "feature_type": "sign.button"},
                {"step_key": "open", "feature_type": "flow.open"},
                _login_step(),
            ],
        },
    )
    assert [item["step_key"] for item in created["resolved"]["steps"]] == [
        "sign",
        "open",
        "login",
    ]

    payload = {
        "name": "ordered-template",
        "steps": [
            _login_step(),
            {"step_key": "sign", "feature_type": "sign.button"},
            {"step_key": "open", "feature_type": "flow.open"},
        ],
    }
    updated = client.put(f"/api/v1/templates/{created['id']}", json=payload)
    assert updated.status_code == 200, updated.text
    assert [item["step_key"] for item in updated.json()["resolved"]["steps"]] == [
        "login",
        "sign",
        "open",
    ]


def test_new_step_can_be_inserted_between_template_steps(client: TestClient) -> None:
    parent = _create_template(client, _base_template())
    child = _create_template(
        client,
        {
            "name": "inserted-step",
            "parent_id": parent["id"],
            "steps": [
                {
                    "step_key": "wait",
                    "feature_type": "action.wait",
                    "position": 1500,
                    "config": {"seconds": 2},
                }
            ],
        },
    )
    assert [item["step_key"] for item in child["resolved"]["steps"]] == [
        "open",
        "wait",
        "login",
        "sign",
    ]


def test_selector_candidates_are_supported(client: TestClient) -> None:
    payload = _base_template()
    payload["steps"][1]["config"] = {
        "username_selector": ["#username", "input[name='username']"],
        "password_selector": ["#password"],
        "submit_selector": ["button[type='submit']", "input[type='submit']"],
    }
    created = _create_template(client, payload)
    login = next(item for item in created["resolved"]["steps"] if item["step_key"] == "login")
    assert login["config"]["username_selector"] == ["#username", "input[name='username']"]
    assert login["config"]["submit_selector"] == ["button[type='submit']", "input[type='submit']"]

    payload["name"] = "empty-required-selectors"
    payload["steps"][1]["config"] = {
        "username_selector": [],
        "password_selector": ["#password"],
        "submit_selector": ["button"],
    }
    response = client.post("/api/v1/templates", json=payload)
    assert response.status_code == 422
    assert "username_selector" in response.json()["detail"]


def test_singletons_conflict(client: TestClient) -> None:
    payload = _base_template()
    payload["steps"].append(
        {"step_key": "open-again", "feature_type": "flow.open", "config": {}}
    )
    response = client.post("/api/v1/templates", json=payload)
    assert response.status_code == 422
    assert "flow.open" in response.json()["detail"]


def test_site_resolution_lane_and_base_url(client: TestClient) -> None:
    template = _create_template(
        client,
        {
            "name": "nexusphp-turnstile",
            "steps": [
                {"step_key": "open", "feature_type": "flow.open"},
                _login_step(),
                {"step_key": "turnstile", "feature_type": "challenge.turnstile"},
                {"step_key": "sign", "feature_type": "sign.button"},
            ],
        },
    )
    site = _create_site(
        client,
        {
            "name": "example-pt",
            "base_url": "https://pt.example.com/",
            "template_id": template["id"],
            "lane_policy": "interaction",
            "tags": ["PT", "PT", " 主站 "],
            "steps": [
                {
                    "step_key": "login",
                    "feature_type": "auth.form_login",
                    "config": {"password_selector": ["#new-password"]},
                }
            ],
        },
    )

    assert site["base_url"] == "https://pt.example.com"
    assert site["lane"] == "interaction"
    assert site["tags"] == ["PT", "主站"]
    steps = {item["step_key"]: item for item in site["resolved"]["steps"]}
    assert steps["open"]["config"]["url"] == "https://pt.example.com"
    assert steps["sign"]["config"]["button_selector"] == ["button[type='submit']"]
    assert steps["login"]["config"]["password_selector"] == ["#new-password"]
    assert steps["login"]["source"] == "site"

    explicit_general = _create_site(
        client,
        {
            "name": "manual-general",
            "base_url": "https://pt.example.com",
            "template_id": template["id"],
            "lane_policy": "general",
        },
    )
    assert explicit_general["lane"] == "general"

    explicit = _create_site(
        client,
        {
            "name": "manual-proxy",
            "base_url": "https://pt.example.com",
            "lane_policy": "proxy_general",
        },
    )
    assert explicit["lane"] == "proxy_general"


def test_site_crud_and_template_delete_guard(client: TestClient) -> None:
    template = _create_template(client, _base_template())
    site = _create_site(
        client,
        {
            "name": "site-a",
            "base_url": "https://a.example.com",
            "template_id": template["id"],
            "checkin_after": "08:30",
        },
    )
    assert site["template_name"] == "nexusphp-base"
    assert site["checkin_after"] == "08:30"

    updated = client.put(
        f"/api/v1/sites/{site['id']}",
        json={
            "name": "site-a",
            "base_url": "https://a.example.com/new",
            "template_id": template["id"],
            "notes": "updated",
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["config_revision"] == 2
    assert updated.json()["base_url"] == "https://a.example.com/new"
    assert updated.json()["notes"] == "updated"

    response = client.delete(f"/api/v1/templates/{template['id']}")
    assert response.status_code == 409

    response = client.delete(f"/api/v1/sites/{site['id']}")
    assert response.status_code == 204
    assert client.delete(f"/api/v1/templates/{template['id']}").status_code == 204


def test_template_cycle_and_step_validation(client: TestClient) -> None:
    first = _create_template(client, _base_template())
    second = _create_template(
        client,
        {
            "name": "child",
            "parent_id": first["id"],
            "steps": [{"step_key": "wait", "feature_type": "action.wait"}],
        },
    )
    payload = _base_template()
    payload["parent_id"] = second["id"]
    response = client.put(f"/api/v1/templates/{first['id']}", json=payload)
    assert response.status_code == 422
    assert "循环" in response.json()["detail"]

    bad = _base_template()
    bad["name"] = "bad-unknown-feature"
    bad["steps"] = [{"step_key": "x", "feature_type": "missing.feature", "config": {}}]
    assert client.post("/api/v1/templates", json=bad).status_code == 422

    bad = _base_template()
    bad["name"] = "bad-duplicate-key"
    bad["steps"] = [
        {"step_key": "same", "feature_type": "action.wait", "config": {}},
        {"step_key": "same", "feature_type": "action.wait", "config": {}},
    ]
    assert client.post("/api/v1/templates", json=bad).status_code == 422

    bad = _base_template()
    bad["name"] = "bad-config-type"
    bad["steps"] = [
        {
            "step_key": "wait",
            "feature_type": "action.wait",
            "config": {"seconds": "not-a-number"},
        }
    ]
    assert client.post("/api/v1/templates", json=bad).status_code == 422


def test_site_validation_and_csrf(client: TestClient) -> None:
    assert client.post(
        "/api/v1/sites",
        json={"name": "x", "base_url": "ftp://example.com"},
    ).status_code == 422

    assert client.post(
        "/api/v1/sites",
        json={
            "name": "x",
            "base_url": "https://example.com",
            "checkin_after": "25:00",
        },
    ).status_code == 422

    csrf = client.headers.pop("X-CSRF-Token")
    try:
        response = client.post(
            "/api/v1/templates",
            json={"name": "no-csrf", "steps": []},
        )
        assert response.status_code == 403
    finally:
        client.headers.update({"X-CSRF-Token": csrf})


def _grouped_template(name: str = "grouped-template") -> dict:
    return {
        "name": name,
        "groups": [
            {"group_key": "tgrp_open", "name": "访问", "position": 1000},
            {"group_key": "tgrp_sign", "name": "签到", "position": 2000},
        ],
        "steps": [
            {
                "step_key": "open",
                "feature_type": "flow.open",
                "group_key": "tgrp_open",
                "position": 1000,
            },
            {
                "step_key": "sign",
                "feature_type": "sign.button",
                "group_key": "tgrp_sign",
                "position": 2000,
            },
        ],
    }


def test_groups_and_site_local_group_resolution(client: TestClient) -> None:
    template = _create_template(client, _grouped_template())
    assert [group["group_key"] for group in template["groups"]] == [
        "tgrp_open",
        "tgrp_sign",
    ]
    assert [group["group_key"] for group in template["resolved"]["groups"]] == [
        "tgrp_open",
        "tgrp_sign",
    ]
    site = _create_site(
        client,
        {
            "name": "grouped-site",
            "base_url": "https://pt.example.com",
            "template_id": template["id"],
            "groups": [
                {"group_key": "sgrp_extra", "name": "站点扩展", "position": 1500}
            ],
            "steps": [
                {
                    "step_key": "site_wait",
                    "feature_type": "action.wait",
                    "group_key": "sgrp_extra",
                    "position": 1500,
                    "config": {"seconds": 1},
                }
            ],
        },
    )
    assert [item["step_key"] for item in site["resolved"]["steps"]] == [
        "open",
        "site_wait",
        "sign",
    ]
    assert [group["group_key"] for group in site["resolved"]["groups"]] == [
        "tgrp_open",
        "sgrp_extra",
        "tgrp_sign",
    ]


def test_used_template_locks_existing_groups_but_allows_additions(
    client: TestClient,
) -> None:
    payload = _grouped_template("locked-template")
    template = _create_template(client, payload)
    _create_site(
        client,
        {
            "name": "locked-template-site",
            "base_url": "https://pt.example.com",
            "template_id": template["id"],
        },
    )
    detail = client.get(f"/api/v1/templates/{template['id']}").json()
    assert detail["groups_locked"] is True
    assert detail["dependent_site_count"] == 1

    renamed = {**payload, "groups": [dict(item) for item in payload["groups"]]}
    renamed["groups"][0]["name"] = "改名"
    response = client.put(f"/api/v1/templates/{template['id']}", json=renamed)
    assert response.status_code == 409

    added_group = {
        **payload,
        "groups": [
            *payload["groups"],
            {"group_key": "tgrp_new", "name": "新分组", "position": 3000},
        ],
    }
    response = client.put(f"/api/v1/templates/{template['id']}", json=added_group)
    assert response.status_code == 200, response.text
    assert [group["group_key"] for group in response.json()["groups"]] == [
        "tgrp_open",
        "tgrp_sign",
        "tgrp_new",
    ]

    added_step = {
        **payload,
        "groups": added_group["groups"],
        "steps": [
            *payload["steps"],
            {
                "step_key": "wait",
                "feature_type": "action.wait",
                "group_key": "tgrp_new",
                "position": 2500,
                "config": {"seconds": 1},
            },
            {
                "step_key": "ungrouped",
                "feature_type": "action.wait",
                "position": 2600,
                "config": {"seconds": 1},
            },
        ],
    }
    response = client.put(f"/api/v1/templates/{template['id']}", json=added_step)
    assert response.status_code == 200, response.text
    assert "wait" in [item["step_key"] for item in response.json()["resolved"]["steps"]]

    moved_existing = {
        **added_step,
        "steps": [
            {**payload["steps"][0], "group_key": "tgrp_sign"},
            *added_step["steps"][1:],
        ],
    }
    response = client.put(f"/api/v1/templates/{template['id']}", json=moved_existing)
    assert response.status_code == 409

    reordered = {
        **added_step,
        "groups": [
            {**added_step["groups"][0], "position": 2000},
            {**added_step["groups"][1], "position": 1000},
            *added_step["groups"][2:],
        ],
    }
    response = client.put(f"/api/v1/templates/{template['id']}", json=reordered)
    assert response.status_code == 409


def test_site_support_resign_injects_system_signal(client: TestClient) -> None:
    payload = {
        "name": "resign-signal-site",
        "base_url": "https://pt.example.com",
        "steps": [
            {
                "step_key": "resign",
                "feature_type": "post.resign",
                "condition": "signal:sys_resign_pending:true",
                "position": 1000,
            }
        ],
    }
    response = client.post("/api/v1/sites", json=payload)
    assert response.status_code == 201, response.text
    detail = response.json()
    assert detail["support_resign"] is True
    assert detail["resign_max_days"] == 30
    assert any(
        signal["signal_key"] == "sys_resign_pending"
        and signal["source"] == "system"
        for signal in detail["resolved_signals"]
    )

    disabled = {**payload, "support_resign": False, "resign_max_days": 60}
    response = client.put(f"/api/v1/sites/{detail['id']}", json=disabled)
    assert response.status_code == 200, response.text
    assert response.json()["support_resign"] is False
    assert response.json()["resign_max_days"] == 60
    assert any(
        signal["signal_key"] == "sys_resign_pending"
        and "不支持补签时恒为假" in signal["description"]
        for signal in response.json()["resolved_signals"]
    )

    reserved = {
        **disabled,
        "signals": [
            {
                "signal_key": "sys_resign_pending",
                "label": "自定义断签",
                "include_texts": ["x"],
            }
        ],
    }
    response = client.put(f"/api/v1/sites/{detail['id']}", json=reserved)
    assert response.status_code == 422
    assert "保留" in response.json()["detail"]


def test_compound_signal_conditions(client: TestClient) -> None:
    payload = {
        "name": "compound-condition-site",
        "base_url": "https://pt.example.com",
        "signals": [
            {
                "signal_key": "sig_signed",
                "label": "已签到",
                "include_texts": ["已签到"],
            },
            {
                "signal_key": "sig_captcha",
                "label": "验证码",
                "include_texts": ["验证码"],
            },
        ],
        "steps": [
            {
                "step_key": "resign",
                "feature_type": "post.resign",
                "condition": (
                    "all(signal:sig_signed:false,"
                    "any(signal:sig_captcha:true,signal:sys_resign_pending:true))"
                ),
                "position": 1000,
            }
        ],
    }
    response = client.post("/api/v1/sites", json=payload)
    assert response.status_code == 201, response.text
    detail = response.json()
    assert detail["steps"][0]["condition"].startswith("all(")
    assert {item["signal_key"] for item in detail["resolved_signals"]} >= {
        "sig_signed",
        "sig_captcha",
        "sys_resign_pending",
    }

    invalid = {
        **payload,
        "name": "compound-condition-invalid",
        "steps": [
            {
                "step_key": "resign",
                "feature_type": "post.resign",
                "condition": "all(signal:sig_signed:false,signal:sig_missing:true)",
                "position": 1000,
            }
        ],
    }
    response = client.post("/api/v1/sites", json=invalid)
    assert response.status_code == 422
    assert "sig_missing" in response.json()["detail"]


def test_template_can_use_system_resign_signal(client: TestClient) -> None:
    template = _create_template(
        client,
        {
            "name": "template-resign-signal",
            "steps": [
                {
                    "step_key": "resign",
                    "feature_type": "post.resign",
                    "condition": "signal:sys_resign_pending:true",
                    "position": 1000,
                }
            ],
        },
    )
    assert template["valid"] is True
    assert any(
        signal["signal_key"] == "sys_resign_pending"
        and signal["source"] == "system"
        for signal in template["resolved_signals"]
    )


def test_site_cannot_use_template_group_or_insert_inside_group(client: TestClient) -> None:
    payload = _grouped_template("range-template")
    payload["groups"] = [{"group_key": "tgrp_all", "name": "全部", "position": 1000}]
    payload["steps"][0]["group_key"] = "tgrp_all"
    payload["steps"][1]["group_key"] = "tgrp_all"
    payload["steps"][1]["position"] = 3000
    template = _create_template(client, payload)

    response = client.post(
        "/api/v1/sites",
        json={
            "name": "bad-group-site",
            "base_url": "https://pt.example.com",
            "template_id": template["id"],
            "steps": [
                {
                    "step_key": "site_wait",
                    "feature_type": "action.wait",
                    "group_key": "tgrp_all",
                    "position": 2000,
                    "config": {"seconds": 1},
                }
            ],
        },
    )
    assert response.status_code == 422
    assert "模板分组" in response.json()["detail"]

    response = client.post(
        "/api/v1/sites",
        json={
            "name": "inside-group-site",
            "base_url": "https://pt.example.com",
            "template_id": template["id"],
            "groups": [
                {"group_key": "sgrp_extra", "name": "站点扩展", "position": 2000}
            ],
            "steps": [
                {
                    "step_key": "site_wait",
                    "feature_type": "action.wait",
                    "group_key": "sgrp_extra",
                    "position": 2000,
                    "config": {"seconds": 1},
                }
            ],
        },
    )
    # Step coordinates in an independent local group do not intersect inherited groups.
    assert response.status_code == 201


def test_ungrouped_steps_between_inherited_groups_roundtrip(client: TestClient) -> None:
    parent = _create_template(client, {
        "name": "independent-group-ranges",
        "groups": [
            {"group_key": "tgrp_first", "name": "First", "position": 1000000,
             "step_anchor": 1000},
            {"group_key": "tgrp_second", "name": "Second", "position": 2000000,
             "step_anchor": 3000},
        ],
        "steps": [
            {"step_key": "first_open", "feature_type": "flow.open",
             "group_key": "tgrp_first", "position": 1000},
            {"step_key": "first_wait", "feature_type": "action.wait",
             "group_key": "tgrp_first", "position": 9000},
            {"step_key": "second_wait", "feature_type": "action.wait",
             "group_key": "tgrp_second", "position": 3000},
        ],
    })
    local_step = {"step_key": "between", "feature_type": "action.wait", "position": 2000}
    expected = ["first_open", "first_wait", "between", "second_wait"]
    for kind in ("sites", "templates"):
        payload = {
            "name": f"between-{kind}", "steps": [local_step],
            **({"base_url": "https://example.com", "template_id": parent["id"]}
               if kind == "sites" else {"parent_id": parent["id"]}),
        }
        created = client.post(f"/api/v1/{kind}", json=payload)
        assert created.status_code == 201, created.text
        item_id = created.json()["id"]
        for response in (
            created,
            client.put(f"/api/v1/{kind}/{item_id}", json=payload),
            client.get(f"/api/v1/{kind}/{item_id}"),
        ):
            assert response.status_code in (200, 201), response.text
            assert [step["step_key"] for step in response.json()["resolved"]["steps"]] == expected


def test_used_ungrouped_template_can_be_grouped_once(client: TestClient) -> None:
    template = _create_template(client, _base_template())
    _create_site(
        client,
        {
            "name": "legacy-ungrouped-site",
            "base_url": "https://pt.example.com",
            "template_id": template["id"],
        },
    )
    payload = _base_template()
    response = client.put(f"/api/v1/templates/{template['id']}", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["groups"] == []

    payload["groups"] = [
        {"group_key": "tgrp_login", "name": "登录", "position": 1000},
        {"group_key": "tgrp_sign", "name": "签到", "position": 2000},
    ]
    for step in payload["steps"]:
        step["group_key"] = "tgrp_sign" if step["step_key"] == "sign" else "tgrp_login"
    response = client.put(f"/api/v1/templates/{template['id']}", json=payload)
    assert response.status_code == 200, response.text
    assert len(response.json()["groups"]) == 2

    payload["groups"][0]["name"] = "已锁定"
    response = client.put(f"/api/v1/templates/{template['id']}", json=payload)
    assert response.status_code == 409


def test_site_bitwarden_preferences_roundtrip(client: TestClient) -> None:
    response = client.post(
        "/api/v1/sites",
        json={
            "name": "bitwarden-site",
            "base_url": "https://pt.example.com",
            "bitwarden_enabled": True,
            "bitwarden_match_mode": "root_domain",
            "steps": [{"step_key": "open", "feature_type": "flow.open"}],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["bitwarden_enabled"] is True
    assert body["bitwarden_match_mode"] == "root_domain"
    assert body["bitwarden_custom_match"] is None

    site_id = body["id"]
    updated = client.put(
        f"/api/v1/sites/{site_id}",
        json={
            "name": "bitwarden-site",
            "base_url": "https://pt.example.com",
            "bitwarden_enabled": True,
            "bitwarden_match_mode": "custom",
            "bitwarden_custom_match": "  my-pt-entry  ",
            "steps": [{"step_key": "open", "feature_type": "flow.open"}],
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["bitwarden_custom_match"] == "my-pt-entry"


def test_site_bitwarden_custom_match_requires_value(client: TestClient) -> None:
    response = client.post(
        "/api/v1/sites",
        json={
            "name": "bitwarden-custom-missing",
            "base_url": "https://pt.example.com",
            "bitwarden_enabled": True,
            "bitwarden_match_mode": "custom",
            "steps": [{"step_key": "open", "feature_type": "flow.open"}],
        },
    )
    assert response.status_code == 422


def test_sign_iframe_form_alias_upgrades_to_image_captcha(client: TestClient) -> None:
    response = client.post(
        "/api/v1/sites",
        json={
            "name": "legacy-iframe-site",
            "base_url": "https://open.cd",
            "steps": [
                {
                    "step_key": "iframe",
                    "feature_type": "sign.iframe_form",
                    "config": {
                        "iframe_selector": ["iframe[id='i_signin']"],
                        "submit_selector": ["button[id='ok']"],
                    },
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    detail = response.json()
    step = detail["steps"][0]
    assert step["feature_type"] == "sign.iframe.image_captcha"
    resolved = detail["resolved"]["steps"][0]
    assert resolved["config"]["image_selector"]
    assert resolved["config"]["input_selector"] == [
        "input[name='imagestring']",
        "input[name='captcha']",
    ]


def test_flow_jump_requires_condition_and_target(client: TestClient) -> None:
    missing_condition = client.post(
        "/api/v1/templates",
        json={
            "name": "jump-missing-condition",
            "steps": [{"step_key": "jump", "feature_type": "flow.jump"}],
        },
    )
    assert missing_condition.status_code == 422

    missing_target = client.post(
        "/api/v1/templates",
        json={
            "name": "jump-missing-target",
            "steps": [
                {
                    "step_key": "jump",
                    "feature_type": "flow.jump",
                    "config": {"texts": ["验证失败"], "action": "jump"},
                }
            ],
        },
    )
    assert missing_target.status_code == 422

    valid = client.post(
        "/api/v1/templates",
        json={
            "name": "jump-valid",
            "steps": [
                {"step_key": "open", "feature_type": "flow.open"},
                {
                    "step_key": "jump",
                    "feature_type": "flow.jump",
                    "config": {
                        "texts": ["验证失败"],
                        "selectors": [".ant-modal .error"],
                        "action": "jump",
                        "target_step_key": "open",
                        "max_jumps": 5,
                    },
                },
            ],
        },
    )
    assert valid.status_code == 201, valid.text
    jump = next(step for step in valid.json()["steps"] if step["step_key"] == "jump")
    assert jump["config"]["max_jumps"] == 5


def test_group_order_controls_resolved_step_order(client: TestClient) -> None:
    response = client.post(
        "/api/v1/templates",
        json={
            "name": "group-order",
            "groups": [
                {"group_key": "tgrp_a", "name": "A", "position": 9000},
                {"group_key": "tgrp_b", "name": "B", "position": 2500},
            ],
            "steps": [
                {
                    "step_key": "open",
                    "feature_type": "flow.open",
                    "group_key": "tgrp_a",
                    "position": 1000,
                },
                {
                    "step_key": "navigate",
                    "feature_type": "action.navigate",
                    "group_key": "tgrp_b",
                    "position": 2000,
                },
                {
                    "step_key": "wait",
                    "feature_type": "action.wait",
                    "group_key": "tgrp_a",
                    "position": 3000,
                    "config": {"seconds": 1},
                },
                {
                    "step_key": "click",
                    "feature_type": "action.click",
                    "position": 500,
                    "config": {"selector": ["a"]},
                },
                {
                    "step_key": "click2",
                    "feature_type": "action.click",
                    "position": 1500,
                    "config": {"selector": ["b"]},
                },
            ],
        },
    )
    assert response.status_code == 201, response.text
    keys = [step["step_key"] for step in response.json()["resolved"]["steps"]]
    assert keys == ["click", "navigate", "click2", "open", "wait"]


def test_override_positions_advance_by_layer(client: TestClient) -> None:
    parent = client.post(
        "/api/v1/templates",
        json={
            "name": "override-parent",
            "steps": [
                {"step_key": "open", "feature_type": "flow.open", "position": 1000}
            ],
        },
    )
    assert parent.status_code == 201, parent.text
    child = client.post(
        "/api/v1/templates",
        json={
            "name": "override-child",
            "parent_id": parent.json()["id"],
            "steps": [
                {
                    "step_key": "open",
                    "feature_type": "flow.open",
                    "config": {"wait_until": "load"},
                }
            ],
        },
    )
    assert child.status_code == 201, child.text
    child_step = child.json()["resolved"]["steps"][0]
    assert child_step["step_key"] == "open"
    assert child_step["position"] == 1001

    site = client.post(
        "/api/v1/sites",
        json={
            "name": "override-site",
            "base_url": "https://example.com",
            "template_id": child.json()["id"],
            "steps": [
                {
                    "step_key": "open",
                    "feature_type": "flow.open",
                    "config": {"wait_until": "networkidle"},
                }
            ],
        },
    )
    assert site.status_code == 201, site.text
    site_step = site.json()["resolved"]["steps"][0]
    assert site_step["step_key"] == "open"
    assert site_step["position"] == 1002


def test_sites_options_view_is_lightweight(client: TestClient) -> None:
    site = _create_site(
        client,
        {
            "name": "options-site",
            "base_url": "https://options.example.com",
            "steps": [
                {"step_key": "wait", "feature_type": "action.wait", "config": {"seconds": 1}}
            ],
        },
    )
    response = client.get("/api/v1/sites?view=options")
    assert response.status_code == 200, response.text
    item = next(row for row in response.json() if row["id"] == site["id"])
    assert set(item) == {
        "id",
        "name",
        "base_url",
        "enabled",
        "template_id",
        "template_name",
        "lane_policy",
        "lane",
        "valid",
        "validation_error",
    }
    assert item["valid"] is None

    response = client.get("/api/v1/sites?view=options&include_valid=true")
    assert response.status_code == 200, response.text
    item = next(row for row in response.json() if row["id"] == site["id"])
    assert item["valid"] is True
    assert item["lane"] == "general"


def test_list_sites_uses_bounded_queries(client: TestClient) -> None:
    for index in range(8):
        _create_site(
            client,
            {
                "name": f"bounded-site-{index}",
                "base_url": f"https://pt{index}.example.com",
                "steps": [
                    {
                        "step_key": "wait",
                        "feature_type": "action.wait",
                        "config": {"seconds": 1},
                    }
                ],
            },
        )
    app = client.app
    statements: list[str] = []

    def before(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        statements.append(statement)

    event.listen(app.state.engine, "before_cursor_execute", before)
    try:
        with app.state.session_factory() as session:
            rows = WorkflowService(
                session, app.state.settings, app.state.crypto
            ).list_sites()
    finally:
        event.remove(app.state.engine, "before_cursor_execute", before)
    assert len(rows) >= 8
    assert len(statements) < 60
