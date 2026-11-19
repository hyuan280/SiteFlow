from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse


LANES = ("general", "interaction", "proxy_general", "proxy_interaction")
TTL_SECONDS = 60
TOMBSTONE_SECONDS = 600
MAX_OWNERS = 1024
MAX_TICKETS = 4096
# Reserve room for permanent fences before admitting identities. Exhaustion is
# fail-closed until a new registry generation, never an invitation to replay.
MAX_RETIRED = 262144
MAX_CLAIMS = 131072
CLOSE_WAIT_SECONDS = 5
RETRY_SECONDS = 1
INVALID_RESERVATION = "Invalid browser dispatch reservation"


def identifier(value: Any) -> str:
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError
    return value


@dataclass
class Ticket:
    id: str
    owner: str
    lane: str | None
    state: str = "pending"
    expires: float = 0
    retired: float = 0
    pin: str | None = None
    session: Any = None
    task: asyncio.Task | None = None
    cleanup_task: asyncio.Task | None = None
    waiter: object = field(default_factory=object)


@dataclass
class Owner:
    run_id: int | None
    tickets: dict[str, Ticket] = field(default_factory=dict)
    retired: float = 0
    closed: bool = False


class DispatchRegistry:
    """Pins are ordinary lane leases, independent of job leases and viewers."""

    def __init__(self, manager):
        self.manager = manager
        self.generation = str(uuid.uuid4())
        self.owners: dict[str, Owner] = {}
        self.tickets: dict[str, Ticket] = {}
        self.retired_owners: set[str] = set()
        self.retired_tickets: set[str] = set()
        self.claims: set[tuple[int, int, int]] = set()
        self.cleanup_tasks: set[asyncio.Task] = set()
        self.reaper: asyncio.Task | None = None
        self.closed = False
        self.close_task: asyncio.Task | None = None

    def start(self):
        if self.reaper is None and not self.closed:
            self.reaper = asyncio.create_task(self._reap(), name="browser-dispatch-reaper")

    def _track_cleanup(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.cleanup_tasks.add(task)

        def settled(done):
            self.cleanup_tasks.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(settled)
        return task

    def _unqueue(self, ticket):
        self.manager._seat_waiters.pop(ticket.waiter, None)
        self.manager._seat_changed.set()

    def _release(self, ticket, now):
        # Invalidate before cancellation or any await: delayed PUTs cannot revive it.
        ticket.state = "released"
        ticket.retired = now
        self._unqueue(ticket)
        if ticket.task is not None and not ticket.task.done():
            ticket.task.cancel()
        if ticket.pin is not None:
            self._cleanup_pin(ticket)

    def _cleanup_pin(self, ticket):
        if ticket.cleanup_task is None or ticket.cleanup_task.done():
            ticket.cleanup_task = self._track_cleanup(self._drop_pin(ticket))
        return ticket.cleanup_task

    async def _drop_pin(self, ticket):
        pin = ticket.pin
        if pin is not None:
            await self.manager.release_lane(ticket.lane, pin)
            if ticket.pin == pin:
                ticket.pin = None
                ticket.session = None

    def expire(self):
        now = time.monotonic()
        for ticket in list(self.tickets.values()):
            if ticket.state in {"pending", "granted"} and ticket.expires <= now:
                self._release(ticket, now)
            if (ticket.retired and now - ticket.retired >= TOMBSTONE_SECONDS
                    and ticket.pin is None and (ticket.task is None or ticket.task.done())
                    and (ticket.cleanup_task is None or ticket.cleanup_task.done())):
                self.retired_tickets.add(ticket.id)
                self.tickets.pop(ticket.id)
                self.owners[ticket.owner].tickets.pop(ticket.id, None)
        for key, owner in list(self.owners.items()):
            if not owner.tickets and now - owner.retired >= TOMBSTONE_SECONDS:
                self.retired_owners.add(key)
                self.owners.pop(key)

    async def _reap(self):
        while True:
            await asyncio.sleep(1)
            self.expire()

    def _room(self, owners=0, tickets=0):
        if (self.closed or self.manager._stopping
                or len(self.owners) + owners > MAX_OWNERS
                or len(self.tickets) + tickets > MAX_TICKETS
                or len(self.owners) + len(self.tickets) + len(self.retired_owners)
                + len(self.retired_tickets) + owners + tickets > MAX_RETIRED):
            raise HTTPException(503, "Browser dispatch unavailable")

    def register(self, owner_id: str, run_id: int, rows: list[dict]) -> dict:
        self.expire()
        if owner_id in self.retired_owners or any(row["id"] in self.retired_tickets for row in rows):
            raise HTTPException(409, "Browser dispatch binding conflict")
        owner = self.owners.get(owner_id)
        if owner is not None and (owner.closed or owner.run_id not in (None, run_id)):
            raise HTTPException(409, "Browser dispatch binding conflict")
        for row in rows:
            ticket = self.tickets.get(row["id"])
            if ticket is not None and (ticket.owner != owner_id or (
                    ticket.lane is not None and ticket.lane != row["lane"])):
                raise HTTPException(409, "Browser dispatch binding conflict")
        self._room(int(owner is None), sum(row["id"] not in self.tickets for row in rows))
        now = time.monotonic()
        if owner is None:
            owner = self.owners[owner_id] = Owner(run_id, retired=now)
        owner.run_id = run_id
        owner.retired = now
        new = []
        # Seed every priority identity before any acquisition task can run.
        for row in sorted(rows, key=lambda row: LANES.index(row["lane"])):
            ticket = self.tickets.get(row["id"])
            if ticket is None:
                ticket = Ticket(row["id"], owner_id, row["lane"])
                self.tickets[ticket.id] = owner.tickets[ticket.id] = ticket
                self.manager._seat_waiters[ticket.waiter] = ticket.lane
                new.append(ticket)
            if ticket.lane is None:
                ticket.lane = row["lane"]  # A pre-create DELETE remains released.
            if ticket.state in {"pending", "granted"}:
                ticket.expires = now + TTL_SECONDS
        self.start()
        for ticket in new:
            ticket.task = asyncio.create_task(self._acquire(ticket), name="browser-dispatch-acquire")
        return self.view([self.tickets[row["id"]] for row in rows])

    def view(self, tickets):
        return {"generation": self.generation, "tickets": [
            {"id": ticket.id, "lane": ticket.lane, "state": ticket.state}
            for ticket in sorted(tickets, key=lambda ticket: LANES.index(ticket.lane))
        ]}

    async def _acquire(self, ticket):
        from worker.app import LaneCapacityError

        try:
            while ticket.state == "pending" and not self.closed:
                try:
                    pin, _ = await self.manager.lease_lane(ticket.lane)
                except LaneCapacityError:
                    if self.manager._stopping:
                        return
                    await asyncio.sleep(RETRY_SECONDS)
                    continue
                # No await between receipt and ownership, including cancel/grant races.
                ticket.pin = pin
                ticket.session = self.manager.get(ticket.lane).session
                if ticket.state == "pending" and ticket.expires > time.monotonic() and not self.closed:
                    ticket.state = "granted"
                else:
                    ticket.state = "released"
                    ticket.retired = time.monotonic()
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            if ticket.state == "pending":
                ticket.state = "failed"
                ticket.retired = time.monotonic()
        finally:
            self._unqueue(ticket)
            if ticket.state != "granted" and ticket.pin is not None:
                await asyncio.shield(self._cleanup_pin(ticket))

    def delete(self, owner_id, ticket_id=None):
        self.expire()
        if owner_id in self.retired_owners or ticket_id in self.retired_tickets:
            return self.view([])
        now = time.monotonic()
        owner = self.owners.get(owner_id)
        ticket = self.tickets.get(ticket_id) if ticket_id is not None else None
        if ticket is not None and ticket.owner != owner_id:
            raise HTTPException(409, "Browser dispatch binding conflict")
        self._room(int(owner is None), int(ticket_id is not None and ticket is None))
        if owner is None:
            owner = self.owners[owner_id] = Owner(None, retired=now)
        owner.retired = now
        if ticket_id is None:
            owner.closed = True
            for ticket in owner.tickets.values():
                self._release(ticket, now)
            return self.view([ticket for ticket in owner.tickets.values() if ticket.lane is not None])
        if ticket is None:
            ticket = Ticket(ticket_id, owner_id, None)
            self.tickets[ticket_id] = owner.tickets[ticket_id] = ticket
        self._release(ticket, now)
        return self.view([ticket] if ticket.lane is not None else [])

    async def lease_job(self, key, run_id, capability, *, job_id, attempt):
        try:
            if (not isinstance(capability, dict) or set(capability) != {"owner", "id", "generation"}
                    or type(run_id) is not int or run_id <= 0
                    or type(job_id) is not int or job_id <= 0
                    or type(attempt) is not int or not 1 <= attempt <= 3
                    or capability["generation"] != self.generation):
                raise ValueError
            owner_id, ticket_id = identifier(capability["owner"]), identifier(capability["id"])
            lane = self.manager.get(key)
            async with self.manager._admission_lock, lane.lock:
                owner = self.owners.get(owner_id)
                ticket = self.tickets.get(ticket_id)
                if (self.closed or self.manager._stopping or owner is None or owner.closed
                        or owner.run_id != run_id or ticket is None or ticket.owner != owner_id
                        or ticket.lane != key or ticket.state != "granted"
                        or ticket.expires <= time.monotonic() or ticket.pin not in lane.leases
                        or ticket.session is None or lane.session is not ticket.session
                        or lane.session.disconnected or lane.status != "running" or not lane.enabled):
                    raise ValueError
                view = lane.view()
                lease = uuid.uuid4().hex
                claim = (run_id, job_id, attempt)
                if claim in self.claims or len(self.claims) >= MAX_CLAIMS:
                    raise ValueError
                # Keep completed and cancelled claims too: retries use a new attempt.
                self.claims.add(claim)
                lane.leases.add(lease)
                lane.last_activity_at = time.monotonic()
                return lease, view
        except (ValueError, TypeError, KeyError, HTTPException):
            raise RuntimeError(INVALID_RESERVATION) from None

    async def close(self):
        if self.close_task is None:
            self.closed = True
            now = time.monotonic()
            for owner in self.owners.values():
                owner.closed = True
            for ticket in self.tickets.values():
                self._release(ticket, now)

            async def cleanup():
                if self.reaper is not None:
                    self.reaper.cancel()
                    await asyncio.gather(self.reaper, return_exceptions=True)
                tasks = [ticket.task for ticket in self.tickets.values() if ticket.task is not None]
                await asyncio.gather(*tasks, return_exceptions=True)
                while self.cleanup_tasks:
                    await asyncio.gather(*self.cleanup_tasks, return_exceptions=True)

            self.close_task = asyncio.create_task(cleanup())
        # Do not cancel or await cancellation acknowledgement on timeout. The
        # retained owner can finish after teardown releases admission/lane locks.
        done, _ = await asyncio.wait({self.close_task}, timeout=CLOSE_WAIT_SECONDS)
        if done:
            self.close_task.result()


class DispatchBoundary:
    """Authenticate before parsing, cap even streamed bodies, and never cache errors."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/browser/dispatch"):
            return await self.app(scope, receive, send)

        async def no_store(message):
            if message["type"] == "http.response.start":
                message["headers"] = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                message["headers"].append((b"cache-control", b"no-store"))
            await send(message)

        headers = scope.get("headers", [])
        auth = [value for key, value in headers if key.lower() == b"authorization"]
        token = os.environ.get("SITEFLOW_BROWSER_TOKEN", "")
        if (any(key.lower() == b"origin" for key, _ in headers) or not token
                or len(auth) != 1 or not secrets.compare_digest(auth[0], f"Bearer {token}".encode())):
            return await JSONResponse({"detail": "Forbidden"}, status_code=403)(scope, receive, no_store)
        body = bytearray()
        try:
            async with asyncio.timeout(10):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > 8192:
                        return await JSONResponse({"detail": "Browser request too large"}, status_code=413)(scope, receive, no_store)
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            return await JSONResponse({"detail": "Browser request timed out"}, status_code=408)(scope, receive, no_store)

        async def replay():
            nonlocal body
            if body is not None:
                message = {"type": "http.request", "body": bytes(body), "more_body": False}
                body = None
                return message
            return await receive()

        await self.app(scope, replay, no_store)


router = APIRouter()


def unique_object(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError
    return value


@router.api_route("/browser/dispatch/{owner_id}", methods=["PUT", "DELETE"])
@router.delete("/browser/dispatch/{owner_id}/{ticket_id}")
async def dispatch_request(request: Request, owner_id: str):
    ticket_id = request.path_params.get("ticket_id")
    try:
        if request.query_params:
            raise ValueError
        identifier(owner_id)
        if ticket_id is not None:
            identifier(ticket_id)
        if request.method == "PUT":
            payload = json.loads((await request.body()).decode("utf-8"), object_pairs_hook=unique_object)
            if (not isinstance(payload, dict) or set(payload) != {"run_id", "tickets"}
                    or type(payload["run_id"]) is not int or payload["run_id"] <= 0
                    or not isinstance(payload["tickets"], list) or not 1 <= len(payload["tickets"]) <= 4):
                raise ValueError
            rows = payload["tickets"]
            for row in rows:
                if (not isinstance(row, dict) or set(row) != {"id", "lane"}
                        or row["lane"] not in LANES):
                    raise ValueError
                identifier(row["id"])
            if len({row["id"] for row in rows}) != len(rows) or len({row["lane"] for row in rows}) != len(rows):
                raise ValueError
    except (ValueError, TypeError, KeyError, RecursionError):
        raise HTTPException(422, "Invalid browser request") from None
    registry = request.app.state.manager.dispatch
    if request.method == "PUT":
        return registry.register(owner_id, payload["run_id"], rows)
    return registry.delete(owner_id, ticket_id)
