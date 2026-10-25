"""Tests for ghostaudit.planner (tools_spec T10: per-resource decision matrix and case assembly)."""

from __future__ import annotations

import csv
import json
import unittest
from collections import OrderedDict
from pathlib import Path

from ghostaudit.models import STATES, Step
from ghostaudit.planner import KINDS, MATRIX, NOT_APPLICABLE, ResourcePlan, assemble, plan
from ghostaudit.steps import from_json, make_step, to_json, validate

DATASET = Path(__file__).resolve().parents[2] / "dataset"

# tools_spec T10 decision matrix: (kind, state) -> (finding, remediation, verification).
SPEC_MATRIX = {
    ("DELETE", "FOUND"): ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"),
    ("DELETE", "IDENTIFIABLE"): ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"),
    ("DELETE", "NOT_FOUND"): (None, None, "VERIFY_RECORD_ABSENT"),
    ("REDACT", "IDENTIFIABLE"): ("REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED"),
    ("REDACT", "REDACTED"): (None, None, "VERIFY_RECORD_REDACTED"),
    ("ANONYMIZE", "IDENTIFIABLE"): ("REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"),
    ("ANONYMIZE", "ANONYMIZED"): (None, None, "VERIFY_RECORD_ANONYMIZED"),
    ("RETAIN", "FOUND"): (None, None, "VERIFY_RECORD_PRESENT"),
    ("DATA_FLOW", "ACTIVE"): ("REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED"),
    ("DATA_FLOW", "DISABLED"): (None, None, "VERIFY_DATA_FLOW_DISABLED"),
    ("BACKUP_ARCHIVE", "ACTIVE"): ("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED"),
    ("BACKUP_ARCHIVE", "BLOCKED"): (None, None, "VERIFY_RESTORE_BLOCKED"),
}


class TestDecisionMatrix(unittest.TestCase):
    def test_matrix_has_exactly_the_spec_rows(self):
        self.assertEqual(dict(MATRIX), SPEC_MATRIX)

    def test_kinds_constant(self):
        self.assertEqual(set(KINDS), {"DELETE", "REDACT", "ANONYMIZE", "RETAIN", "DATA_FLOW", "BACKUP_ARCHIVE"})

    def test_plan_follows_every_row_of_the_spec_matrix(self):
        for (kind, state), (find_op, fix_op, verify_op) in SPEC_MATRIX.items():
            with self.subTest(kind=kind, state=state):
                p = plan("sys", "res_1", kind, state, "evidence_07")
                self.assertEqual([s.operation for s in p.findings], [find_op] if find_op else [])
                self.assertEqual([s.operation for s in p.remediation], [fix_op] if fix_op else [])
                self.assertEqual([s.operation for s in p.verification], [verify_op])

    def test_violation_rows_cite_evidence_on_the_finding_only(self):
        p = plan("search_service", "search_052", "DELETE", "FOUND", "evidence_03")
        self.assertEqual(p.findings, [make_step("REPORT_REMAINING_RECORD", "search_service", "search_052", "evidence_03")])
        self.assertEqual(p.remediation, [make_step("DELETE_RECORD", "search_service", "search_052", "")])
        self.assertEqual(p.verification, [make_step("VERIFY_RECORD_ABSENT", "search_service", "search_052", "")])

    def test_compliant_rows_give_one_verification_that_cites_evidence(self):
        p = plan("security_service", "audit_record_5416", "RETAIN", "FOUND", "evidence_92")
        self.assertEqual(p.findings, [])
        self.assertEqual(p.remediation, [])
        self.assertEqual(p.verification,
                         [make_step("VERIFY_RECORD_PRESENT", "security_service", "audit_record_5416", "evidence_92")])

    def test_plan_copies_system_and_multi_underscore_resource_ids(self):
        p = plan("integration_service", "data_flow_9522", "DATA_FLOW", "ACTIVE", "evidence_03")
        for s in p.findings + p.remediation + p.verification:
            self.assertEqual((s.system_id, s.resource_id), ("integration_service", "data_flow_9522"))

    def test_tombstone_is_planned_as_redact(self):
        p = plan("identity_service", "tombstone_12", "REDACT", "IDENTIFIABLE", "evidence_40")
        self.assertEqual([s.operation for s in p.findings + p.remediation + p.verification],
                         ["REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED"])

    def test_every_undefined_kind_state_pair_raises_value_error(self):
        undefined = [(k, s) for k in KINDS for s in STATES if (k, s) not in SPEC_MATRIX]
        self.assertTrue(undefined)
        for kind, state in undefined:
            with self.subTest(kind=kind, state=state), self.assertRaises(ValueError):
                plan("sys", "res_1", kind, state, "evidence_01")

    def test_unknown_kind_raises_value_error(self):
        for kind in ("ACCOUNT_TOMBSTONE", "delete", "USER_PROFILE", ""):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                plan("sys", "res_1", kind, "FOUND", "evidence_01")

    def test_unknown_state_raises_value_error(self):
        with self.assertRaises(ValueError):
            plan("sys", "res_1", "DELETE", "PRESENT", "evidence_01")


