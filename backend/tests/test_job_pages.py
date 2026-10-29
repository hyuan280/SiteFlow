import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from worker.page_lifecycle import JobPages
from worker.plugin_broker import PluginBroker, BrokerNotFound


class Page:
    def __init__(self, url="http://fixture/checkin"):
        self.url = url
        self.listeners = {}
        self.close = AsyncMock()
        self.wait_for_load_state = AsyncMock()

    def on(self, event, callback):
        self.listeners[event] = callback

    def remove_listener(self, event, callback):
        del self.listeners[event]


def test_owned_popup_handoff_and_cleanup():
    async def run():
        root, popup, unrelated, home = (Page() for _ in range(4))
        lane = SimpleNamespace(task_pages={}, page=home)
        pages = JobPages(root, lane, "job")
        root.listeners["popup"](popup)
        await asyncio.gather(*pages.tasks)
        assert pages.active is popup
        assert lane.task_pages["job"] is popup
        await pages.close()
        root.close.assert_awaited()
        popup.close.assert_awaited()
        unrelated.close.assert_not_awaited()
        home.close.assert_not_awaited()
        assert not root.listeners and not popup.listeners and not lane.task_pages
    asyncio.run(run())


def test_cross_origin_and_late_popup_close():
    async def run():
        root = Page()
        pages = JobPages(root, SimpleNamespace(task_pages={}), "job")
        cross = Page("http://other/")
        root.listeners["popup"](cross)
        await asyncio.gather(*pages.tasks)
        cross.close.assert_awaited()
        assert pages.active is root
        pages.closed = True
        late = Page()
        root.listeners["popup"](late)
        await asyncio.gather(*pages.tasks)
        late.close.assert_awaited()
        await pages.close()
    asyncio.run(run())


def test_release_cancels_and_settles_inflight_action():
    async def run():
        broker = PluginBroker("http://fixture")
        token = broker.register(Page(), site_origin="http://fixture", approved_capabilities=["browser_actions"], authorize=AsyncMock())
        entered = asyncio.Event()
        settled = asyncio.Event()
        async def action(_payload):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                settled.set()
        broker._execute = action
        task = asyncio.create_task(broker.execute({"token": token, "op": "click"}))
        await entered.wait()
        await broker.release_and_wait(token)
        await asyncio.gather(task, return_exceptions=True)
        assert settled.is_set()
        assert not broker._sessions
        try:
            broker._session(token)
        except BrokerNotFound:
            pass
        else:
            raise AssertionError("released token accepted")
    asyncio.run(run())
