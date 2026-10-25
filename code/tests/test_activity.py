"""Tests for ghostaudit.activity (tools_spec.md T8), on synthetic rows that reuse the real templates."""

from __future__ import annotations

import random
import unittest

from ghostaudit import loaders, paths
from ghostaudit.activity import SIGNALS, ActivityDetails, ActivityLog, parse_details
from ghostaudit.models import Activity

DATASET_OK = (paths.dataset_dir() / "system_activity.csv").is_file()

FLOW = "Connector status check confirmed that data flow {} remains enabled."
RESTORE = "Restore-access check confirmed that archive {} can still be restored."
NON_DELETION = "Privacy intake classified request for {} as non-deletion; no destructive action was authorized."
CATALOG = "Data catalog linked {} ({}) to {} and {}."
ATTACHMENT = ("Attachment catalog confirmed {} ({}) belongs only to {} and {}; "
              "no other subject is linked.")
CONNECTOR_REPOP = "Active connector repopulated downstream resource {}."
RESTORE_REPOP = "Restore job repopulated downstream resource {}."
INDEX_REFRESH = "Customer search-index refresh processed {}."
FINAL_RESULT = "Deletion worker received the final result for {} from {}."


def act(activity_id: str, event_time: str, details: str, *, event_type: str = "UPDATE_RECORD",
        resource_id: str = "", result: str = "SUCCESS", source: str = "svc_a",
        target: str = "svc_b") -> Activity:
    return Activity(activity_id=activity_id, event_time=event_time, event_type=event_type,
                    source_system=source, target_system=target, resource_id=resource_id,
                    reported_result=result, details=details)


def ids(rows) -> list[str]:
    return [a.activity_id for a in rows]


# --- parse_details(): templates, signals, resource ids, claims ---

class ParseDetailsSignalTest(unittest.TestCase):

    CASES = (
        ("flow_enabled", "flow_enabled_check", "CHECK_DATA_FLOW", "data_flow_31", FLOW.format("data_flow_31")),
        ("restore_possible", "restore_access_check", "CHECK_RESTORE_ACCESS", "backup_32",
         RESTORE.format("backup_32")),
        ("repopulated", "connector_repopulated", "SYNC_RECORD", "data_flow_33", CONNECTOR_REPOP.format("search_34")),
        ("repopulated", "restore_repopulated", "RESTORE_BACKUP", "backup_35", RESTORE_REPOP.format("search_36")),
        ("non_deletion", "classified_non_deletion", "CLASSIFY_REQUEST", "account_37",
         NON_DELETION.format("account_37")),
        ("identity_link", "catalog_link", "LINK_RESOURCE_IDENTITY", "account_38",
         CATALOG.format("account_38", "account_38", "mia.k@example.com", "user_38")),
        ("identity_link", "attachment_link", "LINK_RESOURCE_IDENTITY", "object_39",
         ATTACHMENT.format("object_39", "invoice_39.pdf", "mia.k@example.com", "user_39")),
        (None, "index_refresh", "INDEX_RECORD", "search_40", INDEX_REFRESH.format("search_40")),
        (None, "final_result", "REDACT_RECORD", "support_41", FINAL_RESULT.format("support_41", "support_platform")),
    )

    def test_each_template_gives_its_signal(self):
        for signal, template, event_type, rid, text in self.CASES:
            with self.subTest(template=template):
                d = parse_details(act("activity_1", "2026-09-01T00:00:00Z", text, event_type=event_type,
                                      resource_id=rid))
                self.assertIsInstance(d, ActivityDetails)
                self.assertEqual(d.template, template)
                self.assertEqual(d.signal, signal)

    def test_every_signal_is_declared_in_SIGNALS(self):
        produced = {c[0] for c in self.CASES} - {None}
        self.assertEqual(produced, set(SIGNALS))


