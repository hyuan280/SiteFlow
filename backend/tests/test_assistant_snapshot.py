from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.assistant import AssistantError, AssistantManager, BrowserScope
from app.services.assistant_prompt import SYSTEM_PROMPT, tool_definitions
from app.services.assistant_snapshot import safe_selector_result, safe_snapshot
from app.services.assistant_tools import AssistantTools
from app.services.assistant_urls import trust_required


TARGET = {"base_url": "https://example.com/", "proxy": False}


def node(node_id="n0", **fields):
    return {"node_id": node_id, "parent_id": None, "frame_id": "main", "tag": "div",
            "visible": True, "attributes": {}, **fields}


def snapshot(elements=None, **fields):
    elements = [] if elements is None else elements
    return {"url": TARGET["base_url"], "snapshot_id": str(uuid4()), "frame_id": "main",
            "elements": elements, "offset": 0, "next_offset": None, "total": len(elements),
            "truncated": False, "frames": [{"frame_id": "main", "readable": True,
                                              "url": TARGET["base_url"]}],
            "frames_truncated": False, "limits": {"scan_nodes": 5000, "text_chars": 300,
                "inspect_root_included": True, "inspect_scope": "captured_subtree"}, **fields}


def selector_result(matches=None, **fields):
    matches = [node("m0", visible=False)] if matches is None else matches
    return {"url": TARGET["base_url"], "frame_id": "main", "selector": "#duplicate",
            "match_count": len(matches), "visible_count": 0, "matches": matches,
            "truncated": False, **fields}


def test_generic_hidden_hierarchy_and_exact_verified_proof_survive():
    selector = 'html > body > x-login:nth-of-type(2) >>> input[name=user\\ name]'
    elements = [node(tag="form", attributes={"id": "duplicate"}),
                node("n1", parent_id="n0", tag="x-login", visible=False, has_shadow_root=True),
                node("n2", parent_id="n1", tag="input", visible=False,
                     attributes={"id": "duplicate", "type": "hidden", "name": "user name"},
                     text="FIELD_SECRET", selector=selector, selector_matches=1,
                     selector_source="dom_path")]
    result = safe_snapshot(snapshot(elements), TARGET)
    assert result["ok"] and len(result["elements"]) == 3
    assert "selector" not in result["elements"][0]
    assert result["elements"][1] == elements[1]
    assert result["elements"][2] == {k: v for k, v in elements[2].items() if k != "text"}
    assert result["elements"][2]["selector"] == selector
    assert "FIELD_SECRET" not in json.dumps(result)


@pytest.mark.parametrize("proof", [{}, {"selector_matches": True, "selector_source": "id"},
                                  {"selector_matches": 2, "selector_source": "name"},
                                  {"selector_matches": 1, "selector_source": "guessed"}])
def test_unproven_selector_is_not_forwarded_or_rebuilt(proof):
    result = safe_snapshot(snapshot([node(attributes={"id": "unique"}, selector="#unique", **proof)]), TARGET)
    assert result["ok"] and "selector" not in result["elements"][0]


