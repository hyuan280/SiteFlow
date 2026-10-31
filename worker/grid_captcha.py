"""Conservative nine-cell captcha execution, independent of the backend package."""

import asyncio
import time


_SELECTED = '.geetest_selected, .selected, [aria-checked="true"], [aria-selected="true"]'
_SUCCESS = ["验证成功", "驗證成功", "今天已经签到过啦", "今天已經簽到過啦"]
_REFRESH_WAIT_SECONDS = 5

# Keep the actual nodes in the browser, not locators that may resolve to a new grid.
# The signature deliberately excludes class/selection attributes and colors.
_SNAPSHOT = r"""(root, {cellSelector, selectedSelector}) => {
    const visible = el => el.isConnected && el.getClientRects().length > 0 &&
        getComputedStyle(el).visibility !== 'hidden' &&
        getComputedStyle(el).visibility !== 'collapse';
    const clickable = el => visible(el) && !el.matches(':disabled') &&
        !el.closest('[inert], [aria-disabled="true"]') &&
        getComputedStyle(el).pointerEvents !== 'none';
    const cells = Array.from(root.querySelectorAll(cellSelector)).filter(visible);
    const nodes = Array.from(root.querySelectorAll('*'));
    const signature = () => JSON.stringify(
        [root, ...root.querySelectorAll('*')].map(el => {
            const style = getComputedStyle(el);
            return [el.tagName, el.getAttribute('src'), el.currentSrc || '',
                el.getAttribute('srcset'), style.backgroundImage,
                style.backgroundPosition, style.backgroundSize,
                getComputedStyle(el, '::before').backgroundImage,
                getComputedStyle(el, '::after').backgroundImage,
                el.tagName === 'CANVAS' ? el.toDataURL() : '',
                Array.from(el.childNodes).filter(n => n.nodeType === 3)
                    .map(n => n.textContent).join('')];
        }));
    const original = signature();
    // Only image-bearing content proves regeneration; status text does not.
    const image = JSON.stringify([root, ...nodes].map(el => {
        const style = getComputedStyle(el);
        return [el.tagName, el.getAttribute('src'), el.currentSrc || '',
            el.getAttribute('srcset'), style.backgroundImage,
            style.backgroundPosition, style.backgroundSize,
            getComputedStyle(el, '::before').backgroundImage,
            getComputedStyle(el, '::after').backgroundImage,
            el.tagName === 'CANVAS' ? el.toDataURL() : ''];
    }).filter(parts => parts[1] || parts[2] || parts[3] || parts[9] ||
        [parts[4], parts[7], parts[8]].some(value => value && value !== 'none')));
    const read = () => {
        const current = Array.from(root.querySelectorAll(cellSelector)).filter(visible);
        const descendants = Array.from(root.querySelectorAll('*'));
        return {
            valid: visible(root) && cells.length === 9 && current.length === 9 &&
                nodes.length === descendants.length &&
                nodes.every((el, i) => descendants[i] === el) &&
                cells.every((el, i) => current[i] === el && clickable(el)),
            signature: signature(),
            selected: cells.flatMap((el, i) => el.matches(selectedSelector) ? [i + 1] : [])
        };
    };
    return {root, cells, original, image, read, clickable};
}"""

# Validation and dispatch share one JS task: there is no await between checking
# the challenge/selection and clicking the pinned node.
_ACT = r"""(state, {selected, index, button}) => {
    const now = state.read();
    if (!now.valid || now.signature !== state.original ||
        JSON.stringify(now.selected) !== JSON.stringify(selected)) return false;
    const target = index == null ? button : state.cells[index - 1];
    if (!target) return true;
    if (!state.clickable(target)) return false;
    target.scrollIntoView({block: 'nearest', inline: 'nearest'});
    const rect = target.getBoundingClientRect();
    const top = target.ownerDocument.elementFromPoint(
        rect.left + rect.width / 2, rect.top + rect.height / 2);
    if (!top || (top !== target && !target.contains(top))) return false;
    target.click();
    return true;
}"""

