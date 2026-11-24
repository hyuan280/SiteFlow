"""Code-owned authority and tool definitions for the conversational assistant."""

from pathlib import Path

from app.services.config_ai_protocol import tool_definitions as config_definitions
from app.schemas.workflow import StepInput
from app.services.config_ai_protocol import _strip_schema_annotations


_ORDINARY_MODULES = (
    "common", "consent", "browser", "evidence", "steps", "workflow",
    "post", "settings", "scheduler", "context", "takeover",
)
_RESOURCE_DIR = Path(__file__).with_name("assistant_prompts")


def build_system_prompt(*, takeover=False, tool_names=None) -> str:
    """Read code-selected resources; tool availability never grants authority."""
    if not takeover:
        return "\n\n".join(
            (_RESOURCE_DIR / f"{name}.md").read_text(encoding="utf-8").strip()
            for name in _ORDINARY_MODULES
        ) + "\n"

    names = frozenset(tool_names or ())
    sections = [(_RESOURCE_DIR / "common.md").read_text(encoding="utf-8").strip()]
    # Block labels are tool names, never filesystem paths supplied by a caller.
    resource = (_RESOURCE_DIR / "takeover_context.md").read_text(encoding="utf-8")
    for block in resource.split("<!-- capability:"):
        if not block.strip():
            continue
        capability, text = block.split(" -->", 1)
        if capability == "always" or capability in names:
            sections.append(text.strip())
    return "\n\n".join(sections) + "\n"


SYSTEM_PROMPT = build_system_prompt()


