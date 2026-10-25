"""Integration: identity -> parsers -> evidence index -> planner -> steps, on the real dataset.

These tests prove the tools compose (tools_spec.md T4, T6, T7, T9, T10). The small
resource -> kind mapping in the case_11 test is written inside the test on purpose: choosing
kinds and resources is the case engine's job, not a tool's.
"""

from __future__ import annotations

import csv
import json
import unittest

from ghostaudit import loaders, paths
from ghostaudit.activity import ActivityLog
from ghostaudit.evidence_index import EvidenceIndex
from ghostaudit.identity import IdentityIndex
from ghostaudit.normalize import split_resource_id
from ghostaudit.planner import assemble, plan
from ghostaudit.steps import to_json, validate
from ghostaudit.timeutil import parse_ts

_DATA = paths.dataset_dir()
DATASET_OK = all((_DATA / name).is_file() for name in (
    "evidence_files.csv", "user_identities.csv", "user_audit_log.csv", "system_activity.csv",
    "sample_deletion_requests.csv", "sample_output.csv"))

NOVAK_EMAIL = "novak.tomasz@icloud.com"
NOVAK_CUTOFF = "2026-10-01T12:00:00Z"
NOVAK_STATES = {
    "search_052": "FOUND",
    "analytics_2034": "IDENTIFIABLE",
    "support_864": "IDENTIFIABLE",
    "data_flow_9522": "ACTIVE",
    "audit_record_9394": "FOUND",
    "account_4248": "NOT_FOUND",
}

_SHARED: dict = {}


def shared() -> dict:
    """Build the expensive real-data objects once per test run."""
    if not _SHARED:
        _SHARED["index"] = EvidenceIndex.build()
        _SHARED["identities"] = IdentityIndex(loaders.load_identities(), loaders.load_audit_log())
        _SHARED["activity"] = ActivityLog(loaders.load_activity())
        _SHARED["samples"] = loaders.load_requests("sample_deletion_requests.csv")
        with open(_DATA / "sample_output.csv", encoding="utf-8", newline="") as fh:
            _SHARED["gold"] = {row["case_id"]: row for row in csv.DictReader(fh)}
    return _SHARED