class TestAssemble(unittest.TestCase):
    def test_all_compliant_plans_give_complete_with_evidence_kept(self):
        status, steps = assemble([
            plan("support_platform", "support_4715", "REDACT", "REDACTED", "evidence_92"),
            plan("account_service", "account_5684", "DELETE", "NOT_FOUND", "evidence_54"),
        ])
        self.assertEqual(status, "COMPLETE")
        self.assertEqual([(s.step_no, s.resource_id, s.evidence_id) for s in steps],
                         [(1, "account_5684", "evidence_54"), (2, "support_4715", "evidence_92")])
        self.assertTrue(all(s.phase == "VERIFICATION" for s in steps))

    def test_any_finding_makes_the_case_incomplete(self):
        status, _ = assemble([
            plan("account_service", "account_4248", "DELETE", "NOT_FOUND", "evidence_54"),
            plan("search_service", "search_052", "DELETE", "FOUND", "evidence_03"),
        ])
        self.assertEqual(status, "INCOMPLETE")

    def test_incomplete_keeps_finding_evidence(self):
        _, steps = assemble([plan("search_service", "search_052", "DELETE", "FOUND", "evidence_03")])
        self.assertEqual([s.evidence_id for s in steps if s.phase == "FINDING"], ["evidence_03"])

    def test_incomplete_blanks_evidence_of_already_compliant_checks(self):
        _, steps = assemble([
            plan("account_service", "account_4248", "DELETE", "NOT_FOUND", "evidence_54"),
            plan("search_service", "search_052", "DELETE", "FOUND", "evidence_03"),
        ])
        verification = [s for s in steps if s.phase == "VERIFICATION"]
        self.assertEqual([(s.resource_id, s.evidence_id) for s in verification],
                         [("account_4248", ""), ("search_052", "")])

    def test_incomplete_blanks_evidence_given_on_remediation_and_verification(self):
        hand_made = ResourcePlan(
            findings=[make_step("REPORT_EXPOSED_PII", "support_platform", "support_864", "evidence_03")],
            remediation=[make_step("REDACT_RECORD", "support_platform", "support_864", "evidence_03")],
            verification=[make_step("VERIFY_RECORD_REDACTED", "support_platform", "support_864", "evidence_03")],
        )
        status, steps = assemble([hand_made])
        self.assertEqual(status, "INCOMPLETE")
        self.assertEqual([s.evidence_id for s in steps], ["evidence_03", "", ""])

    def test_incomplete_steps_are_sorted_numbered_and_valid(self):
        status, steps = assemble([
            plan("analytics_platform", "analytics_2034", "ANONYMIZE", "IDENTIFIABLE", "evidence_03"),
            plan("integration_service", "data_flow_9522", "DATA_FLOW", "ACTIVE", "evidence_03"),
            plan("security_service", "audit_record_9394", "RETAIN", "FOUND", "evidence_06"),
            plan("search_service", "search_052", "DELETE", "FOUND", "evidence_03"),
        ])
        self.assertEqual([s.step_no for s in steps], list(range(1, len(steps) + 1)))
        self.assertEqual([s.operation for s in steps], [
            "REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW",
            "DISABLE_DATA_FLOW", "DELETE_RECORD", "ANONYMIZE_RECORD",
            "VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_ANONYMIZED",
            "VERIFY_DATA_FLOW_DISABLED"])
        self.assertEqual(validate(status, steps), [])

    def test_complete_steps_pass_validate(self):
        status, steps = assemble([plan("backup_service", "backup_3120", "BACKUP_ARCHIVE", "BLOCKED", "evidence_92")])
        self.assertEqual(validate(status, steps), [])

    def test_duplicate_steps_across_plans_are_merged(self):
        a = plan("search_service", "search_052", "DELETE", "FOUND", "evidence_03")
        b = plan("search_service", "search_052", "DELETE", "FOUND", "evidence_03")
        _, steps = assemble([a, b])
        self.assertEqual(len(steps), 3)

    def test_assemble_accepts_a_generator(self):
        status, steps = assemble(plan("account_service", f"account_{i}", "DELETE", "NOT_FOUND", "evidence_54")
                                 for i in (2, 1))
        self.assertEqual(status, "COMPLETE")
        self.assertEqual([s.resource_id for s in steps], ["account_1", "account_2"])

    def test_assemble_without_plans_raises_value_error(self):
        with self.assertRaises(ValueError):
            assemble([])

    def test_remediation_without_any_finding_raises_value_error(self):
        orphan = ResourcePlan(remediation=[make_step("DELETE_RECORD", "a", "r_1")],
                              verification=[make_step("VERIFY_RECORD_ABSENT", "a", "r_1", "evidence_01")])
        with self.assertRaises(ValueError):
            assemble([orphan])

    def test_not_applicable_constant_is_status_with_no_steps(self):
        self.assertEqual(NOT_APPLICABLE, ("NOT_APPLICABLE", []))
        self.assertEqual(validate(*NOT_APPLICABLE), [])


