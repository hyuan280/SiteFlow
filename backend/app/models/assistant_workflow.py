"""Durable identities for assistant configuration writes and full runs."""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class AssistantWorkflowAction(Base):
    __tablename__ = "assistant_workflow_actions"
    __table_args__ = (UniqueConstraint("session_id", "kind", "request_fingerprint"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(
        ForeignKey("assistant_sessions.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    site_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    site_history_key: Mapped[str] = mapped_column(String(36), default="", nullable=False)
    site_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    state: Mapped[str] = mapped_column(String(16), default="reserved", nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False,
    )
