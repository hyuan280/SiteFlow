from __future__ import annotations

import json
import re

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.features.base import FeatureConfigError
from app.features.catalog import PostBuyCardConfig, PurchaseClick, get_feature
from app.models.workflow import Site, WorkflowStep
from app.services.workflows import WorkflowService


KEY = "post.buy_card"
DEFAULT_CLICKS = [{
    "scope": "product",
    "selectors": ["input[type='submit']", "button[type='submit']"],
}]
DEFAULTS = {
    "shop_url": "{base_url}/mybonus.php",
    "card_title": ["购买补签卡"],
    "purchase_quantity": 0,
    "layout": "table",
    "product_selectors": [],
    "title_selectors": ["h1", "h2", "h3", ".magic-card-header", ".card-title"],
    "price_selectors": [],
    "price_column_index": 2,
    "price_regex": r"([+-]?[0-9][0-9,]*(?:\.[0-9]+)?)",
    "points_selectors": [],
    "points_regex": r"魔力值[^\d+\-]*([+-]?\d[\d,]*(?:\.\d+)?)",
    "card_count_selectors": [],
    "card_count_regex": r"[补補][签簽]卡[:：]\s*(\d+)",
    "card_count_url": "",
    "verification_timeout_seconds": 10,
    "quantity_selectors": [],
    "purchase_clicks": DEFAULT_CLICKS,
    "option_selector": 'input[name="option"]',
    "exchange_action": "exchange",
    "submit_value": "交换",
    "max_price": None,
    "purchase_interval_seconds": 12,
}


def test_buy_card_schema_and_defaults(client):
    response = client.get("/api/v1/features")
    assert response.status_code == 200, response.text
    view = next(item for item in response.json() if item["key"] == KEY)
    assert view["version"] == 1
    assert view["defaults"] == DEFAULTS
    assert view["required_fields"] == view["base_required_fields"] == []
    assert get_feature(KEY).validate_full({}) == DEFAULTS
    schema = view["config_schema"]
    assert schema["additionalProperties"] is False
    props = schema["properties"]
    assert set(props) == set(DEFAULTS)
    assert all(props[key]["default"] == value for key, value in DEFAULTS.items())
    assert props["purchase_quantity"]["title"] == "购买数量"
    for key, minimum, maximum in (
        ("purchase_quantity", 0, 365), ("verification_timeout_seconds", 1, 60),
        ("price_column_index", 0, 10), ("purchase_interval_seconds", 0, 120),
    ):
        assert props[key]["type"] == "integer"
        assert props[key]["minimum"] == minimum
        assert props[key]["maximum"] == maximum
    assert props["layout"]["enum"] == ["table", "grid", "custom"]
    assert props["layout"]["enum_titles"] == {
        "table": "表格", "grid": "网格卡片", "custom": "自定义",
    }
    for key in (
        "product_selectors", "title_selectors", "price_selectors",
        "points_selectors", "card_count_selectors", "card_title", "quantity_selectors",
    ):
        assert props[key]["type"] == "array"
        assert props[key]["items"]["type"] == "string"
        assert props[key]["items"]["minLength"] == 1
    assert props["card_title"]["minItems"] == 1
    quantity = props["quantity_selectors"]
    assert quantity["title"] == "购买数量输入框候选"
    assert quantity["maxItems"] == 20
    assert quantity.get("minItems", 0) == 0
    assert "ui:widget" not in quantity
    for hint in ("当前唯一商品内", "留空逐张", "目标剩余", "余额", "min/max/step", "单张价格"):
        assert hint in quantity["description"]
    assert "单张价格" in props["price_selectors"]["description"]
    assert "单张价格" in props["price_regex"]["description"]
    clicks = props["purchase_clicks"]
    assert clicks["type"] == "array"
    assert clicks["minItems"] == 1
    assert clicks["maxItems"] == 10
    assert clicks["ui:widget"] == "purchase-clicks"
    click_schema = schema["$defs"][clicks["items"]["$ref"].rsplit("/", 1)[1]]
    assert click_schema["additionalProperties"] is False
    assert click_schema["required"] == ["selectors"]
    click_props = click_schema["properties"]
    assert set(click_props) == {"scope", "selectors"}
    assert click_props["scope"]["title"] == "点击范围"
    assert click_props["scope"]["enum"] == ["product", "page"]
    assert click_props["scope"]["default"] == "product"
    assert click_props["selectors"]["title"] == "点击选择器候选"
    assert click_props["selectors"]["minItems"] == 1
    assert click_props["selectors"]["maxItems"] == 20
    assert click_props["selectors"]["items"] == {"type": "string", "minLength": 1}
    assert props["max_price"]["anyOf"][0] == {"type": "integer", "minimum": 1}
    for key in ("option_selector", "exchange_action", "submit_value"):
        assert props[key]["ui:hidden"] is True
        assert "已废弃" in props[key]["description"]
        assert "忽略" in props[key]["description"]


