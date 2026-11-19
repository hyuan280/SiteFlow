from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


BROWSER_BACKEND_UNAVAILABLE = "浏览器授权设置暂不可用或状态异常；如刚保存，结果可能未知，请刷新确认，不要自动重试。"


class BrowserLicenseView(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    configured: bool
    source: Literal["env", "manager", "unset", "unknown"]
    editable: bool


class BrowserSettingsView(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    backend: Literal["cloak-manager", "unsupported"]
    connected: bool
    license: BrowserLicenseView
    license_tier: Literal["keyless", "free", "pro", "none", "unknown"]
    binary_version: Annotated[
        str, StringConstraints(max_length=64, pattern=r"^[0-9]+(?:\.[0-9]+)*$")
    ] | None
    error: Literal["浏览器授权设置暂不可用或状态异常；如刚保存，结果可能未知，请刷新确认，不要自动重试。"] | None

    @model_validator(mode="after")
    def validate_license_state(self):
        if self.backend == "cloak-manager":
            license = self.license
            configured = license.source in {"env", "manager"}
            editable = (self.connected and self.error is None
                        and license.source in {"manager", "unset"})
            if license.configured != configured or license.editable != editable:
                raise ValueError("Inconsistent browser license state")
        return self


class LaneStatus(BaseModel):
    key: str
    label: str
    enabled: bool
    status: str
    browser: str
    display: str | None = None
    profile: str | None = None
    vnc_available: bool = False
    in_use: int = 0
    max_concurrency: int = 1
    viewers: int = 0
    idle_stop_seconds: int = 300
    idle_remaining: int | None = None
    can_start: bool = False
    can_stop: bool = False
    error: str | None = None
    started_at: str | None = None


class LanesResponse(BaseModel):
    worker_connected: bool
    lanes: list[LaneStatus]
    error: str | None = None


class LaneSettingValue(BaseModel):
    value: int
    source: str
    editable: bool


class LaneSettingsView(BaseModel):
    idle_stop_seconds: LaneSettingValue
    general_concurrency: LaneSettingValue
    proxy_concurrency: LaneSettingValue


class LaneFocusRequest(BaseModel):
    job_id: str


class LaneSettingsUpdate(BaseModel):
    idle_stop_seconds: int | None = Field(default=None, ge=0, le=86400)
    general_concurrency: int | None = Field(default=None, ge=1, le=10)
    proxy_concurrency: int | None = Field(default=None, ge=1, le=10)
