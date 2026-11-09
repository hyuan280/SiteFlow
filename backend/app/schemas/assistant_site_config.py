"""Closed, strictly typed inputs for exact-patch site-setting approvals."""

from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from app.schemas.workflow import SiteSettingsPatch


class SiteConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class SiteConfigPatch(SiteSettingsPatch):
    model_config = ConfigDict(hide_input_in_errors=True)

    tags: list[Annotated[str, StringConstraints(min_length=1, max_length=128)]] | None = Field(
        default=None, max_length=50)

    @field_validator("trusted_urls", mode="before")
    @classmethod
    def exact_patterns(cls, value):
        if isinstance(value, list) and any(
            isinstance(pattern, str) and pattern != pattern.strip() for pattern in value
        ):
            raise ValueError("Patterns must not have surrounding whitespace")
        return value


class SiteConfigObservation(SiteConfigInput):
    trusted_origins: list[Annotated[str, StringConstraints(min_length=1, max_length=512)]] | None = Field(
        default=None, max_length=20)
    proxy: bool | None = None

    @model_validator(mode="after")
    def values(self):
        if any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("Observation values cannot be null")
        if self.trusted_origins is not None:
            from app.schemas.assistant import SetTarget

            self.trusted_origins = SetTarget.origins(self.trusted_origins)
        return self


class UpdateSiteConfig(SiteConfigInput):
    operation_id: UUID
    patch: SiteConfigPatch = Field(default_factory=SiteConfigPatch)
    observation: SiteConfigObservation = Field(default_factory=SiteConfigObservation)
    successful_run_id: int | None = Field(default=None, ge=1)

    @field_validator("operation_id", mode="before")
    @classmethod
    def identifier(cls, value):
        return UUID(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def nonempty(self):
        patch = self.patch.model_dump(exclude_unset=True)
        observation = self.observation.model_dump(exclude_unset=True)
        if not patch and not observation:
            raise ValueError("A patch or observation change is required")
        if patch.get("enabled") is True and (observation or set(patch) != {"enabled"}):
            raise ValueError("Enable must be reviewed separately")
        if self.successful_run_id is not None and patch.get("enabled") is not True:
            raise ValueError("Successful run evidence is only for enabling")
        return self
