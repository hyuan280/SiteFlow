from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.integration import ConfigSource


class NotificationFieldView(BaseModel):
    value: Any = None
    source: ConfigSource
    editable: bool
    error: str | None = None


class NotificationSecretView(BaseModel):
    configured: bool
    source: ConfigSource
    editable: bool
    masked: str | None = None


class NotificationLayoutView(BaseModel):
    title_template: str
    body_template: str
    format: Literal["text", "html"]
    editable: bool = True


class EmailNotificationView(BaseModel):
    enabled: bool
    notify_success: bool
    notify_manual: bool
    available: bool
    smtp_host: NotificationFieldView
    smtp_port: NotificationFieldView
    security: NotificationFieldView
    username: NotificationFieldView
    password: NotificationSecretView
    from_address: NotificationFieldView
    to_addresses: NotificationFieldView
    error: str | None = None


class HTTPNotificationView(BaseModel):
    enabled: bool
    notify_success: bool
    notify_manual: bool
    available: bool
    url: NotificationFieldView
    method: NotificationFieldView
    headers: NotificationFieldView
    header_names: list[str] = Field(default_factory=list)
    timeout_seconds: NotificationFieldView
    error: str | None = None


class NotificationSettingsView(BaseModel):
    layout: NotificationLayoutView
    email: EmailNotificationView
    http: HTTPNotificationView


class NotificationLayoutUpdate(BaseModel):
    title_template: str = Field(min_length=1, max_length=200)
    body_template: str = Field(min_length=1, max_length=20000)
    format: Literal["text", "html"]


class EmailNotificationUpdate(BaseModel):
    enabled: bool | None = None
    notify_success: bool | None = None
    notify_manual: bool | None = None
    smtp_host: str | None = Field(default=None, max_length=255)
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    security: Literal["ssl", "starttls", "none"] | None = None
    username: str | None = Field(default=None, max_length=320)
    password: str | None = Field(default=None, max_length=2048)
    from_address: str | None = Field(default=None, max_length=320)
    to_addresses: list[str] | None = None


class HTTPNotificationUpdate(BaseModel):
    enabled: bool | None = None
    notify_success: bool | None = None
    notify_manual: bool | None = None
    url: str | None = Field(default=None, max_length=2048)
    method: Literal["POST", "PUT", "PATCH"] | None = None
    headers: dict[str, str] | None = None
    timeout_seconds: float | None = Field(default=None, ge=1, le=120)


class ChannelDeliveryResult(BaseModel):
    channel: str
    ok: bool
    message: str


class NotificationTestResult(BaseModel):
    ok: bool
    message: str
    results: list[ChannelDeliveryResult] = Field(default_factory=list)
