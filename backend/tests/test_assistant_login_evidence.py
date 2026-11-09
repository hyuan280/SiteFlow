from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.test_assistant_browser import Manager, TOKEN, URL, public_dns  # noqa: F401
from tests.test_assistant_execution import execution  # noqa: F401
from tests.test_assistant_step import request
from worker import executor
from worker.app import create_app
from worker.assistant_browser import AssistantBrowser, _success_evidence_result


PRIVATE = "private-password-selector-token"


def evidence():
    return {
        "selector_mode": "visible", "frame_scope": "main_and_visible_same_origin",
        "snapshot_consistency": "sampled_after_timeout", "captured_after_timeout": True,
        "waited_ms": 1002, "elapsed_ms": 12, "configured_count": 1,
        "evidence_truncated": False, "visible_after_timeout": False, "reason": "matches_hidden",
        "selectors": [{
            "selector_index": 0, "match_count": 2, "visible_count": 0, "checked_count": 2,
            "truncated": False, "read_errors": 0, "frames": [{
                "frame_index": 0, "scope": "main", "match_count": 2, "visible_count": 0,
                "checked_count": 2, "truncated": False,
            }],
        }],
    }


def test_projection_copies_complete_contract_without_mutating_source():
    source = evidence()
    projected = _success_evidence_result(source)
    assert projected == source
    projected["selectors"][0]["frames"][0]["match_count"] = 0
    assert source == evidence()


@pytest.mark.parametrize("level", ["root", "selector", "frame"])
@pytest.mark.parametrize("key", ["selector", "url", "body", "password", "provider_message"])
def test_unknown_fields_at_every_level_are_rejected(level, key):
    source = evidence()
    node = source if level == "root" else source["selectors"][0]
    if level == "frame":
        node = node["frames"][0]
    node[key] = PRIVATE
    assert _success_evidence_result(source) is None
    assert node[key] == PRIVATE


@pytest.mark.parametrize("path,value", [
    (("selector_mode",), PRIVATE), (("captured_after_timeout",), 1),
    (("snapshot_consistency",), PRIVATE), (("reason",), PRIVATE),
    (("waited_ms",), True), (("elapsed_ms",), float("nan")),
    (("elapsed_ms",), float("inf")), (("elapsed_ms",), -1),
    (("configured_count",), "1"), (("evidence_truncated",), 0),
    (("visible_after_timeout",), PRIVATE), (("selectors",), {}),
    (("selectors", 0, "selector_index"), True),
    (("selectors", 0, "match_count"), PRIVATE),
    (("selectors", 0, "checked_count"), 101),
    (("selectors", 0, "read_errors"), 11),
    (("selectors", 0, "frames", 0, "frame_index"), 10),
    (("selectors", 0, "frames", 0, "scope"), PRIVATE),
    (("selectors", 0, "frames", 0, "error"), PRIVATE),
    (("selectors", 0, "frames", 0, "visible_count"), float("nan")),
])
def test_malformed_primitives_are_omitted(path, value):
    source = evidence()
    node = source
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    assert _success_evidence_result(source) is None


@pytest.mark.parametrize("level", ["root", "selector", "frame"])
def test_missing_required_fields_are_rejected(level):
    source = evidence()
    node, key = (source, "waited_ms") if level == "root" else (source["selectors"][0], "checked_count")
    if level == "frame":
        node = node["frames"][0]
    del node[key]
    assert _success_evidence_result(source) is None


def test_array_and_byte_bounds():
    source = evidence()
    selector = source["selectors"][0]
    source["selectors"] = [copy.deepcopy(selector) for _ in range(11)]
    assert _success_evidence_result(source) is None
    source = evidence()
    frame = source["selectors"][0]["frames"][0]
    source["selectors"][0]["frames"] = [copy.deepcopy(frame) for _ in range(11)]
    assert _success_evidence_result(source) is None
    frame.update(scope="unreadable_frame", match_count=None, visible_count=None,
                 checked_count=0, truncated=True, error="frame_not_eligible")
    selector["frames"] = [copy.deepcopy(frame) for _ in range(10)]
    source["selectors"] = [copy.deepcopy(selector) for _ in range(10)]
    assert len(json.dumps(source).encode()) > 16000
    assert _success_evidence_result(source) is None


@pytest.mark.parametrize("scope", ["cross_origin", "hidden_frame", "unreadable_frame"])
def test_static_excluded_frame_metadata_is_preserved(scope):
    source = evidence()
    frame = source["selectors"][0]["frames"][0]
    frame.update(scope=scope, match_count=None, visible_count=None, checked_count=0,
                 error="frame_not_eligible")
    assert _success_evidence_result(source) == source


