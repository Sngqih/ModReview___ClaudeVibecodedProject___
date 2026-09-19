"""Cleaning, ranking, budgeting and the citation post-check. Pure functions, no I/O."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime

from modreview.models import Claim, LecturerNote, ReviewReport, SourceDoc

# Owner's choice: after recency and score, Disqus beats Reddit beats Web.
SOURCE_PRIORITY = {"nusmods": 0, "disqus": 1, "reddit": 2, "web": 3}
# Undated docs (most web pages) rank as if they were about three years old.
UNDATED_AGE_YEARS = 3
# Budget pass 1 keeps this many of the best docs from each opinion source, so one prolific
# source (97 Disqus reviews for MA1521) cannot crowd Reddit and the web out entirely.
PER_SOURCE_FLOOR = 3
TRUNCATION_MARK = " [...]"

_WS_RE = re.compile(r"[ \t ]+")
_NL_RE = re.compile(r"\n{3,}")
_NORM_RE = re.compile(r"[^a-z0-9]+")


# --------------------------------------------------------------------------- clean


def normalise_whitespace(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(_WS_RE.sub(" ", line).strip() for line in text.split("\n"))
    return _NL_RE.sub("\n\n", text).strip()


def content_hash(text: str) -> str:
    """Hash of the text with case, punctuation and spacing removed (near-duplicate key)."""
    return hashlib.sha1(_NORM_RE.sub("", text.lower()).encode()).hexdigest()


def truncate(text: str, limit: int) -> str:
    """Cut at a paragraph or sentence boundary at or before ``limit`` characters."""
    if len(text) <= limit:
        return text
    cut = text[: limit - len(TRUNCATION_MARK)]
    for sep in ("\n\n", "\n", ". "):
        idx = cut.rfind(sep)
        if idx >= limit * 0.6:
            cut = cut[: idx + (1 if sep == ". " else 0)]
            break
    return cut.rstrip() + TRUNCATION_MARK


def clean_docs(docs: list[SourceDoc], max_chars_per_doc: int) -> list[SourceDoc]:
    """Normalise whitespace, drop empty and near-duplicate docs, truncate long ones.

    When two docs share content, the first one wins, so callers should pass higher-priority
    sources first (NUSMods, Disqus, Reddit, Web - the order ``gather_sources`` returns).
    """
    seen: set[str] = set()
    out: list[SourceDoc] = []
    for d in docs:
        text = normalise_whitespace(d.text)
        if not text:
            continue
        h = content_hash(text)
        if h in seen:
            continue
        seen.add(h)
        out.append(d.model_copy(update={"text": truncate(text, max_chars_per_doc)}))
    return out


# --------------------------------------------------------------------------- rank + budget


def _age_years(d: SourceDoc, now: datetime) -> int:
    if d.published_at is None:
        return UNDATED_AGE_YEARS
    pub = d.published_at if d.published_at.tzinfo else d.published_at.replace(tzinfo=UTC)
    return max(0, (now - pub).days // 365)


def rank_key(d: SourceDoc, now: datetime) -> tuple:
    """Sort key: newer year-bucket first, then higher score, then source priority."""
    return (_age_years(d, now), -(d.score or 0), SOURCE_PRIORITY.get(d.source, 9), d.id)


def rank_docs(docs: list[SourceDoc], now: datetime | None = None) -> list[SourceDoc]:
    now = now or datetime.now(UTC)
    return sorted(docs, key=lambda d: rank_key(d, now))


def select_docs(
    docs: list[SourceDoc],
    max_total_chars: int,
    *,
    now: datetime | None = None,
    per_source_floor: int = PER_SOURCE_FLOOR,
) -> list[SourceDoc]:
    """Pick docs until ``max_total_chars``. NUSMods facts are always kept.

    Pass 1 takes the top ``per_source_floor`` docs of each opinion source; pass 2 fills the
    rest of the budget in global rank order, skipping any doc that no longer fits.
    """
    facts = [d for d in docs if d.source == "nusmods"]
    ranked = rank_docs([d for d in docs if d.source != "nusmods"], now)
    chosen: dict[str, SourceDoc] = {d.id: d for d in facts}
    used = sum(len(d.text) for d in facts)

    def try_add(d: SourceDoc) -> None:
        nonlocal used
        if d.id not in chosen and used + len(d.text) <= max_total_chars:
            chosen[d.id] = d
            used += len(d.text)

    for src in ("disqus", "reddit", "web"):
        for d in [d for d in ranked if d.source == src][:per_source_floor]:
            try_add(d)
    for d in ranked:
        try_add(d)

    order = {d.id: i for i, d in enumerate(ranked)}
    opinions = sorted(
        (d for d in chosen.values() if d.source != "nusmods"), key=lambda d: order[d.id]
    )
    return facts + opinions


# --------------------------------------------------------------------------- citations


def enforce_citations(
    report: ReviewReport, valid_ids: set[str], module_code: str
) -> tuple[ReviewReport, int]:
    """Drop citation ids that match no doc, then drop claims left with no citations.

    Returns the cleaned report and how many ids were removed. Also pins ``module_code`` to
    the code that was actually requested.
    """
    removed = 0

    def fix(claims: list[Claim]) -> list[Claim]:
        nonlocal removed
        out = []
        for c in claims:
            unique = list(dict.fromkeys(c.source_ids))
            ids = [i for i in unique if i in valid_ids]
            removed += len(unique) - len(ids)
            if ids:
                out.append(c.model_copy(update={"source_ids": ids}))
        return out

    lecturers = []
    for note in report.lecturers_and_teaching:
        notes = fix(note.notes)
        if notes:
            lecturers.append(LecturerNote(name=note.name, semester=note.semester, notes=notes))

    cleaned = report.model_copy(
        update={
            "module_code": module_code,
            "workload_summary": fix(report.workload_summary),
            "assessment_breakdown": fix(report.assessment_breakdown),
            "content_and_usefulness": fix(report.content_and_usefulness),
            "lecturers_and_teaching": lecturers,
            "common_complaints": fix(report.common_complaints),
            "tips_to_do_well": fix(report.tips_to_do_well),
            "who_should_take_it": fix(report.who_should_take_it),
            "disagreements": fix(report.disagreements),
        }
    )
    return cleaned, removed
