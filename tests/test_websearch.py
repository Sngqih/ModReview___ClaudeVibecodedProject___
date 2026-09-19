from __future__ import annotations

import httpx
import pytest
import respx

from modreview.http import make_client
from modreview.search.base import SearchHit
from modreview.settings import AppConfig, Secrets
from modreview.sources.base import SourceContext, SourceUnavailable
from modreview.sources.websearch import (
    WebSearchSource,
    classify,
    extract_page,
    is_excluded,
    mentions_code,
    queries_for,
)
from tests.helpers import FakeSearch, load_text

BLOG = "https://example-blog.sg/cs2040s-review"
OTHER = "https://another-site.com/laptops"
NUS = "https://www.comp.nus.edu.sg/programmes/ug/cs2040s-outline"
NUSMODS = "https://nusmods.com/courses/CS2040S/data-structures-and-algorithms"
REDDIT = "https://old.reddit.com/r/nus/comments/abc123/x/"
PDF = "https://www.comp.nus.edu.sg/cs2040s/syllabus.pdf"
LOGIN = "https://members.example.com/post/1"

LONG_CONTENT = (
    "CS2040S is the data structures module at NUS. Workload was around 12 hours a week. "
    "The problem sets are weekly and graded. Finals are 40 percent, midterm 20 percent. "
    "I found the module tough but fair, and the bell curve steep. Start each PS early."
) * 2


def _ctx(client, search) -> SourceContext:
    return SourceContext(
        client=client, config=AppConfig(), secrets=Secrets(_env_file=None), search=search
    )


def test_helpers() -> None:
    assert is_excluded(NUSMODS) and is_excluded(REDDIT) and not is_excluded(NUS)
    assert mentions_code("took cs2040s last sem", "CS2040S")
    assert not mentions_code("CS2040 is different", "CS2040S")
    assert not mentions_code("XCS2040S", "CS2040S")
    plans = queries_for("CS2040S", "Data Structures and Algorithms")
    assert len(plans) == 5 and '"Data Structures and Algorithms"' in plans[1].query
    official, blogs = plans[3], plans[4]
    assert official.include_domains == ("nus.edu.sg",) and not official.dated
    assert "github.io" in blogs.include_domains and blogs.dated
    assert len(queries_for("CS2040S", None)) == 4
    assert len(queries_for("CS2040S", None, course_pages=False, blogs=False)) == 2
    assert classify("https://www.comp.nus.edu.sg/~stevenha/cs2040s.html") == "course_page"
    assert classify("https://chrisjwelly.github.io/blog/cs2040s") == "blog"
    assert classify("https://medium.com/@x/cs2040s") == "blog"
    assert classify("https://example.com/cs2040s") == "web"


def test_extract_page_pulls_article_not_nav() -> None:
    text, title, author, date = extract_page(load_text("web_page.html"), BLOG)
    assert "heaviest module of the semester" in text
    assert "Home" not in text.split("\n")[0]
    assert title and "module reviews" in title
    assert date is not None and date.year == 2024


async def test_no_provider_is_unavailable() -> None:
    async with make_client() as client:
        with pytest.raises(SourceUnavailable):
            await WebSearchSource(_ctx(client, None)).fetch("CS2040S", None)


@respx.mock
async def test_fetch_filters_and_extracts() -> None:
    hits = [
        SearchHit(url=BLOG, title="Review", snippet="s", content=LONG_CONTENT),  # no fetch
        SearchHit(url=NUS, title="Outline", snippet="s"),  # fetched, mentions code
        SearchHit(url=OTHER, title="Laptops", snippet="s"),  # fetched, no code -> dropped
        SearchHit(url=NUSMODS, title="NUSMods", snippet="s"),  # excluded domain
        SearchHit(url=REDDIT, title="Reddit", snippet="s"),  # excluded domain
        SearchHit(url=PDF, title="PDF", snippet="s"),  # pdf skipped
        SearchHit(url=LOGIN, title="Members", snippet="s"),  # 403 -> skipped
        SearchHit(url=BLOG + "?ref=x", title="dupe", snippet="s"),  # dedupe
    ]
    nus_route = respx.get(NUS).mock(
        return_value=httpx.Response(
            200, text=load_text("web_page.html"), headers={"content-type": "text/html"}
        )
    )
    respx.get(OTHER).mock(
        return_value=httpx.Response(
            200, text=load_text("web_page_other.html"), headers={"content-type": "text/html"}
        )
    )
    respx.get(LOGIN).mock(return_value=httpx.Response(403))
    blog_route = respx.get(BLOG)
    pdf_route = respx.get(PDF)
    nusmods_route = respx.get(NUSMODS)

    search = FakeSearch(hits)
    async with make_client() as client:
        src = WebSearchSource(_ctx(client, search))
        docs = await src.fetch("CS2040S", "Data Structures and Algorithms")

    assert [d.url for d in docs] == [BLOG, NUS]
    assert [d.source for d in docs] == ["web", "course_page"]
    assert docs[0].text == LONG_CONTENT and docs[0].title == "Review"
    assert "heaviest module" in docs[1].text and docs[1].author == "Blogger Tan"
    assert nus_route.call_count == 1
    assert blog_route.call_count == 0 and pdf_route.call_count == 0
    assert nusmods_route.call_count == 0
    assert src.note and "1 official" in src.note and "skipped" in src.note
    general = [c for c in search.calls if not c["include_domains"]]
    targeted = [c for c in search.calls if c["include_domains"]]
    assert len(general) == 3 and len(targeted) == 2
    assert all("nusmods.com" in c["exclude_domains"] for c in general)
    assert all(c["start_date"] for c in general)  # recency window applied
    official = next(c for c in targeted if c["include_domains"] == ["nus.edu.sg"])
    assert official["start_date"] is None  # official pages are rarely dated


@respx.mock
async def test_course_page_fetched_in_full_even_with_long_excerpt() -> None:
    """Official pages keep their assessment tables below the fold, so they are always read."""
    hit = SearchHit(url=NUS, title="Outline", snippet="s", content="CS2040S outline " * 20)
    route = respx.get(NUS).mock(
        return_value=httpx.Response(
            200, text=load_text("web_page.html"), headers={"content-type": "text/html"}
        )
    )
    async with make_client() as client:
        docs = await WebSearchSource(_ctx(client, FakeSearch([hit]))).fetch("CS2040S", None)
    assert route.call_count == 1 and docs[0].source == "course_page"
    assert "heaviest module" in docs[0].text


async def test_all_queries_failing_raises() -> None:
    async with make_client() as client:
        with pytest.raises(RuntimeError):
            await WebSearchSource(_ctx(client, FakeSearch(fail=True))).fetch("CS2040S", None)
