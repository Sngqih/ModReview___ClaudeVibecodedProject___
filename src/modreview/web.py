"""View helpers: Jinja filters, citation numbering, the model catalog and module search."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta, timezone
from urllib.parse import urlsplit

from modreview.dates import age_years, ay_label, ay_range_label, ay_short, ay_start_year
from modreview.llm.base import ModelInfo
from modreview.llm.registry import make_llm_provider
from modreview.models import Claim, ExamInfo, ReviewReport, SourceDoc
from modreview.review import ReviewResult
from modreview.settings import LLMConfig, Secrets
from modreview.sources.nusmods import tidy_requirement

log = logging.getLogger(__name__)

SGT = timezone(timedelta(hours=8), "SGT")  # Singapore has no DST, so a fixed offset is exact

SOURCE_LABELS = {
    "nusmods": "NUSMods",
    "disqus": "NUSMods review",
    "reddit": "Reddit",
    "course_page": "Official course page",
    "blog": "Senior's blog",
    "web": "Web",
}
SOURCE_GROUPS = {
    "nusmods": "Official listing",
    "course_page": "Official course pages",
    "disqus": "NUSMods reviews",
    "reddit": "Reddit r/nus",
    "blog": "Seniors' blogs",
    "web": "Other pages",
}
STALE_YEARS = 2  # a claim whose newest source is older than this is greyed as possibly stale
TIMELINE_YEARS = 5  # academic years shown in the header timeline (current + 4 before)
RECENT_YEARS = 2  # the latest academic years drawn in the accent colour
DIFFICULTY_LABELS = {1: "Easy", 2: "Manageable", 3: "Moderate", 4: "Hard", 5: "Very hard"}
SENTIMENT_TONES = {
    "very positive": "good",
    "positive": "good",
    "mixed": "mixed",
    "negative": "bad",
    "very negative": "bad",
    "insufficient data": "none",
}

# (heading, attribute) in display order. Lecturers are rendered separately.
REPORT_SECTIONS: list[tuple[str, str]] = [
    ("Workload", "workload_summary"),
    ("Assessment", "assessment_breakdown"),
    ("Content and usefulness", "content_and_usefulness"),
    ("Common complaints", "common_complaints"),
    ("Tips to do well", "tips_to_do_well"),
    ("Who should take it", "who_should_take_it"),
    ("Where sources disagree", "disagreements"),
]


# --------------------------------------------------------------------------- filters


def sgt(dt: datetime | None, fmt: str = "%d %b %Y, %H:%M") -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(SGT).strftime(fmt).lstrip("0")


def exam_text(ex: ExamInfo) -> str:
    if ex.exam_date is None:
        return "No final exam listed"
    when = sgt(ex.exam_date, "%a %d %b %Y, %I:%M %p").replace(" 0", " ")
    return f"{when} ({ex.exam_duration_min} min)" if ex.exam_duration_min else when


def difficulty_label(n: int | None) -> str:
    return DIFFICULTY_LABELS.get(n or 0, "Not enough evidence")


def sentiment_tone(s: str) -> str:
    return SENTIMENT_TONES.get(s, "none")


def excerpt(text: str, n: int = 260) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + "…"


def host(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.")


def money(cost: float | None) -> str:
    if cost is None:
        return "not priced"
    if cost == 0:
        return "$0.00 (free tier)"
    return f"${cost:.4f}" if cost < 0.01 else f"${cost:.2f}"


def thousands(n: int | None) -> str:
    return f"{n or 0:,}"


def safe_url(url: str) -> str:
    """Only http(s) links reach an href; anything else (javascript:, data:) becomes '#'."""
    return url if urlsplit(url).scheme in ("http", "https") else "#"


def sem_label(n: int) -> str:
    return {1: "Sem 1", 2: "Sem 2", 3: "Special Term I", 4: "Special Term II"}.get(n, f"Term {n}")


def workload_rows(workload: list[float] | None) -> list[tuple[str, float, int]]:
    """(label, hours, bar width %) for the facts panel."""
    if not workload:
        return []
    top = max(max(workload), 1.0)
    labels = ("Lecture", "Tutorial", "Lab", "Project", "Preparation")
    return [(lab, hrs, round(hrs / top * 100)) for lab, hrs in zip(labels, workload, strict=False)]


# --------------------------------------------------------------------------- citations


def section_claims(report: ReviewReport) -> list[list[Claim]]:
    """Every claim list in the order the page shows them."""
    out = [getattr(report, attr) for _, attr in REPORT_SECTIONS[:3]]
    out.extend(note.notes for note in report.lecturers_and_teaching)
    out.extend(getattr(report, attr) for _, attr in REPORT_SECTIONS[3:])
    return out


def citation_numbers(result: ReviewResult) -> dict[str, int]:
    """doc id -> number. Cited docs are numbered in order of first appearance on the page,
    then the uncited docs follow in pipeline order, so numbers read 1, 2, 3 down the page."""
    numbers: dict[str, int] = {}
    if result.report is not None:
        for claims in section_claims(result.report):
            for c in claims:
                for sid in c.source_ids:
                    numbers.setdefault(sid, len(numbers) + 1)
    for d in result.docs:
        numbers.setdefault(d.id, len(numbers) + 1)
    return numbers


def grouped_sources(
    result: ReviewResult, numbers: dict[str, int]
) -> tuple[list[tuple[str, list[SourceDoc]]], list[tuple[str, list[SourceDoc]]]]:
    """(cited groups, uncited groups); each group is (heading, docs sorted by number)."""
    cited_ids = result.report.all_source_ids() if result.report else set()

    def group(docs: list[SourceDoc]) -> list[tuple[str, list[SourceDoc]]]:
        out = []
        for src, heading in SOURCE_GROUPS.items():
            items = sorted((d for d in docs if d.source == src), key=lambda d: numbers[d.id])
            if items:
                out.append((heading, items))
        return out

    cited = [d for d in result.docs if d.id in cited_ids]
    uncited = [d for d in result.docs if d.id not in cited_ids]
    if result.report is None:  # facts-only or failed: nothing is "cited", show all openly
        return group(result.docs), []
    return group(cited), group(uncited)


def _month(dt: datetime) -> str:
    return sgt(dt, "%b %Y")


def claim_date(claim: Claim, docs_by_id: dict[str, SourceDoc], now: datetime | None = None):
    """When the information behind a claim is from, worked out from the docs it cites.

    Returns {"label", "title", "stale", "kind"} or None. ``kind`` is "dated", "official"
    (only the NUSMods listing or undated official pages) or "undated"."""
    docs = [docs_by_id[i] for i in claim.source_ids if i in docs_by_id]
    if not docs:
        return None
    dated = [d for d in docs if d.published_at is not None and d.source != "nusmods"]
    if not dated:
        if all(d.source in ("nusmods", "course_page") for d in docs):
            return {
                "label": "official",
                "title": "From the official listing or course page (current run)",
                "stale": False,
                "kind": "official",
            }
        return {
            "label": "undated",
            "title": "The sources behind this point carry no date",
            "stale": False,
            "kind": "undated",
        }
    dates = [d.published_at for d in dated]
    newest, oldest = max(dates), min(dates)
    label = ay_range_label(dates) or ""
    span = (
        _month(newest)
        if _month(newest) == _month(oldest)
        else f"{_month(oldest)} to {_month(newest)}"
    )
    title = f"Based on sources from {span}"
    if all(d.date_estimated for d in dated):
        label = "~" + label
        title += " (dates estimated from the text)"
    stale = age_years(newest, now) > STALE_YEARS
    if stale:
        title += f". Newest source is over {STALE_YEARS} years old, so this may be out of date."
    return {"label": label, "title": title, "stale": stale, "kind": "dated"}


def source_timeline(result: ReviewResult, now: datetime | None = None) -> dict:
    """Counts of the opinion sources used per academic year, for the header chart."""
    now = now or datetime.now(UTC)
    current = ay_start_year(now)
    years = list(range(current - TIMELINE_YEARS + 1, current + 1))
    counts = dict.fromkeys(years, 0)
    older = undated = estimated = 0
    newest: datetime | None = None
    for d in result.docs:
        if d.source == "nusmods":
            continue
        if d.published_at is None:
            undated += 1
            continue
        estimated += d.date_estimated
        y = ay_start_year(d.published_at)
        if y in counts:
            counts[y] += 1
        elif y < years[0]:
            older += 1
        newest = d.published_at if newest is None or d.published_at > newest else newest
    top = max([*counts.values(), older, 1])
    bars = [
        {
            "label": ay_short(y),
            "full": f"AY{ay_short(y)}",
            "count": counts[y],
            "pct": round(counts[y] / top * 100),
            "recent": y > current - RECENT_YEARS,
            "current": y == current,
        }
        for y in years
    ]
    if older:
        bars.insert(
            0,
            {"label": "older", "full": f"Before AY{ay_short(years[0])}", "count": older,
             "pct": round(older / top * 100), "recent": False, "current": False},
        )  # fmt: skip
    recent_total = sum(b["count"] for b in bars if b["recent"])
    dated_total = sum(b["count"] for b in bars)
    return {
        "bars": bars,
        "undated": undated,
        "estimated": estimated,
        "newest": newest,
        "newest_label": ay_label(newest, with_sem=True) if newest else None,
        "recent_share": round(recent_total / dated_total * 100) if dated_total else 0,
        "too_old": result.docs_too_old,
        "current_label": f"AY{ay_short(current)}",
    }


def docs_map(result: ReviewResult) -> dict[str, SourceDoc]:
    return {d.id: d for d in result.docs}


def cite_tip(doc: SourceDoc) -> str:
    title = doc.title or host(doc.url)
    if doc.published_at:
        when = ("about " if doc.date_estimated else "") + (
            ay_label(doc.published_at, with_sem=True)
            if doc.date_estimated
            else sgt(doc.published_at, "%d %b %Y")
        )
    else:
        when = "undated"
    return f"{title} · {when}"


def doc_period(doc: SourceDoc) -> str:
    """'12 May 2025 · AY24/25 S2', 'about AY24/25 S1 (estimated)', or 'undated'."""
    if doc.published_at is None:
        return "undated"
    if doc.date_estimated:
        return f"about {ay_label(doc.published_at)} (estimated from the text)"
    return f"{sgt(doc.published_at, '%d %b %Y')} · {ay_label(doc.published_at, with_sem=True)}"


# --------------------------------------------------------------------------- model catalog

NON_TEXT_MARKERS = (
    "tts", "image", "embed", "robotics", "live", "audio", "transcribe",
    "computer-use", "aqa", "imagen", "veo", "omni", "customtools", "learnlm", "vision",
    "deep-research", "antigravity",
)  # fmt: skip


def is_text_model(model_id: str) -> bool:
    mid = model_id.lower()
    return not any(m in mid for m in NON_TEXT_MARKERS)


class ModelCatalog:
    """Cached ``list_models()`` per provider for the dropdowns (10 minutes)."""

    def __init__(self, secrets: Secrets, ttl_s: float = 600) -> None:
        self._secrets = secrets
        self._ttl = ttl_s
        self._cache: dict[str, tuple[float, list[ModelInfo]]] = {}

    async def models(
        self, provider_name: str, llm_config: LLMConfig | None = None
    ) -> tuple[list[ModelInfo], str | None]:
        hit = self._cache.get(provider_name)
        if hit and time.monotonic() - hit[0] < self._ttl:
            return hit[1], None
        provider, why = make_llm_provider(provider_name, self._secrets, llm_config)
        if provider is None:
            return [], why
        try:
            models = await asyncio.wait_for(provider.list_models(), timeout=15)
        except Exception as exc:  # shown in the UI; never crash a page over the dropdown
            log.info("list_models(%s) failed: %s", provider_name, exc)
            return [], str(exc)[:200] or type(exc).__name__
        models = [m for m in models if is_text_model(m.id)]
        self._cache[provider_name] = (time.monotonic(), models)
        return models, None

    async def options(
        self, provider_name: str, current: str, llm_config: LLMConfig | None = None
    ) -> tuple[list[str], str | None]:
        """Model ids for a <select>, with ``current`` always present and first."""
        models, error = await self.models(provider_name, llm_config)
        ids = [m.id for m in models if m.id != current]
        return [current, *ids] if current else ids, error

    def invalidate(self) -> None:
        self._cache.clear()


# --------------------------------------------------------------------------- module search

ModuleListLoader = Callable[[], Awaitable[list[dict]]]


class ModuleIndex:
    """In-memory module list for the search box (loaded once, refreshed daily)."""

    def __init__(self, loader: ModuleListLoader, ttl_s: float = 24 * 3600) -> None:
        self._loader = loader
        self._ttl = ttl_s
        self._mods: list[dict] = []
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()

    async def _get(self) -> list[dict]:
        if self._mods and time.monotonic() - self._loaded_at < self._ttl:
            return self._mods
        async with self._lock:
            if not self._mods or time.monotonic() - self._loaded_at >= self._ttl:
                try:
                    self._mods = await self._loader()
                    self._loaded_at = time.monotonic()
                except Exception as exc:
                    log.warning("module list unavailable: %s", exc)
        return self._mods

    async def search(self, q: str, limit: int = 8) -> list[dict]:
        q = q.strip()
        if len(q) < 2:
            return []
        mods = await self._get()
        up, low = q.upper().replace(" ", ""), q.lower()
        prefix = [m for m in mods if m.get("moduleCode", "").startswith(up)]
        if len(prefix) >= limit or len(q) < 3:
            return prefix[:limit]
        seen = {m["moduleCode"] for m in prefix}
        titled = [
            m for m in mods if m.get("moduleCode") not in seen and low in m.get("title", "").lower()
        ]
        return (prefix + titled)[:limit]


def register_template_helpers(env) -> None:
    env.filters.update(
        sgt=sgt,
        exam_text=exam_text,
        difficulty_label=difficulty_label,
        sentiment_tone=sentiment_tone,
        excerpt=excerpt,
        host=host,
        money=money,
        thousands=thousands,
        cite_tip=cite_tip,
        safe_url=safe_url,
        sem_label=sem_label,
        doc_period=doc_period,
        tidy_req=tidy_requirement,
    )
    env.globals.update(
        SOURCE_LABELS=SOURCE_LABELS,
        REPORT_SECTIONS=REPORT_SECTIONS,
        workload_rows=workload_rows,
        claim_date=claim_date,
        source_timeline=source_timeline,
        docs_map=docs_map,
    )
