import base64
import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi.testclient import TestClient
from sqlalchemy import insert, text

from app.main import create_app
from app.models.workflow import Site
from app.services.cookiecloud import encrypt_payload
from tests.helpers import login_client

SETTINGS = "/api/v1/cookiecloud/servers"


@pytest.mark.parametrize(("algorithm", "expected"), [
    ("aes-128-cbc-fixed", "h6OhKBTCKJikhtsVtF/olv/mog1TTuaGO7/CAh/vhUsIYcgOB95hdjH9ztmhc0rz"),
    ("legacy", "U2FsdGVkX18xMjM0NTY3OC6vHv/vwPUB/r8fs/a36bF0DNON0x3JVjQ6Q3GIAFd6AT0ymel7otdZ2kAkJcacvw=="),
])
def test_cookiecloud_wire_vectors(monkeypatch, algorithm, expected):
    # Fixed vectors from Node's AES-CBC implementation for the CookieCloud wire formats.
    monkeypatch.setattr("app.services.cookiecloud.secrets.token_bytes", lambda _: b"12345678")
    payload = {"cookie_data": {}, "local_storage_data": {}}
    assert encrypt_payload("Test_UUID", "Test password", payload, algorithm) == expected


def store_cookie(client, site, value):
    response = client.put(f"/api/v1/sites/{site['id']}/cookies", json={"cookies": f"session={value}"})
    assert response.status_code == 200, response.text


def add_site(client, name):
    response = client.post("/api/v1/sites", json={"name": name, "base_url": f"https://{name}.example"})
    assert response.status_code == 201, response.text
    return response.json()


def read_cookies(public, server, method="GET"):
    url = f"/cookiecloud/{server['id']}/get/{server['uuid']}"
    response = public.request(method, url, params={"crypto_type": "aes-128-cbc-fixed"})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["crypto_type"] == "aes-128-cbc-fixed"
    key = hashlib.md5(f"{server['uuid']}-{server['password']}".encode()).hexdigest()[:16].encode()
    decryptor = Cipher(algorithms.AES(key), modes.CBC(bytes(16))).decryptor()
    decrypted = decryptor.update(base64.b64decode(response.json()["encrypted"])) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    payload = json.loads(unpadder.update(decrypted) + unpadder.finalize())
    assert payload["local_storage_data"] == {}
    return payload["cookie_data"]


