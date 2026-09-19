"""Web pages found through the configured ``SearchProvider``.

Three kinds of page, each from its own query plan and classified by host:

* ``course_page`` - lecturers' official pages on nus.edu.sg (current assessment structure).
* ``blog``        - seniors' write-ups on blog hosts (github.io, Medium, Substack, ...).
* ``web``         - everything else that mentions the module code.

General and blog queries are limited to the recency window (``max_source_age_years``);
official pages are not, because they are rarely dated but describe the current run.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

import trafilatura

from modreview.clean import strip_html
from modreview.dates import cutoff
from modreview.http import get_with_retry
from modreview.models import SourceDoc, SourceName
from modreview.search.base import SearchHit
from modreview.sources.base import SourceContext, SourceUnavailable

log = logging.getLogger(__name__)

# nusmods.com is covered by the NUSMods + Disqus sources, reddit.com by the Reddit source.
EXCLUDED_DOMAINS = ("nusmods.com", "reddit.com")
OFFICIAL_DOMAINS = ("nus.edu.sg",)
BLOG_DOMAINS = (
    "github.io",
    "medium.com",
    "substack.com",
    "wordpress.com",
    "blogspot.com",
    "notion.site",
    "hashnode.dev",
    "dev.to",
)
FETCH_CONCURRENCY = 4
MIN_CONTENT_CHARS = 200
OFFICIAL_RESULTS = 5
BLOG_RESULTS = 6


@dataclass(frozen=True)
class QueryPlan:
    query: str
    include_domains: tuple[str, ...] = ()
    dated: bool = True  # apply the recency window
    max_results: int | None = None


def queries_for(
    code: str,
    title: str | None,
    *,
    course_pages: bool = True,
    blogs: bool = True,
) -> list[QueryPlan]:
    plans = [QueryPlan(f'"{code}" NUS review')]
    if title:
        plans.append(QueryPlan(f'"{code}" "{title}" NUS'))
    plans.append(QueryPlan(f'"{code}" NUS workload exam'))
    if course_pages:
        plans.append(
            QueryPlan(f'"{code}"', OFFICIAL_DOMAINS, dated=False, max_results=OFFICIAL_RESULTS)
        )
    if blogs:
        plans.append(QueryPlan(f'"{code}" NUS', BLOG_DOMAINS, max_results=BLOG_RESULTS))
    return plans


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _on(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def classify(url: str) -> SourceName:
    host = host_of(url)
    if _on(host, OFFICIAL_DOMAINS):
        return "course_page"
    if _on(host, BLOG_DOMAINS):
        return "blog"
    return "web"


def is_excluded(url: str) -> bool:
    return _on(host_of(url), EXCLUDED_DOMAINS)


def looks_like_pdf(url: str) -> bool:
    return urlsplit(url).path.lower().endswith(".pdf")


def mentions_code(text: str, code: str) -> bool:
    return re.search(rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])", text, re.I) is not None


def _norm_url(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/").lower()


def extract_page(html: str, url: str) -> tuple[str, str | None, str | None, datetime | None]:
    """(text, title, author, date) via trafilatura; text is '' when nothing usable."""
    try:
        doc = trafilatura.bare_extraction(
            html, url=url, include_comments=True, with_metadata=True, favor_recall=True
        )
    except Exception as exc:  # trafilatura is defensive but not infallible on odd input
        log.debug("trafilatura failed on %s: %s", url, exc)
        return "", None, None, None
    if doc is None:
        return "", None, None, None
    get = (lambda k: doc.get(k)) if isinstance(doc, dict) else (lambda k: getattr(doc, k, None))
    text = get("text") or ""
    comments = get("comments") or ""
    if comments:
        text = f"{text}\n\nComments:\n{comments}"
    published = None
    if raw := get("date"):
        try:
            published = datetime.fromisoformat(str(raw))
        except ValueError:
            published = None
    return text.strip(), get("title"), get("author"), published


class WebSearchSource:
    name = "web"

    def __init__(self, ctx: SourceContext) -> None:
        self._ctx = ctx
        self.note: str | None = None

    async def fetch(self, module_code: str, module_title: str | None = None) -> list[SourceDoc]:
        self.note = None
        search = self._ctx.search
        if search is None:
            raise SourceUnavailable("no search provider configured")
        cfg = self._ctx.config
        max_results = cfg.search.max_results
        since = cutoff(cfg.limits.max_source_age_years).date().isoformat()
        plans = queries_for(
            module_code,
            module_title,
            course_pages=cfg.sources.course_pages,
            blogs=cfg.sources.blogs,
        )
        results = await asyncio.gather(
            *(
                search.search(
                    p.query,
                    p.max_results or max_results,
                    include_domains=list(p.include_domains) or None,
                    exclude_domains=None if p.include_domains else list(EXCLUDED_DOMAINS),
                    start_date=since if p.dated else None,
                )
                for p in plans
            ),
            return_exceptions=True,
        )
        seen: set[str] = set()
        hits: list[SearchHit] = []
        failed = 0
        for res in results:
            if isinstance(res, BaseException):
                failed += 1
                log.warning("web search query failed: %s", res)
                continue
            for h in res:
                key = _norm_url(h.url)
                if key in seen or is_excluded(h.url) or looks_like_pdf(h.url):
                    continue
                seen.add(key)
                hits.append(h)
        if failed and not hits:
            raise RuntimeError("all web search queries failed")
        hits = hits[: max_results * 3]

        sem = asyncio.Semaphore(FETCH_CONCURRENCY)
        docs = await asyncio.gather(*(self._to_doc(h, module_code, sem) for h in hits))
        out = [d for d in docs if d is not None]
        kinds = {k: sum(1 for d in out if d.source == k) for k in ("course_page", "blog")}
        parts = []
        if kinds["course_page"]:
            parts.append(f"{kinds['course_page']} official")
        if kinds["blog"]:
            parts.append(f"{kinds['blog']} blog{'s' if kinds['blog'] != 1 else ''}")
        dropped = len(hits) - len(out)
        if dropped:
            parts.append(f"{dropped} skipped (no text or no mention of {module_code})")
        self.note = ", ".join(parts) or None
        return out

    async def _to_doc(self, hit: SearchHit, code: str, sem: asyncio.Semaphore) -> SourceDoc | None:
        kind = classify(hit.url)
        # Search providers sometimes return page text with literal markup in it.
        text = strip_html(hit.content or "")
        title, author, published = hit.title, None, hit.published_at
        # Official pages are worth a full read (assessment tables sit below the fold);
        # other pages only when the provider's excerpt is too thin.
        if kind == "course_page" or len(text) < MIN_CONTENT_CHARS:
            async with sem:
                fetched = await self._fetch_text(hit.url)
            if fetched is not None:
                full, t2, author, d2 = fetched
                if len(full) > len(text):
                    text = full
                title = title or t2
                published = published or d2
            elif len(text) < MIN_CONTENT_CHARS:
                return None
        if len(text) < MIN_CONTENT_CHARS or not mentions_code(text, code):
            return None
        return SourceDoc.make(
            source=kind,
            url=hit.url,
            title=title or host_of(hit.url),
            author=author,
            published_at=published,
            text=text,
            score=None,
        )

    async def _fetch_text(self, url: str):
        try:
            resp = await get_with_retry(self._ctx.client, url, retries=1)
        except Exception as exc:
            log.info("web fetch failed %s: %s", url, exc)
            return None
        if resp.status_code in (401, 403) or "login" in str(resp.url).lower():
            return None  # behind a login or blocked; not worth fighting
        if resp.status_code >= 400:
            return None
        ctype = resp.headers.get("content-type", "")
        if "html" not in ctype and "xml" not in ctype:
            return None
        text, title, author, published = extract_page(resp.text, str(resp.url))
        if not text:
            return None
        return text, title, author, published
