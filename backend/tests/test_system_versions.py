import httpx
import pytest

from app.api.v1 import system

VERSION_PATH = "/api/v1/system/version"
COMMIT = "a" * 40


def test_version_endpoint_requires_login(anon_client):
    assert anon_client.get(VERSION_PATH).status_code == 401


@pytest.mark.parametrize("bitwarden_status", [200, 404, 503])
def test_version_endpoint_reports_each_build_independently(
    client, monkeypatch, tmp_path, bitwarden_status,
):
    api_info = tmp_path / "build-info"
    api_info.write_text(f"v1.0-dev\n{COMMIT}\n", encoding="utf-8")
    monkeypatch.setattr(system, "BUILD_INFO_PATH", api_info)
    client.app.state.settings.worker_url = "http://test-worker:6300/"

    def upstream(request):
        assert request.method == "GET"
        assert "authorization" not in request.headers
        assert request.url.path == "/version"
        if request.url.host == "test-worker":
            return httpx.Response(200, json={"version": "v2.0", "commit": "b" * 40})
        assert request.url.host == "bitwarden"
        return httpx.Response(bitwarden_status, json={"version": "v3.0", "commit": "c" * 40})

    async_client = httpx.AsyncClient
    monkeypatch.setattr(
        system.httpx, "AsyncClient",
        lambda **kwargs: async_client(transport=httpx.MockTransport(upstream), **kwargs),
    )
    response = client.get(VERSION_PATH)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "api": {"version": "v1.0-dev", "commit": COMMIT},
        "worker": {"version": "v2.0", "commit": "b" * 40},
        "bitwarden": {
            200: {"version": "v3.0", "commit": "c" * 40},
            404: {"version": None, "commit": None},
            503: None,
        }[bitwarden_status],
    }
