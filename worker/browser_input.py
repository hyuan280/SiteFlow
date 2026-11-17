"""Keep humanized interactions, but use native Playwright for observations."""

import asyncio
from contextlib import asynccontextmanager
from functools import wraps


LIFECYCLE_TIMEOUT_SECONDS = 10


@asynccontextmanager
async def hold_page_input(page):
    """Let sensitive actions authorize after waiting for the shared input gate."""
    context = getattr(page, "context", None)
    gate = getattr(getattr(context, "_impl_obj", None), "_siteflow_input_guard", None)
    if gate is None:
        yield
        return
    async with gate.hold(page):
        yield


class _InputGuard:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.owner = None
        self.blocked = None
        self._lifecycle_task = None

    @asynccontextmanager
    async def hold(self, page=None, *, timeout=None):
        task = asyncio.current_task()
        if self.blocked is not None and self.blocked is not task:
            raise TimeoutError("Browser lifecycle pending")
        if self.owner is task or self.blocked is task:
            yield
            return
        async with asyncio.timeout(timeout):
            await self.lock.acquire()
        try:
            if self.blocked is not None:
                raise TimeoutError("Browser lifecycle pending")
            self.owner = task
            try:
                if page is not None:
                    await self.lifecycle(page.bring_to_front)
                yield
            finally:
                self.owner = None
        finally:
            self.lock.release()

    async def lifecycle(self, operation, *, creates_page=False):
        """Bound gate occupancy, retaining unknown native work until it settles."""
        caller = asyncio.current_task()
        if caller is self._lifecycle_task:
            # The SDK's instance wrapper can call the guarded class method.
            return await operation()

        deadline = asyncio.get_running_loop().time() + LIFECYCLE_TIMEOUT_SECONDS
        async with self.hold(timeout=LIFECYCLE_TIMEOUT_SECONDS):
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("Browser lifecycle pending")

            async def run():
                return await operation()

            task = self._lifecycle_task = asyncio.create_task(run())
            children = getattr(caller, "_siteflow_lifecycle_tasks", None)
            if children is None:
                children = caller._siteflow_lifecycle_tasks = set()
            children.add(task)
            # Let a job owner retain native work even after its caller times out.
            register = getattr(caller, "_siteflow_lifecycle_register", None)
            if register is not None:
                register(task)
            self.owner = task

            def settled(done):
                children.discard(done)
                if self.blocked is done:
                    self.blocked = None
                if self._lifecycle_task is done:
                    self._lifecycle_task = None
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(settled)
            try:
                remaining = max(0, deadline - asyncio.get_running_loop().time())
                await asyncio.wait({task}, timeout=remaining)
            except asyncio.CancelledError:
                if not task.done():
                    self.blocked = task
                if not creates_page:
                    raise
                # Defer creator cancellation until its result reaches JobPages.
            finally:
                self.owner = caller
            if not task.done():
                # An outstanding native call may still steal focus. Free the
                # lock, but fail closed until it settles instead of cancelling
                # it or admitting conflicting input after a timeout.
                self.blocked = task

        if not creates_page and not task.done():
            raise TimeoutError("Browser lifecycle pending")
        # Creation must return its late page to the tracked JobPages creator,
        # never turn a dispatched creation into an unowned timeout result.
        while True:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    return task.result()
                if not creates_page:
                    raise


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
            if not focus:
                return await gate.lifecycle(lambda: method(self, *args, **kwargs),
                                            creates_page=kind == "BrowserContext")
            async with gate.hold(page):
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
        ("Page", ("bring_to_front",)),
        ("BrowserContext", ("new_page",)),
    ):
        cls = getattr(async_api, kind)
        for name in names:
            if hasattr(cls, name):
                setattr(cls, name, guard(getattr(cls, name), kind,
                                        name not in {"bring_to_front", "new_page"}))


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
    # input action foregrounded; serialize only short creation/focus transitions.
    # Close cleanup must not hold the shared gate while a tab/browser hangs.
    gate = getattr(context._impl_obj, "_siteflow_input_guard", None)
    if gate is None:
        gate = context._impl_obj._siteflow_input_guard = _InputGuard()
        # The SDK installs an instance-level new_page wrapper that captures the
        # previous class method, so protect that entry point explicitly too.
        new_page = context.new_page

        @wraps(new_page)
        async def guarded_new_page(*args, **kwargs):
            return await gate.lifecycle(lambda: new_page(*args, **kwargs), creates_page=True)

        context.new_page = guarded_new_page
    _install_input_guard()
    # SDK 0.6.3 installs idempotent, process-wide class wrappers. Restore only
    # reads, including Locator/Frame reads created after page/popup attachment.
    # Never toggle page humanization around awaits: concurrent actions need it.
    for kind in ("Page", "Frame", "Locator", "ElementHandle"):
        cls = getattr(async_api, kind)
        for name in ("input_value", "text_content", "inner_text", "inner_html", "get_attribute"):
            setattr(cls, name, original(cls, name))
