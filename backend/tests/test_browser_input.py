import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker.browser_input import _InputGuard, _install_hover_recovery, hold_page_input, humanize_actions


def test_only_reads_are_restored_for_all_target_classes(monkeypatch):
    monkeypatch.setattr("worker.browser_input._install_hover_recovery", lambda: None)
    monkeypatch.setattr("worker.browser_input._install_input_guard", lambda: None)
    reads = ("input_value", "text_content", "inner_text", "inner_html", "get_attribute")
    untouched = ("click", "fill", "press", "scroll_into_view_if_needed", "dispatch_event",
                 "wait_for", "is_visible", "count", "screenshot", "evaluate")
    native = lambda *args: None
    wrapped = lambda *args: None
    classes = {name: type(name, (), {method: native for method in reads + untouched})
               for name in ("Page", "Frame", "Locator", "ElementHandle")}
    calls = []

    def patch(context, config):
        calls.append((context, config))
        for cls in classes.values():
            for name in reads + ("click", "fill", "press", "scroll_into_view_if_needed", "dispatch_event"):
                setattr(cls, name, wrapped)

    monkeypatch.setitem(sys.modules, "cloakbrowser.human", SimpleNamespace(
        patch_context_async=patch, resolve_config=lambda preset: preset))
    monkeypatch.setitem(sys.modules, "cloakbrowser.human.patch", SimpleNamespace(original=lambda cls, name: native))
    monkeypatch.setitem(sys.modules, "playwright", SimpleNamespace(async_api=SimpleNamespace(**classes)))
    for context in (SimpleNamespace(_impl_obj=SimpleNamespace(), new_page=native),
                    SimpleNamespace(_impl_obj=SimpleNamespace(), new_page=native)):
        humanize_actions(context)
        for cls in classes.values():
            assert all(getattr(cls, name) is native for name in reads)
            assert all(getattr(cls, name) is wrapped for name in ("click", "fill", "press", "scroll_into_view_if_needed", "dispatch_event"))
            assert all(getattr(cls, name) is native for name in ("wait_for", "is_visible", "count", "screenshot", "evaluate"))
    assert len(calls) == 2


def test_hover_interception_moves_away_and_requires_a_fresh_hit_test(monkeypatch):
    class Retry(Exception):
        def __init__(self, reason):
            self.reason = reason

    class Human:
        cfg = object()
        worlds = SimpleNamespace()

        async def _hit(self, target, x, y):
            self.checks += 1
            if self.reason:
                raise Retry(self.reason)

        async def move_to(self, x, y, cfg):
            self.moves.append((x, y, cfg))

    async def viewport():
        return {"width": 1920, "height": 947}

    Human.worlds.viewport = viewport
    monkeypatch.setitem(sys.modules, "cloakbrowser.human.engine", SimpleNamespace(Human=Human, _Retry=Retry))
    _install_hover_recovery()
    patched = Human._hit
    _install_hover_recovery()
    assert Human._hit is patched

    async def exercise():
        human = Human()
        human.moves, human.checks = [], 0
        human.reason = "<div id=menu> intercepts pointer events"
        for x, edge in ((650, 1919), (1500, 1)):
            try:
                await human._hit(object(), x, 195)
            except Retry as exc:
                assert exc.reason == human.reason
            else:
                raise AssertionError("Recovery must not treat an obstructed target as clickable")
            assert human.moves[-1] == (edge, 195, human.cfg)
        human.reason = "element is outside of the viewport"
        try:
            await human._hit(object(), 650, 195)
        except Retry:
            pass
        assert len(human.moves) == 2
        human.reason = None
        await human._hit(object(), 650, 195)
        assert human.checks == 4
        assert len(human.moves) == 2

    asyncio.run(exercise())


def test_input_guard_serializes_focus_changes_and_releases_after_cancel():
    async def exercise():
        gate = _InputGuard()
        events = []
        entered = asyncio.Event()

        class Page:
            async def bring_to_front(self):
                events.append("focus")

        async def action():
            async with gate.hold(Page()):
                events.append("action")
                async with gate.hold(Page()):
                    events.append("nested")
                entered.set()
                await asyncio.Event().wait()

        async def new_page():
            async with gate.hold():
                events.append("new_page")

        task = asyncio.create_task(action())
        await entered.wait()
        other = asyncio.create_task(new_page())
        await asyncio.sleep(0)
        assert events == ["focus", "action", "nested"]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.wait_for(other, 1)
        assert events == ["focus", "action", "nested", "new_page"]
        assert gate.owner is None

    asyncio.run(exercise())


