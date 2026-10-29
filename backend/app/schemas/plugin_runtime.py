from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

PLUGIN_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
CAPABILITY_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


def validate_plugin_id(value: str) -> str:
    if not PLUGIN_ID_PATTERN.fullmatch(value):
        raise ValueError("plugin id must match ^[a-z][a-z0-9-]{0,62}$")
    return value


def validate_capabilities(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if not CAPABILITY_PATTERN.fullmatch(value):
            raise ValueError(f"invalid plugin capability: {value}")
        if value not in result:
            result.append(value)
    return result


class PluginManifest(BaseModel):
    id: str
    name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    protocol_version: int = Field(ge=1, le=1000)
    entrypoint: str | None = Field(default=None, max_length=256)
    capabilities: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return validate_plugin_id(value)

    @field_validator("capabilities")
    @classmethod
    def _validate_capabilities(cls, value: list[str]) -> list[str]:
        return validate_capabilities(value)


class PluginRegisterRequest(BaseModel):
    plugin_id: str
    protocol_version: int = Field(ge=1, le=1000)
    endpoint: str = Field(min_length=1, max_length=512)
    manifest: PluginManifest

    @field_validator("plugin_id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return validate_plugin_id(value)


class PluginHeartbeatRequest(BaseModel):
    plugin_id: str
    generation: int | None = Field(default=None, ge=1)

    @field_validator("plugin_id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return validate_plugin_id(value)


class PluginUnregisterRequest(BaseModel):
    plugin_id: str
    generation: int | None = Field(default=None, ge=1)

    @field_validator("plugin_id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return validate_plugin_id(value)


class PluginCheckView(BaseModel):
    key: str
    label: str
    ok: bool
    skipped: bool = False
    message: str = ""


class PluginApproveRequest(BaseModel):
    capabilities: list[str] | None = None

    @field_validator("capabilities")
    @classmethod
    def _validate_capabilities(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return validate_capabilities(value)


class PluginView(BaseModel):
    removed_at: datetime | None = None
    removed_by: str | None = None
    plugin_id: str
    name: str
    version: str
    protocol_version: int
    endpoint: str
    capabilities: list[str]
    feature_count: int
    captcha_method_count: int
    checks: list[PluginCheckView]
    status: str
    enabled: bool
    generation: int
    approved_capabilities: list[str]
    approved_at: datetime | None
    approved_by: str | None
    registered_at: datetime
    last_heartbeat_at: datetime
    updated_at: datetime
