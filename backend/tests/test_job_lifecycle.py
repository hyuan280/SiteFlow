import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from worker import executor, page_lifecycle as lifecycle
from worker.app import LaneManager
from tests.test_job_pages import Page
from tests.test_browser_input import guarded_api  # noqa: F401


@pytest.fixture(autouse=True)
def budgets(monkeypatch, tmp_path):
    monkeypatch.setattr(executor, "ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(lifecycle, "SETUP_TIMEOUT_SECONDS", .05)
    monkeypatch.setattr(lifecycle, "PAGE_CLOSE_TIMEOUT_SECONDS", .1)
    monkeypatch.setattr(lifecycle, "CDP_CLOSE_TIMEOUT_SECONDS", .05)
    monkeypatch.setattr(lifecycle, "LEASE_RELEASE_TIMEOUT_SECONDS", .05)
    monkeypatch.setattr(lifecycle, "CLEANUP_RETRY_SECONDS", .01)


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.001)


async def forever(*_args):
    await asyncio.Event().wait()


def resources(*pages):
    manager = LaneManager()
    lane = manager.get("general")
    lane.status = "running"
    lane.context = SimpleNamespace(new_page=AsyncMock(side_effect=list(pages)), pages=[])
    lane.page = SimpleNamespace(bring_to_front=AsyncMock())
    for page in pages:
        page.context = lane.context
        page.bring_to_front = AsyncMock()
    return manager, lane


def cdp_context(page, *, success=True):
    async def send(method, params=None):
        if method == "Target.getTargetInfo":
            assert params is None
            return {"targetInfo": {"targetId": "owned-target", "type": "page"}}
        assert method == "Target.closeTarget"
        assert params == {"targetId": "owned-target"}
        return {"success": success}

    session = SimpleNamespace(send=AsyncMock(side_effect=send), detach=AsyncMock())
    page.context.new_cdp_session = AsyncMock(return_value=session)
    return session


def payload(job):
    return {"job_id": job, "steps": [{"step_key": "claim", "feature_type": "sign.button"}]}


def test_hung_close_does_not_block_next_job_and_preserves_success(monkeypatch):
    async def run():
        a, b, unrelated = Page(), Page(), Page()
        manager, lane = resources(a, b)
        close_started = asyncio.Event()

        async def close():
            close_started.set()
            await forever()

        a.close.side_effect = close
        session = cdp_context(a)
        started_b = asyncio.Event()

        async def step(page, *_args, **_kwargs):
            if page is b:
                started_b.set()
            return "success", "Claim completed", {}

        monkeypatch.setattr(executor, "_run_step", step)
        task_a = asyncio.create_task(executor.execute_job(manager, payload("A")))
        await close_started.wait()
        task_b = asyncio.create_task(executor.execute_job(manager, payload("B")))
        await asyncio.wait_for(started_b.wait(), .08)
        assert not task_a.done()
        result_a, result_b = await asyncio.gather(task_a, task_b)
        assert result_a["status"] == result_b["status"] == "success"
        assert result_a["message"] == "Claim completed"
        assert not lane.leases and lane.in_use == 0
        session.send.assert_any_await("Target.closeTarget", {"targetId": "owned-target"})
        lane.context.new_cdp_session.assert_awaited_once_with(a)
        lane.page.bring_to_front.assert_not_awaited()
        unrelated.close.assert_not_awaited()
        await until(lambda: not manager._job_cleanup_tasks)

    asyncio.run(run())


def test_unconfirmed_close_quarantines_then_releases_without_replay(monkeypatch):
    async def run():
        a, b = Page(), Page()
        manager, lane = resources(a, b)
        a.close.side_effect = RuntimeError("private browser URL")
        a.is_closed = MagicMock()  # A truthy mock is not confirmation.
        session = cdp_context(a, success=False)
        step = AsyncMock(return_value=("success", "Claim completed", {}))
        monkeypatch.setattr(executor, "_run_step", step)
        result = await executor.execute_job(manager, payload("A"))
        assert result["status"] == "success"
        assert lane.in_use == 1 and len(lane.leases) == 1
        assert a.listeners and manager._job_cleanup_tasks
        result_b = await executor.execute_job(manager, payload("B"))
        assert result_b["status"] == "success" and lane.in_use == 1
        assert step.await_count == 2
        a.is_closed.return_value = True
        await until(lambda: not manager._job_cleanup_tasks)
        assert not lane.leases and not a.listeners and not lane.task_pages
        assert step.await_count == 2
        assert session.send.await_count >= 2

    asyncio.run(run())


