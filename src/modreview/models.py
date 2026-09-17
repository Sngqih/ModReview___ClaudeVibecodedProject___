"""Core pydantic models shared by sources, pipeline and UI."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

SourceName = Literal["nusmods", "disqus", "reddit", "web"]

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
