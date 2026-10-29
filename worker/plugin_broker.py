from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

DEFAULT_OPS = {
    "goto",
    "click",
    "fill",
    "read_text",
    "wait_for",
    "screenshot",
    "query_selector",
}
MAX_SELECTORS = 20


class BrokerError(RuntimeError):
    pass


class BrokerNotFound(BrokerError):
    pass


class BrokerExpired(BrokerError):
    pass


class BrokerDenied(BrokerError):
    pass


class BrokerActionError(BrokerError):
    pass


def _origin(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _selectors(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("selectors")
    if raw is None:
        raw = payload.get("selector")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise BrokerActionError("selector or selectors is required")
    values = [str(item) for item in raw if str(item).strip()]
    if not values:
        raise BrokerActionError("selector or selectors is required")
    if len(values) > MAX_SELECTORS:
        raise BrokerActionError(f"at most {MAX_SELECTORS} selectors are allowed")
    return values


@dataclass
class BrokerSession:
    token: str
    page: Any
    site_origin: str
    allowed_ops: set[str]
    expires_at: float
    step_key: str = ""
    log: Callable[[str, str], None] | None = None
    artifacts: Any = None
    authorize: Any = None
    lifecycle: Any = None
    actions: set = field(default_factory=set, repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def expired(self) -> bool:
        return time.monotonic() >= self.expires_at


class PluginBroker:
    def __init__(self, public_url: str, *, default_ttl_seconds: float = 120) -> None:
        self.public_url = public_url
        self.default_ttl_seconds = max(5, min(3600, default_ttl_seconds))
        self._sessions: dict[str, BrokerSession] = {}

    def register(
        self,
        page: Any,
        *,
        site_origin: str,
        step_key: str = "",
        allowed_ops: set[str] | None = None,
        ttl_seconds: float | None = None,
        log: Callable[[str, str], None] | None = None,
        artifacts: Any = None,
        approved_capabilities: list[str] | None = None,
        authorize: Any = None,
        lifecycle: Any = None,
    ) -> str:
        if not approved_capabilities or "browser_actions" not in approved_capabilities or authorize is None:
            raise BrokerDenied("browser_actions approval and authorization are required")
        token = secrets.token_urlsafe(32)
        ttl = self.default_ttl_seconds if ttl_seconds is None else ttl_seconds
        ttl = max(5, min(3600, float(ttl)))
        self._sessions[token] = BrokerSession(
            token=token,
            page=page,
            site_origin=site_origin.lower(),
            allowed_ops=set(DEFAULT_OPS if allowed_ops is None else allowed_ops) & DEFAULT_OPS,
            expires_at=time.monotonic() + ttl,
            step_key=step_key,
            log=log,
            artifacts=artifacts,
            authorize=authorize,
            lifecycle=lifecycle,
        )
        return token

    def release(self, token: str) -> None:
        session = self._sessions.pop(token, None)
        if session is not None:
            for task in session.actions:
                task.cancel()

    async def release_and_wait(self, token: str) -> None:
        session = self._sessions.get(token)
        self.release(token)
        if session is not None:
            await asyncio.gather(*session.actions, return_exceptions=True)

    def _session(self, token: str) -> BrokerSession:
        for key, session in list(self._sessions.items()):
            if session.expired():
                self.release(key)
        session = self._sessions.get(token)
        if session is None:
            raise BrokerNotFound("broker token is invalid or expired")
        if session.expired():
            self._sessions.pop(token, None)
            raise BrokerExpired("broker token is expired")
        return session

    async def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        token = str(payload.get("token") or "")
        session = self._session(token)
        async def watch():
            while True:
                await asyncio.sleep(0.5)
                if self._sessions.get(token) is not session:
                    raise BrokerDenied("broker grant revoked")
                try:
                    await session.authorize()
                except Exception:
                    self.release(token)
                    raise BrokerDenied("broker grant revoked") from None
        action = asyncio.create_task(self._execute(payload))
        session.actions.add(action)
        monitor = asyncio.create_task(watch())
        try:
            done, _ = await asyncio.wait({action, monitor}, return_when=asyncio.FIRST_COMPLETED)
            if monitor in done:
                await monitor
            result = await action
            if self._sessions.get(token) is not session or session.expired():
                raise BrokerDenied("broker grant revoked")
            return result
        finally:
            action.cancel()
            monitor.cancel()
            await asyncio.gather(action, monitor, return_exceptions=True)
            session.actions.discard(action)

    async def _execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        token = str(payload.get("token") or "")
        op = str(payload.get("op") or "")
        if not token or not op:
            raise BrokerActionError("token and op are required")
        session = self._session(token)
        if op not in DEFAULT_OPS or op not in session.allowed_ops:
            raise BrokerDenied(f"browser action is not allowed: {op}")
        try:
            timeout_ms = int(payload.get("timeout_ms") or 10000)
        except (TypeError, ValueError):
            timeout_ms = 10000
        timeout_ms = max(250, min(30000, timeout_ms))
        async with session.lock:
            if self._sessions.get(token) is not session:
                raise BrokerDenied("broker grant revoked")
            try:
                await session.authorize()
            except Exception:
                self.release(token)
                raise BrokerDenied("broker grant revoked") from None
            if session.expired():
                raise BrokerExpired("broker token is expired")
            if op != "goto" and _origin(session.page.url) != session.site_origin:
                raise BrokerDenied("browser action left the current site origin")
            if session.log:
                session.log("INFO", f"plugin broker action: {op}")
            if op == "goto":
                return await self._goto(session, payload, timeout_ms)
            if op == "click":
                return await self._click(session, payload, timeout_ms)
            if op == "fill":
                return await self._fill(session, payload, timeout_ms)
            if op == "read_text":
                return await self._read_text(session, payload, timeout_ms)
            if op == "wait_for":
                return await self._wait_for(session, payload, timeout_ms)
            if op == "screenshot":
                return await self._screenshot(session)
            if op == "query_selector":
                return await self._query_selector(session, payload)
        raise BrokerActionError(f"unsupported browser action: {op}")

    async def _locator(
        self,
        session: BrokerSession,
        selectors: list[str],
        *,
        timeout_ms: int,
        state: str,
    ) -> Any:
        deadline = time.monotonic() + timeout_ms / 1000
        last_error: Exception | None = None
        for selector in selectors:
            remaining = max(1, int((deadline - time.monotonic()) * 1000))
            locator = session.page.locator(selector).first
            try:
                await locator.wait_for(state=state, timeout=min(remaining, timeout_ms))
                return locator
            except Exception as exc:
                last_error = exc
                continue
        detail = f": {type(last_error).__name__}: {str(last_error)[:200]}" if last_error else ""
        raise BrokerActionError(f"no selector matched{detail}")

    async def _goto(
        self, session: BrokerSession, payload: dict[str, Any], timeout_ms: int
    ) -> dict[str, Any]:
        url = str(payload.get("url") or "")
        if not url:
            raise BrokerActionError("url is required for goto")
        if _origin(url) != session.site_origin:
            raise BrokerDenied("goto is limited to the current site origin")
        wait_until = str(payload.get("wait_until") or "domcontentloaded")
        if wait_until not in {"domcontentloaded", "load", "networkidle"}:
            wait_until = "domcontentloaded"
        await session.page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        return {"ok": True, "url": session.page.url}

    async def _click(
        self, session: BrokerSession, payload: dict[str, Any], timeout_ms: int
    ) -> dict[str, Any]:
        locator = await self._locator(
            session, _selectors(payload), timeout_ms=timeout_ms, state="visible"
        )
        context = getattr(session.page, "context", None)
        before = {id(page) for page in getattr(context, "pages", [])}
        await locator.click(timeout=timeout_ms)
        switched = False
        if context is not None:
            deadline = time.monotonic() + min(timeout_ms / 1000, 5.0)
            while time.monotonic() < deadline:
                for page in reversed(getattr(context, "pages", [])):
                    if id(page) in before:
                        continue
                    if session.lifecycle is not None and page not in session.lifecycle.pages:
                        continue
                    try:
                        if _origin(page.url) == session.site_origin:
                            session.page = page
                            if session.lifecycle is not None:
                                session.lifecycle.active = page
                                if session.lifecycle.job_key:
                                    session.lifecycle.lane.task_pages[session.lifecycle.job_key] = page
                            switched = True
                            break
                    except Exception:
                        continue
                if switched:
                    break
                await asyncio.sleep(0.1)
        return {"ok": True, "url": session.page.url, "switched_page": switched}

    async def _fill(
        self, session: BrokerSession, payload: dict[str, Any], timeout_ms: int
    ) -> dict[str, Any]:
        locator = await self._locator(
            session, _selectors(payload), timeout_ms=timeout_ms, state="visible"
        )
        if bool(payload.get("clear", True)):
            await locator.fill("", timeout=timeout_ms)
        await locator.fill(str(payload.get("value") or ""), timeout=timeout_ms)
        return {"ok": True}

    async def _read_text(
        self, session: BrokerSession, payload: dict[str, Any], timeout_ms: int
    ) -> dict[str, Any]:
        locator = await self._locator(
            session, _selectors(payload), timeout_ms=timeout_ms, state="attached"
        )
        text = await locator.inner_text(timeout=timeout_ms)
        return {"ok": True, "text": text}

    async def _wait_for(
        self, session: BrokerSession, payload: dict[str, Any], timeout_ms: int
    ) -> dict[str, Any]:
        state = str(payload.get("state") or "visible")
        if state not in {"attached", "detached", "visible", "hidden"}:
            state = "visible"
        await self._locator(
            session, _selectors(payload), timeout_ms=timeout_ms, state=state
        )
        return {"ok": True}

    async def _screenshot(self, session: BrokerSession) -> dict[str, Any]:
        image = await session.page.screenshot(type="png")
        saved = False
        if session.artifacts is not None:
            saved = session.artifacts.save_image(
                image,
                prefix="plugin",
                label="插件截图",
                step_key=session.step_key or None,
                kind="screenshot",
            )
        return {"ok": True, "size_bytes": len(image or b""), "saved": saved}

    async def _query_selector(
        self, session: BrokerSession, payload: dict[str, Any]
    ) -> dict[str, Any]:
        selectors = _selectors(payload)
        counts = []
        for selector in selectors:
            try:
                counts.append(
                    {"selector": selector, "count": await session.page.locator(selector).count()}
                )
            except Exception:
                counts.append({"selector": selector, "count": 0})
        return {"ok": True, "exists": any(item["count"] > 0 for item in counts), "matches": counts}
