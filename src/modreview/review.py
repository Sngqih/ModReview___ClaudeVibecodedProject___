"""Result and progress models shared by the pipeline, the cache and the UI."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from modreview.llm.base import Usage
from modreview.models import ModuleFacts, ReviewReport, SourceDoc, SourceStatus

Stage = Literal["cache_hit", "fetching", "cleaning", "summarising", "done", "facts_only", "error"]


class ReviewResult(BaseModel):
    module_code: str
    facts: ModuleFacts
    report: ReviewReport | None = None
    docs: list[SourceDoc] = Field(default_factory=list)  # the docs the report may cite
    statuses: list[SourceStatus] = Field(default_factory=list)
    provider: str | None = None
    model: str | None = None  # the model that was requested (part of the cache key)
    answered_by: str | None = None  # the model that actually wrote it (differs on fallback)
    prompt_version: str = ""
    usage: Usage | None = None
    cost_usd: float | None = None
    fallback_used: bool = False
    generated_at: datetime
    from_cache: bool = False
    docs_collected: int = 0
    docs_too_old: int = 0  # left out by the recency cutoff
    prompt_chars: int = 0
    dropped_citations: int = 0
    # Set when there is no report: "no student opinions found", or the LLM error message.
    facts_only_reason: str | None = None
    llm_error: str | None = None

    def doc_index(self) -> dict[str, int]:
        """doc id -> citation number (1-based) in the order the docs are listed."""
        return {d.id: i for i, d in enumerate(self.docs, start=1)}


class ProgressEvent(BaseModel):
    kind: Literal["source", "stage"]
    stage: Stage | None = None
    message: str = ""
    status: SourceStatus | None = None
