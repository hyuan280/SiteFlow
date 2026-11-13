"""Worker-side defense in depth for host-reviewed, single builtin steps."""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import unquote_plus
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Feature = Literal[
    "flow.open", "action.navigate", "auth.form_login", "auth.password_verify",
    "action.wait", "sign.button", "sign.unit3d_claim", "challenge.altcha",
    "challenge.slider", "action.click",
    "challenge.2fa", "challenge.turnstile", "challenge.image_captcha",
    "challenge.grid_captcha", "sign.iframe.image_captcha", "sign.image_quiz",
    "action.fill", "flow.result",
]
FEATURES = frozenset(Feature.__args__)

# Trusted builtin metadata, mirrored from the catalog without a backend dependency.
# Never infer permission categories from key prefixes or caller-supplied metadata.
FEATURE_CATEGORIES = {
    "flow.open": "flow",
    "action.navigate": "action",
    "auth.form_login": "auth",
    "auth.password_verify": "auth",
    "action.wait": "action",
    "sign.button": "sign",
    "sign.unit3d_claim": "sign",
    "challenge.altcha": "challenge",
    "challenge.slider": "challenge",
    "action.click": "action",
    "challenge.2fa": "challenge",
    "challenge.turnstile": "challenge",
    "challenge.image_captcha": "challenge",
    "challenge.grid_captcha": "challenge",
    "sign.iframe.image_captcha": "sign",
    "sign.image_quiz": "sign",
    "action.fill": "action",
    "flow.result": "flow",
}


class ExecutionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    feature_types: list[Feature] = Field(min_length=1, max_length=18)
    allow_sign: bool = False


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    username: str | None = Field(default=None, min_length=1, max_length=4096, repr=False)
    password: str = Field(min_length=1, max_length=4096, repr=False)


class PrivateSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    llm_prompt: str = Field(default="", max_length=4000, repr=False)
    llm_enabled: bool = False
    use_vision: bool = True
    judgment_scope: Literal["success_only", "all"] = "success_only"
    model: str = Field(default="", max_length=256, repr=False)


class PrivateVision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    base_url: str = Field(min_length=1, max_length=4096, repr=False)
    api_key: str = Field(min_length=1, max_length=4096, repr=False)
    model: str = Field(min_length=1, max_length=256, repr=False)
    timeout: float = Field(default=120, gt=0, le=600)
    max_tokens: int | None = Field(default=None, ge=0, le=1000000)
    supports_vision: bool = True
    execution_settings: PrivateSettings | None = Field(default=None, repr=False)

    @field_validator("base_url")
    @classmethod
    def provider_url(cls, value):
        from worker.secret_client import require_site_origin

        try:
            # Host-selected internal HTTP endpoints are valid; unsafe protocols,
            # userinfo and control characters are not. No browser DNS policy here.
            require_site_origin(value, value, [])
        except RuntimeError:
            raise ValueError("Invalid provider endpoint") from None
        return value


