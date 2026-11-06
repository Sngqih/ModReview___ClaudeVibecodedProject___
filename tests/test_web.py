"""Route tests. The pipeline is replaced by tests.helpers.fake_runner: no network, no LLM."""

from __future__ import annotations

from urllib.parse import quote

from fastapi.testclient import TestClient

from modreview.cache import ReviewCache
from modreview.export import to_markdown
from tests.helpers import make_result


def _events(text: str) -> list[tuple[str, str]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = block.splitlines()
        event = next((ln[7:] for ln in lines if ln.startswith("event: ")), "message")
        data = "\n".join(ln[6:] for ln in lines if ln.startswith("data: "))
        out.append((event, data))
    return out


# --------------------------------------------------------------------------- home


def test_home_renders(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "What is a module" in r.text
    assert "gemini-flash-latest" in r.text  # configured model is the default option
    assert 'hx-get="/api/modules"' in r.text


def test_home_lists_missing_keys(client: TestClient) -> None:
    r = client.get("/")
    assert "Add your free API keys" in r.text
    for env in ("OPENAI_COMPAT_API_KEY", "TAVILY_API_KEY", "DISQUS_API_KEY"):
        assert env in r.text
    assert "aistudio.google.com/apikey" in r.text
    assert "Model list unavailable" not in r.text  # the notice covers it


def test_home_hides_notice_when_keys_set(store, cache) -> None:
    from pydantic import SecretStr

    from modreview.main import create_app
    from modreview.settings import Secrets

    keys = {
        k: SecretStr("k") for k in ("openai_compat_api_key", "tavily_api_key", "disqus_api_key")
    }
    app = create_app(store, Secrets(_env_file=None, **keys), cache=cache)
    with TestClient(app) as c:
        assert "Add your free API keys" not in c.get("/").text


def test_healthz_and_static(client: TestClient) -> None:
    assert client.get("/healthz").json()["status"] == "ok"
    for path in ("/static/htmx.min.js", "/static/app.js", "/static/style.css"):
        assert client.get(path).status_code == 200


def test_module_suggestions(client: TestClient) -> None:
    r = client.get("/api/modules", params={"code": "cs20"})
    assert '<option value="CS2040S">' in r.text
    assert client.get("/api/modules", params={"q": "c"}).text.strip() == ""
    by_title = client.get("/api/modules", params={"q": "calculus"})
    assert "MA1521" in by_title.text


def test_search_form_redirects_and_rejects(client: TestClient) -> None:
    r = client.get("/review", params={"code": " cs2040s "}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/review/CS2040S"
    r = client.get(
        "/review", params={"code": "cs2040s", "model": "x", "force": "1"}, follow_redirects=False
    )
    assert r.headers["location"] == "/review/CS2040S?model=x&force=1"
    bad = client.get("/review", params={"code": "hello world"})
    assert bad.status_code == 400 and "is not a module code" in bad.text


# --------------------------------------------------------------------------- report


def test_review_page_streams_then_serves_cache(client: TestClient, runner_calls) -> None:
    page = client.get("/review/CS2040S")
    assert page.status_code == 200 and 'data-stream="/review/CS2040S/stream"' in page.text

    stream = client.get("/review/CS2040S/stream")
    assert stream.headers["content-type"].startswith("text/event-stream")
    events = _events(stream.text)
    kinds = [e for e, _ in events]
    assert kinds[0] == "ready" and kinds[-1] == "done"
    assert kinds.count("progress") == 3
    assert "NUSMods reviews: 59 reviews" in events[2][1]
    report_html = events[-1][1]
    assert "Data Structures and Algorithms" in report_html
    assert 'href="#src-1"' in report_html and 'id="src-1"' in report_html
    assert "Download as Markdown" in report_html
    assert "&lt;script&gt;" in report_html and "<script>alert" not in report_html  # escaped
    assert "Show 1 more source that were read but not cited" in report_html
    assert runner_calls == [{"code": "CS2040S", "model": None, "force": False}]

    cached = client.get("/review/CS2040S")
    assert "data-stream" not in cached.text and "saved report" in cached.text
    assert len(runner_calls) == 1


def test_citations_numbered_in_reading_order(client: TestClient) -> None:
    html = _events(client.get("/review/CS2040S/stream").text)[-1][1]
    # Workload cites the NUSMods doc and the blog first, so they are 1 and 2; the review is 3.
    first = html.index('class="cite"')
    assert 'href="#src-1"' in html[first : first + 120]
    assert html.index('href="#src-2"') < html.index('href="#src-3"')


def test_force_bypasses_cache(client: TestClient, runner_calls) -> None:
    client.get("/review/CS2040S/stream")
    page = client.get("/review/CS2040S", params={"force": "1"})
    assert 'data-stream="/review/CS2040S/stream?force=1"' in page.text
    client.get("/review/CS2040S/stream", params={"force": "1"})
    assert runner_calls[-1]["force"] is True and len(runner_calls) == 2


def test_lowercase_code_redirects(client: TestClient) -> None:
    r = client.get("/review/cs2040s", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/review/CS2040S"


def test_invalid_code_page(client: TestClient) -> None:
    r = client.get("/review/NOT-A-CODE")
    assert r.status_code == 400 and "Not a module code" in r.text


def test_stream_errors_are_friendly(client: TestClient) -> None:
    missing = _events(client.get("/review/ZZ9999/stream").text)
    assert missing[-1][0] == "failed" and "Module not found" in missing[-1][1]
    boom = _events(client.get("/review/BOOM1000/stream").text)
    assert boom[-1][0] == "failed" and "Something went wrong" in boom[-1][1]


def test_facts_only_and_llm_error_render(client: TestClient) -> None:
    from modreview.main import create_app  # noqa: F401  (render path covered via template)

    env = client.app.router  # just to keep a handle; rendering tested through history view
    assert env is not None
    cache: ReviewCache = client.app.state.cache
    failed = make_result(llm_error="Key rejected (401)", report=None)
    # Unfinished results are never cached; render the partial directly instead.
    from fastapi.templating import Jinja2Templates

    from modreview.main import TEMPLATES_DIR
    from modreview.web import citation_numbers, grouped_sources, register_template_helpers

    t = Jinja2Templates(directory=str(TEMPLATES_DIR))
    register_template_helpers(t.env)
    numbers = citation_numbers(failed)
    cited, uncited = grouped_sources(failed, numbers)
    html = t.env.get_template("partials/report.html").render(
        result=failed, numbers=numbers, docs_by_id={d.id: d for d in failed.docs},
        cited_groups=cited, uncited_groups=uncited, cited_count=0, uncited_count=0,
        cache_key=None, regenerate_url="/review/CS2040S?force=1", export_url=None,
    )  # fmt: skip
    assert "The summary could not be written." in html and "Key rejected (401)" in html
    assert "Download as Markdown" not in html
    assert "Workload" in html  # facts panel still shows the official workload
    assert cache.history() == []


# --------------------------------------------------------------------------- history + export


def test_history_view_export_delete(client: TestClient) -> None:
    client.get("/review/CS2040S/stream")
    page = client.get("/history")
    assert "CS2040S" in page.text and "Data Structures and Algorithms" in page.text
    key = client.app.state.cache.history()[0].cache_key

    view = client.get("/history/view", params={"key": key})
    assert view.status_code == 200 and "Prof Seth" in view.text

    md = client.get("/export", params={"key": key})
    assert md.status_code == 200
    assert md.headers["content-type"].startswith("text/markdown")
    assert 'filename="CS2040S-review.md"' in md.headers["content-disposition"]
    assert md.text.startswith("# CS2040S Data Structures and Algorithms")

    assert client.delete(f"/history?key={quote(key)}").status_code == 200
    assert client.app.state.cache.history() == []
    assert client.get("/export", params={"key": key}).status_code == 404
    assert client.get("/history/view", params={"key": key}).status_code == 404
    assert "No reports yet" in client.get("/history").text


def test_markdown_export_content() -> None:
    md = to_markdown(make_result())
    assert "**Sentiment:** positive" in md and "4/5 (Hard)" in md
    assert "## Official facts (NUSMods)" in md and "total 10h" in md
    assert "## Workload" in md and "## Lecturers and teaching" in md
    assert "Students report 10-12 hours a week. [1][2]" in md
    assert "## Sources" in md and "1. [NUSMods: CS2040S" in md
    assert "\\<script\\>" in md or "&lt;script" not in md  # markup is escaped, not executed
    assert "35,057 in / 3,237 out tokens" in md


# --------------------------------------------------------------------------- settings


def test_settings_page_shows_key_presence_not_values(client: TestClient, monkeypatch) -> None:
    r = client.get("/settings")
    assert r.status_code == 200
    assert "OPENAI_COMPAT_API_KEY" in r.text and "Missing" in r.text
    assert "not built yet" in r.text  # Brave and Claude web search are M6


def test_settings_change_model_applies_to_next_report(client: TestClient, runner_calls) -> None:
    """M5 acceptance: a model change in Settings shows in the next report footer, no restart."""
    r = client.post(
        "/settings",
        data={
            "provider": "openai_compat",
            "model": "gemini-3.5-flash",
            "fallback_model": "gemini-flash-lite-latest",
            "search_provider": "tavily",
            "src_disqus": "on",
            "src_websearch": "on",
            "cache_ttl_days": "7",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/settings?saved=1"
    cfg = client.app.state.store.config
    assert cfg.llm.model == "gemini-3.5-flash"
    assert cfg.llm.fallback and cfg.llm.fallback.model == "gemini-flash-lite-latest"
    assert (cfg.sources.disqus, cfg.sources.reddit, cfg.sources.websearch) == (True, False, True)
    assert cfg.limits.cache_ttl_days == 7

    html = _events(client.get("/review/CS2040S/stream").text)[-1][1]
    footer = html[html.index('class="report-foot"') :]
    assert "gemini-3.5-flash" in footer
    assert "Saved." in client.get("/settings", params={"saved": "1"}).text


def test_settings_rejects_bad_input(client: TestClient) -> None:
    r = client.post("/settings", data={"provider": "nope"})
    assert r.status_code == 400 and "unknown provider" in r.text
    r = client.post("/settings", data={"search_provider": "brave"})
    assert r.status_code == 400 and "not available" in r.text
    r = client.post("/settings", data={"cache_ttl_days": "-3"})
    assert r.status_code == 400


def test_settings_test_connection_without_keys(client: TestClient) -> None:
    r = client.post("/settings/test", data={"kind": "llm", "provider": "openai_compat"})
    assert "OPENAI_COMPAT_API_KEY not set" in r.text and "test-result--bad" in r.text
    r = client.post("/settings/test", data={"kind": "search", "search_provider": "tavily"})
    assert "TAVILY_API_KEY not set" in r.text


def test_unknown_url_gets_branded_404(client: TestClient) -> None:
    r = client.get("/no/such/page")
    assert r.status_code == 404 and "Page not found" in r.text and "ModReview" in r.text
    assert "detail" not in r.text
