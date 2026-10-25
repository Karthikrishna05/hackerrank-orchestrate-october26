"""Independent tests for ghostaudit.engine (engine_plan.md §2 and owner decisions 5-15).

Most tests inject a small in-memory Context (IdentityIndex, EvidenceIndex, ActivityLog, PolicyTable)
built from synthetic observations, coverages and activity rows; no file is read. A real-data class
runs the engine on the participant dataset once, and a guard checks that product code holds no
hard-coded case, evidence or activity ids.
"""

from __future__ import annotations

import ast
import csv
import random
import re
import unittest
from dataclasses import replace
from pathlib import Path

from ghostaudit import engine, loaders, paths
from ghostaudit.activity import ActivityLog
from ghostaudit.engine import CaseResult, Context, audit_case, note_kind
from ghostaudit.evidence_index import EvidenceIndex
from ghostaudit.identity import IdentityIndex
from ghostaudit.models import (
    Activity,
    AuditChange,
    Coverage,
    IntentResult,
    Observation,
    Request,
    Rule,
    Step,
    UserIdentity,
)
from ghostaudit.normalize import SYSTEM_BY_PREFIX, email_hash, natural_key, system_for
from ghostaudit.planner import MATRIX
from ghostaudit.rules import DATA_FLOW, RULE_CATEGORIES, PolicyTable
from ghostaudit.steps import validate

# ------------------------------------------------------------------ synthetic world

REQUESTED = "2026-09-01T08:00:00Z"
REPORTED = "2026-09-03T02:00:00Z"
AUDIT = "2026-09-04T12:00:00Z"
BEFORE = "2026-08-20T10:00:00Z"     # before requested_at
DURING = "2026-09-02T10:00:00Z"     # inside [requested_at, audit_at]
LATE = "2026-09-10T10:00:00Z"       # after audit_at

EMAIL = "mira.calloway@example.test"
OLD_EMAIL = "mira.old@example.test"
PHONE = "+15550100901"
USER = "user_901"
OTHER_EMAIL = "t.brennick@example.test"
OTHER_USER = "user_902"

SUBJECT = UserIdentity(USER, "Mira Calloway", EMAIL, "+1 555 010 0901", email_hash(EMAIL), "2026-07-01T00:00:00Z")
OTHER = UserIdentity(OTHER_USER, "Tomas Brennick", OTHER_EMAIL, "+1 555 010 0902", email_hash(OTHER_EMAIL),
                     "2026-07-01T00:00:00Z")
AUDIT_LOG = (AuditChange("audit_9001", USER, "2026-07-01T00:00:00Z", "email", OLD_EMAIL, EMAIL),)

# The participant policy table (deletion_rules.csv), as fixture data.
RULES = tuple(Rule(f"rule_{i:02d}", cat, "fixture", action) for i, (cat, action) in enumerate((
    ("USER_PROFILE", "DELETE"), ("SECONDARY_IDENTIFIER", "DELETE"), ("SEARCH_INDEX_ENTRY", "DELETE"),
    ("CACHE_ENTRY", "DELETE"), ("SUPPORT_MESSAGE", "REDACT"), ("SUPPORT_ATTACHMENT", "DELETE"),
    ("ANALYTICS_PROFILE", "ANONYMIZE"), ("PRODUCT_TELEMETRY", "ANONYMIZE"), ("USER_EXPORT", "DELETE"),
    ("MARKETING_PROFILE", "DELETE"), ("EMAIL_DELIVERY_LOG", "REDACT"), ("SECURITY_AUDIT_LOG", "RETAIN"),
    ("LEGAL_HOLD_RECORD", "RETAIN"), ("BACKUP_ARCHIVE", "RETAIN"), ("ACCOUNT_TOMBSTONE", "REDACT"),
), 1))

DELETE_TEXT = "Please delete my account and all personal data linked to it."
NON_DELETION_TEXT = "Please stop the promotional emails. I still need the account, so I am not asking for deletion."

FOUND_IDS = frozenset({EMAIL, USER, "name:mira calloway"})


def obs(rid: str, state: str, *, file_id: str = "evidence_901", observed: str = DURING,
        captured: str | None = None, category: str | None = None, ids=(), store: str | None = None,
        src: str | None = None, system: str | None = None, **fields) -> Observation:
    """One synthetic observation; `captured` defaults to `observed`; `src` is Observation.source."""
    return Observation(file_id, captured or observed, observed, system or system_for(rid) or "", rid, state,
                       category, frozenset(ids), store, src or f"txt:{rid}:{state}",
                       {"resource_id": rid, **fields})


def doc(file_id: str, observed: str, category: str, system: str, *, ids=(), captured: str | None = None,
        **fields) -> Observation:
    """A document-level observation (no resource id), shaped like the PDF parser's: state FOUND."""
    return Observation(file_id, captured or observed, observed, system, "", "FOUND", category, frozenset(ids), None,
                       f"pdf:{file_id}", dict(fields))


def link(aid: str, rid: str, when: str, *, email: str = EMAIL, user: str = USER, secondary: str | None = None,
         template: str = "catalog", target: str | None = None) -> Activity:
    """A LINK_RESOURCE_IDENTITY row in one of the two catalog templates."""
    sec = secondary or rid
    if template == "catalog":
        details = f"Data catalog linked {rid} ({sec}) to {email} and {user}."
    else:
        details = (f"Attachment catalog confirmed {rid} ({sec}) belongs only to {email} and {user}; "
                   "no other subject is linked.")
    return Activity(aid, when, "LINK_RESOURCE_IDENTITY", "data_catalog", target or system_for(rid) or "",
                    rid, "SUCCESS", details)


_ATTEMPT_TEXT = {
    "FAILED": "Deletion worker stopped after the target system returned an error. Target resource: {}.",
    "PARTIAL": "Deletion worker completed some internal work but could not confirm the final state. "
               "Target resource: {}.",
    "SUCCESS": "Deletion worker reported the requested operation as completed. Target resource: {}.",
}


def attempt(aid: str, rid: str, when: str, result: str = "FAILED", *, column: str | None = None) -> Activity:
    """A deletion-worker row naming `rid` in its text; `column` overrides the resource_id column."""
    col = column or rid
    return Activity(aid, when, "DELETE_RECORD", "deletion_worker", system_for(col) or "", col, result,
                    _ATTEMPT_TEXT[result].format(rid))


def classify_row(aid: str, rid: str, when: str) -> Activity:
    return Activity(aid, when, "CLASSIFY_REQUEST", "privacy_intake", "privacy_case_management", rid, "SUCCESS",
                    f"Privacy intake classified request for {rid} as non-deletion; "
                    "no destructive action was authorized.")


def make_ctx(observations=(), coverages=(), activity=(), *, identities=(SUBJECT, OTHER),
             audit_log=AUDIT_LOG, intent_mode: str = "rules", llm_lookup=None) -> Context:
    return Context(IdentityIndex(identities, audit_log), EvidenceIndex(list(observations), list(coverages)),
                   ActivityLog(list(activity)), PolicyTable(RULES), intent_mode, llm_lookup)


def req(case_id: str = "case_9001", *, email: str = EMAIL, text: str = DELETE_TEXT,
        requested: str = REQUESTED, audit: str = AUDIT) -> Request:
    return Request(case_id, email, requested, "COMPLETE", REPORTED, audit, text)


def tuples(steps) -> list[tuple]:
    """Steps without step_no: (phase, system_id, resource_id, operation, state, evidence_id)."""
    return [(s.phase, s.system_id, s.resource_id, s.operation, s.state, s.evidence_id) for s in steps]


def for_rid(result: CaseResult, rid: str) -> list[tuple]:
    return [t for t in tuples(result.steps) if t[2] == rid]


