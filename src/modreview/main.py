"""FastAPI app factory, routes, and the ``modreview`` entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import threading
import webbrowser
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from modreview import __version__
from modreview.cache import ReviewCache, cache_key
from modreview.export import to_markdown
from modreview.http import make_client
from modreview.llm.prompt import PROMPT_VERSION
from modreview.llm.registry import LLM_PROVIDER_NAMES, make_llm_provider
from modreview.models import InvalidModuleCode, ModuleNotFound, normalise_module_code
from modreview.pipeline import run_review
from modreview.review import ProgressEvent, ReviewResult
from modreview.search.registry import SEARCH_PROVIDER_NAMES, is_built, make_search_provider
from modreview.settings import (
    AppConfig,
    ConfigStore,
    LimitsConfig,
    LLMFallback,
    Secrets,
    SourcesConfig,
    config_path_from_env,
    data_dir_from_env,
)
from modreview.sources.nusmods import NUSModsClient
from modreview.web import (
    ModelCatalog,
    ModuleIndex,
    citation_numbers,
    grouped_sources,
    register_template_helpers,
)

log = logging.getLogger(__name__)

PKG_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PKG_DIR / "templates"
STATIC_DIR = PKG_DIR / "static"

KEY_LABELS = [
    ("openai_compat_api_key", "OPENAI_COMPAT_API_KEY", "Gemini (OpenAI-compatible)", True),
    ("tavily_api_key", "TAVILY_API_KEY", "Tavily web search", True),
    ("disqus_api_key", "DISQUS_API_KEY", "Disqus (NUSMods reviews)", True),
    ("anthropic_api_key", "ANTHROPIC_API_KEY", "Anthropic Claude", False),
    ("brave_api_key", "BRAVE_API_KEY", "Brave search", False),
    ("reddit_client_id", "REDDIT_CLIENT_ID", "Reddit OAuth", False),
]
PROVIDER_LABELS = {"openai_compat": "OpenAI-compatible (Gemini)", "anthropic": "Anthropic"}
SEARCH_LABELS = {
    "tavily": "Tavily",
    "brave": "Brave",
    "claude_websearch": "Claude web search",
    "none": "None (no web or Reddit)",
}


def sse(event: str, data: str) -> str:
    """One Server-Sent Event. Multi-line HTML becomes several ``data:`` lines."""
    lines = data.splitlines() or [""]
    return f"event: {event}\n" + "".join(f"data: {line}\n" for line in lines) + "\n"


def create_app(
    store: ConfigStore | None = None,
    secrets: Secrets | None = None,
    *,
    cache: ReviewCache | None = None,
    module_loader=None,
    runner=None,
) -> FastAPI:
    store = store or ConfigStore(config_path_from_env())
    secrets = secrets or Secrets()
    cache = cache or ReviewCache(data_dir_from_env() / "modreview.db")
    runner = runner or run_review

    async def default_module_loader() -> list[dict]:
        async with make_client(store.config.limits.http_timeout_s) as client:
            return await NUSModsClient(client, data_dir=data_dir_from_env()).module_list()

    modules = ModuleIndex(module_loader or default_module_loader)
    catalog = ModelCatalog(secrets)

    app = FastAPI(title="ModReview", version=__version__, docs_url=None, redoc_url=None)
    app.state.store, app.state.secrets, app.state.cache = store, secrets, cache

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.autoescape = True  # never render scraped text raw
    templates.env.globals["app_version"] = __version__
    # Cache-busting token for /static: changes whenever a static file changes.
    templates.env.globals["asset_v"] = str(
        int(max(f.stat().st_mtime for f in STATIC_DIR.iterdir() if f.is_file()))
    )
    register_template_helpers(templates.env)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def render(request: Request, name: str, ctx: dict[str, Any], status: int = 200):
        ctx.setdefault("nav", "")
        return templates.TemplateResponse(request, name, ctx, status_code=status)

    def render_str(name: str, ctx: dict[str, Any]) -> str:
        return templates.env.get_template(name).render(**ctx)

    def review_url(code: str, model: str | None = None, force: bool = False) -> str:
        params: dict[str, str] = {}
        if model and model != store.config.llm.model:
            params["model"] = model
        if force:
            params["force"] = "1"
        return f"/review/{code}" + (f"?{urlencode(params)}" if params else "")

    def report_ctx(result: ReviewResult) -> dict[str, Any]:
        key = None
        if result.report is not None and result.provider and result.model:
            key = cache_key(
                result.module_code, result.provider, result.model, result.prompt_version
            )
        numbers = citation_numbers(result)
        cited_groups, uncited_groups = grouped_sources(result, numbers)
        return {
            "result": result,
            "numbers": numbers,
            "docs_by_id": {d.id: d for d in result.docs},
            "cited_groups": cited_groups,
            "uncited_groups": uncited_groups,
            "cited_count": sum(len(g[1]) for g in cited_groups),
            "uncited_count": sum(len(g[1]) for g in uncited_groups),
            "cache_key": key,
            "regenerate_url": review_url(result.module_code, result.model, force=True),
            "export_url": f"/export?{urlencode({'key': key})}" if key else None,
        }

    async def home_ctx(error: str | None = None, prefill: str = "") -> dict[str, Any]:
        cfg = store.config
        models, model_error = await catalog.options(cfg.llm.provider, cfg.llm.model, cfg.llm)
        return {
            "nav": "search",
            "cfg": cfg,
            "models": models,
            "model_error": model_error,
            "recent": cache.recent_codes(8),
            "error": error,
            "prefill": prefill,
        }

    # ------------------------------------------------------------------ home + search

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        return render(request, "index.html", await home_ctx())

    @app.get("/api/modules", response_class=HTMLResponse)
    async def module_suggestions(request: Request):
        # htmx sends the input's own name: "code" on the home page, "a"/"b" on Compare.
        params = request.query_params
        query = next((params[k] for k in ("q", "code", "a", "b") if params.get(k)), "")
        mods = await modules.search(query)
        return render(request, "partials/suggestions.html", {"mods": mods})

    @app.get("/review")
    async def review_form(request: Request, code: str = "", model: str = "", force: str = ""):
        try:
            norm = normalise_module_code(code)
        except InvalidModuleCode as exc:
            return render(request, "index.html", await home_ctx(str(exc), code), status=400)
        return RedirectResponse(review_url(norm, model or None, bool(force)), status_code=303)

    # ------------------------------------------------------------------ report

    @app.get("/review/{code}", response_class=HTMLResponse)
    async def review_page(request: Request, code: str, model: str = "", force: str = ""):
        try:
            norm = normalise_module_code(code)
        except InvalidModuleCode as exc:
            return render(
                request, "error.html", {"title": "Not a module code", "message": str(exc)}, 400
            )
        if norm != code:
            return RedirectResponse(review_url(norm, model or None, bool(force)), 301)
        cfg = store.config
        model_name = model or cfg.llm.model
        if not force:
            hit = cache.get(
                norm, cfg.llm.provider, model_name, PROMPT_VERSION, cfg.limits.cache_ttl_days
            )
            if hit is not None:
                ctx = report_ctx(hit.model_copy(update={"from_cache": True}))
                return render(request, "review.html", {**ctx, "code": norm, "nav": "search"})
        params = {k: v for k, v in (("model", model), ("force", force)) if v}
        stream_url = f"/review/{norm}/stream" + (f"?{urlencode(params)}" if params else "")
        return render(
            request,
            "review.html",
            {"result": None, "code": norm, "model": model_name, "stream_url": stream_url},
        )

    @app.get("/review/{code}/stream")
    async def review_stream(code: str, model: str = "", force: str = "", view: str = ""):
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        async def progress(ev: ProgressEvent) -> None:
            await queue.put(("progress", ev))

        async def worker() -> None:
            try:
                result = await runner(
                    code,
                    store.config,
                    secrets,
                    cache,
                    model=model or None,
                    force=bool(force),
                    progress=progress,
                )
                await queue.put(("result", result))
            except InvalidModuleCode as exc:
                await queue.put(("error", ("Not a module code", str(exc))))
            except ModuleNotFound as exc:
                await queue.put(("error", ("Module not found", str(exc))))
            except Exception as exc:  # the page must always get an answer
                log.exception("review of %s failed", code)
                await queue.put(("error", ("Something went wrong", f"{type(exc).__name__}: {exc}")))

        task = asyncio.create_task(worker())

        async def events() -> AsyncIterator[str]:
            try:
                yield sse("ready", "")  # flush headers so the browser shows progress at once
                while True:
                    kind, payload = await queue.get()
                    if kind == "progress":
                        yield sse(
                            "progress", render_str("partials/progress_item.html", {"ev": payload})
                        )
                    elif kind == "result":
                        if view == "compare":
                            html = render_str("partials/compare_column.html", {"result": payload})
                        else:
                            html = render_str("partials/report.html", report_ctx(payload))
                        yield sse("done", html)
                        return
                    else:
                        title, message = payload
                        html = render_str(
                            "partials/stream_error.html", {"title": title, "message": message}
                        )
                        yield sse("failed", html)
                        return
            finally:
                if not task.done():
                    task.cancel()  # the browser went away; stop fetching

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/compare", response_class=HTMLResponse)
    async def compare(request: Request, a: str = "", b: str = ""):
        cfg = store.config
        ctx: dict[str, Any] = {"nav": "compare", "a": a, "b": b, "columns": [], "error": None}
        if not (a.strip() and b.strip()):  # empty or half-filled: show the form, prefilled
            ctx["a"], ctx["b"] = a.strip().upper(), b.strip().upper()
            return render(request, "compare.html", ctx)
        try:
            codes = [normalise_module_code(a), normalise_module_code(b)]
        except InvalidModuleCode as exc:
            ctx["error"] = str(exc)
            return render(request, "compare.html", ctx, 400)
        if codes[0] == codes[1]:
            ctx["error"] = "Pick two different modules."
            return render(request, "compare.html", ctx, 400)
        for code in codes:
            hit = cache.get(
                code, cfg.llm.provider, cfg.llm.model, PROMPT_VERSION, cfg.limits.cache_ttl_days
            )
            ctx["columns"].append(
                {
                    "code": code,
                    "result": hit,
                    "stream_url": None if hit else f"/review/{code}/stream?view=compare",
                }
            )
        ctx["a"], ctx["b"] = codes
        ctx["model"] = cfg.llm.model
        return render(request, "compare.html", ctx)

    @app.get("/export")
    async def export(key: str):
        result = cache.get_by_key(key)
        if result is None:
            return Response("That report is no longer cached.", status_code=404)
        filename = f"{result.module_code}-review.md"
        return Response(
            to_markdown(result),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # ------------------------------------------------------------------ history

    @app.get("/history", response_class=HTMLResponse)
    async def history(request: Request):
        return render(request, "history.html", {"nav": "history", "rows": cache.history()})

    @app.get("/history/view", response_class=HTMLResponse)
    async def history_view(request: Request, key: str):
        result = cache.get_by_key(key)
        if result is None:
            return render(
                request,
                "error.html",
                {"title": "Report not found", "message": "It may have been deleted."},
                404,
            )
        ctx = report_ctx(result.model_copy(update={"from_cache": True}))
        return render(request, "review.html", {**ctx, "code": result.module_code})

    @app.delete("/history", response_class=HTMLResponse)
    async def history_delete(key: str):
        cache.delete(key)
        return HTMLResponse("")  # htmx swaps the row out

    # ------------------------------------------------------------------ settings

    def settings_ctx(**extra) -> dict[str, Any]:
        present = secrets.present()
        keys = [
            {"env": env, "label": label, "set": present.get(attr, False), "required": req}
            for attr, env, label, req in KEY_LABELS
        ]
        return {
            "nav": "settings",
            "cfg": store.config,
            "keys": keys,
            "providers": [(p, PROVIDER_LABELS.get(p, p)) for p in LLM_PROVIDER_NAMES],
            "search_providers": [(p, SEARCH_LABELS[p], is_built(p)) for p in SEARCH_PROVIDER_NAMES],
            **extra,
        }

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, saved: str = ""):
        cfg = store.config
        models, model_error = await catalog.options(cfg.llm.provider, cfg.llm.model, cfg.llm)
        fb = cfg.llm.fallback.model if cfg.llm.fallback else ""
        return render(
            request,
            "settings.html",
            settings_ctx(
                models=models,
                model_error=model_error,
                fallback_models=["", *([fb] if fb else []), *[m for m in models if m != fb]],
                saved=bool(saved),
            ),
        )

    @app.get("/settings/models", response_class=HTMLResponse)
    async def settings_models(request: Request, provider: str, name: str = "model"):
        cfg = store.config
        current = cfg.llm.model if provider == cfg.llm.provider else ""
        models, error = await catalog.options(provider, current, cfg.llm)
        return render(
            request,
            "partials/model_select.html",
            {"name": name, "models": models, "selected": current, "error": error},
        )

    @app.post("/settings", response_class=HTMLResponse)
    async def settings_save(request: Request):
        form = await request.form()
        cfg = store.config
        try:
            provider = str(form.get("provider") or cfg.llm.provider)
            if provider not in LLM_PROVIDER_NAMES:
                raise ValueError(f"unknown provider {provider!r}")
            model = str(form.get("model") or "").strip() or cfg.llm.model
            fallback = str(form.get("fallback_model") or "").strip()
            search_provider = str(form.get("search_provider") or cfg.search.provider)
            if search_provider not in SEARCH_PROVIDER_NAMES or not is_built(search_provider):
                raise ValueError(f"search provider {search_provider!r} is not available")
            new = AppConfig.model_validate(
                {
                    **cfg.model_dump(),
                    "llm": {
                        **cfg.llm.model_dump(),
                        "provider": provider,
                        "model": model,
                        "fallback": LLMFallback(provider=provider, model=fallback).model_dump()
                        if fallback and fallback != model
                        else None,
                    },
                    "search": {**cfg.search.model_dump(), "provider": search_provider},
                    "sources": SourcesConfig(
                        nusmods=True,
                        disqus="src_disqus" in form,
                        reddit="src_reddit" in form,
                        websearch="src_websearch" in form,
                    ).model_dump(),
                    "limits": LimitsConfig.model_validate(
                        {
                            **cfg.limits.model_dump(),
                            "cache_ttl_days": int(
                                form.get("cache_ttl_days") or cfg.limits.cache_ttl_days
                            ),
                        }
                    ).model_dump(),
                }
            )
        except (ValueError, ValidationError) as exc:
            models, model_error = await catalog.options(cfg.llm.provider, cfg.llm.model, cfg.llm)
            return render(
                request,
                "settings.html",
                settings_ctx(
                    models=models,
                    model_error=model_error,
                    fallback_models=["", *models],
                    error=str(exc),
                ),
                400,
            )
        store.save(new)
        if provider != cfg.llm.provider:
            catalog.invalidate()
        log.info("settings saved: %s/%s, search=%s", provider, model, search_provider)
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.post("/settings/test", response_class=HTMLResponse)
    async def settings_test(request: Request):
        form = await request.form()
        kind = str(form.get("kind") or "llm")
        name = str(form.get("search_provider") if kind == "search" else form.get("provider") or "")
        ok, message = False, ""
        try:
            if kind == "llm":
                provider, why = make_llm_provider(name, secrets, store.config.llm)
                if provider is None:
                    message = why or "unavailable"
                else:
                    message = await asyncio.wait_for(provider.test_connection(), 20)
                    ok = True
            else:
                async with make_client(15) as client:
                    search, why = make_search_provider(name, secrets, client)
                    if search is None:
                        message = why or "unavailable"
                    else:
                        message = await asyncio.wait_for(search.test_connection(), 20)
                        ok = True
        except Exception as exc:
            message = str(exc)[:300] or type(exc).__name__
        return render(request, "partials/test_result.html", {"ok": ok, "message": message})

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app


def _open_browser_later(url: str, delay: float = 1.2) -> None:
    def _open() -> None:
        webbrowser.open(url)

    threading.Timer(delay, _open).start()


def run() -> None:
    """``uv run modreview`` - start the server on 127.0.0.1 and open the browser.

    ``uv run modreview fetch CODE`` and ``uv run modreview review CODE`` run the debug CLIs
    in ``cli.py`` instead.
    """
    if len(sys.argv) > 1 and sys.argv[1] in ("fetch", "review"):
        from modreview import cli

        sub = sys.argv[1]
        sys.argv = [f"{sys.argv[0]} {sub}", *sys.argv[2:]]
        (cli.fetch_main if sub == "fetch" else cli.review_main)()
        return

    import uvicorn

    parser = argparse.ArgumentParser(
        prog="modreview",
        epilog="subcommands: 'modreview fetch CODE' (sources only), "
        "'modreview review CODE' (full report in the terminal)",
    )
    parser.add_argument(
        "--debug", action="store_true", help="verbose logs, source counts, prompt size"
    )
    parser.add_argument("--port", type=int, default=None, help="override [server].port")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # Never log API keys. The Disqus key travels in a query string, and httpx logs full
    # request URLs at INFO, so httpx/httpcore are held at WARNING even under --debug.
    for noisy in ("httpx", "httpcore", "trafilatura", "htmldate", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    store = ConfigStore(config_path_from_env())
    host = "127.0.0.1"  # hard-wired: this app is never exposed beyond the laptop
    port = args.port or store.config.server.port
    url = f"http://{host}:{port}/"

    if not args.no_browser:
        _open_browser_later(url)
    log.info("ModReview %s at %s", __version__, url)
    uvicorn.run(
        create_app(store), host=host, port=port, log_level="debug" if args.debug else "info"
    )
