from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.crypto import CryptoError, CryptoService
from app.models.integration import IntegrationConfig, utcnow
from app.schemas.integration import (
    BitwardenView,
    ConfigSource,
    IntegrationTestResult,
    TokenView,
    ValueView,
)

DEFAULT_BITWARDEN_URL = "http://bitwarden:8090"
BITWARDEN_URL_KEY = "bitwarden.url"
BITWARDEN_TOKEN_KEY = "bitwarden.token"
BITWARDEN_ENABLED_KEY = "bitwarden.enabled"
BITWARDEN_MATCH_MODES = {"domain", "root_domain", "custom"}

_HEALTH_CACHE: dict[tuple[str, float], tuple[float, "BitwardenHealth"]] = {}
_HEALTH_LOCK = threading.Lock()
_HEALTH_CACHE_SECONDS = 15.0


class BitwardenError(RuntimeError):
    pass


class BitwardenConfigLockedError(RuntimeError):
    pass


class BitwardenValidationError(BitwardenError, ValueError):
    pass


@dataclass(frozen=True)
class BitwardenHealth:
    ok: bool
    configured: bool
    unlocked: bool
    version: str | None
    user_email: str | None
    message: str | None


@dataclass(frozen=True)
class BitwardenConfig:
    url: str | None
    url_source: ConfigSource
    url_editable: bool
    token: str | None
    token_source: ConfigSource
    token_editable: bool
    enabled_setting: bool
    enabled_source: ConfigSource
    enabled_editable: bool


@dataclass(frozen=True)
class BitwardenSecret:
    username: str
    password: str
    item_name: str | None = None
    matched_by: str | None = None


def clear_health_cache() -> None:
    with _HEALTH_LOCK:
        _HEALTH_CACHE.clear()


def validate_bitwarden_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise BitwardenValidationError("Bitwarden URL 必须使用 http 或 https")
    if not parsed.hostname:
        raise BitwardenValidationError("Bitwarden URL 必须包含主机名")
    if parsed.query or parsed.fragment:
        raise BitwardenValidationError("Bitwarden URL 不能包含 query 或 fragment")
    return value.rstrip("/")