@pytest.mark.parametrize("tag", ["input", "textarea", "select", "option", "script", "style", "template", "noscript"])
def test_sensitive_text_and_attributes_are_omitted_not_the_node(tag):
    attrs = {key: "PRIVATE" for key in (
        "value", "defaultValue", "data-value", "onclick", "ONLOAD", "style", "srcdoc", "srcset",
        "imagesrcset", "ping", "selected", "checked", "data-api-key", "data-access-token",
        "data-csrf", "data-pass_word", "data-storage", "data-session")}
    attrs.update(id="script-id", src="https://cdn.example/script.js?token=PRIVATE&tab=one")
    result = safe_snapshot(snapshot([node(tag=tag, visible=False, text="PRIVATE", attributes=attrs)]), TARGET)
    item = result["elements"][0]
    assert item["tag"] == tag and item["visible"] is False and "text" not in item
    assert item["attributes"] == {"id": "script-id", "src": "https://cdn.example/script.js?token=[REDACTED]&tab=one"}
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("projector", ["snapshot", "selector"])
@pytest.mark.parametrize("semantic_key,name", [
    ("name", "csrf-token"), ("property", "api_key"), ("NAME", "ACCESS_TOKEN"),
    ("PROPERTY", "site:credential"), ("name", "session-id"), ("property", "pass_word"),
])
def test_meta_sensitive_content_is_dropped_but_skeleton_survives(projector, semantic_key, name):
    item = node("n0" if projector == "snapshot" else "m0", tag="META", visible=False,
                attributes={"CONTENT": "PRIVATE", semantic_key: name, "id": "meta-marker"})
    result = (safe_snapshot(snapshot([item]), TARGET) if projector == "snapshot"
              else safe_selector_result(selector_result([item]), TARGET))
    assert result["ok"]
    projected = result["elements" if projector == "snapshot" else "matches"][0]
    assert projected == {**item, "attributes": {semantic_key: name, "id": "meta-marker"}}
    assert "PRIVATE" not in json.dumps(result)


def test_public_meta_content_and_non_meta_content_are_retained():
    items = [node(tag="meta", attributes={"name": "description", "content": "Public description"}),
             node("n1", tag="meta", attributes={"property": "og:title", "content": "Public title"}),
             node("n2", tag="custom-widget", attributes={"name": "csrf-token", "content": "Public hint"})]
    result = safe_snapshot(snapshot(items), TARGET)
    assert result["ok"] and result["elements"] == items


@pytest.mark.parametrize("projector", ["snapshot", "selector"])
@pytest.mark.parametrize("url,expected", [
    ("https://cdn.example/document?token=PRIVATE&tab=one#details",
     "https://cdn.example/document?token=[REDACTED]&tab=one#details"),
    ("https://cdn.example/?next=https%3a%2f%2fsso.example%2f%3Ftoken%3DPRIVATE%26tab%3Done",
     "https://cdn.example/?next=https%3a%2f%2fsso.example%2f%3Ftoken%3D%5BREDACTED%5D%26tab%3Done"),
    ("data:text/html,<script>PRIVATE</script>", None),
    ("javascript:PRIVATE", None), ("//user:PRIVATE@cdn.example/doc", None),
    ("https://user:PRIVATE@cdn.example/doc", None),
])
def test_object_data_is_url_projected_in_all_node_results(projector, url, expected):
    item = node("n0" if projector == "snapshot" else "m0", tag="OBJECT", visible=False,
                attributes={"id": "object-marker", "DATA": url})
    result = (safe_snapshot(snapshot([item]), TARGET) if projector == "snapshot"
              else safe_selector_result(selector_result([item]), TARGET))
    assert result["ok"]
    projected = result["elements" if projector == "snapshot" else "matches"][0]
    assert projected["attributes"] == {"id": "object-marker", **({"DATA": expected} if expected else {})}
    assert projected["visible"] is False and "PRIVATE" not in json.dumps(result)


def test_generic_data_attribute_is_not_treated_as_object_url():
    item = node(tag="custom-widget", attributes={"data": "public status"})
    assert safe_snapshot(snapshot([item]), TARGET)["elements"] == [item]


def test_bounds_and_recursive_url_projection_preserve_public_bytes():
    nested = "https://cdn.example/?next=https%3a%2f%2fsso.example%2f%3Ftoken%3DPRIVATE%26tab%3Done&tab=two#section"
    attrs = {"data-url": nested, "class": "x" * 900, "href": "javascript:PRIVATE"}
    attrs.update({f"data-safe-{index}": "ok" for index in range(40)})
    raw = snapshot([node(attributes=attrs, text="a" * 500)], url="https://example.com/?state=PRIVATE&tab=foo")
    raw["limits"].update(arbitrary="PRIVATE", scan_nodes=True)
    result = safe_snapshot(raw, TARGET)
    item = result["elements"][0]
    assert result["url"] == "https://example.com/?state=[REDACTED]&tab=foo"
    assert item["attributes"]["data-url"] == nested.replace("PRIVATE", "%5BREDACTED%5D")
    assert len(item["attributes"]) == 24 and len(item["attributes"]["class"]) == 512
    assert len(item["text"]) == 300 and "href" not in item["attributes"]
    assert "scan_nodes" not in result["limits"] and "arbitrary" not in result["limits"]
    assert "PRIVATE" not in json.dumps(result)


