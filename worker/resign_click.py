"""Calendar-only resign clicks. Ambiguous observations never authorize a submit."""
from __future__ import annotations

import asyncio
import copy
import re
import time
from datetime import date
from urllib.parse import urljoin, urlsplit

from worker.browser_input import hold_page_input
from worker.resign_checkpoint import CHECKPOINT_TIMEOUT_SECONDS, ResignCheckpointError


DEFAULTS = {
    "calendar_title_selector": [".cal-board > .cal-water"],
    "calendar_prev_selector": ['.cal-arr[aria-label="上一月"]'],
    "calendar_next_selector": ['.cal-arr[aria-label="下一月"]'],
    "calendar_day_selector": ['.cal-grid > button.cal-cell:not(.mute):has(.cal-num:text-is("{day}"))'],
    "calendar_selected_selector": ['.cal-grid > button.cal-cell.on:not(.mute):has(.cal-num:text-is("{day}"))'],
    "done_selector": ['.cal-grid > button.cal-cell:not(.mute):has(.cal-num:text-is("{day}")) .cal-badge.att'],
    "button_selector": [".cal-att button.cal-att-pill.act"],
    "card_count_selector": [".cal-help"],
}


class UnsafeCalendar(Exception):
    pass


def _origin(url):
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise UnsafeCalendar("日历地址必须是无凭据的 HTTP(S) 地址")
    return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


