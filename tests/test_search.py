from __future__ import annotations

import httpx
import respx
from pydantic import SecretStr

from modreview.http import make_client
from modreview.search.base import site_filter_query
from modreview.search.registry import make_search_provider
from modreview.search.tavily import TAVILY_URL, TavilyProvider
from modreview.settings import Secrets
from tests.helpers import load_json


def test_site_filter_query() -> None:
    q = site_filter_query("x", ["reddit.com"], ["nusmods.com"])
    assert q == "x site:reddit.com -site:nusmods.com"


@respx.mock
async def test_tavily_parses_results() -> None:
    route = respx.post(TAVILY_URL).mock(
        return_value=httpx.Response(200, json=load_json("tavily_search.json"))
    )
    async with make_client() as client:
        hits = await TavilyProvider("tvly-test", client).search(
            '"CS2040S" NUS review', 5, exclude_domains=["nusmods.com"]
        )
    req = route.calls[0].request
    assert req.headers["authorization"] == "Bearer tvly-test"
    body = __import__("json").loads(req.content)
    assert body["exclude_domains"] == ["nusmods.com"] and body["max_results"] == 5
    assert "api_key" not in body
    assert len(hits) == 5
    assert hits[0].content and hits[0].published_at and hits[0].published_at.year == 2024
    assert hits[1].content is None and hits[1].snippet == "Short snippet only."


async def test_registry() -> None:
    async with make_client() as client:
        p, why = make_search_provider("none", Secrets(_env_file=None), client)
        assert p is None and "none" in why
        p, why = make_search_provider("tavily", Secrets(_env_file=None), client)
        assert p is None and why == "TAVILY_API_KEY not set"
        p, why = make_search_provider(
            "tavily", Secrets(_env_file=None, tavily_api_key=SecretStr("k")), client
        )
        assert p is not None and p.name == "tavily" and why is None
        p, why = make_search_provider("bogus", Secrets(_env_file=None), client)
        assert p is None and "unknown" in why