class ParseDetailsResourceTest(unittest.TestCase):

    def test_restore_job_adds_the_downstream_resource_from_the_text(self):
        d = parse_details(act("activity_3", "2026-08-22T12:58:26Z", RESTORE_REPOP.format("search_4882"),
                              event_type="RESTORE_BACKUP", resource_id="backup_9287"))
        self.assertEqual(d.resource_ids, ("backup_9287", "search_4882"))
        self.assertEqual(d.signal, "repopulated")

    def test_connector_repopulation_adds_the_downstream_resource(self):
        d = parse_details(act("activity_4", "2026-01-12T06:55:20Z", CONNECTOR_REPOP.format("search_2818"),
                              resource_id="data_flow_8165"))
        self.assertEqual(d.resource_ids, ("data_flow_8165", "search_2818"))

    def test_resource_ids_are_deduplicated_with_the_row_resource_first(self):
        d = parse_details(act("activity_5", "2026-09-01T00:00:00Z",
                              CATALOG.format("account_77", "account_77", "mia.k@example.com", "user_77"),
                              resource_id="account_77"))
        self.assertEqual(d.resource_ids, ("account_77",))
        self.assertEqual(d.secondary, "account_77")

    def test_user_ids_emails_and_file_names_are_not_resource_ids(self):
        d = parse_details(act("activity_6", "2026-09-01T00:00:00Z",
                              ATTACHMENT.format("object_19", "delivery_confirmation_19.pdf",
                                                "anna_k_88@example.com", "user_07"),
                              event_type="LINK_RESOURCE_IDENTITY", resource_id="object_19"))
        self.assertEqual(d.resource_ids, ("object_19",))
        self.assertEqual(d.secondary, "delivery_confirmation_19.pdf")
        self.assertEqual(d.identifiers, frozenset({"anna_k_88@example.com", "user_07"}))

    def test_a_non_resource_row_resource_id_is_dropped(self):
        for rid in ("", "user_11", "n/a"):
            with self.subTest(resource_id=rid):
                d = parse_details(act("activity_7", "2026-09-01T00:00:00Z", INDEX_REFRESH.format("search_5"),
                                      resource_id=rid))
                self.assertEqual(d.resource_ids, ("search_5",))

    def test_system_names_in_the_text_are_not_resource_ids(self):
        d = parse_details(act("activity_8", "2026-09-01T00:00:00Z",
                              FINAL_RESULT.format("support_9251", "support_platform"), resource_id="support_9251"))
        self.assertEqual(d.resource_ids, ("support_9251",))


class ParseDetailsClaimTest(unittest.TestCase):

    def test_claimed_result_mirrors_reported_result(self):
        for result in ("SUCCESS", "FAILED", "PARTIAL", ""):
            with self.subTest(result=result):
                d = parse_details(act("activity_1", "2026-09-01T00:00:00Z", FINAL_RESULT.format("account_1", "x"),
                                      resource_id="account_1", result=result))
                self.assertEqual(d.claimed_result, result)

    def test_a_success_claim_does_not_create_a_signal(self):
        d = parse_details(act("activity_1", "2026-09-01T00:00:00Z",
                              "Deletion worker reported the requested operation as completed. "
                              "Target resource: account_1.",
                              event_type="DELETE_RECORD", resource_id="account_1", result="SUCCESS"))
        self.assertEqual(d.template, "operation_completed")
        self.assertIsNone(d.signal)

    def test_unknown_text_gives_unknown_template_without_crashing(self):
        d = parse_details(act("activity_9", "2026-09-01T00:00:00Z",
                              "A brand-new nightly job touched account_77 and wrote mia.k@example.com.",
                              resource_id="search_76", result="PARTIAL"))
        self.assertEqual(d.template, "unknown")
        self.assertIsNone(d.signal)
        self.assertIsNone(d.secondary)
        self.assertEqual(d.resource_ids, ("search_76", "account_77"))
        self.assertEqual(d.claimed_result, "PARTIAL")
        self.assertIn("mia.k@example.com", d.identifiers)

    def test_empty_details_are_unknown(self):
        d = parse_details(act("activity_10", "2026-09-01T00:00:00Z", "", resource_id="account_1"))
        self.assertEqual((d.template, d.signal, d.resource_ids), ("unknown", None, ("account_1",)))

    def test_templates_must_match_the_whole_text(self):
        d = parse_details(act("activity_11", "2026-09-01T00:00:00Z",
                              FLOW.format("data_flow_1") + " Operator note appended.", resource_id="data_flow_1"))
        self.assertEqual(d.template, "unknown")
        self.assertIsNone(d.signal)

    def test_surrounding_whitespace_is_ignored(self):
        d = parse_details(act("activity_12", "2026-09-01T00:00:00Z", "  " + FLOW.format("data_flow_1") + "\n",
                              resource_id="data_flow_1"))
        self.assertEqual(d.signal, "flow_enabled")


# --- ActivityLog ---

