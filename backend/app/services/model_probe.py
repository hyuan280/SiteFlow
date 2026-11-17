"""Bounded, ephemeral registry probes. No provider prose or completion is exposed."""
from __future__ import annotations

import asyncio
import json
import re

import httpx

from app.schemas.integration import IntegrationTestResult

_IMAGE = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC"
)
_LIMIT = 64 * 1024
_PROVIDER_ERRORS = {
    "invalid_api_key": "密钥无效，请检查认证配置。",
    "authentication_error": "认证失败，请检查密钥及其权限。",
    "model_not_found": "模型不存在或当前密钥无权访问。",
    "unsupported_model": "服务不支持此模型。",
    "unsupported_parameter": "模型不支持请求中的参数。",
    "unsupported_value": "模型不支持请求中的参数值或输入类型。",
    "rate_limit_exceeded": "请求频率超过服务限制，请稍后再试。",
    "rate_limit_error": "请求频率超过服务限制，请稍后再试。",
    "insufficient_quota": "服务额度不足，请检查账户余额或配额。",
    "quota_exceeded": "服务额度已用尽，请检查账户配额。",
    "invalid_request_error": "服务拒绝请求，请检查模型、参数及视觉输入支持。",
}


def _provider_error(raw, config):
    """Only exact known codes survive; arbitrary messages, headers and HTML never do."""
    try:
        error = json.loads(raw).get("error")
        if not isinstance(error, dict):
            return None
        result = {}
        for field in ("code", "type"):
            value = error.get(field)
            if (isinstance(value, str) and value in _PROVIDER_ERRORS
                    and not any(secret and secret in value for secret in (
                        config["api_key"], config["base_url"], config["model"],
                    ))):
                result[field] = value
        if result:
            result["message"] = _PROVIDER_ERRORS[next(iter(result.values()))]
            return result
    except (ValueError, AttributeError, RecursionError):
        pass
    return None


async def _probe(config):
    mode = "vision" if config["supports_vision"] else "text"
    data = {"mode": mode}

    def result(message, stage, *, ok=False, **details):
        return IntegrationTestResult(ok=ok, message=message, data={**data, "stage": stage, **details})

    content = "Reply with the single word OK."
    if mode == "vision":
        content = [
            {"type": "text", "text": "Describe this image in a few words."},
            {"type": "image_url", "image_url": {"url": _IMAGE}},
        ]
    body = {"model": config["model"], "messages": [{"role": "user", "content": content}]}
    if config.get("max_tokens"):
        body["max_tokens"] = config["max_tokens"]
    seconds = config["timeout"]
    try:
        # The outer deadline also bounds slow trickle responses across HTTPX phases.
        async with asyncio.timeout(seconds):
            async with httpx.AsyncClient(
                timeout=seconds, trust_env=False, follow_redirects=False,
            ) as client:
                async with client.stream(
                    "POST", config["base_url"] + "/chat/completions",
                    headers={"Authorization": f"Bearer {config['api_key']}",
                             "Accept-Encoding": "identity"}, json=body,
                ) as response:
                    status = response.status_code
                    data["http_status"] = status
                    if response.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
                        return result("服务返回了不支持的压缩编码，请配置服务返回未压缩响应。", "invalid_response")
                    raw = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(raw) + len(chunk) > _LIMIT:
                            return result("服务响应超过 64 KiB 限制，已停止读取。", "response_limit")
                        raw.extend(chunk)
        if not 200 <= status < 300:
            reason = {
                401: "认证失败，请检查密钥。", 403: "访问被拒绝，请检查密钥权限。",
                404: "接口或模型不存在，请检查地址和模型名称。",
                429: "请求受限或额度不足，请检查速率限制和账户配额。",
            }.get(status, "服务返回错误，请检查服务状态和请求配置。")
            if 300 <= status < 400:
                reason = "服务要求重定向；为保护密钥，测试不会跟随重定向。"
            provider = _provider_error(raw, config)
            if provider:
                reason += " " + provider["message"]
            return result(f"模型测试失败（HTTP {status}）：{reason}", "http",
                          **({"provider_error": provider} if provider else {}))
        try:
            envelope = json.loads(raw)
        except (ValueError, RecursionError):
            return result("服务响应不是有效 JSON，请检查是否使用 OpenAI 兼容接口。", "invalid_json")
        if not isinstance(envelope, dict) or "error" in envelope:
            return result("服务响应不是有效的聊天完成结果。", "invalid_response")
        choices = envelope.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return result("服务响应缺少有效的 choices 聊天结果。", "invalid_response")
        choice = choices[0]
        if choice.get("finish_reason") == "length":
            return result("模型输出因长度限制被截断，请提高最大令牌数后重试。", "output_length")
        if choice.get("finish_reason") == "content_filter":
            return result("服务的内容过滤器阻止了测试输出。", "content_filter")
        message = choice.get("message")
        if not isinstance(message, dict):
            return result("服务响应缺少有效的 message 聊天消息。", "invalid_response")
        text = message.get("content")
        if message.get("refusal") or (isinstance(text, list) and any(
            isinstance(part, dict) and part.get("type") == "refusal" for part in text
        )):
            return result("模型拒绝了测试请求，请检查模型能力和服务策略。", "refusal")
        if isinstance(text, list):
            text = "".join(part["text"] for part in text if isinstance(part, dict)
                           and part.get("type") == "text" and isinstance(part.get("text"), str))
        if isinstance(text, str):
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
            if re.search(r"</?think\b", text, flags=re.IGNORECASE):
                text = ""
        if not isinstance(text, str) or not text.strip() or any(
            ord(char) < 32 and char not in "\n\r\t" or 127 <= ord(char) <= 159 for char in text
        ):
            return result("模型未返回有效文本（仅推理内容或空输出不算成功）。", "invalid_response")
        return result("视觉请求成功。" if mode == "vision" else "文本请求成功（未启用视觉识别）。",
                      "complete", ok=True)
    except (TimeoutError, httpx.TimeoutException) as exc:
        phase = next((name for kind, name in (
            (httpx.ConnectTimeout, "connect"), (httpx.ReadTimeout, "read"),
            (httpx.WriteTimeout, "write"), (httpx.PoolTimeout, "pool"),
        ) if isinstance(exc, kind)), "total")
        return result(f"模型测试超时（{seconds:g} 秒），请检查服务连接或稍后重试。", "timeout",
                      timeout_phase=phase, timeout_seconds=seconds)
    except httpx.RequestError:
        return result("无法连接模型服务，请检查地址、网络和 TLS 证书。", "connection")
    except Exception:
        return result("模型测试失败，服务连接或响应无效。", "invalid_response")


def probe_model(config):
    # Registry routes execute in FastAPI's thread pool, without an event loop.
    return asyncio.run(_probe(config))
