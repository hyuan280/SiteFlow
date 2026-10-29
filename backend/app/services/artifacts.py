from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.auth import ensure_aware
from app.core.config import Settings
from app.models.execution import JobArtifact
from app.schemas.artifact import ArtifactCleanupResult, ArtifactStatsView

logger = logging.getLogger("siteflow.artifacts")


class ArtifactService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.root = Path(settings.artifact_root).resolve()

    def _path(self, relative_path: str) -> Path | None:
        path = (self.root / relative_path).resolve()
        if path == self.root or self.root not in path.parents:
            return None
        return path

    def stats(self) -> ArtifactStatsView:
        rows = list(self.session.scalars(select(JobArtifact).order_by(JobArtifact.id)))
        log_count = 0
        log_bytes = 0
        screenshot_count = 0
        screenshot_bytes = 0
        missing_count = 0
        oldest: datetime | None = None
        for row in rows:
            path = self._path(row.relative_path)
            size = 0
            if path is not None and path.is_file():
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
            else:
                missing_count += 1
            if row.kind in {"screenshot", "captcha"}:
                screenshot_count += 1
                screenshot_bytes += size
            else:
                log_count += 1
                log_bytes += size
            created = ensure_aware(row.created_at)
            if oldest is None or created < oldest:
                oldest = created
        return ArtifactStatsView(
            total_count=len(rows),
            total_bytes=log_bytes + screenshot_bytes,
            log_count=log_count,
            log_bytes=log_bytes,
            screenshot_count=screenshot_count,
            screenshot_bytes=screenshot_bytes,
            missing_count=missing_count,
            oldest_created_at=oldest,
            retention_days=self.settings.artifact_retention_days,
            cleanup_interval_seconds=self.settings.artifact_cleanup_interval_seconds,
            cleanup_enabled=self.settings.artifact_cleanup_enabled,
        )

    def cleanup(self) -> ArtifactCleanupResult:
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=self.settings.artifact_retention_days
        )
        rows = list(self.session.scalars(select(JobArtifact)))
        removed_count = 0
        removed_bytes = 0
        for row in rows:
            if ensure_aware(row.created_at) >= cutoff:
                continue
            path = self._path(row.relative_path)
            if path is not None and path.is_file():
                try:
                    removed_bytes += path.stat().st_size
                    path.unlink()
                except OSError:
                    pass
            self.session.delete(row)
            removed_count += 1
        self.session.flush()
        self.session.commit()

        remaining = {
            row.relative_path
            for row in self.session.scalars(select(JobArtifact))
        }
        orphan_count = 0
        orphan_bytes = 0
        cutoff_ts = cutoff.timestamp()
        if self.root.is_dir():
            for path in self.root.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    relative = path.relative_to(self.root).as_posix()
                except ValueError:
                    continue
                if relative in remaining:
                    continue
                try:
                    if path.stat().st_mtime >= cutoff_ts:
                        continue
                    orphan_bytes += path.stat().st_size
                    path.unlink()
                    orphan_count += 1
                except OSError:
                    continue
            for path in sorted(self.root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
                if not path.is_dir():
                    continue
                try:
                    path.rmdir()
                except OSError:
                    continue
        return ArtifactCleanupResult(
            removed_count=removed_count,
            removed_bytes=removed_bytes,
            orphan_count=orphan_count,
            orphan_bytes=orphan_bytes,
            retention_days=self.settings.artifact_retention_days,
        )


class ArtifactCleanupRunner:
    def __init__(self, session_factory: sessionmaker, settings: Settings) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if not self.settings.artifact_cleanup_enabled or self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="artifact-cleanup")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                result = await asyncio.to_thread(self._run_once)
                if result.removed_count or result.orphan_count:
                    logger.info(
                        "artifact cleanup removed=%s bytes=%s orphans=%s orphan_bytes=%s",
                        result.removed_count,
                        result.removed_bytes,
                        result.orphan_count,
                        result.orphan_bytes,
                    )
            except Exception:
                logger.exception("artifact cleanup failed")
            await asyncio.sleep(self.settings.artifact_cleanup_interval_seconds)

    def _run_once(self) -> ArtifactCleanupResult:
        with self.session_factory() as session:
            return ArtifactService(session, self.settings).cleanup()
