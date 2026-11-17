from __future__ import annotations

import asyncio
from urllib.parse import urlsplit


SETUP_TIMEOUT_SECONDS = 10
PAGE_CLOSE_TIMEOUT_SECONDS = 3
CDP_CLOSE_TIMEOUT_SECONDS = 3
LEASE_RELEASE_TIMEOUT_SECONDS = 5
CLEANUP_RETRY_SECONDS = 5
SHUTDOWN_TIMEOUT_SECONDS = 5
SHUTDOWN_CANCEL_TIMEOUT_SECONDS = 1


async def shutdown_job_cleanup(manager, *, finalize=False):
    """Quiesce before browser teardown; reconcile confirmed pages afterwards."""
    manager._job_cleanup_stopping = True
    owners = list(getattr(manager, "_job_cleanup_owners", ()))
    if finalize:
        for owner in owners:
            task = asyncio.create_task(owner._finish_shutdown(close_new=owner._late_shutdown_needed))
            owner._track(task, manager._job_cleanup_tasks)
        tasks = set(getattr(manager, "_job_cleanup_tasks", ()))
        if tasks:
            await asyncio.wait(tasks, timeout=SHUTDOWN_TIMEOUT_SECONDS)
    # Include shielded close/creation/release tasks, not just the reapers.
    tasks = set(getattr(manager, "_job_cleanup_tasks", ()))
    for owner in list(getattr(manager, "_job_cleanup_owners", ())):
        tasks.update(owner._owned_tasks())
    tasks = {task for task in tasks if not task.done()}
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=SHUTDOWN_CANCEL_TIMEOUT_SECONDS)
    for owner in owners:
        owner._forget_if_settled()
    if finalize:
        manager._job_cleanup_finalized = True
        for owner in list(getattr(manager, "_job_cleanup_owners", ())):
            if owner._late_shutdown_needed and not owner._released and not owner.pending:
                owner._schedule_late_shutdown()


