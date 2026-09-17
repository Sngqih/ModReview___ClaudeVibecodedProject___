"""Shared test helpers: fixtures dir, a fake search provider, a no-op sleep."""

from __future__ import annotations

import json
from pathlib import Path

from modreview.llm.base import ModelInfo, Usage
from modreview.models import Claim, LecturerNote, ReviewReport
from modreview.search.base import SearchHit

FIXTURES = Path(__file__).parent / "fixtures"


def load_json(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def load_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeSearch:
    name = "fake"

    def __init__(self, hits: list[SearchHit] | None = None, *, fail: bool = False) -> None:
        self.hits = hits or []
        self.fail = fail
        self.calls: list[dict] = []

    async def search(self, query, max_results, *, include_domains=None, exclude_domains=None):
        self.calls.append(
            {
                "query": query,
                "max_results": max_results,
                "include_domains": include_domains,
                "exclude_domains": exclude_domains,
            }
        )
        if self.fail:
            raise RuntimeError("search is down")
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
