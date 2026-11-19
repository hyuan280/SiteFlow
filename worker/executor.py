from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import math
import os
import re
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from worker.browser_backend import Desktop
from worker.cfbox_response import resolve_cfbox_prediction

WAIT_UNTIL = {"domcontentloaded", "load", "networkidle"}
SYSTEM_RESIGN_SIGNAL_KEY = "sys_resign_pending"
CHECKPOINT_RESIGN_FEATURES = frozenset({"post.resign_loop", "post.resign_dialog"})
PAGE_TIMEOUT_MS = 30000
SELECTOR_WAIT_MS = 10000
SIGNAL_WAIT_MS = 3000
FRAME_OP_TIMEOUT_SECONDS = 2.0
SIGNAL_EVAL_TIMEOUT_SECONDS = 10.0
SCREEN_WIDTH = 1920
SCREEN_HEIGHT = 1080
StepCallback = Callable[[dict[str, Any]], Awaitable[None]]
ARTIFACT_ROOT = Path(os.environ.get("SITEFLOW_ARTIFACT_ROOT", "/app/artifacts"))
CF_MARKER_SELECTOR = (
    ".cf-turnstile, [id^='cf-chl-widget']:not(input):not(textarea), "
    "iframe[src*='challenges.cloudflare.com'], iframe[src*='challenge-platform']"
)
# Share the bounded, composed-tree walk between marker and token reads.
CF_DOM_SCAN_SCRIPT = """
    const elements = [];
    const roots = [document];
    let readable = true;
    for (let index = 0; index < roots.length; index++) {
        const walker = document.createTreeWalker(roots[index], NodeFilter.SHOW_ELEMENT);
        let element;
        while ((element = walker.nextNode())) {
            if (elements.length >= 20000) { readable = false; break; }
            elements.push(element);
            if (element.shadowRoot) {
                if (roots.length >= 256) readable = false;
                else roots.push(element.shadowRoot);
            }
        }
        if (elements.length >= 20000) { readable = false; break; }
    }
    const trustedScript = elements.some((element) => {
        if (!element.matches('script[src]')) return false;
        try {
            const url = new URL(element.src, document.baseURI);
            return url.protocol === 'https:' && !url.username && !url.password && url.hostname === 'challenges.cloudflare.com'
                && url.pathname.startsWith('/turnstile/');
        } catch { return false; }
    });
    const isMarker = (element) => !element.matches('input, textarea, script') && (
        element.matches('.cf-turnstile, [id^="cf-chl-widget"], '
            + 'iframe[src*="challenges.cloudflare.com"], iframe[src*="challenge-platform"]')
        || (trustedScript && element.matches('.ts-box'))
    );
"""
CF_ELEMENT_VISIBLE_SCRIPT = """(element) => {
    const rect = element.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    const ownStyle = getComputedStyle(element);
    if (ownStyle.visibility === 'hidden' || ownStyle.visibility === 'collapse') return false;
    let node = element;
    for (let depth = 0; node && depth < 256; depth++) {
        const style = getComputedStyle(node);
        if (style.display === 'none' || style.contentVisibility === 'hidden'
            || Number(style.opacity || 1) === 0) return false;
        node = node.assignedSlot || node.parentElement || node.getRootNode().host;
    }
    if (node) throw new Error('CF visibility depth exceeded');
    return true;
}"""
CF_VISIBLE_MARKER_SCRIPT = "() => {" + CF_DOM_SCAN_SCRIPT + """
    const visible = """ + CF_ELEMENT_VISIBLE_SCRIPT + """;
    for (const element of elements) {
        if (isMarker(element) && visible(element)) return true;
    }
    if (!readable) throw new Error('CF DOM scan limit exceeded');
    return false;
}"""
CF_TOKEN_LENGTH_SCRIPT = "() => {" + CF_DOM_SCAN_SCRIPT + """
    if (!readable) throw new Error('CF token scan limit exceeded');
    let length = 0;
    for (const element of elements) {
        if (element.matches('[name="cf-turnstile-response"]')) {
            length = Math.max(length, String(element.value || '').length);
        }
    }
    return length;
}"""
CF_PAGE_TEXT_SCRIPT = """() => {
    if (!document.body) throw new Error('CF page body unavailable');
    return document.body.innerText;
}"""
PAGE_ERROR_MARKERS = (
    "bad gateway",
    "service unavailable",
    "service temporarily unavailable",
    "gateway time-out",
    "gateway timeout",
    "internal server error",
    "origin is unreachable",
    "web server is down",
    "error code 502",
    "error code 503",
    "error code 504",
    "error code 520",
    "error code 521",
    "error code 522",
    "error code 523",
    "error code 524",
    "网站正在维护",
    "站点正在维护",
    "系统维护中",
    "站点维护中",
    "网站维护中",
    "正在维护",
    "关闭维护",
    "the website has been stopped",
    "under maintenance",
    "maintenance mode",
    "site is down",
    "404 not found",
    "页面不存在",
)
PAGE_ERROR_TITLE_RE = re.compile(
    r"(?:^|\D)(?:403|404|500|502|503|504|520|521|522|523|524)(?:\D|$)"
    r"|not found|bad gateway|service unavailable|internal server error",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _StepInputValidationError(RuntimeError):
    def __init__(self, message: str, field: str) -> None:
        super().__init__(message)
        self.field = field


def _step_result(
    step: dict[str, Any],
    status: str,
    message: str,
    *,
    started: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "step_key": step.get("step_key"),
        "feature_type": step.get("feature_type"),
        "label": step.get("label") or step.get("feature_type") or step.get("step_key"),
        "status": status,
        "message": message,
        "data": data or {},
        "started_at": started,
        "finished_at": _now(),
    }


def _safe_name(value: Any) -> str:
    cleaned = "".join(
        char for char in str(value or "") if char.isalnum() or char in "._-"
    )
    return cleaned[:80] or "unknown"


class JobArtifacts:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.pages = None
        self.enabled = False
        self.directory: Path | None = None
        self.log_path: Path | None = None
        self.screenshots: list[dict[str, Any]] = []
        self._screenshot_count = 0
        run_part = f"run-{_safe_name(payload.get('run_id'))}"
        job_part = f"job-{_safe_name(payload.get('job_id'))}"
        self.directory = ARTIFACT_ROOT / run_part / job_part
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.log_path = self.directory / "job.log"
            self.log_path.write_text("", encoding="utf-8")
            self.enabled = True
        except OSError:
            self.directory = None
            self.log_path = None
        self.log(
            "INFO",
            f"job start site={payload.get('site_name') or '-'} "
            f"run={payload.get('run_id')} job={payload.get('job_id')} "
            f"attempt={payload.get('attempt') or 1}",
        )

    def log(self, level: str, message: str) -> None:
        if not self.enabled or self.log_path is None:
            return
        line = f"[{datetime.now(timezone.utc).isoformat()}] {level} {message}\n"
        try:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            pass

    def _metadata(
        self,
        path: Path,
        kind: str,
        label: str,
        step_key: str | None,
    ) -> dict[str, Any]:
        data = path.read_bytes()
        return {
            "kind": kind,
            "label": label,
            "step_key": step_key,
            "relative_path": path.relative_to(ARTIFACT_ROOT).as_posix(),
            "content_type": "text/plain; charset=utf-8" if kind == "log" else "image/png",
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    def snapshot(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if self.enabled and self.log_path is not None and self.log_path.is_file():
            try:
                items.append(
                    self._metadata(self.log_path, "log", "执行日志", None)
                )
            except OSError:
                pass
        items.extend(dict(item) for item in self.screenshots)
        return items

    def save_image(
        self,
        image: bytes,
        *,
        prefix: str,
        label: str,
        step_key: str | None,
        kind: str = "captcha",
    ) -> bool:
        if not self.enabled or self.directory is None or not image:
            return False
        self._screenshot_count += 1
        name = f"{prefix}-{_safe_name(step_key or 'job')}-{self._screenshot_count}.png"
        path = self.directory / name
        try:
            path.write_bytes(image)
            self.screenshots.append(
                self._metadata(path, kind, label[:200], step_key)
            )
            self.log("INFO", f"{prefix} image saved: {name}")
            return True
        except OSError as exc:
            self.log("WARN", f"{prefix} image write failed: {exc}")
            return False

    async def capture_failure(
        self,
        page: Any,
        step_key: str | None,
        label: str,
        reason: str = "失败",
    ) -> None:
        if not self.enabled or self.directory is None:
            return
        try:
            image = await page.screenshot(type="png")
        except Exception:
            self.log("WARN", "failure screenshot failed")
            return
        if not image:
            return
        self._screenshot_count += 1
        name = f"failure-{_safe_name(step_key or 'job')}-{self._screenshot_count}.png"
        path = self.directory / name
        try:
            path.write_bytes(image)
            self.screenshots.append(
                self._metadata(path, "screenshot", f"{reason}截图 · {label}", step_key)
            )
            self.log("WARN", f"{reason}截图已保存: {name}")
        except OSError as exc:
            self.log("WARN", f"{reason}截图写入失败: {exc}")


async def _emit_progress(
    on_step: StepCallback | None,
    job_status: str,
    job_message: str,
    results: list[dict[str, Any]],
    artifacts: list[dict[str, Any]] | None = None,
) -> None:
    if on_step is None:
        return
    try:
        await on_step(
            {
                "status": job_status,
                "message": job_message,
                "steps": [dict(item) for item in results],
                "artifacts": artifacts or [],
            }
        )
    except Exception:
        pass


async def _find_locator(
    page: Any,
    selectors: list[str],
    *,
    wait_ms: int = 0,
    on_match: Callable[[str], None] | None = None,
    visible_any: bool = False,
):
    deadline = time.monotonic() + max(0, wait_ms) / 1000
    while True:
        for selector in selectors:
            if not selector:
                continue
            try:
                matches = page.locator(selector)
                if visible_any:
                    candidates = [matches.nth(index) for index in range(min(await matches.count(), 100))]
                else:
                    candidates = [matches.first] if await matches.first.count() else []
                for locator in candidates:
                    if await locator.is_visible():
                        if on_match is not None:
                            on_match(selector)
                        return locator
            except Exception:
                continue
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.2)


def _page_frames(page: Any) -> list[Any]:
    try:
        return list(page.frames)
    except Exception:
        return [page]


async def _success_frame_scope(page: Any, frame: Any, deadline: float) -> str:
    """Classify without reading an excluded frame's document."""
    def origin(url: str):
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                return None
            port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        except ValueError:
            return None
        return (parsed.scheme, parsed.hostname.lower(), port)

    async with asyncio.timeout(max(0, deadline - time.monotonic())):
        main = getattr(page, "main_frame", page)
        if frame is main:
            return "main"
        page_origin = origin(page.url)
        if page_origin is None:
            return "unreadable_frame"
        chain = []
        current = frame
        for _ in range(100):
            if current is main:
                break
            try:
                parsed = urlsplit(current.url)
            except ValueError:
                return "unreadable_frame"
            inherited = parsed.scheme == "about" and parsed.path in {"blank", "srcdoc"}
            if not inherited and origin(current.url) != page_origin:
                return "cross_origin"
            chain.append(current)
            current = current.parent_frame
            if current is None:
                return "unreadable_frame"
        else:
            return "unreadable_frame"
        # Child locators can report visible inside a hidden iframe. Every
        # embedding element must also be visible, including inherited origins.
        for child in chain:
            handle = await child.frame_element()
            try:
                if not await handle.is_visible():
                    return "hidden_frame"
            finally:
                # Cleanup must not turn a stalled frame read into an unbounded wait.
                await asyncio.wait_for(
                    handle.dispose(),
                    timeout=max(0.001, min(FRAME_OP_TIMEOUT_SECONDS, deadline - time.monotonic())),
                )
        return "same_origin"


async def _find_success_locator(
    page: Any,
    selectors: list[str],
    *,
    wait_ms: int,
    on_match: Callable[[str], None] | None = None,
):
    """Read evidence only in the main document or visible same-origin frame chains."""
    deadline = time.monotonic() + max(0, wait_ms) / 1000

    async def success_match(frame: Any, selector: str):
        if await _success_frame_scope(page, frame, deadline) not in {"main", "same_origin"}:
            return None
        locator = await _find_locator(frame, [selector], visible_any=True)
        if locator is not None and await _success_frame_scope(page, frame, deadline) in {"main", "same_origin"}:
            return locator
        return None

    while True:
        # Re-read frames and locators as SPA rendering and redirects settle.
        for frame in _page_frames(page)[:100]:
            for selector in selectors:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                if not selector:
                    continue
                try:
                    locator = await asyncio.wait_for(
                        success_match(frame, selector),
                        timeout=min(FRAME_OP_TIMEOUT_SECONDS, remaining),
                    )
                except Exception:
                    continue
                if locator is not None:
                    if on_match is not None:
                        on_match(selector)
                    return locator
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        await asyncio.sleep(min(0.2, remaining))


async def _login_success_evidence(
    page: Any, selectors: list[str], *, waited_ms: int,
    log: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """A bounded, non-atomic snapshot, never a second login success decision."""
    from playwright.async_api import Error as PlaywrightError

    started = time.monotonic()
    deadline = started + 3.0
    frames = _page_frames(page)
    evidence: dict[str, Any] = {
        "selector_mode": "visible", "frame_scope": "main_and_visible_same_origin",
        "snapshot_consistency": "sampled_after_timeout", "captured_after_timeout": True,
        "waited_ms": waited_ms, "elapsed_ms": 0, "configured_count": len(selectors),
        "selectors": [], "evidence_truncated": len(selectors) > 10 or len(frames) > 10,
    }
    for selector_index, selector in enumerate(selectors[:10]):
        summary: dict[str, Any] = {
            "selector_index": selector_index, "match_count": 0, "visible_count": 0,
            "checked_count": 0, "truncated": len(frames) > 10,
            "read_errors": 0, "frames": [],
        }
        evidence["selectors"].append(summary)
        if not frames:
            summary["read_errors"] = 1
            summary["truncated"] = True
        for frame_index, frame in enumerate(frames[:10]):
            sample: dict[str, Any] = {
                "frame_index": frame_index, "scope": "unreadable_frame",
                "match_count": None, "visible_count": None,
                "checked_count": 0, "truncated": False,
            }
            summary["frames"].append(sample)
            reading_selector = False
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                async with asyncio.timeout(min(FRAME_OP_TIMEOUT_SECONDS, remaining)):
                    sample["scope"] = await _success_frame_scope(page, frame, deadline)
                    if sample["scope"] not in {"main", "same_origin"}:
                        sample["error"] = "frame_not_eligible"
                        if sample["scope"] == "unreadable_frame":
                            summary["read_errors"] += 1
                            sample["truncated"] = True
                        continue
                    reading_selector = True
                    matches = frame.locator(selector)
                    count = await matches.count()
                    if type(count) is not int or not 0 <= count <= 2**53 - 1:
                        raise RuntimeError()
                    sample["match_count"] = count
                    sample["visible_count"] = 0
                    for index in range(min(count, 100 - summary["checked_count"])):
                        visible = await matches.nth(index).is_visible()
                        if type(visible) is not bool:
                            raise RuntimeError()
                        sample["checked_count"] += 1
                        summary["checked_count"] += 1
                        sample["visible_count"] += int(visible)
                    sample["truncated"] = sample["checked_count"] < count
            except Exception as exc:
                # Only static codes leave this boundary; provider messages may contain secrets.
                code = "read_timeout"
                try:
                    if frame.is_detached() is True:
                        code = "frame_detached"
                except Exception:
                    pass
                if code != "frame_detached" and reading_selector:
                    if isinstance(exc, ValueError) or (isinstance(exc, PlaywrightError) and re.search(
                        r"invalid selector|not a valid selector|while parsing.*selector|"
                        r"unknown engine|unexpected token|unsupported token",
                        exc.message, re.IGNORECASE,
                    )):
                        code = "invalid_selector"
                sample["error"] = code
                sample["truncated"] = True
                summary["read_errors"] += 1
            finally:
                summary["match_count"] += sample["match_count"] or 0
                summary["visible_count"] += sample["visible_count"] or 0
                summary["truncated"] |= sample["truncated"]
        evidence["evidence_truncated"] |= summary["truncated"]

    visible = any(item["visible_count"] for item in evidence["selectors"])
    excluded = any(frame["error"] == "frame_not_eligible"
                   for item in evidence["selectors"] for frame in item["frames"] if "error" in frame)
    evidence["visible_after_timeout"] = visible
    evidence["reason"] = (
        "visible_after_timeout" if visible else
        "read_incomplete" if evidence["evidence_truncated"] else
        "matches_hidden" if any(item["match_count"] for item in evidence["selectors"]) else
        "frame_scope_excluded" if excluded else "selector_missing"
    )
    evidence["elapsed_ms"] = max(0, int((time.monotonic() - started) * 1000))
    # Trim detail, not aggregate counts, to fit the entire serialized snapshot.
    while len(json.dumps(evidence).encode("utf-8")) > 16000:
        summary = next(item for item in reversed(evidence["selectors"]) if item["frames"])
        summary["frames"].pop()
        summary["truncated"] = evidence["evidence_truncated"] = True
        if not visible:
            evidence["reason"] = "read_incomplete"
    if log is not None:
        for item in evidence["selectors"]:
            scopes = {scope: sum(frame["scope"] == scope for frame in item["frames"])
                      for scope in ("main", "same_origin", "cross_origin", "hidden_frame", "unreadable_frame")}
            log("INFO", f"login success evidence #{item['selector_index']} "
                f"matches={item['match_count']} visible={item['visible_count']} "
                f"checked={item['checked_count']} read_errors={item['read_errors']} "
                f"truncated={item['truncated']} frames={json.dumps(scopes)}")
    return evidence


async def _selector_exists(page: Any, selector: str) -> bool:
    for frame in _page_frames(page):
        try:
            count = await asyncio.wait_for(
                frame.locator(selector).first.count(),
                timeout=FRAME_OP_TIMEOUT_SECONDS,
            )
            if count:
                return True
        except Exception:
            continue
    return False


async def _page_text(page: Any, *, strict: bool = False) -> str:
    parts: list[str] = []
    try:
        parts.append(
            await asyncio.wait_for(
                page.title(), timeout=FRAME_OP_TIMEOUT_SECONDS
            )
        )
    except Exception:
        if strict:
            raise
        pass
    for frame in _page_frames(page):
        try:
            parts.append(
                await asyncio.wait_for(
                    frame.inner_text("body"), timeout=FRAME_OP_TIMEOUT_SECONDS
                )
            )
        except Exception:
            if strict:
                raise
            continue
    return "\n".join(parts).casefold()


async def _cf_page_state(page: Any) -> dict[str, Any]:
    marker: bool | None = False
    dom_marker: bool | None = False
    frame_challenge = False
    frames_checked = frames_hidden = frames_unreadable = 0
    token_length = 0
    token_readable = text_readable = True
    marker_readable = True
    parts: list[str] = []
    try:
        # Health/state reads must not run the SDK's humanized action pipeline.
        title = await asyncio.wait_for(page.title(), timeout=FRAME_OP_TIMEOUT_SECONDS)
        if not isinstance(title, str):
            raise ValueError("Invalid CF page title")
        parts.append(title)
    except Exception:
        text_readable = False
    try:
        main_frame = getattr(page, "main_frame", page)
        frames = [frame for frame in page.frames if frame is not main_frame]
    except Exception:
        frames = []
        marker = None
        marker_readable = False
        text_readable = token_readable = False
    if len(frames) > 256:
        frames = frames[:256]
        marker = None
        marker_readable = False
        text_readable = token_readable = False
    for frame in [page, *frames]:
        if frame is not page:
            # A child can have a layout box even when its outer iframe is hidden.
            try:
                ancestors = []
                ancestor = frame
                while ancestor is not None and ancestor is not main_frame:
                    if len(ancestors) >= 64:
                        raise ValueError("CF frame depth exceeded")
                    ancestors.append(ancestor)
                    ancestor = ancestor.parent_frame
                visible = True
                for ancestor in reversed(ancestors):
                    element = await asyncio.wait_for(ancestor.frame_element(), timeout=FRAME_OP_TIMEOUT_SECONDS)
                    try:
                        visible = await asyncio.wait_for(element.evaluate(CF_ELEMENT_VISIBLE_SCRIPT), timeout=FRAME_OP_TIMEOUT_SECONDS)
                        if type(visible) is not bool:
                            raise ValueError("Invalid CF frame visibility")
                    finally:
                        await element.dispose()
                    if not visible:
                        break
                if not visible:
                    frames_hidden += 1
                    continue
            except Exception:
                frames_unreadable += 1
                marker_readable = False
                marker = None if marker is not True else True
                text_readable = token_readable = False
                continue
            frames_checked += 1
            try:
                url = str(frame.url).casefold()
                if "challenges.cloudflare.com" in url or "challenge-platform" in url:
                    frame_challenge = True
                    marker = True
            except Exception:
                marker_readable = False
                marker = None if marker is not True else True
        try:
            found = await asyncio.wait_for(frame.evaluate(CF_VISIBLE_MARKER_SCRIPT), timeout=FRAME_OP_TIMEOUT_SECONDS)
            if type(found) is not bool:
                raise ValueError("Invalid CF marker result")
            if found:
                marker = dom_marker = True
        except Exception:
            marker_readable = False
            marker = None if marker is not True else True
            dom_marker = None if dom_marker is not True else True
        try:
            length = await asyncio.wait_for(frame.evaluate(CF_TOKEN_LENGTH_SCRIPT), timeout=FRAME_OP_TIMEOUT_SECONDS)
            token_length = max(token_length, int(length))
        except Exception:
            token_readable = False
        try:
            text = await asyncio.wait_for(frame.evaluate(CF_PAGE_TEXT_SCRIPT), timeout=FRAME_OP_TIMEOUT_SECONDS)
            if not isinstance(text, str):
                raise ValueError("Invalid CF page text")
            parts.append(text)
        except Exception:
            text_readable = False
    text = "\n".join(parts).casefold()
    challenge_words = (
        "just a moment",
        "checking your browser",
        "performing security verification",
        "checking if the site connection is secure",
        "verify you are human",
        "verifying you are human",
        "security verification",
        "enable javascript and cookies to continue",
        "needs to review the security of your connection",
        "请稍候",
        "正在进行安全验证",
        "challenge-platform",
        "cf-challenge",
        "确认您是真人",
    )
    current_url = _cf_url_key(getattr(page, "url", ""))
    url_challenge = (
        "__cf_chl_" in current_url.casefold()
        or "challenge-platform" in current_url.casefold()
    )
    return {
        "marker": marker,
        "marker_readable": marker_readable,
        "token_length": token_length,
        "token_readable": token_readable,
        "challenge_text": any(word.casefold() in text for word in challenge_words),
        "text_readable": text_readable,
        "frame_challenge": frame_challenge,
        "url_challenge": url_challenge,
        "dom_marker": dom_marker,
        "frames_checked": frames_checked,
        "frames_hidden": frames_hidden,
        "frames_unreadable": frames_unreadable,
    }


def _image_dimensions(image: bytes, fallback: tuple[int, int]) -> tuple[int, int]:
    """Use the submitted image, not a remote page's absent viewport metadata."""
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(image)) as decoded:
            width, height = decoded.size
        if width > 0 and height > 0:
            return width, height
    except Exception:
        pass
    return fallback


def _cf_url_key(value: Any) -> str:
    return str(value or "").split("#", 1)[0]


def _cf_progress_reason(
    initial_url: str,
    current_url: str,
    token_length: int,
    previous_token_length: int,
    clicked: bool,
) -> str | None:
    if not clicked:
        return None
    if token_length > previous_token_length and token_length > 0:
        return "Turnstile token 已更新"
    initial = _cf_url_key(initial_url)
    current = _cf_url_key(current_url)
    if not initial or not current or initial == current:
        return None
    current_lower = current.casefold()
    if "__cf_chl_" in current_lower or "challenge-platform" in current_lower:
        return None
    return "页面已跳转"


async def _scroll_cf_into_view(page: Any) -> None:
    try:
        await asyncio.wait_for(page.evaluate("() => {" + CF_DOM_SCAN_SCRIPT + """
            const visible = """ + CF_ELEMENT_VISIBLE_SCRIPT + """;
            const element = elements.find((element) => isMarker(element) && visible(element));
            if (element) element.scrollIntoView({block: 'nearest', inline: 'nearest'});
        }"""), timeout=FRAME_OP_TIMEOUT_SECONDS)
    except Exception:
        pass


async def _find_frame(page: Any, selectors: list[str]):
    for selector in selectors:
        if not selector:
            continue
        try:
            locator = page.locator(selector).first
            if not await locator.count():
                continue
            handle = await locator.element_handle()
            if handle is None:
                continue
            frame = await handle.content_frame()
            if frame is not None:
                return frame
        except Exception:
            continue
    return None


async def _sleep(seconds: float, cancel: asyncio.Event | None) -> bool:
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        if cancel is not None and cancel.is_set():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        await asyncio.sleep(min(0.25, remaining))


def _short(value: Any, limit: int = 120) -> str:
    text = str(value or "").replace("\n", " ").strip()
    return text[:limit] + ("…" if len(text) > limit else "")


async def _evaluate_signal(page: Any, signal: dict[str, Any]) -> tuple[str, str]:
    try:
        text = await _page_text(page)
    except Exception:
        return "unknown", "页面内容读取失败"
    includes: list[tuple[str, bool]] = []
    for value in signal.get("include_texts") or []:
        includes.append((f'文本 "{_short(value)}"', str(value).casefold() in text))
    for selector in signal.get("include_selectors") or []:
        includes.append(
            (f'选择器 "{_short(selector)}"', await _selector_exists(page, str(selector)))
        )
    if not includes:
        return "false", "未配置包含条件"
    mode = signal.get("match_mode") or "any"
    matched = all(item[1] for item in includes) if mode == "all" else any(
        item[1] for item in includes
    )
    if not matched:
        detail = "，".join(f"{name}={'命中' if ok else '未命中'}" for name, ok in includes)
        return "false", f"未命中包含配置：{detail}"
    for value in signal.get("exclude_texts") or []:
        if str(value).casefold() in text:
            return "false", f'命中排除文本 "{_short(value)}"'
    for selector in signal.get("exclude_selectors") or []:
        if await _selector_exists(page, str(selector)):
            return "false", f'命中排除选择器 "{_short(selector)}"'
    hit = [name for name, ok in includes if ok]
    return "true", "命中配置：" + "，".join(hit)


def _split_condition_body(body: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(body):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return []
        elif char == "," and depth == 0:
            parts.append(body[start:index].strip())
            start = index + 1
    parts.append(body[start:].strip())
    return parts if all(parts) else []


def _parse_condition(value: str | None):  # type: ignore[no-untyped-def]
    text = (value or "").strip()
    if not text or text == "always":
        return ("always",)
    for kind in ("all", "any"):
        prefix = f"{kind}("
        if text.startswith(prefix):
            if not text.endswith(")"):
                return None
            body = text[len(prefix):-1].strip()
            if not body:
                return None
            children = [_parse_condition(part) for part in _split_condition_body(body)]
            if any(child is None for child in children):
                return None
            return (kind, children)
    if not text.startswith("signal:"):
        return None
    body = text[len("signal:"):]
    key, separator, expected = body.rpartition(":")
    if not separator or expected not in {"true", "false"} or not key:
        return None
    return ("signal", key, expected == "true")


async def _evaluate_signal_condition(
    page: Any,
    key: str,
    expected: bool,
    signals: dict[str, dict[str, Any]],
    log: Callable[[str, str], None] | None = None,
    runtime_signals: dict[str, tuple[bool, str]] | None = None,
) -> str:
    signal = signals.get(key)
    label = str(signal.get("label") or signal.get("signal_key") or key) if signal else key
    if signal is None or not signal.get("enabled", True):
        if log is not None:
            log("WARN", f"检查信号条件 {label}：信号不存在或已禁用")
        return "unknown"
    if log is not None:
        log("INFO", f"检查信号条件 {label}（期望 {str(expected).lower()}）")
    if runtime_signals and key in runtime_signals:
        value, detail = runtime_signals[key]
        matched = value == expected
        if log is not None:
            log(
                "INFO",
                f"检查信号条件 {label}：条件{'成立' if matched else '不成立'}；{detail}",
            )
        return "true" if matched else "false"
    deadline = time.monotonic() + SIGNAL_WAIT_MS / 1000
    detail = ""
    while True:
        try:
            value, detail = await asyncio.wait_for(
                _evaluate_signal(page, signal),
                timeout=SIGNAL_EVAL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            if log is not None:
                log("WARN", f"检查信号条件 {label}：读取超时，按无法判断处理")
            return "unknown"
        if value == "unknown":
            if log is not None:
                log("WARN", f"检查信号条件 {label}：无法判断；{detail}")
            return "unknown"
        matched = (value == "true") == expected
        if matched:
            if log is not None:
                log("INFO", f"检查信号条件 {label}：条件成立；{detail}")
            return "true"
        if time.monotonic() >= deadline:
            if log is not None:
                log("INFO", f"检查信号条件 {label}：条件不成立；{detail}")
            return "false"
        await asyncio.sleep(0.25)


async def _evaluate_condition_node(
    node: Any,
    page: Any,
    signals: dict[str, dict[str, Any]],
    log: Callable[[str, str], None] | None = None,
    runtime_signals: dict[str, tuple[bool, str]] | None = None,
) -> str:
    kind = node[0]
    if kind == "always":
        return "true"
    if kind == "signal":
        return await _evaluate_signal_condition(
            page, node[1], node[2], signals, log, runtime_signals
        )
    children = node[1]
    if kind == "all":
        saw_unknown = False
        for child in children:
            value = await _evaluate_condition_node(
                child, page, signals, log, runtime_signals
            )
            if value == "false":
                if log is not None:
                    log("INFO", "条件组 all：不满足")
                return "false"
            if value == "unknown":
                saw_unknown = True
        if saw_unknown:
            if log is not None:
                log("WARN", "条件组 all：存在无法判断的条件")
            return "unknown"
        if log is not None:
            log("INFO", "条件组 all：全部满足")
        return "true"
    if kind == "any":
        saw_unknown = False
        for child in children:
            value = await _evaluate_condition_node(
                child, page, signals, log, runtime_signals
            )
            if value == "true":
                if log is not None:
                    log("INFO", "条件组 any：至少一个满足")
                return "true"
            if value == "unknown":
                saw_unknown = True
        if saw_unknown:
            if log is not None:
                log("WARN", "条件组 any：存在无法判断的条件")
            return "unknown"
        if log is not None:
            log("INFO", "条件组 any：全部不满足")
        return "false"
    return "unknown"


async def _condition_allows(
    page: Any,
    condition: str | None,
    signals: dict[str, dict[str, Any]],
    log: Callable[[str, str], None] | None = None,
    runtime_signals: dict[str, tuple[bool, str]] | None = None,
) -> str:
    parsed = _parse_condition(condition)
    if parsed is None:
        if log is not None:
            log("WARN", f"执行条件无法解析：{condition}")
        return "unknown"
    return await _evaluate_condition_node(
        parsed, page, signals, log, runtime_signals
    )


async def _fetch_otp(
    config: dict[str, Any] | None,
    service_hint: str,
) -> tuple[str | None, str, str]:
    if not config or not config.get("url") or not config.get("token"):
        return None, "waiting_human", "2FAuth 未配置"
    hint = service_hint.strip().casefold()
    if not hint:
        return None, "waiting_human", "2FAuth 服务名为空"
    try:
        headers = {"Authorization": f"Bearer {config['token']}"}
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"{str(config['url']).rstrip('/')}/api/v1/twofaccounts",
                headers=headers,
            )
        if response.status_code in {401, 403}:
            return None, "fail", "2FAuth Token 无效"
        if response.status_code != 200:
            return None, "fail", f"2FAuth 返回 HTTP {response.status_code}"
        payload = response.json()
        accounts = payload if isinstance(payload, list) else payload.get("data", [])
        if not isinstance(accounts, list):
            return None, "fail", "2FAuth 账号列表格式无效"

        def normalized(value: Any) -> str:
            return str(value or "").strip().casefold()

        exact = [item for item in accounts if normalized(item.get("service")) == hint]
        if len(exact) == 1:
            candidates = exact
        else:
            candidates = [
                item
                for item in accounts
                if (
                    hint in normalized(item.get("service"))
                    or hint in normalized(item.get("account"))
                )
            ]
        if not candidates:
            return None, "waiting_human", f"未找到匹配 2FAuth 服务：{service_hint}"
        if len(candidates) > 1:
            return None, "waiting_human", f"2FAuth 服务 {service_hint} 匹配到多个账号"

        account_id = candidates[0].get("id")
        if account_id is None:
            return None, "fail", "2FAuth 账号缺少 id"
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"{str(config['url']).rstrip('/')}/api/v1/twofaccounts/{account_id}/otp",
                headers=headers,
            )
        if response.status_code != 200:
            return None, "fail", f"2FAuth OTP 返回 HTTP {response.status_code}"
        body = response.json()
        if not isinstance(body, dict):
            return None, "fail", "2FAuth OTP 响应格式无效"
        code = body.get("password") or body.get("otp")
        if not code:
            return None, "fail", "2FAuth 未返回动态验证码"
        return str(code), "success", "已获取动态验证码"
    except Exception as exc:
        return None, "fail", f"2FAuth 请求失败: {type(exc).__name__}: {str(exc)[:300]}"


async def _fetch_bitwarden_credentials(
    provider: dict[str, Any],
    log: Callable[[str, str], None] | None = None,
) -> dict[str, str]:
    note = log or (lambda _level, _message: None)
    if str(provider.get("type") or "") != "bitwarden":
        raise RuntimeError("不支持的凭据来源")
    url = str(provider.get("url") or "").rstrip("/")
    if not url:
        raise RuntimeError("Bitwarden URL 未配置")
    mode = str(provider.get("mode") or "domain")
    host = str(provider.get("host") or "").strip()
    custom = str(provider.get("custom") or "").strip()
    body: dict[str, Any] = {"mode": mode, "host": host}
    if custom:
        body["custom"] = custom
    token = str(provider.get("token") or "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        timeout = max(1.0, min(float(provider.get("timeout") or 30), 120.0))
    except (TypeError, ValueError):
        timeout = 30.0
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{url}/lookup", json=body, headers=headers)
    except Exception as exc:
        raise RuntimeError(
            f"Bitwarden 查询失败: {type(exc).__name__}: {str(exc)[:200]}"
        ) from exc
    if response.status_code == 401:
        raise RuntimeError("Bitwarden Token 无效")
    if response.status_code == 404:
        raise RuntimeError("Bitwarden 中没有找到匹配的登录条目")
    if response.status_code != 200:
        detail = ""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                detail = str(payload.get("detail") or payload.get("error") or "")
        except Exception:
            detail = response.text[:200]
        raise RuntimeError(
            f"Bitwarden 查询失败: HTTP {response.status_code}"
            + (f" · {detail}" if detail else "")
        )
    try:
        payload = response.json()
    except Exception as exc:
        raise RuntimeError("Bitwarden 返回了无效 JSON") from exc
    username = str(payload.get("username") or "") if isinstance(payload, dict) else ""
    password = str(payload.get("password") or "") if isinstance(payload, dict) else ""
    if not username:
        raise RuntimeError("Bitwarden 匹配条目缺少用户名")
    note("INFO", "Bitwarden 登录凭据获取完成")
    return {"username": username, "password": password}


async def _run_turnstile(
    page: Any,
    config: dict[str, Any],
    cfbox: dict[str, Any] | None,
    cancel: asyncio.Event | None,
    xdisplay: Desktop | None,
    lane_key: str,
    log: Callable[[str, str], None] | None = None,
    private: Any = None,
) -> tuple[str, str, dict[str, Any]]:
    wait_seconds = max(1, min(120, int(config.get("wait_seconds") or 15)))
    diagnostics: dict[str, Any] = {"state": "unknown", "phase": "page_check"}
    try:
        async with asyncio.timeout(wait_seconds):
            return await _run_turnstile_detection(
                page, config, cfbox, cancel, xdisplay, lane_key, log, diagnostics,
                **({"private": private} if private is not None else {}),
            )
    except TimeoutError:
        if log:
            log("WARN", f"Cloudflare 超时诊断：{json.dumps(diagnostics, ensure_ascii=False)}")
        return "unknown", f"Turnstile 双重确认超时（{wait_seconds}s）", diagnostics


async def _run_turnstile_detection(
    page: Any,
    config: dict[str, Any],
    cfbox: dict[str, Any] | None,
    cancel: asyncio.Event | None,
    xdisplay: Desktop | None,
    lane_key: str,
    log: Callable[[str, str], None] | None = None,
    diagnostics: dict[str, Any] | None = None,
    private: Any = None,
) -> tuple[str, str, dict[str, Any]]:
    note = log or (lambda _level, _message: None)
    if not cfbox or not cfbox.get("url"):
        note("WARN", "CFbox 未配置，无法自动处理 Cloudflare")
        return "waiting_human", "CFbox 未配置", {}
    wait_seconds = max(1, min(120, int(config.get("wait_seconds") or 15)))
    note("INFO", f"Cloudflare 检测开始，最长等待 {wait_seconds}s")
    deadline = time.monotonic() + wait_seconds
    headers = {"X-API-Key": str(cfbox.get("api_key") or "")}
    if not headers["X-API-Key"]:
        headers = {}
    last_state = "unknown"
    clicked = False
    click_count = 0
    page_click_count = 0
    x_click_count = 0
    click_mode: str | None = None
    use_x_fallback = False
    last_click: dict[str, float] | None = None
    last_click_at: float | None = None
    page_click_started_at: float | None = None
    initial_url = _cf_url_key(getattr(page, "url", ""))
    previous_token_length = 0
    states: list[str] = []
    seen_cf = False
    seen_page_challenge = False
    unresolved_challenge = False
    no_cf_since: float | None = None
    first_pass = True
    reported_state: str | None = None
    challenge_reported = False
    reported_blockers: tuple[str, ...] | None = None
    clicked_page_challenge = False
    token_empty_after_click = False
    fresh_token_after_click = False
    evidence_page_cleared = False
    reported_visual_conflict = False
    last_heartbeat = time.monotonic()
    low_confidence_state: str | None = None
    low_confidence_count = 0
    low_confidence_url: str | None = None
    low_confidence_since: float | None = None

    def observe_completion_token(state):
        nonlocal clicked_page_challenge, token_empty_after_click, fresh_token_after_click
        nonlocal evidence_page_cleared, no_cf_since
        if not clicked_page_challenge:
            return
        challenge_present = state["marker"] or state["challenge_text"] or state["url_challenge"]
        if challenge_present and evidence_page_cleared:
            clicked_page_challenge = False
            token_empty_after_click = False
            fresh_token_after_click = False
            no_cf_since = None
            return
        if state["marker"] is False and not challenge_present and state["text_readable"]:
            evidence_page_cleared = True
        if not state.get("token_readable"):
            clicked_page_challenge = False
            token_empty_after_click = False
            fresh_token_after_click = False
        elif int(state.get("token_length") or 0) > 0:
            fresh_token_after_click = token_empty_after_click
        elif fresh_token_after_click:
            # Token expiry starts a new challenge; it cannot reuse the old click.
            clicked_page_challenge = False
            token_empty_after_click = False
            fresh_token_after_click = False
            no_cf_since = None

    diagnostics = diagnostics if diagnostics is not None else {}
    while time.monotonic() < deadline:
        if cancel is not None and cancel.is_set():
            return "canceled", "已取消", {}
        diagnostics["phase"] = "page_check"
        state = await _cf_page_state(page)
        diagnostics["page"] = {key: state[key] for key in ("marker", "dom_marker", "frame_challenge", "frames_checked", "frames_hidden", "frames_unreadable", "challenge_text", "url_challenge", "text_readable")}
        current_url = _cf_url_key(getattr(page, "url", ""))
        token_length = int(state.get("token_length") or 0)
        has_cf = (
            state["marker"]
            or state["challenge_text"]
            or state["url_challenge"]
        )
        if (current_url != low_confidence_url or has_cf
                or state["marker"] is None or not state["text_readable"]):
            low_confidence_state = None
            low_confidence_count = 0
            low_confidence_since = None
        progress = _cf_progress_reason(
            initial_url,
            current_url,
            token_length,
            previous_token_length,
            clicked,
        )
        previous_token_length = token_length
        if has_cf and progress == "页面已跳转":
            note(
                "INFO",
                f"检测到新的 Cloudflare 挑战（{progress}，当前地址 {current_url}），"
                "继续处理",
            )
            page_click_count = 0
            last_state = "unknown"
            last_click = None
            last_click_at = None
            use_x_fallback = False
            initial_url = current_url
            clicked_page_challenge = False
            token_empty_after_click = False
            fresh_token_after_click = False
            evidence_page_cleared = False
            no_cf_since = None
        observe_completion_token(state)
        if has_cf:
            seen_cf = True
            seen_page_challenge = True
            unresolved_challenge = True
            if not (
                last_state == "verified" and token_length > 0
                and not state["challenge_text"] and not state["url_challenge"]
            ):
                no_cf_since = None
        diagnostics.update(unresolved_challenge=unresolved_challenge, seen_page_challenge=seen_page_challenge)

        # Always consult CFbox, including when page markers have disappeared.
        if time.monotonic() < deadline:
            if has_cf and not challenge_reported:
                note("INFO", "检测到 Cloudflare 挑战")
                challenge_reported = True
            if has_cf:
                await _scroll_cf_into_view(page)
            if first_pass:
                await asyncio.sleep(0.5)
                first_pass = False
            if (
                not use_x_fallback
                and page_click_count >= 2
                and last_state == "unchecked"
                and page_click_started_at is not None
                # Measure from the first click: each click refreshes last_click_at.
                and time.monotonic() - page_click_started_at >= 4
            ):
                if lane_key not in {"interaction", "proxy_interaction"}:
                    note(
                        "WARN",
                        "Playwright 点击未生效，转交互车道重试"
                        f"（state={last_state}, token={int(state.get('token_length') or 0)}, "
                        f"url={current_url}）",
                    )
                    return (
                        "fallback_interaction",
                        "Playwright 点击未生效，转交互车道重试",
                        {
                            "state": last_state,
                            "clicked": clicked,
                            "click_count": click_count,
                            "page_click_count": page_click_count,
                            "click_mode": click_mode,
                            "states": states,
                        },
                    )
                if not xdisplay:
                    return "fail", "需要 X fallback，但车道没有 display", {}
                use_x_fallback = True
                states.append("x-fallback")
                note("INFO", "切换 mss + xdotool 方式点击 Cloudflare")
            if use_x_fallback and not xdisplay:
                return "fail", "需要 X fallback，但车道没有 display", {}
            try:
                diagnostics["phase"] = "screenshot"
                if private is not None:
                    await private.pin_challenge()
                if use_x_fallback and xdisplay:
                    image = await xdisplay.screenshot()
                    fallback_size = (SCREEN_WIDTH, SCREEN_HEIGHT)
                else:
                    image = await page.screenshot(type="png")
                    viewport = page.viewport_size or {}
                    fallback_size = (
                        int(viewport.get("width") or SCREEN_WIDTH),
                        int(viewport.get("height") or SCREEN_HEIGHT),
                    )
                width, height = _image_dimensions(image, fallback_size)
                if not image:
                    return "fail", "CF 截图为空", {}
                async with httpx.AsyncClient(timeout=20) as client:
                    diagnostics["phase"] = "cfbox_request"
                    response = await client.post(
                        f"{str(cfbox['url']).rstrip('/')}/predict",
                        headers=headers,
                        files={"image": ("screen.png", image, "image/png")},
                    )
                if response.status_code in {401, 403}:
                    return "fail", "CFbox API Key 无效", {}
                if response.status_code != 200:
                    return "fail", f"CFbox 返回 HTTP {response.status_code}", {}
                body = response.json()
                if not isinstance(body, dict):
                    return "fail", "CFbox 响应格式无效", {}
                last_state = body.get("state")
                if not isinstance(last_state, str) or last_state not in {
                    "absent", "verified", "unknown", "unchecked", "processing", "error"
                }:
                    return "fail", "CFbox 响应状态无效", {}
                prediction = resolve_cfbox_prediction(body)
                last_state = prediction["state"]
                confidence = prediction["effective_confidence"]
                allow_clear_override = prediction["allow_clear_override"]
                states.append(last_state)
                diagnostics.update(state=last_state, summary_state=prediction["summary_state"],
                                   challenge_states=prediction["challenge_states"],
                                   challenge_confidences=prediction["challenge_confidences"],
                                   low_confidence_eligible=prediction["low_confidence_eligible"],
                                   multiple_unresolved=prediction["multiple_unresolved"],
                                   confidence=confidence, predictions=len(states), clicked=clicked, click_count=click_count)
                if prediction["summary_state"] != last_state:
                    note("INFO", f"CFbox 子挑战汇总：顶层={prediction['summary_state']}，子状态={prediction['challenge_states']}，子置信度={prediction['challenge_confidences']}，有效状态={last_state}，可用复选框={'有' if prediction['center'] else '无'}，低置信度复核资格={prediction['low_confidence_eligible']}")
                if last_state != reported_state:
                    note("INFO", f"CFbox 识别状态：{last_state}，confidence={confidence}")
                    reported_state = last_state
                # Navigation or widget rendering may occur during inference.
                diagnostics["phase"] = "page_recheck"
                state = await _cf_page_state(page)
                diagnostics["page"] = {key: state[key] for key in ("marker", "dom_marker", "frame_challenge", "frames_checked", "frames_hidden", "frames_unreadable", "challenge_text", "url_challenge", "text_readable")}
                has_cf = bool(
                    state["marker"] or state["challenge_text"] or state["url_challenge"]
                )
                seen_cf = seen_cf or has_cf
                seen_page_challenge = seen_page_challenge or has_cf
                if has_cf or prediction["has_challenge_evidence"] or last_state in {"processing", "unchecked", "error"}:
                    unresolved_challenge = True
                    seen_cf = True
                ready = await page.evaluate(
                    "() => document.readyState !== 'loading' && !!document.body"
                )
                diagnostics["ready"] = bool(ready)
                page_clear = (
                    state["marker"] is False and not state["challenge_text"]
                    and not state["url_challenge"] and state["text_readable"] and ready
                )
                observed_url = _cf_url_key(getattr(page, "url", ""))
                low_confidence = last_state in {"error", "unknown"} and confidence is not None and confidence < 0.95
                if prediction["low_confidence_eligible"] and page_clear and observed_url == current_url:
                    if low_confidence_state != last_state or low_confidence_url != observed_url:
                        low_confidence_count = 0
                        low_confidence_since = time.monotonic()
                    low_confidence_state = last_state
                    low_confidence_url = observed_url
                    low_confidence_count += 1
                    if low_confidence_count <= 3:
                        note("INFO", f"CFbox 低置信度复核：state={last_state}, confidence={confidence:.4f}, 连续截图={low_confidence_count}/3；页面无可见 CF 挑战")
                else:
                    low_confidence_state = None
                    low_confidence_count = 0
                    low_confidence_since = None
                    low_confidence_url = observed_url
                low_confidence_confirmed = (
                    low_confidence_count >= 3 and low_confidence_since is not None
                    and time.monotonic() - low_confidence_since >= 2
                )
                diagnostics["low_confidence_count"] = low_confidence_count
                token_length = int(state.get("token_length") or 0)
                observe_completion_token(state)
                diagnostics.update(token_present=token_length > 0, token_readable=state.get("token_readable", False), fresh_token_after_click=fresh_token_after_click)
                if last_state == "verified":
                    seen_cf = True
                # A solved Turnstile can remain visible with its response token.
                solved_widget = (
                    last_state == "verified"
                    and allow_clear_override
                    and state.get("marker_readable", True)
                    and state.get("token_readable", False)
                    and int(state.get("token_length") or 0) > 0
                    and not state["challenge_text"]
                    and not state["url_challenge"]
                )
                visual_error_confirmed = (
                    last_state == "error" and clicked_page_challenge
                    and allow_clear_override
                    and fresh_token_after_click and token_length > 0
                    and state["marker"] is False
                    and not state["challenge_text"] and not state["url_challenge"]
                )
                if last_state == "error" and not has_cf and not reported_visual_conflict:
                    note("WARN", "CFbox 视觉状态 error，但本轮未检出页面挑战标记；继续重新截图，检查连续低置信度或本轮新 token 证据")
                    reported_visual_conflict = True
                # Unknown inference cannot erase a previously unresolved challenge.
                # Keep the legacy shortcut only for pages with no conflicting evidence.
                if (
                    (last_state in {"absent", "verified"} and allow_clear_override and page_clear)
                    or (solved_widget and state["marker"] is not None and state["text_readable"] and ready)
                    or (visual_error_confirmed and page_clear)
                    or low_confidence_confirmed
                ):
                    unresolved_challenge = False
                diagnostics.update(unresolved_challenge=unresolved_challenge, seen_page_challenge=seen_page_challenge)
                visual_resolved = low_confidence_confirmed or allow_clear_override and (
                    last_state in {"absent", "verified"}
                    or (last_state == "unknown" and not low_confidence and not unresolved_challenge)
                    or visual_error_confirmed or low_confidence_confirmed
                )
                if (
                    (not has_cf or solved_widget)
                    and state["marker"] is not None
                    and state["text_readable"]
                    and ready
                    # Unknown visual inference falls back to a readable, clear page.
                    and visual_resolved
                ):
                    diagnostics["blockers"] = []
                    reported_blockers = None
                    if no_cf_since is None:
                        no_cf_since = time.monotonic()
                    if low_confidence_confirmed or time.monotonic() - no_cf_since >= 2:
                        message = (
                            "Cloudflare 挑战未检出（连续 3 次低置信度截图，页面无可见 CF 组件）" if low_confidence_confirmed
                            else "Cloudflare 验证已通过（新 token 与页面状态确认，CFbox 视觉状态 error）" if visual_error_confirmed
                            else "Cloudflare 验证已通过" if solved_widget
                            else "Cloudflare 挑战已消失" if seen_page_challenge
                            else "Cloudflare 视觉冲突已解除，页面无可见挑战" if seen_cf
                            else "未检测到 Cloudflare 挑战"
                        )
                        note("INFO", message)
                        result = {
                            "state": "passed" if seen_cf else "absent",
                            "cfbox_state": last_state,
                            "cfbox_summary_state": prediction["summary_state"],
                            "challenge_states": prediction["challenge_states"],
                            "clicked": clicked,
                            "click_count": click_count,
                            "states": states,
                            "completion_reason": (
                                "low_confidence_and_clear_page" if low_confidence_confirmed
                                else "fresh_token_and_clear_page" if visual_error_confirmed
                                else "verified_and_token" if solved_widget
                                else "verified_and_clear_page" if last_state == "verified"
                                else "absent_and_clear_page" if last_state == "absent"
                                else "unknown_and_clear_page"
                            ),
                            "confidence": confidence,
                            "unresolved_challenge": unresolved_challenge,
                            "seen_page_challenge": seen_page_challenge,
                        }
                        if low_confidence_confirmed:
                            result.update(confidence=confidence, low_confidence_count=low_confidence_count)
                        return "success", message, result
                else:
                    no_cf_since = None
                    blockers = tuple(reason for reason, blocked in (
                        ("visible_widget", bool(state["marker"]) and not solved_widget),
                        ("challenge_text", state["challenge_text"]),
                        ("challenge_url", state["url_challenge"]),
                        ("marker_unreadable", state["marker"] is None),
                        ("text_unreadable", not state["text_readable"]),
                        ("page_not_ready", not ready),
                        ("cfbox_unresolved", not visual_resolved),
                    ) if blocked)
                    diagnostics["blockers"] = list(blockers)
                    if blockers != reported_blockers:
                        note("INFO", f"Cloudflare 等待原因：{', '.join(blockers)}（CFbox={last_state}）")
                        reported_blockers = blockers
                if time.monotonic() - last_heartbeat >= 10:
                    note("INFO", f"Cloudflare 仍在重新识别：state={last_state}, confidence={confidence}, predictions={len(states)}, low_confidence_count={low_confidence_count}, low_confidence_eligible={prediction['low_confidence_eligible']}, child_confidences={prediction['challenge_confidences']}, blockers={diagnostics['blockers']}, token_present={token_length > 0}, fresh_token_after_click={fresh_token_after_click}")
                    last_heartbeat = time.monotonic()
                if last_state not in {"absent", "verified", "unknown"}:
                    seen_cf = True
                if last_state == "absent" and has_cf:
                    last_state = "absent (but CF DOM is present)"
                center = prediction["center"]
                if last_state == "unchecked" and isinstance(center, dict):
                    x_pct = float(center.get("x") or 0)
                    y_pct = float(center.get("y") or 0)
                    if x_pct <= 1 and y_pct <= 1:
                        x_pct *= 100
                        y_pct *= 100
                    if x_pct > 0 and y_pct > 0:
                        x = width * x_pct / 100
                        y = height * y_pct / 100
                        if use_x_fallback and xdisplay:
                            await xdisplay.click(x, y)
                            x_click_count += 1
                            click_mode = "x"
                        else:
                            if private is not None:
                                await private.click_challenge(x, y)
                            else:
                                await page.mouse.click(x, y)
                            page_click_count += 1
                            if page_click_started_at is None:
                                page_click_started_at = time.monotonic()
                            click_mode = "page"
                        clicked = True
                        if has_cf and not clicked_page_challenge:
                            clicked_page_challenge = bool(state.get("token_readable")) and token_length <= 0
                            token_empty_after_click = bool(state.get("token_readable")) and token_length <= 0
                            fresh_token_after_click = False
                            evidence_page_cleared = False
                        click_count += 1
                        last_click_at = time.monotonic()
                        last_click = {"x": round(x, 1), "y": round(y, 1)}
                        note(
                            "INFO",
                            f"点击 Cloudflare 复选框 x={round(x, 1)} y={round(y, 1)} "
                            f"方式={click_mode}",
                        )
            except Exception as exc:
                return "fail", f"CFbox 识别失败: {type(exc).__name__}: {str(exc)[:300]}", {}
        diagnostics["phase"] = "poll_wait"
        if not await _sleep(1, cancel):
            return "canceled", "已取消", {}
    suffix = "，已点击复选框" if clicked else ""
    return (
        "unknown",
        f"Turnstile 未在 {wait_seconds}s 内完成（最后状态 {last_state}{suffix}）",
        {
            **diagnostics,
            "state": last_state,
            "clicked": clicked,
            "click_count": click_count,
            "page_click_count": page_click_count,
            "x_click_count": x_click_count,
            "click_mode": click_mode,
            "last_click": last_click,
            "states": states,
        },
    )


async def _run_altcha(
    page: Any,
    config: dict[str, Any],
    log: Callable[[str, str], None] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    note = log or (lambda _level, _message: None)
    widget_selectors = list(config.get("widget_selector") or [])
    checkbox_selectors = list(config.get("checkbox_selector") or [])
    token_selectors = list(config.get("token_selector") or [])
    success_selectors = list(config.get("success_selector") or [])
    try:
        wait_seconds = max(1, min(int(config.get("wait_seconds") or 30), 120))
    except (TypeError, ValueError):
        wait_seconds = 30
    note(
        "INFO",
        f"ALTCHA 组件选择器：{_short(', '.join(map(str, widget_selectors)))}；"
        f"复选框选择器：{_short(', '.join(map(str, checkbox_selectors)))}",
    )
    widget = await _find_locator(
        page,
        widget_selectors,
        wait_ms=SELECTOR_WAIT_MS,
        on_match=lambda value: note("INFO", f"ALTCHA 组件命中选择器：{value}"),
    )
    if widget is None:
        return "fail", "未找到 ALTCHA 组件", {}

    async def verified() -> tuple[bool, str | None, int]:
        state = None
        try:
            state_locator = widget.locator(".altcha").first
            if await state_locator.count():
                state = await state_locator.get_attribute("data-state")
        except Exception:
            state = None
        token_length = 0
        for selector in token_selectors:
            try:
                locator = page.locator(str(selector)).first
                if await locator.count():
                    value = await locator.input_value()
                    token_length = max(token_length, len(value or ""))
            except Exception:
                continue
        if state == "verified" or token_length > 0:
            return True, state, token_length
        for selector in success_selectors:
            if await _selector_exists(page, str(selector)):
                return True, state, token_length
        return False, state, token_length

    ok, state, token_length = await verified()
    if ok:
        note("INFO", f"ALTCHA 已处于验证通过状态（state={state or '-'}）")
        return "success", "ALTCHA 已通过", {
            "state": state,
            "token_length": token_length,
            "clicked": False,
        }

    checkbox = await _find_locator(
        page,
        checkbox_selectors,
        wait_ms=SELECTOR_WAIT_MS,
        on_match=lambda value: note("INFO", f"ALTCHA 复选框命中选择器：{value}"),
    )
    if checkbox is None:
        return "fail", "未找到 ALTCHA 验证复选框", {}
    await checkbox.click(timeout=PAGE_TIMEOUT_MS)
    note("INFO", "ALTCHA 复选框已点击，等待验证完成")

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        await asyncio.sleep(0.5)
        ok, state, token_length = await verified()
        if ok:
            note(
                "INFO",
                f"ALTCHA 验证通过（state={state or '-'}，token 长度={token_length}）",
            )
            return "success", "ALTCHA 验证通过", {
                "state": state,
                "token_length": token_length,
                "clicked": True,
            }
        if state == "error":
            return "fail", "ALTCHA 验证失败", {"state": state}
    return "fail", f"ALTCHA 验证超时（{wait_seconds}s）", {"state": state}


async def _run_slider(
    page: Any,
    config: dict[str, Any],
    cancel: asyncio.Event | None,
    log: Callable[[str, str], None] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    note = log or (lambda _level, _message: None)
    container_selectors = list(config.get("container_selector") or [])
    handle_selectors = list(config.get("handle_selector") or [])
    note(
        "INFO",
        f"滑块容器选择器：{_short(', '.join(map(str, container_selectors)))}；"
        f"手柄选择器：{_short(', '.join(map(str, handle_selectors)))}",
    )
    container = await _find_locator(
        page,
        container_selectors,
        wait_ms=SELECTOR_WAIT_MS,
        on_match=lambda value: note("INFO", f"滑块容器命中选择器：{value}"),
    )
    handle = await _find_locator(
        page,
        handle_selectors,
        wait_ms=SELECTOR_WAIT_MS,
        on_match=lambda value: note("INFO", f"滑块手柄命中选择器：{value}"),
    )
    if container is None or handle is None:
        return "fail", "未找到滑块容器或手柄", {}
    container_box = await container.bounding_box()
    handle_box = await handle.bounding_box()
    if not container_box or not handle_box:
        return "fail", "无法读取滑块位置", {}
    start_x = handle_box["x"] + handle_box["width"] / 2
    start_y = handle_box["y"] + handle_box["height"] / 2
    end_x = container_box["x"] + container_box["width"] - handle_box["width"] / 2
    note("INFO", f"拖动滑块 x={round(start_x, 1)} -> {round(end_x, 1)}")
    await page.mouse.move(start_x, start_y)
    await page.mouse.down()
    try:
        await page.mouse.move(end_x, start_y, steps=25)
    finally:
        await page.mouse.up()
    await asyncio.sleep(0.6)
    success = list(config.get("success_selector") or [])
    if success and await _find_locator(
        page,
        success,
        wait_ms=SELECTOR_WAIT_MS,
        on_match=lambda value: note("INFO", f"滑块成功元素命中选择器：{value}"),
    ) is None:
        return "unknown", "滑块拖动后未识别到成功元素", {}
    return "success", "滑块已拖动", {}


_ocr_engine: Any = None
_ocr_lock = threading.Lock()

CAPTCHA_CHARSETS = {
    "alnum": r"[A-Za-z0-9]+",
    "digits": r"[0-9]+",
    "lower": r"[a-z]+",
    "upper": r"[A-Z]+",
    "letters": r"[A-Za-z]+",
    "digits_lower": r"[a-z0-9]+",
    "digits_upper": r"[A-Z0-9]+",
}


def _captcha_format_error(text: str, charset: str, length: int) -> str | None:
    if not text:
        return "识别结果为空"
    pattern = CAPTCHA_CHARSETS.get(charset)
    if pattern is not None and re.fullmatch(pattern, text) is None:
        return f"字符集不符合 {charset}"
    if length > 0 and len(text) != length:
        return f"长度 {len(text)} 不符合期望 {length}"
    return None


def _log_captcha_result(
    log: Callable[[str, str], None],
    provider: str,
    text: str,
    confidence: float | None = None,
) -> None:
    display = str(text or "").replace("\r", "\\r").replace("\n", "\\n")[:120]
    suffix = f"，置信度={confidence:.3f}" if confidence is not None else ""
    log("INFO", f"{provider} 识别到的验证码：{display or '空'}{suffix}")


def _classify_captcha(image_bytes: bytes) -> tuple[str, float]:
    global _ocr_engine
    with _ocr_lock:
        if _ocr_engine is None:
            import ddddocr

            _ocr_engine = ddddocr.DdddOcr(show_ad=False, beta=True)
        result = _ocr_engine.classification(image_bytes, probability=True)
        if isinstance(result, dict):
            return (
                str(result.get("text") or "").strip(),
                float(result.get("confidence") or 0.0),
            )
        return str(result or "").strip(), 1.0


_nexusphp_gd5_recognizer: Any = None
_nexusphp_gd5_lock = threading.Lock()


def _recognize_nexusphp_gd5(image_bytes: bytes) -> tuple[str, int]:
    global _nexusphp_gd5_recognizer
    from io import BytesIO

    from PIL import Image

    with _nexusphp_gd5_lock:
        if _nexusphp_gd5_recognizer is None:
            from worker.nexusphp_gd5 import NexusPhpGD5Recognizer

            _nexusphp_gd5_recognizer = NexusPhpGD5Recognizer()
        with Image.open(BytesIO(image_bytes)) as image:
            text, margin = _nexusphp_gd5_recognizer.recognize(image)
    return str(text or "").strip(), int(margin)


async def _refresh_captcha_image(
    scope: Any,
    image: Any,
    refresh_selectors: list[str],
    image_selectors: list[str],
    log: Callable[[str, str], None],
    private: Any = None,
) -> tuple[Any, bytes] | None:
    try:
        old_bytes = await image.screenshot(type="png", timeout=PAGE_TIMEOUT_MS)
    except Exception:
        old_bytes = b""
    old_digest = hashlib.sha256(old_bytes).hexdigest()

    target = None
    if refresh_selectors:
        target = await _find_locator(
            scope,
            refresh_selectors,
            wait_ms=SELECTOR_WAIT_MS,
            on_match=lambda value: log("INFO", f"验证码刷新元素命中选择器：{value}"),
        )
    if target is None:
        log("INFO", "未配置验证码刷新选择器，尝试点击验证码图片")
        target = image
    try:
        if private is not None:
            if target is not image:
                target = await private.target(scope, refresh_selectors, "click")
            await private.mutate(target)
        else:
            await target.click(timeout=PAGE_TIMEOUT_MS)
        log("INFO", "已点击验证码刷新元素")
    except Exception as exc:
        log("WARN", f"刷新验证码失败：{type(exc).__name__}: {str(exc)[:200]}")
        return None

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        await scope.wait_for_timeout(300)
        refreshed = await _find_locator(scope, image_selectors, wait_ms=1000)
        if refreshed is None:
            continue
        try:
            refreshed_bytes = await refreshed.screenshot(
                type="png", timeout=PAGE_TIMEOUT_MS
            )
        except Exception:
            continue
        if (
            refreshed_bytes
            and hashlib.sha256(refreshed_bytes).hexdigest() != old_digest
        ):
            log("INFO", "验证码图片已刷新")
            return refreshed, refreshed_bytes
    log("WARN", "点击后验证码图片未变化")
    return None


class _CaptchaMethodUnavailable(RuntimeError):
    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.reason = reason


CAPTCHA_NATURAL_IMAGE_SCRIPT = """(img) => {
    if (!img || img.tagName !== 'IMG') return null;
    const width = img.naturalWidth;
    const height = img.naturalHeight;
    if (!width || !height || width > 2000 || height > 2000) return null;
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext('2d');
    if (!context) return null;
    context.drawImage(img, 0, 0, width, height);
    try {
        return {width, height, data: canvas.toDataURL('image/png')};
    } catch (_error) {
        return null;
    }
}"""


async def _capture_captcha_bytes(
    image: Any,
    method: str,
    log: Callable[[str, str], None],
) -> bytes:
    if method == "nexusphp_gd5":
        try:
            natural = await image.evaluate(CAPTCHA_NATURAL_IMAGE_SCRIPT)
        except Exception:
            natural = None
        if isinstance(natural, dict):
            data = natural.get("data")
            width = natural.get("width")
            height = natural.get("height")
            if (
                isinstance(data, str)
                and data.startswith("data:image/")
                and isinstance(width, int)
                and isinstance(height, int)
            ):
                try:
                    raw = base64.b64decode(data.split(",", 1)[1], validate=True)
                except (ValueError, TypeError):
                    raw = b""
                if raw and len(raw) <= 5 * 1024 * 1024:
                    log("INFO", f"使用验证码原图 {width}×{height}")
                    return raw
    return await image.screenshot(type="png", timeout=PAGE_TIMEOUT_MS)


CAPTCHA_METHOD_LABELS = {
    "ddddocr": "ddddocr",
    "vision": "视觉模型",
    "nexusphp_gd5": "nexusphp_gd5",
}


async def _read_captcha(
    scope: Any,
    image: Any,
    config: dict[str, Any],
    log: Callable[[str, str], None],
    vision: dict[str, Any] | None = None,
    capture: Callable[[bytes, str], Awaitable[None]] | None = None,
    plugin_runtimes: dict[str, dict[str, Any]] | None = None,
    private: Any = None,
) -> tuple[str, str, str, dict[str, Any]]:
    image_selectors = list(config.get("image_selector") or [])
    refresh_selectors = list(config.get("refresh_selector") or [])
    try:
        min_confidence = float(config.get("min_confidence", 0.95))
    except (TypeError, ValueError):
        min_confidence = 0.95
    min_confidence = max(0.0, min(1.0, min_confidence))
    charset = str(config.get("charset") or "alnum")
    try:
        expected_length = max(0, min(20, int(config.get("length") or 0)))
    except (TypeError, ValueError):
        expected_length = 0
    method = str(config.get("captcha_method") or "ddddocr")
    method_label = CAPTCHA_METHOD_LABELS.get(method, method)

    def data(
        confidence: float | None,
        refreshed: bool,
        reason: str | None = None,
        margin: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "provider": method,
            "min_confidence": min_confidence,
            "charset": charset,
            "length": expected_length,
            "refreshed": refreshed,
        }
        if confidence is not None:
            payload["confidence"] = round(confidence, 4)
        if margin is not None:
            payload["match_margin"] = margin
        if reason is not None:
            payload["reason"] = reason
        return payload

    def check_result(
        value: str,
        confidence: float | None,
        margin: int | None,
    ) -> str | None:
        if method == "ddddocr" and confidence is not None and confidence < min_confidence:
            return f"置信度不足：{confidence:.3f} < {min_confidence:.3f}"
        if method == "nexusphp_gd5" and (margin is None or margin <= 0):
            return "模板匹配不唯一，图像可能不是 NexusPHP GD 5 号字体验证码"
        issue = _captcha_format_error(value, charset, expected_length)
        if (
            issue is None
            and method == "vision"
            and any(char.isspace() for char in value)
        ):
            return "识别结果包含空白字符"
        return issue

    async def select_method(
        image_value: bytes,
    ) -> tuple[str, float | None, int | None]:
        if private is not None:
            await private.validate_origin()
        if method == "ddddocr":
            text_value, confidence = await asyncio.to_thread(
                _classify_captcha, image_value
            )
            if private is not None:
                private.register_secret(text_value)
            _log_captcha_result(log, "ddddocr", text_value, confidence)
            return text_value, confidence, None
        if method == "vision":
            if not vision or not all(
                vision.get(key) for key in ("base_url", "model", "api_key")
            ):
                raise _CaptchaMethodUnavailable(
                    "未配置视觉模型，无法使用视觉模型识别图形验证码",
                    "vision_unavailable",
                )
            if not vision.get("supports_vision", True):
                raise _CaptchaMethodUnavailable(
                    "大模型未开启视觉支持，无法使用视觉模型识别图形验证码",
                    "vision_not_supported",
                )
            from worker.vision import DEFAULT_PROMPT, recognize

            execution_settings = vision.get("execution_settings") or {}
            text_value = await asyncio.to_thread(
                recognize,
                image_value,
                vision,
                execution_settings.get("llm_prompt") or DEFAULT_PROMPT,
            )
            if private is not None:
                private.register_secret(text_value)
            _log_captcha_result(log, "视觉模型", text_value)
            return text_value, None, None
        if method == "nexusphp_gd5":
            text_value, margin = await asyncio.to_thread(
                _recognize_nexusphp_gd5, image_value
            )
            if private is not None:
                private.register_secret(text_value)
            _log_captcha_result(log, "nexusphp_gd5", text_value)
            log("INFO", f"nexusphp_gd5 模板匹配余量：{margin}")
            return text_value, None, margin
        if method.startswith("plugin."):
            parts = method.split(".", 2)
            if len(parts) < 3:
                raise _CaptchaMethodUnavailable(
                    f"无效的插件验证码方法：{method}", "unknown_method"
                )
            runtime = (plugin_runtimes or {}).get(parts[1])
            if not runtime:
                raise _CaptchaMethodUnavailable(
                    f"插件 {parts[1]} 未启用或未审批，无法识别验证码",
                    "plugin_unavailable",
                )
            from worker.plugin_client import PluginClientError, recognize_captcha, cancellable_call
            from worker.plugin_authorization import authorize

            try:
                await authorize(runtime, "captcha_methods")
                result = await cancellable_call(
                    recognize_captcha,
                    str(runtime.get("endpoint") or ""),
                    str(runtime.get("token") or ""),
                    method_key=method,
                    image=image_value,
                    charset=charset,
                    length=expected_length,
                    timeout=30.0,
                    authorize=lambda: authorize(runtime, "captcha_methods"),
                )
                await authorize(runtime, "captcha_methods")
            except (PluginClientError, RuntimeError) as exc:
                raise _CaptchaMethodUnavailable(str(exc), "plugin_failed") from exc
            text_value = str(result.get("text") or "")
            confidence_value = result.get("confidence")
            margin_value = result.get("margin")
            _log_captcha_result(log, method_label, text_value, confidence_value)
            return (
                text_value,
                float(confidence_value) if confidence_value is not None else None,
                int(margin_value) if margin_value is not None else None,
            )
        raise _CaptchaMethodUnavailable(
            f"未知的验证码识别方式：{method}",
            "unknown_method",
        )

    async def save_capture(image_value: bytes, label: str) -> None:
        if capture is None:
            return
        try:
            await capture(image_value, label)
        except Exception as exc:
            log(
                "WARN",
                f"验证码截图保存失败: {type(exc).__name__}: {str(exc)[:200]}",
            )

    try:
        image_bytes = await _capture_captcha_bytes(image, method, log)
    except Exception as exc:
        return "fail", "", f"图形验证码截图失败: {type(exc).__name__}: {str(exc)[:200]}", {}
    if not image_bytes:
        return "fail", "", "图形验证码截图为空", {}
    await save_capture(image_bytes, "验证码截图")
    try:
        text, confidence, margin = await select_method(image_bytes)
    except _CaptchaMethodUnavailable as exc:
        return "fail", "", str(exc), data(None, False, exc.reason)
    except Exception as exc:
        from worker.vision import ModelCallError, format_model_error, model_error_data

        if method == "vision":
            error = exc if isinstance(exc, ModelCallError) else ModelCallError("invalid_response", "request")
            message = f"图形验证码识别失败（{method_label}）: {format_model_error(error, vision)}"
            log("WARN", message)
            return "fail", "", message, {
                **data(None, False, "provider_failed"), **model_error_data(error, vision),
            }
        if isinstance(exc, ModelCallError):
            return "fail", "", f"图形验证码识别失败（{method_label}）", data(None, False, "provider_failed")
        return (
            "fail",
            "",
            f"图形验证码识别失败（{method_label}）: {type(exc).__name__}: {str(exc)[:200]}",
            data(None, False, "provider_failed"),
        )

    issue = check_result(text, confidence, margin)
    if issue is not None:
        log("WARN", f"{method_label} 验证码识别结果不可用：{issue}")
        refreshed_image = await _refresh_captcha_image(
            scope, image, refresh_selectors, image_selectors, log,
            **({"private": private} if private is not None else {}),
        )
        if refreshed_image is None:
            return (
                "restart",
                "",
                "图形验证码识别结果不可用，且没有可用的刷新方式",
                data(confidence, False, issue, margin),
            )
        image, image_bytes = refreshed_image
        await save_capture(image_bytes, "验证码截图（刷新后）")
        try:
            text, confidence, margin = await select_method(image_bytes)
        except _CaptchaMethodUnavailable as exc:
            return "fail", "", str(exc), data(None, True, exc.reason)
        except Exception as exc:
            from worker.vision import ModelCallError, format_model_error, model_error_data

            if method == "vision":
                error = exc if isinstance(exc, ModelCallError) else ModelCallError("invalid_response", "request")
                message = f"刷新后图形验证码识别失败（{method_label}）: {format_model_error(error, vision)}"
                log("WARN", message)
                return "fail", "", message, {
                    **data(None, True, "provider_failed"), **model_error_data(error, vision),
                }
            if isinstance(exc, ModelCallError):
                return "fail", "", f"刷新后图形验证码识别失败（{method_label}）", data(None, True, "provider_failed")
            return (
                "fail",
                "",
                f"刷新后图形验证码识别失败（{method_label}）: {type(exc).__name__}: {str(exc)[:200]}",
                data(None, True, "provider_failed"),
            )
        issue = check_result(text, confidence, margin)
        if issue is not None:
            return (
                "restart",
                "",
                f"刷新后图形验证码识别结果仍不可用：{issue}",
                data(confidence, True, issue, margin),
            )
        return "ok", text, "", data(confidence, True, margin=margin)

    return "ok", text, "", data(confidence, False, margin=margin)


async def _run_image_captcha(
    page: Any,
    config: dict[str, Any],
    log: Callable[[str, str], None] | None = None,
    vision: dict[str, Any] | None = None,
    capture: Callable[[bytes, str], Awaitable[None]] | None = None,
    plugin_runtimes: dict[str, dict[str, Any]] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    note = log or (lambda _level, _message: None)
    image_selectors = list(config.get("image_selector") or [])
    note("INFO", f"验证码图片选择器：{_short(', '.join(map(str, image_selectors)))}")
    image = await _find_locator(
        page,
        image_selectors,
        wait_ms=SELECTOR_WAIT_MS,
        on_match=lambda value: note("INFO", f"验证码图片命中选择器：{value}"),
    )
    if image is None:
        return "fail", "未找到图形验证码图片", {}
    status, text, message, data = await _read_captcha(
        page, image, config, note, vision, capture, plugin_runtimes
    )
    if status != "ok":
        return status, message, data
    input_box = await _find_locator(
        page,
        list(config.get("input_selector") or []),
        wait_ms=SELECTOR_WAIT_MS,
        on_match=lambda value: note("INFO", f"验证码输入框命中选择器：{value}"),
    )
    if input_box is None:
        return "fail", "未找到图形验证码输入框", {}
    await input_box.fill(text, timeout=PAGE_TIMEOUT_MS)
    submit_selectors = list(config.get("submit_selector") or [])
    if not submit_selectors:
        return "success", "图形验证码已识别并填写", {
            **data,
            "text_length": len(text),
            "submitted": False,
        }
    submit = await _find_locator(
        page,
        submit_selectors,
        wait_ms=SELECTOR_WAIT_MS,
        on_match=lambda value: note("INFO", f"验证码提交按钮命中选择器：{value}"),
    )
    if submit is None:
        return "fail", "未找到图形验证码提交按钮", {}
    await submit.click(timeout=PAGE_TIMEOUT_MS)
    return "success", "图形验证码已识别并提交", {
        **data,
        "text_length": len(text),
        "submitted": True,
    }


async def _run_iframe_image_captcha(
    page: Any,
    config: dict[str, Any],
    log: Callable[[str, str], None] | None = None,
    vision: dict[str, Any] | None = None,
    capture: Callable[[bytes, str], Awaitable[None]] | None = None,
    plugin_runtimes: dict[str, dict[str, Any]] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    note = log or (lambda _level, _message: None)
    iframe_selectors = list(config.get("iframe_selector") or [])
    note(
        "INFO",
        f"签到 iframe 选择器：{_short(', '.join(map(str, iframe_selectors)))}",
    )
    frame = await _find_frame(page, iframe_selectors)
    if frame is None:
        return "fail", "未找到签到 iframe", {}
    note("INFO", "签到 iframe 已定位")
    for selector, value in (config.get("fields") or {}).items():
        note("INFO", f"填写 iframe 字段：{_short(selector)}")
        locator = await _find_locator(
            frame,
            [str(selector)],
            wait_ms=SELECTOR_WAIT_MS,
            on_match=lambda value: note("INFO", f"iframe 字段命中选择器：{value}"),
        )
        if locator is None:
            return "fail", f"iframe 字段不存在: {selector}", {}
        await locator.fill(str(value), timeout=PAGE_TIMEOUT_MS)

    image_selectors = list(config.get("image_selector") or [])
    note("INFO", f"iframe 验证码图片选择器：{_short(', '.join(map(str, image_selectors)))}")
    image = await _find_locator(
        frame,
        image_selectors,
        wait_ms=SELECTOR_WAIT_MS,
        on_match=lambda value: note("INFO", f"iframe 验证码图片命中选择器：{value}"),
    )
    if image is None:
        return "fail", "未找到 iframe 图形验证码图片", {}
    status, text, message, data = await _read_captcha(
        frame, image, config, note, vision, capture, plugin_runtimes
    )
    if status != "ok":
        return status, message, data
    input_box = await _find_locator(
        frame,
        list(config.get("input_selector") or []),
        wait_ms=SELECTOR_WAIT_MS,
        on_match=lambda value: note("INFO", f"iframe 验证码输入框命中选择器：{value}"),
    )
    if input_box is None:
        return "fail", "未找到 iframe 图形验证码输入框", {}
    await input_box.fill(text, timeout=PAGE_TIMEOUT_MS)
    submit_selectors = list(config.get("submit_selector") or [])
    submit = await _find_locator(
        frame,
        submit_selectors,
        wait_ms=SELECTOR_WAIT_MS,
        on_match=lambda value: note("INFO", f"iframe 提交按钮命中选择器：{value}"),
    )
    if submit is None:
        return "fail", "iframe 提交按钮不存在", {}
    await submit.click(timeout=PAGE_TIMEOUT_MS)
    note("INFO", "iframe 验证码签到表单已提交")
    await asyncio.sleep(1)
    return "success", "iframe 验证码签到表单已提交", {
        **data,
        "text_length": len(text),
        "submitted": True,
        "url": page.url,
    }


async def _run_flow_jump(
    page: Any,
    config: dict[str, Any],
    log: Callable[[str, str], None] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    note = log or (lambda _level, _message: None)
    texts = [str(item).strip() for item in (config.get("texts") or []) if str(item).strip()]
    selectors = [
        str(item).strip()
        for item in (config.get("selectors") or [])
        if str(item).strip()
    ]
    matched_by: list[str] = []
    if config.get("match_mode") == "always":
        matched_by.append("always")
        texts = []
        selectors = []
    if texts:
        page_text = await _page_text(page)
        for text in texts:
            if text.casefold() in page_text:
                matched_by.append(f"text:{text}")
    for selector in selectors:
        if await _selector_exists(page, selector):
            matched_by.append(f"selector:{selector}")
    action = str(config.get("action") or "jump")
    target = str(config.get("target_step_key") or "").strip()
    data = {
        "matched": bool(matched_by),
        "matched_by": matched_by,
        "action": action,
        "target_step_key": target or None,
    }
    if not matched_by:
        note("INFO", "条件跳转未命中")
        return "success", "未命中跳转条件", data
    note("INFO", f"条件跳转命中：{_short(', '.join(matched_by))}")
    if action == "restart":
        return "restart", "命中条件，重新开始任务", data
    return "success", f"命中条件，跳转到 {target}", data


async def _quiz_question_text(
    page: Any,
    selectors: list[str],
    log: Callable[[str, str], None],
) -> str:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count() and await locator.is_visible():
                text = (await locator.inner_text()).strip()
                if text:
                    log("INFO", f"图片答题题目命中选择器：{_short(selector)}")
                    return text[:2000]
        except Exception:
            continue
    return ""


async def _read_quiz_options(
    page: Any,
    selectors: list[str],
    log: Callable[[str, str], None],
) -> tuple[str | None, list[tuple[str, Any, bool]]]:
    for selector in selectors:
        rows: list[tuple[str, Any, bool]] = []
        try:
            locator = page.locator(selector)
            count = await locator.count()
        except Exception:
            continue
        for index in range(min(count, 50)):
            item = locator.nth(index)
            try:
                if not await item.is_visible():
                    continue
                tag = str(
                    await item.evaluate("element => element.tagName.toLowerCase()")
                ).lower()
                input_type = ""
                if tag == "input":
                    input_type = str(
                        await item.get_attribute("type") or ""
                    ).lower()
                value = (await item.input_value()).strip() if tag == "input" else (
                    await item.inner_text()
                ).strip()
                if not value:
                    value = (await item.text_content() or "").strip()
            except Exception:
                continue
            if value:
                rows.append((value, item, tag == "input" and input_type == "submit"))
        if rows:
            log(
                "INFO",
                f"图片答题选项命中选择器：{_short(selector)}，共 {len(rows)} 项",
            )
            return selector, rows
    return None, []


async def _read_quiz_form(
    page: Any,
    config: dict[str, Any],
    log: Callable[[str, str], None],
) -> tuple[dict[str, Any] | None, str]:
    question = await _quiz_question_text(
        page, list(config.get("question_selector") or []), log
    )
    answer_selector, options = await _read_quiz_options(
        page, list(config.get("answer_selector") or []), log
    )
    if answer_selector is None:
        return None, "未找到图片答题选项"
    image = await _find_locator(
        page,
        list(config.get("image_selector") or []),
        wait_ms=SELECTOR_WAIT_MS,
        on_match=lambda value: log("INFO", f"图片答题图片命中选择器：{_short(value)}"),
    )
    if image is None:
        return None, "未找到图片答题图片"
    try:
        source_before = await image.get_attribute("src")
        image_bytes = await image.screenshot(type="png", timeout=PAGE_TIMEOUT_MS)
        source_after = await image.get_attribute("src")
    except Exception as exc:
        return None, f"图片答题截图失败: {type(exc).__name__}: {str(exc)[:200]}"
    if not image_bytes:
        return None, "图片答题截图为空"
    if source_before and source_after and source_before != source_after:
        return None, "图片答题图片在读取过程中发生变化"
    values = [row[0] for row in options]
    _, confirmed = await _read_quiz_options(
        page, [answer_selector], log
    )
    if [row[0] for row in confirmed] != values:
        return None, "图片答题选项在读取过程中发生变化"
    return {
        "question": question,
        "options": values,
        "option_rows": options,
        "answer_selector": answer_selector,
        "image": image_bytes,
    }, ""


async def _wait_quiz_success(
    page: Any,
    config: dict[str, Any],
    answer_selector: str,
    selected_is_submit: bool,
    log: Callable[[str, str], None],
) -> tuple[bool, str]:
    signals = [str(item) for item in (config.get("success_signals") or []) if str(item)]
    appears = [
        str(item) for item in (config.get("success_selector") or []) if str(item)
    ]
    missing = [
        str(item) for item in (config.get("success_when_missing") or []) if str(item)
    ]
    try:
        wait_seconds = max(1, min(int(config.get("success_wait_seconds") or 10), 60))
    except (TypeError, ValueError):
        wait_seconds = 10
    if not signals and not appears and not missing:
        if not selected_is_submit:
            return True, "未配置成功条件"
        deadline = time.monotonic() + min(wait_seconds, 5)
        while True:
            if not await _selector_exists(page, answer_selector):
                return True, f"missing:{answer_selector}"
            if time.monotonic() >= deadline:
                return False, "答题提交后选项仍然存在"
            await asyncio.sleep(0.5)
    deadline = time.monotonic() + wait_seconds
    while True:
        if signals and await _check_success_text(page, signals, log):
            return True, "text"
        for selector in appears:
            if await _selector_exists(page, selector):
                log("INFO", f"图片答题成功元素命中：{_short(selector)}")
                return True, f"selector:{selector}"
        for selector in missing:
            if not await _selector_exists(page, selector):
                log("INFO", f"图片答题成功后元素已消失：{_short(selector)}")
                return True, f"missing:{selector}"
        if time.monotonic() >= deadline:
            return False, "success_timeout"
        await asyncio.sleep(0.5)


async def _refresh_quiz(
    page: Any,
    config: dict[str, Any],
    log: Callable[[str, str], None],
) -> bool:
    selectors = list(config.get("refresh_selector") or [])
    if not selectors:
        return False
    refresh = await _find_locator(
        page,
        selectors,
        wait_ms=SELECTOR_WAIT_MS,
        on_match=lambda value: log("INFO", f"图片答题刷新命中选择器：{_short(value)}"),
    )
    if refresh is None:
        return False
    try:
        await refresh.click(timeout=PAGE_TIMEOUT_MS)
        log("INFO", "已点击图片答题刷新")
        await page.wait_for_timeout(2000)
        return True
    except Exception as exc:
        log("WARN", f"刷新图片答题失败：{type(exc).__name__}: {str(exc)[:200]}")
        return False


async def _run_image_quiz(
    page: Any,
    config: dict[str, Any],
    log: Callable[[str, str], None],
    vision: dict[str, Any] | None,
    capture: Callable[[bytes, str], Awaitable[None]] | None = None,
    private: Any = None,
) -> tuple[str, str, dict[str, Any]]:
    if not vision or not all(
        vision.get(key) for key in ("base_url", "model", "api_key")
    ):
        return (
            "waiting_human",
            "未配置视觉模型，图片答题需要人工处理",
            {"reason": "vision_unavailable"},
        )
    if not vision.get("supports_vision", True):
        return (
            "waiting_human",
            "大模型未开启视觉支持，图片答题需要人工处理",
            {"reason": "vision_not_supported"},
        )
    custom_prompt = str(
        (vision.get("execution_settings") or {}).get("llm_prompt") or ""
    ).strip() or None
    try:
        max_refresh = max(0, min(int(config.get("max_refresh") or 0), 10))
    except (TypeError, ValueError):
        max_refresh = 3
    for attempt in range(max_refresh + 1):
        form, error = await _read_quiz_form(page, config, log)
        if form is None:
            return "unknown", error, {"url": page.url}
        private_rows = []
        if private is not None:
            # Pin all choices before provider inference; never rebind a selected answer.
            for _, locator, _ in form["option_rows"]:
                handle = await locator.element_handle()
                original_form = await handle.evaluate_handle("el => el.form || null")
                private.targets.append((locator, handle, original_form, "choice"))
                await private.validate_target(handle)
                private_rows.append(handle)
        if capture is not None:
            try:
                await capture(form["image"], "图片答题截图")
            except Exception as exc:
                log(
                    "WARN",
                    f"图片答题截图保存失败: {type(exc).__name__}: {str(exc)[:200]}",
                )
        from worker.vision import (
            ImageQuizUnclear, ModelCallError, answer_image_quiz,
            format_model_error, model_error_data,
        )

        log(
            "INFO",
            f"图片答题第 {attempt + 1}/{max_refresh + 1} 次，"
            f"共 {len(form['options'])} 个选项",
        )
        try:
            selected, reason = await asyncio.to_thread(
                answer_image_quiz,
                form["image"],
                form["question"],
                form["options"],
                vision,
                custom_prompt,
            )
            if private is not None:
                if type(selected) is not int or not 1 <= selected <= len(form["options"]):
                    raise ValueError()
                private.register_secret(form["options"][selected - 1])
            log(
                "INFO",
                f"视觉模型选择第 {selected} 项：{_short(form['options'][selected - 1])}",
            )
            if reason:
                log("INFO", f"图片答题依据：{_short(reason)}")
        except ImageQuizUnclear as exc:
            log("WARN", f"视觉模型无法确定图片答题答案：{_short(str(exc))}")
            if attempt >= max_refresh:
                return (
                    "waiting_human",
                    "视觉模型多次无法确定图片答题答案，需要人工处理",
                    {"reason": "vision_unclear", "attempts": attempt + 1},
                )
            if private is not None:
                if not config.get("refresh_selector"):
                    return "waiting_human", "", {}
                refresh = await private.target(page, config["refresh_selector"], "click")
                await private.mutate(refresh)
                await page.wait_for_timeout(2000)
            elif not await _refresh_quiz(page, config, log):
                return (
                    "waiting_human",
                    "视觉模型无法确定图片答题答案，且无法刷新题目，需要人工处理",
                    {"reason": "refresh_unavailable", "attempts": attempt + 1},
                )
            continue
        except ValueError as exc:
            error = exc if isinstance(exc, ModelCallError) else ModelCallError("invalid_response", "request")
            message = f"视觉模型调用失败（{format_model_error(error, vision)}），图片答题需要人工处理"
            log("WARN", message)
            return (
                "waiting_human",
                message,
                {"reason": "vision_failed", "attempts": attempt + 1,
                 **model_error_data(error, vision)},
            )
        message_text = str(config.get("message_text") or "")
        message_selectors = list(config.get("message_selector") or [])
        if message_text and message_selectors:
            message = await _find_locator(
                page,
                message_selectors,
                wait_ms=SELECTOR_WAIT_MS,
                on_match=lambda value: log(
                    "INFO", f"图片答题留言框命中选择器：{_short(value)}"
                ),
            )
            if message is None:
                log("WARN", "未找到图片答题留言框，继续提交答案")
            else:
                if private is not None:
                    target = await private.target(page, message_selectors, "fill")
                    await private.mutate(target, value=message_text)
                else:
                    await message.fill(message_text, timeout=PAGE_TIMEOUT_MS)
                log("INFO", "已填写图片答题留言")
        selected_row = form["option_rows"][selected - 1]
        try:
            if private is not None:
                target = private_rows[selected - 1]
                current = (await target.input_value() if await target.evaluate("el => el.tagName === 'INPUT'")
                           else await target.inner_text()).strip()
                if current != selected_row[0]:
                    raise RuntimeError("Private answer changed")
                await private.mutate(private_rows[selected - 1])
            else:
                await selected_row[1].click(timeout=PAGE_TIMEOUT_MS)
        except Exception as exc:
            return (
                "unknown",
                f"点击图片答题选项失败: {type(exc).__name__}: {str(exc)[:200]}",
                {"url": page.url},
            )
        submit_selectors = list(config.get("submit_selector") or [])
        if submit_selectors:
            submit = await _find_locator(
                page,
                submit_selectors,
                wait_ms=SELECTOR_WAIT_MS,
                on_match=lambda value: log(
                    "INFO", f"图片答题提交按钮命中选择器：{_short(value)}"
                ),
            )
            if submit is not None:
                try:
                    if private is not None:
                        target = await private.target(page, submit_selectors, "submit")
                        await private.mutate(target)
                    else:
                        await submit.click(timeout=PAGE_TIMEOUT_MS)
                except Exception as exc:
                    return (
                        "unknown",
                        f"点击图片答题提交按钮失败: {type(exc).__name__}: {str(exc)[:200]}",
                        {"url": page.url},
                    )
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        ok, success_by = await _wait_quiz_success(
            page,
            config,
            form["answer_selector"],
            bool(selected_row[2]),
            log,
        )
        if ok:
            return "success", "图片答题签到已提交", {
                "url": page.url,
                "selected_index": selected,
                "selected_option": selected_row[0],
                "success_by": success_by,
                "attempts": attempt + 1,
            }
        return "unknown", f"未识别到图片答题成功结果：{success_by}", {
            "url": page.url,
            "selected_index": selected,
            "attempts": attempt + 1,
        }
    return "waiting_human", "图片答题需要人工处理", {"reason": "unknown"}


async def _wait_unit3d_success(
    page: Any,
    config: dict[str, Any],
    log: Callable[[str, str], None],
) -> tuple[bool, str]:
    signals = [str(item) for item in (config.get("success_signals") or []) if str(item)]
    appears = [
        str(item) for item in (config.get("success_selector") or []) if str(item)
    ]
    missing = [
        str(item) for item in (config.get("success_when_missing") or []) if str(item)
    ]
    if not signals and not appears and not missing:
        return True, "未配置成功条件"
    try:
        wait_seconds = max(1, min(int(config.get("success_wait_seconds") or 10), 60))
    except (TypeError, ValueError):
        wait_seconds = 10
    deadline = time.monotonic() + wait_seconds
    while True:
        if signals and await _check_success_text(page, signals, log):
            return True, "text"
        for selector in appears:
            if await _selector_exists(page, selector):
                log("INFO", f"领取成功元素命中：{_short(selector)}")
                return True, f"selector:{selector}"
        for selector in missing:
            if not await _selector_exists(page, selector):
                log("INFO", f"领取成功后元素已消失：{_short(selector)}")
                return True, f"missing:{selector}"
        if time.monotonic() >= deadline:
            return False, "success_timeout"
        await asyncio.sleep(0.5)


async def _check_success_text(
    page: Any,
    values: list[str],
    log: Callable[[str, str], None] | None = None,
) -> bool:
    if not values:
        return True
    try:
        text = (await page.inner_text("body")).casefold()
    except Exception:
        if log is not None:
            log("WARN", "成功信号检查失败：无法读取页面文本")
        return False
    for value in values:
        if str(value).casefold() in text:
            if log is not None:
                log("INFO", f'成功信号命中文本 "{_short(value)}"')
            return True
    if log is not None:
        log(
            "INFO",
            "成功信号未命中，候选："
            + "，".join(f'"{_short(value)}"' for value in values),
        )
    return False


async def _wait_until(page: Any, value: str) -> str:
    return value if value in WAIT_UNTIL else "domcontentloaded"


def _normalize_navigation_url(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return text
    if not parts.scheme or not parts.netloc:
        return text
    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").lower()
    port = parts.port
    if port is not None and (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        port = None
    netloc = hostname
    if parts.username:
        credentials = parts.username
        if parts.password:
            credentials += f":{parts.password}"
        netloc = f"{credentials}@{netloc}"
    if port is not None:
        netloc = f"{netloc}:{port}"
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((scheme, netloc, path, query, parts.fragment))


def _same_navigation_url(current: str, target: str) -> bool:
    current_url = _normalize_navigation_url(current)
    target_url = _normalize_navigation_url(target)
    return bool(current_url and target_url and current_url == target_url)


def _retryable_navigation_error(exc: Exception) -> bool:
    message = str(exc)
    return any(
        token in message
        for token in (
            "net::ERR_",
            "Timeout",
            "ERR_HTTP_RESPONSE_CODE_FAILURE",
            "ERR_CONNECTION",
            "ERR_NAME_NOT_RESOLVED",
        )
    )


def _transient_navigation_error(exc: Exception) -> bool:
    # Only classify the failure prefix, never URLs or page text in the call log.
    match = re.match(r"^(?:Page\.goto:\s*)?net::(ERR_[A-Z0-9_]+)(?=\s|$)", str(exc))
    return match is not None and match.group(1) in {
        "ERR_CONNECTION_CLOSED", "ERR_CONNECTION_RESET", "ERR_CONNECTION_ABORTED",
        "ERR_TIMED_OUT", "ERR_CONNECTION_TIMED_OUT",
    }


async def _export_site_cookies(
    context: Any,
    base_url: str,
    seed_cookies: list[dict] | None,
    log: Callable[[str, str], None],
) -> list[dict] | None:
    if not base_url:
        return None
    urls: set[str] = {base_url}
    scheme = urlsplit(base_url).scheme or "https"
    for cookie in seed_cookies or []:
        if not isinstance(cookie, dict):
            continue
        url = str(cookie.get("url") or "").strip()
        if url:
            urls.add(url)
            continue
        domain = str(cookie.get("domain") or "").strip().lstrip(".")
        if domain:
            urls.add(f"{scheme}://{domain}/")
    try:
        cookies = await context.cookies(sorted(urls))
    except Exception:
        log("WARN", "cookie_export_failed")
        return None
    if not isinstance(cookies, list):
        return None
    log("INFO", f"导出站点 Cookie {len(cookies)} 条")
    return cookies


async def _detect_page_error(
    page: Any,
    log: Callable[[str, str], None] | None = None,
) -> str | None:
    title = ""
    body = ""
    try:
        title = str(await page.title() or "")
    except Exception:
        pass
    try:
        body = str(await page.inner_text("body") or "")
    except Exception:
        pass
    combined = f"{title}\n{body}".casefold()
    reason = ""
    for marker in PAGE_ERROR_MARKERS:
        if marker in combined:
            reason = marker
            break
    if not reason and title and PAGE_ERROR_TITLE_RE.search(title):
        reason = f"页面标题显示 {_short(title, 80)}"
    if not reason:
        return None
    message = f"站点错误页面：{reason}"
    if log is not None:
        log(
            "WARN",
            f"{message}（标题={_short(title, 80) or '-'}，"
            f"正文前 160 字={_short(body, 160) or '-'}）",
        )
    return reason


class _NavigationPageError(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(f"站点错误页面：{reason}")
        self.reason = reason


async def _goto_if_needed(
    page: Any,
    url: str,
    wait_until: str,
    log: Callable[[str, str], None],
    label: str,
    *,
    retry_count: int = 0,
    retry_interval_seconds: float = 1.0,
    check_error_page: bool = False,
    transient_only: bool = False,
) -> bool:
    current = str(getattr(page, "url", "") or "")
    skip_navigation = _same_navigation_url(current, url)
    if skip_navigation and not check_error_page:
        log("INFO", f"当前页面已是目标地址，跳过{label}导航：{current}")
        return False
    retry_count = max(0, min(int(retry_count), 20))
    attempts = retry_count + 1
    log("INFO", f"{label}访问 {url}（最多尝试 {attempts} 次）")
    cache_session = None
    try:
        for attempt in range(1, attempts + 1):
            try:
                if attempt == 1 and skip_navigation:
                    log("INFO", f"当前页面已是目标地址，检查页面状态：{current}")
                else:
                    await page.goto(url, wait_until=wait_until, timeout=PAGE_TIMEOUT_MS)
                    log("INFO", f"页面加载完成，当前地址 {page.url}")
                if check_error_page:
                    reason = await _detect_page_error(page, log)
                    if reason:
                        raise _NavigationPageError(reason)
                return not (attempt == 1 and skip_navigation)
            except Exception as exc:
                error_page = isinstance(exc, _NavigationPageError)
                retryable = (not error_page and _transient_navigation_error(exc) if transient_only
                             else error_page or _retryable_navigation_error(exc))
                if attempt >= attempts or not retryable:
                    log(
                        "ERROR",
                        f"{label}打开失败（第 {attempt}/{attempts} 次）："
                        f"{type(exc).__name__}: {exc}",
                    )
                    raise
                if error_page and cache_session is None:
                    # Scope cache bypass to this task's target, not the shared
                    # profile; never clear another task's cookies or cache.
                    cache_session = await page.context.new_cdp_session(page)
                    await cache_session.send("Network.enable")
                    await cache_session.send("Network.setCacheDisabled", {"cacheDisabled": True})
                    log("INFO", "检测到站点错误页，已临时禁用当前标签页缓存，后续尝试强制重新访问")
                log(
                    "WARN",
                    f"{label}打开失败（第 {attempt}/{attempts} 次）："
                    f"{type(exc).__name__}: {str(exc)[:300]}；"
                    f"{retry_interval_seconds:g} 秒后重试",
                )
                await asyncio.sleep(max(0.0, retry_interval_seconds))
    finally:
        if cache_session is not None:
            try:
                await cache_session.send("Network.setCacheDisabled", {"cacheDisabled": False})
            except Exception:
                log("WARN", "恢复当前标签页缓存设置失败")
            finally:
                try:
                    await cache_session.detach()
                except Exception:
                    pass
    return False


def _regex_number(value: str | None, pattern: str, default: int | None = None) -> int | None:
    if not value:
        return default
    try:
        match = re.search(pattern, value)
    except re.error:
        return default
    if not match:
        return default
    try:
        return int(match.group(1).replace(",", ""))
    except (IndexError, ValueError):
        return default


def _regex_float(value: str | None, pattern: str) -> float | None:
    if not value:
        return None
    try:
        match = re.search(pattern, value)
    except re.error:
        return None
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except (IndexError, ValueError):
        return None


async def _page_body(page: Any) -> str:
    try:
        return await page.inner_text("body")
    except Exception:
        return ""


def _month_label(value: str) -> str:
    match = re.search(r"(\d{4})\D+(\d{1,2})", value or "")
    if not match:
        return ""
    return f"{match.group(1)}-{int(match.group(2)):02d}"


async def _navigate_fc_month(
    page: Any,
    target_month: str,
    config: dict[str, Any],
    log: Callable[[str, str], None],
) -> bool:
    title_selector = str(config.get("calendar_title_selector") or ".fc-toolbar-title")
    prev_selector = str(config.get("calendar_prev_selector") or ".fc-prev-button")
    next_selector = str(config.get("calendar_next_selector") or ".fc-next-button")
    try:
        current_text = (await page.locator(title_selector).first.inner_text()).strip()
    except Exception:
        log("WARN", f"无法读取日历标题：{title_selector}")
        return False
    current_month = _month_label(current_text)
    if not current_month:
        log("WARN", f"无法解析日历月份：{current_text}")
        return False
    if current_month == target_month:
        return True
    selector = prev_selector if target_month < current_month else next_selector
    for _ in range(12):
        try:
            await page.locator(selector).first.click(timeout=PAGE_TIMEOUT_MS)
        except Exception as exc:
            log("WARN", f"翻月点击失败：{type(exc).__name__}: {exc}")
            return False
        await asyncio.sleep(0.8)
        try:
            current_text = (await page.locator(title_selector).first.inner_text()).strip()
        except Exception:
            continue
        current_month = _month_label(current_text)
        if current_month == target_month:
            log("INFO", f"日历翻到 {target_month}")
            return True
    log("WARN", f"无法翻月到 {target_month}，当前 {current_month or current_text}")
    return False


async def _calendar_day_status(page: Any, date_value: str, config: dict[str, Any]) -> str:
    stamp = datetime.strptime(date_value, "%Y-%m-%d")
    label = f"{stamp.year}年{stamp.month}月{stamp.day}日"
    try:
        return str(
            await page.evaluate(
                """(args) => {
                    const days = Array.from(document.querySelectorAll(args.daySelector));
                    const cell = days.find((el) => {
                        if (el.getAttribute('data-date') === args.date) return true;
                        const num = el.querySelector(args.numberSelector);
                        const aria = num ? (num.getAttribute('aria-label') || '') : '';
                        const text = num ? (num.textContent || '').trim() : '';
                        return aria.includes(args.label) || aria.includes(args.date)
                            || text === String(args.day);
                    });
                    if (!cell) return 'not_found';
                    let block = false;
                    let dot = false;
                    cell.querySelectorAll(args.eventSelector).forEach((event) => {
                        if (event.matches(args.blockSelector)) block = true;
                        if (event.matches(args.dotSelector)) dot = true;
                    });
                    return block ? 'signed' : (dot ? 'missed' : 'empty');
                }""",
                {
                    "daySelector": str(
                        config.get("calendar_day_selector") or ".fc-daygrid-day"
                    ),
                    "numberSelector": str(
                        config.get("calendar_day_number_selector")
                        or ".fc-daygrid-day-number"
                    ),
                    "eventSelector": str(
                        config.get("calendar_event_selector")
                        or ".fc-daygrid-event-harness a"
                    ),
                    "blockSelector": str(
                        config.get("calendar_block_selector") or ".fc-daygrid-block-event"
                    ),
                    "dotSelector": str(
                        config.get("calendar_dot_selector") or ".fc-daygrid-dot-event"
                    ),
                    "date": date_value,
                    "label": label,
                    "day": stamp.day,
                },
            )
        )
    except Exception as exc:
        return f"error:{type(exc).__name__}: {exc}"


def _buy_amount(text: str, pattern: str) -> float | None:
    try:
        match = re.search(pattern, text)
        if match is None:
            return None
        value = float(match.group(1).replace(",", ""))
        # Custom/default regexes may start at the digits after a spaced minus.
        if re.search(r"-\s*$", text[:match.start(1)]):
            value = -abs(value)
        return value if math.isfinite(value) else None
    except (re.error, IndexError, TypeError, ValueError, OverflowError):
        return None


async def _buy_page_ready(page: Any) -> bool:
    try:
        return await page.evaluate("""() => {
            if (document.readyState === 'loading' || !document.body) return false;
            return !Array.from(document.querySelectorAll('[aria-busy="true"], .loading, .spinner'))
                .some(el => el.getClientRects().length > 0 &&
                    !['hidden', 'collapse'].includes(getComputedStyle(el).visibility));
        }""") is True
    except Exception:
        return False


async def _extract_card_info(
    page: Any,
    config: dict[str, Any],
    log: Callable[[str, str], None],
    *,
    product: Any = None,
) -> dict[str, Any] | None:
    retained = product is not None
    product = product if retained else await _buy_product(page, config)
    if product is None:
        return None
    try:
        if retained:
            current = await _buy_product(page, config, require_unique_visible=True)
            if current is not None:
                product = current
                retained = False
        if retained and await product.evaluate("""(el, args) => el.isConnected &&
            args.headings.some(selector => Array.from(el.querySelectorAll(selector)).some(
                heading => args.titles.some(title => heading.textContent.includes(title))))""",
            {"headings": config.get("title_selectors") or
             ["h1", "h2", "h3", ".magic-card-header", ".card-title"],
             "titles": config.get("card_title") or ["购买补签卡"]}) is not True:
            return None
        include_hidden = retained and not await product.is_visible()
        selectors = config.get("price_selectors") or []
        if selectors:
            text = await _buy_text(product, selectors, unique=True, include_hidden=include_hidden)
        elif config.get("layout", "table") == "table":
            cells = await product.query_selector_all("td")
            index = int(config.get("price_column_index", 2))
            cell = cells[index] if 0 <= index < len(cells) else None
            text = ""
            if cell and (include_hidden or await cell.is_visible()):
                text = (await cell.text_content() or "") if include_hidden else await cell.inner_text()
        else:
            text = ""
        price = _buy_amount(text, str(config.get("price_regex") or
                                     r"([+-]?[0-9][0-9,]*(?:\.[0-9]+)?)"))
        if price is None or not math.isfinite(price) or price <= 0:
            return None
        return {"price": price}
    except Exception as exc:
        log("WARN", f"读取商店价格失败：{type(exc).__name__}: {exc}")
        return None
    finally:
        if not retained:
            await product.dispose()


async def _buy_product(
    page: Any, config: dict[str, Any], *, require_unique_visible: bool = False,
) -> Any:
    layout = config.get("layout", "table")
    selectors = config.get("product_selectors") or {
        "table": ["table tr"], "grid": ["form.magic-card", "section article", "article"],
    }.get(layout, [])
    if layout not in {"table", "grid", "custom"} or not selectors:
        return None
    handle = await page.evaluate_handle(
        """args => {
            const visible = el => !!el.getClientRects().length &&
                getComputedStyle(el).visibility !== 'hidden';
            if (args.requireUniqueVisible) {
                // A new confirmation product must not be masked by a retained quote.
                const scopes = new Set();
                for (const heading of args.headings) {
                    for (const el of document.querySelectorAll(heading)) {
                        if (!visible(el) || !args.titles.some(title => el.textContent.includes(title))) continue;
                        const scope = args.products.map(selector => el.closest(selector))
                            .find(candidate => candidate && visible(candidate));
                        if (!scope) throw new Error('Visible purchase title has no product scope');
                        scopes.add(scope);
                    }
                }
                if (scopes.size > 1) throw new Error('Ambiguous visible purchase products');
                return scopes.size ? [...scopes][0] : null;
            }
            for (const title of args.titles) {
                for (const product of args.products) {
                    for (const heading of args.headings) {
                        const scopes = new Set();
                        for (const el of document.querySelectorAll(heading)) {
                            if (!visible(el) || !el.textContent.includes(title)) continue;
                            const scope = el.closest(product);
                            if (scope && visible(scope)) scopes.add(scope);
                        }
                        if (scopes.size) return scopes.size === 1 ? [...scopes][0] : null;
                    }
                }
            }
            return null;
        }""",
        {"titles": config.get("card_title") or ["购买补签卡"],
         "headings": config.get("title_selectors") or
         ["h1", "h2", "h3", ".magic-card-header", ".card-title"],
         "products": selectors, "requireUniqueVisible": require_unique_visible},
    )
    element = handle.as_element()
    if element is None:
        await handle.dispose()
    return element


async def _buy_text(
    root: Any, selectors: list[str], *, unique: bool = False, include_hidden: bool = False,
) -> str:
    for selector in selectors or ["body"]:
        elements = await root.query_selector_all(selector)
        try:
            visible = []
            for element in elements:
                if include_hidden or await element.is_visible():
                    visible.append(element)
            if visible:
                if unique and len(visible) != 1:
                    return ""
                return (await visible[0].text_content() or "") if include_hidden else await visible[0].inner_text()
        finally:
            for element in elements:
                await element.dispose()
    return ""


def _buy_origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("购买地址必须为安全的 HTTP(S) 同源地址")
    return parts.scheme, parts.hostname.lower(), parts.port or (443 if parts.scheme == "https" else 80)


def _buy_clicks(config: dict[str, Any]) -> list[dict[str, Any]]:
    clicks = config.get("purchase_clicks", [
        {"scope": "product", "selectors": ["input[type='submit']", "button[type='submit']"]},
    ])
    if not isinstance(clicks, list) or not clicks:
        raise ValueError("购买点击项不能为空")
    for item in clicks:
        if (not isinstance(item, dict) or item.get("scope") not in {"product", "page"}
                or not isinstance(item.get("selectors"), list) or not item["selectors"]
                or any(not isinstance(s, str) or not s.strip() for s in item["selectors"])):
            raise ValueError("购买点击项 scope/selectors 无效")
    return clicks


def _buy_timeout(config: dict[str, Any]) -> float:
    quantity = int(config.get("purchase_quantity", 0)) or 365
    verify = max(1, min(float(config.get("verification_timeout_seconds", 10)), 60))
    interval = max(0, float(config.get("purchase_interval_seconds", 12)))
    clicks = _buy_clicks(config)
    # Include selection/trial waits, real clicks, inventory/shop navigation and verification.
    per_click = (sum(len(item["selectors"]) for item in clicks) + len(clicks)) * SELECTOR_WAIT_MS / 1000
    if config.get("quantity_selectors"):
        per_click += (len(config["quantity_selectors"]) + len(clicks) + 2) * SELECTOR_WAIT_MS / 1000
    return max(180, 60 + min(365, max(1, quantity)) * (interval + per_click + verify + 90))


async def _buy_quantity(
    page: Any, element: Any, origin: Any, cap: int, expected: int | None = None,
) -> int:
    if _buy_origin(page.url) != origin:
        raise ValueError("数量输入页面跨源")
    ready_before = await _buy_page_ready(page)
    if _buy_origin(page.url) != origin or not ready_before:
        raise ValueError("数量输入页面跨源或未就绪")
    state = await element.evaluate("""el => ({
        input: el instanceof HTMLInputElement &&
            ['number', 'text', 'tel'].includes(el.type) && el.isConnected,
        min: el.getAttribute('min'), max: el.getAttribute('max'), step: el.getAttribute('step'),
        value: el.getAttribute('value')
    })""")
    if (not state["input"] or not await element.is_enabled()
            or (expected is None and (not await element.is_visible() or not await element.is_editable()))):
        raise ValueError("购买数量必须是可见、启用且可编辑的 number/text/tel input")
    limits = {}
    for name, default in (("min", 1), ("max", None), ("step", 1)):
        raw = state[name]
        if raw is None or (name == "step" and raw == "any"):
            limits[name] = 1 if raw == "any" else default
            continue
        # Reject malformed HTML attributes rather than accepting browser defaults.
        if not isinstance(raw, str) or not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", raw):
            raise ValueError(f"购买数量 {name} 属性非法")
        try:
            value = Decimal(raw)
            if not value.is_finite() or value != value.to_integral_value() or value < (1 if name == "step" else 0):
                raise ValueError(f"购买数量 {name} 必须为有效整数")
            limits[name] = int(value)
        except InvalidOperation as exc:
            raise ValueError(f"购买数量 {name} 属性非法") from exc
    minimum, maximum, step = limits["min"], limits["max"], limits["step"]
    if maximum is not None and maximum < minimum:
        raise ValueError("购买数量 min/max 范围非法")
    cap = min(cap, maximum) if maximum is not None else cap
    # HTML step base is min, then a valid initial value attribute, otherwise zero.
    base = Decimal(limits["min"]) if state["min"] is not None else Decimal(0)
    initial = state["value"]
    if (state["min"] is None and isinstance(initial, str) and
            re.fullmatch(r"-?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", initial)):
        base = Decimal(initial)
    if state["step"] == "any":
        base = Decimal(0)
    if not base.is_finite() or base != base.to_integral_value():
        raise ValueError("购买数量 step 基准无法产生合法正整数")
    base = int(base)
    quantity = base + (cap - base) // step * step
    if quantity <= 0 or quantity < minimum:
        raise ValueError("购买数量限制内无可购买的合法正整数")
    if expected is not None:
        if expected > quantity or expected < minimum or expected <= 0 or (expected - base) % step:
            raise ValueError("购买数量限制已变化，停止购买")
        if await element.input_value() != str(expected):
            raise ValueError("购买数量输入值核验失败")
        if await element.evaluate("el => el.checkValidity()") is not True:
            raise ValueError("购买数量 input checkValidity 核验失败")
    ready_after = await _buy_page_ready(page)
    if _buy_origin(page.url) != origin or not ready_after:
        raise ValueError("数量输入检查期间页面跨源或未就绪")
    return quantity if expected is None else expected


async def _buy_fill_quantity(
    page: Any, config: dict[str, Any], origin: Any, cap: int,
) -> tuple[Any, int, Any]:
    if _buy_origin(page.url) != origin:
        raise ValueError("数量输入页面跨源")
    product = await _buy_product(page, config)
    if product is None:
        raise ValueError("无法唯一定位补签卡商品")
    selected = None
    try:
        async with asyncio.timeout(SELECTOR_WAIT_MS / 1000):
            while selected is None:
                for selector in config["quantity_selectors"]:
                    elements = await product.query_selector_all(selector)
                    try:
                        for element in elements:
                            editable = await element.evaluate("""el => el instanceof HTMLInputElement &&
                                ['number', 'text', 'tel'].includes(el.type) && !el.readOnly""")
                            if (editable is True and await element.is_visible()
                                    and await element.is_enabled() and await element.is_editable()):
                                selected = element
                                break
                    finally:
                        for element in elements:
                            if element is not selected:
                                await element.dispose()
                    if selected is not None:
                        break
                if selected is None:
                    await asyncio.sleep(0.1)
            quantity = await _buy_quantity(page, selected, origin, cap)
            # Only real fill; after this point never try another candidate.
            await selected.fill(str(quantity), timeout=SELECTOR_WAIT_MS)
            await selected.press("Tab", timeout=SELECTOR_WAIT_MS)
            if _buy_origin(page.url) != origin:
                raise ValueError("数量失焦期间页面跨源")
            await page.wait_for_load_state("networkidle", timeout=SELECTOR_WAIT_MS)
            await _buy_quantity(page, selected, origin, cap, quantity)
        return selected, quantity, product
    except BaseException as exc:
        if selected is not None:
            await selected.dispose()
        await product.dispose()
        if isinstance(exc, TimeoutError):
            raise ValueError("购买数量输入框查找、填写或核验超时，已停止") from exc
        raise


async def _buy_button(page: Any, config: dict[str, Any], item: dict[str, Any], origin: Any) -> Any:
    if _buy_origin(page.url) != origin:
        raise ValueError("付款页面跨源")
    root = await _buy_product(page, config) if item["scope"] == "product" else page
    if root is None:
        raise ValueError("无法唯一定位补签卡商品")
    try:
        deadline = asyncio.get_running_loop().time() + SELECTOR_WAIT_MS / 1000
        while True:
            for selector in item["selectors"]:
                for button in await root.query_selector_all(selector):
                    if not await button.is_visible() or not await button.is_enabled():
                        continue
                    try:
                        await button.click(trial=True, timeout=250)
                    except Exception:
                        continue
                    # Use the browser's form owner, including repaired tr > form markup.
                    urls = await button.evaluate("""el => ({
                        action: el.form ? el.form.action : null,
                        override: el.hasAttribute('formaction') ? el.formAction : null,
                        href: el.closest('a[href]')?.href || null,
                        target: el.formTarget || el.form?.target || ''
                    })""")
                    for url in (urls["action"], urls["override"], urls["href"]):
                        if url and _buy_origin(url) != origin:
                            raise ValueError("拒绝跨源 action/formaction/href")
                    if urls["target"] not in {"", "_self"}:
                        raise ValueError("拒绝在其他窗口付款")
                    if _buy_origin(page.url) != origin:
                        raise ValueError("付款页面跨源")
                    return button
            if asyncio.get_running_loop().time() >= deadline:
                raise ValueError("未找到可见、启用且可用的购买按钮")
            await asyncio.sleep(0.1)
    finally:
        if root is not page:
            await root.dispose()


async def _purchase_cards(
    page: Any,
    config: dict[str, Any],
    shop_url: str,
    needed: int,
    log: Callable[[str, str], None],
    *,
    pending_count: int | None = None,
) -> dict[str, Any]:
    result = {"status": "fail", "message": "购买未开始", "bought": 0,
              "requested_quantity": needed, "mode": "fixed" if config.get("purchase_quantity", 0) else "auto",
              "needed": needed, "cards_before": None, "cards_after": None,
              "payment_attempted": False, "rounds": []}
    origin = _buy_origin(shop_url)
    count_url = urljoin(shop_url, str(config.get("card_count_url") or ""))
    clicks = _buy_clicks(config)
    verify_seconds = max(1, min(float(config.get("verification_timeout_seconds", 10)), 60))
    count_pattern = str(config.get("card_count_regex") or r"[补補][签簽]卡[:：]\s*(\d+)")

    async def cards() -> int | None:
        if _buy_origin(page.url) != origin:
            raise ValueError("库存页面跨源")
        count = _regex_number(await _buy_text(page, config.get("card_count_selectors") or []), count_pattern)
        if _buy_origin(page.url) != origin:
            raise ValueError("库存读取期间页面跨源")
        return count

    async def goto(url: str) -> None:
        parts = urlsplit(url)
        log("INFO", f"购卡页面导航：{parts.scheme}://{parts.netloc}{parts.path}（查询参数仅在日志中隐藏，导航保留）")
        await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        if _buy_origin(page.url) != origin:
            raise ValueError("购买导航跨源")

    async def wait_cards(expected: int | None = None) -> int | None:
        observed = None
        checks = 0
        started = asyncio.get_running_loop().time()
        log("INFO", f"读取购卡库存：来源={'独立库存地址' if config.get('card_count_url') else '商店页面'}，选择器={'已配置' if config.get('card_count_selectors') else 'body'}，最多等待 {verify_seconds:g}s")
        try:
            async with asyncio.timeout(verify_seconds):
                while True:
                    try:
                        observed = None
                        ready_before = await _buy_page_ready(page)
                        count = await cards()
                        checks += 1
                        ready_after = await _buy_page_ready(page)
                        if _buy_origin(page.url) != origin:
                            raise ValueError("库存就绪检查期间页面跨源")
                        if ready_before and ready_after:
                            observed = count
                        if observed is not None and (expected is None or observed == expected):
                            log("INFO", f"已读取补签卡数量：{observed} 张（检查 {checks} 次）")
                            return observed
                    except Exception:
                        if _buy_origin(page.url) != origin or getattr(page, "is_closed", lambda: False)():
                            raise
                        observed = None
                    await asyncio.sleep(0.2)
        except TimeoutError:
            pass
        elapsed = asyncio.get_running_loop().time() - started
        log("WARN", f"库存读取等待结束：耗时 {elapsed:.2f}s，检查 {checks} 次，最后数量 {observed if observed is not None else '未匹配'}；请检查库存地址、数量选择器和正则第一捕获组")
        return observed

    async def wait_shop() -> tuple[float, float]:
        error = "商店页面未就绪"
        try:
            async with asyncio.timeout(verify_seconds):
                while True:
                    try:
                        if _buy_origin(page.url) != origin:
                            raise ValueError("商店页面跨源")
                        if await _buy_page_ready(page):
                            error = "无法唯一定位商品或读取正价格"
                            info = await _extract_card_info(page, config, log)
                            if info is not None:
                                price = info["price"]
                                maximum = float(config["max_price"]) if config.get("max_price") is not None else None
                                if maximum is not None and (not math.isfinite(maximum) or price > maximum):
                                    raise ValueError(f"补签卡价格 {price} 超过上限 {maximum}")
                                error = "无法读取有效商店余额"
                                points = _buy_amount(await _buy_text(page, config.get("points_selectors") or []),
                                                    str(config.get("points_regex") or r"魔力值[^\d+\-]*([+-]?\d[\d,]*(?:\.\d+)?)"))
                                ready_after = await _buy_page_ready(page)
                                if _buy_origin(page.url) != origin:
                                    raise ValueError("商店读取期间页面跨源")
                                if points is not None and points >= 0 and ready_after:
                                    return price, points
                        if _buy_origin(page.url) != origin:
                            raise ValueError("商店就绪检查期间页面跨源")
                    except Exception as exc:
                        if _buy_origin(page.url) != origin or getattr(page, "is_closed", lambda: False)():
                            raise
                        error = str(exc)
                    await asyncio.sleep(0.2)
        except TimeoutError:
            raise ValueError(error) from None

    dialog_seen = False

    async def dismiss_dialog(dialog: Any) -> None:
        nonlocal dialog_seen
        dialog_seen = True
        await dialog.dismiss()

    page.on("dialog", dismiss_dialog)
    try:
        quantity_selectors = config.get("quantity_selectors", [])
        if (not isinstance(quantity_selectors, list) or len(quantity_selectors) > 20
                or any(not isinstance(s, str) or not s.strip() for s in quantity_selectors)):
            raise ValueError("购买数量输入框候选必须为最多 20 个非空 CSS 选择器")
        if _buy_origin(count_url) != origin:
            raise ValueError("独立库存地址必须与商店同源")
        await goto(count_url if config.get("card_count_url") else shop_url)
        baseline = await wait_cards()
        result.update(cards_before=baseline, cards_after=baseline)
        if baseline is not None and baseline < 0:
            raise ValueError("Negative inventory cannot define a bounded purchase quantity")
        if baseline is None:
            raise ValueError(f"在 {verify_seconds:g}s 内未找到补签卡数量，请检查补签卡库存地址、数量选择器和正则第一捕获组")
        if pending_count is not None:
            needed = max(0, pending_count - baseline)
            result.update(needed=needed, requested_quantity=needed)
        if needed <= 0:
            result.update(status="success", message=f"补签卡已足够（当前 {baseline} 张）")
            return result
        for index in range(min(365, needed)):
            if result["bought"] >= needed:
                break
            if index:
                await asyncio.sleep(max(0, float(config.get("purchase_interval_seconds", 12))))
                if config.get("card_count_url"):
                    await goto(count_url)
                current = await wait_cards(expected=baseline)
                if current != baseline:
                    result.update(status="waiting_human", cards_after=current, message="购买轮次之间库存异常，已停止")
                    return result
            if config.get("card_count_url"):
                await goto(shop_url)
            price, points = await wait_shop()
            # Keep the finite local float source, but do not floor a binary float ratio.
            balance_n, balance_d = Decimal(str(points)).as_integer_ratio()
            price_n, price_d = Decimal(str(price)).as_integer_ratio()
            affordable = (balance_n * price_d) // (balance_d * price_n)
            if affordable <= 0:
                result.update(status="success", message=f"魔力值不足，已购买 {result['bought']}/{needed} 张补签卡")
                return result
            round_result = {"round": index + 1, "cards_before": baseline, "cards_after": None,
                            "price": price, "points_before": points, "clicks": 0,
                            "qty": 1, "requested": needed - result["bought"], "expected_cards": baseline + 1,
                            "dispatched": False, "status": "unknown", "message": "购买结果待核验"}
            result["rounds"].append(round_result)
            interrupted = False
            quantity_input = None
            quote_product = None
            quantity = 1
            cap = min(needed - result["bought"], affordable)
            try:
                if quantity_selectors:
                    quantity_input, quantity, quote_product = await _buy_fill_quantity(page, config, origin, cap)
                round_result.update(qty=quantity, requested=quantity, expected_cards=baseline + quantity)
                for item in clicks:
                    async with asyncio.timeout(SELECTOR_WAIT_MS / 1000):
                        button = await _buy_button(page, config, item, origin)
                    if dialog_seen:
                        await button.dispose()
                        break
                    # Once dispatched, no alternate selector or replay is safe.
                    try:
                        async with asyncio.timeout(SELECTOR_WAIT_MS / 1000):
                            if _buy_origin(page.url) != origin or not await _buy_page_ready(page):
                                raise ValueError("付款页面跨源或未就绪")
                            info = (await _extract_card_info(page, config, log, product=quote_product)
                                    if quote_product is not None else await _extract_card_info(page, config, log))
                            current_points = _buy_amount(await _buy_text(page, config.get("points_selectors") or []),
                                str(config.get("points_regex") or r"魔力值[^\d+\-]*([+-]?\d[\d,]*(?:\.\d+)?)"))
                            maximum = float(config["max_price"]) if config.get("max_price") is not None else None
                            if (info is None or info["price"] != price or
                                    (maximum is not None and (not math.isfinite(maximum) or info["price"] > maximum))):
                                raise ValueError("购买单张价格已变化或超过上限，已停止")
                            if current_points is None or current_points < 0:
                                raise ValueError("无法读取有效付款余额")
                            current_n, current_d = Decimal(str(current_points)).as_integer_ratio()
                            if current_n * price_d < quantity * price_n * current_d:
                                raise ValueError("付款余额不足本轮数量，已停止")
                            if quantity_input is not None:
                                await _buy_quantity(page, quantity_input, origin, cap, quantity)
                            if _buy_origin(page.url) != origin or not await _buy_page_ready(page) or _buy_origin(page.url) != origin:
                                raise ValueError("付款检查期间页面跨源或未就绪")
                        round_result["dispatched"] = True
                        result["payment_attempted"] = True
                        await button.click(timeout=SELECTOR_WAIT_MS)
                        round_result["clicks"] += 1
                    finally:
                        await button.dispose()
                    if dialog_seen:
                        break
            except (Exception, asyncio.CancelledError) as exc:
                interrupted = isinstance(exc, asyncio.CancelledError)
                round_result["error"] = f"{type(exc).__name__}: {exc}"
                round_result["message"] = round_result["error"]
                if not round_result["dispatched"]:
                    if interrupted:
                        raise
                    raise ValueError(str(exc)) from exc
            finally:
                if quantity_input is not None:
                    await quantity_input.dispose()
                if quote_product is not None:
                    await quote_product.dispose()
            if dialog_seen:
                round_result["message"] = "原生弹窗已拒绝，购买结果未知"
                result.update(status="waiting_human", message="原生弹窗已拒绝，购买结果未知，需要人工核验")
                return result
            observed = None
            settled = False
            verified = False
            verification_started = asyncio.get_running_loop().time()
            verification_checks = 0
            last_verification_state = None
            log("INFO", f"购买后开始库存核验：本轮 {quantity} 张，购买前 {baseline} 张，期望 {baseline + quantity} 张，最多等待 {verify_seconds:g}s")
            try:
                async with asyncio.timeout(verify_seconds):
                    refreshed = False
                    next_refresh = 0.0
                    while True:
                        try:
                            if _buy_origin(page.url) != origin:
                                raise ValueError("库存页面跨源")
                            # Never interrupt an unsettled POST/navigation/AJAX with a GET.
                            settled = False
                            try:
                                await page.wait_for_load_state("networkidle", timeout=1000)
                                settled = await _buy_page_ready(page)
                            except Exception:
                                pass
                            if _buy_origin(page.url) != origin:
                                raise ValueError("库存等待期间页面跨源")
                            now = asyncio.get_running_loop().time()
                            if not config.get("card_count_url") or refreshed:
                                observed = await cards()
                                verification_checks += 1
                                settled = await _buy_page_ready(page) and settled
                                if _buy_origin(page.url) != origin:
                                    raise ValueError("库存就绪检查期间页面跨源")
                            if observed == baseline + quantity and settled:
                                verified = True
                                break
                            verification_state = (observed, settled)
                            if verification_state != last_verification_state:
                                log("INFO", f"库存尚未确认，继续等待：当前 {observed if observed is not None else '未读到'} 张，期望 {baseline + quantity} 张，页面就绪={settled}")
                                last_verification_state = verification_state
                            if settled and now >= next_refresh:
                                # Explicit GET, never reload a POST result or replay payment.
                                settled = False
                                await goto(count_url if config.get("card_count_url") else shop_url)
                                refreshed = True
                                next_refresh = asyncio.get_running_loop().time() + 1
                                continue
                        except Exception:
                            observed = None
                            if _buy_origin(page.url) != origin or getattr(page, "is_closed", lambda: False)():
                                raise
                            round_result["verification_error"] = "页面加载或库存读取暂时失败"
                            log("WARN", "库存页面加载或读取暂时失败，将在剩余验证时间内继续核验，不会重复购买")
                        await asyncio.sleep(0.1)
            except (Exception, asyncio.CancelledError) as exc:
                interrupted = interrupted or isinstance(exc, asyncio.CancelledError)
                round_result["verification_error"] = f"{type(exc).__name__}: {exc}"
            round_result["cards_after"] = observed
            round_result["verification_checks"] = verification_checks
            round_result["verification_elapsed_seconds"] = round(asyncio.get_running_loop().time() - verification_started, 3)
            log("INFO" if verified else "WARN", f"库存核验结束：检查 {verification_checks} 次，耗时 {round_result['verification_elapsed_seconds']:g}s，购买前 {baseline} 张，最后读取 {observed if observed is not None else '未知'} 张，已确认={verified}")
            result["cards_after"] = observed
            if dialog_seen or not verified:
                round_result["message"] = f"验证等待结束前未确认库存恰好增加 {quantity}，需要人工核验"
                result.update(status="waiting_human", message=f"购买后在 {verify_seconds:g}s 验证等待内未确认库存恰好增加 {quantity}（购买前 {baseline}，最后读取 {observed if observed is not None else '未知'}），结果未知，需要人工核验")
                return result
            baseline = observed
            result["bought"] += quantity
            round_result["status"] = "success"
            round_result["message"] = f"库存已核验增加 {quantity}"
            log("INFO", f"已核验购买 {result['bought']}/{needed} 张补签卡")
            if interrupted or not settled:
                result.update(status="waiting_human", message="库存已增加，但执行被中断或提交尚未结束，已停止后续购买")
                return result
            if result["bought"] < needed and index < min(365, needed) - 1:
                await goto(shop_url)
        result.update(status="success", message=f"已购买 {result['bought']} 张补签卡，当前 {baseline} 张")
    except asyncio.CancelledError:
        if not result["rounds"]:
            raise
        result.update(status="waiting_human", message="购买执行超时或取消，已停止后续购买")
    except Exception as exc:
        result.update(status="waiting_human" if result["bought"] else "fail", message=str(exc))
    finally:
        page.remove_listener("dialog", dismiss_dialog)
    return result


async def _run_buy_card(
    page: Any,
    config: dict[str, Any],
    pending_dates: list[str],
    log: Callable[[str, str], None],
) -> tuple[str, str, dict[str, Any]]:
    shop_url = urljoin(page.url, str(config.get("shop_url") or "{base_url}/mybonus.php"))
    if "{base_url}" in shop_url:
        return "fail", "商店地址未解析 {base_url}", {}
    quantity = int(config.get("purchase_quantity", 0))
    if not 0 <= quantity <= 365:
        return "fail", "购买数量必须在 0..365 之间", {}
    result = await _purchase_cards(page, config, shop_url, quantity, log,
                                   pending_count=None if quantity else len(pending_dates))
    return result["status"], result["message"], {"buy_card": result}


async def _run_resign(
    page: Any,
    config: dict[str, Any],
    pending_dates: list[str],
    log: Callable[[str, str], None],
) -> tuple[str, str, dict[str, Any]]:
    if not pending_dates:
        return "success", "没有待补签日期", {"resign": {"pending": [], "success": [], "already_signed": [], "failed": []}}
    attendance_url = urljoin(
        page.url, str(config.get("attendance_url") or "{base_url}/attendance.php")
    )
    if "{base_url}" in attendance_url:
        return "fail", "签到页地址未解析 {base_url}", {}
    await _goto_if_needed(page, attendance_url, "domcontentloaded", log, "签到页面")
    await asyncio.sleep(2)
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    cards_before = _regex_number(
        await _page_body(page),
        str(config.get("card_count_regex") or r"[补補][签簽]卡[:：]\s*(\d+)"),
    )
    if cards_before is None:
        return "fail", "未找到补签卡数量，可能不支持补签", {}
    if cards_before <= 0:
        return (
            "success",
            "补签卡不足，未执行补签",
            {
                "resign": {
                    "pending": pending_dates,
                    "success": [],
                    "already_signed": [],
                    "failed": [],
                    "remaining": pending_dates,
                    "cards_before": 0,
                    "cards_after": 0,
                }
            },
        )
    months: dict[str, list[str]] = {}
    for value in pending_dates:
        months.setdefault(value[:7], []).append(value)
    already_signed: list[dict[str, Any]] = []
    missed: list[str] = []
    failed: list[dict[str, Any]] = []
    for month in sorted(months):
        if not await _navigate_fc_month(page, month, config, log):
            for value in sorted(months[month]):
                failed.append({"date": value, "message": f"无法翻月到 {month}"})
            log("WARN", f"跳过 {month} 的补签检查，保留待补签状态")
            continue
        for value in sorted(months[month]):
            state = await _calendar_day_status(page, value, config)
            if state == "missed":
                missed.append(value)
                log("INFO", f"{value} 日历显示漏签，加入补签列表")
            elif state in {"signed", "empty", "not_found"}:
                already_signed.append(
                    {
                        "date": value,
                        "message": "日历已签到" if state == "signed" else "日历无漏签标记",
                    }
                )
                log("INFO", f"{value} 无需补签：{state}")
            else:
                failed.append({"date": value, "message": state})
                log("WARN", f"{value} 日历检查失败：{state}")
    limit = min(len(missed), cards_before)
    to_retro = missed[:limit]
    remaining = missed[limit:]
    endpoint = urljoin(page.url, str(config.get("ajax_path") or "ajax.php"))
    action_name = str(config.get("ajax_action") or "attendanceRetroactive")
    date_param = str(config.get("ajax_date_param") or "params[date]")
    success: list[dict[str, Any]] = []
    unconfirmed: list[dict[str, Any]] = []
    for value in to_retro:
        try:
            raw = await page.evaluate(
                """async (args) => {
                    const body = new URLSearchParams();
                    body.append(args.dateParam, args.date);
                    body.append('action', args.action);
                    const resp = await fetch(args.url, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'},
                        body: body.toString(),
                    });
                    return {status: resp.status, text: (await resp.text()).slice(0, 2000)};
                }""",
                {"url": endpoint, "dateParam": date_param, "date": value, "action": action_name},
            )
        except Exception as exc:
            unconfirmed.append({"date": value, "message": "Resign submission outcome uncertain"})
            log("WARN", f"补签 {value} 请求失败：{type(exc).__name__}: {exc}")
            break
        status_code = int((raw or {}).get("status") or 0)
        text = str((raw or {}).get("text") or "")
        if status_code >= 400:
            unconfirmed.append(
                {"date": value, "message": f"HTTP {status_code}: {text[:200]}"}
            )
            log("WARN", f"补签 {value} 返回 HTTP {status_code}")
            break
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = {}
        ret = payload.get("ret") if isinstance(payload, dict) else None
        message = str(payload.get("msg") if isinstance(payload, dict) else "") or text[:200]
        if ret == 0:
            success.append({"date": value, "message": message or "补签成功"})
            log("INFO", f"补签成功：{value} {message}")
        elif ret == -1 or "已经签到" in message:
            already_signed.append({"date": value, "message": message or "已经签到"})
            log("INFO", f"{value} 已经签到，视为已处理")
        else:
            unconfirmed.append({"date": value, "message": message or "补签结果未知"})
            log("WARN", f"补签失败：{value} {message}")
            break
    cards_after = max(0, cards_before - len(success))
    data = {
        "url": page.url,
        "resign": {
            "pending": pending_dates,
            "success": success,
            "already_signed": already_signed,
            "failed": failed,
            "unconfirmed": unconfirmed,
            "remaining": remaining,
            "cards_before": cards_before,
            "cards_after": cards_after,
        },
    }
    if unconfirmed:
        return "waiting_human", "\u8865\u7b7e\u63d0\u4ea4\u7ed3\u679c\u672a\u77e5\uff0c\u8bf7\u4eba\u5de5\u6838\u9a8c", {**data, "outcome_uncertain": True}
    if failed:
        return (
            "fail",
            f"补签完成 {len(success)} 天，失败 {len(failed)} 天",
            data,
        )
    if success:
        suffix = f"，{len(remaining)} 天因补签卡不足未处理" if remaining else ""
        return "success", f"成功补签 {len(success)} 天{suffix}", data
    if remaining:
        return "success", f"补签卡不足，{len(remaining)} 天未补签", data
    return "success", f"无需补签，已处理 {len(already_signed)} 天", data


async def _run_plugin_step(
    page: Any,
    step: dict[str, Any],
    config: dict[str, Any],
    feature: str,
    plugin_runtimes: dict[str, dict[str, Any]] | None,
    plugin_broker: Any,
    log: Callable[[str, str], None],
    artifacts: Any = None,
) -> tuple[str, str, dict[str, Any]]:
    parts = feature.split(".", 2)
    if len(parts) < 3:
        return "fail", f"无效的插件功能 key：{feature}", {}
    plugin_id = parts[1]
    runtime = (plugin_runtimes or {}).get(plugin_id)
    if not runtime:
        return "fail", f"插件 {plugin_id} 未启用、未审批或未注册", {}
    browser_allowed = "browser_actions" in runtime.get("approved_capabilities", [])
    if browser_allowed and plugin_broker is None:
        return "fail", "浏览器动作代理不可用", {}
    from worker.plugin_authorization import authorize
    try:
        await authorize(runtime)
    except RuntimeError:
        return "fail", "插件授权已撤销", {}
    try:
        timeout_seconds = int(step.get("timeout_seconds") or 30)
    except (TypeError, ValueError):
        timeout_seconds = 30
    timeout_seconds = max(1, min(600, timeout_seconds))
    parsed = urlsplit(str(page.url))
    site_origin = f"{parsed.scheme}://{parsed.netloc}"
    broker_token = plugin_broker.register(
        page,
        site_origin=site_origin,
        step_key=str(step.get("step_key") or ""),
        ttl_seconds=timeout_seconds + 30,
        log=log,
        artifacts=artifacts,
        approved_capabilities=runtime.get("approved_capabilities"),
        authorize=lambda: authorize(runtime, "browser_actions"),
        lifecycle=getattr(artifacts, "pages", None),
    ) if browser_allowed else ""
    async def monitor():
        while True:
            await asyncio.sleep(0.5)
            await authorize(runtime)
    monitor_task = asyncio.create_task(monitor())
    import threading
    cancel_event = threading.Event()
    execution_task = None
    try:
        from worker.plugin_client import PluginClientError, execute_step

        try:
            execution_task = asyncio.create_task(asyncio.to_thread(
                execute_step,
                str(runtime.get("endpoint") or ""),
                str(runtime.get("token") or ""),
                execution_id=uuid.uuid4().hex,
                plugin_id=plugin_id,
                feature_key=feature,
                config=config,
                site_origin=site_origin,
                current_url=str(page.url),
                deadline_ms=timeout_seconds * 1000,
                broker_url=plugin_broker.public_url if browser_allowed else "",
                broker_token=broker_token,
                timeout=float(timeout_seconds),
                cancel_event=cancel_event,
            ))
            done, _ = await asyncio.wait({execution_task, monitor_task}, return_when=asyncio.FIRST_COMPLETED)
            if monitor_task in done:
                return "fail", "插件授权已撤销", {}
            result = await execution_task
            await authorize(runtime)
        except PluginClientError as exc:
            return "fail", str(exc), {}
        except RuntimeError:
            return "fail", "插件授权已撤销", {}
    finally:
        cancel_event.set()
        if broker_token:
            await plugin_broker.release_and_wait(broker_token)
        monitor_task.cancel()
        await asyncio.gather(monitor_task, return_exceptions=True)
        if execution_task is not None and not execution_task.done():
            await asyncio.gather(execution_task, return_exceptions=True)
    data = result.get("data")
    if not isinstance(data, dict):
        data = {}
    return str(result.get("status") or "error"), str(result.get("message") or ""), data


def _model_redaction_values(*sources: tuple[Any, bool]) -> tuple[str, ...]:
    """Collect known job secrets before model helpers normalize or truncate replies."""
    pending = list(sources)
    seen = set()
    secrets = set()
    while pending:
        item, sensitive = pending.pop()
        if isinstance(item, str) and item and sensitive:
            secrets.add(item)
        elif isinstance(item, (dict, list, tuple, set)) and (id(item), sensitive) not in seen:
            seen.add((id(item), sensitive))
            if isinstance(item, dict):
                pending.extend((child, sensitive or (isinstance(key, str) and bool(re.search(
                    r"password|passwd|secret|token|api.?key|credential|authorization|cookie|"
                    r"username|account|otp|endpoint|(?:^|_)url$|^model$|^_model_redaction_values$", key, re.I,
                )))) for key, child in item.items())
            else:
                pending.extend((child, sensitive) for child in item)
    return tuple(sorted(secrets))


async def _run_step(
    page: Any,
    step: dict[str, Any],
    credentials: dict[str, str] | None,
    twofauth: dict[str, Any] | None,
    cfbox: dict[str, Any] | None,
    cancel: asyncio.Event | None,
    xdisplay: Desktop | None,
    lane_key: str,
    log: Callable[[str, str], None] | None = None,
    resign_pending_dates: list[str] | None = None,
    vision: dict[str, Any] | None = None,
    capture_image: Callable[[bytes, str], Awaitable[None]] | None = None,
    credential_task: asyncio.Task[dict[str, str]] | None = None,
    plugin_runtimes: dict[str, dict[str, Any]] | None = None,
    plugin_broker: Any = None,
    artifacts: Any = None,
    secret_payload: dict[str, Any] | None = None,
    resign_progress: StepCallback | None = None,
) -> tuple[str, str, dict[str, Any]]:
    feature = str(step.get("feature_type") or "")
    config = step.get("config") or {}
    if not isinstance(config, dict):
        return "fail", "步骤配置不是对象", {"reason": "input_validation_failed", "field": "config"}
    note = log or (lambda _level, _message: None)
    if vision is not None:
        vision = {**vision, "_model_redaction_values": _model_redaction_values(
            (vision, False), (credentials, True), (twofauth, False), (cfbox, False),
            (secret_payload, False), (plugin_runtimes, False),
        )}
        if (secret_payload or {}).get("_result_secret_fingerprints"):
            vision["_result_secret_fingerprints"] = secret_payload["_result_secret_fingerprints"]

    def record_secret(*values: Any) -> None:
        if secret_payload is not None:
            for value in values:
                if isinstance(value, str) and 0 < len(value) <= 500:
                    secret_payload.setdefault("_result_secret_fingerprints", set()).add(
                        (len(value), hashlib.sha256(value.encode("utf-8")).hexdigest())
                    )

    from worker.assistant_private import private_context, run_private_step
    private = private_context(secret_payload)
    if private is not None and feature in {
        "action.fill", "challenge.turnstile", "challenge.image_captcha",
        "sign.iframe.image_captcha", "challenge.grid_captcha", "sign.image_quiz",
    }:
        return await run_private_step(private, step, vision, cfbox, lane_key)

    if feature.startswith("plugin."):
        return await _run_plugin_step(
            page,
            step,
            config,
            feature,
            plugin_runtimes,
            plugin_broker,
            note,
            artifacts,
        )

    def selectors_note(label: str, selectors: Any) -> None:
        values = [str(item) for item in (selectors or []) if str(item)]
        note("INFO", f"{label}选择器：{', '.join(values) if values else '无'}")

    def matched_note(label: str):  # type: ignore[no-untyped-def]
        return lambda value: note("INFO", f"{label}命中选择器：{value}")

    if feature in {"flow.open", "action.navigate"}:
        url = str(config.get("url") or "")
        if not url:
            return "fail", "地址为空", {}
        wait_until = await _wait_until(page, str(config.get("wait_until") or "domcontentloaded"))
        retry_count = int(config.get("retry_count") or 0)
        try:
            await _goto_if_needed(
                page,
                url,
                wait_until,
                note,
                "页面",
                retry_count=retry_count,
                check_error_page=True,
                transient_only=feature == "action.navigate",
            )
        except _NavigationPageError as exc:
            return "fail", str(exc), {
                "url": page.url,
                "error_page": exc.reason,
            }
        return "success", f"已打开 {page.url}", {"url": page.url}

    if feature == "flow.jump":
        return await _run_flow_jump(page, config, note)

    if feature in {"flow.result", "action.result"}:
        value = bool(config.get("output_value", True))
        behavior = (vision or {}).get("execution_settings") or {}
        if (behavior.get("llm_enabled") is True and config.get("llm_enabled") is True
                and (value or behavior.get("judgment_scope") == "all")):
            from worker.vision import (
                DEFAULT_RESULT_PROMPT,
                ResultJudgmentError,
                format_model_error,
                judge_result,
                model_error_data,
                sanitize_judgment_reason,
            )

            judgment_started = time.monotonic()
            model = {**(vision or {})}
            if behavior.get("model"):
                model["model"] = behavior["model"]
            redaction_values = _model_redaction_values((model, False))
            model["_model_redaction_values"] = redaction_values
            if not all(model.get(key) for key in ("base_url", "model", "api_key")):
                message = "未配置大模型，无法判断输出结果"
                note("ERROR", message)
                return "fail", message, {"reason": "model_unavailable"}
            use_vision = behavior.get("use_vision", True)
            if use_vision and not model.get("supports_vision", True):
                message = "大模型未开启视觉支持，无法判断输出结果"
                note("ERROR", message)
                return "fail", message, {"reason": "vision_not_supported"}
            note("INFO", f"大模型判断输出结果开始，输入方式：{'页面截图（不含页面文本）' if use_vision else '页面文本'}")
            stage = "evidence"
            try:
                if use_vision:
                    image = await page.screenshot(type="png", timeout=PAGE_TIMEOUT_MS)
                    if not isinstance(image, bytes) or not image:
                        raise ValueError()
                    evidence = {"image": image}
                else:
                    # innerText excludes input values and hidden DOM; bound what
                    # leaves the browser and treat page content as untrusted data.
                    text = await asyncio.wait_for(page.evaluate(
                        "() => document.body ? document.body.innerText.slice(0, 24000) : ''"
                    ), timeout=5)
                    if not isinstance(text, str) or not text.strip():
                        raise ValueError()
                    evidence = {"page_text": text}
                stage = "provider_request"
                prompt = str(config.get("llm_prompt") or "").strip()
                judgment = await judge_result(
                    model, prompt or behavior.get("llm_prompt") or DEFAULT_RESULT_PROMPT,
                    redaction_values=redaction_values, **evidence,
                )
                stage = "response_validation"
                if (type(judgment) is not dict or set(judgment) != {"result", "reason"}
                        or type(judgment["result"]) is not bool
                        or type(judgment["reason"]) is not str
                        or not judgment["reason"].strip() or len(judgment["reason"]) > 500):
                    raise ValueError()
                reason = sanitize_judgment_reason(
                    judgment["reason"], model, redaction_values=redaction_values,
                )
                if not reason:
                    raise ValueError()
            except Exception as exc:
                code = "evidence_error" if stage == "evidence" else "invalid_response"
                diagnostics = {"provider_error": code, "stage": stage}
                if stage != "evidence" and isinstance(exc, ResultJudgmentError):
                    diagnostics = model_error_data(exc, model)
                    detail = format_model_error(exc, model)
                else:
                    detail = f"{code}，阶段 {stage}"
                message = f"大模型判断失败：{detail}"
                note("ERROR", f"{message}；耗时 {time.monotonic() - judgment_started:.2f}s")
                data = {"reason": "model_judgment_failed", **diagnostics}
                return "fail", message, data
            judged = judgment["result"]
            note("INFO", f"大模型判断结果: {str(judged).lower()}；判断依据: {reason}")
            message = f"大模型判断结果: {str(judged).lower()}"
            if judged == value:
                message = str(config.get("message") or "").strip() or f"输出结果: {str(value).lower()}"
            return "success", message, {"value": judged, "message": message,
                                         "original_value": value, "judged_by": "llm",
                                         "judgment_reason": reason}
        message = str(config.get("message") or "").strip()
        if not message:
            message = f"输出结果: {str(value).lower()}"
        return "success", message, {"value": value, "message": message}

    if feature == "action.wait":
        seconds = int(config.get("seconds") or 2)
        if not await _sleep(seconds, cancel):
            return "canceled", "已取消", {}
        return "success", f"等待 {seconds} 秒", {}

    if feature == "action.click":
        selectors = list(config.get("selector") or [])
        selectors_note("点击目标", selectors)
        locator = await _find_locator(
            page,
            selectors,
            wait_ms=SELECTOR_WAIT_MS,
            on_match=matched_note("点击目标"),
        )
        if locator is None:
            return "fail", "未找到点击目标", {"reason": "selector_not_found", "field": "selector"}
        await locator.click(
            button=str(config.get("button") or "left"),
            click_count=int(config.get("click_count") or 1),
            timeout=PAGE_TIMEOUT_MS,
        )
        return "success", "点击完成", {"url": page.url}

    if feature == "action.fill":
        selectors = list(config.get("selector") or [])
        selectors_note("输入框", selectors)
        locator = await _find_locator(
            page,
            selectors,
            wait_ms=SELECTOR_WAIT_MS,
            on_match=matched_note("输入框"),
        )
        if locator is None:
            return "fail", "未找到输入框", {"reason": "selector_not_found", "field": "selector"}
        if bool(config.get("clear", True)):
            await locator.fill("", timeout=PAGE_TIMEOUT_MS)
        await locator.fill(str(config.get("value") or ""), timeout=PAGE_TIMEOUT_MS)
        return "success", "填写完成", {}

    if feature == "auth.form_login":
        if secret_payload is not None:
            from worker.secret_client import resolve_secret
            from worker.secret_client import require_site_origin
            require_site_origin(page.url, secret_payload["base_url"], secret_payload.get("trusted_urls", []))
            credentials = await resolve_secret(secret_payload, step)
            record_secret(credentials.get("username"), credentials.get("password"))
        elif credentials:
            record_secret(credentials.get("username"), credentials.get("password"))
        if not credentials and credential_task is not None:
            note("INFO", "等待 Bitwarden 登录凭据")
            try:
                credentials = await credential_task
                record_secret(credentials.get("username"), credentials.get("password"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return "fail", f"Bitwarden 凭据获取失败: {str(exc)[:500]}", {"reason": "credentials_unavailable"}
        if not credentials:
            return "fail", "未配置站点登录凭据", {"reason": "credentials_unavailable"}
        username_selectors = list(config.get("username_selector") or [])
        password_selectors = list(config.get("password_selector") or [])
        submit_selectors = list(config.get("submit_selector") or [])
        selectors_note("用户名输入框", username_selectors)
        username = await _find_locator(
            page,
            username_selectors,
            wait_ms=SELECTOR_WAIT_MS,
            on_match=matched_note("用户名输入框"),
        )
        if username is None:
            return "fail", "未找到用户名输入框", {"reason": "selector_not_found", "field": "username_selector"}
        selectors_note("密码输入框", password_selectors)
        password = await _find_locator(
            page,
            password_selectors,
            wait_ms=SELECTOR_WAIT_MS,
            on_match=matched_note("密码输入框"),
        )
        if password is None:
            return "fail", "未找到密码输入框", {"reason": "selector_not_found", "field": "password_selector"}
        selectors_note("登录按钮", submit_selectors)
        submit = await _find_locator(
            page,
            submit_selectors,
            wait_ms=SELECTOR_WAIT_MS,
            on_match=matched_note("登录按钮"),
        )
        if submit is None:
            return "fail", "未找到登录按钮", {"reason": "selector_not_found", "field": "submit_selector"}
        if secret_payload is not None:
            require_site_origin(page.url, secret_payload["base_url"], secret_payload.get("trusted_urls", []))
        try:
            from worker.secret_client import validate_assistant_submission
            await validate_assistant_submission(secret_payload, page, step)
            note("INFO", "开始填写登录用户名")
            await username.fill(credentials["username"], timeout=PAGE_TIMEOUT_MS)
            if await username.input_value(timeout=PAGE_TIMEOUT_MS) != credentials["username"]:
                raise _StepInputValidationError("Sensitive login fill failed", "username_selector")
            if secret_payload is not None:
                require_site_origin(page.url, secret_payload["base_url"], secret_payload.get("trusted_urls", []))
            await validate_assistant_submission(secret_payload, page, step, ("username",))
            note("INFO", "用户名填写完成并校验，开始填写密码")
            await password.fill(credentials["password"], timeout=PAGE_TIMEOUT_MS)
            if await password.input_value(timeout=PAGE_TIMEOUT_MS) != credentials["password"]:
                raise _StepInputValidationError("Sensitive login fill failed", "password_selector")
            note("INFO", "密码填写完成并校验")
        except _StepInputValidationError:
            raise
        except Exception:
            raise RuntimeError("Sensitive login fill failed") from None
        finally:
            credentials = None
        note("INFO", "开始点击登录按钮")
        if secret_payload is not None:
            require_site_origin(page.url, secret_payload["base_url"], secret_payload.get("trusted_urls", []))
        await validate_assistant_submission(secret_payload, page, step, ("username", "password"))
        await submit.click(timeout=PAGE_TIMEOUT_MS)
        note("INFO", "已填写登录凭据并提交")
        success = list(config.get("success_selector") or [])
        if success:
            wait_seconds = max(1, min(int(config.get("success_wait_seconds", 10)), 120))
            note("INFO", f"等待可见登录成功元素（最多 {wait_seconds} 秒，检查主页面及可见同源 iframe）")
            success_started = time.monotonic()
            if await _find_success_locator(
                page,
                success,
                wait_ms=wait_seconds * 1000,
                on_match=matched_note("登录成功元素"),
            ) is None:
                waited_ms = max(0, int((time.monotonic() - success_started) * 1000))
                evidence = await _login_success_evidence(page, success, waited_ms=waited_ms, log=note)
                return "fail", "登录成功元素未出现", {
                    "url": page.url, "reason": "success_evidence_missing", "field": "success_selector",
                    "submitted": True, "verified": False,
                    "success_evidence": evidence,
                }
        else:
            await asyncio.sleep(1)
        return "success", "登录完成", {"url": page.url, "submitted": True, "verified": bool(success)}

    if feature == "auth.password_verify":
        if secret_payload is None:
            return "fail", "密码验证需要受控的站点凭据访问", {"reason": "credentials_unavailable"}
        from worker.secret_client import resolve_secret, require_site_origin, validate_assistant_submission
        from worker.browser_input import hold_page_input

        require_site_origin(page.url, secret_payload["base_url"], secret_payload.get("trusted_urls", []))
        secret = await resolve_secret(secret_payload, step)
        record_secret(secret.get("password"))
        password_value = secret.pop("password", None)
        secret.clear()
        if not isinstance(password_value, str) or not password_value:
            return "fail", "未获取到站点账户密码", {"reason": "credentials_unavailable"}
        try:
            selectors_note("密码输入框", config.get("password_selector"))
            password_input = await _find_locator(
                page, list(config.get("password_selector") or []),
                wait_ms=SELECTOR_WAIT_MS, on_match=matched_note("密码输入框"),
            )
            if password_input is None:
                return "fail", "未找到密码输入框", {"reason": "selector_not_found", "field": "password_selector"}
            selectors_note("密码验证按钮", config.get("submit_selector"))
            submit = await _find_locator(
                page, list(config.get("submit_selector") or []),
                wait_ms=SELECTOR_WAIT_MS, on_match=matched_note("密码验证按钮"),
            )
            if submit is None:
                return "fail", "未找到密码验证按钮", {"reason": "selector_not_found", "field": "submit_selector"}
            async with hold_page_input(page):
                require_site_origin(page.url, secret_payload["base_url"], secret_payload.get("trusted_urls", []))
                await validate_assistant_submission(secret_payload, page, step)
                try:
                    await password_input.fill(password_value, timeout=PAGE_TIMEOUT_MS)
                    if await password_input.input_value(timeout=PAGE_TIMEOUT_MS) != password_value:
                        raise _StepInputValidationError("Sensitive password verification fill failed", "password_selector")
                except _StepInputValidationError:
                    raise
                except Exception:
                    raise RuntimeError("Sensitive password verification fill failed") from None
        finally:
            password_value = None
        note("INFO", "验证密码已填写并校验")
        async with hold_page_input(page):
            require_site_origin(page.url, secret_payload["base_url"], secret_payload.get("trusted_urls", []))
            await validate_assistant_submission(secret_payload, page, step, ("password",))
            try:
                await submit.click(timeout=PAGE_TIMEOUT_MS)
            except Exception:
                raise RuntimeError("Password verification submit failed") from None
        note("INFO", "密码验证已提交")
        success = list(config.get("success_selector") or [])
        if success:
            if await _find_locator(
                page, success, wait_ms=SELECTOR_WAIT_MS,
                on_match=matched_note("密码验证成功元素"),
            ) is None:
                return "fail", "密码验证成功元素未出现", {
                    "url": page.url, "submitted": True, "verified": False,
                    "reason": "success_evidence_missing", "field": "success_selector",
                }
            return "success", "密码验证通过", {"url": page.url, "submitted": True, "verified": True}
        await asyncio.sleep(1)
        return "success", "密码验证已提交，未配置成功元素检查", {"url": page.url, "submitted": True, "verified": False}

    if feature == "challenge.2fa":
        if secret_payload is not None:
            from worker.secret_client import resolve_secret
            from worker.secret_client import require_site_origin
            require_site_origin(page.url, secret_payload["base_url"], secret_payload.get("trusted_urls", []))
            secret = await resolve_secret(secret_payload, step)
            record_secret(secret.get("code"))
            code = secret.pop("code", None)
            fetch_status, fetch_message = "fail", "TOTP provider unavailable"
        else:
            code, fetch_status, fetch_message = await _fetch_otp(
                twofauth, str(config.get("service_hint") or "")
            )
            record_secret(code)
        if code is None:
            return fetch_status, fetch_message, {}
        selectors_note("动态验证码输入框", config.get("code_selector"))
        code_input = await _find_locator(
            page,
            list(config.get("code_selector") or []),
            wait_ms=SELECTOR_WAIT_MS,
            on_match=matched_note("动态验证码输入框"),
        )
        if code_input is None:
            return "fail", "未找到动态验证码输入框", {"reason": "selector_not_found", "field": "code_selector"}
        if secret_payload is not None:
            require_site_origin(page.url, secret_payload["base_url"], secret_payload.get("trusted_urls", []))
        try:
            from worker.secret_client import validate_assistant_submission
            from worker.browser_input import hold_page_input
            async with hold_page_input(page):
                await validate_assistant_submission(secret_payload, page, step)
                if private is not None:
                    target = await private.target(page, list(config.get("code_selector") or []), "code")
                    if config.get("submit_selector"):
                        await private.target(page, list(config["submit_selector"]), "submit")
                    await private.mutate(target, value=code)
                else:
                    await code_input.fill(code, timeout=PAGE_TIMEOUT_MS)
                await validate_assistant_submission(secret_payload, page, step, ("code",))
        except Exception:
            raise RuntimeError("Sensitive TOTP fill failed") from None
        submit_selectors = list(config.get("submit_selector") or [])
        if not submit_selectors:
            note("INFO", f"动态验证码已填写（长度 {len(code)}），由后续步骤提交")
            return "success", "动态验证码已填写，等待后续步骤提交", {
                "url": page.url,
                "submitted": False,
            }
        selectors_note("动态验证码提交按钮", config.get("submit_selector"))
        submit = await _find_locator(
            page,
            submit_selectors,
            wait_ms=SELECTOR_WAIT_MS,
            on_match=matched_note("动态验证码提交按钮"),
        )
        if submit is None:
            return "fail", "未找到动态验证码提交按钮", {"reason": "selector_not_found", "field": "submit_selector"}
        if secret_payload is not None:
            require_site_origin(page.url, secret_payload["base_url"], secret_payload.get("trusted_urls", []))
        async with hold_page_input(page):
            await validate_assistant_submission(secret_payload, page, step, ("code",))
            await submit.click(timeout=PAGE_TIMEOUT_MS)
        note("INFO", f"动态验证码已提交（长度 {len(code)}）")
        success = list(config.get("success_selector") or [])
        if success and await _find_locator(
            page,
            success,
            wait_ms=SELECTOR_WAIT_MS,
            on_match=matched_note("两步验证成功元素"),
        ) is None:
            return "fail", "两步验证成功元素未出现", {
                "url": page.url, "reason": "success_evidence_missing", "field": "success_selector",
                "submitted": True, "verified": False,
            }
        if not success:
            await asyncio.sleep(1)
        return "success", "两步验证完成", {"url": page.url}

    if feature == "challenge.turnstile":
        return await _run_turnstile(page, config, cfbox, cancel, xdisplay, lane_key, note)

    if feature == "challenge.altcha":
        return await _run_altcha(page, config, note)

    if feature == "challenge.slider":
        return await _run_slider(page, config, cancel, note)

    if feature == "challenge.image_captcha":
        return await _run_image_captcha(
            page, config, note, vision, capture_image, plugin_runtimes
        )

    if feature == "sign.button":
        button_selectors = list(config.get("button_selector") or [])
        selectors_note("签到按钮", button_selectors)
        button = await _find_locator(
            page,
            button_selectors,
            wait_ms=SELECTOR_WAIT_MS,
            on_match=matched_note("签到按钮"),
        )
        if button is None:
            return "fail", "未找到签到按钮", {"reason": "selector_not_found", "field": "button_selector"}
        await button.click(timeout=PAGE_TIMEOUT_MS)
        note("INFO", "签到按钮已点击")
        confirm = list(config.get("confirm_selector") or [])
        if confirm:
            selectors_note("签到确认按钮", confirm)
            target = await _find_locator(
                page,
                confirm,
                wait_ms=SELECTOR_WAIT_MS,
                on_match=matched_note("签到确认按钮"),
            )
            if target is not None:
                await target.click(timeout=PAGE_TIMEOUT_MS)
                note("INFO", "签到确认按钮已点击")
        await asyncio.sleep(1)
        signals = list(config.get("success_signals") or [])
        if not await _check_success_text(page, signals, note):
            return "unknown", "未识别到签到成功信号", {
                "url": page.url, "reason": "success_evidence_missing", "field": "success_signals",
            }
        return "success", "签到按钮已点击", {"url": page.url}

    if feature in {"sign.iframe_form", "sign.iframe.image_captcha"}:
        return await _run_iframe_image_captcha(
            page, config, note, vision, capture_image, plugin_runtimes
        )

    if feature == "challenge.grid_captcha":
        from worker.grid_captcha import run_grid_captcha

        return await run_grid_captcha(page, config, note, vision, capture_image)

    if feature == "sign.image_quiz":
        return await _run_image_quiz(page, config, note, vision, capture_image)

    if feature == "sign.unit3d_claim":
        claim_selectors = list(config.get("claim_selector") or [])
        selectors_note("Claim 按钮", claim_selectors)
        claim = await _find_locator(
            page,
            claim_selectors,
            wait_ms=SELECTOR_WAIT_MS,
            on_match=matched_note("Claim 按钮"),
        )
        if claim is None:
            return "fail", "未找到 Claim 按钮", {"reason": "selector_not_found", "field": "claim_selector"}
        await claim.click(timeout=PAGE_TIMEOUT_MS)
        note("INFO", "Claim 按钮已点击")
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        ok, success_by = await _wait_unit3d_success(page, config, note)
        if not ok:
            candidates = list(config.get("success_signals") or [])
            note(
                "WARN",
                f"成功条件未命中，文本候选：{_short(', '.join(map(str, candidates)))}",
            )
            return "unknown", "未识别到领取成功信号", {"url": page.url, "reason": "success_evidence_missing"}
        return "success", "Claim 已完成", {
            "url": page.url,
            "success_by": success_by,
        }

    if feature == "post.buy_card":
        return await _run_buy_card(
            page,
            config,
            list(resign_pending_dates or []),
            note,
        )

    if feature in CHECKPOINT_RESIGN_FEATURES:
        if feature == "post.resign_dialog":
            from worker.resign_dialog import run_resign_dialog as run_resign
        else:
            from worker.resign_loop import run_resign_loop as run_resign
        from worker.resign_checkpoint import make_resign_checkpoint, ResignCheckpointError
        payload = secret_payload or {}
        try:
            durable_progress = make_resign_checkpoint(payload, step.get("step_key"))
        except ResignCheckpointError:
            # Let the runner return its complete fail-closed result before any navigation.
            durable_progress = None
        return await run_resign(
            page, config, list(resign_pending_dates or []), note,
            today=payload.get("resign_today"),
            base_url=payload.get("base_url"), progress=resign_progress,
            durable_progress=durable_progress,
            **({"runtime": payload.setdefault("_resign_runtime", {}).setdefault(
                str(step.get("step_key") or ""), {})}
               if feature == "post.resign_loop" else {"unresolved": payload.get("resign_unresolved_dates")}),
        )

    if feature == "post.resign":
        return await _run_resign(
            page,
            config,
            list(resign_pending_dates or []),
            note,
        )

    return "waiting_human", f"执行器暂未支持功能块 {feature}", {}


async def execute_job(
    manager: Any,
    payload: dict[str, Any],
    cancel: asyncio.Event | None = None,
    on_step: StepCallback | None = None,
) -> dict[str, Any]:
    lane = manager.get(str(payload.get("lane_key") or "general"))
    async with lane.execution_semaphore:
        if cancel is not None and cancel.is_set():
            return {"status": "canceled", "message": "已取消", "steps": []}
        if on_step is not None:
            await on_step({"status": "running", "message": "执行中", "steps": []})
        async def wait_for_disconnect():
            while True:
                session = getattr(lane, "session", None)
                if session is not None and session.disconnected and lane.in_use:
                    return
                await asyncio.sleep(.2)

        last_progress: dict[str, Any] = {}
        business_result: dict[str, Any] | None = None

        async def track_progress(progress: dict[str, Any]) -> None:
            nonlocal last_progress, business_result
            if progress.get("_business_complete") is True:
                # Internal handoff only: terminal publication waits for cleanup.
                business_result = copy.deepcopy({key: value for key, value in progress.items()
                                                 if key != "_business_complete"})
                return
            if payload.get("assistant_consumption_plan") is not None or any(row.get("feature_type") in CHECKPOINT_RESIGN_FEATURES and
                   isinstance(row.get("data", {}).get("resign"), dict)
                   for row in progress.get("steps", [])):
                last_progress = copy.deepcopy(progress)
            if on_step is not None:
                await on_step(progress)

        def interrupted_result(status: str, message: str) -> dict[str, Any]:
            if business_result is not None:
                return copy.deepcopy(business_result)
            if payload.get("assistant_consumption_plan") is not None:
                return {**copy.deepcopy(last_progress), "status": "unknown",
                        "message": "Consumption run interrupted; verify before a fresh request",
                        "reason": "outcome_uncertain"}
            if not last_progress:
                return {"status": status, "message": message, "steps": []}
            partial = copy.deepcopy(last_progress)
            partial.update(status="waiting_human", message=message + "；补签检查点已保留，请人工核验")
            for row in partial.get("steps", []):
                if row.get("status") == "running":
                    row.update(status="waiting_human", message=partial["message"])
            return partial

        execution = asyncio.create_task(_execute_admitted_job(manager, payload, cancel, track_progress))
        watcher = asyncio.create_task(cancel.wait()) if cancel is not None else None
        backend_watcher = asyncio.create_task(wait_for_disconnect())
        try:
            done, _ = await asyncio.wait(
                {execution, backend_watcher, *([watcher] if watcher else [])},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if backend_watcher in done or (watcher is not None and watcher in done):
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
                if backend_watcher in done:
                    return interrupted_result("error", "Browser backend disconnected; task was not replayed")
                return interrupted_result("canceled", "已取消")
            return await execution
        except asyncio.CancelledError:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            if business_result is not None or last_progress:
                return interrupted_result("canceled", "已取消")
            raise
        finally:
            execution.cancel()
            if watcher is not None:
                watcher.cancel()
            backend_watcher.cancel()
            await asyncio.gather(execution, backend_watcher, *([watcher] if watcher is not None else []), return_exceptions=True)
            if business_result is not None:
                await _emit_progress(on_step, business_result["status"], business_result["message"],
                                     business_result["steps"], business_result["artifacts"])


async def _execute_admitted_job(
    manager: Any,
    payload: dict[str, Any],
    cancel: asyncio.Event | None = None,
    on_step: StepCallback | None = None,
) -> dict[str, Any]:
    lane_key = str(payload.get("lane_key") or "general")
    steps = payload.get("steps") or []
    # Code-owned builtin categories prevent a payload label from hiding a post handler.
    builtin_categories = {
        feature: category for category, features in {
            "flow": ("flow.open", "flow.jump", "flow.result"),
            "auth": ("auth.form_login", "auth.password_verify"),
            "challenge": ("challenge.2fa", "challenge.turnstile", "challenge.altcha",
                          "challenge.slider", "challenge.image_captcha", "challenge.grid_captcha"),
            "sign": ("sign.button", "sign.iframe.image_captcha", "sign.image_quiz", "sign.unit3d_claim"),
            "action": ("action.navigate", "action.click", "action.fill", "action.wait"),
            "post": ("post.buy_card", "post.resign", "post.resign_loop", "post.resign_dialog"),
        }.items() for feature in features
    }
    consumption_handlers = {"post.buy_card", "post.resign", "post.resign_loop", "post.resign_dialog"}
    plan = payload.get("assistant_consumption_plan")
    consumption_steps: set[str] = set()
    if payload.get("assistant_run") is True:
        consuming = [step for step in steps if step.get("enabled", True)
                     and (step.get("category") == "post"
                          or builtin_categories.get(step.get("feature_type")) == "post")]
        if consuming or plan is not None:
            try:
                from zoneinfo import ZoneInfo

                review = plan["review"]
                approved = {item["step_key"]: item for item in review["steps"]}
                consumption_steps = {step["step_key"] for step in consuming}
                dates = review["dates"]
                if (not consuming or len(consumption_steps) != len(consuming)
                        or len(approved) != len(review["steps"]) or set(approved) != consumption_steps
                        or payload.get("attempt") != 1 or len(dates) > 365
                        or dates != sorted(set(dates))
                        or dates != payload.get("resign_pending_dates")
                        or review["today"] != payload.get("resign_today")
                        or review["today"] != datetime.now(ZoneInfo(review["timezone"])).date().isoformat()
                        or any(datetime.strptime(day, "%Y-%m-%d").date().isoformat() != day
                               or day >= review["today"] for day in dates)):
                    raise ValueError
                for step in consuming:
                    item = approved[step["step_key"]]
                    if (step["feature_type"] not in consumption_handlers
                            or step.get("category", builtin_categories[step["feature_type"]]) != "post"
                            or item["feature_type"] != step["feature_type"]):
                        raise ValueError
                    if step["feature_type"] == "post.buy_card":
                        config = step.get("config") or {}
                        quantity, price = config.get("purchase_quantity", 0), config.get("max_price")
                        maximum = quantity or len(dates)
                        if (type(quantity) is not int or not 0 <= quantity <= 365
                                or type(price) is not int or price <= 0
                                or item["purchase_quantity"] != quantity
                                or item["maximum_quantity"] != maximum
                                or item["max_price"] != price
                                or item["maximum_spend"] != maximum * price):
                            raise ValueError
            except (ValueError, TypeError, KeyError):
                return {"status": "fail", "message": "Consumption approval plan missing or changed",
                        "steps": [], "reason": "assistant_configuration_changed"}
    consumed_steps: set[str] = set()
    # Job-local continuation state must survive flow.jump pruning step results.
    resign_runtime: dict[str, dict[str, Any]] = {}
    payload["_resign_runtime"] = resign_runtime
    completed_resign_dates: set[str] = set()
    # Presence is authoritative: only old payloads may use the shared runtime.
    model_runtimes = payload.get("feature_model_runtimes")
    if "feature_model_runtimes" in payload and (
        not isinstance(model_runtimes, dict)
        or any(runtime is not None and not isinstance(runtime, dict)
               for runtime in model_runtimes.values())
    ):
        return {"status": "fail", "message": "Invalid feature model runtimes",
                "steps": [], "reason": "model_runtime_invalid"}
    model_runtimes = copy.deepcopy(model_runtimes)
    signal_rows = payload.get("signals") or []
    signals = {
        str(item.get("signal_key")): item
        for item in signal_rows
        if isinstance(item, dict) and item.get("signal_key")
    }
    credentials = None
    twofauth = None
    cfbox = payload.get("cfbox")
    if not isinstance(cfbox, dict):
        cfbox = None

    credential_task: asyncio.Task[dict[str, str]] | None = None
    payment_steps: set[str] = set()
    resign_dispatched_steps: set[str] = set()

    def finish(
        status: str,
        message: str,
        step_results: list[dict[str, Any]],
        output_rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        for key, state in resign_runtime.items():
            if not state:
                continue
            existing = next((item for item in step_results if item.get("step_key") == key), None)
            if existing is not None:
                existing.setdefault("data", {})["resign"] = copy.deepcopy(state)
            else:
                source = next(item for item in steps if item.get("step_key") == key)
                step_results.append(_step_result(source, "waiting_human" if state.get("remaining") else "success",
                                                "补签循环累计状态", started=_now(),
                                                data={"resign": copy.deepcopy(state)}))
                existing = step_results[-1]
            if state.get("remaining"):
                incomplete = "补签循环仍有未确认日期，请返回补签步骤核验或人工处理"
                if existing.get("status") in {"success", "skipped"}:
                    existing.update(status="waiting_human", message=incomplete)
                existing["data"].update(value=False, message=existing.get("message") or incomplete)
                for output in output_rows:
                    if output.get("step_key") == key:
                        output.update(value=False, message=incomplete)
                if status == "success":
                    status, message = "waiting_human", incomplete
        level = "INFO" if status in {"success", "skipped"} else "ERROR"
        job_artifacts.log(level, f"job end status={status} message={message}")
        return {
            "status": status,
            "message": message,
            "steps": step_results,
            "outputs": output_rows,
            "artifacts": job_artifacts.snapshot(),
        }

    from worker.page_lifecycle import JobPages

    lane = manager.get(lane_key)
    pages = JobPages(None, lane, str(payload.get("job_id") or ""))
    if "dispatch_reservation" in payload:
        lease_id, _lane = await manager.dispatch.lease_job(
            lane_key, payload.get("run_id"), payload["dispatch_reservation"],
            job_id=payload.get("job_id"), attempt=payload.get("attempt"),
        )
    else:
        lease_id, _lane = await manager.lease_lane(lane_key)
    try:
        # Claims must succeed before JobArtifacts can truncate an existing log.
        job_artifacts = JobArtifacts(payload)
        job_artifacts.pages = pages
        job_artifacts.log("INFO", f"申请浏览器车道 lane={lane_key}")
        job_artifacts.log(
            "INFO",
            f"浏览器就绪 lane={lane_key} display={_lane.get('display') or '-'} "
            f"browser={_lane.get('browser') or '-'} lease={lease_id[:12]} "
            f"profile={_lane.get('profile') or '-'}",
        )
    except BaseException:
        await pages.cleanup(manager, lane_key, lease_id, lambda *_args: None)
        raise
    page = None
    stage = "cookies"

    async def setup_progress(stage):
        job_artifacts.log("INFO", f"browser_setup_{stage}")
        if on_step is not None:
            await on_step({"status": "running", "message": f"browser_setup_{stage}",
                           "stage": stage, "steps": [], "artifacts": job_artifacts.snapshot()})

    try:
        if lane.context is None:
            raise RuntimeError
        cookie_rows = payload.get("cookies")
        if isinstance(cookie_rows, list) and cookie_rows:
            await setup_progress(stage)
            await job_artifacts.pages.setup(lambda: lane.context.add_cookies(cookie_rows))
        stage = "new_page"
        await setup_progress(stage)
        page = await job_artifacts.pages.setup(lane.context.new_page, creates_page=True)
        stage = "focus"
        await setup_progress(stage)
        await job_artifacts.pages.setup(page.bring_to_front)
        job_key = str(payload.get("job_id") or "")
        if job_key:
            lane.task_pages[job_key] = page
        job_artifacts.log("INFO", "新建任务标签页并置前")
    except BaseException as exc:
        await job_artifacts.pages.cleanup(manager, lane_key, lease_id, job_artifacts.log)
        if isinstance(exc, asyncio.CancelledError):
            raise
        reason = "browser_setup_timeout" if isinstance(exc, asyncio.TimeoutError) else "browser_setup_failed"
        final = finish("fail" if stage == "cookies" else "error", reason, [], [])
        final.update(reason=reason, stage=stage)
        return final

    results: list[dict[str, Any]] = []
    outputs: list[dict[str, Any]] = []
    job_status = "success"
    job_message = "执行中"

    async def emit_progress() -> None:
        await _emit_progress(
            on_step,
            job_status,
            job_message,
            results,
            job_artifacts.snapshot(),
        )

    async def resign_progress(data: dict[str, Any]) -> None:
        results[-1]["data"] = copy.deepcopy(data)
        await emit_progress()

    try:
        if steps:
            step_index_by_key = {
                str(item.get("step_key")): idx
                for idx, item in enumerate(steps)
                if isinstance(item, dict) and item.get("step_key")
            }
            index = 0
            jump_total = 0
            while index < len(steps):
                page = job_artifacts.pages.active
                step = steps[index]
                if not isinstance(step, dict):
                    index += 1
                    continue
                started = _now()
                if cancel is not None and cancel.is_set():
                    results.append(_step_result(step, "canceled", "已取消", started=started))
                    job_status = "canceled"
                    job_message = "执行已取消"
                    job_artifacts.log("WARN", "job canceled before next step")
                    await emit_progress()
                    break
                if not step.get("enabled", True):
                    results.append(
                        _step_result(step, "skipped", "步骤已禁用", started=started)
                    )
                    job_artifacts.log(
                        "INFO", f"step {step.get('step_key')} skipped: disabled"
                    )
                    await emit_progress()
                    index += 1
                    continue
                pending_dates = [value for value in (payload.get("resign_pending_dates") or [])
                                 if value not in completed_resign_dates]
                condition = await _condition_allows(
                    page,
                    step.get("condition"),
                    signals,
                    job_artifacts.log,
                    {
                        SYSTEM_RESIGN_SIGNAL_KEY: (
                            bool(pending_dates),
                            f"待补签 {len(pending_dates)} 天"
                            if pending_dates
                            else "没有未签到日期",
                        )
                    },
                )
                if condition == "false":
                    results.append(
                        _step_result(step, "skipped", "执行条件不满足", started=started)
                    )
                    job_artifacts.log(
                        "INFO", f"step {step.get('step_key')} skipped: condition false"
                    )
                    await emit_progress()
                    index += 1
                    continue
                if condition == "unknown":
                    results.append(
                        _step_result(
                            step,
                            "waiting_human",
                            "执行条件无法判断，需要人工接管",
                            started=started,
                        )
                    )
                    job_status = "waiting_human"
                    job_message = "执行条件无法判断，需要人工接管"
                    job_artifacts.log(
                        "WARN",
                        f"step {step.get('step_key')} waiting_human: condition unknown",
                    )
                    await job_artifacts.capture_failure(
                        page,
                        str(step.get("step_key") or ""),
                        str(
                            step.get("label")
                            or step.get("feature_type")
                            or step.get("step_key")
                            or "步骤"
                        ),
                        "未成功",
                    )
                    await emit_progress()
                    break

                job_artifacts.log(
                    "INFO",
                    f"step {step.get('step_key')} start "
                    f"feature={step.get('feature_type')}",
                )
                results.append(_step_result(step, "running", "执行中", started=started))
                await emit_progress()
                feature_type = str(step.get("feature_type") or "")
                runtime = (model_runtimes.get(feature_type) if model_runtimes is not None
                           else payload.get("vision"))
                if not isinstance(runtime, dict):
                    runtime = {}
                selected_model = {**runtime, "execution_settings":
                                  (payload.get("feature_execution_settings") or {}).get(feature_type, {})}
                timeout = step.get("timeout_seconds")
                if timeout is None:
                    if feature_type == "post.buy_card":
                        step_config = step.get("config")
                        if not isinstance(step_config, dict):
                            step_config = {}
                        timeout = _buy_timeout(step_config)
                    elif feature_type == "post.resign":
                        timeout = 300
                    elif feature_type == "post.resign_loop":
                        from worker.resign_loop import resign_loop_timeout
                        timeout = resign_loop_timeout(step.get("config") or {}, pending_dates)
                    elif feature_type == "post.resign_dialog":
                        from worker.resign_dialog import resign_dialog_timeout
                        timeout = resign_dialog_timeout(step.get("config") or {}, pending_dates)
                    elif feature_type == "challenge.grid_captcha":
                        grid_config = step.get("config") or {}
                        refreshes = max(0, min(int(grid_config.get("max_refresh", 3)), 10))
                        model_timeout = float(selected_model.get("timeout") or 30)
                        success_wait = max(1, min(int(grid_config.get("success_wait_seconds", 10)), 120))
                        timeout = min(3600, (refreshes + 1) * (model_timeout + 20 + success_wait) + 30)
                    elif feature_type in {"flow.open", "action.navigate"}:
                        step_config = step.get("config")
                        if not isinstance(step_config, dict):
                            step_config = {}
                        retry_count = max(
                            0, min(int(step_config.get("retry_count") or 0), 20)
                        )
                        timeout = min(
                            3600, max(30, (retry_count + 1) * (PAGE_TIMEOUT_MS // 1000 + 2))
                        )
                    elif feature_type in {"flow.result", "action.result"} and (
                        (payload.get("feature_execution_settings") or {}).get(feature_type, {}).get("llm_enabled")
                        and (step.get("config") or {}).get("llm_enabled") is True
                    ):
                        timeout = min(3600, max(30, float(selected_model.get("timeout") or 30) + 35))
                    else:
                        timeout = 30
                step_key_value = str(step.get("step_key") or "") or None
                step_label_value = str(
                    step.get("label")
                    or step.get("feature_type")
                    or step.get("step_key")
                    or "步骤"
                )

                async def capture_captcha(
                    image_bytes: bytes,
                    caption: str = "验证码截图",
                    *,
                    step_key: str | None = step_key_value,
                    step_label: str = step_label_value,
                ) -> None:
                    job_artifacts.save_image(
                        image_bytes,
                        prefix="captcha",
                        label=f"{caption} · {step_label}",
                        step_key=step_key,
                    )
                    await emit_progress()

                feature = str(step.get("feature_type") or "")
                loop_resign = feature == "post.resign_loop"
                if payload.get("assistant_run") is True and step.get("step_key") in consumption_steps:
                    key = str(step.get("step_key") or "")
                    if ((key in consumed_steps and not (loop_resign and resign_runtime.get(key))) or
                            plan["review"]["today"] != datetime.now(ZoneInfo(plan["review"]["timezone"])).date().isoformat()):
                        job_status, job_message = "waiting_human", "Consumption step cannot execute twice; request fresh approval"
                        results[-1] = _step_result(step, job_status, job_message, started=started)
                        await emit_progress()
                        break
                    consumed_steps.add(key)
                # Opt in only for host-tagged assistant runs. A raised error after
                # dispatch cannot prove that a business action was not applied.
                assistant_side_effect = payload.get("assistant_run") is True and (
                    feature.startswith(("auth.", "sign.", "post."))
                    or feature in {"action.click", "action.fill", "challenge.2fa",
                                   "challenge.altcha", "challenge.slider",
                                   "challenge.image_captcha", "challenge.grid_captcha"}
                )
                try:
                    status, message, data = await asyncio.wait_for(
                        _run_step(
                            page,
                            step,
                            credentials,
                            twofauth,
                            cfbox,
                            cancel,
                            lane.session.desktop if getattr(lane, "session", None) else None,
                            lane_key,
                            job_artifacts.log,
                            list(payload.get("resign_pending_dates") or []),
                            selected_model,
                            capture_captcha,
                            credential_task,
                            dict(payload.get("plugin_runtimes") or {}),
                            getattr(manager, "plugin_broker", None),
                            job_artifacts,
                            payload,
                            **({"resign_progress": resign_progress}
                               if step.get("feature_type") in CHECKPOINT_RESIGN_FEATURES else {}),
                        ),
                        timeout=max(1, int(timeout)),
                    )
                except asyncio.TimeoutError:
                    status = ("unknown" if assistant_side_effect else
                              "waiting_human" if feature in {"post.buy_card", *CHECKPOINT_RESIGN_FEATURES} else "fail")
                    message = f"步骤超时（{timeout}s）"
                    data = {**(results[-1].get("data", {}) if feature in CHECKPOINT_RESIGN_FEATURES else {}),
                            "reason": "outcome_uncertain" if assistant_side_effect else "execution_timeout",
                            "stage": "step_execution"}
                    if assistant_side_effect:
                        data["cause"] = "execution_timeout"
                except asyncio.CancelledError:
                    partial = results[-1].get("data", {}) if feature in CHECKPOINT_RESIGN_FEATURES else {}
                    job_status = ("unknown" if plan is not None and assistant_side_effect else
                                  "waiting_human" if "resign" in partial else "canceled")
                    if job_status == "unknown":
                        partial = {**partial, "reason": "outcome_uncertain"}
                    results[-1] = _step_result(step, job_status, "已取消", started=started, data=partial)
                    job_message = "执行已取消"
                    job_artifacts.log("WARN", f"step {step.get('step_key')} canceled")
                    await emit_progress()
                    break
                except Exception as exc:
                    if payload.get("assistant_run") is True:
                        status = "unknown" if assistant_side_effect else "fail"
                        message = "步骤执行异常，结果不确定，请核验后再操作" if assistant_side_effect else "步骤执行失败"
                        data = {"reason": "outcome_uncertain" if assistant_side_effect else "execution_error",
                                "stage": "step_execution"}
                        if assistant_side_effect:
                            data["cause"] = "execution_error"
                    else:
                        error_type = "RuntimeError" if isinstance(exc, _StepInputValidationError) else type(exc).__name__
                        status, message = "fail", f"{error_type}: {str(exc)[:500]}"
                        data = {"reason": "execution_error", "stage": "step_execution"}
                    if isinstance(exc, _StepInputValidationError):
                        if not assistant_side_effect:
                            data["reason"] = "input_validation_failed"
                        data["field"] = exc.field
                if feature in CHECKPOINT_RESIGN_FEATURES:
                    partial = results[-1].get("data", {})
                    if "resign" not in data and "resign" in partial:
                        data = {**data, "resign": partial["resign"]}
                    resign = data.get("resign", {})
                    if loop_resign and resign:
                        resign_runtime[str(step.get("step_key") or "")] = copy.deepcopy(resign)
                    if status == "success":
                        completed_resign_dates.update(resign.get("already_signed") or [])
                        completed_resign_dates.update(item["date"] for item in resign.get("success", [])
                                                     if isinstance(item, dict) and "date" in item)
                    if resign.get("submit_attempted") or resign.get("success"):
                        payment_steps.add(str(step.get("step_key") or ""))
                        resign_dispatched_steps.add(str(step.get("step_key") or ""))
                    if (status not in {"success", "skipped", "restart", "fallback_interaction"}
                            and not (assistant_side_effect and status == "unknown")):
                        status = "waiting_human"
                purchase = data.get("buy_card") if isinstance(data, dict) else None
                if plan is not None and status in {"restart", "fallback_interaction"}:
                    status, message = "unknown", "Consumption run cannot retry or switch lanes; verify and request fresh approval"
                    data = {**data, "reason": "outcome_uncertain"}
                if feature == "post.buy_card" and isinstance(purchase, dict) and (
                    purchase.get("payment_attempted") or purchase.get("bought", 0)
                ):
                    payment_steps.add(str(step.get("step_key") or ""))
                if payment_steps and status in {"restart", "fallback_interaction"}:
                    status = "waiting_human"
                    message = ("本任务已发起补签等副作用点击，禁止自动重跑，请人工核验"
                               if resign_dispatched_steps else
                               "本任务已发起购卡点击，禁止自动重跑以避免重复购买，请人工核验")
                page = job_artifacts.pages.active
                category = str(step.get("category") or feature.split(".", 1)[0])
                if category in {"sign", "post"} and status != "skipped":
                    data = {**data, "value": status == "success", "message": message}
                    outputs.append(
                        {
                            "step_key": step.get("step_key"),
                            "value": status == "success",
                            "message": message,
                        }
                    )
                results[-1] = _step_result(
                    step, status, message, started=started, data=data
                )
                level = "INFO" if status in {"success", "skipped"} else "ERROR"
                job_artifacts.log(
                    level,
                    f"step {step.get('step_key')} end status={status} message={message}",
                )
                if status not in {"success", "skipped"}:
                    await job_artifacts.capture_failure(
                        page,
                        str(step.get("step_key") or ""),
                        str(
                            step.get("label")
                            or step.get("feature_type")
                            or step.get("step_key")
                            or "步骤"
                        ),
                        "失败" if status in {"fail", "error"} else "未成功",
                    )
                await emit_progress()
                if (
                    feature == "flow.jump"
                    and status == "success"
                    and bool(data.get("matched"))
                    and str(data.get("action") or "jump") == "jump"
                ):
                    target = str(data.get("target_step_key") or "").strip()
                    target_index = step_index_by_key.get(target)
                    jump_config = step.get("config")
                    if not isinstance(jump_config, dict):
                        jump_config = {}
                    try:
                        jump_limit = max(0, min(int(jump_config.get("max_jumps", 10)), 100))
                    except (TypeError, ValueError):
                        jump_limit = 10
                    if target_index is None:
                        job_status = "fail"
                        job_message = f"跳转目标步骤不存在: {target}"
                        results[-1] = _step_result(
                            step, "fail", job_message, started=started, data=data
                        )
                        job_artifacts.log("ERROR", job_message)
                        await emit_progress()
                        break
                    loop_keys = {str(item.get("step_key") or "") for item in steps
                                 if item.get("feature_type") == "post.resign_loop"
                                 and resign_runtime.get(str(item.get("step_key") or ""))}
                    if (payment_steps - loop_keys).intersection(
                        str(item.get("step_key") or "") for item in steps[target_index:index + 1]
                    ):
                        job_status = "waiting_human"
                        job_message = ("跳转将重复执行已发起的补签等副作用步骤，已停止，请人工核验"
                                       if resign_dispatched_steps else
                                       "跳转将重复执行已发起的购卡步骤，已停止，请人工核验")
                        results[-1] = _step_result(step, job_status, job_message, started=started, data=data)
                        job_artifacts.log("WARN", job_message)
                        await emit_progress()
                        break
                    if jump_total >= jump_limit:
                        job_status = "fail"
                        job_message = f"跳转次数已达到上限 {jump_limit}"
                        results[-1] = _step_result(
                            step, "fail", job_message, started=started, data=data
                        )
                        job_artifacts.log("ERROR", job_message)
                        await emit_progress()
                        break
                    jump_total += 1
                    removed_keys = {
                        str(item.get("step_key"))
                        for item in steps[target_index:]
                        if isinstance(item, dict) and item.get("step_key")
                    }
                    results[:] = [
                        item
                        for item in results
                        if str(item.get("step_key")) not in removed_keys
                    ]
                    outputs[:] = [
                        item
                        for item in outputs
                        if str(item.get("step_key")) not in removed_keys
                    ]
                    job_artifacts.log(
                        "INFO",
                        f"条件跳转 {step.get('step_key')} -> {target} "
                        f"（{jump_total}/{jump_limit}）",
                    )
                    index = target_index
                    await emit_progress()
                    continue
                if status == "success":
                    if feature in {"flow.result", "action.result"}:
                        outputs.append(
                            {
                                "step_key": step.get("step_key"),
                                "value": data.get("value"),
                                "message": data.get("message"),
                            }
                        )
                        job_message = message
                        if data.get("value") is False:
                            job_status = "fail"
                        break
                    if category in {"sign", "post"}:
                        job_message = message
                if status in {"success", "skipped"}:
                    index += 1
                    continue
                if status == "canceled":
                    job_status = "canceled"
                    job_message = message
                    break
                if status == "waiting_human":
                    job_status = "waiting_human"
                    job_message = message
                    break
                if status == "restart":
                    job_status = "restart"
                    job_message = message
                    break
                if status == "fallback_interaction":
                    job_status = "fallback_interaction"
                    job_message = message
                    break
                if payload.get("assistant_run") is True and status == "unknown":
                    job_status = "unknown"
                    job_message = message
                    break
                policy = str(step.get("on_error") or "stop")
                if policy == "continue":
                    job_artifacts.log(
                        "WARN",
                        f"step {step.get('step_key')} failed with on_error=continue; "
                        "continuing without failing the job",
                    )
                    index += 1
                    continue
                if policy == "defer":
                    job_status = "deferred"
                elif policy == "escalate_interaction":
                    job_status = "waiting_human"
                elif status == "unknown":
                    job_status = "unknown"
                else:
                    job_status = "fail"
                job_message = message
                break
        if job_status == "success" and job_message == "执行中":
            job_message = "执行完成"
        if job_status != "success" and not job_artifacts.screenshots:
            reason = "失败" if job_status in {"fail", "error"} else "未成功"
            label = "任务失败" if reason == "失败" else "任务未成功"
            await job_artifacts.capture_failure(page, None, label, reason)
        final = finish(job_status, job_message, results, outputs)
        if on_step is not None:
            await on_step({**final, "_business_complete": True})
        if job_status == "success":
            exported = None
            try:
                exported = await job_artifacts.pages.setup(lambda: _export_site_cookies(
                    lane.context,
                    str(payload.get("base_url") or ""),
                    list(payload.get("cookies") or []),
                    job_artifacts.log,
                ))
            except asyncio.TimeoutError:
                job_artifacts.log("WARN", "cookie_export_timeout")
            except Exception:
                job_artifacts.log("WARN", "cookie_export_failed")
            if exported is not None:
                final["cookies"] = exported
                if on_step is not None:
                    await on_step({**final, "_business_complete": True})
        return final
    finally:
        if credential_task is not None:
            if not credential_task.done():
                credential_task.cancel()
            await asyncio.gather(credential_task, return_exceptions=True)
        await job_artifacts.pages.cleanup(manager, lane_key, lease_id, job_artifacts.log)
