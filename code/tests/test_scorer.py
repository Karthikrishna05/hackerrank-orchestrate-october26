"""Tests for ghostaudit.scorer (tools_spec T12: local evaluator and CLI)."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from ghostaudit.output import read_rows, write_output
from ghostaudit.scorer import FIELDS, MISSING, PRF, Report, score
from ghostaudit.steps import make_step, sort_and_number, step_key

CODE_DIR = Path(__file__).resolve().parents[1]
DATASET = CODE_DIR.parent / "dataset"

CASE_1 = sort_and_number([
    make_step("VERIFY_RECORD_ABSENT", "account_service", "account_001", "evidence_54"),
    make_step("VERIFY_RECORD_PRESENT", "security_service", "audit_record_5416", "evidence_92"),
])
CASE_2 = sort_and_number([
    make_step("REPORT_EXPOSED_PII", "support_platform", "support_1", "evidence_03"),
    make_step("REPORT_EXPOSED_PII", "support_platform", "support_2", "evidence_03"),
    make_step("REDACT_RECORD", "support_platform", "support_1"),
    make_step("REDACT_RECORD", "support_platform", "support_2"),
    make_step("VERIFY_RECORD_REDACTED", "support_platform", "support_1"),
    make_step("VERIFY_RECORD_REDACTED", "support_platform", "support_2"),
])


def gold() -> dict:
    # case_10 first on purpose: the scorer must not depend on mapping order.
    return {"case_10": ("NOT_APPLICABLE", []),
            "case_1": ("COMPLETE", list(CASE_1)),
            "case_2": ("INCOMPLETE", list(CASE_2))}


def renumber(steps):
    return [replace(s, step_no=i) for i, s in enumerate(steps, 1)]


class MetricAssertions(unittest.TestCase):
    def assertPerfect(self, report: Report):
        self.assertEqual(report.status_accuracy, 1.0)
        self.assertEqual((report.steps.precision, report.steps.recall, report.steps.f1), (1.0, 1.0, 1.0))
        self.assertEqual(report.steps_no_evidence.f1, 1.0)
        self.assertEqual(report.field_accuracy, dict.fromkeys(FIELDS, 1.0))
        self.assertEqual(report.field_overall, 1.0)
        self.assertEqual(report.sequence_accuracy, 1.0)
        self.assertEqual(report.levenshtein_mean, 1.0)


class TestGoldVersusGold(MetricAssertions):
    def test_gold_against_itself_scores_one_on_every_metric(self):
        report = score(gold(), gold())
        self.assertPerfect(report)
        self.assertEqual(report.n_cases, 3)
        self.assertEqual(report.diffs, [])
        self.assertEqual(report.missing_predictions, [])
        self.assertEqual(report.extra_predictions, [])

    def test_step_numbers_are_ignored_when_comparing_steps(self):
        pred = gold()
        pred["case_1"] = ("COMPLETE", [replace(s, step_no=s.step_no + 10) for s in CASE_1])
        self.assertPerfect(score(pred, gold()))

    def test_status_confusion_counts_each_gold_status(self):
        report = score(gold(), gold())
        self.assertEqual(report.status_confusion, {("NOT_APPLICABLE", "NOT_APPLICABLE"): 1,
                                                   ("COMPLETE", "COMPLETE"): 1,
                                                   ("INCOMPLETE", "INCOMPLETE"): 1})

    def test_empty_gold_and_prediction_score_one(self):
        report = score({}, {})
        self.assertEqual(report.status_accuracy, 1.0)
        self.assertEqual(report.steps.f1, 1.0)
        self.assertEqual(report.sequence_accuracy, 1.0)


class TestSingleMutations(MetricAssertions):
    """Each hand-made mutation lowers its intended metric and leaves independent metrics at 1.0."""

    def test_status_change_lowers_only_status_accuracy(self):
        pred = gold()
        pred["case_1"] = ("INCOMPLETE", list(CASE_1))
        report = score(pred, gold())
        self.assertAlmostEqual(report.status_accuracy, 2 / 3)
        self.assertEqual(report.steps.f1, 1.0)
        self.assertEqual(report.field_overall, 1.0)
        self.assertEqual(report.sequence_accuracy, 1.0)
        self.assertEqual(report.levenshtein_mean, 1.0)
        self.assertEqual(report.status_confusion[("COMPLETE", "INCOMPLETE")], 1)

    def test_swapping_two_steps_lowers_sequence_but_not_step_set_or_status(self):
        swapped = list(CASE_2)
        swapped[0], swapped[1] = swapped[1], swapped[0]
        pred = gold()
        pred["case_2"] = ("INCOMPLETE", renumber(swapped))
        report = score(pred, gold())
        self.assertAlmostEqual(report.sequence_accuracy, 2 / 3)
        self.assertLess(report.levenshtein_mean, 1.0)
        self.assertEqual(report.status_accuracy, 1.0)
        self.assertEqual((report.steps.precision, report.steps.recall), (1.0, 1.0))
        # Only resource_id differs between the swapped steps, so only that field drops.
        accuracy = report.field_accuracy
        self.assertLess(accuracy["resource_id"], 1.0)
        self.assertEqual({f: a for f, a in accuracy.items() if f != "resource_id"},
                         dict.fromkeys([f for f in FIELDS if f != "resource_id"], 1.0))

    def test_wrong_evidence_lowers_evidence_field_and_full_step_match_only(self):
        pred = gold()
        pred["case_1"] = ("COMPLETE", [replace(CASE_1[0], evidence_id="evidence_55"), CASE_1[1]])
        report = score(pred, gold())
        accuracy = report.field_accuracy
        self.assertLess(accuracy["evidence_id"], 1.0)
        self.assertEqual({f: a for f, a in accuracy.items() if f != "evidence_id"},
                         dict.fromkeys([f for f in FIELDS if f != "evidence_id"], 1.0))
        self.assertLess(report.steps.f1, 1.0)
        self.assertEqual(report.steps_no_evidence.f1, 1.0)
        self.assertEqual(report.status_accuracy, 1.0)

    def test_missing_step_lowers_recall_but_not_precision(self):
        pred = gold()
        pred["case_2"] = ("INCOMPLETE", list(CASE_2[:-1]))
        report = score(pred, gold())
        self.assertLess(report.steps.recall, 1.0)
        self.assertEqual(report.steps.precision, 1.0)
        self.assertEqual(report.status_accuracy, 1.0)
        self.assertEqual((report.steps.tp, report.steps.pred, report.steps.gold), (7, 7, 8))

    def test_extra_step_lowers_precision_but_not_recall(self):
        pred = gold()
        extra = make_step("VERIFY_RECORD_ABSENT", "search_service", "search_9", "evidence_54")
        pred["case_1"] = ("COMPLETE", renumber(list(CASE_1) + [extra]))
        report = score(pred, gold())
        self.assertLess(report.steps.precision, 1.0)
        self.assertEqual(report.steps.recall, 1.0)
        self.assertEqual(report.status_accuracy, 1.0)

    def test_wrong_operation_and_state_lowers_only_those_fields(self):
        pred = gold()
        wrong = replace(CASE_1[0], operation="VERIFY_RECORD_REDACTED", state="REDACTED")
        pred["case_1"] = ("COMPLETE", [wrong, CASE_1[1]])
        accuracy = score(pred, gold()).field_accuracy
        self.assertLess(accuracy["operation"], 1.0)
        self.assertLess(accuracy["state"], 1.0)
        for f in ("phase", "system_id", "resource_id", "evidence_id"):
            self.assertEqual(accuracy[f], 1.0, f)

    def test_missing_prediction_is_counted_wrong(self):
        pred = gold()
        del pred["case_2"]
        report = score(pred, gold())
        self.assertEqual(report.missing_predictions, ["case_2"])
        self.assertAlmostEqual(report.status_accuracy, 2 / 3)
        self.assertAlmostEqual(report.steps.recall, 2 / 8)
        self.assertAlmostEqual(report.sequence_accuracy, 2 / 3)
        self.assertEqual(report.status_confusion[("INCOMPLETE", MISSING)], 1)

    def test_extra_predicted_case_is_listed_and_ignored(self):
        pred = gold()
        pred["case_99"] = ("NOT_APPLICABLE", [])
        report = score(pred, gold())
        self.assertEqual(report.extra_predictions, ["case_99"])
        self.assertPerfect(report)


class TestCaseDiffs(unittest.TestCase):
    def test_diff_lists_missing_and_extra_step_tuples(self):
        pred = gold()
        wrong = replace(CASE_1[0], evidence_id="evidence_55")
        pred["case_1"] = ("COMPLETE", [wrong, CASE_1[1]])
        (diff,) = score(pred, gold()).diffs
        self.assertEqual(diff.case_id, "case_1")
        self.assertEqual(diff.missing, [step_key(CASE_1[0])])
        self.assertEqual(diff.extra, [step_key(wrong)])
        self.assertEqual(diff.out_of_order, [])

    def test_diff_lists_out_of_order_steps_for_a_swap(self):
        swapped = list(CASE_2)
        swapped[0], swapped[1] = swapped[1], swapped[0]
        pred = gold()
        pred["case_2"] = ("INCOMPLETE", renumber(swapped))
        (diff,) = score(pred, gold()).diffs
        self.assertEqual((diff.missing, diff.extra), ([], []))
        self.assertEqual(len(diff.out_of_order), 1)

    def test_status_only_difference_produces_a_diff(self):
        pred = gold()
        pred["case_10"] = ("COMPLETE", [])
        (diff,) = score(pred, gold()).diffs
        self.assertEqual((diff.case_id, diff.gold_status, diff.pred_status), ("case_10", "NOT_APPLICABLE", "COMPLETE"))

    def test_diffs_are_in_numeric_case_order(self):
        pred = gold()
        pred["case_10"] = ("COMPLETE", [])
        pred["case_2"] = ("COMPLETE", list(CASE_2))
        pred["case_1"] = ("INCOMPLETE", list(CASE_1))
        self.assertEqual([d.case_id for d in score(pred, gold()).diffs], ["case_1", "case_2", "case_10"])

    def test_missing_prediction_diff_uses_missing_marker(self):
        pred = gold()
        del pred["case_1"]
        (diff,) = score(pred, gold()).diffs
        self.assertEqual(diff.pred_status, MISSING)
        self.assertEqual(diff.missing, [step_key(s) for s in CASE_1])


class TestPRF(unittest.TestCase):
    def test_empty_counts_are_perfect(self):
        m = PRF()
        self.assertEqual((m.precision, m.recall, m.f1), (1.0, 1.0, 1.0))

    def test_no_predictions_against_gold_is_zero(self):
        m = PRF(tp=0, pred=0, gold=4)
        self.assertEqual((m.precision, m.recall, m.f1), (0.0, 0.0, 0.0))

    def test_f1_is_harmonic_mean(self):
        m = PRF(tp=2, pred=4, gold=2)
        self.assertEqual((m.precision, m.recall), (0.5, 1.0))
        self.assertAlmostEqual(m.f1, 2 / 3)


class TestMarkdown(unittest.TestCase):
    def test_markdown_shows_headline_metrics(self):
        md = score(gold(), gold()).to_markdown()
        self.assertIn("| Case status accuracy | 100.00% (3/3) |", md)
        self.assertIn("| Exact sequence accuracy | 100.00% (3/3) |", md)
        self.assertIn("Step precision / recall / F1", md)
        for f in FIELDS:
            self.assertIn(f"| {f} | 100.00% |", md)

    def test_markdown_lists_per_case_differences(self):
        pred = gold()
        pred["case_1"] = ("INCOMPLETE", list(CASE_1))
        md = score(pred, gold()).to_markdown()
        self.assertIn("**case_1**", md)
        self.assertIn("status INCOMPLETE (gold COMPLETE)", md)

    def test_markdown_truncates_diffs(self):
        pred = {k: ("COMPLETE" if v[0] != "COMPLETE" else "INCOMPLETE", v[1]) for k, v in gold().items()}
        md = score(pred, gold()).to_markdown(max_diffs=1)
        self.assertIn("... 2 more", md)


class TestScoreSignature(unittest.TestCase):
    # Regression: score() used to reject CSV paths (tools_spec T12 takes file paths).
    def test_score_accepts_csv_paths_as_in_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [(c, s, st) for c, (s, st) in sorted(gold().items())]
            path = write_output(rows, Path(tmp) / "gold.csv")
            report = score(path, path)
        self.assertEqual(report.status_accuracy, 1.0)


class TestScorerCli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.gold_path = write_output([(c, s, st) for c, (s, st) in gold().items()], self.dir / "gold.csv")

    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "ghostaudit.scorer", *args], cwd=CODE_DIR,
                              capture_output=True, text=True, timeout=60)

    def test_cli_gold_against_gold_reports_full_marks(self):
        result = self.run_cli("--pred", str(self.gold_path), "--gold", str(self.gold_path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("| Case status accuracy | 100.00% (3/3) |", result.stdout)

    def test_cli_reports_lower_status_accuracy_for_a_mutated_prediction(self):
        pred = gold()
        pred["case_1"] = ("INCOMPLETE", list(CASE_1))
        pred_path = write_output([(c, s, st) for c, (s, st) in pred.items()], self.dir / "pred.csv")
        result = self.run_cli("--pred", str(pred_path), "--gold", str(self.gold_path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("| Case status accuracy | 66.67% (2/3) |", result.stdout)

    def test_cli_writes_markdown_file_matching_stdout(self):
        md_path = self.dir / "report.md"
        result = self.run_cli("--pred", str(self.gold_path), "--gold", str(self.gold_path), "--markdown", str(md_path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(md_path.read_text(encoding="utf-8"), result.stdout)

    def test_cli_requires_pred_and_gold(self):
        self.assertEqual(self.run_cli("--gold", str(self.gold_path)).returncode, 2)


@unittest.skipUnless(DATASET.is_dir(), "dataset not available")
class TestScorerOnGoldSample(MetricAssertions):
    def test_sample_gold_against_itself_scores_one(self):
        rows = read_rows(DATASET / "sample_output.csv")
        report = score(rows, rows)
        self.assertEqual(report.n_cases, 32)
        self.assertPerfect(report)
        self.assertEqual(report.diffs, [])

    def test_cli_on_sample_gold(self):
        result = subprocess.run([sys.executable, "-m", "ghostaudit.scorer", "--pred", str(DATASET / "sample_output.csv"),
                                 "--gold", str(DATASET / "sample_output.csv")],
                                cwd=CODE_DIR, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("100.00% (32/32)", result.stdout)


if __name__ == "__main__":
    unittest.main()
