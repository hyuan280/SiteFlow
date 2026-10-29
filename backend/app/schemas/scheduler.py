from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SchedulerMode = Literal["cron", "interval", "random"]


class SchedulerSettingsView(BaseModel):
    enabled: bool
    enabled_source: str
    mode: SchedulerMode
    cron: str
    interval_seconds: int
    ranges: list[str]
    source: str
    editable: bool
    timezone: str
    next_run_at: datetime | None = None


class SchedulerSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    mode: SchedulerMode
    cron: str = Field(default="", max_length=128)
    interval_seconds: int = Field(default=3600, ge=60, le=2592000)
    ranges: list[str] = Field(default_factory=list, max_length=20)
