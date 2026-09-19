"""Debug CLIs.

* ``uv run modreview fetch CS2040S`` - run the sources only and print doc counts.
* ``uv run modreview review CS2040S`` - run the whole pipeline (uses the LLM) and print
  the report. Served from cache when fresh unless ``--force``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from modreview.cache import ReviewCache
from modreview.http import make_client
from modreview.models import InvalidModuleCode, ModuleNotFound, normalise_module_code
from modreview.pipeline import gather_sources, run_review
from modreview.review import ProgressEvent
from modreview.settings import ConfigStore, Secrets, config_path_from_env, data_dir_from_env


def _setup(debug: bool) -> None:
    # The Windows console defaults to cp1252; reviewer names contain emoji.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    for noisy in ("httpx", "httpcore", "trafilatura", "htmldate", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING if debug else logging.ERROR)


# --------------------------------------------------------------------------- fetch


async def _gather(code: str):
    cfg = ConfigStore(config_path_from_env()).config
    async with make_client(cfg.limits.http_timeout_s) as client:
        return await gather_sources(code, cfg, Secrets(), client)


def _fetch(args: argparse.Namespace) -> int:
    try:
        code = normalise_module_code(args.code)
        facts, results = asyncio.run(_gather(code))
    except (InvalidModuleCode, ModuleNotFound) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2 if isinstance(exc, InvalidModuleCode) else 1

    print(f"{facts.module_code} {facts.title} ({facts.module_credit} MCs, {facts.acad_year})")
    print(f"{'source':8} {'status':6} {'docs':>5}  note")
    for r in results:
        s = r.status
        state = "ok" if s.ok else "FAIL"
        print(f"{s.name:8} {state:6} {s.count:5d}  {s.note or s.error or ''}")
    total = sum(len(r.docs) for r in results)
    chars = sum(len(d.text) for r in results for d in r.docs)
    print(f"total {total} docs, {chars} chars (~{chars // 4} tokens)")
    if args.verbose:
        for r in results:
            for d in r.docs:
                when = d.published_at.date().isoformat() if d.published_at else "-"
                print(f"  [{d.source}] {d.id} {when} score={d.score} {d.title!r} {len(d.text)}ch")
    if args.json:
        payload = {
            "facts": facts.model_dump(mode="json"),
            "results": [r.model_dump(mode="json") for r in results],
        }
        with open(args.json, "w", encoding="utf-8") as fh:  # noqa: ASYNC230 (sync context)
            json.dump(payload, fh, indent=1, ensure_ascii=False)
        print(f"wrote {args.json}")
    return 0


def fetch_main() -> None:
    parser = argparse.ArgumentParser(prog="modreview fetch")
    parser.add_argument("code", help="module code, e.g. CS2040S")
    parser.add_argument("-v", "--verbose", action="store_true", help="list every doc")
    parser.add_argument("--json", metavar="FILE", help="dump facts + docs as JSON")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    _setup(args.debug)
    sys.exit(_fetch(args))


# --------------------------------------------------------------------------- review


def _review(args: argparse.Namespace) -> int:
    cfg = ConfigStore(config_path_from_env()).config
    cache = ReviewCache(data_dir_from_env() / "modreview.db")

    def show(ev: ProgressEvent) -> None:
        print(f"  .. {ev.message}", file=sys.stderr)

    try:
        result = asyncio.run(
            run_review(
                args.code,
                cfg,
                Secrets(),
                cache,
                model=args.model,
                provider=args.provider,
                force=args.force,
                progress=show,
            )
        )
    except (InvalidModuleCode, ModuleNotFound) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2 if isinstance(exc, InvalidModuleCode) else 1

    if result.report is not None:
        print(result.report.model_dump_json(indent=2))
    else:
        print(result.facts.to_text())
    u = result.usage
    cost = "n/a" if result.cost_usd is None else f"${result.cost_usd:.4f}"
    print(
        f"\n{result.provider}/{result.model} prompt={result.prompt_version} "
        f"docs={len(result.docs)}/{result.docs_collected} prompt_chars={result.prompt_chars} "
        f"in={u.input_tokens if u else 0} out={u.output_tokens if u else 0} cost={cost} "
        f"fallback={result.fallback_used} cached={result.from_cache} "
        f"dropped_citations={result.dropped_citations}"
    )
    if result.llm_error:
        print(f"LLM error: {result.llm_error}", file=sys.stderr)
        return 1
    if result.facts_only_reason:
        print(result.facts_only_reason, file=sys.stderr)
    return 0


def review_main() -> None:
    parser = argparse.ArgumentParser(prog="modreview review")
    parser.add_argument("code", help="module code, e.g. CS2040S")
    parser.add_argument("--model", help="override [llm].model")
    parser.add_argument("--provider", help="override [llm].provider")
    parser.add_argument("--force", action="store_true", help="ignore the cache")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    _setup(args.debug)
    sys.exit(_review(args))
