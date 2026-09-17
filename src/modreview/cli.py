"""``uv run modreview fetch CS2040S`` - debug CLI printing doc counts per source."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from modreview.http import make_client
from modreview.models import InvalidModuleCode, ModuleNotFound, normalise_module_code
from modreview.pipeline import gather_sources
from modreview.settings import ConfigStore, Secrets, config_path_from_env


async def _gather(code: str):
    cfg = ConfigStore(config_path_from_env()).config
    secrets = Secrets()
    async with make_client(cfg.limits.http_timeout_s) as client:
        facts, results = await gather_sources(code, cfg, secrets, client)
    return cfg, secrets, facts, results


async def _summarise(cfg, secrets, facts, results, model: str | None) -> int:
    """M3 manual check: run the real LLM on the gathered docs and print the report."""
    from modreview.llm.base import LLMError
    from modreview.llm.summarise import summarise
    from modreview.pipeline import all_docs

    try:
        res = await summarise(facts, all_docs(results), cfg, secrets, model_override=model)
    except LLMError as exc:
        print(f"LLM error: {exc}", file=sys.stderr)
        return 1
    print(res.report.model_dump_json(indent=2))
    cost = f"${res.cost_usd:.4f}" if res.cost_usd is not None else "n/a"
    print(
        f"\n{res.provider}/{res.model} prompt={res.prompt_version} "
        f"in={res.usage.input_tokens} out={res.usage.output_tokens} cost={cost} "
        f"fallback={res.fallback_used} prompt_chars={res.prompt_chars}"
    )
    return 0


def _main(args: argparse.Namespace) -> int:
    try:
        code = normalise_module_code(args.code)
    except InvalidModuleCode as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        cfg, secrets, facts, results = asyncio.run(_gather(code))
    except ModuleNotFound as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

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
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, ensure_ascii=False)
        print(f"wrote {args.json}")
    if args.summarise:
        return asyncio.run(_summarise(cfg, secrets, facts, results, args.model))
    return 0


def fetch_main() -> None:
    parser = argparse.ArgumentParser(prog="modreview fetch")
    parser.add_argument("code", help="module code, e.g. CS2040S")
    parser.add_argument("-v", "--verbose", action="store_true", help="list every doc")
    parser.add_argument("--json", metavar="FILE", help="dump facts + docs as JSON")
    parser.add_argument("--summarise", action="store_true", help="also run the LLM (costs money)")
    parser.add_argument("--model", help="model override for --summarise")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    sys.exit(_main(args))
