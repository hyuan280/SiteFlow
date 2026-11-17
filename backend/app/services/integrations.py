from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.crypto import CryptoError, CryptoService
from app.models.integration import IntegrationConfig, utcnow
from app.services.bitwarden import BitwardenService
from app.schemas.integration import (
    CFBoxView,
    ConfigSource,
    IntegrationsView,
    IntegrationTestResult,
    NumericView,
    ProxyView,
    TokenView,
    TwoFAuthView,
    ValueView,
)

PROXY_URL_KEY = "proxy.url"
TWOFAUTH_URL_KEY = "twofauth.url"
TWOFAUTH_TOKEN_KEY = "twofauth.token"
CFBOX_URL_KEY = "cfbox.url"
CFBOX_API_KEY_KEY = "cfbox.api_key"
MODEL_CONFIG_FIELDS = ("base_url", "model", "api_key", "supports_vision", "max_tokens")

PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}


class ConfigLockedError(RuntimeError):
    pass


class IntegrationValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ResolvedValue:
    value: str | None
    source: ConfigSource
    editable: bool
    row: IntegrationConfig | None = None


def validate_proxy_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme.lower() not in PROXY_SCHEMES:
        raise IntegrationValidationError(
            "proxy URL scheme must be http, https, socks5 or socks5h"
        )
    if not parsed.hostname:
        raise IntegrationValidationError("proxy URL must include a host")
    try:
        port = parsed.port
    except ValueError as exc:
        raise IntegrationValidationError(f"proxy URL has invalid port: {exc}") from exc
    if port is None:
        raise IntegrationValidationError("proxy URL must include a port")
    if not 1 <= port <= 65535:
        raise IntegrationValidationError("proxy URL port must be between 1 and 65535")
    if parsed.path not in ("", "/") or parsed.params or parsed.query or parsed.fragment:
        raise IntegrationValidationError("proxy URL must not include path, query or fragment")
    return value


def validate_twofauth_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise IntegrationValidationError("2FAuth URL must use http or https")
    if not parsed.hostname:
        raise IntegrationValidationError("2FAuth URL must include a host")
    return value.rstrip("/")


def validate_cfbox_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise IntegrationValidationError("CFbox URL must use http or https")
    if not parsed.hostname:
        raise IntegrationValidationError("CFbox URL must include a host")
    return value.rstrip("/")


def mask_proxy_url(value: str) -> str:
    parsed = urlparse(value)
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    if parsed.password is not None:
        user = parsed.username or ""
        return f"{parsed.scheme}://{user}:***@{host}"
    if parsed.username:
        return f"{parsed.scheme}://{parsed.username}@{host}"
    return f"{parsed.scheme}://{host}"