class ActivityLogOrderTest(unittest.TestCase):

    def test_rows_given_out_of_time_order_are_sorted(self):
        rows = [act("activity_3", "2026-09-03T00:00:00Z", INDEX_REFRESH.format("search_3"), resource_id="search_3"),
                act("activity_1", "2026-09-01T00:00:00Z", INDEX_REFRESH.format("search_1"), resource_id="search_1"),
                act("activity_2", "2026-09-02T00:00:00Z", INDEX_REFRESH.format("search_2"), resource_id="search_2")]
        self.assertEqual(ids(ActivityLog(rows).rows), ["activity_1", "activity_2", "activity_3"])

    def test_time_ties_are_broken_by_natural_activity_id(self):
        t = "2026-09-01T00:00:00Z"
        rows = [act(a, t, INDEX_REFRESH.format("search_1"), resource_id="search_1")
                for a in ("activity_10", "activity_9", "activity_100")]
        self.assertEqual(ids(ActivityLog(rows).rows), ["activity_9", "activity_10", "activity_100"])

    def test_time_order_beats_id_order(self):
        rows = [act("activity_4000", "2026-02-01T00:00:00Z", INDEX_REFRESH.format("search_1"), resource_id="search_1"),
                act("activity_5", "2026-03-01T00:00:00Z", INDEX_REFRESH.format("search_2"), resource_id="search_2")]
        self.assertEqual(ids(ActivityLog(rows).rows), ["activity_4000", "activity_5"])

    def test_details_can_be_looked_up_by_id_or_row(self):
        row = act("activity_1", "2026-09-01T00:00:00Z", FLOW.format("data_flow_1"), resource_id="data_flow_1")
        log = ActivityLog([row])
        self.assertEqual(log.details("activity_1"), log.details(row))
        self.assertEqual(log.details("activity_1").signal, "flow_enabled")

    def test_unknown_lists_unmatched_rows(self):
        log = ActivityLog([act("activity_1", "2026-09-01T00:00:00Z", "Something new.", resource_id="search_1"),
                           act("activity_2", "2026-09-01T00:00:00Z", INDEX_REFRESH.format("search_2"),
                               resource_id="search_2")])
        self.assertEqual(log.unknown(), ["activity_1"])

    def test_malformed_event_time_is_rejected(self):
        with self.assertRaises(ValueError):
            ActivityLog([act("activity_42", "yesterday", INDEX_REFRESH.format("search_1"), resource_id="search_1")])

    # Regression: a malformed event_time used to raise without naming the activity.
    def test_malformed_event_time_error_names_the_activity(self):
        with self.assertRaises(ValueError) as ctx:
            ActivityLog([act("activity_42", "yesterday", INDEX_REFRESH.format("search_1"), resource_id="search_1")])
        self.assertIn("activity_42", str(ctx.exception))


class ActivityLogQueryTest(unittest.TestCase):

    def setUp(self):
        self.rows = [
            act("activity_1", "2026-09-01T00:00:00Z", INDEX_REFRESH.format("search_1"), resource_id="search_1"),
            act("activity_2", "2026-09-02T00:00:00Z", CONNECTOR_REPOP.format("search_1"), resource_id="data_flow_2",
                event_type="SYNC_RECORD"),
            act("activity_3", "2026-09-03T00:00:00Z", FLOW.format("data_flow_2"), resource_id="data_flow_2",
                event_type="CHECK_DATA_FLOW"),
            act("activity_4", "2026-09-04T00:00:00Z", RESTORE.format("backup_4"), resource_id="backup_4",
                event_type="CHECK_RESTORE_ACCESS"),
            act("activity_5", "2026-09-05T00:00:00Z", FLOW.format("data_flow_5"), resource_id="data_flow_5",
                event_type="CHECK_DATA_FLOW"),
        ]
        shuffled = self.rows[:]
        random.Random(5).shuffle(shuffled)
        self.log = ActivityLog(shuffled)

    def test_for_resources_matches_row_resource_and_ids_in_the_text(self):
        self.assertEqual(ids(self.log.for_resources("search_1")), ["activity_1", "activity_2"])
        self.assertEqual(ids(self.log.for_resources("data_flow_2")), ["activity_2", "activity_3"])

    def test_for_resources_accepts_a_string_or_an_iterable(self):
        self.assertEqual(ids(self.log.for_resources(["backup_4", "data_flow_5"])), ["activity_4", "activity_5"])
        self.assertEqual(ids(self.log.for_resources({"backup_4"})), ["activity_4"])
        self.assertEqual(self.log.for_resources([]), [])
        self.assertEqual(self.log.for_resources("search_404"), [])

    def test_for_resources_returns_each_row_once_in_time_order(self):
        self.assertEqual(ids(self.log.for_resources(["data_flow_2", "search_1"])),
                         ["activity_1", "activity_2", "activity_3"])

    def test_for_resources_until_is_inclusive(self):
        self.assertEqual(ids(self.log.for_resources("search_1", until="2026-09-02T00:00:00Z")),
                         ["activity_1", "activity_2"])
        self.assertEqual(ids(self.log.for_resources("search_1", until="2026-09-01T23:59:59Z")), ["activity_1"])
        self.assertEqual(self.log.for_resources("search_1", until="2026-08-31T00:00:00Z"), [])

    def test_window_is_inclusive_at_both_ends(self):
        self.assertEqual(ids(self.log.window("2026-09-02T00:00:00Z", "2026-09-04T00:00:00Z")),
                         ["activity_2", "activity_3", "activity_4"])

    def test_window_bounds_are_optional(self):
        self.assertEqual(ids(self.log.window(None, "2026-09-02T00:00:00Z")), ["activity_1", "activity_2"])
        self.assertEqual(ids(self.log.window("2026-09-04T00:00:00Z", None)), ["activity_4", "activity_5"])
        self.assertEqual(len(self.log.window()), 5)

    def test_window_is_empty_when_start_is_after_end(self):
        self.assertEqual(self.log.window("2026-09-04T00:00:00Z", "2026-09-02T00:00:00Z"), [])

    def test_window_compares_times_not_strings(self):
        # 2026-09-02T02:00:00+02:00 is exactly 00:00Z on 2 Sep.
        self.assertEqual(ids(self.log.window("2026-09-02T02:00:00+02:00", "2026-09-02T02:00:00+02:00")),
                         ["activity_2"])

    def test_with_signal_filters_rows_and_respects_until(self):
        self.assertEqual(ids(self.log.with_signal("flow_enabled")), ["activity_3", "activity_5"])
        self.assertEqual(ids(self.log.with_signal("flow_enabled", until="2026-09-03T00:00:00Z")), ["activity_3"])
        self.assertEqual(ids(self.log.with_signal("restore_possible")), ["activity_4"])
        self.assertEqual(ids(self.log.with_signal("repopulated")), ["activity_2"])
        self.assertEqual(self.log.with_signal("non_deletion"), [])


