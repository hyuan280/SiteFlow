from __future__ import annotations

import base64
from io import BytesIO
import json
from pathlib import Path

from PIL import Image, PngImagePlugin
import pytest
from sqlalchemy import select

from app.models.assistant import AssistantMessage
from app.services.assistant import AssistantError, AssistantManager
from app.services.assistant_images import (
    ARTIFACT_IMAGE_NOTICE, MAX_IMAGE_BYTES, artifact_provenance, image_message,
    revoke_browser_images, validate_artifact_image, validate_image,
)
from app.services import assistant_operations
from app.services.assistant_tools import AssistantTools
from app.services.integrations import IntegrationService
from tests.test_assistant_api import (  # noqa: F401
    CONFIG, ROOT, assistant_client, create, provider, send, target, text, tool, wait,
)
from tests.test_assistant_operations import artifact, call, tools  # noqa: F401
from tests.test_assistant_runtime_settings import save


def png(width=100, height=60):
    output = BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("private", "private-metadata-never-export")
    Image.new("RGB", (width, height), "red").save(output, "PNG", pnginfo=metadata)
    return output.getvalue()


def test_image_returns_actual_unmasked_pixels_not_tool_text(tools):
    identifier = artifact(tools, png(2000, 1600), kind="screenshot", path="capture.png")
    result = call(tools, "read_artifact_image", {"artifact_id": identifier})
    assert result["ok"], result
    assert result["provenance"] == {"source": "artifact", "artifact_id": identifier,
                                     "run_id": 1, "job_id": 1, "masked": False}
    image = result.pop("_image")
    assert image["masked"] is False and image["width"] <= 1600 and image["height"] <= 1200
    binary = base64.b64decode(image["image_base64"], validate=True)
    assert len(binary) <= MAX_IMAGE_BYTES and b"private-metadata" not in binary
    with Image.open(BytesIO(binary)) as pixels:
        red, green, blue = pixels.getpixel((0, 0))
        assert red > 200 and green < 20 and blue < 20
    assert "image_base64" not in json.dumps(result)
    with pytest.raises(ValueError):
        validate_image(image)  # browser evidence must still be masked


@pytest.mark.parametrize("binary", [b"not an image", b"<svg></svg>", b"GIF89a", png()[:-25]])
def test_invalid_artifact_image_rejected(binary):
    with pytest.raises(ValueError, match="invalid artifact image"):
        validate_artifact_image(binary)


def test_image_pixel_limit_checked_before_decode(monkeypatch):
    class Oversized:
        width, height = 5000, 5000
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def verify(self):
            raise AssertionError("must reject dimensions first")
    monkeypatch.setattr(Image, "open", lambda *args, **kwargs: Oversized())
    with pytest.raises(ValueError, match="invalid artifact image"):
        validate_artifact_image(b"bounded input")


def test_image_vision_and_kind_checks_precede_file_access(tools):
    identifier = artifact(tools, png(), kind="captcha", path="captcha.png")
    tools.config["supports_vision"] = False
    Path(tools.manager.settings.artifact_root, "captcha.png").unlink()
    assert call(tools, "read_artifact_image", {"artifact_id": identifier})["code"] == "model_vision_required"
    assert call(tools, "read_artifact_log", {"artifact_id": identifier})["code"] == "artifact_kind_mismatch"


def test_browser_revocation_preserves_only_artifact_images():
    image = validate_artifact_image(png())
    historical = image_message(image, '{"source":"artifact"}', artifact=True)
    browser = image_message({**image, "masked": True}, "browser")
    projected = revoke_browser_images([historical, browser])
    assert projected[0] is historical
    assert isinstance(projected[1]["content"], str)
    assert ARTIFACT_IMAGE_NOTICE in historical["content"][0]["text"]
    assert not artifact_provenance({"source": "artifact", "masked": False, "artifact_id": True,
                                    "run_id": 1, "job_id": 1})


