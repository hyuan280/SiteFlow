from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import PLUGIN_ID_PATTERN


class ManifestError(RuntimeError):
    pass


def validate_manifest(manifest: dict[str, Any], expected_id: str | None = None) -> dict[str, Any]:
    plugin_id = manifest.get("id")
    if not isinstance(plugin_id, str) or not PLUGIN_ID_PATTERN.fullmatch(plugin_id):
        raise ManifestError("manifest id is invalid")
    if expected_id is not None and plugin_id != expected_id:
        raise ManifestError("manifest id does not match SITEFLOW_PLUGIN_ID")
    if not isinstance(manifest.get("name"), str) or not manifest["name"].strip():
        raise ManifestError("manifest name is required")
    if not isinstance(manifest.get("version"), str) or not manifest["version"].strip():
        raise ManifestError("manifest version is required")
    if manifest.get("protocol_version") != 1:
        raise ManifestError("unsupported manifest protocol_version")
    capabilities = manifest.get("capabilities", [])
    if not isinstance(capabilities, list) or not all(
        isinstance(item, str) for item in capabilities
    ):
        raise ManifestError("manifest capabilities must be a list of strings")
    return manifest


def load_manifest(path: str, expected_id: str | None = None) -> dict[str, Any]:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"cannot read manifest: {exc}") from exc
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ManifestError("manifest must be a JSON object")
    return validate_manifest(manifest, expected_id)
