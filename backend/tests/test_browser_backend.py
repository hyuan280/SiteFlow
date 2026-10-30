import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker.browser_backend import ManagerBackend, create_backend  # noqa: E402


@pytest.mark.parametrize("tier,version", [("pro", "155"), ("keyless", "155")])
def test_rejects_non_keyless_backend(tmp_path, tier, version):
    async def run():
        backend = ManagerBackend(tmp_path)
        async def request(*args, **kwargs):
            return {"license_tier": tier, "binary_version": version}
        backend.request = request
        # The version gate must precede profile creation and browser connection.
        try:
            with pytest.raises(RuntimeError, match="keyless"):
                await backend.start("general", None)
        finally:
            await backend.close()
    asyncio.run(run())


def test_unknown_backend_fails_explicitly(monkeypatch, tmp_path):
    monkeypatch.setenv("SITEFLOW_BROWSER_BACKEND", "other")
    with pytest.raises(ValueError, match="Unsupported"):
        create_backend(tmp_path)


def test_remote_error_does_not_leak_response(tmp_path):
    async def run():
        backend = ManagerBackend(tmp_path)
        await backend.client.aclose()
        backend.client = httpx.AsyncClient(base_url="http://browser", transport=httpx.MockTransport(
            lambda request: httpx.Response(403, json={"secret": "must-not-leak"})))
        try:
            with pytest.raises(RuntimeError) as error:
                await backend.request("GET", "/api/status")
            assert str(error.value) == "Browser backend returned HTTP 403"
        finally:
            await backend.close()
    asyncio.run(run())


def test_vnc_url_uses_only_configured_provider_and_private_headers(monkeypatch, tmp_path):
    monkeypatch.setenv("SITEFLOW_BROWSER_URL", "https://browser.example")
    monkeypatch.setenv("SITEFLOW_BROWSER_TOKEN", "private-test-token")
    async def run():
        backend = ManagerBackend(tmp_path)
        try:
            url, headers = backend.vnc_connection(SimpleNamespace(profile_id="id/123"))
            assert url == "wss://browser.example/api/profiles/id%2F123/vnc"
            assert "private-test-token" not in url
            assert headers == {"Authorization": "Bearer private-test-token"}
        finally:
            await backend.close()
    asyncio.run(run())


def test_profile_mapping_is_validated(tmp_path):
    (tmp_path / "browser-profiles.json").write_text(json.dumps(["not-a-mapping"]))
    with pytest.raises(ValueError, match="mapping"):
        ManagerBackend(tmp_path)


def test_backend_disconnect_cancels_admitted_job(monkeypatch, tmp_path):
    from worker import executor
    from worker.app import LaneManager

    async def run():
        manager = LaneManager()
        lane = manager.get("general")
        lane.session = SimpleNamespace(disconnected=False)
        lane.leases.add("test-lease")
        entered = asyncio.Event()
        settled = asyncio.Event()

        async def job(*args):
            entered.set()
            try:
                await asyncio.sleep(30)
            finally:
                settled.set()

        monkeypatch.setattr(executor, "_execute_admitted_job", job)
        task = asyncio.create_task(executor.execute_job(manager, {"lane_key": "general"}))
        await entered.wait()
        lane.session.disconnected = True
        result = await asyncio.wait_for(task, 2)
        assert result["status"] == "error"
        assert settled.is_set()
        assert lane.execution_semaphore.active == 0
        await manager.backend.close()
    asyncio.run(run())


def test_shutdown_only_disconnects_then_stops_mapped_profile(tmp_path):
    async def run():
        backend = ManagerBackend(tmp_path)
        calls = []
        async def disconnect():
            calls.append("disconnect")
        async def request(method, path):
            calls.append((method, path))
            return {"status": "running"}
        backend.request = request
        try:
            await backend.stop(SimpleNamespace(browser=SimpleNamespace(close=disconnect), profile_id="owned"))
            assert calls == [
                "disconnect", ("GET", "/api/profiles/owned/status"),
                ("POST", "/api/profiles/owned/stop"),
            ]
        finally:
            await backend.close()
    asyncio.run(run())


@pytest.mark.parametrize("initial_status", ["running", "stopped"])
def test_provider_stop_is_idempotent(tmp_path, initial_status):
    async def run():
        backend = ManagerBackend(tmp_path)
        calls = []
        status = initial_status

        async def disconnect():
            calls.append("disconnect")

        async def request(method, path):
            nonlocal status
            calls.append((method, path))
            if method == "GET":
                return {"status": status}
            assert status == "running"
            status = "stopped"
            return {}

        backend.request = request
        session = SimpleNamespace(browser=SimpleNamespace(close=disconnect), profile_id="owned/123")
        try:
            await backend.stop(session)
            await backend.stop(session)
            expected = ["disconnect", ("GET", "/api/profiles/owned%2F123/status")]
            if initial_status == "running":
                expected.append(("POST", "/api/profiles/owned%2F123/stop"))
            expected.extend(["disconnect", ("GET", "/api/profiles/owned%2F123/status")])
            assert calls == expected
        finally:
            await backend.close()
    asyncio.run(run())


