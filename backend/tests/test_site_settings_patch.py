from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.core.crypto import CryptoService
from app.core.db import Base, create_db_engine, create_session_factory
from app.models.setting import AppSetting
from app.models.workflow import Site, SiteCredential, WorkflowGroup, WorkflowHistory, WorkflowSignal, WorkflowStep
from app.schemas.workflow import SiteSettingsPatch, SiteWrite, TemplateWrite
from app.services.workflows import WorkflowConflictError, WorkflowService, WorkflowValidationError


@pytest.fixture
def context(settings):
    engine = create_db_engine(settings.database_url)
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    crypto = CryptoService(b"x" * 32)
    with factory() as db:
        service = WorkflowService(db, settings, crypto)
        template = service.create_template(TemplateWrite(name="template"))
        site = service.create_site(SiteWrite(
            name="original", base_url="https://example.test", enabled=False,
            template_id=template.id, bitwarden_enabled=True,
            credential_provider_id="builtin.bitwarden", bitwarden_match_mode="custom",
            bitwarden_custom_match="private match", checkin_after="12:30", tags=["keep"],
            groups=[{"group_key": "group", "name": "Group"}],
            signals=[{"signal_key": "signed", "label": "Signed", "include_texts": ["ok"]}],
            steps=[{"step_key": "fill", "feature_type": "action.fill", "group_key": "group",
                    "config": {"selector": ["#token"], "value": "hidden-secret"}}],
        ))
        # Preserve legacy URL bytes that a full SiteWrite round trip would trim.
        db.get(Site, site.id).base_url = "https://example.test/?value=/"
        db.add(SiteCredential(site_id=site.id, username_ciphertext=b"hidden-user",
                              username_nonce=b"nonce", password_ciphertext=b"hidden-password",
                              password_nonce=b"nonce"))
        db.commit()
    yield SimpleNamespace(factory=factory, settings=settings, crypto=crypto, site=site)
    engine.dispose()


def service_for(context, db):
    service = WorkflowService(db, context.settings, context.crypto)
    service._credential_view = Mock(return_value=context.site.credential)
    return service


def patch(context, db, values, **kwargs):
    return service_for(context, db).patch_site_settings(
        context.site.id, values, expected_revision=kwargs.pop("expected_revision", 1),
        expected_history_key=kwargs.pop("expected_history_key", context.site.history_key), **kwargs,
    )


def rows(db, model):
    return [{column.name: getattr(row, column.name) for column in model.__table__.columns}
            for row in db.scalars(select(model).order_by(model.id))]


def test_patch_preserves_workflow_protected_fields_and_records_history(context):
    with context.factory() as db:
        before = {model: rows(db, model) for model in (WorkflowStep, WorkflowGroup, WorkflowSignal)}
        site = db.get(Site, context.site.id)
        protected = {key: getattr(site, key) for key in (
            "base_url", "template_id", "bitwarden_enabled", "credential_provider_id",
            "bitwarden_match_mode", "bitwarden_custom_match", "history_key", "created_at",
        )}
        result = patch(context, db, {
            "name": " renamed ", "trusted_urls": [" ^https://trusted.test/ "],
            "lane_policy": "interaction", "support_resign": False, "resign_max_days": 7,
            "checkin_after": None, "timezone": "UTC", "tags": [" tag ", "tag", ""],
            "notes": "updated",
        })
        assert result.name == "renamed" and result.config_revision == 2
        assert result.trusted_urls == ["^https://trusted.test/"] and result.tags == ["tag"]
        assert result.checkin_after is None and result.timezone == "UTC" and not result.enabled
        assert result.lane_policy == "interaction" and not result.support_resign
        assert result.resign_max_days == 7 and result.notes == "updated"
        assert {key: getattr(site, key) for key in protected} == protected
        assert {model: rows(db, model) for model in before} == before
        assert db.get(SiteCredential, site.id).password_ciphertext == b"hidden-password"
        history = service_for(context, db).get_version("site", site.id, 2)
        assert history.operation == "update"
        assert history.snapshot.local["steps"][0]["config"]["value"] == "hidden-secret"
        assert history.snapshot.local["base_url"] == protected["base_url"]


