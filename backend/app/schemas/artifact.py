from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ArtifactStatsView(BaseModel):
    total_count: int
    total_bytes: int
    log_count: int
    log_bytes: int
    screenshot_count: int
    screenshot_bytes: int
    missing_count: int
    oldest_created_at: datetime | None
    retention_days: int
    cleanup_interval_seconds: int
    cleanup_enabled: bool


class ArtifactCleanupResult(BaseModel):
    removed_count: int
    removed_bytes: int
    orphan_count: int
    orphan_bytes: int
    retention_days: int
