from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from modreview.http import make_client
from modreview.models import ModuleNotFound
from modreview.sources.nusmods import (
    API_BASE,
    NUSModsClient,
    NUSModsSource,
    current_acad_year,
    facts_to_doc,
    parse_module,
    previous_acad_year,
)
from tests.helpers import load_json


@pytest.mark.parametrize(
    ("today", "ay"),
    [
        (date(2026, 9, 17), "2026-2027"),
        (date(2026, 8, 1), "2026-2027"),
        (date(2026, 7, 31), "2025-2026"),
        (date(2027, 1, 5), "2026-2027"),
    ],
)
def test_current_acad_year(today: date, ay: str) -> None:
    assert current_acad_year(today) == ay


def test_previous_acad_year() -> None:
    assert previous_acad_year("2026-2027") == "2025-2026"


def test_parse_real_cs2040s_fixture() -> None:
    facts = parse_module(load_json("nusmods_CS2040S.json"), "2026-2027")
    assert facts.title == "Data Structures and Algorithms"
    assert facts.module_credit == "4"
    assert facts.workload == [3, 0, 1, 3, 3]
    assert facts.workload_total == 10
    assert facts.semesters == [1, 2]
    assert facts.faculty == "Computing"
    assert "CS1010" in (facts.prerequisite or "")
    assert facts.exams and facts.exams[0].semester == 1
    text = facts.to_text()
    assert "Lecture 3h" in text and "Preparation 3h" in text
    assert "Prerequisite:" in text
    doc = facts_to_doc(facts)
    assert doc.source == "nusmods" and doc.url.endswith("/CS2040S")


def test_parse_ma1521_and_cs1101s() -> None:
    for name in ("nusmods_MA1521.json", "nusmods_CS1101S.json"):
        facts = parse_module(load_json(name), "2026-2027")
        assert facts.title and facts.description
        assert facts.workload is not None and len(facts.workload) == 5


def test_parse_string_workload() -> None:
    data = load_json("nusmods_MA1521.json")
    data["workload"] = "3-1-0-0-6"
    assert parse_module(data, "2026-2027").workload == [3, 1, 0, 0, 6]


@respx.mock
async def test_get_module_falls_back_to_previous_ay() -> None:
    respx.get(f"{API_BASE}/2026-2027/modules/CS2040S.json").mock(return_value=httpx.Response(404))
    respx.get(f"{API_BASE}/2025-2026/modules/CS2040S.json").mock(
        return_value=httpx.Response(200, json=load_json("nusmods_CS2040S.json"))
    )
    async with make_client() as client:
        facts = await NUSModsClient(client).get_module("CS2040S", acad_year="2026-2027")
    assert facts.module_code == "CS2040S"


@respx.mock
async def test_get_module_not_found_after_both_years() -> None:
    respx.get(url__regex=rf"{API_BASE}/\d{{4}}-\d{{4}}/modules/ZZ9999\.json").mock(
        return_value=httpx.Response(404)
    )
    async with make_client() as client:
        with pytest.raises(ModuleNotFound):
            await NUSModsSource(NUSModsClient(client)).fetch("ZZ9999")


@respx.mock
async def test_get_module_retries_on_503(monkeypatch) -> None:
    route = respx.get(f"{API_BASE}/2026-2027/modules/MA1521.json")
    route.side_effect = [
        httpx.Response(503),
        httpx.Response(200, json=load_json("nusmods_MA1521.json")),
    ]
    import modreview.http as http_mod

    async def fast(_):
        return None

    monkeypatch.setattr(http_mod.asyncio, "sleep", fast)
    async with make_client() as client:
        facts = await NUSModsClient(client).get_module("MA1521", acad_year="2026-2027")
    assert facts.title
    assert route.call_count == 2


@respx.mock
async def test_module_list_cached_on_disk(tmp_path: Path) -> None:
    sample = load_json("nusmods_moduleList_sample.json")
    route = respx.get(f"{API_BASE}/2026-2027/moduleList.json").mock(
        return_value=httpx.Response(200, json=sample)
    )
    async with make_client() as client:
        api = NUSModsClient(client, data_dir=tmp_path)
        first = await api.module_list("2026-2027")
        second = await api.module_list("2026-2027")
    assert first == second == sample
    assert route.call_count == 1
    cached = json.loads((tmp_path / "moduleList_2026-2027.json").read_text())
    assert len(cached) == len(sample)


def test_tidy_requirement_spaces_glued_logic_words() -> None:
    from modreview.sources.nusmods import tidy_requirement

    raw = (
        "If undertaking an Undergraduate DegreeTHEN( must have completed CS1010 "
        "at a grade of at least DANDmust have completed CS1231)"
    )
    assert tidy_requirement(raw) == (
        "If undertaking an Undergraduate Degree THEN (must have completed CS1010 "
        "at a grade of at least D AND must have completed CS1231)"
    )
    assert tidy_requirement("Standard ANDROID module") == "Standard ANDROID module"
    assert tidy_requirement(None) is None and tidy_requirement("") is None
    once = tidy_requirement(raw)
    assert tidy_requirement(once) == once  # safe to apply again at render time