_CHANGED = r"""(old, fresh) =>
    old.root !== fresh.root || old.image !== fresh.image ||
    old.cells.some((cell, i) => cell !== fresh.cells[i])"""


def _selectors(config, key, default=()):
    value = config.get(key, default)
    if isinstance(value, str):
        value = [value]
    return [item for item in (value or []) if isinstance(item, str) and item.strip()]


def _bounded(config, key, default, low, high):
    try:
        return max(low, min(int(config.get(key, default)), high))
    except (TypeError, ValueError, OverflowError):
        return default


async def _find(page, selectors):
    for selector in selectors:
        for locator in await page.locator(selector).all():
            if await locator.is_visible():
                return await locator.element_handle()
    return None


async def _snapshot(page, config):
    root = await _find(page, _selectors(
        config, "image_selector", [".geetest_panel_box", ".geetest_widget"]
    ))
    if root is None:
        return None
    try:
        # Element screenshots already scroll natively. SDK handle-based scrolling
        # identifies nodes by their box and fails on overlapping loading panels.
        for selector in _selectors(config, "cell_selector", [".geetest_item"]):
            state = await root.evaluate_handle(_SNAPSHOT, {
                "cellSelector": selector,
                "selectedSelector": config.get("selected_selector", _SELECTED),
            })
            try:
                if await state.evaluate("state => state.read().valid"):
                    return state
            except BaseException:
                await state.dispose()
                raise
            await state.dispose()
    finally:
        await root.dispose()
    return None


async def _wait_snapshot(page, config, log):
    deadline = time.monotonic() + 5
    while True:
        try:
            state = await _snapshot(page, config)
            if state is not None:
                try:
                    if await state.evaluate("state => state.read().selected.length > 0"):
                        return state
                    if await _act(state, []):
                        # Re-read the pinned nodes after loading/currentSrc settles.
                        # Both reads must see the same signature and empty cells.
                        await asyncio.sleep(0.1)
                        if await _act(state, []):
                            return state
                except BaseException:
                    await state.dispose()
                    raise
                await state.dispose()
        except Exception as exc:
            # Dynamic widgets may replace/detach their initial loading panel.
            # No selection or submission is dispatched while waiting here.
            if time.monotonic() >= deadline:
                log("WARN", f"九宫格读取失败：phase=read_grid，error_type={type(exc).__name__}")
                raise
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.2)


async def _act(state, selected, *, index=None, button=None):
    return await state.evaluate(_ACT, {
        "selected": sorted(selected), "index": index, "button": button,
    })


async def _changed(old, fresh):
    return (await old.evaluate(_CHANGED, fresh)
        and await _act(fresh, []))


async def _wait_changed(page, config, state):
    deadline = time.monotonic() + _REFRESH_WAIT_SECONDS
    while True:
        fresh = None
        changed = False
        try:
            fresh = await _snapshot(page, config)
            if fresh is not None:
                if await _changed(state, fresh):
                    changed = True
                    return fresh
        except Exception:
            pass
        finally:
            if fresh is not None and not changed:
                await fresh.dispose()
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.1)


async def _refresh(page, config, state):
    button = await _find(page, _selectors(config, "refresh_selector", [".geetest_refresh"]))
    if button is None:
        return None
    try:
        if not await _act(state, [], button=button):
            return None
    finally:
        await button.dispose()
    return await _wait_changed(page, config, state)


