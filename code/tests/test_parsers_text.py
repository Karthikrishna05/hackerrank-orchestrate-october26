"""Tests for ghostaudit.parsers.text_records (tools_spec.md T6, text handover row)."""

from __future__ import annotations

import tempfile
import unittest
import warnings
from pathlib import Path

from ghostaudit.models import ManifestEntry
from ghostaudit.parsers import text_records

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "parsers"
MANIFEST_CAPTURED_AT = "2026-10-01T12:00:00Z"


def entry_for(path: Path, file_id: str = "evidence_t4") -> ManifestEntry:
    return ManifestEntry(file_id=file_id, file_path=str(path), origin="test_fixture",
                         captured_at=MANIFEST_CAPTURED_AT)


def parse_text(path: Path):
    """Parse one handover file; return (observations, coverages, warning messages)."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        obs, cov = text_records.parse(entry_for(path), path)
    return obs, cov, [str(w.message) for w in caught]


def only(observations, resource_id):
    found = [o for o in observations if o.resource_id == resource_id]
    if len(found) != 1:
        raise AssertionError(f"expected one observation of {resource_id}, got {len(found)}")
    return found[0]


class HandoverRecordTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.obs, cls.cov, cls.warnings = parse_text(FIXTURES / "handover.txt")

    def test_header_and_prose_lines_are_ignored_silently(self):
        self.assertEqual(self.warnings, [])

    def test_one_observation_per_record_line(self):
        self.assertEqual([o.resource_id for o in self.obs],
                         ["account_1810", "analytics_6830", "backup_706", "data_flow_9522"])

    def test_handover_gives_no_coverage(self):
        self.assertEqual(self.cov, [])

    def test_result_column_is_the_state(self):
        self.assertEqual([o.state for o in self.obs],
                         ["NOT_FOUND", "ANONYMIZED", "BLOCKED", "ACTIVE"])

    def test_record_type_column_is_the_category(self):
        self.assertEqual([o.category for o in self.obs],
                         ["USER_PROFILE", "ANALYTICS_PROFILE", "BACKUP_ARCHIVE", "DATA_FLOW"])

    def test_system_column_matches_the_resource_system(self):
        self.assertEqual([o.system_id for o in self.obs],
                         ["account_service", "analytics_platform", "backup_service",
                          "integration_service"])

    def test_uppercase_email_subject_is_normalised(self):
        self.assertEqual(only(self.obs, "account_1810").identifiers,
                         frozenset({"choi.jiwoo94@example.com"}))

    def test_user_id_subject_is_an_identifier(self):
        self.assertEqual(only(self.obs, "backup_706").identifiers, frozenset({"user_39"}))

    def test_phone_subject_is_normalised(self):
        self.assertEqual(only(self.obs, "data_flow_9522").identifiers,
                         frozenset({"+819076100000"}))

    def test_placeholder_subject_gives_no_identifiers(self):
        self.assertEqual(only(self.obs, "analytics_6830").identifiers, frozenset())

    def test_observed_at_is_the_row_time(self):
        self.assertEqual(only(self.obs, "analytics_6830").observed_at, "2026-10-01T11:30:00Z")

    def test_captured_at_is_the_manifest_time(self):
        self.assertEqual({o.captured_at for o in self.obs}, {MANIFEST_CAPTURED_AT})

    def test_fields_keep_the_six_named_columns(self):
        self.assertEqual(only(self.obs, "account_1810").fields, {
            "recorded_at": "2026-10-01T12:00:00Z", "system": "account_service",
            "resource_id": "account_1810", "record_type": "USER_PROFILE", "result": "NOT_FOUND",
            "subject_reference": "CHOI.JIWOO94@EXAMPLE.COM"})

    def test_source_is_the_line_number(self):
        self.assertEqual(only(self.obs, "account_1810").source, "txt:L6")


class MalformedHandoverTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, *lines: str) -> Path:
        path = self.tmp / "handover.txt"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_record_line_with_missing_columns_warns(self):
        path = self.write(
            "Snapshot: 2026-10-01T12:00:00Z",
            "- 2026-10-01T12:00:00Z | account_service | account_1 | USER_PROFILE | FOUND | user_1",
            "- 2026-10-01T12:00:00Z | account_service | account_2 | USER_PROFILE")
        obs, _, warns = parse_text(path)
        self.assertEqual([o.resource_id for o in obs], ["account_1"])
        self.assertEqual(len(warns), 1)
        self.assertIn("evidence_t4 line 3", warns[0])

    def test_unknown_result_word_becomes_unknown_with_a_warning(self):
        path = self.write(
            "- 2026-10-01T12:00:00Z | account_service | account_1 | USER_PROFILE | PENDING | user_1")
        obs, _, warns = parse_text(path)
        self.assertEqual(obs[0].state, "UNKNOWN")
        self.assertEqual(len(warns), 1)

    def test_file_without_record_lines_raises(self):
        path = self.write("CUSTOMER PLATFORM - SHIFT HANDOVER", "Nothing to report.")
        with self.assertRaisesRegex(ValueError, "evidence_t4"):
            parse_text(path)


if __name__ == "__main__":
    unittest.main()
