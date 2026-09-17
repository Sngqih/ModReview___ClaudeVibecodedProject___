from __future__ import annotations

import httpx
import pytest
import respx
from pydantic import SecretStr

from modreview.http import make_client
from modreview.settings import AppConfig, Secrets
from modreview.sources.base import SourceContext, SourceUnavailable
from modreview.sources.disqus import DISQUS_LIST_POSTS, DisqusSource, strip_html, thread_selectors
from tests.helpers import load_json


def _ctx(client: httpx.AsyncClient, key: str | None = "pub-key") -> SourceContext:
    secrets = Secrets(_env_file=None, disqus_api_key=SecretStr(key) if key else None)
    return SourceContext(client=client, config=AppConfig(), secrets=secrets)


def test_strip_html() -> None:
    assert strip_html("<p>Hi &quot;there&quot;<br>next</p>") == 'Hi "there"\nnext'


def test_selectors_order() -> None:
    sel = thread_selectors("CS2040S")
    assert sel[0] == ("thread:ident", "CS2040S")
    assert sel[1][1].endswith("/courses/CS2040S/reviews")


async def test_missing_key_is_unavailable() -> None:
    async with make_client() as client:
        with pytest.raises(SourceUnavailable, match="DISQUS_API_KEY"):
            await DisqusSource(_ctx(client, key=None)).fetch("CS2040S")


@respx.mock
async def test_paginates_and_filters() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        assert params["forum"] == "nusmods-prod"
        assert params["thread:ident"] == "CS2040S"
        if params.get("cursor"):
            return httpx.Response(200, json=load_json("disqus_listPosts_p2.json"))
        return httpx.Response(200, json=load_json("disqus_listPosts_p1.json"))

    route = respx.get(DISQUS_LIST_POSTS).mock(side_effect=handler)
    async with make_client() as client:
        src = DisqusSource(_ctx(client))
        docs = await src.fetch("CS2040S")
    assert route.call_count == 2
    # p1 has 4 posts: one too short, one deleted -> 2 kept; p2 adds 1
    assert [d.author for d in docs] == ["cs_kid", "anon2023", "senior"]
    assert docs[0].score == 12 and docs[0].published_at is not None
    assert docs[0].published_at.year == 2024
    assert docs[1].text.startswith('Honestly "ok" module.')  # html stripped, entity unescaped
    assert "<" not in docs[1].text
    assert docs[0].url == "https://nusmods.com/courses/CS2040S/reviews#comment-6000000001"
    assert src.note is None and src.selector_used == ("thread:ident", "CS2040S")


@respx.mock
async def test_falls_back_to_thread_link() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if "thread:ident" in params:
            return httpx.Response(400, json=load_json("disqus_not_found.json"))
        if params.get("thread:link", "").endswith("/courses/CS2040S/reviews"):
            return httpx.Response(200, json=load_json("disqus_listPosts_p2.json"))
        return httpx.Response(400, json=load_json("disqus_not_found.json"))

    respx.get(DISQUS_LIST_POSTS).mock(side_effect=handler)
    async with make_client() as client:
        src = DisqusSource(_ctx(client))
        docs = await src.fetch("CS2040S")
    assert len(docs) == 1
    assert src.selector_used == ("thread:link", "https://nusmods.com/courses/CS2040S/reviews")
    assert src.note == "found via thread:link"


@respx.mock
async def test_no_thread_anywhere_returns_empty() -> None:
    respx.get(DISQUS_LIST_POSTS).mock(
        return_value=httpx.Response(400, json=load_json("disqus_not_found.json"))
    )
    async with make_client() as client:
        assert await DisqusSource(_ctx(client)).fetch("ZZ9999") == []
