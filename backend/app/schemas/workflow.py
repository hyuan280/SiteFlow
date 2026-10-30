from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.features.base import (
    CAPTCHA_DEFAULT_PROMPT,
    RESULT_DEFAULT_PROMPT,
    ErrorPolicy,
    Lane,
    LanePolicy,
)
from app.schemas.credential import SiteCredentialView


class FeatureExecutionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm_prompt: str = Field(
        default=CAPTCHA_DEFAULT_PROMPT,
        min_length=1,
        max_length=4000,
    )


class ResultExecutionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    llm_enabled: bool = Field(default=False, strict=True)
    use_vision: bool = Field(default=True, strict=True)
    judgment_scope: Literal["success_only", "all"] = "success_only"
    model: str = Field(default="", max_length=256)
    llm_prompt: str = Field(default=RESULT_DEFAULT_PROMPT, min_length=1, max_length=4000)


class FeatureView(BaseModel):
    key: str
    name: str
    category: str
    cardinality: str
    requires: list[str]
    description: str
    version: int
    config_schema: dict[str, Any]
    defaults: dict[str, Any] = Field(default_factory=dict)
    required_fields: list[str] = Field(default_factory=list)
    base_required_fields: list[str] = Field(default_factory=list)
    customized: bool = False
    execution_settings: FeatureExecutionSettings | ResultExecutionSettings | None = None
    source: str = "builtin"


class GroupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$")
    name: str = Field(min_length=1, max_length=128)
    position: float = Field(default=1000000.0, ge=0, le=1_000_000_000)
    step_anchor: float | None = Field(default=None, ge=0, le=1_000_000_000)


class GroupView(BaseModel):
    group_key: str
    name: str
    position: float
    step_anchor: float | None = None


class ResolvedGroupView(GroupView):
    source: str


class StepInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$")
    feature_type: str = Field(min_length=1, max_length=128)
    label: str | None = Field(default=None, max_length=128)
    group_key: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$")
    position: float | None = Field(default=None, ge=0, le=1_000_000_000)
    enabled: bool | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    condition: str | None = Field(default=None, max_length=2000)
    on_error: ErrorPolicy | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=3600)


class StepView(BaseModel):
    step_key: str
    feature_type: str
    feature_version: int
    label: str | None
    category: str
    group_key: str | None
    position: float | None
    enabled: bool | None
    config: dict[str, Any]
    condition: str | None
    on_error: str | None
    timeout_seconds: int | None
    source: str | None = None


class ResolvedStepView(BaseModel):
    step_key: str
    feature_type: str
    feature_version: int
    label: str
    category: str
    group_key: str | None
    position: float
    enabled: bool
    config: dict[str, Any]
    condition: str
    condition_unknown_action: Literal["needs_human"] = "needs_human"
    on_error: str
    timeout_seconds: int | None
    requires: list[str]
    source: str


class ResolvedWorkflowView(BaseModel):
    owner_type: Literal["template", "site"]
    owner_id: int
    name: str
    lane: Lane | None
    requires: list[str]
    steps: list[ResolvedStepView]
    groups: list[ResolvedGroupView] = Field(default_factory=list)
    signals: list[ResolvedSignalView] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SignalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal_key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$")
    label: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2000)
    include_texts: list[str] = Field(default_factory=list)
    include_selectors: list[str] = Field(default_factory=list)
    exclude_texts: list[str] = Field(default_factory=list)
    exclude_selectors: list[str] = Field(default_factory=list)
    match_mode: Literal["any", "all"] = "any"
    enabled: bool = True


class SignalView(BaseModel):
    signal_key: str
    label: str
    description: str
    include_texts: list[str]
    include_selectors: list[str]
    exclude_texts: list[str]
    exclude_selectors: list[str]
    match_mode: str
    enabled: bool


class ResolvedSignalView(SignalView):
    source: str


