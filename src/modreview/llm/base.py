"""``LLMProvider`` protocol, ``Usage``/``ModelInfo`` models, error classes, cost estimate."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from modreview.settings import Pricing


class ModelInfo(BaseModel):
    id: str
    display_name: str | None = None
    created_at: datetime | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )


class LLMError(Exception):
    """Base for provider failures."""


class LLMTransientError(LLMError):
    """Rate limit, overloaded, 5xx, timeout, network. Worth trying the fallback model."""


class LLMPermanentError(LLMError):
    """Bad key, no credits, bad request, unknown model. The fallback would fail the same way."""


class LLMOutputError(LLMError):
    """The model answered but the output never validated against the schema."""


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    async def list_models(self) -> list[ModelInfo]: ...

    async def generate_structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[BaseModel],
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> tuple[BaseModel, Usage]: ...

    async def test_connection(self) -> str:
        """Human-readable success line, or raise an ``LLMError``."""
        ...


def estimate_cost(usage: Usage, pricing: dict[str, Pricing], model: str) -> float | None:
    """USD for one report, or None when the model is not in the price table."""
    p = pricing.get(model)
    if p is None:
        return None
    # Cache reads are billed at ~10% and cache writes at ~125% of the input price.
    inp = (
        usage.input_tokens * p.input_per_mtok
        + usage.cache_read_tokens * p.input_per_mtok * 0.1
        + usage.cache_write_tokens * p.input_per_mtok * 1.25
    )
    out = usage.output_tokens * p.output_per_mtok
    return round((inp + out) / 1_000_000, 6)
