"""Revisioned global assistant settings stored in one AppSetting record."""

import json
from datetime import datetime, timezone

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.setting import AppSetting
from app.services.config_ai_generation import _strict_json
from app.schemas.assistant_settings import (
    AssistantSettingsReset,
    AssistantSettingsUpdate,
    AssistantSettingsValues,
    AssistantSettingsView,
)


KEY = "assistant.settings"
_NEW_PERMISSIONS = {"auto_authorize_execution", "auto_authorize_screenshot",
                    "auto_authorize_workflow"}
_LEGACY_FIELDS = {"max_tool_calls", "max_model_rounds", "max_parallel_tool_calls",
                  "turn_timeout_seconds", "max_tokens", "context_max_bytes",
                  "auto_compact", "auto_title"}
_PREVIOUS_FIELDS = set(AssistantSettingsValues.model_fields) - {"max_auth_uses_per_turn"}


def _json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


class AssistantSettingsError(RuntimeError):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


class AssistantSettingsService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _read(self) -> tuple[str | None, AssistantSettingsView]:
        # A scalar read bypasses stale ORM identity-map state and never flushes callers' data.
        with self.session.no_autoflush:
            raw = self.session.scalar(select(AppSetting.value_json).where(AppSetting.key == KEY))
        if raw is None:
            return None, AssistantSettingsView(
                revision=0, source="default", values=AssistantSettingsValues(),
            )
        try:
            data = _strict_json(raw)
            if not isinstance(data, dict) or set(data) != {"revision", "values"}:
                raise ValueError
            values = data["values"]
            if isinstance(values, dict) and set(values) == _LEGACY_FIELDS:
                budget = values["context_max_bytes"]
                if type(budget) is not int or not 64000 <= budget <= 1000000:
                    raise ValueError
                # Exact shipped legacy shape only. Approximate ceil(bytes / 4),
                # clamped to the new minimum; never grant a new permission.
                # Reads stay read-only. The next successful CAS update/reset
                # persists the new shape, comparing the original legacy raw JSON.
                data["values"] = {
                    **{key: value for key, value in values.items() if key != "context_max_bytes"},
                    "context_max_tokens": max(16000, (budget + 3) // 4),
                    **dict.fromkeys(_NEW_PERMISSIONS, False),
                    "max_auth_uses_per_turn": 3,
                }
            elif isinstance(values, dict) and set(values) == _PREVIOUS_FIELDS:
                # Only the exact previous token-budget shape may omit this limit.
                data["values"] = {**values, "max_auth_uses_per_turn": 3}
            view = AssistantSettingsView.model_validate({**data, "source": "db"})
            if view.values.model_fields_set != set(AssistantSettingsValues.model_fields):
                raise ValueError
        except (ValueError, TypeError, RecursionError):
            raise AssistantSettingsError("assistant_settings_invalid", 503) from None
        return raw, view

    def view(self) -> AssistantSettingsView:
        return self._read()[1]

    def load(self) -> AssistantSettingsValues:
        return self.view().values

    def update(self, payload: AssistantSettingsUpdate) -> AssistantSettingsView:
        # Revalidate even model instances so mutated/unchecked inputs cannot persist extra keys.
        payload = AssistantSettingsUpdate.model_validate(payload.model_dump())
        try:
            raw, current = self._read()
            if current.revision != payload.expected_revision:
                raise AssistantSettingsError("assistant_settings_conflict", 409)
            result = AssistantSettingsView(
                revision=current.revision + 1, source="db", values=payload.values,
            )
            encoded = _json(result.model_dump(exclude={"source"}))
            with self.session.no_autoflush:
                if raw is None:
                    self.session.execute(insert(AppSetting).values(key=KEY, value_json=encoded))
                else:
                    changed = self.session.execute(
                        update(AppSetting).where(
                            AppSetting.key == KEY, AppSetting.value_json == raw,
                        ).values(value_json=encoded, updated_at=datetime.now(timezone.utc)),
                        execution_options={"synchronize_session": False},
                    )
                    if changed.rowcount != 1:
                        raise AssistantSettingsError("assistant_settings_conflict", 409)
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            raise AssistantSettingsError("assistant_settings_conflict", 409) from None
        except Exception:
            self.session.rollback()
            raise
        return result

    def reset(self, expected_revision: int) -> AssistantSettingsView:
        payload = AssistantSettingsReset(expected_revision=expected_revision)
        return self.update(AssistantSettingsUpdate(
            expected_revision=payload.expected_revision, values=AssistantSettingsValues(),
        ))
