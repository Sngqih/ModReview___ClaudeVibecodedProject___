"""The ``Source`` protocol and the shared per-request context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import httpx

from modreview.models import SourceDoc
from modreview.search.base import SearchProvider
from modreview.settings import AppConfig, Secrets


class SourceUnavailable(Exception):
    """The source cannot run at all (missing key, no search provider). Not a crash."""


@dataclass
class SourceContext:
    """Everything a source needs. Built once per review request."""

    client: httpx.AsyncClient
    config: AppConfig
    secrets: Secrets
    search: SearchProvider | None = None


@runtime_checkable
class Source(Protocol):
    name: str
    note: str | None  # set by fetch() when the source degraded (e.g. fell back to snippets)

    async def fetch(self, module_code: str, module_title: str | None) -> list[SourceDoc]: ...
