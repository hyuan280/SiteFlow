"""The intentionally narrow, new-site-only configuration generation contract."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.workflow import SiteWrite
from app.services.config_ai_protocol import SubmitWorkflowDraftArgs


class GenerationSite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    base_url: str = Field(min_length=1, max_length=512)

    @field_validator("base_url")
    @classmethod
    def _normalize_base_url(cls, value: str) -> str:
        return SiteWrite(name="generation", base_url=value).base_url


class GenerateSiteDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    site: GenerationSite
    requirements: str = Field(min_length=1, max_length=8000)
    evidence: str = Field(default="", max_length=16000)
    consent_to_send: Literal[True]
    allowed_features: list[str] = Field(min_length=1, max_length=23)

    @field_validator("consent_to_send", mode="before")
    @classmethod
    def _explicit_consent(cls, value: object) -> object:
        # Literal[True] alone also accepts the integer 1.
        if value is not True:
            raise ValueError("Explicit consent is required")
        return value

    @field_validator("allowed_features")
    @classmethod
    def _unique_features(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("Feature selection must be unique")
        return value


class GenerationResult(SubmitWorkflowDraftArgs):
    status: Literal["proposal"] = "proposal"
    requires_review: Literal[True] = True
