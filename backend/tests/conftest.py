from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from tests.helpers import login_client, make_settings


@pytest.fixture
def settings(tmp_path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def anon_client(settings) -> TestClient:
    return TestClient(create_app(settings))


@pytest.fixture
def client(settings) -> TestClient:
    test_client = TestClient(create_app(settings))
    login_client(test_client)
    return test_client
