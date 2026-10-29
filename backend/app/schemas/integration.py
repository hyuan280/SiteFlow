from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ConfigSource(str, Enum):
    ENV = "env"
    DB = "db"
    AUTO = "auto"
    UNSET = "unset"


class ProxyView(BaseModel):
    enabled: bool
    source: ConfigSource
    editable: bool
    protocol: str | None = None
    url_masked: str | None = None
    error: str | None = None


class ValueView(BaseModel):
    value: str | None = None
    source: ConfigSource
    editable: bool
    error: str | None = None


class TokenView(BaseModel):
    configured: bool
    source: ConfigSource
    editable: bool
    masked: str | None = None


class TwoFAuthView(BaseModel):
    enabled: bool
    url: ValueView
    token: TokenView
    error: str | None = None


class CFBoxView(BaseModel):
    enabled: bool
    url: ValueView
    api_key: TokenView
    error: str | None = None


class BitwardenView(BaseModel):
    enabled: bool
    enabled_setting: bool
    enabled_source: ConfigSource
    available: bool
    configured: bool
    auto_detected: bool
    source: ConfigSource
    url: ValueView
    token: TokenView
    default_url: str
    server_version: str | None = None
    user_email: str | None = None
    error: str | None = None


class BooleanView(BaseModel):
    value: bool
    source: ConfigSource
    editable: bool


class VisionView(BaseModel):
    base_url: ValueView
    model: ValueView
    api_key: TokenView
    supports_vision: BooleanView
    available: bool


class VisionUpdate(BaseModel):
    base_url: str | None = Field(default=None, max_length=2048)
    model: str | None = Field(default=None, max_length=256)
    api_key: str | None = Field(default=None, max_length=4096)
    supports_vision: bool | None = None


class IntegrationsView(BaseModel):
    vision: VisionView
    proxy: ProxyView
    twofauth: TwoFAuthView
    cfbox: CFBoxView
    bitwarden: BitwardenView


class ProxyUpdate(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


class TwoFAuthUpdate(BaseModel):
    url: str | None = Field(default=None, max_length=2048)
    token: str | None = Field(default=None, max_length=2048)


class CFBoxUpdate(BaseModel):
    url: str | None = Field(default=None, max_length=2048)
    api_key: str | None = Field(default=None, max_length=2048)


class BitwardenUpdate(BaseModel):
    url: str | None = Field(default=None, max_length=2048)
    token: str | None = Field(default=None, max_length=2048)
    enabled: bool | None = None


class IntegrationTestResult(BaseModel):
    ok: bool
    message: str
    data: dict[str, Any] | None = None
