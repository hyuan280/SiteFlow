"""Builtin authoring availability is separate from execution authority."""

from copy import deepcopy
import asyncio
import json
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest

from app.features.catalog import FEATURE_LIST
from app.models.setting import AppSetting
from app.schemas.config_ai import GenerateSiteDraftRequest, GenerationSite
from app.schemas.assistant import CreateSession
from app.services.assistant_prompt import SYSTEM_PROMPT, tool_definitions
from app.services.assistant_tools import ALLOWED_FEATURES, AssistantTools
from app.services.config_ai_protocol import generation_guide
from app.services.config_ai_tools import ConfigAITools
from app.services.workflows import WorkflowService
from tests.test_buy_card_config import DEFAULTS as BUY_CARD_CONFIG
from tests.test_resign_loop_config import DEFAULTS as RESIGN_LOOP_CONFIG, OLD_DEFAULTS
from tests.test_resign_dialog_config import DEFAULTS as RESIGN_DIALOG_CONFIG
from tests.test_workflow_drafts import _read_only


BUILTINS = [feature.key for feature in FEATURE_LIST]
SITE = {"name": "Example", "base_url": "https://example.com"}
POST_CONFIGS = {
    "post.buy_card": {key: value for key, value in BUY_CARD_CONFIG.items()
                      if key not in {"option_selector", "exchange_action", "submit_value"}},
    "post.resign": OLD_DEFAULTS,
    "post.resign_loop": RESIGN_LOOP_CONFIG,
    "post.resign_dialog": RESIGN_DIALOG_CONFIG,
}


@pytest.fixture
def tools(client):
    with client.app.state.session_factory() as session:
        yield ConfigAITools(WorkflowService(session), GenerationSite(**SITE), BUILTINS)


def test_assistant_discovers_all_builtins_without_target_or_execution(client):
    assert set(ALLOWED_FEATURES) == set(BUILTINS)
    manager = client.app.state.assistant_manager
    sid = manager.create("owner", CreateSession())["id"]
    assistant = AssistantTools(manager, "owner", sid, None)
    with _read_only(client):
        listing = asyncio.run(assistant.dispatch("list_features", {}))
        assert {item["feature_type"] for item in listing["features"]} == set(BUILTINS)
        for key in BUILTINS:
            result = asyncio.run(assistant.dispatch("get_feature_schema", {"feature_type": key}))
            assert result["ok"], result
        for key in ("plugin.private.wait", "sign.iframe_form", "auth.totp_login", "unknown"):
            result = asyncio.run(assistant.dispatch("get_feature_schema", {"feature_type": key}))
            assert not result["ok"]
            assert result["diagnostics"][0]["code"] == "feature_not_allowed"


def test_all_23_builtins_are_discoverable_with_valid_safe_schemas(tools, client):
    assert len(BUILTINS) == 23
    request = GenerateSiteDraftRequest(site=SITE, requirements="Propose only", consent_to_send=True,
                                       allowed_features=BUILTINS)
    assert request.allowed_features == BUILTINS
    assert GenerateSiteDraftRequest.model_json_schema()["properties"]["allowed_features"]["maxItems"] == 23
    with _read_only(client):
        listing = tools.dispatch("list_features", {})
        assert listing["ok"]
        assert {item["feature_type"] for item in listing["features"]} == set(BUILTINS)
        assert all(item["source"] == "builtin" for item in listing["features"])
        posts = tools.dispatch("list_features", {"category": "post"})
        assert {item["feature_type"] for item in posts["features"]} == set(POST_CONFIGS)
        for key in BUILTINS:
            result = tools.dispatch("get_feature_schema", {"feature_type": key})
            assert result["ok"], result
            assert result["generation_hints"]["when_to_use"]
            for name in ("config_schema", "override_config_schema"):
                schema = result[name]
                Draft202012Validator.check_schema(schema)
                assert schema["additionalProperties"] is False
                assert not {"llm_prompt", "llm_enabled", "pending_dates", "resign_pending_dates"} & schema["properties"].keys()
                assert '"default":' not in json.dumps(schema)
    for definition in tools.tool_definitions():
        schema = definition["function"]["parameters"]
        Draft202012Validator.check_schema(schema)
        if "StepInput" in schema.get("$defs", {}):
            assert schema["$defs"]["StepInput"]["properties"]["feature_type"]["enum"] == BUILTINS


