from __future__ import annotations

from pathlib import Path

from modreview.settings import AppConfig, ConfigStore, LLMConfig, Secrets


def test_defaults_when_missing(tmp_path: Path) -> None:
    cfg = AppConfig.load(tmp_path / "nope.toml")
    assert cfg.llm.provider == "anthropic"
    assert cfg.server.host == "127.0.0.1"


def test_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text(
        '[llm]\nprovider="anthropic"\nmodel="m1"\n[llm.fallback]\nmodel="m2"\n'
        '[pricing."m1"]\ninput_per_mtok=1.0\noutput_per_mtok=5.0\n',
        encoding="utf-8",
    )
    cfg = AppConfig.load(p)
    assert cfg.llm.model == "m1"
    assert cfg.llm.fallback is not None and cfg.llm.fallback.model == "m2"
    assert cfg.pricing["m1"].output_per_mtok == 5.0

    cfg.dump(p)
    assert AppConfig.load(p) == cfg


def test_store_update_persists(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    store = ConfigStore(p)
    store.update(llm=LLMConfig(model="claude-sonnet-5"))
    assert ConfigStore(p).config.llm.model == "claude-sonnet-5"


def test_repo_config_toml_parses() -> None:
    root = Path(__file__).resolve().parents[1]
    cfg = AppConfig.load(root / "config.toml")
    assert cfg.llm.model
    assert cfg.limits.max_input_tokens_per_report == 60_000


def test_secrets_present_never_leaks_values(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-super-secret")
    s = Secrets(_env_file=None)
    present = s.present()
    assert present["anthropic_api_key"] is True
    assert present["tavily_api_key"] is False
    assert "sk-super-secret" not in repr(s)
    assert "sk-super-secret" not in str(present)
