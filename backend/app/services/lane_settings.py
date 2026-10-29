from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.setting import AppSetting
from app.schemas.lane import LaneSettingValue, LaneSettingsView

DEFAULT_IDLE_STOP_SECONDS = 300
DEFAULT_GENERAL_CONCURRENCY = 5
DEFAULT_PROXY_CONCURRENCY = 3
IDLE_KEY = "lane_idle_stop_seconds"
GENERAL_KEY = "lane_general_concurrency"
PROXY_KEY = "lane_proxy_concurrency"


class SettingLockedError(RuntimeError):
    pass


class LaneSettingsService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def _db_value(self, key: str, default: int) -> int:
        row = self.session.get(AppSetting, key)
        if row is None:
            return default
        try:
            return int(json.loads(row.value_json))
        except (ValueError, TypeError, json.JSONDecodeError):
            return default

    def _field(
        self,
        *,
        key: str,
        env_value: int | None,
        default: int,
        minimum: int,
        maximum: int,
    ) -> LaneSettingValue:
        if env_value is not None:
            return LaneSettingValue(
                value=max(minimum, min(env_value, maximum)),
                source="env",
                editable=False,
            )
        row = self.session.get(AppSetting, key)
        value = self._db_value(key, default)
        return LaneSettingValue(
            value=max(minimum, min(value, maximum)),
            source="db" if row is not None else "default",
            editable=True,
        )

    def view(self) -> LaneSettingsView:
        return LaneSettingsView(
            idle_stop_seconds=self._field(
                key=IDLE_KEY,
                env_value=self.settings.lane_idle_stop_seconds,
                default=DEFAULT_IDLE_STOP_SECONDS,
                minimum=0,
                maximum=86400,
            ),
            general_concurrency=self._field(
                key=GENERAL_KEY,
                env_value=self.settings.lane_general_concurrency,
                default=DEFAULT_GENERAL_CONCURRENCY,
                minimum=1,
                maximum=10,
            ),
            proxy_concurrency=self._field(
                key=PROXY_KEY,
                env_value=self.settings.lane_proxy_concurrency,
                default=DEFAULT_PROXY_CONCURRENCY,
                minimum=1,
                maximum=10,
            ),
        )

    def _upsert(self, key: str, value: int) -> None:
        row = self.session.get(AppSetting, key)
        if row is None:
            row = AppSetting(key=key, value_json=str(value))
            self.session.add(row)
        else:
            row.value_json = str(value)
            row.updated_at = datetime.now(timezone.utc)

    def update(
        self,
        *,
        idle_stop_seconds: int | None = None,
        general_concurrency: int | None = None,
        proxy_concurrency: int | None = None,
    ) -> LaneSettingsView:
        updates = (
            (
                IDLE_KEY,
                idle_stop_seconds,
                self.settings.lane_idle_stop_seconds,
                "空闲关闭时间",
                "SITEFLOW_LANE_IDLE_STOP_SECONDS",
                0,
                86400,
            ),
            (
                GENERAL_KEY,
                general_concurrency,
                self.settings.lane_general_concurrency,
                "普通车道并行任务数",
                "SITEFLOW_LANE_GENERAL_CONCURRENCY",
                1,
                10,
            ),
            (
                PROXY_KEY,
                proxy_concurrency,
                self.settings.lane_proxy_concurrency,
                "代理车道并行任务数",
                "SITEFLOW_LANE_PROXY_GENERAL_CONCURRENCY",
                1,
                10,
            ),
        )
        for key, value, env_value, label, env_name, minimum, maximum in updates:
            if value is None:
                continue
            if env_value is not None:
                raise SettingLockedError(f"{label}已由 {env_name} 环境变量锁定")
            if value < minimum or value > maximum:
                raise ValueError(f"{label}必须在 {minimum} 到 {maximum} 之间")
            self._upsert(key, value)
        self.session.commit()
        return self.view()