@pytest.mark.parametrize("key", BUILTINS)
def test_every_builtin_can_validate_and_submit_a_local_proposal(tools, client, key):
    config = deepcopy(POST_CONFIGS.get(key, {}))
    config.update({
        "action.click": {"selector": ["#observed-button"]},
        "action.fill": {"selector": ["#public-search"], "value": "public search text"},
        "challenge.2fa": {"service_hint": "Example", "totp_provider_id": "builtin.twofauth"},
        "flow.jump": {"texts": ["Observed retry marker"], "action": "restart", "max_jumps": 1},
        "flow.result": {"output_value": False, "message": "Observed failure"},
    }.get(key, {}))
    draft = {"steps": [{"step_key": "observed", "feature_type": key, "config": config}],
             "groups": [], "signals": []}
    with _read_only(client):
        assert tools.dispatch("get_feature_schema", {"feature_type": key})["ok"]
        result = tools.dispatch("validate_workflow_draft", {"draft": draft})
        assert result["ok"], result
        assert tools.dispatch("submit_workflow_draft", {
            "draft": draft, "explanation": "Review observed configuration; do not execute.",
            "unresolved_questions": [],
        }) == {"ok": True, "submitted": True}
    assert tools.proposal["draft"]["steps"][0]["config"] == config
    assert tools.proposal["status"] == "proposal" and tools.proposal["requires_review"] is True


@pytest.mark.parametrize("key", POST_CONFIGS)
@pytest.mark.parametrize("field", ["pending_dates", "resign_pending_dates", "approval", "api_key"])
def test_post_runtime_authority_is_not_a_model_config_field(tools, key, field):
    assert tools.dispatch("get_feature_schema", {"feature_type": key})["ok"]
    draft = {"steps": [{"step_key": "post", "feature_type": key, "config": {field: None}}],
             "groups": [], "signals": []}
    result = tools.dispatch("validate_workflow_draft", {"draft": draft})
    assert not result["ok"] and result["diagnostics"][0]["code"] == "extra_fields"


def test_secret_fill_defaults_are_not_exported_or_materialized(tools, client):
    secret = "PRIVATE-FILL-DEFAULT-NEVER-EXPORT"
    with client.app.state.session_factory() as writer:
        writer.add(AppSetting(key="feature.action.fill", value_json=json.dumps({
            "defaults": {"value": secret}, "required_fields": ["selector"],
        })))
        writer.commit()
    with _read_only(client):
        schema = tools.dispatch("get_feature_schema", {"feature_type": "action.fill"})
        assert "value" in schema["config_schema"]["properties"]
        draft = {"steps": [{"step_key": "fill", "feature_type": "action.fill",
                            "config": {"selector": ["#public-field"]}}], "groups": [], "signals": []}
        result = tools.dispatch("submit_workflow_draft", {
            "draft": draft, "explanation": "Review only", "unresolved_questions": [],
        })
    assert result["ok"], result
    assert secret not in json.dumps([schema, result, tools.proposal])
    assert "value" not in tools.proposal["draft"]["steps"][0]["config"]


def test_prompt_distinguishes_authoring_captcha_tests_and_consumption_approval():
    prompt = " ".join(SYSTEM_PROMPT.split())
    assert "All 23 reviewed built-ins" in SYSTEM_PROMPT
    assert "Exactly 18 built-ins" in SYSTEM_PROMPT
    assert "flow.jump and the four post features" in SYSTEM_PROMPT
    assert "flow.result returns its configured boolean" in SYSTEM_PROMPT
    assert "CAPTCHA handling does not lock the browser" in prompt
    assert "follow-up execute_step calls on the same page without reopening" in prompt
    assert "Form input values remain omitted from all DOM/selector evidence" in prompt
    assert "fresh dialog for EACH enabled post step" in prompt
    assert "All step dialogs must be approved before the full workflow starts" in prompt
    assert "There is no popup for each date or internal click" in prompt
    assert "Legacy post.resign has full support" in SYSTEM_PROMPT
    assert "not a claim that every physical click's business effects can be detected or blocked" in prompt
    assert "password, cookie, token or OTP substitutes" in SYSTEM_PROMPT
    guide = generation_guide()
    assert "All 23 reviewed built-in features" in guide["guide"]
    assert "never supplied as model parameters" in guide["guide"]
    assert "post.* and action.fill are unavailable" not in json.dumps(guide)
    definitions = {item["function"]["name"]: item["function"] for item in tool_definitions()}
    assert "18" in definitions["execute_step"]["description"]
    assert "fresh per-post-step approval" in definitions["run_site"]["description"]
    assert set(definitions["run_site"]["parameters"]["properties"]) == {"title"}


