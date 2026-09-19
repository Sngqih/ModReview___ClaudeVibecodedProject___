"""Compare page tests (fake pipeline from tests.helpers; no network)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.helpers import make_result
from tests.test_web import _events


def test_compare_empty_and_half_filled_forms(client: TestClient) -> None:
    empty = client.get("/compare")
    assert empty.status_code == 200 and "Two modules, side by side" in empty.text
    assert 'aria-current="page">Compare' in empty.text
    half = client.get("/compare", params={"a": "cs2040s"})
    assert half.status_code == 200 and 'value="CS2040S"' in half.text
    assert "compare__col" not in half.text


def test_compare_rejects_bad_input(client: TestClient) -> None:
    bad = client.get("/compare", params={"a": "CS2040S", "b": "nonsense"})
    assert bad.status_code == 400 and "is not a module code" in bad.text
    same = client.get("/compare", params={"a": "cs2040s", "b": "CS2040S"})
    assert same.status_code == 400 and "Pick two different modules" in same.text


def test_compare_uses_cache_and_streams_the_rest(client: TestClient, runner_calls) -> None:
    client.app.state.cache.put(make_result("CS2040S"))
    page = client.get("/compare", params={"a": "cs2040s", "b": "cs2040c"})
    assert page.status_code == 200
    html = page.text
    assert html.count('class="compare__col"') == 2
    # CS2040S is cached: rendered straight away as 8 aligned cells.
    first_col = html[html.index('aria-label="CS2040S"') : html.index('aria-label="CS2040C"')]
    assert first_col.count('class="compare__cell') == 8
    assert "Hard but foundational" in first_col and "Full report with sources" in first_col
    # CS2040C is not: its column streams with the compact view.
    assert 'data-stream="/review/CS2040C/stream?view=compare"' in html
    assert runner_calls == []

    events = _events(client.get("/review/CS2040C/stream", params={"view": "compare"}).text)
    assert events[-1][0] == "done"
    column = events[-1][1]
    assert column.count('class="compare__cell') == 8 and "report-foot" not in column
    assert runner_calls[-1]["code"] == "CS2040C"


def test_autocomplete_accepts_compare_field_names(client: TestClient) -> None:
    assert '<option value="CS2040S">' in client.get("/api/modules", params={"b": "cs20"}).text


def test_report_links_to_compare(client: TestClient) -> None:
    html = _events(client.get("/review/CS2040S/stream").text)[-1][1]
    assert 'href="/compare?a=CS2040S"' in html