@pytest.mark.parametrize("failed_method", ["GET", "POST"])
@pytest.mark.parametrize("failure", [403, 500, "timeout"])
def test_provider_stop_propagates_real_failures(tmp_path, failed_method, failure):
    async def run():
        backend = ManagerBackend(tmp_path)
        await backend.client.aclose()
        calls = []

        async def disconnect():
            calls.append("disconnect")

        def respond(request):
            calls.append(request.method)
            if request.method == failed_method:
                if failure == "timeout":
                    raise httpx.ReadTimeout("provider unavailable", request=request)
                return httpx.Response(failure, json={"secret": "must-not-leak"})
            return httpx.Response(200, json={"status": "running"})

        backend.client = httpx.AsyncClient(
            base_url="http://browser", transport=httpx.MockTransport(respond),
        )
        try:
            error = httpx.ReadTimeout if failure == "timeout" else RuntimeError
            message = "provider unavailable" if failure == "timeout" else f"HTTP {failure}"
            with pytest.raises(error, match=message):
                await backend.stop(SimpleNamespace(browser=SimpleNamespace(close=disconnect), profile_id="owned"))
            assert calls == (["disconnect", "GET"] if failed_method == "GET"
                             else ["disconnect", "GET", "POST"])
        finally:
            await backend.close()
    asyncio.run(run())


def test_provider_stop_runs_even_if_cdp_close_fails(tmp_path):
    async def run():
        backend = ManagerBackend(tmp_path)
        calls = []

        async def disconnect():
            raise RuntimeError("CDP close failed")

        async def request(method, path):
            calls.append((method, path))
            return {"status": "running"}

        backend.request = request
        try:
            with pytest.raises(RuntimeError, match="CDP close failed"):
                await backend.stop(SimpleNamespace(browser=SimpleNamespace(close=disconnect), profile_id="owned"))
            assert calls == [("GET", "/api/profiles/owned/status"), ("POST", "/api/profiles/owned/stop")]
        finally:
            await backend.close()
    asyncio.run(run())


def test_profile_creation_and_reuse_do_not_adopt_unrelated_profiles(monkeypatch, tmp_path):
    calls = []

    class Browser:
        contexts = [SimpleNamespace()]
        def on(self, event, callback):
            self.callback = callback
        async def close(self):
            pass

    browser = Browser()

    async def connect(url, **kwargs):
        assert url.endswith("/api/profiles/owned/cdp")
        assert kwargs["headers"] == {"Authorization": "Bearer fixture"}
        return browser

    class Playwright:
        async def start(self):
            return SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect), stop=self.stop)
        async def stop(self):
            pass

    monkeypatch.setenv("SITEFLOW_BROWSER_TOKEN", "fixture")
    monkeypatch.setenv("SITEFLOW_BROWSER_DESKTOP", "none")
    monkeypatch.setitem(sys.modules, "playwright.async_api", SimpleNamespace(async_playwright=Playwright))
    monkeypatch.setitem(sys.modules, "worker.browser_input", SimpleNamespace(humanize_actions=lambda context: None))

    async def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/api/status":
            return {"license_tier": "keyless", "binary_version": "146.0.7680.177.5"}
        if path == "/api/profiles":
            assert method == "POST"
            assert kwargs["json"]["proxy"] == "http://private-proxy:8080"
            assert kwargs["json"]["locale"] == "zh-CN"
            return {"id": "owned"}
        if path.endswith("/status"):
            return {"status": "stopped"}
        return {"display": ":100"}

    async def run():
        backend = ManagerBackend(tmp_path)
        backend.request = request
        try:
            for _ in range(2):
                session = await backend.start("general", "http://private-proxy:8080")
                assert session.desktop is None
                assert not session.disconnected
                await backend.stop(session)
            assert sum(path == "/api/profiles" for _, path, _ in calls) == 1
            assert json.loads((tmp_path / "browser-profiles.json").read_text()) == {"general": "owned"}
            assert all(path.startswith("/api/profiles/owned") or path in {"/api/status", "/api/profiles"}
                       for _, path, _ in calls)
            browser.callback(browser)
            assert session.disconnected
        finally:
            await backend.close()
    asyncio.run(run())


def test_lane_start_cancellation_cleans_provider(monkeypatch, tmp_path):
    from worker.app import LaneManager
    async def run():
        manager = LaneManager()
        lane = manager.get("general")
        stopped = []
        session = SimpleNamespace(disconnected=False)
        async def launch(_lane):
            _lane.session = session
            raise asyncio.CancelledError
        async def stop(value):
            stopped.append(value)
        monkeypatch.setattr(manager, "_start_browser", launch)
        monkeypatch.setattr(manager.backend, "stop", stop)
        with pytest.raises(asyncio.CancelledError):
            await manager.start_lane("general")
        assert stopped == [session]
        assert lane.session is None and lane.context is None
        assert lane.status == "error"
        await manager.backend.close()
    asyncio.run(run())


