from __future__ import annotations

import json

import httpx
import pytest
import respx

from modreview.llm.base import LLMOutputError, LLMPermanentError, LLMTransientError
from modreview.llm.openai_compat import OpenAICompatProvider, inline_refs, strip_fences
from modreview.models import ReviewReport
from tests.helpers import sample_report

BASE = "https://generativelanguage.googleapis.com/v1beta/openai"


def _completion(content: str, finish: str = "stop") -> dict:
    return {
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": finish}
        ],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500},
    }


def _provider(**kw) -> OpenAICompatProvider:
    return OpenAICompatProvider(BASE + "/", "test-key", **kw)


def test_inline_refs_removes_all_refs() -> None:
    flat = inline_refs(ReviewReport.model_json_schema())
    dumped = json.dumps(flat)
    assert "$ref" not in dumped and "$defs" not in dumped
    claim = flat["properties"]["workload_summary"]["items"]
    assert claim["properties"]["source_ids"]["type"] == "array"


def test_strip_fences() -> None:
    assert strip_fences('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert strip_fences('{"a": 1}') == '{"a": 1}'


@respx.mock
async def test_list_models_strips_prefix() -> None:
    route = respx.get(f"{BASE}/models").mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": "models/gemini-flash-latest"}, {"id": "models/b"}]}
        )
    )
    models = await _provider().list_models()
    assert [m.id for m in models] == ["b", "gemini-flash-latest"]
    assert route.calls[0].request.headers["authorization"] == "Bearer test-key"


@respx.mock
async def test_generate_structured_sends_schema_and_parses() -> None:
    report = sample_report("CS2040S")
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_completion(report.model_dump_json()))
    )
    out, usage = await _provider(reasoning_effort="low").generate_structured(
        system="sys",
        user="u",
        schema=ReviewReport,
        model="gemini-flash-latest",
        max_tokens=8000,
        temperature=0.2,
    )
    assert out == report
    assert (usage.input_tokens, usage.output_tokens) == (1200, 300)
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "gemini-flash-latest" and body["temperature"] == 0.2
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["response_format"]["type"] == "json_schema"
    assert "$ref" not in json.dumps(body["response_format"])
    assert body["reasoning_effort"] == "low"


@respx.mock
async def test_retry_on_invalid_json_then_success() -> None:
    report = sample_report("CS2040S")
    route = respx.post(f"{BASE}/chat/completions")
    route.side_effect = [
        httpx.Response(200, json=_completion('{"module_code": "CS2040S"}')),
        httpx.Response(200, json=_completion("```json\n" + report.model_dump_json() + "\n```")),
    ]
    out, usage = await _provider().generate_structured(
        system="s", user="u", schema=ReviewReport, model="m", max_tokens=100, temperature=0
    )
    assert out == report and usage.input_tokens == 2400
    retry_prompt = json.loads(route.calls[1].request.content)["messages"][1]["content"]
    assert "did not validate" in retry_prompt


@respx.mock
async def test_errors_are_classified() -> None:
    url = f"{BASE}/chat/completions"
    kw = dict(system="s", user="u", schema=ReviewReport, model="m", max_tokens=100, temperature=0)
    cases = [
        (httpx.Response(429, json=[{"error": {"message": "quota"}}]), LLMTransientError),
        (httpx.Response(503, text="down"), LLMTransientError),
        (httpx.Response(401, json={"error": {"message": "bad key"}}), LLMPermanentError),
        (httpx.Response(404, json={"error": {"message": "no model"}}), LLMPermanentError),
        (httpx.Response(200, json=_completion("{", finish="length")), LLMOutputError),
        (httpx.Response(200, json=_completion("", finish="content_filter")), LLMPermanentError),
    ]
    for resp, exc in cases:
        respx.post(url).mock(return_value=resp)
        with pytest.raises(exc):
            await _provider().generate_structured(**kw)


@respx.mock
async def test_network_error_is_transient() -> None:
    respx.post(f"{BASE}/chat/completions").mock(side_effect=httpx.ConnectError("no route"))
    with pytest.raises(LLMTransientError):
        await _provider().generate_structured(
            system="s", user="u", schema=ReviewReport, model="m", max_tokens=10, temperature=0
        )
