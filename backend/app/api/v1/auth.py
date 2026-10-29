from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.api.deps import (
    get_auth_service,
    get_current_session,
    get_rate_limiter,
    get_settings,
    require_csrf,
)
from app.core.auth import CSRF_COOKIE, SESSION_COOKIE, LoginRateLimiter, hash_token
from app.core.config import Settings
from app.models.auth import AuthSession
from app.schemas.auth import (
    AuthResponse,
    LoginRequest,
    MessageResponse,
    PasswordChangeRequest,
)
from app.services.auth import AuthService, InvalidCredentials, PasswordChangeForbidden

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _cookie_kwargs(settings: Settings) -> dict[str, object]:
    return {
        "path": "/",
        "secure": settings.cookie_secure,
        "samesite": "lax",
        "max_age": settings.session_ttl_hours * 3600,
    }


def _set_auth_cookies(response: Response, settings: Settings, token: str, csrf: str) -> None:
    kwargs = _cookie_kwargs(settings)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, **kwargs)
    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, **kwargs)


def _clear_auth_cookies(response: Response, settings: Settings) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", secure=settings.cookie_secure, samesite="lax")
    response.delete_cookie(CSRF_COOKIE, path="/", secure=settings.cookie_secure, samesite="lax")


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _rate_key(request: Request, username: str) -> str:
    return f"{username.strip().lower()}|{_client_ip(request)}"


@router.post("/login", response_model=AuthResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
    auth: AuthService = Depends(get_auth_service),
    limiter: LoginRateLimiter = Depends(get_rate_limiter),
) -> AuthResponse:
    key = _rate_key(request, payload.username)
    locked, retry_after = limiter.is_locked(
        key,
        max_attempts=settings.login_max_attempts,
        window_seconds=settings.login_lockout_seconds,
    )
    if locked:
        raise HTTPException(
            status_code=429,
            detail=f"登录失败次数过多，请 {retry_after} 秒后再试",
            headers={"Retry-After": str(retry_after)},
        )

    result = auth.authenticate(payload.username.strip(), payload.password)
    if result is None:
        limiter.record_failure(key, window_seconds=settings.login_lockout_seconds)
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    limiter.reset(key)
    user, source = result
    token, csrf, _ = auth.create_session(
        user.username,
        source,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    _set_auth_cookies(response, settings, token, csrf)
    return AuthResponse(user=user, csrf_token=csrf)


@router.get("/me", response_model=AuthResponse)
def me(
    request: Request,
    current: AuthSession = Depends(get_current_session),
    auth: AuthService = Depends(get_auth_service),
) -> AuthResponse:
    csrf = request.cookies.get(CSRF_COOKIE) or ""
    if not csrf or hash_token(csrf) != current.csrf_hash:
        raise HTTPException(status_code=401, detail="会话缺少有效的 CSRF 凭据")
    return AuthResponse(user=auth.user_view(current), csrf_token=csrf)


@router.post("/logout", response_model=MessageResponse)
def logout(
    response: Response,
    current: AuthSession = Depends(require_csrf),
    auth: AuthService = Depends(get_auth_service),
    settings: Settings = Depends(get_settings),
) -> MessageResponse:
    auth.delete_session(current)
    _clear_auth_cookies(response, settings)
    return MessageResponse(message="已退出登录")


@router.post("/password", response_model=AuthResponse)
def change_password(
    payload: PasswordChangeRequest,
    request: Request,
    response: Response,
    current: AuthSession = Depends(require_csrf),
    auth: AuthService = Depends(get_auth_service),
    settings: Settings = Depends(get_settings),
) -> AuthResponse:
    try:
        user = auth.change_password(current, payload.current_password, payload.new_password)
    except PasswordChangeForbidden as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except InvalidCredentials as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    token, csrf, _ = auth.create_session(
        user.username,
        "db",
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    _set_auth_cookies(response, settings, token, csrf)
    return AuthResponse(user=user, csrf_token=csrf)