@pytest.mark.parametrize("config", [
    {"purchase_quantity": -1}, {"purchase_quantity": 366}, {"purchase_quantity": 1.5},
    {"verification_timeout_seconds": 0}, {"verification_timeout_seconds": 61},
    {"verification_timeout_seconds": 1.5}, {"layout": "cards"},
    {"layout": "grid"}, {"layout": "custom", "price_selectors": [".price"]},
    {"layout": "custom", "product_selectors": [".product"]},
    {"purchase_clicks": []}, {"purchase_clicks": DEFAULT_CLICKS * 11},
    {"purchase_clicks": [{}]}, {"purchase_clicks": [{"selectors": []}]},
    {"purchase_clicks": [{"selectors": ["#buy"] * 21}]},
    {"purchase_clicks": [{"selectors": [" "]}]},
    {"purchase_clicks": [{"selectors": "#buy"}]},
    {"purchase_clicks": [{"scope": "dialog", "selectors": ["#buy"]}]},
    {"purchase_clicks": [{"selectors": ["#buy"], "quantity": 2}]},
    {"purchase_clicks": ["#buy"]}, {"card_title": []}, {"card_title": [" "]},
    {"max_price": 0}, {"max_price": 1.5}, {"price_column_index": -1},
    {"price_column_index": 11}, {"purchase_interval_seconds": -1},
    {"purchase_interval_seconds": 121}, {"unknown": True},
    {"dialog_selector": "#dialog"}, {"quantity_selector": "#quantity"},
    {"quantity_scope": "page"}, {"quantity_selectors": ["#quantity"] * 21},
    {"quantity_selectors": [""]}, {"quantity_selectors": [1]},
    {"quantity_selectors": [{"scope": "page", "selectors": ["#quantity"]}]},
] + [{key: value} for key in (
    "product_selectors", "title_selectors", "price_selectors",
    "points_selectors", "card_count_selectors", "quantity_selectors",
) for value in ([" "], "#element")])
def test_buy_card_rejects_invalid_config(client, config):
    with pytest.raises(FeatureConfigError):
        get_feature(KEY).validate_full(config)
    response = client.post("/api/v1/templates", json={
        "name": "invalid-buy-card",
        "steps": [{"step_key": "buy", "feature_type": KEY, "config": config}],
    })
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("click", [
    {"scope": "dialog", "selectors": ["#buy"]},
    {"selectors": ["#buy"], "unknown": True},
])
def test_purchase_click_forbids_invalid_scope_and_unknown_fields(click):
    with pytest.raises(ValidationError):
        PurchaseClick.model_validate(click)
    with pytest.raises(FeatureConfigError):
        get_feature(KEY).validate_override({"purchase_clicks": [click]})


@pytest.mark.parametrize("quantity,timeout", [(0, 1), (1, 10), (365, 60)])
def test_buy_card_quantity_and_click_boundaries(quantity, timeout):
    config = get_feature(KEY).validate_full({
        "purchase_quantity": quantity,
        "verification_timeout_seconds": timeout,
        "purchase_clicks": [{"selectors": [" #buy "] * 20}] * 10,
        "quantity_selectors": [" #quantity "] * 20,
    })
    assert config["purchase_quantity"] == quantity
    assert config["verification_timeout_seconds"] == timeout
    assert config["quantity_selectors"] == ["#quantity"] * 20
    assert config["purchase_clicks"] == [{"scope": "product", "selectors": ["#buy"] * 20}] * 10
    first = PostBuyCardConfig()
    assert isinstance(first.purchase_clicks[0], PurchaseClick)
    first.purchase_clicks[0].selectors.append("#other")
    first.quantity_selectors.append("#quantity")
    assert PostBuyCardConfig().quantity_selectors == []
    assert PostBuyCardConfig().model_dump(mode="json")["purchase_clicks"] == DEFAULT_CLICKS


