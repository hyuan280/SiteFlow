"""Encrypted promoted conclusions, independent of disposable chat history."""

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class AssistantTakeoverConclusion(Base):
    __tablename__ = "assistant_takeover_conclusions"

    # No chat FK: deleting a conversation must not delete this audit projection.
    action_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_job_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    source_run_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    site_history_key: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False,
    )


class AssistantTakeoverConclusionCandidate(Base):
    __tablename__ = "assistant_takeover_conclusion_candidates"

    action_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_job_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_run_id: Mapped[int] = mapped_column(Integer, nullable=False)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    site_history_key: Mapped[str] = mapped_column(String(36), nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False,
    )
