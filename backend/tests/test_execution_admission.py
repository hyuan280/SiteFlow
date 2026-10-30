from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker import executor
from worker.app import LaneManager


def test_admission_precedes_browser_lifecycle_and_progress(monkeypatch):
    async def run():
        manager = LaneManager()
        entered = []
        release = asyncio.Event()
        admitted = asyncio.Event()

        async def execute(_manager, payload, *_args):
            entered.append(payload["job_id"])
            if len(entered) == 5:
                admitted.set()
            await release.wait()
            return {"status": "success"}

        monkeypatch.setattr(executor, "_execute_admitted_job", execute)
        progress = [AsyncMock() for _ in range(8)]
        tasks = [asyncio.create_task(executor.execute_job(
            manager, {"job_id": i}, on_step=progress[i]
        )) for i in range(8)]
        await asyncio.wait_for(admitted.wait(), 2)
        assert len(entered) == 5
        assert sum(p.await_count for p in progress) == 5
        tasks[-1].cancel()
        await asyncio.gather(tasks[-1], return_exceptions=True)
        release.set()
        await asyncio.gather(*tasks[:-1])
        assert len(entered) == 7
        assert manager.get("general").execution_semaphore.active == 0

    asyncio.run(run())


def test_resizing_preserves_active_slots_and_waiters():
    async def run():
        manager = LaneManager()
        limiter = manager.get("general").execution_semaphore
        for _ in range(5):
            await limiter.__aenter__()
        entered = asyncio.Event()

        async def waiter():
            async with limiter:
                entered.set()

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        manager._set_concurrency("general", 2)
        assert manager.get("general").execution_semaphore is limiter
        for _ in range(3):
            await limiter.__aexit__()
        await asyncio.sleep(0)
        assert not entered.is_set()
        manager._set_concurrency("general", 3)
        await asyncio.wait_for(task, 1)
        assert entered.is_set()
        assert limiter.active == 2
        for _ in range(2):
            await limiter.__aexit__()

    asyncio.run(run())


def test_progress_marks_only_admitted_job_running():
    from app.services.executor import ExecutionManager

    job = SimpleNamespace(status="queued", started_at=None)
    session = SimpleNamespace(
        get=lambda *_: job, commit=lambda: None,
    )
    context = MagicMock()
    context.__enter__.return_value = session
    manager = ExecutionManager(lambda: context, SimpleNamespace(
        execution_max_runs=1, execution_max_jobs=20
    ), None)
    manager._save_steps = lambda *args, **kwargs: None
    manager._persist_progress(1, {"status": "running", "steps": []})
    assert job.status == "running"
    assert job.started_at is not None
    started = job.started_at
    manager._persist_progress(1, {"status": "success", "steps": []})
    assert job.started_at == started


@pytest.mark.parametrize("cancellation", ["admitted", "task", "event"])
def test_cookie_injection_cancellation_releases_lease(monkeypatch, tmp_path, cancellation):
    monkeypatch.setattr(executor, "ARTIFACT_ROOT", tmp_path)

    async def run():
        manager = LaneManager()
        lane = manager.get("general")
        lane.status = "running"
        injecting = asyncio.Event()
        cancel = asyncio.Event()

        async def add_cookies(_cookies):
            injecting.set()
            await asyncio.Event().wait()

        lane.context = SimpleNamespace(
            add_cookies=AsyncMock(side_effect=add_cookies), new_page=AsyncMock(), pages=[]
        )
        release = AsyncMock(wraps=manager.release_lane)
        monkeypatch.setattr(manager, "release_lane", release)
        payload = {
            "job_id": 1,
            "cookies": [{"name": "session", "value": "private-cookie", "url": "https://example.test"}],
        }
        execute = executor._execute_admitted_job if cancellation == "admitted" else executor.execute_job
        task = asyncio.create_task(execute(manager, payload, cancel))
        try:
            await asyncio.wait_for(injecting.wait(), 2)
            assert lane.in_use == 1
            lease_id, = lane.leases
            if cancellation == "event":
                cancel.set()
                result = await asyncio.wait_for(task, 2)
                assert result["status"] == "canceled"
            else:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            release.assert_awaited_once_with("general", lease_id)
            assert not lane.leases
            assert lane.execution_semaphore.active == 0
            lane.context.add_cookies.assert_awaited_once_with(payload["cookies"])
            lane.context.new_page.assert_not_awaited()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_cookie_injection_error_releases_lease_without_logging_values(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(executor, "ARTIFACT_ROOT", tmp_path)
    cookies = [{"name": "session", "value": "private-cookie", "url": "https://example.test"}]

    async def run():
        manager = LaneManager()
        lane = manager.get("general")
        lane.status = "running"
        lane.context = SimpleNamespace(
            add_cookies=AsyncMock(side_effect=RuntimeError(f"browser call log: {cookies!r}")),
            new_page=AsyncMock(),
            pages=[],
        )
        release = AsyncMock(wraps=manager.release_lane)
        monkeypatch.setattr(manager, "release_lane", release)
        result = await executor.execute_job(manager, {"job_id": 1, "cookies": cookies})
        assert result["status"] == "fail"
        assert "RuntimeError" in result["message"]
        assert "private-cookie" not in str(result)
        release.assert_awaited_once()
        assert release.await_args.args[0] == "general"
        assert not lane.leases
        assert lane.execution_semaphore.active == 0
        lane.context.new_page.assert_not_awaited()

    asyncio.run(run())
    logs = list(tmp_path.rglob("job.log"))
    assert len(logs) == 1
    log = logs[0].read_text(encoding="utf-8")
    assert "RuntimeError" in log
    assert "private-cookie" not in log
    captured = capsys.readouterr()
    assert "private-cookie" not in captured.out + captured.err
