"""Keep humanized interactions, but use native Playwright for observations."""

import asyncio
from contextlib import asynccontextmanager
from functools import wraps


class _InputGuard:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.owner = None

    @asynccontextmanager
    async def hold(self, page=None):
        task = asyncio.current_task()
        if self.owner is task:
            yield
            return
        async with self.lock:
            self.owner = task
            try:
                if page is not None:
                    await page.bring_to_front()
                yield
            finally:
                self.owner = None


def _install_input_guard():
    from cloakbrowser.human.patch import _impl_page
    from playwright import async_api

    def guard(method, kind, focus):
        if getattr(method, "_siteflow_input_guard", False):
            return method

        @wraps(method)
        async def guarded(self, *args, **kwargs):
            impl = self._impl_obj
            page = None if kind == "BrowserContext" else _impl_page(kind, impl)
            context = impl if page is None else page.context
            gate = getattr(context, "_siteflow_input_guard", None)
            if gate is None:
                return await method(self, *args, **kwargs)
            async with gate.hold(page if focus else None):
                return await method(self, *args, **kwargs)

        guarded._siteflow_input_guard = True
        return guarded

    actions = ("click", "dblclick", "hover", "tap", "fill", "clear", "type",
               "press_sequentially", "press", "check", "uncheck", "set_checked",
               "select_option", "focus", "drag_and_drop", "drag_to", "scroll_into_view_if_needed")
    for kind, names in (
        *((kind, actions) for kind in ("Page", "Frame", "Locator", "ElementHandle")),
        ("Mouse", ("move", "click", "dblclick", "down", "up", "wheel")),
        ("Keyboard", ("type", "press", "down", "up", "insert_text")),
        ("Page", ("bring_to_front", "close")),
        ("BrowserContext", ("new_page",)),
    ):
        cls = getattr(async_api, kind)
        for name in names:
            if hasattr(cls, name):
                setattr(cls, name, guard(getattr(cls, name), kind,
                                        name not in {"bring_to_front", "close", "new_page"}))


def _install_hover_recovery():
    from cloakbrowser.human.engine import Human, _Retry

    hit = Human._hit
    if getattr(hit, "_siteflow_hover_recovery", False):
        return

    @wraps(hit)
    async def hit_with_recovery(self, target, x, y):
        try:
            return await hit(self, target, x, y)
        except _Retry as exc:
            if exc.reason.endswith(" intercepts pointer events"):
                # A curved approach can open a hover menu over the target.
                # Leave it sideways, then let the SDK re-resolve and hit-test.
                viewport = await self.worlds.viewport()
                edge = 1 if x >= viewport["width"] / 2 else viewport["width"] - 1
                await self.move_to(edge, min(max(y, 1), viewport["height"] - 1), self.cfg)
            raise

    hit_with_recovery._siteflow_hover_recovery = True
    Human._hit = hit_with_recovery


def humanize_actions(context):
    from cloakbrowser.human import patch_context_async, resolve_config
    from cloakbrowser.human.patch import original
    from playwright import async_api

    _install_hover_recovery()
    patch_context_async(context, resolve_config("default"))
    # CDP mouse moves can stall on background tabs. Keep a complete public
    # input action foregrounded; new/closed tabs must not steal it mid-action.
    gate = getattr(context._impl_obj, "_siteflow_input_guard", None)
    if gate is None:
        gate = context._impl_obj._siteflow_input_guard = _InputGuard()
        # The SDK installs an instance-level new_page wrapper that captures the
        # previous class method, so protect that entry point explicitly too.
        new_page = context.new_page

        @wraps(new_page)
        async def guarded_new_page(*args, **kwargs):
            async with gate.hold():
                return await new_page(*args, **kwargs)

        context.new_page = guarded_new_page
    _install_input_guard()
    # SDK 0.6.3 installs idempotent, process-wide class wrappers. Restore only
    # reads, including Locator/Frame reads created after page/popup attachment.
    # Never toggle page humanization around awaits: concurrent actions need it.
    for kind in ("Page", "Frame", "Locator", "ElementHandle"):
        cls = getattr(async_api, kind)
        for name in ("input_value", "text_content", "inner_text", "inner_html", "get_attribute"):
            setattr(cls, name, original(cls, name))
