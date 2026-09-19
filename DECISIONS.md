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
- [x] One manual run against the real API returns a valid `ReviewReport` (done 2026-09-19 with Gemini instead of Anthropic, see the free-keys revision and M4)

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

M3 checks 2026-09-17: `pytest` 73 passed, `ruff` clean. Real-API run completed 2026-09-19 (Gemini, see M4).

## Free-keys revision (2026-09-19, owner's request)

The owner wants free API keys only. Verified free tiers on 2026-09-19:
- **Default LLM is now Gemini 2.5 Flash** through Google's OpenAI-compatible endpoint (`OPENAI_COMPAT_*` in `.env`). Free tier, no card. Caveat accepted: Google may use free-tier prompts for training; the inputs are public reviews.
- **Tavily stays** as the search provider (1,000 free credits a month, about 5 per report).
- **Disqus stays** (free public key, 1,000 calls an hour).
- **Brave dropped as a recommendation**: its free tier ended February 2026. The provider can still be built later but is not a default.
- **Anthropic becomes optional**: still selectable, nothing depends on it.
- **Groq not the default**: free, but its per-minute token cap is too small for a ~40K-token prompt.
- Consequence: `openai_compat.py` moves from M6 into M4, and `config.toml` switches `[llm]` to provider `openai_compat`, model `gemini-2.5-flash`, with fallback `gemini-2.5-flash-lite` on the same key.
- Other owner choices for M4: rank ties Disqus > Reddit > Web; regenerate replaces the history row (one row per module + model + prompt version).
- Building is paused until the owner has put the three keys in `.env`.

## M2 live re-check (2026-09-19, with keys)

| Module | NUSMods | Disqus reviews | Reddit threads | Web pages |
|---|---|---|---|---|
| CS2040S | 1 | 59 | 2 (snippets) | 9 |
| CS1101S | 1 | 82 | 3 (snippets) | 10 |
| MA1521 | 1 | 97 | 3 (snippets) | 8 |

- **Disqus**: `thread:ident={CODE}` works on the first try. `thread:link=.../courses/{CODE}/reviews` returns 400 "Unable to find thread"; `thread:link=.../modules/{CODE}/reviews` returns the same thread (the thread's stored link still uses the old `/modules/` path). Selector order kept as is.
- **Tavily**: works; raw results include irrelevant company pages, which the module-code filter drops (4-6 per module).
- **Web**: facebook.com NUSWhispers posts appear in results but trafilatura extracts nothing (login wall), so they are skipped.

## M4: Pipeline and cache

Checklist
- [x] `openai_compat` provider (moved here from M6: it is now the default via Gemini)
- [x] Cleaning: whitespace normalisation, near-duplicate removal (normalised-text hash), truncation to `max_chars_per_doc` at a paragraph/sentence boundary
- [x] Rank and budget: recency bucket, then score, then source priority; NUSMods facts always kept
- [x] Cost guard: chars / 4 estimate against `max_input_tokens_per_report`, shrinking the budget until it fits
- [x] Citation post-check drops unknown ids and claims left without citations
- [x] SQLite cache with TTL; key = code + provider + model + prompt version
- [x] Second request served from cache; `--force` bypasses it (verified live)

Decisions
- **Gemini model ids.** `gemini-2.5-flash` answers 404 "no longer available to new users". Defaults are the aliases `gemini-flash-latest` (primary) and `gemini-flash-lite-latest` (fallback), which Google keeps pointed at current free models. All of `gemini-flash-latest`, `gemini-3.5-flash`, `gemini-flash-lite-latest` and `gemini-3.1-flash-lite` returned valid structured output on 2026-09-19. The settings page (M5) lists every model from `/models`.
- **Gemini "prepayment credits are depleted" 429.** Every call failed with this until the owner fixed billing on the AI Studio project. It means the project is on prepay billing with no credit, not the free tier. README tells future setups not to enable billing.
- **Structured output on openai_compat** uses `response_format: json_schema` with every `$ref` inlined (`inline_refs`), because OpenAI-compatible servers vary in `$ref` support. Output is re-validated with pydantic and fenced JSON is tolerated; one retry carries the validation error back, as in the Anthropic provider.
- **`max_output_tokens` raised to 16000** because Gemini counts thinking tokens against it. A real CS2040S report used about 3,200 output tokens. `[llm].reasoning_effort` exists (sent as `reasoning_effort`) but is unset by default; a Google forum thread reports that disabling thinking on 3.6-flash triggers a bogus 429.
- **Free-tier models are priced at 0** in `[pricing]`, so the footer shows $0.00 rather than "tokens only".
- **Dropped `truststore`** for the app's SSL. It crashed once with a native access violation (`CertFreeCertificateChain`) under Norton's TLS interception. `ssl.create_default_context()` loads the Windows ROOT/CA stores itself and verified all five hosts. One shared context per process.
- **Budget floor per source.** Pass 1 keeps the top 3 docs from each of Disqus, Reddit and Web before filling by global rank, so 97 Disqus reviews cannot crowd out Reddit and the web. Undated docs rank as if three years old. Rank ties go Disqus > Reddit > Web (owner's choice).
- **Near-duplicates**: first occurrence wins, and sources arrive in priority order, so a blog repost of a Disqus review keeps the Disqus copy.
- **Cache semantics.** One row per (module, provider, requested model, prompt version); regenerate overwrites it (owner's choice), so the table doubles as History. A fallback answer is stored under the requested model so the same request hits the cache next time; `fallback_used` records it. Facts-only pages and LLM failures are not cached. `cache_ttl_days = 0` disables the cache.
- **Failures don't raise.** `run_review` raises only for an invalid code or an unknown module. A missing LLM key, rate limit or bad output returns the facts, the sources and `llm_error`, so the page can still render.
- **Thin evidence (owner's choice).** No opinion docs means no LLM call, and the result carries `facts_only_reason`.
- **`modreview review CODE`** replaces `modreview fetch --summarise`. It runs the full pipeline with progress lines. The CLI forces UTF-8 output because reviewer names contain emoji and the Windows console defaults to cp1252.
- **Retry helper** now looks up `asyncio.sleep` at call time, so tests can patch it (a test was sleeping 7 s for real).

Live results 2026-09-19

| Run | Docs sent / collected | Input tokens | Output tokens | Time | Notes |
|---|---|---|---|---|---|
| CS2040S | 74 / 74 | 35,057 | 3,237 | 23 s | 0 citations dropped |
| CS2040S again | cached | - | - | 3 s | no model call |
| CS2040S `--force` | 74 / 74 | 35,057 | 2,649 | 26 s | rebuilt |
| CS1101S | 70 / 101 | 36,120 | 3,160 | ~25 s | budget trimmed 31 docs |

M4 accepted 2026-09-19: `pytest` 102 passed in about 6 s, `ruff` clean.
