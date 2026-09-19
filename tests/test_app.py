from __future__ import annotations

from fastapi.testclient import TestClient


def test_home_renders(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "ModReview" in r.text
    assert "gemini-flash-latest" in r.text


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json()["status"] == "ok"


def test_static_served(client: TestClient) -> None:
    assert client.get("/static/htmx.min.js").status_code == 200
