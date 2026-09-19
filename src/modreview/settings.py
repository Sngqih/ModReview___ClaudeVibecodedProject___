"""Configuration.

Two separate objects on purpose:

* ``Secrets``   - API keys, read from ``.env`` via pydantic-settings. Never written back.
* ``AppConfig`` - everything else, read from ``config.toml``. The settings page edits this
                  and writes it back through ``ConfigStore.save()``.

Only this module and ``llm/registry.py`` know which provider is configured.
"""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path
from typing import Literal

import tomli_w
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"

SearchProviderName = Literal["tavily", "brave", "claude_websearch", "none"]


# --------------------------------------------------------------------------- secrets


class Secrets(BaseSettings):
    """API keys from ``.env`` (or the process environment)."""

    model_config = SettingsConfigDict(
        env_file=os.environ.get("MODREVIEW_ENV_FILE", str(DEFAULT_ENV_PATH)),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    anthropic_api_key: SecretStr | None = None
    disqus_api_key: SecretStr | None = None
    tavily_api_key: SecretStr | None = None
    brave_api_key: SecretStr | None = None
    reddit_client_id: SecretStr | None = None
    reddit_client_secret: SecretStr | None = None
    openai_compat_base_url: str | None = None
    openai_compat_api_key: SecretStr | None = None

    def present(self) -> dict[str, bool]:
        """Which keys are set. Only booleans ever leave this object for the UI."""
        out: dict[str, bool] = {}
        for name, value in self.model_dump().items():
            if isinstance(value, SecretStr):
                out[name] = bool(value.get_secret_value().strip())
            else:
                out[name] = bool(value and str(value).strip())
        return out


# --------------------------------------------------------------------------- config.toml


class LLMFallback(BaseModel):
    provider: str = "openai_compat"
    model: str = "gemini-flash-lite-latest"


class LLMConfig(BaseModel):
    provider: str = "openai_compat"
    model: str = "gemini-flash-latest"
    max_output_tokens: int = Field(16000, ge=256)
    temperature: float = Field(0.2, ge=0.0, le=1.0)
    # Sent as ``reasoning_effort`` by the openai_compat provider (Gemini: thinking level).
    reasoning_effort: str | None = None
    fallback: LLMFallback | None = None


class SearchConfig(BaseModel):
    provider: SearchProviderName = "tavily"
    max_results: int = Field(8, ge=1, le=50)


class SourcesConfig(BaseModel):
    nusmods: bool = True
    disqus: bool = True
    reddit: bool = True
    # Full, dated r/nus threads from the Arctic Shift archive (third-party Reddit archive).
    # Off: Reddit falls back to search-result snippets.
    reddit_archive: bool = True
    websearch: bool = True
    course_pages: bool = True  # lecturers' official pages on nus.edu.sg
    blogs: bool = True  # seniors' write-ups on github.io, Medium, Substack, ...


class LimitsConfig(BaseModel):
    max_chars_per_doc: int = Field(6000, ge=200)
    max_total_chars: int = Field(120_000, ge=1000)
    max_input_tokens_per_report: int = Field(60_000, ge=1000)
    cache_ttl_days: int = Field(14, ge=0)
    # Recency: sources older than this are left out, unless fewer than
    # min_recent_docs newer opinion sources exist. Web search is filtered the same way.
    max_source_age_years: float = Field(4, ge=0.5, le=20)
    min_recent_docs: int = Field(10, ge=0)
    http_timeout_s: float = Field(20.0, gt=0)


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(8765, ge=1, le=65535)


class Pricing(BaseModel):
    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)


class AppConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    pricing: dict[str, Pricing] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> AppConfig:
        if not path.exists():
            log.warning("config.toml not found at %s; using defaults", path)
            return cls()
        with path.open("rb") as fh:
            return cls.model_validate(tomllib.load(fh))

    def dump(self, path: Path) -> None:
        data = self.model_dump(mode="json", exclude_none=True)
        tmp = path.with_suffix(".toml.tmp")
        with tmp.open("wb") as fh:
            tomli_w.dump(data, fh)
        tmp.replace(path)


class ConfigStore:
    """Holds the live ``AppConfig`` so the settings page can change it without a restart."""

    def __init__(self, path: Path = DEFAULT_CONFIG_PATH) -> None:
        self.path = path
        self.config = AppConfig.load(path)

    def reload(self) -> AppConfig:
        self.config = AppConfig.load(self.path)
        return self.config

    def save(self, config: AppConfig) -> AppConfig:
        config.dump(self.path)
        self.config = config
        return config

    def update(self, **sections: BaseModel) -> AppConfig:
        """Replace whole sections, e.g. ``store.update(llm=LLMConfig(...))``."""
        merged = self.config.model_copy(update=sections)
        return self.save(merged)


def config_path_from_env() -> Path:
    return Path(os.environ.get("MODREVIEW_CONFIG", str(DEFAULT_CONFIG_PATH)))


def data_dir_from_env() -> Path:
    p = Path(os.environ.get("MODREVIEW_DATA_DIR", str(DEFAULT_DATA_DIR)))
    p.mkdir(parents=True, exist_ok=True)
    return p