@pytest.fixture
def guarded_api(monkeypatch):
    from playwright import async_api

    events = []
    reads = ("input_value", "text_content", "inner_text", "inner_html", "get_attribute")
    originals = {(cls, name): getattr(cls, name)
                 for cls in (async_api.Page, async_api.Frame, async_api.Locator, async_api.ElementHandle)
                 for name in reads}
    # Record every class method before the installer mutates it, for test isolation.
    for kind in ("Page", "Frame", "Locator", "ElementHandle", "Mouse", "Keyboard", "BrowserContext"):
        cls = getattr(async_api, kind)
        for name, method in vars(cls).items():
            if asyncio.iscoroutinefunction(method):
                monkeypatch.setattr(cls, name, method)

    def page(context):
        result = object.__new__(async_api.Page)
        result._impl_obj = SimpleNamespace(context=context._impl_obj, context_api=context)

        async def focus():
            events.append(("focus", result))
            if hook := getattr(result._impl_obj, "focus_hook", None):
                await hook()

        result._impl_obj.bring_to_front = focus
        return result

    async def focus(self):
        await self._impl_obj.bring_to_front()

    async def action(self, *args, **kwargs):
        impl = getattr(self._impl_obj, "page", self._impl_obj)
        assert impl.context._siteflow_input_guard.owner is asyncio.current_task()
        events.append(("action", impl))
        if hook := getattr(impl, "action_hook", None):
            await hook()

    async def close(self):
        events.append(("close", self))
        if hook := getattr(self._impl_obj, "close_hook", None):
            await hook()
        self._impl_obj.closed = True

    async def new_page(self):
        assert self._impl_obj._siteflow_input_guard.owner is asyncio.current_task()
        events.append(("new_page", self))
        if hook := getattr(self._impl_obj, "new_page_hook", None):
            await hook()
        return page(self)

    monkeypatch.setattr(async_api.Page, "context", property(lambda self: self._impl_obj.context_api))
    monkeypatch.setattr(async_api.Page, "bring_to_front", focus)
    monkeypatch.setattr(async_api.Page, "close", close)
    monkeypatch.setattr(async_api.Page, "on", lambda *args: None)
    monkeypatch.setattr(async_api.Page, "remove_listener", lambda *args: None)
    monkeypatch.setattr(async_api.Page, "is_closed", lambda self: getattr(self._impl_obj, "closed", False))
    for kind in ("Page", "Frame", "Locator", "ElementHandle", "Mouse", "Keyboard"):
        cls = getattr(async_api, kind)
        monkeypatch.setattr(cls, "press" if kind == "Keyboard" else "click", action)
    monkeypatch.setattr(async_api.BrowserContext, "new_page", new_page)

    def patch_context(context, config):
        if "new_page" in vars(context):
            return
        captured = context.new_page

        async def sdk_new_page():
            assert context._impl_obj._siteflow_input_guard.owner is asyncio.current_task()
            return await captured()

        context.new_page = sdk_new_page

    monkeypatch.setattr("worker.browser_input._install_hover_recovery", lambda: None)
    monkeypatch.setitem(sys.modules, "cloakbrowser.human", SimpleNamespace(
        patch_context_async=patch_context, resolve_config=lambda preset: preset))
    monkeypatch.setitem(sys.modules, "cloakbrowser.human.patch", SimpleNamespace(
        _impl_page=lambda kind, impl: getattr(impl, "page", impl),
        original=lambda cls, name: originals[cls, name]))

    def context():
        result = object.__new__(async_api.BrowserContext)
        result._impl_obj = SimpleNamespace()
        humanize_actions(result)
        return result

    return SimpleNamespace(api=async_api, context=context, page=page, events=events, close=close)


