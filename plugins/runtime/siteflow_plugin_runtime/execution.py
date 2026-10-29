from __future__ import annotations

import importlib
import json
from typing import Any, Callable

import grpc

from siteflow_proto import plugin_pb2, plugin_pb2_grpc

from .broker import BrokerClient, BrokerError
from .config import Config, ConfigurationError
from .control import PluginState, _authorize_state

StepHandler = Callable[[dict[str, Any], BrokerClient], dict[str, Any]]
CaptchaHandler = Callable[[dict[str, Any]], dict[str, Any]]


def load_handler(spec: str) -> Callable[..., Any]:
    module_name, separator, attribute = spec.partition(":")
    if not module_name:
        raise ConfigurationError("plugin handler must use module:attribute syntax")
    module = importlib.import_module(module_name)
    handler = getattr(module, attribute or "execute_step", None)
    if not callable(handler):
        raise ConfigurationError(f"plugin handler is not callable: {spec}")
    return handler


class PluginExecutionServicer(plugin_pb2_grpc.PluginExecutionServicer):
    def __init__(
        self,
        state: PluginState,
        execute_handler: StepHandler | None = None,
        captcha_handler: CaptchaHandler | None = None,
    ) -> None:
        self.state = state
        self.execute_handler = execute_handler
        self.captcha_handler = captcha_handler

    def _authorize(self, context: grpc.ServicerContext) -> None:
        _authorize_state(self.state.config, context)

    def ExecuteStep(  # noqa: N802
        self, request: plugin_pb2.StepRequest, context: grpc.ServicerContext
    ) -> plugin_pb2.StepResult:
        self._authorize(context)
        if self.execute_handler is None:
            context.abort(grpc.StatusCode.UNIMPLEMENTED, "plugin step execution is not configured")
        try:
            config = json.loads(request.config_json or "{}")
        except json.JSONDecodeError:
            return plugin_pb2.StepResult(status="error", message="步骤配置不是合法 JSON")
        if not isinstance(config, dict):
            return plugin_pb2.StepResult(status="error", message="步骤配置必须是 JSON 对象")
        timeout = max(1.0, min(3600.0, request.deadline_ms / 1000 or 30.0))
        broker = BrokerClient(
            request.broker.url,
            request.broker.token,
            timeout=timeout,
        )
        try:
            result = self.execute_handler(
                {
                    "execution_id": request.execution_id,
                    "plugin_id": request.plugin_id,
                    "feature_key": request.feature_key,
                    "config": config,
                    "site_origin": request.site_origin,
                    "current_url": request.current_url,
                    "deadline_ms": request.deadline_ms,
                    "is_active": context.is_active,
                },
                broker,
            )
        except BrokerError as exc:
            return plugin_pb2.StepResult(status="error", message=str(exc))
        except Exception as exc:
            return plugin_pb2.StepResult(
                status="error",
                message=f"{type(exc).__name__}: {str(exc)[:500]}",
            )
        if not context.is_active():
            context.abort(grpc.StatusCode.CANCELLED, "step canceled")
        if not isinstance(result, dict):
            return plugin_pb2.StepResult(status="error", message="插件返回了无效结果")
        status = str(result.get("status") or "error")
        message = str(result.get("message") or "")
        data = result.get("data")
        if data is None:
            data_json = str(result.get("data_json") or "{}")
        else:
            data_json = json.dumps(data, ensure_ascii=False)
        return plugin_pb2.StepResult(status=status, message=message, data_json=data_json)

    def RecognizeCaptcha(  # noqa: N802
        self, request: plugin_pb2.CaptchaRequest, context: grpc.ServicerContext
    ) -> plugin_pb2.CaptchaResult:
        self._authorize(context)
        if self.captcha_handler is None:
            context.abort(grpc.StatusCode.UNIMPLEMENTED, "plugin captcha recognition is not configured")
        try:
            result = self.captcha_handler(
                {
                    "method_key": request.method_key,
                    "image": request.image,
                    "charset": request.charset,
                    "length": request.length,
                }
            )
        except Exception as exc:
            context.abort(
                grpc.StatusCode.INTERNAL,
                f"{type(exc).__name__}: {str(exc)[:300]}",
            )
        if not isinstance(result, dict):
            context.abort(grpc.StatusCode.INTERNAL, "plugin returned an invalid captcha result")
        meta = result.get("meta")
        meta_json = result.get("meta_json")
        if not isinstance(meta_json, str):
            meta_json = json.dumps(meta or {}, ensure_ascii=False)
        try:
            confidence = float(result.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        try:
            margin = int(result.get("margin") or 0)
        except (TypeError, ValueError):
            margin = 0
        return plugin_pb2.CaptchaResult(
            text=str(result.get("text") or ""),
            confidence=confidence,
            margin=margin,
            meta_json=meta_json,
        )
