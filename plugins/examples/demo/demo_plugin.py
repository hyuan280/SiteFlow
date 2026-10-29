from __future__ import annotations

from typing import Any

from siteflow_plugin_runtime.broker import BrokerClient


def execute_step(request: dict[str, Any], broker: BrokerClient) -> dict[str, Any]:
    if request.get("feature_key") != "plugin.demo.demo_checkin":
        return {
            "status": "fail",
            "message": f"unknown Demo feature: {request.get('feature_key')}",
            "data": {},
        }
    config = request.get("config")
    if not isinstance(config, dict):
        return {"status": "fail", "message": "step config must be an object", "data": {}}
    selectors = config.get("button_selector") or []
    if not isinstance(selectors, list) or not selectors:
        return {"status": "fail", "message": "button_selector is required", "data": {}}
    popup = config.get("popup_selector") or []
    if popup:
        opened = broker.action("click", selectors=popup)
        if not opened.get("ok") or not opened.get("switched_page"):
            return {"status": "fail", "message": "same-origin popup not opened", "data": opened}
    click = broker.action("click", selectors=[str(item) for item in selectors])
    if not click.get("ok"):
        return {
            "status": "fail",
            "message": str(click.get("message") or "click failed"),
            "data": click,
        }
    success_selectors = config.get("success_selector") or []
    text = ""
    if isinstance(success_selectors, list) and success_selectors:
        read = broker.action(
            "read_text",
            selectors=[str(item) for item in success_selectors],
        )
        if read.get("ok"):
            text = str(read.get("text") or "")
        if not read.get("ok") or not text.strip():
            return {"status": "fail", "message": "success text not found", "data": read}
    expected = config.get("expected_text")
    if expected and text.strip() != expected:
        return {"status": "fail", "message": "unexpected result text", "data": {"text": text}}
    return {
        "status": "success",
        "message": "Demo checkin completed",
        "data": {"text": text, "click": click},
    }


def recognize_captcha(request: dict[str, Any]) -> dict[str, Any]:
    from nexusphp_gd5.recognizer import NexusPhpGD5Recognizer

    if request.get("method_key") != "plugin.demo.gd5":
        raise ValueError("unknown demo captcha method")
    if request.get("length") not in (None, 0, 6):
        raise ValueError("demo GD5 requires six characters")
    text, margin = NexusPhpGD5Recognizer().recognize_bytes(request["image"])
    import string

    charset = request.get("charset")
    charset = {
        "alnum": string.ascii_letters + string.digits,
        "digits": string.digits,
        "lower": string.ascii_lowercase,
        "upper": string.ascii_uppercase,
        "letters": string.ascii_letters,
        "digits_lower": string.digits + string.ascii_lowercase,
        "digits_upper": string.digits + string.ascii_uppercase,
        "any": "",
    }.get(charset, charset)
    if charset and any(char not in charset for char in text):
        raise ValueError("recognized text is outside requested charset")
    return {"text": text, "margin": margin, "confidence": 1.0 if margin > 0 else 0.0,
            "meta": {"engine": "gd5-template", "demo_only": True}}
def resolve_credential(request):
    """Public demo fixture only; never use these credentials for real accounts."""
    return {"username": "admin", "password": "password"}


def generate_totp(request):
    """Fixed public demo code, not an actual TOTP account."""
    return {"code": "123456"}
