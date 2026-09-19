from __future__ import annotations

from datetime import UTC, datetime

import pytest

from modreview.clean import apply_recency, estimate_dates, rank_docs
from modreview.dates import (
    ay_label,
    ay_range_label,
    ay_start_year,
    cutoff,
    infer_date_from_text,
    semester_of,
)
from modreview.models import SourceDoc

NOW = datetime(2026, 9, 19, tzinfo=UTC)


@pytest.mark.parametrize(
    ("when", "label"),
    [
        (datetime(2026, 9, 1, tzinfo=UTC), "AY26/27 S1"),
        (datetime(2026, 3, 12, tzinfo=UTC), "AY25/26 S2"),
        (datetime(2026, 6, 20, tzinfo=UTC), "AY25/26 ST"),
        (datetime(2025, 8, 1, tzinfo=UTC), "AY25/26 S1"),
        (datetime(2025, 7, 31), "AY24/25 ST"),  # naive datetimes are treated as UTC
    ],
)
def test_ay_label(when, label) -> None:
    assert ay_label(when, with_sem=True) == label
    assert ay_label(when) == label.split()[0]


def test_ay_helpers() -> None:
    assert ay_start_year(datetime(2027, 1, 5, tzinfo=UTC)) == 2026
    assert semester_of(datetime(2026, 12, 31, tzinfo=UTC)) == "S1"
    dates = [datetime(2024, 11, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)]
    assert ay_range_label(dates) == "AY24/25–25/26"
    assert ay_range_label(dates[:1]) == "AY24/25"
    assert ay_range_label([]) is None
    assert cutoff(4, NOW).year == 2022


@pytest.mark.parametrize(
    ("text", "year"),
    [
        ("Took it in AY24/25 Sem 2 under Prof X, then TA'd in AY 2025/26.", 2025),
        ("CS2040S AY2023/2024 review", 2023),
        ("Timetable for 2024/2025, semester 1", 2024),
        ("AY 22-23 was rough", 2022),
        ("No year mentioned at all", None),
        ("The 2030/31 roadmap", None),  # future years are ignored
        ("Scores went from 2024/2026", None),  # not consecutive years
    ],
)
def test_infer_date_from_text(text, year) -> None:
    got = infer_date_from_text(text, NOW)
    assert (got.year if got else None) == year
    if got:
        assert (got.month, got.day) == (8, 15)


def _doc(n, *, when=None, text="an opinion", source="disqus"):
    return SourceDoc.make(source=source, url=f"https://x/{n}", text=text, published_at=when)


def test_estimate_dates_marks_guesses_only_for_undated_opinions() -> None:
    dated = _doc(1, when=datetime(2025, 1, 1, tzinfo=UTC), text="AY20/21 memories")
    guess = _doc(2, text="My AY24/25 S2 review", source="blog")
    facts = _doc(3, text="Offered AY26/27", source="nusmods")
    unknown = _doc(4, text="no year", source="web")
    out = {d.id: d for d in estimate_dates([dated, guess, facts, unknown], NOW)}
    assert out[dated.id].published_at.year == 2025 and not out[dated.id].date_estimated
    assert out[guess.id].published_at.year == 2024 and out[guess.id].date_estimated
    assert out[facts.id].published_at is None
    assert out[unknown.id].published_at is None


def test_apply_recency_drops_old_only_when_enough_recent() -> None:
    recent = [_doc(i, when=datetime(2025, 3, 1, tzinfo=UTC)) for i in range(10)]
    old = [_doc(100 + i, when=datetime(2020, 3, 1, tzinfo=UTC)) for i in range(4)]
    undated = [_doc(200)]
    facts = [_doc(300, source="nusmods")]
    kept, dropped = apply_recency(recent + old + undated + facts, 4, 10, NOW)
    assert dropped == 4 and len(kept) == 12
    assert all(d.published_at is None or d.published_at.year >= 2022 for d in kept)
    # Too few recent opinions: keep everything rather than starve the report.
    kept, dropped = apply_recency(recent[:3] + old + facts, 4, 10, NOW)
    assert dropped == 0 and len(kept) == 8


def test_rank_uses_semester_buckets() -> None:
    this_sem = _doc(1, when=datetime(2026, 8, 20, tzinfo=UTC))
    last_sem = _doc(2, when=datetime(2026, 2, 20, tzinfo=UTC))
    last_sem.score = 99  # a popular post from a semester ago still ranks below this semester
    assert [d.id for d in rank_docs([last_sem, this_sem], NOW)] == [this_sem.id, last_sem.id]
