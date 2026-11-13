import os
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx
import regex
from urllib.parse import urlsplit


def require_site_origin(current, base, trusted_urls=None):
    def origin(value):
        if (not isinstance(value, str) or len(value) > 8192
                or any(ord(ch) <= 32 or ord(ch) == 127 for ch in value) or "\\" in value):
            raise ValueError()
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError()
        return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        if origin(current) == origin(base):
            return
        if not isinstance(trusted_urls, list) or len(trusted_urls) > 20:
            raise ValueError()
        for pattern in trusted_urls:
            if not isinstance(pattern, str) or not pattern.strip() or len(pattern) > 1024:
                raise ValueError()
            regex.compile(pattern, flags=regex.VERSION0)
        for pattern in trusted_urls:
            if regex.fullmatch(pattern, current, flags=regex.VERSION0, timeout=0.025):
                return
    except (ValueError, TypeError, regex.error, TimeoutError, KeyError):
        pass
    raise RuntimeError("Secret origin denied: 当前页面不属于站点同源或可信网址")


@dataclass
class AssistantSecretContext:
    """In-process capability, never constructible from a JSON request payload."""

    purpose: str
    step_key: str
    credentials: dict[str, str] = field(repr=False)
    validate: Callable[[], Awaitable[None]] = field(repr=False)
    used: bool = False
    targets: dict = field(default_factory=dict, repr=False)
    action_started: bool = False

    async def resolve(self, step):
        if (self.used or self.purpose not in {"auth.form_login", "auth.password_verify", "challenge.2fa"}
                or step.get("feature_type") != self.purpose or step.get("step_key") != self.step_key):
            raise RuntimeError("Secret request denied")
        self.used = True
        await self.validate()
        return dict(self.credentials)


async def validate_assistant_submission(payload, page, step, filled=()):
    """Assistant-only checks; job execution keeps its existing secret protocol."""
    if payload is None or "_assistant_secret_context" not in payload:
        return
    context = payload["_assistant_secret_context"]
    if (type(context) is not AssistantSecretContext or not context.used
            or step.get("feature_type") != context.purpose or step.get("step_key") != context.step_key):
        raise RuntimeError("Secret request denied")
    require_site_origin(page.url, payload["base_url"], [])
    await context.validate()
    for key in filled:
        if await context.targets[key + "_selector"].input_value() != context.credentials[key]:
            raise RuntimeError("Secret input verification failed")
    context.action_started = True


async def resolve_secret(payload, step):
    if "_assistant_secret_context" in payload:
        context = payload["_assistant_secret_context"]
        if type(context) is not AssistantSecretContext:
            raise RuntimeError("Secret request denied")
        return await context.resolve(step)
    access = payload.get("secret_access") or {}
    purpose = step["feature_type"]
    if purpose not in {"auth.form_login", "auth.password_verify", "challenge.2fa"}:
        raise RuntimeError("Secret request denied")
    body = {
        "job_id": payload["job_id"],
        "site_id": payload["site_id"],
        "step_key": step["step_key"],
        "purpose": purpose,
        "provider": access.get("providers", {}).get(step["step_key"], access.get("providers", {}).get(purpose, "")),
    }
    url = os.environ.get("SITEFLOW_SECRET_API_URL", "http://api:8000/api/v1/internal/secrets/resolve")
    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=False) as client:
            headers = {"Authorization": f"Bearer {access.get('token', '')}"}
            response = await client.post(url, json=body, headers=headers)
            response.raise_for_status()
            body["lease"] = response.json()["lease"]
            response = await client.post(url, json=body, headers=headers)
            response.raise_for_status()
            return response.json()
    except Exception:
        raise RuntimeError("Secret provider unavailable or request denied") from None
