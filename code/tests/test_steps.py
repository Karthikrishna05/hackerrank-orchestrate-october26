"""Tests for ghostaudit.steps (tools_spec T9: step builder, sorter, validator and serialiser)."""

from __future__ import annotations

import csv
import itertools
import json
import unittest
from dataclasses import replace
from pathlib import Path

from ghostaudit.models import Step
from ghostaudit.steps import (
    KEYS,
    OPERATIONS,
    PHASES,
    STATUSES,
    from_json,
    make_step,
    sort_and_number,
    step_key,
    to_json,
    validate,
)

DATASET = Path(__file__).resolve().parents[2] / "dataset"

# The "Allowed Operations" table of problem_statement.md, in its published order.
ALLOWED = [
    ("FINDING", "REPORT_REMAINING_RECORD", "PRESENT"),
    ("FINDING", "REPORT_EXPOSED_PII", "PRESENT"),
    ("FINDING", "REPORT_ACTIVE_DATA_FLOW", "ACTIVE"),
    ("FINDING", "REPORT_RESTORE_RISK", "ACTIVE"),
    ("REMEDIATION", "DISABLE_DATA_FLOW", "DISABLED"),
    ("REMEDIATION", "BLOCK_RESTORE", "BLOCKED"),
    ("REMEDIATION", "DELETE_RECORD", "ABSENT"),
    ("REMEDIATION", "REDACT_RECORD", "REDACTED"),
    ("REMEDIATION", "ANONYMIZE_RECORD", "ANONYMIZED"),
    ("VERIFICATION", "VERIFY_RECORD_ABSENT", "ABSENT"),
    ("VERIFICATION", "VERIFY_RECORD_PRESENT", "PRESENT"),
    ("VERIFICATION", "VERIFY_RECORD_REDACTED", "REDACTED"),
    ("VERIFICATION", "VERIFY_RECORD_ANONYMIZED", "ANONYMIZED"),
    ("VERIFICATION", "VERIFY_DATA_FLOW_DISABLED", "DISABLED"),
    ("VERIFICATION", "VERIFY_RESTORE_BLOCKED", "BLOCKED"),
]


def numbered(*steps: Step) -> list[Step]:
    """Number steps 1..n in the given order without re-sorting."""
    return [replace(s, step_no=i) for i, s in enumerate(steps, 1)]


def incomplete_example() -> list[Step]:
    """A valid INCOMPLETE row: one finding, its fix and its check."""
    return numbered(
        make_step("REPORT_REMAINING_RECORD", "search_service", "search_052", "evidence_03"),
        make_step("DELETE_RECORD", "search_service", "search_052"),
        make_step("VERIFY_RECORD_ABSENT", "search_service", "search_052"),
    )


def complete_example() -> list[Step]:
    return numbered(
        make_step("VERIFY_RECORD_ABSENT", "account_service", "account_5684", "evidence_92"),
        make_step("VERIFY_RECORD_PRESENT", "security_service", "audit_record_5416", "activity_4000"),
    )


