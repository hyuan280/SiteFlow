from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import httpx

from app.core.config import Settings
from app.services.lanes import LANE_LABELS

DISPATCH_ERROR = "Browser dispatch unavailable; execution was not retried"
DISPATCH_GENERATION_ERROR = "Browser dispatch generation changed; execution was not retried"
POLL_SECONDS = 1.0
TRANSPORT_GRACE_SECONDS = 15.0  # Stop well before the worker's 60-second ticket TTL.


class DispatchError(RuntimeError):
    def __init__(self, message: str = DISPATCH_ERROR) -> None:
        super().__init__(message)


class DispatchTransportError(DispatchError):
    pass


@dataclass
class DispatchTicket:
    lane: str
    id: str = field(default_factory=lambda: str(uuid4()))
    state: str = "pending"


class DispatchClient:
    """Narrow, authenticated worker RPCs. Never retry with a new identity."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def _request(self, method: str, owner: str, ticket: str | None = None,
                       payload: dict | None = None) -> dict | None:
        token = self.settings.browser_token
        if token is None or not token.get_secret_value():
            raise DispatchError()
        path = f"/browser/dispatch/{UUID(owner)}"
        if ticket is not None:
            path += f"/{UUID(ticket)}"
        try:
            async with asyncio.timeout(10):
                async with httpx.AsyncClient(
                    timeout=10.0, trust_env=False, follow_redirects=False,
                ) as client:
                    async with client.stream(
                        method, f"{self.settings.worker_url.rstrip('/')}{path}",
                        headers={"Authorization": f"Bearer {token.get_secret_value()}",
                                 "Accept-Encoding": "identity"},
                        **({"json": payload} if payload is not None else {}),
                    ) as response:
                        if response.status_code not in ({200} if method == "PUT" else {200, 204}):
                            if response.status_code in {502, 503, 504}:
                                raise DispatchTransportError()
                            raise DispatchError()
                        if response.headers.get("content-encoding", "identity").lower() != "identity":
                            raise DispatchError()
                        body = bytearray()
                        async for chunk in response.aiter_raw(chunk_size=8192):
                            if len(body) + len(chunk) > 65536:
                                raise DispatchError()
                            body.extend(chunk)
            if method == "DELETE":
                return None
            data = json.loads(body)
            if not isinstance(data, dict):
                raise DispatchError()
            return data
        except DispatchError:
            raise
        except (httpx.TransportError, TimeoutError):
            raise DispatchTransportError() from None
        except Exception:
            # Provider responses, URLs and credentials must not reach job messages/logs.
            raise DispatchError() from None

    async def renew(self, owner: str, run_id: int,
                    tickets: list[DispatchTicket]) -> tuple[str, dict[str, str]]:
        ordered = sorted(tickets, key=lambda item: list(LANE_LABELS).index(item.lane))
        if not 1 <= len(ordered) <= 4 or len({item.lane for item in ordered}) != len(ordered):
            raise DispatchError()
        data = await self._request("PUT", owner, payload={
            "run_id": run_id, "tickets": [{"id": item.id, "lane": item.lane} for item in ordered],
        })
        try:
            generation = str(UUID(data["generation"]))
            rows = data["tickets"]
            if not isinstance(rows, list) or len(rows) != len(ordered):
                raise ValueError()
            expected = {item.id: item.lane for item in ordered}
            states = {}
            for row in rows:
                identity = str(UUID(row["id"]))
                if identity in states or row["lane"] != expected[identity]:
                    raise ValueError()
                state = row["state"]
                if state not in {"pending", "granted", "failed", "released"}:
                    raise ValueError()
                states[identity] = state
            return generation, states
        except Exception:
            raise DispatchError() from None

    async def release(self, owner: str, ticket: str) -> None:
        await self._request("DELETE", owner, ticket)

    async def close(self, owner: str) -> None:
        await self._request("DELETE", owner)


class RunDispatch:
    def __init__(self, client: DispatchClient, run_id: int) -> None:
        self.client = client
        self.run_id = run_id
        self.owner = str(uuid4())
        self.generation: str | None = None
        self.tickets: dict[str, DispatchTicket] = {}
        self.changed = asyncio.Event()
        self.healthy = asyncio.Event()
        self.wake = asyncio.Event()
        self.error: str | None = None
        self.stopping = False

    def abort(self, message: str = DISPATCH_ERROR) -> None:
        self.error = message
        self.healthy.set()
        self.changed.set()

    def add(self, lane: str) -> None:
        self.tickets[lane] = DispatchTicket(lane)
        self.wake.set()

    async def release(self, lane: str) -> None:
        ticket = self.tickets.pop(lane)
        # Remove before DELETE: an in-flight renewal can only hit its tombstone.
        await self.client.release(self.owner, ticket.id)
        self.wake.set()

    async def reservation(self, ticket: DispatchTicket) -> dict[str, str]:
        await self.healthy.wait()
        if self.error or self.stopping or ticket.state != "granted":
            raise DispatchError(self.error or DISPATCH_ERROR)
        return {"owner": self.owner, "id": ticket.id, "generation": self.generation}

    async def maintain(self) -> None:
        failure_since: float | None = None
        loop = asyncio.get_running_loop()
        try:
            while not self.error:
                self.wake.clear()
                snapshot = [self.tickets[lane] for lane in LANE_LABELS if lane in self.tickets]
                if snapshot:
                    try:
                        generation, states = await self.client.renew(self.owner, self.run_id, snapshot)
                        if self.generation is not None and generation != self.generation:
                            self.abort(DISPATCH_GENERATION_ERROR)
                            return
                        self.generation = generation
                        for ticket in snapshot:
                            if self.tickets.get(ticket.lane) is not ticket:
                                continue
                            state = states[ticket.id]
                            if state == "released" or (ticket.state == "granted" and state == "pending"):
                                self.abort()
                                return
                            ticket.state = state
                        failure_since = None
                        self.healthy.set()
                    except DispatchTransportError:
                        self.healthy.clear()
                        failure_since = failure_since if failure_since is not None else loop.time()
                        if loop.time() - failure_since >= TRANSPORT_GRACE_SECONDS:
                            self.abort()
                    self.changed.set()
                try:
                    await asyncio.wait_for(self.wake.wait(), POLL_SECONDS)
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            self.abort()
