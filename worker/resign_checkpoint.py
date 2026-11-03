"""Acknowledged resign reservations, separate from best-effort progress streaming."""
from __future__ import annotations

import asyncio
import copy
import os

import httpx


CHECKPOINT_TIMEOUT_SECONDS = 20
CHECKPOINT_ERROR = "补签持久化检查点未获确认，已停止；服务端可能已保留未决记录，请人工核验"


class ResignCheckpointError(Exception):
    def __init__(self):
        super().__init__(CHECKPOINT_ERROR)


def make_resign_checkpoint(payload, step_key):
    access = payload.get("secret_access")
    token = access.get("token") if isinstance(access, dict) else None
    if (not isinstance(token, str) or not token.strip()
            or not payload.get("job_id") or not payload.get("site_id") or not step_key):
        raise ResignCheckpointError()
    url = os.environ.get("SITEFLOW_RESIGN_CHECKPOINT_URL",
                         "http://api:8000/api/v1/internal/resign/checkpoint")

    async def checkpoint(state, phase, target_date):
        async def post():
            async with httpx.AsyncClient(timeout=CHECKPOINT_TIMEOUT_SECONDS, follow_redirects=False) as client:
                response = await client.post(
                    url, headers={"Authorization": f"Bearer {token}"},
                    json={"job_id": payload["job_id"], "site_id": payload["site_id"],
                          "step_key": step_key, "phase": phase, "date": target_date,
                          "result": {"resign": copy.deepcopy(state)}},
                )
                if response.status_code != 200:
                    raise ResignCheckpointError()
                body = response.json()
                if not isinstance(body, dict) or body.get("ok") is not True:
                    raise ResignCheckpointError()

        try:
            # httpx's per-I/O timeout alone does not bound the whole exchange.
            await asyncio.wait_for(post(), timeout=CHECKPOINT_TIMEOUT_SECONDS)
        except Exception:
            # Never include endpoint, bearer token, response body or transport errors.
            raise ResignCheckpointError() from None

    return checkpoint
