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
]
FEATURES = frozenset(Feature.__args__)


class ExecutionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    feature_types: list[Feature] = Field(min_length=1, max_length=10)
    allow_sign: bool = False


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    username: str | None = Field(default=None, min_length=1, max_length=4096, repr=False)
    password: str = Field(min_length=1, max_length=4096, repr=False)


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    token: str = Field(min_length=1, max_length=512, repr=False)
    operation_id: UUID
    step: dict[str, Any] = Field(repr=False)
    execution: ExecutionPolicy
    credentials: Credentials | None = Field(default=None, repr=False)
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
}


def validate_step(request: ExecuteRequest) -> dict[str, Any]:
    step = request.step
    if set(step) - {"step_key", "feature_type", "config", "timeout_seconds", "label",
                    "group_key", "position", "enabled", "condition", "on_error"}:
        raise ValueError("Invalid step")
    feature = step.get("feature_type")
    if not isinstance(feature, str) or feature not in FEATURES or feature not in request.execution.feature_types:
        raise ValueError("Feature denied")
    if (feature.startswith("sign.") or feature in {"challenge.altcha", "challenge.slider"}) and not request.execution.allow_sign:
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
        if key.endswith("selector") or key in {"success_signals", "success_when_missing"}:
            if (not isinstance(value, list) or len(value) > 30 or any(
                not isinstance(item, str) or not item.strip() or len(item) > 2048 for item in value
            )):
                raise ValueError("Invalid candidates")
        elif key in {"retry_count", "seconds", "click_count", "wait_seconds", "success_wait_seconds"}:
            upper = {"retry_count": 0, "seconds": 300, "click_count": 3,
                      "wait_seconds": 120, "success_wait_seconds": 60}[key]
            if key == "success_wait_seconds" and feature == "auth.form_login":
                upper = 120
            lower = 0 if key == "retry_count" else 1
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError("Invalid count")
        elif not isinstance(value, str) or not value or len(value) > 4096:
            raise ValueError("Invalid config value")
    required = {
        "auth.form_login": ("username_selector", "password_selector", "submit_selector"),
        "auth.password_verify": ("password_selector", "submit_selector"),
        "action.click": ("selector",), "sign.button": ("button_selector",),
        "sign.unit3d_claim": ("claim_selector",),
        "challenge.altcha": ("widget_selector", "checkbox_selector", "token_selector"),
        "challenge.slider": ("container_selector", "handle_selector"),
    }
    if any(not config.get(key) for key in required.get(feature, ())):
        raise ValueError("Missing candidates")
    if config.get("wait_until", "domcontentloaded") not in {"domcontentloaded", "load", "networkidle"}:
        raise ValueError("Invalid wait")
    if config.get("button", "left") not in {"left", "right", "middle"}:
        raise ValueError("Invalid button")
    if feature.startswith("auth."):
        if request.credentials is None or (feature == "auth.form_login" and not request.credentials.username):
            raise ValueError("Credentials unavailable")
    elif request.credentials is not None:
        raise ValueError("Unexpected credentials")
    return {**step, "config": dict(config)}


async def validate_secret_targets(page, step, site_origin, targets=None):
    """Resolve the same ordered candidates as the executor, before releasing secrets."""
    from worker.secret_client import require_site_origin

    require_site_origin(page.url, site_origin, [])
    chosen = {}
    keys = ["password_selector", "submit_selector"]
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
            handles["form"] = await handles["submit_selector"].evaluate_handle("el => el.form")
        for key, (_, locator) in chosen.items():
            valid = await locator.evaluate("""(el, {submit, original, originalForm, kind, origin}) => {
                const form = el.form;
                if (el !== original || !el.isConnected || !submit.isConnected ||
                    !form || form !== originalForm || form !== submit.form || !['BUTTON','INPUT'].includes(submit.tagName) ||
                    !['submit','button'].includes(submit.type) ||
                    (form.target && form.target !== '_self') ||
                    (submit.formTarget && submit.formTarget !== '_self')) return false;
                // JS handlers may prevent the default GET and POST to another same-origin path.
                try {
                    const u = new URL(submit.hasAttribute('formaction') ? submit.formAction : form.action, location.href);
                    if (!['http:','https:'].includes(u.protocol) || u.origin !== origin ||
                        u.username || u.password) return false;
                } catch { return false; }
                if (kind === 'submit_selector') return el === submit;
                return el.tagName === 'INPUT' && !el.disabled && !el.readOnly &&
                    (kind === 'password_selector' ? el.type === 'password' : ['text','email'].includes(el.type));
            }""", {"submit": handles["submit_selector"], "original": handles[key],
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