@pytest.mark.parametrize("action_started", [False, True])
@pytest.mark.parametrize("malformed", [False, True])
@pytest.mark.parametrize("late_visible", [False, True])
def test_worker_api_preserves_safe_evidence_without_outcome_change(public_dns, monkeypatch, action_started, malformed, late_visible):
    async def run():
        manager = Manager()
        browser = AssistantBrowser(manager)
        session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
        source = evidence()
        if late_visible:
            source.update(reason="visible_after_timeout", visible_after_timeout=True)
            source["selectors"][0]["visible_count"] = 1
            source["selectors"][0]["frames"][0]["visible_count"] = 1
        expected = copy.deepcopy(source)
        if malformed:
            source["selectors"][0]["frames"][0]["url"] = PRIVATE
        original = copy.deepcopy(source)

        async def fake_run_step(*_args, **kwargs):
            kwargs["secret_payload"]["_assistant_secret_context"].action_started = action_started
            kwargs["log"]("WARN", PRIVATE)
            return "fail", PRIVATE, {"submitted": True, "verified": False,
                                      "success_evidence": source, "url": PRIVATE, "raw": PRIVATE}

        step = AsyncMock(side_effect=fake_run_step)
        monkeypatch.setattr(executor, "_run_step", step)
        payload = request("auth.form_login", {
            "username_selector": ["#u"], "password_selector": ["#p"],
            "submit_selector": ["#s"], "success_selector": [PRIVATE],
        }, credentials={"username": PRIVATE, "password": PRIVATE})
        app = create_app()
        app.state.assistant_browser = browser
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
                first = await client.post(f"/assistant-browser/{session_id}/execute", json=payload.model_dump(mode="json"))
                second = await client.post(f"/assistant-browser/{session_id}/execute", json=payload.model_dump(mode="json"))
            assert first.status_code == second.status_code == 200
            result = first.json()
            assert second.json() == result
            assert result["status"] == ("unknown" if action_started else "failed")
            assert result["code"] == ("outcome_uncertain" if action_started else "step_failed")
            assert result["result"] == {"submitted": True, "verified": False,
                                        **({} if malformed else {"success_evidence": expected})}
            assert result["logs"] == ["Step execution started", "Step execution finished"]
            assert result["before"]["url"] == result["after"]["url"] == URL
            assert PRIVATE not in json.dumps(result)
            assert source == original
            step.assert_awaited_once()
        finally:
            await browser.stop()
    asyncio.run(run())


def test_non_login_result_does_not_admit_login_evidence(public_dns, monkeypatch):
    async def run():
        browser = AssistantBrowser(Manager())
        try:
            session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
            monkeypatch.setattr(executor, "_run_step", AsyncMock(return_value=(
                "fail", PRIVATE, {"count": 3, "success_evidence": evidence()},
            )))
            result = await browser.execute(session_id, request())
            assert result["result"] == {"count": 3}
            assert result["status"] == "failed"
            assert PRIVATE not in json.dumps(result)
        finally:
            await browser.stop()
    asyncio.run(run())


def test_real_chromium_hidden_snapshot_survives_assistant_execution(public_dns, monkeypatch, execution):
    async def run():
        from playwright.async_api import async_playwright

        candidates = list(Path("/opt/opencode/.cache/ms-playwright").glob(
            "chromium_headless_shell-*/**/chrome-headless-shell"))
        candidates += list(Path("/opt/opencode/.cache/ms-playwright").glob(
            "chromium_headless_shell-*/**/headless_shell"))
        async with async_playwright() as playwright:
            try:
                chromium = await playwright.chromium.launch(
                    executable_path=str(candidates[0]) if candidates else None, headless=True,
                )
            except Exception:
                pytest.skip("Local Chromium executable unavailable")
            browser = AssistantBrowser(Manager())
            try:
                page = await chromium.new_page()
                await page.set_content('<span class="account" hidden>Hidden account template</span>')
                captured = await executor._login_success_evidence(page, [".account"], waited_ms=1000)
                assert captured["reason"] == "matches_hidden"

                async def fake_run_step(*_args, **kwargs):
                    kwargs["secret_payload"]["_assistant_secret_context"].action_started = True
                    return "fail", "Login evidence missing", {
                        "submitted": True, "verified": False, "success_evidence": captured,
                    }

                monkeypatch.setattr(executor, "_run_step", fake_run_step)
                session_id = (await browser.open(URL, "interaction", TOKEN))["session_id"]
                payload = request("auth.form_login", {
                    "username_selector": ["#u"], "password_selector": ["#p"],
                    "submit_selector": ["#s"], "success_selector": [".account"],
                }, credentials={"username": PRIVATE, "password": PRIVATE})
                result = await browser.execute(session_id, payload)
                assert result["status"] == "unknown"
                assert result["result"]["success_evidence"] == captured
                assert result["result"]["verified"] is False
                assert PRIVATE not in json.dumps(result)
                projected = execution._project(result, SimpleNamespace(
                    session_id=session_id, lane_key="interaction",
                ), str(payload.operation_id))
                expected = {key: value for key, value in captured.items() if key != "evidence_truncated"}
                expected["truncated"] = captured["evidence_truncated"]
                assert projected["result"]["success_evidence"] == expected
                assert projected["status"] == "unknown" and projected["result"]["verified"] is False
                assert PRIVATE not in json.dumps(projected)
            finally:
                await browser.stop()
                await chromium.close()
    asyncio.run(run())
