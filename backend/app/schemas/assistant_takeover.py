"""A persistent delegation is separate from interactive assistant permissions."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class SubmitTakeoverConclusion(_Strict):
    conclusion: str = Field(min_length=1, max_length=240)


class TakeoverValues(_Strict):
    enabled: bool = False
    live_access: bool = True
    mode: Literal["analysis", "autofix"] = "analysis"
    # null selects all enabled sites; [] selects none, including on first enable.
    site_ids: list[Annotated[int, Field(ge=1)]] | None = Field(default_factory=list, max_length=10000)
    max_attempts: int = Field(default=1, ge=1, le=3)
    conclusion_ttl_days: int = Field(default=7, ge=1, le=30)

    @field_validator("site_ids")
    @classmethod
    def unique(cls, value):
        if value is not None and len(value) != len(set(value)):
            raise ValueError("Duplicate sites")
        return value


class TakeoverSettingsUpdate(_Strict):
    expected_revision: int = Field(ge=0)
    confirm: Literal[True]
    values: TakeoverValues

    @field_validator("values")
    @classmethod
    def explicit_live_scope(cls, value):
        if value.enabled and "live_access" not in value.model_fields_set:
            raise ValueError("Explicit live-access decision required")
        return value

    @field_validator("confirm", mode="before")
    @classmethod
    def explicit(cls, value):
        if value is not True:
            raise ValueError("Explicit persistent delegation required")
        return value


class TakeoverSettingsView(_Strict):
    revision: int = Field(ge=0)
    values: TakeoverValues
    authorizing_owner: str | None = None
    authorized_at: datetime | None = None
    effective_from: datetime | None = None


class TakeoverActionView(_Strict):
    id: str
    source_job_id: int
    source_run_id: int
    site_id: int
    grant_revision: int
    mode: Literal["analysis", "autofix"]
    status: Literal["queued", "dispatched", "revoked", "completed_analysis", "updated", "failed", "unknown", "reused"]
    reason: str
    conclusion: str | None = Field(default=None, max_length=300)
    session_id: str | None
    attempts: int
    created_at: datetime
    updated_at: datetime


class TakeoverActionsView(_Strict):
    items: list[TakeoverActionView]
