from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class AssistantRunGuard(Base):
    __tablename__ = "assistant_run_guards"

    run_id: Mapped[int] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
    )
    site_id: Mapped[int] = mapped_column(Integer, nullable=False)
    site_history_key: Mapped[str] = mapped_column(String(36), nullable=False)
    site_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
