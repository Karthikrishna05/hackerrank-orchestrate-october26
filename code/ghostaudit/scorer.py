"""T12: local evaluator comparing predicted rows with gold rows."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .models import Step
from .output import read_rows
from .steps import STATUSES, step_key

FIELDS = ("phase", "system_id", "resource_id", "operation", "state", "evidence_id")
MISSING = "(missing)"

Cases = Mapping[str, tuple[str, Sequence[Step]]]
StepTuple = tuple[str, ...]


def _ratio(num: float, den: float) -> float:
    return num / den if den else 1.0


def _case_key(case_id: str) -> tuple[str, int]:
    """Numeric case order: case_99 before case_100."""
    head, _, tail = case_id.rpartition("_")
    return (head, int(tail)) if tail.isdigit() else (case_id, -1)


def _pct(x: float) -> str:
    return f"{100 * x:.2f}%"


def _fmt_step(t: StepTuple) -> str:
    return " ".join(t[:-1]) + " " + (t[-1] or '""')


@dataclass
class PRF:
    """Micro precision/recall/F1 counts over step multisets."""

    tp: int = 0
    pred: int = 0
    gold: int = 0

    @property
    def precision(self) -> float:
        return self.tp / self.pred if self.pred else float(self.gold == 0)

    @property
    def recall(self) -> float:
        return self.tp / self.gold if self.gold else float(self.pred == 0)

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0


@dataclass
class CaseDiff:
    """How one predicted case differs from gold."""

    case_id: str
    gold_status: str
    pred_status: str  # MISSING when the case has no prediction
    missing: list[StepTuple] = field(default_factory=list)
    extra: list[StepTuple] = field(default_factory=list)
    out_of_order: list[StepTuple] = field(default_factory=list)


@dataclass
class Report:
    """Every metric of one scoring run; see to_markdown()."""

    n_cases: int
    missing_predictions: list[str]
    extra_predictions: list[str]
    status_correct: int
    status_confusion: dict[tuple[str, str], int]  # (gold, predicted) -> count
    steps: PRF
    steps_no_evidence: PRF
    field_matches: dict[str, int]
    field_positions: int
    sequence_exact: int
    levenshtein_mean: float
    diffs: list[CaseDiff]

    @property
    def status_accuracy(self) -> float:
        return _ratio(self.status_correct, self.n_cases)

    @property
    def field_accuracy(self) -> dict[str, float]:
        return {f: _ratio(self.field_matches[f], self.field_positions) for f in FIELDS}

    @property
    def field_overall(self) -> float:
        return _ratio(sum(self.field_matches.values()), self.field_positions * len(FIELDS))

    @property
    def sequence_accuracy(self) -> float:
        return _ratio(self.sequence_exact, self.n_cases)

    def to_markdown(self, max_diffs: int | None = None) -> str:
        """Summary tables plus the per-case diff list."""
        def prf(m: PRF) -> str:
            return f"{_pct(m.precision)} / {_pct(m.recall)} / {_pct(m.f1)} (TP {m.tp}, pred {m.pred}, gold {m.gold})"

        out = [
            "# Scoring report", "",
            (f"Cases scored: {self.n_cases} (missing predictions: {len(self.missing_predictions)}, "
             f"extra predicted cases ignored: {len(self.extra_predictions)})"), "",
            "| Metric | Value |", "|---|---|",
            f"| Case status accuracy | {_pct(self.status_accuracy)} ({self.status_correct}/{self.n_cases}) |",
            f"| Step precision / recall / F1 | {prf(self.steps)} |",
            f"| Step P / R / F1 without evidence_id | {prf(self.steps_no_evidence)} |",
            f"| Field-level accuracy (all fields) | {_pct(self.field_overall)} |",
            f"| Exact sequence accuracy | {_pct(self.sequence_accuracy)} ({self.sequence_exact}/{self.n_cases}) |",
            f"| Mean normalised Levenshtein similarity | {_pct(self.levenshtein_mean)} |",
            "", "## Field-level accuracy (position-aligned)", "",
            f"Aligned positions: {self.field_positions}", "",
            "| Field | Accuracy |", "|---|---|",
            *(f"| {f} | {_pct(a)} |" for f, a in self.field_accuracy.items()),
            "", "## Status confusion (rows: gold, columns: predicted)", "",
        ]
        cols = [*STATUSES, MISSING]
        out += ["| gold \\ pred | " + " | ".join(cols) + " |", "|---" * (len(cols) + 1) + "|"]
        for g in sorted({g for g, _ in self.status_confusion}):
            out.append(f"| {g} | " + " | ".join(str(self.status_confusion.get((g, p), 0)) for p in cols) + " |")
        out += ["", f"## Per-case differences ({len(self.diffs)} case(s))", ""]
        for d in self.diffs[:max_diffs]:
            status = (f"status {d.pred_status} (gold {d.gold_status})"
                      if d.pred_status != d.gold_status else f"status {d.gold_status} matches")
            out.append(f"- **{d.case_id}**: {status}")
            for label, items in (("missing", d.missing), ("extra", d.extra),
                                 ("out of order", d.out_of_order)):
                out += [f"  - {label}: `{_fmt_step(t)}`" for t in items]
        if max_diffs is not None and len(self.diffs) > max_diffs:
            out.append(f"- ... {len(self.diffs) - max_diffs} more")
        return "\n".join(out) + "\n"


def _minus(a: Sequence[StepTuple], b: Sequence[StepTuple]) -> list[StepTuple]:
    """Items of `a` not matched one-to-one in `b`, in `a` order."""
    left = Counter(b)
    out = []
    for x in a:
        if left[x]:
            left[x] -= 1
        else:
            out.append(x)
    return out


def _lcs(a: Sequence[StepTuple], b: Sequence[StepTuple]) -> list[StepTuple]:
    """A longest common subsequence of a and b."""
    dp = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) - 1, -1, -1):
        for j in range(len(b) - 1, -1, -1):
            dp[i][j] = dp[i + 1][j + 1] + 1 if a[i] == b[j] else max(dp[i + 1][j], dp[i][j + 1])
    i = j = 0
    out = []
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            out.append(a[i])
            i, j = i + 1, j + 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    return out


def _levenshtein(a: Sequence[StepTuple], b: Sequence[StepTuple]) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def score(pred_rows: Cases | str | Path, gold_rows: Cases | str | Path) -> Report:
    """Score predictions over the gold case set; a missing prediction is wrong everywhere.

    Each argument is a parsed `{case_id: (status, steps)}` mapping or a path to an output CSV.
    """
    if isinstance(pred_rows, (str, Path)):
        pred_rows = read_rows(pred_rows)
    if isinstance(gold_rows, (str, Path)):
        gold_rows = read_rows(gold_rows)
    steps, steps_ne = PRF(), PRF()
    field_matches = dict.fromkeys(FIELDS, 0)
    positions = status_ok = seq_ok = 0
    lev_total = 0.0
    confusion: Counter[tuple[str, str]] = Counter()
    diffs: list[CaseDiff] = []
    missing_cases = []
    for case_id in sorted(gold_rows, key=_case_key):
        gold_status, gold_steps = gold_rows[case_id]
        gold = [step_key(s) for s in gold_steps]
        if case_id in pred_rows:
            pred_status, pred_steps = pred_rows[case_id]
            pred = [step_key(s) for s in pred_steps]
        else:
            missing_cases.append(case_id)
            pred_status, pred = MISSING, []
        present = pred_status != MISSING
        confusion[(gold_status, pred_status)] += 1
        status_ok += pred_status == gold_status
        for m, strip in ((steps, False), (steps_ne, True)):
            g = Counter(t[:-1] if strip else t for t in gold)
            p = Counter(t[:-1] if strip else t for t in pred)
            m.tp += sum((g & p).values())
            m.gold += len(gold)
            m.pred += len(pred)
        n = max(len(pred), len(gold))
        positions += n
        for k, f in enumerate(FIELDS):
            field_matches[f] += sum(pt[k] == gt[k] for pt, gt in zip(pred, gold))
        exact = present and pred == gold
        seq_ok += exact
        lev_total += (1 - _levenshtein(pred, gold) / n if n else 1.0) if present else 0.0
        if not exact or pred_status != gold_status:
            common_pred = _minus(pred, _minus(pred, gold))
            common_gold = _minus(gold, _minus(gold, pred))
            diffs.append(CaseDiff(case_id, gold_status, pred_status, _minus(gold, pred),
                                  _minus(pred, gold), _minus(common_gold, _lcs(common_pred, common_gold))))
    return Report(
        n_cases=len(gold_rows),
        missing_predictions=missing_cases,
        extra_predictions=sorted(set(pred_rows) - set(gold_rows), key=_case_key),
        status_correct=status_ok,
        status_confusion=dict(confusion),
        steps=steps,
        steps_no_evidence=steps_ne,
        field_matches=field_matches,
        field_positions=positions,
        sequence_exact=seq_ok,
        levenshtein_mean=_ratio(lev_total, len(gold_rows)),
        diffs=diffs,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: score a prediction CSV against a gold CSV."""
    ap = argparse.ArgumentParser(prog="python -m ghostaudit.scorer", description=__doc__)
    ap.add_argument("--pred", required=True, help="predicted output CSV")
    ap.add_argument("--gold", required=True, help="gold output CSV")
    ap.add_argument("--markdown", metavar="OUT.md", help="also write the report to this file")
    args = ap.parse_args(argv)
    md = score(read_rows(args.pred), read_rows(args.gold)).to_markdown()
    print(md, end="")
    if args.markdown:
        Path(args.markdown).write_text(md, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
