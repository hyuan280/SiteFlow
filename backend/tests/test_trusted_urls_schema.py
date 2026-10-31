import json

from fastapi.testclient import TestClient
from sqlalchemy import Column, MetaData, Table, create_engine, event, inspect, select, text

from app.core.db import create_db_engine
from app.core.schema_updates import ensure_trusted_urls_column
from app.main import create_app
from app.models.workflow import Site
from tests.helpers import login_client


def test_schema_update_adds_only_missing_column_without_data_loss():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE sites (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"))
        connection.execute(text("INSERT INTO sites VALUES (7, 'existing site')"))
        connection.execute(text("CREATE INDEX existing_site_name ON sites (name)"))
        connection.execute(text("CREATE TABLE unrelated (value TEXT)"))
        connection.execute(text("INSERT INTO unrelated VALUES ('keep me')"))

    ensure_trusted_urls_column(engine)
    columns = {column["name"]: column for column in inspect(engine).get_columns("sites")}
    assert set(columns) == {"id", "name", "trusted_urls_json"}
    assert columns["trusted_urls_json"]["nullable"] is False
    assert columns["trusted_urls_json"]["default"] == "'[]'"
    assert isinstance(columns["trusted_urls_json"]["type"], type(Site.__table__.c.trusted_urls_json.type))
    assert inspect(engine).get_indexes("sites")[0]["name"] == "existing_site_name"
    with engine.begin() as connection:
        assert connection.execute(text("SELECT * FROM sites")).one() == (7, "existing site", "[]")
        connection.execute(text("UPDATE sites SET trusted_urls_json = :patterns"), {
            "patterns": json.dumps([r"^https://existing\.example/"]),
        })

    statements = []
    event.listen(engine, "before_cursor_execute", lambda conn, cursor, statement, *args: statements.append(statement))
    ensure_trusted_urls_column(engine)
    ensure_trusted_urls_column(engine)
    assert not any(statement.lstrip().upper().startswith("ALTER") for statement in statements)
    with engine.begin() as connection:
        assert json.loads(connection.execute(text("SELECT trusted_urls_json FROM sites")).scalar_one()) == [
            r"^https://existing\.example/",
        ]
        connection.execute(text("INSERT INTO sites (id, name) VALUES (8, 'new site')"))
        assert connection.execute(text("SELECT trusted_urls_json FROM sites WHERE id = 8")).scalar_one() == "[]"
        assert connection.execute(text("SELECT value FROM unrelated")).scalar_one() == "keep me"


def test_startup_upgrades_persisted_sites_and_preserves_data(settings):
    engine = create_db_engine(settings.database_url)
    legacy_sites = Table("sites", MetaData(), *[
        Column(
            column.name, column.type, primary_key=column.primary_key, nullable=column.nullable,
            default=column.default.arg if column.default is not None else None,
        )
        for column in Site.__table__.columns if column.name != "trusted_urls_json"
    ])
    legacy_sites.create(engine)
    with engine.begin() as connection:
        connection.execute(legacy_sites.insert().values(
            id=42, name="legacy site", base_url="https://legacy.example", notes="preserve notes",
            tags_json='["old"]', config_revision=9,
        ))
        before = dict(connection.execute(select(legacy_sites)).mappings().one())
    engine.dispose()

    app = create_app(settings)
    client = TestClient(app)
    login_client(client)
    response = client.get("/api/v1/sites/42")
    assert response.status_code == 200, response.text
    assert response.json()["trusted_urls"] == []
    with app.state.engine.connect() as connection:
        after = dict(connection.execute(select(legacy_sites)).mappings().one())
        assert after == before
    patterns = [r"^https://login\.example/"]
    response = client.put("/api/v1/sites/42", json={
        "name": "legacy site", "base_url": "https://legacy.example", "trusted_urls": patterns,
    })
    assert response.status_code == 200, response.text
    reopened = TestClient(create_app(settings))
    login_client(reopened)
    assert reopened.get("/api/v1/sites/42").json()["trusted_urls"] == patterns
    assert reopened.get("/api/v1/sites").json()[0]["trusted_urls"] == patterns


def test_fresh_schema_has_python_and_server_defaults(client):
    app = client.app
    column = next(column for column in inspect(app.state.engine).get_columns("sites")
                  if column["name"] == "trusted_urls_json")
    assert column["nullable"] is False
    assert column["default"] == "'[]'"
    with app.state.session_factory() as session:
        site = Site(name="orm default", base_url="https://orm.example")
        session.add(site)
        session.commit()
        assert site.trusted_urls_json == "[]"
    with app.state.engine.begin() as connection:
        values = dict(connection.execute(text("SELECT * FROM sites")).mappings().one())
        values.pop("id")
        values.pop("trusted_urls_json")
        values["name"] = "server default"
        names = ", ".join(values)
        parameters = ", ".join(f":{name}" for name in values)
        connection.execute(text(f"INSERT INTO sites ({names}) VALUES ({parameters})"), values)
        assert connection.execute(text(
            "SELECT trusted_urls_json FROM sites WHERE name = 'server default'"
        )).scalar_one() == "[]"
    ensure_trusted_urls_column(app.state.engine)
    assert all(site["trusted_urls"] == [] for site in client.get("/api/v1/sites").json())
