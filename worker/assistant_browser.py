"""Internal, host-token-bound browsing of approved origins in a logged-in lane.

This is not a network sandbox: Chromium resolves DNS independently (rebinding is
not pinned), and an existing CDP profile's service workers cannot be disabled by
page routing. Popups can issue their first request before the popup event closes
them. Deploy with browser/proxy egress restrictions for a security boundary.
CDP validates redirect responses before Chromium follows them, preserving the
lane proxy. Local DNS must resolve even in proxy lanes; failures are fail-closed.
Manual login must use a separate user-controlled tab in this shared profile.
The assistant tab only permits POST during a host-reviewed non-read-only step;
outside execution, even human-submitted forms are blocked in this tab.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import math
import ipaddress
import json
import logging
import secrets
import socket
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Literal
from urllib.parse import unquote, urljoin

from fastapi import HTTPException
from pydantic import UUID4, BaseModel, ConfigDict, Field, field_validator

from worker.assistant_urls import MAX_DECODE_ROUNDS, parse_url, project_url
from worker.assistant_snapshot import (
    MAX_FRAME_BYTES, MAX_FRAMES, MAX_SNAPSHOTS, MAX_SNAPSHOT_BYTES,
    SNAPSHOT_SCRIPT, _snapshot, paginate,
)

TTL_SECONDS = 15 * 60
ACQUIRE_TIMEOUT = 2
OPEN_TIMEOUT = 110
MAX_TOMBSTONES = 1024
EXECUTE_TIMEOUT = 90
MAX_OPERATIONS = 20
READ_ONLY = frozenset({"flow.open", "action.navigate", "action.wait"})
MAX_SCREENSHOT_BYTES = 384 * 1024
SCREENSHOT_MASK = (
    'iframe, frame, input:not([type="button"]):not([type="submit"]):not([type="reset"])'
    ':not([type="checkbox"]):not([type="radio"]), textarea, select, '
    '[contenteditable]:not([contenteditable="false"])'
)
logger = logging.getLogger(__name__)
POLICY_CODES = frozenset({
    "browser_url_denied", "browser_dns_denied", "browser_redirect_denied",
    "browser_trust_required", "browser_credential_endpoint_not_approved",
})


def _exception_code(exc: BaseException, fallback: str) -> str:
    code = exc.detail.get("code") if isinstance(exc, HTTPException) and isinstance(exc.detail, dict) else None
    return code if code in POLICY_CODES | {"browser_snapshot_stale", "browser_frame_unreadable",
                                          "browser_frame_unavailable"} else fallback


def _success_evidence_result(value: Any) -> dict[str, Any] | None:
    """Admit only the worker's bounded, value-free post-timeout evidence contract."""
    schemas = {
        "root": {
            "selector_mode": ("visible",),
            "frame_scope": ("main_and_visible_same_origin",),
            "snapshot_consistency": ("sampled_after_timeout",),
            "captured_after_timeout": (True,),
            "waited_ms": 10_000_000, "elapsed_ms": 10_000_000,
            "configured_count": 10000, "selectors": "selector",
            "evidence_truncated": bool, "visible_after_timeout": bool,
            "reason": ("selector_missing", "matches_hidden", "read_incomplete",
                       "visible_after_timeout", "frame_scope_excluded"),
        },
        "selector": {
            "selector_index": 9999, "match_count": 2**53 - 1,
            "visible_count": 100, "checked_count": 100, "truncated": bool,
            "read_errors": 10, "frames": "frame",
        },
        "frame": {
            "frame_index": 9,
            "scope": ("main", "same_origin", "cross_origin", "hidden_frame", "unreadable_frame"),
            "match_count": 2**53 - 1, "visible_count": 100,
            "checked_count": 100, "truncated": bool,
            "error": ("invalid_selector", "read_timeout", "frame_detached", "frame_not_eligible"),
        },
    }

    def project(source, level):
        schema = schemas[level]
        required = schema.keys() - ({"error"} if level == "frame" else set())
        if type(source) is not dict or not required <= source.keys() <= schema.keys():
            raise ValueError()
        result = {}
        for key, rule in schema.items():
            if key not in source:
                continue
            item = source[key]
            if rule in ("selector", "frame"):
                if type(item) is not list or len(item) > 10:
                    raise ValueError()
                result[key] = [project(child, rule) for child in item]
                continue
            if item is None and level == "frame" and key in {"match_count", "visible_count"}:
                pass
            elif rule is bool:
                if type(item) is not bool:
                    raise ValueError()
            elif type(rule) is int:
                if type(item) is not int or not 0 <= item <= rule:
                    raise ValueError()
            elif not any(type(item) is type(option) and item == option for option in rule):
                raise ValueError()
            result[key] = item
        return result

    try:
        result = project(value, "root")
        if len(json.dumps(result, allow_nan=False).encode("utf-8")) <= 16000:
            return result
    except (TypeError, ValueError, RecursionError):
        pass
    return None


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    token: str = Field(min_length=1, max_length=512, repr=False)


class OpenRequest(TokenRequest):
    url: str = Field(max_length=4096)
    lane_key: Literal["interaction", "proxy_interaction"] = "interaction"
    session_id: UUID4 | None = None
    trusted_origins: list[str] = Field(default_factory=list, max_length=20, strict=True)

    @field_validator("trusted_origins")
    @classmethod
    def normalized_origins(cls, value):
        try:
            return sorted(_trusted_origins(value))
        except HTTPException:
            raise ValueError("Invalid trusted origins") from None


class NavigateRequest(TokenRequest):
    url: str = Field(max_length=4096)


class ObserveRequest(TokenRequest):
    offset: int = Field(default=0, ge=0, strict=True)
    limit: int = Field(default=100, ge=1, le=100, strict=True)
    frame_id: str | None = Field(default=None, min_length=1, max_length=256)


class InspectRequest(TokenRequest):
    snapshot_id: UUID4
    node_id: str = Field(min_length=1, max_length=64)
    offset: int = Field(default=0, ge=0, strict=True)
    limit: int = Field(default=100, ge=1, le=100, strict=True)


class CheckSelectorRequest(TokenRequest):
    selector: str = Field(min_length=1, max_length=2048)
    frame_id: str | None = Field(default=None, min_length=1, max_length=256)


def _error(status: int, code: str, *, lane_key: str = "none", stage: str = "request") -> HTTPException:
    logger.warning("Assistant browser error code=%s lane=%s stage=%s", code, lane_key, stage)
    return HTTPException(status, {"code": code})


def _denied(reason: str = "browser_url_denied") -> HTTPException:
    return _error(403, reason, stage="policy")


def _url(url: str, *, document: bool = True) -> tuple[str, str, str]:
    """Validate a control URL without applying observation redaction."""
    try:
        parsed = parse_url(url)
        decoded = url
        for _ in range(MAX_DECODE_ROUNDS + 1):
            if "[redacted]" in decoded.lower():
                raise ValueError()
            previous = decoded
            decoded = unquote(decoded)
            if decoded == previous:
                break
        return parsed
    except ValueError:
        raise _denied() from None


def _trusted_origins(values: list[str]) -> set[str]:
    if not isinstance(values, list) or len(values) > 20:
        raise _denied()
    origins = set()
    for value in values:
        _, origin, _ = _url(value)
        if value != origin:
            raise _denied()
        origins.add(origin)
    return origins


