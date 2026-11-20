"""Turn-scoped tools. Mutation and network authority remain with the host."""

from __future__ import annotations

from urllib.parse import unquote, urlsplit, urlunsplit
import time
from uuid import UUID, uuid4

from sqlalchemy import select

from app.features.catalog import FEATURE_LIST
from app.models.workflow import Site
from app.schemas.assistant import public_url
from app.schemas.config_ai import GenerationSite
from app.services.assistant_snapshot import safe_snapshot as safe_snapshot
from app.services.assistant_urls import (
    URL_NOTICE, approved_url, contains_redaction, project_url, resolve_url, trust_required,
)
from app.services.config_ai_tools import ConfigAITools
from app.services.workflows import WorkflowService
from app.services.assistant_lanes import saved_lane


class _AssistantWorkflowService(WorkflowService):
    def __init__(self, *args, target, **kwargs):
        super().__init__(*args, **kwargs)
        self.target = target

    def validate_draft(self, owner_type, payload, owner_id=None):
        from app.services.assistant_workflows import workflow_candidate, validate_workflow_urls, WorkflowDenied

        candidate, owner_id = workflow_candidate(self, self.target, {
            field: getattr(payload, field) for field in ("steps", "groups", "signals")})
        result = super().validate_draft(owner_type, candidate, owner_id, replace_local=True)
        try:
            validate_workflow_urls(result["resolved"], self.target)
        except WorkflowDenied as exc:
            self.denial_code = str(exc)
            raise
        return result


ALLOWED_FEATURES = [
    feature.key for feature in FEATURE_LIST
    if feature.generation_hints is not None
    and not feature.key.startswith("plugin.")
]


def allowed_features(db):
    from app.services.config_ai_catalog import ConfigAICatalog

    return list(ConfigAICatalog(db, include_plugins=True)._features())


def failure(code: str) -> dict:
    return {"ok": False, "code": code}


