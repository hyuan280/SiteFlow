from __future__ import annotations

import json
import random
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.setting import AppSetting
from app.schemas.scheduler import SchedulerMode, SchedulerSettingsUpdate, SchedulerSettingsView

ENABLED_KEY = "scheduler_enabled"
MODE_KEY = "scheduler_mode"
CRON_KEY = "scheduler_cron"
INTERVAL_KEY = "scheduler_interval_seconds"
RANGES_KEY = "scheduler_ranges_json"
NEXT_RUN_KEY = "scheduler_next_run_at"
TIMEZONE_APPLIED_KEY = "scheduler_timezone_applied"
REVISION_KEY = "scheduler_config_revision"
CONFIG_KEYS = (ENABLED_KEY, MODE_KEY, CRON_KEY, INTERVAL_KEY, RANGES_KEY)

DEFAULT_MODE: SchedulerMode = "cron"
DEFAULT_CRON = "0 10 * * *"
DEFAULT_INTERVAL = 3600


class SchedulerValidationError(ValueError):
    pass


class SchedulerLockedError(RuntimeError):
    pass


class SchedulerConflictError(RuntimeError):
    pass


def validate_ranges(values: list[str]) -> list[str]:
    cleaned: list[str] = []
    for item in values:
        if not isinstance(item, str) or not re.fullmatch(
            r"\d{1,2}:\d{2}-\d{1,2}:\d{2}", item.strip(), flags=re.ASCII
        ):
            raise SchedulerValidationError("Range must be HH:MM-HH:MM")
        text = item.strip()
        try:
            start_text, end_text = text.split("-", 1)
            start_h, start_m = (int(part) for part in start_text.split(":"))
            end_h, end_m = (int(part) for part in end_text.split(":"))
        except (ValueError, TypeError):
            raise SchedulerValidationError("随机调度范围格式应为 HH:MM-HH:MM") from None
        if not (0 <= start_h <= 23 and 0 <= end_h <= 23):
            raise SchedulerValidationError("随机调度范围小时必须在 00-23 之间")
        if not (0 <= start_m <= 59 and 0 <= end_m <= 59):
            raise SchedulerValidationError("随机调度范围分钟必须在 00-59 之间")
        if end_h * 60 + end_m <= start_h * 60 + start_m:
            raise SchedulerValidationError("随机调度范围的结束时间必须晚于开始时间")
        text = f"{start_h:02d}:{start_m:02d}-{end_h:02d}:{end_m:02d}"
        if text not in cleaned:
            cleaned.append(text)
    return cleaned


def random_range_target(day: date, index: int, text: str, tz) -> tuple[datetime, datetime]:
    start_text, end_text = text.split("-", 1)
    start = time.fromisoformat(start_text)
    end = time.fromisoformat(end_text)
    rng = random.Random(f"global:{day.isoformat()}:{index}:{text}")
    start_dt = datetime.combine(day, start, tzinfo=tz)
    end_dt = datetime.combine(day, end, tzinfo=tz)
    seconds = max(1, int((end_dt - start_dt).total_seconds()))
    target = start_dt + timedelta(seconds=rng.randrange(seconds + 1))
    return target, end_dt


def next_random_run(
    ranges: list[str],
    current: datetime,
    *,
    tz: ZoneInfo | None = None,
    include_now: bool = True,
) -> datetime | None:
    if not ranges:
        return None
    local = current.astimezone(tz) if tz is not None else current.astimezone()
    for offset in range(8):
        day = local.date() + timedelta(days=offset)
        best: datetime | None = None
        for index, text in enumerate(ranges):
            try:
                target, end_dt = random_range_target(day, index, text, local.tzinfo)
            except (ValueError, TypeError):
                continue
            if include_now and target <= local <= end_dt:
                candidate = local
            elif target > local:
                candidate = target
            else:
                continue
            if best is None or candidate < best:
                best = candidate
        if best is not None:
            return best
    return None


def next_run_at_for(
    mode: str,
    cron: str,
    interval_seconds: int,
    ranges: list[str],
    current: datetime | None = None,
    tz: ZoneInfo | None = None,
) -> datetime | None:
    value = current or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    if mode == "interval":
        return value + timedelta(seconds=max(60, int(interval_seconds)))
    if mode == "cron":
        if not cron or not croniter.is_valid(cron):
            return None
        local = value.astimezone(tz) if tz is not None else value.astimezone()
        try:
            next_value = croniter(cron, local).get_next(datetime)
        except (ValueError, TypeError):
            return None
        if next_value.tzinfo is None:
            next_value = next_value.replace(tzinfo=local.tzinfo or timezone.utc)
        return next_value
    if mode == "random":
        return next_random_run(ranges, value, tz=tz)
    return None


