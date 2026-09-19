"""Per-claim date tags, the header timeline, and dated exports."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from modreview.export import to_markdown
from modreview.models import Claim, SourceDoc
from modreview.web import claim_date, doc_period, source_timeline
from tests.helpers import make_result
from tests.test_web import _events

NOW = datetime(2026, 9, 19, tzinfo=UTC)


def _d(n, when=None, source="disqus", estimated=False):
    return SourceDoc.make(
        source=source, url=f"https://x/{n}", text="t", published_at=when, date_estimated=estimated
    )


def test_claim_date_kinds() -> None:
    a = _d(1, datetime(2024, 11, 2, tzinfo=UTC))
    b = _d(2, datetime(2026, 2, 10, tzinfo=UTC))
    old = _d(3, datetime(2022, 3, 1, tzinfo=UTC))
    est = _d(4, datetime(2025, 8, 15, tzinfo=UTC), estimated=True)
    facts = _d(5, source="nusmods")
    page = _d(6, source="course_page")
    undated = _d(7)
    docs = {d.id: d for d in (a, b, old, est, facts, page, undated)}

    def tag(*ds):
        return claim_date(Claim(text="x", source_ids=[d.id for d in ds]), docs, NOW)

    t = tag(a, b)
    assert t["label"] == "AY24/25\u201325/26" and not t["stale"] and t["kind"] == "dated"
    assert "Nov 2024 to Feb 2026" in t["title"]
    assert tag(facts, b)["label"] == "AY25/26"  # the official listing does not widen the range
    stale = tag(old)
    assert stale["stale"] and "out of date" in stale["title"]
    assert tag(est)["label"] == "~AY25/26" and "estimated" in tag(est)["title"]
    assert tag(facts)["kind"] == "official" and tag(facts, page)["label"] == "official"
    assert tag(undated)["kind"] == "undated"
    assert claim_date(Claim(text="x", source_ids=["nope"]), docs, NOW) is None


def test_doc_period() -> None:
    assert (
        doc_period(_d(1, datetime(2025, 3, 12, 4, tzinfo=UTC))) == "12 Mar 2025 \u00b7 AY24/25 S2"
    )
    guessed = _d(2, datetime(2024, 8, 15, tzinfo=UTC), estimated=True)
    assert doc_period(guessed) == "about AY24/25 (estimated from the text)"
    assert doc_period(_d(3)) == "undated"


def test_source_timeline_counts_by_academic_year() -> None:
    docs = [
        _d(1, datetime(2026, 9, 1, tzinfo=UTC)),  # AY26/27 (current)
        _d(2, datetime(2026, 3, 1, tzinfo=UTC)),  # AY25/26
        _d(3, datetime(2026, 2, 1, tzinfo=UTC), estimated=True),  # AY25/26
        _d(4, datetime(2023, 1, 1, tzinfo=UTC)),  # AY22/23
        _d(5, datetime(2019, 1, 1, tzinfo=UTC)),  # older than the 5 shown years
        _d(6),  # undated
        _d(7, source="nusmods"),  # facts are not opinions: not counted
    ]
    tl = source_timeline(make_result(docs=docs, docs_too_old=3), NOW)
    labels = [b["label"] for b in tl["bars"]]
    assert labels == ["older", "22/23", "23/24", "24/25", "25/26", "26/27"]
    counts = {b["label"]: b["count"] for b in tl["bars"]}
    assert counts == {"older": 1, "22/23": 1, "23/24": 0, "24/25": 0, "25/26": 2, "26/27": 1}
    recent = [b["label"] for b in tl["bars"] if b["recent"]]
    assert recent == ["25/26", "26/27"]
    assert tl["recent_share"] == 60 and tl["undated"] == 1 and tl["estimated"] == 1
    assert tl["newest_label"] == "AY26/27 S1" and tl["too_old"] == 3
    assert max(b["pct"] for b in tl["bars"]) == 100


def test_report_page_shows_timeline_and_date_tags(client: TestClient) -> None:
    html = _events(client.get("/review/CS2040S/stream").text)[-1][1]
    assert "Sources by academic year" in html and 'class="timeline__col' in html
    assert "<caption>Number of sources used, by NUS academic year</caption>" in html
    assert 'class="claim-date' in html
    assert "AY25/26 S1" in html  # a source's period in the list (2 Nov 2025)


def test_markdown_carries_claim_dates() -> None:
    md = to_markdown(make_result())
    assert "Finals matter most. [3] *(AY25/26)*" in md
    assert "\u00b7 2 Nov 2025 \u00b7 AY25/26 S1" in md  # dated source line


def test_settings_save_new_source_toggles_and_recency(client: TestClient) -> None:
    r = client.post(
        "/settings",
        data={
            "provider": "openai_compat",
            "model": "gemini-flash-latest",
            "search_provider": "tavily",
            "src_disqus": "on",
            "src_reddit": "on",
            "src_websearch": "on",
            "src_course_pages": "on",
            "max_source_age_years": "3",
            "cache_ttl_days": "14",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    cfg = client.app.state.store.config
    assert cfg.sources.course_pages and not cfg.sources.blogs and not cfg.sources.reddit_archive
    assert cfg.limits.max_source_age_years == 3
    page = client.get("/settings").text
    assert 'name="src_reddit_archive"' in page and "Arctic Shift" in page


def test_mixed_naive_and_aware_dates_do_not_crash() -> None:
    """Regression: a naive trafilatura date next to UTC Reddit dates crashed the timeline."""
    naive = SourceDoc.make(
        source="web", url="https://w", text="t", published_at=datetime(2025, 3, 1)
    )
    aware = _d(2, datetime(2026, 3, 1, tzinfo=UTC))
    assert naive.published_at.tzinfo is not None
    tl = source_timeline(make_result(docs=[naive, aware]), NOW)
    assert tl["newest_label"] == "AY25/26 S2"
    docs = {naive.id: naive, aware.id: aware}
    assert claim_date(Claim(text="x", source_ids=[naive.id, aware.id]), docs, NOW)["label"]