@pytest.mark.parametrize("layout,products", [
    ("table", []), ("grid", []), ("custom", [".product"]),
])
def test_buy_card_layout_selector_contract(layout, products):
    config = get_feature(KEY).validate_full({
        "layout": layout, "product_selectors": products, "price_selectors": [" .price "],
        "points_selectors": [], "card_count_selectors": [],
    })
    assert config["layout"] == layout
    assert config["product_selectors"] == products
    assert config["price_selectors"] == [".price"]
    assert config["points_selectors"] == config["card_count_selectors"] == []
    props = get_feature(KEY).config_schema()["properties"]
    assert "table tr" in props["product_selectors"]["description"]
    for selector in ("form.magic-card", "section article", "article"):
        assert selector in props["product_selectors"]["description"]
    for key in ("points_selectors", "card_count_selectors"):
        assert "body" in props[key]["description"]


@pytest.mark.parametrize("value", ["0", "7", "1,234", "9.5", "12,345.67"])
def test_buy_card_default_numeric_regexes(value):
    config = PostBuyCardConfig()
    assert re.search(config.points_regex, f"魔力值：{value} 点").group(1) == value
    assert re.search(config.price_regex, f"价格 {value} 魔力").group(1) == value


def test_buy_card_feature_settings_roundtrip(client):
    path = f"/api/v1/features/{KEY}/settings"
    defaults = {
        "purchase_quantity": 2,
        "layout": "grid",
        "price_selectors": [".price"],
        "quantity_selectors": ["#quantity", "input[name='quantity']"],
        "purchase_clicks": [
            {"scope": "product", "selectors": [".buy"]},
            {"scope": "page", "selectors": ["#confirm", ".confirm"]},
        ],
    }
    response = client.put(path, json={"defaults": defaults, "required_fields": []})
    assert response.status_code == 200, response.text
    assert response.json()["defaults"] == {**DEFAULTS, **defaults}
    assert response.json()["config_schema"]["properties"]["purchase_clicks"]["default"] == (
        defaults["purchase_clicks"]
    )
    assert response.json()["config_schema"]["properties"]["quantity_selectors"]["default"] == (
        defaults["quantity_selectors"]
    )
    response = client.get("/api/v1/features")
    assert response.status_code == 200, response.text
    view = next(item for item in response.json() if item["key"] == KEY)
    assert view["defaults"]["quantity_selectors"] == defaults["quantity_selectors"]
    for invalid in (
        {"purchase_clicks": []}, {"quantity_selectors": [" "]},
        {"quantity_selectors": "#quantity"}, {"quantity_selectors": ["#quantity"] * 21},
    ):
        response = client.put(path, json={"defaults": invalid, "required_fields": []})
        assert response.status_code == 422, response.text
    response = client.delete(path)
    assert response.status_code == 200, response.text
    assert response.json()["defaults"] == DEFAULTS


@pytest.mark.parametrize("quantity", [None, 0, 3, 365])
def test_buy_card_auto_default_and_fixed_quantity_without_pending(client, quantity):
    config = {} if quantity is None else {"purchase_quantity": quantity}
    response = client.post("/api/v1/sites", json={
        "name": "buy-without-pending", "base_url": "https://pt.example.com",
        "steps": [{"step_key": "buy", "feature_type": KEY, "config": config}],
    })
    assert response.status_code == 201, response.text
    site = response.json()
    assert site["valid"] is True
    step = site["resolved"]["steps"][0]
    assert step["config"]["purchase_quantity"] == (quantity or 0)
    assert step["condition"] == "always"
    assert step["feature_version"] == 1


