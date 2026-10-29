from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class MatrixCellView(BaseModel):
    status: str
    attempts: int
    counts: dict[str, int]
    last_run_id: int | None
    last_job_id: int | None
    last_message: str


class MatrixSiteView(BaseModel):
    site_id: int
    name: str
    enabled: bool
    cells: dict[str, MatrixCellView]


class MatrixView(BaseModel):
    start_date: str
    end_date: str
    dates: list[str]
    sites: list[MatrixSiteView]


class FailureStepView(BaseModel):
    step_key: str
    label: str
    feature_type: str
    status: str
    message: str


class FailureItemView(BaseModel):
    job_id: int
    run_id: int
    site_id: int | None
    site_name: str
    lane: str
    attempt: int
    status: str
    message: str
    started_at: datetime | None
    finished_at: datetime | None
    failed_steps: list[FailureStepView]
    screenshot_id: int | None


class FailuresView(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[FailureItemView]


class TrendPointView(BaseModel):
    date: str
    total: int
    success: int
    fail: int
    error: int
    waiting_human: int
    unknown: int
    deferred: int
    canceled: int
    interrupted: int
    success_rate: float


class TrendsView(BaseModel):
    start_date: str
    end_date: str
    points: list[TrendPointView]
    total: int
    success: int
    fail: int
    error: int
    waiting_human: int
    unknown: int
    deferred: int
    canceled: int
    interrupted: int
    success_rate: float