def test_pagination_keeps_out_of_page_parent_and_captured_metadata():
    raw = snapshot([node("n101", parent_id="n99")], offset=100, total=5000,
                   next_offset=101, truncated=True)
    result = safe_snapshot(raw, TARGET)
    assert result["ok"] and result["elements"][0]["parent_id"] == "n99"
    for field in ("snapshot_id", "frame_id", "offset", "total", "next_offset", "truncated", "limits"):
        assert result[field] == raw[field]


@pytest.mark.parametrize("code", [
    "browser_url_denied", "browser_dns_denied", "browser_redirect_denied",
    "browser_trust_required", "browser_credential_endpoint_not_approved",
])
def test_blocked_request_warnings_are_bounded_static_metadata(code):
    warning = {"code": code, "source": "blocked_request", "message": "PRIVATE", "url": "PRIVATE"}
    result = safe_snapshot(snapshot(warnings=[warning] * 10), TARGET)
    assert result["ok"]
    assert result["warnings"] == [{"code": code, "source": "blocked_request"}] * 4
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("warnings", [
    None, {}, "PRIVATE", [], [None, "PRIVATE", [], True],
    [{"code": "browser_url_denied", "source": "PRIVATE"}],
    [{"code": "browser_url_denied"}],
    [{"code": "browser_post_endpoint_denied", "source": "blocked_request"}],
    [{"code": "execution_timeout", "source": "blocked_request"}],
    [{"code": ["browser_url_denied"], "source": "blocked_request"}],
    [{"code": "browser_url_denied: PRIVATE", "source": "blocked_request"}],
])
def test_unreviewed_or_empty_warnings_are_omitted(warnings):
    result = safe_snapshot(snapshot(warnings=warnings), TARGET)
    assert result["ok"] and "warnings" not in result
    assert "PRIVATE" not in json.dumps(result)


def test_warning_entries_are_validated_independently_within_the_bound():
    warning = {"code": "browser_url_denied", "source": "blocked_request"}
    result = safe_snapshot(snapshot(warnings=[None, warning, {"code": "PRIVATE"}, warning,
                                               {"code": "PRIVATE", "source": "blocked_request"}]), TARGET)
    assert result["ok"] and result["warnings"] == [warning, warning]


def test_trust_warning_rebuilds_details_without_granting_authority():
    source = "https://example.com/login?token=PRIVATE&tab=one#form"
    destination = "https://sso.example/login?state=PRIVATE&redirect=%2F#signin"
    warning = {
        "code": "browser_trust_required", "source": "blocked_request",
        "source_url": source, "target_url": destination,
        "source_url_redacted": False, "target_url_redacted": False,
        "source_origin": "PRIVATE", "target_origin": "PRIVATE",
        "redacted_parameters": ["PRIVATE"], "trusted_url_pattern": "PRIVATE",
        "notice": "PRIVATE", "user_action": "PRIVATE", "message": "PRIVATE", "ok": False,
    }
    raw = snapshot(warnings=[warning])
    raw["frames"].append({"frame_id": "sso", "url": destination, "readable": True})
    result = safe_snapshot(raw, TARGET)
    expected = {key: value for key, value in trust_required(source, destination).items() if key != "ok"}
    assert result["ok"] and result["warnings"] == [{**expected, "source": "blocked_request"}]
    assert result["warnings"][0]["source_url_redacted"] is True
    assert result["warnings"][0]["target_url_redacted"] is True
    assert result["warnings"][0]["redacted_parameters"] == ["token", "state"]
    assert result["warnings"][0]["target_origin"] == "https://sso.example"
    assert result["frames"][1]["readable"] is False
    assert "PRIVATE" not in json.dumps(result)
    assert TARGET == {"base_url": "https://example.com/", "proxy": False}
    raw["url"] = destination
    assert safe_snapshot(raw, TARGET) == {"ok": False, "code": "browser_unavailable"}


