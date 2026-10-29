import importlib.util
import json
import threading
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen
from urllib.request import build_opener, HTTPCookieProcessor
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

import pytest
from PIL import Image

from app.schemas.workflow import SiteWrite
from app.features.catalog import get_feature
from app.features.plugin_registry import build_remote_feature

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "plugins/examples/demo"


def load(name):
    spec = importlib.util.spec_from_file_location(name, DEMO / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_import_schema(monkeypatch):
    # Pure schema check; real approval/catalog loading is covered by test_demo_e2e.
    monkeypatch.setattr("app.features.plugin_registry.plugin_captcha_method_keys", lambda: {"plugin.demo.gd5"})
    document = json.loads((DEMO / "sites.import.json").read_text())
    assert (document["format"], document["version"], document["kind"]) == ("siteflow.export", 1, "sites")
    assert len(document["sites"]) == 1
    steps = document["sites"][0]["steps"]
    assert [step["step_key"] for step in steps] == [
        "login_open", "login_totp", "login_submit", "login_assert",
        "checkin_open", "checkin_verify", "form_open", "form_fill", "form_verify",
        "popup_open", "popup_verify", "frame_open", "frame_verify",
        "captcha_150_open", "captcha_150_recognize", "captcha_150_verify",
        "captcha_151_open", "captcha_151_recognize", "captcha_151_verify",
    ]
    assert [step["position"] for step in steps] == [100, 200, 300, 400, *range(1000, 16000, 1000)]
    assert [step["config"]["url"] for step in steps if step["feature_type"] in {"flow.open", "action.navigate"}] == [
        "{base_url}/login", "{base_url}/checkin", "{base_url}/form", "{base_url}/popup", "{base_url}/frame",
        "{base_url}/captcha", "{base_url}/captcha?scaled=1",
    ]
    assert [step["step_key"] for step in steps if "popup_selector" in step["config"]] == ["popup_verify"]
    for row in document["sites"]:
        row = dict(row)
        assert row.pop("template_name") is None
        site = SiteWrite.model_validate(row)
        assert site.base_url == "http://demo-site:8099"
        assert not site.enabled and not site.bitwarden_enabled
        assert "credentials" not in row
        remote = build_remote_feature("demo", json.loads((DEMO / "features.json").read_text())["features"][0])
        for step in site.steps:
            feature = remote if step.feature_type == remote.key else get_feature(step.feature_type)
            assert feature is not None
            feature.validate_full(step.config)


def test_demo_captcha_and_http(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "worker"))
    monkeypatch.syspath_prepend(str(ROOT / "plugins/runtime"))
    monkeypatch.syspath_prepend(str(ROOT / "proto"))
    site = load("demo_site")
    plugin = load("demo_plugin")
    server = ThreadingHTTPServer(("127.0.0.1", 0), site.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with pytest.raises(HTTPError) as denied:
            urlopen(base + "/checkin")
        assert denied.value.code == 401
        for factors in ({"username": "admin", "password": "password"},
                        {"username": "admin", "password": "password", "code": "000000"},
                        {"username": "admin", "password": "wrong", "code": "123456"}):
            with pytest.raises(HTTPError) as denied:
                urlopen(base + "/login", urlencode(factors).encode())
            assert denied.value.code == 401
        cookies = CookieJar()
        opener = build_opener(HTTPCookieProcessor(cookies))
        response = opener.open(base + "/login", urlencode({"username": "admin", "password": "password", "code": "123456"}).encode())
        assert b"Demo login verified: credentials and 2FA" in response.read()
        assert "HttpOnly" in response.headers["Set-Cookie"]
        assert len(list(cookies)) == 1
        for scaled, dimensions in [(False, (150, 40)), (True, (151, 41))]:
            image = opener.open(base + f"/captcha.png?scaled={int(scaled)}").read()
            assert Image.open(BytesIO(image)).size == dimensions
            result = plugin.recognize_captcha({"method_key": "plugin.demo.gd5", "image": image, "length": 6, "charset": "alnum"})
            assert result["text"] == site.captcha()[1]
            assert b"Captcha verified" in opener.open(base + "/verify", urlencode({"answer": result["text"]}).encode()).read()
        with pytest.raises(HTTPError) as error:
            opener.open(base + "/verify", b"answer=wrong")
        assert error.value.code == 422
        for path in ["/checkin", "/form", "/popup", "/iframe", "/frame", "/captcha?scaled=1"]:
            assert opener.open(base + path).status == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("text", ["", "Incorrect captcha", "Demo checkin successful"])
def test_demo_expected_result(monkeypatch, text):
    monkeypatch.syspath_prepend(str(ROOT / "plugins/runtime"))
    monkeypatch.syspath_prepend(str(ROOT / "proto"))
    plugin = load("demo_plugin")

    class Broker:
        def action(self, op, **kwargs):
            return {"ok": True, "text": text}

    result = plugin.execute_step({"feature_key": "plugin.demo.demo_checkin", "config": {
        "button_selector": ["#sign"], "success_selector": ["#success"],
        "expected_text": "Demo checkin successful",
    }}, Broker())
    assert result["status"] == ("success" if text == "Demo checkin successful" else "fail")
