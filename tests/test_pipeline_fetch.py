from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from pydantic import SecretStr

from modreview.http import make_client
from modreview.models import ModuleNotFound, SourceStatus
from modreview.pipeline import all_docs, gather_sources
from modreview.search.base import SearchHit
from modreview.settings import AppConfig, Secrets
from modreview.sources.disqus import DISQUS_LIST_POSTS
from modreview.sources.nusmods import API_BASE
from tests.helpers import FakeSearch, load_json, no_sleep

BLOG = "https://example-blog.sg/cs2040s-review"


@respx.mock
async def test_gather_all_sources_with_partial_failure(tmp_path: Path) -> None:
    respx.get(f"{API_BASE}/2026-2027/modules/CS2040S.json").mock(
        return_value=httpx.Response(200, json=load_json("nusmods_CS2040S.json"))
    )
    respx.get(DISQUS_LIST_POSTS).mock(return_value=httpx.Response(500))  # persistent failure
    reddit = "https://www.reddit.com/r/nus/comments/abc123/x/"
    respx.get(reddit.rstrip("/") + ".json").mock(return_value=httpx.Response(403))
    hits = [
        SearchHit(url=BLOG, title="Blog", snippet="s", content="CS2040S " + "words " * 100),
        SearchHit(url=reddit, title="thread", snippet="reddit snippet about CS2040S"),
    ]
    secrets = Secrets(_env_file=None, disqus_api_key=SecretStr("k"))
    events: list[SourceStatus] = []

    async def progress(s: SourceStatus) -> None:
        events.append(s)

    import modreview.http as http_mod

    async with make_client() as client:
        # make the disqus retry loop instant
        orig = http_mod.asyncio.sleep
        http_mod.asyncio.sleep = no_sleep  # type: ignore[assignment]
        try:
            facts, results = await gather_sources(
                "CS2040S",
                AppConfig(),
                secrets,
                client,
                data_dir=tmp_path,
                progress=progress,
                search=FakeSearch(hits),
                sleep=no_sleep,
            )
        finally:
            http_mod.asyncio.sleep = orig

    assert facts.title == "Data Structures and Algorithms"
    by_name = {r.status.name: r for r in results}
    assert by_name["nusmods"].status.ok and by_name["nusmods"].status.count == 1
    assert not by_name["disqus"].status.ok and "500" in (by_name["disqus"].status.error or "")
    assert by_name["reddit"].status.ok and by_name["reddit"].status.count == 1
    assert "snippets" in (by_name["reddit"].status.note or "")
    assert by_name["web"].status.ok and by_name["web"].status.count == 1
    assert len(all_docs(results)) == 3
    assert [e.name for e in events][0] == "nusmods" and len(events) == 4


@respx.mock
async def test_gather_missing_keys_marks_unavailable(tmp_path: Path) -> None:
    respx.get(url__regex=rf"{API_BASE}/.*/modules/MA1521\.json").mock(
        return_value=httpx.Response(200, json=load_json("nusmods_MA1521.json"))
    )
    async with make_client() as client:
        _, results = await gather_sources(
            "MA1521", AppConfig(), Secrets(_env_file=None), client, data_dir=tmp_path
        )
    by_name = {r.status.name: r.status for r in results}
    assert by_name["disqus"].error == "DISQUS_API_KEY not set"
    assert "search provider" in (by_name["reddit"].error or "")
    assert "search provider" in (by_name["web"].error or "")
    assert by_name["nusmods"].ok


@respx.mock
async def test_gather_unknown_module_raises(tmp_path: Path) -> None:
    respx.get(url__regex=rf"{API_BASE}/.*/modules/ZZ9999\.json").mock(
        return_value=httpx.Response(404)
    )
    async with make_client() as client:
        with pytest.raises(ModuleNotFound):
            await gather_sources(
                "ZZ9999", AppConfig(), Secrets(_env_file=None), client, data_dir=tmp_path
            )
