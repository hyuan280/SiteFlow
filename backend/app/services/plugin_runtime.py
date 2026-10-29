from __future__ import annotations

import ipaddress
import json
import socket
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import grpc
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import safe_compare
from app.core.config import Settings
from app.models.plugin import PluginRegistration, utcnow
from app.schemas.plugin_runtime import (
    PluginManifest,
    PluginRegisterRequest,
    PluginView,
)
from app.features.plugin_registry import (
    build_remote_captcha_method,
    build_remote_feature,
    refresh_plugin_registry,
)
from siteflow_proto import plugin_pb2, plugin_pb2_grpc
from app.services.plugin_audit import record

PLUGIN_PROTOCOL_VERSION = 1
PLUGIN_AUTH_METADATA_KEY = "x-plugin-token"
PLUGIN_CAPABILITIES = {
    "features",
    "captcha_methods",
    "browser_actions",
}
HIGH_TRUST_PLUGIN_CAPABILITIES = {
    "secret.credentials",
    "secret.totp",
}
TERMINAL_PLUGIN_STATUSES = {"unapproved", "incompatible", "disabled"}


class PluginRuntimeError(RuntimeError):
    pass


class PluginAuthenticationError(PluginRuntimeError):
    pass


class PluginValidationError(PluginRuntimeError):
    pass


class PluginNotFoundError(PluginRuntimeError):
    pass


class PluginGenerationConflict(PluginRuntimeError):
    pass


class PluginUnreachableError(PluginRuntimeError):
    pass


class PluginConfigurationError(PluginRuntimeError):
    pass


def _allowlist_match(host: str, port: int | None, values: list[str]) -> bool:
    host_port = f"{host}:{port}" if port is not None else host
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    for item in values:
        if item.lower() in {host.lower(), host_port.lower()}:
            return True
        if ip is not None and "/" in item:
            try:
                if ip in ipaddress.ip_network(item, strict=False):
                    return True
            except ValueError:
                continue
    return False