def _iso(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise UnsafeCalendar("日期必须为 canonical ISO 日期")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise UnsafeCalendar("无效 ISO 日期") from exc
    if parsed.isoformat() != value:
        raise UnsafeCalendar("日期必须为 canonical ISO 日期")
    return parsed


def resign_click_timeout(config, pending):
    """Bound each target's month navigation, selection and confirmation budget."""
    wait = max(1, min(float(config.get("success_wait_seconds", 10)), 60))
    jumps = max(0, min(int(config.get("max_month_jumps", 12)), 120))
    return min(7200, 30 + max(1, len(pending)) * (
        (jumps + 4) * wait + 15 + 2 * CHECKPOINT_TIMEOUT_SECONDS))


async def run_resign_click(
    page, config, pending, log, *, today=None, unresolved=None, base_url=None, progress=None,
    durable_progress=None,
):
    # Import lazily: executor dispatches here, and its readiness predicate is shared.
    from worker.executor import _buy_page_ready, _month_label

    state = {"pending": list(pending), "success": [], "already_signed": [], "failed": [],
             "remaining": list(pending), "cards_before": None, "cards_after": None,
             "submit_attempted": False, "unconfirmed": []}
    current_date = None
    origin = None
    installed = False
    dialog_installed = False
    popup_seen = False

    async def checkpoint():
        if progress is not None:
            try:
                await progress({"resign": copy.deepcopy(state)})
            except Exception:
                pass  # Streaming is best effort, never an authorization ACK.

    async def durable_checkpoint(phase):
        try:
            await asyncio.wait_for(
                durable_progress(copy.deepcopy(state), phase, current_date),
                timeout=CHECKPOINT_TIMEOUT_SECONDS,
            )
        except Exception:
            raise ResignCheckpointError() from None

    def result(status, message):
        return status, message, {"resign": copy.deepcopy(state)}

    async def same_page():
        if popup_seen:
            raise UnsafeCalendar("检测到新窗口，补签已停止")
        if _origin(page.url) != origin:
            raise UnsafeCalendar("日历已离开固定同源地址")
        return await _buy_page_ready(page) and _origin(page.url) == origin

    def selectors(key, stamp=None):
        values = config.get(key, DEFAULTS[key])
        if key == "card_count_selector" and not values:
            values = ["body"]
        if not isinstance(values, list) or not all(isinstance(item, str) and item for item in values):
            raise UnsafeCalendar(f"{key} 必须是选择器数组")
        if any(re.search(r"[{}]", re.sub(r"\{(?:date|day|month|year)\}", "", item)) for item in values):
            raise UnsafeCalendar("选择器模板仅允许 date/day/month/year")
        replacements = {} if stamp is None else {
            "date": stamp.isoformat(), "day": str(stamp.day),
            "month": str(stamp.month), "year": str(stamp.year),
        }
        return [re.sub(r"\{(date|day|month|year)\}",
                       lambda match: replacements.get(match[1], match[0]), value) for value in values]

    async def unique(root, key, stamp=None, within=None):
        for selector in selectors(key, stamp):
            locator = root.locator(selector)
            visible = []
            for index in range(await locator.count()):
                handle = await locator.nth(index).element_handle(timeout=500)
                contained = handle is not None and (within is None or await handle.evaluate(
                    "(el, parent) => parent.isConnected && parent.contains(el)", within))
                if contained and await handle.is_visible():
                    visible.append(handle)
                elif handle is not None:
                    await handle.dispose()
            if len(visible) > 1:
                for handle in visible:
                    await handle.dispose()
                raise UnsafeCalendar(f"{key} 存在多个可见匹配")
            if visible:
                return visible[0]
        return None

    async def month(stamp=None):
        if not await same_page():
            return None
        handle = await unique(page, "calendar_title_selector", stamp)
        if handle is None:
            return None
        try:
            value = _month_label(await handle.inner_text())
            if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
                raise UnsafeCalendar("无法确定日历月份")
            return value if await same_page() and await handle.evaluate("el => el.isConnected") else None
        finally:
            await handle.dispose()

    async def poll(check, message):
        deadline = time.monotonic() + wait
        while True:
            value = await check()
            if value:
                return value
            if time.monotonic() >= deadline:
                raise UnsafeCalendar(message)
            await asyncio.sleep(.1)

    async def ensure_month(stamp):
        target = stamp.strftime("%Y-%m")
        current = await poll(lambda: month(stamp), "日历月份或页面就绪状态无法核验")
        for _ in range(jumps):
            if current == target:
                return
            previous = current
            key = "calendar_prev_selector" if current > target else "calendar_next_selector"
            async with hold_page_input(page):
                if await month(stamp) != current:
                    raise UnsafeCalendar("翻月前月份已改变")
                button = await unique(page, key, stamp)
                if button is None:
                    raise UnsafeCalendar("未找到唯一翻月按钮")
                try:
                    await safe_action(button)
                    await button.click(timeout=int(wait * 1000), no_wait_after=True)
                finally:
                    await button.dispose()

            async def changed():
                value = await month(stamp)
                return value if value and value != previous else None

            current = await poll(changed, "翻月后标题未改变或页面未就绪")
            # A navigation in the opposite direction is not trustworthy.
            if (current > previous) != (target > previous):
                raise UnsafeCalendar("翻月方向异常")
        if current != target:
            raise UnsafeCalendar("已达到最大翻月次数")

    async def cell(stamp, selected=False):
        observed = await month(stamp)
        if observed is None:
            return None
        if observed != stamp.strftime("%Y-%m"):
            raise UnsafeCalendar("目标日期的月份未确认")
        handle = await unique(page, "calendar_selected_selector" if selected else "calendar_day_selector", stamp)
        if handle is None:
            return None
        try:
            metadata = await handle.evaluate("""el => ({
                connected: el.isConnected, mute: el.classList.contains('mute'),
                data: el.getAttribute('data-date'),
                labels: [el.getAttribute('aria-label') || '',
                    el.querySelector('.cal-num')?.getAttribute('aria-label') || ''],
                number: (el.querySelector('.cal-num')?.textContent ||
                    el.getAttribute('data-day') || el.textContent || '').trim()
            })""")
            full_dates = []
            if metadata["data"] is not None:
                full_dates.append(_iso(metadata["data"]).isoformat())
            for label in metadata["labels"]:
                full_dates.extend(re.findall(r"\d{4}-\d{2}-\d{2}", label))
                full_dates.extend(f"{int(y):04d}-{int(m):02d}-{int(d):02d}" for y, m, d in
                                  re.findall(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日", label))
            if not metadata["connected"] or metadata["mute"]:
                raise UnsafeCalendar("日期单元格无效或属于相邻月")
            if full_dates:
                if any(value != stamp.isoformat() for value in full_dates):
                    raise UnsafeCalendar("日期单元格完整日期与目标矛盾")
            else:
                # Never downgrade contradictory full-date markup to a day-number match.
                dated = page.locator('[data-date]')
                for index in range(await dated.count()):
                    candidate = dated.nth(index)
                    if await candidate.is_visible() and re.fullmatch(
                        r"\d{4}-\d{2}-\d{2}", await candidate.get_attribute("data-date") or "",
                    ):
                        raise UnsafeCalendar("页面提供完整日期，拒绝数字日期回退")
                if metadata["number"] != str(stamp.day):
                    raise UnsafeCalendar("日期数字与目标矛盾")
            observed = await month(stamp)
            if observed is None or not await handle.evaluate("el => el.isConnected"):
                await handle.dispose()
                return None
            if observed != stamp.strftime("%Y-%m"):
                raise UnsafeCalendar("读取日期时月份已改变")
            return handle
        except BaseException:
            await handle.dispose()
            raise

    async def done(stamp):
        handle = await cell(stamp)
        if handle is None:
            return False
        try:
            badge = await unique(page, "done_selector", stamp, within=handle)
            if badge is None:
                return False
            try:
                return (await badge.evaluate("(el, parent) => parent.isConnected && parent.contains(el)", handle)
                        and await month(stamp) == stamp.strftime("%Y-%m"))
            finally:
                await badge.dispose()
        finally:
            await handle.dispose()

    async def count(stamp):
        if not await same_page():
            return None
        handle = await unique(page, "card_count_selector", stamp)
        if handle is None:
            return None
        try:
            text = await handle.inner_text()
            matches = list(re.finditer(config.get("card_count_regex", r"补签卡（当前\s*(\d+)\s*张"), text))
            if len(matches) != 1:
                return None
            raw = matches[0].group(1)
            if not re.fullmatch(r"\d+", raw) or re.search(r"-\s*$", text[:matches[0].start(1)]):
                raise UnsafeCalendar("补签卡库存不是非负整数")
            return int(raw) if await same_page() and await handle.evaluate("el => el.isConnected") else None
        finally:
            await handle.dispose()

    async def safe_action(handle, submit=False):
        if not await same_page():
            raise UnsafeCalendar("点击前页面未就绪")
        info = await handle.evaluate("""el => {
            const form = el.form || el.closest('form');
            return {connected: el.isConnected, disabled: el.disabled,
                text: (el.innerText || el.value || '').trim(),
                urls: [el.getAttribute('href'), el.getAttribute('formaction'),
                    form?.getAttribute('action')].filter(x => x !== null && x !== undefined),
                targets: [el.getAttribute('target'), el.getAttribute('formtarget'),
                    form?.getAttribute('target'), document.querySelector('base')?.getAttribute('target')],
                base: document.baseURI};
        }""")
        if not info["connected"] or info["disabled"] or not await handle.is_visible():
            raise UnsafeCalendar("点击元素已失效")
        if submit and not re.search(r"补签|補簽", info["text"]):
            raise UnsafeCalendar("按钮未明确显示补签，拒绝今日签到或空按钮")
        if any(value and value.lower() != "_self" for value in info["targets"]):
            raise UnsafeCalendar("禁止新窗口或其他 target")
        if _origin(info["base"]) != origin or any(
            _origin(urljoin(info["base"], value)) != origin for value in info["urls"]
        ):
            raise UnsafeCalendar("点击 action/href 不是固定日历同源")
        await page.evaluate("""() => {
            if (!window.__siteflowResignOpen) {
                window.__siteflowResignOpen = window.open;
                window.open = () => null;
            }
        }""")

    async def guard_route(route):
        try:
            allowed = _origin(route.request.url) == origin
        except (ValueError, UnsafeCalendar):
            allowed = False
        if allowed:
            await route.fallback()
        else:
            await route.abort()

    async def dismiss(dialog):
        await dialog.dismiss()

    async def close_popup(popup):
        nonlocal popup_seen
        popup_seen = True
        await popup.close()

    try:
        reference = _iso(today)  # Missing scheduler-local today is deliberately fail closed.
        targets = [_iso(value) for value in pending]
        if len(set(pending)) != len(pending) or any(value >= reference for value in targets):
            raise UnsafeCalendar("补签日期必须唯一且严格早于 resign_today")
        unresolved = {_iso(value).isoformat() for value in (unresolved or [])}
        wait = max(1, min(float(config.get("success_wait_seconds", 10)), 60))
        jumps = max(0, min(int(config.get("max_month_jumps", 12)), 120))
        if not targets:
            return result("success", "没有待补签日期")
        if durable_progress is None:
            raise ResignCheckpointError()
        base = base_url or page.url
        attendance_value = (config.get("attendance_url") or "{base_url}/calendar").replace("{base_url}", base.rstrip("/"))
        attendance = urljoin(base.rstrip("/") + "/", attendance_value)
        origin = _origin(base)
        if _origin(attendance) != origin:
            raise UnsafeCalendar("attendance_url 与站点 base 不同源")
        page.on("dialog", dismiss)
        page.on("popup", close_popup)
        dialog_installed = True
        await page.route("**/*", guard_route)
        installed = True
        await page.goto(attendance, wait_until="domcontentloaded", timeout=30000)
        await poll(same_page, "日历页面未就绪")
        await page.evaluate("""() => {
            window.__siteflowResignOpen = window.open;
            window.open = () => null;
        }""")
        await checkpoint()
        # Resolve *all* past unknown targets read-only before consuming any card.
        resolved = set()
        for stamp in targets:
            if stamp.isoformat() in unresolved:
                await ensure_month(stamp)
                if not await done(stamp):
                    raise UnsafeCalendar("历史未决日期未发现目标 done，禁止再次提交")
                resolved.add(stamp.isoformat())
                state["already_signed"].append(stamp.isoformat())
                state["remaining"].remove(stamp.isoformat())
                await checkpoint()

        for stamp in targets:
            current_date = stamp.isoformat()
            if current_date in resolved:
                continue
            await ensure_month(stamp)
            if await done(stamp):
                state["already_signed"].append(current_date)
                state["remaining"].remove(current_date)
                await checkpoint()
                continue
            async def readable_count():
                value = await count(stamp)
                return (value,) if value is not None else None

            before = (await poll(readable_count, "补签卡库存无法核验"))[0]
            if state["cards_before"] is None:
                state["cards_before"] = before
            if state["cards_after"] is not None and state["cards_after"] != before:
                raise UnsafeCalendar("补签卡库存发生异常变化")
            state["cards_after"] = before
            if before == 0:
                await checkpoint()
                return result("success", "补签卡为 0，保留剩余日期")
            async with hold_page_input(page):
                handle = await cell(stamp)
                if handle is None:
                    raise UnsafeCalendar("未找到唯一目标日期")
                try:
                    await safe_action(handle)
                    await handle.click(timeout=int(wait * 1000), no_wait_after=True)
                finally:
                    await handle.dispose()

            async def selected():
                handle = await cell(stamp, selected=True)
                if handle is None:
                    return False
                await handle.dispose()
                return True

            await poll(selected, "选择日期后无法确认目标 selected")
            button = await unique(page, "button_selector", stamp)
            if button is None:
                raise UnsafeCalendar("未找到唯一补签按钮")
            try:
                async with hold_page_input(page):
                    async def authorize():
                        if not await selected() or await done(stamp):
                            raise UnsafeCalendar("提交前目标日期选择或 done 状态已改变")
                        if await count(stamp) != before:
                            raise UnsafeCalendar("提交前库存已改变或不可读")
                        fresh = await unique(page, "button_selector", stamp)
                        try:
                            if fresh is None or not await button.evaluate("(el, other) => el === other", fresh):
                                raise UnsafeCalendar("补签按钮 DOM 已替换")
                        finally:
                            if fresh is not None:
                                await fresh.dispose()
                        await safe_action(button, submit=True)
                        if await month(stamp) != stamp.strftime("%Y-%m") or not await selected():
                            raise UnsafeCalendar("提交复核期间目标月份或 selected 已改变")

                    await authorize()
                    previously_attempted = state["submit_attempted"]
                    state["submit_attempted"] = True
                    state["unconfirmed"].append({"date": current_date, "message": "已授权点击，结果待核验"})
                    try:
                        await durable_checkpoint("prepare")
                    except BaseException:
                        # No ACK means no click, not proof that the server rolled back.
                        # Do not let the stream independently claim a failed reservation.
                        state["unconfirmed"] = [item for item in state["unconfirmed"] if item["date"] != current_date]
                        state["submit_attempted"] = previously_attempted
                        raise
                    await checkpoint()
                    await authorize()  # The checkpoint itself can yield to a changing SPA.
                    try:
                        await button.click(timeout=int(wait * 1000), no_wait_after=True)
                    except Exception as exc:
                        log("WARN", f"补签点击返回异常，仅读取核验，不重试: {type(exc).__name__}")
            finally:
                await button.dispose()

            async def confirmed():
                if not await same_page():
                    return False
                if await month(stamp) != stamp.strftime("%Y-%m"):
                    return False
                signed = await done(stamp)
                after = await count(stamp)
                if after is not None:
                    state["cards_after"] = after
                    if after not in {before, before - 1}:
                        raise UnsafeCalendar("补签后库存变化异常")
                if (signed and after == before - 1 and await same_page()
                        and await month(stamp) == stamp.strftime("%Y-%m") and await done(stamp)
                        and await count(stamp) == before - 1):
                    state["success"].append({"date": current_date, "message": "目标 done 且补签卡恰好减少 1"})
                    state["remaining"].remove(current_date)
                    state["unconfirmed"] = [item for item in state["unconfirmed"] if item["date"] != current_date]
                    try:
                        await durable_checkpoint("confirm")
                    finally:
                        # A failed confirm ACK must not erase a real, observed success.
                        await checkpoint()
                    return True
                return False

            await poll(confirmed, "补签提交结果未确认，停止并等待人工核验")
        return result("success", "补签日期已核验")
    except asyncio.CancelledError:
        # wait_for and job cancellation must not erase already-confirmed dates.
        await checkpoint()
        return result("waiting_human", "补签被取消或超时，保留检查点；服务端可能仍有未决预留，请人工核验")
    except Exception as exc:
        message = f"补签停止: {type(exc).__name__}: {str(exc)[:300]}"
        if (current_date and not any(item["date"] == current_date for item in state["unconfirmed"])
                and not any(item["date"] == current_date for item in state["success"])):
            state["failed"].append({"date": current_date, "message": message})
        await checkpoint()
        return result("waiting_human", message)
    finally:
        if dialog_installed:
            page.remove_listener("dialog", dismiss)
            page.remove_listener("popup", close_popup)
        if installed:
            try:
                await page.unroute("**/*", guard_route)
                await page.evaluate("""() => {
                    if (window.__siteflowResignOpen) window.open = window.__siteflowResignOpen;
                    delete window.__siteflowResignOpen;
                }""")
            except Exception:
                pass
