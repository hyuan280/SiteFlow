from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.setting import AppSetting
from app.schemas.system import SystemSettings

SHOW_SYSTEM_RESOURCES_KEY = "show_system_resources"


class SystemSettingsService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def view(self) -> SystemSettings:
        row = self.session.get(AppSetting, SHOW_SYSTEM_RESOURCES_KEY)
        enabled = False
        if row is not None:
            try:
                enabled = json.loads(row.value_json) is True
            except (ValueError, TypeError):
                pass
        return SystemSettings(show_system_resources=enabled)

    def update(self, payload: SystemSettings) -> SystemSettings:
        row = self.session.get(AppSetting, SHOW_SYSTEM_RESOURCES_KEY)
        encoded = json.dumps(payload.show_system_resources)
        if row is None:
            self.session.add(AppSetting(key=SHOW_SYSTEM_RESOURCES_KEY, value_json=encoded))
        else:
            row.value_json = encoded
            row.updated_at = datetime.now(timezone.utc)
        self.session.commit()
        return self.view()