def _project_url(url: str, known: set) -> dict[str, Any]:
    from worker.assistant_step import secret_in_url

    projected = project_url(url)
    if secret_in_url(url, known):
        return {"url": "[redacted]", "url_redacted": True,
                "redacted_parameters": ["known_credential"]}
    return projected


def _blocked_warning(failure: dict, known: set) -> dict:
    from worker.assistant_step import secret_in_url

    code = failure.get("code")
    warning = {"code": code if isinstance(code, str) and code in POLICY_CODES else "browser_url_denied",
               "source": "blocked_request"}
    if code != "browser_trust_required":
        return warning
    parameters = []
    for prefix in ("source", "target"):
        value = failure.get(prefix + "_url")
        if not isinstance(value, str) or not 1 <= len(value) <= 4096:
            continue
        try:
            view = _project_url(value, known)
        except ValueError:
            if value != "[redacted]":
                continue
            view = {"url": value, "url_redacted": True, "redacted_parameters": ["known_credential"]}
        warning[prefix + "_url"] = view["url"]
        warning[prefix + "_url_redacted"] = view["url_redacted"] or failure.get(prefix + "_url_redacted") is True
        parameters.extend(view["redacted_parameters"])
        if view["url"] != "[redacted]":
            origin = parse_url(view["url"])[1]
            if not secret_in_url(origin, known):
                warning[prefix + "_origin"] = origin
    names = failure.get("redacted_parameters")
    if isinstance(names, list):
        parameters.extend(names[:32])
    warning["redacted_parameters"] = list(dict.fromkeys(
        name for name in parameters if isinstance(name, str) and 1 <= len(name) <= 128
        and not secret_in_url(name, known)))[:32]
    # Leave room for frame metadata and at least one full-sized DOM node.
    if len(json.dumps(warning, ensure_ascii=True).encode()) > 8 * 1024:
        return {"code": warning["code"], "source": "blocked_request", "details_truncated": True,
                "reason": "browser_warning_details_limit"}
    return warning


def _redact_known(value: Any, known: set) -> Any:
    from worker.assistant_step import secret_in_url

    if isinstance(value, str):
        return "[redacted]" if secret_in_url(value, known) else value
    if isinstance(value, list):
        return [_redact_known(item, known) for item in value]
    if isinstance(value, dict):
        result = {key: _redact_known(item, known) for key, item in value.items()}
        for key, flag, parameters in (("url", "url_redacted", "redacted_parameters"),
                                      ("href", "href_redacted", "href_redacted_parameters")):
            if isinstance(value.get(key), str) and secret_in_url(value[key], known):
                result[flag] = True
                result[parameters] = ["known_credential"]
        return result
    return value


class PublicDNS:
    def __init__(self) -> None:
        self.cache: dict[str, float] = {}

    async def check(self, host: str) -> None:
        if host == "localhost" or host.endswith(".localhost"):
            raise _denied("browser_dns_denied")
        now = time.monotonic()
        if self.cache.get(host, 0) > now:
            return
        try:
            try:
                addresses = [ipaddress.ip_address(host)]
            except ValueError:
                answers = await asyncio.wait_for(
                    asyncio.get_running_loop().getaddrinfo(
                        host, None, type=socket.SOCK_STREAM
                    ), 2,
                )
                addresses = [ipaddress.ip_address(answer[4][0]) for answer in answers]
            if not addresses or any(
                not address.is_global or address.is_multicast or address.is_reserved
                or address.is_unspecified or address.is_loopback or address.is_link_local
                or (isinstance(address, ipaddress.IPv6Address)
                    and (address.sixtofour is not None or address.teredo is not None))
                for address in addresses
            ):
                raise _denied("browser_dns_denied")
        except (OSError, ValueError, TimeoutError):
            raise _denied("browser_dns_denied") from None
        if len(self.cache) >= 128:
            self.cache.clear()
        self.cache[host] = now + 2


@dataclass
class BrowserSession:
    session_id: str
    lane_key: str
    token_digest: bytes
    origin: str
    expires_at: float
    deadline: float
    lane: Any = None
    initial_url: str = ""
    approved_origins: set[str] = field(default_factory=set)
    lease_id: str | None = None
    acquired: bool = False
    page: Any = None
    cdp: Any = None
    dns: PublicDNS = field(default_factory=PublicDNS)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tasks: set[asyncio.Task] = field(default_factory=set)
    provisioning: set[asyncio.Task] = field(default_factory=set)
    closed: bool = False
    ready: bool = False
    quarantined: bool = False
    opening: asyncio.Task | None = None
    cleanup: asyncio.Task | None = None
    pages: list[Any] = field(default_factory=list)
    redirects: dict[str, int] = field(default_factory=dict)
    frame_id: str | None = None
    failure: dict[str, Any] | None = None
    executing: bool = False
    writes_authorized: bool = False
    credential_execution: bool = False
    blocked_requests: list[dict[str, Any]] = field(default_factory=list, repr=False)
    uncertain: bool = False
    same_origin_only: bool = False
    execution_task: asyncio.Task | None = None
    operations: dict[str, tuple[bytes, dict[str, Any] | None]] = field(default_factory=dict)
    operation_steps: dict[str, bytes] = field(default_factory=dict, repr=False)
    consumed_permits: set[str] = field(default_factory=set, repr=False)
    unresolved_operations: set[str] = field(default_factory=set)
    secret_fingerprints: set[tuple[int, bytes]] = field(default_factory=set, repr=False)
    snapshots: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    latest_snapshots: dict[str, str] = field(default_factory=dict, repr=False)
    document_versions: dict[str, tuple[str, str]] = field(default_factory=dict, repr=False)

    def view(self) -> dict[str, Any]:
        result = {"session_id": self.session_id, "lane_key": self.lane_key,
                  "expires_at": datetime.fromtimestamp(self.expires_at, timezone.utc).isoformat()}
        if self.ready and self.page is not None:
            result.update(_project_url(self.page.url, self.secret_fingerprints))
        return result


