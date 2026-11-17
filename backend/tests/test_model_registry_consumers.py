from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import threading
from types import SimpleNamespace
from uuid import uuid4

from fastapi import Request
import httpx
import pytest

from app.api.v1 import config_ai
from app.features.catalog import FEATURES
from app.models.assistant import AssistantSession
from app.services.assistant import AssistantError
from app.services.assistant_operations import _known_keys
from app.services.feature_settings import FeatureSettingsService
from app.services.integrations import IntegrationService, IntegrationValidationError
from app.services.model_registry import ModelRegistryService, REGISTRY_KEY
from app.services.notifications import NotificationService
from tests.test_assistant_api import (  # noqa: F401
    ROOT, assistant_client as mocked_assistant_client, create, provider, send, text, tool, wait,
)
from tests.test_assistant_execution import (  # noqa: F401
    arguments, execution, response, run,
)
from tests.test_assistant_operations import (  # noqa: F401
    artifact, call, tools,
)
from tests.test_config_ai_generate_api import PROPOSAL, URL, _payload


MODEL_RUNTIME = IntegrationService.model_runtime


@pytest.fixture
def assistant_client(mocked_assistant_client, monkeypatch):
    # Registry consumer tests must exercise real selection, not the API fixture's runtime mock.
    monkeypatch.setattr(IntegrationService, "model_runtime", MODEL_RUNTIME)
    return mocked_assistant_client


def model(label, *, usages=None, enabled=True, vision=False):
    return {
        "id": str(uuid4()), "name": label, "enabled": enabled,
        "usages": usages if usages is not None else ["assistant", "feature_cards"],
        "config": {
            "base_url": f"https://{label}.example/v1", "model": f"private-{label}-model",
            "api_key": f"opaque-{label}-credential", "supports_vision": vision,
        },
    }


def save(manager, items, revision=0):
    with manager.session_factory() as db:
        registry = ModelRegistryService(db, manager.settings, manager.crypto)
        registry.update({"expected_revision": revision, "items": [*items, {
            "id": "primary", "name": "Primary", "enabled": False, "usages": [],
        }]}, "test")


def corrupt(manager):
    with manager.session_factory() as db:
        IntegrationService(db, manager.settings, manager.crypto)._upsert(
            REGISTRY_KEY, "private-corrupt-registry", "test")
        db.commit()


def assert_private_absent(value, items):
    encoded = json.dumps(value)
    for item in items:
        for field in ("api_key", "base_url", "model"):
            assert item["config"][field] not in encoded


def test_assistant_and_image_features_select_independently(execution):
    manager = execution.manager
    disabled = model("disabled", enabled=False, vision=True)
    reserved = model("reserved", usages=["mcp"], vision=True)
    image = model("image", usages=["feature_cards"], vision=True)
    chat = model("chat", usages=["assistant"])
    items = [disabled, reserved, image, chat]
    save(manager, items)
    runtime = manager._runtime()
    assert runtime["model"] == chat["config"]["model"]
    assert runtime["supports_vision"] is False
    private, secrets = execution._private_runtime(arguments("sign.image_quiz")["step"])
    assert set(private) == {"vision"}
    assert private["vision"]["api_key"] == image["config"]["api_key"]
    assert set(private["vision"]) == {
        "base_url", "api_key", "model", "timeout", "supports_vision", "execution_settings",
    }
    assert image["config"]["api_key"] in secrets


@pytest.mark.parametrize("feature,config", [
    ("sign.image_quiz", {}), ("challenge.grid_captcha", {}),
    ("challenge.image_captcha", {"captcha_method": "vision"}),
    ("sign.iframe.image_captcha", {"captcha_method": "vision"}),
])
def test_actual_image_paths_skip_text_feature_model(execution, feature, config):
    text_model, image = model("text"), model("image", vision=True)
    save(execution.manager, [text_model, image])
    private, _ = execution._private_runtime(arguments(feature, config)["step"])
    assert private["vision"]["model"] == image["config"]["model"]


def test_incomplete_matching_model_is_skipped(execution):
    partial, chat = model("partial"), model("chat")
    save(execution.manager, [partial, chat])
    partial["config"] = {"model": "incomplete"}
    with execution.manager.session_factory() as db:
        registry = ModelRegistryService(db, execution.manager.settings, execution.manager.crypto)
        doc, _ = registry._load()
        raw = doc.model_dump(exclude_unset=True)
        raw["items"][0] = partial
        registry.primary._upsert(REGISTRY_KEY, json.dumps(raw), "test")
        db.commit()
    assert execution.manager._runtime()["model"] == chat["config"]["model"]


def test_image_feature_without_vision_model_is_controlled(execution):
    save(execution.manager, [model("text")])
    assert run(execution, arguments("sign.image_quiz")) == {
        "ok": False, "code": "provider_unavailable",
    }
    execution.manager._worker.assert_not_awaited()