def reset_next_run_for_timezone(session: Session, timezone_name: str) -> bool:
    marker = session.get(AppSetting, TIMEZONE_APPLIED_KEY)
    applied = None
    if marker is not None:
        try:
            applied = json.loads(marker.value_json)
        except (json.JSONDecodeError, TypeError):
            applied = None
    if applied == timezone_name:
        return False
    next_run = session.get(AppSetting, NEXT_RUN_KEY)
    if next_run is not None:
        overdue = False
        if not isinstance(applied, str):
            try:
                value = datetime.fromisoformat(json.loads(next_run.value_json))
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                overdue = value <= datetime.now(timezone.utc)
            except (ValueError, TypeError):
                pass
        # Preserve an upgrade's missed trigger, but recompute untrusted future timing.
        if not overdue:
            session.delete(next_run)
    if marker is None:
        session.add(
            AppSetting(key=TIMEZONE_APPLIED_KEY, value_json=json.dumps(timezone_name))
        )
    else:
        marker.value_json = json.dumps(timezone_name)
        marker.updated_at = datetime.now(timezone.utc)
    session.commit()
    return True


def parse_env_ranges(value: str | None) -> list[str] | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SchedulerValidationError("SITEFLOW_SCHEDULER_RANGES 不是合法 JSON") from exc
        if not isinstance(parsed, list):
            raise SchedulerValidationError("SITEFLOW_SCHEDULER_RANGES 必须是数组")
        return validate_ranges([str(item) for item in parsed])
    return validate_ranges([item.strip() for item in text.split(",") if item.strip()])