class IntegrationService:
    def model_env_configured(self) -> bool:
        for field in MODEL_CONFIG_FIELDS:
            value = getattr(self.settings, f"vision_{field}")
            if hasattr(value, "get_secret_value"):
                value = value.get_secret_value()
            if value is not None and (not isinstance(value, str) or value.strip()):
                return True
        return False

    def resolve_vision(self, field: str) -> ResolvedValue:
        value = getattr(self.settings, f"vision_{field}")
        if field == "max_tokens":
            return self._resolve(str(value) if value is not None else None, "vision.max_tokens")
        if hasattr(value, "get_secret_value"):
            value = value.get_secret_value()
        return self._resolve((value or "").strip() or None, f"vision.{field}")

    def resolve_vision_supports_vision(self) -> ResolvedValue:
        env_value = self.settings.vision_supports_vision
        if env_value is not None:
            return ResolvedValue(
                value="true" if env_value else "false",
                source=ConfigSource.ENV,
                editable=False,
            )
        row = self._row("vision.supports_vision")
        if row is None:
            return ResolvedValue(value="true", source=ConfigSource.UNSET, editable=True)
        return ResolvedValue(
            value=(self._decrypt_row("vision.supports_vision", row) or "").strip().lower(),
            source=ConfigSource.DB,
            editable=True,
            row=row,
        )

    def vision_runtime(self) -> dict | None:
        from app.services.vision import validate_base_url
        max_tokens = self._vision_max_tokens_view()
        if max_tokens.error:
            return None
        values = {field: self.resolve_vision(field).value for field in ("base_url", "model", "api_key")}
        if not all(values.values()):
            return None
        try:
            values["base_url"] = validate_base_url(values["base_url"])
        except ValueError:
            return None
        supports = self.resolve_vision_supports_vision().value == "true"
        runtime = {**values, "supports_vision": supports, "timeout": self.settings.vision_timeout}
        if max_tokens.value:
            runtime["max_tokens"] = max_tokens.value
        return runtime

    def model_runtime(self, usage, *, requires_vision=False) -> dict | None:
        from app.services.model_registry import ModelRegistryService
        return ModelRegistryService(self.session, self.settings, self.crypto).select_runtime(
            usage, requires_vision=requires_vision,
        )

    def _vision_max_tokens_view(self) -> NumericView:
        source = (
            ConfigSource.ENV if self.settings.vision_max_tokens is not None
            else ConfigSource.DB if self._row("vision.max_tokens") is not None
            else ConfigSource.UNSET
        )
        value = None
        error = None
        try:
            raw = self.resolve_vision("max_tokens").value
            if source != ConfigSource.UNSET:
                if not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal():
                    raise ValueError("invalid max_tokens")
                value = int(raw)
        except (IntegrationValidationError, ValueError, TypeError):
            error = "Vision max_tokens is invalid or could not be decrypted"
        return NumericView(
            value=value, source=source, editable=source != ConfigSource.ENV, error=error
        )

    def _vision_view(self):
        from app.schemas.integration import BooleanView, VisionView
        from app.services.vision import validate_base_url
        values = {}
        for field in ("base_url", "model", "api_key"):
            resolved = self.resolve_vision(field)
            if field == "api_key":
                values[field] = TokenView(configured=bool(resolved.value), source=resolved.source,
                    editable=resolved.editable, masked=f"****{resolved.row.fingerprint}" if resolved.row else None)
            else:
                error = None
                if field == "base_url" and resolved.value:
                    try:
                        validate_base_url(resolved.value)
                    except ValueError as exc:
                        error = str(exc)
                values[field] = ValueView(value=resolved.value if not error else None,
                    source=resolved.source, editable=resolved.editable, error=error)
        supports = self.resolve_vision_supports_vision()
        values["supports_vision"] = BooleanView(
            value=supports.value == "true",
            source=supports.source,
            editable=supports.editable,
        )
        values["max_tokens"] = self._vision_max_tokens_view()
        if self.model_env_configured():
            for value in values.values():
                value.editable = False
        return VisionView(**values, available=self.vision_runtime() is not None)

    def update_vision(self, *, _commit=True, **fields) -> IntegrationsView | None:
        from app.services.vision import validate_base_url
        try:
            if fields and self.model_env_configured():
                raise ConfigLockedError("Model configuration is locked by environment")
            if _commit:
                from app.services.model_registry import ModelRegistryService
                ModelRegistryService(self.session, self.settings, self.crypto).touch_legacy(add_primary=True)
            pending = {}
            for field, value in fields.items():
                if value is None and field != "max_tokens":
                    continue
                resolved = (
                    self._vision_max_tokens_view()
                    if field == "max_tokens"
                    else self.resolve_vision_supports_vision()
                    if field == "supports_vision"
                    else self.resolve_vision(field)
                )
                if not resolved.editable:
                    raise ConfigLockedError(f"Vision {field} is locked by environment")
                if field == "max_tokens":
                    if value is None or (isinstance(value, str) and not value.strip()):
                        value = 0
                    if type(value) is not int or value < 0:
                        raise IntegrationValidationError("Vision max_tokens must be a nonnegative integer")
                    pending[field] = str(value)
                    continue
                if field == "supports_vision":
                    pending[field] = "true" if bool(value) else "false"
                    continue
                value = value.strip()
                if not value:
                    raise IntegrationValidationError(f"Vision {field} must not be empty")
                if field == "base_url":
                    try:
                        value = validate_base_url(value)
                    except ValueError as exc:
                        raise IntegrationValidationError(str(exc)) from None
                pending[field] = value
            for field, value in pending.items():
                self._upsert(f"vision.{field}", value, "api")
            if _commit:
                self.session.commit()
                return self.get_integrations()
        except Exception:
            if _commit:
                self.session.rollback()
            raise

    def delete_vision(self) -> IntegrationsView:
        try:
            if self.model_env_configured():
                raise ConfigLockedError("Model configuration is locked by environment")
            from app.services.model_registry import ModelRegistryService
            ModelRegistryService(self.session, self.settings, self.crypto).touch_legacy()
            self._delete_vision_config()
            self.session.commit()
            return self.get_integrations()
        except Exception:
            self.session.rollback()
            raise

    def _delete_vision_config(self) -> None:
        for field in MODEL_CONFIG_FIELDS:
            self._delete(f"vision.{field}")

    def test_vision(self, *, _config=...) -> IntegrationTestResult:
        import base64
        from app.services.vision import recognize, test_text
        config = self.vision_runtime() if _config is ... else _config
        if not config:
            return IntegrationTestResult(ok=False, message="Vision is not configured or invalid")
        if not config.get("supports_vision", True):
            try:
                test_text(config)
                return IntegrationTestResult(
                    ok=True,
                    message="文本请求成功（未启用视觉识别）",
                    data={"mode": "text", "supports_vision": False},
                )
            except ValueError:
                return IntegrationTestResult(
                    ok=False,
                    message="文本请求失败",
                    data={"mode": "text", "supports_vision": False},
                )
        image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC")
        try:
            recognize(image, config, "Describe this image in a few words.")
            return IntegrationTestResult(ok=True, message="Vision image request succeeded")
        except ValueError:
            return IntegrationTestResult(ok=False, message="Vision image request failed or returned invalid output")

    def __init__(self, session: Session, settings: Settings, crypto: CryptoService) -> None:
        self.session = session
        self.settings = settings
        self.crypto = crypto

    # ── resolution ──────────────────────────────────────────────────────

    def _row(self, key: str) -> IntegrationConfig | None:
        return self.session.get(IntegrationConfig, key)

    def _decrypt_row(self, key: str, row: IntegrationConfig | None) -> str | None:
        if row is None:
            return None
        try:
            return self.crypto.decrypt(key, row.value_ciphertext, row.nonce, row.key_version)
        except CryptoError as exc:
            raise IntegrationValidationError(f"{key} 解密失败: {exc}") from exc

    def _resolve(
        self,
        env_value: str | None,
        key: str,
    ) -> ResolvedValue:
        if env_value:
            return ResolvedValue(value=env_value, source=ConfigSource.ENV, editable=False)
        row = self._row(key)
        if row is None:
            return ResolvedValue(value=None, source=ConfigSource.UNSET, editable=True)
        return ResolvedValue(
            value=self._decrypt_row(key, row),
            source=ConfigSource.DB,
            editable=True,
            row=row,
        )

    def resolve_proxy(self) -> ResolvedValue:
        return self._resolve(self.settings.proxy_url_value(), PROXY_URL_KEY)

    def resolve_twofauth_url(self) -> ResolvedValue:
        return self._resolve(self.settings.twofauth_url_value(), TWOFAUTH_URL_KEY)

    def resolve_twofauth_token(self) -> ResolvedValue:
        return self._resolve(self.settings.twofauth_token_value(), TWOFAUTH_TOKEN_KEY)

    def resolve_cfbox_url(self) -> ResolvedValue:
        return self._resolve(self.settings.cfbox_url_value(), CFBOX_URL_KEY)

    def resolve_cfbox_api_key(self) -> ResolvedValue:
        return self._resolve(self.settings.cfbox_api_key_value(), CFBOX_API_KEY_KEY)

    # ── views ───────────────────────────────────────────────────────────

    def get_integrations(self) -> IntegrationsView:
        proxy = self._proxy_view()
        twofauth = self._twofauth_view()
        cfbox = self._cfbox_view()
        bitwarden = BitwardenService(self.session, self.settings, self.crypto).view()
        return IntegrationsView(
            vision=self._vision_view(),
            proxy=proxy,
            twofauth=twofauth,
            cfbox=cfbox,
            bitwarden=bitwarden,
        )

    def _proxy_view(self) -> ProxyView:
        resolved = self.resolve_proxy()
        if resolved.value is None:
            return ProxyView(
                enabled=False,
                source=ConfigSource.UNSET,
                editable=True,
                error="代理未配置",
            )
        try:
            validate_proxy_url(resolved.value)
        except IntegrationValidationError as exc:
            return ProxyView(
                enabled=False,
                source=resolved.source,
                editable=resolved.editable,
                error=str(exc),
            )
        parsed = urlparse(resolved.value)
        return ProxyView(
            enabled=True,
            source=resolved.source,
            editable=resolved.editable,
            protocol=parsed.scheme.lower(),
            url_masked=mask_proxy_url(resolved.value),
        )

    def _twofauth_view(self) -> TwoFAuthView:
        url_value = self.resolve_twofauth_url()
        token_value = self.resolve_twofauth_token()
        errors: list[str] = []

        url_view = ValueView(
            value=url_value.value,
            source=url_value.source,
            editable=url_value.editable,
        )
        if url_value.value is None:
            errors.append("2FAuth URL 未配置")
        else:
            try:
                validate_twofauth_url(url_value.value)
            except IntegrationValidationError as exc:
                url_view.error = str(exc)
                errors.append(str(exc))

        token_configured = token_value.value is not None
        token_view = TokenView(
            configured=token_configured,
            source=token_value.source,
            editable=token_value.editable,
            masked=(
                f"****{token_value.row.fingerprint}"
                if token_value.source == ConfigSource.DB and token_value.row is not None
                else None
            ),
        )
        if not token_configured:
            errors.append("2FAuth Token 未配置")

        return TwoFAuthView(
            enabled=not errors,
            url=url_view,
            token=token_view,
            error="；".join(errors) if errors else None,
        )

    def _cfbox_view(self) -> CFBoxView:
        url_value = self.resolve_cfbox_url()
        api_key_value = self.resolve_cfbox_api_key()
        errors: list[str] = []

        url_view = ValueView(
            value=url_value.value,
            source=url_value.source,
            editable=url_value.editable,
        )
        if url_value.value is None:
            errors.append("CFbox URL 未配置")
        else:
            try:
                validate_cfbox_url(url_value.value)
            except IntegrationValidationError as exc:
                url_view.error = str(exc)
                errors.append(str(exc))

        api_key_configured = api_key_value.value is not None
        api_key_view = TokenView(
            configured=api_key_configured,
            source=api_key_value.source,
            editable=api_key_value.editable,
            masked=(
                f"****{api_key_value.row.fingerprint}"
                if api_key_value.source == ConfigSource.DB and api_key_value.row is not None
                else None
            ),
        )
        return CFBoxView(
            enabled=not errors,
            url=url_view,
            api_key=api_key_view,
            error="；".join(errors) if errors else None,
        )

    def cfbox_enabled(self) -> bool:
        return self._cfbox_view().enabled

    # ── mutations ───────────────────────────────────────────────────────

    def update_proxy(self, url: str, updated_by: str = "api") -> IntegrationsView:
        if self.settings.proxy_url_value():
            raise ConfigLockedError("代理 URL 已由 SITEFLOW_PROXY_URL 环境变量锁定")
        value = validate_proxy_url(url.strip())
        self._upsert(PROXY_URL_KEY, value, updated_by)
        self.session.commit()
        return self.get_integrations()

    def delete_proxy(self, updated_by: str = "api") -> IntegrationsView:
        if self.settings.proxy_url_value():
            raise ConfigLockedError("代理 URL 已由 SITEFLOW_PROXY_URL 环境变量锁定")
        self._delete(PROXY_URL_KEY)
        self.session.commit()
        return self.get_integrations()

    def update_twofauth(
        self,
        *,
        url: str | None = None,
        token: str | None = None,
        updated_by: str = "api",
    ) -> IntegrationsView:
        if url is not None:
            if self.settings.twofauth_url_value():
                raise ConfigLockedError("2FAuth URL 已由 SITEFLOW_TWOFAUTH_URL 环境变量锁定")
            value = validate_twofauth_url(url.strip())
            self._upsert(TWOFAUTH_URL_KEY, value, updated_by)
        if token is not None:
            if self.settings.twofauth_token_value():
                raise ConfigLockedError("2FAuth Token 已由 SITEFLOW_TWOFAUTH_TOKEN 环境变量锁定")
            value = token.strip()
            if not value:
                raise IntegrationValidationError("2FAuth Token 不能为空")
            self._upsert(TWOFAUTH_TOKEN_KEY, value, updated_by)
        self.session.commit()
        return self.get_integrations()

    def delete_twofauth(self, updated_by: str = "api") -> IntegrationsView:
        if self.settings.twofauth_url_value() or self.settings.twofauth_token_value():
            raise ConfigLockedError("2FAuth 配置已由环境变量锁定")
        self._delete(TWOFAUTH_URL_KEY)
        self._delete(TWOFAUTH_TOKEN_KEY)
        self.session.commit()
        return self.get_integrations()

    def update_cfbox(
        self,
        *,
        url: str | None = None,
        api_key: str | None = None,
        updated_by: str = "api",
    ) -> IntegrationsView:
        if url is not None:
            if self.settings.cfbox_url_value():
                raise ConfigLockedError("CFbox URL 已由 SITEFLOW_CFBOX_URL 环境变量锁定")
            value = validate_cfbox_url(url.strip())
            self._upsert(CFBOX_URL_KEY, value, updated_by)
        if api_key is not None:
            if self.settings.cfbox_api_key_value():
                raise ConfigLockedError("CFbox API Key 已由 SITEFLOW_CFBOX_API_KEY 环境变量锁定")
            value = api_key.strip()
            if value:
                self._upsert(CFBOX_API_KEY_KEY, value, updated_by)
            else:
                self._delete(CFBOX_API_KEY_KEY)
        self.session.commit()
        return self.get_integrations()

    def delete_cfbox(self, updated_by: str = "api") -> IntegrationsView:
        if self.settings.cfbox_url_value() or self.settings.cfbox_api_key_value():
            raise ConfigLockedError("CFbox 配置已由环境变量锁定")
        self._delete(CFBOX_URL_KEY)
        self._delete(CFBOX_API_KEY_KEY)
        self.session.commit()
        return self.get_integrations()

    def _upsert(self, key: str, value: str, updated_by: str) -> None:
        encrypted = self.crypto.encrypt(key, value)
        row = self._row(key)
        if row is None:
            row = IntegrationConfig(key=key)
            self.session.add(row)
        row.value_ciphertext = encrypted.ciphertext
        row.nonce = encrypted.nonce
        row.key_version = encrypted.key_version
        row.is_secret = True
        row.fingerprint = self.crypto.fingerprint(value)
        row.updated_at = utcnow()
        row.updated_by = updated_by

    def _delete(self, key: str) -> None:
        row = self._row(key)
        if row is not None:
            self.session.delete(row)

    # ── connectivity tests ──────────────────────────────────────────────

    def test_proxy(self) -> IntegrationTestResult:
        resolved = self.resolve_proxy()
        if resolved.value is None:
            return IntegrationTestResult(ok=False, message="代理未配置")
        try:
            validate_proxy_url(resolved.value)
        except IntegrationValidationError as exc:
            return IntegrationTestResult(ok=False, message=str(exc))
        proxy = resolved.value
        if proxy.startswith("socks5h://"):
            proxy = "socks5://" + proxy[len("socks5h://"):]
        started = time.monotonic()
        try:
            with httpx.Client(proxy=proxy, timeout=self.settings.request_timeout) as client:
                response = client.get("https://api.ipify.org?format=json")
                response.raise_for_status()
                payload = response.json()
            return IntegrationTestResult(
                ok=True,
                message="代理连接成功",
                data={
                    "ip": payload.get("ip"),
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                    "protocol": urlparse(resolved.value).scheme,
                },
            )
        except Exception as exc:
            return IntegrationTestResult(
                ok=False,
                message=f"代理连接失败: {type(exc).__name__}: {str(exc)[:200]}",
            )

    def test_twofauth(self) -> IntegrationTestResult:
        url_value = self.resolve_twofauth_url()
        token_value = self.resolve_twofauth_token()
        if not url_value.value or not token_value.value:
            return IntegrationTestResult(ok=False, message="2FAuth URL 或 Token 未配置")
        try:
            base = validate_twofauth_url(url_value.value)
        except IntegrationValidationError as exc:
            return IntegrationTestResult(ok=False, message=str(exc))
        try:
            with httpx.Client(timeout=self.settings.request_timeout) as client:
                response = client.get(
                    f"{base}/api/v1/twofaccounts",
                    headers={"Authorization": f"Bearer {token_value.value}"},
                )
                if response.status_code != 200:
                    return IntegrationTestResult(
                        ok=False,
                        message=f"2FAuth 返回 HTTP {response.status_code}",
                    )
                payload = response.json()
            accounts = payload if isinstance(payload, list) else []
            services = sorted({item.get("service") for item in accounts if item.get("service")})
            return IntegrationTestResult(
                ok=True,
                message="2FAuth 连接成功",
                data={"account_count": len(accounts), "services": services},
            )
        except Exception as exc:
            return IntegrationTestResult(
                ok=False,
                message=f"2FAuth 连接失败: {type(exc).__name__}: {str(exc)[:200]}",
            )


    def test_cfbox(self) -> IntegrationTestResult:
        url_value = self.resolve_cfbox_url()
        api_key_value = self.resolve_cfbox_api_key()
        if not url_value.value:
            return IntegrationTestResult(ok=False, message="CFbox URL 未配置")
        try:
            base = validate_cfbox_url(url_value.value)
        except IntegrationValidationError as exc:
            return IntegrationTestResult(ok=False, message=str(exc))
        try:
            headers = {"X-API-Key": api_key_value.value} if api_key_value.value else {}
            with httpx.Client(timeout=self.settings.request_timeout) as client:
                response = client.post(
                    f"{base}/predict",
                    headers=headers,
                    files={"image": ("empty.png", b"", "image/png")},
                )
            if response.status_code in {401, 403}:
                message = "CFbox API Key 无效" if api_key_value.value else "CFbox 需要 API Key"
                return IntegrationTestResult(ok=False, message=message)
            if response.status_code in {200, 400}:
                return IntegrationTestResult(
                    ok=True,
                    message="CFbox 连接成功",
                    data={"status_code": response.status_code},
                )
            return IntegrationTestResult(
                ok=False,
                message=f"CFbox 返回 HTTP {response.status_code}",
            )
        except Exception as exc:
            return IntegrationTestResult(
                ok=False,
                message=f"CFbox 连接失败: {type(exc).__name__}: {str(exc)[:200]}",
            )
