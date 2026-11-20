"""Host-only runtime contracts. Never accept these inside a model step."""
from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PrivateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class OperationAuthorization(PrivateModel):
    scope: Literal["assistant_operation"]
    session_id: str = Field(min_length=1, max_length=64)
    operation_id: str = Field(min_length=1, max_length=64)
    token: str = Field(min_length=1, max_length=4096, repr=False)

    @field_validator("session_id", "operation_id")
    @classmethod
    def identifier(cls, value):
        return str(UUID(value))


class PluginRuntime(PrivateModel):
    plugin_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    endpoint: str = Field(min_length=1, max_length=4096, repr=False)
    token: str = Field(min_length=1, max_length=4096, repr=False)
    generation: int = Field(ge=1)
    permission_epoch: int = Field(ge=0)
    approved_capabilities: list[Literal["features", "captcha_methods", "browser_actions", "secret.credentials", "secret.totp"]] = Field(max_length=5)
    authorization: OperationAuthorization = Field(repr=False)


class SecretAccess(PrivateModel):
    token: str = Field(min_length=1, max_length=4096, repr=False)


class JobRuntime(PrivateModel):
    job_id: str = Field(min_length=1, max_length=128)
    site_id: str = Field(min_length=1, max_length=128)
    secret_access: SecretAccess = Field(repr=False)
    resign_pending_dates: list[str] = Field(max_length=365)
    resign_today: str
    resign_unresolved_dates: list[str] = Field(default_factory=list, max_length=365)

    @field_validator("resign_today")
    @classmethod
    def day(cls, value):
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError("Invalid date")
        return value

    @field_validator("resign_pending_dates", "resign_unresolved_dates")
    @classmethod
    def dates(cls, value):
        for item in value:
            cls.day(item)
        if value != sorted(set(value)):
            raise ValueError("Invalid dates")
        return value


class AssistantPluginBroker:
    """Bind the shared broker to this existing assistant tab's private guard."""

    def __init__(self, broker, context):
        self.broker = broker
        self.context = context
        self.public_url = broker.public_url

    def register(self, page, **kwargs):
        if page is not self.context.page:
            raise RuntimeError("Assistant page denied")
        return self.broker.register(page, assistant_context=self.context, **kwargs)

    async def release_and_wait(self, token):
        await self.broker.release_and_wait(token)
