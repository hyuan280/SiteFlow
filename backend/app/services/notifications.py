from __future__ import annotations

import html
import json
import logging
import re
import smtplib
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.crypto import CryptoError, CryptoService
from app.models.execution import Run, SiteJob
from app.models.integration import IntegrationConfig, utcnow
from app.models.setting import AppSetting
from app.schemas.integration import ConfigSource
from app.schemas.notification import (
    ChannelDeliveryResult,
    EmailNotificationUpdate,
    EmailNotificationView,
    HTTPNotificationUpdate,
    HTTPNotificationView,
    NotificationFieldView,
    NotificationLayoutUpdate,
    NotificationLayoutView,
    NotificationSecretView,
    NotificationSettingsView,
    NotificationTestResult,
)
from app.services.execution import RUN_STATUS_LABELS

logger = logging.getLogger("siteflow.notifications")

EMAIL_CONFIG_KEY = "notification.email"
HTTP_CONFIG_KEY = "notification.http"
EMAIL_ENABLED_KEY = "notification.email.enabled"
HTTP_ENABLED_KEY = "notification.http.enabled"
EMAIL_NOTIFY_SUCCESS_KEY = "notification.email.notify_success"
EMAIL_NOTIFY_MANUAL_KEY = "notification.email.notify_manual"
HTTP_NOTIFY_SUCCESS_KEY = "notification.http.notify_success"
HTTP_NOTIFY_MANUAL_KEY = "notification.http.notify_manual"
LAYOUT_KEY = "notification.layout"

EMAIL_SECURITY_VALUES = {"ssl", "starttls", "none"}
HTTP_METHOD_VALUES = {"POST", "PUT", "PATCH"}
DEFAULT_HTTP_TIMEOUT = 10.0
DEFAULT_EMAIL_PORT = 587
DEFAULT_EMAIL_SECURITY = "starttls"

DEFAULT_LAYOUT_TITLE = "[SiteFlow] 运行 #{{run_id}} {{status_label}}"
DEFAULT_LAYOUT_BODY = """SiteFlow 运行 #{{run_id}} 已结束
状态：{{status_label}}
站点：{{site_count}} 个（成功 {{success_count}}，失败 {{fail_count}}，等待人工 {{waiting_count}}）
触发：{{trigger_label}}
结束时间：{{finished_at}}

{{sites}}"""
DEFAULT_LAYOUT_FORMAT = "text"

STATUS_LABELS = {
    **RUN_STATUS_LABELS,
    "deferred": "已推迟",
    "skipped": "已跳过",
}

PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


class NotificationConfigLockedError(RuntimeError):
    pass


class NotificationValidationError(ValueError):
    pass


class NotificationDeliveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeliveryTarget:
    channel: str
    enabled: bool
    values: dict[str, Any] | None
    error: str | None = None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _secret_text(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "get_secret_value"):
        value = value.get_secret_value()
    text = str(value)
    return text if text else None


def _json_object(value: str | None) -> tuple[dict[str, Any], str | None]:
    if value is None or not value.strip():
        return {}, None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        return {}, f"请求头 JSON 无效：{exc.msg}"
    if not isinstance(parsed, dict):
        return {}, "请求头必须是 JSON 对象"
    headers: dict[str, str] = {}
    for key, item in parsed.items():
        if not isinstance(key, str) or not isinstance(item, str):
            return {}, "请求头的键和值都必须是字符串"
        if "\r" in key or "\n" in key or "\r" in item or "\n" in item:
            return {}, "请求头不能包含换行"
        headers[key] = item
    return headers, None


def _split_addresses(value: str | None) -> list[str] | None:
    if value is None:
        return None
    items = [item.strip() for item in re.split(r"[,;\n]", value) if item.strip()]
    return items or None


def _validate_address(value: str, label: str) -> str:
    if any(char in value for char in "\r\n"):
        raise NotificationValidationError(f"{label}不能包含换行")
    _, address = parseaddr(value.strip())
    if not address or "@" not in address or address != address.strip():
        raise NotificationValidationError(f"{label}不是有效邮箱地址")
    local, _, domain = address.rpartition("@")
    if not local or not domain or "." not in domain:
        raise NotificationValidationError(f"{label}不是有效邮箱地址")
    return address


