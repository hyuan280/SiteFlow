"""Opt-in real worker container, gRPC runtime, HTTP fixture and isolated API DB."""
import json
import os
import socket
import subprocess
import threading
import time
import uvicorn
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path

import grpc
import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.helpers import login_client, make_settings

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "plugins/examples/demo"


@pytest.mark.skipif(os.environ.get("SITEFLOW_DEMO_E2E") != "1", reason="requires Docker worker image and frontend node_modules")
def test_imported_demo_real_execution(tmp_path, monkeypatch):
    # Never send test requests to an unrelated worker already using this port.
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", 16300))
    for path in [ROOT, ROOT / "worker", ROOT / "proto", ROOT / "plugins/runtime", DEMO]:
        monkeypatch.syspath_prepend(str(path))
    import demo_site
    import demo_plugin
    from siteflow_plugin_runtime.config import Config
    from siteflow_plugin_runtime.control import PluginState, PluginControlServicer
    from siteflow_plugin_runtime.execution import PluginExecutionServicer
    from siteflow_plugin_runtime.secrets import SecretProviderServicer
    from siteflow_proto import plugin_pb2_grpc

    state = PluginState.load(Config(control_url="http://unused", plugin_id="demo", endpoint="unused", token_file=None,
        token_value="isolated-demo-token", manifest_path=str(DEMO / "plugin.json"),
        features_file=str(DEMO / "features.json"), captcha_methods_file=str(DEMO / "captcha_methods.json")))
    rpc = grpc.server(ThreadPoolExecutor(max_workers=8))
    plugin_pb2_grpc.add_PluginControlServicer_to_server(PluginControlServicer(state), rpc)
    calls = []
    bad_otp = [False]
    fault = [None]
    fault_entered = threading.Event()
    fault_stopped = threading.Event()

    def credentials(request):
        assert set(request) == {"job_id", "site_id", "step_key", "site_origin", "match_mode", "custom_match", "service_hint", "username_hint"}
        assert request["match_mode"] == "domain"
        calls.append("credentials")
        return demo_plugin.resolve_credential(request)

    def totp(request):
        assert request["service_hint"] == "SiteFlow Demo"
        assert request["username_hint"] == "admin"
        calls.append("totp")
        return {"code": "000000"} if bad_otp[0] else demo_plugin.generate_totp(request)

    plugin_pb2_grpc.add_SecretProviderServicer_to_server(SecretProviderServicer(state, credentials, totp), rpc)

    def step(request, broker):
        calls.append("step")
        if fault[0]:
            fault_entered.set()
            try:
                if fault[0] == "delayed":
                    broker.action("click", selectors=["#delayed-action"], timeout_ms=30000)
                else:
                    while request["is_active"]():
                        time.sleep(0.02)
                return {"status": "success", "message": "late result must not apply"}
            finally:
                fault_stopped.set()
        return demo_plugin.execute_step(request, broker)

    def captcha(request):
        calls.append("captcha")
        return demo_plugin.recognize_captcha(request)

    plugin_pb2_grpc.add_PluginExecutionServicer_to_server(PluginExecutionServicer(state, step, captcha), rpc)
    port = rpc.add_insecure_port("127.0.0.1:0")
    rpc.start()
    fixture = ThreadingHTTPServer(("127.0.0.1", 0), demo_site.Handler)
    thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    thread.start()
    name = "siteflow-demo-e2e-" + str(os.getpid())
    api_socket = socket.socket()
    api_socket.bind(("127.0.0.1", 0))
    api_port = api_socket.getsockname()[1]
    api_server = None
    api_thread = None
    browser_name = name + "-browser"
    x11_volume = name + "-x11"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        browser_port = probe.getsockname()[1]
    browser_image = os.environ.get("SITEFLOW_DEMO_BROWSER_IMAGE", "cloakhq/cloakbrowser-manager@sha256:04b82e42fe74ec34352580da47b096695e6a8d08745f88e3f5fd821be5ebf989")
    try:
        subprocess.run(["docker", "run", "--rm", "-d", "--name", browser_name, "--network", "host",
            "--shm-size", "1g", "--tmpfs", "/data", "--tmpfs", "/tmp",
            "-v", f"{x11_volume}:/tmp/.X11-unix",
            "-e", "AUTH_TOKEN=isolated-browser-token", "-e", "CLOAKBROWSER_AUTO_UPDATE=false",
            "-e", "CLOAKBROWSER_BINARY_PATH=/root/.cloakbrowser/chromium-146.0.7680.177.5/chrome",
            "--entrypoint", "uvicorn", browser_image, "backend.main:app", "--host", "127.0.0.1", "--port", str(browser_port)], check=True, capture_output=True)
        for _ in range(120):
            try:
                if httpx.get(f"http://127.0.0.1:{browser_port}/api/health").is_success:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            pytest.fail("isolated browser backend did not start: " + subprocess.check_output(["docker", "logs", browser_name], stderr=subprocess.STDOUT).decode())
        subprocess.run(["docker", "run", "--rm", "-d", "--name", name, "--network", "host",
            "-e", f"SITEFLOW_BROWSER_URL=http://127.0.0.1:{browser_port}", "-e", "SITEFLOW_BROWSER_TOKEN=isolated-browser-token",
            "-v", f"{x11_volume}:/tmp/.X11-unix:ro",
            "-e", "SITEFLOW_PLUGIN_BROKER_URL=http://127.0.0.1:16300/plugin-broker",
            "-e", f"SITEFLOW_SECRET_API_URL=http://127.0.0.1:{api_port}/api/v1/internal/secrets/resolve",
            "-e", f"SITEFLOW_PLUGIN_AUTH_API_URL=http://127.0.0.1:{api_port}/api/v1/internal/plugins/authorize",
            "-e", "SITEFLOW_WORKER_DATA=/tmp/demo-worker",
            "-v", f"{ROOT}/worker:/app/worker:ro", "-v", f"{ROOT}/proto/siteflow_proto:/app/siteflow_proto:ro",
            "--entrypoint", "uvicorn", os.environ.get("SITEFLOW_DEMO_WORKER_IMAGE", "siteflow-worker"), "worker.app:create_app", "--factory", "--host", "127.0.0.1", "--port", "16300"], check=True, capture_output=True)
        for _ in range(120):
            try:
                if httpx.get("http://127.0.0.1:16300/health").is_success:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            pytest.fail("isolated worker did not start: " + subprocess.check_output(["docker", "logs", name], stderr=subprocess.STDOUT).decode())
        smoke = subprocess.run(["docker", "exec", "-i", name, "python"],
            input=(ROOT / "backend/tests/remote_browser_smoke.py").read_text(),
            capture_output=True, text=True, timeout=120)
        assert smoke.returncode == 0, smoke.stdout + smoke.stderr
        settings = make_settings(tmp_path, worker_url="http://127.0.0.1:16300", worker_timeout=180,
            browser_token="isolated-browser-token",
            plugin_runtime_tokens='{"demo":"isolated-demo-token"}', plugin_resolve_endpoint=False,
            plugin_endpoint_allowlist=f"127.0.0.1:{port}", execution_max_jobs=1)
        with TestClient(create_app(settings)) as client:
            api_server = uvicorn.Server(uvicorn.Config(client.app, lifespan="off", log_level="warning"))
            api_thread = threading.Thread(target=api_server.run, kwargs={"sockets": [api_socket]}, daemon=True)
            api_thread.start()
            for _ in range(100):
                if api_server.started:
                    break
                time.sleep(0.05)
            assert api_server.started
            login_client(client)
            response = client.post("/api/v1/plugin-runtime/register", headers={"X-Plugin-Token": "isolated-demo-token"}, json={
                "plugin_id": "demo", "protocol_version": 1, "endpoint": f"127.0.0.1:{port}", "manifest": state.manifest})
            assert response.status_code == 201, response.text
            assert response.json()["status"] == "unapproved"
            response = client.post("/api/v1/plugin-runtime/plugins/demo/approve", json={})
            assert response.status_code == 200, response.text
            rows = json.loads(subprocess.check_output(["node", str(DEMO / "import_adapter.mjs")], cwd=ROOT))
            assert len(rows) == 1
            # Humanized input and reads add deliberate pauses to multi-action RPCs.
            for step in rows[0]["steps"]:
                if step["feature_type"].startswith("plugin."):
                    step["timeout_seconds"] = 60
            ids = []
            for row in rows:
                assert row["enabled"] is False and row["bitwarden_enabled"] is False
                row["base_url"] = f"http://127.0.0.1:{fixture.server_port}"
                response = client.post("/api/v1/sites", json=row)
                assert response.status_code == 201, response.text
                ids.append(response.json()["id"])
                # Enable only the isolated imported copy.
                row["enabled"] = True
                response = client.put(f"/api/v1/sites/{ids[-1]}", json=row)
                assert response.status_code == 200, response.text
            response = client.post("/api/v1/runs", json={"site_ids": ids, "force": True})
            assert response.status_code == 201, response.text
            run_id = response.json()["id"]
            for _ in range(600):
                result = client.get(f"/api/v1/runs/{run_id}").json()
                if result["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.5)
            if result["status"] != "success":
                logs = subprocess.run(["docker", "exec", name, "python", "-c",
                    "from pathlib import Path; print('\\n'.join(p.read_text() for p in Path('/app/artifacts').glob('run-1/job-*/job.log')))"], capture_output=True, text=True)
                pytest.fail(str(result) + "\n" + logs.stdout)
            assert len(result["jobs"]) == 1
            for job in result["jobs"]:
                assert job["status"] == "success", job
                assert [s["step_key"] for s in job["steps"]] == [s["step_key"] for s in rows[0]["steps"]]
                assert len(job["steps"]) == 19
                assert all(s["status"] == "success" for s in job["steps"]), job
                for step, definition in zip(job["steps"], rows[0]["steps"]):
                    if "expected_text" in definition["config"]:
                        assert step["data"]["text"] == definition["config"]["expected_text"]
            assert calls.count("captcha") == 2
            assert calls.count("step") == 7
            assert calls.count("credentials") == calls.count("totp") == 1
            assert demo_site.Handler.login_results[-1] is True
            assert not client.app.state.executor.secret_providers.leases
            # Manager normalizes RFB. SiteFlow must forward without Kasm rewriting.
            with client.websocket_connect("/api/v1/lanes/general/vnc", subprotocols=["binary"]) as viewer:
                assert viewer.receive_bytes().startswith(b"RFB ")
                viewer.send_bytes(b"RFB 003.008\n")
                security = viewer.receive_bytes()
                assert 1 in security[1:]
                viewer.send_bytes(b"\x01")
                assert viewer.receive_bytes() == b"\x00\x00\x00\x00"
                viewer.send_bytes(b"\x01")
                initialized = viewer.receive_bytes()
                assert int.from_bytes(initialized[:2], "big") == 1920
                assert int.from_bytes(initialized[2:4], "big") == 1080
                # Ask for standard raw framebuffer data and send a mouse event.
                # Manager, not SiteFlow, performs the Kasm pointer translation.
                viewer.send_bytes(b"\x02\x00\x00\x01\x00\x00\x00\x00")
                viewer.send_bytes(b"\x05\x00\x00\x64\x00\x64")
                viewer.send_bytes(b"\x03\x00\x00\x00\x00\x00\x00\x40\x00\x40")
                assert viewer.receive_bytes()[0] == 0
            assert "isolated-demo-token" not in json.dumps(result)
            assert "123456" not in json.dumps(result)
            picker = client.post("/api/v1/picker/sessions", json={
                "url": f"http://127.0.0.1:{fixture.server_port}/fault", "lane_key": "interaction"})
            assert picker.is_success, picker.text
            picker_id = picker.json()["session_id"]
            assert client.get(f"/api/v1/picker/sessions/{picker_id}").is_success
            assert client.delete(f"/api/v1/picker/sessions/{picker_id}").is_success
            print("PASS: one imported site, 19 ordered steps, server-verified login, real credential/TOTP gRPC through leases")
            negative = rows[0]
            negative["base_url"] = f"http://127.0.0.1:{fixture.server_port}"
            negative["enabled"] = True
            negative["steps"][-1]["config"]["expected_text"] = "This must not match"
            response = client.put(f"/api/v1/sites/{ids[0]}", json=negative)
            assert response.status_code == 200, response.text
            response = client.post("/api/v1/runs", json={"site_ids": [ids[0]], "force": True})
            assert response.status_code == 201, response.text
            for _ in range(600):
                failed = client.get(f"/api/v1/runs/{response.json()['id']}").json()
                if failed["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.5)
            assert failed["status"] == "fail", failed
            assert failed["jobs"][0]["status"] == "fail"
            assert len(failed["jobs"][0]["steps"]) == 19
            assert all(s["status"] == "success" for s in failed["jobs"][0]["steps"][:-1])
            assert failed["jobs"][0]["steps"][-1]["status"] == "fail"
            assert failed["jobs"][0]["steps"][-1]["message"] == "unexpected result text"
            print("PASS: unexpected result persists failed step, job and run")
            bad_otp[0] = True
            response = client.post("/api/v1/runs", json={"site_ids": ids, "force": True})
            assert response.status_code == 201
            for _ in range(600):
                rejected = client.get(f"/api/v1/runs/{response.json()['id']}").json()
                if rejected["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.5)
            assert rejected["status"] != "success", rejected
            assert demo_site.Handler.login_results[-1] is False
            assert calls.count("credentials") == calls.count("totp") == 3
            print("PASS: wrong remotely generated OTP rejected by server and job")
            if os.environ.get("SITEFLOW_DEMO_FAULTS") == "1":
                import copy
                bad_otp[0] = False
                original = json.loads(subprocess.check_output(["node", str(DEMO / "import_adapter.mjs")], cwd=ROOT))[0]
                handoff = copy.deepcopy(original)
                handoff.update(base_url=f"http://127.0.0.1:{fixture.server_port}", enabled=True)
                handoff["steps"] = [s for s in handoff["steps"] if s["step_key"] in {"login_open", "login_totp", "login_submit", "popup_open", "popup_verify"}]
                handoff["steps"].append({"step_key": "popup_builtin", "feature_type": "action.click", "position": 7500, "config": {"selector": ["#sign"]}})
                assert client.put(f"/api/v1/sites/{ids[0]}", json=handoff).is_success
                response = client.post("/api/v1/runs", json={"site_ids": ids, "force": True})
                handoff_run = response.json()["id"]
                for _ in range(300):
                    completed = client.get(f"/api/v1/runs/{handoff_run}").json()
                    if completed["status"] not in {"queued", "running"}:
                        break
                    time.sleep(0.1)
                assert completed["status"] == "success", completed
                assert completed["jobs"][0]["steps"][-1]["step_key"] == "popup_builtin"
                assert completed["jobs"][0]["steps"][-1]["status"] == "success"
                for scenario in json.loads((DEMO / "faults.json").read_text()):
                    fault_entered.clear()
                    fault_stopped.clear()
                    fault[0] = scenario["mode"]
                    row = copy.deepcopy(original)
                    row.update(base_url=f"http://127.0.0.1:{fixture.server_port}", enabled=True)
                    # No secret calls in fault cases; only the local fixture is used.
                    row["steps"] = [row["steps"][0], next(s for s in row["steps"] if s["feature_type"].startswith("plugin."))]
                    row["steps"][0]["config"]["url"] = "{base_url}/fault"
                    mutations_before = demo_site.Handler.fault_mutations
                    row["steps"][-1]["timeout_seconds"] = scenario["timeout"]
                    response = client.put(f"/api/v1/sites/{ids[0]}", json=row)
                    assert response.status_code == 200, response.text
                    if scenario["mode"] == "drop":
                        rpc.stop(0).wait()
                    response = client.post("/api/v1/runs", json={"site_ids": ids, "force": True})
                    assert response.status_code == 201, response.text
                    fault_run = response.json()["id"]
                    if scenario["name"] in {"cancel", "revoke"}:
                        assert fault_entered.wait(30)
                        terminated_at = time.monotonic()
                        if scenario["name"] == "cancel":
                            response = client.post(f"/api/v1/runs/{fault_run}/cancel")
                        else:
                            response = client.post("/api/v1/plugin-runtime/plugins/demo/revoke", json={})
                        assert response.is_success, response.text
                    for _ in range(120):
                        failed = client.get(f"/api/v1/runs/{fault_run}").json()
                        if failed["status"] not in {"queued", "running"}:
                            break
                        time.sleep(0.1)
                    assert failed["status"] not in {"success", "queued", "running"}, failed
                    if scenario["mode"] != "drop":
                        assert fault_stopped.wait(5)
                    if scenario["name"] in {"cancel", "revoke"}:
                        print(f"{scenario['name']} observed stop latency: {time.monotonic() - terminated_at:.3f}s")
                    status = httpx.get("http://127.0.0.1:16300/status").json()
                    assert all(lane["in_use"] == 0 for lane in status["lanes"])
                    assert status["broker_sessions"] == 0
                    assert all(lane["task_pages"] == 0 and lane["browser_pages"] <= 1 for lane in status["lanes"])
                    assert not client.app.state.executor.secret_providers.leases
                    time.sleep(4.2)
                    assert demo_site.Handler.fault_mutations == mutations_before
                    print(f"PASS fault {scenario['name']}: terminal failure/cancel, settled resources, no delayed site mutation")
                    if scenario["name"] == "revoke":
                        response = client.post("/api/v1/plugin-runtime/plugins/demo/approve", json={})
                        assert response.is_success, response.text
                # Killing the external Manager must terminate an in-flight job;
                # no browser restart or job replay is allowed while it holds a lease.
                # Use a browser-only wait instead of relying on that RPC.
                row["steps"][-1] = {"step_key": "backend_wait", "feature_type": "action.wait",
                    "position": 2000, "config": {"seconds": 5}}
                row["steps"].append({"step_key": "backend_click", "feature_type": "action.click",
                    "position": 3000, "config": {"selector": ["#delayed-action"]}})
                assert client.put(f"/api/v1/sites/{ids[0]}", json=row).is_success
                response = client.post("/api/v1/runs", json={"site_ids": ids, "force": True})
                backend_run = response.json()["id"]
                for _ in range(100):
                    current = client.get(f"/api/v1/runs/{backend_run}").json()
                    if any(s["step_key"] == "backend_wait" for j in current["jobs"] for s in j["steps"]):
                        break
                    time.sleep(.1)
                subprocess.run(["docker", "stop", "-t", "1", browser_name], check=True, capture_output=True)
                for _ in range(150):
                    current = client.get(f"/api/v1/runs/{backend_run}").json()
                    if current["status"] not in {"queued", "running"}:
                        break
                    time.sleep(.1)
                assert current["status"] not in {"success", "queued", "running"}, current
                status = httpx.get("http://127.0.0.1:16300/status").json()
                assert all(lane["in_use"] == 0 and lane["task_pages"] == 0 for lane in status["lanes"])
    finally:
        if api_server:
            api_server.should_exit = True
        if api_thread:
            api_thread.join(timeout=10)
        api_socket.close()
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        subprocess.run(["docker", "rm", "-f", browser_name], capture_output=True)
        subprocess.run(["docker", "volume", "rm", x11_volume], capture_output=True)
        fixture.shutdown()
        fixture.server_close()
        thread.join()
        rpc.stop(0).wait()
