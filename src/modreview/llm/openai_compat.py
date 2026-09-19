"""Any OpenAI-compatible ``/chat/completions`` endpoint: Gemini (default), Groq, OpenRouter,
Ollama, LM Studio.

Structured output uses ``response_format: {"type": "json_schema", ...}``. The schema is
dereferenced (no ``$ref``/``$defs``) because several compatible servers only accept a flat
schema. The result is always re-validated with pydantic; one retry carries the error back.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from modreview.http import make_client
from modreview.llm.base import (
    LLMOutputError,
    LLMPermanentError,
    LLMTransientError,
    ModelInfo,
    Usage,
)

log = logging.getLogger(__name__)

RETRY_HINT = (
    "\n\nYour previous answer did not validate against the JSON schema. "
    "Fix these problems and answer again with valid JSON only:\n{error}"
)
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Replace every ``{"$ref": "#/$defs/X"}`` with a copy of X and drop ``$defs``."""
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].rsplit("/", 1)[-1]
                resolved = walk(copy.deepcopy(defs[name]))
                extra = {k: walk(v) for k, v in node.items() if k != "$ref"}
                return {**resolved, **extra}
            return {k: walk(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


def strip_fences(text: str) -> str:
    m = _FENCE_RE.match(text)
    return m.group(1) if m else text


def _error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:300]
    if isinstance(data, list) and data:
        data = data[0]
    err = data.get("error", data) if isinstance(data, dict) else data
    if isinstance(err, dict):
        return str(err.get("message") or err)[:300]
    return str(err)[:300]


def map_status(resp: httpx.Response) -> Exception:
    msg = _error_message(resp)
    code = resp.status_code
    if code == 429:
        return LLMTransientError(f"Rate limited (429): {msg}")
    if code >= 500:
        return LLMTransientError(f"Server error ({code}): {msg}")
    if code in (401, 403):
        return LLMPermanentError(f"Key rejected ({code}): {msg}")
    if code == 404:
        return LLMPermanentError(f"Model or endpoint not found (404): {msg}")
    return LLMPermanentError(f"Request failed ({code}): {msg}")


class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 180.0,
        reasoning_effort: str | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._client = client
        self._timeout = timeout_s
        self.reasoning_effort = reasoning_effort

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}"} if self._key else {}

    async def _request(self, method: str, path: str, **kw) -> httpx.Response:
        url = f"{self._base}/{path.lstrip('/')}"
        try:
            if self._client is not None:
                return await self._client.request(method, url, headers=self._headers(), **kw)
            async with make_client(self._timeout) as c:
                return await c.request(method, url, headers=self._headers(), **kw)
        except httpx.TimeoutException as exc:
            raise LLMTransientError(f"Timed out talking to {self._base}") from exc
        except httpx.TransportError as exc:
            raise LLMTransientError(f"Could not reach {self._base}: {exc}") from exc

    async def list_models(self) -> list[ModelInfo]:
        resp = await self._request("GET", "models")
        if resp.status_code != 200:
            raise map_status(resp)
        out = []
        for m in resp.json().get("data", []):
            mid = str(m.get("id", ""))
            mid = mid.removeprefix("models/")  # Gemini prefixes ids; requests accept bare ids
            if mid:
                out.append(ModelInfo(id=mid, display_name=m.get("display_name") or mid))
        out.sort(key=lambda m: m.id)
        return out

    async def test_connection(self) -> str:
        models = await self.list_models()
        return f"ok: key accepted, {len(models)} models available"

    async def generate_structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[BaseModel],
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> tuple[BaseModel, Usage]:
        json_schema = inline_refs(schema.model_json_schema())
        total = Usage()
        prompt = user
        last_error = ""
        for attempt in range(2):
            body: dict[str, Any] = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": max_tokens,
                "temperature": temperature,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": schema.__name__, "schema": json_schema},
                },
            }
            if self.reasoning_effort:
                body["reasoning_effort"] = self.reasoning_effort
            resp = await self._request("POST", "chat/completions", json=body)
            if resp.status_code != 200:
                raise map_status(resp)
            data = resp.json()
            u = data.get("usage") or {}
            total = total + Usage(
                input_tokens=int(u.get("prompt_tokens") or 0),
                output_tokens=int(u.get("completion_tokens") or 0),
            )
            choice = (data.get("choices") or [{}])[0]
            finish = choice.get("finish_reason")
            text = ((choice.get("message") or {}).get("content") or "").strip()
            if finish == "length":
                raise LLMOutputError(
                    f"Output cut off at max_tokens={max_tokens}; raise [llm].max_output_tokens."
                )
            if finish == "content_filter":
                raise LLMPermanentError("The provider's safety filter blocked this request.")
            try:
                return schema.model_validate(json.loads(strip_fences(text))), total
            except (ValidationError, ValueError) as exc:
                last_error = str(exc)[:2000]
                log.warning(
                    "structured output failed validation (attempt %d): %s", attempt + 1, exc
                )
                prompt = user + RETRY_HINT.format(error=last_error)
        raise LLMOutputError(f"Model output never validated after 2 attempts: {last_error}")
