from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    site_ids: list[int] | None = Field(default=None, max_length=500)
    force: bool = False
    today_unsuccessful: bool = False


class JobStepView(BaseModel):
    step_key: str
    feature_type: str
    label: str
    status: str
    message: str
    data: dict[str, Any]
    started_at: datetime | None
    finished_at: datetime | None


class ArtifactView(BaseModel):
    id: int
    step_key: str | None
    kind: str
    label: str
    content_type: str
    size_bytes: int
    sha256: str
    created_at: datetime


class SiteJobView(BaseModel):
    id: int
    site_id: int | None
    site_name: str
    lane: str
    attempt: int
    status: str
    message: str
    result: dict[str, Any]
    started_at: datetime | None
    finished_at: datetime | None
    steps: list[JobStepView]
    artifacts: list[ArtifactView] = Field(default_factory=list)


class RunSummary(BaseModel):
    id: int
    mode: str
    trigger: str
    status: str
    force: bool
    cancel_requested: bool
    requested_sites: list[int]
    summary: dict[str, int]
    site_count: int
    site_names: list[str]
    created_by: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RunDetail(RunSummary):
    jobs: list[SiteJobView]


class ExecutionResult(BaseModel):
    status: str
    message: str
    steps: list[dict[str, Any]]


WorkerJobStatus = Literal[
    "success",
    "fail",
    "error",
    "deferred",
    "unknown",
    "skipped",
    "waiting_human",
    "canceled",
    "interrupted",
]