@pytest.mark.parametrize("use_vision", [False, True])
def test_flow_result_routes_by_host_vision_setting(execution, use_vision):
    text_model = model("text", usages=["feature_cards"])
    image = model("image", usages=["feature_cards"], vision=True)
    save(execution.manager, [text_model, image])
    with execution.manager.session_factory() as db:
        FeatureSettingsService(db).update(FEATURES["flow.result"], {
            "llm_enabled": True, "llm_prompt": "host-private-prompt",
        }, [], {"llm_enabled": True, "judgment_scope": "all", "use_vision": use_vision,
                "model": "upstream-model-override"})
    step = execution._normalize(arguments("flow.result", {"output_value": False})["step"])
    private, secrets = execution._private_runtime(step)
    expected = image if use_vision else text_model
    assert private["vision"]["api_key"] == expected["config"]["api_key"]
    behavior = private["vision"]["execution_settings"]
    assert behavior["use_vision"] is use_vision
    assert behavior["model"] == "upstream-model-override"
    assert "upstream-model-override" in secrets


@pytest.mark.parametrize("broken", [False, True])
def test_unavailable_registry_consumers_return_controlled_errors(execution, broken):
    items = [model("reserved", usages=["mcp"], vision=True), model("disabled", enabled=False)]
    save(execution.manager, items)
    if broken:
        corrupt(execution.manager)
    with pytest.raises(AssistantError) as caught:
        execution.manager._runtime()
    assert caught.value.code == "model_unavailable" and caught.value.status == 503
    result = run(execution, arguments("sign.image_quiz"))
    assert result == {"ok": False, "code": "provider_unavailable"}
    execution.manager._worker.assert_not_awaited()
    assert_private_absent(result, items)


def test_text_assistant_does_not_auto_switch_for_artifact_image(assistant_client, provider):
    client = assistant_client
    chat, image = model("chat", usages=["assistant"]), model("image", vision=True)
    save(client.app.state.assistant_manager, [chat, image])
    identifier = create(client)
    # The existing image tool gate uses the frozen assistant runtime, not the registry.
    manager = client.app.state.assistant_manager
    assert manager._runtime()["supports_vision"] is False
    provider[0].extend([tool("read_artifact_image", {"artifact_id": 1}), text("No image available")])
    send(client, identifier)
    detail = wait(client, identifier)
    assert detail["messages"][-1]["content"] == "No image available"
    assert all(json.loads(request.content)["model"] == chat["config"]["model"]
               for request in provider[1])
    assert_private_absent(detail, [chat, image])


def test_actual_image_read_keeps_text_assistant_runtime(tools):
    tools.manager.settings.config_ai_enabled = True
    chat, image = model("chat", usages=["assistant"]), model("image", vision=True)
    save(tools.manager, [chat, image])
    tools.config = tools.manager._runtime()
    identifier = artifact(tools, b"unused pixels", kind="screenshot")
    assert call(tools, "read_artifact_image", {"artifact_id": identifier}) == {
        "ok": False, "code": "model_vision_required",
    }
    assert tools.config["model"] == chat["config"]["model"]


def test_no_eligible_assistant_fails_before_message_or_provider(assistant_client, provider):
    client = assistant_client
    save(client.app.state.assistant_manager, [model("image", usages=["feature_cards"], vision=True)])
    identifier = create(client)
    before = client.get(f"{ROOT}/{identifier}").json()
    assert send(client, identifier, status=503).json() == {
        "detail": {"code": "model_unavailable"},
    }
    assert client.get(f"{ROOT}/{identifier}").json() == before
    assert not provider[1]


def test_turn_runtime_frozen_across_registry_reorder_summary_and_title(
    assistant_client, provider,
):
    client = assistant_client
    manager = client.app.state.assistant_manager
    first, second = model("first"), model("second")
    save(manager, [first, second])
    identifier = create(client)
    with manager.session_factory() as db:
        for index in range(48):
            manager._message(db, identifier, "assistant", f"Historical response {index}")
        db.commit()
    started, release = threading.Event(), threading.Event()

    async def blocked(request):
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.005)
        return tool("list_features")

    provider[0].extend([blocked, text("First complete")])
    client.app.state.title_provider[0].append(text('{"title":"Generated title"}'))
    send(client, identifier)
    assert started.wait(3)
    try:
        save(manager, [second, first], revision=1)
    finally:
        release.set()
    detail = wait(client, identifier)
    assert detail["title"] == "Generated title"
    summaries = client.app.state.summary_provider[1]
    titles = client.app.state.title_provider[1]
    assert summaries and titles and len(provider[1]) == 2
    for request in provider[1] + summaries + titles:
        assert json.loads(request.content)["model"] == first["config"]["model"]
        assert request.headers["authorization"] == "Bearer " + first["config"]["api_key"]
    provider[0].append(text("Second complete"))
    send(client, identifier)
    wait(client, identifier)
    assert json.loads(provider[1][-1].content)["model"] == second["config"]["model"]
    assert_private_absent(detail, [first, second])


