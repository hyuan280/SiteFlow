from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker import executor, secret_client, vision  # noqa: E402


FINGERPRINTS = "_result_secret_fingerprints"


def fingerprint(value):
    return len(value), hashlib.sha256(value.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("use_vision", [False, True])
@pytest.mark.parametrize("code", ["1234", "123456", "1234567890"])
def test_execute_job_redacts_consumed_credentials(
    monkeypatch, tmp_path, caplog, capsys, use_vision, code,
):
    caplog.set_level("DEBUG")
    monkeypatch.setattr(executor, "ARTIFACT_ROOT", tmp_path)
    username = "private-user"
    password = "private-login  \tpassword"
    verify_password = "private\x1b[31mverify-password\x1b[0m"
    values = (username, password, verify_password, code)
    resolved = {
        "login": {"username": username, "password": password},
        "verify": {"password": verify_password},
        "otp": {"code": code},
    }
    monkeypatch.setenv("SITEFLOW_SECRET_API_URL", "https://secrets.invalid/resolve")
    locators = {
        selector: SimpleNamespace(
            fill=AsyncMock(), input_value=AsyncMock(return_value=value), click=AsyncMock(),
        )
        for selector, value in (
            ("#username", username), ("#password", password),
            ("#verify-password", verify_password), ("#code", code),
            ("#submit", ""), ("#success", ""),
        )
    }
    monkeypatch.setattr(executor, "_find_locator", AsyncMock(
        side_effect=lambda page, selectors, **kwargs: locators[selectors[0]],
    ))
    page = SimpleNamespace(
        url="https://site.invalid/", on=Mock(), remove_listener=Mock(),
        bring_to_front=AsyncMock(), close=AsyncMock(),
        evaluate=AsyncMock(return_value="Signed in. Checked in. 2026 9876543210"),
        screenshot=AsyncMock(return_value=b"dummy-screenshot"),
    )
    lane = SimpleNamespace(
        execution_semaphore=asyncio.Semaphore(1), task_pages={}, page=None,
        context=SimpleNamespace(new_page=AsyncMock(return_value=page),
                                cookies=AsyncMock(return_value=[])),
    )
    manager = SimpleNamespace(
        get=Mock(return_value=lane),
        lease_lane=AsyncMock(return_value=("safe-lease", {"browser": "fake"})),
        release_lane=AsyncMock(),
    )
    raw_reason = (
        f"http://a Logged in as {username}; accepted {password}; verified {verify_password}; "
        f"entered {code}; visible 2026 9876543210."
    )
    text = json.dumps({"result": True, "reason": raw_reason})
    message = {"content": text}
    requests = []
    original_client = httpx.AsyncClient

    def handle(request):
        requests.append(request)
        if request.url.host == "secrets.invalid":
            body = json.loads(request.content)
            assert set(body) == {"job_id", "site_id", "step_key", "purpose", "provider"} | (
                {"lease"} if "lease" in body else set()
            )
            if "lease" not in body:
                return httpx.Response(200, json={"lease": "one-use-lease"})
            return httpx.Response(200, json=resolved[body["step_key"]])
        assert request.url.host == "provider.invalid"
        return httpx.Response(200, json={"choices": [{
            "message": message,
            "finish_reason": "stop",
        }]})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handle), **kwargs,
    ))
    model = {
        "base_url": "https://provider.invalid/v1", "model": "result-model",
        "api_key": "provider-key",
    }
    payload = {
        "job_id": 1, "run_id": 1, "site_id": 2, "base_url": page.url,
        "vision": model,
        "feature_execution_settings": {"flow.result": {
            "llm_enabled": True, "use_vision": use_vision,
        }},
        "steps": [
            {"step_key": "login", "feature_type": "auth.form_login", "config": {
                "username_selector": ["#username"], "password_selector": ["#password"],
                "submit_selector": ["#submit"], "success_selector": ["#success"],
            }},
            {"step_key": "verify", "feature_type": "auth.password_verify", "config": {
                "password_selector": ["#verify-password"], "submit_selector": ["#submit"],
                "success_selector": ["#success"],
            }},
            {"step_key": "otp", "feature_type": "challenge.2fa", "config": {
                "code_selector": ["#code"], "submit_selector": ["#submit"],
                "success_selector": ["#success"],
            }},
            {"step_key": "result", "feature_type": "flow.result", "config": {
                "output_value": True, "llm_enabled": True,
            }},
        ],
    }
    progress = AsyncMock()
    result = asyncio.run(executor.execute_job(manager, payload, on_step=progress))
    assert result["status"] == "success"
    assert [row["status"] for row in result["steps"]] == ["success"] * 4
    assert len(requests) == 7
    assert [json.loads(request.content)["step_key"] for request in requests[:-1]] == [
        "login", "login", "verify", "verify", "otp", "otp",
    ]
    for selector, value in zip(
        ("#username", "#password", "#verify-password", "#code"), values,
    ):
        locators[selector].fill.assert_awaited_once_with(value, timeout=executor.PAGE_TIMEOUT_MS)
    assert type(payload[FINGERPRINTS]) is set
    assert payload[FINGERPRINTS] == {fingerprint(value) for value in values}
    assert FINGERPRINTS not in model
    assert "credentials" not in payload
    assert all(value not in repr(payload) for value in values)
    reason = result["steps"][-1]["data"]["judgment_reason"]
    assert reason == (
        "[REDACTED] Logged in as [REDACTED]; accepted [REDACTED]; "
        "verified [REDACTED]; entered [REDACTED]; visible 2026 9876543210."
    )
    assert vision.sanitize_judgment_reason(reason, {FINGERPRINTS: payload[FINGERPRINTS]}) == reason
    request_text = requests[-1].content.decode()
    assert set(json.loads(request_text)) == {
        "model", "temperature", "messages",
    }
    log = (tmp_path / "run-1" / "job-1" / "job.log").read_text()
    assert reason in log
    output = json.dumps(result) + json.dumps([call.args[0] for call in progress.await_args_list])
    output += log + caplog.text + repr(capsys.readouterr())
    all_requests = "".join(request.content.decode() for request in requests)
    for secret in (*values, "private-login", "private-verify-password", FINGERPRINTS,
                   *(digest for _, digest in payload[FINGERPRINTS])):
        assert secret not in output
        assert secret not in all_requests
    page.close.assert_awaited_once()
    manager.release_lane.assert_awaited_once_with("general", "safe-lease")


