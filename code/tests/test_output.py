"""Tests for ghostaudit.output (tools_spec T11: output writer, reader, validator and CLI)."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from ghostaudit import output
from ghostaudit.models import Request
from ghostaudit.output import HEADER, order_like_requests, read_rows, validate_output, write_output
from ghostaudit.steps import make_step, sort_and_number

CODE_DIR = Path(__file__).resolve().parents[1]
DATASET = CODE_DIR.parent / "dataset"


def req(case_id: str) -> Request:
    return Request(case_id, f"{case_id}@x.com", "2026-01-01T00:00:00Z", "COMPLETE",
                   "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z", "Please delete my account")


REQUESTS = [req("case_1"), req("case_2"), req("case_10")]

COMPLETE_STEPS = sort_and_number([make_step("VERIFY_RECORD_ABSENT", "account_service", "account_001", "evidence_54")])
INCOMPLETE_STEPS = sort_and_number([
    make_step("REPORT_REMAINING_RECORD", "search_service", "search_052", "evidence_03"),
    make_step("DELETE_RECORD", "search_service", "search_052"),
    make_step("VERIFY_RECORD_ABSENT", "search_service", "search_052"),
])

GOOD_ROWS = [("case_1", "COMPLETE", COMPLETE_STEPS),
             ("case_2", "INCOMPLETE", INCOMPLETE_STEPS),
             ("case_10", "NOT_APPLICABLE", [])]


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def write_text(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8", newline="")
        return path

    def good_file(self) -> Path:
        return write_output(GOOD_ROWS, self.dir / "out.csv")

    def lines(self, path: Path) -> list[str]:
        return path.read_text(encoding="utf-8").split("\n")


class TestWriteOutput(TempDirTestCase):
    def test_write_output_writes_the_exact_header(self):
        path = self.good_file()
        self.assertEqual(self.lines(path)[0], "case_id,deletion_status,steps_json")
        self.assertEqual(HEADER, ["case_id", "deletion_status", "steps_json"])

    def test_write_output_returns_the_path_it_wrote(self):
        target = self.dir / "named.csv"
        self.assertEqual(write_output(GOOD_ROWS, str(target)), target)
        self.assertTrue(target.is_file())

    def test_write_output_keeps_the_given_row_order(self):
        path = write_output(list(reversed(GOOD_ROWS)), self.dir / "out.csv")
        with open(path, encoding="utf-8", newline="") as fh:
            self.assertEqual([r["case_id"] for r in csv.DictReader(fh)], ["case_10", "case_2", "case_1"])

    def test_not_applicable_row_is_written_as_empty_json_array(self):
        path = write_output([("case_000", "NOT_APPLICABLE", [])], self.dir / "out.csv")
        self.assertEqual(self.lines(path)[1], "case_000,NOT_APPLICABLE,[]")

    def test_steps_json_column_is_csv_quoted_and_parses_back(self):
        path = self.good_file()
        self.assertTrue(self.lines(path)[2].startswith('case_2,INCOMPLETE,"[{""step_no"":1,'))
        with open(path, encoding="utf-8", newline="") as fh:
            rows = {r["case_id"]: r for r in csv.DictReader(fh)}
        parsed = json.loads(rows["case_2"]["steps_json"])
        self.assertEqual([s["operation"] for s in parsed],
                         ["REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"])

    def test_write_output_uses_utf8_without_bom_and_lf_line_endings(self):
        raw = self.good_file().read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\r\n", raw)
        self.assertTrue(raw.endswith(b"\n"))

    def test_write_output_defaults_to_output_csv_at_repo_root(self):
        with mock.patch("ghostaudit.paths.repo_root", return_value=self.dir):
            path = write_output(GOOD_ROWS)
        self.assertEqual(path, self.dir / "output.csv")
        self.assertTrue(path.is_file())


class TestOrderLikeRequests(unittest.TestCase):
    def test_reorders_rows_to_request_file_order(self):
        shuffled = [GOOD_ROWS[2], GOOD_ROWS[0], GOOD_ROWS[1]]
        self.assertEqual([r[0] for r in order_like_requests(shuffled, REQUESTS)], ["case_1", "case_2", "case_10"])

    def test_unknown_case_id_raises_value_error(self):
        with self.assertRaises(ValueError):
            order_like_requests(GOOD_ROWS + [("case_99", "NOT_APPLICABLE", [])], REQUESTS)

    def test_duplicate_case_id_raises_value_error(self):
        with self.assertRaises(ValueError):
            order_like_requests(GOOD_ROWS + [GOOD_ROWS[0]], REQUESTS)


class TestReadRows(TempDirTestCase):
    def test_read_rows_round_trips_write_output(self):
        rows = read_rows(self.good_file())
        self.assertEqual(rows, {case_id: (status, list(steps)) for case_id, status, steps in GOOD_ROWS})

    def test_read_rows_accepts_alphabetical_key_order(self):
        steps = ('[{""evidence_id"":""evidence_54"",""operation"":""VERIFY_RECORD_ABSENT"",""phase"":""VERIFICATION"",'
                 '""resource_id"":""account_001"",""state"":""ABSENT"",""step_no"":1,""system_id"":""account_service""}]')
        path = self.write_text("gold.csv", f'case_id,deletion_status,steps_json\ncase_1,COMPLETE,"{steps}"\n')
        self.assertEqual(read_rows(path), {"case_1": ("COMPLETE", list(COMPLETE_STEPS))})

    def test_read_rows_accepts_a_utf8_bom(self):
        path = self.dir / "bom.csv"
        path.write_text("case_id,deletion_status,steps_json\ncase_1,NOT_APPLICABLE,[]\n", encoding="utf-8-sig")
        self.assertEqual(read_rows(path), {"case_1": ("NOT_APPLICABLE", [])})

    def test_read_rows_rejects_wrong_header(self):
        path = self.write_text("bad.csv", "case_id,status,steps_json\ncase_1,NOT_APPLICABLE,[]\n")
        with self.assertRaises(ValueError):
            read_rows(path)

    def test_read_rows_rejects_wrong_field_count(self):
        path = self.write_text("bad.csv", "case_id,deletion_status,steps_json\ncase_1,NOT_APPLICABLE\n")
        with self.assertRaises(ValueError):
            read_rows(path)

    def test_read_rows_rejects_duplicate_case(self):
        path = self.write_text("bad.csv", "case_id,deletion_status,steps_json\n"
                               "case_1,NOT_APPLICABLE,[]\ncase_1,NOT_APPLICABLE,[]\n")
        with self.assertRaises(ValueError):
            read_rows(path)

    def test_read_rows_reports_line_and_case_for_bad_json(self):
        path = self.write_text("bad.csv", "case_id,deletion_status,steps_json\n"
                               "case_1,NOT_APPLICABLE,[]\ncase_2,COMPLETE,[{oops\n")
        with self.assertRaises(ValueError) as ctx:
            read_rows(path)
        self.assertIn("line 3", str(ctx.exception))
        self.assertIn("case_2", str(ctx.exception))


class TestValidateOutput(TempDirTestCase):
    def assertOneErrorContaining(self, errors: list[str], *needles: str):
        matching = [e for e in errors if all(n in e for n in needles)]
        self.assertTrue(matching, f"no error containing {needles!r} in {errors!r}")

    def test_valid_file_has_no_errors(self):
        self.assertEqual(validate_output(self.good_file(), REQUESTS), [])

    def test_wrong_header_is_reported(self):
        path = self.write_text("bad.csv", "case_id,status,steps\n"
                               "case_1,NOT_APPLICABLE,[]\ncase_2,NOT_APPLICABLE,[]\ncase_10,NOT_APPLICABLE,[]\n")
        self.assertOneErrorContaining(validate_output(path, REQUESTS), "header")

    def test_header_with_extra_column_is_reported(self):
        path = self.write_text("bad.csv", "case_id,deletion_status,steps_json,notes\n")
        self.assertOneErrorContaining(validate_output(path, REQUESTS), "header")

    def test_duplicate_row_is_reported(self):
        path = write_output(GOOD_ROWS + [GOOD_ROWS[0]], self.dir / "out.csv")
        self.assertOneErrorContaining(validate_output(path, REQUESTS), "duplicate", "case_1")

    def test_missing_row_is_reported(self):
        path = write_output(GOOD_ROWS[:2], self.dir / "out.csv")
        self.assertOneErrorContaining(validate_output(path, REQUESTS), "case_10")

    def test_case_not_in_request_file_is_reported(self):
        path = write_output(GOOD_ROWS + [("case_77", "NOT_APPLICABLE", [])], self.dir / "out.csv")
        self.assertOneErrorContaining(validate_output(path, REQUESTS), "case_77")

    def test_bad_json_is_reported_with_its_case(self):
        path = self.write_text("bad.csv", "case_id,deletion_status,steps_json\n"
                               "case_1,NOT_APPLICABLE,[]\ncase_2,NOT_APPLICABLE,not-json\ncase_10,NOT_APPLICABLE,[]\n")
        self.assertOneErrorContaining(validate_output(path, REQUESTS), "case_2")

    def test_json_object_instead_of_array_is_reported(self):
        path = self.write_text("bad.csv", "case_id,deletion_status,steps_json\n"
                               'case_1,NOT_APPLICABLE,{}\ncase_2,NOT_APPLICABLE,[]\ncase_10,NOT_APPLICABLE,[]\n')
        self.assertOneErrorContaining(validate_output(path, REQUESTS), "case_1")

    def test_invalid_status_is_reported(self):
        path = write_output([GOOD_ROWS[0], ("case_2", "PARTIALLY_COMPLETE", INCOMPLETE_STEPS), GOOD_ROWS[2]],
                            self.dir / "out.csv")
        self.assertOneErrorContaining(validate_output(path, REQUESTS), "case_2", "PARTIALLY_COMPLETE")

    def test_rows_out_of_request_order_are_reported(self):
        path = write_output([GOOD_ROWS[1], GOOD_ROWS[0], GOOD_ROWS[2]], self.dir / "out.csv")
        self.assertOneErrorContaining(validate_output(path, REQUESTS), "order")

    def test_numeric_case_order_is_accepted(self):
        # case_10 after case_2 is correct numeric order even though "case_10" < "case_2" as strings.
        self.assertEqual(validate_output(self.good_file(), REQUESTS), [])

    def test_wrong_field_count_is_reported(self):
        path = self.write_text("bad.csv", "case_id,deletion_status,steps_json\n"
                               "case_1,NOT_APPLICABLE\ncase_2,NOT_APPLICABLE,[]\ncase_10,NOT_APPLICABLE,[]\n")
        self.assertOneErrorContaining(validate_output(path, REQUESTS), "line 2", "3 fields")

    def test_step_rule_violation_is_reported_with_its_case(self):
        bad = [replace(s, evidence_id="") for s in COMPLETE_STEPS]
        path = write_output([("case_1", "COMPLETE", bad), GOOD_ROWS[1], GOOD_ROWS[2]], self.dir / "out.csv")
        self.assertOneErrorContaining(validate_output(path, REQUESTS), "case_1", "evidence")

    def test_non_consecutive_step_numbers_are_reported(self):
        bad = [replace(s, step_no=s.step_no * 2) for s in INCOMPLETE_STEPS]
        path = write_output([GOOD_ROWS[0], ("case_2", "INCOMPLETE", bad), GOOD_ROWS[2]], self.dir / "out.csv")
        self.assertOneErrorContaining(validate_output(path, REQUESTS), "case_2", "step_no")

    def test_not_applicable_with_steps_is_reported(self):
        path = write_output([GOOD_ROWS[0], GOOD_ROWS[1], ("case_10", "NOT_APPLICABLE", COMPLETE_STEPS)],
                            self.dir / "out.csv")
        self.assertOneErrorContaining(validate_output(path, REQUESTS), "case_10", "NOT_APPLICABLE")

    def test_empty_file_reports_header_and_every_missing_request(self):
        errors = validate_output(self.write_text("empty.csv", ""), REQUESTS)
        self.assertOneErrorContaining(errors, "header")
        self.assertOneErrorContaining(errors, "3 request(s)")


class TestOutputCli(TempDirTestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "ghostaudit.output", *args], cwd=CODE_DIR,
                              capture_output=True, text=True, timeout=60)

    def requests_file(self) -> Path:
        lines = ["case_id,email,requested_at,reported_status,reported_at,audit_at,request_text"]
        lines += [",".join([r.case_id, r.email, r.requested_at, r.reported_status, r.reported_at,
                            r.audit_at, r.request_text]) for r in REQUESTS]
        return self.write_text("requests.csv", "\n".join(lines) + "\n")

    def test_cli_exits_zero_and_prints_ok_for_a_valid_file(self):
        result = self.run_cli("--check", str(self.good_file()), "--requests", str(self.requests_file()))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK", result.stdout)
        self.assertIn("3 requests", result.stdout)

    def test_cli_exits_one_and_lists_violations_for_an_invalid_file(self):
        bad = write_output(GOOD_ROWS[:2], self.dir / "bad.csv")
        result = self.run_cli("--check", str(bad), "--requests", str(self.requests_file()))
        self.assertEqual(result.returncode, 1)
        self.assertIn("case_10", result.stdout)
        self.assertIn("violation", result.stdout)

    def test_cli_requires_check_argument(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 2)

    def test_main_returns_exit_code_in_process(self):
        with mock.patch("sys.stdout"):
            code = output.main(["--check", str(self.good_file()), "--requests", str(self.requests_file())])
        self.assertEqual(code, 0)


@unittest.skipUnless(DATASET.is_dir(), "dataset not available")
class TestOutputOnRealDataset(unittest.TestCase):
    def test_gold_sample_output_validates_against_sample_requests(self):
        from ghostaudit import loaders
        requests = loaders.load_requests("sample_deletion_requests.csv")
        self.assertEqual(validate_output(DATASET / "sample_output.csv", requests), [])

    def test_cli_accepts_gold_sample_with_dataset_file_name(self):
        result = subprocess.run([sys.executable, "-m", "ghostaudit.output", "--check",
                                 str(DATASET / "sample_output.csv"), "--requests", "sample_deletion_requests.csv"],
                                cwd=CODE_DIR, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("32 requests", result.stdout)

    def test_blank_template_reports_all_518_requests_missing(self):
        from ghostaudit import loaders
        errors = validate_output(DATASET / "output.csv", loaders.load_requests())
        self.assertEqual(len(errors), 1)
        self.assertIn("518 request(s)", errors[0])

    def test_request_file_is_in_numeric_case_order(self):
        from ghostaudit import loaders
        ids = [int(r.case_id.split("_")[1]) for r in loaders.load_requests()]
        self.assertEqual(ids, sorted(ids))


if __name__ == "__main__":
    unittest.main()
