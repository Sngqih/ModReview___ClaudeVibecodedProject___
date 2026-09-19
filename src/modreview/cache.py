"""SQLite cache and history. One row per (module, provider, model, prompt version).

Regenerating a report overwrites its row (owner's choice), so the cache table is the history.
Only finished LLM reports are stored; facts-only pages and failed runs are not.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from modreview.review import ReviewResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    cache_key      TEXT PRIMARY KEY,
    module_code    TEXT NOT NULL,
    title          TEXT NOT NULL,
    provider       TEXT NOT NULL,
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    sentiment      TEXT,
    difficulty     INTEGER,
    created_at     TEXT NOT NULL,
    result_json    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reports_code ON reports (module_code);
CREATE INDEX IF NOT EXISTS reports_created ON reports (created_at);
"""


def cache_key(module_code: str, provider: str, model: str, prompt_version: str) -> str:
    return f"{module_code}|{provider}|{model}|{prompt_version}"


class HistoryRow(BaseModel):
    cache_key: str
    module_code: str
    title: str
    provider: str
    model: str
    prompt_version: str
    sentiment: str | None
    difficulty: int | None
    created_at: datetime


class ReviewCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def get(
        self,
        module_code: str,
        provider: str,
        model: str,
        prompt_version: str,
        ttl_days: int,
        *,
        now: datetime | None = None,
    ) -> ReviewResult | None:
        """A fresh cached result, or None. ``ttl_days = 0`` disables the cache."""
        if ttl_days <= 0:
            return None
        key = cache_key(module_code, provider, model, prompt_version)
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT created_at, result_json FROM reports WHERE cache_key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        created = datetime.fromisoformat(row["created_at"])
        if (now or datetime.now(UTC)) - created > timedelta(days=ttl_days):
            return None
        return ReviewResult.model_validate_json(row["result_json"])

    def get_by_key(self, key: str) -> ReviewResult | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT result_json FROM reports WHERE cache_key = ?", (key,)
            ).fetchone()
        return ReviewResult.model_validate_json(row["result_json"]) if row else None

    def put(self, result: ReviewResult) -> str:
        if result.report is None or result.provider is None or result.model is None:
            raise ValueError("only finished LLM reports are cached")
        key = cache_key(result.module_code, result.provider, result.model, result.prompt_version)
        stored = result.model_copy(update={"from_cache": False})
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute(
                """INSERT INTO reports (cache_key, module_code, title, provider, model,
                       prompt_version, sentiment, difficulty, created_at, result_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(cache_key) DO UPDATE SET
                       title = excluded.title, sentiment = excluded.sentiment,
                       difficulty = excluded.difficulty, created_at = excluded.created_at,
                       result_json = excluded.result_json""",
                (
                    key,
                    result.module_code,
                    result.facts.title,
                    result.provider,
                    result.model,
                    result.prompt_version,
                    result.report.overall_sentiment,
                    result.report.difficulty,
                    result.generated_at.isoformat(),
                    stored.model_dump_json(),
                ),
            )
        return key

    def history(self, limit: int = 200) -> list[HistoryRow]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT cache_key, module_code, title, provider, model, prompt_version,
                          sentiment, difficulty, created_at
                   FROM reports ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [HistoryRow.model_validate(dict(r)) for r in rows]

    def recent_codes(self, limit: int = 8) -> list[HistoryRow]:
        """Most recent row per module, newest first (for the home page)."""
        seen: set[str] = set()
        out: list[HistoryRow] = []
        for row in self.history(limit * 5):
            if row.module_code not in seen:
                seen.add(row.module_code)
                out.append(row)
            if len(out) == limit:
                break
        return out

    def delete(self, key: str) -> bool:
        with self._lock, closing(self._connect()) as conn, conn:
            cur = conn.execute("DELETE FROM reports WHERE cache_key = ?", (key,))
        return cur.rowcount > 0
