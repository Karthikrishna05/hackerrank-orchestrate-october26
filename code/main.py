"""Ghost Data Still Exists: audit every deletion request and write output.csv.

Usage: python code/main.py [--requests deletion_requests.csv] [--output output.csv] [--intent auto|rules|cache]
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ghostaudit import engine, loaders, paths
from ghostaudit.output import (
    order_like_requests,
    validate_output,
    write_output,
)


def _requests(name: str):
    """A request CSV given as a file name in dataset/ or as a path."""
    path = Path(name)
    if path.is_file():
        return loaders.load_requests(path=path)
    return loaders.load_requests(name)


def _summary(results: list[engine.CaseResult]) -> list[str]:
    statuses = Counter(r.status for r in results)
    sources = Counter(r.intent.source if r.intent else "none" for r in results)
    notes = Counter(engine.note_kind(n) for r in results for n in r.notes)
    cases = Counter(k for r in results for k in {engine.note_kind(n) for n in r.notes})
    lines = ["statuses: " + ", ".join(f"{k}={statuses[k]}" for k in sorted(statuses)),
             "intent sources: " + ", ".join(f"{k}={sources[k]}" for k in sorted(sources)),
             "notes by kind (notes / cases):"]
    lines += [f"  {k}: {notes[k]} / {cases[k]}" for k in sorted(notes)] or ["  (none)"]
    return lines


def main(argv: list[str] | None = None) -> int:
    """Run the engine over a request file, write and validate the output CSV."""
    ap = argparse.ArgumentParser(prog="python code/main.py", description=__doc__.splitlines()[0])
    ap.add_argument("--requests", default="deletion_requests.csv",
                    help="request CSV: a file name in dataset/ or a path (default: deletion_requests.csv)")
    ap.add_argument("--output", default=str(paths.repo_root() / "output.csv"),
                    help="output CSV path (default: <repo>/output.csv)")
    ap.add_argument("--intent", choices=engine.INTENT_MODES, default="auto",
                    help="auto: LLM cache when present, else keyword rules; rules: keywords only; "
                         "cache: LLM cache only (a missing entry is an error)")
    args = ap.parse_args(argv)

    start = time.monotonic()
    requests = _requests(args.requests)
    try:
        ctx = engine.Context.build(args.intent)
        results = engine.run(requests, ctx)
    except (LookupError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    out = write_output(order_like_requests([r.row() for r in results], requests), args.output)
    errors = validate_output(out, requests)

    print(f"{out}: {len(results)} rows from {len(requests)} requests in {time.monotonic() - start:.1f}s")
    print("\n".join(_summary(results)))
    if errors:
        print(f"validate_output: {len(errors)} violation(s)", file=sys.stderr)
        for e in errors[:20]:
            print(f"  {e}", file=sys.stderr)
        return 1
    print("validate_output: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
