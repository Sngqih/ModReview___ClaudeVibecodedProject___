"""r/nus discussions.

Paths, in order of preference:

1. OAuth via ``asyncpraw`` if ``REDDIT_CLIENT_ID``/``REDDIT_CLIENT_SECRET`` are set.
2. The Arctic Shift archive (``[sources].reddit_archive``, on by default): full, dated
   threads and comment trees, no key. A free third-party archive of Reddit data, so it is
   used politely (a handful of calls, 1 s apart) and stops on any rate-limit answer.
3. Discover threads with the search provider, then best-effort fetch ``{thread}.json``
   (public, throttled, no guarantee). At least 2 s between calls. If the ``.json`` endpoint
   answers 429/403 (or non-JSON), fall back to the search snippet. Never get around a block.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from datetime import UTC, datetime

from modreview.dates import ay_windows, cutoff
from modreview.http import SleepFn, get_with_retry
from modreview.models import SourceDoc
from modreview.search.base import SearchHit
from modreview.sources.base import SourceContext, SourceUnavailable

log = logging.getLogger(__name__)

MAX_THREADS = 5
MAX_COMMENTS = 30
MIN_GAP_S = 2.0

ARCHIVE_API = "https://arctic-shift.photon-reddit.com/api"
ARCHIVE_THREADS = 6  # threads whose title names the module, read in full
ARCHIVE_COMMENT_THREADS = 6  # other threads with comments that mention the module
ARCHIVE_GAP_S = 1.0
ARCHIVE_TIMEOUT_S = 30.0
ARCHIVE_BUDGET_S = 25.0  # stop searching further back once this much time has passed
ARCHIVE_TREE_GRACE_S = 15.0  # extra time allowed for reading the matched threads
ARCHIVE_PARALLEL = 2  # year windows searched at once (a free service: keep it low)
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
        elif kind == "t1" or (kind is None and "body" in data):
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


def mentions(text: str, code: str) -> bool:
    return re.search(rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])", text or "", re.I) is not None


def thread_url(post: dict, link_id: str) -> str:
    permalink = post.get("permalink") or f"/r/nus/comments/{link_id}/"
    return f"https://www.reddit.com{permalink}".rstrip("/")


def comment_group_doc(
    post: dict, link_id: str, comments: list[dict], code: str, max_chars: int
) -> SourceDoc:
    """Comments that mention the module inside a thread about something else."""
    title = (post.get("title") or "r/nus thread").strip()
    ranked = sorted(comments, key=lambda c: int(c.get("score") or 0), reverse=True)
    parts = [f"Thread: {title}", f"Comments that mention {code}:"]
    used = sum(len(p) + 1 for p in parts)
    for c in ranked:
        when = _ts(c.get("created_utc"))
        stamp = when.date().isoformat() if when else "undated"
        line = f"- [{int(c.get('score') or 0)} points, {stamp}] {(c.get('body') or '').strip()}"
        if used + len(line) > max_chars:
            break
        parts.append(line)
        used += len(line) + 1
    dates = [d for d in (_ts(c.get("created_utc")) for c in comments) if d]
    return SourceDoc.make(
        source="reddit",
        url=thread_url(post, link_id),
        chunk=f"comments:{code}",
        title=f"r/nus: {title} (comments on {code})",
        published_at=max(dates) if dates else None,
        text="\n".join(parts),
        score=max((int(c.get("score") or 0) for c in comments), default=0),
    )


class ArchiveError(Exception):
    pass


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
        self._discovered: list[SearchHit] | None = None

    async def fetch(self, module_code: str, module_title: str | None = None) -> list[SourceDoc]:
        self.note = None
        self._discovered = None
        s = self._ctx.secrets
        if s.reddit_client_id and s.reddit_client_secret:
            try:
                return await self._fetch_oauth(module_code, module_title)
            except ImportError:
                log.warning("REDDIT_CLIENT_ID set but asyncpraw not installed; using search path")
                self.note = "asyncpraw missing, used search path"
        prefix = ""
        if self._ctx.config.sources.reddit_archive:
            try:
                docs = await self._fetch_via_archive(module_code, module_title)
            except ArchiveError as exc:
                log.warning("Reddit archive unavailable: %s", exc)
                prefix = f"archive unavailable ({exc}); "
            else:
                if docs:
                    return docs
                prefix = "nothing recent in the archive; "
                if self._ctx.search is None:
                    self.note = "nothing recent in the archive"
                    return []
        docs = await self._fetch_via_search(module_code, module_title)
        if prefix:
            self.note = prefix + (self.note or "used search")
        return docs

    # ------------------------------------------------------------------ archive

    async def _archive_get(self, path: str, params: dict) -> list:
        """One archive call. Retries once on its "Timeout. Maybe slow down" answer."""
        for attempt in range(2):
            try:
                resp = await self._ctx.client.get(
                    f"{ARCHIVE_API}/{path}", params=params, timeout=ARCHIVE_TIMEOUT_S
                )
            except Exception as exc:  # network, TLS, anything: fall back to search
                raise ArchiveError(f"{path}: {type(exc).__name__}") from exc
            if resp.status_code == 422 and "timeout" in resp.text.lower() and attempt == 0:
                await self._sleep(2 * ARCHIVE_GAP_S)
                continue
            if resp.status_code == 429:
                raise ArchiveError("rate limited")
            if resp.status_code != 200:
                raise ArchiveError(f"{path} answered {resp.status_code}")
            try:
                return resp.json().get("data") or []
            except ValueError as exc:
                raise ArchiveError(f"{path}: not JSON") from exc
        raise ArchiveError(f"{path} timed out")

    async def _fetch_via_archive(self, code: str, title: str | None = None) -> list[SourceDoc]:
        """Comments are searched one academic year at a time (the archive's full-text search
        times out on wide ranges), newest year first, within a time budget. Threads come from
        those comments' thread ids plus r/nus links found by web search; one ``posts/ids``
        call fetches them, and threads whose title names the module are read in full."""
        cfg = self._ctx.config
        max_chars = cfg.limits.max_chars_per_doc
        loop = asyncio.get_running_loop()
        deadline = loop.time() + ARCHIVE_BUDGET_S
        errors: list[str] = []
        stop = asyncio.Event()  # set on the first "rate limited": no further requests

        async def safe(path: str, params: dict) -> list:
            if stop.is_set():
                raise ArchiveError("rate limited")
            try:
                return await self._archive_get(path, params)
            except ArchiveError as exc:
                if str(exc) == "rate limited":
                    stop.set()
                    raise
                errors.append(str(exc))
                return []

        windows = ay_windows(cfg.limits.max_source_age_years)
        gate = asyncio.Semaphore(ARCHIVE_PARALLEL)

        async def one_year(i: int, after: str, before: str | None) -> list:
            params = {"subreddit": "nus", "body": code, "limit": 50, "after": after}
            if before:
                params["before"] = before
            async with gate:
                if i >= ARCHIVE_PARALLEL:
                    await self._sleep(ARCHIVE_GAP_S)
                return await safe("comments/search", params)

        tasks = [asyncio.create_task(one_year(i, a, b)) for i, (a, b) in enumerate(windows)]
        done, pending = await asyncio.wait(tasks, timeout=ARCHIVE_BUDGET_S)
        for t in pending:
            t.cancel()  # out of time: keep the newer years we already have
        comments: list[dict] = []
        searched = 0
        for t in tasks:
            if t not in done:
                continue
            searched += 1
            exc = t.exception()
            if exc is not None:
                raise exc if isinstance(exc, ArchiveError) else ArchiveError(str(exc))
            comments += t.result()
        if searched and len(errors) >= searched:
            raise ArchiveError(errors[0])  # every year failed: treat the archive as down
        unique: dict[str, dict] = {}
        for c in comments:
            if mentions(c.get("body", ""), code):
                unique.setdefault(str(c.get("id") or id(c)), c)
        comments = list(unique.values())

        # Thread candidates: from the comments, plus r/nus links the web search found.
        candidate_ids: list[str] = []
        for c in comments:
            link = str(c.get("link_id", "")).removeprefix("t3_")
            if link and link not in candidate_ids:
                candidate_ids.append(link)
        if self._ctx.search is not None:
            try:
                for hit in await self._discover_once(code, title):
                    key = thread_key(hit.url)
                    if key and key not in candidate_ids:
                        candidate_ids.append(key)
            except Exception as exc:  # discovery is a bonus here, never fatal
                log.info("r/nus discovery failed: %s", exc)
        if not candidate_ids:
            return []

        await self._sleep(ARCHIVE_GAP_S)
        posts = {
            p.get("id", ""): p
            for p in await safe("posts/ids", {"ids": ",".join(candidate_ids[:100])})
        }
        oldest = cutoff(cfg.limits.max_source_age_years).timestamp()
        title_posts = sorted(
            (
                p
                for p in posts.values()
                if mentions(p.get("title", ""), code) and float(p.get("created_utc") or 0) >= oldest
            ),
            key=lambda p: float(p.get("created_utc") or 0),
            reverse=True,
        )[:ARCHIVE_THREADS]
        title_ids = {p["id"] for p in title_posts}

        groups: dict[str, list[dict]] = defaultdict(list)
        for c in comments:
            link = str(c.get("link_id", "")).removeprefix("t3_")
            if link and link not in title_ids:
                groups[link].append(c)
        ordered = sorted(
            groups.items(),
            key=lambda kv: (
                max(int(c.get("score") or 0) for c in kv[1]),
                max(float(c.get("created_utc") or 0) for c in kv[1]),
            ),
            reverse=True,
        )[:ARCHIVE_COMMENT_THREADS]

        docs: list[SourceDoc] = []
        n_comments = 0
        for post in title_posts:
            if loop.time() > deadline + ARCHIVE_TREE_GRACE_S:
                break
            await self._sleep(ARCHIVE_GAP_S)
            tree = await safe("comments/tree", {"link_id": post["id"], "limit": 300})
            flat = flatten_comments({"kind": "Listing", "data": {"children": tree}})
            n_comments += len(flat)
            docs.append(build_thread_doc(post, flat, thread_url(post, post["id"]), max_chars))
        for link, cs in ordered:
            docs.append(comment_group_doc(posts.get(link, {}), link, cs, code, max_chars))
            n_comments += len(cs)

        n_threads = sum(1 for d in docs if "(comments on" not in (d.title or ""))
        note = f"archive: {n_threads} thread{'s' if n_threads != 1 else ''}"
        note += f", {n_comments} comment{'s' if n_comments != 1 else ''}"
        if ordered:
            note += (
                f" incl. mentions in {len(ordered)} other thread{'s' if len(ordered) != 1 else ''}"
            )
        self.note = note
        return docs

    # ------------------------------------------------------------------ search + .json

    async def _discover_once(self, code: str, title: str | None) -> list[SearchHit]:
        """``_discover`` at most once per fetch: the archive path and the snippet fallback
        share the same r/nus search results (and search credits)."""
        if self._discovered is None:
            self._discovered = await self._discover(code, title)
        return self._discovered

    async def _discover(self, module_code: str, module_title: str | None) -> list[SearchHit]:
        search = self._ctx.search
        if search is None:
            raise SourceUnavailable("needs a search provider to find r/nus threads")
        max_results = self._ctx.config.search.max_results
        queries = [f'"{module_code}" r/nus']
        if module_title:
            queries.append(f'"{module_code}" "{module_title}" reddit')
        since = cutoff(self._ctx.config.limits.max_source_age_years).date().isoformat()
        results = await asyncio.gather(
            *(
                search.search(q, max_results, include_domains=["reddit.com"], start_date=since)
                for q in queries
            ),
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
        hits = await self._discover_once(module_code, module_title)
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
