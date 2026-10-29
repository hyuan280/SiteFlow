from __future__ import annotations

import json
import random
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from croniter import croniter
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

DEFAULT_MODE: SchedulerMode = "cron"
DEFAULT_CRON = "0 10 * * *"
DEFAULT_INTERVAL = 3600


class SchedulerValidationError(ValueError):
    pass


class SchedulerLockedError(RuntimeError):
    pass


def validate_ranges(values: list[str]) -> list[str]:
    cleaned: list[str] = []
    for item in values:
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
        row = self.session.get(AppSetting, key)
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
            except ValueError:
                next_run_at = None
        return SchedulerSettingsView(
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
                    if self.session.get(AppSetting, MODE_KEY)
                    or self.session.get(AppSetting, ENABLED_KEY)
                    else "default"
                )
            ),
            editable=not self._env_configured(),
            timezone=self._zone().key,
            next_run_at=next_run_at if enabled else None,
        )

    def update(self, payload: SchedulerSettingsUpdate) -> SchedulerSettingsView:
        if self._env_configured():
            raise SchedulerLockedError("调度设置已由环境变量锁定")
        ranges = validate_ranges(payload.ranges)
        if payload.mode == "cron":
            if not payload.cron.strip():
                raise SchedulerValidationError("定时调度需要填写 cron 表达式")
            if not croniter.is_valid(payload.cron):
                raise SchedulerValidationError("cron 表达式无效")
        if payload.mode == "interval" and payload.interval_seconds < 60:
            raise SchedulerValidationError("间隔调度不能少于 60 秒")
        if payload.mode == "random" and not ranges:
            raise SchedulerValidationError("随机调度至少需要一个时间范围")
        self._upsert(MODE_KEY, payload.mode)
        self._upsert(CRON_KEY, payload.cron.strip())
        self._upsert(INTERVAL_KEY, payload.interval_seconds)
        self._upsert(RANGES_KEY, ranges)
        if payload.enabled is not None:
            self._upsert(ENABLED_KEY, bool(payload.enabled))
        enabled = payload.enabled if payload.enabled is not None else self._enabled()
        if not enabled:
            self._delete(NEXT_RUN_KEY)
        else:
            next_run = next_run_at_for(
                payload.mode,
                payload.cron.strip(),
                payload.interval_seconds,
                ranges,
                tz=self._zone(),
            )
            if next_run is None:
                self._delete(NEXT_RUN_KEY)
            else:
                self._upsert(NEXT_RUN_KEY, next_run.astimezone(timezone.utc).isoformat())
        self.session.commit()
        return self.view()

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
