from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


def ensure_trusted_urls_column(engine: Engine) -> None:
    """Add the trusted URL field to persisted sites without rewriting existing data."""
    with engine.begin() as connection:
        columns = inspect(connection).get_columns("sites")
        if not any(column["name"] == "trusted_urls_json" for column in columns):
            connection.execute(text(
                "ALTER TABLE sites ADD COLUMN trusted_urls_json TEXT NOT NULL DEFAULT '[]'"
            ))