def test_cookiecloud_server_lifecycle_and_site_isolation(client):
    public = TestClient(client.app)
    first_site = add_site(client, "first")
    second_site = add_site(client, "second")
    store_cookie(client, first_site, "first-secret")
    store_cookie(client, second_site, "second-secret")
    first = {
        "id": "z-home_1", "name": "Home", "uuid": "Home-uuid",
        "password": "client-password-1", "site_keys": [first_site["history_key"]],
    }
    second = {
        "id": "a-work", "name": "Work", "uuid": "Work-uuid",
        "password": "client-password-2", "site_keys": [second_site["history_key"]],
    }
    assert public.get(SETTINGS).status_code == 401
    assert client.post(SETTINGS, json=first, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    for server in (first, second):
        response = client.post(SETTINGS, json=server)
        assert response.status_code == 201, response.text
        assert server["password"] not in response.text
        assert server["uuid"] not in response.text
    assert client.post(SETTINGS, json=first).status_code == 409
    invalid = client.post(SETTINGS, json={**first, "id": "UPPER"})
    assert invalid.status_code == 422
    assert first["password"] not in invalid.text
    assert client.post(SETTINGS, json={**first, "id": "missing", "site_keys": [str(uuid4())]}).status_code == 422
    assert [row["id"] for row in client.get(SETTINGS).json()] == [first["id"], second["id"]]
    config_url = f"{SETTINGS}/{first['id']}"
    assert public.get(f"{config_url}/connection").status_code == 401
    connection = client.get(f"{config_url}/connection")
    assert connection.json() == {"uuid": first["uuid"], "password": first["password"]}
    assert connection.headers["cache-control"] == "no-store"
    assert public.get(f"/cookiecloud/{first['id']}/get/{second['uuid']}").status_code == 404
    assert public.post(f"/cookiecloud/{first['id']}/update", json={"uuid": first["uuid"]}).status_code == 404
    cookies = read_cookies(public, first)
    assert set(cookies) == {"first.example"}
    assert cookies["first.example"][0]["value"] == "first-secret"
    assert cookies["first.example"][0]["sameSite"] == "unspecified"
    assert set(read_cookies(public, second, "POST")) == {"second.example"}

    store_cookie(client, first_site, "fresh-cookie")
    assert read_cookies(public, first)["first.example"][0]["value"] == "fresh-cookie"
    raw_update = {
        "name": "Updated", "uuid": "Rotated-uuid",
        "site_keys": [first_site["history_key"], second_site["history_key"]],
    }
    assert client.put(config_url, json=raw_update).status_code == 200
    assert public.get(f"/cookiecloud/{first['id']}/get/{first['uuid']}").status_code == 404
    assert set(read_cookies(public, {**first, **raw_update})) == {"first.example", "second.example"}
    assert client.put(config_url, json={**raw_update, "site_keys": []}).status_code == 200
    assert read_cookies(public, {**first, **raw_update}) == {}

    assert client.delete(f"/api/v1/sites/{second_site['id']}").status_code == 204
    replacement = add_site(client, "second")
    store_cookie(client, replacement, "replacement-cookie")
    assert read_cookies(public, second) == {}
    assert client.get(SETTINGS).json()[1]["site_keys"] == []
    assert client.delete(config_url).status_code == 204
    assert public.get(f"/cookiecloud/{first['id']}/get/{raw_update['uuid']}").status_code == 404


def test_cookiecloud_saves_and_downloads_a_large_site_selection(client):
    public = TestClient(client.app)
    first = add_site(client, "bulk-first")
    last = add_site(client, "bulk-last")
    with client.app.state.session_factory() as session:
        extra_sites = [
            {"history_key": str(uuid4()), "name": f"bulk-{index}", "base_url": f"https://bulk-{index}.example"}
            for index in range(999)
        ]
        session.execute(insert(Site), extra_sites)
        session.commit()

    store_cookie(client, first, "first-cookie")
    store_cookie(client, last, "last-cookie")
    keys = [first["history_key"], *(site["history_key"] for site in extra_sites), last["history_key"]]
    server = {
        "id": "bulk", "name": "Bulk selection", "uuid": "Bulk-uuid",
        "password": "bulk-client-password", "site_keys": keys,
    }
    response = client.post(SETTINGS, json=server)
    assert response.status_code == 201, response.text
    assert response.json()["site_keys"] == keys
    assert client.get(SETTINGS).json()[0]["site_keys"] == keys

    new_site = add_site(client, "created-after-select-all")
    store_cookie(client, new_site, "not-shared")
    cookies = read_cookies(public, server)
    assert set(cookies) == {"bulk-first.example", "bulk-last.example"}
    assert cookies["bulk-last.example"][0]["value"] == "last-cookie"

    update = {"name": server["name"], "uuid": server["uuid"], "site_keys": []}
    assert client.put(f"{SETTINGS}/{server['id']}", json=update).json()["site_keys"] == []
    assert read_cookies(public, server) == {}


def test_cookiecloud_preserves_exact_cookie_domain_and_omits_expired_cookies(client, monkeypatch):
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr("app.services.cookiecloud.utcnow", lambda: now)
    site = add_site(client, "www")
    cookie_rows = [
        {"name": "session", "value": "synthetic-session", "domain": "www.example"},
        {
            "name": "cf_clearance", "value": "synthetic-clearance", "domain": ".example",
            "expirationDate": now.timestamp() + 60,
        },
        {
            "name": "cf_clearance", "value": "expired", "domain": "www.example",
            "expirationDate": now.timestamp() - 60,
        },
    ]
    saved = client.put(
        f"/api/v1/sites/{site['id']}/cookies", json={"cookies": json.dumps(cookie_rows)},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["cookie_count"] == 3

    server = {
        "id": "domain-groups", "name": "Domain groups", "uuid": "Domain-uuid",
        "password": "domain-client-password", "site_keys": [site["history_key"]],
    }
    created = client.post(SETTINGS, json=server)
    assert created.status_code == 201, created.text

    cookies = read_cookies(TestClient(client.app), server)
    assert set(cookies) == {"www.example", ".example"}
    assert [cookie["name"] for cookie in cookies["www.example"]] == ["session"]
    [clearance] = cookies[".example"]
    assert clearance["name"] == "cf_clearance"
    assert clearance["value"] == "synthetic-clearance"
    assert clearance["domain"] == ".example"
    assert clearance["hostOnly"] is False
    assert clearance["expirationDate"] == now.timestamp() + 60


def test_cookiecloud_all_sites_includes_future_sites_until_disabled(client):
    public = TestClient(client.app)
    first = add_site(client, "current")
    store_cookie(client, first, "current-cookie")
    server = {
        "id": "automatic", "name": "All sites", "uuid": "Auto-uuid",
        "password": "auto-client-password", "site_keys": [], "all_sites": True,
    }
    created = client.post(SETTINGS, json=server)
    assert created.status_code == 201, created.text
    assert created.json()["all_sites"] is True
    assert set(read_cookies(public, server)) == {"current.example"}

    future = add_site(client, "future")
    assert set(read_cookies(public, server)) == {"current.example"}
    store_cookie(client, future, "future-cookie")
    assert set(read_cookies(public, server)) == {"current.example", "future.example"}
    assert client.get(SETTINGS).json()[0]["all_sites"] is True

    selection = {
        "name": server["name"], "uuid": server["uuid"],
        "site_keys": [first["history_key"]], "all_sites": False,
    }
    updated = client.put(f"{SETTINGS}/{server['id']}", json=selection)
    assert updated.status_code == 200, updated.text
    assert updated.json()["all_sites"] is False
    assert set(read_cookies(public, server)) == {"current.example"}


def test_cookiecloud_existing_rows_migrate_with_manual_selection(client, settings):
    site = add_site(client, "legacy")
    store_cookie(client, site, "legacy-cookie")
    server = {
        "id": "legacy", "name": "Legacy", "uuid": "Legacy-uuid",
        "password": "legacy-client-password", "site_keys": [site["history_key"]],
    }
    assert client.post(SETTINGS, json=server).status_code == 201
    with client.app.state.engine.begin() as connection:
        connection.execute(text("ALTER TABLE cookiecloud_servers DROP COLUMN all_sites"))

    app = create_app(settings)
    upgraded = TestClient(app)
    login_client(upgraded)
    row = upgraded.get(SETTINGS).json()[0]
    assert row["all_sites"] is False
    assert row["site_keys"] == server["site_keys"]
    assert set(read_cookies(TestClient(app), server)) == {"legacy.example"}


def test_cookiecloud_endpoint_base_uses_existing_domain(client, settings):
    url = f"{SETTINGS}/endpoint-base"
    assert client.get(url).json() == {"base_url": ""}
    settings.siteflow_domain = " https://siteflow.example.com:8443/ "
    response = client.get(url)
    assert response.json() == {"base_url": "https://siteflow.example.com:8443"}


def test_pycookiecloud_preflight_and_default_download(client):
    public = TestClient(client.app)
    server = {
        "id": "python-client", "name": "Python client", "uuid": "Python-uuid",
        "password": "python-client-password", "site_keys": [],
    }
    base = f"/cookiecloud/{server['id']}"
    assert public.get(base).status_code == 404
    assert client.post(SETTINGS, json=server).status_code == 201

    for url in (base, f"{base}/"):
        preflight = public.get(url, follow_redirects=False)
        assert preflight.status_code == 200
        assert preflight.json() == {"status": "OK"}
        assert preflight.headers["cache-control"] == "no-store"

    download = public.get(f"{base}/get/{server['uuid']}")
    assert download.status_code == 200
    assert download.json()["crypto_type"] == "legacy"
    assert base64.b64decode(download.json()["encrypted"]).startswith(b"Salted__")
    assert client.delete(f"{SETTINGS}/{server['id']}").status_code == 204
    assert public.get(base).status_code == 404
