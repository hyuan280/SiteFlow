import json

import pytest
from pydantic import ValidationError

from app.models.setting import AppSetting
from app.schemas.assistant_takeover import TakeoverSettingsUpdate, TakeoverValues
from app.services.assistant_settings import AssistantSettingsError
from app.services.assistant_takeover_settings import KEY, TakeoverSettingsService
from tests.test_assistant_takeover import OWNER, ctx, grant  # noqa: F401


@pytest.mark.parametrize("days", [1, 7, 30])
def test_ttl_settings_persist_full_shape_and_keep_cas_semantics(ctx, days):
    with ctx[1].session_factory() as db:
        assert TakeoverSettingsService(db).view().values.conclusion_ttl_days == 7
    policy = grant(ctx, conclusion_ttl_days=days)
    with ctx[1].session_factory() as db:
        service = TakeoverSettingsService(db)
        assert service.view() == policy
        assert policy.values.conclusion_ttl_days == days
        assert set(json.loads(db.get(AppSetting, KEY).value_json)["values"]) == set(TakeoverValues.model_fields)
        saved = service.update(TakeoverSettingsUpdate.model_validate({
            "expected_revision": policy.revision, "confirm": True,
            "values": {"enabled": True, "live_access": False},
        }), OWNER)
        assert saved.values.conclusion_ttl_days == 7
        assert saved.revision == policy.revision + 1
        assert saved.effective_from == saved.authorized_at > policy.effective_from
        assert json.loads(db.get(AppSetting, KEY).value_json)["values"]["conclusion_ttl_days"] == 7
        with pytest.raises(AssistantSettingsError, match="conflict"):
            service.update(TakeoverSettingsUpdate(expected_revision=policy.revision,
                confirm=True, values=saved.values), OWNER)


@pytest.mark.parametrize("value", [0, 31, -1, True, False, "7", 7.0, None, [], {}])
def test_ttl_rejects_non_integer_or_out_of_range_values(value):
    with pytest.raises(ValidationError):
        TakeoverValues(conclusion_ttl_days=value)


@pytest.mark.parametrize("missing", [
    {"conclusion_ttl_days"}, {"live_access", "conclusion_ttl_days"}, {"live_access"},
])
def test_exact_legacy_settings_migrate_without_writes_or_authority_expansion(ctx, missing):
    policy = grant(ctx, live_access=True)
    data = policy.model_dump(mode="json")
    for field in missing:
        data["values"].pop(field)
    raw = json.dumps(data)
    with ctx[1].session_factory() as db:
        row = db.get(AppSetting, KEY)
        row.value_json = raw
        db.commit()
        updated_at = row.updated_at
        service = TakeoverSettingsService(db)
        migrated = service.view()
        assert migrated.revision == policy.revision
        assert migrated.authorizing_owner == policy.authorizing_owner
        assert migrated.authorized_at == policy.authorized_at
        assert migrated.effective_from == policy.effective_from
        assert migrated.values.conclusion_ttl_days == 7
        assert migrated.values.live_access is ("live_access" not in missing)
        assert set(migrated.values.model_fields_set) == set(TakeoverValues.model_fields)
        assert not db.dirty
        db.commit()
        db.expire_all()
        assert row.value_json == raw and row.updated_at == updated_at
        saved = service.update(TakeoverSettingsUpdate(expected_revision=migrated.revision,
            confirm=True, values=migrated.values), OWNER)
        assert saved.revision == policy.revision + 1
        assert saved.effective_from == saved.authorized_at > policy.effective_from
        assert saved.values.live_access == migrated.values.live_access
        db.refresh(row)
        assert set(json.loads(row.value_json)["values"]) == set(TakeoverValues.model_fields)


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("damage", [
    "enabled", "mode", "site_ids", "max_attempts", "unknown", "string", "bool", "duplicate",
])
def test_stored_partial_or_malformed_settings_fail_closed(ctx, legacy, damage):
    data = grant(ctx).model_dump(mode="json")
    if legacy:
        data["values"].pop("conclusion_ttl_days")
        data["values"].pop("live_access")
    if damage in {"enabled", "mode", "site_ids", "max_attempts"}:
        data["values"].pop(damage)
    elif damage == "unknown":
        data["values"]["unknown"] = True
    elif damage in {"string", "bool"}:
        data["values"]["conclusion_ttl_days"] = "7" if damage == "string" else True
    raw = json.dumps(data)
    if damage == "duplicate":
        raw = raw.replace('"enabled": true', '"enabled": false, "enabled": true')
    with ctx[1].session_factory() as db:
        row = db.get(AppSetting, KEY)
        row.value_json = raw
        db.commit()
        with pytest.raises(AssistantSettingsError, match="takeover_settings_invalid"):
            TakeoverSettingsService(db).view()
        assert row.value_json == raw and not db.dirty


def test_api_ttl_default_range_and_strict_validation(client):
    path = "/api/v1/assistant/takeover/settings"
    assert client.get(path).json()["values"]["conclusion_ttl_days"] == 7
    response = client.put(path, json={"expected_revision": 0, "confirm": True, "values": {}})
    assert response.status_code == 200
    assert response.json()["values"]["conclusion_ttl_days"] == 7
    revision = response.json()["revision"]
    for invalid in [0, 31, "7", True, 7.0, None]:
        response = client.put(path, json={"expected_revision": revision, "confirm": True,
            "values": {"conclusion_ttl_days": invalid}})
        assert response.status_code == 422
        assert client.get(path).json()["revision"] == revision
    for days in [1, 30]:
        response = client.put(path, json={"expected_revision": revision, "confirm": True,
            "values": {"conclusion_ttl_days": days}})
        assert response.status_code == 200
        assert response.json()["values"]["conclusion_ttl_days"] == days
        revision += 1
