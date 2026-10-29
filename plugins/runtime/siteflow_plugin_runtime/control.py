from __future__ import annotations

import hmac
import json
import threading
from dataclasses import dataclass, field
from typing import Any

import grpc

from siteflow_proto import plugin_pb2, plugin_pb2_grpc

from .config import Config, ConfigurationError
from .manifest import ManifestError, load_manifest, validate_manifest
from .metadata import load_metadata


@dataclass
class PluginState:
    config: Config
    manifest: dict[str, Any]
    features: list[dict[str, Any]] = field(default_factory=list)
    captcha_methods: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    @classmethod
    def load(cls, config: Config) -> "PluginState":
        manifest = load_manifest(config.manifest_path, config.plugin_id)
        return cls(
            config=config,
            manifest=manifest,
            features=load_metadata(config.features_file, "features"),
            captcha_methods=load_metadata(
                config.captcha_methods_file, "methods"
            ),
        )

    def snapshot(self) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        with self._lock:
            return (
                dict(self.manifest),
                list(self.features),
                list(self.captcha_methods),
            )

    def reload(self) -> dict[str, Any]:
        manifest = validate_manifest(
            load_manifest(self.config.manifest_path, self.config.plugin_id),
            self.config.plugin_id,
        )
        features = load_metadata(self.config.features_file, "features")
        methods = load_metadata(self.config.captcha_methods_file, "methods")
        with self._lock:
            self.manifest = manifest
            self.features = features
            self.captcha_methods = methods
        return manifest


def _manifest_message(manifest: dict[str, Any]) -> plugin_pb2.Manifest:
    return plugin_pb2.Manifest(
        id=manifest["id"],
        name=manifest["name"],
        version=manifest["version"],
        protocol_version=manifest["protocol_version"],
        entrypoint=manifest.get("entrypoint") or "",
        capabilities=list(manifest.get("capabilities", [])),
    )


def _feature_message(feature: dict[str, Any]) -> plugin_pb2.FeatureDefinition:
    schema = feature.get("config_schema")
    if schema is None:
        schema_json = feature.get("config_schema_json", "{}")
    else:
        schema_json = json.dumps(schema, ensure_ascii=False)
    return plugin_pb2.FeatureDefinition(
        key=str(feature.get("key", "")),
        name=str(feature.get("name", "")),
        category=str(feature.get("category", "")),
        cardinality=str(feature.get("cardinality", "single")),
        version=int(feature.get("version", 1)),
        description=str(feature.get("description", "")),
        config_schema_json=schema_json,
        requires=[str(item) for item in feature.get("requires", [])],
    )


def _captcha_message(method: dict[str, Any]) -> plugin_pb2.CaptchaMethod:
    return plugin_pb2.CaptchaMethod(
        key=str(method.get("key", "")),
        label=str(method.get("label", "")),
        description=str(method.get("description", "")),
        supports_vision=bool(method.get("supports_vision", False)),
    )


def _authorize_state(config: Config, context: grpc.ServicerContext) -> None:
    metadata = dict(context.invocation_metadata())
    supplied = metadata.get("x-plugin-token", "")
    try:
        expected = config.token()
    except ConfigurationError as exc:
        context.abort(grpc.StatusCode.INTERNAL, str(exc))
    if not supplied or not hmac.compare_digest(expected, supplied):
        context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid plugin token")


class PluginControlServicer(plugin_pb2_grpc.PluginControlServicer):
    def __init__(self, state: PluginState) -> None:
        self.state = state

    def _authorize(self, context: grpc.ServicerContext) -> None:
        _authorize_state(self.state.config, context)

    def Health(  # noqa: N802
        self, request: plugin_pb2.Empty, context: grpc.ServicerContext
    ) -> plugin_pb2.HealthStatus:
        self._authorize(context)
        manifest, _, _ = self.state.snapshot()
        return plugin_pb2.HealthStatus(
            ok=True,
            plugin_id=manifest["id"],
            version=manifest["version"],
            protocol_version=manifest["protocol_version"],
        )

    def GetManifest(  # noqa: N802
        self, request: plugin_pb2.Empty, context: grpc.ServicerContext
    ) -> plugin_pb2.Manifest:
        self._authorize(context)
        manifest, _, _ = self.state.snapshot()
        return _manifest_message(manifest)

    def ListFeatures(  # noqa: N802
        self, request: plugin_pb2.Empty, context: grpc.ServicerContext
    ) -> plugin_pb2.FeatureList:
        self._authorize(context)
        _, features, _ = self.state.snapshot()
        return plugin_pb2.FeatureList(
            features=[_feature_message(item) for item in features]
        )

    def ListCaptchaMethods(  # noqa: N802
        self, request: plugin_pb2.Empty, context: grpc.ServicerContext
    ) -> plugin_pb2.CaptchaMethodList:
        self._authorize(context)
        _, _, methods = self.state.snapshot()
        return plugin_pb2.CaptchaMethodList(
            methods=[_captcha_message(item) for item in methods]
        )

    def Reload(  # noqa: N802
        self, request: plugin_pb2.ReloadRequest, context: grpc.ServicerContext
    ) -> plugin_pb2.ReloadResult:
        self._authorize(context)
        try:
            manifest = self.state.reload()
        except ManifestError as exc:
            return plugin_pb2.ReloadResult(ok=False, message=str(exc))
        return plugin_pb2.ReloadResult(
            ok=True,
            version=manifest["version"],
            message=request.reason,
        )
