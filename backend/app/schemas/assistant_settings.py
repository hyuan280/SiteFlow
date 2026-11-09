"""Global assistant limits, independent of model/provider configuration."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _SettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class AssistantSettingsValues(_SettingsModel):
    max_tool_calls: int = Field(default=20, ge=1, le=200)
    max_model_rounds: int = Field(default=12, ge=1, le=100)
    max_parallel_tool_calls: int = Field(default=4, ge=1, le=8)
    turn_timeout_seconds: int = Field(default=180, ge=30, le=1800)
    # None inherits the global model limit; zero omits the provider parameter.
    max_tokens: int | None = Field(default=None, ge=0, le=9007199254740991)
    context_max_tokens: int = Field(default=64000, ge=16000, le=1000000)
    auto_compact: bool = True
    auto_title: bool = True
    auto_authorize_execution: bool = False
    auto_authorize_screenshot: bool = False
    auto_authorize_workflow: bool = False


class AssistantSettingsUpdate(_SettingsModel):
    expected_revision: int = Field(ge=0)
    values: AssistantSettingsValues


class AssistantSettingsReset(_SettingsModel):
    expected_revision: int = Field(ge=0)


class AssistantSettingsView(_SettingsModel):
    revision: int = Field(ge=0)
    source: Literal["default", "db"]
    values: AssistantSettingsValues
