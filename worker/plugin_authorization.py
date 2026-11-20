import os
import httpx


async def authorize(runtime, capability="features"):
    access = runtime.get("authorization") or {}
    url = os.environ.get("SITEFLOW_PLUGIN_AUTH_API_URL", "http://api:8000/api/v1/internal/plugins/authorize")
    body = {"job_id": access.get("job_id"), "plugin_id": runtime.get("plugin_id"),
            "generation": runtime.get("generation"), "capability": capability}
    if access.get("scope") == "assistant_operation":
        url = os.environ.get("SITEFLOW_ASSISTANT_PLUGIN_AUTH_API_URL",
                             "http://api:8000/api/v1/internal/assistant-plugins/authorize")
        body = {"session_id": access.get("session_id"), "operation_id": access.get("operation_id"),
                "plugin_id": runtime.get("plugin_id"), "generation": runtime.get("generation"),
                "capability": capability}
    elif access.get("scope") is not None:
        raise RuntimeError("Plugin authorization denied")
    try:
        async with httpx.AsyncClient(timeout=2, follow_redirects=False) as client:
            response = await client.post(url, headers={"Authorization": f"Bearer {access.get('token', '')}"}, json=body)
            response.raise_for_status()
    except Exception:
        raise RuntimeError("Plugin authorization denied") from None
