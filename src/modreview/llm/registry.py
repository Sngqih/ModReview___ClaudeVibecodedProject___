"""name -> LLMProvider. The only module (besides settings) that knows the concrete classes."""

from __future__ import annotations

from modreview.llm.base import LLMProvider
from modreview.settings import LLMConfig, Secrets

LLM_PROVIDER_NAMES = ("anthropic", "openai_compat")


def make_llm_provider(
    name: str, secrets: Secrets, llm_config: LLMConfig | None = None
) -> tuple[LLMProvider | None, str | None]:
    """Return ``(provider, None)`` or ``(None, reason)``."""
    if name == "anthropic":
        key = secrets.anthropic_api_key
        if not key or not key.get_secret_value().strip():
            return None, "ANTHROPIC_API_KEY not set"
        from modreview.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(key.get_secret_value()), None
    if name == "openai_compat":
        if not secrets.openai_compat_base_url:
            return None, "OPENAI_COMPAT_BASE_URL not set"
        from modreview.llm.openai_compat import OpenAICompatProvider

        key = secrets.openai_compat_api_key
        return (
            OpenAICompatProvider(
                secrets.openai_compat_base_url,
                key.get_secret_value() if key else None,
                reasoning_effort=llm_config.reasoning_effort if llm_config else None,
            ),
            None,
        )
    return None, f"unknown LLM provider {name!r}"
