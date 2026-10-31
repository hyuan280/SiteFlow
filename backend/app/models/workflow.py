from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Template(Base):
    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("templates.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    trusted_urls_json: Mapped[str] = mapped_column(
        Text, default="[]", server_default="[]", nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("templates.id", ondelete="SET NULL"), nullable=True, index=True
    )
    lane_policy: Mapped[str] = mapped_column(String(32), default="auto", nullable=False)
    support_resign: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    resign_max_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    bitwarden_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    credential_provider_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    bitwarden_match_mode: Mapped[str] = mapped_column(
        String(32), default="domain", nullable=False
    )
    bitwarden_custom_match: Mapped[str | None] = mapped_column(String(255), nullable=True)
    checkin_after: Mapped[str | None] = mapped_column(String(5), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai", nullable=False)
    tags_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    config_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class SiteCredential(Base):
    __tablename__ = "site_credentials"

    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), primary_key=True
    )
    username_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    username_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    password_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    password_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    cookies_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    cookies_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    cookie_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cookies_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    key_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class WorkflowGroup(Base):
    __tablename__ = "workflow_groups"
    __table_args__ = (
        UniqueConstraint("owner_type", "owner_id", "group_key", name="uq_workflow_group_key"),
        Index("ix_workflow_groups_owner", "owner_type", "owner_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_type: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
    group_key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    position: Mapped[float] = mapped_column(Float, default=1000000.0, nullable=False)
    step_anchor: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class SiteResignRecord(Base):
    __tablename__ = "site_resign_records"
    __table_args__ = (
        UniqueConstraint("site_id", "resign_date", name="uq_site_resign_date"),
        Index("ix_site_resign_records_site", "site_id", "resign_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=False
    )
    resign_date: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class WorkflowStep(Base):
    __tablename__ = "workflow_steps"
    __table_args__ = (
        UniqueConstraint("owner_type", "owner_id", "step_key", name="uq_workflow_step_key"),
        Index("ix_workflow_steps_owner", "owner_type", "owner_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_type: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
    step_key: Mapped[str] = mapped_column(String(64), nullable=False)
    feature_type: Mapped[str] = mapped_column(String(128), nullable=False)
    feature_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    group_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    position: Mapped[float | None] = mapped_column(Float, nullable=True)
    enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    config_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    condition: Mapped[str | None] = mapped_column(Text, nullable=True)
    on_error: Mapped[str | None] = mapped_column(String(32), nullable=True)
    timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class WorkflowSignal(Base):
    __tablename__ = "workflow_signals"
    __table_args__ = (
        UniqueConstraint("owner_type", "owner_id", "signal_key", name="uq_workflow_signal_key"),
        Index("ix_workflow_signals_owner", "owner_type", "owner_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_type: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
    signal_key: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    include_texts_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    include_selectors_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    exclude_texts_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    exclude_selectors_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    match_mode: Mapped[str] = mapped_column(String(8), default="any", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
