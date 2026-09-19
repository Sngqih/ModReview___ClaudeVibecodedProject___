"""Reddit via the Arctic Shift archive, against respx-mocked responses (no network)."""

from __future__ import annotations

import httpx
import pytest
import respx

from modreview.http import make_client
from modreview.search.base import SearchHit
from modreview.settings import AppConfig, Secrets, SourcesConfig
from modreview.sources.base import SourceContext
from modreview.sources.reddit import ARCHIVE_API, RedditSource
from tests.helpers import FakeSearch, no_sleep

pytestmark = pytest.mark.archive

TS_2026 = 1789000000  # 2026-09-10
TS_2025 = 1750000000  # 2025-06-15

POSTS = [
    {
        "id": "abc123",
        "title": "Failing CS2040S",
        "selftext": "Midterm went badly, what now?",
        "score": 29,
        "created_utc": TS_2026,
        "author": "worried",
        "permalink": "/r/nus/comments/abc123/failing_cs2040s/",
        "num_comments": 3,
    },
    {  # title does not name the module: ignored as a title match
        "id": "zzz999",
        "title": "Which mods to take?",
        "selftext": "",
        "score": 3,
        "created_utc": TS_2026,
        "permalink": "/r/nus/comments/zzz999/which_mods/",
    },
]
TREE = [
    {
        "kind": "t1",
        "data": {
            "body": "Finals are 40%, you can still recover. Do past papers.",
            "score": 24,
            "created_utc": TS_2026,
            "replies": {
                "kind": "Listing",
                "data": {
                    "children": [
                        {
                            "kind": "t1",
                            "data": {
                                "body": "Agree, S2 grading is fair.",
                                "score": 5,
                                "replies": "",
                            },
                        }
                    ]
                },
            },
        },
    },
    {"kind": "t1", "data": {"body": "[removed]", "score": 1, "replies": ""}},
]
COMMENTS = [
    {  # in the title thread: already covered by its tree
        "id": "c1",
        "link_id": "t3_abc123",
        "body": "CS2040S finals are 40%",
        "score": 24,
        "created_utc": TS_2026,
    },
    {  # in another thread: grouped as a comments document
        "id": "c2",
        "link_id": "t3_def456",
        "body": "Took CS2040S in 25/26 S2, chill if you do the PS early.",
        "score": 14,
        "created_utc": TS_2026,
    },
    {
        "id": "c3",
        "link_id": "t3_def456",
        "body": "CS2040S labs are graded individually.",
        "score": 2,
        "created_utc": TS_2025,
    },
    {  # body match on a different code: dropped by the relevance check
        "id": "c4",
        "link_id": "t3_ghi789",
        "body": "CS2040S2 is not a thing",
        "score": 50,
        "created_utc": TS_2026,
    },
]
OTHER_THREAD = {
    "id": "def456",
    "title": "How hard is it to fail CS2106?",
    "score": 20,
    "created_utc": TS_2026,
    "permalink": "/r/nus/comments/def456/how_hard_is_it_to_fail_cs2106/",
}


def _ctx(client, search=None, archive=True) -> SourceContext:
    cfg = AppConfig(sources=SourcesConfig(reddit_archive=archive))
    return SourceContext(client=client, config=cfg, secrets=Secrets(_env_file=None), search=search)


def _mock_archive(first_window_timeouts: int = 0) -> dict[str, respx.Route]:
    """Comments only in the newest academic-year window; older windows are empty."""
    state = {"timeouts": first_window_timeouts}

    def comments(request: httpx.Request) -> httpx.Response:
        if "before" in request.url.params:  # an older window
            return httpx.Response(200, json={"data": []})
        if state["timeouts"]:
            state["timeouts"] -= 1
            return httpx.Response(
                422, json={"data": None, "error": "Timeout. Maybe slow down a bit"}
            )
        return httpx.Response(200, json={"data": COMMENTS + COMMENTS[:1]})  # one duplicate

    return {
        "comments": respx.get(f"{ARCHIVE_API}/comments/search").mock(side_effect=comments),
        "ids": respx.get(f"{ARCHIVE_API}/posts/ids").mock(
            return_value=httpx.Response(200, json={"data": [POSTS[0], OTHER_THREAD]})
        ),
        "tree": respx.get(f"{ARCHIVE_API}/comments/tree").mock(
            return_value=httpx.Response(200, json={"data": TREE})
        ),
    }


