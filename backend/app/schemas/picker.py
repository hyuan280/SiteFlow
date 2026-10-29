from __future__ import annotations

from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator


class PickerOpenRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    lane_key: str = Field(default="interaction", max_length=32)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        value = value.strip()
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("URL 必须是包含主机名的 http(s) 地址")
        return value


class PickerSelection(BaseModel):
    selector: str
    tag: str = ""
    text: str = ""
    id: str = ""
    name: str = ""
    url: str = ""


class PickerSessionView(BaseModel):
    session_id: str
    lane_key: str
    url: str
    current_url: str | None = None
    picked: PickerSelection | None = None
    expires_in: int = 0
