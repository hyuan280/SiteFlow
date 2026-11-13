"""Local workflow data only; site settings and credentials are not importable."""

from pydantic import BaseModel, ConfigDict

from app.schemas.workflow import GroupInput, SignalInput, StepInput


class TemplateSiteImport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_id: int | None
    steps: list[StepInput]
    groups: list[GroupInput]
    signals: list[SignalInput]
