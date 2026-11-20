from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, StrictBool, field_validator

from app.features.base import Category

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
    generation: int | None = Field(default=None, ge=1)
    permission_epoch: int | None = Field(default=None, ge=1)

    @field_validator("capabilities")
    @classmethod
    def _validate_capabilities(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return validate_capabilities(value)


class PluginFeatureCategoriesRequest(BaseModel):
    categories: dict[str, Category | None] = Field(default_factory=dict)
    assistant_permissions: dict[str, StrictBool] = Field(default_factory=dict)
    generation: int = Field(ge=1)
    permission_epoch: int = Field(ge=1)


class PluginFeatureView(BaseModel):
    key: str
    name: str
    declared_category: Category
    category: Category
    category_overridden: bool
    risk_index: int = Field(ge=1, le=5)
    risk_label: str
    assistant_allowed: bool = False
    assistant_policy_revision: int = Field(default=0, ge=0)


class PluginSecretProviderView(PluginFeatureView):
    provider_id: str
    capability: Literal["secret.credentials", "secret.totp"]


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
    features: list[PluginFeatureView] = Field(default_factory=list)
    captcha_method_count: int
    captcha_methods: list[PluginFeatureView] = Field(default_factory=list)
    secret_providers: list[PluginSecretProviderView] = Field(default_factory=list)
    checks: list[PluginCheckView]
    status: str
    enabled: bool
    generation: int
    permission_epoch: int
    approved_capabilities: list[str]
    approved_at: datetime | None
    approved_by: str | None
    registered_at: datetime
    last_heartbeat_at: datetime
    updated_at: datetime
