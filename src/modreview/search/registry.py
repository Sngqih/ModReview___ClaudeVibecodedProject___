"""name -> SearchProvider. The only place that knows the concrete search classes."""

from __future__ import annotations

import httpx

from modreview.search.base import SearchProvider
from modreview.settings import Secrets

SEARCH_PROVIDER_NAMES = ("tavily", "brave", "claude_websearch", "none")


def make_search_provider(
    name: str, secrets: Secrets, client: httpx.AsyncClient
) -> tuple[SearchProvider | None, str | None]:
    """Return ``(provider, None)`` or ``(None, reason)`` when it cannot be built."""
    if name == "none":
        return None, "search provider set to none"
    if name == "tavily":
        if not secrets.tavily_api_key or not secrets.tavily_api_key.get_secret_value().strip():
            return None, "TAVILY_API_KEY not set"
        from modreview.search.tavily import TavilyProvider

        return TavilyProvider(secrets.tavily_api_key.get_secret_value(), client), None
    if name == "brave":
        if not secrets.brave_api_key or not secrets.brave_api_key.get_secret_value().strip():
            return None, "BRAVE_API_KEY not set"
        from modreview.search.brave import BraveProvider

        return BraveProvider(secrets.brave_api_key.get_secret_value(), client), None
    if name == "claude_websearch":
        if (
            not secrets.anthropic_api_key
            or not secrets.anthropic_api_key.get_secret_value().strip()
        ):
            return None, "ANTHROPIC_API_KEY not set"
        from modreview.search.claude_websearch import ClaudeWebSearchProvider

        return ClaudeWebSearchProvider(secrets.anthropic_api_key.get_secret_value()), None
    return None, f"unknown search provider {name!r}"
