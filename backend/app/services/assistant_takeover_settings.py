"""Strict compare-and-swap policy, never inherited from global auto-authorization."""

from datetime import datetime, timezone

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from app.models.setting import AppSetting
from app.models.workflow import Site
from app.schemas.assistant_takeover import TakeoverSettingsUpdate, TakeoverSettingsView, TakeoverValues
from app.services.assistant_settings import AssistantSettingsError
from app.services.config_ai_generation import _json
from app.services.assistant_urls import origin


KEY = "assistant.takeover.settings"
SITE_GRANTS_KEY = "assistant.takeover.site_grants"


class TakeoverSettingsService:
    def __init__(self, session):
        self.session = session

    def _read(self):
        with self.session.no_autoflush:
            raw = self.session.scalar(select(AppSetting.value_json).where(AppSetting.key == KEY))
        if raw is None:
            return None, TakeoverSettingsView(revision=0, values=TakeoverValues())
        try:
            view = TakeoverSettingsView.model_validate_json(raw)
            fields = set(TakeoverValues.model_fields)
            stored_fields = view.values.model_fields_set.copy()
            if stored_fields not in (fields, fields - {"conclusion_ttl_days"},
                                     fields - {"live_access"},
                                     fields - {"live_access", "conclusion_ttl_days"}):
                raise ValueError
            # Only exact legacy shapes migrate in memory, never expanding authority.
            if "live_access" not in stored_fields:
                view.values.live_access = False
            if "conclusion_ttl_days" not in stored_fields:
                view.values.conclusion_ttl_days = 7
            if (view.model_fields_set != set(TakeoverSettingsView.model_fields)
                    or view.values.model_fields_set != set(TakeoverValues.model_fields)
                    or view.revision < 1 or not view.authorizing_owner
                    or view.authorized_at is None or view.effective_from is None
                    or view.authorized_at.tzinfo is None or view.effective_from.tzinfo is None):
                raise ValueError
            # Reject duplicate JSON keys too.
            from app.services.config_ai_generation import _strict_json

            _strict_json(raw)
            return raw, view
        except (ValueError, TypeError, RecursionError):
            raise AssistantSettingsError("takeover_settings_invalid", 503) from None

    def view(self):
        return self._read()[1]

    def site_allowed(self, policy, site):
        if not policy.values.live_access:
            return True
        if policy.values.site_ids is None:
            # This is explicit consent for all enabled sites, including future sites.
            return True
        if site.id not in policy.values.site_ids:
            return False
        from app.services.config_ai_generation import _strict_json

        raw = self.session.scalar(select(AppSetting.value_json).where(AppSetting.key == SITE_GRANTS_KEY))
        try:
            saved = _strict_json(raw)
            return (isinstance(saved, dict) and set(saved) == {"revision", "bindings"}
                    and type(saved["revision"]) is int and saved["revision"] == policy.revision
                    and isinstance(saved["bindings"], dict)
                    and set(saved["bindings"]) == {str(identifier) for identifier in policy.values.site_ids}
                    and saved["bindings"].get(str(site.id)) == {
                        "history_key": site.history_key, "origin": origin(site.base_url)})
        except (ValueError, TypeError, RecursionError):
            return False

    def update(self, payload, owner):
        payload = TakeoverSettingsUpdate.model_validate(payload.model_dump())
        try:
            raw, current = self._read()
            if current.revision != payload.expected_revision:
                raise AssistantSettingsError("takeover_settings_conflict", 409)
            now = datetime.now(timezone.utc)
            result = TakeoverSettingsView(revision=current.revision + 1, values=payload.values,
                                          authorizing_owner=owner, authorized_at=now, effective_from=now)
            encoded = _json(result.model_dump(mode="json"))
            if raw is None:
                self.session.execute(insert(AppSetting).values(key=KEY, value_json=encoded))
            else:
                changed = self.session.execute(update(AppSetting).where(
                    AppSetting.key == KEY, AppSetting.value_json == raw,
                ).values(value_json=encoded, updated_at=now),
                    execution_options={"synchronize_session": False})
                if changed.rowcount != 1:
                    raise AssistantSettingsError("takeover_settings_conflict", 409)
            bindings = {}
            if payload.values.live_access and payload.values.site_ids is not None:
                for identifier in sorted(payload.values.site_ids):
                    # Serialize identity capture with site edits/deletion, after the policy CAS.
                    locked = self.session.execute(update(Site).where(Site.id == identifier).values(
                        config_revision=Site.config_revision),
                        execution_options={"synchronize_session": False})
                    if locked.rowcount != 1:
                        raise AssistantSettingsError("takeover_site_unavailable", 422)
                    site = self.session.get(Site, identifier, populate_existing=True)
                    bindings[str(identifier)] = {"history_key": site.history_key, "origin": origin(site.base_url)}
            grants = _json({"revision": result.revision, "bindings": bindings})
            changed = self.session.execute(update(AppSetting).where(
                AppSetting.key == SITE_GRANTS_KEY).values(value_json=grants, updated_at=now),
                execution_options={"synchronize_session": False})
            if changed.rowcount != 1:
                self.session.execute(insert(AppSetting).values(key=SITE_GRANTS_KEY, value_json=grants))
            self.session.commit()
            return result
        except IntegrityError:
            self.session.rollback()
            raise AssistantSettingsError("takeover_settings_conflict", 409) from None
        except Exception:
            self.session.rollback()
            raise