@pytest.mark.parametrize("interrupt", ["cancel", "timeout"])
def test_late_page_creation_retains_ownership_and_lease(interrupt):
    async def run():
        page = Page()
        manager, lane = resources(page)
        entered, complete = asyncio.Event(), asyncio.Event()

        async def create():
            entered.set()
            await complete.wait()
            return page

        lane.context.new_page.side_effect = create
        task = asyncio.create_task(executor._execute_admitted_job(manager, {"job_id": "late"}))
        await entered.wait()
        if interrupt == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        else:
            result = await asyncio.wait_for(task, 1)
            assert result["reason"] == "browser_setup_timeout"
            assert result["stage"] == "new_page"
        assert lane.in_use == 1 and manager._job_cleanup_tasks
        page.close.assert_not_awaited()
        complete.set()
        await until(lambda: not manager._job_cleanup_tasks)
        page.close.assert_awaited_once()
        assert not lane.leases and not page.listeners

    asyncio.run(run())


def test_duplicate_cancel_cleanup_shares_close_and_release():
    async def run():
        page = Page()
        manager, lane = resources(page)
        lease, _ = await manager.lease_lane("general")
        release = AsyncMock(wraps=manager.release_lane)
        manager.release_lane = release
        owner = lifecycle.JobPages(page, lane, "job")
        entered, complete = asyncio.Event(), asyncio.Event()

        async def close():
            entered.set()
            await complete.wait()

        page.close.side_effect = close
        tasks = [asyncio.create_task(owner.cleanup(manager, "general", lease, lambda *_: None))
                 for _ in range(2)]
        await entered.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        complete.set()
        assert await owner.cleanup(manager, "general", lease, lambda *_: None)
        await until(lambda: not manager._job_cleanup_tasks)
        page.close.assert_awaited_once()
        release.assert_awaited_once()
        assert not owner.tasks and not owner.pending
        assert all(task.done() for task in owner._closing.values())
        assert not lane.leases

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["cookies", "new_page", "focus"])
def test_setup_timeout_is_static_and_progress_has_artifacts(stage, tmp_path):
    async def run():
        page = Page()
        manager, lane = resources(page)
        progress = AsyncMock()
        data = {"job_id": stage, "cookies": [{"value": "private-cookie"}]}
        lane.context.add_cookies = AsyncMock()
        if stage == "cookies":
            lane.context.add_cookies.side_effect = forever
        elif stage == "new_page":
            # Complete later to avoid leaving a deliberately live creator.
            async def create():
                await asyncio.sleep(.08)
                return page
            lane.context.new_page.side_effect = create
        else:
            page.bring_to_front.side_effect = forever
        result = await executor.execute_job(manager, data, on_step=progress)
        assert result["reason"] == result["message"] == "browser_setup_timeout"
        assert result["stage"] == stage
        stages = [call.args[0] for call in progress.await_args_list if "stage" in call.args[0]]
        assert stages[-1]["message"] == f"browser_setup_{stage}"
        assert stages[-1]["artifacts"] and result["artifacts"]
        assert "private-cookie" not in str(result) + str(stages)
        await until(lambda: not manager._job_cleanup_tasks)
        assert not lane.leases

    asyncio.run(run())
    assert "private-cookie" not in next(tmp_path.rglob("job.log")).read_text()


