import os

import httpx
from urllib.parse import urlsplit


def require_site_origin(current, base):
    def origin(value):
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise RuntimeError("Secret origin denied")
        return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)
    if origin(current) != origin(base):
        raise RuntimeError("Secret origin denied")


async def resolve_secret(payload, step):
    access = payload.get("secret_access") or {}
    purpose = step["feature_type"]
    if purpose not in {"auth.form_login", "challenge.2fa"}:
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