@pytest.mark.parametrize("values", [{}, {"name": " original "}, {"tags": [" keep ", "keep"]}])
def test_noop_does_not_write_revision_history_or_timestamp(context, values):
    with context.factory() as db:
        before = rows(db, WorkflowHistory)
        timestamp = db.get(Site, context.site.id).updated_at
        result = patch(context, db, values)
        assert result.config_revision == 1
        assert db.get(Site, result.id).updated_at == timestamp
        assert rows(db, WorkflowHistory) == before


@pytest.mark.parametrize("values", [
    {"name": None}, {"name": " "}, {"enabled": None}, {"enabled": 1}, {"enabled": "true"},
    {"resign_max_days": True}, {"resign_max_days": "7"}, {"resign_max_days": 0},
    {"resign_max_days": 366}, {"trusted_urls": ["["]}, {"trusted_urls": [None]},
    {"trusted_urls": ["a"] * 21}, {"tags": [1]}, {"tags": None}, {"notes": None},
    {"notes": "a" * 4001}, {"checkin_after": "24:00"}, {"checkin_after": "1:00"},
    {"timezone": "Invalid/Timezone"}, {"timezone": "../UTC"}, {"lane_policy": "invalid"},
    {"base_url": "https://other.test"}, {"template_id": 1}, {"steps": []}, {"groups": []},
    {"signals": []}, {"credential_provider_id": "builtin.manual"}, {"config_revision": 2},
])
def test_strict_patch_schema_and_service_reject_invalid_input(context, values):
    with pytest.raises(ValidationError):
        SiteSettingsPatch.model_validate(values)
    with context.factory() as db:
        before = rows(db, WorkflowHistory)
        with pytest.raises(ValidationError):
            patch(context, db, values)
        assert rows(db, WorkflowHistory) == before
        assert db.get(Site, context.site.id).config_revision == 1


@pytest.mark.parametrize("values", [{}, {"notes": "change"}])
@pytest.mark.parametrize("revision,key", [(2, None), (1, "other-history")])
def test_stale_identity_rejected_even_for_noop(context, values, revision, key):
    with context.factory() as db:
        with pytest.raises(WorkflowConflictError):
            patch(context, db, values, expected_revision=revision,
                  expected_history_key=key or context.site.history_key)
        assert db.get(Site, context.site.id).config_revision == 1


def test_unique_name_and_resolution_failures_roll_back(context, monkeypatch):
    with context.factory() as db:
        service_for(context, db).create_site(SiteWrite(name="taken", base_url="https://other.test"))
        with pytest.raises(WorkflowConflictError):
            patch(context, db, {"name": "taken"})
        service = service_for(context, db)
        monkeypatch.setattr(service, "resolve_site", Mock(side_effect=WorkflowValidationError("invalid")))
        with pytest.raises(WorkflowValidationError):
            service.patch_site_settings(context.site.id, {"notes": "change"}, expected_revision=1,
                                        expected_history_key=context.site.history_key)
        assert db.get(Site, context.site.id).config_revision == 1
        assert db.get(Site, context.site.id).notes == ""


def test_uncommitted_patch_and_caller_state_roll_back_together(context, monkeypatch):
    with context.factory() as db:
        commit = Mock(wraps=db.commit)
        monkeypatch.setattr(db, "commit", commit)
        db.add(AppSetting(key="caller.pending", value_json=json.dumps(True)))
        assert patch(context, db, {"notes": "pending"}, _commit=False).config_revision == 2
        assert service_for(context, db).list_versions("site", context.site.id).total == 2
        commit.assert_not_called()
        db.rollback()
    with context.factory() as db:
        assert db.get(Site, context.site.id).notes == ""
        assert db.get(Site, context.site.id).config_revision == 1
        assert db.get(AppSetting, "caller.pending") is None
        assert service_for(context, db).list_versions("site", context.site.id).total == 1


def test_core_patch_allows_manual_enable_without_assistant_proof(context):
    with context.factory() as db:
        assert patch(context, db, {"enabled": True}).enabled