def _resolved_addresses(host: str, port: int, *, https: bool) -> set[str]:
    try:
        records = socket.getaddrinfo(
            host,
            port or (443 if https else 80),
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise PluginValidationError(f"plugin endpoint cannot be resolved: {exc}") from exc
    addresses = {record[4][0] for record in records}
    if not addresses:
        raise PluginValidationError("plugin endpoint resolved to no addresses")
    return addresses


def _validate_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if address.is_global:
        raise PluginValidationError("public plugin endpoints must be allowlisted")
    if address.is_loopback or address.is_link_local or address.is_multicast or address.is_reserved:
        raise PluginValidationError("special plugin endpoints must be allowlisted")


def _parse_plugin_endpoint(endpoint: str) -> tuple[str, int]:
    raw = endpoint.strip()
    if raw.startswith("grpc://"):
        raw = raw[len("grpc://"):]
    elif "://" in raw:
        raise PluginValidationError("plugin endpoint must use host:port or grpc://host:port")
    if not raw or any(char in raw for char in "/?#"):
        raise PluginValidationError("plugin endpoint must be host:port without path or query")
    try:
        parsed = urlsplit(f"//{raw}")
        port = parsed.port
    except ValueError as exc:
        raise PluginValidationError(f"plugin endpoint has invalid port: {exc}") from exc
    if parsed.username or parsed.password:
        raise PluginValidationError("plugin endpoint must not include credentials")
    if not parsed.hostname:
        raise PluginValidationError("plugin endpoint must include a host")
    if port is None:
        raise PluginValidationError("plugin endpoint must include a port")
    return parsed.hostname.lower(), port


def _format_plugin_endpoint(host: str, port: int) -> str:
    if ":" in host:
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def validate_plugin_endpoint(
    endpoint: str,
    allowlist: list[str],
    *,
    resolve: bool = True,
) -> str:
    host, port = _parse_plugin_endpoint(endpoint)
    canonical = _format_plugin_endpoint(host, port)
    if _allowlist_match(host, port, allowlist):
        return canonical

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if host == "localhost" and not resolve:
            raise PluginValidationError("loopback plugin endpoints must be allowlisted") from None
        if not resolve:
            if "." not in host or host.endswith((".internal", ".local", ".svc")):
                return canonical
            raise PluginValidationError("public plugin endpoints must be allowlisted") from None
        for resolved in _resolved_addresses(host, port, https=False):
            _validate_address(ipaddress.ip_address(resolved))
        return canonical

    _validate_address(address)
    return canonical


def _grpc_metadata(token: str) -> tuple[tuple[str, str], ...]:
    return ((PLUGIN_AUTH_METADATA_KEY, token),)


def _grpc_channel(endpoint: str) -> grpc.Channel:
    return grpc.insecure_channel(
        endpoint,
        options=(("grpc.enable_http_proxy", 0),),
    )


def _grpc_failure(action: str, exc: grpc.RpcError) -> PluginUnreachableError:
    return PluginUnreachableError(
        f"plugin {action} failed: {exc.code().name}: {exc.details()}"
    )


def verify_plugin_health(endpoint: str, token: str, timeout: float) -> None:
    channel = _grpc_channel(endpoint)
    try:
        stub = plugin_pb2_grpc.PluginControlStub(channel)
        response = stub.Health(
            plugin_pb2.Empty(), timeout=timeout, metadata=_grpc_metadata(token)
        )
    except grpc.RpcError as exc:
        raise _grpc_failure("health check", exc) from exc
    finally:
        channel.close()
    if not response.ok:
        raise PluginUnreachableError(
            response.message or "plugin health check reported failure"
        )


def _manifest_from_proto(message: plugin_pb2.Manifest) -> PluginManifest:
    return PluginManifest(
        id=message.id,
        name=message.name,
        version=message.version,
        protocol_version=message.protocol_version,
        entrypoint=message.entrypoint or None,
        capabilities=list(message.capabilities),
    )


def fetch_plugin_manifest(endpoint: str, token: str, timeout: float) -> PluginManifest:
    channel = _grpc_channel(endpoint)
    try:
        stub = plugin_pb2_grpc.PluginControlStub(channel)
        message = stub.GetManifest(
            plugin_pb2.Empty(), timeout=timeout, metadata=_grpc_metadata(token)
        )
    except grpc.RpcError as exc:
        raise _grpc_failure("manifest request", exc) from exc
    finally:
        channel.close()
    try:
        return _manifest_from_proto(message)
    except (ValueError, ValidationError) as exc:
        raise PluginUnreachableError("plugin manifest response is invalid") from exc


def _schema_from_json(raw: str) -> dict[str, object]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise PluginUnreachableError("plugin feature schema is not valid JSON") from exc
    if not isinstance(value, dict):
        raise PluginUnreachableError("plugin feature schema must be an object")
    return value


def fetch_plugin_features(endpoint: str, token: str, timeout: float) -> list[dict[str, object]]:
    channel = _grpc_channel(endpoint)
    try:
        stub = plugin_pb2_grpc.PluginControlStub(channel)
        response = stub.ListFeatures(
            plugin_pb2.Empty(), timeout=timeout, metadata=_grpc_metadata(token)
        )
    except grpc.RpcError as exc:
        raise _grpc_failure("feature list", exc) from exc
    finally:
        channel.close()
    result: list[dict[str, object]] = []
    for item in response.features:
        result.append(
            {
                "key": item.key,
                "name": item.name,
                "category": item.category,
                "cardinality": item.cardinality,
                "version": item.version,
                "description": item.description,
                "config_schema": _schema_from_json(item.config_schema_json),
                "requires": list(item.requires),
            }
        )
    return result


def fetch_plugin_captcha_methods(
    endpoint: str, token: str, timeout: float
) -> list[dict[str, object]]:
    channel = _grpc_channel(endpoint)
    try:
        stub = plugin_pb2_grpc.PluginControlStub(channel)
        response = stub.ListCaptchaMethods(
            plugin_pb2.Empty(), timeout=timeout, metadata=_grpc_metadata(token)
        )
    except grpc.RpcError as exc:
        raise _grpc_failure("captcha method list", exc) from exc
    finally:
        channel.close()
    return [
        {
            "key": item.key,
            "label": item.label,
            "description": item.description,
            "supports_vision": item.supports_vision,
        }
        for item in response.methods
    ]


def _validate_plugin_metadata(
    plugin_id: str,
    features: list[dict[str, object]],
    captcha_methods: list[dict[str, object]],
) -> None:
    seen_features: set[str] = set()
    for item in features:
        try:
            feature = build_remote_feature(plugin_id, item)
        except Exception as exc:
            raise PluginValidationError(str(exc)) from exc
        if feature.key in seen_features:
            raise PluginValidationError(f"duplicate plugin feature: {feature.key}")
        seen_features.add(feature.key)
    seen_methods: set[str] = set()
    for item in captcha_methods:
        try:
            method = build_remote_captcha_method(plugin_id, item)
        except Exception as exc:
            raise PluginValidationError(str(exc)) from exc
        if method.key in seen_methods:
            raise PluginValidationError(f"duplicate plugin captcha method: {method.key}")
        seen_methods.add(method.key)


def _check(
    key: str,
    label: str,
    ok: bool,
    message: str = "",
    *,
    skipped: bool = False,
) -> dict[str, object]:
    return {
        "key": key,
        "label": label,
        "ok": ok,
        "skipped": skipped,
        "message": message,
    }


def _base_checks() -> list[dict[str, object]]:
    return [
        _check("protocol", "协议版本", True, "协议版本兼容"),
        _check("endpoint", "端点校验", True, "端点通过内部网络校验"),
        _check("capabilities", "能力校验", True, "能力声明有效"),
    ]


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class PluginRuntimeService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def _tokens(self) -> dict[str, str]:
        try:
            return self.settings.plugin_token_map()
        except RuntimeError as exc:
            raise PluginConfigurationError(str(exc)) from exc

    def _authenticate(self, plugin_id: str, token: str) -> None:
        tokens = self._tokens()
        expected = tokens.get(plugin_id)
        if expected is None or not token or not safe_compare(expected, token):
            raise PluginAuthenticationError("invalid plugin token")

    def _view(self, row: PluginRegistration, now: datetime | None = None) -> PluginView:
        status = row.status
        if row.removed_at is not None:
            status = "removed"
        elif not row.enabled:
            status = "disabled"
        elif status not in TERMINAL_PLUGIN_STATUSES:
            current = now or utcnow()
            heartbeat = _aware(row.last_heartbeat_at or row.registered_at)
            ttl = timedelta(seconds=self.settings.plugin_heartbeat_ttl_seconds)
            status = "unhealthy" if current - heartbeat > ttl else "available"
        return PluginView(
            removed_at=row.removed_at,
            removed_by=row.removed_by,
            plugin_id=row.plugin_id,
            name=row.name,
            version=row.version,
            protocol_version=row.protocol_version,
            endpoint=row.endpoint,
            capabilities=json.loads(row.capabilities_json),
            feature_count=len(json.loads(row.features_json)),
            captcha_method_count=len(json.loads(row.captcha_methods_json)),
            checks=json.loads(row.checks_json),
            status=status,
            enabled=row.enabled,
            generation=row.generation,
            approved_capabilities=json.loads(row.approved_capabilities_json),
            approved_at=row.approved_at,
            approved_by=row.approved_by,
            registered_at=row.registered_at,
            last_heartbeat_at=row.last_heartbeat_at,
            updated_at=row.updated_at,
        )

    def register(self, payload: PluginRegisterRequest, token: str) -> PluginView:
        self._authenticate(payload.plugin_id, token)
        self._require_not_removed(self.session.get(PluginRegistration, payload.plugin_id))
        manifest = payload.manifest
        if manifest.id != payload.plugin_id:
            raise PluginValidationError("manifest id must match plugin_id")
        if payload.protocol_version != manifest.protocol_version:
            raise PluginValidationError("request and manifest protocol versions must match")
        if payload.protocol_version != PLUGIN_PROTOCOL_VERSION:
            raise PluginValidationError(
                f"unsupported plugin protocol version: {payload.protocol_version}"
            )

        capabilities = set(manifest.capabilities)
        unsupported = capabilities - PLUGIN_CAPABILITIES - HIGH_TRUST_PLUGIN_CAPABILITIES
        if unsupported:
            raise PluginValidationError(
                f"unsupported plugin capabilities: {', '.join(sorted(unsupported))}"
            )
        endpoint = validate_plugin_endpoint(
            payload.endpoint,
            self.settings.plugin_endpoint_allowlist_values(),
            resolve=self.settings.plugin_resolve_endpoint,
        )
        checks = _base_checks()
        features: list[dict[str, object]] = []
        captcha_methods: list[dict[str, object]] = []
        if self.settings.plugin_verify_endpoint:
            verify_plugin_health(
                endpoint, token, self.settings.plugin_metadata_timeout
            )
            checks.append(_check("health", "健康检查", True, "gRPC 健康检查通过"))
            remote_manifest = fetch_plugin_manifest(
                endpoint, token, self.settings.plugin_metadata_timeout
            )
            if remote_manifest.model_dump() != manifest.model_dump():
                raise PluginValidationError(
                    "registered manifest does not match plugin manifest"
                )
            checks.append(_check("manifest", "Manifest 校验", True, "远端 manifest 一致"))
            if "features" in capabilities:
                features = fetch_plugin_features(
                    endpoint, token, self.settings.plugin_metadata_timeout
                )
                _validate_plugin_metadata(payload.plugin_id, features, [])
                checks.append(
                    _check("features", "功能定义", True, f"{len(features)} 个功能")
                )
            if "captcha_methods" in capabilities:
                captcha_methods = fetch_plugin_captcha_methods(
                    endpoint, token, self.settings.plugin_metadata_timeout
                )
                _validate_plugin_metadata(payload.plugin_id, [], captcha_methods)
                checks.append(
                    _check(
                        "captcha_methods",
                        "验证码方法",
                        True,
                        f"{len(captcha_methods)} 个验证码方法",
                    )
                )
        else:
            checks.append(
                _check(
                    "health",
                    "健康检查",
                    True,
                    "已跳过远端健康检查",
                    skipped=True,
                )
            )
            checks.append(
                _check(
                    "manifest",
                    "Manifest 校验",
                    True,
                    "已跳过远端 manifest 校验",
                    skipped=True,
                )
            )
            if "features" in capabilities:
                checks.append(
                    _check(
                        "features",
                        "功能定义",
                        True,
                        "已跳过远端功能定义读取",
                        skipped=True,
                    )
                )
            if "captcha_methods" in capabilities:
                checks.append(
                    _check(
                        "captcha_methods",
                        "验证码方法",
                        True,
                        "已跳过远端验证码方法读取",
                        skipped=True,
                    )
                )

        now = utcnow()
        row = self.session.get(PluginRegistration, payload.plugin_id)
        if row is None:
            row = PluginRegistration(
                plugin_id=payload.plugin_id,
                name=manifest.name,
                version=manifest.version,
                protocol_version=manifest.protocol_version,
                endpoint=endpoint,
                capabilities_json=json.dumps(sorted(capabilities)),
                manifest_json=json.dumps(
                    manifest.model_dump(), ensure_ascii=False, sort_keys=True
                ),
                checks_json=json.dumps(checks, ensure_ascii=False),
                features_json=json.dumps(features, ensure_ascii=False),
                captcha_methods_json=json.dumps(captcha_methods, ensure_ascii=False),
                approved_capabilities_json="[]",
                status="unapproved",
                generation=1,
                registered_at=now,
                last_heartbeat_at=now,
                updated_at=now,
            )
            self.session.add(row)
        else:
            self.session.refresh(row)
            self._require_not_removed(row)
            previous_capabilities = set(json.loads(row.capabilities_json))
            approved_capabilities = set(json.loads(row.approved_capabilities_json))
            if (previous_capabilities != capabilities
                or json.loads(row.manifest_json) != manifest.model_dump()
                or row.endpoint != endpoint
                or json.loads(row.features_json) != features
                 or json.loads(row.captcha_methods_json) != captcha_methods):
                from app.services.plugin_impacts import snapshot_change
                snapshot_change(self.session, row, features, capabilities, captcha_methods)
                row.permission_epoch += 1
                record(self.session, "metadata_change", plugin_id=row.plugin_id, reason="metadata_changed")
                approved_capabilities = set()
                row.approved_at = None
                row.approved_by = None
            row.name = manifest.name
            row.version = manifest.version
            row.protocol_version = manifest.protocol_version
            row.endpoint = endpoint
            row.capabilities_json = json.dumps(sorted(capabilities))
            row.manifest_json = json.dumps(
                manifest.model_dump(), ensure_ascii=False, sort_keys=True
            )
            row.checks_json = json.dumps(checks, ensure_ascii=False)
            row.features_json = json.dumps(features, ensure_ascii=False)
            row.captcha_methods_json = json.dumps(captcha_methods, ensure_ascii=False)
            row.approved_capabilities_json = json.dumps(
                sorted(approved_capabilities), ensure_ascii=False
            )
            if not row.enabled:
                row.status = "disabled"
            else:
                row.status = (
                    "available"
                    if row.approved_at is not None
                    and approved_capabilities == capabilities
                    else "unapproved"
                )
            row.generation += 1
            row.registered_at = now
            row.last_heartbeat_at = now
            row.updated_at = now
        record(self.session, "register", plugin_id=row.plugin_id)
        self.session.commit()
        refresh_plugin_registry(self.session)
        return self._view(row, now=now)

    def heartbeat(
        self, plugin_id: str, token: str, generation: int | None = None
    ) -> PluginView:
        self._authenticate(plugin_id, token)
        row = self.session.get(PluginRegistration, plugin_id)
        if row is None:
            raise PluginNotFoundError("plugin is not registered")
        if generation is not None and generation != row.generation:
            raise PluginGenerationConflict("plugin generation does not match")
        now = utcnow()
        if row.removed_at is not None:
            row.status = "removed"
        elif not row.enabled:
            row.status = "disabled"
        elif row.status not in TERMINAL_PLUGIN_STATUSES:
            row.status = "available"
        row.last_heartbeat_at = now
        row.updated_at = now
        self.session.commit()
        return self._view(row, now=now)

    def unregister(self, plugin_id: str, token: str, generation: int | None = None) -> None:
        self._authenticate(plugin_id, token)
        row = self.session.get(PluginRegistration, plugin_id)
        if row is None:
            raise PluginNotFoundError("plugin is not registered")
        if generation is not None and generation != row.generation:
            raise PluginGenerationConflict("plugin generation does not match")
        now = utcnow()
        row.last_heartbeat_at = now - timedelta(
            seconds=self.settings.plugin_heartbeat_ttl_seconds + 1
        )
        row.updated_at = now
        self.session.commit()
        refresh_plugin_registry(self.session)

    def approve(
        self,
        plugin_id: str,
        capabilities: list[str] | None,
        actor: str,
    ) -> PluginView:
        row = self.session.get(PluginRegistration, plugin_id)
        if row is None:
            raise PluginNotFoundError("plugin is not registered")
        self._require_not_removed(row)
        declared = set(json.loads(row.capabilities_json))
        requested = declared if capabilities is None else set(capabilities)
        invalid = requested - declared
        if invalid:
            raise PluginValidationError(
                f"plugin does not declare capabilities: {', '.join(sorted(invalid))}"
            )
        checks = json.loads(row.checks_json)
        failed = [
            str(item.get("label", item.get("key", "check")))
            for item in checks
            if not item.get("ok") and not item.get("skipped")
        ]
        if failed:
            raise PluginValidationError(
                f"plugin checks are not passed: {', '.join(failed)}"
            )
        now = utcnow()
        row.approved_capabilities_json = json.dumps(sorted(requested), ensure_ascii=False)
        row.approved_at = now
        row.approved_by = actor
        if not row.enabled:
            row.status = "disabled"
        else:
            row.status = "available" if requested == declared else "unapproved"
        row.updated_at = now
        row.permission_epoch += 1
        record(self.session, "approve", plugin_id=plugin_id, actor=actor, reason="admin")
        self.session.commit()
        refresh_plugin_registry(self.session)
        return self._view(row, now=now)

    def revoke(self, plugin_id: str, actor: str) -> PluginView:
        row = self.session.get(PluginRegistration, plugin_id)
        if row is None:
            raise PluginNotFoundError("plugin is not registered")
        self._require_not_removed(row)
        row.approved_capabilities_json = "[]"
        row.approved_at = None
        row.approved_by = None
        row.status = "unapproved" if row.enabled else "disabled"
        row.permission_epoch += 1
        row.updated_at = utcnow()
        record(self.session, "revoke", plugin_id=plugin_id, actor=actor, reason="admin")
        self.session.commit()
        refresh_plugin_registry(self.session)
        return self._view(row)

    def disable(self, plugin_id: str, actor: str | None = None) -> PluginView:
        row = self.session.get(PluginRegistration, plugin_id)
        if row is None:
            raise PluginNotFoundError("plugin is not registered")
        self._require_not_removed(row)
        row.enabled = False
        row.status = "disabled"
        row.permission_epoch += 1
        row.updated_at = utcnow()
        record(self.session, "disable", plugin_id=plugin_id, actor=actor, reason="admin")
        self.session.commit()
        refresh_plugin_registry(self.session)
        return self._view(row)

    def enable(self, plugin_id: str, actor: str | None = None) -> PluginView:
        row = self.session.get(PluginRegistration, plugin_id)
        if row is None:
            raise PluginNotFoundError("plugin is not registered")
        self._require_not_removed(row)
        row.enabled = True
        declared = set(json.loads(row.capabilities_json))
        approved = set(json.loads(row.approved_capabilities_json))
        row.status = (
            "available"
            if row.approved_at is not None and approved == declared
            else "unapproved"
        )
        row.updated_at = utcnow()
        record(self.session, "enable", plugin_id=plugin_id, actor=actor, reason="admin")
        self.session.commit()
        refresh_plugin_registry(self.session)
        return self._view(row)

    def check(self, plugin_id: str) -> PluginView:
        row = self.session.get(PluginRegistration, plugin_id)
        if row is None:
            raise PluginNotFoundError("plugin is not registered")
        self._require_not_removed(row)
        token = self._tokens().get(plugin_id)
        if token is None:
            raise PluginAuthenticationError("plugin token is not configured")
        checks = _base_checks()
        health_ok = True
        try:
            verify_plugin_health(
                row.endpoint, token, self.settings.plugin_metadata_timeout
            )
            checks.append(_check("health", "健康检查", True, "gRPC 健康检查通过"))
        except PluginUnreachableError as exc:
            health_ok = False
            checks.append(_check("health", "健康检查", False, str(exc)))
        manifest_ok = True
        if health_ok:
            try:
                remote_manifest = fetch_plugin_manifest(
                    row.endpoint, token, self.settings.plugin_metadata_timeout
                )
                stored = json.loads(row.manifest_json)
                if remote_manifest.model_dump() != stored:
                    manifest_ok = False
                    checks.append(
                        _check(
                            "manifest",
                            "Manifest 校验",
                            False,
                            "远端 manifest 与已注册内容不一致",
                        )
                    )
                else:
                    checks.append(
                        _check("manifest", "Manifest 校验", True, "远端 manifest 一致")
                    )
            except PluginUnreachableError as exc:
                manifest_ok = False
                checks.append(_check("manifest", "Manifest 校验", False, str(exc)))
        else:
            manifest_ok = False
            checks.append(
                _check(
                    "manifest",
                    "Manifest 校验",
                    False,
                    "健康检查失败，未执行 manifest 校验",
                )
            )
        capabilities = set(json.loads(row.capabilities_json))
        if health_ok and manifest_ok and "features" in capabilities:
            try:
                features = fetch_plugin_features(
                    row.endpoint, token, self.settings.plugin_metadata_timeout
                )
                _validate_plugin_metadata(plugin_id, features, [])
                if json.loads(row.features_json) != features:
                    from app.services.plugin_impacts import snapshot_change
                    snapshot_change(self.session, row, features, capabilities, json.loads(row.captcha_methods_json))
                    row.approved_capabilities_json = "[]"
                    row.approved_at = None
                    row.approved_by = None
                    row.permission_epoch += 1
                    row.status = "unapproved" if row.enabled else "disabled"
                row.features_json = json.dumps(features, ensure_ascii=False)
                checks.append(
                    _check("features", "功能定义", True, f"{len(features)} 个功能")
                )
            except (PluginUnreachableError, PluginValidationError) as exc:
                checks.append(_check("features", "功能定义", False, str(exc)))
        if health_ok and manifest_ok and "captcha_methods" in capabilities:
            try:
                methods = fetch_plugin_captcha_methods(
                    row.endpoint, token, self.settings.plugin_metadata_timeout
                )
                _validate_plugin_metadata(plugin_id, [], methods)
                if json.loads(row.captcha_methods_json) != methods:
                    from app.services.plugin_impacts import snapshot_change
                    snapshot_change(self.session, row, json.loads(row.features_json), capabilities, methods)
                    row.approved_capabilities_json = "[]"
                    row.approved_at = None
                    row.approved_by = None
                    row.permission_epoch += 1
                    row.status = "unapproved" if row.enabled else "disabled"
                row.captcha_methods_json = json.dumps(methods, ensure_ascii=False)
                checks.append(
                    _check(
                        "captcha_methods",
                        "验证码方法",
                        True,
                        f"{len(methods)} 个验证码方法",
                    )
                )
            except (PluginUnreachableError, PluginValidationError) as exc:
                checks.append(
                    _check("captcha_methods", "验证码方法", False, str(exc))
                )
        if health_ok and manifest_ok:
            checks.insert(0, _check("runtime", "运行时检查", True, "插件可连接"))
        else:
            checks.insert(0, _check("runtime", "运行时检查", False, "插件当前不可正常连接"))
        row.checks_json = json.dumps(checks, ensure_ascii=False)
        if not manifest_ok:
            row.approved_capabilities_json = "[]"
            row.approved_at = None
            row.approved_by = None
            row.permission_epoch += 1
            row.status = "unapproved" if row.enabled else "disabled"
        row.updated_at = utcnow()
        self.session.commit()
        refresh_plugin_registry(self.session)
        return self._view(row)

    def runtime_for_plugin(self, plugin_id: str) -> dict[str, object]:
        row = self.session.get(PluginRegistration, plugin_id)
        if row is None:
            raise PluginNotFoundError("plugin is not registered")
        self._require_not_removed(row)
        if not row.enabled:
            raise PluginValidationError(f"插件 {plugin_id} 已停止接入")
        capabilities = set(json.loads(row.capabilities_json))
        approved = set(json.loads(row.approved_capabilities_json))
        if row.approved_at is None or approved != capabilities:
            raise PluginValidationError(f"插件 {plugin_id} 尚未完成权限审批")
        token = self._tokens().get(plugin_id)
        if token is None:
            raise PluginAuthenticationError(f"插件 {plugin_id} 的 token 未配置")
        return {
            "plugin_id": plugin_id,
            "endpoint": row.endpoint,
            "token": token,
            "generation": row.generation,
            "permission_epoch": row.permission_epoch,
            "approved_capabilities": sorted(approved & capabilities),
        }

    def list_plugins(self) -> list[PluginView]:
        rows = self.session.scalars(
            select(PluginRegistration).order_by(PluginRegistration.plugin_id)
        ).all()
        now = utcnow()
        return [self._view(row, now=now) for row in rows]

    @staticmethod
    def _require_not_removed(row):
        if row is not None and row.removed_at is not None:
            raise PluginGenerationConflict("plugin integration removed; explicit admin restore required")

    def references(self, plugin_id):
        from app.services.plugin_impacts import references
        row = self.session.get(PluginRegistration, plugin_id)
        if row is None:
            raise PluginNotFoundError("plugin is not registered")
        return references(self.session, row)

    def change_impacts(self, plugin_id):
        row = self.session.get(PluginRegistration, plugin_id)
        if row is None:
            raise PluginNotFoundError("plugin is not registered")
        return json.loads(row.change_impacts_json)

    def remove(self, plugin_id, actor):
        row = self.session.get(PluginRegistration, plugin_id)
        if row is None:
            raise PluginNotFoundError("plugin is not registered")
        self.references(plugin_id)
        if row.removed_at is None:
            row.removed_at = utcnow()
            row.removed_by = actor
            row.enabled = False
            row.status = "removed"
            row.approved_at = None
            row.approved_by = None
            row.approved_capabilities_json = "[]"
            row.permission_epoch += 1
            row.updated_at = utcnow()
            record(self.session, "remove", plugin_id=plugin_id, actor=actor, reason="admin")
            self.session.commit()
            refresh_plugin_registry(self.session)
        return self._view(row)

    def restore(self, plugin_id, actor):
        row = self.session.get(PluginRegistration, plugin_id)
        if row is None:
            raise PluginNotFoundError("plugin is not registered")
        if row.removed_at is None:
            raise PluginGenerationConflict("plugin integration is not removed")
        row.removed_at = None
        row.removed_by = None
        row.enabled = True
        row.status = "unapproved"
        row.approved_at = None
        row.approved_by = None
        row.approved_capabilities_json = "[]"
        row.permission_epoch += 1
        row.updated_at = utcnow()
        record(self.session, "restore", plugin_id=plugin_id, actor=actor, reason="admin")
        self.session.commit()
        refresh_plugin_registry(self.session)
        return self._view(row)

    def get_plugin(self, plugin_id: str) -> PluginView:
        row = self.session.get(PluginRegistration, plugin_id)
        if row is None:
            raise PluginNotFoundError("plugin is not registered")
        return self._view(row)
