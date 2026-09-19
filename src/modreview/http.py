"""Shared httpx client with a descriptive User-Agent and polite retry/backoff."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import ssl
from collections.abc import Awaitable, Callable

import httpx

from modreview import USER_AGENT

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}
SleepFn = Callable[[float], Awaitable[None]]

_SSL_CONTEXT: ssl.SSLContext | None = None


def ssl_context() -> ssl.SSLContext:
    """Trust the OS certificate store (needed when antivirus/proxy software re-signs TLS).

    ``ssl.create_default_context()`` loads the Windows ROOT/CA stores on Windows and the
    system bundle elsewhere. Built once and shared: loading the store is slow.
    (``truststore`` was tried first but crashed with a native access violation on this
    laptop; see DECISIONS.md.)
    """
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        _SSL_CONTEXT = ssl.create_default_context()
    return _SSL_CONTEXT


def make_client(timeout_s: float = 20.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
        verify=ssl_context(),
        timeout=httpx.Timeout(timeout_s),
        follow_redirects=True,
        http2=False,
    )


async def get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    retries: int = 3,
    base_delay: float = 1.0,
    sleep: SleepFn | None = None,
) -> httpx.Response:
    """GET with exponential backoff on 429/5xx and transport errors.

    Returns the last response even if it is still an error status, so callers decide
    what a 404 or a persistent 403 means for them.
    """
    last_exc: Exception | None = None
    resp: httpx.Response | None = None
    for attempt in range(retries + 1):
        try:
            resp = await client.get(url, params=params, headers=headers)
        except httpx.TransportError as exc:
            last_exc = exc
            resp = None
        if resp is not None and resp.status_code not in RETRY_STATUSES:
            return resp
        if attempt == retries:
            break
        delay = base_delay * (2**attempt) + random.uniform(0, 0.3)
        if resp is not None and (ra := resp.headers.get("Retry-After")):
            with contextlib.suppress(ValueError):
                delay = max(delay, float(ra))
        log.debug(
            "retry %d/%d for %s (status=%s) in %.1fs",
            attempt + 1,
            retries,
            url,
            resp.status_code if resp is not None else "transport-error",
            delay,
        )
        await (sleep or asyncio.sleep)(delay)  # looked up at call time so tests can patch it
    if resp is not None:
        return resp
    assert last_exc is not None
    raise last_exc
