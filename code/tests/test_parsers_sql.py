"""Unit tests for ghostaudit.parsers.sql_snapshot (tools_spec.md T6, SQL row).

Fixtures in fixtures/parsers/ cover the three SQL families found in the dataset:
generic `customer_data_<store>` tables with `record_json`, typed `<collection>_<store>`
tables, and the inventory schema with `snapshot_scope`.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
import warnings
from pathlib import Path

from ghostaudit.models import ManifestEntry
from ghostaudit.parsers import ParseWarning, sql_snapshot

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "parsers"
MANIFEST_CAPTURED_AT = "2026-09-21T10:05:00Z"  # differs from the SQL header on purpose


def entry_for(path: Path, file_id: str = "evidence_t1",
              captured_at: str = MANIFEST_CAPTURED_AT) -> ManifestEntry:
    return ManifestEntry(file_id=file_id, file_path=str(path), origin="test_fixture",
                         captured_at=captured_at)


def parse_sql(path: Path, **entry_kw):
    """Parse one SQL file; return (observations, coverages, warning messages)."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        obs, cov = sql_snapshot.parse(entry_for(path, **entry_kw), path)
    return obs, cov, [str(w.message) for w in caught]


def by_resource(observations, resource_id):
    return [o for o in observations if o.resource_id == resource_id]


def only(observations, resource_id):
    found = by_resource(observations, resource_id)
    if len(found) != 1:
        raise AssertionError(f"expected one observation of {resource_id}, got {len(found)}")
    return found[0]


