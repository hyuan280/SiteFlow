from __future__ import annotations

import json
import threading
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.features.base import Category, FeatureConfigError
from app.models.plugin import PluginRegistration

_lock = threading.RLock()
_plugin_features: dict[str, "RemoteFeature"] = {}
_plugin_captcha_methods: dict[str, "RemoteCaptchaMethod"] = {}
PLUGIN_FEATURE_CATEGORIES = {"flow", "auth", "challenge", "sign", "action", "post"}
PLUGIN_CARDINALITIES = {"single", "multiple"}


@dataclass(frozen=True)
class RemoteCaptchaMethod:
    key: str
    label: str
    description: str
    supports_vision: bool
    plugin_id: str


@dataclass(frozen=True)
class RemoteFeature:
    key: str
    name: str
    category: Category
    cardinality: str
    requires: frozenset[str]
    description: str
    version: int
    schema: dict[str, Any]
    plugin_id: str

    def config_schema(self) -> dict[str, Any]:
        return deepcopy(self.schema)

    def _validator(self, schema: dict[str, Any]) -> Draft202012Validator:
        return Draft202012Validator(schema)

    @staticmethod
    def _message(exc: ValidationError) -> str:
        return "plugin configuration incompatible with schema"

    def validate_override(self, config: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(config, dict):
            raise FeatureConfigError("config 必须是 JSON 对象")
        properties = self.schema.get("properties", {})
        unknown = sorted(set(config) - set(properties))
        if unknown:
            raise FeatureConfigError(f"未知配置字段: {', '.join(unknown)}")
        partial_schema = deepcopy(self.schema)
        partial_schema.pop("required", None)
        values = {key: value for key, value in config.items() if value is not None}
        errors = sorted(
            self._validator(partial_schema).iter_errors(values),
            key=lambda item: list(item.absolute_path),
        )
        if errors:
            raise FeatureConfigError(self._message(errors[0]))
        return config

    def validate_full(self, config: dict[str, Any]) -> dict[str, Any]:
        errors = sorted(
            self._validator(self.schema).iter_errors(config),
            key=lambda item: list(item.absolute_path),
        )
        if errors:
            raise FeatureConfigError(self._message(errors[0]))
        return config

    def validate_field(self, key: str, value: Any) -> None:
        properties = self.schema.get("properties", {})
        if key not in properties:
            raise FeatureConfigError(f"未知配置字段: {key}")
        if value is None:
            return
        partial_schema = {
            "$schema": self.schema.get("$schema", "https://json-schema.org/draft/2020-12/schema"),
            "type": "object",
            "properties": {key: properties[key]},
            "required": [key],
        }
        if "$defs" in self.schema:
            partial_schema["$defs"] = self.schema["$defs"]
        errors = sorted(
            self._validator(partial_schema).iter_errors({key: value}),
            key=lambda item: list(item.absolute_path),
        )
        if errors:
            raise FeatureConfigError(self._message(errors[0]))


def schema_defaults(schema: dict[str, Any]) -> dict[str, Any]:
    defaults: dict[str, Any] = {}
    for key, prop in (schema.get("properties") or {}).items():
        if isinstance(prop, dict) and "default" in prop:
            defaults[key] = deepcopy(prop["default"])
    return defaults


def schema_required(schema: dict[str, Any]) -> list[str]:
    required = schema.get("required") or []
    return [str(item) for item in required if isinstance(item, str)]


def _plugin_prefix(plugin_id: str) -> str:
    return f"plugin.{plugin_id}."


def build_remote_feature(plugin_id: str, item: dict[str, Any]) -> RemoteFeature:
    key = str(item.get("key") or "")
    if not key.startswith(_plugin_prefix(plugin_id)):
        raise FeatureConfigError(f"插件功能 key 必须使用 {_plugin_prefix(plugin_id)} 前缀")
    name = str(item.get("name") or "").strip()
    if not name:
        raise FeatureConfigError(f"插件功能 {key} 缺少名称")
    category = str(item.get("category") or "")
    if category not in PLUGIN_FEATURE_CATEGORIES:
        raise FeatureConfigError(f"插件功能 {key} 的分类无效: {category}")
    cardinality = str(item.get("cardinality") or "single")
    if cardinality not in PLUGIN_CARDINALITIES:
        raise FeatureConfigError(f"插件功能 {key} 的 cardinality 无效: {cardinality}")
    schema = item.get("config_schema")
    if not isinstance(schema, dict) or schema.get("type", "object") != "object":
        raise FeatureConfigError(f"插件功能 {key} 的 config_schema 必须是 object schema")
    if not isinstance(schema.get("properties", {}), dict):
        raise FeatureConfigError(f"插件功能 {key} 的 properties 必须是对象")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise FeatureConfigError(f"插件功能 {key} 的 config_schema 无效: {exc.message}") from exc
    requires = item.get("requires") or []
    if not isinstance(requires, list) or not all(isinstance(value, str) for value in requires):
        raise FeatureConfigError(f"插件功能 {key} 的 requires 必须是字符串数组")
    try:
        version = int(item.get("version", 1))
    except (TypeError, ValueError) as exc:
        raise FeatureConfigError(f"插件功能 {key} 的版本无效") from exc
    if version < 1:
        raise FeatureConfigError(f"插件功能 {key} 的版本必须大于 0")
    return RemoteFeature(
        key=key,
        name=name,
        category=category,  # type: ignore[arg-type]
        cardinality=cardinality,
        requires=frozenset(requires),
        description=str(item.get("description") or ""),
        version=version,
        schema=deepcopy(schema),
        plugin_id=plugin_id,
    )


def build_remote_captcha_method(
    plugin_id: str, item: dict[str, Any]
) -> RemoteCaptchaMethod:
    key = str(item.get("key") or "")
    if not key.startswith(_plugin_prefix(plugin_id)):
        raise FeatureConfigError(f"插件验证码 key 必须使用 {_plugin_prefix(plugin_id)} 前缀")
    label = str(item.get("label") or "").strip()
    if not label:
        raise FeatureConfigError(f"插件验证码 {key} 缺少 label")
    return RemoteCaptchaMethod(
        key=key,
        label=label,
        description=str(item.get("description") or ""),
        supports_vision=bool(item.get("supports_vision", False)),
        plugin_id=plugin_id,
    )


def _json_list(raw: str) -> list:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def plugin_metadata_for_row(
    row: PluginRegistration,
) -> tuple[list[RemoteFeature], list[RemoteCaptchaMethod]]:
    capabilities = set(_json_list(row.capabilities_json))
    approved = set(_json_list(row.approved_capabilities_json))
    if row.removed_at is not None or not row.enabled or row.approved_at is None or approved != capabilities:
        return [], []
    features: list[RemoteFeature] = []
    methods: list[RemoteCaptchaMethod] = []
    if "features" in capabilities:
        for item in _json_list(row.features_json):
            if isinstance(item, dict):
                try:
                    features.append(build_remote_feature(row.plugin_id, item))
                except FeatureConfigError:
                    continue
    if "captcha_methods" in capabilities:
        for item in _json_list(row.captcha_methods_json):
            if isinstance(item, dict):
                try:
                    methods.append(build_remote_captcha_method(row.plugin_id, item))
                except FeatureConfigError:
                    continue
    return features, methods


def refresh_plugin_registry(session: Session) -> None:
    rows = session.scalars(select(PluginRegistration)).all()
    features: dict[str, RemoteFeature] = {}
    methods: dict[str, RemoteCaptchaMethod] = {}
    for row in rows:
        item_features, item_methods = plugin_metadata_for_row(row)
        for feature in item_features:
            features[feature.key] = feature
        for method in item_methods:
            methods[method.key] = method
    with _lock:
        _plugin_features.clear()
        _plugin_features.update(features)
        _plugin_captcha_methods.clear()
        _plugin_captcha_methods.update(methods)


def clear_plugin_registry() -> None:
    with _lock:
        _plugin_features.clear()
        _plugin_captcha_methods.clear()


def plugin_features() -> list[RemoteFeature]:
    with _lock:
        return sorted(_plugin_features.values(), key=lambda item: (item.plugin_id, item.key))


def get_plugin_feature(key: str) -> RemoteFeature | None:
    with _lock:
        return _plugin_features.get(key)


def plugin_captcha_methods() -> list[RemoteCaptchaMethod]:
    with _lock:
        return sorted(_plugin_captcha_methods.values(), key=lambda item: (item.plugin_id, item.key))


def plugin_captcha_method_keys() -> set[str]:
    with _lock:
        return set(_plugin_captcha_methods)