class PrivateCFBox(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    url: str = Field(min_length=1, max_length=4096, repr=False)
    api_key: str = Field(default="", max_length=4096, repr=False)

    @field_validator("url")
    @classmethod
    def provider_url(cls, value):
        return PrivateVision.provider_url(value)


class PrivateRuntime(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    totp_code: str | None = Field(default=None, min_length=1, max_length=128, repr=False)
    vision: PrivateVision | None = Field(default=None, repr=False)
    cfbox: PrivateCFBox | None = Field(default=None, repr=False)
    feature_execution_settings: dict[Literal[
        "challenge.image_captcha", "sign.iframe.image_captcha", "sign.image_quiz",
        "challenge.grid_captcha", "flow.result",
    ], PrivateSettings] | None = Field(default=None, repr=False)


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    token: str = Field(min_length=1, max_length=512, repr=False)
    operation_id: UUID
    step: dict[str, Any] = Field(repr=False)
    execution: ExecutionPolicy
    credentials: Credentials | None = Field(default=None, repr=False)
    private_runtime: PrivateRuntime | None = Field(default=None, repr=False)
    site_origin: str = Field(min_length=1, max_length=4096)
    # Internal host-only metadata, never part of model-provided step arguments.
    retry_of: UUID | None = None
    retry_authorization: UUID | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def paired_retry(self):
        if ((self.retry_of is None) != (self.retry_authorization is None)
                or self.retry_of == self.operation_id):
            raise ValueError("Invalid retry authorization")
        return self

    @field_validator("step")
    @classmethod
    def bounded_step(cls, value):
        try:
            if len(json.dumps(value, ensure_ascii=True, allow_nan=False).encode()) > 65536:
                raise ValueError()
        except (TypeError, ValueError, RecursionError):
            raise ValueError("Invalid step") from None
        return value


# Only configuration consumed by these reviewed branches is admitted. Full
# feature normalization belongs to the backend, not to the model or the worker.
CONFIG_FIELDS = {
    "flow.open": {"url", "wait_until", "retry_count"},
    "action.navigate": {"url", "wait_until", "retry_count"},
    "auth.form_login": {"username_selector", "password_selector", "submit_selector",
                        "success_selector", "success_wait_seconds", "username_field", "password_field"},
    "auth.password_verify": {"password_selector", "submit_selector", "success_selector"},
    "action.wait": {"seconds"},
    "action.click": {"selector", "button", "click_count"},
    "sign.button": {"button_selector", "confirm_selector", "success_signals"},
    "sign.unit3d_claim": {"claim_selector", "success_signals", "success_selector",
                          "success_when_missing", "success_wait_seconds"},
    "challenge.altcha": {"widget_selector", "checkbox_selector", "token_selector",
                         "success_selector", "wait_seconds"},
    "challenge.slider": {"container_selector", "handle_selector", "success_selector"},
    "challenge.2fa": {"code_selector", "submit_selector", "success_selector", "service_hint",
                      "username_hint", "totp_provider_id"},
    "challenge.turnstile": {"wait_seconds"},
    "challenge.image_captcha": {"image_selector", "input_selector", "submit_selector",
        "refresh_selector", "captcha_method", "charset", "length", "min_confidence"},
    "challenge.grid_captcha": {"image_selector", "cell_selector", "selected_selector",
        "refresh_selector", "submit_selector", "max_refresh", "success_signals",
        "success_selector", "success_when_missing", "success_wait_seconds"},
    "sign.image_quiz": {"image_selector", "answer_selector", "question_selector",
        "message_selector", "message_text", "refresh_selector", "submit_selector",
        "success_signals", "success_selector", "success_when_missing", "success_wait_seconds", "max_refresh"},
    "action.fill": {"selector", "value", "clear"},
    "flow.result": {"output_value", "message", "llm_enabled", "llm_prompt"},
}
CONFIG_FIELDS["sign.iframe.image_captcha"] = CONFIG_FIELDS["challenge.image_captcha"] | {"iframe_selector", "fields"}


def validate_step(request: ExecuteRequest) -> dict[str, Any]:
    step = request.step
    if set(step) - {"step_key", "feature_type", "config", "timeout_seconds", "label",
                    "group_key", "position", "enabled", "condition", "on_error"}:
        raise ValueError("Invalid step")
    feature = step.get("feature_type")
    if not isinstance(feature, str) or feature not in FEATURES or feature not in request.execution.feature_types:
        raise ValueError("Feature denied")
    category = FEATURE_CATEGORIES.get(feature)
    if category not in {"flow", "auth", "challenge", "sign", "action"}:
        raise ValueError("Category denied")
    if category in {"sign", "challenge"} and not request.execution.allow_sign:
        raise ValueError("Signing denied")
    if not isinstance(step.get("step_key"), str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", step["step_key"]):
        raise ValueError("Invalid step")
    if step.get("condition") not in (None, "", "always") or step.get("enabled", True) is not True:
        raise ValueError("Step is not unconditional and enabled")
    timeout = step.get("timeout_seconds")
    if timeout is not None and (type(timeout) is not int or not 1 <= timeout <= 3600):
        raise ValueError("Invalid timeout")
    config = step.get("config")
    if not isinstance(config, dict) or set(config) - CONFIG_FIELDS[feature]:
        raise ValueError("Invalid config")
    for key, value in config.items():
        if key == "selected_selector":
            if not isinstance(value, str) or not value.strip() or len(value) > 2048:
                raise ValueError("Invalid selector")
        elif key in {"clear", "output_value", "llm_enabled"}:
            if type(value) is not bool:
                raise ValueError("Invalid boolean")
        elif key == "fields":
            if type(value) is not dict or value:
                raise ValueError("Static fields denied")
        elif key == "min_confidence":
            if type(value) not in {int, float} or not 0 <= value <= 1:
                raise ValueError("Invalid confidence")
        elif key.endswith("selector") or key in {"success_signals", "success_when_missing"}:
            if (not isinstance(value, list) or len(value) > 30 or any(
                not isinstance(item, str) or not item.strip() or len(item) > 2048 for item in value
            )):
                raise ValueError("Invalid candidates")
        elif key in {"retry_count", "seconds", "click_count", "wait_seconds", "success_wait_seconds", "length", "max_refresh"}:
            upper = {"retry_count": 0, "seconds": 300, "click_count": 3,
                       "wait_seconds": 120, "success_wait_seconds": 60, "length": 20, "max_refresh": 10}[key]
            if key == "success_wait_seconds" and feature in {"auth.form_login", "challenge.grid_captcha"}:
                upper = 120
            lower = 0 if key in {"retry_count", "length", "max_refresh"} else 1
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError("Invalid count")
        elif not isinstance(value, str) or (not value and key not in {
            "value", "message", "message_text", "llm_prompt", "username_hint", "service_hint"
        }) or len(value) > 4096:
            raise ValueError("Invalid config value")
    required = {
        "auth.form_login": ("username_selector", "password_selector", "submit_selector"),
        "auth.password_verify": ("password_selector", "submit_selector"),
        "action.click": ("selector",), "sign.button": ("button_selector",),
        "sign.unit3d_claim": ("claim_selector",),
        "challenge.altcha": ("widget_selector", "checkbox_selector", "token_selector"),
        "challenge.slider": ("container_selector", "handle_selector"),
        "challenge.2fa": ("code_selector",),
        "challenge.image_captcha": ("image_selector", "input_selector"),
        "sign.iframe.image_captcha": ("iframe_selector", "image_selector", "input_selector", "submit_selector"),
        "sign.image_quiz": ("image_selector", "answer_selector"),
        "action.fill": ("selector",),
    }
    if any(not config.get(key) for key in required.get(feature, ())):
        raise ValueError("Missing candidates")
    if config.get("wait_until", "domcontentloaded") not in {"domcontentloaded", "load", "networkidle"}:
        raise ValueError("Invalid wait")
    if config.get("button", "left") not in {"left", "right", "middle"}:
        raise ValueError("Invalid button")
    if category == "auth":
        if request.credentials is None or (feature == "auth.form_login" and not request.credentials.username):
            raise ValueError("Credentials unavailable")
    elif request.credentials is not None:
        raise ValueError("Unexpected credentials")
    if config.get("captcha_method", "ddddocr") not in {"ddddocr", "vision", "nexusphp_gd5"}:
        raise ValueError("Provider denied")
    if config.get("totp_provider_id", "builtin.twofauth") != "builtin.twofauth":
        raise ValueError("Provider denied")
    if config.get("charset", "alnum") not in {"alnum", "digits", "lower", "upper", "letters", "digits_lower", "digits_upper", "any"}:
        raise ValueError("Invalid charset")
    runtime = request.private_runtime
    if feature == "challenge.2fa" and (runtime is None or runtime.totp_code is None):
        raise ValueError("TOTP unavailable")
    if runtime is not None:
        if runtime.totp_code is not None and feature != "challenge.2fa":
            raise ValueError("Unexpected TOTP")
        if runtime.cfbox is not None and feature != "challenge.turnstile":
            raise ValueError("Unexpected CFbox")
        if runtime.vision is not None and feature not in {
            "challenge.image_captcha", "sign.iframe.image_captcha", "sign.image_quiz", "challenge.grid_captcha", "flow.result"
        }:
            raise ValueError("Unexpected vision")
        if runtime.feature_execution_settings and set(runtime.feature_execution_settings) != {feature}:
            raise ValueError("Unexpected settings")
    return {**step, "config": dict(config)}


async def validate_secret_targets(page, step, site_origin, targets=None):
    """Resolve the same ordered candidates as the executor, before releasing secrets."""
    from worker.secret_client import require_site_origin

    require_site_origin(page.url, site_origin, [])
    chosen = {}
    keys = ["password_selector", "submit_selector"]
    if step["feature_type"] == "challenge.2fa":
        keys = ["code_selector"]
        if step["config"].get("submit_selector"):
            keys.append("submit_selector")
    if step["feature_type"] == "auth.form_login":
        keys.insert(0, "username_selector")
    for key in keys:
        for selector in step["config"][key]:
            locator = page.locator(selector)
            count = await locator.count()
            if not count or not await locator.first.is_visible():
                continue
            if count != 1:
                raise RuntimeError("Secret target denied")
            chosen[key] = (selector, locator)
            break
        if key not in chosen:
            raise RuntimeError("Secret target denied")
    # Retain the original nodes and form, not just selectors that can be rebound.
    owned = targets is None or not targets
    handles = {} if owned else targets
    try:
        if owned:
            for key, (_, locator) in chosen.items():
                handles[key] = await locator.element_handle()
            handles["form"] = await handles[keys[-1]].evaluate_handle("el => el.form")
        for key, (_, locator) in chosen.items():
            valid = await locator.evaluate("""(el, {submit, original, originalForm, kind, origin}) => {
                const form = el.form;
                if (el.ownerDocument.location.origin !== origin || el !== original || !el.isConnected ||
                    !form || form !== originalForm || (submit && (!submit.isConnected ||
                    form !== submit.form || !['BUTTON','INPUT'].includes(submit.tagName) ||
                    !['submit','button'].includes(submit.type))) ||
                    (form.target && form.target !== '_self') ||
                    (submit && submit.formTarget && submit.formTarget !== '_self')) return false;
                // JS handlers may prevent the default GET and POST to another same-origin path.
                try {
                    const u = new URL(submit && submit.hasAttribute('formaction') ? submit.formAction : form.action, location.href);
                    if (!['http:','https:'].includes(u.protocol) || u.origin !== origin ||
                        u.username || u.password) return false;
                } catch { return false; }
                if (kind === 'submit_selector') return el === submit;
                return el.tagName === 'INPUT' && !el.disabled && !el.readOnly &&
                    (kind === 'password_selector' ? el.type === 'password' :
                     kind === 'code_selector' ? ['text','tel','number'].includes(el.type) : ['text','email'].includes(el.type));
            }""", {"submit": handles.get("submit_selector"), "original": handles[key],
                    "originalForm": handles["form"],
                    "kind": key, "origin": site_origin})
            if valid is not True:
                raise RuntimeError("Secret target denied")
        require_site_origin(page.url, site_origin, [])
        for key, (selector, _) in chosen.items():
            step["config"][key] = [selector]
        if targets is not None and owned:
            targets.update(handles)
            owned = False
    finally:
        if owned:
            for handle in handles.values():
                if handle is not None:
                    await handle.dispose()


def fingerprints(values):
    return {(len(value), hashlib.sha256(value.encode()).digest()) for value in values if value}


def contains_secret(value, known):
    return any(hashlib.sha256(value[i:i + length].encode()).digest() == digest
               for length, digest in known for i in range(len(value) - length + 1))


def secret_in_url(value, known):
    # Include form-style '+' encoding and nested percent encoding. Keep this
    # bounded even for hostile URLs; normal URLs converge after one decode.
    for _ in range(8):
        if contains_secret(value, known):
            return True
        decoded = unquote_plus(value)
        if decoded == value:
            return False
        value = decoded
    return contains_secret(value, known)


def redact(value, known):
    if isinstance(value, str):
        return "[redacted]" if contains_secret(value, known) else value
    if isinstance(value, list):
        return [redact(item, known) for item in value]
    if isinstance(value, dict):
        return {key: redact(item, known) for key, item in value.items()}
    return value