@pytest.mark.parametrize("url", [
    "[redacted]", "[REDACTED]", "http://127.0.0.1/PRIVATE", "javascript:PRIVATE",
    "https://user:PRIVATE@sso.example/", "https://sso.example/" + "x" * 4096,
    None, [], {},
])
@pytest.mark.parametrize("field", ["source_url", "target_url"])
def test_invalid_or_fully_redacted_trust_warning_keeps_only_generic_warning(url, field):
    warning = {"code": "browser_trust_required", "source": "blocked_request",
               "source_url": TARGET["base_url"], "target_url": "https://sso.example/login",
               "source_origin": "PRIVATE", "target_origin": "PRIVATE",
               "redacted_parameters": ["PRIVATE"], field: url}
    result = safe_snapshot(snapshot(warnings=[warning]), TARGET)
    assert result["ok"] and result["warnings"] == [{
        "code": "browser_trust_required", "source": "blocked_request",
    }]
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("fields,expected", [
    ({"details_truncated": True, "reason": "browser_warning_details_limit"},
     {"details_truncated": True, "reason": "browser_warning_details_limit"}),
    ({"details_truncated": True, "reason": "PRIVATE"}, {"details_truncated": True}),
    ({"details_truncated": 1, "reason": "browser_warning_details_limit"}, {}),
    ({"details_truncated": "PRIVATE", "reason": "PRIVATE"}, {}),
    ({"reason": "browser_warning_details_limit"}, {}),
])
def test_warning_truncation_metadata_is_static_and_strict(fields, expected):
    warning = {"code": "browser_trust_required", "source": "blocked_request"}
    result = safe_snapshot(snapshot(warnings=[{**warning, **fields}]), TARGET)
    assert result["ok"] and result["warnings"] == [{**warning, **expected}]
    assert "PRIVATE" not in json.dumps(result)


def test_non_trust_warning_cannot_smuggle_trust_details():
    warning = {"code": "browser_url_denied", "source": "blocked_request"}
    result = safe_snapshot(snapshot(warnings=[{
        **warning, "source_url": TARGET["base_url"], "target_url": "https://sso.example/",
        "target_origin": "PRIVATE", "redacted_parameters": ["PRIVATE"],
    }]), TARGET)
    assert result["ok"] and result["warnings"] == [warning]


def test_trust_warning_details_count_toward_complete_snapshot_budget():
    elements = [node(f"n{index}", attributes={"class": "x" * 512}, text="x" * 300)
                for index in range(100)]
    raw = snapshot(elements)
    result = safe_snapshot(raw, TARGET)
    assert result["ok"]
    size = len(json.dumps(result, ensure_ascii=True).encode())
    # Public URL attributes are retained up to 4096 bytes, unlike generic attributes.
    for index in range(8):
        elements[index]["attributes"]["href"] = "https://example.com/" + "x" * 3980
    result = safe_snapshot(raw, TARGET)
    assert result["ok"] and len(json.dumps(result, ensure_ascii=True).encode()) > size
    warning = {"code": "browser_trust_required", "source": "blocked_request",
               "source_url": "https://example.com/" + "x" * 3980,
               "target_url": "https://sso.example/" + "x" * 3980}
    raw["warnings"] = [warning] * 4
    assert safe_snapshot(raw, TARGET) == {"ok": False, "code": "browser_unavailable"}


@pytest.mark.parametrize("fields", [
    {"url": "https://other.example/"}, {"url": "http://127.0.0.1/"},
    {"frames": []}, {"snapshot_id": "invalid"},
])
def test_warning_does_not_bypass_page_or_snapshot_validation(fields):
    warning = {"code": "browser_url_denied", "source": "blocked_request"}
    assert safe_snapshot(snapshot(warnings=[warning], **fields), TARGET) == {
        "ok": False, "code": "browser_unavailable",
    }


