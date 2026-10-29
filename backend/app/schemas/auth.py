from __future__ import annotations

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class UserView(BaseModel):
    username: str
    auth_source: str
    can_change_password: bool


class AuthResponse(BaseModel):
    user: UserView
    csrf_token: str


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=8, max_length=1024)


class MessageResponse(BaseModel):
    ok: bool = True
    message: str
