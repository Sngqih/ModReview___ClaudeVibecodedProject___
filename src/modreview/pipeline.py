"""Orchestration: fetch -> clean -> summarise -> cache.

M2 ships the fetch stage (``gather_sources``). Cleaning, budgeting, summarising and the
cache arrive in M3/M4.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from modreview.models import ModuleFacts, ModuleNotFound, SourceDoc, SourceResult, SourceStatus
from modreview.search.registry import make_search_provider
from modreview.settings import AppConfig, Secrets, data_dir_from_env
from modreview.sources.base import Source, SourceContext, SourceUnavailable
from modreview.sources.disqus import DisqusSource
from modreview.sources.nusmods import NUSModsClient, NUSModsSource
from modreview.sources.reddit import RedditSource
from modreview.sources.websearch import WebSearchSource

log = logging.getLogger(__name__)

ProgressFn = Callable[[SourceStatus], Awaitable[None] | None]


async def _emit(progress: ProgressFn | None, status: SourceStatus) -> None:
    if progress is None:
        return
    res = progress(status)
    if asyncio.iscoroutine(res):
        await res


async def _run_source(
    source: Source, code: str, title: str | None, progress: ProgressFn | None
) -> SourceResult:
    name = source.name  # type: ignore[assignment]
    try:
        docs = await source.fetch(code, title)
        status = SourceStatus(name=name, ok=True, count=len(docs), note=source.note)
        result = SourceResult(status=status, docs=docs)
    except SourceUnavailable as exc:
        result = SourceResult(status=SourceStatus(name=name, ok=False, error=str(exc)))
    except Exception as exc:
        log.exception("source %s failed", name)
        msg = f"{type(exc).__name__}: {exc}"[:200]
        result = SourceResult(status=SourceStatus(name=name, ok=False, error=msg))
    await _emit(progress, result.status)
    return result


async def gather_sources(
    code: str,
    config: AppConfig,
    secrets: Secrets,
    client: httpx.AsyncClient,
    *,
    data_dir: Path | None = None,
    progress: ProgressFn | None = None,
    search=None,
    sleep=asyncio.sleep,
) -> tuple[ModuleFacts, list[SourceResult]]:
    """NUSMods first (the title feeds the other queries), then the rest concurrently.

    Raises ``ModuleNotFound`` if NUSMods has never heard of the code. Any other source
    failing is recorded in its ``SourceStatus`` and never raises.
    """
    data_dir = data_dir or data_dir_from_env()
    nusmods = NUSModsSource(NUSModsClient(client, data_dir=data_dir))
    try:
        nus_docs = await nusmods.fetch(code, None)
    except ModuleNotFound:
        await _emit(progress, SourceStatus(name="nusmods", ok=False, error="module not found"))
        raise
    assert nusmods.facts is not None
    facts = nusmods.facts
    results = [SourceResult(status=SourceStatus(name="nusmods", ok=True, count=1), docs=nus_docs)]
    await _emit(progress, results[0].status)

    if search is None and (config.sources.reddit or config.sources.websearch):
        search, reason = make_search_provider(config.search.provider, secrets, client)
        if search is None:
            log.info("search provider unavailable: %s", reason)
    ctx = SourceContext(client=client, config=config, secrets=secrets, search=search)

    sources: list[Source] = []
    if config.sources.disqus:
        sources.append(DisqusSource(ctx))
    if config.sources.reddit:
        sources.append(RedditSource(ctx, sleep=sleep))
    if config.sources.websearch:
        sources.append(WebSearchSource(ctx))

    others = await asyncio.gather(
        *(_run_source(s, code, facts.title, progress) for s in sources), return_exceptions=True
    )
    for s, r in zip(sources, others, strict=True):
        if isinstance(r, BaseException):  # _run_source already catches; belt and braces
            r = SourceResult(status=SourceStatus(name=s.name, ok=False, error=str(r)[:200]))
        results.append(r)
    return facts, results


def all_docs(results: list[SourceResult]) -> list[SourceDoc]:
    return [d for r in results for d in r.docs]
