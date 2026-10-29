from __future__ import annotations

import logging
import threading
from typing import Any

import httpx

from .config import Config

logger = logging.getLogger(__name__)


class RegistrationError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RegistrationClient:
    def __init__(
        self,
        config: Config,
        manifest: dict[str, Any],
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self.manifest = manifest
        self._client = http_client or httpx.Client(
            timeout=5.0,
            verify=config.verify_tls,
        )
        self._owns_client = http_client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _post(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        url = f"{self.config.control_url}{path}"
        headers = {
            "X-Plugin-Token": self.config.token(),
            "Accept": "application/json",
        }
        try:
            return self._client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise RegistrationError(f"plugin control request failed: {exc}") from exc
        except OSError as exc:
            raise RegistrationError(f"plugin control request failed: {exc}") from exc

    def register(self) -> int:
        response = self._post(
            "/api/v1/plugin-runtime/register",
            {
                "plugin_id": self.config.plugin_id,
                "protocol_version": self.manifest["protocol_version"],
                "endpoint": self.config.endpoint,
                "manifest": self.manifest,
            },
        )
        if response.status_code != 201:
            raise RegistrationError(
                f"plugin registration failed with HTTP {response.status_code}",
                response.status_code,
            )
        try:
            generation = response.json()["generation"]
        except (ValueError, KeyError, TypeError) as exc:
            raise RegistrationError("plugin registration response has no generation") from exc
        if not isinstance(generation, int) or generation < 1:
            raise RegistrationError("plugin registration generation is invalid")
        logger.info("registered plugin %s generation %s", self.config.plugin_id, generation)
        return generation

    def heartbeat(self, generation: int) -> bool:
        response = self._post(
            "/api/v1/plugin-runtime/heartbeat",
            {"plugin_id": self.config.plugin_id, "generation": generation},
        )
        if response.status_code in {401, 403, 404, 409, 422}:
            logger.warning(
                "plugin %s heartbeat rejected with HTTP %s",
                self.config.plugin_id,
                response.status_code,
            )
            return False
        if response.status_code != 200:
            raise RegistrationError(
                f"plugin heartbeat failed with HTTP {response.status_code}",
                response.status_code,
            )
        return True

    def unregister(self, generation: int) -> None:
        try:
            response = self._post(
                "/api/v1/plugin-runtime/unregister",
                {"plugin_id": self.config.plugin_id, "generation": generation},
            )
        except RegistrationError as exc:
            logger.warning("plugin unregister failed: %s", exc)
            return
        if response.status_code not in {204, 404}:
            logger.warning("plugin unregister returned HTTP %s", response.status_code)

    def run_forever(self, stop: threading.Event) -> None:
        generation: int | None = None
        delay = 1
        try:
            while not stop.is_set():
                try:
                    if generation is None:
                        generation = self.register()
                        delay = max(1, self.config.heartbeat_seconds)
                    elif self.heartbeat(generation):
                        delay = max(1, self.config.heartbeat_seconds)
                    else:
                        generation = None
                        delay = 1
                except RegistrationError as exc:
                    generation = None
                    delay = min(30, max(1, delay * 2))
                    logger.warning("plugin control retry in %ss: %s", delay, exc)
                except Exception as exc:
                    generation = None
                    delay = min(30, max(1, delay * 2))
                    logger.exception("plugin control loop failed: %s", exc)
                self._wait(stop, delay)
        finally:
            if generation is not None:
                self.unregister(generation)
            self.close()

    @staticmethod
    def _wait(stop: threading.Event, seconds: float) -> None:
        stop.wait(seconds)