@pytest.mark.parametrize("info", [
    {}, {"targetInfo": {"targetId": "", "type": "page"}},
    {"targetInfo": {"targetId": "other", "type": "browser"}},
])
def test_cdp_requires_page_target_identity(info):
    async def run():
        page = Page()
        page.close.side_effect = RuntimeError("private")
        manager, lane = resources(page)
        session = cdp_context(page)
        session.send.side_effect = None
        session.send.return_value = info
        owner = lifecycle.JobPages(page, lane, "job")
        assert await owner.close() is False
        session.send.assert_awaited_once_with("Target.getTargetInfo")
        page.close.side_effect = None
        assert await owner.close() is True

    asyncio.run(run())


def test_release_timeout_keeps_same_release_task():
    async def run():
        page = Page()
        manager, lane = resources(page)
        lease, _ = await manager.lease_lane("general")
        original = manager.release_lane
        complete = asyncio.Event()

        async def release(*args):
            await complete.wait()
            return await original(*args)

        manager.release_lane = AsyncMock(side_effect=release)
        owner = lifecycle.JobPages(page, lane, "job")
        assert await owner.cleanup(manager, "general", lease, lambda *_: None) is False
        assert lane.in_use == 1 and manager._job_cleanup_tasks
        await asyncio.sleep(.08)
        manager.release_lane.assert_awaited_once()
        complete.set()
        await until(lambda: not manager._job_cleanup_tasks)
        manager.release_lane.assert_awaited_once()
        assert not lane.leases

    asyncio.run(run())


def test_late_popup_and_stalled_adoption_stay_owned():
    async def run():
        root, popup, late = Page(), Page(), Page()
        manager, lane = resources(root)
        lease, _ = await manager.lease_lane("general")
        owner = lifecycle.JobPages(root, lane, "job")
        popup.wait_for_load_state.side_effect = forever
        root.listeners["popup"](popup)
        await asyncio.sleep(0)

        async def close_root():
            root.listeners["popup"](late)

        root.close.side_effect = close_root
        await owner.cleanup(manager, "general", lease, lambda *_: None)
        await until(lambda: not manager._job_cleanup_tasks)
        for page in (root, popup, late):
            page.close.assert_awaited_once()
            assert not page.listeners
        assert not owner.tasks and not lane.leases

    asyncio.run(run())


def test_stalled_native_and_cdp_close_are_bounded_and_retry():
    async def run():
        page = Page()
        manager, lane = resources(page)
        lease, _ = await manager.lease_lane("general")
        page.close.side_effect = forever
        session = cdp_context(page)
        session.send.side_effect = forever
        owner = lifecycle.JobPages(page, lane, "job")
        assert await asyncio.wait_for(owner.cleanup(manager, "general", lease, lambda *_: None), .3) is False
        assert lane.in_use == 1 and page.listeners
        page.close.side_effect = None
        await until(lambda: not manager._job_cleanup_tasks)
        assert not lane.leases and not page.listeners
        assert all(task.done() for task in (*owner._native.values(), *owner._cdp.values()))

    asyncio.run(run())


def test_already_closed_page_is_confirmed_without_commands():
    async def run():
        page = Page()
        page.is_closed = lambda: True
        manager, lane = resources(page)
        owner = lifecycle.JobPages(page, lane, "job")
        assert await owner.close() is True
        assert await owner.close() is True
        page.close.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("interrupt", ["none", "event", "task", "disconnect"])