@pytest.mark.parametrize("kind", ["Page", "Frame", "Locator", "ElementHandle", "Mouse", "Keyboard"])
def test_suspended_close_does_not_block_other_tab_input_or_creation(guarded_api, kind):
    async def exercise():
        context = guarded_api.context()
        closing, active = guarded_api.page(context), guarded_api.page(context)
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_close():
            entered.set()
            await release.wait()

        closing._impl_obj.close_hook = slow_close
        assert guarded_api.api.Page.close is guarded_api.close
        task = asyncio.create_task(closing.close())
        try:
            async with asyncio.timeout(1):
                await entered.wait()
                target = active
                if kind != "Page":
                    target = object.__new__(getattr(guarded_api.api, kind))
                    target._impl_obj = SimpleNamespace(page=active._impl_obj)
                await getattr(target, "press" if kind == "Keyboard" else "click")("target")
                await context.new_page()
                await guarded_api.api.BrowserContext.new_page(context)
            assert not task.done()
            assert context._impl_obj._siteflow_input_guard.owner is None
        finally:
            release.set()
            await task

    asyncio.run(exercise())


def test_lifecycle_waits_for_atomic_input_and_reentrant_sdk_wrappers(guarded_api):
    async def exercise():
        # The first SDK instance captures an unguarded class method; later ones
        # capture the guarded method and must re-enter in the same asyncio task.
        guarded_api.context()
        context = guarded_api.context()
        humanize_actions(context)
        active, other = guarded_api.page(context), guarded_api.page(context)
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_action():
            entered.set()
            await release.wait()

        active._impl_obj.action_hook = slow_action
        action = asyncio.create_task(active.click("target"))
        async with asyncio.timeout(1):
            await entered.wait()
            new_page = asyncio.create_task(context.new_page())
            focus = asyncio.create_task(other.bring_to_front())
            await asyncio.sleep(0)
            assert [event[0] for event in guarded_api.events] == ["focus", "action"]
            assert not new_page.done() and not focus.done()
            release.set()
            await asyncio.gather(action, new_page, focus)
        assert [event[0] for event in guarded_api.events] == ["focus", "action", "new_page", "focus"]
        assert context._impl_obj._siteflow_input_guard.owner is None

    asyncio.run(exercise())


@pytest.mark.parametrize("operation", ["focus", "action_focus", "private_focus"])
@pytest.mark.parametrize("failure", ["timeout", "cancel", "error"])
def test_lifecycle_failure_releases_gate_and_allows_later_input(guarded_api, monkeypatch, operation, failure):
    monkeypatch.setattr("worker.browser_input.LIFECYCLE_TIMEOUT_SECONDS", 0.02 if failure == "timeout" else 1)

    async def exercise():
        guarded_api.context()
        context = guarded_api.context()
        active = guarded_api.page(context)
        gate = context._impl_obj._siteflow_input_guard
        entered = asyncio.Event()
        release = asyncio.Event()

        async def stall():
            assert gate.owner is asyncio.current_task()
            entered.set()
            if failure == "error":
                raise RuntimeError("native failure")
            await release.wait()

        async def private_input():
            async with hold_page_input(active):
                await active.click("target")

        active._impl_obj.focus_hook = stall
        call = {"focus": active.bring_to_front, "action_focus": lambda: active.click("target"),
                "private_focus": private_input}[operation]
        task = asyncio.create_task(call())
        async with asyncio.timeout(1):
            await entered.wait()
            if failure == "cancel":
                task.cancel()
            expected = {"timeout": TimeoutError, "cancel": asyncio.CancelledError, "error": RuntimeError}[failure]
            with pytest.raises(expected) as error:
                await task
        if failure == "timeout":
            assert str(error.value) == "Browser lifecycle pending"
        assert gate.owner is None and not gate.lock.locked()
        if failure != "error":
            assert gate.blocked is not None
            with pytest.raises(TimeoutError, match="^Browser lifecycle pending$"):
                await active.click("target")
            pending = gate.blocked
            release.set()
            await pending
            await asyncio.sleep(0)
        assert gate.blocked is None
        active._impl_obj.focus_hook = None
        context._impl_obj.new_page_hook = None
        async with asyncio.timeout(1):
            # Private authorization stays inside the gate and nested actions
            # must not switch focus between validation and mutation.
            async with hold_page_input(active):
                assert gate.owner is asyncio.current_task()
                start = len(guarded_api.events)
                await active.click("target")
                assert [event[0] for event in guarded_api.events[start:]] == ["action"]
            await context.new_page()
        assert gate.owner is None and not gate.lock.locked()

    asyncio.run(exercise())