async def _wait_success(page, config, old=None):
    signals = _selectors(config, "success_signals", _SUCCESS)
    appears = _selectors(config, "success_selector")
    missing = _selectors(config, "success_when_missing")
    deadline = time.monotonic() + _bounded(config, "success_wait_seconds", 10, 1, 120)
    while True:
        if signals:
            try:
                text = await page.locator("body").inner_text(timeout=1000)
                if any(signal in text for signal in signals):
                    return "text"
            except Exception:
                pass
        for selector in appears:
            try:
                if any([await item.is_visible() for item in await page.locator(selector).all()]):
                    return "selector"
            except Exception:
                pass
        # Explicit disappearance is opt-in; ALL configured selectors must
        # be absent/hidden. An invalid selector or failed read is not absence.
        if missing:
            try:
                visible = [await item.is_visible() for selector in missing
                           for item in await page.locator(selector).all()]
                if not any(visible):
                    return "missing"
            except Exception:
                pass
        if old is not None:
            fresh = None
            changed = False
            try:
                fresh = await _snapshot(page, config)
                if fresh is not None and await _changed(old, fresh):
                    changed = True
                    return fresh
            except Exception:
                # A transitional/detached grid is neither success nor retry proof.
                pass
            finally:
                if fresh is not None and not changed:
                    await fresh.dispose()
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.2)


async def run_grid_captcha(page, config, log, vision, capture=None):
    """Share max_refresh (default 3, bounded 0..10) across all retry causes.

    attempts counts model calls; verification_attempts counts all rounds,
    including stale captures that are discarded before calling the model.
    """
    max_refresh = _bounded(config, "max_refresh", 3, 0, 10)
    counts = {"attempts": 0, "verification_attempts": 0, "retries": 0}
    state = None
    try:
        for retry in range(max_refresh + 1):
            counts["verification_attempts"] = retry + 1
            counts["retries"] = retry
            current, state = state, None
            status, message, data = await _run_grid_round(
                page, config, log, vision, capture, current, counts, max_refresh,
            )
            if status != "retry":
                return status, message, {**data, **counts}
            state = data["state"]
            log("WARN", f"九宫格题目已重新生成：verification_attempts={retry + 1}，"
                f"retries={retry}/{max_refresh}，attempts={counts['attempts']}")
            if retry == max_refresh:
                return "waiting_human", "九宫格验证重试次数已用尽，需要人工处理", {
                    "reason": "retry_exhausted", **counts,
                }
    finally:
        if state is not None:
            await state.dispose()


