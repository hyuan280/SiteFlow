import io
import struct
import zlib

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.models.integration import IntegrationConfig
from app.services.integrations import IntegrationService, IntegrationValidationError
from app.services import vision


PATH = "/api/v1/integrations/vision"
CONNECTION = {
    "base_url": "https://example.com/v1",
    "model": "vision-model",
    "api_key": "private-provider-key",
}


def service_for(client, session):
    return IntegrationService(session, client.app.state.settings, client.app.state.crypto)


def test_encrypted_cap_roundtrip_omitted_and_reset(client):
    view = client.get("/api/v1/integrations").json()["vision"]
    assert view["max_tokens"] == {
        "value": None, "source": "unset", "editable": True, "error": None,
    }
    response = client.put(PATH, json={**CONNECTION, "max_tokens": 8193})
    assert response.status_code == 200
    assert CONNECTION["api_key"] not in response.text
    assert response.json()["vision"]["max_tokens"] == {
        "value": 8193, "source": "db", "editable": True, "error": None,
    }
    with client.app.state.session_factory() as session:
        service = service_for(client, session)
        row = session.get(IntegrationConfig, "vision.max_tokens")
        assert row.is_secret
        assert b"8193" not in row.value_ciphertext
        assert service._decrypt_row("vision.max_tokens", row) == "8193"
        assert service.vision_runtime() == {
            **CONNECTION, "supports_vision": True, "timeout": 30, "max_tokens": 8193,
        }
    for payload in ({}, {"model": "vision-model"}, {"base_url": None}):
        assert client.put(PATH, json=payload).json()["vision"]["max_tokens"]["value"] == 8193
    assert client.delete(PATH).json()["vision"]["max_tokens"]["value"] is None
    with client.app.state.session_factory() as session:
        assert session.get(IntegrationConfig, "vision.max_tokens") is None


@pytest.mark.parametrize("value", [None, "", "  ", 0])
def test_clear_cap(client, value):
    assert client.put(PATH, json={**CONNECTION, "max_tokens": 2048}).status_code == 200
    response = client.put(PATH, json={"max_tokens": value})
    assert response.status_code == 200
    assert response.json()["vision"]["max_tokens"] == {
        "value": 0, "source": "db", "editable": True, "error": None,
    }
    with client.app.state.session_factory() as session:
        service = service_for(client, session)
        assert service.resolve_vision("max_tokens").value == "0"
        assert service.vision_runtime() == {
            **CONNECTION, "supports_vision": True, "timeout": 30,
        }


@pytest.mark.parametrize("value", [True, False, -1, 1.5, 1.0, "2048", "secret-invalid"])
def test_invalid_cap_api_and_service(client, value):
    assert client.put(PATH, json={"max_tokens": 73}).status_code == 200
    assert client.put(PATH, json={"max_tokens": value}).status_code == 422
    with client.app.state.session_factory() as session:
        service = service_for(client, session)
        with pytest.raises(IntegrationValidationError, match="nonnegative integer") as error:
            service.update_vision(max_tokens=value)
        assert "secret-invalid" not in str(error.value)
        assert service.resolve_vision("max_tokens").value == "73"


@pytest.mark.parametrize("cap", [0, 9876])
def test_environment_precedence_and_lock(client, monkeypatch, cap):
    assert client.put(PATH, json={**CONNECTION, "max_tokens": 456}).status_code == 200
    monkeypatch.setenv("SITEFLOW_MODEL_MAX_TOKENS", str(cap))
    settings = Settings(_env_file=None)
    assert settings.vision_max_tokens == cap
    client.app.state.settings.vision_max_tokens = settings.vision_max_tokens
    view = client.get("/api/v1/integrations").json()["vision"]
    assert view["max_tokens"] == {
        "value": cap, "source": "env", "editable": False, "error": None,
    }
    assert client.put(PATH, json={"model": "vision-model", "api_key": None}).status_code == 409
    for value in (None, "", 0, 123):
        assert client.put(PATH, json={"max_tokens": value}).status_code == 409
    assert client.delete(PATH).status_code == 409
    with client.app.state.session_factory() as session:
        service = service_for(client, session)
        assert service.resolve_vision("max_tokens").value == str(cap)
        runtime = service.vision_runtime()
        assert runtime == {
            **CONNECTION, "supports_vision": True, "timeout": 30,
            **({"max_tokens": cap} if cap else {}),
        }
        assert service._decrypt_row("vision.max_tokens", service._row("vision.max_tokens")) == "456"