@pytest.mark.parametrize("quantity_override,expected_selectors", [
    ({}, ["#parent-quantity", ".parent-quantity"]),
    ({"quantity_selectors": [" #quantity ", ".quantity"]}, ["#quantity", ".quantity"]),
    ({"quantity_selectors": []}, []),
    ({"quantity_selectors": None}, []),
])
def test_buy_card_inheritance_replaces_clicks_and_preserves_raw_history(
    client, quantity_override, expected_selectors,
):
    parent_config = {
        "purchase_quantity": 4, "card_count_url": "{base_url}/inventory.php",
        "quantity_selectors": ["#parent-quantity", ".parent-quantity"],
        "purchase_clicks": [
            {"scope": "product", "selectors": ["#buy", ".buy"]},
            {"scope": "page", "selectors": ["#confirm"]},
        ],
    }
    response = client.post("/api/v1/templates", json={
        "name": "buy-parent",
        "steps": [{"step_key": "buy", "feature_type": KEY, "config": parent_config}],
    })
    assert response.status_code == 201, response.text
    parent_id = response.json()["id"]
    child_config = {
        "purchase_clicks": [{"selectors": [" #replacement ", "#fallback"]}],
        **quantity_override,
    }
    payload = {
        "name": "buy-child", "base_url": "https://pt.example.com", "template_id": parent_id,
        "steps": [{"step_key": "buy", "feature_type": KEY, "config": child_config}],
    }
    response = client.post("/api/v1/sites", json=payload)
    assert response.status_code == 201, response.text
    site = response.json()
    resolved = site["resolved"]["steps"][0]["config"]
    assert resolved["purchase_clicks"] == [{
        "scope": "product", "selectors": ["#replacement", "#fallback"],
    }]
    assert resolved["purchase_quantity"] == 4
    assert resolved["quantity_selectors"] == expected_selectors
    assert client.get(f"/api/v1/sites/{site['id']}").json()["resolved"]["steps"][0]["config"] == resolved
    assert resolved["card_count_url"] == "https://pt.example.com/inventory.php"
    assert site["steps"][0]["config"] == child_config
    url = f"/api/v1/sites/{site['id']}"
    history = client.get(f"{url}/versions/1").json()
    assert history["snapshot"]["local"]["steps"][0]["config"] == child_config
    assert history["snapshot"]["dependencies"][0]["local"]["steps"][0]["config"] == parent_config
    with client.app.state.session_factory() as session:
        row = session.scalar(select(WorkflowStep).where(
            WorkflowStep.owner_type == "site", WorkflowStep.owner_id == site["id"],
        ))
        assert json.loads(row.config_json) == child_config
    payload["steps"][0]["config"] = {
        "purchase_clicks": None, "purchase_quantity": 0, "quantity_selectors": None,
    }
    response = client.put(url, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["resolved"]["steps"][0]["config"]["purchase_clicks"] == DEFAULT_CLICKS
    assert response.json()["resolved"]["steps"][0]["config"]["quantity_selectors"] == []
    response = client.post(f"{url}/versions/1/restore", json={"expected_revision": 2})
    assert response.status_code == 200, response.text
    assert response.json()["steps"][0]["config"] == child_config
    assert response.json()["resolved"]["steps"][0]["config"] == resolved
    assert client.get(f"{url}/versions/1").json() == history


def test_buy_card_legacy_json_is_not_migrated_or_rewritten(client):
    legacy = {
        "card_title": "旧标题", "option_selector": "#old-option",
        "exchange_action": "old-exchange", "submit_value": "旧提交",
        "points_regex": r"魔力值[^\d]*(\d[\d,.]*\d)", "max_price": 123,
    }
    raw = json.dumps(legacy, ensure_ascii=False, indent=2)
    app = client.app
    with app.state.session_factory() as session:
        site = Site(name="legacy-buy", base_url="https://pt.example.com", config_revision=7)
        session.add(site)
        session.flush()
        step = WorkflowStep(
            owner_type="site", owner_id=site.id, step_key="buy", feature_type=KEY,
            feature_version=1, category="post", config_json=raw,
        )
        session.add(step)
        session.commit()
        site_id, step_id = site.id, step.id
        WorkflowService(session, app.state.settings, app.state.crypto).baseline_workflows()
    url = f"/api/v1/sites/{site_id}"
    for _ in range(2):
        response = client.get(url)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["steps"][0]["config"] == legacy
        config = body["resolved"]["steps"][0]["config"]
        assert config == {**DEFAULTS, **legacy, "card_title": ["旧标题"],
                          "shop_url": "https://pt.example.com/mybonus.php"}
        history = client.get(f"{url}/versions/7").json()
        assert history["snapshot"]["local"]["steps"][0]["config"] == legacy
        assert history["snapshot"]["features"][KEY]["versions"] == [1]
        with app.state.session_factory() as session:
            row = session.get(WorkflowStep, step_id)
            assert row.config_json == raw
            assert row.feature_version == 1
            assert session.get(Site, site_id).config_revision == 7
