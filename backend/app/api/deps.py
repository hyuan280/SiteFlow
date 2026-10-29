from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.auth import CSRF_COOKIE, SESSION_COOKIE, LoginRateLimiter, hash_token, safe_compare
from app.core.config import Settings
from app.core.crypto import CryptoService
from app.models.auth import AuthSession
from app.services.auth import AuthService


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_crypto(request: Request) -> CryptoService:
    return request.app.state.crypto


def get_db(request: Request):  # type: ignore[no-untyped-def]
    session_factory = request.app.state.session_factory
    session: Session = session_factory()
    try:
        yield session
    finally:
        session.close()


def get_auth_service(
    request: Request,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AuthService:
    return AuthService(session, settings, request.app.state.bootstrap)


def get_rate_limiter(request: Request) -> LoginRateLimiter:
    return request.app.state.rate_limiter


def get_current_session(
    request: Request,
    session: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AuthSession:
    raw_token = request.cookies.get(SESSION_COOKIE)
    if not raw_token:
        raise HTTPException(status_code=401, detail="未登录")
    auth = AuthService(session, settings, request.app.state.bootstrap)
    row = auth.get_session(raw_token)
    if row is None:
        raise HTTPException(status_code=401, detail="会话已失效")
    return row


def require_csrf(
    request: Request,
    current: AuthSession = Depends(get_current_session),
) -> AuthSession:
    header = request.headers.get("X-CSRF-Token") or ""
    cookie = request.cookies.get(CSRF_COOKIE) or ""
    if not header or not cookie or not safe_compare(header, cookie):
        raise HTTPException(status_code=403, detail="CSRF 校验失败")
    if not safe_compare(hash_token(header), current.csrf_hash):
        raise HTTPException(status_code=403, detail="CSRF 校验失败")
    return current
