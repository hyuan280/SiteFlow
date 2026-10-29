from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from sqlalchemy.orm import Session

from app.core.auth import (
    BootstrapState,
    ensure_aware,
    hash_password,
    hash_token,
    new_csrf_token,
    new_session_token,
    safe_compare,
    utcnow,
    verify_password,
)
from app.core.config import Settings
from app.models.auth import AuthSession, User
from app.schemas.auth import UserView

logger = logging.getLogger("siteflow.auth")


class PasswordChangeForbidden(RuntimeError):
    pass


class InvalidCredentials(RuntimeError):
    pass


def _print_bootstrap_banner(username: str, password: str) -> None:
    lines = [
        "=" * 64,
        "SiteFlow initial admin",
        f"username: {username}",
        f"password: {password}",
        "This password is regenerated on every restart until you change it.",
        "Change it in the web UI to persist it in SQLite.",
        "=" * 64,
    ]
    print("\n".join(lines), flush=True)


def bootstrap_admin(session_factory, settings: Settings) -> BootstrapState:
    """Resolve the single admin credential for this process."""
    env_password = settings.admin_password_value()
    with session_factory() as session:
        if env_password:
            username = settings.admin_username_value()
            logger.info("admin credentials are managed by environment variables (username=%s)", username)
            return BootstrapState(mode="env", username=username)

        user = session.query(User).order_by(User.id.asc()).first()
        if user is not None:
            logger.info("admin password loaded from database (username=%s)", user.username)
            return BootstrapState(mode="db", username=user.username)

        password = secrets.token_urlsafe(18)
        password_hash = hash_password(password)
        # A new bootstrap password invalidates sessions created with the old one.
        session.query(AuthSession).delete()
        session.commit()
        _print_bootstrap_banner("admin", password)
        return BootstrapState(mode="bootstrap", username="admin", password_hash=password_hash)


class AuthService:
    def __init__(self, session: Session, settings: Settings, bootstrap: BootstrapState) -> None:
        self.session = session
        self.settings = settings
        self.bootstrap = bootstrap

    def can_change_password(self) -> bool:
        return self.settings.admin_password_value() is None

    def authenticate(self, username: str, password: str) -> tuple[UserView, str] | None:
        # Environment credentials are authoritative and never fall through to DB.
        env_password = self.settings.admin_password_value()
        if env_password:
            if (
                safe_compare(username, self.settings.admin_username_value())
                and safe_compare(password, env_password)
            ):
                return self._user_view(username, "env"), "env"
            return None

        user = self.session.query(User).filter(User.username == username).first()
        if user is not None and verify_password(user.password_hash, password):
            return self._user_view(user.username, "db"), "db"

        if self.bootstrap.mode == "bootstrap" and self.bootstrap.password_hash:
            if username == self.bootstrap.username and verify_password(
                self.bootstrap.password_hash, password
            ):
                return self._user_view(username, "bootstrap"), "bootstrap"
        return None

    def _user_view(self, username: str, source: str) -> UserView:
        return UserView(
            username=username,
            auth_source=source,
            can_change_password=source != "env" and self.can_change_password(),
        )

    def user_view(self, session: AuthSession) -> UserView:
        return self._user_view(session.username, session.auth_source)

    def create_session(
        self,
        username: str,
        auth_source: str,
        *,
        ip: str | None,
        user_agent: str | None,
    ) -> tuple[str, str, AuthSession]:
        self.delete_expired_sessions()
        raw_token = new_session_token()
        csrf_token = new_csrf_token()
        now = utcnow()
        row = AuthSession(
            token_hash=hash_token(raw_token),
            username=username,
            auth_source=auth_source,
            csrf_hash=hash_token(csrf_token),
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(hours=self.settings.session_ttl_hours),
            ip=ip,
            user_agent=(user_agent or "")[:512] or None,
        )
        self.session.add(row)
        self.session.commit()
        return raw_token, csrf_token, row

    def get_session(self, raw_token: str) -> AuthSession | None:
        token_hash = hash_token(raw_token)
        row = self.session.query(AuthSession).filter(AuthSession.token_hash == token_hash).first()
        if row is None:
            return None
        now = utcnow()
        if ensure_aware(row.expires_at) <= now:
            self.session.delete(row)
            self.session.commit()
            return None
        if (now - ensure_aware(row.last_seen_at)).total_seconds() > 60:
            row.last_seen_at = now
            self.session.commit()
        return row

    def delete_session(self, row: AuthSession) -> None:
        self.session.delete(row)
        self.session.commit()

    def delete_expired_sessions(self) -> None:
        self.session.query(AuthSession).filter(AuthSession.expires_at <= utcnow()).delete(
            synchronize_session=False
        )
        self.session.commit()
        self.session.expunge_all()

    def change_password(self, session: AuthSession, current_password: str, new_password: str) -> UserView:
        if self.settings.admin_password_value():
            raise PasswordChangeForbidden("管理员密码由环境变量管理，不能在系统中修改")

        if session.auth_source == "db":
            user = self.session.query(User).filter(User.username == session.username).first()
            if user is None or not verify_password(user.password_hash, current_password):
                raise InvalidCredentials("当前密码错误")
        elif session.auth_source == "bootstrap":
            if not self.bootstrap.password_hash or not verify_password(
                self.bootstrap.password_hash, current_password
            ):
                raise InvalidCredentials("当前密码错误")
            user = self.session.query(User).filter(User.username == session.username).first()
            if user is None:
                user = User(username=session.username, password_hash="")
                self.session.add(user)
        else:
            raise PasswordChangeForbidden("当前会话不能修改密码")

        now = utcnow()
        user.password_hash = hash_password(new_password)
        user.updated_at = now
        user.password_changed_at = now
        self.session.query(AuthSession).filter(AuthSession.username == session.username).delete(
            synchronize_session=False
        )
        self.session.commit()
        self.session.expunge_all()

        # Persisted DB credentials supersede the in-memory bootstrap password.
        self.bootstrap.mode = "db"
        self.bootstrap.username = user.username
        self.bootstrap.password_hash = None
        logger.info("admin password persisted to database (username=%s)", user.username)
        return self._user_view(user.username, "db")
