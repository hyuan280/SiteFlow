import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker.browser_input import _InputGuard, _install_hover_recovery, humanize_actions


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