async def _run_grid_round(page, config, log, vision, capture, state, counts, max_refresh):
    """One recognition round, retrying only confirmed regenerated, empty grids.

    Success is positive text OR a visible success selector OR, when explicitly
    configured, all success_when_missing selectors being absent/hidden.
    """
    if not vision or not all(vision.get(key) for key in ("base_url", "model", "api_key")):
        return "waiting_human", "九宫格验证需要配置视觉模型", {
            "reason": "vision_unavailable",
        }
    if vision.get("supports_vision") is not True:
        return "waiting_human", "九宫格验证需要开启模型视觉支持", {
            "reason": "vision_not_supported",
        }

    from worker.vision import GridCaptchaUnclear, answer_grid_captcha

    custom_prompt = str(
        (vision.get("execution_settings") or {}).get("llm_prompt") or ""
    ).strip() or None
    attempts = counts["attempts"]
    phase = "read_grid"
    try:
        if state is None:
            state = await _wait_snapshot(page, config, log)
        if state is None:
            return "waiting_human", "未找到包含九个可点击单元的九宫格验证区域", {
                "reason": "invalid_grid",
            }
        phase = "check_challenge"
        if not await _act(state, []):
            preselected = await state.evaluate("state => state.read().selected.length > 0")
            fresh = None if preselected else await _wait_changed(page, config, state)
            if fresh is not None:
                return "retry", "", {"state": fresh}
            return "waiting_human", "九宫格题目已变化或存在预选单元，需要人工处理", {
                "reason": "unsafe_state", "attempts": attempts,
            }
        root = await state.evaluate_handle("state => state.root")
        try:
            phase = "capture"
            image = await root.as_element().screenshot(type="png", timeout=5000)
        except Exception:
            fresh = await _wait_changed(page, config, state)
            if fresh is not None:
                return "retry", "", {"state": fresh}
            raise
        finally:
            await root.dispose()
        if not image or not await _act(state, []):
            fresh = await _wait_changed(page, config, state) if image else None
            if fresh is not None:
                return "retry", "", {"state": fresh}
            return "waiting_human", "九宫格截图期间题目变化，需要人工处理", {
                "reason": "stale_challenge", "attempts": attempts,
            }
        if capture is not None:
            try:
                await capture(image, "Grid captcha")
            except Exception:
                log("WARN", "九宫格验证码截图保存失败")
        attempts += 1
        counts["attempts"] = attempts
        log("INFO", f"九宫格视觉识别，第 {counts['verification_attempts']}/{max_refresh + 1} 次")
        try:
            answer = await answer_grid_captcha(image, vision, custom_prompt)
        except GridCaptchaUnclear:
            if counts["retries"] >= max_refresh:
                return "waiting_human", "九宫格多次识别不清，需要人工处理", {
                    "reason": "vision_unclear", "attempts": attempts,
                }
            fresh = await _refresh(page, config, state)
            if fresh is None:
                return "waiting_human", "九宫格无法安全刷新或题目未变化，需要人工处理", {
                    "reason": "refresh_unavailable", "attempts": attempts,
                }
            return "retry", "", {"state": fresh}
        except Exception as exc:
            reason = getattr(exc, "reason", "invalid_response")
            if reason not in {"http_error", "timeout", "network_error", "invalid_response"}:
                reason = "invalid_response"
            status = getattr(exc, "status_code", None)
            diagnostics = {"provider_error": reason}
            if reason == "http_error" and type(status) is int and 100 <= status <= 599:
                diagnostics["http_status"] = status
            log("WARN", f"九宫格模型请求失败：{reason}" +
                (f"，HTTP {diagnostics['http_status']}" if "http_status" in diagnostics else ""))
            return "waiting_human", "九宫格视觉模型调用失败，需要人工处理", {
                "reason": "vision_failed", "attempts": attempts, **diagnostics,
            }
        if (not isinstance(answer, list) or not answer or
                any(type(index) is not int or not 1 <= index <= 9 for index in answer) or
                len(set(answer)) != len(answer)):
            return "waiting_human", "九宫格模型返回无效编号，需要人工处理", {
                "reason": "invalid_answer", "attempts": attempts,
            }
        selected = []
        phase = "select_cells"
        for index in sorted(answer):
            if not await _act(state, selected, index=index):
                fresh = await _wait_changed(page, config, state)
                if fresh is not None:
                    return "retry", "", {"state": fresh}
                return "waiting_human", "九宫格题目或选中状态变化，已停止选择", {
                    "reason": "stale_challenge", "attempts": attempts,
                }
            selected.append(index)
        submit = await _find(page, _selectors(config, "submit_selector", [".geetest_commit"]))
        if submit is None:
            return "waiting_human", "未找到九宫格提交按钮，需要人工处理", {
                "reason": "submit_unavailable", "attempts": attempts,
            }
        try:
            phase = "submit"
            if not await _act(state, selected, button=submit):
                fresh = await _wait_changed(page, config, state)
                if fresh is not None:
                    return "retry", "", {"state": fresh}
                return "waiting_human", "九宫格选中状态无法核对，未提交", {
                    "reason": "unsafe_selection", "attempts": attempts,
                }
        finally:
            await submit.dispose()
        phase = "wait_success"
        success_by = await _wait_success(page, config, state)
        if success_by is not None and not isinstance(success_by, str):
            return "retry", "", {"state": success_by}
        data = {"selected_indices": sorted(answer), "attempts": attempts}
        if success_by:
            return "success", "九宫格验证提交成功", {
                **data, "success_by": success_by,
            }
        return "unknown", "九宫格已提交但未检测到明确成功信号", {
            **data, "reason": "success_timeout",
        }
    except Exception as exc:
        log("WARN", f"九宫格执行异常：phase={phase}，error_type={type(exc).__name__}")
        return "unknown", f"九宫格验证未能安全完成（阶段：{phase}）", {
            "reason": "execution_failed", "attempts": attempts,
            "phase": phase, "error_type": type(exc).__name__,
        }
    finally:
        if state is not None:
            await state.dispose()