class TestOperationTable(unittest.TestCase):
    def test_operations_match_the_problem_statement_table(self):
        self.assertEqual({op: (spec.phase, spec.state) for op, spec in OPERATIONS.items()},
                         {op: (phase, state) for phase, op, state in ALLOWED})

    def test_operation_ranks_follow_the_table_order(self):
        ranked = sorted(OPERATIONS, key=lambda op: OPERATIONS[op].rank)
        self.assertEqual(ranked, [op for _, op, _ in ALLOWED])
        self.assertEqual(len({spec.rank for spec in OPERATIONS.values()}), 15)

    def test_phases_statuses_and_keys_constants(self):
        self.assertEqual(PHASES, ("FINDING", "REMEDIATION", "VERIFICATION"))
        self.assertEqual(set(STATUSES), {"COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"})
        self.assertEqual(KEYS, ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"))


class TestMakeStep(unittest.TestCase):
    def test_make_step_fills_phase_and_state_for_every_operation(self):
        for phase, op, state in ALLOWED:
            with self.subTest(op=op):
                s = make_step(op, "sys", "res_1", "evidence_01")
                self.assertEqual((s.phase, s.operation, s.state), (phase, op, state))
                self.assertEqual((s.system_id, s.resource_id, s.evidence_id), ("sys", "res_1", "evidence_01"))

    def test_make_step_defaults_to_empty_evidence_and_unnumbered(self):
        s = make_step("DELETE_RECORD", "search_service", "search_052")
        self.assertEqual(s.evidence_id, "")
        self.assertEqual(s.step_no, 0)

    def test_make_step_rejects_unknown_operation(self):
        for bad in ("PURGE_RECORD", "delete_record", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    make_step(bad, "sys", "res_1")


class TestStepKey(unittest.TestCase):
    def test_step_key_is_every_field_except_step_no(self):
        s = Step(7, "FINDING", "sys", "res_1", "REPORT_EXPOSED_PII", "PRESENT", "evidence_02")
        self.assertEqual(step_key(s), ("FINDING", "sys", "res_1", "REPORT_EXPOSED_PII", "PRESENT", "evidence_02"))
        self.assertEqual(step_key(s), step_key(replace(s, step_no=1)))


class TestSortAndNumber(unittest.TestCase):
    def test_orders_phases_finding_remediation_verification(self):
        steps = [make_step("VERIFY_RECORD_ABSENT", "a", "r_1"),
                 make_step("DELETE_RECORD", "a", "r_1"),
                 make_step("REPORT_REMAINING_RECORD", "a", "r_1", "evidence_01")]
        self.assertEqual([s.phase for s in sort_and_number(steps)], list(PHASES))

    def test_orders_operations_by_table_rank_within_a_phase(self):
        ops = ["ANONYMIZE_RECORD", "REDACT_RECORD", "DELETE_RECORD", "BLOCK_RESTORE", "DISABLE_DATA_FLOW"]
        # system ids ascend opposite to the rank order, so only the rank can produce the expected order.
        steps = [make_step(op, f"sys_{chr(ord('a') + i)}", "r_1") for i, op in enumerate(ops)]
        self.assertEqual([s.operation for s in sort_and_number(steps)], list(reversed(ops)))

    def test_orders_findings_by_rank_before_system_id(self):
        steps = [make_step("REPORT_ACTIVE_DATA_FLOW", "integration_service", "data_flow_9522", "evidence_03"),
                 make_step("REPORT_EXPOSED_PII", "support_platform", "support_864", "evidence_03"),
                 make_step("REPORT_EXPOSED_PII", "analytics_platform", "analytics_2034", "evidence_03"),
                 make_step("REPORT_REMAINING_RECORD", "search_service", "search_052", "evidence_03")]
        self.assertEqual([s.resource_id for s in sort_and_number(steps)],
                         ["search_052", "analytics_2034", "support_864", "data_flow_9522"])

    def test_orders_by_system_id_then_resource_id_within_an_operation(self):
        steps = [make_step("VERIFY_RECORD_ABSENT", "search_service", "search_4882"),
                 make_step("VERIFY_RECORD_ABSENT", "account_service", "account_5762"),
                 make_step("VERIFY_RECORD_ABSENT", "search_service", "search_052")]
        self.assertEqual([(s.system_id, s.resource_id) for s in sort_and_number(steps)],
                         [("account_service", "account_5762"), ("search_service", "search_052"),
                          ("search_service", "search_4882")])

    def test_resource_ids_sort_as_strings_per_the_spec_key(self):
        # Spec T9 key is (phase rank, op rank, system_id, resource_id): plain string order.
        steps = [make_step("DISABLE_DATA_FLOW", "integration_service", "data_flow_9522"),
                 make_step("DISABLE_DATA_FLOW", "integration_service", "data_flow_10007")]
        self.assertEqual([s.resource_id for s in sort_and_number(steps)], ["data_flow_10007", "data_flow_9522"])

    def test_numbers_steps_consecutively_from_one(self):
        steps = [replace(make_step("VERIFY_RECORD_ABSENT", "a", f"r_{i}"), step_no=99 - i) for i in range(4)]
        self.assertEqual([s.step_no for s in sort_and_number(steps)], [1, 2, 3, 4])

    def test_drops_exact_duplicates_even_with_different_step_numbers(self):
        a = make_step("DELETE_RECORD", "search_service", "search_052")
        out = sort_and_number([replace(a, step_no=3), a, replace(a, step_no=1)])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0], replace(a, step_no=1))

    def test_keeps_steps_that_differ_only_in_evidence(self):
        a = make_step("REPORT_REMAINING_RECORD", "search_service", "search_052", "evidence_03")
        b = replace(a, evidence_id="evidence_07")
        self.assertEqual(len(sort_and_number([a, b])), 2)

    def test_result_is_independent_of_input_order(self):
        base = [make_step("REPORT_EXPOSED_PII", "support_platform", "support_864", "evidence_03"),
                make_step("REDACT_RECORD", "support_platform", "support_864"),
                make_step("VERIFY_RECORD_REDACTED", "support_platform", "support_864"),
                make_step("VERIFY_RECORD_ABSENT", "account_service", "account_4248"),
                make_step("DISABLE_DATA_FLOW", "integration_service", "data_flow_9522")]
        expected = sort_and_number(base)
        for perm in itertools.permutations(base):
            self.assertEqual(sort_and_number(perm), expected)

    def test_is_idempotent(self):
        once = sort_and_number(incomplete_example()[::-1])
        self.assertEqual(sort_and_number(once), once)

    def test_empty_input_gives_empty_list(self):
        self.assertEqual(sort_and_number([]), [])

    def test_accepts_a_generator(self):
        out = sort_and_number(make_step("VERIFY_RECORD_ABSENT", "a", f"r_{i}") for i in range(3))
        self.assertEqual(len(out), 3)

    def test_unknown_operation_raises_value_error(self):
        bad = Step(0, "FINDING", "a", "r_1", "REPORT_SOMETHING", "PRESENT", "evidence_01")
        with self.assertRaises(ValueError):
            sort_and_number([bad])

    def test_output_passes_validate(self):
        status_steps = sort_and_number(incomplete_example()[::-1])
        self.assertEqual(validate("INCOMPLETE", status_steps), [])


