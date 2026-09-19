from __future__ import annotations

from datetime import UTC, datetime, timedelta

from modreview.clean import (
    TRUNCATION_MARK,
    clean_docs,
    content_hash,
    enforce_citations,
    normalise_whitespace,
    rank_docs,
    select_docs,
    truncate,
)
from modreview.models import Claim, LecturerNote, SourceDoc
from tests.helpers import sample_report

NOW = datetime(2026, 9, 19, tzinfo=UTC)


def _doc(source, n, *, days_old=None, score=None, text=None) -> SourceDoc:
    return SourceDoc.make(
        source=source,
        url=f"https://{source}/{n}",
        text=text or f"{source} doc {n} ".ljust(100, "x"),  # exactly 100 chars
        published_at=None if days_old is None else NOW - timedelta(days=days_old),
        score=score,
    )


def test_normalise_whitespace() -> None:
    assert normalise_whitespace("a \t b\r\n\r\n\r\n\nc  ") == "a b\n\nc"


def test_content_hash_ignores_case_and_punctuation() -> None:
    assert content_hash("Hard module!!  Do PS early.") == content_hash("hard module do ps early")
    assert content_hash("hard module") != content_hash("easy module")


def test_truncate_prefers_paragraph_boundary() -> None:
    text = ("p" * 70) + "\n\n" + ("q" * 70)
    out = truncate(text, 100)
    assert out == "p" * 70 + TRUNCATION_MARK
    assert truncate("short", 100) == "short"
    hard = truncate("z" * 500, 100)
    assert len(hard) == 100 and hard.endswith(TRUNCATION_MARK)


def test_clean_docs_dedupes_and_truncates() -> None:
    a = _doc("disqus", 1, text="Great module.   Do the PS early!")
    b = _doc("web", 2, text="great module do the ps early")  # near duplicate, later source
    c = _doc("reddit", 3, text="   ")  # empty after cleaning
    d = _doc("web", 4, text="long " * 400)
    out = clean_docs([a, b, c, d], 300)
    assert [x.id for x in out] == [a.id, d.id]
    assert out[0].text == "Great module. Do the PS early!"
    assert len(out[1].text) <= 300


def test_rank_recency_then_score_then_source() -> None:
    old_hi = _doc("disqus", 1, days_old=800, score=50)
    new_lo = _doc("web", 2, days_old=10, score=0)
    new_hi = _doc("reddit", 3, days_old=20, score=9)
    tie_disqus = _doc("disqus", 4, days_old=30, score=0)
    undated = _doc("web", 5)
    ranked = rank_docs([old_hi, new_lo, new_hi, tie_disqus, undated], NOW)
    assert [d.id for d in ranked] == [new_hi.id, tie_disqus.id, new_lo.id, old_hi.id, undated.id]


def test_select_docs_keeps_facts_and_source_floor() -> None:
    facts = _doc("nusmods", 0, text="F" * 500)
    disqus = [_doc("disqus", i, days_old=i, score=100) for i in range(20)]
    reddit = [_doc("reddit", i, days_old=900) for i in range(5)]
    web = [_doc("web", i) for i in range(5)]
    budget = 500 + 12 * 100
    out = select_docs([*disqus, facts, *reddit, *web], budget, now=NOW)
    assert out[0] is facts
    counts = {s: sum(1 for d in out if d.source == s) for s in ("disqus", "reddit", "web")}
    assert counts == {"disqus": 6, "reddit": 3, "web": 3}  # floor 3 each, rest by rank
    assert sum(len(d.text) for d in out) <= budget


def test_select_docs_skips_docs_that_do_not_fit() -> None:
    facts = _doc("nusmods", 0, text="F" * 100)
    big = _doc("disqus", 1, days_old=1, text="B" * 1000)
    small = _doc("disqus", 2, days_old=2, text="S" * 100)
    out = select_docs([facts, big, small], 300, now=NOW)
    assert [d.id for d in out] == [facts.id, small.id]


def test_enforce_citations_drops_unknown_ids_and_empty_claims() -> None:
    r = sample_report("cs2040s-wrong")
    r = r.model_copy(
        update={
            "workload_summary": [
                Claim(text="ok", source_ids=["d1", "d1", "ghost"]),
                Claim(text="all fake", source_ids=["ghost", "phantom"]),
            ],
            "lecturers_and_teaching": [
                LecturerNote(name="X", semester=None, notes=[Claim(text="n", source_ids=["zz"])])
            ],
        }
    )
    cleaned, removed = enforce_citations(r, {"d1", "d2"}, "CS2040S")
    assert cleaned.module_code == "CS2040S"
    assert [c.source_ids for c in cleaned.workload_summary] == [["d1"]]
    assert cleaned.lecturers_and_teaching == []
    assert removed == 4  # ghost + ghost + phantom + zz (the duplicate d1 is merged, not counted)
    assert cleaned.all_source_ids() <= {"d1", "d2"}
