from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from uuid import uuid4


def ensure_trusted_urls_column(engine: Engine) -> None:
    """Add the trusted URL field to persisted sites without rewriting existing data."""
    with engine.begin() as connection:
        columns = inspect(connection).get_columns("sites")
        if not any(column["name"] == "trusted_urls_json" for column in columns):
            connection.execute(text(
                "ALTER TABLE sites ADD COLUMN trusted_urls_json TEXT NOT NULL DEFAULT '[]'"
            ))


def ensure_cookiecloud_all_sites_column(engine: Engine) -> None:
    with engine.begin() as connection:
        columns = inspect(connection).get_columns("cookiecloud_servers")
        if not any(column["name"] == "all_sites" for column in columns):
            connection.execute(text(
                "ALTER TABLE cookiecloud_servers ADD COLUMN all_sites BOOLEAN NOT NULL DEFAULT FALSE"
            ))


def ensure_workflow_history_keys(engine: Engine) -> None:
    """Incremental migration: keep existing local revisions and stable identities."""
    with engine.begin() as connection:
        for table in ("templates", "sites"):
            columns = inspect(connection).get_columns(table)
            if not any(column["name"] == "history_key" for column in columns):
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN history_key VARCHAR(36)"))
            ids = connection.execute(text(
                f"SELECT id FROM {table} WHERE history_key IS NULL OR history_key = ''"
            )).scalars().all()
            for owner_id in ids:
                connection.execute(text(
                    f"UPDATE {table} SET history_key = :key WHERE id = :id"
                ), {"key": str(uuid4()), "id": owner_id})
            connection.execute(text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{table}_history_key ON {table} (history_key)"
            ))