@pytest.mark.parametrize("reason,values,expected", [
    ("Logged in as private-user", ("private-user",), "Logged in as [REDACTED]"),
    ("http://a " + "s" * 490, ("s" * 490,), "[REDACTED] [REDACTED]"),
    ("accepted private  \tpassword", ("private  \tpassword",), "accepted [REDACTED]"),
    ("accepted private\x1b[31mpassword\x1b[0m", ("private\x1b[31mpassword\x1b[0m",),
     "accepted [REDACTED]"),
    ("abcdefgh abcdefgh", ("abcde", "defgh"), "[REDACTED] [REDACTED]"),
    ("2026 1234 9876543210 1234", ("1234",), "2026 [REDACTED] 9876543210 [REDACTED]"),
    ("s" * 500, ("s" * 500,), "[REDACTED]"),
])
def test_fingerprint_redaction_precedes_transforms_and_merges_raw_intervals(reason, values, expected):
    config = {FINGERPRINTS: {fingerprint(value) for value in values}}
    assert vision.sanitize_judgment_reason(reason, config) == expected
    assert vision.sanitize_judgment_reason(expected, config) == expected


def test_fingerprint_and_plaintext_matches_share_original_offsets():
    config = {FINGERPRINTS: {fingerprint("defgh"), fingerprint("private-user")}}
    assert vision.sanitize_judgment_reason(
        "abcdefgh Logged in as private-user", config, ("abcde", "private"),
    ) == "[REDACTED] Logged in as [REDACTED]"


@pytest.mark.parametrize("username", ["u" * 500, "", None])
def test_records_only_resolved_fields_and_bounded_raw_values(monkeypatch, username):
    monkeypatch.setattr(secret_client, "resolve_secret", AsyncMock(return_value={
        "username": username, "password": "p" * 501, "code": "123456",
        "unused": "not-consumed",
    }))
    monkeypatch.setattr(executor, "_find_locator", AsyncMock(return_value=None))
    payload = {"base_url": "https://site.invalid/"}
    result = asyncio.run(executor._run_step(
        SimpleNamespace(url=payload["base_url"]),
        {"feature_type": "auth.form_login", "config": {}},
        None, None, None, None, None, "general", secret_payload=payload,
    ))
    assert result[0] == "fail"
    if username:
        assert payload[FINGERPRINTS] == {fingerprint(username)}
        assert username not in repr(payload) + repr(result)
    else:
        assert FINGERPRINTS not in payload
