"""OpenAI-compatible image requests. Provider errors never cross this boundary."""
import base64
from urllib.parse import urlparse

import httpx


DEFAULT_PROMPT = "Read the captcha in this image. Return only its characters, without explanation, spaces, or Markdown. Preserve letter case."


def validate_base_url(value: str) -> str:
    parsed = urlparse(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not parsed.path.rstrip("/").endswith("/v1")):
        raise ValueError("Vision base_url must be an http(s) /v1 root without credentials, query or fragment")
    try:
        parsed.port
    except ValueError:
        raise ValueError("Vision base_url has invalid port") from None
    return value.rstrip("/")


def test_text(config: dict, prompt: str = "Reply with the single word OK.") -> str:
    try:
        base = validate_base_url(config["base_url"])
        with httpx.Client(timeout=config.get("timeout", 30), trust_env=False) as client:
            response = client.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {config['api_key']}"},
                json={
                    "model": config["model"],
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 16,
                },
            )
            response.raise_for_status()
            text = response.json()["choices"][0]["message"]["content"]
        if not isinstance(text, str) or not text.strip() or len(text) > 512:
            raise ValueError("invalid response")
        return text.strip()
    except Exception:
        raise ValueError("Vision provider text request failed or returned invalid output") from None


def recognize(image: bytes, config: dict, prompt: str) -> str:
    try:
        base = validate_base_url(config["base_url"])
        with httpx.Client(timeout=config.get("timeout", 30), trust_env=False) as client:
            response = client.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {config['api_key']}"},
                json={"model": config["model"], "messages": [{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(image).decode("ascii")}},
                ]}], "max_tokens": 128},
            )
            response.raise_for_status()
            text = response.json()["choices"][0]["message"]["content"]
        if not isinstance(text, str) or not text.strip() or len(text) > 512:
            raise ValueError("invalid response")
        return text.strip()
    except Exception:
        raise ValueError("Vision provider request failed or returned invalid output") from None
