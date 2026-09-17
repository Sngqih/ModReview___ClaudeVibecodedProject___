"""r/nus discussions.

Three paths, in order of preference:

1. OAuth via ``asyncpraw`` if ``REDDIT_CLIENT_ID``/``REDDIT_CLIENT_SECRET`` are set.
2. Discover threads with the search provider, then best-effort fetch ``{thread}.json``
   (public, throttled, no guarantee). At least 2 s between calls.
3. If the ``.json`` endpoint answers 429/403 (or non-JSON), fall back to the search snippet.
   Never try to get around a block.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime

from modreview.http import SleepFn, get_with_retry
from modreview.models import SourceDoc
from modreview.search.base import SearchHit
from modreview.sources.base import SourceContext, SourceUnavailable

log = logging.getLogger(__name__)

MAX_THREADS = 5
MAX_COMMENTS = 30
MIN_GAP_S = 2.0
_THREAD_RE = re.compile(r"reddit\.com/r/([A-Za-z0-9_]+)/comments/([a-z0-9]+)", re.I)


def thread_key(url: str) -> str | None:
    m = _THREAD_RE.search(url)
    return m.group(2).lower() if m else None


def is_nus_thread(url: str) -> bool:
    m = _THREAD_RE.search(url)
    return bool(m) and m.group(1).lower() == "nus"


def _ts(raw) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(raw), tz=UTC)
    except (TypeError, ValueError):
        return None


def flatten_comments(listing: dict) -> list[dict]:
    """Walk a Reddit comment listing recursively; replies at any depth compete equally."""
    out: list[dict] = []

    def walk(node) -> None:
        if not isinstance(node, dict):
            return
        kind = node.get("kind")
        data = node.get("data") or {}
        if kind == "Listing":
            for child in data.get("children", []):
                walk(child)
        elif kind == "t1":
            body = (data.get("body") or "").strip()
            if body and body not in ("[deleted]", "[removed]"):
                out.append(data)
            replies = data.get("replies")
            if isinstance(replies, dict):
                walk(replies)

    walk(listing)
    return out


def build_thread_doc(post: dict, comments: list[dict], url: str, max_chars: int) -> SourceDoc:
    title = (post.get("title") or "").strip()
    body = (post.get("selftext") or "").strip()
    if body in ("[deleted]", "[removed]"):
        body = ""
    top = sorted(comments, key=lambda c: int(c.get("score") or 0), reverse=True)[:MAX_COMMENTS]
    parts = [f"Post: {title}"]
    if body:
        parts.append(body)
    used = sum(len(p) + 1 for p in parts)
    if top:
        parts.append("\nTop comments:")
        for c in top:
            line = f"- [{int(c.get('score') or 0)} points] {(c.get('body') or '').strip()}"
            if used + len(line) > max_chars:
                break
            parts.append(line)
            used += len(line) + 1
    return SourceDoc.make(
        source="reddit",
        url=url,
        title=f"r/nus: {title}" if title else "r/nus thread",
        author=post.get("author"),
        published_at=_ts(post.get("created_utc")),
        text="\n".join(parts),
        score=int(post.get("score") or 0),
    )


def snippet_doc(hit: SearchHit) -> SourceDoc:
    text = (hit.content or hit.snippet or "").strip()
    return SourceDoc.make(
        source="reddit",
        url=hit.url,
        title=f"r/nus: {hit.title}" if hit.title else "r/nus thread (snippet)",
        published_at=hit.published_at,
        text=f"Post: {hit.title or ''}\n{text}".strip(),
        score=None,
    )


class RedditSource:
    name = "reddit"

    def __init__(self, ctx: SourceContext, sleep: SleepFn = asyncio.sleep) -> None:
        self._ctx = ctx
        self._sleep = sleep
        self.note: str | None = None

    async def fetch(self, module_code: str, module_title: str | None = None) -> list[SourceDoc]:
        self.note = None
        s = self._ctx.secrets
        if s.reddit_client_id and s.reddit_client_secret:
            try:
                return await self._fetch_oauth(module_code, module_title)
            except ImportError:
                log.warning("REDDIT_CLIENT_ID set but asyncpraw not installed; using search path")
                self.note = "asyncpraw missing, used search path"
        return await self._fetch_via_search(module_code, module_title)

    # ------------------------------------------------------------------ search + .json

    async def _discover(self, module_code: str, module_title: str | None) -> list[SearchHit]:
        search = self._ctx.search
        if search is None:
            raise SourceUnavailable("needs a search provider to find r/nus threads")
        max_results = self._ctx.config.search.max_results
        queries = [f'"{module_code}" r/nus']
        if module_title:
            queries.append(f'"{module_code}" "{module_title}" reddit')
        results = await asyncio.gather(
            *(search.search(q, max_results, include_domains=["reddit.com"]) for q in queries),
            return_exceptions=True,
        )
        seen: set[str] = set()
        hits: list[SearchHit] = []
        for res in results:
            if isinstance(res, BaseException):
                log.warning("Reddit discovery query failed: %s", res)
                continue
            for h in res:
                key = thread_key(h.url)
                if not key or not is_nus_thread(h.url) or key in seen:
                    continue
                seen.add(key)
                hits.append(h)
        return hits[:MAX_THREADS]

    async def _fetch_via_search(
        self, module_code: str, module_title: str | None
    ) -> list[SourceDoc]:
        hits = await self._discover(module_code, module_title)
        if not hits:
            return []
        max_chars = self._ctx.config.limits.max_chars_per_doc
        docs: list[SourceDoc] = []
        blocked = False
        fallbacks = 0
        for i, hit in enumerate(hits):
            if blocked:
                docs.append(snippet_doc(hit))
                fallbacks += 1
                continue
            if i > 0:
                await self._sleep(MIN_GAP_S)
            doc = await self._fetch_thread_json(hit, max_chars)
            if doc is None:
                blocked = True  # stop hammering: every remaining thread uses its snippet
                docs.append(snippet_doc(hit))
                fallbacks += 1
            else:
                docs.append(doc)
        if fallbacks:
            self.note = f"reddit.com blocked .json; {fallbacks} of {len(hits)} from search snippets"
        return docs

    async def _fetch_thread_json(self, hit: SearchHit, max_chars: int) -> SourceDoc | None:
        base = hit.url.split("?")[0].rstrip("/")
        url = f"{base}.json"
        try:
            resp = await get_with_retry(
                self._ctx.client,
                url,
                params={"limit": 100, "sort": "top", "raw_json": 1},
                retries=1,
                sleep=self._sleep,
            )
        except Exception as exc:  # transport error after retry
            log.info("Reddit .json failed for %s: %s", url, exc)
            return None
        if resp.status_code in (403, 429) or resp.status_code >= 400:
            log.info("Reddit .json %s for %s; falling back to snippet", resp.status_code, url)
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        if not isinstance(data, list) or not data:
            return None
        try:
            post = data[0]["data"]["children"][0]["data"]
        except (KeyError, IndexError, TypeError):
            return None
        comments = flatten_comments(data[1]) if len(data) > 1 else []
        return build_thread_doc(post, comments, base, max_chars)

    # ------------------------------------------------------------------ OAuth (optional)

    async def _fetch_oauth(self, module_code: str, module_title: str | None) -> list[SourceDoc]:
        import asyncpraw  # optional dependency: `uv sync --extra reddit`

        from modreview import USER_AGENT

        s = self._ctx.secrets
        max_chars = self._ctx.config.limits.max_chars_per_doc
        docs: list[SourceDoc] = []
        async with asyncpraw.Reddit(
            client_id=s.reddit_client_id.get_secret_value(),
            client_secret=s.reddit_client_secret.get_secret_value(),
            user_agent=USER_AGENT,
        ) as reddit:
            sub = await reddit.subreddit("nus")
            query = f'"{module_code}"'
            async for sub_post in sub.search(query, limit=MAX_THREADS, sort="relevance"):
                await sub_post.load()
                await sub_post.comments.replace_more(limit=0)
                flat = [
                    {"body": c.body, "score": c.score, "author": str(c.author)}
                    for c in sub_post.comments.list()
                    if getattr(c, "body", None)
                ]
                post = {
                    "title": sub_post.title,
                    "selftext": sub_post.selftext,
                    "score": sub_post.score,
                    "created_utc": sub_post.created_utc,
                    "author": str(sub_post.author),
                }
                url = f"https://www.reddit.com{sub_post.permalink}".rstrip("/")
                docs.append(build_thread_doc(post, flat, url, max_chars))
        self.note = "via Reddit OAuth"
        return docs
