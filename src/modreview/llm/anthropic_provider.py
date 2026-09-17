"""Anthropic provider: Models API for the dropdown, structured outputs for the report.

Structured JSON comes from ``messages.parse(output_format=schema)`` (the API's
``output_config.format`` JSON-schema mode), which every current Claude model supports and
which does not need forced tool use. See DECISIONS.md (M3) for why not tool use.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic
from pydantic import BaseModel, ValidationError

from modreview.llm.base import (
    LLMOutputError,
    LLMPermanentError,
    LLMTransientError,
    ModelInfo,
    Usage,
)

log = logging.getLogger(__name__)

RETRY_HINT = (
    "\n\nYour previous answer did not validate against the schema. "
    "Fix these problems and answer again with valid JSON only:\n{error}"
)


def _usage(u: Any) -> Usage:
    return Usage(
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
    )


def map_error(exc: Exception) -> Exception:
    """SDK exception -> our transient/permanent classes (most specific first)."""
    if isinstance(exc, anthropic.RateLimitError | anthropic.InternalServerError):
        return LLMTransientError(str(exc))
    if isinstance(exc, anthropic.AuthenticationError):
        return LLMPermanentError("Anthropic rejected the API key (401). Check ANTHROPIC_API_KEY.")
    if isinstance(exc, anthropic.PermissionDeniedError):
        return LLMPermanentError(f"Anthropic permission error (403): {exc.message}")
    if isinstance(exc, anthropic.NotFoundError):
        return LLMPermanentError(f"Model or endpoint not found (404): {exc.message}")
    if isinstance(exc, anthropic.BadRequestError):
        return LLMPermanentError(f"Bad request (400): {exc.message}")
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code == 402:
            return LLMPermanentError(f"Billing problem (402): {exc.message}")
        if exc.status_code >= 500 or getattr(exc, "type", "") == "overloaded_error":
            return LLMTransientError(f"Anthropic {exc.status_code}: {exc.message}")
        return LLMPermanentError(f"Anthropic {exc.status_code}: {exc.message}")
    if isinstance(exc, anthropic.APITimeoutError | anthropic.APIConnectionError):
        return LLMTransientError(f"Could not reach Anthropic: {exc}")
    return exc


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        client: Any = None,
        timeout_s: float = 120.0,
    ) -> None:
        # ``client`` is injectable so tests can pass a fake with .models.list / .messages.parse
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout_s, max_retries=2
        )

    async def list_models(self) -> list[ModelInfo]:
        try:
            out: list[ModelInfo] = []
            async for m in self._client.models.list():
                out.append(
                    ModelInfo(
                        id=m.id,
                        display_name=getattr(m, "display_name", None),
                        created_at=getattr(m, "created_at", None),
                        max_input_tokens=getattr(m, "max_input_tokens", None),
                        max_output_tokens=getattr(m, "max_tokens", None),
                    )
                )
        except Exception as exc:
            raise map_error(exc) from exc
        out.sort(key=lambda m: (m.created_at or 0) and m.created_at.timestamp(), reverse=True)
        return out

    async def test_connection(self) -> str:
        models = await self.list_models()
        return f"ok: key accepted, {len(models)} models available"

    async def generate_structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[BaseModel],
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> tuple[BaseModel, Usage]:
        # ``temperature`` is accepted for protocol compatibility but not sent: current Claude
        # models reject sampling parameters and the SDK's parse() has no such argument.
        total = Usage()
        prompt = user
        last_error = ""
        for attempt in range(2):
            try:
                resp = await self._client.messages.parse(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": prompt}],
                    output_format=schema,
                )
            except Exception as exc:
                raise map_error(exc) from exc
            total = total + _usage(resp.usage)
            if getattr(resp, "stop_reason", None) == "refusal":
                raise LLMPermanentError("The model refused this request (stop_reason=refusal).")
            if getattr(resp, "stop_reason", None) == "max_tokens":
                raise LLMOutputError(
                    f"Output cut off at max_tokens={max_tokens}; raise [llm].max_output_tokens."
                )
            parsed = getattr(resp, "parsed_output", None)
            if isinstance(parsed, schema):
                return parsed, total
            # Fall back to validating the text ourselves (covers fakes and odd SDK paths).
            text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
            try:
                return schema.model_validate(json.loads(text)), total
            except (ValidationError, ValueError) as exc:
                last_error = str(exc)[:2000]
                log.warning(
                    "structured output failed validation (attempt %d): %s", attempt + 1, exc
                )
                prompt = user + RETRY_HINT.format(error=last_error)
        raise LLMOutputError(f"Model output never validated after 2 attempts: {last_error}")