def test_url_attributes_can_exceed_generic_attribute_limit_without_truncation():
    url = "https://cdn.example/script.js?path=" + "a" * 2000 + "&token=PRIVATE#section"
    raw = snapshot([node(tag="script", attributes={"id": "cf-script", "src": url,
                                                  "data-cfasync": "false"}, text="PRIVATE")])
    result = safe_snapshot(raw, TARGET)
    assert result["elements"][0]["attributes"] == {
        "id": "cf-script", "src": url.replace("PRIVATE", "[REDACTED]"), "data-cfasync": "false"}
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("fields", [
    {"offset": True}, {"offset": -1}, {"offset": 5001}, {"total": 5001}, {"total": True},
    {"next_offset": True}, {"next_offset": 0}, {"next_offset": 2}, {"next_offset": -1},
    {"total": 2}, {"snapshot_id": "not-uuid"}, {"truncated": "false"}, {"frames": []},
    {"frame_id": "wrong"}, {"elements": [node()] * 101},
])
def test_invalid_snapshot_metadata_fails_closed(fields):
    raw = snapshot([node()])
    raw.update(fields)
    assert safe_snapshot(raw, TARGET) == {"ok": False, "code": "browser_unavailable"}


def test_frames_are_projected_independently_and_never_expand_authority():
    raw = snapshot()
    raw["frames"] += [{"frame_id": "other", "parent_frame_id": "main", "readable": True,
                       "url": "https://sso.example/frame?token=PRIVATE&tab=one", "cdp": "PRIVATE"},
                      {"frame_id": "private", "readable": True, "url": "http://127.0.0.1/PRIVATE"}]
    result = safe_snapshot(raw, TARGET)
    assert result["frames"][1]["url"] == "https://sso.example/frame?token=[REDACTED]&tab=one"
    assert not result["frames"][1]["readable"]
    assert result["frames"][1]["reason"] == "browser_trust_required"
    assert result["frames"][2]["reason"] == "browser_url_denied"
    assert "PRIVATE" not in json.dumps(result)
    trusted = {**TARGET, "trusted_origins": ["https://sso.example"]}
    assert safe_snapshot(raw, trusted)["frames"][1]["readable"] is True
    raw["frames"] += [{"frame_id": f"f{i}", "readable": False, "url": TARGET["base_url"]} for i in range(80)]
    result = safe_snapshot(raw, TARGET)
    assert len(result["frames"]) == 64 and result["frames_truncated"] is True
    raw.update(frame_id="other", elements=[node(frame_id="other")], total=1)
    assert not safe_snapshot(raw, TARGET)["ok"]


@pytest.mark.parametrize("reason", ["browser_frame_unreadable", "browser_dns_denied"])
def test_unreadable_frame_metadata_preserves_safe_reason_without_dom_authority(reason):
    raw = snapshot()
    raw["frames"].append({"frame_id": "child", "readable": False, "reason": reason,
                          "url": "https://example.com/child?token=PRIVATE"})
    result = safe_snapshot(raw, TARGET)
    assert result["ok"]
    assert result["frames"][1]["reason"] == reason
    assert result["frames"][1]["readable"] is False and "PRIVATE" not in json.dumps(result)


def test_selector_result_preserves_hidden_samples_and_bounded_counts():
    result = safe_selector_result(selector_result(match_count=20), TARGET)
    assert result["ok"] and result["match_count"] == 20 and result["visible_count"] == 0
    assert result["matches"][0]["visible"] is False and result["truncated"] is True
    for changes in ({"match_count": True}, {"visible_count": 2}, {"match_count": 10001},
                    {"selector": "x" * 2049}, {"matches": [node("m0")] * 11}):
        assert not safe_selector_result(selector_result(**changes), TARGET)["ok"]


