"""Shared test helpers: fixtures dir, a fake search provider, a no-op sleep."""

from __future__ import annotations

import json
from pathlib import Path

from modreview.llm.base import ModelInfo, Usage
from modreview.llm.prompt import PROMPT_VERSION
from modreview.models import Claim, LecturerNote, ReviewReport
from modreview.search.base import SearchHit

FIXTURES = Path(__file__).parent / "fixtures"


def load_json(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def load_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeSearch:
    name = "fake"

    def __init__(
        self,
        hits: list[SearchHit] | None = None,
        *,
        fail: bool = False,
        by_domain: dict[tuple, list[SearchHit]] | None = None,
    ) -> None:
        self.hits = hits or []
        self.fail = fail
        # Optional: different hits per include_domains tuple (() = unrestricted queries).
        self.by_domain = by_domain
        self.calls: list[dict] = []

    async def search(
        self, query, max_results, *, include_domains=None, exclude_domains=None, start_date=None
    ):
        self.calls.append(
            {
                "query": query,
                "max_results": max_results,
                "include_domains": include_domains,
                "exclude_domains": exclude_domains,
                "start_date": start_date,
            }
        )
        if self.fail:
            raise RuntimeError("search is down")
        if self.by_domain is not None:
            key = tuple(include_domains or ())
            return list(self.by_domain.get(key, self.by_domain.get((), [])))
        return list(self.hits)


async def no_sleep(_: float) -> None:
    return None


def sample_report(code: str) -> ReviewReport:
    c1 = Claim(text="Around 10-12 hours a week, above the official 10.", source_ids=["d1"])
    c2 = Claim(text="Finals 40%, midterm 20%, problem sets 25%.", source_ids=["d2"])
    return ReviewReport(
        module_code=code,
        one_line_verdict="Hard but foundational; start problem sets early.",
        overall_sentiment="positive",
        difficulty=4,
        workload_summary=[c1],
        assessment_breakdown=[c2],
        content_and_usefulness=[Claim(text="Used in every later CS module.", source_ids=["d1"])],
        lecturers_and_teaching=[
            LecturerNote(
                name="Prof Seth",
                semester="AY23/24 S1",
                notes=[Claim(text="Engaging lectures.", source_ids=["d2"])],
            )
        ],
        common_complaints=[Claim(text="Steep bell curve.", source_ids=["d1", "d2"])],
        tips_to_do_well=[Claim(text="Do past-year papers.", source_ids=["d1"])],
        who_should_take_it=[],
        disagreements=[],
        data_freshness="Most reviews from AY23/24",
        confidence="medium",
    )


def make_result(code: str = "CS2040S", model: str = "gemini-flash-latest", **kw):
    """A finished ReviewResult whose citations point at real docs."""
    from datetime import UTC, datetime, timedelta

    from modreview.models import SourceDoc, SourceStatus
    from modreview.review import ReviewResult
    from modreview.sources.nusmods import facts_to_doc, parse_module

    facts = parse_module(load_json("nusmods_CS2040S.json"), "2026-2027")
    facts_doc = facts_to_doc(facts)
    review = SourceDoc.make(
        source="disqus",
        url="https://nusmods.com/courses/CS2040S/reviews#comment-1",
        title="NUSMods review by cs_kid",
        text="Heavy but fair. Start the problem sets early. <script>alert(1)</script>",
        published_at=datetime(2025, 11, 2, tzinfo=UTC),
        score=12,
    )
    blog = SourceDoc.make(
        source="web",
        url="https://example-blog.sg/cs2040s",
        title="My CS2040S review",
        text="CS2040S took me 12 hours a week.",
    )
    unused = SourceDoc.make(
        source="reddit", url="https://www.reddit.com/r/nus/comments/x/y", text="Not cited."
    )
    ids = [facts_doc.id, review.id, blog.id]
    report = sample_report(code).model_copy(
        update={
            "workload_summary": [
                Claim(text="Students report 10-12 hours a week.", source_ids=[ids[0], ids[2]])
            ],
            "assessment_breakdown": [Claim(text="Finals matter most.", source_ids=[ids[1]])],
            "content_and_usefulness": [],
            "lecturers_and_teaching": [
                LecturerNote(
                    name="Prof Seth",
                    semester="AY23/24 S1",
                    notes=[Claim(text="Clear lectures.", source_ids=[ids[1]])],
                )
            ],
            "common_complaints": [Claim(text="Steep curve.", source_ids=[ids[1]])],
            "tips_to_do_well": [],
            "who_should_take_it": [],
            "disagreements": [],
        }
    )
    base = {
        "module_code": code,
        "facts": facts,
        "report": report,
        "docs": [facts_doc, review, blog, unused],
        "statuses": [
            SourceStatus(name="nusmods", ok=True, count=1),
            SourceStatus(name="disqus", ok=True, count=1),
            SourceStatus(name="reddit", ok=True, count=1, note="from search snippets"),
            SourceStatus(name="web", ok=False, error="TAVILY_API_KEY not set"),
        ],
        "provider": "openai_compat",
        "model": model,
        "answered_by": model,
        "prompt_version": PROMPT_VERSION,
        "usage": Usage(input_tokens=35057, output_tokens=3237),
        "cost_usd": 0.0,
        # Relative to now so the 14-day cache still treats it as fresh.
        "generated_at": datetime.now(UTC) - timedelta(hours=1),
        "docs_collected": 5,
    }
    base.update(kw)
    return ReviewResult(**base)


def fake_runner(calls: list[dict]):
    """Stands in for pipeline.run_review in web tests: emits progress, returns a result."""
    from modreview.models import (
        InvalidModuleCode,
        ModuleNotFound,
        SourceStatus,
        normalise_module_code,
    )
    from modreview.review import ProgressEvent

    async def run(code, config, secrets, cache, *, model=None, force=False, progress=None, **_):
        calls.append({"code": code, "model": model, "force": force})
        code = normalise_module_code(code)
        if code == "ZZ9999":
            raise ModuleNotFound("ZZ9999 was not found on NUSMods.")
        if code == "BOOM1000":
            raise RuntimeError("kaboom")
        if progress:
            await progress(
                ProgressEvent(kind="stage", stage="fetching", message="Collecting sources")
            )
            await progress(
                ProgressEvent(
                    kind="source",
                    status=SourceStatus(name="disqus", ok=True, count=59),
                    message="NUSMods reviews: 59 reviews",
                )
            )
            await progress(ProgressEvent(kind="stage", stage="summarising", message="Summarising"))
        result = make_result(code, model or config.llm.model)
        cache.put(result)
        return result

    _ = InvalidModuleCode  # re-raised naturally by normalise_module_code
    return run


class FakeLLM:
    """Deterministic LLMProvider for tests: returns a canned report or raises."""

    name = "fake"

    def __init__(self, report: ReviewReport | None = None, error: Exception | None = None):
        self.report = report
        self.error = error
        self.calls: list[dict] = []

    async def list_models(self):
        return [ModelInfo(id="fake-1", display_name="Fake 1")]

    async def test_connection(self) -> str:
        return "ok"

    async def generate_structured(self, *, system, user, schema, model, max_tokens, temperature):
        self.calls.append({"system": system, "user": user, "model": model})
        if self.error:
            raise self.error
        return self.report, Usage(input_tokens=1000, output_tokens=200)