class JobPages:
    """Own only the root page and its popup descendants, never context siblings."""

    def __init__(self, page, lane, job_key):
        self.active = page
        self.lane = lane
        self.job_key = job_key
        self.closed = False
        self.pages = []
        self.tasks = set()
        self.listeners = []
        self.pending = set()
        self.confirmed = set()
        self._native = {}
        self._cdp = {}
        self._closing = {}
        self._close_task = None
        self._cleanup_task = None
        self._first_cleanup = None
        self._release_task = None
        self._released = False
        self._manager = None
        self._cleanup_args = None
        self._unconfirmed_creator = False
        self._lifecycle_children = set()
        self._late_shutdown_needed = False
        self._shutdown_finish_task = None
        if page is not None:
            self.own(page)

    def _track(self, task, collection):
        collection.add(task)
        task._siteflow_lifecycle_register = self._register_lifecycle

        def settled(done):
            collection.discard(done)
            self._settled(done)

        task.add_done_callback(settled)
        return task

    def _owned_tasks(self):
        tasks = {task for task in (
            *self.tasks, *self.pending, *self._native.values(), *self._cdp.values(),
            *self._closing.values(), *self._lifecycle_children, self._close_task,
            self._cleanup_task, self._release_task, self._shutdown_finish_task,
        ) if task is not None}
        remaining = list(tasks)
        while remaining:
            for child in getattr(remaining.pop(), "_siteflow_lifecycle_tasks", ()):
                if child not in tasks:
                    tasks.add(child)
                    remaining.append(child)
        return tasks

    def _register_lifecycle(self, task):
        self._lifecycle_children.add(task)
        task._siteflow_lifecycle_register = self._register_lifecycle
        task.add_done_callback(self._settled)

    def _settled(self, task):
        if not task.cancelled():
            task.exception()
        self._forget_if_settled()

    def _forget_if_settled(self):
        if self._manager is not None and self._released and all(task.done() for task in self._owned_tasks()):
            self._manager._job_cleanup_owners.discard(self)

    async def setup(self, operation, *, creates_page=False):
        async def run():
            result = await operation()
            if creates_page:
                self.own(result)
                self.active = result
                if getattr(self._manager, "_job_cleanup_stopping", False):
                    self._late_shutdown_needed = True
            return result

        task = self._track(asyncio.create_task(run()), self.pending)
        if creates_page:
            def creation_settled(done):
                if done.cancelled():
                    # Shutdown cancellation cannot prove no remote target was created.
                    self._unconfirmed_creator = True
                elif done.exception() is None and getattr(self._manager, "_job_cleanup_finalized", False):
                    self._schedule_late_shutdown()

            task.add_done_callback(creation_settled)
        try:
            # Do not cancel page creation: its returned page must first be owned.
            done, _ = await asyncio.wait({task}, timeout=SETUP_TIMEOUT_SECONDS)
            if not done:
                raise asyncio.TimeoutError
            return task.result()
        finally:
            if not creates_page and not task.done():
                task.cancel()

    def own(self, page):
        if any(owned is page for owned in self.pages):
            return
        self.pages.append(page)

        def popup(child):
            self.own(child)
            if not getattr(self._manager, "_job_cleanup_stopping", False):
                self._track(asyncio.create_task(self.adopt(child, page.url)), self.tasks)

        page.on("popup", popup)
        self.listeners.append((page, popup))

    async def adopt(self, page, opener_url):
        try:
            if self.closed:
                await self._close_page(page)
                return
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
            source = urlsplit(opener_url)
            target = urlsplit(page.url)
            if not self.closed and (source.scheme, source.netloc) == (target.scheme, target.netloc):
                self.active = page
                if self.job_key:
                    self.lane.task_pages[self.job_key] = page
            else:
                await self._close_page(page)
        except Exception:
            await self._close_page(page)

    def _is_closed(self, page):
        try:
            # Mock attributes and other truthy values are not closure evidence.
            return page.is_closed() is True
        except Exception:
            return False

    async def _bounded(self, tasks, page, operation, timeout):
        key = id(page)
        task = tasks.get(key)
        if task is None or task.done():
            task = asyncio.create_task(operation())
            tasks[key] = task
            task.add_done_callback(self._settled)
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            task.cancel()
        elif not task.cancelled():
            try:
                task.result()
            except Exception:
                pass

    async def _close_page(self, page):
        key = id(page)
        task = self._closing.get(key)
        if task is None or task.done():
            task = asyncio.create_task(self._close_owned_page(page))
            self._closing[key] = task
            task.add_done_callback(self._settled)
        return await asyncio.shield(task)

    async def _close_owned_page(self, page):
        key = id(page)
        if key in self.confirmed or self._is_closed(page):
            self.confirmed.add(key)
            return True

        async def native_close():
            await page.close()
            self.confirmed.add(key)

        await self._bounded(self._native, page, native_close, PAGE_CLOSE_TIMEOUT_SECONDS)
        if key in self.confirmed or self._is_closed(page):
            self.confirmed.add(key)
            return True

        async def target_close():
            session = await page.context.new_cdp_session(page)
            try:
                info = await session.send("Target.getTargetInfo")
                target = info.get("targetInfo", {})
                target_id = target.get("targetId")
                if target.get("type") != "page" or not isinstance(target_id, str) or not target_id:
                    return
                result = await session.send("Target.closeTarget", {"targetId": target_id})
                if result.get("success") is True:
                    self.confirmed.add(key)
            finally:
                await session.detach()

        await self._bounded(self._cdp, page, target_close, CDP_CLOSE_TIMEOUT_SECONDS)
        if self._is_closed(page):
            self.confirmed.add(key)
        return key in self.confirmed

    async def _close_round(self):
        self.closed = True
        for task in list(self.tasks):
            task.cancel()
        # Keep listeners through unconfirmed closure and late page creation.
        pages = list(self.pages)
        await asyncio.gather(*(self._close_page(page) for page in pages))
        await asyncio.sleep(0)
        if self.pending or self._unconfirmed_creator or any(id(page) not in self.confirmed for page in self.pages):
            return False
        for page, listener in self.listeners:
            try:
                page.remove_listener("popup", listener)
            except Exception:
                pass
        self.listeners.clear()
        if self.job_key:
            self.lane.task_pages.pop(self.job_key, None)
        return True

    async def close(self):
        if self._close_task is None or self._close_task.done():
            self._close_task = asyncio.create_task(self._close_round())
            self._close_task.add_done_callback(self._settled)
        return await asyncio.shield(self._close_task)

    async def cleanup(self, manager, lane_key, lease_id, log):
        if self._manager is None:
            self._manager = manager
            self._cleanup_args = (lane_key, lease_id, log)
            if not hasattr(manager, "_job_cleanup_owners"):
                manager._job_cleanup_owners = set()
            if not hasattr(manager, "_job_cleanup_tasks"):
                manager._job_cleanup_tasks = set()
            manager._job_cleanup_owners.add(self)
        if self._released:
            return True
        if getattr(manager, "_job_cleanup_stopping", False):
            return False
        if self._cleanup_task is None or self._cleanup_task.done():
            self._first_cleanup = asyncio.get_running_loop().create_future()
            first_cleanup = self._first_cleanup

            async def reap():
                try:
                    while True:
                        released = False
                        try:
                            if await self.close():
                                released = await self._release()
                        except Exception:
                            # Resource diagnostics must not change the business result.
                            pass
                        log("INFO" if released else "WARN",
                            "browser_cleanup_released" if released else "browser_cleanup_quarantined")
                        if not first_cleanup.done():
                            first_cleanup.set_result(released)
                        if released:
                            return
                        await asyncio.sleep(CLEANUP_RETRY_SECONDS)
                finally:
                    if not first_cleanup.done():
                        first_cleanup.set_result(False)

            self._cleanup_task = self._track(asyncio.create_task(reap()), manager._job_cleanup_tasks)
            # Cancellation before the coroutine's first turn skips its finally.
            self._cleanup_task.add_done_callback(
                lambda _: first_cleanup.set_result(False) if not first_cleanup.done() else None)
        return await asyncio.shield(self._first_cleanup)

    async def _release(self):
        if self._released:
            return True
        lane_key, lease_id, _ = self._cleanup_args
        if self._release_task is None or self._release_task.cancelled():
            self._release_task = asyncio.create_task(self._manager.release_lane(lane_key, lease_id))
            self._release_task.add_done_callback(self._settled)
        release = self._release_task
        done, _ = await asyncio.wait({release}, timeout=LEASE_RELEASE_TIMEOUT_SECONDS)
        if done:
            if self._release_task is release:
                self._release_task = None
            release.result()
            self._released = True
        return self._released

    def _schedule_late_shutdown(self):
        if self._shutdown_finish_task is None:
            self._shutdown_finish_task = self._track(
                asyncio.create_task(self._finish_shutdown(close_new=True)),
                self._manager._job_cleanup_tasks)

    async def _finish_shutdown(self, *, close_new=False):
        for page in self.pages:
            if self._is_closed(page):
                self.confirmed.add(id(page))
        if self._unconfirmed_creator or any(not task.done() for task in self.pending):
            return
        if not close_new and any(id(page) not in self.confirmed for page in self.pages):
            return
        # Late creators get one bounded owned-page close, never a new job/reaper.
        if await self.close():
            await self._release()