@pytest.mark.parametrize("auto_compact", [False, True])
def test_artifact_pixels_encrypted_replayed_independent_of_browser_target(
    assistant_client, provider, monkeypatch, auto_compact,
):
    client = assistant_client
    monkeypatch.setattr(IntegrationService, "vision_runtime", lambda self: {**CONFIG, "supports_vision": True})
    save(client, auto_title=False, auto_compact=auto_compact)
    manager = client.app.state.assistant_manager
    sid = create(client)
    current = AssistantTools(manager, "admin", sid, None)
    Path(manager.settings.artifact_root).mkdir(exist_ok=True)
    from app.models.execution import Run, SiteJob
    with manager.session_factory() as db:
        db.add(Run(id=1, created_by="another-admin", status="completed"))
        db.flush()
        db.add(SiteJob(id=1, run_id=1, site_name="Historical", lane="general", status="success"))
        db.commit()
    identifier = artifact(current, png(), kind="screenshot", path="historical.png")
    # Only integration shims are local to this test; shared dispatch/prompt wiring
    # belongs to the main integration agent, not the operations/image implementation.
    from app.services import assistant as module
    original_definitions, original_dispatch = module.tool_definitions, AssistantTools.dispatch
    monkeypatch.setattr(module, "tool_definitions", lambda: original_definitions() + [
        {**entry, "function": {**entry["function"], "parameters": {
            **entry["function"]["parameters"], "properties": {
                **entry["function"]["parameters"]["properties"], "title": {"type": "string"}},
            "required": [*entry["function"]["parameters"]["required"], "title"]}}}
        for entry in assistant_operations.tool_definitions()
        if entry["function"]["name"] not in {item["function"]["name"] for item in original_definitions()}
    ])
    async def dispatch(self, name, args):
        if name == "read_artifact_image":
            return await assistant_operations.dispatch(self, name, args)
        return await original_dispatch(self, name, args)
    monkeypatch.setattr(AssistantTools, "dispatch", dispatch)
    provider[0].extend([tool("read_artifact_image", {"artifact_id": identifier}), text("Historical pixels reviewed")])
    send(client, sid, content="Read the historical artifact image")
    detail = wait(client, sid)
    assert detail["messages"][-1]["content"] == "Historical pixels reviewed", detail
    outbound = json.loads(provider[1][-1].content)["messages"]
    multimodal = [item for item in outbound if isinstance(item.get("content"), list)]
    assert len(multimodal) == 1
    assert ARTIFACT_IMAGE_NOTICE in multimodal[0]["content"][0]["text"]
    binary = base64.b64decode(multimodal[0]["content"][1]["image_url"]["url"].split(",", 1)[1])
    for item in outbound:
        if item["role"] == "tool":
            assert "image_base64" not in item["content"] and "data:image" not in item["content"]
    assert "image_base64" not in json.dumps(detail) and "data:image" not in json.dumps(detail)
    metadata = json.loads(next(item["content"] for item in detail["messages"]
                               if item.get("tool_name") == "read_artifact_image"))
    response = client.get(metadata["image_url"])
    assert response.status_code == 200 and response.content == binary
    assert response.headers["cache-control"] == "no-store"
    with manager.session_factory() as db:
        row = db.scalar(select(AssistantMessage).where(AssistantMessage.uuid == metadata["image_id"]))
        stored = manager._decrypt(row)
        assert artifact_provenance(stored["_image_provenance"])
        assert b"image_base64" not in row.ciphertext and binary not in row.ciphertext
    fresh = AssistantManager(manager.session_factory, manager.settings, manager.crypto)
    assert fresh.get_image("admin", sid, metadata["image_id"]) == binary
    with pytest.raises(AssistantError):
        fresh.get_image("other", sid, metadata["image_id"])
    # Historical image authority survives an unrelated target and browser-consent
    # change. Browser screenshot authority must not be reused for these pixels.
    target(client, sid, screenshot_consent=False)
    _, replay = manager._history("admin", sid)
    assert any(isinstance(item.get("content"), list) for item in replay)
    provider[0].append(text("History still available"))
    send(client, sid, content="Continue reviewing the historical artifact")
    assert wait(client, sid)["messages"][-1]["content"] == "History still available"
    following = json.loads(provider[1][-1].content)["messages"]
    assert any(isinstance(item.get("content"), list) for item in following)