def test_projection_enforces_complete_json_result_budget():
    attrs = {f"data-field-{index}": "x" * 512 for index in range(24)}
    elements = [node(f"n{index}", attributes=attrs) for index in range(100)]
    assert not safe_snapshot(snapshot(elements), TARGET)["ok"]
    matches = [node(f"m{index}", attributes=attrs, text="x" * 300,
                    selector="x" * 2048, selector_matches=1, selector_source="dom_path")
               for index in range(10)]
    assert not safe_selector_result(selector_result(matches), TARGET)["ok"]


@pytest.mark.parametrize("name,args", [
    ("browser_observe", {"offset": True}), ("browser_observe", {"offset": "1"}),
    ("browser_observe", {"offset": -1}), ("browser_observe", {"limit": 101}),
    ("browser_observe", {"limit": False}), ("browser_observe", {"frame_id": None}),
    ("browser_observe", {"frame_id": "x" * 257}), ("browser_observe", {"token": "bad"}),
    ("browser_inspect", {"node_id": "n0"}),
    ("browser_inspect", {"snapshot_id": "bad", "node_id": "n0"}),
    ("browser_inspect", {"snapshot_id": str(uuid4()), "node_id": 1}),
    ("browser_inspect", {"snapshot_id": str(uuid4()), "node_id": "n0", "frame_id": "main"}),
    ("browser_check_selector", {}), ("browser_check_selector", {"selector": " "}),
    ("browser_check_selector", {"selector": "x" * 2049}),
    ("browser_check_selector", {"selector": "#safe", "offset": 0}),
    ("browser_unknown", {}),
])
def test_strict_browser_arguments_do_not_reach_worker(name, args):
    manager = SimpleNamespace(browser_tool=AsyncMock())
    tools = AssistantTools(manager, "owner", "session", TARGET)
    assert asyncio.run(tools.dispatch(name, args)) == {"ok": False, "code": "invalid_arguments"}
    manager.browser_tool.assert_not_awaited()


@pytest.mark.parametrize("name,operation,args", [
    ("browser_observe", "observe", {"offset": 0, "limit": 1, "frame_id": "main"}),
    ("browser_inspect", "inspect", {"snapshot_id": str(uuid4()), "node_id": "n0", "limit": 1}),
    ("browser_check_selector", "check-selector", {"selector": "#duplicate", "frame_id": "main"}),
])
def test_dispatch_routes_payload_with_identity_and_safe_projection(name, operation, args):
    manager = AssistantManager(None, None, None)
    scope = BrowserScope("owner", str(uuid4()), "HOST_TOKEN", "interaction", "", time.monotonic() + 900)
    manager.browsers["session"] = scope
    raw = selector_result() if operation == "check-selector" else snapshot([node()])
    if operation == "inspect":
        raw["snapshot_id"] = args["snapshot_id"]
    raw.update(session_id=scope.session_id, lane_key=scope.lane_key, token=scope.token, cdp="PRIVATE")
    manager._worker = AsyncMock(return_value=raw)
    tools = AssistantTools(manager, "owner", "session", TARGET)
    result = asyncio.run(tools.dispatch(name, args))
    assert result["ok"]
    manager._worker.assert_awaited_once_with(f"{scope.session_id}/{operation}", {**args, "token": scope.token})
    assert manager.browsers["session"] is scope
    assert "HOST_TOKEN" not in json.dumps(result) and "PRIVATE" not in json.dumps(result)
    raw["session_id"] = str(uuid4())
    manager._close_browser = AsyncMock()
    assert not asyncio.run(tools.dispatch(name, args))["ok"]
    manager._close_browser.assert_awaited_once_with("session")


def test_inspect_normalizes_uuid_without_mutating_caller_arguments():
    manager = SimpleNamespace(browser_tool=AsyncMock(return_value={"ok": True}))
    tools = AssistantTools(manager, "owner", "session", TARGET)
    identifier = str(uuid4())
    args = {"snapshot_id": identifier.upper(), "node_id": "n0"}
    assert asyncio.run(tools.dispatch("browser_inspect", args))["ok"]
    assert manager.browser_tool.call_args.args[-1] == {"snapshot_id": identifier, "node_id": "n0"}
    assert args["snapshot_id"] == identifier.upper()


