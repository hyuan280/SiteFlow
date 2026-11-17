from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.integration import VisionView
from app.services.vision import validate_base_url

Usage = Literal["feature_cards", "assistant", "mcp"]
Revision = Annotated[int, Field(strict=True, ge=0, le=9007199254740991)]


class RegistrySchema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class ModelTestConfig(RegistrySchema):
    base_url: str = Field(default=None, min_length=1, max_length=2048)
    model: str = Field(default=None, min_length=1, max_length=256)
    supports_vision: bool = True
    max_tokens: int | None = Field(default=None, ge=0, le=2147483647)

    @field_validator("model")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("field must not be blank")
        return value

    @field_validator("base_url")
    @classmethod
    def url(cls, value: str) -> str:
        return validate_base_url(value)


class ModelConfigUpdate(ModelTestConfig):
    api_key: str = Field(default=None, min_length=1, max_length=4096, repr=False)

    @field_validator("api_key")
    @classmethod
    def key_nonblank(cls, value: str) -> str:
        return cls.nonblank(value)


class ModelTestRequest(RegistrySchema):
    expected_revision: Revision
    config: ModelTestConfig


class ModelMetadata(RegistrySchema):
    id: str = Field(max_length=36)
    name: str = Field(min_length=1, max_length=128)
    enabled: bool
    usages: list[Usage] = Field(max_length=3)

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if value != "primary":
            parsed = UUID(value)
            if parsed.version != 4 or str(parsed) != value:
                raise ValueError("id must be a canonical UUID4")
        return value

    @field_validator("name")
    @classmethod
    def name_nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value

    @field_validator("usages")
    @classmethod
    def unique_usages(cls, value: list[Usage]) -> list[Usage]:
        if len(value) != len(set(value)):
            raise ValueError("duplicate usages")
        return value


class ModelUpdate(ModelMetadata):
    config: ModelConfigUpdate = Field(default_factory=ModelConfigUpdate)


class RegistryUpdate(RegistrySchema):
    expected_revision: Revision
    items: list[ModelUpdate] = Field(max_length=21)

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [item.id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("unique ids required")
        return self


class ModelView(ModelMetadata):
    source: Literal["env", "db"]
    config_editable: bool
    config: VisionView


class RegistryView(RegistrySchema):
    revision: Revision
    items: list[ModelView] = Field(max_length=21)

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [item.id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("unique ids required")
        return self


class RegistryDocument(RegistrySchema):
    version: int = Field(strict=True, ge=1, le=1)
    revision: Revision
    items: list[ModelUpdate] = Field(max_length=21)
    # Internal origin marker for metadata that belongs only to deployment configuration.
    environment_only: bool = Field(default=False, repr=False)
    # Historical literal redaction only; never exposed or used for provider requests.
    redaction_keys: list[Annotated[str, Field(min_length=1, max_length=4096)]] = Field(
        default_factory=list, max_length=512, repr=False,
    )

    @field_validator("redaction_keys")
    @classmethod
    def valid_redaction_keys(cls, value):
        if any(not key.strip() for key in value) or len(value) != len(set(value)):
            raise ValueError("invalid redaction archive")
        return value

    @model_validator(mode="after")
    def valid_items(self):
        RegistryUpdate(expected_revision=self.revision, items=self.items)
        if any(item.id == "primary" and item.config.model_fields_set for item in self.items):
            raise ValueError("primary configuration belongs to legacy keys")
        return self
