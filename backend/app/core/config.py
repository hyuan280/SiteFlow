from __future__ import annotations

import base64
import binascii
import json

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = "SiteFlow"
    database_url: str = "sqlite:///./data/siteflow.db"
    artifact_root: str = Field(default="./data/artifacts", validation_alias="SITEFLOW_ARTIFACT_ROOT")
    artifact_retention_days: int = Field(
        default=30, ge=1, le=3650, validation_alias="SITEFLOW_ARTIFACT_RETENTION_DAYS"
    )
    artifact_cleanup_interval_seconds: int = Field(
        default=21600,
        ge=300,
        le=86400 * 7,
        validation_alias="SITEFLOW_ARTIFACT_CLEANUP_INTERVAL_SECONDS",
    )
    artifact_cleanup_enabled: bool = Field(
        default=True, validation_alias="SITEFLOW_ARTIFACT_CLEANUP_ENABLED"
    )
    request_timeout: float = 10.0
    vision_base_url: str | None = Field(default=None, validation_alias="SITEFLOW_VISION_BASE_URL")
    vision_api_key: SecretStr | None = Field(default=None, validation_alias="SITEFLOW_VISION_API_KEY")
    vision_model: str | None = Field(default=None, validation_alias="SITEFLOW_VISION_MODEL")
    vision_timeout: float = Field(default=30, gt=0, le=300, validation_alias="SITEFLOW_VISION_TIMEOUT")
    vision_supports_vision: bool | None = Field(
        default=None, validation_alias="SITEFLOW_VISION_SUPPORTS_VISION"
    )

    notification_email_enabled: bool | None = Field(
        default=None, validation_alias="SITEFLOW_NOTIFICATION_EMAIL_ENABLED"
    )
    notification_email_smtp_host: str | None = Field(
        default=None, validation_alias="SITEFLOW_NOTIFICATION_EMAIL_SMTP_HOST"
    )
    notification_email_smtp_port: int | None = Field(
        default=None,
        ge=1,
        le=65535,
        validation_alias="SITEFLOW_NOTIFICATION_EMAIL_SMTP_PORT",
    )
    notification_email_security: str | None = Field(
        default=None, validation_alias="SITEFLOW_NOTIFICATION_EMAIL_SECURITY"
    )
    notification_email_username: str | None = Field(
        default=None, validation_alias="SITEFLOW_NOTIFICATION_EMAIL_USERNAME"
    )
    notification_email_password: SecretStr | None = Field(
        default=None, validation_alias="SITEFLOW_NOTIFICATION_EMAIL_PASSWORD"
    )
    notification_email_from: str | None = Field(
        default=None, validation_alias="SITEFLOW_NOTIFICATION_EMAIL_FROM"
    )
    notification_email_to: str | None = Field(
        default=None, validation_alias="SITEFLOW_NOTIFICATION_EMAIL_TO"
    )
    notification_http_enabled: bool | None = Field(
        default=None, validation_alias="SITEFLOW_NOTIFICATION_HTTP_ENABLED"
    )
    notification_http_url: str | None = Field(
        default=None, validation_alias="SITEFLOW_NOTIFICATION_HTTP_URL"
    )
    notification_http_method: str | None = Field(
        default=None, validation_alias="SITEFLOW_NOTIFICATION_HTTP_METHOD"
    )
    notification_http_headers: str | None = Field(
        default=None, validation_alias="SITEFLOW_NOTIFICATION_HTTP_HEADERS"
    )
    notification_http_timeout: float | None = Field(
        default=None, gt=0, le=120, validation_alias="SITEFLOW_NOTIFICATION_HTTP_TIMEOUT"
    )

    master_key: SecretStr | None = Field(default=None, validation_alias="SITEFLOW_MASTER_KEY")
    browser_token: SecretStr | None = Field(default=None, validation_alias="SITEFLOW_BROWSER_TOKEN")
    proxy_url: SecretStr | None = Field(default=None, validation_alias="SITEFLOW_PROXY_URL")
    twofauth_url: str | None = Field(default=None, validation_alias="SITEFLOW_TWOFAUTH_URL")
    twofauth_token: SecretStr | None = Field(default=None, validation_alias="SITEFLOW_TWOFAUTH_TOKEN")
    cfbox_url: str | None = Field(default=None, validation_alias="SITEFLOW_CFBOX_URL")
    cfbox_api_key: SecretStr | None = Field(
        default=None, validation_alias="SITEFLOW_CFBOX_API_KEY"
    )
    bitwarden_url: str | None = Field(default=None, validation_alias="SITEFLOW_BITWARDEN_URL")
    bitwarden_token: SecretStr | None = Field(
        default=None, validation_alias="SITEFLOW_BITWARDEN_TOKEN"
    )
    bitwarden_enabled: bool | None = Field(
        default=None, validation_alias="SITEFLOW_BITWARDEN_ENABLED"
    )
    bitwarden_auto_detect: bool = Field(
        default=True, validation_alias="SITEFLOW_BITWARDEN_AUTO_DETECT"
    )
    bitwarden_timeout: float = Field(default=30.0, validation_alias="SITEFLOW_BITWARDEN_TIMEOUT")
    bitwarden_probe_timeout: float = Field(
        default=2.0, validation_alias="SITEFLOW_BITWARDEN_PROBE_TIMEOUT"
    )

    worker_url: str = Field(default="http://worker:6300", validation_alias="SITEFLOW_WORKER_URL")
    worker_timeout: float = Field(default=3.0, validation_alias="SITEFLOW_WORKER_TIMEOUT")
    plugin_runtime_tokens: SecretStr | None = Field(
        default=None, validation_alias="SITEFLOW_PLUGIN_TOKENS"
    )
    plugin_heartbeat_ttl_seconds: int = Field(
        default=90, ge=10, le=3600, validation_alias="SITEFLOW_PLUGIN_HEARTBEAT_TTL_SECONDS"
    )
    plugin_endpoint_allowlist: str = Field(
        default="", validation_alias="SITEFLOW_PLUGIN_ENDPOINT_ALLOWLIST"
    )
    plugin_verify_endpoint: bool = Field(
        default=True, validation_alias="SITEFLOW_PLUGIN_VERIFY_ENDPOINT"
    )
    plugin_resolve_endpoint: bool = Field(
        default=True, validation_alias="SITEFLOW_PLUGIN_RESOLVE_ENDPOINT"
    )
    plugin_metadata_timeout: float = Field(
        default=3.0, gt=0, le=60, validation_alias="SITEFLOW_PLUGIN_METADATA_TIMEOUT"
    )
    execution_timeout: float = Field(default=600.0, validation_alias="SITEFLOW_EXECUTION_TIMEOUT")
    execution_max_runs: int = Field(default=2, validation_alias="SITEFLOW_EXECUTION_MAX_RUNS")
    execution_max_jobs: int = Field(default=10, validation_alias="SITEFLOW_EXECUTION_MAX_JOBS")
    scheduler_enabled: bool | None = Field(
        default=None, validation_alias="SITEFLOW_SCHEDULER_ENABLED"
    )
    scheduler_poll_seconds: int = Field(
        default=30, validation_alias="SITEFLOW_SCHEDULER_POLL_SECONDS"
    )
    scheduler_mode: str | None = Field(default=None, validation_alias="SITEFLOW_SCHEDULER_MODE")
    scheduler_cron: str | None = Field(default=None, validation_alias="SITEFLOW_SCHEDULER_CRON")
    scheduler_interval_seconds: int | None = Field(
        default=None, validation_alias="SITEFLOW_SCHEDULER_INTERVAL_SECONDS"
    )
    scheduler_ranges: str | None = Field(default=None, validation_alias="SITEFLOW_SCHEDULER_RANGES")
    scheduler_timezone: str = Field(
        default="Asia/Shanghai",
        validation_alias="SITEFLOW_SCHEDULER_TIMEZONE",
    )
    lane_idle_stop_seconds: int | None = Field(
        default=None,
        validation_alias="SITEFLOW_LANE_IDLE_STOP_SECONDS",
    )
    lane_general_concurrency: int | None = Field(
        default=None,
        validation_alias="SITEFLOW_LANE_GENERAL_CONCURRENCY",
    )
    lane_proxy_concurrency: int | None = Field(
        default=None,
        validation_alias="SITEFLOW_LANE_PROXY_GENERAL_CONCURRENCY",
    )

    admin_username: str | None = Field(default=None, validation_alias="SITEFLOW_ADMIN_USERNAME")
    admin_password: SecretStr | None = Field(default=None, validation_alias="SITEFLOW_ADMIN_PASSWORD")
    session_ttl_hours: int = Field(default=168, validation_alias="SITEFLOW_SESSION_TTL_HOURS")
    cookie_secure: bool = Field(default=False, validation_alias="SITEFLOW_COOKIE_SECURE")
    login_max_attempts: int = Field(default=5, validation_alias="SITEFLOW_LOGIN_MAX_ATTEMPTS")
    login_lockout_seconds: int = Field(default=300, validation_alias="SITEFLOW_LOGIN_LOCKOUT_SECONDS")

    def master_key_bytes(self) -> bytes:
        if self.master_key is None:
            raise RuntimeError("SITEFLOW_MASTER_KEY is required")
        raw = self.master_key.get_secret_value().strip()
        if raw.startswith("base64:"):
            raw = raw[len("base64:"):]
        try:
            key = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RuntimeError("SITEFLOW_MASTER_KEY is not valid base64") from exc
        if len(key) != 32:
            raise RuntimeError("SITEFLOW_MASTER_KEY must decode to exactly 32 bytes")
        return key

    def proxy_url_value(self) -> str | None:
        if self.proxy_url is None:
            return None
        value = self.proxy_url.get_secret_value().strip()
        return value or None

    def twofauth_url_value(self) -> str | None:
        return (self.twofauth_url or "").strip() or None

    def twofauth_token_value(self) -> str | None:
        if self.twofauth_token is None:
            return None
        value = self.twofauth_token.get_secret_value().strip()
        return value or None

    def cfbox_url_value(self) -> str | None:
        return (self.cfbox_url or "").strip() or None

    def cfbox_api_key_value(self) -> str | None:
        if self.cfbox_api_key is None:
            return None
        value = self.cfbox_api_key.get_secret_value().strip()
        return value or None

    def bitwarden_url_value(self) -> str | None:
        return (self.bitwarden_url or "").strip() or None

    def bitwarden_token_value(self) -> str | None:
        if self.bitwarden_token is None:
            return None
        value = self.bitwarden_token.get_secret_value().strip()
        return value or None

    def plugin_token_map(self) -> dict[str, str]:
        if self.plugin_runtime_tokens is None:
            return {}
        raw = self.plugin_runtime_tokens.get_secret_value().strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("SITEFLOW_PLUGIN_TOKENS is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("SITEFLOW_PLUGIN_TOKENS must be a JSON object")
        tokens: dict[str, str] = {}
        for key, value in parsed.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise RuntimeError("SITEFLOW_PLUGIN_TOKENS keys and values must be strings")
            if not key or not value:
                raise RuntimeError("SITEFLOW_PLUGIN_TOKENS entries must not be empty")
            tokens[key] = value
        return tokens

    def plugin_endpoint_allowlist_values(self) -> list[str]:
        return [item.strip() for item in self.plugin_endpoint_allowlist.split(",") if item.strip()]

    def admin_password_value(self) -> str | None:
        if self.admin_password is None:
            return None
        value = self.admin_password.get_secret_value().strip()
        return value or None

    def admin_username_value(self) -> str:
        return (self.admin_username or "admin").strip() or "admin"


def load_settings() -> Settings:
    return Settings()
