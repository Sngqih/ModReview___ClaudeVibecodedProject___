from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from modreview.main import create_app
from modreview.settings import AppConfig, ConfigStore, Secrets


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    AppConfig().dump(p)
    return p


@pytest.fixture
def store(config_path: Path) -> ConfigStore:
    return ConfigStore(config_path)


@pytest.fixture
def secrets() -> Secrets:
    # _env_file=None: never read the owner's real .env in tests
    return Secrets(_env_file=None)


@pytest.fixture
def client(store: ConfigStore, secrets: Secrets) -> TestClient:
    return TestClient(create_app(store, secrets))