@pytest.mark.parametrize("consumption", [False, True])
def test_completed_result_survives_cleanup_interruption(monkeypatch, interrupt, consumption):
    monkeypatch.setattr(lifecycle, "PAGE_CLOSE_TIMEOUT_SECONDS", .4)

    async def run():
        page = Page()
        manager, lane = resources(page)
        lane.session = SimpleNamespace(disconnected=False, desktop=None, display=None)
        cancel = asyncio.Event()
        closing = asyncio.Event()
        progress = AsyncMock()

        async def close():
            closing.set()
            await forever()

        page.close.side_effect = close
        cdp_context(page, success=False)
        step = AsyncMock(side_effect=[
            ("success", "Claim", {"claimed": True}),
            ("success", "Claim completed", {"value": True, "message": "Claim completed"}),
        ])
        monkeypatch.setattr(executor, "_run_step", step)
        monkeypatch.setattr(executor, "_export_site_cookies", AsyncMock(return_value=[{"name": "saved"}]))
        data = payload("cancel")
        data["steps"].append({"step_key": "result", "feature_type": "flow.result"})
        if consumption:
            # This activates the wrapper's consumption interruption policy.
            data["assistant_consumption_plan"] = {}
        task = asyncio.create_task(executor.execute_job(manager, data, cancel, progress))
        await asyncio.wait_for(closing.wait(), 1)
        assert not any(call.args[0]["message"] == "Claim completed" for call in progress.await_args_list)
        if interrupt == "event":
            cancel.set()
        elif interrupt == "task":
            task.cancel()
        elif interrupt == "disconnect":
            lane.session.disconnected = True
        result = await asyncio.wait_for(task, .8)
        assert result["status"] == "success"
        assert result["message"] == "Claim completed"
        assert result["steps"][0]["data"]["claimed"] is True
        assert result["outputs"][-1] == {"step_key": "result", "value": True, "message": "Claim completed"}
        assert result["cookies"] == [{"name": "saved"}]
        assert result["artifacts"]
        assert "_business_complete" not in str(result) + str(progress.await_args_list)
        assert progress.await_args.args[0]["status"] == "success"
        assert step.await_count == 2
        assert lane.execution_semaphore.active == 0
        assert lane.in_use == 1 and manager._job_cleanup_tasks
        page.is_closed = lambda: True
        await until(lambda: not manager._job_cleanup_tasks)
        assert not lane.leases

    asyncio.run(run())


def test_cdp_success_survives_hung_detach_and_duplicate_close_cancel():
    async def run():
        page = Page()
        manager, lane = resources(page)
        page.close.side_effect = RuntimeError("private")
        session = cdp_context(page)
        session.detach.side_effect = forever
        owner = lifecycle.JobPages(page, lane, "job")
        closing = asyncio.create_task(owner.close())
        await until(lambda: session.detach.await_count == 1)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert await asyncio.wait_for(owner.close(), .2) is True
        assert await owner.close() is True
        page.close.assert_awaited_once()
        assert session.send.await_count == 2
        assert not page.listeners

    asyncio.run(run())


@pytest.mark.parametrize("started", [False, True])
def test_canceled_reaper_reports_false_and_cleanup_restarts(started):
    async def run():
        page = Page()
        manager, lane = resources(page)
        lease, _ = await manager.lease_lane("general")
        entered = asyncio.Event()

        async def close():
            entered.set()
            await forever()

        page.close.side_effect = close
        cdp_context(page, success=False)
        owner = lifecycle.JobPages(page, lane, "job")
        cleanup = asyncio.create_task(owner.cleanup(manager, "general", lease, lambda *_: None))
        await asyncio.sleep(0)
        if started:
            await entered.wait()
        owner._cleanup_task.cancel()
        assert await asyncio.wait_for(cleanup, .2) is False
        assert lane.in_use == 1 and owner in manager._job_cleanup_owners
        page.is_closed = lambda: True
        assert await asyncio.wait_for(owner.cleanup(manager, "general", lease, lambda *_: None), .4) is True
        await until(lambda: not manager._job_cleanup_tasks and not manager._job_cleanup_owners)
        assert not lane.leases

    asyncio.run(run())


