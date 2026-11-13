from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock

from fastapi import HTTPException
import pytest
from pydantic import ValidationError

from app.features.catalog import FEATURES as CATALOG
from tests.test_assistant_browser import Manager, TOKEN, URL, public_dns  # noqa: F401
from tests.test_assistant_private_steps import request
from worker import assistant_step, executor
from worker.assistant_browser import AssistantBrowser, READ_ONLY
from worker.assistant_step import ExecuteRequest, FEATURES, FEATURE_CATEGORIES, validate_step


CASES = [
    ("flow.open", {"url": URL}),
    ("action.navigate", {"url": URL}),
    ("auth.form_login", {"username_selector": ["#u"], "password_selector": ["#p"], "submit_selector": ["#s"]}),
    ("auth.password_verify", {"password_selector": ["#p"], "submit_selector": ["#s"]}),
    ("action.wait", {"seconds": 1}),
    ("sign.button", {"button_selector": ["#sign"]}),
    ("sign.unit3d_claim", {"claim_selector": ["#claim"]}),
    ("challenge.altcha", {"widget_selector": ["#w"], "checkbox_selector": ["#c"], "token_selector": ["#t"]}),
    ("challenge.slider", {"container_selector": ["#c"], "handle_selector": ["#h"]}),
    ("action.click", {"selector": ["#next"]}),
    ("challenge.2fa", {"code_selector": ["#code"]}),
    ("challenge.turnstile", {}),
    ("challenge.image_captcha", {"image_selector": ["#image"], "input_selector": ["#code"]}),
    ("challenge.grid_captcha", {}),
    ("sign.iframe.image_captcha", {"iframe_selector": ["iframe"], "image_selector": ["#image"], "input_selector": ["#code"], "submit_selector": ["#s"]}),
    ("sign.image_quiz", {"image_selector": ["#image"], "answer_selector": [".answer"]}),
    ("action.fill", {"selector": ["#public"], "value": "comment"}),
    ("flow.result", {"output_value": False}),
]


def test_trusted_categories_match_exact_builtin_catalog_metadata():
    assert len(FEATURE_CATEGORIES) == 18
    assert set(FEATURE_CATEGORIES) == FEATURES == {feature for feature, _ in CASES}
    assert FEATURE_CATEGORIES == {key: CATALOG[key].category for key in FEATURES}
    assert FEATURE_CATEGORIES["challenge.2fa"] == "challenge"
    assert not any(key in FEATURES for key, feature in CATALOG.items() if feature.category == "post")


@pytest.mark.parametrize("feature,config", CASES)
def test_unknown_click_allows_fresh_builtin_without_clearing_history(public_dns, monkeypatch, feature, config):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        sid = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        session = browser.sessions[sid]
        try:
            run_step = AsyncMock(side_effect=RuntimeError("callback outcome unavailable"))
            monkeypatch.setattr(executor, "_run_step", run_step)
            original = request("action.click", {"selector": ["#original"]})
            unknown = await browser.execute(sid, original)
            assert unknown["status"] == "unknown"
            original_id = str(original.operation_id)
            cached = session.operations[original_id]

            payload = request(feature, config, runtime={"totp_code": "123456"} if feature == "challenge.2fa" else None)
            if FEATURE_CATEGORIES[feature] == "auth":
                payload.credentials = assistant_step.Credentials(username="private-user", password="private-password")
            # Click/fill remain permitted without broadening the legacy sign flag.
            payload.execution.allow_sign = FEATURE_CATEGORIES[feature] in {"sign", "challenge"}
            validate_targets = AsyncMock()
            monkeypatch.setattr(assistant_step, "validate_secret_targets", validate_targets)

            async def succeed(page, step, **kwargs):
                assert page is manager.page
                assert session.writes_authorized == (feature not in READ_ONLY)
                context = kwargs["secret_payload"].get("_assistant_secret_context")
                if context is not None:
                    await context.resolve(step)
                    with pytest.raises(RuntimeError):
                        await context.resolve(step)
                return "success", "", {"value": False}

            run_step.side_effect = succeed
            result = await browser.execute(sid, payload)
            assert result["status"] == "success", result
            if payload.credentials is not None or feature == "challenge.2fa":
                validate_targets.assert_awaited_once()
            assert result["operation_id"] == str(payload.operation_id)
            assert await browser.execute(sid, payload) == result
            assert await browser.execute(sid, original) == unknown
            assert run_step.await_count == 2
            assert session.operations[original_id] == cached
            assert session.uncertain and session.unresolved_operations == {original_id}
            assert not session.consumed_permits
            assert not session.writes_authorized
            assert manager.context.new_page.await_count == manager.lease_lane.await_count == 1
            changed = original.model_copy(deep=True)
            changed.step["config"]["selector"] = ["#different"]
            with pytest.raises(HTTPException, match="Browser operation conflict"):
                await browser.execute(sid, changed)
        finally:
            await browser.stop()
    asyncio.run(run())


def test_explicit_retry_still_checks_exact_step_and_consumes_permit_once(public_dns, monkeypatch):
    async def run():
        browser = AssistantBrowser(Manager())
        sid = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        try:
            run_step = AsyncMock(return_value=("unknown", "", {}))
            monkeypatch.setattr(executor, "_run_step", run_step)
            original = request("action.click", {"selector": ["#original"]})
            other = request("action.click", {"selector": ["#other"]})
            unknown = await browser.execute(sid, original)
            await browser.execute(sid, other)
            session = browser.sessions[sid]
            approved = original.model_copy(deep=True, update={
                "operation_id": uuid.uuid4(), "retry_of": original.operation_id,
                "retry_authorization": uuid.uuid4(),
            })
            changed = approved.model_copy(deep=True)
            changed.step["config"]["selector"] = ["#different"]
            with pytest.raises(HTTPException, match="retry operation conflict"):
                await browser.execute(sid, changed)
            assert not session.consumed_permits
            run_step.return_value = ("success", "", {})
            result = await browser.execute(sid, approved)
            assert result["retry_of"] == str(original.operation_id)
            assert await browser.execute(sid, approved) == result
            assert session.consumed_permits == {str(approved.retry_authorization)}
            assert session.uncertain and session.unresolved_operations == {str(other.operation_id)}
            assert await browser.execute(sid, original) == unknown
            with pytest.raises(HTTPException, match="retry authorization unavailable"):
                await browser.execute(sid, approved.model_copy(update={"operation_id": uuid.uuid4()}))
            assert run_step.await_count == 3
        finally:
            await browser.stop()
    asyncio.run(run())


@pytest.mark.parametrize("category", [None, "post", "plugin", "", "AUTH"])
def test_missing_or_unapproved_trusted_category_fails_closed(monkeypatch, category):
    if category is None:
        monkeypatch.delitem(FEATURE_CATEGORIES, "action.wait")
    else:
        monkeypatch.setitem(FEATURE_CATEGORIES, "action.wait", category)
    with pytest.raises(ValueError, match="Category denied"):
        validate_step(request("action.wait", {"seconds": 1}))


@pytest.mark.parametrize("feature", ["action.wait", "plugin.custom", "post.buy_card"])
@pytest.mark.parametrize("location", ["step", "config", "execution", "request"])
def test_caller_cannot_spoof_category(feature, location):
    raw = request("action.wait", {"seconds": 1}).model_dump(mode="json")
    raw["step"]["feature_type"] = feature
    target = {"step": raw["step"], "config": raw["step"]["config"],
              "execution": raw["execution"], "request": raw}[location]
    target["category"] = "flow"
    with pytest.raises((ValueError, ValidationError)):
        validate_step(ExecuteRequest.model_validate(raw))