@respx.mock
async def test_archive_builds_dated_thread_and_comment_docs() -> None:
    routes = _mock_archive()
    async with make_client() as client:
        src = RedditSource(_ctx(client), sleep=no_sleep)
        docs = await src.fetch("CS2040S", "Data Structures and Algorithms")

    assert len(docs) == 2
    thread, mentions = docs
    assert thread.url == "https://www.reddit.com/r/nus/comments/abc123/failing_cs2040s"
    assert thread.published_at and thread.published_at.year == 2026
    assert "[24 points] Finals are 40%" in thread.text and "Agree, S2 grading" in thread.text
    assert "[removed]" not in thread.text
    assert mentions.title == "r/nus: How hard is it to fail CS2106? (comments on CS2040S)"
    assert mentions.score == 14 and mentions.published_at.year == 2026  # newest comment
    assert "[14 points, 2026-09-10] Took CS2040S" in mentions.text
    assert "CS2040S2" not in mentions.text  # different module code filtered out
    assert src.note and src.note.startswith("archive: 1 thread")

    params = [c.request.url.params for c in routes["comments"].calls]
    assert len(params) == 5  # one search per academic year over the 4-year window
    newest = next(p for p in params if "before" not in p)
    assert newest["subreddit"] == "nus" and newest["body"] == "CS2040S"
    assert newest["after"].endswith("-08-01")
    assert any(p.get("before") == newest["after"] for p in params)  # windows chain back
    ids = routes["ids"].calls[0].request.url.params["ids"].split(",")
    assert ids[:2] == ["abc123", "def456"]
    assert routes["tree"].calls[0].request.url.params["link_id"] == "abc123"


@respx.mock
async def test_archive_timeouts_in_one_window_are_tolerated() -> None:
    routes = _mock_archive(first_window_timeouts=2)  # the newest window fails twice
    async with make_client() as client:
        src = RedditSource(_ctx(client), sleep=no_sleep)
        docs = await src.fetch("CS2040S", None)
    assert routes["comments"].call_count >= 3  # retried once, then moved on to older years
    assert docs == []  # older windows were empty: nothing recent, so no documents
    assert "nothing recent in the archive" in (src.note or "")


@respx.mock
async def test_archive_down_falls_back_to_search_snippets() -> None:
    respx.get(url__startswith=ARCHIVE_API).mock(return_value=httpx.Response(503))
    respx.get(url__regex=r"https://www\.reddit\.com/.*\.json").mock(
        return_value=httpx.Response(403)
    )
    hit = SearchHit(
        url="https://www.reddit.com/r/nus/comments/abc123/x/",
        title="CS2040S thread",
        snippet="heavy but fair",
    )
    async with make_client() as client:
        src = RedditSource(_ctx(client, FakeSearch([hit])), sleep=no_sleep)
        docs = await src.fetch("CS2040S", None)
    assert len(docs) == 1 and "heavy but fair" in docs[0].text
    assert src.note and src.note.startswith("archive unavailable")


@respx.mock
async def test_archive_uses_web_search_threads_too() -> None:
    """A thread found only by web search is still read in full through the archive."""
    respx.get(f"{ARCHIVE_API}/comments/search").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    ids = respx.get(f"{ARCHIVE_API}/posts/ids").mock(
        return_value=httpx.Response(200, json={"data": [POSTS[0]]})
    )
    respx.get(f"{ARCHIVE_API}/comments/tree").mock(
        return_value=httpx.Response(200, json={"data": TREE})
    )
    hit = SearchHit(url="https://www.reddit.com/r/nus/comments/abc123/failing_cs2040s/", title="t")
    async with make_client() as client:
        src = RedditSource(_ctx(client, FakeSearch([hit])), sleep=no_sleep)
        docs = await src.fetch("CS2040S", None)
    assert ids.calls[0].request.url.params["ids"] == "abc123"
    assert len(docs) == 1 and "Finals are 40%" in docs[0].text


@respx.mock
async def test_archive_rate_limit_stops_immediately() -> None:
    comments = respx.get(f"{ARCHIVE_API}/comments/search").mock(return_value=httpx.Response(429))
    ids = respx.get(f"{ARCHIVE_API}/posts/ids")
    async with make_client() as client:
        src = RedditSource(_ctx(client, FakeSearch([])), sleep=no_sleep)
        assert await src.fetch("CS2040S", None) == []
    assert comments.call_count <= 2 and ids.call_count == 0  # only the in-flight pair
    assert "rate limited" in (src.note or "")


async def test_archive_switched_off_uses_search_only() -> None:
    async with make_client() as client:
        src = RedditSource(_ctx(client, FakeSearch([]), archive=False), sleep=no_sleep)
        assert await src.fetch("CS2040S", None) == []
    assert src.note is None