class GenericReplicaSnapshotTests(unittest.TestCase):
    """`customer_data_primary` + `customer_data_read_replica` with JSON records."""

    @classmethod
    def setUpClass(cls):
        cls.obs, cls.cov, cls.warnings = parse_sql(FIXTURES / "generic_read_replica.sql")
        cls.primary = [o for o in cls.obs if o.store == "primary"]

    def test_parses_without_warnings(self):
        self.assertEqual(self.warnings, [])

    def test_emits_one_observation_per_row_in_every_table(self):
        self.assertEqual(len(self.obs), 12 + 3)

    def test_store_comes_from_the_table_suffix(self):
        stores = {o.source: o.store for o in self.obs}
        self.assertEqual(stores, {"sql:customer_data_primary": "primary",
                                  "sql:customer_data_read_replica": "read_replica"})

    def test_row_only_in_replica_gives_only_a_replica_observation(self):
        drift = by_resource(self.obs, "account_994")
        self.assertEqual([o.store for o in drift], ["read_replica"])

    def test_row_in_both_tables_gives_one_observation_per_store(self):
        stores = sorted(o.store for o in by_resource(self.obs, "analytics_1001"))
        self.assertEqual(stores, ["primary", "read_replica"])

    def test_system_comes_from_the_resource_prefix(self):
        expected = {
            "account_994": "account_service", "analytics_1002": "analytics_platform",
            "tombstone_201": "identity_service", "data_flow_301": "integration_service",
            "backup_401": "backup_service", "audit_record_501": "security_service",
            "support_602": "support_platform", "search_701": "search_service",
        }
        for rid, system in expected.items():
            with self.subTest(resource_id=rid):
                self.assertEqual(by_resource(self.obs, rid)[0].system_id, system)

    def test_category_comes_from_the_collection_name(self):
        expected = {
            "account_994": "USER_PROFILE", "analytics_1002": "ANALYTICS_PROFILE",
            "tombstone_201": "ACCOUNT_TOMBSTONE", "data_flow_301": "DATA_FLOW",
            "backup_401": "BACKUP_ARCHIVE", "audit_record_501": "SECURITY_AUDIT_LOG",
            "support_602": "SUPPORT_MESSAGE", "search_701": "SEARCH_INDEX_ENTRY",
        }
        for rid, category in expected.items():
            with self.subTest(resource_id=rid):
                self.assertEqual(by_resource(self.obs, rid)[0].category, category)

    def test_observed_at_is_the_row_updated_at(self):
        self.assertEqual(only(self.primary, "analytics_1002").observed_at, "2026-09-20T08:15:00Z")
        self.assertEqual(only(self.obs, "account_994").observed_at, "2026-09-19T06:30:00Z")

    def test_captured_at_is_the_manifest_time_not_the_sql_header(self):
        self.assertEqual({o.captured_at for o in self.obs}, {MANIFEST_CAPTURED_AT})

    def test_file_id_comes_from_the_manifest_entry(self):
        self.assertEqual({o.file_id for o in self.obs}, {"evidence_t1"})

    def test_analytics_with_every_identifier_null_is_anonymized(self):
        self.assertEqual(only(self.primary, "analytics_1001").state, "ANONYMIZED")

    def test_analytics_with_an_identifier_is_identifiable(self):
        self.assertEqual(only(self.primary, "analytics_1002").state, "IDENTIFIABLE")

    def test_tombstone_with_only_the_stable_reference_is_redacted(self):
        tomb = only(self.primary, "tombstone_201")
        self.assertEqual(tomb.state, "REDACTED")
        self.assertEqual(tomb.identifiers, frozenset({"user_901"}))

    def test_tombstone_that_still_holds_a_name_is_identifiable(self):
        self.assertEqual(only(self.primary, "tombstone_202").state, "IDENTIFIABLE")

    def test_connector_enabled_flag_maps_to_active_or_disabled(self):
        self.assertEqual(only(self.primary, "data_flow_301").state, "ACTIVE")
        self.assertEqual(only(self.primary, "data_flow_302").state, "DISABLED")

    def test_backup_restore_allowed_flag_maps_to_active_or_blocked(self):
        self.assertEqual(only(self.primary, "backup_401").state, "ACTIVE")
        self.assertEqual(only(self.primary, "backup_402").state, "BLOCKED")

    def test_support_message_with_redaction_marker_is_redacted(self):
        self.assertEqual(only(self.primary, "support_601").state, "REDACTED")

    def test_support_message_with_an_email_in_the_text_is_identifiable(self):
        msg = only(self.primary, "support_602")
        self.assertEqual(msg.state, "IDENTIFIABLE")
        self.assertIn("r.tevita@example.com", msg.identifiers)

    def test_records_without_state_fields_are_found(self):
        self.assertEqual(only(self.obs, "account_994").state, "FOUND")
        self.assertEqual(only(self.primary, "search_701").state, "FOUND")
        self.assertEqual(only(self.primary, "audit_record_501").state, "FOUND")

    def test_uppercase_emails_are_normalised(self):
        self.assertIn("t.quill@example.com", only(self.primary, "analytics_1002").identifiers)
        self.assertIn("k.orlov@example.com", only(self.primary, "backup_401").identifiers)
        for o in self.obs:
            for token in o.identifiers:
                with self.subTest(resource_id=o.resource_id, token=token):
                    self.assertEqual(token, token.lower())

    def test_profile_identifiers_include_phone_reference_and_weak_name(self):
        self.assertEqual(only(self.obs, "account_994").identifiers, frozenset({
            "user_903", "d.kopecka@example.com", "+420601234567", "name:dana kopecka"}))

    def test_nested_search_display_name_becomes_a_weak_name_token(self):
        self.assertIn("name:tomas quill", only(self.primary, "search_701").identifiers)

    def test_integrity_seal_never_becomes_an_identifier(self):
        audit = only(self.primary, "audit_record_501")
        self.assertEqual(audit.identifiers, frozenset({"user_901"}))
        self.assertFalse(any(len(t) == 64 for t in audit.identifiers))

    def test_fields_keep_the_record_and_table_columns_but_not_raw_json(self):
        fields = only(self.obs, "account_994").fields
        self.assertEqual(fields["collection_name"], "customer_profiles")
        self.assertEqual(fields["origin_system"], "account_service")
        self.assertEqual(fields["full_name"], "Dana Kopecka")
        self.assertNotIn("record_json", fields)

    def test_coverage_lists_every_collection_seen_for_each_table(self):
        pairs = {
            ("account_service", "customer_profiles"),
            ("analytics_platform", "customer_analytics_reports"),
            ("backup_service", "backup_catalog"),
            ("identity_service", "deleted_customer_references"),
            ("integration_service", "connector_configurations"),
            ("search_service", "customer_search_documents"),
            ("security_service", "security_audit_events"),
            ("support_platform", "support_conversations"),
        }
        expected = {(store, s, c) for store in ("primary", "read_replica") for s, c in pairs}
        self.assertEqual({(c.store, c.system_id, c.collection) for c in self.cov}, expected)

    def test_primary_coverage_proves_absence_of_a_replica_only_row(self):
        self.assertIn(("primary", "account_service", "customer_profiles"),
                      {(c.store, c.system_id, c.collection) for c in self.cov})

    def test_coverage_uses_header_capture_time_and_manifest_capture(self):
        self.assertEqual({c.observed_at for c in self.cov}, {"2026-09-21T10:00:00Z"})
        self.assertEqual({c.captured_at for c in self.cov}, {MANIFEST_CAPTURED_AT})
        self.assertEqual({c.file_id for c in self.cov}, {"evidence_t1"})