@pytest.mark.parametrize("operation", ["instance_new_page", "class_new_page", "focus"])
@pytest.mark.parametrize("failure", ["timeout", "cancel"])
def test_lifecycle_wait_is_bounded_without_interrupting_active_input(guarded_api, monkeypatch, operation, failure):
    monkeypatch.setattr("worker.browser_input.LIFECYCLE_TIMEOUT_SECONDS", 0.02 if failure == "timeout" else 1)

    async def exercise():
        context = guarded_api.context()
        active, other = guarded_api.page(context), guarded_api.page(context)
        entered, release = asyncio.Event(), asyncio.Event()
        gate = context._impl_obj._siteflow_input_guard

        async def slow_action():
            entered.set()
            await release.wait()

        active._impl_obj.action_hook = slow_action
        action = asyncio.create_task(active.click("target"))
        try:
            async with asyncio.timeout(1):
                await entered.wait()
                call = {"instance_new_page": context.new_page,
                        "class_new_page": lambda: guarded_api.api.BrowserContext.new_page(context),
                        "focus": other.bring_to_front}[operation]
                waiter = asyncio.create_task(call())
                await asyncio.sleep(0)
                if failure == "cancel":
                    waiter.cancel()
                with pytest.raises(TimeoutError if failure == "timeout" else asyncio.CancelledError):
                    await waiter
                assert gate.owner is action and gate.lock.locked()
                assert [event[0] for event in guarded_api.events] == ["focus", "action"]
        finally:
            release.set()
            await action
        async with asyncio.timeout(1):
            await context.new_page()
            await other.click("target")
        assert gate.owner is None and not gate.lock.locked()

    asyncio.run(exercise())


@pytest.mark.parametrize("operation", ["focus"])
def test_lifecycle_has_one_budget_for_wait_and_native_call(guarded_api, monkeypatch, operation):
    monkeypatch.setattr("worker.browser_input.LIFECYCLE_TIMEOUT_SECONDS", 0.5)

    async def exercise():
        context = guarded_api.context()
        active = guarded_api.page(context)
        gate = context._impl_obj._siteflow_input_guard
        entered, release = asyncio.Event(), asyncio.Event()
        native_entered = False

        async def hold_input():
            async with gate.hold():
                entered.set()
                await release.wait()

        async def native():
            nonlocal native_entered
            native_entered = True
            await asyncio.sleep(0.3)

        context._impl_obj.new_page_hook = native
        active._impl_obj.focus_hook = native
        holder = asyncio.create_task(hold_input())
        async with asyncio.timeout(2):
            await entered.wait()
            call = {"instance_new_page": context.new_page,
                    "class_new_page": lambda: guarded_api.api.BrowserContext.new_page(context),
                    "focus": active.bring_to_front}[operation]
            waiter = asyncio.create_task(call())
            try:
                await asyncio.sleep(0.3)
                release.set()
                # Each phase is under 0.5s, but their combined time exceeds it.
                with pytest.raises(TimeoutError):
                    await waiter
                assert native_entered
                assert gate.blocked is not None
                await gate.blocked
            finally:
                release.set()
                await holder
        assert gate.owner is None and not gate.lock.locked()

    asyncio.run(exercise())


