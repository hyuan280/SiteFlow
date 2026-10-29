from __future__ import annotations

import json
import asyncio
import threading
from typing import Any

import grpc

from siteflow_proto import plugin_pb2, plugin_pb2_grpc

PLUGIN_AUTH_METADATA_KEY = "x-plugin-token"


class PluginClientError(RuntimeError):
    pass


async def cancellable_call(function, *args, authorize=None, **kwargs):
    cancel = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(function, *args, cancel_event=cancel, **kwargs))
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=0.5)
            if authorize is not None:
                await authorize()
        return await task
    finally:
        cancel.set()
        await asyncio.gather(task, return_exceptions=True)


def _channel(endpoint: str) -> grpc.Channel:
    return grpc.insecure_channel(
        endpoint,
        options=(("grpc.enable_http_proxy", 0),),
    )


def _metadata(token: str) -> tuple[tuple[str, str], ...]:
    return ((PLUGIN_AUTH_METADATA_KEY, token),)


def execute_step(
    endpoint: str,
    token: str,
    *,
    execution_id: str,
    plugin_id: str,
    feature_key: str,
    config: dict[str, Any],
    site_origin: str,
    current_url: str,
    deadline_ms: int,
    broker_url: str,
    broker_token: str,
    timeout: float,
    cancel_event: Any = None,
) -> dict[str, Any]:
    channel = _channel(endpoint)
    try:
        stub = plugin_pb2_grpc.PluginExecutionStub(channel)
        call = stub.ExecuteStep.future(
            plugin_pb2.StepRequest(
                execution_id=execution_id,
                plugin_id=plugin_id,
                feature_key=feature_key,
                config_json=json.dumps(config, ensure_ascii=False),
                site_origin=site_origin,
                current_url=current_url,
                deadline_ms=max(1, int(deadline_ms)),
                broker=plugin_pb2.BrokerInfo(url=broker_url, token=broker_token),
            ),
            timeout=timeout,
            metadata=_metadata(token),
        )
        while True:
            if cancel_event is not None and cancel_event.is_set():
                call.cancel()
                raise PluginClientError("plugin step authorization revoked")
            try:
                response = call.result(timeout=0.1)
                if cancel_event is not None and cancel_event.is_set():
                    call.cancel()
                    raise PluginClientError("plugin step canceled; late result rejected")
                break
            except grpc.FutureTimeoutError:
                continue
    except grpc.RpcError as exc:
        raise PluginClientError(
            f"plugin step execution failed: {exc.code().name}: {exc.details()}"
        ) from exc
    finally:
        channel.close()
    try:
        data = json.loads(response.data_json or "{}")
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {
        "status": str(response.status or "error"),
        "message": str(response.message or ""),
        "data": data,
    }


def recognize_captcha(
    endpoint: str,
    token: str,
    *,
    method_key: str,
    image: bytes,
    charset: str,
    length: int,
    timeout: float,
    cancel_event: Any = None,
) -> dict[str, Any]:
    channel = _channel(endpoint)
    try:
        stub = plugin_pb2_grpc.PluginExecutionStub(channel)
        call = stub.RecognizeCaptcha.future(
            plugin_pb2.CaptchaRequest(
                method_key=method_key,
                image=image,
                charset=charset,
                length=max(0, int(length)),
            ),
            timeout=timeout,
            metadata=_metadata(token),
        )
        while True:
            if cancel_event is not None and cancel_event.is_set():
                call.cancel()
                raise PluginClientError("plugin captcha canceled")
            try:
                response = call.result(timeout=0.1)
                if cancel_event is not None and cancel_event.is_set():
                    raise PluginClientError("plugin captcha late result rejected")
                break
            except grpc.FutureTimeoutError:
                continue
    except grpc.RpcError as exc:
        raise PluginClientError(
            f"plugin captcha recognition failed: {exc.code().name}: {exc.details()}"
        ) from exc
    finally:
        channel.close()
    try:
        meta = json.loads(response.meta_json or "{}")
    except json.JSONDecodeError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    confidence = float(response.confidence) if response.confidence else None
    return {
        "text": str(response.text or ""),
        "confidence": confidence,
        "margin": int(response.margin or 0),
        "meta": meta,
    }