class SchedulerSettingsService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def _load(self, key: str, default):
        row = self.session.scalar(select(AppSetting).where(AppSetting.key == key).execution_options(
            populate_existing=True))
        if row is None:
            return default
        try:
            return json.loads(row.value_json)
        except (json.JSONDecodeError, TypeError):
            return default

    def _zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.settings.scheduler_timezone)
        except Exception:
            return ZoneInfo("UTC")

    def _env_configured(self) -> bool:
        return any(
            value is not None
            for value in (
                self.settings.scheduler_enabled,
                self.settings.scheduler_mode,
                self.settings.scheduler_cron,
                self.settings.scheduler_interval_seconds,
                self.settings.scheduler_ranges,
            )
        )

    def _enabled(self) -> bool:
        if self.settings.scheduler_enabled is not None:
            return bool(self.settings.scheduler_enabled)
        return bool(self._load(ENABLED_KEY, True))

    def view(self) -> SchedulerSettingsView:
        revision = self._load(REVISION_KEY, 0)
        mode = self._load(MODE_KEY, DEFAULT_MODE)
        cron = self._load(CRON_KEY, DEFAULT_CRON)
        interval = self._load(INTERVAL_KEY, DEFAULT_INTERVAL)
        ranges = self._load(RANGES_KEY, [])
        if not isinstance(ranges, list):
            ranges = []
        env_mode = self.settings.scheduler_mode
        env_cron = self.settings.scheduler_cron
        env_interval = self.settings.scheduler_interval_seconds
        env_ranges = parse_env_ranges(self.settings.scheduler_ranges)
        if env_mode is not None:
            mode = env_mode
        if env_cron is not None:
            cron = env_cron
        if env_interval is not None:
            interval = env_interval
        if env_ranges is not None:
            ranges = env_ranges
        if mode not in {"cron", "interval", "random"}:
            mode = DEFAULT_MODE
        enabled = self._enabled()
        if self.settings.scheduler_enabled is not None:
            enabled_source = "env"
        elif self.session.get(AppSetting, ENABLED_KEY) is not None:
            enabled_source = "db"
        else:
            enabled_source = "default"
        next_run = self._load(NEXT_RUN_KEY, None)
        next_run_at = None
        if isinstance(next_run, str):
            try:
                next_run_at = datetime.fromisoformat(next_run)
                if next_run_at.tzinfo is None:
                    next_run_at = next_run_at.replace(tzinfo=timezone.utc)
            except ValueError:
                next_run_at = None
        return SchedulerSettingsView(
            revision=revision,
            enabled=enabled,
            enabled_source=enabled_source,
            mode=mode,
            cron=str(cron),
            interval_seconds=int(interval),
            ranges=[str(item) for item in ranges],
            source=(
                "env"
                if self._env_configured()
                else (
                    "db"
                    if any(self.session.get(AppSetting, key) for key in CONFIG_KEYS)
                    else "default"
                )
            ),
            editable=not self._env_configured(),
            timezone=self._zone().key,
            next_run_at=next_run_at if enabled else None,
        )

    def binding(self) -> dict:
        """Freeze configuration, including unversioned/raw edits, but not runtime timing."""
        with self.session.no_autoflush:
            revision = self._load(REVISION_KEY, 0)
            raw = dict(self.session.execute(select(AppSetting.key, AppSetting.value_json).where(
                AppSetting.key.in_(CONFIG_KEYS))).all())
        return {"revision": revision, "raw": {key: raw.get(key) for key in CONFIG_KEYS},
                "environment": {name: getattr(self.settings, name) for name in (
                    "scheduler_enabled", "scheduler_mode", "scheduler_cron",
                    "scheduler_interval_seconds", "scheduler_ranges", "scheduler_timezone")}}

    def preview(self, payload: SchedulerSettingsUpdate) -> SchedulerSettingsUpdate:
        """Validate and canonicalize without flushing, reserving, or writing settings."""
        if self._env_configured():
            raise SchedulerLockedError("调度设置已由环境变量锁定")
        ranges = validate_ranges(payload.ranges)
        cron = " ".join(payload.cron.split()).upper()
        if (cron and (len(cron.split()) != 5 or not croniter.is_valid(cron))) or (
            payload.mode == "cron" and not cron
        ):
            raise SchedulerValidationError("Invalid five-field cron expression")
        if cron:
            try:
                croniter(cron, datetime.now(self._zone())).get_next(datetime)
            except (ValueError, TypeError, OverflowError):
                raise SchedulerValidationError("Cron has no valid next occurrence") from None
        if payload.mode == "interval" and payload.interval_seconds < 60:
            raise SchedulerValidationError("间隔调度不能少于 60 秒")
        if payload.mode == "random" and not ranges:
            raise SchedulerValidationError("随机调度至少需要一个时间范围")
        return payload.model_copy(update={"cron": cron, "ranges": ranges})

    def update(self, payload: SchedulerSettingsUpdate, *,
               expected_revision: int | None = None, _commit: bool = True,
               fields: set[str] | None = None) -> SchedulerSettingsView:
        # Even legacy clients use the revision observed before loading their write.
        current = self.view()
        expected = expected_revision if expected_revision is not None else payload.expected_revision
        if expected is None:
            expected = current.revision
        if expected != current.revision:
            raise SchedulerConflictError("scheduler_revision_conflict")
        payload = self.preview(payload)
        values = {MODE_KEY: payload.mode, CRON_KEY: payload.cron,
                  INTERVAL_KEY: payload.interval_seconds, RANGES_KEY: payload.ranges}
        if payload.enabled is not None:
            values[ENABLED_KEY] = payload.enabled
        if fields is not None:
            keys = {"enabled": ENABLED_KEY, "mode": MODE_KEY, "cron": CRON_KEY,
                    "interval_seconds": INTERVAL_KEY, "ranges": RANGES_KEY}
            values = {keys[name]: values[keys[name]] for name in fields}
        raw = self.binding()["raw"]
        try:
            changed = any(raw[key] is None or json.loads(raw[key]) != value
                          for key, value in values.items())
        except (ValueError, TypeError):
            changed = True
        try:
            revision_row = self.session.scalar(select(AppSetting.value_json).where(
                AppSetting.key == REVISION_KEY))
            if revision_row is None:
                if expected != 0:
                    raise SchedulerConflictError("scheduler_revision_conflict")
                self.session.add(AppSetting(key=REVISION_KEY, value_json=str(int(changed))))
                self.session.flush()
            else:
                result = self.session.execute(update(AppSetting).where(
                    AppSetting.key == REVISION_KEY, AppSetting.value_json == str(expected)
                ).values(value_json=str(expected + int(changed)),
                         updated_at=datetime.now(timezone.utc)),
                    execution_options={"synchronize_session": False})
                if result.rowcount != 1:
                    raise SchedulerConflictError("scheduler_revision_conflict")
            if changed:
                for key, value in values.items():
                    self._upsert(key, value)
                effective_change = any(getattr(current, name) != getattr(payload, name)
                                       for name in ("mode", "cron", "interval_seconds", "ranges")
                                       if fields is None or name in fields)
                effective_change |= payload.enabled is not None and payload.enabled != current.enabled
                if effective_change or self._load(NEXT_RUN_KEY, None) is None:
                    enabled = payload.enabled if payload.enabled is not None else current.enabled
                    next_run = next_run_at_for(payload.mode, payload.cron, payload.interval_seconds,
                                               payload.ranges, tz=self._zone()) if enabled else None
                    if next_run is None:
                        self._delete(NEXT_RUN_KEY)
                    else:
                        self._upsert(NEXT_RUN_KEY, next_run.astimezone(timezone.utc).isoformat())
            self.session.flush()
            if _commit:
                self.session.commit()
            return self.view()
        except (IntegrityError, OperationalError) as exc:
            if _commit:
                self.session.rollback()
            raise SchedulerConflictError("scheduler_revision_conflict") from exc
        except SchedulerConflictError:
            if _commit:
                self.session.rollback()
            raise

    def set_next_run(self, value: datetime | None) -> None:
        if value is None:
            self._delete(NEXT_RUN_KEY)
        else:
            self._upsert(NEXT_RUN_KEY, value.astimezone(timezone.utc).isoformat())
        self.session.commit()

    def _upsert(self, key: str, value) -> None:
        row = self.session.get(AppSetting, key)
        if row is None:
            row = AppSetting(key=key, value_json=json.dumps(value, ensure_ascii=False))
            self.session.add(row)
        else:
            row.value_json = json.dumps(value, ensure_ascii=False)
            row.updated_at = datetime.now(timezone.utc)

    def _delete(self, key: str) -> None:
        row = self.session.get(AppSetting, key)
        if row is not None:
            self.session.delete(row)