class TypedSnapshotTests(unittest.TestCase):
    """Typed `customer_profiles_<store>` and `search_documents_primary` tables."""

    @classmethod
    def setUpClass(cls):
        cls.obs, cls.cov, cls.warnings = parse_sql(FIXTURES / "typed_failover_and_search.sql")

    def test_parses_without_warnings(self):
        self.assertEqual(self.warnings, [])

    def test_emits_one_observation_per_row(self):
        self.assertEqual(len(self.obs), 4)

    def test_profile_row_only_in_failover_replica_has_only_that_store(self):
        self.assertEqual([o.store for o in by_resource(self.obs, "account_005")], ["failover_replica"])

    def test_profile_row_in_both_tables_has_both_stores(self):
        self.assertEqual(sorted(o.store for o in by_resource(self.obs, "account_8741")),
                         ["failover_replica", "primary"])

    def test_typed_profiles_map_to_account_service_user_profile_found(self):
        o = only(self.obs, "account_005")
        self.assertEqual((o.system_id, o.category, o.state),
                         ("account_service", "USER_PROFILE", "FOUND"))

    def test_typed_profile_identifiers_are_normalised(self):
        self.assertEqual(only(self.obs, "account_005").identifiers, frozenset({
            "user_905", "lior.haddani@example.com", "+212648271500", "name:lior haddani"}))

    def test_typed_search_table_maps_to_search_service(self):
        o = only(self.obs, "search_001")
        self.assertEqual((o.system_id, o.category, o.store, o.state),
                         ("search_service", "SEARCH_INDEX_ENTRY", "primary", "FOUND"))
        self.assertEqual(o.source, "sql:search_documents_primary")

    def test_search_observed_at_is_indexed_at(self):
        self.assertEqual(only(self.obs, "search_001").observed_at, "2026-09-01T22:12:00Z")

    def test_search_fields_keep_the_source_resource_link(self):
        self.assertEqual(only(self.obs, "search_001").fields["source_resource_id"], "account_006")

    def test_one_coverage_per_typed_table(self):
        self.assertEqual({(c.store, c.system_id, c.collection) for c in self.cov}, {
            ("failover_replica", "account_service", "customer_profiles"),
            ("primary", "account_service", "customer_profiles"),
            ("primary", "search_service", "customer_search_documents"),
        })
        self.assertEqual(len(self.cov), 3)

    def test_typed_coverage_uses_the_header_capture_time(self):
        self.assertEqual({c.observed_at for c in self.cov}, {"2026-09-02T23:00:00Z"})

    def test_empty_typed_table_still_gives_coverage(self):
        obs, cov, warns = parse_sql(FIXTURES / "typed_empty_primary.sql")
        self.assertEqual((obs, warns), ([], []))
        self.assertEqual([(c.store, c.system_id, c.collection, c.observed_at) for c in cov],
                         [("primary", "account_service", "customer_profiles", "2026-09-03T05:00:00Z")])