class AssistantBrowser:
    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self.sessions: dict[str, BrowserSession] = {}
        self._reaper: asyncio.Task | None = None
        self._stopping = False
        self.tombstones: dict[str, tuple[bytes, float]] = {}

    async def start(self) -> None:
        self._reaper = asyncio.create_task(self._reap(), name="assistant-browser-reaper")

    async def stop(self) -> None:
        self._stopping = True
        if self._reaper:
            self._reaper.cancel()
            await asyncio.gather(self._reaper, return_exceptions=True)
            self._reaper = None
        await asyncio.gather(*(self._dispose(s) for s in list(self.sessions.values())))

    async def _reap(self) -> None:
        while True:
            await asyncio.sleep(5)
            self._prune_tombstones()
            for session in list(self.sessions.values()):
                if session.closed or session.deadline <= time.monotonic():
                    await self._dispose(session)

    async def _dispose(self, session: BrowserSession) -> None:
        session.closed = True
        session.snapshots.clear()
        session.latest_snapshots.clear()
        session.document_versions.clear()
        self._remember(session.session_id, session.token_digest)
        if session.cleanup is None or session.cleanup.done():
            session.cleanup = asyncio.create_task(self._cleanup(session))
        cleanup = session.cleanup
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise

    async def _cleanup(self, session: BrowserSession) -> None:
        if session.execution_task is not None and not session.execution_task.done():
            # Let a just-published coroutine enter its cancellation/finally scope.
            await asyncio.sleep(0)
            session.execution_task.cancel()
            await asyncio.gather(session.execution_task, return_exceptions=True)
        if session.opening is not None and not session.opening.done():
            session.opening.cancel()
            await asyncio.gather(session.opening, return_exceptions=True)
        # Shield resource-producing operations so cancellation cannot lose a
        # lease or a page between remote creation and assignment to its owner.
        if session.provisioning:
            _, pending = await asyncio.wait(session.provisioning, timeout=5)
            if pending:
                session.quarantined = True
                return
            await asyncio.gather(*list(session.provisioning), return_exceptions=True)
            session.provisioning.clear()
        async with session.lock:
            while session.tasks:
                await asyncio.gather(*list(session.tasks), return_exceptions=True)
            # Never return a lane to other automation while an owned target may
            # still be alive. Retain failed targets and admission for the reaper.
            for page in list(session.pages):
                if await self._close_target(session, page):
                    session.pages.remove(page)
            if session.pages:
                session.quarantined = True
                return
            if session.lease_id is not None:
                try:
                    await asyncio.wait_for(
                        self.manager.release_lane(session.lane_key, session.lease_id), 5,
                    )
                except BaseException:
                    session.quarantined = True
                    return
                session.lease_id = None
            if session.acquired:
                await session.lane.execution_semaphore.__aexit__(None, None, None)
                session.acquired = False
            if session.cdp is not None:
                try:
                    await asyncio.wait_for(session.cdp.detach(), 5)
                except Exception:
                    pass
            session.quarantined = False
            self.sessions.pop(session.session_id, None)

    async def _close_target(self, session: BrowserSession, page: Any) -> bool:
        try:
            await asyncio.wait_for(page.close(), 3)
            return True
        except BaseException:
            pass
        cdp = None
        try:
            cdp = session.cdp if page is session.page and session.cdp is not None else await asyncio.wait_for(
                session.lane.context.new_cdp_session(page), 3,
            )
            target = await asyncio.wait_for(cdp.send("Target.getTargetInfo"), 3)
            result = await asyncio.wait_for(cdp.send("Target.closeTarget", {
                "targetId": target["targetInfo"]["targetId"],
            }), 3)
            return result.get("success") is True
        except BaseException:
            return False
        finally:
            if cdp is not None and cdp is not session.cdp:
                try:
                    await asyncio.wait_for(cdp.detach(), 3)
                except Exception:
                    pass

    def _popup(self, session: BrowserSession, page: Any) -> None:
        session.pages.append(page)

        async def close() -> None:
            if await self._close_target(session, page):
                session.pages.remove(page)
            else:
                session.closed = session.quarantined = True

        task = asyncio.create_task(close())
        session.tasks.add(task)
        task.add_done_callback(session.tasks.discard)

    async def _route(self, session: BrowserSession, route: Any) -> None:
        document = False
        try:
            request = route.request
            document = request.resource_type == "document" or request.is_navigation_request()
            host, origin, _ = _url(request.url, document=False)
            self._guard_secret_url(session, request.url, request.method)
            methods = {"GET", "HEAD", "POST"} if session.writes_authorized else {"GET", "HEAD"}
            if session.closed or request.method not in methods:
                raise _denied()
            if (session.executing or session.same_origin_only) and origin != session.origin:
                raise _denied()
            if document:
                if origin not in session.approved_origins:
                    if request.frame == session.page.main_frame:
                        raise await self._navigation_error(session, request.url)
                    raise _denied("browser_trust_required")
            await session.dns.check(host)
            # Keep the browser's lane proxy and cookies. APIRequest/route.fetch
            # on a CDP context does not inherit the remote browser's proxy.
            # The CDP response guard below prevents un-routed redirect hops.
            await route.continue_()
        except Exception as exc:
            self._record_blocked(session, request.url, request.method, exc)
            if (document and request.frame == session.page.main_frame and isinstance(exc, HTTPException)
                    and isinstance(exc.detail, dict)):
                session.failure = exc.detail
                _error(403, session.failure["code"], lane_key=session.lane_key, stage="route")
            try:
                await route.abort("blockedbyclient")
            except Exception:
                pass

    def _guard_secret_url(self, session: BrowserSession, url: str, method: str) -> None:
        from worker.assistant_step import secret_in_url

        if secret_in_url(url, session.secret_fingerprints):
            session.failure = {"code": "browser_url_denied"}
            raise _denied()
        if (session.credential_execution and method == "POST" and
                _url(url, document=False)[1] != session.origin):
            session.failure = {"code": "browser_redirect_denied"}
            raise _denied(session.failure["code"])

    def _record_blocked(self, session: BrowserSession, url: str, method: str,
                        exc: BaseException) -> None:
        code = _exception_code(exc, "browser_url_denied")
        if session.executing:
            session.failure = {"code": code}
        if len(session.blocked_requests) >= 5:
            return
        try:
            view = _project_url(url, session.secret_fingerprints)
            if len(json.dumps(view, ensure_ascii=True).encode()) > 2048:
                raise ValueError()
        except ValueError:
            view = {"url": "[redacted]", "url_redacted": True, "redacted_parameters": []}
        session.blocked_requests.append({
            "code": code,
            "method": method if method in {"GET", "HEAD", "POST", "PUT", "DELETE", "PATCH",
                                           "OPTIONS", "CONNECT", "TRACE"} else "OTHER",
            **view,
        })

    async def _navigation_error(self, session: BrowserSession, target: str,
                                source: str | None = None) -> HTTPException:
        """Only public, validated candidates may be offered for host confirmation."""
        source = source or (session.page.url if session.page is not None else session.initial_url)
        if source == "about:blank":
            source = session.initial_url
        self._guard_secret_url(session, source, "GET")
        self._guard_secret_url(session, target, "GET")
        host, _, _ = _url(target)
        _url(source)
        await session.dns.check(host)
        if session.executing or session.credential_execution or session.same_origin_only:
            return _denied("browser_redirect_denied")
        try:
            source_view = _project_url(source, session.secret_fingerprints)
            target_view = _project_url(target, session.secret_fingerprints)
        except ValueError:
            return _denied()
        failure = {
            "code": "browser_trust_required",
            "source_url": source_view["url"], "target_url": target_view["url"],
            "source_url_redacted": source_view["url_redacted"],
            "target_url_redacted": target_view["url_redacted"],
            "redacted_parameters": list(dict.fromkeys(
                source_view["redacted_parameters"] + target_view["redacted_parameters"])),
        }
        session.failure = failure
        error = _error(403, failure["code"], lane_key=session.lane_key, stage="policy")
        error.detail = failure
        return error

    async def _response(self, session: BrowserSession, event: dict[str, Any]) -> None:
        blocked_url = event.get("request", {}).get("url", "")
        blocked_method = event.get("request", {}).get("method", "")
        try:
            try:
                if session.closed:
                    raise _denied("browser_redirect_denied")
                status = event.get("responseStatusCode", 0)
                key = event.get("networkId") or event["requestId"]
                previous = event.get("redirectedRequestId", "")
                if status in {301, 302, 303, 307, 308}:
                    request = event["request"]
                    self._guard_secret_url(session, request["url"], request["method"])
                    document = event.get("resourceType") == "Document"
                    _, source_origin, _ = _url(request["url"], document=False)
                    locations = [header["value"] for header in event.get("responseHeaders", [])
                                 if header["name"].lower() == "location"]
                    if len(locations) != 1 or any(
                         ord(c) < 32 or 127 <= ord(c) <= 159 or c == "\\"
                         for c in locations[0] + unquote(locations[0])
                    ):
                        raise _denied("browser_redirect_denied")
                    target = urljoin(request["url"], locations[0])
                    method = request["method"]
                    if status == 303 or (status in {301, 302} and method == "POST"):
                        method = "GET"
                    blocked_url, blocked_method = target, method
                    self._guard_secret_url(session, target, method)
                    host, origin, _ = _url(target, document=False)
                    if (session.executing or session.same_origin_only) and (source_origin != session.origin or origin != session.origin):
                        raise _denied("browser_redirect_denied")
                    methods = {"GET", "HEAD", "POST"} if session.writes_authorized else {"GET", "HEAD"}
                    if request["method"] not in methods:
                        raise _denied()
                    if document:
                        if source_origin not in session.approved_origins:
                            raise _denied("browser_redirect_denied")
                        if origin not in session.approved_origins:
                            if event.get("frameId") == session.frame_id:
                                raise await self._navigation_error(session, target, request["url"])
                            raise _denied("browser_trust_required")
                    count = max(session.redirects.get(key, 0), session.redirects.pop(previous, 0))
                    if count >= 8 or len(session.redirects) >= 256:
                        raise _denied("browser_redirect_denied")
                    await session.dns.check(host)
                    session.redirects[key] = count + 1
                else:
                    session.redirects.pop(key, None)
                    session.redirects.pop(previous, None)
            except Exception as exc:
                self._record_blocked(session, blocked_url, blocked_method, exc)
                code = "browser_redirect_denied"
                if (isinstance(exc, HTTPException) and isinstance(exc.detail, dict)
                        and exc.detail.get("code") in POLICY_CODES):
                    code = exc.detail["code"]
                    session.failure = exc.detail if code == "browser_trust_required" else {"code": code}
                    _error(403, code, lane_key=session.lane_key, stage="redirect")
                else:
                    session.failure = _error(403, code, lane_key=session.lane_key, stage="redirect").detail
                await session.cdp.send("Fetch.failRequest", {
                    "requestId": event["requestId"], "errorReason": "BlockedByClient",
                })
                return
            await session.cdp.send("Fetch.continueResponse", {"requestId": event["requestId"]})
        except Exception:
            session.closed = session.quarantined = True
            await self._dispose(session)

    def _prune_tombstones(self) -> None:
        now = time.monotonic()
        for session_id, (_, deadline) in list(self.tombstones.items()):
            if deadline <= now:
                del self.tombstones[session_id]

    def _remember(self, session_id: str, digest: bytes) -> None:
        self._prune_tombstones()
        if session_id not in self.tombstones and len(self.tombstones) >= MAX_TOMBSTONES:
            raise _error(503, "browser_lane_unavailable")
        self.tombstones[session_id] = (digest, time.monotonic() + TTL_SECONDS)

    def _id(self, session_id: str) -> str:
        try:
            value = uuid.UUID(str(session_id))
            if value.version != 4:
                raise ValueError
            return str(value)
        except ValueError:
            raise HTTPException(404, "Browser session unavailable") from None

    async def open(self, url: str, lane_key: str, token: str,
                   session_id: str | None = None,
                   trusted_origins: list[str] | None = None) -> dict[str, Any]:
        if lane_key not in {"interaction", "proxy_interaction"} or not token or len(token) > 512:
            raise _denied()
        if self._stopping:
            raise _error(503, "browser_lane_unavailable", lane_key=lane_key, stage="open")
        approved = _trusted_origins(trusted_origins if trusted_origins is not None else [])
        approved.add(_url(url)[1])
        session_id = self._id(session_id) if session_id is not None else str(uuid.uuid4())
        digest = hashlib.sha256(token.encode()).digest()
        self._prune_tombstones()
        tombstone = self.tombstones.get(session_id)
        if tombstone is not None:
            if not secrets.compare_digest(tombstone[0], digest):
                raise HTTPException(404, "Browser session unavailable")
            raise _error(410, "browser_closed", lane_key=lane_key, stage="open")
        session = self.sessions.get(session_id)
        if session is not None:
            if not secrets.compare_digest(session.token_digest, digest):
                raise HTTPException(404, "Browser session unavailable")
            if session.closed:
                raise _error(410, "browser_closed", lane_key=lane_key, stage="open")
            if session.deadline <= time.monotonic():
                await self._dispose(session)
                raise _error(410, "browser_session_expired", lane_key=lane_key, stage="open")
            if (session.initial_url != url or session.lane_key != lane_key
                    or session.approved_origins != approved):
                raise HTTPException(409, "Browser session conflict")
            if session.ready:
                await self._current(session)
                return session.view()
        else:
            if len(self.sessions) + len(self.tombstones) >= MAX_TOMBSTONES:
                raise _error(503, "browser_lane_unavailable", lane_key=lane_key, stage="open")
            session = BrowserSession(
                session_id, lane_key, digest, "", time.time() + TTL_SECONDS,
                time.monotonic() + TTL_SECONDS, initial_url=url, approved_origins=approved,
            )
            # Publish ownership before DNS, lease, page creation, or any await.
            self.sessions[session_id] = session
            session.opening = asyncio.create_task(self._initialize(session))
        deadline = session.deadline - TTL_SECONDS + OPEN_TIMEOUT
        try:
            async with asyncio.timeout_at(deadline):
                await asyncio.shield(session.opening)
            if session.closed:
                raise _error(410, "browser_closed", lane_key=lane_key, stage="open")
            return session.view()
        except BaseException as exc:
            # Retain cleanup as a session-owned task if the absolute open budget
            # expires; the host's known ID remains closable and the reaper retries.
            disposal = asyncio.create_task(self._dispose(session))
            try:
                async with asyncio.timeout_at(deadline):
                    await asyncio.shield(disposal)
            except (TimeoutError, asyncio.CancelledError):
                pass
            if isinstance(exc, TimeoutError):
                raise _error(504, "browser_open_timeout", lane_key=lane_key, stage="open") from None
            if isinstance(exc, (HTTPException, asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise _error(502, "browser_lane_unavailable", lane_key=lane_key, stage="open") from None

    async def _initialize(self, session: BrowserSession) -> None:
        host, session.origin, _ = _url(session.initial_url)
        await session.dns.check(host)
        for origin in session.approved_origins:
            await session.dns.check(_url(origin)[0])
        try:
            session.lane = self.manager.get(session.lane_key)
        except Exception:
            raise _error(409, "browser_lane_unavailable", lane_key=session.lane_key, stage="lookup") from None
        lane = session.lane

        async def lease() -> None:
            try:
                session.lease_id, _ = await self.manager.lease_lane(session.lane_key)
            except Exception:
                raise _error(409, "browser_lane_unavailable", lane_key=session.lane_key, stage="lease") from None

        task = asyncio.create_task(lease())
        session.provisioning.add(task)
        await asyncio.shield(task)
        session.provisioning.discard(task)
        try:
            async def acquire() -> None:
                await lane.execution_semaphore.__aenter__()
                session.acquired = True

            await asyncio.wait_for(acquire(), ACQUIRE_TIMEOUT)
        except TimeoutError:
            raise _error(409, "browser_lane_busy", lane_key=session.lane_key, stage="acquire") from None
        if lane.context is None:
            raise _error(409, "browser_lane_unavailable", lane_key=session.lane_key, stage="context")

        async def new_page() -> None:
            session.page = await lane.context.new_page()
            session.pages.append(session.page)

        task = asyncio.create_task(new_page())
        session.provisioning.add(task)
        await asyncio.shield(task)
        session.provisioning.discard(task)
        page = session.page
        page.on("popup", lambda child: self._popup(session, child))
        session.cdp = await lane.context.new_cdp_session(page)
        tree = await session.cdp.send("Page.getFrameTree")
        session.frame_id = tree["frameTree"]["frame"]["id"]
        session.cdp.on("Fetch.requestPaused", lambda event: self._response(session, event))
        await session.cdp.send("Fetch.enable", {
            "patterns": [{"urlPattern": "*", "requestStage": "Response"}],
        })
        await page.route("**/*", lambda route: self._route(session, route))
        await page.route_web_socket("**/*", lambda ws: ws.close())
        await page.bring_to_front()
        try:
            await page.goto(session.initial_url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            if session.failure:
                raise HTTPException(403, session.failure) from None
            raise _error(502, "browser_navigation_failed", lane_key=session.lane_key, stage="open") from None
        if session.failure:
            raise HTTPException(403, session.failure)
        await self._current(session)
        session.ready = True

    async def _session(self, session_id: str, token: str) -> BrowserSession:
        session_id = self._id(session_id)
        session = self.sessions.get(session_id)
        digest = hashlib.sha256(token.encode()).digest()
        if session is None or not secrets.compare_digest(session.token_digest, digest):
            raise HTTPException(404, "Browser session unavailable")
        if session.deadline <= time.monotonic():
            await self._dispose(session)
            raise _error(410, "browser_session_expired", lane_key=session.lane_key, stage="session")
        if session.closed or not session.ready:
            raise HTTPException(409, "Browser session unavailable")
        return session

    async def _current(self, session: BrowserSession) -> str:
        self._guard_secret_url(session, session.page.url, "GET")
        host, origin, _ = _url(session.page.url, document=False)
        if (session.executing or session.same_origin_only) and origin != session.origin:
            raise _denied("browser_redirect_denied")
        if origin not in session.approved_origins:
            raise await self._navigation_error(session, session.page.url, session.initial_url)
        await session.dns.check(host)
        return session.page.url

    async def observe(self, session_id: str, token: str, offset: int = 0,
                      limit: int = 100, frame_id: str | None = None) -> dict[str, Any]:
        session = await self._session(session_id, token)
        async with session.lock:
            try:
                if session.closed:
                    raise HTTPException(404, "Browser session unavailable")
                return await self._collect(session, offset, limit, frame_id)
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(502, "Browser observation unavailable") from None

    async def _frames(self, session: BrowserSession) -> tuple[list[dict], dict[str, dict], bool]:
        """Only CDP-discovered frame IDs are accepted as observation targets."""
        tree = await session.cdp.send("Page.getFrameTree")
        root = tree["frameTree"]
        registry, frames = {}, []
        stack = [(root, None)]
        size = 0
        metadata_truncated = False
        while stack and len(frames) < MAX_FRAMES:
            branch, parent_id = stack.pop()
            frame = dict(branch["frame"])
            frame_id = frame["id"]
            url = frame.get("unreachableUrl") or frame.get("url", "")
            if frame.get("urlFragment") and "#" not in url:
                url += frame["urlFragment"]
            frame["url"] = url
            item = {"frame_id": frame_id, "readable": False}
            if parent_id is not None:
                item["parent_frame_id"] = parent_id
            try:
                item.update(_project_url(url, session.secret_fingerprints))
                host, origin, _ = _url(url, document=False)
                await session.dns.check(host)
                if origin not in session.approved_origins:
                    item["reason"] = "browser_trust_required"
                elif (session.same_origin_only or session.executing) and origin != session.origin:
                    item["reason"] = "browser_redirect_denied"
                elif frame.get("unreachableUrl"):
                    item["reason"] = "browser_frame_unavailable"
                else:
                    try:
                        await session.cdp.send("Page.createIsolatedWorld", {
                            "frameId": frame_id, "worldName": "siteflow-assistant-snapshot",
                        })
                        item["readable"] = True
                    except Exception:
                        item["reason"] = "browser_frame_unavailable"
            except (HTTPException, ValueError) as exc:
                item.setdefault("url", "[redacted]")
                item.setdefault("url_redacted", True)
                item.setdefault("redacted_parameters", [])
                item["reason"] = (exc.detail.get("code", "browser_url_denied")
                                  if isinstance(exc, HTTPException) and isinstance(exc.detail, dict)
                                  else "browser_url_denied")
            size += len(json.dumps(item, ensure_ascii=True).encode())
            if size > MAX_FRAME_BYTES:
                stack.append((branch, parent_id))
                break
            registry[frame_id] = frame
            frames.append(item)
            # Bound pending metadata even for documents with thousands of frames.
            children = branch.get("childFrames", [])
            remaining = MAX_FRAMES - len(frames) - len(stack)
            if len(children) > remaining:
                metadata_truncated = True
            stack.extend((child, frame_id) for child in reversed(children[:max(0, remaining)]))
        versions = {key: (frame.get("loaderId", ""), frame.get("url", ""))
                    for key, frame in registry.items()}
        root_id = root["frame"]["id"]
        if (session.document_versions and
                session.document_versions.get(root_id) != versions.get(root_id)):
            session.snapshots.clear()
            session.latest_snapshots.clear()
        for key, snapshot in list(session.snapshots.items()):
            if versions.get(snapshot["frame_id"]) != snapshot["version"]:
                del session.snapshots[key]
                session.latest_snapshots.pop(snapshot["frame_id"], None)
        session.latest_snapshots = {frame_id: key for frame_id, key in session.latest_snapshots.items()
                                    if key in session.snapshots}
        session.document_versions = versions
        return frames, registry, bool(stack) or metadata_truncated

    def _frame(self, frames: list[dict], frame_id: str | None) -> dict:
        frame = (next((item for item in frames if item["frame_id"] == frame_id), None)
                 if frame_id else next(iter(frames), None))
        if frame is None:
            raise HTTPException(404, {"code": "browser_frame_unavailable"})
        if not frame["readable"]:
            raise HTTPException(403, {"code": "browser_frame_unreadable"})
        return frame

    def _snapshot_page(self, session, snapshot, frames, frames_truncated, nodes, offset, limit, warning=None):
        metadata = {**session.view(), "snapshot_id": snapshot["snapshot_id"],
                    "frame_id": snapshot["frame_id"], "frames": frames}
        metadata["frames_truncated"] = frames_truncated
        if warning is not None:
            metadata["warnings"] = [warning]
        # Two execution snapshots plus their envelope must fit the host's 64 KiB
        # worker-response budget. Standalone observations retain the 32 KiB cap.
        budget = 24 * 1024 if session.executing else MAX_SNAPSHOT_BYTES
        projected = self._redact_snapshot({**metadata, "elements": nodes}, session, "elements")
        metadata = {key: value for key, value in projected.items() if key != "elements"}
        try:
            return paginate(metadata, projected["elements"], offset, limit,
                            snapshot["truncated"] or frames_truncated, budget)
        except HTTPException as exc:
            if (warning is None or len(warning) <= 2 or not isinstance(exc.detail, dict)
                    or exc.detail.get("code") != "browser_snapshot_limit"):
                raise
            metadata["warnings"] = [{"code": warning["code"], "source": "blocked_request",
                                     "details_truncated": True, "reason": "browser_warning_details_limit"}]
            return paginate(metadata, projected["elements"], offset, limit,
                            snapshot["truncated"] or frames_truncated, budget)

    def _redact_snapshot(self, result, session, key):
        redacted = _redact_known(result, session.secret_fingerprints)
        for original, node in zip(result[key], redacted[key]):
            if original.get("selector") != node.get("selector"):
                for field in ("selector", "selector_matches", "selector_source"):
                    node.pop(field, None)
        return redacted

    async def _collect(self, session: BrowserSession, offset: int = 0,
                       limit: int = 100, frame_id: str | None = None) -> dict[str, Any]:
        pending_failure = session.failure
        await self._current(session)
        frames, registry, frames_truncated = await self._frames(session)
        frame = self._frame(frames, frame_id)
        frame_id = frame["frame_id"]
        if offset:
            snapshot = session.snapshots.get(session.latest_snapshots.get(frame_id))
            if snapshot is None:
                raise _error(409, "browser_snapshot_stale")
        else:
            version = session.document_versions[frame_id]
            raw = await asyncio.wait_for(self._isolated_snapshot(session, registry[frame_id]), 10)
            snapshot = {**_snapshot(raw, frame_id, session.secret_fingerprints), "snapshot_id": str(uuid.uuid4()),
                        "frame_id": frame_id, "version": version}
            snapshot = {**snapshot, "elements": self._redact_snapshot(
                {"elements": snapshot["elements"]}, session, "elements")["elements"]}
            await self._current(session)
            frames, _, frames_truncated = await self._frames(session)
            if version != session.document_versions.get(frame_id):
                raise _error(409, "browser_snapshot_stale")
            self._frame(frames, frame_id)
            while len(session.snapshots) >= MAX_SNAPSHOTS:
                evicted = session.snapshots.pop(next(iter(session.snapshots)))
                if session.latest_snapshots.get(evicted["frame_id"]) == evicted["snapshot_id"]:
                    session.latest_snapshots.pop(evicted["frame_id"], None)
            session.snapshots[snapshot["snapshot_id"]] = snapshot
            session.latest_snapshots[frame_id] = snapshot["snapshot_id"]
        warning = (_blocked_warning(pending_failure, session.secret_fingerprints)
                   if pending_failure and not session.executing else None)
        result = self._snapshot_page(session, snapshot, frames, frames_truncated,
                                     snapshot["elements"], offset, limit, warning)
        if warning is not None and session.failure is pending_failure:
            session.failure = None
        return result

    async def inspect(self, session_id: str, token: str, snapshot_id: str,
                      node_id: str, offset: int = 0, limit: int = 100) -> dict[str, Any]:
        """Return an immutable captured subtree, including the requested root."""
        session = await self._session(session_id, token)
        async with session.lock:
            try:
                if session.closed:
                    raise HTTPException(404, "Browser session unavailable")
                await self._current(session)
                frames, _, frames_truncated = await self._frames(session)
                snapshot = session.snapshots.get(snapshot_id)
                if snapshot is None:
                    raise _error(409, "browser_snapshot_stale")
                self._frame(frames, snapshot["frame_id"])
                descendants, nodes = {node_id}, []
                for node in snapshot["elements"]:
                    if node["node_id"] == node_id or node["parent_id"] in descendants:
                        nodes.append(node)
                        descendants.add(node["node_id"])
                if not nodes:
                    raise _error(409, "browser_snapshot_stale")
                return self._snapshot_page(session, snapshot, frames, frames_truncated, nodes, offset, limit)
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(502, "Browser observation unavailable") from None

    async def check_selector(self, session_id: str, token: str, selector: str,
                             frame_id: str | None = None) -> dict[str, Any]:
        session = await self._session(session_id, token)
        async with session.lock:
            try:
                if session.closed:
                    raise HTTPException(404, "Browser session unavailable")
                await self._current(session)
                frames, registry, _ = await self._frames(session)
                frame_id = self._frame(frames, frame_id)["frame_id"]
                version = session.document_versions[frame_id]
                raw = await asyncio.wait_for(self._isolated_snapshot(
                    session, registry[frame_id], {"mode": "check", "selector": selector}), 10)
                if not isinstance(raw, dict) or raw.get("invalid_selector"):
                    raise HTTPException(422, {"code": "browser_selector_invalid"})
                await self._current(session)
                frames, _, _ = await self._frames(session)
                if version != session.document_versions.get(frame_id):
                    raise _error(409, "browser_snapshot_stale")
                self._frame(frames, frame_id)
                matches = _snapshot({"elements": raw.get("matches", [])}, frame_id,
                                    session.secret_fingerprints)["elements"][:10]
                response = {**session.view(), "frame_id": frame_id, "selector": selector,
                            "match_count": min(10000, max(0, int(raw.get("match_count", 0)))),
                            "visible_count": min(10000, max(0, int(raw.get("visible_count", 0)))),
                            "matches": [], "truncated": raw.get("truncated") is True
                                or raw.get("match_count", 0) > 10}
                projected = self._redact_snapshot({**response, "matches": matches}, session, "matches")
                response = {**projected, "matches": []}
                for node in projected["matches"]:
                    response["matches"].append(node)
                    if len(json.dumps(response, ensure_ascii=True).encode()) > MAX_SNAPSHOT_BYTES:
                        response["matches"].pop()
                        response["truncated"] = True
                        break
                return response
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(502, "Browser observation unavailable") from None

    async def execute(self, session_id: str, request: Any) -> dict[str, Any]:
        from worker.assistant_step import validate_step

        session = await self._session(session_id, request.token)
        try:
            step = validate_step(request)
            _, origin, _ = _url(request.site_origin)
            if origin != session.origin:
                raise ValueError()
            if step["feature_type"] in {"flow.open", "action.navigate"}:
                _, target_origin, _ = _url(step["config"].get("url"), document=False)
                if target_origin != session.origin:
                    raise ValueError()
        except (ValueError, TypeError, HTTPException):
            raise HTTPException(422, "Invalid browser execution") from None
        operation_id = str(request.operation_id)
        retry_of = str(request.retry_of) if request.retry_of is not None else None
        permit = str(request.retry_authorization) if request.retry_authorization is not None else None
        step_digest = hashlib.sha256(json.dumps(
            step, sort_keys=True, ensure_ascii=True, allow_nan=False,
        ).encode()).digest()
        # Bind retries to the complete reviewed request without retaining secrets.
        digest = hashlib.sha256(session.token_digest + json.dumps(
            request.model_dump(mode="json", exclude={"token", "operation_id"}),
            sort_keys=True, ensure_ascii=True, allow_nan=False,
        ).encode()).digest()
        prior = session.operations.get(operation_id)
        if prior is not None:
            if not secrets.compare_digest(prior[0], digest):
                raise HTTPException(409, "Browser operation conflict")
            if prior[1] is None:
                raise HTTPException(409, "Browser operation in progress")
            return _redact_known(prior[1], session.secret_fingerprints)
        if session.lock.locked():
            raise HTTPException(409, "Browser session busy")
        if retry_of is not None:
            if permit is None or permit in session.consumed_permits or retry_of == operation_id:
                raise HTTPException(409, "Browser retry authorization unavailable")
            original = session.operations.get(retry_of)
            if original is not None:
                if (original[1] is None or original[1].get("status") != "unknown"
                        or retry_of not in session.unresolved_operations
                        or not secrets.compare_digest(session.operation_steps.get(retry_of, b""), step_digest)):
                    raise HTTPException(409, "Browser retry operation conflict")
            elif session.uncertain:
                raise HTTPException(409, "Browser retry operation unavailable")
            # A replacement session has no local original. Its authenticated
            # host owns durable exact-step approval and permit issuance.
        elif permit is not None:
            raise HTTPException(422, "Invalid browser retry authorization")
        if (session.uncertain and retry_of is None
                and step["feature_type"] not in READ_ONLY):
            raise HTTPException(409, "Browser outcome uncertain; close and reconfirm target")
        if len(session.operations) >= MAX_OPERATIONS:
            raise HTTPException(409, "Browser operation limit reached")
        # Acquire before publishing the task; no lane or page is reacquired.
        await session.lock.acquire()
        if permit is not None:
            session.consumed_permits.add(permit)
        session.operations[operation_id] = (digest, None)
        session.operation_steps[operation_id] = step_digest
        task = asyncio.create_task(self._execute_locked(session, operation_id, step, request))
        session.execution_task = task
        # Disconnects must not create an ambiguous replay window. Close/reap owns
        # cancellation; retries see in-progress or the cached terminal response.
        return await asyncio.shield(task)

    async def _execute_locked(self, session, operation_id, step, request):
        from worker.assistant_step import fingerprints, validate_secret_targets
        from worker.executor import _run_step
        from worker.secret_client import AssistantSecretContext

        before = {"elements": []}
        after = {"elements": []}
        status, code, result = "error", "execution_error", {}
        context = None
        started = False
        diagnostics = {}
        operation_failure = None
        evidence_deadline = None
        secret_payload = {"base_url": session.origin, "trusted_urls": []}
        deadline = min(session.deadline, time.monotonic() + min(
            EXECUTE_TIMEOUT, step.get("timeout_seconds") or EXECUTE_TIMEOUT,
        ))
        try:
            async with asyncio.timeout_at(deadline):
                if session.closed:
                    raise _denied()
                # Observation trust never delegates execution or credentials.
                if _url(session.page.url)[1] != session.origin:
                    raise _denied()
                await session.dns.check(_url(request.site_origin)[0])
                if step["feature_type"] in {"flow.open", "action.navigate"}:
                    await session.dns.check(_url(step["config"]["url"], document=False)[0])
                if request.credentials is not None:
                    credentials = request.credentials.model_dump(exclude_none=True)
                    session.secret_fingerprints.update(fingerprints(credentials.values()))

                    async def validate_targets():
                        try:
                            await validate_secret_targets(session.page, step, session.origin, context.targets)
                        except HTTPException as exc:
                            if _exception_code(exc, "") in POLICY_CODES:
                                session.failure = exc.detail
                            raise

                    context = AssistantSecretContext(
                        step["feature_type"], step["step_key"], credentials, validate_targets,
                    )
                    secret_payload["_assistant_secret_context"] = context
                session.failure = None
                session.blocked_requests.clear()
                session.same_origin_only = True
                session.executing = True
                session.writes_authorized = step["feature_type"] not in READ_ONLY
                session.credential_execution = context is not None
                before = await self._collect(session)
                started = True
                raw_status, _, data = await _run_step(
                    session.page, step, credentials=None, twofauth=None, cfbox=None,
                    cancel=None, xdisplay=None, lane_key=session.lane_key,
                    log=lambda *_: None, capture_image=None, artifacts=None,
                    secret_payload=secret_payload,
                )
                if session.failure or session.closed:
                    raise HTTPException(403, session.failure or {"code": "browser_url_denied"})
                await self._current(session)
                status = {"success": "success", "fail": "failed", "unknown": "unknown"}.get(raw_status, "unknown")
                if status == "failed" and context is not None and context.action_started:
                    status = "unknown"
                code = {"success": "step_succeeded", "failed": "step_failed", "unknown": "outcome_uncertain"}[status]
                if isinstance(data, dict):
                    result = {key: data[key] for key in ("submitted", "verified", "count")
                              if key in data and (type(data[key]) is bool or
                                  (key == "count" and type(data[key]) is int and 0 <= data[key] <= 10000))}
                    if step["feature_type"] == "auth.form_login":
                        evidence = _success_evidence_result(data.get("success_evidence"))
                        if evidence is not None:
                            result["success_evidence"] = evidence
                if step["feature_type"] == "auth.form_login" and status == "success":
                    result = {"submitted": True, "verified": bool(step["config"].get("success_selector"))}
                evidence_deadline = min(session.deadline, time.monotonic() + 3)
                try:
                    async with asyncio.timeout_at(evidence_deadline):
                        after = await self._collect(session)
                except (Exception, asyncio.CancelledError) as exc:
                    diagnostics["evidence_errorcode"] = _exception_code(exc, "browser_snapshot_unavailable")
                    raise
                if session.failure or session.closed:
                    raise HTTPException(403, session.failure or {"code": "browser_url_denied"})
        except TimeoutError:
            status, code, result = "error", "execution_timeout", {}
        except asyncio.CancelledError:
            status, code, result = "error", "execution_canceled", {}
        except Exception as exc:
            status, code, result = "error", "execution_error", {}
            diagnostics["reason"] = _exception_code(exc, code)
        finally:
            operation_failure = session.failure
            if operation_failure and operation_failure.get("code") in POLICY_CODES:
                diagnostics["reason"] = operation_failure["code"]
            if status == "error":
                diagnostics.setdefault("reason", code)
                # Evidence has its own small budget; it must not replace the
                # execution error or close an otherwise safe, useful page.
                try:
                    if evidence_deadline is None:
                        evidence_deadline = min(session.deadline, time.monotonic() + 3)
                    async with asyncio.timeout_at(evidence_deadline):
                        if session.closed:
                            raise HTTPException(409, "Browser session unavailable")
                        after = await self._collect(session)
                except (Exception, asyncio.CancelledError) as exc:
                    diagnostics.setdefault("evidence_errorcode", _exception_code(exc, "browser_snapshot_unavailable"))
            if session.failure is operation_failure:
                session.failure = None
            if status == "error" and (context.action_started if context is not None else started):
                status, code = "unknown", "outcome_uncertain"
            if status == "unknown":
                session.unresolved_operations.add(operation_id)
                session.uncertain = True
            elif status == "success" and request.retry_of is not None:
                # A failed retry (e.g. missing target) says nothing about the original action.
                # Resolve only a successful retry's ancestry, never unrelated unknowns.
                resolved = str(request.retry_of)
                while resolved in session.unresolved_operations:
                    session.unresolved_operations.discard(resolved)
                    original = session.operations.get(resolved)
                    parent = original[1].get("retry_of") if original and original[1] else None
                    if parent is None:
                        break
                    resolved = parent
                session.uncertain = bool(session.unresolved_operations)
            if context is not None:
                context.credentials.clear()
                handles = list(context.targets.values())
                context.targets.clear()

                async def dispose_handles():
                    for handle in handles:
                        try:
                            await asyncio.wait_for(handle.dispose(), 1)
                        except Exception:
                            pass

                # Do not await in the result-publication path: close can cancel
                # here too, and must never leave the operation or lock pending.
                task = asyncio.create_task(dispose_handles())
                session.tasks.add(task)
                task.add_done_callback(session.tasks.discard)
            secret_payload.clear()
            # Keep cross-origin subresources blocked after execution as DOM input
            # values or delayed page scripts may still contain credentials.
            session.executing = False
            session.writes_authorized = False
            session.credential_execution = False
            if session.blocked_requests:
                diagnostics["blocked_requests"] = list(session.blocked_requests)
            response = {
                "session_id": session.session_id, "lane_key": session.lane_key,
                "operation_id": operation_id, "status": status, "code": code,
                "logs": ["Step execution started", "Step execution finished"],
                "before": before, "after": after, "result": result,
                "diagnostics": diagnostics,
            }
            if request.retry_of is not None:
                response["retry_of"] = str(request.retry_of)
            digest = session.operations[operation_id][0]
            session.operations[operation_id] = (digest, response)
            session.lock.release()
        return response

    async def screenshot(self, session_id: str, token: str) -> dict[str, Any]:
        from PIL import Image

        session = await self._session(session_id, token)
        if session.lock.locked():
            raise HTTPException(409, "Browser session busy")
        async with session.lock:
            try:
                async with asyncio.timeout(10):
                    if session.closed:
                        raise HTTPException(404, "Browser session unavailable")
                    url = await self._current(session)
                    if session.failure:
                        raise HTTPException(403, session.failure)
                    tree = await session.cdp.send("Page.getFrameTree")
                    frame = tree["frameTree"]["frame"]
                    if frame.get("unreachableUrl") or frame.get("url") != url.split("#", 1)[0]:
                        raise _error(409, "browser_snapshot_stale")
                    world = await session.cdp.send("Page.createIsolatedWorld", {
                        "frameId": frame["id"], "worldName": "siteflow-assistant-screenshot",
                    })
                    viewport = await session.cdp.send("Runtime.callFunctionOn", {
                        "functionDeclaration": "function() { return {width: innerWidth, height: innerHeight}; }",
                        "executionContextId": world["executionContextId"], "returnByValue": True,
                    })
                    dimensions = viewport["result"]["value"]
                    if ("exceptionDetails" in viewport or any(
                        type(dimensions.get(key)) not in {int, float}
                        or not math.isfinite(dimensions[key]) or dimensions[key] < 0
                        for key in ("width", "height")
                    )):
                        raise ValueError()
                    width = min(1600, int(dimensions["width"]))
                    height = min(1200, int(dimensions["height"]))
                    if width < 1 or height < 1:
                        raise ValueError()
                    # Mask every embedded document, including approved frames:
                    # frame attachment/navigation cannot race a trust decision.
                    image = await session.page.screenshot(
                        type="jpeg", quality=60, full_page=False, scale="css",
                        clip={"x": 0, "y": 0, "width": width, "height": height},
                        mask=[session.page.locator(SCREENSHOT_MASK)], animations="disabled", timeout=5000,
                    )
                    if len(image) > MAX_SCREENSHOT_BYTES:
                        raise _error(502, "browser_screenshot_limit")
                    with Image.open(BytesIO(image)) as decoded:
                        if decoded.format != "JPEG" or decoded.size != (width, height):
                            raise ValueError()
                    if await self._current(session) != url:
                        raise _error(409, "browser_snapshot_stale")
                    current = (await session.cdp.send("Page.getFrameTree"))["frameTree"]["frame"]
                    if (session.closed or session.failure or current.get("unreachableUrl")
                            or any(current.get(key) != frame.get(key) for key in ("id", "loaderId", "url", "urlFragment"))):
                        raise _error(409, "browser_snapshot_stale")
                    return {**session.view(), "mime_type": "image/jpeg",
                            "image_base64": base64.b64encode(image).decode("ascii"),
                            "width": width, "height": height, "masked": True}
            except HTTPException:
                raise
            except Exception:
                raise _error(502, "browser_screenshot_unavailable") from None

    async def refresh(self, session_id: str, token: str) -> dict[str, Any]:
        session = await self._session(session_id, token)
        if session.lock.locked():
            raise HTTPException(409, "Browser session busy")
        async with session.lock:
            try:
                async with asyncio.timeout(30):
                    if session.closed:
                        raise HTTPException(404, "Browser session unavailable")
                    url = await self._current(session)
                    session.failure = None
                    # reload can replay POST; a fragment-only goto can skip GET.
                    await session.page.goto(url.split("#", 1)[0], wait_until="domcontentloaded", timeout=30000)
                    if session.failure:
                        raise HTTPException(403, session.failure)
                    await self._current(session)
                    session.snapshots.clear()
                    session.latest_snapshots.clear()
                    session.document_versions.clear()
                    return {**session.view(), "refreshed": True}
            except HTTPException:
                raise
            except Exception:
                if session.failure:
                    raise HTTPException(403, session.failure) from None
                raise _error(502, "browser_navigation_failed", lane_key=session.lane_key, stage="refresh") from None

    async def navigate(self, session_id: str, token: str, url: str) -> dict[str, Any]:
        session = await self._session(session_id, token)
        async with session.lock:
            try:
                if session.closed:
                    raise HTTPException(404, "Browser session unavailable")
                host, origin, _ = _url(url)
                self._guard_secret_url(session, url, "GET")
                if (session.same_origin_only or session.executing) and origin != session.origin:
                    raise _denied()
                if origin not in session.approved_origins:
                    raise await self._navigation_error(session, url)
                await session.dns.check(host)
                session.failure = None
                await session.page.goto(url, wait_until="domcontentloaded", timeout=30000)
                if session.failure:
                    raise HTTPException(403, session.failure)
                await self._current(session)
                return session.view()
            except HTTPException:
                raise
            except Exception:
                if session.failure:
                    raise HTTPException(403, session.failure) from None
                raise _error(502, "browser_navigation_failed", lane_key=session.lane_key, stage="navigate") from None

    async def _isolated_snapshot(self, session: BrowserSession, frame: dict,
                                 arguments: dict | None = None) -> Any:
        # frame is resolved from Page.getFrameTree, never a caller's CDP pointer.
        origin = _url(frame["url"], document=False)[1]
        if (origin not in session.approved_origins or
                ((session.executing or session.same_origin_only) and origin != session.origin)):
            raise _denied()
        await session.dns.check(_url(frame["url"], document=False)[0])
        world = await session.cdp.send("Page.createIsolatedWorld", {
            "frameId": frame["id"], "worldName": "siteflow-assistant-snapshot",
        })
        result = await session.cdp.send("Runtime.callFunctionOn", {
            "functionDeclaration": SNAPSHOT_SCRIPT,
            "executionContextId": world["executionContextId"],
            "returnByValue": True, "awaitPromise": False,
            "arguments": [{"value": {**(arguments or {"mode": "snapshot"}),
                                      "expected_url": frame["url"]}}],
        })
        if "exceptionDetails" in result:
            raise HTTPException(502, "Browser observation unavailable")
        value = result["result"].get("value")
        if isinstance(value, dict) and value.get("stale"):
            raise _error(409, "browser_snapshot_stale")
        return value

    async def close(self, session_id: str, token: str) -> dict[str, bool]:
        session_id = self._id(session_id)
        digest = hashlib.sha256(token.encode()).digest()
        session = self.sessions.get(session_id)
        if session is None:
            self._prune_tombstones()
            tombstone = self.tombstones.get(session_id)
            if tombstone is not None and not secrets.compare_digest(tombstone[0], digest):
                raise HTTPException(404, "Browser session unavailable")
            if tombstone is None and len(self.sessions) + len(self.tombstones) >= MAX_TOMBSTONES:
                raise _error(503, "browser_lane_unavailable", stage="close")
            self._remember(session_id, digest)
            return {"closed": True}
        if not secrets.compare_digest(session.token_digest, digest):
            raise HTTPException(404, "Browser session unavailable")
        await self._dispose(session)
        if session.quarantined:
            raise HTTPException(503, "Browser cleanup pending")
        return {"closed": True}
