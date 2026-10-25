"""Unit tests for ghostaudit.parsers.json_records (tools_spec.md T6, JSON row).

Shapes: flat `records[]` lists with an explicit `result`, snapshot bundles (including
`operational_record_history` and an empty snapshot), `service_resource_export`, and
collection dumps.
"""

from __future__ import annotations

import json
import tempfile
import unittest
import warnings
from pathlib import Path

from ghostaudit.models import ManifestEntry
from ghostaudit.parsers import json_records

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "parsers"


def entry_for(path: Path, file_id: str = "evidence_t2",
              captured_at: str = "2026-09-30T00:00:00Z") -> ManifestEntry:
    return ManifestEntry(file_id=file_id, file_path=str(path), origin="test_fixture",
                         captured_at=captured_at)


def parse_json(path: Path, **entry_kw):
    """Parse one JSON file; return (observations, coverages, warning messages)."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        obs, cov = json_records.parse(entry_for(path, **entry_kw), path)
    return obs, cov, [str(w.message) for w in caught]


def only(observations, resource_id):
    found = [o for o in observations if o.resource_id == resource_id]
    if len(found) != 1:
        raise AssertionError(f"expected one observation of {resource_id}, got {len(found)}")
    return found[0]


class TempJsonMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, doc) -> Path:
        path = self.tmp / "evidence.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path


class FlatRecordsListTests(unittest.TestCase):
    """`records[]` with `system`, `record_type`, `result`, `subject_reference`."""

    @classmethod
    def setUpClass(cls):
        cls.captured = "2026-08-12T09:30:00Z"
        cls.obs, cls.cov, cls.warnings = parse_json(FIXTURES / "flat_records.json",
                                                    captured_at=cls.captured)

    def test_parses_without_warnings(self):
        self.assertEqual(self.warnings, [])

    def test_one_observation_per_record(self):
        self.assertEqual([o.resource_id for o in self.obs],
                         ["account_1084", "analytics_2034", "data_flow_9522", "analytics_6830"])

    def test_explicit_result_is_used_as_the_state(self):
        self.assertEqual([o.state for o in self.obs],
                         ["NOT_FOUND", "IDENTIFIABLE", "ACTIVE", "ANONYMIZED"])

    def test_record_type_gives_the_category(self):
        self.assertEqual([o.category for o in self.obs],
                         ["USER_PROFILE", "ANALYTICS_PROFILE", "DATA_FLOW", "ANALYTICS_PROFILE"])

    def test_system_comes_from_the_resource_prefix(self):
        self.assertEqual(only(self.obs, "data_flow_9522").system_id, "integration_service")

    def test_observed_at_is_the_record_recorded_at(self):
        self.assertEqual(only(self.obs, "account_1084").observed_at, "2026-08-12T08:00:00Z")

    def test_record_without_time_falls_back_to_export_time(self):
        self.assertEqual(only(self.obs, "analytics_6830").observed_at, "2026-08-12T09:00:00Z")

    def test_captured_at_is_the_manifest_time(self):
        self.assertEqual({o.captured_at for o in self.obs}, {self.captured})

    def test_uppercase_subject_reference_is_normalised(self):
        self.assertEqual(only(self.obs, "account_1084").identifiers,
                         frozenset({"m.alvarez@example.com"}))

    def test_user_id_subject_reference_is_an_identifier(self):
        self.assertEqual(only(self.obs, "data_flow_9522").identifiers, frozenset({"user_907"}))

    def test_placeholder_subject_gives_no_identifiers(self):
        self.assertEqual(only(self.obs, "analytics_6830").identifiers, frozenset())

    def test_source_locates_the_record(self):
        self.assertEqual(only(self.obs, "analytics_2034").source, "json:records[1]")

    def test_fields_keep_the_raw_record(self):
        self.assertEqual(only(self.obs, "account_1084").fields["subject_reference"],
                         "M.ALVAREZ@EXAMPLE.COM")


class FlatRecordsEdgeCaseTests(TempJsonMixin, unittest.TestCase):

    def test_lowercase_result_is_upper_cased(self):
        path = self.write({"exported_at": "2026-01-01T00:00:00Z", "records": [
            {"resource_id": "account_1", "record_type": "USER_PROFILE", "result": "found"}]})
        obs, _, warns = parse_json(path)
        self.assertEqual((obs[0].state, warns), ("FOUND", []))

    def test_unknown_result_word_becomes_unknown_with_a_warning(self):
        path = self.write({"exported_at": "2026-01-01T00:00:00Z", "records": [
            {"resource_id": "account_1", "record_type": "USER_PROFILE", "result": "MAYBE"}]})
        obs, _, warns = parse_json(path)
        self.assertEqual(obs[0].state, "UNKNOWN")
        self.assertEqual(len(warns), 1)
        self.assertIn("evidence_t2", warns[0])

    # Regression (fixed): json_records._record_obs uses `"result" in rec`, so an explicit JSON null result
    # becomes UNKNOWN (with a warning) instead of falling back to the derived state, unlike
    # the shared record_state() helper, which ignores an empty result.
    def test_null_result_falls_back_to_the_derived_state(self):
        path = self.write({"exported_at": "2026-01-01T00:00:00Z", "records": [
            {"resource_id": "analytics_1", "record_type": "ANALYTICS_PROFILE", "result": None,
             "user_id": None, "email": None, "phone": None, "email_hash": None}]})
        obs, _, warns = parse_json(path)
        self.assertEqual((obs[0].state, warns), ("ANONYMIZED", []))

    def test_record_without_resource_id_is_skipped_with_a_warning(self):
        path = self.write({"exported_at": "2026-01-01T00:00:00Z", "records": [
            {"record_type": "USER_PROFILE", "result": "FOUND"},
            {"resource_id": "account_2", "record_type": "USER_PROFILE", "result": "FOUND"}]})
        obs, _, warns = parse_json(path)
        self.assertEqual([o.resource_id for o in obs], ["account_2"])
        self.assertEqual(len(warns), 1)
        self.assertIn("records[0]", warns[0])

    def test_unrecognised_shape_raises_value_error_naming_the_file(self):
        path = self.write({"exported_at": "2026-01-01T00:00:00Z", "items": []})
        with self.assertRaisesRegex(ValueError, "evidence_t2"):
            parse_json(path)

    def test_invalid_json_raises(self):
        path = self.tmp / "broken.json"
        path.write_text('{"records": [', encoding="utf-8")
        with self.assertRaises(ValueError):
            parse_json(path)


class SnapshotBundleTests(unittest.TestCase):
    """`snapshots[]`, each with its own `captured_at`, data store and collection."""

    @classmethod
    def setUpClass(cls):
        cls.obs, cls.cov, cls.warnings = parse_json(FIXTURES / "snapshot_bundle.json",
                                                    captured_at="2026-09-15T12:00:00Z")

    def test_parses_without_warnings(self):
        self.assertEqual(self.warnings, [])

    def test_one_observation_per_record_across_snapshots(self):
        self.assertEqual(sorted(o.resource_id for o in self.obs),
                         ["account_9031", "search_2228", "search_9252", "tombstone_440"])

    def test_snapshot_captured_at_becomes_observed_at(self):
        self.assertEqual(only(self.obs, "search_2228").observed_at, "2026-09-09T12:00:00Z")
        self.assertEqual(only(self.obs, "tombstone_440").observed_at, "2026-09-15T11:00:00Z")

    def test_empty_snapshot_gives_coverage_of_its_collection(self):
        matches = [c for c in self.cov
                   if (c.system_id, c.collection) == ("account_service", "customer_profiles")]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].observed_at, "2026-09-15T12:00:00Z")
        self.assertEqual(matches[0].captured_at, "2026-09-15T12:00:00Z")
        self.assertEqual(matches[0].file_id, "evidence_t2")

    def test_operational_record_history_gives_no_coverage(self):
        self.assertNotIn("operational_record_history", {c.collection for c in self.cov})

    def test_history_records_use_their_record_type_and_result(self):
        account, search = only(self.obs, "account_9031"), only(self.obs, "search_9252")
        self.assertEqual((account.category, account.state), ("USER_PROFILE", "FOUND"))
        self.assertEqual((search.category, search.state), ("SEARCH_INDEX_ENTRY", "NOT_FOUND"))

    def test_history_record_time_is_its_recorded_at(self):
        self.assertEqual(only(self.obs, "account_9031").observed_at, "2026-09-02T09:00:00Z")

    def test_history_lookup_value_is_a_normalised_identifier(self):
        self.assertEqual(only(self.obs, "account_9031").identifiers,
                         frozenset({"s.walker@example.com"}))

    def test_history_system_comes_from_the_resource_not_the_data_store(self):
        self.assertEqual(only(self.obs, "account_9031").system_id, "account_service")

    def test_tombstone_without_result_has_derived_redacted_state(self):
        tomb = only(self.obs, "tombstone_440")
        self.assertEqual((tomb.category, tomb.state), ("ACCOUNT_TOMBSTONE", "REDACTED"))
        self.assertEqual(tomb.identifiers, frozenset({"user_908"}))

    def test_search_document_category_comes_from_the_snapshot_collection(self):
        doc = only(self.obs, "search_2228")
        self.assertEqual((doc.category, doc.state), ("SEARCH_INDEX_ENTRY", "FOUND"))
        self.assertEqual(doc.identifiers,
                         frozenset({"c.oliva@example.com", "user_908", "name:camila oliva"}))


class ServiceResourceExportTests(unittest.TestCase):
    """`service_exports.{system}.{section}[]`, each item with its own `observed_at`."""

    @classmethod
    def setUpClass(cls):
        cls.obs, cls.cov, cls.warnings = parse_json(FIXTURES / "service_resource_export.json",
                                                    captured_at="2026-01-29T11:44:59Z")

    def test_parses_without_warnings(self):
        self.assertEqual(self.warnings, [])

    def test_one_observation_per_item(self):
        self.assertEqual(len(self.obs), 11)

    def test_each_item_keeps_its_own_observed_at(self):
        self.assertEqual(only(self.obs, "account_2424").observed_at, "2026-01-29T09:08:49Z")
        self.assertEqual(only(self.obs, "backup_3382").observed_at, "2026-01-29T11:39:19Z")

    def test_profile_lookup_status_200_is_found(self):
        self.assertEqual(only(self.obs, "account_2424").state, "FOUND")

    def test_profile_lookup_status_404_is_not_found(self):
        self.assertEqual(only(self.obs, "account_2425").state, "NOT_FOUND")

    def test_search_with_hits_is_found(self):
        self.assertEqual(only(self.obs, "search_1091").state, "FOUND")

    def test_search_with_zero_total_hits_is_not_found(self):
        self.assertEqual(only(self.obs, "search_1092").state, "NOT_FOUND")

    def test_analytics_profile_state_follows_identifiers(self):
        self.assertEqual(only(self.obs, "analytics_131").state, "IDENTIFIABLE")
        self.assertEqual(only(self.obs, "analytics_132").state, "ANONYMIZED")

    def test_disabled_connector_is_disabled(self):
        self.assertEqual(only(self.obs, "data_flow_2263").state, "DISABLED")

    def test_restorable_backup_entry_is_active(self):
        self.assertEqual(only(self.obs, "backup_3382").state, "ACTIVE")

    def test_ticket_without_requester_details_is_redacted(self):
        self.assertEqual(only(self.obs, "support_1191").state, "REDACTED")

    def test_sections_map_to_categories(self):
        expected = {"account_2424": "USER_PROFILE", "search_1091": "SEARCH_INDEX_ENTRY",
                    "analytics_131": "ANALYTICS_PROFILE", "data_flow_2263": "DATA_FLOW",
                    "backup_3382": "BACKUP_ARCHIVE", "support_1191": "SUPPORT_MESSAGE",
                    "audit_record_2152": "SECURITY_AUDIT_LOG", "export_9388": "USER_EXPORT"}
        for rid, category in expected.items():
            with self.subTest(resource_id=rid):
                self.assertEqual(only(self.obs, rid).category, category)

    def test_nested_profile_email_is_a_normalised_identifier(self):
        self.assertEqual(only(self.obs, "account_2424").identifiers,
                         frozenset({"a.rao@example.com"}))

    def test_search_query_and_hit_documents_give_identifiers(self):
        self.assertIn("j.lund@example.com", only(self.obs, "search_1091").identifiers)
        self.assertIn("a.rao@example.com", only(self.obs, "search_1092").identifiers)

    def test_source_names_system_section_and_index(self):
        self.assertEqual(only(self.obs, "account_2425").source,
                         "json:service_exports.account_service.profile_lookups[1]")

    def test_system_matches_the_exporting_service(self):
        self.assertEqual(only(self.obs, "export_9388").system_id, "export_service")
        self.assertEqual(only(self.obs, "audit_record_2152").system_id, "security_service")


class CollectionDumpTests(unittest.TestCase):
    """`customer_analytics_report` and `customer_export_jobs` style dumps."""

    @classmethod
    def setUpClass(cls):
        cls.analytics, _, cls.analytics_warnings = parse_json(FIXTURES / "analytics_report_dump.json")
        cls.exports, _, cls.export_warnings = parse_json(FIXTURES / "export_jobs_dump.json")

    def test_parse_without_warnings(self):
        self.assertEqual(self.analytics_warnings + self.export_warnings, [])

    def test_analytics_dump_derives_state_from_identifier_fields(self):
        self.assertEqual(only(self.analytics, "analytics_901").state, "ANONYMIZED")
        self.assertEqual(only(self.analytics, "analytics_824").state, "IDENTIFIABLE")

    def test_dump_collection_gives_the_category(self):
        self.assertEqual({o.category for o in self.analytics}, {"ANALYTICS_PROFILE"})
        self.assertEqual(only(self.exports, "export_001").category, "USER_EXPORT")

    def test_dump_capture_time_is_the_observed_at(self):
        self.assertEqual({o.observed_at for o in self.analytics}, {"2026-09-07T09:45:00Z"})
        self.assertEqual(only(self.exports, "export_001").observed_at, "2026-09-14T15:40:00Z")

    def test_analytics_dump_identifiers_include_the_email_hash(self):
        self.assertIn("d61fb631b92c3bac651984c5cca965640b4a854f1619c2be08e8e2751ed4257b",
                      only(self.analytics, "analytics_824").identifiers)

    def test_export_job_is_found_in_export_service(self):
        job = only(self.exports, "export_001")
        self.assertEqual((job.state, job.system_id), ("FOUND", "export_service"))
        self.assertIn("g.costello@example.com", job.identifiers)


if __name__ == "__main__":
    unittest.main()
