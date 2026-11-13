"""Public assistant inputs; authorization is never inferred from conversation text."""

from __future__ import annotations

import ipaddress
import re
from typing import Annotated, Literal
from urllib.parse import unquote, urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.assistant_site_config import UpdateSiteConfig
from app.schemas.assistant_scheduler import UpdateScheduler
from app.schemas.assistant_operations import RunCancelInteraction
from app.services.assistant_templates import TemplateFromSiteRequest


def public_url(value: str) -> str:
    """Retain full URLs while rejecting userinfo and non-public destinations.

    The worker additionally checks DNS at open, navigation and observation time.
    This parser is not a network sandbox or protection against DNS rebinding.
    """
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError("Invalid public URL")
    decoded = unquote(value)
    if (any(ord(c) <= 32 or 127 <= ord(c) <= 159 or c.isspace() or c == "\\" for c in value)
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 or c == "\\" for c in decoded)):
        raise ValueError("Invalid public URL")
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower()
        if (parsed.scheme not in {"http", "https"} or not host
                or parsed.username is not None or parsed.password is not None
                or not re.match(r"https?://", value, re.I)
                or "%" in host or host.endswith(".")
                or host == "localhost" or host.endswith((".localhost", ".local", ".internal"))):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if ("." not in host or not re.fullmatch(r"[a-z0-9.-]+", host)
                    or any(not label or len(label) > 63 or label.startswith("-")
                           or label.endswith("-") for label in host.split("."))
                    or host.rsplit(".", 1)[-1].isdigit()):
                raise ValueError
        else:
            if (not address.is_global or address.is_multicast or address.is_reserved
                    or (isinstance(address, ipaddress.IPv6Address)
                        and (address.sixtofour is not None or address.teredo is not None))):
                raise ValueError
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        authority = f"[{host}]" if ":" in host else host
        if port is not None and port != (443 if parsed.scheme == "https" else 80):
            authority += f":{port}"
        suffix = value[len(parsed.scheme) + 3 + len(parsed.netloc):]
        if not suffix.startswith("/"):
            suffix = "/" + suffix
        return f"{parsed.scheme}://{authority}{suffix}"
    except (ValueError, UnicodeError):
        raise ValueError("Invalid public URL") from None


class AssistantInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, strict=True)


class CreateSession(AssistantInput):
    title: str | None = Field(default="\u65b0\u4f1a\u8bdd", max_length=128)
    consent_to_send: bool = False


class RenameSession(AssistantInput):
    title: str = Field(min_length=1, max_length=128)

    @field_validator("title", mode="before")
    @classmethod
    def trim_title(cls, value):
        return value.strip() if isinstance(value, str) else value


class SetTarget(AssistantInput):
    name: str = Field(min_length=1, max_length=128)
    base_url: str = Field(min_length=1, max_length=512)
    credential_mode: Literal["manual", "bitwarden", "cookie", "none"]
    authorized: Literal[True] | None = None
    confirmed: Literal[True] = True
    proxy: bool
    browser_consent: Literal[True] = True
    screenshot_consent: bool = False
    site_id: int | None = Field(default=None, ge=1)
    workflow_site_id: int | None = Field(default=None, ge=1)
    workflow_consent: bool = False
    run_lane_policy: Literal["auto", "general", "interaction", "proxy_general", "proxy_interaction"] = "auto"
    execution_consent: bool = False
    credential_consent: bool = False
    allow_sign: bool = False
    execution_features: list[str] = Field(default_factory=list, max_length=18)
    trusted_origins: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("trusted_origins")
    @classmethod
    def origins(cls, values):
        for value in values:
            parsed = urlsplit(public_url(value))
            if value != f"{parsed.scheme}://{parsed.netloc}":
                raise ValueError("Trusted destinations must be canonical public origins only")
        return list(dict.fromkeys(values))

    @field_validator("authorized", "confirmed", "browser_consent", mode="before")
    @classmethod
    def consent(cls, value, info):
        if info.field_name == "authorized" and value is None:
            return value
        if value is not True:
            raise ValueError("Explicit confirmation required")
        return value

    @model_validator(mode="after")
    def authorization(self):
        if (self.authorized is not True
                and not {"confirmed", "browser_consent"} <= self.model_fields_set):
            raise ValueError("Explicit authorization or legacy confirmations required")
        return self

    @field_validator("base_url")
    @classmethod
    def url(cls, value):
        return public_url(value)


class BrowserRecovery(AssistantInput):
    pass


class RetryApprovalDecision(AssistantInput):
    decision: Literal["approve", "deny"]


def interaction_id(value: str) -> str:
    if (not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value)
            or value in {"__proto__", "constructor", "prototype"}):
        raise ValueError("Invalid interaction ID")
    return value


class InteractionResponse(AssistantInput):
    decision: Literal["approve", "deny"]
    selections: list[str] | None = Field(default=None, max_length=20)
    values: dict[str, str] | None = Field(default=None, max_length=8)

    @model_validator(mode="after")
    def bounded(self):
        for value in self.selections or []:
            interaction_id(value)
        for value in self.values or {}:
            interaction_id(value)
        if self.values is not None and any(
            not 1 <= len(k) <= 64 or len(v) > 2000 for k, v in self.values.items()
        ):
            raise ValueError("Invalid input")
        if self.decision == "deny" and (self.selections is not None or self.values is not None):
            raise ValueError("Denial has no input")
        return self


