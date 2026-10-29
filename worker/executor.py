from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx

WAIT_UNTIL = {"domcontentloaded", "load", "networkidle"}
SYSTEM_RESIGN_SIGNAL_KEY = "sys_resign_pending"
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
    ".cf-turnstile, div[id^='cf-chl-widget'], "
    "iframe[src*='challenges.cloudflare.com'], iframe[src*='challenge-platform']"
)
CF_VISIBLE_MARKER_SCRIPT = """() => {
    const selectors = [
        '.cf-turnstile',
        'div[id^="cf-chl-widget"]',
        'iframe[src*="challenges.cloudflare.com"]',
        'iframe[src*="challenge-platform"]',
    ];
    return selectors.some((selector) =>
        [...document.querySelectorAll(selector)].some((element) => {
            const rect = element.getBoundingClientRect();
            const style = getComputedStyle(element);
            return rect.width > 0
                && rect.height > 0
                && style.display !== 'none'
                && style.visibility !== 'hidden'
                && Number(style.opacity || 1) !== 0;
        })
    );
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
        except Exception as exc:
            self.log("WARN", f"failure screenshot failed: {type(exc).__name__}: {exc}")
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
):
    deadline = time.monotonic() + max(0, wait_ms) / 1000
    while True:
        for selector in selectors:
            if not selector:
                continue
            try:
                locator = page.locator(selector).first
                if await locator.count() and await locator.is_visible():
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
    try:
        marker = bool(
            await asyncio.wait_for(
                page.evaluate(CF_VISIBLE_MARKER_SCRIPT),
                timeout=FRAME_OP_TIMEOUT_SECONDS,
            )
        )
    except Exception:
        # A failed visibility check is not evidence of absence. Counting hidden
        # widgets here would also turn solved challenges into false positives.
        marker = None
    token_length = 0
    try:
        token_length = int(
            await asyncio.wait_for(
                page.evaluate(
                    """() => {
                        let length = 0;
                        for (const el of document.querySelectorAll(
                            '[name="cf-turnstile-response"]'
                        )) {
                            length = Math.max(length, String(el.value || '').length);
                        }
                        return length;
                    }"""
                ),
                timeout=FRAME_OP_TIMEOUT_SECONDS,
            )
            or 0
        )
    except Exception:
        pass
    try:
        text = await _page_text(page, strict=True)
        text_readable = True
    except Exception:
        text = ""
        text_readable = False
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
    urls: list[str] = []
    for frame in _page_frames(page):
        try:
            urls.append(str(frame.url))
        except Exception:
            continue
    frame_challenge = any(
        "challenges.cloudflare.com" in url or "challenge-platform" in url for url in urls
    )
    current_url = _cf_url_key(getattr(page, "url", ""))
    url_challenge = (
        "__cf_chl_" in current_url.casefold()
        or "challenge-platform" in current_url.casefold()
    )
    return {
        "marker": marker,
        "token_length": token_length,
        "challenge_text": any(word.casefold() in text for word in challenge_words),
        "text_readable": text_readable,
        "frame_challenge": frame_challenge,
        "url_challenge": url_challenge,
        "urls": urls,
    }


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


def _x11_screenshot(display: str) -> bytes | None:
    try:
        import mss
        from mss.tools import to_png

        # Each capture owns its X connection; never change process-wide DISPLAY.
        with mss.mss(display=display) as capture:
            frame = capture.grab({"left": 0, "top": 0, "width": SCREEN_WIDTH, "height": SCREEN_HEIGHT})
            return to_png(frame.rgb, frame.size)
    except Exception:
        return None


def _x11_click(display: str, x: float, y: float) -> None:
    subprocess.run(
        [
            "xdotool",
            "mousemove",
            str(int(round(x))),
            str(int(round(y))),
            "sleep",
            "0.1",
            "click",
            "1",
        ],
        env={**os.environ, "DISPLAY": display},
        capture_output=True,
        timeout=5,
        check=False,
    )


async def _scroll_cf_into_view(page: Any) -> None:
    try:
        locator = page.locator(CF_MARKER_SELECTOR).first
        if await locator.count():
            await locator.scroll_into_view_if_needed(timeout=3000)
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
    xdisplay: str | None,
    lane_key: str,
    log: Callable[[str, str], None] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    wait_seconds = max(1, min(120, int(config.get("wait_seconds") or 15)))
    try:
        async with asyncio.timeout(wait_seconds):
            return await _run_turnstile_detection(
                page, config, cfbox, cancel, xdisplay, lane_key, log
            )
    except TimeoutError:
        return "unknown", f"Turnstile 双重确认超时（{wait_seconds}s）", {"state": "unknown"}


async def _run_turnstile_detection(
    page: Any,
    config: dict[str, Any],
    cfbox: dict[str, Any] | None,
    cancel: asyncio.Event | None,
    xdisplay: str | None,
    lane_key: str,
    log: Callable[[str, str], None] | None = None,
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
    initial_url = _cf_url_key(getattr(page, "url", ""))
    previous_token_length = 0
    states: list[str] = []
    seen_cf = False
    no_cf_since: float | None = None
    first_pass = True
    reported_state: str | None = None
    challenge_reported = False
    while time.monotonic() < deadline:
        if cancel is not None and cancel.is_set():
            return "canceled", "已取消", {}
        state = await _cf_page_state(page)
        current_url = _cf_url_key(getattr(page, "url", ""))
        token_length = int(state.get("token_length") or 0)
        has_cf = (
            state["marker"]
            or state["challenge_text"]
            or state["url_challenge"]
        )
        progress = _cf_progress_reason(
            initial_url,
            current_url,
            token_length,
            previous_token_length,
            clicked,
        )
        previous_token_length = token_length
        if has_cf and progress and (
            state["challenge_text"] or state["url_challenge"]
            or progress != "Turnstile token 已更新"
        ):
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
        if has_cf:
            seen_cf = True
            if not (
                last_state == "verified" and token_length > 0
                and not state["challenge_text"] and not state["url_challenge"]
            ):
                no_cf_since = None

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
                and last_click_at is not None
                and time.monotonic() - last_click_at >= 4
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
                if use_x_fallback and xdisplay:
                    image = await asyncio.to_thread(_x11_screenshot, xdisplay)
                    width, height = SCREEN_WIDTH, SCREEN_HEIGHT
                else:
                    image = await page.screenshot(type="png")
                    viewport = page.viewport_size or {
                        "width": SCREEN_WIDTH,
                        "height": SCREEN_HEIGHT,
                    }
                    width, height = viewport["width"], viewport["height"]
                if not image:
                    return "fail", "CF 截图为空", {}
                async with httpx.AsyncClient(timeout=20) as client:
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
                states.append(last_state)
                if last_state != reported_state:
                    note("INFO", f"CFbox 识别状态：{last_state}")
                    reported_state = last_state
                # Navigation or widget rendering may occur during inference.
                state = await _cf_page_state(page)
                has_cf = bool(
                    state["marker"] or state["challenge_text"] or state["url_challenge"]
                )
                seen_cf = seen_cf or has_cf
                ready = await page.evaluate(
                    "() => document.readyState !== 'loading' && !!document.body"
                )
                if last_state == "verified":
                    seen_cf = True
                # A solved Turnstile can remain visible with its response token.
                solved_widget = (
                    last_state == "verified"
                    and int(state.get("token_length") or 0) > 0
                    and not state["challenge_text"]
                    and not state["url_challenge"]
                )
                if (
                    (not has_cf or solved_widget)
                    and state["marker"] is not None
                    and state["text_readable"]
                    and ready
                    # Unknown visual inference falls back to a readable, clear page.
                    and last_state in {"absent", "verified", "unknown"}
                ):
                    if no_cf_since is None:
                        no_cf_since = time.monotonic()
                    if time.monotonic() - no_cf_since >= 2:
                        message = (
                            "Cloudflare 验证已通过" if solved_widget
                            else "Cloudflare 挑战已消失" if seen_cf
                            else "未检测到 Cloudflare 挑战"
                        )
                        note("INFO", message)
                        return "success", message, {
                            "state": "passed" if seen_cf else "absent",
                            "cfbox_state": last_state,
                            "clicked": clicked,
                            "click_count": click_count,
                            "states": states,
                        }
                else:
                    no_cf_since = None
                if last_state not in {"absent", "verified", "unknown"}:
                    seen_cf = True
                if last_state == "absent" and has_cf:
                    last_state = "absent (but CF DOM is present)"
                center = body.get("checkbox_center_pct")
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
                            await asyncio.to_thread(_x11_click, xdisplay, x, y)
                            x_click_count += 1
                            click_mode = "x"
                        else:
                            await page.mouse.click(x, y)
                            page_click_count += 1
                            click_mode = "page"
                        clicked = True
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
        if not await _sleep(1, cancel):
            return "canceled", "已取消", {}
    suffix = "，已点击复选框" if clicked else ""
    return (
        "unknown",
        f"Turnstile 未在 {wait_seconds}s 内完成（最后状态 {last_state}{suffix}）",
        {
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
        if method == "ddddocr":
            text_value, confidence = await asyncio.to_thread(
                _classify_captcha, image_value
            )
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
            _log_captcha_result(log, "视觉模型", text_value)
            return text_value, None, None
        if method == "nexusphp_gd5":
            text_value, margin = await asyncio.to_thread(
                _recognize_nexusphp_gd5, image_value
            )
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
            scope, image, refresh_selectors, image_selectors, log
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
        if capture is not None:
            try:
                await capture(form["image"], "图片答题截图")
            except Exception as exc:
                log(
                    "WARN",
                    f"图片答题截图保存失败: {type(exc).__name__}: {str(exc)[:200]}",
                )
        from worker.vision import ImageQuizUnclear, answer_image_quiz

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
            if not await _refresh_quiz(page, config, log):
                return (
                    "waiting_human",
                    "视觉模型无法确定图片答题答案，且无法刷新题目，需要人工处理",
                    {"reason": "refresh_unavailable", "attempts": attempt + 1},
                )
            continue
        except ValueError:
            return (
                "waiting_human",
                "视觉模型调用失败，图片答题需要人工处理",
                {"reason": "vision_failed", "attempts": attempt + 1},
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
                await message.fill(message_text, timeout=PAGE_TIMEOUT_MS)
                log("INFO", "已填写图片答题留言")
        selected_row = form["option_rows"][selected - 1]
        try:
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
    except Exception as exc:
        log("WARN", f"导出站点 Cookie 失败：{type(exc).__name__}: {exc}")
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


async def _goto_if_needed(
    page: Any,
    url: str,
    wait_until: str,
    log: Callable[[str, str], None],
    label: str,
    *,
    retry_count: int = 0,
    retry_interval_seconds: float = 1.0,
) -> bool:
    current = str(getattr(page, "url", "") or "")
    if _same_navigation_url(current, url):
        log("INFO", f"当前页面已是目标地址，跳过{label}导航：{current}")
        return False
    retry_count = max(0, min(int(retry_count), 20))
    attempts = retry_count + 1
    log("INFO", f"{label}访问 {url}（最多尝试 {attempts} 次）")
    for attempt in range(1, attempts + 1):
        try:
            await page.goto(url, wait_until=wait_until, timeout=PAGE_TIMEOUT_MS)
            log("INFO", f"页面加载完成，当前地址 {page.url}")
            return True
        except Exception as exc:
            if attempt >= attempts or not _retryable_navigation_error(exc):
                log(
                    "ERROR",
                    f"{label}打开失败（第 {attempt}/{attempts} 次）："
                    f"{type(exc).__name__}: {exc}",
                )
                raise
            log(
                "WARN",
                f"{label}打开失败（第 {attempt}/{attempts} 次）："
                f"{type(exc).__name__}: {str(exc)[:300]}；"
                f"{retry_interval_seconds:g} 秒后重试",
            )
            await asyncio.sleep(max(0.0, retry_interval_seconds))
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


async def _extract_card_info(
    page: Any,
    config: dict[str, Any],
    log: Callable[[str, str], None],
) -> dict[str, Any] | None:
    raw_titles = config.get("card_title") or ["购买补签卡"]
    if isinstance(raw_titles, str):
        raw_titles = [raw_titles]
    titles = [str(item) for item in raw_titles if str(item).strip()]
    if not titles:
        titles = ["购买补签卡"]
    option_selector = str(config.get("option_selector") or 'input[name="option"]')
    price_index = int(config.get("price_column_index", 2))
    try:
        info = await page.evaluate(
            """(args) => {
                const headings = Array.from(document.querySelectorAll('h1,h2,h3,.magic-card-header'));
                let matches = [];
                for (const candidate of args.titles) {
                    matches = headings.filter(el => (el.textContent || '').includes(candidate));
                    if (matches.length) break;
                }
                if (matches.length !== 1) return null;
                const title = matches[0];
                if (!title) return null;
                const card = title.closest('form.magic-card');
                const row = title.closest('tr');
                const scope = card || row;
                if (!scope) return null;
                const options = scope.querySelectorAll(args.optionSelector);
                if (options.length !== 1) return null;
                const option = options[0];
                const form = card || option.form || title.closest('form');
                const cells = row ? row.querySelectorAll('td') : [];
                let price = cells.length > args.priceIndex
                    ? (cells[args.priceIndex].textContent || '').trim()
                    : '';
                if (card) {
                    const coins = [...card.querySelectorAll('.magic-footer-stats span')]
                        .filter(el => (el.textContent || '').includes('\uD83E\uDE99'));
                    if (coins.length !== 1) return null;
                    price = coins[0].textContent;
                }
                const fields = {};
                let submitName = 'submit';
                if (form) {
                    if (form.method.toLowerCase() !== 'post') return null;
                    for (const el of form.querySelectorAll('input[type="hidden"][name]')) {
                        if (el.matches(':disabled')) continue;
                        if (Object.hasOwn(fields, el.name)) return null;
                        fields[el.name] = el.value;
                    }
                    const buttons = [...scope.querySelectorAll('button,input')].filter(
                        el => el.type === 'submit' && !el.matches(':disabled'));
                    if (buttons.length !== 1) return null;
                    const button = buttons[0];
                    if (button.hasAttribute('formaction') || button.hasAttribute('formmethod')) return null;
                    submitName = button.name;
                    if (submitName) fields[submitName] = button.hasAttribute('value')
                        ? button.value : (button.textContent || '').trim();
                }
                fields[option.name || 'option'] = option.value;
                return {
                    option: option.value || option.getAttribute('value') || '',
                    price,
                    action: form ? form.action : null,
                    fields,
                    submitName,
                };
            }""",
            {"titles": titles, "optionSelector": option_selector, "priceIndex": price_index},
        )
    except Exception as exc:
        log("WARN", f"读取商店补签卡信息失败：{type(exc).__name__}: {exc}")
        return None
    if not isinstance(info, dict):
        return None
    option = str(info.get("option") or "")
    price_text = str(info.get("price") or "")
    price = _regex_number(price_text, r"([0-9][0-9,.]*)")
    if not option or price is None:
        return None
    info['price'] = price
    return info


async def _purchase_cards(
    page: Any,
    config: dict[str, Any],
    shop_url: str,
    needed: int,
    log: Callable[[str, str], None],
) -> dict[str, Any]:
    cards_before = _regex_number(
        await _page_body(page),
        str(config.get("card_count_regex") or r"[补補][签簽]卡[:：]\s*(\d+)"),
    )
    if cards_before is None:
        return {"status": "fail", "message": "未找到补签卡数量", "bought": 0}
    points_before = _regex_float(
        await _page_body(page),
        str(config.get("points_regex") or r"魔力值[^\d]*(\d[\d,.]*\d)"),
    )
    info = await _extract_card_info(page, config, log)
    if info is None:
        return {"status": "fail", "message": "未在商店找到购买补签卡商品", "bought": 0}
    price = info['price']
    max_price = config.get("max_price")
    if max_price is not None and price > int(max_price):
        return {
            "status": "fail",
            "message": f"补签卡价格 {price} 超过上限 {max_price}",
            "bought": 0,
        }
    affordable = int(points_before // price) if points_before is not None and price > 0 else 0
    to_buy = min(max(0, needed), affordable)
    log(
        "INFO",
        f"补签卡购买：当前 {cards_before} 张，需要 {needed} 张，"
        f"价格 {price}，可买 {affordable} 张，本次买 {to_buy} 张",
    )
    if to_buy <= 0:
        return {
            "status": "success",
            "message": "魔力值不足，未购买补签卡",
            "bought": 0,
            "needed": needed,
            "cards_before": cards_before,
            "cards_after": cards_before,
            "points_before": points_before,
            "points_after": points_before,
        }
    bought = 0
    failure = None
    for index in range(to_buy):
        if index:
            info = await _extract_card_info(page, config, log)
        if info is None:
            failure = "商店商品参数刷新失败"
            break
        price = info['price']
        if price <= 0 or (max_price is not None and price > int(max_price)):
            failure = "补签卡价格无效或超过上限"
            break
        points = _regex_float(await _page_body(page), str(
            config.get('points_regex') or r"魔力值[^\d]*(\d[\d,.]*\d)"))
        if points is None or points < price:
            break
        exchange_url = info.get('action') or shop_url
        if not info.get('action') or config.get('exchange_action', 'exchange') != 'exchange':
            parts = urlsplit(exchange_url)
            query = dict(parse_qsl(parts.query))
            query['action'] = str(config.get('exchange_action') or 'exchange')
            exchange_url = urlunsplit(parts._replace(query=urlencode(query)))
        exchange_url = urljoin(page.url, exchange_url)
        fields = dict(info.get('fields') or {'option': info['option']})
        submit_name = info.get('submitName', 'submit')
        if submit_name and (not info.get('action') or config.get('submit_value', '交换') != '交换'):
            fields[submit_name] = str(config.get('submit_value') or '交换')
        try:
            response = await page.evaluate(
                """async (args) => {
                    const url = new URL(args.url, location.href);
                    if (!['http:', 'https:'].includes(url.protocol) || url.origin !== location.origin)
                        throw new Error('Unsafe exchange form action');
                    const body = new URLSearchParams(args.fields);
                    const resp = await fetch(url.href, {
                        method: 'POST',
                        mode: 'same-origin',
                        redirect: 'error',
                        headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'},
                        body: body.toString(),
                    });
                    return {status: resp.status, text: (await resp.text()).slice(0, 1000)};
                }""",
                {"url": exchange_url, "fields": fields},
            )
        except Exception as exc:
            log("WARN", f"购买第 {index + 1} 张失败：{type(exc).__name__}: {exc}")
            failure = "补签卡购买请求失败"
            break
        text = str((response or {}).get("text") or "")
        status_code = int((response or {}).get("status") or 0)
        if status_code >= 400:
            log("WARN", f"购买第 {index + 1} 张返回 HTTP {status_code}：{text[:200]}")
            break
        if any(word in text for word in ("不足", "不够", "已经存在", "已有")):
            log("WARN", f"购买第 {index + 1} 张失败：{text[:200]}")
            break
        bought += 1
        log("INFO", f"已购买第 {bought}/{to_buy} 张补签卡")
        try:
            await page.goto(shop_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            await asyncio.sleep(2)
        except Exception as exc:
            log("WARN", f"购买后刷新商店失败：{type(exc).__name__}: {exc}")
            break
        if index < to_buy - 1:
            await asyncio.sleep(int(config.get("purchase_interval_seconds", 12)))
    cards_after = _regex_number(
        await _page_body(page),
        str(config.get("card_count_regex") or r"[补補][签簽]卡[:：]\s*(\d+)"),
    )
    points_after = _regex_float(
        await _page_body(page),
        str(config.get("points_regex") or r"魔力值[^\d]*(\d[\d,.]*\d)"),
    )
    if bought >= needed:
        message = f"已购买 {bought} 张补签卡，当前 {cards_after if cards_after is not None else '-'} 张"
    elif bought > 0:
        message = (
            f"魔力值不足，已购买 {bought}/{needed} 张补签卡，"
            f"当前 {cards_after if cards_after is not None else '-'} 张"
        )
    else:
        message = "魔力值不足，未购买补签卡"
    return {
        "status": "fail" if failure else "success",
        "message": failure or message,
        "bought": bought,
        "needed": needed,
        "cards_before": cards_before,
        "cards_after": cards_after,
        "points_before": points_before,
        "points_after": points_after,
    }


async def _run_buy_card(
    page: Any,
    config: dict[str, Any],
    pending_dates: list[str],
    log: Callable[[str, str], None],
) -> tuple[str, str, dict[str, Any]]:
    shop_url = urljoin(page.url, str(config.get("shop_url") or "{base_url}/mybonus.php"))
    if "{base_url}" in shop_url:
        return "fail", "商店地址未解析 {base_url}", {}
    if not pending_dates:
        return "success", "没有待补签日期，跳过购买补签卡", {
            "buy_card": {"needed": 0, "bought": 0}
        }
    await _goto_if_needed(page, shop_url, "domcontentloaded", log, "商店页面")
    await asyncio.sleep(2)
    body = await _page_body(page)
    cards = _regex_number(
        body, str(config.get("card_count_regex") or r"[补補][签簽]卡[:：]\s*(\d+)")
    )
    if cards is None:
        return "fail", "未找到补签卡数量", {}
    needed = max(0, len(pending_dates) - cards)
    if needed <= 0:
        return "success", f"补签卡已足够（当前 {cards} 张）", {
            "buy_card": {"needed": 0, "bought": 0, "cards_before": cards}
        }
    result = await _purchase_cards(page, config, shop_url, needed, log)
    data = {"buy_card": {**result, "cards_before": result.get("cards_before", cards)}}
    if result.get("status") == "success":
        return "success", str(result.get("message") or "补签卡购买完成"), data
    return "fail", str(result.get("message") or "补签卡购买失败"), data


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
            failed.append({"date": value, "message": f"{type(exc).__name__}: {exc}"})
            log("WARN", f"补签 {value} 请求失败：{type(exc).__name__}: {exc}")
            continue
        status_code = int((raw or {}).get("status") or 0)
        text = str((raw or {}).get("text") or "")
        if status_code >= 400:
            failed.append(
                {"date": value, "message": f"HTTP {status_code}: {text[:200]}"}
            )
            log("WARN", f"补签 {value} 返回 HTTP {status_code}")
            continue
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
            failed.append({"date": value, "message": message or "补签失败"})
            log("WARN", f"补签失败：{value} {message}")
    cards_after = max(0, cards_before - len(success))
    data = {
        "url": page.url,
        "resign": {
            "pending": pending_dates,
            "success": success,
            "already_signed": already_signed,
            "failed": failed,
            "remaining": remaining,
            "cards_before": cards_before,
            "cards_after": cards_after,
        },
    }
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


async def _run_step(
    page: Any,
    step: dict[str, Any],
    credentials: dict[str, str] | None,
    twofauth: dict[str, Any] | None,
    cfbox: dict[str, Any] | None,
    cancel: asyncio.Event | None,
    xdisplay: str | None,
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
) -> tuple[str, str, dict[str, Any]]:
    feature = str(step.get("feature_type") or "")
    config = step.get("config") or {}
    if not isinstance(config, dict):
        return "fail", "步骤配置不是对象", {}
    note = log or (lambda _level, _message: None)
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
        retry_count = int(config.get("retry_count") or 0) if feature == "flow.open" else 0
        await _goto_if_needed(
            page,
            url,
            wait_until,
            note,
            "页面",
            retry_count=retry_count,
        )
        error_page = await _detect_page_error(page, note)
        if error_page:
            return "fail", f"站点错误页面：{error_page}", {
                "url": page.url,
                "error_page": error_page,
            }
        return "success", f"已打开 {page.url}", {"url": page.url}

    if feature == "flow.jump":
        return await _run_flow_jump(page, config, note)

    if feature in {"flow.result", "action.result"}:
        value = bool(config.get("output_value", True))
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
            return "fail", "未找到点击目标", {}
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
            return "fail", "未找到输入框", {}
        if bool(config.get("clear", True)):
            await locator.fill("", timeout=PAGE_TIMEOUT_MS)
        await locator.fill(str(config.get("value") or ""), timeout=PAGE_TIMEOUT_MS)
        return "success", "填写完成", {}

    if feature == "auth.form_login":
        if secret_payload is not None:
            from worker.secret_client import resolve_secret
            from worker.secret_client import require_site_origin
            require_site_origin(page.url, secret_payload["base_url"])
            credentials = await resolve_secret(secret_payload, step)
        if not credentials and credential_task is not None:
            note("INFO", "等待 Bitwarden 登录凭据")
            try:
                credentials = await credential_task
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return "fail", f"Bitwarden 凭据获取失败: {str(exc)[:500]}", {}
        if not credentials:
            return "fail", "未配置站点登录凭据", {}
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
            return "fail", "未找到用户名输入框", {}
        selectors_note("密码输入框", password_selectors)
        password = await _find_locator(
            page,
            password_selectors,
            wait_ms=SELECTOR_WAIT_MS,
            on_match=matched_note("密码输入框"),
        )
        if password is None:
            return "fail", "未找到密码输入框", {}
        selectors_note("登录按钮", submit_selectors)
        submit = await _find_locator(
            page,
            submit_selectors,
            wait_ms=SELECTOR_WAIT_MS,
            on_match=matched_note("登录按钮"),
        )
        if submit is None:
            return "fail", "未找到登录按钮", {}
        if secret_payload is not None:
            require_site_origin(page.url, secret_payload["base_url"])
        try:
            await username.fill(credentials["username"], timeout=PAGE_TIMEOUT_MS)
            await password.fill(credentials["password"], timeout=PAGE_TIMEOUT_MS)
        except Exception:
            raise RuntimeError("Sensitive login fill failed") from None
        finally:
            credentials = None
        await submit.click(timeout=PAGE_TIMEOUT_MS)
        note("INFO", "已填写登录凭据并提交")
        success = list(config.get("success_selector") or [])
        if success:
            if await _find_locator(
                page,
                success,
                wait_ms=SELECTOR_WAIT_MS,
                on_match=matched_note("登录成功元素"),
            ) is None:
                return "fail", "登录成功元素未出现", {"url": page.url}
        else:
            await asyncio.sleep(1)
        return "success", "登录完成", {"url": page.url}

    if feature == "challenge.2fa":
        if secret_payload is not None:
            from worker.secret_client import resolve_secret
            from worker.secret_client import require_site_origin
            require_site_origin(page.url, secret_payload["base_url"])
            secret = await resolve_secret(secret_payload, step)
            code = secret.pop("code", None)
            fetch_status, fetch_message = "fail", "TOTP provider unavailable"
        else:
            code, fetch_status, fetch_message = await _fetch_otp(
                twofauth, str(config.get("service_hint") or "")
            )
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
            return "fail", "未找到动态验证码输入框", {}
        if secret_payload is not None:
            require_site_origin(page.url, secret_payload["base_url"])
        try:
            await code_input.fill(code, timeout=PAGE_TIMEOUT_MS)
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
            return "fail", "未找到动态验证码提交按钮", {}
        await submit.click(timeout=PAGE_TIMEOUT_MS)
        note("INFO", f"动态验证码已提交（长度 {len(code)}）")
        success = list(config.get("success_selector") or [])
        if success and await _find_locator(
            page,
            success,
            wait_ms=SELECTOR_WAIT_MS,
            on_match=matched_note("两步验证成功元素"),
        ) is None:
            return "fail", "两步验证成功元素未出现", {"url": page.url}
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
            return "fail", "未找到签到按钮", {}
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
            return "unknown", "未识别到签到成功信号", {"url": page.url}
        return "success", "签到按钮已点击", {"url": page.url}

    if feature in {"sign.iframe_form", "sign.iframe.image_captcha"}:
        return await _run_iframe_image_captcha(
            page, config, note, vision, capture_image, plugin_runtimes
        )

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
            return "fail", "未找到 Claim 按钮", {}
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
            return "unknown", "未识别到领取成功信号", {"url": page.url}
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
        if cancel is None:
            return await _execute_admitted_job(manager, payload, cancel, on_step)
        execution = asyncio.create_task(_execute_admitted_job(manager, payload, cancel, on_step))
        watcher = asyncio.create_task(cancel.wait()) if cancel is not None else None
        try:
            if watcher is not None:
                done, _ = await asyncio.wait({execution, watcher}, return_when=asyncio.FIRST_COMPLETED)
                if watcher in done:
                    execution.cancel()
                    await asyncio.gather(execution, return_exceptions=True)
                    return {"status": "canceled", "message": "已取消", "steps": []}
            return await execution
        finally:
            execution.cancel()
            if watcher is not None:
                watcher.cancel()
            await asyncio.gather(execution, *([watcher] if watcher is not None else []), return_exceptions=True)


async def _execute_admitted_job(
    manager: Any,
    payload: dict[str, Any],
    cancel: asyncio.Event | None = None,
    on_step: StepCallback | None = None,
) -> dict[str, Any]:
    lane_key = str(payload.get("lane_key") or "general")
    steps = payload.get("steps") or []
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

    job_artifacts = JobArtifacts(payload)
    credential_task: asyncio.Task[dict[str, str]] | None = None

    def finish(
        status: str,
        message: str,
        step_results: list[dict[str, Any]],
        output_rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        level = "INFO" if status in {"success", "skipped"} else "ERROR"
        job_artifacts.log(level, f"job end status={status} message={message}")
        return {
            "status": status,
            "message": message,
            "steps": step_results,
            "outputs": output_rows,
            "artifacts": job_artifacts.snapshot(),
        }

    job_artifacts.log("INFO", f"申请浏览器车道 lane={lane_key}")
    lease_id, _lane = await manager.lease_lane(lane_key)
    lane = manager.get(lane_key)
    job_artifacts.log(
        "INFO",
        f"浏览器就绪 lane={lane_key} display={_lane.get('display') or '-'} "
        f"browser={_lane.get('browser') or '-'} lease={lease_id[:12]} "
        f"profile={_lane.get('profile') or '-'}",
    )
    page = None
    if lane.context is None:
        await manager.release_lane(lane_key, lease_id)
        job_artifacts.log("INFO", f"释放浏览器车道 lane={lane_key} lease={lease_id[:12]}")
        return finish("error", "车道没有可用浏览器上下文", [], [])
    cookie_rows = payload.get("cookies")
    if isinstance(cookie_rows, list) and cookie_rows:
        try:
            await lane.context.add_cookies(cookie_rows)
            job_artifacts.log("INFO", f"已注入站点 Cookie {len(cookie_rows)} 条")
        except Exception as exc:
            await manager.release_lane(lane_key, lease_id)
            job_artifacts.log(
                "ERROR", f"导入站点 Cookie 失败: {type(exc).__name__}: {exc}"
            )
            return finish(
                "fail",
                f"导入站点 Cookie 失败: {type(exc).__name__}: {str(exc)[:300]}",
                [],
                [],
            )
    try:
        page = await lane.context.new_page()
        from worker.page_lifecycle import JobPages
        job_artifacts.pages = JobPages(page, lane, str(payload.get("job_id") or ""))
        await page.bring_to_front()
        job_key = str(payload.get("job_id") or "")
        if job_key:
            lane.task_pages[job_key] = page
        job_artifacts.log("INFO", "新建任务标签页并置前")
    except BaseException as exc:
        if getattr(job_artifacts, "pages", None) is not None:
            await job_artifacts.pages.close()
        await manager.release_lane(lane_key, lease_id)
        if isinstance(exc, asyncio.CancelledError):
            raise
        job_artifacts.log("INFO", f"释放浏览器车道 lane={lane_key} lease={lease_id[:12]}")
        return finish(
            "error", f"创建任务标签页失败: {type(exc).__name__}: {str(exc)[:300]}", [], []
        )

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
                pending_dates = list(payload.get("resign_pending_dates") or [])
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
                timeout = step.get("timeout_seconds")
                if timeout is None:
                    feature_type = str(step.get("feature_type") or "")
                    if feature_type == "post.buy_card":
                        step_config = step.get("config")
                        if not isinstance(step_config, dict):
                            step_config = {}
                        interval = int(step_config.get("purchase_interval_seconds") or 12)
                        max_buy = int(payload.get("resign_max_days") or 30)
                        timeout = min(3600, max(180, (interval + 5) * max_buy + 120))
                    elif feature_type == "post.resign":
                        timeout = 300
                    elif feature_type == "flow.open":
                        step_config = step.get("config")
                        if not isinstance(step_config, dict):
                            step_config = {}
                        retry_count = max(
                            0, min(int(step_config.get("retry_count") or 0), 20)
                        )
                        timeout = min(
                            3600, max(30, (retry_count + 1) * (PAGE_TIMEOUT_MS // 1000 + 2))
                        )
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

                try:
                    status, message, data = await asyncio.wait_for(
                        _run_step(
                            page,
                            step,
                            credentials,
                            twofauth,
                            cfbox,
                            cancel,
                            lane.display,
                            lane_key,
                            job_artifacts.log,
                            list(payload.get("resign_pending_dates") or []),
                            {**(payload.get("vision") or {}), "execution_settings":
                             (payload.get("feature_execution_settings") or {}).get(step.get("feature_type"), {})},
                            capture_captcha,
                            credential_task,
                            dict(payload.get("plugin_runtimes") or {}),
                            getattr(manager, "plugin_broker", None),
                            job_artifacts,
                            payload,
                        ),
                        timeout=max(1, int(timeout)),
                    )
                except asyncio.TimeoutError:
                    status, message, data = "fail", f"步骤超时（{timeout}s）", {}
                except asyncio.CancelledError:
                    results[-1] = _step_result(step, "canceled", "已取消", started=started)
                    job_status = "canceled"
                    job_message = "执行已取消"
                    job_artifacts.log("WARN", f"step {step.get('step_key')} canceled")
                    await emit_progress()
                    break
                except Exception as exc:
                    status, message, data = (
                        "fail",
                        f"{type(exc).__name__}: {str(exc)[:500]}",
                        {},
                    )
                feature = str(step.get("feature_type") or "")
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
        if job_status == "success":
            exported = await _export_site_cookies(
                lane.context,
                str(payload.get("base_url") or ""),
                list(payload.get("cookies") or []),
                job_artifacts.log,
            )
            if exported is not None:
                final["cookies"] = exported
        await emit_progress()
        return final
    finally:
        if credential_task is not None:
            if not credential_task.done():
                credential_task.cancel()
            await asyncio.gather(credential_task, return_exceptions=True)
        job_key = str(payload.get("job_id") or "")
        if job_key:
            lane.task_pages.pop(job_key, None)
        await job_artifacts.pages.close()
        try:
            if lane.page is not None and not lane.task_pages:
                await lane.page.bring_to_front()
        except Exception:
            pass
        await manager.release_lane(lane_key, lease_id)
        job_artifacts.log("INFO", f"释放浏览器车道 lane={lane_key} lease={lease_id[:12]}")
