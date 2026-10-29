from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker import executor
from worker.app import LaneManager


def test_admission_precedes_browser_lifecycle_and_progress(monkeypatch):
    async def run():
        manager = LaneManager()
        entered = []
        release = asyncio.Event()

        async def execute(_manager, payload, *_args):
            entered.append(payload["job_id"])
            await release.wait()
            return {"status": "success"}

        monkeypatch.setattr(executor, "_execute_admitted_job", execute)
        progress = [AsyncMock() for _ in range(8)]
        tasks = [asyncio.create_task(executor.execute_job(
            manager, {"job_id": i}, on_step=progress[i]
        )) for i in range(8)]
        await asyncio.sleep(0)
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