@pytest.mark.parametrize("value", [True, False, -1, 1.5, 1.0, "1.5", "1.0", "true", "-1"])
def test_settings_reject_invalid_cap(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, vision_max_tokens=value)


@pytest.mark.parametrize("value", ["-1", "1.5", "1.0", "true"])
def test_environment_rejects_invalid_cap(monkeypatch, value):
    monkeypatch.setenv("SITEFLOW_MODEL_MAX_TOKENS", value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize("value", ["", " ", "\t"])
def test_blank_environment_explicitly_disables_cap(client, monkeypatch, value):
    assert client.put(PATH, json={**CONNECTION, "max_tokens": 8192}).status_code == 200
    monkeypatch.setenv("SITEFLOW_MODEL_MAX_TOKENS", value)
    settings = Settings(_env_file=None)
    assert settings.vision_max_tokens == 0
    client.app.state.settings.vision_max_tokens = settings.vision_max_tokens
    view = client.get("/api/v1/integrations").json()["vision"]
    assert view["max_tokens"] == {"value": 0, "source": "env", "editable": False, "error": None}
    with client.app.state.session_factory() as session:
        assert "max_tokens" not in service_for(client, session).vision_runtime()


@pytest.mark.parametrize("stored", ["-1", "true", "1.5", "", "private-provider-key", "corrupt-ciphertext"])
def test_invalid_stored_cap_fails_closed_and_can_be_cleared(client, monkeypatch, stored):
    assert client.put(PATH, json=CONNECTION).status_code == 200
    with client.app.state.session_factory() as session:
        service = service_for(client, session)
        service._upsert("vision.max_tokens", stored, "test")
        session.commit()
        if stored == "corrupt-ciphertext":
            service._row("vision.max_tokens").value_ciphertext = b"corrupt"
            session.commit()
        assert service.vision_runtime() is None
    response = client.get("/api/v1/integrations")
    assert response.status_code == 200
    view = response.json()["vision"]
    assert view["available"] is False
    assert view["max_tokens"]["value"] is None
    assert view["max_tokens"]["source"] == "db"
    assert view["max_tokens"]["editable"] is True
    assert view["max_tokens"]["error"]
    assert CONNECTION["api_key"] not in response.text

    def unexpected(*args, **kwargs):
        pytest.fail("invalid cap must prevent provider requests")

    monkeypatch.setattr(httpx.Client, "post", unexpected)
    response = client.request("POST", PATH + "/test")
    assert response.json()["ok"] is False
    assert CONNECTION["api_key"] not in response.text
    assert client.put(PATH, json={"max_tokens": None}).json()["vision"]["available"]


@pytest.mark.parametrize("cap", ["absent", None, 0, 1, 8193])
@pytest.mark.parametrize("supports_vision", [False, True])
def test_api_selftest_exact_provider_payload(client, monkeypatch, cap, supports_vision):
    payload = {**CONNECTION, "supports_vision": supports_vision}
    if cap != "absent":
        payload["max_tokens"] = cap
    assert client.put(PATH, json=payload).status_code == 200
    calls = []

    def post(self, url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "OK"}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.Client, "post", post)
    response = client.request("POST", PATH + "/test")
    assert response.json()["ok"] is True
    assert CONNECTION["api_key"] not in response.text
    assert len(calls) == 1
    url, kwargs = calls[0]
    assert url == CONNECTION["base_url"] + "/chat/completions"
    assert kwargs["headers"] == {"Authorization": f"Bearer {CONNECTION['api_key']}"}
    body = kwargs["json"]
    content = body["messages"][0]["content"]
    if supports_vision:
        assert content[0] == {"type": "text", "text": "Describe this image in a few words."}
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    else:
        assert content == "Reply with the single word OK."
    expected = {"model": CONNECTION["model"], "messages": [{"role": "user", "content": content}]}
    if type(cap) is int and cap > 0:
        expected["max_tokens"] = cap
    assert body == expected