@pytest.mark.parametrize("name,args,raw", [
    ("browser_observe", {"offset": 1}, snapshot(offset=0)),
    ("browser_observe", {"frame_id": "different"}, snapshot()),
    ("browser_observe", {"limit": 1}, snapshot([node(), node("n1")])),
    ("browser_inspect", {"snapshot_id": str(uuid4()), "node_id": "n0"}, snapshot()),
    ("browser_check_selector", {"selector": "#expected"}, selector_result()),
])
def test_response_request_binding_mismatches_fail_closed(name, args, raw):
    manager = AssistantManager(None, None, None)
    scope = BrowserScope("owner", str(uuid4()), "HOST_TOKEN", "interaction", "", time.monotonic() + 900)
    manager.browsers["session"] = scope
    raw.update(session_id=scope.session_id, lane_key=scope.lane_key)
    manager._worker = AsyncMock(return_value=raw)
    manager._close_browser = AsyncMock()
    result = asyncio.run(manager.browser_tool("owner", "session", TARGET, name, None, args))
    assert result["code"] == "browser_unavailable"
    manager._close_browser.assert_awaited_once_with("session")


@pytest.mark.parametrize("name,args", [
    ("browser_observe", {"frame_id": "child"}),
    ("browser_inspect", {"snapshot_id": str(uuid4()), "node_id": "n0"}),
    ("browser_check_selector", {"selector": "#login", "frame_id": "child"}),
])
@pytest.mark.parametrize("code", ["browser_snapshot_stale", "browser_selector_invalid",
                                  "browser_frame_unavailable", "browser_frame_unreadable"])
def test_semantic_worker_errors_leave_browser_open(code, name, args):
    manager = AssistantManager(None, None, None)
    scope = BrowserScope("owner", str(uuid4()), "HOST_TOKEN", "interaction", "", time.monotonic() + 900)
    manager.browsers["session"] = scope
    manager._worker = AsyncMock(side_effect=AssistantError("browser_unavailable", 502, reason=code))
    manager._close_browser = AsyncMock()
    tools = AssistantTools(manager, "owner", "session", TARGET)
    result = asyncio.run(tools.dispatch(name, args))
    assert result["code"] == code and result["reason"] == code
    assert "page remains open" in result["notice"]
    assert manager.browsers["session"] is scope
    manager._close_browser.assert_not_awaited()


def test_top_level_dns_denial_retains_browser_without_evidence():
    manager = AssistantManager(None, None, None)
    scope = BrowserScope("owner", str(uuid4()), "HOST_TOKEN", "interaction", "", time.monotonic() + 900)
    manager.browsers["session"] = scope
    manager._worker = AsyncMock(side_effect=AssistantError("browser_unavailable", 502, reason="browser_dns_denied"))
    manager._close_browser = AsyncMock()
    tools = AssistantTools(manager, "owner", "session", TARGET)
    result = asyncio.run(tools.dispatch("browser_observe", {}))
    assert result["reason"] == "browser_dns_denied"
    assert result["page_retained"] is True and result["ok"] is False
    assert manager.browsers["session"] is scope
    manager._close_browser.assert_not_awaited()


def test_tool_schemas_and_prompt_explain_capture_limits():
    definitions = {item["function"]["name"]: item["function"] for item in tool_definitions()}
    for name in ("browser_observe", "browser_inspect", "browser_check_selector"):
        assert definitions[name]["parameters"]["additionalProperties"] is False
        assert "token" not in definitions[name]["parameters"]["properties"]
    assert set(definitions["browser_inspect"]["parameters"]["required"]) == {"snapshot_id", "node_id", "title"}
    for phrase in ("visible=false", "parent_id", "captured_subtree", "next_offset", "not a subtree-targeted", "screenshots"):
        assert phrase in SYSTEM_PROMPT
