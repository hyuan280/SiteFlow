"""Failure identities and reuse audit survive deletion of assistant chats."""

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class TakeoverFailureSignature(Base):
    __tablename__ = "assistant_takeover_failure_signatures"
    __table_args__ = (Index(
        "ix_takeover_failure_match", "owner", "site_history_key", "site_origin",
        "config_revision", "config_fingerprint", "version", "failure_hash",
    ),)

    source_job_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    site_history_key: Mapped[str] = mapped_column(String(36), nullable=False)
    site_origin: Mapped[str] = mapped_column(String(512), nullable=False)
    config_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    failure_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class TakeoverDedupLink(Base):
    __tablename__ = "assistant_takeover_dedup_links"

    action_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_action_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False,
    )


class TakeoverAnalysisProvenance(Base):
    """A stale observation permanently disqualifies this analysis from reuse."""

    __tablename__ = "assistant_takeover_analysis_provenance"

    action_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    inspected_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_current: Mapped[bool] = mapped_column(Boolean, nullable=False)
