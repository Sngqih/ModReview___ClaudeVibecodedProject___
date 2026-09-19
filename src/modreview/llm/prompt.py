"""Prompt assembly. The system prompt is a versioned file; docs are wrapped in <doc> tags."""

from __future__ import annotations

import html
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from modreview.dates import ay_label
from modreview.models import ModuleFacts, SourceDoc

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
# v2 (2026-09-19): period/estimated attributes, today's academic year, recency-first rules,
# course_page and blog source types. Changing this retires cached reports (cache key).
PROMPT_VERSION = "summarise_v2"


@lru_cache(maxsize=4)
def load_system_prompt(version: str = PROMPT_VERSION) -> str:
    return (PROMPTS_DIR / f"{version}.md").read_text(encoding="utf-8").strip()


def _attr(value: object) -> str:
    return html.escape(str(value), quote=True)


def wrap_doc(doc: SourceDoc) -> str:
    if doc.published_at:
        date = doc.published_at.date().isoformat()
        period = f' period="{ay_label(doc.published_at, with_sem=True)}"'
        estimated = ' date_estimated="yes"' if doc.date_estimated else ""
    else:
        date, period, estimated = "unknown", "", ""
    score = "" if doc.score is None else f' score="{doc.score}"'
    title = f' title="{_attr(doc.title)}"' if doc.title else ""
    # A document must not be able to close its own delimiter.
    body = doc.text.replace("</doc", "<\\/doc")
    return (
        f'<doc id="{doc.id}" source="{doc.source}" date="{date}"{period}{estimated}{score}'
        f'{title} url="{_attr(doc.url)}">\n{body}\n</doc>'
    )


def build_user_message(
    facts: ModuleFacts, docs: list[SourceDoc], now: datetime | None = None
) -> str:
    now = now or datetime.now(UTC)
    n_opinion = sum(1 for d in docs if d.source != "nusmods")
    header = (
        f"Module: {facts.module_code} {facts.title} ({facts.module_credit} MCs, "
        f"NUSMods data for {facts.acad_year}).\n"
        f"Today is {now:%d %b %Y}, which is {ay_label(now, with_sem=True)}.\n"
        f"You are given {len(docs)} documents ({n_opinion} of them student opinions or "
        "course pages), newest first within each source. Write the review report for this "
        "module.\n\n"
        "<documents>\n"
    )
    body = "\n\n".join(wrap_doc(d) for d in docs)
    return header + body + "\n</documents>"


def estimate_tokens(text: str) -> int:
    return len(text) // 4