@pytest.mark.parametrize("cap", ["absent", None, 0, 8193])
@pytest.mark.parametrize("mode", ["text", "image"])
def test_direct_provider_token_kwargs(monkeypatch, cap, mode):
    config = dict(CONNECTION)
    if cap != "absent":
        config["max_tokens"] = cap
    bodies = []

    def post(self, url, **kwargs):
        bodies.append(kwargs["json"])
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "OK"}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.Client, "post", post)
    result = vision.test_text(config) if mode == "text" else vision.recognize(b"image", config, "read")
    assert result == "OK"
    assert len(bodies) == 1
    if cap == 8193:
        assert bodies[0]["max_tokens"] == 8193
    else:
        assert "max_tokens" not in bodies[0]


@pytest.mark.parametrize("cap", [True, False, -1, 1.5, 1.0, "8193", "private-provider-key"])
@pytest.mark.parametrize("mode", ["text", "image"])
def test_invalid_direct_config_never_calls_provider(monkeypatch, cap, mode):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid cap must prevent provider requests")

    monkeypatch.setattr(httpx.Client, "post", unexpected)
    config = {**CONNECTION, "max_tokens": cap}
    with pytest.raises(ValueError, match="Vision provider") as error:
        if mode == "text":
            vision.test_text(config)
        else:
            vision.recognize(b"image", config, "read")
    assert CONNECTION["api_key"] not in str(error.value)


@pytest.mark.parametrize("supports_vision", [False, True])
def test_capped_selftest_sanitizes_provider_errors(client, monkeypatch, supports_vision):
    assert client.put(PATH, json={
        **CONNECTION, "max_tokens": 8193, "supports_vision": supports_vision,
    }).status_code == 200

    def fail(*args, **kwargs):
        raise RuntimeError(CONNECTION["api_key"] + " private provider response")

    monkeypatch.setattr(httpx.Client, "post", fail)
    response = client.request("POST", PATH + "/test")
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert CONNECTION["api_key"] not in response.text
    assert "private provider response" not in response.text


def test_selftest_png_crc_and_pixels(client, monkeypatch):
    assert client.put(PATH, json=CONNECTION).status_code == 200
    images = []
    monkeypatch.setattr(vision, "recognize", lambda image, *args: images.append(image) or "OK")
    assert client.request("POST", PATH + "/test").json()["ok"]
    image, = images
    assert image[:8] == b"\x89PNG\r\n\x1a\n"
    offset = 8
    chunks = []
    compressed = b""
    while offset < len(image):
        length, = struct.unpack("!I", image[offset:offset + 4])
        kind = image[offset + 4:offset + 8]
        data = image[offset + 8:offset + 8 + length]
        crc, = struct.unpack("!I", image[offset + 8 + length:offset + 12 + length])
        assert crc == zlib.crc32(kind + data) & 0xFFFFFFFF
        chunks.append(kind)
        if kind == b"IHDR":
            assert struct.unpack("!2I5B", data) == (1, 1, 8, 2, 0, 0, 0)
        if kind == b"IDAT":
            compressed += data
        offset += length + 12
    assert chunks == [b"IHDR", b"IDAT", b"IEND"]
    assert offset == len(image)
    assert zlib.decompress(compressed) == b"\x00\xff\xff\xff"
    pil = pytest.importorskip("PIL.Image")
    with pil.open(io.BytesIO(image)) as decoded:
        decoded.verify()
    with pil.open(io.BytesIO(image)) as decoded:
        assert decoded.getpixel((0, 0)) == (255, 255, 255)