class BitwardenService:
    def __init__(self, session: Session, settings: Settings, crypto: CryptoService) -> None:
        self.session = session
        self.settings = settings
        self.crypto = crypto

    # ── configuration ──────────────────────────────────────────────────

    def _row(self, key: str) -> IntegrationConfig | None:
        return self.session.get(IntegrationConfig, key)

    def _decrypt_row(self, key: str, row: IntegrationConfig) -> str:
        try:
            return self.crypto.decrypt(key, row.value_ciphertext, row.nonce, row.key_version)
        except CryptoError as exc:
            raise BitwardenValidationError(f"{key} 解密失败: {exc}") from exc

    def _db_value(self, key: str) -> tuple[str | None, IntegrationConfig | None]:
        row = self._row(key)
        if row is None:
            return None, None
        return self._decrypt_row(key, row), row

    def _resolve_config(self) -> BitwardenConfig:
        env_url = (self.settings.bitwarden_url_value() or "").rstrip("/") or None
        env_token = self.settings.bitwarden_token_value()
        env_enabled = self.settings.bitwarden_enabled
        db_url, _ = self._db_value(BITWARDEN_URL_KEY)
        db_token, _ = self._db_value(BITWARDEN_TOKEN_KEY)
        db_enabled_raw, _ = self._db_value(BITWARDEN_ENABLED_KEY)
        db_enabled: bool | None = None
        if db_enabled_raw is not None:
            db_enabled = db_enabled_raw.strip().lower() == "true"

        if env_url:
            url, url_source = env_url, ConfigSource.ENV
        elif db_url:
            url, url_source = db_url, ConfigSource.DB
        elif self.settings.bitwarden_auto_detect:
            url, url_source = DEFAULT_BITWARDEN_URL, ConfigSource.AUTO
        else:
            url, url_source = None, ConfigSource.UNSET

        if env_token:
            token, token_source = env_token, ConfigSource.ENV
        elif db_token:
            token, token_source = db_token, ConfigSource.DB
        else:
            token, token_source = None, ConfigSource.UNSET

        if env_enabled is not None:
            enabled, enabled_source = env_enabled, ConfigSource.ENV
        elif db_enabled is not None:
            enabled, enabled_source = db_enabled, ConfigSource.DB
        elif self.settings.bitwarden_auto_detect and url is not None:
            enabled, enabled_source = False, ConfigSource.AUTO
        else:
            enabled, enabled_source = False, ConfigSource.UNSET

        return BitwardenConfig(
            url=url,
            url_source=url_source,
            url_editable=env_url is None,
            token=token,
            token_source=token_source,
            token_editable=env_token is None,
            enabled_setting=enabled,
            enabled_source=enabled_source,
            enabled_editable=env_enabled is None,
        )

    def _token_view(self, config: BitwardenConfig) -> TokenView:
        row = self._row(BITWARDEN_TOKEN_KEY) if config.token_source == ConfigSource.DB else None
        return TokenView(
            configured=config.token is not None,
            source=config.token_source,
            editable=config.token_editable,
            masked=f"****{row.fingerprint}" if row is not None else None,
        )

    # ── health ─────────────────────────────────────────────────────────

    def _probe(
        self,
        url: str,
        force: bool = False,
        deep: bool = False,
        token: str | None = None,
    ) -> BitwardenHealth:
        key = (url, round(self.settings.bitwarden_probe_timeout, 2))
        now = time.monotonic()
        if not force and not deep:
            with _HEALTH_LOCK:
                cached = _HEALTH_CACHE.get(key)
            if cached is not None and cached[0] > now:
                return cached[1]

        health = self._request_health(url, deep=deep, token=token)
        if not deep:
            with _HEALTH_LOCK:
                _HEALTH_CACHE[key] = (now + _HEALTH_CACHE_SECONDS, health)
        return health

    def _request_health(
        self,
        url: str,
        deep: bool = False,
        token: str | None = None,
    ) -> BitwardenHealth:
        try:
            timeout = (
                self.settings.bitwarden_timeout
                if deep
                else self.settings.bitwarden_probe_timeout
            )
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            with httpx.Client(timeout=timeout) as client:
                if deep:
                    response = client.post(f"{url}/check", headers=headers)
                else:
                    response = client.get(f"{url}/health")
            if response.status_code != 200:
                return BitwardenHealth(
                    ok=False,
                    configured=False,
                    unlocked=False,
                    version=None,
                    user_email=None,
                    message=f"Bitwarden 服务返回 HTTP {response.status_code}",
                )
            payload = response.json()
            if not isinstance(payload, dict):
                payload = {}
            raw_configured = bool(payload.get("configured", True))
            ready = bool(payload.get("ready", True))
            configured = raw_configured and ready
            if not raw_configured:
                message = "Bitwarden 容器已连接，但未配置 BW_CLIENTID、BW_CLIENTSECRET 和 BW_PASSWORD"
            elif not ready:
                message = str(payload.get("error") or "Bitwarden 登录或解锁失败")
            else:
                message = None
            return BitwardenHealth(
                ok=str(payload.get("status", "ok")) == "ok",
                configured=configured,
                unlocked=bool(payload.get("unlocked", False)),
                version=str(payload["version"]) if payload.get("version") else None,
                user_email=str(payload["user_email"]) if payload.get("user_email") else None,
                message=message,
            )
        except Exception as exc:
            return BitwardenHealth(
                ok=False,
                configured=False,
                unlocked=False,
                version=None,
                user_email=None,
                message=f"无法连接 Bitwarden 服务: {type(exc).__name__}: {str(exc)[:200]}",
            )

    # ── views ──────────────────────────────────────────────────────────

    def view(self, force: bool = False, deep: bool = False) -> BitwardenView:
        config = self._resolve_config()
        url_view = ValueView(
            value=config.url,
            source=config.url_source,
            editable=config.url_editable,
        )
        token_view = self._token_view(config)

        health = BitwardenHealth(False, False, False, None, None, None)
        enabled_setting = config.enabled_setting
        if config.url is not None and (
            config.enabled_source == ConfigSource.AUTO
            or (config.enabled_source in {ConfigSource.ENV, ConfigSource.DB} and config.enabled_setting)
        ):
            health = self._probe(
                config.url, force=force, deep=deep, token=config.token
            )
            if config.enabled_source == ConfigSource.AUTO:
                enabled_setting = health.ok and health.configured

        effective = enabled_setting and health.ok and health.configured
        error: str | None = None
        if config.url is None:
            error = "未启用自动检测，且未配置 Bitwarden URL"
        elif config.enabled_source == ConfigSource.UNSET:
            error = "Bitwarden 凭据集成未启用"
        elif config.enabled_source in {ConfigSource.ENV, ConfigSource.DB} and config.enabled_setting:
            if not health.ok:
                error = health.message or "Bitwarden 服务不可用"
            elif not health.configured:
                error = health.message or "Bitwarden 服务已连接，但尚未配置保险库凭据"
        elif config.enabled_source == ConfigSource.AUTO and not effective:
            error = health.message or "未检测到可用的 Bitwarden 服务"

        sources = {config.url_source, config.token_source, config.enabled_source}
        if ConfigSource.ENV in sources:
            overall_source = ConfigSource.ENV
        elif ConfigSource.DB in sources:
            overall_source = ConfigSource.DB
        elif ConfigSource.AUTO in sources:
            overall_source = ConfigSource.AUTO
        else:
            overall_source = ConfigSource.UNSET

        return BitwardenView(
            enabled=effective,
            enabled_setting=enabled_setting,
            enabled_source=config.enabled_source,
            available=health.ok,
            configured=health.configured,
            auto_detected=config.url_source == ConfigSource.AUTO,
            source=overall_source,
            url=url_view,
            token=token_view,
            default_url=DEFAULT_BITWARDEN_URL,
            server_version=health.version,
            user_email=health.user_email,
            error=error,
        )

    # ── mutations ──────────────────────────────────────────────────────

    def update(
        self,
        *,
        url: str | None = None,
        token: str | None = None,
        enabled: bool | None = None,
        updated_by: str = "api",
    ) -> BitwardenView:
        if url is not None:
            if self.settings.bitwarden_url_value():
                raise BitwardenConfigLockedError(
                    "Bitwarden URL 已由 SITEFLOW_BITWARDEN_URL 环境变量锁定"
                )
            value = url.strip()
            if value:
                self._upsert(BITWARDEN_URL_KEY, validate_bitwarden_url(value), updated_by)
            else:
                self._delete(BITWARDEN_URL_KEY)
        if token is not None:
            if self.settings.bitwarden_token_value():
                raise BitwardenConfigLockedError(
                    "Bitwarden Token 已由 SITEFLOW_BITWARDEN_TOKEN 环境变量锁定"
                )
            value = token.strip()
            if value:
                self._upsert(BITWARDEN_TOKEN_KEY, value, updated_by)
            else:
                self._delete(BITWARDEN_TOKEN_KEY)
        if enabled is not None:
            if self.settings.bitwarden_enabled is not None:
                raise BitwardenConfigLockedError(
                    "Bitwarden 启用状态已由 SITEFLOW_BITWARDEN_ENABLED 环境变量锁定"
                )
            self._upsert(BITWARDEN_ENABLED_KEY, "true" if enabled else "false", updated_by)
        self.session.commit()
        clear_health_cache()
        return self.view(force=True)

    def delete(self, updated_by: str = "api") -> BitwardenView:
        if (
            self.settings.bitwarden_url_value()
            or self.settings.bitwarden_token_value()
            or self.settings.bitwarden_enabled is not None
        ):
            raise BitwardenConfigLockedError("Bitwarden 配置已由环境变量锁定")
        self._delete(BITWARDEN_URL_KEY)
        self._delete(BITWARDEN_TOKEN_KEY)
        self._delete(BITWARDEN_ENABLED_KEY)
        self.session.commit()
        clear_health_cache()
        return self.view(force=True)

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

    # ── lookup ─────────────────────────────────────────────────────────

    def lookup(self, mode: str, host: str, custom: str | None = None) -> BitwardenSecret:
        if mode not in BITWARDEN_MATCH_MODES:
            raise BitwardenValidationError(f"未知的 Bitwarden 匹配方式: {mode}")
        if mode == "custom" and not (custom or "").strip():
            raise BitwardenValidationError("Bitwarden 自定义匹配值不能为空")
        view = self.view()
        config = self._resolve_config()
        if not view.enabled or config.url is None:
            raise BitwardenError(view.error or "Bitwarden 凭据集成未启用")
        body: dict[str, object] = {"mode": mode, "host": host}
        if mode == "custom":
            body["custom"] = (custom or "").strip()
        headers = {"Authorization": f"Bearer {config.token}"} if config.token else {}
        try:
            with httpx.Client(timeout=self.settings.bitwarden_timeout) as client:
                response = client.post(
                    f"{config.url}/lookup",
                    json=body,
                    headers=headers,
                )
        except Exception as exc:
            raise BitwardenError(
                f"Bitwarden 查询失败: {type(exc).__name__}: {str(exc)[:200]}"
            ) from exc
        if response.status_code == 401:
            raise BitwardenError("Bitwarden Token 无效")
        if response.status_code == 404:
            raise BitwardenError("Bitwarden 中没有找到匹配的登录条目")
        if response.status_code != 200:
            detail = ""
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    detail = str(payload.get("detail") or payload.get("error") or "")
            except Exception:
                detail = response.text[:200]
            raise BitwardenError(
                f"Bitwarden 查询失败: HTTP {response.status_code}"
                + (f" · {detail}" if detail else "")
            )
        try:
            payload = response.json()
        except Exception as exc:
            raise BitwardenError("Bitwarden 返回了无效 JSON") from exc
        username = str(payload.get("username") or "") if isinstance(payload, dict) else ""
        password = str(payload.get("password") or "") if isinstance(payload, dict) else ""
        if not username:
            raise BitwardenError("Bitwarden 匹配条目缺少用户名")
        return BitwardenSecret(
            username=username,
            password=password,
            item_name=str(payload.get("name") or "") or None,
            matched_by=str(payload.get("matched_by") or "") or None,
        )

    @staticmethod
    def _site_lookup_request(site: object) -> tuple[str, str, str | None]:
        base_url = str(getattr(site, "base_url", "") or "")
        host = (urlparse(base_url).hostname or "").strip().lower()
        if not host:
            raise BitwardenError("站点地址缺少有效域名，无法查询 Bitwarden")
        mode = str(getattr(site, "bitwarden_match_mode", "domain") or "domain")
        custom = getattr(site, "bitwarden_custom_match", None)
        return mode, host, str(custom) if custom else None

    def lookup_runtime_for_site(self, site: object) -> dict[str, object]:
        """Return deferred lookup parameters without syncing the vault."""
        mode, host, custom = self._site_lookup_request(site)
        view = self.view()
        config = self._resolve_config()
        if not view.enabled or config.url is None:
            raise BitwardenError(view.error or "Bitwarden 凭据集成未启用")
        return {
            "type": "bitwarden",
            "url": config.url,
            "token": config.token,
            "mode": mode,
            "host": host,
            "custom": custom,
            "timeout": self.settings.bitwarden_timeout,
        }

    def resolve_for_site(self, site: object) -> BitwardenSecret:
        mode, host, custom = self._site_lookup_request(site)
        return self.lookup(mode, host, custom)

    def test(self) -> IntegrationTestResult:
        view = self.view(force=True, deep=True)
        data = {
            "available": view.available,
            "configured": view.configured,
            "enabled": view.enabled,
            "source": view.source.value,
            "version": view.server_version,
            "user_email": view.user_email,
        }
        if view.available and view.configured:
            return IntegrationTestResult(ok=True, message="Bitwarden 服务连接成功", data=data)
        return IntegrationTestResult(
            ok=False,
            message=view.error or "Bitwarden 服务不可用",
            data=data,
        )
