"""Tavily search provider (default). Returns cleaned page content, saving a fetch."""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

from modreview.search.base import SearchHit

log = logging.getLogger(__name__)

TAVILY_URL = "https://api.tavily.com/search"


class TavilyProvider:
    name = "tavily"

    def __init__(self, api_key: str, client: httpx.AsyncClient) -> None:
        self._key = api_key
        self._client = client

    async def search(
        self,
        query: str,
        max_results: int,
        *,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
    ) -> list[SearchHit]:
        body: dict = {
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
            "include_raw_content": False,
        }
        if include_domains:
            body["include_domains"] = include_domains
        if exclude_domains:
            body["exclude_domains"] = exclude_domains
        resp = await self._client.post(
            TAVILY_URL, json=body, headers={"Authorization": f"Bearer {self._key}"}
        )
        resp.raise_for_status()
        data = resp.json()
        hits: list[SearchHit] = []
        for r in data.get("results", []):
            url = r.get("url")
            if not url:
                continue
            published = None
            if raw := r.get("published_date"):
                try:
                    published = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                except ValueError:
                    published = None
            content = r.get("content") or ""
            hits.append(
                SearchHit(
                    url=url,
                    title=r.get("title"),
                    snippet=content[:500],
                    content=content if len(content) > 500 else None,
                    published_at=published,
                    score=r.get("score"),
                )
            )
        return hits

    async def test_connection(self) -> str:
        hits = await self.search("NUS CS2040S", 1)
        return f"ok, {len(hits)} hit(s) for a test query"
