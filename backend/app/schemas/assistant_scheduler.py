"""Closed, sparse inputs for reviewed global scheduler changes."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.scheduler import SchedulerMode


class SchedulerPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    enabled: bool | None = None
    mode: SchedulerMode | None = None
    cron: str | None = Field(default=None, max_length=128)
    interval_seconds: int | None = Field(default=None, ge=60, le=2592000)
    ranges: list[str] | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def nonempty(self):
        if not self.model_fields_set or any(
            getattr(self, name) is None for name in self.model_fields_set
        ):
            raise ValueError("A nonempty patch with non-null values is required")
        return self


class UpdateScheduler(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    operation_id: UUID
    patch: SchedulerPatch

    @field_validator("operation_id", mode="before")
    @classmethod
    def identifier(cls, value):
        return UUID(value) if isinstance(value, str) else value
