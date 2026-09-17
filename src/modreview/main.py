"""FastAPI app factory, routes, and the ``modreview`` entrypoint."""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import webbrowser
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from modreview import __version__
from modreview.settings import ConfigStore, Secrets, config_path_from_env

log = logging.getLogger(__name__)

PKG_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PKG_DIR / "templates"
STATIC_DIR = PKG_DIR / "static"


def create_app(store: ConfigStore | None = None, secrets: Secrets | None = None) -> FastAPI:
    store = store or ConfigStore(config_path_from_env())
    secrets = secrets or Secrets()

    app = FastAPI(title="ModReview", version=__version__, docs_url=None, redoc_url=None)
    app.state.store = store
    app.state.secrets = secrets

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.autoescape = True  # never render scraped text raw
    templates.env.globals["app_version"] = __version__
    app.state.templates = templates

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> HTMLResponse:
        cfg = store.config
        return templates.TemplateResponse(
            request,
            "index.html",
            {"model": cfg.llm.model, "provider": cfg.llm.provider},
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app


def _open_browser_later(url: str, delay: float = 1.0) -> None:
    def _open() -> None:
        webbrowser.open(url)

    threading.Timer(delay, _open).start()


def run() -> None:
    """``uv run modreview`` - start the server on 127.0.0.1 and open the browser.

    ``uv run modreview fetch CS2040S`` runs the source-debug CLI instead (see ``cli.py``).
    """
    if len(sys.argv) > 1 and sys.argv[1] == "fetch":
        from modreview.cli import fetch_main

        sys.argv = [f"{sys.argv[0]} fetch", *sys.argv[2:]]
        fetch_main()
        return

    import uvicorn

    parser = argparse.ArgumentParser(
        prog="modreview", epilog="subcommand: modreview fetch CODE  (debug: doc counts per source)"
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
    # Never log API keys: nothing in this app logs Secrets, and httpx logs stay at WARNING.
    logging.getLogger("httpx").setLevel(logging.WARNING)

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