class InventorySnapshotTests(unittest.TestCase):
    """Inventory schema: `snapshot_scope` plus 8 typed tables, no header comment."""

    @classmethod
    def setUpClass(cls):
        cls.obs, cls.cov, cls.warnings = parse_sql(FIXTURES / "inventory.sql")

    def test_parses_without_warnings(self):
        self.assertEqual(self.warnings, [])

    def test_emits_one_observation_per_row(self):
        self.assertEqual(len(self.obs), 9)

    def test_every_row_has_the_inventory_store(self):
        self.assertEqual({o.store for o in self.obs}, {"inventory"})

    def test_source_names_the_table(self):
        self.assertEqual(only(self.obs, "data_flow_006").source, "sql:connector_subscriptions")

    def test_coverage_is_one_per_snapshot_scope_system(self):
        self.assertEqual(
            sorted((c.system_id, c.collection, c.store, c.observed_at) for c in self.cov),
            [("account_service", "customer_profiles", "inventory", "2026-02-13T07:43:27Z"),
             ("integration_service", "connector_subscriptions", "inventory", "2026-02-13T07:40:00Z")])

    def test_systems_missing_from_scope_get_no_coverage(self):
        covered = {c.system_id for c in self.cov}
        for system in ("search_service", "analytics_platform", "support_platform",
                       "backup_service", "security_service", "export_service"):
            with self.subTest(system=system):
                self.assertNotIn(system, covered)

    def test_integer_enabled_flag_maps_to_active_or_disabled(self):
        self.assertEqual(only(self.obs, "data_flow_006").state, "ACTIVE")
        self.assertEqual(only(self.obs, "data_flow_007").state, "DISABLED")

    def test_integer_restore_allowed_zero_is_blocked(self):
        self.assertEqual(only(self.obs, "backup_5061").state, "BLOCKED")

    def test_analytics_profile_state_follows_identifier_columns(self):
        self.assertEqual(only(self.obs, "analytics_5443").state, "ANONYMIZED")
        self.assertEqual(only(self.obs, "analytics_5444").state, "IDENTIFIABLE")

    def test_support_record_state_follows_requester_columns(self):
        self.assertEqual(only(self.obs, "support_6391").state, "REDACTED")
        self.assertEqual(only(self.obs, "support_6392").state, "IDENTIFIABLE")

    def test_table_names_map_to_categories(self):
        expected = {"account_3626": "USER_PROFILE", "analytics_5443": "ANALYTICS_PROFILE",
                    "support_6391": "SUPPORT_MESSAGE", "data_flow_006": "DATA_FLOW",
                    "backup_5061": "BACKUP_ARCHIVE", "audit_record_1112": "SECURITY_AUDIT_LOG"}
        for rid, category in expected.items():
            with self.subTest(resource_id=rid):
                self.assertEqual(only(self.obs, rid).category, category)

    def test_uppercase_inventory_email_is_normalised(self):
        self.assertEqual(only(self.obs, "account_3626").identifiers,
                         frozenset({"o.erling10@example.com"}))

    def test_observed_at_is_the_row_updated_at(self):
        self.assertEqual(only(self.obs, "account_3626").observed_at, "2026-02-13T06:34:26Z")

    def test_empty_inventory_gives_scope_coverage_and_no_observations(self):
        obs, cov, warns = parse_sql(FIXTURES / "inventory_empty.sql")
        self.assertEqual((obs, warns), ([], []))
        self.assertEqual([(c.system_id, c.collection, c.store, c.observed_at) for c in cov],
                         [("account_service", "customer_profiles", "inventory", "2026-09-05T07:18:01Z")])


class MalformedSqlTests(unittest.TestCase):
    """Bad input is reported, never skipped silently (spec §1, Errors)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, text: str) -> Path:
        path = self.tmp / "snapshot.sql"
        path.write_text(text, encoding="utf-8")
        return path

    def test_unrecognised_table_is_skipped_with_a_warning(self):
        path = self.write("-- Captured at: 2026-09-01T00:00:00Z\n"
                          "CREATE TABLE audit_trail (resource_id TEXT, note TEXT);\n"
                          "INSERT INTO audit_trail VALUES ('account_1', 'x');\n")
        obs, cov, warns = parse_sql(path)
        self.assertEqual((obs, cov), ([], []))
        self.assertEqual(len(warns), 1)
        self.assertIn("evidence_t1", warns[0])
        self.assertIn("audit_trail", warns[0])

    def test_row_without_resource_id_is_skipped_with_a_warning(self):
        path = self.write(
            "-- Captured at: 2026-09-01T00:00:00Z\n"
            "CREATE TABLE customer_profiles_primary (resource_id TEXT, user_id TEXT, full_name TEXT,"
            " email TEXT, phone TEXT, account_status TEXT, updated_at TEXT);\n"
            "INSERT INTO customer_profiles_primary VALUES (NULL, 'user_1', 'A B', 'a@example.com',"
            " '+1 212 555 0100', 'ACTIVE', '2026-09-01T00:00:00Z');\n")
        obs, cov, warns = parse_sql(path)
        self.assertEqual(obs, [])
        self.assertEqual(len(cov), 1)
        self.assertEqual(len(warns), 1)
        self.assertIn("without resource_id", warns[0])

    def test_invalid_sql_raises(self):
        path = self.write("CREATE TABLE customer_data_primary (resource_id TEXT;\n")
        with self.assertRaises(sqlite3.Error):
            parse_sql(path)


if __name__ == "__main__":
    unittest.main()
