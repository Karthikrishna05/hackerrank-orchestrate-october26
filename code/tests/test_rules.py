"""Tests for ghostaudit.rules (tools_spec T5: policy table and category mapping)."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from ghostaudit.models import Rule
from ghostaudit.rules import (
    CATEGORY_BY_COLLECTION,
    DATA_FLOW,
    RULE_CATEGORIES,
    PolicyTable,
    category_for,
)

DATASET = Path(__file__).resolve().parents[2] / "dataset"

SPEC_COLLECTIONS = {
    "customer_profiles": "USER_PROFILE",
    "customer_search_documents": "SEARCH_INDEX_ENTRY",
    "customer_analytics_reports": "ANALYTICS_PROFILE",
    "customer_analytics_profiles": "ANALYTICS_PROFILE",
    "support_conversations": "SUPPORT_MESSAGE",
    "support_records": "SUPPORT_MESSAGE",
    "ticket_attachments": "SUPPORT_ATTACHMENT",
    "customer_export_jobs": "USER_EXPORT",
    "customer_exports": "USER_EXPORT",
    "connector_configurations": "DATA_FLOW",
    "connector_subscriptions": "DATA_FLOW",
    "backup_catalog": "BACKUP_ARCHIVE",
    "backup_restore_entries": "BACKUP_ARCHIVE",
    "security_audit_events": "SECURITY_AUDIT_LOG",
    "security_audit_records": "SECURITY_AUDIT_LOG",
    "retained_order_records": "LEGAL_HOLD_RECORD",
    "deleted_customer_references": "ACCOUNT_TOMBSTONE",
}

SAMPLE_RULES = [
    Rule("rule_01", "USER_PROFILE", "Always", "DELETE"),
    Rule("rule_05", "SUPPORT_MESSAGE", "Shared context must be retained", "REDACT"),
    Rule("rule_07", "ANALYTICS_PROFILE", "Record is still attributable to a user", "ANONYMIZE"),
    Rule("rule_12", "SECURITY_AUDIT_LOG", "Within retention period", "RETAIN"),
    Rule("rule_15", "ACCOUNT_TOMBSTONE", "Only stable user_id is needed", "REDACT"),
]


class TestPolicyTable(unittest.TestCase):
    def setUp(self):
        self.table = PolicyTable(SAMPLE_RULES)

    def test_action_returns_the_required_action_per_category(self):
        expected = {"USER_PROFILE": "DELETE", "SUPPORT_MESSAGE": "REDACT", "ANALYTICS_PROFILE": "ANONYMIZE",
                    "SECURITY_AUDIT_LOG": "RETAIN", "ACCOUNT_TOMBSTONE": "REDACT"}
        for category, action in expected.items():
            with self.subTest(category=category):
                self.assertEqual(self.table.action(category), action)

    def test_rule_returns_the_whole_rule(self):
        self.assertEqual(self.table.rule("SUPPORT_MESSAGE"), SAMPLE_RULES[1])

    def test_unknown_category_has_no_action(self):
        self.assertIsNone(self.table.action("CACHE_ENTRY"))
        self.assertIsNone(self.table.rule("CACHE_ENTRY"))

    def test_none_category_has_no_action(self):
        self.assertIsNone(self.table.action(None))

    def test_data_flow_is_not_a_policy_category(self):
        self.assertIsNone(self.table.action(DATA_FLOW))

    def test_duplicate_category_raises_value_error(self):
        with self.assertRaises(ValueError):
            PolicyTable(SAMPLE_RULES + [Rule("rule_99", "USER_PROFILE", "Always", "RETAIN")])

    def test_empty_rule_list_gives_empty_table(self):
        self.assertIsNone(PolicyTable([]).action("USER_PROFILE"))


class TestCategoryTables(unittest.TestCase):
    def test_category_by_collection_matches_the_spec_exactly(self):
        self.assertEqual(CATEGORY_BY_COLLECTION, SPEC_COLLECTIONS)

    def test_rule_categories_has_fifteen_entries_and_excludes_data_flow(self):
        self.assertEqual(len(RULE_CATEGORIES), 15)
        self.assertNotIn(DATA_FLOW, RULE_CATEGORIES)
        self.assertEqual(DATA_FLOW, "DATA_FLOW")

    def test_every_mapped_category_is_a_rule_category_or_data_flow(self):
        for collection, category in CATEGORY_BY_COLLECTION.items():
            with self.subTest(collection=collection):
                self.assertTrue(category in RULE_CATEGORIES or category == DATA_FLOW)


class TestCategoryFor(unittest.TestCase):
    def test_category_from_collection(self):
        for collection, category in SPEC_COLLECTIONS.items():
            with self.subTest(collection=collection):
                self.assertEqual(category_for(collection), category)

    def test_explicit_record_type_wins_over_collection(self):
        self.assertEqual(category_for("support_conversations", "SUPPORT_ATTACHMENT"), "SUPPORT_ATTACHMENT")
        self.assertEqual(category_for(collection="customer_profiles", record_type="ACCOUNT_TOMBSTONE"),
                         "ACCOUNT_TOMBSTONE")

    def test_record_type_is_matched_case_and_whitespace_insensitively(self):
        self.assertEqual(category_for(record_type="  search_index_entry "), "SEARCH_INDEX_ENTRY")

    def test_record_type_data_flow_is_recognised(self):
        self.assertEqual(category_for(record_type="DATA_FLOW"), DATA_FLOW)

    def test_unknown_record_type_falls_back_to_collection(self):
        self.assertEqual(category_for("ticket_attachments", "attachment_blob"), "SUPPORT_ATTACHMENT")

    def test_unknown_collection_returns_none_not_a_guess(self):
        for collection in ("customer_widgets", "operational_record_history", "customer_profiles_primary",
                           "Customer_Profiles"):
            with self.subTest(collection=collection):
                self.assertIsNone(category_for(collection))

    def test_no_collection_and_no_record_type_returns_none(self):
        self.assertIsNone(category_for())
        self.assertIsNone(category_for(None, None))
        self.assertIsNone(category_for("", ""))

    def test_unknown_record_type_without_collection_returns_none(self):
        self.assertIsNone(category_for(record_type="MYSTERY"))


@unittest.skipUnless(DATASET.is_dir(), "dataset not available")
class TestRulesOnRealDataset(unittest.TestCase):
    """Read-only acceptance checks from tools_spec T5."""

    @classmethod
    def setUpClass(cls):
        from ghostaudit import loaders
        cls.rules = loaders.load_rules()
        cls.table = PolicyTable(cls.rules)

    def test_all_fifteen_rules_load_into_the_table(self):
        self.assertEqual(len(self.rules), 15)
        for rule in self.rules:
            with self.subTest(category=rule.data_category):
                self.assertEqual(self.table.action(rule.data_category), rule.required_action)

    def test_rule_categories_match_the_rule_file(self):
        self.assertEqual({r.data_category for r in self.rules}, set(RULE_CATEGORIES))

    def test_every_action_is_one_of_the_four_policy_actions(self):
        self.assertLessEqual({r.required_action for r in self.rules}, {"DELETE", "REDACT", "ANONYMIZE", "RETAIN"})

    def test_key_policy_actions(self):
        self.assertEqual(self.table.action("USER_PROFILE"), "DELETE")
        self.assertEqual(self.table.action("SUPPORT_MESSAGE"), "REDACT")
        self.assertEqual(self.table.action("ANALYTICS_PROFILE"), "ANONYMIZE")
        self.assertEqual(self.table.action("SECURITY_AUDIT_LOG"), "RETAIN")
        self.assertEqual(self.table.action("ACCOUNT_TOMBSTONE"), "REDACT")
        self.assertIsNone(self.table.action(DATA_FLOW))

    def test_every_json_collection_in_the_evidence_maps_or_carries_record_types(self):
        collections: dict[str, list] = {}

        def walk(node):
            if isinstance(node, dict):
                if isinstance(node.get("collection"), str):
                    collections.setdefault(node["collection"], []).append(node)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        for path in sorted((DATASET / "evidence").rglob("*.json")):
            walk(json.loads(path.read_text(encoding="utf-8")))
        self.assertTrue(collections)
        for name, nodes in collections.items():
            with self.subTest(collection=name):
                if category_for(name) is not None:
                    continue
                # A mixed collection is acceptable only when each record names its own category.
                records = [r for n in nodes for r in n.get("records", [])]
                self.assertTrue(records, f"{name} has no category and no records")
                for record in records:
                    self.assertIsNotNone(category_for(name, record.get("record_type")))

    def test_every_typed_sql_inventory_table_maps_to_a_category(self):
        tables = set()
        for path in sorted((DATASET / "evidence").rglob("*.sql")):
            tables |= set(re.findall(r"CREATE TABLE\s+(\w+)", path.read_text(encoding="utf-8"), re.I))
        # Generic per-store tables (customer_data_<store>, <collection>_<store>) and the
        # snapshot_scope table are the parser's job, not a collection mapping.
        typed = {t for t in tables if t != "snapshot_scope" and not t.startswith("customer_data_")
                 and not t.endswith(("_primary", "_replica"))}
        self.assertTrue(typed)
        for table in sorted(typed):
            with self.subTest(table=table):
                self.assertIsNotNone(category_for(table))


if __name__ == "__main__":
    unittest.main()
