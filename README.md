# ModReview

Type an NUS module code, get a structured, sourced review built from NUSMods, NUSMods student reviews (Disqus), r/nus and the wider web, summarised by an LLM. Runs only on your laptop at `http://127.0.0.1:8765`.

## Setup

1. Install [`uv`](https://docs.astral.sh/uv/) (`pip install uv` works), then run `uv sync`.
2. Get an **Anthropic API key** from the Claude Console (platform.claude.com) and add credits. API billing is separate from a Claude.ai Pro/Max subscription.
3. Get a **Tavily** (or Brave) search API key. Both have free tiers.
4. Register a **Disqus API application** and copy its public key.
5. *(Optional)* Apply for Reddit Data API access. The app works without it.
6. Copy `.env.example` to `.env`, fill in the keys, then run:

```bash
uv run modreview
```

Flags: `--debug` (source counts and prompt size), `--port N`, `--no-browser`.

Debug the sources without the LLM:

```bash
uv run modreview fetch CS2040S -v
```

Notes for this laptop: TLS is re-signed by local software, so uv uses the Windows certificate store (`[tool.uv] system-certs`) and the app uses `truststore`. Reddit's public `.json` endpoint answers 403 here, so the Reddit source falls back to search snippets.

Non-secret settings live in `config.toml` and can be changed from the Settings page. Suggested models: `claude-haiku-4-5-20251001` (cheapest, fine for summarising) or `claude-sonnet-5` (more nuance).

## Development

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

See `PLAN.md` for the spec and `DECISIONS.md` for deviations.
