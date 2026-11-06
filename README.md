# ModReview

Type an NUS module code, get a structured, sourced review built from NUSMods, NUSMods student reviews (Disqus), r/nus and the wider web, summarised by an LLM. Runs only on your laptop at `http://127.0.0.1:8765`. Every API key it needs has a free tier.

![Report page for CS2040S: verdict, sentiment and difficulty, sections with numbered citations, and the official NUSMods facts panel](docs/screenshots/report.png)

| Search | Compare two modules |
|---|---|
| ![Home page with module search and recent reports](docs/screenshots/home.png) | ![CS2040S and CS1101S side by side, rows aligned](docs/screenshots/compare.png) |

| Settings | Dark mode, narrow window |
|---|---|
| ![Settings: model, fallback, search provider, sources, key status](docs/screenshots/settings.png) | ![MA1521 report in dark mode at a narrow width](docs/screenshots/report-dark-narrow.png) |

## Setup

About 10 minutes the first time, most of it signing up for the three free keys. Everything runs on your own computer, and each person uses their own keys and free quotas.

**1. Install uv** (it fetches Python 3.12 for you). On Windows, in PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

On macOS or Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

If you already have Python, `pip install uv` works too.

**2. Get the code.** On GitHub, click **Code → Download ZIP** and unzip it anywhere, or `git clone` the repository.

**3. Add your free keys.** In the ModReview folder, copy `.env.example` to a new file named `.env`, then paste each key after its `=`:

| `.env` line | Service | Where to get it |
|---|---|---|
| `OPENAI_COMPAT_API_KEY` | Google AI Studio (Gemini, writes the summary) | [aistudio.google.com/apikey](https://aistudio.google.com/apikey). Do not set up billing: a project with billing but no credit stops working instead of using the free tier. |
| `TAVILY_API_KEY` | Tavily web search | [app.tavily.com](https://app.tavily.com), 1,000 free credits a month (about 5 per report) |
| `DISQUS_API_KEY` | Disqus, for NUSMods student reviews | [disqus.com/api/applications](https://disqus.com/api/applications/): register an app, copy its public "API Key" |

`.env` stays on your computer: it is git-ignored and never uploaded. You can skip this step to try the app first; without keys you get the official NUSMods facts and Reddit threads but no summary, and the home page lists which keys are missing.

**4. Start it.** On Windows, double-click **`Start ModReview.bat`**. Anywhere else, run this in the ModReview folder:

```bash
uv run modreview
```

Your browser opens at `http://127.0.0.1:8765`. The first start installs the dependencies (about a minute); later starts take a few seconds. Close the window, or press Ctrl+C, to stop it.

Flags: `--debug` (source counts and prompt size), `--port N`, `--no-browser`.

## Using it

- **Search**: type a code or part of a title and press Enter. Press `/` anywhere to jump to the search box. A report takes 20 to 40 seconds the first time and is reused for 14 days.
- **Report**: every point has small numbers; hover one to see the source, click it to jump to the source list. The grey tag after each point says which academic years its information is from (a "~" means the date was estimated from the text; "older" means the newest source is over two years old). The chart at the top shows how many sources came from each academic year. The right-hand panel is the official NUSMods data. Regenerate rebuilds the report; Download saves it as Markdown, dates included.
- **Settings**: change the model, fallback model, search provider, sources (including the Reddit archive, official course pages and seniors' blogs), how old a source may be, and the cache lifetime. Changes apply to the next report without a restart. Keys are only shown as set or missing.
- **Compare**: pick two modules to see verdict, difficulty, official workload and the main points side by side. A module without a saved report is built on the spot; both can build at once. The Compare button on a report pre-fills it.
- **History**: every saved report, with download and delete.

## Terminal tools

```bash
uv run modreview fetch CS2040S -v
```

Runs the sources only and lists every document found. Uses no LLM.

```bash
uv run modreview review CS2040S
```

Runs the whole pipeline and prints the report JSON. A second run within 14 days comes from the cache; add `--force` to rebuild, `--model NAME` to try another model.

## Models

`config.toml` sets the model. The default is `gemini-flash-latest` with `gemini-flash-lite-latest` as the fallback, both through Gemini's OpenAI-compatible endpoint (built in, so only the key is needed). The same provider works with any OpenAI-compatible server (Groq, OpenRouter, Ollama, LM Studio): set `OPENAI_COMPAT_BASE_URL` in `.env` and change the model name. Local servers need no key. Claude works too: set `[llm] provider = "anthropic"` and add `ANTHROPIC_API_KEY` (paid).

## Troubleshooting

- **A report takes about a minute.** r/nus threads come from the Arctic Shift archive, a free third-party Reddit archive that can be slow. If it is down, the app falls back to search snippets.
- **"Summary could not be written" with a 429 or "credits depleted" error.** The Gemini free tier is rate limited; wait a minute, or pick the lighter fallback model in Settings. A Google project with billing switched on but no credit fails the same way: use a project without billing.
- **SSL or certificate errors** on a network with antivirus or a company proxy that re-signs HTTPS. uv and the app both use the operating system's certificate store, so the fix is usually to make sure that software's certificate is installed in the system store.
- **Port 8765 already in use.** Another copy is running; close it, or start with `uv run modreview --port 8766`.

## Development

```bash
uv run pytest
```

```bash
uv run ruff check . && uv run ruff format --check .
```

See `PLAN.md` for the spec and `DECISIONS.md` for deviations.

Screenshots are taken with headless Chrome against the running app, for example:

```bash
chrome --headless=new --window-size=1280,2000 --blink-settings=preferredColorScheme=1 --screenshot=docs/screenshots/report.png http://127.0.0.1:8765/review/CS2040S
```