def _validate_addresses(values: list[str] | None, label: str) -> list[str]:
    if not values:
        raise NotificationValidationError(f"请至少填写一个{label}")
    cleaned: list[str] = []
    for item in values:
        address = _validate_address(item, label)
        if address not in cleaned:
            cleaned.append(address)
    return cleaned


def validate_http_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme.lower() not in {"http", "https"}:
        raise NotificationValidationError("HTTP 通知地址必须使用 http 或 https")
    if not parsed.hostname:
        raise NotificationValidationError("HTTP 通知地址必须包含主机名")
    return value.strip()


def validate_headers(headers: Any) -> dict[str, str]:
    if not isinstance(headers, dict):
        raise NotificationValidationError("请求头必须是 JSON 对象")
    result: dict[str, str] = {}
    for key, value in headers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise NotificationValidationError("请求头的键和值都必须是字符串")
        key = key.strip()
        if not key:
            raise NotificationValidationError("请求头名称不能为空")
        if any(char in key for char in "\r\n:") or "\r" in value or "\n" in value:
            raise NotificationValidationError("请求头包含非法字符")
        result[key] = value
    return result


def _email_errors(values: dict[str, Any], *, require_complete: bool) -> dict[str, str]:
    errors: dict[str, str] = {}
    host = _text(values.get("smtp_host"))
    if require_complete and not host:
        errors["smtp_host"] = "SMTP 主机未配置"
    port = values.get("smtp_port")
    if port is not None:
        try:
            port_value = int(port)
        except (TypeError, ValueError):
            errors["smtp_port"] = "SMTP 端口必须是数字"
        else:
            if not 1 <= port_value <= 65535:
                errors["smtp_port"] = "SMTP 端口必须在 1–65535 之间"
    elif require_complete:
        errors["smtp_port"] = "SMTP 端口未配置"

    security = _text(values.get("security")) or DEFAULT_EMAIL_SECURITY
    if security not in EMAIL_SECURITY_VALUES:
        errors["security"] = "加密方式必须是 SSL、STARTTLS 或 none"

    username = _text(values.get("username"))
    password = _secret_text(values.get("password"))
    if username and not password:
        errors["password"] = "填写用户名时必须配置密码"

    from_address = _text(values.get("from_address"))
    if from_address:
        try:
            _validate_address(from_address, "发件人")
        except NotificationValidationError as exc:
            errors["from_address"] = str(exc)
    elif require_complete:
        errors["from_address"] = "发件人未配置"

    to_addresses = values.get("to_addresses")
    if to_addresses:
        try:
            _validate_addresses([str(item) for item in to_addresses], "收件人")
        except NotificationValidationError as exc:
            errors["to_addresses"] = str(exc)
    elif require_complete:
        errors["to_addresses"] = "收件人未配置"
    return errors


def _http_errors(values: dict[str, Any], *, require_complete: bool) -> dict[str, str]:
    errors: dict[str, str] = {}
    url = _text(values.get("url"))
    if url:
        try:
            validate_http_url(url)
        except NotificationValidationError as exc:
            errors["url"] = str(exc)
    elif require_complete:
        errors["url"] = "HTTP 通知地址未配置"

    method = _text(values.get("method")) or "POST"
    if method.upper() not in HTTP_METHOD_VALUES:
        errors["method"] = "请求方法必须是 POST、PUT 或 PATCH"

    headers = values.get("headers")
    if headers is not None:
        try:
            validate_headers(headers)
        except NotificationValidationError as exc:
            errors["headers"] = str(exc)

    timeout = values.get("timeout_seconds")
    if timeout is not None:
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError):
            errors["timeout_seconds"] = "超时时间必须是数字"
        else:
            if not 1 <= timeout_value <= 120:
                errors["timeout_seconds"] = "超时时间必须在 1–120 秒之间"
    return errors


