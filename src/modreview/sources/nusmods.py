"""NUSMods official API (no key). Structured facts + module list for autocomplete."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from modreview.http import get_with_retry
from modreview.models import ExamInfo, ModuleFacts, ModuleNotFound, SourceDoc

log = logging.getLogger(__name__)

API_BASE = "https://api.nusmods.com/v2"
MODULE_LIST_TTL_S = 24 * 3600


def current_acad_year(today: date | None = None) -> str:
    """NUS academic year starts in August: Sept 2026 -> '2026-2027', Mar 2026 -> '2025-2026'."""
    today = today or date.today()
    start = today.year if today.month >= 8 else today.year - 1
    return f"{start}-{start + 1}"


def previous_acad_year(ay: str) -> str:
    start = int(ay.split("-")[0]) - 1
    return f"{start}-{start + 1}"


def _parse_workload(raw) -> list[float] | None:
    if raw is None:
        return None
    if isinstance(raw, list):
        try:
            return [float(x) for x in raw]
        except (TypeError, ValueError):
            return None
    if isinstance(raw, str):  # some modules use "2-1-0-3-4"
        try:
            return [float(x) for x in raw.replace("–", "-").split("-")]
        except ValueError:
            return None
    return None


_GLUED_LOGIC_RE = re.compile(r"(?<=[A-Za-z0-9)])(THEN|AND|OR)(?=[a-z(])")


def tidy_requirement(text: str | None) -> str | None:
    """NUSMods glues its logic words to neighbours ("DegreeTHEN(", "DANDmust"); space them."""
    if not text:
        return None
    text = _GLUED_LOGIC_RE.sub(r" \1 ", text)
    text = re.sub(r"\(\s+", "(", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_module(data: dict, acad_year: str) -> ModuleFacts:
    code = data["moduleCode"]
    sems = sorted({int(s["semester"]) for s in data.get("semesterData", []) if "semester" in s})
    exams = [
        ExamInfo(
            semester=int(s["semester"]),
            exam_date=_parse_dt(s.get("examDate")),
            exam_duration_min=s.get("examDuration"),
        )
        for s in data.get("semesterData", [])
        if "semester" in s
    ]
    return ModuleFacts(
        module_code=code,
        title=data.get("title", code),
        acad_year=data.get("acadYear", acad_year),
        description=data.get("description", "") or "",
        module_credit=str(data.get("moduleCredit", "")),
        faculty=data.get("faculty"),
        department=data.get("department"),
        grading_basis=data.get("gradingBasisDescription"),
        workload=_parse_workload(data.get("workload")),
        prerequisite=tidy_requirement(data.get("prerequisite")),
        preclusion=tidy_requirement(data.get("preclusion")),
        semesters=sems,
        exams=exams,
        nusmods_url=f"https://nusmods.com/courses/{code}",
    )


def facts_to_doc(facts: ModuleFacts) -> SourceDoc:
    return SourceDoc.make(
        source="nusmods",
        url=facts.nusmods_url,
        title=f"NUSMods: {facts.module_code} {facts.title}",
        text=facts.to_text(),
        published_at=None,
    )


class NUSModsClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        data_dir: Path | None = None,
        base_url: str = API_BASE,
    ) -> None:
        self._client = client
        self._data_dir = data_dir
        self._base = base_url

    async def get_module(self, code: str, acad_year: str | None = None) -> ModuleFacts:
        """Current AY first, then the previous one. Raises ``ModuleNotFound`` after both 404."""
        ay = acad_year or current_acad_year()
        for candidate in (ay, previous_acad_year(ay)):
            url = f"{self._base}/{candidate}/modules/{code}.json"
            resp = await get_with_retry(self._client, url)
            if resp.status_code == 404:
                log.info("NUSMods: %s not in %s", code, candidate)
                continue
            resp.raise_for_status()
            return parse_module(resp.json(), candidate)
        raise ModuleNotFound(f"{code} was not found on NUSMods for {ay} or the year before.")

    async def module_list(self, acad_year: str | None = None) -> list[dict]:
        """``[{moduleCode, title, semesters}]`` cached on disk for a day."""
        ay = acad_year or current_acad_year()
        cache = self._data_dir / f"moduleList_{ay}.json" if self._data_dir else None
        if cache and cache.exists() and time.time() - cache.stat().st_mtime < MODULE_LIST_TTL_S:
            return json.loads(cache.read_text(encoding="utf-8"))
        resp = await get_with_retry(self._client, f"{self._base}/{ay}/moduleList.json")
        if resp.status_code == 404:
            ay = previous_acad_year(ay)
            resp = await get_with_retry(self._client, f"{self._base}/{ay}/moduleList.json")
        resp.raise_for_status()
        data = resp.json()
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(data), encoding="utf-8")
        return data


class NUSModsSource:
    name = "nusmods"

    def __init__(self, api: NUSModsClient) -> None:
        self._api = api
        self.note: str | None = None
        self.facts: ModuleFacts | None = None

    async def fetch(self, module_code: str, module_title: str | None = None) -> list[SourceDoc]:
        self.note = None
        self.facts = await self._api.get_module(module_code)
        return [facts_to_doc(self.facts)]


def now_utc() -> datetime:
    return datetime.now(UTC)
