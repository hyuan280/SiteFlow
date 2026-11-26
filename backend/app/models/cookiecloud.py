from datetime import datetime

from sqlalchemy import Boolean, DateTime, JSON, LargeBinary, String, false
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.workflow import utcnow


class CookieCloudServer(Base):
    __tablename__ = "cookiecloud_servers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    all_sites: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    # Site identity keys remain safe even when a deleted integer site ID is reused.
    site_keys: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