class AssistantTools:
    def __init__(self, manager, owner: str, session_id: str, target: dict | None, *, config=None,
                 turn=None):
        self.manager = manager
        self.owner = owner
        self.session_id = session_id
        self.target = target
        self.config = config or {}
        self.consulted: set[str] = set()
        self.turn = turn or manager._active_turns.get(session_id) or {
            "id": str(uuid4()), "deadline": time.monotonic() +
            self.config.get("_assistant_settings", {}).get("turn_timeout_seconds", 180),
        }
        self.call_context = {}
        self.interaction_blocked = False

    async def dispatch(self, name: str, arguments: dict) -> dict:
        from app.services.assistant_interactions import ensure_permission, request_interaction
        from app.services.assistant_takeover import takeover_tool_allowed
        from app.services.assistant import AssistantError

        try:
            decision = takeover_tool_allowed(self, name, arguments)
        except AssistantError as exc:
            return failure("tool_result_unavailable" if name == "get_tool_result" else exc.code)
        if decision is not True:
            return decision
        if self.interaction_blocked:
            return {**failure("prerequisite_missing"), "terminal": True, "blocking": True}
        if name == "list_secret_providers":
            if (not isinstance(arguments, dict) or set(arguments) - {"capability"}
                    or arguments.get("capability") not in {None, "secret.credentials", "secret.totp"}):
                return failure("invalid_arguments")
            from app.services.assistant_provider_policy import authorized_providers

            capabilities = ([arguments["capability"]] if arguments.get("capability") else
                            ["secret.credentials", "secret.totp"])
            providers = [
                {"id": "builtin.manual", "name": "Saved site credentials", "capability": "secret.credentials"},
                {"id": "builtin.bitwarden", "name": "Bitwarden", "capability": "secret.credentials"},
                {"id": "builtin.twofauth", "name": "2FAuth", "capability": "secret.totp"},
            ]
            with self.manager.session_factory() as db:
                self.manager._owned(db, self.owner, self.session_id)
                for capability in capabilities:
                    providers.extend({key: item[key] for key in ("id", "name", "capability")}
                                     for item in authorized_providers(db, capability)
                                     if item["id"].startswith("plugin."))
            return {"ok": True, "providers": [item for item in providers if item["capability"] in capabilities]}
        if name == "clear_bound_site_cookies":
            from app.services.assistant_cookies import clear_bound_site_cookies

            return await clear_bound_site_cookies(self, arguments)
        if name == "submit_takeover_conclusion":
            from app.services.assistant_takeover_conclusions import submit_conclusion

            return await submit_conclusion(self, arguments)
        from app.services import assistant_operations, assistant_scheduler

        if name in {item["function"]["name"] for item in assistant_scheduler.tool_definitions()}:
            return await assistant_scheduler.dispatch(self, name, arguments)
        if name in {item["function"]["name"] for item in assistant_operations.tool_definitions()}:
            return await assistant_operations.dispatch(self, name, arguments)
        if name == "update_site_config":
            from app.services.assistant_site_config import request_site_config

            return await request_site_config(self, arguments)
        if name == "create_template_from_site":
            from app.services.assistant_templates import dispatch

            return await dispatch(self, name, arguments)
        if name == "request_user_interaction":
            if not isinstance(arguments, dict) or set(arguments) != {"request"}:
                return failure("invalid_arguments")
            if isinstance(arguments["request"], dict) and arguments["request"].get("purpose") in {
                "site_config", "consumption", "template_import", "scheduler_config", "run_cancel", "cookie_clear",
            }:
                return failure("invalid_arguments")
            result = await request_interaction(self, arguments["request"])
            request = arguments["request"]
            if (isinstance(request, dict) and request.get("purpose") == "question"
                    and request.get("required", True) is True and result.get("terminal")
                    and not result.get("ok")):
                result = {**result, "blocking": True}
            if result.get("blocking"):
                self.interaction_blocked = True
            return result
        if name in {"import_workflow_draft", "run_site", "execute_step", "browser_screenshot"}:
            capability = {"execute_step": "execution", "browser_screenshot": "screenshot"}.get(name, "workflow")
            feature = None
            if name == "execute_step":
                if (not isinstance(arguments, dict) or set(arguments) != {"operation_id", "step"}
                        or not isinstance(arguments.get("step"), dict)):
                    return failure("invalid_arguments")
                feature = arguments["step"].get("feature_type")
                from app.services.assistant_categories import execution_features

                with self.manager.session_factory() as db:
                    if not isinstance(feature, str) or feature not in execution_features(db):
                        return failure("execution_feature_denied")
            elif name == "import_workflow_draft":
                if (not isinstance(arguments, dict) or set(arguments) != {"proposal_message_id"}
                        or type(arguments["proposal_message_id"]) is not int
                        or arguments["proposal_message_id"] < 1):
                    return failure("invalid_arguments")
            elif not isinstance(arguments, dict) or arguments:
                return failure("invalid_arguments")
            if name == "browser_screenshot" and self.config.get("supports_vision") is not True:
                return failure("model_vision_required")
            # Validate before loading authority. Other tools use the host-created
            # target, refreshed by interactions/imports and the conversation loop.
            with self.manager.session_factory() as db:
                self.target = self.manager._decrypt(
                    self.manager._owned(db, self.owner, self.session_id)).get("target")
            permission = await ensure_permission(self, capability, feature)
            if not permission.get("ok"):
                return permission
        if name in {"import_workflow_draft", "run_site", "get_run_results", "get_run_log"}:
            from app.services.assistant_workflows import AssistantWorkflows

            workflows = AssistantWorkflows(
                self.manager, self.owner, self.session_id, self.target, turn=self.turn,
                auth_limit=self.config.get("_assistant_settings", {}).get("max_auth_uses_per_turn"))
            if name == "run_site":
                from app.services.assistant_consumption import run_with_consumption_approval

                return await run_with_consumption_approval(self, workflows)
            result = await workflows.dispatch(name, arguments)
            self.target = workflows.target
            return result
        if name == "retry_step":
            from app.services.assistant_retry import run_with_retry_approval

            if not isinstance(arguments, dict) or set(arguments) != {"operation_id"}:
                return failure("invalid_arguments")
            return await run_with_retry_approval(
                self.manager, self.owner, self.session_id, arguments["operation_id"],
                turn=self.turn, invocation={"turn_id": self.turn["id"], **self.call_context},
                auth_limit=self.config.get("_assistant_settings", {}).get("max_auth_uses_per_turn"))
        if name == "get_tool_result":
            if (not isinstance(arguments, dict) or set(arguments) - {"message_id", "offset", "limit"}
                    or type(arguments.get("message_id")) is not int or arguments["message_id"] < 1
                    or type(arguments.get("offset", 0)) is not int or arguments.get("offset", 0) < 0
                    or type(arguments.get("limit", 10000)) is not int
                    or not 1 <= arguments.get("limit", 10000) <= 10000):
                return failure("invalid_arguments")
            try:
                return self.manager.get_tool_result(self.owner, self.session_id, **arguments)
            except Exception:
                return failure("tool_result_unavailable")
        if name == "execute_step":
            from app.services.assistant_execution import AssistantExecution

            return await AssistantExecution(
                self.manager, self.owner, self.session_id, self.target,
                turn=self.turn, invocation={"turn_id": self.turn["id"], **self.call_context},
                auth_limit=self.config.get("_assistant_settings", {}).get("max_auth_uses_per_turn"),
            ).execute(arguments)
        from app.services.assistant_data import AssistantData

        if name in {item["function"]["name"] for item in AssistantData.tool_definitions()}:
            return await AssistantData(self.manager).dispatch(name, arguments)
        if name.startswith("browser_"):
            if not self.target:
                return failure("target_confirmation_required")
            if not isinstance(arguments, dict):
                return failure("invalid_arguments")
            valid = True
            if name == "browser_navigate":
                valid = valid and set(arguments) in ({"path"}, {"url"})
            elif name in {"browser_observe", "browser_inspect", "browser_check_selector"}:
                allowed = {"selector", "frame_id"} if name == "browser_check_selector" else {"offset", "limit"}
                required = set()
                if name == "browser_observe":
                    allowed.add("frame_id")
                elif name == "browser_inspect":
                    required = {"snapshot_id", "node_id"}
                    allowed.update(required)
                else:
                    required = {"selector"}
                valid = valid and required <= set(arguments) <= allowed
                for field, minimum, maximum in (("offset", 0, 5000), ("limit", 1, 100)):
                    if field in arguments:
                        valid = valid and type(arguments[field]) is int and minimum <= arguments[field] <= maximum
                for field, maximum in (("frame_id", 256), ("node_id", 64), ("selector", 2048)):
                    if field in arguments:
                        value = arguments[field]
                        valid = valid and isinstance(value, str) and 1 <= len(value) <= maximum and bool(value.strip())
                if "snapshot_id" in arguments:
                    try:
                        value = arguments["snapshot_id"]
                        valid = valid and isinstance(value, str) and UUID(value).version == 4
                        if valid:
                            arguments = {**arguments, "snapshot_id": str(UUID(value))}
                    except (ValueError, TypeError, AttributeError):
                        valid = False
            else:
                valid = valid and name in {"browser_open", "browser_refresh", "browser_screenshot"} and not arguments
            if not valid:
                return failure("invalid_arguments")
            if name == "browser_screenshot":
                if self.target.get("confirmed") is not True:
                    return failure("target_confirmation_required")
                if self.target.get("screenshot_consent") is not True:
                    return failure("screenshot_consent_required")
                if self.config.get("supports_vision") is not True:
                    return failure("model_vision_required")
            url = None
            if name == "browser_navigate":
                try:
                    value = arguments.get("path", arguments.get("url"))
                    maximum = 2048 if "path" in arguments else 4096
                    if (not isinstance(value, str) or not value or len(value) > maximum
                            or contains_redaction(value)):
                        raise ValueError
                    if "path" in arguments:
                        if (urlsplit(value).scheme or urlsplit(value).netloc
                                or unquote(value).startswith("//")):
                            raise ValueError
                        value = resolve_url(self.target["base_url"], value)
                    url = public_url(value)
                    if not approved_url(url, self.target):
                        return {**trust_required(self.target["base_url"], url),
                                "lane_key": "proxy_interaction" if self.target.get("proxy") else "interaction"}
                    if project_url(url)["url_redacted"]:
                        raise ValueError
                except ValueError:
                    return failure("invalid_navigation")
            if name in {"browser_observe", "browser_inspect", "browser_check_selector"}:
                return await self.manager.browser_tool(self.owner, self.session_id, self.target, name, url, arguments)
            return await self.manager.browser_tool(self.owner, self.session_id, self.target, name, url)

        if name == "check_site":
            if set(arguments) != {"base_url"} or not isinstance(arguments["base_url"], str):
                return failure("invalid_arguments")
            try:
                if len(arguments["base_url"]) > 512:
                    raise ValueError
                requested = public_url(arguments["base_url"])
            except ValueError:
                return failure("invalid_url")
            hostname = urlsplit(requested).hostname
            items = []
            with self.manager.session_factory() as db:
                # Select only the permitted columns, never credential/settings rows.
                for name_, base in db.execute(select(Site.name, Site.base_url)):
                    try:
                        parsed = urlsplit(base)
                        safe = public_url(urlunsplit((
                            parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", "",
                        )))
                    except ValueError:
                        continue
                    if urlsplit(safe).hostname == hostname:
                        items.append({"name": name_[:128], "base_url": safe})
                        if len(items) == 20:
                            break
            return {"ok": True, "exists": bool(items), "items": items}

        if name in {"validate_workflow_draft", "submit_workflow_draft"} and not self.target:
            return failure("target_confirmation_required")
        if name in {"validate_workflow_draft", "submit_workflow_draft"} and (
                contains_redaction(arguments.get("draft"))
                or contains_redaction(self.target["base_url"])
                or project_url(self.target["base_url"])["url_redacted"]):
            return {**failure("redacted_url_not_usable"), "notice": URL_NOTICE}
        if (name in {"validate_workflow_draft", "submit_workflow_draft"}
                and self.target.get("credential_mode") in {"none", "cookie"}
                and isinstance(arguments.get("draft"), dict)):
            steps = arguments["draft"].get("steps", [])
            if isinstance(steps, list) and any(
                isinstance(step, dict) and isinstance(step.get("feature_type"), str)
                and step["feature_type"].startswith("auth.") for step in steps
            ):
                return failure("credential_mode_incompatible")
        with self.manager.session_factory() as db:
            service = _AssistantWorkflowService(db, self.manager.settings, self.manager.crypto,
                                                target=self.target or {})
            if (name in {"validate_workflow_draft", "submit_workflow_draft"}
                    and self.target.get("workflow_site_id") is not None):
                from app.services.assistant_workflows import workflow_candidate, WorkflowDenied

                try:
                    workflow_candidate(service, self.target, {"steps": [], "groups": [], "signals": []})
                except WorkflowDenied as exc:
                    return failure(str(exc))
            site = self.target or {"name": "unconfirmed", "base_url": "https://example.com/"}
            tools = ConfigAITools(service, GenerationSite(
                name=site["name"], base_url=site["base_url"],
            ), allowed_features(db), include_plugins=True)
            tools._consulted = self.consulted
            result = tools.dispatch(name, arguments)
            if getattr(service, "denial_code", None):
                return failure(service.denial_code)
            if tools.proposal is not None:
                # Local proposal only, never the service's effective/resolved preview.
                result = {**result, "proposal": tools.proposal}
            if self.target and name in {"validate_workflow_draft", "submit_workflow_draft"} and result.get("ok"):
                provider_id = "builtin.bitwarden" if self.target["credential_mode"] == "bitwarden" else "builtin.manual"
                if self.target["credential_mode"] == "site":
                    selected = db.execute(select(Site.credential_provider_id, Site.bitwarden_enabled).where(
                        Site.id == self.target.get("site_id"))).first()
                    provider_id = (selected.credential_provider_id or (
                        "builtin.bitwarden" if selected.bitwarden_enabled else "builtin.manual")) if selected else None
                result["required_site_settings"] = {
                    "credential_mode": self.target["credential_mode"],
                    "credential_provider_id": provider_id,
                    "lane_policy": saved_lane(self.target),
                    "trusted_origins": self.target.get("trusted_origins", []),
                    "notice": "Import applies host-confirmed settings only with workflow_consent; manual saving requires separate review. Credentials and Cookie remain outside the proposal. trusted_origins is observation only and does not authorize credential trusted_urls; credential trust requires separate user review.",
                }
            return result
