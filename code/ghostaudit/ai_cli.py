"""Fill or inspect the AI caches: `python -m ghostaudit.ai_cli {status,intent,vision}` (run from `code/`).

Exit codes: 0 done (or ready), 1 some items failed, 2 not ready: no API key or no network (nothing written),
or OpenRouter refused the key or found no credit (HTTP 401/402; the remaining items are left for the next run).
"""

from __future__ import annotations

import argparse
import sys

from . import intent_llm, llm, vision
from .loaders import load_requests
from .parsers import images

DEFAULT_REQUESTS = ("deletion_requests.csv", "sample_deletion_requests.csv")
NOT_READY = 2


def _texts(names) -> dict[str, list[str]]:
    return {n: [r.request_text for r in load_requests(n)] for n in names}


def _not_ready(reason: str) -> int:
    print(f"AI calls unavailable: {reason}. Nothing was written.\n"
          "To enable: set OPENROUTER_API_KEY (never commit it) and allow openrouter.ai in the "
          "environment's network settings.", file=sys.stderr)
    return NOT_READY


def _refused(reason: str) -> int:
    """Owner decision 27: a bad key or no credit (HTTP 401/402) is "not ready" too."""
    print(f"AI calls refused: {reason}. The API key is invalid or the account has no credit: check "
          "OPENROUTER_API_KEY (never commit it) and the OpenRouter account. Items not done are left for "
          "the next run.", file=sys.stderr)
    return NOT_READY


def cmd_status(args, client: llm.OpenRouterClient) -> int:
    print(f"API key (OPENROUTER_API_KEY): {'present' if client.has_key() else 'missing'}")
    ok, detail = client.reachable()
    print(f"Base URL: {client.base_url} -> {'reachable' if ok else 'unreachable'} ({detail})")
    print(f"Text model (OPENROUTER_TEXT_MODEL): {client.text_model}")
    print(f"Vision model (OPENROUTER_VISION_MODEL): {client.vision_model}")
    every = set()
    for name, texts in _texts(args.requests).items():
        distinct = {intent_llm.cache_key(t): t for t in texts}
        every |= set(distinct)
        hits = sum(intent_llm.cached(t) is not None for t in distinct.values())
        print(f"Intent cache ({intent_llm.PROMPT_VERSION}): {name}: {hits}/{len(distinct)} distinct texts "
              f"({len(texts)} requests)")
    shots = vision.screenshots()
    mapped = sum(images.cache_path(e.file_id).is_file() for e in shots)
    raw = len(shots) - len(vision.pending(shots))
    print(f"Vision cache ({vision.PROMPT_VERSION}): {mapped}/{len(shots)} screenshots mapped, {raw} raw extractions")
    return 0 if client.has_key() and ok else NOT_READY


def cmd_intent(args, client: llm.OpenRouterClient) -> int:
    by_file = _texts(args.requests)
    texts = [t for ts in by_file.values() for t in ts]
    keys = {intent_llm.cache_key(t) for t in texts}
    todo = keys if args.force else {intent_llm.cache_key(t) for t in texts if intent_llm.cached(t) is None}
    if todo:
        ready, reason = client.available()
        if not ready:
            return _not_ready(reason)
    report: dict = {}
    results = intent_llm.classify_all(texts, client=client, batch_size=args.batch_size, force=args.force,
                                      report=report)
    print(f"Intent: {report.get('texts', len(keys))} distinct texts, {report.get('cached', len(results))} from cache, "
          f"{report.get('classified', 0)} classified in {report.get('calls', 0)} model responses, "
          f"{len(report.get('failed', []))} failed")
    for err in report.get("errors", []):
        print(f"  error: {err}")
    deletion = sum(r.authorizes_deletion for r in results.values())
    print(f"  authorizes deletion: {deletion}; does not: {len(results) - deletion}")
    _compare_with_rules(texts, results)
    if report.get("stopped_status") in llm.AUTH_STATUS:
        return _refused(report["stopped"])
    return 1 if report.get("failed") else 0


def _compare_with_rules(texts: list[str], results: dict) -> None:
    """Report disagreements with the keyword baseline when it is available (never overrides)."""
    try:
        from . import intent_rules
    except ImportError:
        return
    diffs = sorted({intent_llm.cache_key(t) for t in texts if intent_llm.cache_key(t) in results
                    and intent_rules.classify(t).authorizes_deletion
                    != results[intent_llm.cache_key(t)].authorizes_deletion})
    print(f"  disagreements with the keyword baseline: {len(diffs)}")
    for key in diffs:
        print(f"    {key[:12]}: llm={results[key].authorizes_deletion} ({results[key].request_type}) "
              f"quote={results[key].quote!r}")


def cmd_vision(args, client: llm.OpenRouterClient) -> int:
    if vision.pending(force=args.force):
        ready, reason = client.available()
        if not ready:
            return _not_ready(reason)
    summary = vision.fill_cache(client=client, force=args.force)
    print(f"Vision: {summary['called']} screenshots read, {summary['remapped']} re-mapped from cache, "
          f"{len(summary['failed'])} failed")
    for fid, info in summary["files"].items():
        checks = ", ".join(f"{c.get('resource_id', '-')}: {c['status']}" for c in info["cross_check"])
        print(f"  {fid}: {info['records']} records, {len(info['dropped'])} dropped, "
              f"{len(info['aliases'])} aliases; link check: {checks}")
        for d in info["dropped"]:
            print(f"    dropped {d['resource_id']!r}: {d['reason']}")
        for c in info["cross_check"]:
            if c["status"] == "conflict":
                print(f"    CONFLICT {c['resource_id']}: screen shows {c['shown_identifiers']}, "
                      f"catalog link {c['activity_id']} says {c['link_identifiers']}")
    for fid, err in summary["failed"].items():
        print(f"  {fid}: FAILED {err}")
    if summary.get("stopped_status") in llm.AUTH_STATUS:
        return _refused(summary["stopped"])
    return 1 if summary["failed"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ghostaudit.ai_cli", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "intent", "vision"):
        p = sub.add_parser(name)
        p.add_argument("--requests", nargs="+", default=list(DEFAULT_REQUESTS), metavar="NAME",
                       help="request CSV names inside dataset/ (default: evaluation and sample files)")
        p.add_argument("--force", action="store_true", help="ignore the cache and call the model again")
        p.add_argument("--batch-size", type=int, default=10, help="texts per intent call (default 10)")
    args = parser.parse_args(argv)
    client = llm.OpenRouterClient()
    try:
        return {"status": cmd_status, "intent": cmd_intent, "vision": cmd_vision}[args.command](args, client)
    except llm.LLMError as exc:
        if exc.status in llm.AUTH_STATUS:
            return _refused(str(exc))
        return _not_ready(str(exc)) if exc.fatal else 1


if __name__ == "__main__":
    sys.exit(main())
