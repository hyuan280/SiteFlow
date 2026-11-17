import json

import httpx
import pytest
from pydantic import ValidationError

from app.schemas.model_registry import ModelTestRequest
from app.services import vision
from app.services.model_registry import REGISTRY_KEY
from tests.test_model_registry import CONNECTION, PATH, extra, primary, registry, save


INVALID_URLS = [
    "https://private-endpoint.example/v1?",
    "https://private-endpoint.example/v1#",
    "https://private-endpoint.example/v1?private-query",
    "https://private-endpoint.example/v1#private-fragment",
    "https://private-endpoint.example\\/v1",
    "https://private-endpoint.example/pri\nvate/v1",
    "https://private-endpoint.example/pri\rvate/v1",
    "https://private-endpoint.example/pri\tvate/v1",
    "\x00https://private-endpoint.example/v1",
    "https://private-endpoint.example/v1\n",
    "https://private-endpoint.example/\x7f/v1",
    "https://private-endpoint.example/\x85/v1",
]


@pytest.mark.parametrize("url", INVALID_URLS)
def test_raw_url_and_draft_schema_reject_ambiguous_input(url):
    with pytest.raises(ValueError) as error:
        vision.validate_base_url(url)
    assert "private-" not in str(error.value)
    with pytest.raises(ValidationError) as error:
        ModelTestRequest.model_validate({"expected_revision": 0, "config": {"base_url": url}})
    assert "private-" not in str(error.value)


@pytest.mark.parametrize("url", INVALID_URLS)
def test_draft_request_invalid_url_is_static_before_network(client, monkeypatch, url):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid URL must not call provider")

    monkeypatch.setattr(httpx.AsyncClient, "send", unexpected)
    response = client.request("POST", PATH + "/primary/test",
                              json={"expected_revision": 0, "config": {"base_url": url}})
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "invalid_model_registry"}}


@pytest.mark.parametrize("url", INVALID_URLS)
@pytest.mark.parametrize("mode", ["text", "vision"])
def test_legacy_helpers_reject_raw_url_before_network(monkeypatch, url, mode):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid URL must not call provider")

    monkeypatch.setattr(httpx.Client, "post", unexpected)
    config = {**CONNECTION, "base_url": url}
    with pytest.raises(ValueError) as error:
        if mode == "text":
            vision.test_text(config)
        else:
            vision.recognize(b"image", config, "describe")
    assert "private-" not in str(error.value)


@pytest.mark.parametrize("url", INVALID_URLS)
@pytest.mark.parametrize("target", ["primary", "secondary"])
def test_invalid_stored_url_fails_before_network(client, monkeypatch, url, target):
    chosen = extra()
    assert save(client, [primary(config=CONNECTION), chosen]).status_code == 200
    with client.app.state.session_factory() as session:
        service = registry(client, session)
        if target == "primary":
            service.primary._upsert("vision.base_url", url, "test")
        else:
            chosen["config"]["base_url"] = url
            service.primary._upsert(REGISTRY_KEY, json.dumps({
                "version": 1, "revision": 1, "items": [primary(), chosen],
            }), "test")
        session.commit()

    def unexpected(*args, **kwargs):
        pytest.fail("invalid stored URL must not call provider")

    monkeypatch.setattr(httpx.AsyncClient, "send", unexpected)
    monkeypatch.setattr(httpx.Client, "post", unexpected)
    model_id = "primary" if target == "primary" else chosen["id"]
    for kwargs in ({}, {"json": {"expected_revision": 1, "config": {"model": "draft"}}}):
        response = client.request("POST", f"{PATH}/{model_id}/test", **kwargs)
        if target == "primary":
            assert response.status_code == (422 if kwargs else 200)
            if not kwargs:
                assert not response.json()["ok"]
        else:
            assert response.status_code == 503
        assert "private-" not in response.text
    if target == "primary":
        response = client.request("POST", "/api/v1/integrations/vision/test")
        assert not response.json()["ok"] and "private-" not in response.text


@pytest.mark.parametrize("url", [
    "https://example.com/v1", "http://localhost:1234/api/v1/", "https://[::1]:1234/v1",
    " https://example.com/v1/ ",
])
def test_valid_roots_preserved(url):
    assert vision.validate_base_url(url) == url.strip().rstrip("/")
