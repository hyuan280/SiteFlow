"""Submit date-labelled dialog buttons only after a durable reservation ACK."""
from __future__ import annotations

import asyncio
import copy
import re
import time
from datetime import date
from urllib.parse import urljoin

from worker.browser_input import hold_page_input
from worker.resign_checkpoint import CHECKPOINT_TIMEOUT_SECONDS, ResignCheckpointError
from worker.resign_click import UnsafeCalendar, _iso, _origin


DEFAULTS = {
    "entry_selector": ['.attendance-card header button[title*="补签卡"]'],
    "dialog_selector": ['[role="dialog"][aria-label^="补签"]'],
    "dialog_ready_selector": [],
    "button_selector": ["button"],
    "card_count_selector": ['.attendance-card header button[title*="补签卡"]'],
    "done_selector": ['.attendance-calendar__day.is-done[title^="{date}"]'],
    "success_selector": [],
}


def resign_dialog_timeout(config, pending):
    wait = max(1, min(float(config.get("dialog_wait_seconds", 10)), 60))
    success = max(1, min(float(config.get("success_wait_seconds", 10)), 60))
    return min(7200, 30 + max(1, len(pending)) * (3 * wait + success + 15 + 2 * CHECKPOINT_TIMEOUT_SECONDS))


async def run_resign_dialog(
    page, config, pending, log, *, today=None, unresolved=None, base_url=None,
    progress=None, durable_progress=None,
):
    from worker.executor import _buy_page_ready

    state = {"pending": list(pending), "success": [], "already_signed": [], "failed": [],
             "remaining": list(pending), "cards_before": None, "cards_after": None,
             "submit_attempted": False, "unconfirmed": [], "inferred_signed": []}
    current_date = None
    installed = False
    popup_seen = False
    observation_task = None
    guard_script = None
    context_route_installed = False
    page_route_installed = False
    dialog_dates_seen = False

    async def checkpoint():
        if progress is not None:
            try:
                await progress({"resign": copy.deepcopy(state)})
            except Exception:
                pass

    def result(status, message):
        return status, message, {"resign": copy.deepcopy(state)}

    async def durable(phase):
        try:
            await asyncio.wait_for(durable_progress(copy.deepcopy(state), phase, current_date),
                                   timeout=CHECKPOINT_TIMEOUT_SECONDS)
        except Exception:
            raise ResignCheckpointError() from None

    async def ready():
        if popup_seen or _origin(page.url) != origin:
            raise UnsafeCalendar("弹窗补签离开固定同源页面或打开了新窗口")
        return await _buy_page_ready(page) and _origin(page.url) == origin

    def selectors(key, stamp=None):
        values = config.get(key, DEFAULTS[key])
        if not isinstance(values, list) or not all(isinstance(s, str) and s for s in values):
            raise UnsafeCalendar(f"{key} 必须为选择器数组")
        if stamp is None:
            return values
        replacements = {"date": stamp.isoformat(), "day": str(stamp.day),
                        "month": str(stamp.month), "year": str(stamp.year)}
        return [re.sub(r"\{(date|day|month|year)\}", lambda m: replacements[m[1]], s) for s in values]

    async def unique(root, key, stamp=None):
        for selector in selectors(key, stamp):
            handles = []
            for handle in await root.query_selector_all(selector):
                if handle is not None and await handle.is_visible():
                    handles.append(handle)
                elif handle is not None:
                    await handle.dispose()
            if len(handles) > 1:
                for handle in handles:
                    await handle.dispose()
                raise UnsafeCalendar(f"{key} 存在多个可见匹配")
            if handles:
                return handles[0]
        return None

    async def poll(check, timeout, message):
        deadline = time.monotonic() + timeout
        while True:
            value = await check()
            if value:
                return value
            if time.monotonic() >= deadline:
                raise UnsafeCalendar(message)
            await asyncio.sleep(.05)

    async def inventory():
        if not await ready():
            return None
        handle = await unique(page, "card_count_selector")
        if handle is None:
            return None
        try:
            text = await handle.inner_text()
            matches = list(card_pattern.finditer(text))
            if len(matches) != 1:
                return None
            raw = matches[0].group(1)
            if not raw or not re.fullmatch(r"\d+", raw) or re.search(r"-\s*$", text[:matches[0].start(1)]):
                raise UnsafeCalendar("补签卡库存不是非负整数")
            return int(raw) if await ready() and await handle.evaluate("el => el.isConnected") else None
        finally:
            await handle.dispose()

    async def readable_inventory():
        value = await inventory()
        return (value,) if value is not None else None

    async def marked_done(stamp):
        if not await ready():
            return False
        handle = await unique(page, "done_selector", stamp)
        if handle is None:
            return False
        try:
            return await handle.evaluate("el => el.isConnected") and await ready()
        finally:
            await handle.dispose()

    async def messages():
        output = []
        for selector in selectors("success_selector"):
            for locator in await page.locator(selector).all():
                if await locator.is_visible():
                    text = await locator.inner_text()
                    if success_pattern.search(text):
                        output.append(text)
        return output

    def parsed(text):
        matches = list(date_pattern.finditer(text))
        if not matches:
            return None  # Close/help buttons are allowed in the candidate collection.
        if len(matches) != 1:
            raise UnsafeCalendar("按钮文本包含多个日期")
        groups = matches[0].groupdict()
        full_dates = re.findall(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)", text)
        explicit = {_iso(value).isoformat() for value in full_dates}
        if len(explicit) > 1:
            raise UnsafeCalendar("按钮文本包含矛盾的完整日期")
        if groups.get("date"):
            value = _iso(groups["date"]).isoformat()
            if explicit and explicit != {value}:
                raise UnsafeCalendar("按钮完整日期与正则日期矛盾")
            return value
        try:
            month, day = int(groups["month"]), int(groups["day"])
            if groups.get("year"):
                value = date(int(groups["year"]), month, day).isoformat()
                if explicit and explicit != {value}:
                    raise UnsafeCalendar("按钮完整日期与正则日期矛盾")
                return value
            if explicit:
                value = date.fromisoformat(next(iter(explicit)))
                if value.month != month or value.day != day:
                    raise UnsafeCalendar("按钮完整日期与月日矛盾")
                return value.isoformat()
            date(2000, month, day)  # Validate month/day, including leap day.
            candidates = [stamp for stamp in targets if stamp.month == month and stamp.day == day]
            if len(candidates) > 1:
                raise UnsafeCalendar("按钮缺少年份，待补签日期存在跨年歧义")
            return candidates[0].isoformat() if candidates else f"--{month:02d}-{day:02d}"
        except (ValueError, TypeError, KeyError) as exc:
            raise UnsafeCalendar("按钮日期无法解析") from exc

    async def safe_action(handle, container=None):
        nonlocal guard_script
        if not await ready():
            raise UnsafeCalendar("点击前页面未就绪")
        guard_script = await page.evaluate("""key => {
            if (key && Object.prototype.hasOwnProperty.call(window, key)) return key;
            key = '__siteflowDialogOpen_' + Math.random().toString(36).slice(2);
            window[key] = window.open;
            window.open = () => null;
            return key;
        }""", guard_script)
        info = await handle.evaluate("""el => {
            const form = el.form || el.closest('form');
            return {connected:el.isConnected, disabled:el.disabled,
                urls:[el.getAttribute('href'),el.getAttribute('formaction'),form?.getAttribute('action')]
                    .filter(x=>x!==null && x!==undefined),
                targets:[el.getAttribute('target'),el.getAttribute('formtarget'),form?.getAttribute('target'),
                    document.querySelector('base')?.getAttribute('target')], base:document.baseURI};
        }""")
        if not info["connected"] or info["disabled"] or not await handle.is_visible():
            raise UnsafeCalendar("点击元素失效或禁用")
        if container is not None and not await handle.evaluate(
            "(el, root) => root.isConnected && root.contains(el)", container,
        ):
            raise UnsafeCalendar("日期按钮已离开弹窗")
        if any(t and t.lower() != "_self" for t in info["targets"]):
            raise UnsafeCalendar("禁止新窗口或其他 target")
        if _origin(info["base"]) != origin or any(
            _origin(urljoin(info["base"], value)) != origin for value in info["urls"]
        ):
            raise UnsafeCalendar("点击地址不是固定同源")

    async def dialog_ready():
        if not await ready():
            return False
        handle = await unique(page, "dialog_selector")
        if handle is None:
            return False
        await handle.dispose()
        return True

    async def open_dialog():
        if await dialog_ready():
            return
        async def entry_ready():
            if not await ready():
                return False
            handle = await unique(page, "entry_selector")
            if handle is None:
                return False
            await handle.dispose()
            return True
        await poll(entry_ready, wait, "未找到唯一弹窗入口")
        async with hold_page_input(page):
            handle = await unique(page, "entry_selector")
            if handle is None:
                raise UnsafeCalendar("弹窗入口已改变")
            try:
                await safe_action(handle)
                await handle.click(timeout=int(wait * 1000), no_wait_after=True)
            finally:
                await handle.dispose()
        await poll(dialog_ready, wait, "补签弹窗未出现或未就绪")

    async def target_button(stamp):
        nonlocal dialog_dates_seen
        dialog_dates_seen = False
        if not await dialog_ready():
            raise UnsafeCalendar("补签弹窗未就绪")
        container = await unique(page, "dialog_selector")
        try:
            for selector in selectors("button_selector"):
                found = []
                seen = set()
                for locator in await container.query_selector_all(selector):
                    if not await locator.is_visible():
                        await locator.dispose()
                        continue
                    value = parsed(await locator.inner_text())
                    labels = await locator.evaluate("""el => [el.getAttribute('data-date'),
                        el.getAttribute('title'), el.getAttribute('aria-label')].filter(Boolean).join(' ')""")
                    labelled = {_iso(v).isoformat() for v in re.findall(
                        r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)", labels,
                    )}
                    if value is not None and value.startswith("--") and len(labelled) == 1:
                        full = next(iter(labelled))
                        if value[2:] == full[5:]:
                            value = full
                    if value is not None and labelled and labelled != {value}:
                        await locator.dispose()
                        for item in found:
                            await item.dispose()
                        raise UnsafeCalendar("按钮文本日期与完整日期属性矛盾")
                    if value is not None:
                        dialog_dates_seen = True
                        if value in seen:
                            await locator.dispose()
                            for item in found:
                                await item.dispose()
                            raise UnsafeCalendar("弹窗中同一日期存在多个按钮")
                        seen.add(value)
                    if value == stamp.isoformat():
                        found.append(locator)
                    else:
                        await locator.dispose()
                if found:
                    return found[0]
            return None
        finally:
            await container.dispose()

    async def guard_route(route):
        # Context routing also guards the first request of a newly opened window.
        try:
            owner = route.request.frame.page
        except Exception:
            owner = None
        if owner is not page:
            if owner is None:
                await route.abort()
                return
            if owner is not None and owner.opener is not None and await owner.opener() is page:
                await route.abort()
                return
            await route.fallback()
            return
        try:
            allowed = _origin(route.request.url) == origin
        except (ValueError, UnsafeCalendar):
            allowed = False
        await (route.fallback() if allowed else route.abort())

    async def dismiss(dialog):
        await dialog.dismiss()

    async def close_popup(popup):
        nonlocal popup_seen
        popup_seen = True
        await popup.close()

    try:
        reference = _iso(today)
        targets = [_iso(value) for value in pending]
        if len(set(pending)) != len(pending) or any(stamp >= reference for stamp in targets):
            raise UnsafeCalendar("补签日期必须唯一且严格早于 resign_today")
        unknown = {_iso(value).isoformat() for value in (unresolved or [])}
        if not targets:
            return result("success", "没有待补签日期")
        if durable_progress is None:
            raise ResignCheckpointError()
        origin = _origin(base_url or page.url)
        wait = max(1, min(float(config.get("dialog_wait_seconds", 10)), 60))
        success_wait = max(1, min(float(config.get("success_wait_seconds", 10)), 60))
        date_pattern = re.compile(config.get("date_regex", r"(?<!\d)(?P<month>\d{2})-(?P<day>\d{2})(?!\d)"))
        if not ("date" in date_pattern.groupindex or {"month", "day"} <= date_pattern.groupindex.keys()):
            raise UnsafeCalendar("日期正则必须包含 date 或 month/day 命名组")
        card_pattern = re.compile(config.get("card_count_regex", r"补签\s*[×x]\s*(\d+)"))
        success_pattern = re.compile(config.get("success_regex", "补签成功"))
        if not card_pattern.groups or not (selectors("done_selector") or selectors("success_selector")):
            raise UnsafeCalendar("必须配置卡数捕获组及成功证据")
        page.on("dialog", dismiss)
        page.on("popup", close_popup)
        installed = True
        await page.route("**/*", guard_route)
        page_route_installed = True
        await page.context.route("**/*", guard_route)
        context_route_installed = True
        await poll(ready, wait, "补签页面未就绪")
        await checkpoint()
        # Unknown submissions require positive date-bound evidence, never absence alone.
        for stamp in targets:
            if stamp.isoformat() in unknown:
                if not await marked_done(stamp):
                    raise UnsafeCalendar("历史未决日期未发现目标完成标记，禁止再次提交")
                state["already_signed"].append(stamp.isoformat())
                state["remaining"].remove(stamp.isoformat())
        await checkpoint()
        for stamp in targets:
            current_date = stamp.isoformat()
            if current_date not in state["remaining"]:
                continue
            if await marked_done(stamp):
                state["already_signed"].append(current_date)
                state["remaining"].remove(current_date)
                await checkpoint()
                continue
            await open_dialog()
            # Wait the full readiness budget before treating an absent date as signed.
            deadline = time.monotonic() + wait
            button = None
            while button is None:
                button = await target_button(stamp)
                if button is not None or time.monotonic() >= deadline:
                    break
                await asyncio.sleep(.1)
            if button is None:
                if config.get("missing_button_as_signed", False):
                    container = await unique(page, "dialog_selector")
                    try:
                        marker = await unique(container, "dialog_ready_selector") if container is not None else None
                        if marker is not None:
                            await marker.dispose()
                        if not dialog_dates_seen and marker is None:
                            raise UnsafeCalendar("缺少日期列表加载完成证据，不能把空弹窗视为已签到")
                    finally:
                        if container is not None:
                            await container.dispose()
                    state["already_signed"].append(current_date)
                    state["inferred_signed"].append(current_date)
                    state["remaining"].remove(current_date)
                    await checkpoint()
                    continue
                raise UnsafeCalendar("弹窗中未找到目标日期按钮")
            try:
                before = (await poll(readable_inventory, wait, "补签卡库存无法核验"))[0]
                if state["cards_before"] is None:
                    state["cards_before"] = before
                if state["cards_after"] is not None and before != state["cards_after"]:
                    raise UnsafeCalendar("补签卡库存发生异常变化")
                state["cards_after"] = before
                if before == 0:
                    return result("success", "补签卡为 0，保留剩余日期")
                baseline = []
                evidence = {"message": False, "error": None}

                async def observe():
                    while True:
                        try:
                            if await ready():
                                evidence["message"] |= any(text not in baseline for text in await messages())
                        except UnsafeCalendar as exc:
                            evidence["error"] = exc
                            return
                        except Exception:
                            pass  # A same-origin reload can destroy an in-flight observation.
                        await asyncio.sleep(.025)

                async with hold_page_input(page):
                    async def authorize():
                        fresh = await target_button(stamp)
                        container = await unique(page, "dialog_selector")
                        try:
                            if fresh is None or container is None or not await button.evaluate("(el, other) => el === other", fresh):
                                raise UnsafeCalendar("补签日期按钮 DOM 已改变")
                            if parsed(await button.inner_text()) != current_date or await marked_done(stamp):
                                raise UnsafeCalendar("提交前日期或完成状态已改变")
                            await safe_action(button, container)
                            if await inventory() != before:
                                raise UnsafeCalendar("提交前库存已改变或不可读")
                        finally:
                            if fresh is not None:
                                await fresh.dispose()
                            if container is not None:
                                await container.dispose()
                    await authorize()
                    previous = state["submit_attempted"]
                    state["submit_attempted"] = True
                    state["unconfirmed"].append({"date": current_date, "message": "已授权点击，结果待核验"})
                    try:
                        await durable("prepare")
                    except BaseException:
                        state["submit_attempted"] = previous
                        state["unconfirmed"] = [x for x in state["unconfirmed"] if x["date"] != current_date]
                        raise
                    await checkpoint()
                    await authorize()
                    baseline = await messages()
                    observation_task = asyncio.create_task(observe())
                    try:
                        await button.click(timeout=int(wait * 1000), no_wait_after=True)
                    except Exception as exc:
                        log("WARN", f"补签点击返回异常，仅读取核验，不重试: {type(exc).__name__}")

                async def confirmed():
                    if evidence["error"]:
                        raise evidence["error"]
                    try:
                        if not await ready():
                            return False
                        after = await inventory()
                        if after is not None:
                            state["cards_after"] = after
                            if after not in {before, before - 1}:
                                raise UnsafeCalendar("补签后库存变化异常")
                        proof = evidence["message"] or await marked_done(stamp)
                        return proof and after == before - 1 and await ready() and await inventory() == before - 1
                    except UnsafeCalendar:
                        raise
                    except Exception:
                        return False

                await poll(confirmed, success_wait, "补签提交结果未确认，停止并等待人工核验")
                state["success"].append({"date": current_date, "message": "补签成功证据且补签卡恰好减少 1"})
                state["remaining"].remove(current_date)
                state["unconfirmed"] = [x for x in state["unconfirmed"] if x["date"] != current_date]
                try:
                    await durable("confirm")
                finally:
                    await checkpoint()
            finally:
                if observation_task is not None:
                    observation_task.cancel()
                    await asyncio.gather(observation_task, return_exceptions=True)
                    observation_task = None
                await button.dispose()
        return result("success", "弹窗补签日期已核验")
    except asyncio.CancelledError:
        await checkpoint()
        return result("waiting_human", "补签取消或超时，保留检查点；请人工核验未决日期")
    except Exception as exc:
        message = f"补签停止: {type(exc).__name__}: {str(exc)[:300]}"
        if current_date and not any(x["date"] == current_date for x in state["unconfirmed"] + state["success"]):
            state["failed"].append({"date": current_date, "message": message})
        await checkpoint()
        return result("waiting_human", message)
    finally:
        if installed:
            page.remove_listener("dialog", dismiss)
            page.remove_listener("popup", close_popup)
        if page_route_installed:
            try:
                await page.unroute("**/*", guard_route)
            except Exception:
                pass
        if context_route_installed:
            try:
                await page.context.unroute("**/*", guard_route)
            except Exception:
                pass
        if guard_script:
            try:
                await page.evaluate("""key => {
                    if (Object.prototype.hasOwnProperty.call(window, key)) {
                        window.open = window[key]; delete window[key];
                    }
                }""", guard_script)
            except Exception:
                pass
