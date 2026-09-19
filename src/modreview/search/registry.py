"""name -> SearchProvider. The only place that knows the concrete search classes."""

from __future__ import annotations

import importlib

import httpx

from modreview.search.base import SearchProvider
from modreview.settings import Secrets

SEARCH_PROVIDER_NAMES = ("tavily", "brave", "claude_websearch", "none")


def is_built(name: str) -> bool:
    """Whether the provider's module exists yet (Brave and Claude web search are M6)."""
    if name in ("none", "tavily"):
        return True
    try:
        importlib.import_module(f"modreview.search.{name}")
    except ImportError:
        return False
    return True


def _key(secret) -> str | None:
    value = secret.get_secret_value().strip() if secret else ""
    return value or None


def make_search_provider(
    name: str, secrets: Secrets, client: httpx.AsyncClient
) -> tuple[SearchProvider | None, str | None]:
    """Return ``(provider, None)`` or ``(None, reason)`` when it cannot be built."""
    if name == "none":
        return None, "search provider set to none"
    if name not in SEARCH_PROVIDER_NAMES:
        return None, f"unknown search provider {name!r}"
    if not is_built(name):
        return None, f"the {name} search provider is not built yet"
    if name == "tavily":
        if not (key := _key(secrets.tavily_api_key)):
            return None, "TAVILY_API_KEY not set"
        from modreview.search.tavily import TavilyProvider

        return TavilyProvider(key, client), None
    if name == "brave":
        if not (key := _key(secrets.brave_api_key)):
            return None, "BRAVE_API_KEY not set"
        from modreview.search.brave import BraveProvider  # type: ignore[import-not-found]

        return BraveProvider(key, client), None
    # claude_websearch
    if not (key := _key(secrets.anthropic_api_key)):
        return None, "ANTHROPIC_API_KEY not set"
    from modreview.search.claude_websearch import (  # type: ignore[import-not-found]
        ClaudeWebSearchProvider,
    )

    return ClaudeWebSearchProvider(key), None