class InteractionTarget(AssistantInput):
    name: str = Field(min_length=1, max_length=128)
    base_url: str = Field(min_length=1, max_length=512)
    credential_mode: Literal["manual", "bitwarden", "cookie", "none"] = "none"
    site_id: int | None = Field(default=None, ge=1)
    workflow_site_id: int | None = Field(default=None, ge=1)

    @field_validator("name")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Name required")
        return value.strip()

    @field_validator("base_url")
    @classmethod
    def url(cls, value):
        return public_url(value)


class TargetInteraction(AssistantInput):
    purpose: Literal["target"]
    target: InteractionTarget


class TrustedOriginsInteraction(AssistantInput):
    purpose: Literal["trusted_origins"]
    origins: list[str] = Field(min_length=1, max_length=20)

    @field_validator("origins")
    @classmethod
    def canonical(cls, values):
        return SetTarget.origins(values)


class ProxyInteraction(AssistantInput):
    purpose: Literal["proxy"]
    enabled: bool
    scope: Literal["browser", "workflow", "both"] = "both"


class AuthorizationInteraction(AssistantInput):
    purpose: Literal["authorization"]
    capability: Literal["execution", "screenshot", "workflow"]
    feature_type: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def feature(self):
        if self.capability != "execution" and self.feature_type is not None:
            raise ValueError("Only execution has a triggering feature")
        return self


class InteractionOption(AssistantInput):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$",
                    json_schema_extra={"not": {"enum": ["__proto__", "constructor", "prototype"]}})
    label: str = Field(min_length=1, max_length=160, pattern=r"\S")

    @field_validator("id")
    @classmethod
    def identifier(cls, value):
        return interaction_id(value)

    @field_validator("label")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Label required")
        return value


class InteractionField(InteractionOption):
    required: bool = True


class QuestionInteraction(AssistantInput):
    purpose: Literal["question"]
    kind: Literal["authorization", "choice", "input"]
    title: str = Field(min_length=1, max_length=120, pattern=r"\S")
    description: str = Field(default="", max_length=1000)
    options: list[InteractionOption] = Field(default_factory=list, max_length=20)
    multiple: bool = False
    fields: list[InteractionField] = Field(default_factory=list, max_length=8)
    required: bool = True

    @field_validator("title")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Title required")
        return value

    @model_validator(mode="after")
    def shape(self):
        if (self.kind == "choice") != bool(self.options) or (self.kind == "input") != bool(self.fields):
            raise ValueError("Invalid question shape")
        if self.multiple and self.kind != "choice":
            raise ValueError("Multiple choices only")
        for items in (self.options, self.fields):
            if len({item.id for item in items}) != len(items):
                raise ValueError("Duplicate IDs")
        return self


class InteractionPurposeTemplateImport(TemplateFromSiteRequest):
    purpose: Literal["template_import"]


class InteractionPurposeSiteConfig(UpdateSiteConfig):
    purpose: Literal["site_config"]


class InteractionPurposeScheduler(UpdateScheduler):
    purpose: Literal["scheduler_config"]


class ConsumptionInteraction(AssistantInput):
    purpose: Literal["consumption"]
    run_plan_id: str = Field(min_length=36, max_length=36)
    step_key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]{0,63}$")


class CookieClearInteraction(AssistantInput):
    purpose: Literal["cookie_clear"]
    operation_id: str = Field(pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")

    @field_validator("operation_id")
    @classmethod
    def canonical_operation(cls, value):
        return str(UUID(value))


PublicUserInteraction = Annotated[
    TargetInteraction | TrustedOriginsInteraction | ProxyInteraction | AuthorizationInteraction
    | QuestionInteraction, Field(discriminator="purpose"),
]

UserInteraction = Annotated[
    TargetInteraction | TrustedOriginsInteraction | ProxyInteraction | AuthorizationInteraction
    | QuestionInteraction | InteractionPurposeSiteConfig | ConsumptionInteraction
    | InteractionPurposeTemplateImport | InteractionPurposeScheduler | RunCancelInteraction
    | CookieClearInteraction,
    Field(discriminator="purpose"),
]


class AuthorizeRetry(AssistantInput):
    operation_id: UUID
    confirmed: Literal[True]
    request_id: UUID

    @field_validator("confirmed", mode="before")
    @classmethod
    def confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit confirmation required")
        return value

    @field_validator("operation_id", "request_id", mode="before")
    @classmethod
    def request_uuid(cls, value):
        return UUID(value) if isinstance(value, str) else value


class SendMessage(AssistantInput):
    content: str = Field(min_length=1, max_length=12000)
    consent_to_send: Literal[True]
    request_id: UUID

    @field_validator("consent_to_send", mode="before")
    @classmethod
    def consent(cls, value):
        if value is not True:
            raise ValueError("Explicit consent required")
        return value

    @field_validator("request_id", mode="before")
    @classmethod
    def request_uuid(cls, value):
        if isinstance(value, str):
            return UUID(value)
        return value
