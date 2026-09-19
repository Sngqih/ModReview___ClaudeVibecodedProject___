from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from modreview.cache import ReviewCache
from modreview.main import create_app
from modreview.settings import AppConfig, ConfigStore, Secrets
from tests.helpers import load_json


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
def cache(tmp_path: Path) -> ReviewCache:
    return ReviewCache(tmp_path / "test.db")


@pytest.fixture
def runner_calls() -> list[dict]:
    return []


@pytest.fixture
def client(store, secrets, cache, runner_calls) -> TestClient:
    from tests.helpers import fake_runner

    async def module_loader() -> list[dict]:
        return load_json("nusmods_moduleList_sample.json")

    app = create_app(
        store, secrets, cache=cache, module_loader=module_loader, runner=fake_runner(runner_calls)
    )
    return TestClient(app)