@pytest.mark.parametrize("confirmed", [False, True])
def test_manager_shutdown_settles_owned_cleanup_and_is_idempotent(monkeypatch, confirmed):
    monkeypatch.setattr(lifecycle, "SHUTDOWN_TIMEOUT_SECONDS", .2)
    monkeypatch.setattr(lifecycle, "SHUTDOWN_CANCEL_TIMEOUT_SECONDS", .1)

    async def run():
        page, unrelated = Page(), Page()
        manager, lane = resources(page)
        manager.backend = SimpleNamespace(stop=AsyncMock(), close=AsyncMock())
        lane.session = SimpleNamespace(disconnected=False, display=None)
        lease, _ = await manager.lease_lane("general")
        page.close.side_effect = forever
        session = cdp_context(page, success=False)
        owner = lifecycle.JobPages(page, lane, "job")
        waiter = asyncio.create_task(owner.cleanup(manager, "general", lease, lambda *_: None))
        await until(lambda: page.close.await_count == 1)

        async def stop(_session):
            # All job cleanup commands must settle before existing browser teardown.
            assert all(task.done() for task in owner._owned_tasks())
            page.is_closed = lambda: confirmed

        manager.backend.stop.side_effect = stop
        await asyncio.wait_for(manager.stop(), 1)
        assert await asyncio.wait_for(waiter, .2) is False
        assert all(task.done() for task in owner._owned_tasks())
        assert not manager._job_cleanup_tasks
        unrelated.close.assert_not_awaited()
        assert lane.context is None
        if confirmed:
            assert not lane.leases and not manager._job_cleanup_owners and not page.listeners
        else:
            assert lease in lane.leases and owner in manager._job_cleanup_owners
            assert page.listeners
        counts = (page.close.await_count, session.send.await_count)
        await asyncio.wait_for(manager.stop(), 1)
        assert counts == (page.close.await_count, session.send.await_count)
        manager.backend.stop.assert_awaited_once()

    asyncio.run(run())


def test_shutdown_disposes_pending_creator_and_release_tasks(monkeypatch):
    monkeypatch.setattr(lifecycle, "SHUTDOWN_TIMEOUT_SECONDS", .1)
    monkeypatch.setattr(lifecycle, "SHUTDOWN_CANCEL_TIMEOUT_SECONDS", .1)

    async def run():
        page = Page()
        manager, lane = resources(page)
        lease, _ = await manager.lease_lane("general")
        owner = lifecycle.JobPages(None, lane, "creator")
        creation = asyncio.create_task(owner.setup(forever, creates_page=True))
        await until(lambda: bool(owner.pending))
        waiter = asyncio.create_task(owner.cleanup(manager, "general", lease, lambda *_: None))
        await until(lambda: owner._cleanup_task is not None)
        await lifecycle.shutdown_job_cleanup(manager)
        assert await waiter is False
        await asyncio.gather(creation, return_exceptions=True)
        assert all(task.done() for task in owner._owned_tasks())
        assert lease in lane.leases
        await lifecycle.shutdown_job_cleanup(manager, finalize=True)
        assert lease in lane.leases and owner in manager._job_cleanup_owners

        # A separate confirmed owner can still have a stalled lease release.
        other = SimpleNamespace(get=manager.get, release_lane=AsyncMock(side_effect=forever))
        releasing = lifecycle.JobPages(page, lane, "release")
        cleanup = asyncio.create_task(releasing.cleanup(other, "general", lease, lambda *_: None))
        await until(lambda: other.release_lane.await_count == 1)
        await lifecycle.shutdown_job_cleanup(other)
        assert await cleanup is False
        assert all(task.done() for task in releasing._owned_tasks())
        assert lease in lane.leases

    asyncio.run(run())


def test_restart_reuses_pending_release_after_reaper_cancellation():
    async def run():
        page = Page()
        manager, lane = resources(page)
        lease, _ = await manager.lease_lane("general")
        release = manager.release_lane
        finish = asyncio.Event()

        async def delayed(*args):
            await finish.wait()
            return await release(*args)

        manager.release_lane = AsyncMock(side_effect=delayed)
        owner = lifecycle.JobPages(page, lane, "job")
        waiting = asyncio.create_task(owner.cleanup(manager, "general", lease, lambda *_: None))
        await until(lambda: manager.release_lane.await_count == 1)
        release_task = owner._release_task
        owner._cleanup_task.cancel()
        assert await waiting is False
        assert not release_task.done()
        retried = asyncio.create_task(owner.cleanup(manager, "general", lease, lambda *_: None))
        await asyncio.sleep(0)
        finish.set()
        assert await retried is True
        manager.release_lane.assert_awaited_once()
        assert not lane.leases
        await until(lambda: not manager._job_cleanup_tasks and not manager._job_cleanup_owners)

    asyncio.run(run())