def kinds(result: CaseResult) -> set[str]:
    return {note_kind(n) for n in result.notes}


# A citable companion record that keeps a case alive when the record under test is dropped.
COMPANION = obs("search_9990", "FOUND", file_id="evidence_999", category="SEARCH_INDEX_ENTRY", ids={EMAIL})
COMPANION_STEPS = [
    ("FINDING", "search_service", "search_9990", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_999"),
    ("REMEDIATION", "search_service", "search_9990", "DELETE_RECORD", "ABSENT", ""),
    ("VERIFICATION", "search_service", "search_9990", "VERIFY_RECORD_ABSENT", "ABSENT", ""),
]
# A compliant companion (disabled flow) that keeps a COMPLETE case COMPLETE.
COMPLIANT_COMPANION = obs("data_flow_9991", "DISABLED", file_id="evidence_999", category=DATA_FLOW, ids={USER})


class EngineCase(unittest.TestCase):
    """Shared assertions."""

    def assertValidRow(self, result: CaseResult):
        self.assertEqual(validate(result.status, result.steps), [], tuples(result.steps))
        self.assertEqual([s.step_no for s in result.steps], list(range(1, len(result.steps) + 1)))


# ------------------------------------------------------------------ base scenarios

INCOMPLETE_WORLD = [
    obs("account_9101", "FOUND", category="USER_PROFILE", ids=FOUND_IDS, store="primary"),
    obs("search_9102", "FOUND", category="SEARCH_INDEX_ENTRY", ids={EMAIL}),
    obs("data_flow_9103", "ACTIVE", file_id="evidence_902", category=DATA_FLOW, ids={USER}),
    obs("audit_record_9104", "FOUND", file_id="evidence_902", category="SECURITY_AUDIT_LOG", ids={USER},
        retention_until="2027-03-01T00:00:00Z"),
    obs("support_9105", "IDENTIFIABLE", file_id="evidence_902", category="SUPPORT_MESSAGE", ids={EMAIL}),
    # Linked only through the hash of the subject's previous email (E2).
    obs("analytics_9106", "IDENTIFIABLE", file_id="evidence_903", category="ANALYTICS_PROFILE",
        ids={email_hash(OLD_EMAIL)}),
    obs("backup_9107", "ACTIVE", file_id="evidence_903", category="BACKUP_ARCHIVE", ids={USER}),
    # Decoy: another person's record must never appear.
    obs("account_9199", "FOUND", file_id="evidence_903", category="USER_PROFILE",
        ids={OTHER_EMAIL, OTHER_USER, "name:mira calloway"}),
]

INCOMPLETE_EXPECTED = [
    ("FINDING", "account_service", "account_9101", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_901"),
    ("FINDING", "search_service", "search_9102", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_901"),
    ("FINDING", "analytics_platform", "analytics_9106", "REPORT_EXPOSED_PII", "PRESENT", "evidence_903"),
    ("FINDING", "support_platform", "support_9105", "REPORT_EXPOSED_PII", "PRESENT", "evidence_902"),
    ("FINDING", "integration_service", "data_flow_9103", "REPORT_ACTIVE_DATA_FLOW", "ACTIVE", "evidence_902"),
    ("FINDING", "backup_service", "backup_9107", "REPORT_RESTORE_RISK", "ACTIVE", "evidence_903"),
    ("REMEDIATION", "integration_service", "data_flow_9103", "DISABLE_DATA_FLOW", "DISABLED", ""),
    ("REMEDIATION", "backup_service", "backup_9107", "BLOCK_RESTORE", "BLOCKED", ""),
    ("REMEDIATION", "account_service", "account_9101", "DELETE_RECORD", "ABSENT", ""),
    ("REMEDIATION", "search_service", "search_9102", "DELETE_RECORD", "ABSENT", ""),
    ("REMEDIATION", "support_platform", "support_9105", "REDACT_RECORD", "REDACTED", ""),
    ("REMEDIATION", "analytics_platform", "analytics_9106", "ANONYMIZE_RECORD", "ANONYMIZED", ""),
    ("VERIFICATION", "account_service", "account_9101", "VERIFY_RECORD_ABSENT", "ABSENT", ""),
    ("VERIFICATION", "search_service", "search_9102", "VERIFY_RECORD_ABSENT", "ABSENT", ""),
    ("VERIFICATION", "security_service", "audit_record_9104", "VERIFY_RECORD_PRESENT", "PRESENT", ""),
    ("VERIFICATION", "support_platform", "support_9105", "VERIFY_RECORD_REDACTED", "REDACTED", ""),
    ("VERIFICATION", "analytics_platform", "analytics_9106", "VERIFY_RECORD_ANONYMIZED", "ANONYMIZED", ""),
    ("VERIFICATION", "integration_service", "data_flow_9103", "VERIFY_DATA_FLOW_DISABLED", "DISABLED", ""),
    ("VERIFICATION", "backup_service", "backup_9107", "VERIFY_RESTORE_BLOCKED", "BLOCKED", ""),
]

COMPLETE_WORLD = [
    # Seen with the subject's email before the request, then gone in a file captured by audit_at.
    obs("account_9201", "FOUND", file_id="evidence_911", observed=BEFORE, category="USER_PROFILE", ids=FOUND_IDS),
    obs("account_9201", "NOT_FOUND", file_id="evidence_912", category="USER_PROFILE"),
    obs("support_9202", "REDACTED", file_id="evidence_912", category="SUPPORT_MESSAGE", ids={USER}),
    # No identifier: reached through a field reference to the subject's account (one hop).
    obs("analytics_9203", "ANONYMIZED", file_id="evidence_912", category="ANALYTICS_PROFILE",
        source_account="account_9201"),
    obs("data_flow_9204", "DISABLED", file_id="evidence_913", category=DATA_FLOW, ids={USER}),
    obs("backup_9205", "BLOCKED", file_id="evidence_913", category="BACKUP_ARCHIVE", ids={USER}),
    obs("audit_record_9206", "FOUND", file_id="evidence_913", category="SECURITY_AUDIT_LOG", ids={USER},
        retention_until="2027-06-30T00:00:00Z"),
    # A tombstone keeping only the stable user_id.
    obs("tombstone_9207", "FOUND", file_id="evidence_913", category="ACCOUNT_TOMBSTONE", ids={USER}),
]

COMPLETE_EXPECTED = [
    ("VERIFICATION", "account_service", "account_9201", "VERIFY_RECORD_ABSENT", "ABSENT", "evidence_912"),
    ("VERIFICATION", "security_service", "audit_record_9206", "VERIFY_RECORD_PRESENT", "PRESENT", "evidence_913"),
    ("VERIFICATION", "identity_service", "tombstone_9207", "VERIFY_RECORD_REDACTED", "REDACTED", "evidence_913"),
    ("VERIFICATION", "support_platform", "support_9202", "VERIFY_RECORD_REDACTED", "REDACTED", "evidence_912"),
    ("VERIFICATION", "analytics_platform", "analytics_9203", "VERIFY_RECORD_ANONYMIZED", "ANONYMIZED",
     "evidence_912"),
    ("VERIFICATION", "integration_service", "data_flow_9204", "VERIFY_DATA_FLOW_DISABLED", "DISABLED",
     "evidence_913"),
    ("VERIFICATION", "backup_service", "backup_9205", "VERIFY_RESTORE_BLOCKED", "BLOCKED", "evidence_913"),
]


class IncompleteCaseTest(EngineCase):

    def setUp(self):
        self.result = audit_case(req(), make_ctx(INCOMPLETE_WORLD))

    def test_status_and_exact_canonical_steps(self):
        self.assertEqual(self.result.status, "INCOMPLETE")
        self.assertEqual(tuples(self.result.steps), INCOMPLETE_EXPECTED)
        self.assertValidRow(self.result)

    def test_findings_cite_and_future_steps_are_blank(self):
        for s in self.result.steps:
            if s.phase == "FINDING":
                self.assertRegex(s.evidence_id, r"^(?:evidence|activity|audit)_\d+$")
            else:
                self.assertEqual(s.evidence_id, "", s)

    def test_flows_and_restores_are_stopped_before_records_change(self):
        ops = [s.operation for s in self.result.steps if s.phase == "REMEDIATION"]
        last_guard = max(i for i, op in enumerate(ops) if op in ("DISABLE_DATA_FLOW", "BLOCK_RESTORE"))
        first_change = min(i for i, op in enumerate(ops)
                           if op in ("DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD"))
        self.assertLess(last_guard, first_change)

    def test_every_remediated_record_has_a_verification(self):
        fixed = {s.resource_id for s in self.result.steps if s.phase == "REMEDIATION"}
        verified = {s.resource_id for s in self.result.steps if s.phase == "VERIFICATION"}
        self.assertLessEqual(fixed, verified)

    def test_decoy_with_shared_name_is_ignored(self):
        self.assertEqual(for_rid(self.result, "account_9199"), [])

    def test_case_id_intent_and_row(self):
        self.assertEqual(self.result.case_id, "case_9001")
        self.assertIsInstance(self.result.intent, IntentResult)
        self.assertTrue(self.result.intent.authorizes_deletion)
        self.assertEqual(self.result.row(), ("case_9001", "INCOMPLETE", self.result.steps))

    def test_historical_email_resolves_to_the_same_answer(self):
        by_old = audit_case(req(email=OLD_EMAIL), make_ctx(INCOMPLETE_WORLD))
        self.assertEqual(by_old.row(), self.result.row())

    def test_request_email_case_does_not_matter(self):
        upper = audit_case(req(email=EMAIL.upper()), make_ctx(INCOMPLETE_WORLD))
        self.assertEqual(upper.row(), self.result.row())


class CompleteCaseTest(EngineCase):

    def setUp(self):
        self.result = audit_case(req(), make_ctx(COMPLETE_WORLD))

    def test_status_and_exact_verification_steps(self):
        self.assertEqual(self.result.status, "COMPLETE")
        self.assertEqual(tuples(self.result.steps), COMPLETE_EXPECTED)
        self.assertValidRow(self.result)

    def test_only_cited_verification_steps(self):
        self.assertTrue(self.result.steps)
        for s in self.result.steps:
            self.assertEqual(s.phase, "VERIFICATION")
            self.assertRegex(s.evidence_id, r"^evidence_\d+$")

    def test_absence_cited_via_coverage_when_no_observing_file_is_citable(self):
        # The record's only observations sit in files captured after audit_at; a full listing of
        # its collection, captured by audit_at, proves the absence.
        world = [
            obs("account_9301", "FOUND", file_id="evidence_931", observed=BEFORE, captured=LATE,
                category="USER_PROFILE", ids=FOUND_IDS),
            obs("account_9301", "NOT_FOUND", file_id="evidence_932", captured=LATE, category="USER_PROFILE"),
            COMPLIANT_COMPANION,
        ]
        cov = Coverage("evidence_933", "2026-09-03T09:00:00Z", "2026-09-03T09:00:00Z", "account_service",
                       "primary", "customer_profiles")
        result = audit_case(req(), make_ctx(world, [cov]))
        self.assertEqual(result.status, "COMPLETE")
        self.assertEqual(for_rid(result, "account_9301"),
                         [("VERIFICATION", "account_service", "account_9301", "VERIFY_RECORD_ABSENT", "ABSENT",
                           "evidence_933")])
        self.assertValidRow(result)

    def test_absence_without_any_citable_proof_is_dropped_from_a_complete_case(self):
        world = [
            obs("account_9301", "FOUND", file_id="evidence_931", observed=BEFORE, captured=LATE,
                category="USER_PROFILE", ids=FOUND_IDS),
            obs("account_9301", "NOT_FOUND", file_id="evidence_932", captured=LATE, category="USER_PROFILE"),
            COMPLIANT_COMPANION,
        ]
        result = audit_case(req(), make_ctx(world))
        self.assertEqual(result.status, "COMPLETE")
        self.assertEqual(for_rid(result, "account_9301"), [])
        self.assertIn("uncited_verification", kinds(result))
        self.assertValidRow(result)

    # Regression: an absence never cites a file showing the record present (owner decisions 20, 23).
    def test_absence_is_never_cited_with_a_file_that_shows_the_record_present(self):
        world = [
            obs("account_9351", "FOUND", file_id="evidence_951", observed=BEFORE, category="USER_PROFILE",
                ids=FOUND_IDS),
            obs("account_9351", "NOT_FOUND", file_id="evidence_952", captured=LATE, category="USER_PROFILE"),
            COMPLIANT_COMPANION,
        ]
        cov = Coverage("evidence_953", "2026-09-03T09:00:00Z", "2026-09-03T09:00:00Z", "account_service",
                       "primary", "customer_profiles")
        result = audit_case(req(), make_ctx(world, [cov]))
        cited = {t[5] for t in for_rid(result, "account_9351")}
        self.assertNotIn("evidence_951", cited)


class NotApplicableTest(EngineCase):

    def test_non_deletion_request_is_not_applicable_with_no_steps(self):
        result = audit_case(req(text=NON_DELETION_TEXT), make_ctx(INCOMPLETE_WORLD))
        self.assertEqual((result.status, result.steps), ("NOT_APPLICABLE", []))
        self.assertFalse(result.intent.authorizes_deletion)
        self.assertTrue(all(note_kind(n).startswith("intent") for n in result.notes), result.notes)
        self.assertValidRow(result)

    def test_non_deletion_corroborated_by_classify_row(self):
        rows = [classify_row("activity_9501", "account_9101", DURING)]
        result = audit_case(req(text=NON_DELETION_TEXT), make_ctx(INCOMPLETE_WORLD, activity=rows))
        self.assertEqual(result.status, "NOT_APPLICABLE")
        self.assertIn("intent_corroborated", kinds(result))

    def test_text_decides_when_classify_row_disagrees(self):
        # Plan §3 A1: a disagreement is reported, never silently overridden.
        rows = [classify_row("activity_9501", "account_9101", DURING)]
        result = audit_case(req(), make_ctx(INCOMPLETE_WORLD, activity=rows))
        self.assertEqual(result.status, "INCOMPLETE")
        self.assertIn("intent_conflict", kinds(result))

    def test_unknown_email_non_deletion(self):
        result = audit_case(req(email="nobody@example.test", text=NON_DELETION_TEXT), make_ctx(INCOMPLETE_WORLD))
        self.assertEqual((result.status, result.steps), ("NOT_APPLICABLE", []))

    # Owner decision 8: a deletion case with no usable evidence falls back to NOT_APPLICABLE, with a note.
    def test_decision8_unknown_email(self):
        result = audit_case(req(email="nobody@example.test"), make_ctx(INCOMPLETE_WORLD))
        self.assertEqual((result.status, result.steps), ("NOT_APPLICABLE", []))
        self.assertIn("safest_row", kinds(result))

    def test_decision8_known_subject_without_evidence(self):
        result = audit_case(req(), make_ctx([obs("account_9199", "FOUND", ids={OTHER_EMAIL})]))
        self.assertEqual((result.status, result.steps), ("NOT_APPLICABLE", []))
        self.assertTrue(result.intent.authorizes_deletion)
        self.assertIn("safest_row", kinds(result))

    # Regression: an email held by two users gives the safest row with a note (owner decision 20).
    def test_email_held_by_two_users_falls_back_to_the_safest_row(self):
        log = AUDIT_LOG + (AuditChange("audit_9002", OTHER_USER, "2026-05-01T00:00:00Z", "email", EMAIL,
                                       OTHER_EMAIL),)
        result = audit_case(req(), make_ctx(INCOMPLETE_WORLD, audit_log=log))
        self.assertEqual((result.status, result.steps), ("NOT_APPLICABLE", []))
        self.assertIn("safest_row", kinds(result))

    def test_decision8_only_uncitable_evidence(self):
        late_only = [obs("account_9601", "FOUND", captured=LATE, category="USER_PROFILE", ids=FOUND_IDS)]
        result = audit_case(req(), make_ctx(late_only))
        self.assertEqual((result.status, result.steps), ("NOT_APPLICABLE", []))
        self.assertIn("safest_row", kinds(result))
        self.assertIn("uncitable_finding", kinds(result))


class GhostCopyTest(EngineCase):

    def test_present_only_in_a_replica_of_the_same_file_counts_as_present(self):
        world = [
            obs("account_9401", "FOUND", file_id="evidence_941", observed="2026-09-02T10:00:00Z",
                category="USER_PROFILE", ids=FOUND_IDS, store="read_replica", src="sql:replica"),
            obs("account_9401", "NOT_FOUND", file_id="evidence_941", observed="2026-09-02T10:05:00Z",
                category="USER_PROFILE", store="primary", src="sql:primary"),
        ]
        result = audit_case(req(), make_ctx(world))
        self.assertEqual(result.status, "INCOMPLETE")
        self.assertEqual(for_rid(result, "account_9401")[0],
                         ("FINDING", "account_service", "account_9401", "REPORT_REMAINING_RECORD", "PRESENT",
                          "evidence_941"))
        self.assertIn("ghost_copy", kinds(result))
        self.assertValidRow(result)

    def test_store_order_inside_the_file_does_not_matter(self):
        for replica_source, primary_source in (("sql:a_replica", "sql:b_primary"), ("sql:b_replica", "sql:a_primary")):
            with self.subTest(replica=replica_source, primary=primary_source):
                world = [
                    obs("account_9401", "FOUND", file_id="evidence_941", category="USER_PROFILE", ids=FOUND_IDS,
                        store="read_replica", src=replica_source),
                    obs("account_9401", "NOT_FOUND", file_id="evidence_941", category="USER_PROFILE",
                        store="primary", src=primary_source),
                ]
                result = audit_case(req(), make_ctx(world))
                self.assertEqual(result.status, "INCOMPLETE")
                self.assertEqual(for_rid(result, "account_9401")[0][3], "REPORT_REMAINING_RECORD")


class CategoryHintTest(EngineCase):

    def _single(self, observation: Observation) -> CaseResult:
        return audit_case(req(), make_ctx([observation]))

    def test_email_opt_in_analytics_is_a_marketing_profile_to_delete(self):
        result = self._single(obs("analytics_9501", "IDENTIFIABLE", category="ANALYTICS_PROFILE", ids={EMAIL},
                                  email_opt_in="true"))
        self.assertEqual([t[3] for t in tuples(result.steps)],
                         ["REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"])
        self.assertIn("category_hint", kinds(result))

    def test_attachment_marker_makes_a_support_attachment_to_delete(self):
        for extra in ({"storage_class": "standard"}, {"event": "attachment_uploaded"}):
            with self.subTest(extra=extra):
                result = self._single(obs("support_9502", "IDENTIFIABLE", category="SUPPORT_MESSAGE", ids={EMAIL},
                                          file_name="receipt_scan.pdf", **extra))
                self.assertEqual([t[3] for t in tuples(result.steps)],
                                 ["REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"])
                self.assertIn("category_hint", kinds(result))

    def test_legacy_contact_index_copy_is_a_secondary_identifier_to_delete(self):
        for extra in ({"source": "legacy_contact_index"}, {"event": "deletion_marker_check"}):
            with self.subTest(extra=extra):
                result = self._single(obs("support_9503", "FOUND", category="SUPPORT_MESSAGE", ids={EMAIL}, **extra))
                self.assertEqual([t[3] for t in tuples(result.steps)],
                                 ["REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"])
                self.assertIn("category_hint", kinds(result))

    def test_plain_support_message_stays_redact(self):
        result = self._single(obs("support_9504", "IDENTIFIABLE", category="SUPPORT_MESSAGE", ids={EMAIL},
                                  file_name="receipt_scan.pdf"))
        self.assertEqual([t[3] for t in tuples(result.steps)],
                         ["REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED"])
        self.assertNotIn("category_hint", kinds(result))

    def test_hint_applies_only_to_its_base_category(self):
        result = self._single(obs("account_9505", "FOUND", category="USER_PROFILE", ids=FOUND_IDS,
                                  email_opt_in="true"))
        self.assertNotIn("category_hint", kinds(result))
        self.assertEqual(result.steps[0].operation, "REPORT_REMAINING_RECORD")

    def test_category_from_prefix_when_no_observation_names_one(self):
        result = self._single(obs("support_9506", "IDENTIFIABLE", ids={EMAIL}))
        self.assertEqual(result.steps[0].operation, "REPORT_EXPOSED_PII")

    def test_attachment_link_template_names_the_category_of_an_unknown_prefix(self):
        rows = [link("activity_9507", "ticket_file_9507", DURING, template="attachment",
                     target="support_platform")]
        result = audit_case(req(), make_ctx([COMPANION], activity=rows))
        self.assertIn(("FINDING", "support_platform", "ticket_file_9507", "REPORT_REMAINING_RECORD", "PRESENT",
                       "activity_9507"), tuples(result.steps))
        self.assertIn(("REMEDIATION", "support_platform", "ticket_file_9507", "DELETE_RECORD", "ABSENT", ""),
                      tuples(result.steps))
        self.assertValidRow(result)


class TombstoneTest(EngineCase):

    def test_tombstone_with_only_user_id_is_redacted(self):
        result = audit_case(req(), make_ctx([obs("tombstone_9601", "FOUND", category="ACCOUNT_TOMBSTONE",
                                                 ids={USER})]))
        self.assertEqual(tuples(result.steps),
                         [("VERIFICATION", "identity_service", "tombstone_9601", "VERIFY_RECORD_REDACTED",
                           "REDACTED", "evidence_901")])

    def test_tombstone_with_email_is_exposed(self):
        result = audit_case(req(), make_ctx([obs("tombstone_9602", "FOUND", category="ACCOUNT_TOMBSTONE",
                                                 ids={USER, EMAIL})]))
        self.assertEqual([t[3] for t in tuples(result.steps)],
                         ["REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED"])

    # Regression: a tombstone that still shows the person's name is exposed (owner decision 20).
    def test_tombstone_still_showing_the_name_is_exposed(self):
        result = audit_case(req(), make_ctx([obs("tombstone_9603", "FOUND", category="ACCOUNT_TOMBSTONE",
                                                 ids={USER, "name:mira calloway"})]))
        self.assertEqual(result.steps[0].operation, "REPORT_EXPOSED_PII")


class RetentionTest(EngineCase):

    def test_retained_record_inside_retention_is_verified_present(self):
        world = [obs("audit_record_9701", "FOUND", category="SECURITY_AUDIT_LOG", ids={USER},
                     retention_until="2027-01-01T00:00:00Z")]
        result = audit_case(req(), make_ctx(world))
        self.assertEqual(result.status, "COMPLETE")
        self.assertEqual(tuples(result.steps),
                         [("VERIFICATION", "security_service", "audit_record_9701", "VERIFY_RECORD_PRESENT",
                           "PRESENT", "evidence_901")])

    def test_identifiable_retained_record_is_still_retained(self):
        world = [obs("audit_record_9702", "IDENTIFIABLE", category="SECURITY_AUDIT_LOG", ids={USER, EMAIL},
                     retention_until="2027-01-01T00:00:00Z")]
        result = audit_case(req(), make_ctx(world))
        self.assertEqual([t[3] for t in tuples(result.steps)], ["VERIFY_RECORD_PRESENT"])

    def test_active_legal_hold_is_verified_present(self):
        world = [obs("legal_hold_9703", "FOUND", category="LEGAL_HOLD_RECORD", ids={USER},
                     retained_until="2028-01-01T00:00:00Z")]
        result = audit_case(req(), make_ctx(world))
        self.assertEqual(tuples(result.steps),
                         [("VERIFICATION", "legal_service", "legal_hold_9703", "VERIFY_RECORD_PRESENT", "PRESENT",
                           "evidence_901")])

    def test_expired_retention_is_flagged_not_verified(self):
        # Plan §2.2: RETAIN only while retention_until > audit_at; otherwise flag it. Equal is expired.
        for field_name, until in (("retention_until", "2026-08-01T00:00:00Z"), ("retention_until", AUDIT),
                                  ("retained_until", "2026-01-01T00:00:00Z")):
            with self.subTest(field=field_name, until=until):
                world = [obs("audit_record_9704", "FOUND", category="SECURITY_AUDIT_LOG", ids={USER},
                             **{field_name: until}), COMPANION]
                result = audit_case(req(), make_ctx(world))
                self.assertEqual(for_rid(result, "audit_record_9704"), [])
                self.assertIn("retain_expired", kinds(result))
                self.assertEqual(tuples(result.steps), COMPANION_STEPS)

    def test_missing_retained_record_is_reported_in_notes(self):
        # Plan E11: a missing retained record has no allowed operation; it goes to the run log.
        world = [obs("audit_record_9705", "NOT_FOUND", category="SECURITY_AUDIT_LOG", ids={USER},
                     retention_until="2027-01-01T00:00:00Z"), COMPANION]
        result = audit_case(req(), make_ctx(world))
        self.assertEqual(for_rid(result, "audit_record_9705"), [])
        self.assertIn("retained_missing", kinds(result))

    def test_undecidable_state_is_flagged_not_guessed(self):
        # Plan §2.2: a state the matrix does not define for a kind is flagged, never guessed.
        world = [obs("support_9706", "NOT_FOUND", category="SUPPORT_MESSAGE", ids={EMAIL}), COMPANION]
        result = audit_case(req(), make_ctx(world))
        self.assertEqual(for_rid(result, "support_9706"), [])
        self.assertIn("state_undecidable", kinds(result))
        self.assertEqual(tuples(result.steps), COMPANION_STEPS)


class Decision5OldRecordTest(EngineCase):
    """Owner decisions 5, 9, 15: an absent record whose evidence all predates requested_at is skipped,
    unless a link row naming this subject falls inside [requested_at, audit_at]."""

    OLD = [
        obs("account_9801", "FOUND", file_id="evidence_981", observed="2026-07-01T10:00:00Z",
            category="USER_PROFILE", ids=FOUND_IDS),
        obs("account_9801", "NOT_FOUND", file_id="evidence_982", observed="2026-07-05T10:00:00Z",
            category="USER_PROFILE"),
        COMPLIANT_COMPANION,
    ]

    def test_old_record_is_skipped_with_a_note(self):
        result = audit_case(req(), make_ctx(self.OLD))
        self.assertEqual(for_rid(result, "account_9801"), [])
        self.assertIn("earlier_request_record", kinds(result))
        self.assertEqual(result.status, "COMPLETE")

    def test_subject_link_inside_the_window_keeps_it(self):
        for when in (REQUESTED, DURING, AUDIT):
            with self.subTest(when=when):
                result = audit_case(req(), make_ctx(self.OLD, activity=[link("activity_9801", "account_9801", when)]))
                self.assertNotIn("earlier_request_record", kinds(result))
                self.assertEqual(for_rid(result, "account_9801"),
                                 [("VERIFICATION", "account_service", "account_9801", "VERIFY_RECORD_ABSENT",
                                   "ABSENT", "evidence_982")])
                self.assertValidRow(result)

    def test_link_naming_another_subject_does_not_keep_it(self):
        rows = [link("activity_9802", "account_9801", DURING, email=OTHER_EMAIL, user=OTHER_USER)]
        result = audit_case(req(), make_ctx(self.OLD, activity=rows))
        self.assertEqual(for_rid(result, "account_9801"), [])
        self.assertIn("earlier_request_record", kinds(result))

    def test_subject_link_outside_the_window_does_not_keep_it(self):
        for when in ("2026-09-01T07:59:59Z", "2026-07-03T10:00:00Z", "2026-09-04T12:00:01Z", LATE):
            with self.subTest(when=when):
                result = audit_case(req(), make_ctx(self.OLD, activity=[link("activity_9803", "account_9801", when)]))
                self.assertEqual(for_rid(result, "account_9801"), [])
                self.assertIn("earlier_request_record", kinds(result))

    def test_absence_seen_after_the_request_is_not_old(self):
        for when in (DURING, REQUESTED):  # evidence at requested_at does not predate the request
            with self.subTest(when=when):
                world = [self.OLD[0], replace(self.OLD[1], observed_at=when, captured_at=when), COMPLIANT_COMPANION]
                result = audit_case(req(), make_ctx(world))
                self.assertNotIn("earlier_request_record", kinds(result))
                self.assertEqual([t[3] for t in for_rid(result, "account_9801")], ["VERIFY_RECORD_ABSENT"])

    def test_old_but_still_present_record_is_reported(self):
        # Decision 5 covers already-deleted records only.
        world = [self.OLD[0], COMPLIANT_COMPANION]
        result = audit_case(req(), make_ctx(world))
        self.assertEqual(result.status, "INCOMPLETE")
        self.assertEqual(for_rid(result, "account_9801")[0][3], "REPORT_REMAINING_RECORD")


class Decision6LateExportTest(EngineCase):
    """Owner decisions 6, 13, 14: a record seen present by audit_at only in a later-captured file."""

    LATE_ACCOUNT = obs("account_9901", "FOUND", file_id="evidence_991", observed=DURING, captured=LATE,
                       category="USER_PROFILE", ids=FOUND_IDS)

    def _run(self, observations, rows):
        return audit_case(req(), make_ctx([*observations, COMPANION], activity=rows))

    def test_cites_the_failed_attempt_with_a_note(self):
        result = self._run([self.LATE_ACCOUNT], [attempt("activity_9901", "account_9901", "2026-09-02T09:00:00Z")])
        self.assertEqual(for_rid(result, "account_9901")[0],
                         ("FINDING", "account_service", "account_9901", "REPORT_REMAINING_RECORD", "PRESENT",
                          "activity_9901"))
        self.assertIn("late_export_cited_activity", kinds(result))
        self.assertValidRow(result)

    def test_cites_the_latest_failed_or_partial_row_at_or_before_audit(self):
        rows = [
            attempt("activity_9902", "account_9901", "2026-09-01T09:00:00Z", "PARTIAL"),
            attempt("activity_9903", "account_9901", "2026-09-03T09:00:00Z", "FAILED"),
            attempt("activity_9904", "account_9901", "2026-09-04T10:00:00Z", "SUCCESS"),
            attempt("activity_9905", "account_9901", LATE, "FAILED"),
        ]
        result = self._run([self.LATE_ACCOUNT], rows)
        self.assertEqual(for_rid(result, "account_9901")[0][5], "activity_9903")

    def test_partial_row_and_row_at_audit_at_count(self):
        for when, result_text in (("2026-09-02T09:00:00Z", "PARTIAL"), (AUDIT, "FAILED")):
            with self.subTest(when=when):
                result = self._run([self.LATE_ACCOUNT], [attempt("activity_9906", "account_9901", when, result_text)])
                self.assertEqual(for_rid(result, "account_9901")[0][5], "activity_9906")

    # Regression: a finding never cites a file showing the record absent (owner decision 20).
    def test_finding_is_never_cited_with_a_file_that_shows_the_record_absent(self):
        world = [
            obs("account_9961", "NOT_FOUND", file_id="evidence_961", observed="2026-09-02T09:00:00Z",
                category="USER_PROFILE", ids=FOUND_IDS),
            obs("account_9961", "FOUND", file_id="evidence_962", observed="2026-09-03T09:00:00Z", captured=LATE,
                category="USER_PROFILE", ids=FOUND_IDS),
        ]
        result = self._run(world, [attempt("activity_9961", "account_9961", "2026-09-02T08:00:00Z")])
        self.assertEqual(for_rid(result, "account_9961")[0][5], "activity_9961")

    def test_without_a_row_the_finding_is_dropped(self):
        for rows in ([], [attempt("activity_9907", "account_9901", "2026-09-02T09:00:00Z", "SUCCESS")],
                     [attempt("activity_9908", "account_9901", LATE, "FAILED")]):
            with self.subTest(rows=[a.activity_id for a in rows]):
                result = self._run([self.LATE_ACCOUNT], rows)
                self.assertEqual(for_rid(result, "account_9901"), [])
                self.assertIn("uncitable_finding", kinds(result))
                self.assertNotIn("late_export_cited_activity", kinds(result))
                self.assertEqual(tuples(result.steps), COMPANION_STEPS)

    def test_text_mention_in_another_rows_resource_column_does_not_count(self):
        # Decision 14: only rows whose resource_id column is the record.
        rows = [attempt("activity_9909", "account_9901", "2026-09-02T09:00:00Z", column="cache_9909")]
        result = self._run([self.LATE_ACCOUNT], rows)
        self.assertEqual(for_rid(result, "account_9901"), [])
        self.assertIn("uncitable_finding", kinds(result))

    def test_applies_to_redact_and_anonymize_records(self):
        cases = (
            (obs("support_9910", "IDENTIFIABLE", file_id="evidence_991", captured=LATE, category="SUPPORT_MESSAGE",
                 ids={EMAIL}), "REPORT_EXPOSED_PII"),
            (obs("analytics_9911", "IDENTIFIABLE", file_id="evidence_991", captured=LATE,
                 category="ANALYTICS_PROFILE", ids={EMAIL}), "REPORT_EXPOSED_PII"),
        )
        for observation, op in cases:
            with self.subTest(rid=observation.resource_id):
                rid = observation.resource_id
                result = self._run([observation], [attempt("activity_9912", rid, "2026-09-02T09:00:00Z", "PARTIAL")])
                self.assertEqual(for_rid(result, rid)[0][3:], (op, "PRESENT", "activity_9912"))

    def test_data_flows_and_backups_are_not_covered(self):
        # Decision 13: records only.
        for observation in (
                obs("data_flow_9913", "ACTIVE", file_id="evidence_991", captured=LATE, category=DATA_FLOW, ids={USER}),
                obs("backup_9914", "ACTIVE", file_id="evidence_991", captured=LATE, category="BACKUP_ARCHIVE",
                    ids={USER})):
            with self.subTest(rid=observation.resource_id):
                rid = observation.resource_id
                result = self._run([observation], [attempt("activity_9915", rid, "2026-09-02T09:00:00Z")])
                self.assertEqual(for_rid(result, rid), [])
                self.assertIn("uncitable_finding", kinds(result))
                self.assertNotIn("late_export_cited_activity", kinds(result))

    def test_record_observed_after_audit_is_not_present(self):
        too_late = replace(self.LATE_ACCOUNT, observed_at=LATE)
        result = self._run([too_late], [attempt("activity_9916", "account_9901", "2026-09-02T09:00:00Z")])
        self.assertEqual(for_rid(result, "account_9901"), [])


class LinkRowTest(EngineCase):
    """Owner decisions 9 and 10: link rows count only inside [requested_at, audit_at]; a record known
    only from a link row is assumed present and cites the link row."""

    def _run(self, rows):
        return audit_case(req(), make_ctx([COMPANION], activity=rows))

    def test_link_only_record_is_present_and_cites_the_link_row(self):
        result = self._run([link("activity_9951", "marketing_9951", DURING)])
        self.assertEqual(for_rid(result, "marketing_9951"), [
            ("FINDING", "marketing_platform", "marketing_9951", "REPORT_REMAINING_RECORD", "PRESENT", "activity_9951"),
            ("REMEDIATION", "marketing_platform", "marketing_9951", "DELETE_RECORD", "ABSENT", ""),
            ("VERIFICATION", "marketing_platform", "marketing_9951", "VERIFY_RECORD_ABSENT", "ABSENT", ""),
        ])
        self.assertTrue({"link_fallback", "link_assumed_present"} <= kinds(result))
        self.assertValidRow(result)

    def test_window_bounds_are_inclusive(self):
        for when in (REQUESTED, AUDIT):
            with self.subTest(when=when):
                result = self._run([link("activity_9952", "marketing_9952", when)])
                self.assertEqual(for_rid(result, "marketing_9952")[0][5], "activity_9952")

    def test_link_outside_the_window_is_ignored(self):
        for when in (BEFORE, "2026-09-01T07:59:59Z", "2026-09-04T12:00:01Z", LATE):
            with self.subTest(when=when):
                result = self._run([link("activity_9953", "marketing_9953", when)])
                self.assertEqual(for_rid(result, "marketing_9953"), [])
                self.assertEqual(tuples(result.steps), COMPANION_STEPS)

    def test_link_naming_someone_else_is_ignored(self):
        result = self._run([link("activity_9954", "marketing_9954", DURING, email=OTHER_EMAIL, user=OTHER_USER)])
        self.assertEqual(for_rid(result, "marketing_9954"), [])

    def test_link_through_the_old_email_counts(self):
        result = self._run([link("activity_9955", "marketing_9955", DURING, email=OLD_EMAIL, user="user_999")])
        self.assertEqual(for_rid(result, "marketing_9955")[0][5], "activity_9955")

    def test_link_document_adds_document_evidence(self):
        document = doc("evidence_956", DURING, "USER_EXPORT", "export_service", reference="NST-2026-0956")
        rows = [link("activity_9956", "export_9956", DURING, secondary="NST-2026-0956")]
        result = audit_case(req(), make_ctx([COMPANION, document], activity=rows))
        self.assertEqual(for_rid(result, "export_9956")[0],
                         ("FINDING", "export_service", "export_9956", "REPORT_REMAINING_RECORD", "PRESENT",
                          "evidence_956"))
        self.assertIn("link_document", kinds(result))

    # Regression: a link-only retained record cites its link row in a COMPLETE case (owner decisions 10, 20).
    def test_link_only_retained_record_cites_the_link_row(self):
        rows = [link("activity_9957", "audit_record_9957", DURING)]
        result = audit_case(req(), make_ctx([COMPLIANT_COMPANION], activity=rows))
        self.assertEqual(result.status, "COMPLETE")
        self.assertIn(("VERIFICATION", "security_service", "audit_record_9957", "VERIFY_RECORD_PRESENT", "PRESENT",
                       "activity_9957"), tuples(result.steps))


class TimeAndDeterminismTest(EngineCase):

    LATER_EVIDENCE = [
        obs("account_9101", "NOT_FOUND", file_id="evidence_970", observed=LATE, category="USER_PROFILE"),
        obs("support_9105", "REDACTED", file_id="evidence_970", observed=LATE, category="SUPPORT_MESSAGE",
            ids={USER}),
        obs("data_flow_9103", "DISABLED", file_id="evidence_970", observed=LATE, category=DATA_FLOW, ids={USER}),
        obs("search_9180", "FOUND", file_id="evidence_970", observed=LATE, category="SEARCH_INDEX_ENTRY",
            ids={EMAIL}),
        obs("account_9201", "FOUND", file_id="evidence_970", observed=LATE, category="USER_PROFILE", ids=FOUND_IDS),
        obs("audit_record_9206", "NOT_FOUND", file_id="evidence_970", observed=LATE, category="SECURITY_AUDIT_LOG"),
        obs("tombstone_9207", "FOUND", file_id="evidence_970", observed=LATE, category="ACCOUNT_TOMBSTONE",
            ids={USER, EMAIL}),
    ]
    LATER_COVERAGE = [Coverage("evidence_971", LATE, LATE, "account_service", "primary", "customer_profiles")]
    LATER_ACTIVITY = [
        attempt("activity_9971", "account_9101", LATE, "FAILED"),
        link("activity_9972", "marketing_9972", LATE),
        classify_row("activity_9973", "account_9101", LATE),
        link("activity_9974", "account_9201", LATE),
    ]

    def test_evidence_after_audit_never_changes_the_answer(self):
        for name, world in (("incomplete", INCOMPLETE_WORLD), ("complete", COMPLETE_WORLD)):
            with self.subTest(world=name):
                base = audit_case(req(), make_ctx(world))
                later = audit_case(req(), make_ctx([*world, *self.LATER_EVIDENCE], self.LATER_COVERAGE,
                                                   self.LATER_ACTIVITY))
                self.assertEqual(later.row(), base.row())
                self.assertEqual(later.notes, base.notes)

    def test_same_input_same_output(self):
        ctx = make_ctx(INCOMPLETE_WORLD)
        first = audit_case(req(), ctx)
        second = audit_case(req(), ctx)
        self.assertEqual(first, second)

    def test_input_order_does_not_matter(self):
        base = audit_case(req(), make_ctx(COMPLETE_WORLD + INCOMPLETE_WORLD))
        rng = random.Random(9001)
        for _ in range(5):
            world = COMPLETE_WORLD + INCOMPLETE_WORLD
            rng.shuffle(world)
            self.assertEqual(audit_case(req(), make_ctx(world)).row(), base.row())

    def test_run_keeps_request_order(self):
        ctx = make_ctx(INCOMPLETE_WORLD)
        requests = [req("case_9003"), req("case_9001", text=NON_DELETION_TEXT), req("case_9002")]
        results = engine.run(requests, ctx)
        self.assertEqual([r.case_id for r in results], ["case_9003", "case_9001", "case_9002"])
        self.assertEqual([r.status for r in results], ["INCOMPLETE", "NOT_APPLICABLE", "INCOMPLETE"])

    def test_cases_of_the_same_person_are_independent(self):
        # E15: each case is recomputed from its own audit_at.
        ctx = make_ctx(INCOMPLETE_WORLD + [obs("account_9101", "NOT_FOUND", file_id="evidence_975",
                                               observed="2026-09-20T10:00:00Z", category="USER_PROFILE")])
        early = audit_case(req("case_9001"), ctx)
        late = audit_case(req("case_9002", requested="2026-09-15T08:00:00Z", audit="2026-09-25T12:00:00Z"), ctx)
        again = audit_case(req("case_9001"), ctx)
        self.assertEqual(for_rid(early, "account_9101")[0][3], "REPORT_REMAINING_RECORD")
        self.assertEqual(for_rid(late, "account_9101")[0][3], "VERIFY_RECORD_ABSENT")
        self.assertEqual(again.row(), early.row())


class IntentSourceTest(unittest.TestCase):

    def test_rules_mode_uses_the_keyword_baseline(self):
        r = make_ctx().intent(DELETE_TEXT)
        self.assertEqual(r.source, "rules")
        self.assertTrue(r.authorizes_deletion)

    def test_llm_cache_hit_wins_in_auto_mode(self):
        hit = IntentResult(False, "access", "en", 0.9, "llm:test-model", "")
        ctx = make_ctx(intent_mode="auto", llm_lookup=lambda text: hit)
        self.assertIs(ctx.intent(DELETE_TEXT), hit)

    def test_auto_mode_falls_back_to_rules_on_a_miss(self):
        ctx = make_ctx(intent_mode="auto", llm_lookup=lambda text: None)
        self.assertEqual(ctx.intent(DELETE_TEXT).source, "rules")

    def test_cache_mode_miss_is_an_error(self):
        ctx = make_ctx(intent_mode="cache", llm_lookup=lambda text: None)
        with self.assertRaises(LookupError):
            ctx.intent(DELETE_TEXT)

    def test_unknown_mode_is_rejected_before_loading(self):
        with self.assertRaises(ValueError):
            Context.build("llm")

    def test_audit_case_uses_the_context_intent(self):
        no = IntentResult(False, "other", "en", 0.9, "llm:test-model", "")
        ctx = make_ctx(INCOMPLETE_WORLD, intent_mode="auto", llm_lookup=lambda text: no)
        result = audit_case(req(), ctx)
        self.assertEqual((result.status, result.steps), ("NOT_APPLICABLE", []))
        self.assertIs(result.intent, no)


class DataTablesTest(unittest.TestCase):

    CATEGORIES = RULE_CATEGORIES | {DATA_FLOW}

    def test_prefix_categories_are_known(self):
        self.assertLessEqual(set(engine.CATEGORY_BY_PREFIX.values()), self.CATEGORIES)
        self.assertLessEqual(set(engine.CATEGORY_BY_LINK_TEMPLATE.values()), self.CATEGORIES)

    def test_every_system_prefix_has_a_category(self):
        self.assertEqual(set(engine.CATEGORY_BY_PREFIX), set(SYSTEM_BY_PREFIX))

    def test_category_hints_are_well_formed(self):
        for hint in engine.CATEGORY_HINTS:
            with self.subTest(hint=hint.name):
                self.assertLessEqual(hint.base, self.CATEGORIES)
                self.assertIn(hint.category, self.CATEGORIES)
                self.assertTrue(hint.when)
                for clause in hint.when:
                    for key_rx, value_rx in clause:
                        re.compile(key_rx)
                        re.compile(value_rx)

    def test_plan_hint_targets(self):
        targets = {h.name: (set(h.base), h.category) for h in engine.CATEGORY_HINTS}
        self.assertEqual(targets["email_opt_in"], ({"ANALYTICS_PROFILE"}, "MARKETING_PROFILE"))
        self.assertEqual(targets["attachment_marker"], ({"SUPPORT_MESSAGE"}, "SUPPORT_ATTACHMENT"))
        self.assertEqual(targets["legacy_contact_copy"], ({"SUPPORT_MESSAGE"}, "SECONDARY_IDENTIFIER"))

    def test_state_rules_map_into_the_matrix(self):
        for (kind, state), target in engine.STATE_RULES.items():
            with self.subTest(kind=kind, state=state):
                self.assertNotIn((kind, state), MATRIX)
                if target == "@identifiers":
                    self.assertIn((kind, "IDENTIFIABLE"), MATRIX)
                    self.assertIn((kind, engine.COMPLIANT_STATE[kind]), MATRIX)
                else:
                    self.assertIn((kind, target), MATRIX)

    def test_present_state_is_a_present_matrix_state(self):
        for kind, state in engine.PRESENT_STATE.items():
            with self.subTest(kind=kind):
                self.assertIn((kind, state), MATRIX)
                finding = MATRIX[(kind, state)][0]
                if kind == "RETAIN":
                    self.assertEqual(MATRIX[(kind, state)][2], "VERIFY_RECORD_PRESENT")
                else:
                    self.assertIsNotNone(finding)

    def test_retention_fields_and_unsuccessful_results(self):
        self.assertEqual(set(engine.RETENTION_FIELDS), {"retention_until", "retained_until"})
        self.assertEqual(engine.UNSUCCESSFUL_RESULTS, frozenset({"FAILED", "PARTIAL"}))

    def test_note_kind_and_identifier_free_hook(self):
        self.assertEqual(note_kind("link_fallback: x known only from y"), "link_fallback")
        self.assertEqual(note_kind("plain"), "plain")
        profile = make_ctx().identities.resolve(EMAIL)
        self.assertEqual(engine.identifier_free_resources(req(), profile, make_ctx()), [])


# ------------------------------------------------------------------ real data

_DATA = paths.dataset_dir()
DATASET_OK = all((_DATA / n).is_file() for n in (
    "deletion_requests.csv", "sample_deletion_requests.csv", "sample_output.csv", "evidence_files.csv",
    "user_identities.csv", "user_audit_log.csv", "system_activity.csv", "deletion_rules.csv"))


@unittest.skipUnless(DATASET_OK, "participant dataset not present")
class RealDataTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.ctx = Context.build("rules")
        cls.requests = loaders.load_requests()
        cls.samples = loaders.load_requests("sample_deletion_requests.csv")
        cls.results = {r.case_id: r for r in engine.run(cls.requests, cls.ctx)}
        cls.sample_results = {r.case_id: r for r in engine.run(cls.samples, cls.ctx)}
        from ghostaudit.output import read_rows
        cls.gold = read_rows(_DATA / "sample_output.csv")

    def test_every_evaluation_case_gives_one_valid_row(self):
        self.assertEqual(len(self.requests), 518)
        self.assertEqual(sorted(self.results, key=natural_key), sorted((r.case_id for r in self.requests),
                                                                       key=natural_key))
        for case_id, r in self.results.items():
            self.assertEqual(validate(r.status, r.steps), [], case_id)

    def test_status_counts(self):
        counts = {s: sum(r.status == s for r in self.results.values())
                  for s in ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")}
        print(f"\n  real-data status counts (--intent rules): {counts}")
        self.assertEqual(sum(counts.values()), 518)
        self.assertEqual(counts["NOT_APPLICABLE"], 10)
        not_applicable = sorted((c for c, r in self.results.items() if r.status == "NOT_APPLICABLE"), key=natural_key)
        self.assertEqual(not_applicable, [f"case_{n}" for n in range(533, 543)])

    def test_case_126_cites_the_failed_attempt(self):
        r = self.results["case_126"]
        self.assertEqual(r.status, "INCOMPLETE")
        findings = [s for s in r.steps if s.phase == "FINDING" and s.resource_id == "account_8819"]
        self.assertEqual(len(findings), 1)
        self.assertEqual((findings[0].operation, findings[0].evidence_id), ("REPORT_REMAINING_RECORD", "activity_3114"))

    def test_sample_case_11_matches_gold(self):
        r = self.sample_results["case_11"]
        gold_status, gold_steps = self.gold["case_11"]
        self.assertEqual((r.status, r.steps), (gold_status, gold_steps))

    def test_sample_case_17_drops_the_earlier_request_account(self):
        r = self.sample_results["case_17"]
        self.assertNotIn("account_6523", {s.resource_id for s in r.steps})

    def test_sample_step_precision_is_one(self):
        from ghostaudit import scorer
        report = scorer.score({c: (r.status, r.steps) for c, r in self.sample_results.items()}, self.gold)
        print(f"\n  sample run: status accuracy {report.status_accuracy:.4f}, step P/R/F1 "
              f"{report.steps.precision:.4f}/{report.steps.recall:.4f}/{report.steps.f1:.4f}")
        self.assertEqual(report.steps.precision, 1.0)
        self.assertEqual(report.missing_predictions, [])

    def test_cited_files_agree_with_the_cited_state(self):
        present = {"FOUND", "IDENTIFIABLE", "ACTIVE"}
        for case_id, r in {**self.results, **self.sample_results}.items():
            for s in r.steps:
                if not s.evidence_id.startswith("evidence_"):
                    continue
                states = {o.state for o in self.ctx.evidence.in_file(s.evidence_id) if o.resource_id == s.resource_id}
                if not states:
                    continue
                if s.operation == "VERIFY_RECORD_ABSENT":
                    self.assertIn("NOT_FOUND", states, (case_id, s))
                if s.phase == "FINDING":
                    self.assertTrue(states & present, (case_id, s))

    def test_no_citation_is_captured_after_audit_at(self):
        from ghostaudit.timeutil import parse_ts
        captured = {m.file_id: parse_ts(m.captured_at) for m in loaders.load_manifest()}
        events = {a.activity_id: parse_ts(a.event_time) for a in self.ctx.activity.rows}
        by_case = {r.case_id: r for r in [*self.requests, *self.samples]}
        for case_id, r in {**self.results, **self.sample_results}.items():
            cut = parse_ts(by_case[case_id].audit_at)
            for s in r.steps:
                if s.evidence_id.startswith("evidence_"):
                    self.assertLessEqual(captured[s.evidence_id], cut, (case_id, s))
                elif s.evidence_id.startswith("activity_"):
                    self.assertLessEqual(events[s.evidence_id], cut, (case_id, s))


# ------------------------------------------------------------------ no hard-coding guard

_CODE = Path(__file__).resolve().parents[1]
PRODUCT_FILES = sorted([*(_CODE / "ghostaudit").rglob("*.py"), _CODE / "main.py"])
ID_LITERAL = re.compile(r"\b(?:case|evidence|activity)_\d+\b")

# Docstring examples are allowed: they document sort orders and are never executed. Found today:
#   ghostaudit/normalize.py natural_key: "orders `evidence_9` before `evidence_10`"
#   ghostaudit/scorer.py _case_key: "case_99 before case_100"
ALLOWED_DOCSTRINGS = {("normalize.py", "evidence_9"), ("normalize.py", "evidence_10"),
                      ("scorer.py", "case_99"), ("scorer.py", "case_100")}


def _docstring_lines(tree: ast.AST) -> set[int]:
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                lines.update(range(first.lineno, first.end_lineno + 1))
    return lines


class NoHardcodingTest(unittest.TestCase):

    def test_product_files_found(self):
        names = {p.name for p in PRODUCT_FILES}
        self.assertTrue({"engine.py", "intent_rules.py", "main.py"} <= names, names)

    def test_no_literal_case_evidence_or_activity_ids(self):
        offenders = []
        for path in PRODUCT_FILES:
            source = path.read_text(encoding="utf-8")
            doc_lines = _docstring_lines(ast.parse(source))
            for m in ID_LITERAL.finditer(source):
                line = source.count("\n", 0, m.start()) + 1
                if line in doc_lines and (path.name, m.group()) in ALLOWED_DOCSTRINGS:
                    continue
                offenders.append(f"{path.relative_to(_CODE)}:{line}: {m.group()}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