class TestValidateHappyPaths(unittest.TestCase):
    def test_valid_complete_row_has_no_errors(self):
        self.assertEqual(validate("COMPLETE", complete_example()), [])

    def test_valid_incomplete_row_has_no_errors(self):
        self.assertEqual(validate("INCOMPLETE", incomplete_example()), [])

    def test_not_applicable_with_no_steps_is_valid(self):
        self.assertEqual(validate("NOT_APPLICABLE", []), [])

    def test_activity_and_audit_ids_are_valid_citations(self):
        steps = numbered(make_step("VERIFY_RECORD_ABSENT", "a", "r_1", "activity_4000"),
                         make_step("VERIFY_RECORD_PRESENT", "c", "r_3", "evidence_54"),
                         make_step("VERIFY_RECORD_REDACTED", "b", "r_2", "audit_282"))
        self.assertEqual(validate("COMPLETE", steps), [])

    def test_incomplete_may_mix_already_compliant_checks_with_blank_evidence(self):
        steps = numbered(
            make_step("REPORT_ACTIVE_DATA_FLOW", "integration_service", "data_flow_9522", "evidence_03"),
            make_step("DISABLE_DATA_FLOW", "integration_service", "data_flow_9522"),
            make_step("VERIFY_RECORD_ABSENT", "account_service", "account_4248"),
            make_step("VERIFY_DATA_FLOW_DISABLED", "integration_service", "data_flow_9522"))
        self.assertEqual(validate("INCOMPLETE", steps), [])