class ActivityLogLinksTest(unittest.TestCase):

    def test_links_map_resources_to_lowercased_identity_tokens(self):
        log = ActivityLog([act("activity_1", "2026-09-01T00:00:00Z",
                               CATALOG.format("account_5", "account_5", "Mia.K@Example.COM", "USER_5"),
                               event_type="LINK_RESOURCE_IDENTITY", resource_id="account_5")])
        links = log.links()
        self.assertEqual(list(links), ["account_5"])
        self.assertIn("mia.k@example.com", links["account_5"])
        self.assertIn("user_5", links["account_5"])
        self.assertNotIn("Mia.K@Example.COM", links["account_5"])

    def test_links_respect_until_inclusively(self):
        log = ActivityLog([
            act("activity_1", "2026-09-01T00:00:00Z", CATALOG.format("account_5", "account_5", "a@example.com", "user_5"),
                event_type="LINK_RESOURCE_IDENTITY", resource_id="account_5"),
            act("activity_2", "2026-09-03T00:00:00Z", CATALOG.format("search_6", "search_6", "b@example.com", "user_6"),
                event_type="LINK_RESOURCE_IDENTITY", resource_id="search_6"),
        ])
        self.assertEqual(list(log.links(until="2026-09-01T00:00:00Z")), ["account_5"])
        self.assertEqual(list(log.links(until="2026-08-31T23:59:59Z")), [])
        self.assertEqual(list(log.links()), ["account_5", "search_6"])

    def test_links_ignore_rows_of_other_event_types(self):
        log = ActivityLog([act("activity_1", "2026-09-01T00:00:00Z",
                               CATALOG.format("account_5", "account_5", "a@example.com", "user_5"),
                               event_type="UPDATE_RECORD", resource_id="account_5")])
        self.assertEqual(log.links(), {})

    def test_links_merge_several_rows_for_one_resource(self):
        log = ActivityLog([
            act("activity_1", "2026-09-01T00:00:00Z", CATALOG.format("export_5", "export_5", "a@example.com", "user_5"),
                event_type="LINK_RESOURCE_IDENTITY", resource_id="export_5"),
            act("activity_2", "2026-09-02T00:00:00Z",
                ATTACHMENT.format("export_5", "export_5.csv", "a.old@example.com", "user_5"),
                event_type="LINK_RESOURCE_IDENTITY", resource_id="export_5"),
        ])
        self.assertTrue({"a@example.com", "a.old@example.com", "user_5"} <= log.links()["export_5"])


