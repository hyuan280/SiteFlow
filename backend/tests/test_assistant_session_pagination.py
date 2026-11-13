from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import event, select

from app.models.assistant import AssistantSession
from app.models.auth import AuthSession
from app.services.assistant import AssistantError
from tests.test_assistant_api import ROOT, assistant_client  # noqa: F401


def seed(manager):
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    expected = []
    with manager.session_factory() as db:
        for index in range(215):
            identifier = str(UUID(int=index + 1))
            row = AssistantSession(
                id=identifier, owner="admin", status="idle", created_at=now,
                updated_at=now + timedelta(seconds=index // 3),
            )
            manager._encrypt(row, {"title": f"Session {index}", "target": None})
            db.add(row)
            expected.append(identifier)
        # A newer foreign owner's row must not affect lookahead or page boundaries.
        row = AssistantSession(id=str(UUID(int=999)), owner="other", status="idle",
                               created_at=now, updated_at=now + timedelta(days=1))
        manager._encrypt(row, {"title": "PRIVATE", "target": None})
        db.add(row)
        db.commit()
    return list(reversed(expected))


def test_pages_owner_ties_defaults_and_read_only(assistant_client):
    client = assistant_client
    manager = client.app.state.assistant_manager
    expected = seed(manager)
    with manager.session_factory() as db:
        auth = db.scalar(select(AuthSession))
        before = (auth.last_seen_at, auth.expires_at)
    first = client.get(ROOT)
    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    page = first.json()
    assert (page["offset"], page["limit"], page["next_offset"], page["truncated"]) == (0, 100, 100, True)
    assert [item["id"] for item in page["items"]] == expected[:100]
    assert "PRIVATE" not in first.text
    collected = list(page["items"])
    for offset, next_offset, size in [(100, 200, 100), (200, None, 15), (215, None, 0), (9999, None, 0)]:
        response = client.get(ROOT, params={"offset": offset})
        assert response.status_code == 200
        page = response.json()
        assert page["offset"] == offset and page["limit"] == 100
        assert page["next_offset"] == next_offset
        assert page["truncated"] is (next_offset is not None)
        assert len(page["items"]) == size
        collected.extend(page["items"])
    assert [item["id"] for item in collected] == expected
    assert manager.list_sessions("other")["items"][0]["title"] == "PRIVATE"
    assert manager.list_sessions("nobody") == {
        "items": [], "offset": 0, "limit": 100, "next_offset": None, "truncated": False,
    }
    assert client.get(ROOT, params={"offset": 214, "limit": 1}).json()["next_offset"] is None
    assert client.get(ROOT, params={"offset": 0, "limit": 1}).json()["next_offset"] == 1
    # Even an arbitrarily large nonnegative offset is an exhausted page, not a DB overflow.
    assert client.get(ROOT, params={"offset": 10**30}).json()["items"] == []
    with manager.session_factory() as db:
        auth = db.scalar(select(AuthSession))
        assert (auth.last_seen_at, auth.expires_at) == before


@pytest.mark.parametrize("name,value", [
    ("offset", "-1"), ("limit", "0"), ("limit", "101"), ("limit", "-1"),
    *[(name, value) for name in ("offset", "limit") for value in
      ("1.0", "1.5", "1e1", "true", "false", "null", "", " 1 ", "NaN", "Infinity", "abc", "\u0661")],
])
def test_query_bounds_and_strict_integers(assistant_client, name, value):
    response = assistant_client.get(ROOT, params={name: value})
    assert response.status_code == 422, response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("offset,limit", [
    (-1, 100), (False, 100), (0.0, 100), ("0", 100),
    (0, 0), (0, 101), (0, True), (0, 1.0), (0, "1"),
])
def test_manager_validates_bounds(assistant_client, offset, limit):
    with pytest.raises(AssistantError) as exc:
        assistant_client.app.state.assistant_manager.list_sessions("admin", offset, limit)
    assert exc.value.status == 422


def test_lookahead_is_not_decrypted_and_no_count(assistant_client, monkeypatch):
    manager = assistant_client.app.state.assistant_manager
    seed(manager)
    decrypted, statements = [], []
    original = manager._decrypt

    def decrypt(row):
        decrypted.append(row.id)
        return original(row)

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    monkeypatch.setattr(manager, "_decrypt", decrypt)
    with manager.session_factory() as db:
        engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        page = manager.list_sessions("admin", offset=100, limit=10)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(decrypted) == 10 and len(page["items"]) == 10
    assert page["next_offset"] == 110 and page["truncated"] is True
    assert len(statements) == 1 and "count(" not in statements[0].lower()