class TestValidateStatusRules(unittest.TestCase):
    def test_invalid_status_is_reported(self):
        for bad in ("complete", "PARTIALLY_COMPLETE", "", "DONE"):
            with self.subTest(bad=bad):
                self.assertTrue(validate(bad, complete_example()))

    def test_not_applicable_with_steps_is_reported(self):
        self.assertTrue(validate("NOT_APPLICABLE", complete_example()))

    def test_complete_with_a_finding_is_reported(self):
        steps = numbered(make_step("REPORT_REMAINING_RECORD", "a", "r_1", "evidence_01"),
                         make_step("VERIFY_RECORD_ABSENT", "a", "r_1", "evidence_01"))
        self.assertTrue(validate("COMPLETE", steps))

    def test_complete_with_a_remediation_is_reported(self):
        steps = numbered(make_step("DELETE_RECORD", "a", "r_1", "evidence_01"),
                         make_step("VERIFY_RECORD_ABSENT", "a", "r_1", "evidence_01"))
        self.assertTrue(validate("COMPLETE", steps))

    def test_complete_verification_without_evidence_is_reported(self):
        steps = numbered(make_step("VERIFY_RECORD_ABSENT", "a", "r_1", "evidence_01"),
                         make_step("VERIFY_RECORD_PRESENT", "b", "r_2", ""))
        errors = validate("COMPLETE", steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("step 2", errors[0])

    def test_complete_with_no_steps_is_reported(self):
        self.assertTrue(validate("COMPLETE", []))

    def test_incomplete_finding_without_evidence_is_reported(self):
        steps = incomplete_example()
        steps[0] = replace(steps[0], evidence_id="")
        self.assertTrue(validate("INCOMPLETE", steps))

    def test_incomplete_remediation_with_evidence_is_reported(self):
        steps = incomplete_example()
        steps[1] = replace(steps[1], evidence_id="evidence_03")
        errors = validate("INCOMPLETE", steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("step 2", errors[0])

    def test_incomplete_verification_with_evidence_is_reported(self):
        steps = incomplete_example()
        steps[2] = replace(steps[2], evidence_id="evidence_03")
        errors = validate("INCOMPLETE", steps)
        self.assertEqual(len(errors), 1)
        self.assertIn("step 3", errors[0])

    def test_incomplete_without_any_finding_is_reported(self):
        steps = numbered(make_step("DELETE_RECORD", "a", "r_1"), make_step("VERIFY_RECORD_ABSENT", "a", "r_1"))
        self.assertTrue(validate("INCOMPLETE", steps))


class TestValidateStepRules(unittest.TestCase):
    def test_operations_out_of_table_order_are_reported(self):
        steps = numbered(make_step("VERIFY_RECORD_PRESENT", "security_service", "audit_record_1", "evidence_1"),
                         make_step("VERIFY_RECORD_ABSENT", "account_service", "account_1", "evidence_1"))
        self.assertTrue(any("canonical order" in e for e in validate("COMPLETE", steps)))

    def test_same_operation_out_of_system_order_is_reported(self):
        steps = numbered(make_step("VERIFY_RECORD_ABSENT", "search_service", "search_1", "evidence_1"),
                         make_step("VERIFY_RECORD_ABSENT", "account_service", "account_1", "evidence_1"))
        self.assertTrue(any("canonical order" in e for e in validate("COMPLETE", steps)))

    def test_operation_with_wrong_state_is_reported(self):
        steps = incomplete_example()
        steps[1] = replace(steps[1], state="DELETED")
        self.assertTrue(validate("INCOMPLETE", steps))

    def test_operation_in_wrong_phase_is_reported(self):
        steps = incomplete_example()
        steps[2] = replace(steps[2], phase="REMEDIATION")
        self.assertTrue(validate("INCOMPLETE", steps))

    def test_unknown_operation_is_reported(self):
        steps = incomplete_example()
        steps[1] = replace(steps[1], operation="PURGE_RECORD")
        self.assertTrue(validate("INCOMPLETE", steps))

    def test_unknown_phase_is_reported(self):
        steps = incomplete_example()
        steps[2] = replace(steps[2], phase="CHECK")
        self.assertTrue(validate("INCOMPLETE", steps))

    def test_phase_order_violation_is_reported(self):
        f, r, v = incomplete_example()
        self.assertTrue(validate("INCOMPLETE", numbered(f, v, r)))

    def test_step_numbers_must_start_at_one(self):
        steps = [replace(s, step_no=s.step_no - 1) for s in incomplete_example()]
        self.assertTrue(validate("INCOMPLETE", steps))

    def test_step_numbers_must_be_consecutive(self):
        steps = incomplete_example()
        steps[2] = replace(steps[2], step_no=4)
        self.assertTrue(validate("INCOMPLETE", steps))

    def test_step_number_must_be_an_int_not_bool_or_string(self):
        for bad in (True, "1", 1.0):
            with self.subTest(bad=bad):
                steps = complete_example()
                steps[0] = replace(steps[0], step_no=bad)
                self.assertTrue(validate("COMPLETE", steps))

    def test_duplicate_step_is_reported(self):
        a = make_step("VERIFY_RECORD_ABSENT", "a", "r_1", "evidence_01")
        self.assertTrue(validate("COMPLETE", numbered(a, a)))

    def test_malformed_evidence_id_is_reported(self):
        for bad in ("file_01", "evidence_", "Evidence_01", "evidence_01 ", "case_01"):
            with self.subTest(bad=bad):
                steps = numbered(make_step("VERIFY_RECORD_ABSENT", "a", "r_1", bad))
                self.assertTrue(validate("COMPLETE", steps))

    def test_empty_system_or_resource_id_is_reported(self):
        self.assertTrue(validate("COMPLETE", numbered(make_step("VERIFY_RECORD_ABSENT", "", "r_1", "evidence_01"))))
        self.assertTrue(validate("COMPLETE", numbered(make_step("VERIFY_RECORD_ABSENT", "a", "", "evidence_01"))))

    def test_non_string_field_is_reported_without_crashing(self):
        bad = Step(1, "VERIFICATION", "a", "r_1", "VERIFY_RECORD_ABSENT", "ABSENT", None)
        self.assertTrue(validate("COMPLETE", [bad]))


class TestToJson(unittest.TestCase):
    def test_to_json_uses_schema_key_order_and_compact_separators(self):
        step = Step(1, "FINDING", "account_service", "account_005", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_01")
        self.assertEqual(to_json([step]),
                         '[{"step_no":1,"phase":"FINDING","system_id":"account_service","resource_id":"account_005",'
                         '"operation":"REPORT_REMAINING_RECORD","state":"PRESENT","evidence_id":"evidence_01"}]')

    def test_to_json_key_order_is_keys(self):
        objs = json.loads(to_json(complete_example()))
        for obj in objs:
            self.assertEqual(tuple(obj), KEYS)

    def test_to_json_has_no_whitespace_between_tokens(self):
        text = to_json(incomplete_example())
        self.assertNotIn(", ", text)
        self.assertNotIn(": ", text)
        self.assertNotIn("\n", text)

    def test_to_json_keeps_non_ascii_characters(self):
        step = Step(1, "VERIFICATION", "système", "réf_1", "VERIFY_RECORD_ABSENT", "ABSENT", "evidence_01")
        text = to_json([step])
        self.assertIn("système", text)
        self.assertNotIn("\\u", text)

    def test_to_json_keeps_empty_evidence_as_empty_string(self):
        self.assertIn('"evidence_id":""', to_json(incomplete_example()))

    def test_to_json_of_no_steps_is_empty_array(self):
        self.assertEqual(to_json([]), "[]")


class TestFromJson(unittest.TestCase):
    def test_from_json_round_trips_to_json(self):
        steps = incomplete_example()
        self.assertEqual(from_json(to_json(steps)), steps)

    def test_from_json_accepts_any_key_order(self):
        text = ('[{"evidence_id":"evidence_54","operation":"VERIFY_RECORD_ABSENT","phase":"VERIFICATION",'
                '"resource_id":"account_001","state":"ABSENT","step_no":1,"system_id":"account_service"}]')
        self.assertEqual(from_json(text), [Step(1, "VERIFICATION", "account_service", "account_001",
                                                "VERIFY_RECORD_ABSENT", "ABSENT", "evidence_54")])

    def test_from_json_accepts_whitespace(self):
        self.assertEqual(from_json(json.dumps(json.loads(to_json(complete_example())), indent=2)),
                         complete_example())

    def test_from_json_of_empty_array_is_empty_list(self):
        self.assertEqual(from_json("[]"), [])

    def test_from_json_rejects_invalid_json(self):
        with self.assertRaises(ValueError):
            from_json("[{")

    def test_from_json_rejects_a_non_array(self):
        with self.assertRaises(ValueError):
            from_json('{"step_no":1}')

    def test_from_json_rejects_a_missing_key(self):
        obj = json.loads(to_json(complete_example()))[0]
        del obj["evidence_id"]
        with self.assertRaises(ValueError):
            from_json(json.dumps([obj]))

    def test_from_json_rejects_an_extra_key(self):
        obj = json.loads(to_json(complete_example()))[0]
        obj["note"] = "x"
        with self.assertRaises(ValueError):
            from_json(json.dumps([obj]))

    def test_from_json_rejects_a_non_object_element(self):
        with self.assertRaises(ValueError):
            from_json("[1]")


@unittest.skipUnless(DATASET.is_dir(), "dataset not available")
class TestStepsOnGoldSample(unittest.TestCase):
    """tools_spec T9 acceptance: re-sorting and re-serialising every gold sample row."""

    @classmethod
    def setUpClass(cls):
        with open(DATASET / "sample_output.csv", encoding="utf-8", newline="") as fh:
            cls.rows = list(csv.DictReader(fh))

    def test_gold_rows_round_trip_json_equal_32_of_32(self):
        self.assertEqual(len(self.rows), 32)
        for row in self.rows:
            with self.subTest(case_id=row["case_id"]):
                out = to_json(sort_and_number(from_json(row["steps_json"])))
                self.assertEqual(json.loads(out), json.loads(row["steps_json"]))

    def test_schema_ordered_gold_rows_round_trip_byte_identical_24(self):
        schema_rows = [r for r in self.rows if tuple(json.loads(r["steps_json"])[0]) == KEYS]
        self.assertEqual(len(schema_rows), 24)
        for row in schema_rows:
            with self.subTest(case_id=row["case_id"]):
                self.assertEqual(to_json(sort_and_number(from_json(row["steps_json"]))), row["steps_json"])

    def test_every_gold_row_passes_validate(self):
        for row in self.rows:
            with self.subTest(case_id=row["case_id"]):
                self.assertEqual(validate(row["deletion_status"], from_json(row["steps_json"])), [])


if __name__ == "__main__":
    unittest.main()
