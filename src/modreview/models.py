"""Core pydantic models shared by sources, pipeline and UI."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

SourceName = Literal["nusmods", "disqus", "reddit", "course_page", "blog", "web"]

MODULE_CODE_RE = re.compile(r"^[A-Z]{2,4}\d{4}[A-Z]{0,3}$")


class InvalidModuleCode(ValueError):
    pass


class ModuleNotFound(LookupError):
    pass


def normalise_module_code(raw: str) -> str:
    """Uppercase, strip, validate. Raises ``InvalidModuleCode`` before any network call."""
    code = (raw or "").strip().upper().replace(" ", "")
    if not MODULE_CODE_RE.match(code):
        raise InvalidModuleCode(
            f"{raw!r} is not a module code. Expected something like CS2040S or MA1521."
        )
    return code


def doc_id(url: str, chunk: str = "") -> str:
    h = hashlib.sha1(f"{url}\x00{chunk}".encode()).hexdigest()
    return f"d{h[:10]}"


class SourceDoc(BaseModel):
    id: str
    source: SourceName
    url: str
    title: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    text: str
    score: int | None = None
    # True when published_at was guessed from an academic year mentioned in the text.
    date_estimated: bool = False

    @field_validator("published_at")
    @classmethod
    def _utc(cls, v: datetime | None) -> datetime | None:
        """Sources mix naive dates (trafilatura) and UTC ones (Reddit, Disqus); comparing
        the two raises, so every date is stored as UTC-aware."""
        if v is None:
            return None
        return v.replace(tzinfo=UTC) if v.tzinfo is None else v.astimezone(UTC)

    @classmethod
    def make(cls, *, source: SourceName, url: str, text: str, chunk: str = "", **kw) -> SourceDoc:
        return cls(id=doc_id(url, chunk), source=source, url=url, text=text, **kw)


class ExamInfo(BaseModel):
    semester: int
    exam_date: datetime | None = None
    exam_duration_min: int | None = None


WORKLOAD_LABELS = ("Lecture", "Tutorial", "Lab", "Project", "Preparation")


class ModuleFacts(BaseModel):
    """Structured facts from the NUSMods API. Shown in the facts panel and passed to the LLM."""

    module_code: str
    title: str
    acad_year: str
    description: str = ""
    module_credit: str = ""
    faculty: str | None = None
    department: str | None = None
    grading_basis: str | None = None
    workload: list[float] | None = None
    prerequisite: str | None = None
    preclusion: str | None = None
    semesters: list[int] = Field(default_factory=list)
    exams: list[ExamInfo] = Field(default_factory=list)
    nusmods_url: str = ""

    @property
    def workload_pairs(self) -> list[tuple[str, float]]:
        if not self.workload:
            return []
        return list(zip(WORKLOAD_LABELS, self.workload, strict=False))

    @property
    def workload_total(self) -> float:
        return sum(self.workload) if self.workload else 0.0

    def to_text(self) -> str:
        lines = [
            f"{self.module_code} {self.title}",
            f"Academic year: {self.acad_year}",
            f"Credits: {self.module_credit} MCs",
        ]
        if self.faculty or self.department:
            lines.append(f"Faculty/department: {self.faculty or '?'} / {self.department or '?'}")
        if self.grading_basis:
            lines.append(f"Grading: {self.grading_basis}")
        if self.workload:
            parts = ", ".join(f"{label} {hrs:g}h" for label, hrs in self.workload_pairs)
            lines.append(f"Official workload per week: {parts} (total {self.workload_total:g}h)")
        if self.semesters:
            lines.append("Offered in semester(s): " + ", ".join(str(s) for s in self.semesters))
        for ex in self.exams:
            if ex.exam_date:
                dur = f", {ex.exam_duration_min} min" if ex.exam_duration_min else ""
                lines.append(f"Exam sem {ex.semester}: {ex.exam_date:%Y-%m-%d}{dur}")
            else:
                lines.append(f"Exam sem {ex.semester}: no exam listed")
        if self.prerequisite:
            lines.append(f"Prerequisite: {self.prerequisite}")
        if self.preclusion:
            lines.append(f"Preclusion: {self.preclusion}")
        if self.description:
            lines.append("")
            lines.append(self.description.strip())
        return "\n".join(lines)


class SourceStatus(BaseModel):
    """What happened to one source during a fetch. Rendered in the progress list and footer."""

    name: SourceName
    ok: bool
    count: int = 0
    note: str | None = None  # e.g. "fallback to search snippets"
    error: str | None = None  # e.g. "DISQUS_API_KEY not set"


class SourceResult(BaseModel):
    status: SourceStatus
    docs: list[SourceDoc] = Field(default_factory=list)


# --------------------------------------------------------------------------- LLM output


Sentiment = Literal[
    "very positive", "positive", "mixed", "negative", "very negative", "insufficient data"
]
Confidence = Literal["high", "medium", "low"]


class Claim(BaseModel):
    text: str = Field(description="One plain-language claim, 1-2 sentences.")
    source_ids: list[str] = Field(
        description="IDs of the <doc> elements that support this claim, e.g. ['d3a9f1c2e0']."
    )


class LecturerNote(BaseModel):
    name: str | None = Field(description="Lecturer name as written in the sources, or null.")
    semester: str | None = Field(description='Semester the note applies to, e.g. "AY24/25 S1".')
    notes: list[Claim]


class ReviewReport(BaseModel):
    """The structured review. Every claim must cite at least one <doc id>."""

    module_code: str
    one_line_verdict: str = Field(description="One sentence a student could read and move on.")
    overall_sentiment: Sentiment
    difficulty: Literal[1, 2, 3, 4, 5] | None = Field(
        description="1 = easy, 5 = very hard. null when the evidence is too thin."
    )
    workload_summary: list[Claim] = Field(
        description="Real hours vs the official workload numbers."
    )
    assessment_breakdown: list[Claim] = Field(
        description="Finals, midterms, projects, weightings, bell curve talk."
    )
    content_and_usefulness: list[Claim]
    lecturers_and_teaching: list[LecturerNote]
    common_complaints: list[Claim]
    tips_to_do_well: list[Claim]
    who_should_take_it: list[Claim]
    disagreements: list[Claim] = Field(description="Where sources conflict, side by side.")
    data_freshness: str = Field(description='e.g. "Most reviews from AY22/23 to AY24/25".')
    confidence: Confidence

    def claim_lists(self) -> list[list[Claim]]:
        out = [
            self.workload_summary,
            self.assessment_breakdown,
            self.content_and_usefulness,
            self.common_complaints,
            self.tips_to_do_well,
            self.who_should_take_it,
            self.disagreements,
        ]
        out.extend(note.notes for note in self.lecturers_and_teaching)
        return out

    def all_source_ids(self) -> set[str]:
        return {sid for claims in self.claim_lists() for c in claims for sid in c.source_ids}


def chunk_text(text: str, size: int) -> list[str]:
    """Split on paragraph boundaries into chunks of at most ``size`` chars."""
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for para in text.split("\n"):
        if buf_len + len(para) + 1 > size and buf:
            chunks.append("\n".join(buf))
            buf, buf_len = [], 0
        while len(para) > size:  # a single monster paragraph
            chunks.append(para[:size])
            para = para[size:]
        buf.append(para)
        buf_len += len(para) + 1
    if buf:
        chunks.append("\n".join(buf))
    return chunks