def test_selected_provider_400_does_not_retry_another_model(assistant_client, provider):
    client = assistant_client
    first, second = model("first"), model("second", vision=True)
    save(client.app.state.assistant_manager, [first, second])
    provider[0].append(httpx.Response(400, text=json.dumps(first["config"])))
    identifier = create(client)
    send(client, identifier)
    detail = wait(client, identifier)
    assert "provider_http" in detail["messages"][-1]["content"]
    assert len(provider[1]) == 1
    assert json.loads(provider[1][0].content)["model"] == first["config"]["model"]
    assert_private_absent(detail, [first, second])


@pytest.mark.parametrize("unavailable", [None, "no_match", "corrupt"])
def test_standalone_generation_uses_assistant_usage(client, monkeypatch, unavailable):
    manager = client.app.state.assistant_manager
    manager.settings.config_ai_enabled = True
    image = model("image", usages=["feature_cards"], vision=True)
    chat = model("chat", usages=["assistant"])
    save(manager, [image, chat] if unavailable != "no_match" else [image])
    if unavailable == "corrupt":
        corrupt(manager)
    captured = []

    async def generate(service, payload, config):
        captured.append(config)
        return deepcopy(PROPOSAL)

    async def connected(request):
        return False

    monkeypatch.setattr(config_ai, "generate_site_draft", generate)
    monkeypatch.setattr(Request, "is_disconnected", connected)
    result = client.post(URL, json=_payload())
    if unavailable:
        assert result.status_code == 503
        assert result.json() == {"detail": {"code": "model_unavailable"}}
        assert captured == []
    else:
        assert result.status_code == 200, result.text
        assert captured[0]["model"] == chat["config"]["model"]
        assert captured[0]["supports_vision"] is False
    assert_private_absent(result.json(), [image, chat])


@pytest.mark.parametrize("field", ["api_key", "base_url", "model"])
def test_private_feature_provider_values_never_return(execution, field):
    image = model("image", usages=["feature_cards"], vision=True)
    save(execution.manager, [image])

    async def worker(path, payload):
        assert payload["private_runtime"]["vision"]["api_key"] == image["config"]["api_key"]
        raw = response(execution.manager.browsers[execution.session_id], payload["operation_id"])
        raw["logs"] = [image["config"][field]]
        return raw

    execution.manager._worker.side_effect = worker
    result = run(execution, arguments("sign.image_quiz"))
    assert result == {"ok": False, "code": "secret_output"}
    with execution.manager.session_factory() as db:
        data = execution.manager._decrypt(db.get(AssistantSession, execution.session_id))
    assert_private_absent([result, data], [image])


def test_all_registry_keys_redact_historical_logs_and_notifications(tools):
    entries = [model("selected"), model("disabled", enabled=False),
               model("reserved", usages=["mcp"])]
    save(tools.manager, entries)
    primary_key = "opaque-disabled-primary-credential"
    with tools.manager.session_factory() as db:
        IntegrationService(db, tools.manager.settings, tools.manager.crypto)._upsert(
            "vision.api_key", primary_key, "test")
        db.commit()
    raw = "Useful diagnostic " + " ".join(item["config"]["api_key"] for item in entries) + " " + primary_key
    identifier = artifact(tools, raw.encode())
    result = call(tools, "read_artifact_log", {"artifact_id": identifier})
    assert result["ok"] and "Useful diagnostic" in result["content"]
    event = {"sites": [{"message": raw}]}
    with tools.manager.session_factory() as db:
        known = _known_keys(db, tools)
        assert all(item["config"]["api_key"] in known for item in entries)
        assert primary_key in known
        notifier = SimpleNamespace(session=db, settings=tools.manager.settings, crypto=tools.manager.crypto)
        NotificationService._sanitize_messages(notifier, event)
    for item in entries:
        assert item["config"]["api_key"] not in json.dumps([result, event])
    assert primary_key not in json.dumps([result, event])
    assert "[redacted]" in event["sites"][0]["message"]


def test_corrupt_registry_fails_closed_for_logs_and_notifications(tools):
    identifier = artifact(tools, b"unavailable-key-must-not-escape")
    corrupt(tools.manager)
    result = call(tools, "read_artifact_log", {"artifact_id": identifier})
    assert result == {"ok": False, "code": "data_unavailable"}
    event = {"sites": [{"message": "unavailable-key-must-not-escape"}]}
    with tools.manager.session_factory() as db:
        with pytest.raises(IntegrationValidationError):
            _known_keys(db, tools)
        notifier = SimpleNamespace(session=db, settings=tools.manager.settings, crypto=tools.manager.crypto)
        NotificationService._sanitize_messages(notifier, event)
    assert event["sites"][0]["message"] == "[redacted]"
