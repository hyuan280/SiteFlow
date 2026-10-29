import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from worker.app import LaneManager  # noqa: E402


def test_browser_options_match_manager_profile(monkeypatch, tmp_path):
    calls = []

    class Page:
        async def goto(self, *args, **kwargs):
            pass

        async def evaluate(self, *args):
            pass

        async def wait_for_timeout(self, *args):
            pass

    context = SimpleNamespace(pages=[Page()])

    async def launch(**kwargs):
        calls.append(kwargs)
        return context

    monkeypatch.setitem(sys.modules, "cloakbrowser", SimpleNamespace(launch_persistent_context_async=launch))
    lane = SimpleNamespace(profile=tmp_path, proxy=False, display=":123", label="Test", key="test")
    asyncio.run(LaneManager._start_browser(None, lane))
    options = calls[0]
    assert options["headless"] is False
    assert options["user_data_dir"] == str(tmp_path)
    assert options["viewport"] == {"width": 1920, "height": 947}
    assert options["locale"] == "zh-CN"
    assert options["timezone"] == "Asia/Shanghai"
    assert options["humanize"] is True
    assert options["human_preset"] == "default"
    assert options["env"]["DISPLAY"] == ":123"
    assert "--fingerprint-platform=windows" in options["args"]
    assert "--fingerprint-gpu-vendor=Intel" in options["args"]
    assert "--fingerprint-screen-width=1920" in options["args"]
    assert "--fingerprint-screen-height=1080" in options["args"]
    assert "--use-angle=swiftshader" in options["args"]
    assert not any(arg.startswith("--fingerprint=") for arg in options["args"])
    assert lane.context is context
