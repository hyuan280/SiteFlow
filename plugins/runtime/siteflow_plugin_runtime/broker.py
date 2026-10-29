from __future__ import annotations

from typing import Any

import httpx


class BrokerError(RuntimeError):
    pass


class BrokerClient:
    def __init__(self, url: str, token: str, *, timeout: float = 30.0) -> None:
        self.url = url
        self.token = token
        self.timeout = timeout

    def action(self, op: str, **payload: Any) -> dict[str, Any]:
        body = {"token": self.token, "op": op, **payload}
        try:
            response = httpx.post(self.url, json=body, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise BrokerError(f"browser broker request failed: {exc}") from exc
        if response.status_code != 200:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise BrokerError(f"browser action {op} failed: {detail}")
        try:
            value = response.json()
        except ValueError as exc:
            raise BrokerError("browser broker returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise BrokerError("browser broker returned a non-object response")
        return value