class NotificationService:
    def __init__(self, session: Session, settings: Settings, crypto: CryptoService) -> None:
        self.session = session
        self.settings = settings
        self.crypto = crypto

    # ── storage and resolution ─────────────────────────────────────────

    def _load_channel(self, key: str) -> tuple[dict[str, Any], str | None]:
        row = self.session.get(IntegrationConfig, key)
        if row is None:
            return {}, None
        try:
            plaintext = self.crypto.decrypt(key, row.value_ciphertext, row.nonce, row.key_version)
        except CryptoError:
            return {}, "通知配置解密失败"
        try:
            parsed = json.loads(plaintext)
        except json.JSONDecodeError:
            return {}, "通知配置不是合法 JSON"
        if not isinstance(parsed, dict):
            return {}, "通知配置必须是 JSON 对象"
        return parsed, None

    def _save_channel(self, key: str, values: dict[str, Any]) -> None:
        if not values:
            self._delete_channel(key)
            return
        plaintext = json.dumps(values, ensure_ascii=False, sort_keys=True)
        encrypted = self.crypto.encrypt(key, plaintext)
        row = self.session.get(IntegrationConfig, key)
        if row is None:
            row = IntegrationConfig(key=key)
            self.session.add(row)
        row.value_ciphertext = encrypted.ciphertext
        row.nonce = encrypted.nonce
        row.key_version = encrypted.key_version
        row.is_secret = True
        row.fingerprint = self.crypto.fingerprint(plaintext)
        row.updated_at = utcnow()
        row.updated_by = "api"

    def _delete_channel(self, key: str) -> None:
        row = self.session.get(IntegrationConfig, key)
        if row is not None:
            self.session.delete(row)

    def _env_email(self) -> tuple[dict[str, Any], dict[str, str]]:
        errors: dict[str, str] = {}
        headers: dict[str, Any] = {}
        values = {
            "smtp_host": _text(self.settings.notification_email_smtp_host),
            "smtp_port": self.settings.notification_email_smtp_port,
            "security": _text(self.settings.notification_email_security),
            "username": _text(self.settings.notification_email_username),
            "password": _secret_text(self.settings.notification_email_password),
            "from_address": _text(self.settings.notification_email_from),
            "to_addresses": _split_addresses(self.settings.notification_email_to),
        }
        for key, value in values.items():
            if value is not None:
                headers[key] = value
        return headers, errors

    def _env_http(self) -> tuple[dict[str, Any], dict[str, str]]:
        errors: dict[str, str] = {}
        env_headers, header_error = _json_object(self.settings.notification_http_headers)
        if header_error:
            errors["headers"] = header_error
        values = {
            "url": _text(self.settings.notification_http_url),
            "method": _text(self.settings.notification_http_method),
            "headers": env_headers if self.settings.notification_http_headers else None,
            "timeout_seconds": self.settings.notification_http_timeout,
        }
        present = {key: value for key, value in values.items() if value is not None}
        return present, errors

    @staticmethod
    def _pick(
        env_values: dict[str, Any],
        db_values: dict[str, Any],
        field: str,
        default: Any = None,
    ) -> tuple[Any, ConfigSource, bool]:
        if field in env_values and env_values[field] is not None:
            return env_values[field], ConfigSource.ENV, False
        if field in db_values and db_values[field] not in (None, "", []):
            return db_values[field], ConfigSource.DB, True
        return default, ConfigSource.UNSET, True

    def _enabled(self, channel: str) -> bool:
        key = EMAIL_ENABLED_KEY if channel == "email" else HTTP_ENABLED_KEY
        row = self.session.get(AppSetting, key)
        if row is not None:
            try:
                return bool(json.loads(row.value_json))
            except (json.JSONDecodeError, TypeError):
                pass
        env_value = (
            self.settings.notification_email_enabled
            if channel == "email"
            else self.settings.notification_http_enabled
        )
        return bool(env_value)

    def _set_enabled(self, channel: str, value: bool) -> None:
        key = EMAIL_ENABLED_KEY if channel == "email" else HTTP_ENABLED_KEY
        self._set_bool_setting(key, value)

    @staticmethod
    def _flag_key(channel: str, name: str) -> str:
        keys = {
            ("email", "success"): EMAIL_NOTIFY_SUCCESS_KEY,
            ("email", "manual"): EMAIL_NOTIFY_MANUAL_KEY,
            ("http", "success"): HTTP_NOTIFY_SUCCESS_KEY,
            ("http", "manual"): HTTP_NOTIFY_MANUAL_KEY,
        }
        return keys[(channel, name)]

    def _flag(self, channel: str, name: str) -> bool:
        row = self.session.get(AppSetting, self._flag_key(channel, name))
        if row is not None:
            try:
                return bool(json.loads(row.value_json))
            except (json.JSONDecodeError, TypeError):
                pass
        return True

    def _set_flag(self, channel: str, name: str, value: bool) -> None:
        self._set_bool_setting(self._flag_key(channel, name), value)

    def _set_bool_setting(self, key: str, value: bool) -> None:
        row = self.session.get(AppSetting, key)
        if row is None:
            row = AppSetting(key=key)
            self.session.add(row)
        row.value_json = json.dumps(bool(value))
        row.updated_at = datetime.now(timezone.utc)

    def _accepts_run(self, channel: str, event: dict[str, Any]) -> bool:
        if not self._flag(channel, "success") and event.get("status") == "success":
            return False
        if not self._flag(channel, "manual") and event.get("trigger") != "schedule":
            return False
        return True

    def _layout(self) -> dict[str, str]:
        row = self.session.get(AppSetting, LAYOUT_KEY)
        layout = {
            "title_template": DEFAULT_LAYOUT_TITLE,
            "body_template": DEFAULT_LAYOUT_BODY,
            "format": DEFAULT_LAYOUT_FORMAT,
        }
        if row is None:
            return layout
        try:
            stored = json.loads(row.value_json)
        except (json.JSONDecodeError, TypeError):
            return layout
        if not isinstance(stored, dict):
            return layout
        if isinstance(stored.get("title_template"), str) and stored["title_template"].strip():
            layout["title_template"] = stored["title_template"]
        if isinstance(stored.get("body_template"), str) and stored["body_template"].strip():
            layout["body_template"] = stored["body_template"]
        if stored.get("format") in {"text", "html"}:
            layout["format"] = stored["format"]
        return layout

    def _layout_error(self, title: str, body: str) -> str | None:
        if not title.strip() or len(title) > 200:
            return "通知标题必须为 1–200 字符"
        if not body.strip() or len(body) > 20000:
            return "通知布局正文必须为 1–20000 字符"
        return None

    def _email_values(self, db: dict[str, Any], env: dict[str, Any]) -> dict[str, Any]:
        return {
            "smtp_host": self._pick(env, db, "smtp_host")[0],
            "smtp_port": self._pick(env, db, "smtp_port", DEFAULT_EMAIL_PORT)[0],
            "security": self._pick(env, db, "security", DEFAULT_EMAIL_SECURITY)[0],
            "username": self._pick(env, db, "username")[0],
            "password": self._pick(env, db, "password")[0],
            "from_address": self._pick(env, db, "from_address")[0],
            "to_addresses": self._pick(env, db, "to_addresses")[0],
        }

    def _http_values(self, db: dict[str, Any], env: dict[str, Any]) -> dict[str, Any]:
        return {
            "url": self._pick(env, db, "url")[0],
            "method": self._pick(env, db, "method", "POST")[0],
            "headers": self._pick(env, db, "headers", {})[0] or {},
            "timeout_seconds": self._pick(
                env, db, "timeout_seconds", DEFAULT_HTTP_TIMEOUT
            )[0],
        }

    def _email_view(self) -> EmailNotificationView:
        db, db_error = self._load_channel(EMAIL_CONFIG_KEY)
        env, env_errors = self._env_email()
        values = self._email_values(db, env)
        errors = _email_errors(values, require_complete=True)
        if db_error:
            errors["smtp_host"] = db_error

        def field(name: str, default: Any = None) -> NotificationFieldView:
            value, source, editable = self._pick(env, db, name, values.get(name, default))
            return NotificationFieldView(
                value=value,
                source=source,
                editable=editable,
                error=errors.get(name) or env_errors.get(name),
            )

        password, password_source, password_editable = self._pick(env, db, "password")
        row = self.session.get(IntegrationConfig, EMAIL_CONFIG_KEY)
        return EmailNotificationView(
            enabled=self._enabled("email"),
            notify_success=self._flag("email", "success"),
            notify_manual=self._flag("email", "manual"),
            available=not errors,
            smtp_host=field("smtp_host"),
            smtp_port=field("smtp_port"),
            security=field("security", DEFAULT_EMAIL_SECURITY),
            username=field("username"),
            password=NotificationSecretView(
                configured=bool(password),
                source=password_source,
                editable=password_editable,
                masked=(
                    f"****{row.fingerprint}"
                    if password and password_source == ConfigSource.DB and row is not None
                    else None
                ),
            ),
            from_address=field("from_address"),
            to_addresses=field("to_addresses"),
            error="；".join(dict.fromkeys(errors.values())) if errors else None,
        )

    def _http_view(self) -> HTTPNotificationView:
        db, db_error = self._load_channel(HTTP_CONFIG_KEY)
        env, env_errors = self._env_http()
        values = self._http_values(db, env)
        errors = _http_errors(values, require_complete=True)
        if db_error:
            errors["url"] = db_error

        def field(name: str, default: Any = None) -> NotificationFieldView:
            value, source, editable = self._pick(env, db, name, values.get(name, default))
            return NotificationFieldView(
                value=value,
                source=source,
                editable=editable,
                error=errors.get(name) or env_errors.get(name),
            )

        _, headers_source, headers_editable = self._pick(env, db, "headers")
        headers_value = values.get("headers") or {}
        header_names = sorted(headers_value) if isinstance(headers_value, dict) else []
        return HTTPNotificationView(
            enabled=self._enabled("http"),
            notify_success=self._flag("http", "success"),
            notify_manual=self._flag("http", "manual"),
            available=not errors,
            url=field("url"),
            method=field("method", "POST"),
            headers=NotificationFieldView(
                value=None,
                source=headers_source,
                editable=headers_editable,
                error=errors.get("headers") or env_errors.get("headers"),
            ),
            header_names=header_names,
            timeout_seconds=field("timeout_seconds", DEFAULT_HTTP_TIMEOUT),
            error="；".join(dict.fromkeys(errors.values())) if errors else None,
        )

    def view(self) -> NotificationSettingsView:
        layout = self._layout()
        return NotificationSettingsView(
            layout=NotificationLayoutView(**layout),
            email=self._email_view(),
            http=self._http_view(),
        )

    # ── updates ────────────────────────────────────────────────────────

    def update_layout(self, payload: NotificationLayoutUpdate) -> NotificationSettingsView:
        error = self._layout_error(payload.title_template, payload.body_template)
        if error:
            raise NotificationValidationError(error)
        row = self.session.get(AppSetting, LAYOUT_KEY)
        if row is None:
            row = AppSetting(key=LAYOUT_KEY)
            self.session.add(row)
        row.value_json = json.dumps(
            {
                "title_template": payload.title_template,
                "body_template": payload.body_template,
                "format": payload.format,
            },
            ensure_ascii=False,
        )
        row.updated_at = datetime.now(timezone.utc)
        self.session.commit()
        return self.view()

    def _normalize_email_updates(self, payload: EmailNotificationUpdate) -> dict[str, Any]:
        updates = payload.model_dump(
            exclude_unset=True, exclude={"enabled", "notify_success", "notify_manual"}
        )
        normalized: dict[str, Any] = {}
        for field, value in updates.items():
            if field == "smtp_host":
                normalized[field] = _text(value)
            elif field == "smtp_port":
                normalized[field] = int(value) if value is not None else None
            elif field == "security":
                normalized[field] = _text(value)
            elif field == "username":
                normalized[field] = _text(value)
            elif field == "password":
                normalized[field] = value if value else None
            elif field == "from_address":
                normalized[field] = _text(value)
            elif field == "to_addresses":
                normalized[field] = (
                    [str(item).strip() for item in value if str(item).strip()]
                    if value is not None
                    else None
                )
        return normalized

    def _normalize_http_updates(self, payload: HTTPNotificationUpdate) -> dict[str, Any]:
        updates = payload.model_dump(
            exclude_unset=True, exclude={"enabled", "notify_success", "notify_manual"}
        )
        normalized: dict[str, Any] = {}
        for field, value in updates.items():
            if field == "url":
                normalized[field] = _text(value)
            elif field == "method":
                normalized[field] = (_text(value) or "").upper() or None
            elif field == "headers":
                normalized[field] = validate_headers(value) if value is not None else None
            elif field == "timeout_seconds":
                normalized[field] = float(value) if value is not None else None
        return normalized

    def update_email(self, payload: EmailNotificationUpdate) -> NotificationSettingsView:
        db, db_error = self._load_channel(EMAIL_CONFIG_KEY)
        if db_error:
            raise NotificationValidationError(f"{db_error}，请先清除数据库配置")
        env, _ = self._env_email()
        updates = self._normalize_email_updates(payload)
        for field in updates:
            if field in env:
                raise NotificationConfigLockedError(
                    f"邮件通知字段 {field} 已由环境变量锁定"
                )
        proposed = dict(db)
        for field, value in updates.items():
            if value is None or value == [] or value == "":
                proposed.pop(field, None)
            else:
                proposed[field] = value
        effective = self._email_values(proposed, env)
        effective_enabled = payload.enabled if payload.enabled is not None else self._enabled("email")
        errors = _email_errors(effective, require_complete=bool(effective_enabled))
        if errors:
            raise NotificationValidationError("；".join(dict.fromkeys(errors.values())))
        self._save_channel(EMAIL_CONFIG_KEY, proposed)
        if payload.enabled is not None:
            self._set_enabled("email", payload.enabled)
        if payload.notify_success is not None:
            self._set_flag("email", "success", payload.notify_success)
        if payload.notify_manual is not None:
            self._set_flag("email", "manual", payload.notify_manual)
        self.session.commit()
        return self.view()

    def update_http(self, payload: HTTPNotificationUpdate) -> NotificationSettingsView:
        db, db_error = self._load_channel(HTTP_CONFIG_KEY)
        if db_error:
            raise NotificationValidationError(f"{db_error}，请先清除数据库配置")
        env, _ = self._env_http()
        updates = self._normalize_http_updates(payload)
        for field in updates:
            if field in env:
                raise NotificationConfigLockedError(
                    f"HTTP 通知字段 {field} 已由环境变量锁定"
                )
        proposed = dict(db)
        for field, value in updates.items():
            if value is None or value == "":
                proposed.pop(field, None)
            else:
                proposed[field] = value
        effective = self._http_values(proposed, env)
        effective_enabled = payload.enabled if payload.enabled is not None else self._enabled("http")
        errors = _http_errors(effective, require_complete=bool(effective_enabled))
        if errors:
            raise NotificationValidationError("；".join(dict.fromkeys(errors.values())))
        self._save_channel(HTTP_CONFIG_KEY, proposed)
        if payload.enabled is not None:
            self._set_enabled("http", payload.enabled)
        if payload.notify_success is not None:
            self._set_flag("http", "success", payload.notify_success)
        if payload.notify_manual is not None:
            self._set_flag("http", "manual", payload.notify_manual)
        self.session.commit()
        return self.view()

    def delete_email(self) -> NotificationSettingsView:
        self._delete_channel(EMAIL_CONFIG_KEY)
        self.session.commit()
        return self.view()

    def delete_http(self) -> NotificationSettingsView:
        self._delete_channel(HTTP_CONFIG_KEY)
        self.session.commit()
        return self.view()

    # ── rendering ──────────────────────────────────────────────────────

    def _timezone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.settings.scheduler_timezone)
        except ZoneInfoNotFoundError:
            return ZoneInfo("UTC")

    def _format_time(self, value: datetime | None) -> str:
        if value is None:
            return "-"
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(self._timezone()).strftime("%Y-%m-%d %H:%M:%S %Z")

    def _run_event(self, run_id: int) -> dict[str, Any]:
        run = self.session.get(Run, run_id)
        if run is None:
            raise NotificationValidationError(f"运行 {run_id} 不存在")
        jobs = list(
            self.session.query(SiteJob)
            .filter(SiteJob.run_id == run_id)
            .order_by(SiteJob.id)
            .all()
        )
        counts: dict[str, int] = {}
        for job in jobs:
            counts[job.status] = counts.get(job.status, 0) + 1
        status_label = STATUS_LABELS.get(run.status, run.status)
        trigger_label = {"schedule": "定时调度", "manual": "手动执行"}.get(
            run.trigger, run.trigger
        )
        site_rows = [
            {
                "site_id": job.site_id,
                "site_name": job.site_name,
                "status": job.status,
                "status_label": STATUS_LABELS.get(job.status, job.status),
                "message": (job.message or "")[:500],
                "lane": job.lane,
                "attempt": job.attempt,
            }
            for job in jobs
        ]
        return {
            "run_id": run.id,
            "status": run.status,
            "status_label": status_label,
            "trigger": run.trigger,
            "trigger_label": trigger_label,
            "mode": run.mode,
            "created_by": run.created_by,
            "started_at": self._format_time(run.started_at),
            "finished_at": self._format_time(run.finished_at),
            "created_at": self._format_time(run.created_at),
            "site_count": len(jobs),
            "success_count": counts.get("success", 0),
            "fail_count": counts.get("fail", 0) + counts.get("error", 0),
            "waiting_count": counts.get("waiting_human", 0),
            "unknown_count": counts.get("unknown", 0),
            "deferred_count": counts.get("deferred", 0),
            "canceled_count": counts.get("canceled", 0),
            "sites": site_rows,
        }

    def _sample_event(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        return {
            "run_id": 0,
            "status": "fail",
            "status_label": "失败",
            "trigger": "manual",
            "trigger_label": "手动执行",
            "mode": "manual",
            "created_by": "admin",
            "started_at": self._format_time(now),
            "finished_at": self._format_time(now),
            "created_at": self._format_time(now),
            "site_count": 3,
            "success_count": 2,
            "fail_count": 1,
            "waiting_count": 0,
            "unknown_count": 0,
            "deferred_count": 0,
            "canceled_count": 0,
            "sites": [
                {
                    "site_id": None,
                    "site_name": "示例站点 A",
                    "status": "success",
                    "status_label": "成功",
                    "message": "签到成功",
                    "lane": "general",
                    "attempt": 1,
                },
                {
                    "site_id": None,
                    "site_name": "示例站点 B",
                    "status": "success",
                    "status_label": "成功",
                    "message": "签到成功",
                    "lane": "general",
                    "attempt": 1,
                },
                {
                    "site_id": None,
                    "site_name": "示例站点 C",
                    "status": "fail",
                    "status_label": "失败",
                    "message": "登录失败",
                    "lane": "general",
                    "attempt": 1,
                },
            ],
        }

    def _render(self, event: dict[str, Any]) -> dict[str, str]:
        layout = self._layout()
        raw_context = dict(event)
        site_lines = [
            f"{item['site_name']}：{item['status_label']}"
            + (f" · {item['message']}" if item.get("message") else "")
            for item in event.get("sites", [])
        ]
        raw_context["sites"] = "\n".join(site_lines)
        raw_context["summary"] = (
            f"成功 {event['success_count']}，失败 {event['fail_count']}，"
            f"等待人工 {event['waiting_count']}"
        )
        if layout["format"] == "html":
            body_context = {
                key: html.escape(str(value)) if not isinstance(value, list) else value
                for key, value in raw_context.items()
            }
            body_context["sites"] = "<br>".join(html.escape(line) for line in site_lines)
            body_context["summary"] = html.escape(raw_context["summary"])
        else:
            body_context = raw_context

        def render(template: str, context: dict[str, Any]) -> str:
            def replace(match: re.Match[str]) -> str:
                key = match.group(1)
                value = context.get(key)
                return str(value) if value is not None else match.group(0)

            return PLACEHOLDER_RE.sub(replace, template)

        return {
            "title": render(layout["title_template"], raw_context),
            "body": render(layout["body_template"], body_context),
            "format": layout["format"],
        }

    # ── delivery ───────────────────────────────────────────────────────

    def _email_target(self) -> DeliveryTarget:
        enabled = self._enabled("email")
        db, db_error = self._load_channel(EMAIL_CONFIG_KEY)
        env, env_errors = self._env_email()
        values = self._email_values(db, env)
        errors = _email_errors(values, require_complete=True)
        if db_error:
            errors["config"] = db_error
        errors.update(env_errors)
        target_values = {
            **values,
            "smtp_port": int(values.get("smtp_port") or DEFAULT_EMAIL_PORT),
            "security": values.get("security") or DEFAULT_EMAIL_SECURITY,
            "to_addresses": [str(item) for item in values.get("to_addresses") or []],
        }
        return DeliveryTarget(
            channel="email",
            enabled=enabled,
            values=target_values,
            error="；".join(dict.fromkeys(errors.values())) if errors else None,
        )

    def _http_target(self) -> DeliveryTarget:
        enabled = self._enabled("http")
        db, db_error = self._load_channel(HTTP_CONFIG_KEY)
        env, env_errors = self._env_http()
        values = self._http_values(db, env)
        errors = _http_errors(values, require_complete=True)
        if db_error:
            errors["config"] = db_error
        errors.update(env_errors)
        target_values = {
            **values,
            "method": (values.get("method") or "POST").upper(),
            "headers": values.get("headers") or {},
            "timeout_seconds": float(values.get("timeout_seconds") or DEFAULT_HTTP_TIMEOUT),
        }
        return DeliveryTarget(
            channel="http",
            enabled=enabled,
            values=target_values,
            error="；".join(dict.fromkeys(errors.values())) if errors else None,
        )

    def _targets(self, channel: str | None = None) -> list[DeliveryTarget]:
        targets: list[DeliveryTarget] = []
        if channel in {None, "email"}:
            targets.append(self._email_target())
        if channel in {None, "http"}:
            targets.append(self._http_target())
        return targets

    def _send_email(self, target: DeliveryTarget, message: dict[str, str]) -> str:
        assert target.values is not None
        values = target.values
        msg = EmailMessage()
        msg["Subject"] = message["title"].replace("\r", " ").replace("\n", " ")
        msg["From"] = values["from_address"]
        msg["To"] = ", ".join(values["to_addresses"])
        if message["format"] == "html":
            msg.set_content(message["body"], subtype="html")
        else:
            msg.set_content(message["body"])
        host = values["smtp_host"]
        port = int(values["smtp_port"])
        security = values["security"]
        timeout = 20
        context = ssl.create_default_context()
        try:
            if security == "ssl":
                client: smtplib.SMTP = smtplib.SMTP_SSL(
                    host, port, timeout=timeout, context=context
                )
            else:
                client = smtplib.SMTP(host, port, timeout=timeout)
        except (TimeoutError, socket.timeout) as exc:
            raise NotificationDeliveryError(
                f"连接 SMTP 超时（{host}:{port} / {security}）。"
                "请检查服务器地址、端口和网络出口；465 通常使用 SSL，"
                "587/25 通常使用 STARTTLS。"
            ) from exc
        try:
            client.ehlo()
            if security == "starttls":
                client.starttls(context=context)
                client.ehlo()
            if values.get("username") and values.get("password"):
                client.login(str(values["username"]), str(values["password"]))
            client.send_message(msg)
        except (TimeoutError, socket.timeout) as exc:
            raise NotificationDeliveryError(
                f"SMTP 通信超时（{host}:{port} / {security}）"
            ) from exc
        finally:
            try:
                client.quit()
            except Exception:
                pass
        return f"邮件已发送到 {len(values['to_addresses'])} 个收件人"

    def _send_http(self, target: DeliveryTarget, message: dict[str, str], event: dict[str, Any]) -> str:
        assert target.values is not None
        values = target.values
        payload = {
            "event": "run.finished",
            "title": message["title"],
            "body": message["body"],
            "content": message["body"],
            "format": message["format"],
            "run": event,
        }
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "SiteFlow-Notifier/1.0",
            **values.get("headers", {}),
        }
        with httpx.Client(timeout=float(values["timeout_seconds"])) as client:
            response = client.request(
                str(values["method"]),
                str(values["url"]),
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
        return f"HTTP {response.status_code}"

    def _send(
        self,
        message: dict[str, str],
        event: dict[str, Any],
        *,
        channel: str | None = None,
        require_enabled: bool = True,
        respect_filters: bool = False,
    ) -> list[ChannelDeliveryResult]:
        results: list[ChannelDeliveryResult] = []
        for target in self._targets(channel):
            if require_enabled and not target.enabled:
                continue
            if respect_filters and not self._accepts_run(target.channel, event):
                continue
            if target.error:
                results.append(
                    ChannelDeliveryResult(
                        channel=target.channel,
                        ok=False,
                        message=target.error,
                    )
                )
                continue
            try:
                if target.channel == "email":
                    detail = self._send_email(target, message)
                else:
                    detail = self._send_http(target, message, event)
            except NotificationDeliveryError as exc:
                results.append(
                    ChannelDeliveryResult(
                        channel=target.channel,
                        ok=False,
                        message=str(exc)[:500],
                    )
                )
            except Exception as exc:
                results.append(
                    ChannelDeliveryResult(
                        channel=target.channel,
                        ok=False,
                        message=f"{type(exc).__name__}: {str(exc)[:500]}",
                    )
                )
            else:
                results.append(
                    ChannelDeliveryResult(
                        channel=target.channel,
                        ok=True,
                        message=detail,
                    )
                )
        return results

    def send_run(self, run_id: int) -> list[ChannelDeliveryResult]:
        event = self._run_event(run_id)
        message = self._render(event)
        return self._send(message, event, require_enabled=True, respect_filters=True)

    def test_channel(self, channel: str) -> NotificationTestResult:
        event = self._sample_event()
        message = self._render(event)
        results = self._send(message, event, channel=channel, require_enabled=False)
        if not results:
            return NotificationTestResult(
                ok=False,
                message="通知通道未配置",
                results=[],
            )
        ok = all(result.ok for result in results)
        return NotificationTestResult(
            ok=ok,
            message="测试通知已发送" if ok else "测试通知发送失败",
            results=results,
        )
