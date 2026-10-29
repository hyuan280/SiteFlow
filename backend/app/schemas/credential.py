from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SiteCredentialView(BaseModel):
    configured: bool = False
    username_masked: str | None = None
    cookies_configured: bool = False
    cookie_count: int = 0
    updated_at: datetime | None = None
    cookies_updated_at: datetime | None = None
    last_verified_at: datetime | None = None
    last_error: str | None = None
    error: str | None = None


class SiteCookieUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cookies: str = Field(min_length=1, max_length=200_000)


class SiteCredentialUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str | None = Field(default=None, min_length=1, max_length=512)
    password: str | None = Field(default=None, min_length=1, max_length=2048)

    @field_validator("username")
    @classmethod
    def _strip_username(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("用户名不能为空")
        return value
