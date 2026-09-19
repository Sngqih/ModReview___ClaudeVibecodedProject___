"""Run the LLM with fallback and return the report plus bookkeeping."""

from __future__ import annotations

import logging

from pydantic import BaseModel

from modreview.llm.base import (
    LLMError,
    LLMOutputError,
    LLMPermanentError,
    LLMProvider,
    LLMTransientError,
    Usage,
    estimate_cost,
)
from modreview.llm.prompt import PROMPT_VERSION, build_user_message, load_system_prompt
from modreview.llm.registry import make_llm_provider
from modreview.models import ModuleFacts, ReviewReport, SourceDoc
from modreview.settings import AppConfig, Secrets

log = logging.getLogger(__name__)


class SummaryResult(BaseModel):
    report: ReviewReport
    provider: str
    model: str
    prompt_version: str = PROMPT_VERSION
    usage: Usage
    cost_usd: float | None = None
    fallback_used: bool = False
    prompt_chars: int = 0


def resolve_providers(
    config: AppConfig,
    secrets: Secrets,
    model_override: str | None = None,
    provider_override: str | None = None,
) -> list[tuple[LLMProvider, str]]:
    """[(provider, model)] in the order to try: primary, then fallback if configured."""
    primary_name = provider_override or config.llm.provider
    primary, why = make_llm_provider(primary_name, secrets, config.llm)
    if primary is None:
        raise LLMPermanentError(why or f"provider {primary_name!r} unavailable")
    chain = [(primary, model_override or config.llm.model)]
    fb = config.llm.fallback
    if fb and not provider_override and (fb.provider, fb.model) != (primary_name, chain[0][1]):
        fallback, why = make_llm_provider(fb.provider, secrets, config.llm)
        if fallback is None:
            log.info("fallback provider unavailable: %s", why)
        else:
            chain.append((fallback, fb.model))
    return chain


async def summarise(
    facts: ModuleFacts,
    docs: list[SourceDoc],
    config: AppConfig,
    secrets: Secrets,
    *,
    model_override: str | None = None,
    provider_override: str | None = None,
    chain: list[tuple[LLMProvider, str]] | None = None,
) -> SummaryResult:
    """Call the primary model; on transient or repeated-bad-output errors try the fallback.

    Permanent errors (bad key, no credits, bad request) are raised immediately since the
    fallback would fail the same way.
    """
    chain = chain or resolve_providers(config, secrets, model_override, provider_override)
    system = load_system_prompt()
    user = build_user_message(facts, docs)
    last: LLMError | None = None
    for i, (provider, model) in enumerate(chain):
        try:
            report, usage = await provider.generate_structured(
                system=system,
                user=user,
                schema=ReviewReport,
                model=model,
                max_tokens=config.llm.max_output_tokens,
                temperature=config.llm.temperature,
            )
        except (LLMTransientError, LLMOutputError) as exc:
            last = exc
            log.warning(
                "%s/%s failed (%s); %s",
                provider.name,
                model,
                exc,
                "trying fallback" if i + 1 < len(chain) else "no fallback left",
            )
            continue
        assert isinstance(report, ReviewReport)
        return SummaryResult(
            report=report,
            provider=provider.name,
            model=model,
            usage=usage,
            cost_usd=estimate_cost(usage, config.pricing, model),
            fallback_used=i > 0,
            prompt_chars=len(system) + len(user),
        )
    assert last is not None
    raise last
