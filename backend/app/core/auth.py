from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError, VerificationError

SESSION_COOKIE = "siteflow_session"
CSRF_COOKIE = "siteflow_csrf"

_password_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
)


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def safe_compare(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@dataclass
class BootstrapState:
    mode: str  # env / db / bootstrap
    username: str
    password_hash: str | None = None


class LoginRateLimiter:
    """Small in-process limiter; SiteFlow runs one API worker."""

    def __init__(self) -> None:
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def is_locked(self, key: str, *, max_attempts: int, window_seconds: int) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            attempts = [item for item in self._failures.get(key, []) if now - item < window_seconds]
            self._failures[key] = attempts
            if len(attempts) < max_attempts:
                return False, 0
            retry_after = max(1, int(window_seconds - (now - min(attempts))))
            return True, retry_after

    def record_failure(self, key: str, *, window_seconds: int) -> None:
        now = time.monotonic()
        with self._lock:
            attempts = [item for item in self._failures.get(key, []) if now - item < window_seconds]
            attempts.append(now)
            self._failures[key] = attempts

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
