from __future__ import annotations

import asyncio
import json
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from tests.test_assistant_browser import TOKEN, public_dns  # noqa: F401
from tests.test_assistant_private_steps import CAPTCHA, chromium_fixture, request
from worker import executor, plugin_client
from worker.assistant_step import contains_secret, validate_step


CONFIG = {"code_selector": ["#code"], "totp_provider_id": "plugin.vault",
          "service_hint": "Fixture"}
CODE = "719632"


@pytest.mark.parametrize("provider", [None, "builtin.twofauth", "plugin.vault", "plugin.vault-2"])
def test_host_totp_provider_without_plugin_runtime(provider):
    config = dict(CONFIG)
    if provider is None:
        config.pop("totp_provider_id")
        config.pop("service_hint")
    else:
        config["totp_provider_id"] = provider
    payload = request("challenge.2fa", config, runtime={"totp_code": CODE})
    assert not payload.plugin_runtimes
    assert validate_step(payload)["config"] == config


@pytest.mark.parametrize("provider", [
    "builtin.unknown", "builtin.bitwarden", "unknown", "plugin.", "plugin.Vault",
    "plugin.vault.solve", "plugin.vault_name", "plugin.-vault", "plugin.vault\n",
    "plugin." + "a" * 64,
])
def test_unknown_or_malformed_totp_provider_denied(provider):
    payload = request("challenge.2fa", {**CONFIG, "totp_provider_id": provider},
                      runtime={"totp_code": CODE})
    with pytest.raises(ValueError, match="Provider denied"):
        validate_step(payload)


@pytest.mark.parametrize("hint", [None, "", " \t\n"])
def test_plugin_totp_requires_service_hint(hint):
    config = dict(CONFIG)
    if hint is None:
        config.pop("service_hint")
    else:
        config["service_hint"] = hint
    with pytest.raises(ValueError, match="Service hint unavailable"):
        validate_step(request("challenge.2fa", config, runtime={"totp_code": CODE}))


@pytest.mark.parametrize("runtime", [None, {}, {"totp_code": None}])
def test_plugin_totp_requires_host_private_code(runtime):
    with pytest.raises(ValueError, match="TOTP unavailable"):
        validate_step(request("challenge.2fa", dict(CONFIG), runtime=runtime))


@pytest.mark.parametrize("feature,config", [
    ("action.wait", {"seconds": 1}),
    ("challenge.turnstile", {}),
    ("challenge.image_captcha", {"image_selector": ["#image"], "input_selector": ["#code"]}),
    ("flow.result", {}),
])
def test_totp_provider_unexpected_on_other_builtins(feature, config):
    with pytest.raises(ValueError, match="Invalid config"):
        validate_step(request(feature, {**config, "totp_provider_id": "plugin.vault"}))


@pytest.mark.parametrize("submit", [False, True])
def test_chromium_host_plugin_totp_executes_privately_without_provider_rpc(
    public_dns, monkeypatch, caplog, submit,
):
    rpc = Mock(side_effect=AssertionError("Worker must not resolve a TOTP provider"))
    monkeypatch.setattr(plugin_client, "_channel", rpc)
    monkeypatch.setattr(executor, "_fetch_otp", rpc)

    async def run():
        async with chromium_fixture(CAPTCHA) as (browser, sid, origin, page, _):
            await page.evaluate("code.oninput=()=>reflected.textContent=code.value; void 0")
            config = dict(CONFIG)
            if submit:
                config["submit_selector"] = ["#submit"]
            payload = request("challenge.2fa", config, origin, {"totp_code": CODE})
            response = await browser.execute(sid, payload)
            assert response["status"] == "success", response
            assert response["result"] == ({} if submit else {"submitted": False})
            assert await page.locator("#code").input_value() == CODE
            if submit:
                assert await page.locator("body").get_attribute("data-solved") == CODE
            assert contains_secret(CODE, browser.sessions[sid].secret_fingerprints)
            observed = await browser.observe(sid, TOKEN)
            checked = await browser.check_selector(sid, TOKEN, "#code")
            assert CODE not in json.dumps([response, observed, checked])
            assert CODE not in repr(payload) and CODE not in caplog.text
            assert (await browser.screenshot(sid, TOKEN))["masked"] is True
            assert await browser.execute(sid, payload) == response
            rpc.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("provider,runtime", [
    ("builtin.unknown", {"totp_code": CODE}), ("plugin.vault", None),
])
def test_chromium_invalid_provider_or_missing_code_rejected_before_fill(public_dns, provider, runtime):
    async def run():
        async with chromium_fixture(CAPTCHA) as (browser, sid, origin, page, seen):
            payload = request("challenge.2fa", {**CONFIG, "totp_provider_id": provider}, origin, runtime)
            with pytest.raises(HTTPException) as error:
                await browser.execute(sid, payload)
            assert error.value.status_code == 422
            assert CODE not in str(error.value)
            assert await page.locator("#code").input_value() == ""
            assert not any(row[0] == "POST" for row in seen)
    asyncio.run(run())
