# DECISIONS

Deviations from PLAN.md and choices the plan left open. Newest at the bottom of each milestone.

## M1: Skeleton and config

Checklist
- [x] Project layout matches PLAN §3
- [x] `pyproject.toml` with `uv` groups, `modreview` + `modreview-fetch` scripts
- [x] `.env.example`, `config.toml`, `.gitignore` (with `.env`)
- [x] Settings: secrets from `.env` (pydantic-settings), settings from `config.toml`, reloadable + writable
- [x] FastAPI app factory with empty home page
- [x] `uv run modreview` starts on 127.0.0.1 and opens the browser
- [x] `pytest` and `ruff` pass

Decisions
- **Two config objects, not one.** `Secrets` (pydantic-settings, `.env`) and `AppConfig` (pydantic model parsed from `config.toml` with `tomllib`). The settings page only ever writes `AppConfig` back, so a bug there can never touch keys.
- **`ConfigStore` holds the live `AppConfig`.** It has `reload()` and `save()`. Routes read config through the store on every request, so the settings page changes take effect without a restart (M5 acceptance check).
- **Port 8765** rather than 8000 to avoid colliding with other local dev servers. Configurable under `[server]` in `config.toml`.
- **`[limits].max_input_tokens_per_report` and `[server]` added to `config.toml`** beyond the plan's example, because §9 requires the cost guard to be configurable and the port has to live somewhere.
- **SSE is hand-rolled** on Starlette's `StreamingResponse` instead of adding `sse-starlette`. It is ~20 lines and one fewer dependency.
- **htmx 2.0.4 vendored** from unpkg into `static/htmx.min.js`.
- **uv installed via `pip install uv`** into the Python 3.12 Scripts dir (already on PATH), since it was not on the machine.
- **`[tool.uv] system-certs = true`** in `pyproject.toml`: uv's bundled roots could not verify pypi.org on this laptop (`UnknownIssuer`, something intercepts TLS). Using the Windows store fixes it without any per-shell env var.
- **Static preview config**: `.claude/launch.json` (repo) starts the server for the desktop app's browser pane with `--no-browser --debug`.

M1 accepted 2026-09-17: server up on 127.0.0.1:8765, `pytest` 8 passed, `ruff check` + `ruff format --check` clean.
