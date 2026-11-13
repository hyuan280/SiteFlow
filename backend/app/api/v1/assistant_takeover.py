"""Authenticated takeover policy and owner-scoped persistent audit."""

import asyncio

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_readonly_current_session
from app.api.v1.assistant import _AssistantRoute, mutation_owner
from app.models.auth import AuthSession
from app.schemas.assistant_takeover import TakeoverActionsView, TakeoverSettingsUpdate, TakeoverSettingsView
from app.services.assistant_takeover_settings import TakeoverSettingsService


router = APIRouter(prefix="/api/v1/assistant/takeover", tags=["assistant"],
                   route_class=_AssistantRoute)


@router.get("/settings", response_model=TakeoverSettingsView)
def get_settings(current: AuthSession = Depends(get_readonly_current_session),
                 db: Session = Depends(get_db)):
    return TakeoverSettingsService(db).view()


@router.put("/settings", response_model=TakeoverSettingsView)
async def put_settings(payload: TakeoverSettingsUpdate, request: Request,
                       owner: str = Depends(mutation_owner)):
    def update():
        with request.app.state.session_factory() as db:
            return TakeoverSettingsService(db).update(payload, owner)

    result = await asyncio.to_thread(update)
    request.app.state.assistant_takeover.wake()
    return result


@router.get("/actions", response_model=TakeoverActionsView)
def get_actions(request: Request, current: AuthSession = Depends(get_readonly_current_session),
                limit: int = Query(default=100, ge=1, le=100)):
    return request.app.state.assistant_takeover.actions(current.username, limit)