@pytest.mark.parametrize("entry", ["instance", "class"])
@pytest.mark.parametrize("cancel_creator", [False, True])
def test_job_pages_retains_late_creation_while_guard_fails_closed(
        guarded_api, monkeypatch, entry, cancel_creator):
    from worker.page_lifecycle import JobPages

    monkeypatch.setattr("worker.browser_input.LIFECYCLE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr("worker.page_lifecycle.SETUP_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr("worker.page_lifecycle.CLEANUP_RETRY_SECONDS", 0.001)

    async def exercise():
        guarded_api.context()
        context = guarded_api.context()
        active = guarded_api.page(context)
        gate = context._impl_obj._siteflow_input_guard
        pages = JobPages(None, SimpleNamespace(task_pages={}), "job")
        entered, release = asyncio.Event(), asyncio.Event()
        cancelled = False
        released = []

        async def release_lane(*args):
            released.append(args)

        manager = SimpleNamespace(release_lane=release_lane)

        async def native():
            nonlocal cancelled
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled = True
                # A native implementation can withhold cancellation acknowledgement.
                await release.wait()

        context._impl_obj.new_page_hook = native
        call = context.new_page if entry == "instance" else lambda: guarded_api.api.BrowserContext.new_page(context)
        setup = asyncio.create_task(pages.setup(call, creates_page=True))
        try:
            async with asyncio.timeout(1):
                await entered.wait()
                creator = next(iter(pages.pending))
                if cancel_creator:
                    creator.cancel()
                with pytest.raises(TimeoutError):
                    await setup
                assert not cancelled
                assert creator in pages.pending and not creator.done()
                assert gate.blocked is not None and not gate.lock.locked()
                assert gate.owner is None
                # Neither existing input nor a second setup can steal focus.
                for operation in (lambda: active.click("target"), active.bring_to_front,
                                  context.new_page):
                    with pytest.raises(TimeoutError, match="^Browser lifecycle pending$"):
                        await operation()
                with pytest.raises(TimeoutError, match="^Browser lifecycle pending$"):
                    async with hold_page_input(active):
                        raise AssertionError("private authorization bypassed quarantine")
                assert await pages.close() is False
                assert pages.pending and not pages.pages
                assert await pages.cleanup(manager, "general", "lease", lambda *args: None) is False
                assert not released
                # Reads and unrelated close cleanup do not use the input gate.
                await active.close()
                assert active.is_closed()
                release.set()
                result = await creator
                await asyncio.sleep(0)
                assert not cancelled
                assert result in pages.pages and not pages.pending
                await pages._cleanup_task
                assert result.is_closed()
                assert released == [("general", "lease")]
                assert gate.blocked is None and gate.owner is None
                context._impl_obj.new_page_hook = None
                await context.new_page()
                await guarded_api.page(context).click("target")
        finally:
            release.set()
            await asyncio.gather(setup, *pages.pending, return_exceptions=True)

    asyncio.run(exercise())


@pytest.mark.parametrize("operation", ["focus", "action_focus", "private_focus"])
def test_focus_cancellation_does_not_wait_for_native_acknowledgement(guarded_api, monkeypatch, operation):
    monkeypatch.setattr("worker.browser_input.LIFECYCLE_TIMEOUT_SECONDS", 1)

    async def exercise():
        context = guarded_api.context()
        active = guarded_api.page(context)
        gate = context._impl_obj._siteflow_input_guard
        entered, release = asyncio.Event(), asyncio.Event()
        cancelled = False

        async def native():
            nonlocal cancelled
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled = True
                await release.wait()

        async def private():
            async with hold_page_input(active):
                await active.click("target")

        active._impl_obj.focus_hook = native
        call = {"focus": active.bring_to_front, "action_focus": lambda: active.click("target"),
                "private_focus": private}[operation]
        caller = asyncio.create_task(call())
        try:
            async with asyncio.timeout(1):
                await entered.wait()
                caller.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await caller
                assert not cancelled
                assert gate.blocked is not None and not gate.lock.locked()
                with pytest.raises(TimeoutError, match="^Browser lifecycle pending$"):
                    await active.click("target")
                pending = gate.blocked
                release.set()
                await pending
                await asyncio.sleep(0)
                active._impl_obj.focus_hook = None
                await active.click("target")
                assert gate.blocked is None and gate.owner is None
        finally:
            release.set()
            await asyncio.gather(caller, return_exceptions=True)

    asyncio.run(exercise())


@pytest.mark.parametrize("late", [False, True])
def test_creation_error_clears_quarantine_without_poisoning_later_input(guarded_api, monkeypatch, late):
    monkeypatch.setattr("worker.browser_input.LIFECYCLE_TIMEOUT_SECONDS", 0.01)

    async def exercise():
        context = guarded_api.context()
        gate = context._impl_obj._siteflow_input_guard
        entered, release = asyncio.Event(), asyncio.Event()

        async def native():
            entered.set()
            await release.wait()
            raise RuntimeError("native creation failed")

        context._impl_obj.new_page_hook = native
        creator = asyncio.create_task(context.new_page())
        async with asyncio.timeout(1):
            await entered.wait()
            if late:
                while gate.blocked is None:
                    await asyncio.sleep(0)
            release.set()
            with pytest.raises(RuntimeError, match="^native creation failed$"):
                await creator
            assert gate.blocked is None and gate.owner is None and not gate.lock.locked()
            context._impl_obj.new_page_hook = None
            await context.new_page()
            await guarded_api.page(context).click("target")

    asyncio.run(exercise())
