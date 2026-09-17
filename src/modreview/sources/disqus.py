"""NUSMods student reviews via the Disqus API (forum ``nusmods-prod``)."""

from __future__ import annotations

import html
import logging
import re
from datetime import UTC, datetime

from modreview.http import get_with_retry
from modreview.models import SourceDoc
from modreview.sources.base import SourceContext, SourceUnavailable

log = logging.getLogger(__name__)

DISQUS_LIST_POSTS = "https://disqus.com/api/3.0/threads/listPosts.json"
FORUM = "nusmods-prod"
MAX_POSTS = 300
MIN_CHARS = 30
PAGE_SIZE = 100

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")


def strip_html(s: str) -> str:
    s = re.sub(r"<\s*br\s*/?>|</p>|</div>", "\n", s, flags=re.I)
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    s = _WS_RE.sub(" ", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def thread_selectors(code: str) -> list[tuple[str, str]]:
    """Order matters: the current NUSMods frontend uses the module code as thread identifier."""
    return [
        ("thread:ident", code),
        ("thread:link", f"https://nusmods.com/courses/{code}/reviews"),
        ("thread:link", f"https://nusmods.com/modules/{code}/reviews"),
    ]


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class DisqusSource:
    name = "disqus"

    def __init__(self, ctx: SourceContext) -> None:
        self._ctx = ctx
        self.note: str | None = None
        self.selector_used: tuple[str, str] | None = None

    async def fetch(self, module_code: str, module_title: str | None = None) -> list[SourceDoc]:
        self.note = None
        key = self._ctx.secrets.disqus_api_key
        if not key or not key.get_secret_value().strip():
            raise SourceUnavailable("DISQUS_API_KEY not set")
        api_key = key.get_secret_value()

        for param, value in thread_selectors(module_code):
            posts = await self._list_posts(api_key, param, value)
            if posts:
                self.selector_used = (param, value)
                if param != "thread:ident":
                    self.note = f"found via {param}"
                break
        else:
            return []

        thread_url = f"https://nusmods.com/courses/{module_code}/reviews"
        docs: list[SourceDoc] = []
        for p in posts:
            if p.get("isDeleted") or p.get("isSpam"):
                continue
            text = (p.get("raw_message") or strip_html(p.get("message") or "")).strip()
            if len(text) < MIN_CHARS:
                continue
            post_id = str(p.get("id", ""))
            author = (p.get("author") or {}).get("name")
            docs.append(
                SourceDoc.make(
                    source="disqus",
                    url=f"{thread_url}#comment-{post_id}" if post_id else thread_url,
                    chunk=post_id,
                    title=f"NUSMods review by {author or 'anonymous'}",
                    author=author,
                    published_at=_parse_dt(p.get("createdAt")),
                    text=text,
                    score=int(p.get("likes") or 0) - int(p.get("dislikes") or 0),
                )
            )
        return docs

    async def _list_posts(self, api_key: str, param: str, value: str) -> list[dict]:
        posts: list[dict] = []
        cursor: str | None = None
        while len(posts) < MAX_POSTS:
            params = {
                "api_key": api_key,
                "forum": FORUM,
                param: value,
                "limit": PAGE_SIZE,
                "order": "desc",
            }
            if cursor:
                params["cursor"] = cursor
            resp = await get_with_retry(self._ctx.client, DISQUS_LIST_POSTS, params=params)
            if resp.status_code == 400:
                # Disqus answers 400 / code 2 for "unable to find thread". Try the next selector.
                log.info("Disqus: no thread for %s=%s", param, value)
                return []
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") not in (0, None):
                log.warning("Disqus: API code %s: %s", data.get("code"), data.get("response"))
                return []
            posts.extend(data.get("response") or [])
            cur = data.get("cursor") or {}
            if not cur.get("hasNext") or not cur.get("next"):
                break
            cursor = cur["next"]
        return posts[:MAX_POSTS]
