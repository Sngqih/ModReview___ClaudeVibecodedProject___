from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from modreview.cache import ReviewCache, cache_key
from modreview.llm.base import Usage
from modreview.review import ReviewResult
from modreview.sources.nusmods import parse_module
from tests.helpers import load_json, sample_report


def _result(code="CS2040S", model="m1", when=None) -> ReviewResult:
    return ReviewResult(
        module_code=code,
        facts=parse_module(load_json("nusmods_CS2040S.json"), "2026-2027"),
        report=sample_report(code),
        provider="fake",
        model=model,
        prompt_version="summarise_v1",
        usage=Usage(input_tokens=10, output_tokens=5),
        generated_at=when or datetime.now(UTC),
    )


def test_put_get_round_trip(tmp_path: Path) -> None:
    c = ReviewCache(tmp_path / "db.sqlite")
    key = c.put(_result())
    assert key == cache_key("CS2040S", "fake", "m1", "summarise_v1")
    got = c.get("CS2040S", "fake", "m1", "summarise_v1", ttl_days=14)
    assert got is not None and got.report == sample_report("CS2040S")
    assert got.from_cache is False  # the pipeline sets it, the store does not


def test_cache_key_separates_model_and_prompt(tmp_path: Path) -> None:
    c = ReviewCache(tmp_path / "db.sqlite")
    c.put(_result(model="m1"))
    assert c.get("CS2040S", "fake", "m2", "summarise_v1", 14) is None
    assert c.get("CS2040S", "fake", "m1", "summarise_v2", 14) is None


def test_ttl_expiry_and_disable(tmp_path: Path) -> None:
    c = ReviewCache(tmp_path / "db.sqlite")
    old = datetime.now(UTC) - timedelta(days=20)
    c.put(_result(when=old))
    assert c.get("CS2040S", "fake", "m1", "summarise_v1", ttl_days=14) is None
    assert c.get("CS2040S", "fake", "m1", "summarise_v1", ttl_days=30) is not None
    assert c.get("CS2040S", "fake", "m1", "summarise_v1", ttl_days=0) is None


def test_regenerate_replaces_row(tmp_path: Path) -> None:
    c = ReviewCache(tmp_path / "db.sqlite")
    c.put(_result(when=datetime(2026, 1, 1, tzinfo=UTC)))
    c.put(_result(when=datetime(2026, 9, 1, tzinfo=UTC)))
    rows = c.history()
    assert len(rows) == 1 and rows[0].created_at.month == 9


def test_history_recent_and_delete(tmp_path: Path) -> None:
    c = ReviewCache(tmp_path / "db.sqlite")
    base = datetime(2026, 9, 1, tzinfo=UTC)
    c.put(_result("CS2040S", "m1", base))
    c.put(_result("CS2040S", "m2", base + timedelta(hours=1)))
    c.put(_result("MA1521", "m1", base + timedelta(hours=2)))
    assert [r.module_code for r in c.history()] == ["MA1521", "CS2040S", "CS2040S"]
    assert [r.module_code for r in c.recent_codes()] == ["MA1521", "CS2040S"]
    assert c.history()[0].sentiment == "positive" and c.history()[0].difficulty == 4
    key = c.history()[0].cache_key
    assert c.get_by_key(key) is not None
    assert c.delete(key) is True and c.delete(key) is False
    assert len(c.history()) == 2


def test_put_rejects_unfinished(tmp_path: Path) -> None:
    c = ReviewCache(tmp_path / "db.sqlite")
    r = _result().model_copy(update={"report": None})
    with pytest.raises(ValueError):
        c.put(r)