# Inverse of the spec matrix, used only to rebuild planner inputs from gold rows.
KIND_BY_VERIFY = {
    "VERIFY_RECORD_ABSENT": ("DELETE", "FOUND", "NOT_FOUND"),
    "VERIFY_RECORD_REDACTED": ("REDACT", "IDENTIFIABLE", "REDACTED"),
    "VERIFY_RECORD_ANONYMIZED": ("ANONYMIZE", "IDENTIFIABLE", "ANONYMIZED"),
    "VERIFY_RECORD_PRESENT": ("RETAIN", None, "FOUND"),
    "VERIFY_DATA_FLOW_DISABLED": ("DATA_FLOW", "ACTIVE", "DISABLED"),
    "VERIFY_RESTORE_BLOCKED": ("BACKUP_ARCHIVE", "ACTIVE", "BLOCKED"),
}


def gold_inputs(steps: list[Step]) -> list[tuple[str, str, str, str, str]]:
    """(system_id, resource_id, kind, state, evidence_id) per resource of one gold row."""
    by_resource: OrderedDict[tuple[str, str], list[Step]] = OrderedDict()
    for s in steps:
        by_resource.setdefault((s.system_id, s.resource_id), []).append(s)
    inputs = []
    for (system_id, resource_id), group in by_resource.items():
        verify = [s for s in group if s.phase == "VERIFICATION"]
        finding = [s for s in group if s.phase == "FINDING"]
        kind, bad_state, good_state = KIND_BY_VERIFY[verify[0].operation]
        if finding:
            inputs.append((system_id, resource_id, kind, bad_state, finding[0].evidence_id))
        else:
            inputs.append((system_id, resource_id, kind, good_state, verify[0].evidence_id))
    return inputs


@unittest.skipUnless(DATASET.is_dir(), "dataset not available")
class TestAssembleOnGoldSample(unittest.TestCase):
    """tools_spec T10 acceptance: assemble reproduces every gold sample row from its tuples."""

    def test_assemble_reproduces_all_32_gold_rows(self):
        with open(DATASET / "sample_output.csv", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 32)
        for row in rows:
            with self.subTest(case_id=row["case_id"]):
                gold_steps = from_json(row["steps_json"])
                plans = [plan(*args) for args in gold_inputs(gold_steps)]
                status, steps = assemble(plans)
                self.assertEqual(status, row["deletion_status"])
                self.assertEqual(json.loads(to_json(steps)), json.loads(row["steps_json"]))


if __name__ == "__main__":
    unittest.main()
