from __future__ import annotations

import httpx
import pytest
import respx

from modreview.http import make_client
from modreview.search.base import SearchHit
from modreview.settings import AppConfig, Secrets
from modreview.sources.base import SourceContext, SourceUnavailable
from modreview.sources.reddit import (
    RedditSource,
    build_thread_doc,
    flatten_comments,
    is_nus_thread,
    thread_key,
)
from tests.helpers import FakeSearch, load_json, no_sleep

T1 = "https://www.reddit.com/r/nus/comments/abc123/cs2040s_how_hard/"
T2 = "https://www.reddit.com/r/nus/comments/def456/cs2040s_vs_cs2040/"
NOT_NUS = "https://www.reddit.com/r/singapore/comments/zzz999/unrelated/"
HITS = [
    SearchHit(url=T1, title="CS2040S how hard is it really?", snippet="snippet one"),
    SearchHit(url=T1 + "?utm=1", title="dupe", snippet="dupe"),
    SearchHit(url=NOT_NUS, title="not nus", snippet="x"),
    SearchHit(url=T2, title="CS2040S vs CS2040", snippet="Take S if you can, the PS teach more."),
]


def _ctx(client, search) -> SourceContext:
    return SourceContext(
        client=client, config=AppConfig(), secrets=Secrets(_env_file=None), search=search
    )


def test_thread_key_and_subreddit_filter() -> None:
    assert thread_key(T1) == "abc123"
    assert thread_key("https://example.com") is None
    assert is_nus_thread(T1) and not is_nus_thread(NOT_NUS)


def test_flatten_comments_and_top_by_score() -> None:
    data = load_json("reddit_thread.json")
    comments = flatten_comments(data[1])
    assert [c["score"] for c in comments] == [30, 55, 10]  # nested reply included, deleted dropped
    doc = build_thread_doc(data[0]["data"]["children"][0]["data"], comments, T1, 6000)
    lines = doc.text.splitlines()
    assert lines[0] == "Post: CS2040S how hard is it really?"
    assert "[55 points] Agree." in doc.text
    assert doc.text.index("[55 points]") < doc.text.index("[30 points]")
    assert doc.score == 45 and doc.published_at is not None


def test_build_thread_doc_respects_char_budget() -> None:
    data = load_json("reddit_thread.json")
    comments = flatten_comments(data[1])
    doc = build_thread_doc(data[0]["data"]["children"][0]["data"], comments, T1, 150)
    assert len(doc.text) <= 200 and "[10 points]" not in doc.text


async def test_no_search_provider_is_unavailable() -> None:
    async with make_client() as client:
        with pytest.raises(SourceUnavailable):
            await RedditSource(_ctx(client, None), sleep=no_sleep).fetch("CS2040S", "DSA")


@respx.mock
async def test_json_ok_then_blocked_falls_back_to_snippets() -> None:
    respx.get(T1.rstrip("/") + ".json").mock(
        return_value=httpx.Response(200, json=load_json("reddit_thread.json"))
    )
    blocked = respx.get(T2.rstrip("/") + ".json").mock(
        return_value=httpx.Response(403, text="<html>blocked</html>")
    )
    search = FakeSearch(HITS)
    async with make_client() as client:
        src = RedditSource(_ctx(client, search), sleep=no_sleep)
        docs = await src.fetch("CS2040S", "Data Structures and Algorithms")
    assert len(docs) == 2  # dupe and r/singapore dropped
    assert "[55 points]" in docs[0].text
    assert docs[1].text == "Post: CS2040S vs CS2040\nTake S if you can, the PS teach more."
    assert src.note and "1 of 2 from search snippets" in src.note
    assert blocked.call_count == 1
    assert all(c["include_domains"] == ["reddit.com"] for c in search.calls)
    assert len(search.calls) == 2


@respx.mock
async def test_blocked_first_thread_stops_further_json_calls() -> None:
    r1 = respx.get(T1.rstrip("/") + ".json").mock(return_value=httpx.Response(403))
    r2 = respx.get(T2.rstrip("/") + ".json").mock(return_value=httpx.Response(200, json=[]))
    async with make_client() as client:
        src = RedditSource(_ctx(client, FakeSearch(HITS)), sleep=no_sleep)
        docs = await src.fetch("CS2040S", None)
    assert len(docs) == 2 and r1.call_count == 1 and r2.call_count == 0
    assert "2 of 2" in (src.note or "")


async def test_search_failure_yields_empty() -> None:
    async with make_client() as client:
        src = RedditSource(_ctx(client, FakeSearch(fail=True)), sleep=no_sleep)
        assert await src.fetch("CS2040S", None) == []
