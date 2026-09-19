"""Orchestration: validate -> cache -> fetch -> clean -> budget -> summarise -> check -> cache.

``gather_sources`` runs the sources; ``run_review`` is the whole pipeline the UI calls.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx

from modreview.cache import ReviewCache
from modreview.clean import clean_docs, enforce_citations, select_docs
from modreview.http import make_client
from modreview.llm.base import LLMError, LLMProvider
from modreview.llm.prompt import PROMPT_VERSION, build_user_message, load_system_prompt
from modreview.llm.summarise import summarise
from modreview.models import (
    ModuleFacts,
    ModuleNotFound,
    SourceDoc,
    SourceResult,
    SourceStatus,
    normalise_module_code,
)
from modreview.review import ProgressEvent, ReviewResult
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


# --------------------------------------------------------------------------- full pipeline

EventFn = Callable[[ProgressEvent], Awaitable[None] | None]

SOURCE_LABELS = {
    "nusmods": ("NUSMods", "module facts"),
    "disqus": ("NUSMods reviews", "reviews"),
    "reddit": ("Reddit", "threads"),
    "web": ("Web", "pages"),
}


async def _emit_event(fn: EventFn | None, event: ProgressEvent) -> None:
    if fn is None:
        return
    res = fn(event)
    if asyncio.iscoroutine(res):
        await res


def source_message(s: SourceStatus) -> str:
    name, unit = SOURCE_LABELS.get(s.name, (s.name, "docs"))
    if not s.ok:
        return f"{name}: unavailable ({s.error})"
    msg = f"{name}: {s.count} {unit}"
    return f"{msg} ({s.note})" if s.note else msg


def fit_to_token_limit(
    facts: ModuleFacts, docs: list[SourceDoc], max_total_chars: int, max_input_tokens: int
) -> tuple[list[SourceDoc], int]:
    """Budget docs by characters, then shrink further until the prompt passes the cost guard
    (estimated tokens = characters / 4)."""
    system_chars = len(load_system_prompt())
    budget = max_total_chars
    selected = select_docs(docs, budget)
    size = system_chars + len(build_user_message(facts, selected))
    while size // 4 > max_input_tokens and budget > 2000:
        budget = int(budget * 0.8)
        selected = select_docs(docs, budget)
        size = system_chars + len(build_user_message(facts, selected))
    return selected, size


async def run_review(
    raw_code: str,
    config: AppConfig,
    secrets: Secrets,
    cache: ReviewCache,
    *,
    model: str | None = None,
    provider: str | None = None,
    force: bool = False,
    progress: EventFn | None = None,
    client: httpx.AsyncClient | None = None,
    search=None,
    chain: list[tuple[LLMProvider, str]] | None = None,
    sleep=asyncio.sleep,
    data_dir: Path | None = None,
) -> ReviewResult:
    """Build (or load from cache) one module review.

    Raises ``InvalidModuleCode`` before any network call and ``ModuleNotFound`` when NUSMods
    does not know the code. Source failures and LLM failures do not raise: they come back
    inside the ``ReviewResult`` so the page can still show the official facts.
    """
    code = normalise_module_code(raw_code)
    provider_name = provider or config.llm.provider
    model_name = model or config.llm.model

    if not force:
        hit = cache.get(
            code, provider_name, model_name, PROMPT_VERSION, config.limits.cache_ttl_days
        )
        if hit is not None:
            await _emit_event(
                progress,
                ProgressEvent(kind="stage", stage="cache_hit", message="Loaded from cache"),
            )
            return hit.model_copy(update={"from_cache": True})

    await _emit_event(
        progress, ProgressEvent(kind="stage", stage="fetching", message="Collecting sources")
    )

    async def on_source(s: SourceStatus) -> None:
        await _emit_event(
            progress, ProgressEvent(kind="source", status=s, message=source_message(s))
        )

    own_client = client is None
    client = client or make_client(config.limits.http_timeout_s)
    try:
        facts, results = await gather_sources(
            code,
            config,
            secrets,
            client,
            data_dir=data_dir,
            progress=on_source,
            search=search,
            sleep=sleep,
        )
    finally:
        if own_client:
            await client.aclose()

    statuses = [r.status for r in results]
    collected = all_docs(results)
    docs = clean_docs(collected, config.limits.max_chars_per_doc)
    base = {
        "module_code": code,
        "facts": facts,
        "statuses": statuses,
        "generated_at": datetime.now(UTC),
        "docs_collected": len(collected),
        "prompt_version": PROMPT_VERSION,
    }
    log.debug(
        "%s: sources %s; %d docs collected, %d after cleaning",
        code,
        {s.name: s.count for s in statuses},
        len(collected),
        len(docs),
    )

    if not any(d.source != "nusmods" for d in docs):
        await _emit_event(
            progress,
            ProgressEvent(kind="stage", stage="facts_only", message="No student opinions found"),
        )
        return ReviewResult(
            **base,
            docs=docs,
            facts_only_reason=(
                "No student reviews, Reddit threads or web pages were found, so there is "
                "nothing to summarise. Showing the official facts only."
            ),
        )

    selected, prompt_chars = fit_to_token_limit(
        facts, docs, config.limits.max_total_chars, config.limits.max_input_tokens_per_report
    )
    log.debug(
        "%s: %d docs selected, prompt %d chars (~%d tokens)",
        code,
        len(selected),
        prompt_chars,
        prompt_chars // 4,
    )
    await _emit_event(
        progress,
        ProgressEvent(
            kind="stage",
            stage="summarising",
            message=f"Summarising {len(selected)} documents with {model_name}",
        ),
    )
    try:
        res = await summarise(
            facts,
            selected,
            config,
            secrets,
            model_override=model,
            provider_override=provider,
            chain=chain,
        )
    except LLMError as exc:
        log.warning("%s: summarising failed: %s", code, exc)
        await _emit_event(progress, ProgressEvent(kind="stage", stage="error", message=str(exc)))
        return ReviewResult(
            **base,
            docs=selected,
            provider=provider_name,
            model=model_name,
            prompt_chars=prompt_chars,
            llm_error=str(exc),
        )

    report, dropped = enforce_citations(res.report, {d.id for d in selected}, code)
    if dropped:
        log.info("%s: dropped %d citation(s) that matched no document", code, dropped)
    result = ReviewResult(
        **base,
        report=report,
        docs=selected,
        provider=res.provider,
        model=res.model,
        usage=res.usage,
        cost_usd=res.cost_usd,
        fallback_used=res.fallback_used,
        prompt_chars=res.prompt_chars,
        dropped_citations=dropped,
    )
    # Cache under the requested provider/model, so a fallback answer still serves the same
    # request next time. The footer shows the model that actually answered via fallback_used.
    cache.put(result.model_copy(update={"provider": provider_name, "model": model_name}))
    await _emit_event(progress, ProgressEvent(kind="stage", stage="done", message="Done"))
    return result