@pytest.mark.parametrize("interrupt", ["timeout", "event", "task", "disconnect"])
def test_model_result_is_complete_before_hung_cookie_export(monkeypatch, tmp_path, interrupt):
    from worker import vision

    async def run():
        page = Page()
        manager, lane = resources(page)
        lane.session = SimpleNamespace(disconnected=False, desktop=None, display=None)
        entered = asyncio.Event()
        cancel = asyncio.Event()
        progress = AsyncMock()
        page.evaluate = AsyncMock(return_value="Claim completed")

        async def cookies(_urls):
            entered.set()
            await forever()

        lane.context.cookies = AsyncMock(side_effect=cookies)
        judge = AsyncMock(return_value={"result": True, "reason": "Claim confirmed"})
        monkeypatch.setattr(vision, "judge_result", judge)
        # Give the disconnect watcher time to fire while the read is pending.
        monkeypatch.setattr(lifecycle, "SETUP_TIMEOUT_SECONDS", .3 if interrupt == "disconnect" else .05)
        data = {"job_id": "export", "base_url": "https://fixture.test", "steps": [{
            "step_key": "result", "feature_type": "flow.result",
            "config": {"llm_enabled": True, "message": "Claim completed"},
        }], "feature_execution_settings": {"flow.result": {"llm_enabled": True, "use_vision": False}},
                "vision": {"base_url": "https://model.invalid", "model": "test", "api_key": "private"}}
        task = asyncio.create_task(executor.execute_job(manager, data, cancel, progress))
        await asyncio.wait_for(entered.wait(), 1)
        judge.assert_awaited_once()
        if interrupt == "event":
            cancel.set()
        elif interrupt == "task":
            task.cancel()
        elif interrupt == "disconnect":
            lane.session.disconnected = True
        result = await asyncio.wait_for(task, 1)
        assert result["status"] == "success" and result["message"] == "Claim completed"
        assert result["steps"][0]["data"]["judged_by"] == "llm"
        assert result["outputs"] == [{"step_key": "result", "value": True, "message": "Claim completed"}]
        assert "cookies" not in result
        assert all("cookies" not in call.args[0] and "_business_complete" not in call.args[0]
                   for call in progress.await_args_list)
        judge.assert_awaited_once()
        lane.context.cookies.assert_awaited_once()
        page.close.assert_awaited_once()
        await until(lambda: not manager._job_cleanup_tasks)
        assert not lane.leases

    asyncio.run(run())
    if interrupt == "timeout":
        assert "cookie_export_timeout" in next(tmp_path.rglob("job.log")).read_text()


@pytest.mark.parametrize("fail_finalize", [False, True])
def test_cancel_stop_prelude_still_tears_down_once(monkeypatch, fail_finalize):
    async def run():
        page = Page()
        manager, lane = resources(page)
        lane.session = SimpleNamespace(disconnected=False, display=None)
        manager.backend = SimpleNamespace(stop=AsyncMock(), close=AsyncMock())
        entered, finish = asyncio.Event(), asyncio.Event()

        async def cleanup(_manager, *, finalize=False):
            if finalize:
                if fail_finalize:
                    raise RuntimeError("finalize failed")
            else:
                entered.set()
                await finish.wait()

        monkeypatch.setattr(lifecycle, "shutdown_job_cleanup", cleanup)
        task = asyncio.create_task(manager.stop())
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        manager.backend.stop.assert_awaited_once()
        manager.backend.close.assert_awaited_once()
        assert manager._stop_task.done()
        if fail_finalize:
            with pytest.raises(RuntimeError, match="finalize failed"):
                await manager.stop()
        else:
            await manager.stop()
        manager.backend.stop.assert_awaited_once()
        manager.backend.close.assert_awaited_once()

    asyncio.run(run())


