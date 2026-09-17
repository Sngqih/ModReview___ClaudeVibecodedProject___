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

## M2: Sources

Checklist
- [x] NUSMods source: facts + module list cache, AY fallback, code validation
- [x] Disqus source: ident -> link fallbacks, pagination, HTML strip, length filter
- [x] Web search source: Tavily provider, query dedupe, domain exclusions, trafilatura extraction, code filter
- [x] Reddit source: search discovery -> .json best effort -> snippet fallback; optional asyncpraw path
- [x] Debug CLI prints doc counts per source
- [x] Fixture tests for every source (respx, no network)
- [x] Real endpoint behaviour recorded below

Real endpoint behaviour (2026-09-17, no API keys available yet)
- **NUSMods**: `GET /v2/2026-2027/modules/{CODE}.json` returns 200 for CS2040S, CS1101S, MA1521; `ZZ9999` gives an nginx 404 HTML page. `moduleList.json` is ~560 KB with 6773 entries. The three responses are saved under `tests/fixtures/` verbatim; the module list is trimmed to a 27-entry sample.
- **Reddit public `.json`**: both `/r/nus/search.json` and `/r/nus.json` answer **403 with an HTML block page** from this network, even with the descriptive User-Agent. So on this laptop the Reddit source will always fall back to search snippets. The code still tries `.json` once per report (first thread only, then stops) in case Reddit relaxes it. Not circumvented.
- **Disqus / Tavily / Anthropic**: not yet verified live (keys pending). The Disqus selector order (`thread:ident` then two `thread:link` forms) is implemented; `DisqusSource.selector_used` records which one worked so the live check is a one-liner later.

Decisions
- **`uv run modreview fetch CODE` instead of `uv run modreview-fetch CODE`.** OneDrive refused to let uv create `modreview-fetch.exe` in `.venv/Scripts` (access denied on that exact filename only, works elsewhere). One console script with a `fetch` subcommand sidesteps it. Same flags: `-v` lists docs, `--json FILE` dumps them.
- **`truststore` for httpx.** Python's certifi bundle failed with `CERTIFICATE_VERIFY_FAILED` on this laptop (TLS is re-signed locally, Norton is installed). `http.make_client()` uses the Windows certificate store instead. Same root cause as the uv `system-certs` setting.
- **`SearchProvider.search()` takes `include_domains`/`exclude_domains`.** Tavily honours them natively; providers without native filters can fold them into the query with `site_filter_query()`. Reddit discovery uses `include_domains=["reddit.com"]` and web search excludes nusmods.com and reddit.com, which is more reliable than `site:` in the query text.
- **One `SourceDoc` per Reddit thread**, not per comment: post + top 30 comments by score (replies flattened, per owner's choice), truncated to `max_chars_per_doc` from the lowest-scored comment up. Per-comment docs would swamp the budget with two-line docs.
- **Reddit stops calling `.json` after the first block** in a request. Every remaining thread uses its search snippet; the status note says how many.
- **Web pages from nus.edu.sg are kept** (owner's choice). Only nusmods.com and reddit.com are excluded, since other sources cover them.
- **`asyncpraw` is an optional extra** (`uv sync --extra reddit`), only imported when Reddit OAuth credentials are present. If the import fails the source logs it and uses the search path.
- **Disqus posts shorter than 30 chars, deleted or spam are dropped**; `raw_message` is preferred over stripping `message` HTML. Score = likes minus dislikes.
- **`gather_sources()` runs NUSMods first, then Disqus/Reddit/Web concurrently** with `return_exceptions=True`; a missing key becomes `SourceStatus.error`, never an exception. A progress callback fires once per source, which M5 wires to SSE.

M2 accepted 2026-09-17: `pytest` 57 passed, `ruff` clean, live `modreview fetch` for CS2040S/CS1101S/MA1521 returns NUSMods facts; "at least one other source" check is pending API keys (fixture tests cover them).

## M3: LLM layer

Checklist
- [x] `LLMProvider` protocol, `Usage`, `ModelInfo`, transient/permanent/output error classes
- [x] `AnthropicProvider`: Models API listing, structured output, one validation retry, error mapping
- [x] Registry (`llm/registry.py`), fallback chain, usage + cost estimate from `[pricing]`
- [x] Versioned prompt `prompts/summarise_v1.md`, `<doc>` wrapping with delimiter hardening
- [x] Fake provider tests (no network)
- [ ] One manual run against the real API returns a valid `ReviewReport` (pending `ANTHROPIC_API_KEY` in `.env`; run `uv run modreview fetch CS2040S --summarise`)

Decisions
- **Structured output via `output_config.format` (SDK `messages.parse(output_format=Schema)`), not forced tool use.** The plan's tool-use trick relies on `tool_choice: {type: "tool"}`, which the newest Claude models reject (400). JSON-schema structured outputs are supported by every current model including Haiku 4.5, validate to the pydantic model directly, and need no tool definition. If a future provider lacks structured outputs, its own `generate_structured` can use tool use behind the same protocol.
- **`temperature` is kept in the protocol and config but the Anthropic provider does not send it.** Current Claude models reject sampling parameters and the SDK's `parse()` has no such argument. The `openai_compat` provider (M6) will use it.
- **Fallback triggers (owner's choice):** `LLMTransientError` (429, 5xx/overloaded, timeout, network) and `LLMOutputError` (output failed validation twice, or was cut off at `max_tokens`). `LLMPermanentError` (401/403/404/400/402, refusal) is raised immediately; the fallback would fail identically.
- **Validation retry** happens inside the provider: the second attempt appends the pydantic error to the user message. Both attempts' tokens are counted in `Usage`.
- **Cost estimate** treats cache reads as 10% and cache writes as 125% of the input price. Unknown model -> `cost_usd = None`, the UI shows tokens only.
- **Model list** comes from `client.models.list()` sorted newest first; nothing is hard-coded. Pricing table updated to current list prices (Haiku 4.5 $1/$5, Sonnet 5 $2/$10, Opus 5 $5/$25).
- **Prompt hardening:** any `</doc` inside a document body is rewritten to `<\/doc` so a scraped page cannot close its own delimiter; attributes are HTML-escaped. The system prompt states that documents are untrusted data.
- **`--summarise` flag on `modreview fetch`** exists for the manual real-API check and prints the report JSON, usage and cost.
- **Thin evidence (owner's choice, implemented in M4):** when no opinion docs come back, the pipeline will skip the LLM and render a facts-only page.

M3 checks 2026-09-17: `pytest` 73 passed, `ruff` clean. Real-API run pending the key.