def tool_definitions() -> list[dict]:
    definitions = config_definitions()
    from app.services.assistant_data import AssistantData

    definitions.extend(AssistantData.tool_definitions())
    definitions.append({"type": "function", "function": {
        "name": "list_secret_providers",
        "description": "Discover builtin and currently assistant-authorized plugin credential/TOTP providers. Public metadata only; no secrets, integration settings or authorization grants.",
        "parameters": {"type": "object", "properties": {
            "capability": {"type": "string", "enum": ["secret.credentials", "secret.totp"]},
        }, "additionalProperties": False},
    }})
    from app.services import assistant_operations, assistant_scheduler, assistant_takeover_conclusions

    module_definitions = assistant_scheduler.tool_definitions() + assistant_operations.tool_definitions()
    module_definitions += assistant_takeover_conclusions.tool_definitions()
    for definition in module_definitions:
        _strip_schema_annotations(definition["function"]["parameters"])
    definitions.extend(module_definitions)
    from pydantic import TypeAdapter
    from app.schemas.assistant import PublicUserInteraction
    from app.schemas.assistant_site_config import UpdateSiteConfig

    interaction_schema = TypeAdapter(PublicUserInteraction).json_schema()
    _strip_schema_annotations(interaction_schema)
    # The outer title is tool activity metadata. The nested question title is public UI text.
    definitions.append({"type": "function", "function": {
        "name": "request_user_interaction",
        "description": "Wait for typed host target, authorization or question interaction in this call. Settings changes use update_site_config, not legacy trusted_origins/proxy inputs. No state patches or secrets. Denials/timeouts are terminal for this subject this turn.",
        "parameters": {"type": "object", "$defs": interaction_schema.pop("$defs", {}),
                       "properties": {"request": interaction_schema},
                       "required": ["request"], "additionalProperties": False},
    }})
    settings_schema = UpdateSiteConfig.model_json_schema()
    _strip_schema_annotations(settings_schema)
    definitions.append({"type": "function", "function": {
        "name": "update_site_config",
        "description": "Only for an explicit user-requested settings change. Review exact patch/observation changes and wait in this same call for authenticated approval; global defaults never bypass review. No arbitrary site_id, credentials, workflow arrays or implicit enabling. enabled=true alone requires same-session current-config full success.",
        "parameters": settings_schema,
    }})
    from app.services.assistant_templates import TemplateFromSiteRequest

    template_schema = TemplateFromSiteRequest.model_json_schema()
    _strip_schema_annotations(template_schema)
    definitions.append({"type": "function", "function": {
        "name": "create_template_from_site",
        "description": "Only on explicit user request: import the host-bound saved site's local workflow into a new or existing template after exact host review. No source ID argument, successful assistant run requirement, source mutation, settings/credential copy, automatic approval or execution. Replacements require destination revision/history identity, preserve metadata and original effective groups/step order; new groups/steps may be inserted. No implicit merge.",
        "parameters": template_schema,
    }})
    step_schema = StepInput.model_json_schema()
    _strip_schema_annotations(step_schema)
    definitions.append({"type": "function", "function": {
        "name": "execute_step",
        "description": "Test one host-authorized builtin or opted-in plugin feature on the current same-origin page. Post execution requires explicit standing policy and a bound saved site. Host-effective category sets policy; historical unknowns do not block non-post steps. Auth attempts/retries share max_auth_uses_per_turn (default 3, 1-20), reserved before dispatch; failures count. challenge.2fa keeps OTP private, outside this budget. Nonsecret action.fill only; flow.result returns its boolean. Website side effects remain possible. CAPTCHA allows follow-up observation, selector checks, consented screenshots and authorized single steps without reopening; input values remain omitted.",
        "parameters": {"type": "object", "properties": {
            "operation_id": {"type": "string", "format": "uuid"},
            "step": step_schema,
        }, "required": ["operation_id", "step"], "additionalProperties": False},
    }})
    for name, description, properties, required in (
        ("import_workflow_draft", "One successful stored session submission. Replace all three local arrays, no merge; keep host-owned template binding/protected metadata. Explicit host workflow consent/frozen destination revision required.",
         {"proposal_message_id": {"type": "integer", "minimum": 1}}, ["proposal_message_id"]),
        ("run_site", "Run only the host-bound saved workflow with workflow consent. Explicit enabled per-feature post policy is standing authorization; otherwise legacy builtin post steps require bounded approval. Host computes dates and runtime scope. Repeats reuse this proposal/revision's run. No site, force or pending-date arguments.", {}, []),
        ("get_run_results", "Read secret-free structured results only for a full run created by this conversation. Optionally wait up to 20 seconds before returning status.",
         {"run_id": {"type": "integer", "minimum": 1},
          "cursor": {"type": "integer", "minimum": 0},
          "snapshot_id": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
          "limit": {"type": "integer", "minimum": 1, "maximum": 50},
          "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 20}}, ["run_id"]),
        ("get_run_log", "Read a bounded byte page of curated structured diagnostic events for this conversation's run, not raw private artifact messages.",
         {"run_id": {"type": "integer", "minimum": 1},
          "offset": {"type": "integer", "minimum": 0},
          "snapshot_id": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
          "limit": {"type": "integer", "minimum": 1, "maximum": 16000}}, ["run_id"]),
        ("get_tool_result", "Read a character page of an original tool record from this conversation; no browser action or replay.",
         {"message_id": {"type": "integer", "minimum": 1},
          "offset": {"type": "integer", "minimum": 0, "default": 0},
          "limit": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 10000}}, ["message_id"]),
        ("browser_open", "Open the host-confirmed target, automatically starting its selected interaction or proxy_interaction lane.", {}, []),
        ("clear_bound_site_cookies", "Only on explicit user request: fresh human approval clears the bound saved site's cookies and exact-host cookies in the already ready shared browser lane, without reload. No arbitrary site/domain/URL, parent-domain clear or automatic retry of unknown outcomes. Account/password and localStorage retained.",
         {"operation_id": {"type": "string", "format": "uuid", "pattern": "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"}}, ["operation_id"]),
        ("browser_screenshot", "Capture bounded input-masked JPEG evidence, including after CAPTCHA handling; requires separate host screenshot consent and vision support. Mask input controls in the main page and readable same-origin frames; fully mask other frames. Visible page text, reflected CAPTCHA answers and PII may remain; not complete secret redaction.", {}, []),
        ("browser_refresh", "Safe GET navigation of the owned current URL, never repost; preserves uncertain outcomes. Do not refresh automatically from spinner evidence.", {}, []),
         ("retry_step", "Where the current host tool scope permits, automatically repeat exactly the stored uncertain supported non-post step using one-use host authority and a fresh UUID, without a human dialog. Auth retries charge another use of the shared per-user-turn cap, including failures. Preserve original unknown provenance. Autonomous takeover denies this tool. No step edits or post execution.",
         {"operation_id": {"type": "string", "format": "uuid"}}, ["operation_id"]),
        ("browser_observe", "Capture/page generic DOM including hidden nodes; offset=0 refreshes, next_offset continues the selected frame. No form values or screenshots.",
         {"offset": {"type": "integer", "minimum": 0, "maximum": 5000, "default": 0},
          "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100},
          "frame_id": {"type": "string", "minLength": 1, "maxLength": 256}}, []),
        ("browser_inspect", "Page a root-inclusive immutable captured subtree, never live DOM or nodes omitted by collection caps.",
         {"snapshot_id": {"type": "string", "format": "uuid"},
          "node_id": {"type": "string", "minLength": 1, "maxLength": 64},
          "offset": {"type": "integer", "minimum": 0, "maximum": 5000, "default": 0},
          "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100}}, ["snapshot_id", "node_id"]),
        ("browser_check_selector", "Check live CSS match/visible counts and bounded sanitized samples in one frame. Supports explicit >>> open-shadow segments, not arbitrary Playwright engines.",
         {"selector": {"type": "string", "minLength": 1, "maxLength": 2048},
          "frame_id": {"type": "string", "minLength": 1, "maxLength": 256}}, ["selector"]),
        ("browser_navigate", "One path<=2048 or URL<=4096; keep public suffixes. Confirm new origins.",
         {"path": {"type": "string", "minLength": 1, "maxLength": 2048},
          "url": {"type": "string", "minLength": 1, "maxLength": 4096}}, []),
        ("check_site", "Check whether a user-supplied public URL's hostname is already configured; no secrets.",
         {"base_url": {"type": "string", "minLength": 1, "maxLength": 512}}, ["base_url"]),
    ):
        definitions.append({"type": "function", "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": required, "additionalProperties": False},
        }})
        if name == "browser_navigate":
            definitions[-1]["function"]["parameters"]["oneOf"] = [
                {"required": ["path"], "not": {"required": ["url"]}},
                {"required": ["url"], "not": {"required": ["path"]}},
            ]
    for definition in definitions:
        schema = definition["function"]["parameters"]
        schema["properties"]["title"] = {"type": "string", "minLength": 1, "maxLength": 120}
        schema.setdefault("required", []).append("title")
    return definitions
