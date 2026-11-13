"""Takeover audit and dispatch bindings deliberately outlive assistant chats."""

from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


def utcnow():
    return datetime.now(timezone.utc)


class AssistantJobConfigSnapshot(Base):
    __tablename__ = "assistant_job_config_snapshots"

    job_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(Integer, nullable=False)
    history_key: Mapped[str] = mapped_column(String(36), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AssistantTakeoverAction(Base):
    __tablename__ = "assistant_takeover_actions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_job_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    source_run_id: Mapped[int] = mapped_column(Integer, nullable=False)
    site_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    site_history_key: Mapped[str] = mapped_column(String(36), nullable=False)
    site_origin: Mapped[str] = mapped_column(String(512), nullable=False)
    owner: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    grant_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    import_receipt_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DailyConversation(Base):
    """Daily chat routing, never a delegation or action replay permit."""

    __tablename__ = "assistant_takeover_daily_conversations"
    __table_args__ = (UniqueConstraint("owner", "site_history_key", "local_date"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    site_history_key: Mapped[str] = mapped_column(String(36), nullable=False)
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    anchor_finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    anchor_action_id: Mapped[str] = mapped_column(String(36), nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AssistantTakeoverStepOutcome(Base):
    """Secret-free live-step safety facts, independent of conversation lifetime."""

    __tablename__ = "assistant_takeover_step_outcomes"

    operation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    site_id: Mapped[int] = mapped_column(Integer, nullable=False)
    site_history_key: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    site_origin: Mapped[str] = mapped_column(String(512), nullable=False)
    action_id: Mapped[str] = mapped_column(String(36), nullable=False)
    retry_of: Mapped[str | None] = mapped_column(String(36), nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    dispatched: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
