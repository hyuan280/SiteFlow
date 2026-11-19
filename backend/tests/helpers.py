from __future__ import annotations

import base64
import secrets
from uuid import UUID, uuid4

from app.core.config import Settings


class GrantAllDispatchClient:
    """Reservation RPC fake for tests that isolate worker business execution."""

    def __init__(self):
        self.generation = str(uuid4())
        self.owners = {}
        self.tickets = {}
        self.released = set()
        self.closed = set()

    async def renew(self, owner, run_id, tickets):
        assert str(UUID(owner)) == owner
        assert isinstance(run_id, int) and run_id > 0
        assert owner not in self.closed
        assert self.owners.setdefault(owner, run_id) == run_id
        lanes = ("general", "interaction", "proxy_general", "proxy_interaction")
        assert 1 <= len(tickets) <= len(lanes)
        assert len({ticket.lane for ticket in tickets}) == len(tickets)
        assert len({ticket.id for ticket in tickets}) == len(tickets)
        for ticket in tickets:
            assert str(UUID(ticket.id)) == ticket.id
            assert ticket.lane in lanes
            key = owner, ticket.id
            assert key not in self.released
            assert self.tickets.setdefault(key, ticket.lane) == ticket.lane
        return self.generation, {ticket.id: "granted" for ticket in tickets}

    async def release(self, owner, ticket):
        self.released.add((owner, ticket))
        self.tickets.pop((owner, ticket), None)

    async def close(self, owner):
        self.closed.add(owner)
        for key in list(self.tickets):
            if key[0] == owner:
                self.released.add(key)
                del self.tickets[key]

    def validate(self, payload):
        reservation = payload["dispatch_reservation"]
        assert set(reservation) == {"owner", "id", "generation"}
        assert reservation["generation"] == self.generation
        assert self.owners[reservation["owner"]] == payload["run_id"]
        assert self.tickets[reservation["owner"], reservation["id"]] == payload["lane_key"]


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "_env_file": None,
        "database_url": f"sqlite:///{tmp_path}/test.db",
        "artifact_root": str(tmp_path / "artifacts"),
        "master_key": base64.b64encode(secrets.token_bytes(32)).decode(),
        "proxy_url": None,
        "twofauth_url": None,
        "twofauth_token": None,
        "cfbox_url": "http://cfbox.example.com",
        "cfbox_api_key": "test-cfbox-key",
        "bitwarden_url": None,
        "bitwarden_token": None,
        "bitwarden_enabled": None,
        "bitwarden_auto_detect": False,
        "admin_username": "admin",
        "admin_password": "test-password",
        "worker_timeout": 0.2,
    }
    values.update(overrides)
    return Settings(**values)


def login_client(client, username: str = "admin", password: str = "test-password") -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    csrf = response.json()["csrf_token"]
    client.headers.update({"X-CSRF-Token": csrf})
    return csrf
