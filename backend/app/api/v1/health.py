from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import text

router = APIRouter(prefix="/api/v1", tags=["health"])


@router.get("/health")
def health(request: Request) -> dict[str, object]:
    engine = request.app.state.engine
    db_ok = True
    db_error = None
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        db_ok = False
        db_error = f"{type(exc).__name__}: {str(exc)[:200]}"
    return {
        "ok": db_ok,
        "app": request.app.state.settings.app_name,
        "database": {"ok": db_ok, "error": db_error},
    }
