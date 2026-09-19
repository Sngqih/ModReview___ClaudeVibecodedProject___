"""End-to-end pipeline with respx-mocked sources and a fake LLM. No real network."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from pydantic import SecretStr

from modreview.cache import ReviewCache
from modreview.http import make_client
from modreview.llm.base import LLMPermanentError
from modreview.models import Claim, InvalidModuleCode, ModuleNotFound
from modreview.pipeline import run_review
from modreview.review import ProgressEvent
from modreview.search.base import SearchHit
from modreview.settings import AppConfig, LimitsConfig, Secrets
from modreview.sources.disqus import DISQUS_LIST_POSTS
from modreview.sources.nusmods import API_BASE
from tests.helpers import FakeLLM, FakeSearch, load_json, no_sleep, sample_report

BLOG = "https://example-blog.sg/cs2040s-review"


def _mock_sources() -> None:
    respx.get(url__regex=rf"{API_BASE}/.*/modules/CS2040S\.json").mock(
        return_value=httpx.Response(200, json=load_json("nusmods_CS2040S.json"))
    )

    def disqus(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("cursor"):
            return httpx.Response(200, json=load_json("disqus_listPosts_p2.json"))
        return httpx.Response(200, json=load_json("disqus_listPosts_p1.json"))

    respx.get(DISQUS_LIST_POSTS).mock(side_effect=disqus)
    respx.get(url__regex=r"https://www\.reddit\.com/.*\.json").mock(
        return_value=httpx.Response(403)
    )


def _hits() -> list[SearchHit]:
    return [
        SearchHit(url=BLOG, title="Blog", snippet="s", content="CS2040S review. " + "w " * 200),
        SearchHit(
            url="https://www.reddit.com/r/nus/comments/abc123/x/",
            title="CS2040S thread",
            snippet="People say CS2040S is heavy but fair.",
        ),
    ]


def _report_citing(ids: list[str]):
    r = sample_report("CS2040S")
    return r.model_copy(
        update={
            "workload_summary": [Claim(text="Heavy.", source_ids=[ids[0], "made-up-id"])],
            "assessment_breakdown": [Claim(text="Finals matter.", source_ids=[ids[1]])],
        }
    )


class CitingLLM(FakeLLM):
    """Cites the first two real doc ids it is shown, plus one invented id."""

    async def generate_structured(self, *, system, user, schema, model, max_tokens, temperature):
        import re

        ids = re.findall(r'<doc id="(d[0-9a-f]+)"', user)
        self.report = _report_citing(ids)
        return await super().generate_structured(
            system=system,
            user=user,
            schema=schema,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )


def _secrets() -> Secrets:
    return Secrets(_env_file=None, disqus_api_key=SecretStr("k"))


async def _run(cache, llm, *, force=False, config=None, model=None, events=None, search=None):
    async with make_client() as client:
        return await run_review(
            "cs2040s",
            config or AppConfig(),
            _secrets(),
            cache,
            model=model,
            force=force,
            client=client,
            search=search if search is not None else FakeSearch(_hits()),
            chain=[(llm, model or "fake-model")],
            sleep=no_sleep,
            progress=(events.append if events is not None else None),
            data_dir=cache.path.parent,
        )


@respx.mock
async def test_full_pipeline_then_cache_then_force(tmp_path: Path) -> None:
    _mock_sources()
    cache = ReviewCache(tmp_path / "db.sqlite")
    llm = CitingLLM()
    events: list[ProgressEvent] = []

    first = await _run(cache, llm, events=events)
    assert first.report is not None and not first.from_cache
    assert first.module_code == "CS2040S" and first.facts.title
    # The post-check removes "made-up-id" and the sample report's placeholder ids d1/d2.
    assert first.dropped_citations >= 1
    assert "made-up-id" not in first.report.all_source_ids()
    assert first.report.all_source_ids() <= {d.id for d in first.docs}
    assert first.report.workload_summary and first.report.assessment_breakdown
    assert first.docs[0].source == "nusmods"
    assert {d.source for d in first.docs} == {"nusmods", "disqus", "reddit", "web"}
    assert first.docs_collected >= len(first.docs)
    assert first.usage and first.usage.input_tokens == 1000
    stages = [e.stage for e in events if e.kind == "stage"]
    assert stages == ["fetching", "summarising", "done"]
    assert sum(1 for e in events if e.kind == "source") == 4
    user_prompt = llm.calls[0]["user"]
    assert "<doc id=" in user_prompt and "CS2040S" in user_prompt

    second = await _run(cache, llm, events=(events2 := []))
    assert second.from_cache and len(llm.calls) == 1  # no new LLM call
    assert [e.stage for e in events2] == ["cache_hit"]
    assert second.report == first.report

    third = await _run(cache, llm, force=True)
    assert not third.from_cache and len(llm.calls) == 2

    other_model = await _run(cache, llm, model="other-model")
    assert not other_model.from_cache and len(llm.calls) == 3  # cache key includes model


@respx.mock
async def test_facts_only_when_no_opinions(tmp_path: Path) -> None:
    _mock_sources()
    respx.get(DISQUS_LIST_POSTS).mock(
        return_value=httpx.Response(200, json={"code": 0, "response": []})
    )
    cache = ReviewCache(tmp_path / "db.sqlite")
    llm = FakeLLM(report=sample_report("CS2040S"))
    result = await _run(cache, llm, search=FakeSearch([]))
    assert result.report is None and result.facts_only_reason
    assert llm.calls == []  # owner's choice: skip the LLM entirely
    assert cache.history() == []  # facts-only pages are not cached


@respx.mock
async def test_llm_failure_returns_facts_and_error(tmp_path: Path) -> None:
    _mock_sources()
    cache = ReviewCache(tmp_path / "db.sqlite")
    llm = FakeLLM(error=LLMPermanentError("Key rejected (401)"))
    result = await _run(cache, llm)
    assert result.report is None and result.llm_error == "Key rejected (401)"
    assert result.docs and result.facts.title
    assert cache.history() == []


def test_token_guard_trims_docs() -> None:
    from modreview.llm.prompt import build_user_message, load_system_prompt
    from modreview.models import SourceDoc
    from modreview.pipeline import fit_to_token_limit
    from modreview.sources.nusmods import facts_to_doc, parse_module

    facts = parse_module(load_json("nusmods_CS2040S.json"), "2026-2027")
    facts_doc = facts_to_doc(facts)
    opinions = [
        SourceDoc.make(source="disqus", url=f"https://d/{i}", text=f"review {i} ".ljust(1000, "x"))
        for i in range(10)
    ]
    base = len(load_system_prompt()) + len(build_user_message(facts, [facts_doc]))
    limit = (base + 3000) // 4  # room for roughly 2-3 of the 10 reviews

    untouched, _ = fit_to_token_limit(facts, [facts_doc, *opinions], 100_000, 60_000)
    trimmed, size = fit_to_token_limit(facts, [facts_doc, *opinions], 100_000, limit)
    assert len(untouched) == 11
    assert size // 4 <= limit and 1 < len(trimmed) < 11
    assert trimmed[0].source == "nusmods"


@respx.mock
async def test_char_budget_applied(tmp_path: Path) -> None:
    _mock_sources()
    cache = ReviewCache(tmp_path / "db.sqlite")
    tight = AppConfig(limits=LimitsConfig(max_total_chars=1500))
    result = await _run(cache, CitingLLM(), config=tight)
    assert sum(len(d.text) for d in result.docs) <= 1500
    assert result.docs[0].source == "nusmods"


async def test_invalid_code_rejected_before_network(tmp_path: Path) -> None:
    cache = ReviewCache(tmp_path / "db.sqlite")
    with pytest.raises(InvalidModuleCode):
        await run_review("not a code", AppConfig(), _secrets(), cache)


@respx.mock
async def test_unknown_module(tmp_path: Path) -> None:
    respx.get(url__regex=rf"{API_BASE}/.*/modules/ZZ9999\.json").mock(
        return_value=httpx.Response(404)
    )
    cache = ReviewCache(tmp_path / "db.sqlite")
    async with make_client() as client:
        with pytest.raises(ModuleNotFound):
            await run_review(
                "ZZ9999", AppConfig(), _secrets(), cache, client=client, data_dir=tmp_path
            )
