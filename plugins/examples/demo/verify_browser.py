"""Verify one imported site, 19 steps, remote secret login and negative failures.

Uses only the disposable import/API/worker/gRPC/browser test environment.
"""
import os
from pathlib import Path
import subprocess


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[3]
    raise SystemExit(subprocess.call(
        [str(root / ".venv/bin/pytest"), "-q", "-s", "tests/test_demo_e2e.py"],
        cwd=root / "backend", env={**os.environ, "SITEFLOW_DEMO_E2E": "1"},
    ))