def test_prepare_stops_only_recorded_profiles_and_disables_autostart(tmp_path):
    (tmp_path / "browser-profiles.json").write_text(json.dumps({"general": "owned"}))
    async def run():
        backend = ManagerBackend(tmp_path)
        calls = []
        async def request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            return {"status": "running"}
        backend.request = request
        try:
            await backend.prepare()
            assert calls == [
                ("GET", "/api/profiles/owned/status", {}),
                ("POST", "/api/profiles/owned/stop", {}),
                ("PUT", "/api/profiles/owned", {"json": {
                    "auto_launch": False, "restore_session": False, "capture_preview": False,
                }}),
            ]
        finally:
            await backend.close()
    asyncio.run(run())


@pytest.mark.parametrize("protection", ["idle", "lease", "viewer", "disabled"])
def test_siteflow_idle_policy_controls_provider_stop(monkeypatch, protection):
    from worker.app import LaneManager
    async def run():
        manager = LaneManager()
        manager.idle_stop_seconds = 1 if protection != "disabled" else 0
        lane = manager.get("general")
        lane.idle_stop_seconds = manager.idle_stop_seconds
        lane.status = "running"
        lane.browser = "running"
        lane.session = SimpleNamespace(disconnected=False, display=":100")
        lane.last_activity_at = 0
        if protection == "lease":
            lane.leases.add("active")
        if protection == "viewer":
            lane.viewers.add("active")
        stopped = []
        async def stop(session):
            stopped.append(session)
        monkeypatch.setattr(manager.backend, "stop", stop)
        monitor = asyncio.create_task(manager._idle_monitor())
        try:
            await asyncio.sleep(1.1)
            assert len(stopped) == (1 if protection == "idle" else 0)
            assert lane.status == ("stopped" if protection == "idle" else "running")
            if protection in {"lease", "viewer"}:
                if protection == "lease":
                    await manager.release_lane("general", "active")
                else:
                    await manager.viewer_release("general", "active")
                assert lane.last_activity_at > 0
                await asyncio.sleep(2.1)
                assert len(stopped) == 1
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
            await manager.backend.close()
    asyncio.run(run())


def test_failed_provider_stop_is_not_reported_as_stopped(monkeypatch):
    from worker.app import LaneManager, LaneStartError
    async def run():
        manager = LaneManager()
        lane = manager.get("general")
        lane.status = "running"
        lane.session = SimpleNamespace(disconnected=False)
        async def stop(session):
            raise RuntimeError("unavailable")
        monkeypatch.setattr(manager.backend, "stop", stop)
        try:
            with pytest.raises(LaneStartError):
                await manager.stop_lane("general")
            assert lane.status == "error"
            assert lane.session is not None
        finally:
            await manager.backend.close()
    asyncio.run(run())


@pytest.mark.parametrize("idle_seconds", [0, 3600])
@pytest.mark.parametrize("leased", [False, True])
def test_disconnected_monitor_stops_after_leases_drain_and_retries(monkeypatch, idle_seconds, leased):
    from worker.app import LaneManager

    async def run():
        manager = LaneManager()
        manager.idle_stop_seconds = idle_seconds
        lane = manager.get("general")
        lane.idle_stop_seconds = idle_seconds
        lane.status = lane.browser = "running"
        session = SimpleNamespace(disconnected=True, display=":100")
        context = SimpleNamespace(pages=[])
        lane.session = session
        lane.context = context
        lane.page = page = object()
        lane.viewers.add("stale-viewer")
        if leased:
            lane.leases.add("active")
        stopped = []

        async def stop(value):
            assert lane.lock.locked()
            stopped.append(value)
            if len(stopped) == 1:
                raise RuntimeError("provider unavailable")

        # Drive monitor iterations without relying on wall-clock sleeps.
        waiting = asyncio.Queue()
        ticks = asyncio.Queue()

        async def sleep(delay):
            assert delay == 1
            waiting.put_nowait(None)
            await ticks.get()

        async def tick():
            ticks.put_nowait(None)
            await asyncio.wait_for(waiting.get(), 2)

        monkeypatch.setattr(manager.backend, "stop", stop)
        monkeypatch.setattr("worker.app.asyncio.sleep", sleep)
        monitor = asyncio.create_task(manager._idle_monitor())
        try:
            await asyncio.wait_for(waiting.get(), 2)
            if leased:
                await tick()
                assert stopped == []
                assert lane.status == lane.browser == "error"
                assert lane.session is session
                assert lane.leases == {"active"}
                await manager.release_lane("general", "active")
            await tick()
            assert stopped == [session]
            assert lane.status == lane.browser == "error"
            assert lane.session is session
            assert lane.context is context and lane.page is page
            assert lane.viewers == {"stale-viewer"}
            await tick()
            assert stopped == [session, session]
            assert lane.status == lane.browser == "stopped"
            assert lane.session is None and lane.context is None and lane.page is None
            assert not lane.viewers and not lane.leases
            assert lane.error is None
            await tick()
            assert stopped == [session, session]
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
            await manager.backend.close()
    asyncio.run(run())