class TemplateSummary(BaseModel):
    id: int
    name: str
    description: str
    parent_id: int | None
    enabled: bool
    version: int
    step_count: int
    signal_count: int
    lane: Lane | None
    valid: bool
    validation_error: str | None
    created_at: datetime
    updated_at: datetime


class TemplateDetail(TemplateSummary):
    steps: list[StepView]
    groups: list[GroupView]
    groups_locked: bool = False
    dependent_site_count: int = 0
    dependent_template_count: int = 0
    signals: list[SignalView]
    resolved_signals: list[ResolvedSignalView]
    resolved: ResolvedWorkflowView | None


class TemplateWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=4000)
    parent_id: int | None = None
    enabled: bool = True
    steps: list[StepInput] = Field(default_factory=list)
    groups: list[GroupInput] = Field(default_factory=list)
    signals: list[SignalInput] = Field(default_factory=list)


class SiteSummary(BaseModel):
    id: int
    name: str
    base_url: str
    enabled: bool
    template_id: int | None
    template_name: str | None
    lane_policy: LanePolicy
    lane: Lane | None
    support_resign: bool
    resign_max_days: int
    bitwarden_enabled: bool
    credential_provider_id: str | None = None
    bitwarden_match_mode: Literal["domain", "root_domain", "custom"]
    bitwarden_custom_match: str | None
    checkin_after: str | None
    timezone: str
    tags: list[str]
    notes: str
    config_revision: int
    step_count: int
    signal_count: int
    credential: SiteCredentialView
    valid: bool
    validation_error: str | None
    created_at: datetime
    updated_at: datetime


class SiteOptionView(BaseModel):
    id: int
    name: str
    base_url: str
    enabled: bool
    template_id: int | None
    template_name: str | None
    lane_policy: LanePolicy
    lane: Lane | None
    valid: bool | None = None
    validation_error: str | None = None


class SiteDetail(SiteSummary):
    steps: list[StepView]
    groups: list[GroupView]
    signals: list[SignalView]
    resolved_signals: list[ResolvedSignalView]
    resolved: ResolvedWorkflowView | None


class SiteWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    base_url: str = Field(min_length=1, max_length=512)
    enabled: bool = True
    template_id: int | None = None
    lane_policy: LanePolicy = "auto"
    support_resign: bool = True
    resign_max_days: int = Field(default=30, ge=1, le=365)
    bitwarden_enabled: bool = False
    credential_provider_id: str | None = Field(default=None, max_length=80, pattern=r"^(builtin\.(manual|bitwarden)|plugin\.[a-z][a-z0-9-]{0,62})$")
    bitwarden_match_mode: Literal["domain", "root_domain", "custom"] = "domain"
    bitwarden_custom_match: str | None = Field(default=None, max_length=255)
    checkin_after: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=64)
    tags: list[str] = Field(default_factory=list, max_length=50)
    notes: str = Field(default="", max_length=4000)
    steps: list[StepInput] = Field(default_factory=list)
    groups: list[GroupInput] = Field(default_factory=list)
    signals: list[SignalInput] = Field(default_factory=list)

    @field_validator("base_url")
    @classmethod
    def _normalize_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            raise ValueError("base_url 不能为空")
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("base_url 必须是包含主机名的 http(s) URL")
        return value

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for tag in value:
            tag = tag.strip()
            if tag and tag not in cleaned:
                cleaned.append(tag)
        return cleaned

    @field_validator("bitwarden_custom_match")
    @classmethod
    def _clean_bitwarden_custom_match(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @model_validator(mode="after")
    def _validate_bitwarden(self) -> "SiteWrite":
        if (
            (self.credential_provider_id not in {None, "builtin.manual"} or (self.credential_provider_id is None and self.bitwarden_enabled))
            and self.bitwarden_match_mode == "custom"
            and not self.bitwarden_custom_match
        ):
            raise ValueError("凭据提供商自定义匹配方式需要填写匹配值")
        return self