def test_captcha_followup_policy_preserves_credential_privacy_and_masking_limits():
    definitions = {item["function"]["name"]: item["function"] for item in tool_definitions()}
    guide = generation_guide()["guide"]
    for policy in (SYSTEM_PROMPT, guide):
        policy = " ".join(policy.split())
        assert "Image CAPTCHA OCR text and solver answers are not" in policy
        assert "OTP/2FA codes" in policy
        assert "provider keys" in policy
        assert "readable same-origin frames" in policy
        assert "other frames" in policy
        assert "every secret pixel" in policy
        assert "summary-only" not in policy
        assert "until browser_open reopens it" not in policy
    execute = definitions["execute_step"]["description"]
    assert "follow-up observation, selector checks, consented screenshots" in execute
    assert "authorized single steps without reopening; input values remain omitted" in execute
    screenshot = definitions["browser_screenshot"]["description"]
    assert "readable same-origin frames" in screenshot
    assert "fully mask other frames" in screenshot
    assert "not complete secret redaction" in screenshot
    assert "No form values" in definitions["browser_observe"]["description"]


def test_category_policy_prompt_and_protected_context_preserve_scope_and_provenance():
    from app.services.assistant import AssistantManager
    from app.services.assistant_context import _NO_RETRY, _SUMMARY_PROMPT

    facts = [{"operation_id": "original", "status": "unknown"}]
    note = AssistantManager._context_safety_note(facts)["content"]
    for policy in (SYSTEM_PROMPT, note, _NO_RETRY):
        policy = " ".join(policy.split())
        for text in ("Feature.category", "max_auth_uses_per_turn", "default 3", "1-20",
                     "failures", "challenge.2fa", "OTP", "without a human dialog",
                     "internal click", "autonomous takeover"):
            assert text.lower() in policy.lower(), text
        assert "per-post-step" in policy or "per-step" in policy
    assert json.dumps(facts, ensure_ascii=False, separators=(",", ":")) in note
    assert "provenance, not current" in _SUMMARY_PROMPT
    assert "Analysis cannot save or import configuration" in SYSTEM_PROMPT
    assert "Plugins may declare the same six categories" in SYSTEM_PROMPT
    assert "direct purchase or dated consumption clicks are denied" not in SYSTEM_PROMPT
    definitions = {item["function"]["name"]: item["function"] for item in tool_definitions()}
    retry = definitions["retry_step"]["description"]
    assert "exactly the stored" in retry and "without a human dialog" in retry
    assert "Autonomous takeover denies this tool" in retry
    assert set(definitions["retry_step"]["parameters"]["properties"]) == {"title", "operation_id"}


@pytest.mark.parametrize("document", [
    "README.md", "docs/ai-assistant.md", "docs/assistant-discovery.md",
    "docs/usage.md", "docs/config-ai.md", "docs/run-notifications.md",
])
def test_category_policy_documentation_semantics(document):
    text = " ".join((Path(__file__).resolve().parents[2] / document).read_text().split())
    for term in ("Feature.category", "max_auth_uses_per_turn", "challenge.2fa", "retry_step"):
        assert term in text, (document, term)
    if document in {"docs/config-ai.md", "docs/run-notifications.md"}:
        assert "failures" in text and "before credential resolution" in text
        assert ("internal click" in text or "internal-click" in text) and "all" in text.lower()
        assert "metadata is unverified" in text
    else:
        assert "失败" in text and "解析凭据前预留" in text
        assert "内部点击" in text and "全部批准后" in text
        assert "元数据未验证" in text
    for obsolete in ("必须宿主批准", "未知完整运行及主动单步", "不执行变更单步或导入",
                     "不明单步仍需正常重试审批", "未知结果仍禁止自主重复",
                     "真实提交及未知结果均不能自动重试", "需先人工核验并走宿主重试批准"):
        assert obsolete not in text, (document, obsolete)