class ActivityLogNonDeletionTest(unittest.TestCase):

    def test_flags_keep_the_latest_classification_per_resource(self):
        # activity_9 is the higher-numbered id but happened earlier; time order decides.
        log = ActivityLog([
            act("activity_9", "2026-08-01T00:00:00Z", NON_DELETION.format("account_1"),
                event_type="CLASSIFY_REQUEST", resource_id="account_1"),
            act("activity_3", "2026-08-05T00:00:00Z", NON_DELETION.format("account_1"),
                event_type="CLASSIFY_REQUEST", resource_id="account_1"),
            act("activity_4", "2026-08-02T00:00:00Z", NON_DELETION.format("account_2"),
                event_type="CLASSIFY_REQUEST", resource_id="account_2"),
        ])
        self.assertEqual(log.non_deletion_flags(), {"account_1": "activity_3", "account_2": "activity_4"})

    def test_flags_respect_until(self):
        log = ActivityLog([
            act("activity_1", "2026-08-01T00:00:00Z", NON_DELETION.format("account_1"),
                event_type="CLASSIFY_REQUEST", resource_id="account_1"),
            act("activity_2", "2026-08-05T00:00:00Z", NON_DELETION.format("account_1"),
                event_type="CLASSIFY_REQUEST", resource_id="account_1"),
        ])
        self.assertEqual(log.non_deletion_flags(until="2026-08-04T00:00:00Z"), {"account_1": "activity_1"})
        self.assertEqual(log.non_deletion_flags(until="2026-07-01T00:00:00Z"), {})

    def test_only_classify_request_rows_produce_flags(self):
        log = ActivityLog([
            act("activity_1", "2026-08-01T00:00:00Z", NON_DELETION.format("account_1"),
                event_type="UPDATE_RECORD", resource_id="account_1"),
            act("activity_2", "2026-08-01T00:00:00Z", INDEX_REFRESH.format("search_2"),
                event_type="CLASSIFY_REQUEST", resource_id="search_2"),
        ])
        self.assertEqual(log.non_deletion_flags(), {})


# --- Real data (dataset/system_activity.csv) ---

@unittest.skipUnless(DATASET_OK, "dataset/system_activity.csv not present")
class RealActivityTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.rows = loaders.load_activity()
        cls.log = ActivityLog(cls.rows)

    def signal_count(self, signal):
        return sum(1 for a in self.log.rows if self.log.details(a).signal == signal)

    def test_all_4011_rows_are_loaded(self):
        self.assertEqual(len(self.log.rows), 4011)

    def test_every_details_template_is_recognised(self):
        self.assertEqual(self.log.unknown(), [])

    def test_signal_counts_match_the_dataset_analysis(self):
        self.assertEqual(self.signal_count("flow_enabled"), 89)
        self.assertEqual(self.signal_count("restore_possible"), 28)
        self.assertEqual(self.signal_count("non_deletion"), 10)
        self.assertEqual(self.signal_count("identity_link"), 12)
        self.assertEqual(self.signal_count("repopulated"), 32)  # 31 connector + 1 restore job

    def test_activity_4000_links_account_5762_to_haruka(self):
        d = self.log.details("activity_4000")
        self.assertEqual(d.signal, "identity_link")
        self.assertEqual(d.resource_ids, ("account_5762",))
        self.assertTrue({"haruka.s1838@zoho.com", "user_11"} <= d.identifiers)
        self.assertTrue({"haruka.s1838@zoho.com", "user_11"} <= self.log.links()["account_5762"])

    def test_activity_3387_restore_repopulated_search_4882(self):
        d = self.log.details("activity_3387")
        self.assertEqual(d.resource_ids, ("backup_9287", "search_4882"))
        self.assertEqual(d.signal, "repopulated")
        self.assertEqual(d.claimed_result, "SUCCESS")
        self.assertIn("activity_3387", ids(self.log.for_resources("search_4882")))

    def test_links_cover_twelve_resources(self):
        self.assertEqual(len(self.log.links()), 12)

    def test_non_deletion_flags_cover_ten_resources(self):
        flags = self.log.non_deletion_flags()
        self.assertEqual(len(flags), 10)
        self.assertTrue(all(r.startswith("account_") for r in flags))

    def test_rows_are_in_time_order_whatever_the_input_order(self):
        self.assertEqual(ids(ActivityLog(list(reversed(self.rows))).rows), ids(self.log.rows))
        times = [a.event_time for a in self.log.rows]
        self.assertEqual(times, sorted(times))  # all real timestamps share one ISO-Z format


if __name__ == "__main__":
    unittest.main()
