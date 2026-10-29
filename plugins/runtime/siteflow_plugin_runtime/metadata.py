from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .manifest import ManifestError


def load_metadata(path: str | None, key: str) -> list[dict[str, Any]]:
    if not path:
        return []
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot load plugin metadata {path}: {exc}") from exc
    if isinstance(raw, dict):
        raw = raw.get(key, [])
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise ManifestError(f"plugin metadata {path} must contain a list of objects")
    return raw
