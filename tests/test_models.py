from __future__ import annotations

import pytest

from modreview.models import (
    InvalidModuleCode,
    SourceDoc,
    chunk_text,
    doc_id,
    normalise_module_code,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("cs2040s", "CS2040S"),
        (" ma1521 ", "MA1521"),
        ("GESS1025", "GESS1025"),
        ("CS 2040", "CS2040"),
    ],
)
def test_normalise_ok(raw: str, expected: str) -> None:
    assert normalise_module_code(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "CS", "2040S", "C2040S", "CS2040SXYZW", "hello world", "<b>x</b>"]
)
def test_normalise_rejects(raw: str) -> None:
    with pytest.raises(InvalidModuleCode):
        normalise_module_code(raw)


def test_doc_id_stable_and_chunk_sensitive() -> None:
    assert doc_id("https://a", "1") == doc_id("https://a", "1")
    assert doc_id("https://a", "1") != doc_id("https://a", "2")
    d = SourceDoc.make(source="web", url="https://a", text="x")
    assert d.id.startswith("d") and len(d.id) == 11


def test_chunk_text_splits_on_paragraphs() -> None:
    text = "\n".join(["para " * 20] * 10)  # 10 paras of 100 chars
    chunks = chunk_text(text, 350)
    assert len(chunks) == 4
    assert all(len(c) <= 350 for c in chunks)
    assert "\n".join(chunks) == text
    assert chunk_text("short", 100) == ["short"]