def test_shutdown_owns_guard_native_and_disposes_late_page(guarded_api, monkeypatch):
    monkeypatch.setattr("worker.browser_input.LIFECYCLE_TIMEOUT_SECONDS", .01)
    monkeypatch.setattr(lifecycle, "SHUTDOWN_CANCEL_TIMEOUT_SECONDS", .01)
    monkeypatch.setattr(lifecycle, "SHUTDOWN_TIMEOUT_SECONDS", .02)

    async def run():
        context = guarded_api.context()
        context._impl_obj.pages = []
        manager = LaneManager()
        lane = manager.get("general")
        lane.status, lane.context = "running", context
        manager.backend = SimpleNamespace(close=AsyncMock())
        lease, _ = await manager.lease_lane("general")
        owner = lifecycle.JobPages(None, lane, "late-native")
        entered, finish = asyncio.Event(), asyncio.Event()
        canceled = asyncio.Event()

        async def native():
            entered.set()
            while not finish.is_set():
                try:
                    await finish.wait()
                except asyncio.CancelledError:
                    canceled.set()

        context._impl_obj.new_page_hook = native
        creation = asyncio.create_task(owner.setup(context.new_page, creates_page=True))
        await entered.wait()
        native_task = context._impl_obj._siteflow_input_guard._lifecycle_task
        assert native_task in owner._owned_tasks()
        assert await owner.cleanup(manager, "general", lease, lambda *_: None) is False
        with pytest.raises(asyncio.TimeoutError):
            await creation
        await asyncio.wait_for(manager.stop(), .5)
        assert canceled.is_set() and native_task in owner._owned_tasks() and not native_task.done()
        assert lease in lane.leases
        finish.set()
        await until(lambda: owner._released and not manager._job_cleanup_tasks)
        assert len(owner.pages) == 1 and owner.pages[0].is_closed()
        assert not lane.leases and native_task.done()
        await until(lambda: not manager._job_cleanup_owners)

    asyncio.run(run())


def test_owner_shutdown_does_not_cancel_another_jobs_guard_native(guarded_api, monkeypatch):
    monkeypatch.setattr("worker.browser_input.LIFECYCLE_TIMEOUT_SECONDS", .01)

    async def run():
        context = guarded_api.context()
        page = guarded_api.page(context)
        owner = lifecycle.JobPages(None, SimpleNamespace(task_pages={}), "unrelated")
        finish, entered = asyncio.Event(), asyncio.Event()
        canceled = False

        async def focus():
            nonlocal canceled
            entered.set()
            try:
                await finish.wait()
            except asyncio.CancelledError:
                canceled = True
                raise

        page._impl_obj.focus_hook = focus
        other = asyncio.create_task(page.bring_to_front())
        await entered.wait()
        native_task = context._impl_obj._siteflow_input_guard._lifecycle_task
        manager = SimpleNamespace(release_lane=AsyncMock())
        await owner.cleanup(manager, "general", "lease", lambda *_: None)
        assert native_task not in owner._owned_tasks()
        await lifecycle.shutdown_job_cleanup(manager)
        assert not canceled and not native_task.done()
        finish.set()
        await asyncio.gather(other, return_exceptions=True)
        await native_task

    asyncio.run(run())


def test_shutdown_tracks_native_focus_after_setup_caller_finishes(guarded_api, monkeypatch):
    monkeypatch.setattr("worker.browser_input.LIFECYCLE_TIMEOUT_SECONDS", .01)

    async def run():
        context = guarded_api.context()
        page = guarded_api.page(context)
        owner = lifecycle.JobPages(page, SimpleNamespace(task_pages={}), "focus")
        entered = asyncio.Event()

        async def focus():
            entered.set()
            await forever()

        page._impl_obj.focus_hook = focus
        setup = asyncio.create_task(owner.setup(page.bring_to_front))
        await entered.wait()
        native_task = context._impl_obj._siteflow_input_guard._lifecycle_task
        with pytest.raises(asyncio.TimeoutError):
            await setup
        await asyncio.sleep(0)
        assert not owner.pending and native_task in owner._owned_tasks()
        manager = SimpleNamespace(release_lane=AsyncMock())
        await owner.cleanup(manager, "general", "lease", lambda *_: None)
        assert owner in manager._job_cleanup_owners
        await lifecycle.shutdown_job_cleanup(manager)
        assert native_task.cancelled()
        await until(lambda: not manager._job_cleanup_owners)

    asyncio.run(run())
