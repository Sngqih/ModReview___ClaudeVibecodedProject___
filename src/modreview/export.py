"""Markdown export of a finished report."""

from __future__ import annotations

import re

from modreview.review import ReviewResult
from modreview.web import (
    REPORT_SECTIONS,
    SOURCE_LABELS,
    citation_numbers,
    difficulty_label,
    exam_text,
    sgt,
)

_MD_SPECIAL = re.compile(r"([\[\]*_`<>])")


def _esc(text: str) -> str:
    return _MD_SPECIAL.sub(r"\\\1", " ".join(text.split()))


def _cites(ids: list[str], numbers: dict[str, int]) -> str:
    return "".join(f"[{numbers[i]}]" for i in ids if i in numbers)


def to_markdown(result: ReviewResult) -> str:
    f = result.facts
    r = result.report
    numbers = citation_numbers(result)
    out: list[str] = [f"# {f.module_code} {_esc(f.title)}", ""]

    if r is not None:
        diff = f"{r.difficulty}/5 ({difficulty_label(r.difficulty)})" if r.difficulty else "n/a"
        out += [
            f"> {_esc(r.one_line_verdict)}",
            "",
            f"**Sentiment:** {r.overall_sentiment} · **Difficulty:** {diff} · "
            f"**Confidence:** {r.confidence}",
            "",
            f"*{_esc(r.data_freshness)}*",
            "",
        ]
    elif result.facts_only_reason or result.llm_error:
        out += [f"> {_esc(result.facts_only_reason or result.llm_error or '')}", ""]

    out += ["## Official facts (NUSMods)", ""]
    dept = " / ".join(x for x in (f.faculty, f.department) if x)
    sems = ", ".join(f"Semester {s}" for s in f.semesters) or "not offered this year"
    out.append(f"- {f.module_credit} MCs · {dept or 'unknown department'} · {sems}")
    if f.workload:
        parts = ", ".join(f"{label} {hrs:g}h" for label, hrs in f.workload_pairs)
        out.append(f"- Workload per week: {parts} (total {f.workload_total:g}h)")
    for ex in f.exams:
        out.append(f"- Exam, semester {ex.semester}: {exam_text(ex)}")
    if f.prerequisite:
        out.append(f"- Prerequisite: {_esc(f.prerequisite)}")
    if f.preclusion:
        out.append(f"- Preclusion: {_esc(f.preclusion)}")
    out.append("")

    if r is not None:

        def section(title: str, claims) -> None:
            if not claims:
                return
            out.extend([f"## {title}", ""])
            out.extend(f"- {_esc(c.text)} {_cites(c.source_ids, numbers)}" for c in claims)
            out.append("")

        for title, attr in REPORT_SECTIONS[:3]:
            section(title, getattr(r, attr))
        if r.lecturers_and_teaching:
            out.extend(["## Lecturers and teaching", ""])
            for note in r.lecturers_and_teaching:
                who = " · ".join(x for x in (note.name, note.semester) if x) or "Unnamed"
                out.append(f"**{_esc(who)}**")
                out.append("")
                out.extend(f"- {_esc(c.text)} {_cites(c.source_ids, numbers)}" for c in note.notes)
                out.append("")
        for title, attr in REPORT_SECTIONS[3:]:
            section(title, getattr(r, attr))

    out.extend(["## Sources", ""])
    for d in sorted(result.docs, key=lambda d: numbers[d.id]):
        when = sgt(d.published_at, "%Y-%m-%d") if d.published_at else "undated"
        title = _esc(d.title or d.url)
        out.append(f"{numbers[d.id]}. [{title}]({d.url}) · {SOURCE_LABELS.get(d.source)} · {when}")
    out.append("")

    model = result.answered_by or result.model or "no model"
    tokens = ""
    if result.usage:
        tokens = f", {result.usage.input_tokens:,} in / {result.usage.output_tokens:,} out tokens"
    out += [
        "---",
        "",
        f"Generated {sgt(result.generated_at)} SGT by ModReview with {result.provider}/{model}, "
        f"prompt {result.prompt_version}{tokens}. "
        f"{len(result.docs)} of {result.docs_collected} collected documents were used.",
        "",
    ]
    return "\n".join(out)
