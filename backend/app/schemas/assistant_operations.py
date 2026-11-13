"""Internal, exact-run cancellation request; no force or model-supplied review."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CancelRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    run_id: int = Field(ge=1, le=9223372036854775807)


class RunCancelInteraction(CancelRunRequest):
    purpose: Literal["run_cancel"]
