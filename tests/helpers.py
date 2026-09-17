"""Shared test helpers: fixtures dir, a fake search provider, a no-op sleep."""

from __future__ import annotations

import json
from pathlib import Path

from modreview.search.base import SearchHit

FIXTURES = Path(__file__).parent / "fixtures"


def load_json(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def load_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeSearch:
    name = "fake"

    def __init__(self, hits: list[SearchHit] | None = None, *, fail: bool = False) -> None:
        self.hits = hits or []
        self.fail = fail
        self.calls: list[dict] = []

    async def search(self, query, max_results, *, include_domains=None, exclude_domains=None):
        self.calls.append(
            {
                "query": query,
                "max_results": max_results,
                "include_domains": include_domains,
                "exclude_domains": exclude_domains,
            }
        )
        if self.fail:
            raise RuntimeError("search is down")
        return list(self.hits)


async def no_sleep(_: float) -> None:
    return None
