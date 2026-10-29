from __future__ import annotations

from pydantic import BaseModel, Field


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
