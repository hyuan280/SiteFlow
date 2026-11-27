"""Keep downloaded browser caches separate from legacy image-bundled binaries."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
KEYLESS = "cloakhq/cloakbrowser-manager@sha256:04b82e42fe74ec34352580da47b096695e6a8d08745f88e3f5fd821be5ebf989"
LICENSED = "cloakhq/cloakbrowser-manager@sha256:d3d9b86cfd94347f7356811cf8222c8bc48152725b053dc6ce2e68f541e00c14"
BINARY = "/root/.cloakbrowser/chromium-146.0.7680.177.5/chrome"


@pytest.mark.parametrize("manifest", ["docker-compose.yml", "ops/docker-compose.yml"])
@pytest.mark.parametrize("licensed_override", [False, True])
def test_cache_mount_does_not_hide_bundled_browser(manifest, licensed_override):
    if shutil.which("docker") is None:
        pytest.skip("requires Docker Compose CLI, not a Docker daemon")
    env = {
        "PATH": os.environ["PATH"], "HOME": "/tmp",
        "SITEFLOW_BROWSER_TOKEN": "isolated-compose-test-token",
    }
    if licensed_override:
        env.update(SITEFLOW_BROWSER_IMAGE=LICENSED, CLOAKBROWSER_BINARY_PATH="")
    result = subprocess.run([
        "docker", "compose", "--env-file", str(ROOT / ".env.example"),
        "-f", str(ROOT / manifest), "config", "--no-env-resolution", "--format", "json",
    ], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, "isolated Compose configuration did not validate"
    config = json.loads(result.stdout)
    browser = config["services"]["browser"]
    assert browser["environment"]["CLOAKBROWSER_CACHE_DIR"] == "/var/cache/cloakbrowser"
    assert browser["environment"]["CLOAKBROWSER_AUTO_UPDATE"] == "false"
    assert browser["image"] == (LICENSED if licensed_override else KEYLESS)
    assert browser["environment"]["CLOAKBROWSER_BINARY_PATH"] == ("" if licensed_override else BINARY)
    mounts = browser["volumes"]
    assert any(mount["source"] == "browser_cache" and mount["target"] == "/var/cache/cloakbrowser"
               for mount in mounts)
    bundled = Path(BINARY)
    assert all(Path(mount["target"]) not in bundled.parents and Path(mount["target"]) != bundled
               for mount in mounts)
    assert config["volumes"]["browser_cache"]["name"] == "siteflow_browser_cache"
