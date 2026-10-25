"""T11: write, read and validate output.csv."""

from __future__ import annotations

import argparse
import csv
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

from . import loaders, paths
from .models import Request, Step
from .steps import from_json, to_json, validate

HEADER = ["case_id", "deletion_status", "steps_json"]

Row = tuple[str, str, Sequence[Step]]


def write_output(rows: Iterable[Row], path: str | Path | None = None) -> Path:
    """Write (case_id, status, steps) rows in the given order; defaults to <repo>/output.csv."""
    out = Path(path) if path is not None else paths.repo_root() / "output.csv"
    with open(out, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(HEADER)
        for case_id, status, steps in rows:
            writer.writerow([case_id, status, to_json(steps)])
    return out


def order_like_requests(rows: Iterable[Row], requests: Sequence[Request]) -> list[Row]:
    """Rows reordered to the request file order; unknown or duplicate case_ids raise ValueError."""
    order = {r.case_id: i for i, r in enumerate(requests)}
    by_case: dict[str, Row] = {}
    for row in rows:
        case_id = row[0]
        if case_id not in order:
            raise ValueError(f"{case_id}: not in the request file")
        if case_id in by_case:
            raise ValueError(f"{case_id}: duplicate row")
        by_case[case_id] = row
    return sorted(by_case.values(), key=lambda row: order[row[0]])


def read_rows(path: str | Path) -> dict[str, tuple[str, list[Step]]]:
    """Strictly read an output-format CSV into {case_id: (status, steps)}."""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        if next(reader, None) != HEADER:
            raise ValueError(f"{path}: header must be {','.join(HEADER)}")
        rows: dict[str, tuple[str, list[Step]]] = {}
        for record in reader:
            if len(record) != 3:
                raise ValueError(f"{path} line {reader.line_num}: expected 3 fields")
            case_id, status, steps_json = record
            if case_id in rows:
                raise ValueError(f"{path} line {reader.line_num}: duplicate {case_id}")
            try:
                rows[case_id] = (status, from_json(steps_json))
            except ValueError as exc:
                raise ValueError(f"{path} line {reader.line_num} ({case_id}): {exc}") from exc
    return rows


def validate_output(path: str | Path, requests: Sequence[Request]) -> list[str]:
    """Every contract violation in an output file, checked against the request list."""
    errors: list[str] = []
    order = {r.case_id: i for i, r in enumerate(requests)}
    seen: list[str] = []
    seen_set: set[str] = set()
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header != HEADER:
            errors.append(f"header is {header!r}, expected {HEADER}")
        for record in reader:
            line = reader.line_num
            if len(record) != 3:
                errors.append(f"line {line}: expected 3 fields, got {len(record)}")
                continue
            case_id, status, steps_json = record
            tag = f"line {line} ({case_id})"
            if case_id not in order:
                errors.append(f"{tag}: case_id not in the request file")
            elif case_id in seen_set:
                errors.append(f"{tag}: duplicate case_id")
            else:
                seen.append(case_id)
                seen_set.add(case_id)
            try:
                steps = from_json(steps_json)
            except ValueError as exc:
                errors.append(f"{tag}: {exc}")
                continue
            errors.extend(f"{tag}: {e}" for e in validate(status, steps))
    missing = [c for c in order if c not in seen_set]
    if missing:
        errors.append(f"{len(missing)} request(s) have no row, e.g. {', '.join(missing[:5])}")
    if seen != sorted(seen, key=order.__getitem__):
        errors.append("rows are not in request-file (case_id) order")
    return errors


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: validate an output file and print its violations."""
    ap = argparse.ArgumentParser(prog="python -m ghostaudit.output", description=__doc__)
    ap.add_argument("--check", required=True, metavar="PATH", help="output CSV to validate")
    ap.add_argument("--requests", default="deletion_requests.csv",
                    help="request CSV: a file name in dataset/ or a path")
    args = ap.parse_args(argv)
    req = Path(args.requests)
    requests = loaders.load_requests(path=req) if req.is_file() else loaders.load_requests(args.requests)
    errors = validate_output(args.check, requests)
    for e in errors:
        print(e)
    print(f"{args.check}: {'OK' if not errors else f'{len(errors)} violation(s)'} "
          f"against {len(requests)} requests")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
