"""Independent tests for the CLI `python code/main.py` (engine_plan.md §3 module contracts).

Every run writes into a temporary directory; the repository's own output.csv is never written
(checked by hashing it before and after the class).
"""

from __future__ import annotations

import csv
import hashlib
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ghostaudit import loaders, paths
from ghostaudit.output import validate_output

REPO = paths.repo_root()
MAIN = REPO / "code" / "main.py"
DATA = paths.dataset_dir()
DATASET_OK = all((DATA / n).is_file() for n in (
    "deletion_requests.csv", "sample_deletion_requests.csv", "evidence_files.csv", "user_identities.csv",
    "user_audit_log.csv", "system_activity.csv", "deletion_rules.csv"))
INTENT_CACHE = REPO / "code" / "cache" / "intent"
HAS_INTENT_CACHE = INTENT_CACHE.is_dir() and any(INTENT_CACHE.glob("*.json"))
STATUSES = ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")


def run_cli(*args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(MAIN), *args], cwd=REPO, capture_output=True, text=True,
                          encoding="utf-8", timeout=timeout)


def _digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _statuses_line(stdout: str) -> dict[str, int]:
    m = re.search(r"^statuses: (.*)$", stdout, re.MULTILINE)
    if not m:
        return {}
    return {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", m.group(1))}


def _file_statuses(path: Path) -> list[tuple[str, str]]:
    with open(path, encoding="utf-8", newline="") as fh:
        return [(row["case_id"], row["deletion_status"]) for row in csv.DictReader(fh)]


class CliArgumentTest(unittest.TestCase):
    """Argument handling; argparse fails before any data is loaded."""

    def test_unknown_argument_fails(self):
        proc = run_cli("--bogus")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("unrecognized arguments", proc.stderr)

    def test_unknown_intent_mode_fails(self):
        proc = run_cli("--intent", "llm")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("invalid choice", proc.stderr)

    def test_help_lists_the_options(self):
        proc = run_cli("--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for option in ("--requests", "--output", "--intent", "auto", "rules", "cache"):
            self.assertIn(option, proc.stdout)


@unittest.skipUnless(DATASET_OK, "participant dataset not present")
class CliRunTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.repo_output = REPO / "output.csv"
        cls.repo_output_digest = _digest(cls.repo_output)
        cls.tmp = tempfile.TemporaryDirectory(prefix="ghostaudit-cli-")
        cls.tmpdir = Path(cls.tmp.name)
        cls.requests = loaders.load_requests()
        cls.out = cls.tmpdir / "output.csv"
        cls.proc = run_cli("--intent", "rules", "--output", str(cls.out))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        if _digest(cls.repo_output) != cls.repo_output_digest:
            raise AssertionError("the repository output.csv was modified by the CLI tests")

    def test_rules_run_exits_zero_and_output_validates(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)
        self.assertTrue(self.out.is_file())
        self.assertEqual(validate_output(self.out, self.requests), [])
        self.assertIn("validate_output: OK", self.proc.stdout)

    def test_one_row_per_request_in_request_order(self):
        rows = _file_statuses(self.out)
        self.assertEqual(len(rows), 518)
        self.assertEqual([c for c, _ in rows], [r.case_id for r in self.requests])
        numbers = [int(c.split("_")[1]) for c, _ in rows]
        self.assertEqual(numbers, sorted(numbers))

    def test_summary_mentions_the_status_counts(self):
        reported = _statuses_line(self.proc.stdout)
        self.assertEqual(set(reported), set(STATUSES), self.proc.stdout)
        self.assertEqual(sum(reported.values()), 518)
        written = {s: sum(st == s for _, st in _file_statuses(self.out)) for s in STATUSES}
        self.assertEqual(reported, written)
        self.assertEqual(reported["NOT_APPLICABLE"], 10)
        self.assertIn("518 rows from 518 requests", self.proc.stdout)
        self.assertIn("intent sources: rules=518", self.proc.stdout)

    def test_sample_requests_by_name_and_by_path_give_identical_bytes(self):
        by_name = self.tmpdir / "samples_by_name.csv"
        by_path = self.tmpdir / "samples_by_path.csv"
        first = run_cli("--requests", "sample_deletion_requests.csv", "--intent", "rules", "--output", str(by_name))
        second = run_cli("--requests", str(DATA / "sample_deletion_requests.csv"), "--intent", "rules",
                         "--output", str(by_path))
        for proc in (first, second):
            self.assertEqual(proc.returncode, 0, proc.stderr)
        samples = loaders.load_requests("sample_deletion_requests.csv")
        self.assertEqual(validate_output(by_name, samples), [])
        self.assertEqual(len(_file_statuses(by_name)), 32)
        self.assertEqual(by_name.read_bytes(), by_path.read_bytes(), "same input must give the same output")

    @unittest.skipIf(HAS_INTENT_CACHE, "an LLM intent cache exists, so --intent cache can succeed")
    def test_cache_mode_without_cache_fails_clearly(self):
        out = self.tmpdir / "cache_mode.csv"
        proc = run_cli("--intent", "cache", "--output", str(out))
        self.assertNotEqual(proc.returncode, 0)
        self.assertRegex(proc.stderr, r"(?m)^error: .*cache")
        self.assertNotIn("Traceback", proc.stderr)
        self.assertFalse(out.exists(), "no output file is written when the intent source fails")

    def test_missing_request_file_fails_without_writing(self):
        out = self.tmpdir / "missing.csv"
        proc = run_cli("--requests", "no_such_requests.csv", "--intent", "rules", "--output", str(out))
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
