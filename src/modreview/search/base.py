"""``SearchProvider`` protocol."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class SearchHit(BaseModel):
    url: str
    title: str | None = None
    snippet: str = ""
    content: str | None = None  # cleaned page content if the provider returns it
    published_at: datetime | None = None
    score: float | None = None


@runtime_checkable
class SearchProvider(Protocol):
    name: str

    async def search(
        self,
        query: str,
        max_results: int,
        *,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
    ) -> list[SearchHit]: ...


def site_filter_query(
    query: str,
    include_domains: list[str] | None,
    exclude_domains: list[str] | None,
) -> str:
    """For providers without native domain filters: fold them into the query string."""
    parts = [query]
    if include_domains:
        parts.append(" OR ".join(f"site:{d}" for d in include_domains))
    for d in exclude_domains or []:
        parts.append(f"-site:{d}")
    return " ".join(parts)
