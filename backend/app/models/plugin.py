from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PluginAuditEvent(Base):
    __tablename__ = "plugin_audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    plugin_id: Mapped[str | None] = mapped_column(String(64), index=True)
    provider_id: Mapped[str | None] = mapped_column(String(64))
    site_id: Mapped[int | None] = mapped_column(Integer)
    job_id: Mapped[int | None] = mapped_column(Integer)
    step_key: Mapped[str | None] = mapped_column(String(128))
    event: Mapped[str] = mapped_column(String(32), index=True)
    action: Mapped[str] = mapped_column(String(32))
    result: Mapped[str] = mapped_column(String(16), index=True)
    reason: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str | None] = mapped_column(String(128))


class PluginRegistration(Base):
    __tablename__ = "plugin_registrations"

    plugin_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    protocol_version: Mapped[int] = mapped_column(Integer, nullable=False)
    endpoint: Mapped[str] = mapped_column(String(512), nullable=False)
    capabilities_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    manifest_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    checks_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    features_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    captcha_methods_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    approved_capabilities_json: Mapped[str] = mapped_column(
        Text, default="[]", nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default="unapproved", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    generation: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    permission_epoch: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removed_by: Mapped[str | None] = mapped_column(String(128))
    previous_metadata_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    change_impacts_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    last_heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