@unittest.skipUnless(DATASET_OK, "participant dataset not present")
class IntegrationTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        data = shared()
        cls.index = data["index"]
        cls.identities = data["identities"]
        cls.activity = data["activity"]
        cls.samples = data["samples"]
        cls.requests = {r.case_id: r for r in cls.samples}
        cls.gold = data["gold"]

    # (a) identity -> evidence index ------------------------------------------------------

    def test_novak_resolves_and_links_exactly_six_resources_with_expected_states(self):
        subject = self.identities.resolve(NOVAK_EMAIL)
        self.assertIsNotNone(subject)
        self.assertEqual(subject.user_id, "user_32")
        matches = self.index.for_subject(subject, NOVAK_CUTOFF)
        self.assertEqual({m.observation.resource_id for m in matches}, set(NOVAK_STATES))
        for rid, state in NOVAK_STATES.items():
            with self.subTest(resource=rid):
                self.assertEqual(self.index.state_at(rid, NOVAK_CUTOFF).state, state)

    # (b) identity -> index -> planner -> assemble -> steps JSON equals gold ----------------

    def test_case_11_composes_into_the_gold_row(self):
        request = self.requests["case_11"]
        self.assertEqual(request.email, NOVAK_EMAIL)
        cutoff = request.audit_at
        # Written here, not in product code: the policy kind for each resource in this case.
        kind_by_prefix = {"search": "DELETE", "analytics": "ANONYMIZE", "support": "REDACT",
                          "data_flow": "DATA_FLOW", "account": "DELETE", "audit_record": "RETAIN"}
        subject = self.identities.resolve(request.email)
        resources = sorted({m.observation.resource_id for m in self.index.for_subject(subject, cutoff)})
        plans = []
        for rid in resources:
            current = self.index.state_at(rid, cutoff)
            kind = kind_by_prefix[split_resource_id(rid)[0]]
            plans.append(plan(current.system_id, rid, kind, current.state, self.index.citable(rid, cutoff)))
        status, steps = assemble(plans)

        gold = self.gold["case_11"]
        gold_steps = json.loads(gold["steps_json"])
        self.assertEqual(status, gold["deletion_status"])
        self.assertEqual(status, "INCOMPLETE")
        self.assertEqual(len(steps), 14)
        self.assertEqual(validate(status, steps), [])
        self.assertEqual(json.loads(to_json(steps)), gold_steps)
        self.assertEqual({s.evidence_id for s in steps if s.phase == "FINDING"}, {"evidence_03"})

    # (c) citation agreement with gold ------------------------------------------------------

    def _gold_citations(self):
        for case_id in sorted(self.gold, key=lambda c: int(c.split("_")[1])):
            cutoff = self.requests[case_id].audit_at
            for step in json.loads(self.gold[case_id]["steps_json"]):
                if step["evidence_id"]:
                    yield case_id, cutoff, step

    def _tool_citation(self, step, cutoff):
        rid = step["resource_id"]
        if step["operation"] == "VERIFY_RECORD_ABSENT" and not self.index.history(rid, cutoff):
            hit = self.index.absent_in(rid, step["system_id"], cutoff)
            return hit.file_id if hit else None
        return self.index.citable(rid, cutoff)

    def test_tool_citations_agree_with_gold_for_at_least_74_of_75_steps(self):
        cited = list(self._gold_citations())
        self.assertEqual(len(cited), 75)
        disagreements = [(case_id, step["resource_id"], step["evidence_id"], self._tool_citation(step, cutoff))
                         for case_id, cutoff, step in cited
                         if self._tool_citation(step, cutoff) != step["evidence_id"]]
        # Known disagreement (an engine-level choice, not a tool bug): case_04 account_004 is proven
        # absent by gold with evidence_54, while absent_in() picks the newer full listing evidence_90.
        self.assertGreaterEqual(75 - len(disagreements), 74, disagreements)

    def test_citable_matches_gold_whenever_the_gold_file_names_the_resource(self):
        checked = 0
        for case_id, cutoff, step in self._gold_citations():
            rid, gold_file = step["resource_id"], step["evidence_id"]
            if not any(o.resource_id == rid for o in self.index.in_file(gold_file)):
                continue  # absence proofs (full listings) are covered by the test above
            checked += 1
            with self.subTest(case=case_id, resource=rid):
                self.assertEqual(self.index.citable(rid, cutoff), gold_file)
        self.assertGreaterEqual(checked, 70)

    def test_no_gold_citation_or_tool_citation_is_captured_after_audit_at(self):
        captured = {m.file_id: parse_ts(m.captured_at) for m in loaders.load_manifest()}
        for case_id, cutoff, step in self._gold_citations():
            tool = self._tool_citation(step, cutoff)
            with self.subTest(case=case_id, resource=step["resource_id"]):
                self.assertLessEqual(captured[step["evidence_id"]], parse_ts(cutoff))
                if tool is not None:
                    self.assertLessEqual(captured[tool], parse_ts(cutoff))

    # (d) the time filter on real data ------------------------------------------------------

    def test_for_subject_never_returns_observations_after_audit_at(self):
        checked = 0
        for request in self.samples[:20]:
            subject = self.identities.resolve(request.email)
            self.assertIsNotNone(subject, request.case_id)
            cut = parse_ts(request.audit_at)
            links = self.activity.links(until=request.audit_at)
            for strict in (False, True):
                matches = self.index.for_subject(subject, request.audit_at, links, strict=strict)
                checked += len(matches)
                with self.subTest(case=request.case_id, strict=strict):
                    late = [(m.observation.file_id, m.observation.resource_id, m.observation.observed_at)
                            for m in matches if parse_ts(m.observation.observed_at) > cut]
                    self.assertEqual(late, [])
                    if strict:
                        self.assertTrue(all(parse_ts(m.observation.captured_at) <= cut for m in matches))
        self.assertGreater(checked, 0)

    def test_state_at_and_history_respect_audit_at_for_linked_resources(self):
        for request in self.samples[:20]:
            subject = self.identities.resolve(request.email)
            cut = parse_ts(request.audit_at)
            for rid in {m.observation.resource_id for m in self.index.for_subject(subject, request.audit_at)}:
                with self.subTest(case=request.case_id, resource=rid):
                    history = self.index.history(rid, request.audit_at)
                    self.assertTrue(history)
                    self.assertTrue(all(parse_ts(o.observed_at) <= cut for o in history))
                    self.assertIs(self.index.state_at(rid, request.audit_at), history[-1])


if __name__ == "__main__":
    unittest.main()
