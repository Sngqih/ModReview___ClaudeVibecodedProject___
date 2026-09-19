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

## M5: UI

Checklist
- [x] Home: search with live module autocomplete, model dropdown (default = configured), ignore-cache checkbox, recent reports
- [x] Report page with SSE progress, official facts panel with workload bars, sections in schema order, numbered citations with hover tooltips, sources grouped by type, footer with model/prompt/tokens/cost/time, Regenerate
- [x] Settings: provider + live model list, fallback model, search provider, source toggles, cache lifetime, key presence (booleans only), Test connection per provider
- [x] History with view, Markdown download and delete
- [x] Light and dark mode; readable at phone width
- [x] Markdown export
- [x] Full flow works in the browser; a model change in Settings shows in the next report's footer without a restart (test `test_settings_change_model_applies_to_next_report`, plus manual check)

Owner's design choices: editorial minimal (minimalist-ui skill), two-column report with a sticky facts panel, citation click jumps to the Sources list, cited sources first with the rest collapsed, terracotta accent.

Decisions
- **Type and colour.** Newsreader (serif headings, verdict), Geist (body), Geist Mono (metadata), from Google Fonts with system fallbacks. Warm off-white paper, `#111` ink, terracotta `#B4532A` only for citations, links, the difficulty meter and the verdict rule. Sentiment uses pale pastel pills. Dark mode swaps to warm charcoal with a peach accent. No emoji; status icons are an inline SVG sprite.
- **Citation numbers follow reading order.** Cited docs are numbered by first appearance down the page (1, 2, 3...), then uncited docs continue the sequence, so every source has one stable number in the page and in the Markdown export.
- **SSE is hand-rolled** (`StreamingResponse`, ~40 lines incl. client JS). The stream sends each progress line as rendered HTML, then the finished report as one `done` event that replaces the progress card. The client closes the EventSource on error, because its automatic reconnect would restart the whole pipeline. A browser that disconnects cancels the pipeline task.
- **Pipeline injection.** `create_app(..., cache=, module_loader=, runner=)` lets tests swap the pipeline, cache and module list, so the web tests never touch the network or the real database.
- **Search box.** Autocomplete is a native `<datalist>` filled by htmx from `/api/modules` (prefix match on code, then title substring). Enter always submits (Chrome's suggestion popup can swallow it), and a typed title resolves to the top suggestion.
- **Settings writes `config.toml` with `tomli-w`, which drops the comments** in that file. Accepted: the Settings page explains each option, and PLAN.md documents the file.
- **Keys are read at startup.** The Settings page says so; editing `.env` needs a restart. Values never reach templates, only `Secrets.present()` booleans.
- **Model dropdown filter.** Non-text models (TTS, image, embedding, robotics, live, Google's deep-research and antigravity agents) are hidden. The list is cached for 10 minutes per provider.
- **Only http(s) URLs reach an `href`** (`safe_url` filter), and every scraped string is autoescaped. Outbound links use `rel="noopener noreferrer"`.
- **Facts-only and failed reports render** with a notice, the official facts and the collected sources; they have no export link because they are not cached.

Bugs found during the browser check and fixed
- Disqus `raw_message` still carries user-typed HTML (`<b>`, `&amp;`), which showed literally in excerpts and went to the LLM. Both Disqus and search-provider page text now go through the shared `clean.strip_html`.
- After a fallback, the live page showed "gemini-flash-lite-latest (fallback for gemini-flash-lite-latest)" and its Markdown link 404'd, because the returned result carried the answering model instead of the requested one. `model` is now always the requested model (the cache key) and `answered_by` the model that wrote it. A regression test covers it.
- The Settings text-input style leaked into the search box (double border), and Chrome drew a datalist arrow; both fixed.
- Google returned a real 503 for `gemini-flash-latest` during the MA1521 run; the fallback to `gemini-flash-lite-latest` handled it as designed.

M5 accepted 2026-09-19: `pytest` 118 passed, `ruff` clean; home, live progress, report, citations, settings, test-connection, history, export, dark mode and phone width checked in the browser.

## M6: Polish (stretch)

Owner's scope (2026-09-19): compare view and README screenshots. Brave and Claude web search were skipped because neither is free any more; the `openai_compat` provider was already built in M4.

Checklist
- [x] Compare view `/compare?a=&b=`: cached columns render at once, uncached ones build live over the same SSE stream (`?view=compare`), both concurrently
- [x] README screenshots (home, report, compare, settings, dark narrow)
- [x] Tests for the compare page and column stream

Decisions
- **Rows line up with CSS subgrid.** Each column is exactly 8 cells (header, verdict, workload, assessment, complaints, tips, who should take it, link) spanning the parent's 8 rows, so row N has the same height in both columns whatever the text length. Below 960px the columns stack.
- **Compare shows claims without citation chips**, capped at 3 per section with a "N more in the full report" note; each column links to the full report, where every claim is cited.
- **The stream endpoint gained `view=compare`**, which renders the compact column instead of the full report. The client script now handles any number of streams on a page, and each compare column replaces only itself.
- **Autocomplete accepts `a`/`b`** (the compare inputs' names) as well as `code`/`q`; with two boxes sharing one suggestion list, a typed title is resolved only for the box the suggestions belong to.
- **Screenshots via headless Chrome** with a throwaway profile under the scratchpad and `--blink-settings=preferredColorScheme=1` to force light mode (the laptop's Windows theme is dark). Chrome will not size a window below about 500px, so the narrow shot is 520px wide.

Bugs found while taking screenshots, fixed
- **Content could stay invisible.** The fade-in relied on IntersectionObserver alone; headless Chrome (and the hidden preview pane) skipped the callback, leaving the page blank below the header. Content already on screen at load is now revealed by a timer, the observer handles only content further down, and a 2.5 s safety net reveals anything still hidden in view.
- **Long course-code lists overflowed the facts panel** (and the whole page on a phone). Facts text, claims and status lines now wrap anywhere; below 560px the header splits into two rows so the four nav links fit.
- **NUSMods glues its logic words** ("DegreeTHEN(", "DANDmust"). `tidy_requirement` spaces them at parse time, and the same filter runs at render time so reports cached before the fix display cleanly too.
- **Stale scripts after an update.** Static URLs were versioned by the app version, which does not change between edits. They now carry the newest static file's modification time.

M6 accepted 2026-09-19: `pytest` 124 passed, `ruff` clean, compare checked live (rows aligned to the pixel), screenshots reviewed.

## More sources, recency first, design audit (2026-09-19/20)

Owner's request: find more places seniors write about modules, prioritise recent information and show on the page when each piece of information is from, then check the design with the downloaded design skill.

### Source ideas tested live (2026-09-19)

| Idea | Result | Decision |
|---|---|---|
| Reddit via Arctic Shift archive | Full dated r/nus threads, comment trees and scores; comments 4 days old | **Built** (owner's choice), with a Settings toggle |
| Official course pages (nus.edu.sg) | Lecturers' pages with current assessment weights, e.g. Prof Halim's CS2040S page (35K chars, dated Jul 2026) | **Built** |
| Seniors' blogs (github.io, Medium, Substack, ...) | TA and student write-ups found by targeted search | **Built** |
| NUSWhispers public API | Dated confessions with likes, but mostly questions; newest CS2040S match from 2024 | Not built (weak on recency) |
| Tavily `include_raw_content` / extract for Reddit | No text for reddit.com; extract fails | Not usable |
| Tavily `start_date` | Works as a recency filter, but general results carry no publish date | **Used** for general and blog queries |
| HardwareZone, Telegram (t.me) | Nothing relevant | Dropped |
| GitHub repos | Notes and code, not opinions | Dropped |

### Decisions

- **Arctic Shift is a third-party archive of Reddit data**, not Reddit's API. The owner accepted the grey-zone trade-off; it is used politely (few calls, 1 s gaps, at most 2 in flight, stops on the first 429), can be switched off in Settings, and falls back to search snippets.
- **Archive behaviour learned live**: its full-text search times out ("Timeout. Maybe slow down a bit", HTTP 422) on wide date ranges and its title search failed repeatedly. So comments are searched **one academic year at a time** (newest first, two at once, 25 s budget); threads come from those comments' ids plus r/nus links from web search, fetched in one `posts/ids` call; threads whose title names the module are read in full (`comments/tree`); other mentions become per-thread "comments on CODE" documents dated by their newest comment. A new report now takes about a minute (was about 25 s); the progress text says so.
- **Web search classifies by host**: nus.edu.sg -> `course_page` (always fetched in full, no date filter since such pages are rarely dated), blog hosts -> `blog`, the rest `web`. Two extra queries per report (about 2 more Tavily credits).
- **Recency**: ranking uses half-year (semester) buckets instead of years. Opinion sources older than `max_source_age_years` (4) are dropped unless fewer than `min_recent_docs` (10) newer ones remain. Undated pages get a date estimated from the newest academic year they mention ("AY24/25"), marked `date_estimated`. Search and archive queries use the same window.
- **Prompt v2** (`summarise_v2`): each doc carries `period="AY25/26 S2"` (and `date_estimated`), the message states today's academic year, and the rules tell the model to build from the latest two years and name the semester when relying on older material. The version bump retires v1 reports from the cache; they stay in History.
- **Dates on the page**: every claim gets a tag computed from the docs it cites (not from the model): `AY24/25-25/26`, `~AY24/25` when estimated, `official` for the NUSMods listing or course pages, and a visible "older" marker when the newest source is over 2 years old. The report header has a "Sources by academic year" column chart (last 5 AYs + older), recent 2 AYs in the accent. Built per the dataviz skill: one series (no legend, a colour key instead), columns <= 24 px with 4 px rounded tops, value on each cap, whole column as hover target, and a visually hidden table. Colours validated: contrast >= 3:1 and CVD separation pass in light and dark (the grey is a deliberate de-emphasis hue, so the categorical lightness/chroma checks do not apply).
- **Dates normalised to UTC in `SourceDoc`**: live data mixed naive (trafilatura) and aware (Reddit, Disqus) datetimes, which crashed the timeline (regression test added). Normalising in the model also repairs cached reports on load.
- **Tests never reach the archive**: an autouse fixture blocks it unless a test is marked `archive`, and those use respx. (An early run hit the real service from tests; caught by the suite's runtime jumping to 77 s.)

### Design audit (redesign-existing-projects skill)

Fixed: orphaned heading words (`text-wrap: balance/pretty`); cool text grey on warm paper (text `#2F3437` -> `#34312C`, hairlines warmed); plain black shadows (warm tokens); flat surfaces (fixed grain layer at 3.5% opacity); `100vh` (-> `100dvh`); unaligned tile footers (pinned to the bottom); nine all-caps mono labels (sentence case, caps kept for page eyebrows and sentiment pills); outlined "confidence"/"cached" pills (plain text); two outline buttons in the report footer (Compare is now a text link); spinner-only loading (report-shaped skeleton, still for reduced motion); raw JSON 404 (branded page); instant anchor jumps (smooth scroll, off for reduced motion); ad hoc z-index values (token scale); missing description/theme-color meta.

Kept deliberately: bordered white cards (the minimalist-ui spec the owner chose), system light/dark (no toggle). Not applicable: legal links and cookie consent (local single-user app).

Checks 2026-09-20: `pytest` 155 passed, `ruff` clean; live CS2040S report: 82 docs collected, 63 used after cleaning and the 4-year cutoff (19 older left out), 0 citations dropped, 52 s end to end.
