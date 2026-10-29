from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

PLUGIN_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


class ConfigurationError(RuntimeError):
    pass


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or not value.strip():
        raise ConfigurationError(f"{name} is required")
    return value.strip()


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    control_url: str
    plugin_id: str
    endpoint: str
    token_file: str | None
    manifest_path: str
    heartbeat_seconds: int = 20
    bind: str = "0.0.0.0"
    port: int = 7000
    features_file: str | None = None
    captcha_methods_file: str | None = None
    register_enabled: bool = True
    verify_tls: bool = True
    token_value: str | None = field(default=None, repr=False)
    handler: str | None = None
    captcha_handler: str | None = None
    app_path: str | None = None
    credential_handler: str | None = None
    totp_handler: str | None = None

    @classmethod
    def from_env(cls) -> "Config":
        plugin_id = _env("SITEFLOW_PLUGIN_ID")
        if not PLUGIN_ID_PATTERN.fullmatch(plugin_id):
            raise ConfigurationError("SITEFLOW_PLUGIN_ID must match ^[a-z][a-z0-9-]{0,62}$")
        token_file = os.environ.get("SITEFLOW_PLUGIN_TOKEN_FILE") or None
        token_value = os.environ.get("SITEFLOW_PLUGIN_TOKEN") or None
        if not token_file and not token_value:
            raise ConfigurationError(
                "SITEFLOW_PLUGIN_TOKEN_FILE or SITEFLOW_PLUGIN_TOKEN is required"
            )
        return cls(
            control_url=_env("SITEFLOW_CONTROL_URL").rstrip("/"),
            plugin_id=plugin_id,
            endpoint=_env("SITEFLOW_PLUGIN_ENDPOINT").rstrip("/"),
            token_file=token_file,
            manifest_path=_env("SITEFLOW_PLUGIN_MANIFEST", "/plugin/current/plugin.json"),
            heartbeat_seconds=int(os.environ.get("SITEFLOW_PLUGIN_HEARTBEAT_SECONDS", "20")),
            bind=os.environ.get("SITEFLOW_PLUGIN_BIND", "0.0.0.0"),
            port=int(os.environ.get("SITEFLOW_PLUGIN_PORT", "7000")),
            features_file=os.environ.get("SITEFLOW_PLUGIN_FEATURES_FILE") or None,
            captcha_methods_file=os.environ.get("SITEFLOW_PLUGIN_CAPTCHA_METHODS_FILE") or None,
            register_enabled=_bool_env("SITEFLOW_PLUGIN_REGISTER_ENABLED", True),
            verify_tls=_bool_env("SITEFLOW_PLUGIN_VERIFY_TLS", True),
            token_value=token_value,
            handler=os.environ.get("SITEFLOW_PLUGIN_HANDLER") or None,
            captcha_handler=os.environ.get("SITEFLOW_PLUGIN_CAPTCHA_HANDLER") or None,
            app_path=os.environ.get("SITEFLOW_PLUGIN_APP_PATH") or None,
            credential_handler=os.environ.get("SITEFLOW_PLUGIN_CREDENTIAL_HANDLER") or None,
            totp_handler=os.environ.get("SITEFLOW_PLUGIN_TOTP_HANDLER") or None,
        )

    def token(self) -> str:
        if self.token_file:
            try:
                token = Path(self.token_file).read_text(encoding="utf-8").strip()
                if token:
                    return token
            except OSError:
                if not self.token_value:
                    raise ConfigurationError(
                        f"cannot read plugin token: {self.token_file}"
                    ) from None
        if self.token_value:
            return self.token_value
        raise ConfigurationError("plugin token is not configured")
