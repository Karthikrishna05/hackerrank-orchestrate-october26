"""Tests for ghostaudit.evidence_index (tools_spec.md T7), on small synthetic indexes."""

from __future__ import annotations

import random
import unittest
from pathlib import Path

from ghostaudit.evidence_index import (
    REASONS,
    EvidenceIndex,
    SubjectMatch,
    is_resource_id,
    natural_key,
)
from ghostaudit.identity import IdentityIndex
from ghostaudit.models import (
    AuditChange,
    Coverage,
    ManifestEntry,
    Observation,
    SubjectProfile,
    UserIdentity,
)
from ghostaudit.normalize import email_hash, system_for
from ghostaudit.timeutil import parse_ts

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "index"

CUT = "2026-09-01T12:00:00Z"
EARLY = "2026-08-01T00:00:00Z"
BEFORE = "2026-09-01T11:00:00Z"
AFTER = "2026-09-01T13:00:00Z"
LATE = "2026-12-31T00:00:00Z"

SUBJECT_EMAIL = "ana.subject@example.com"
OTHER_EMAIL = "bo.other@example.com"


def obs(resource_id: str = "account_1", *, file_id: str = "evidence_01", observed_at: str = BEFORE,
        captured_at: str | None = None, state: str = "FOUND", identifiers=(), store: str | None = None,
        source: str = "test:1", fields: dict | None = None, system_id: str | None = None) -> Observation:
    """A synthetic observation; captured_at defaults to observed_at."""
    return Observation(
        file_id=file_id,
        captured_at=captured_at or observed_at,
        observed_at=observed_at,
        system_id=system_id or system_for(resource_id) or "unknown_service",
        resource_id=resource_id,
        state=state,
        category=None,
        identifiers=frozenset(identifiers),
        store=store,
        source=source,
        fields=dict(fields or {}),
    )


def cov(file_id: str, system_id: str, observed_at: str, captured_at: str | None = None,
        store: str | None = None, collection: str = "customer_profiles") -> Coverage:
    """A synthetic full-collection coverage."""
    return Coverage(file_id=file_id, captured_at=captured_at or observed_at, observed_at=observed_at,
                    system_id=system_id, store=store, collection=collection)


def profile(user_id: str = "user_1", emails=(SUBJECT_EMAIL,), phones=(), hashes=(), names=()) -> SubjectProfile:
    return SubjectProfile(user_id=user_id, names=set(names), emails=set(emails), phones=set(phones),
                          email_hashes=set(hashes))


def index(observations=(), coverages=(), manifest=()) -> EvidenceIndex:
    return EvidenceIndex(list(observations), list(coverages), list(manifest))


def matched(matches) -> dict[tuple[str, str, str], str]:
    """{(file_id, resource_id, source): reason} for easy assertions."""
    return {(m.observation.file_id, m.observation.resource_id, m.observation.source): m.reason for m in matches}


# --- The time cut-off ---

class TimeCutoffTest(unittest.TestCase):

    def test_history_excludes_observations_after_cutoff(self):
        idx = index([obs(observed_at=BEFORE, source="a"), obs(observed_at=AFTER, source="b")])
        self.assertEqual([o.source for o in idx.history("account_1", CUT)], ["a"])

    def test_observation_exactly_at_cutoff_counts_as_before(self):
        at_cut = obs(observed_at=CUT, identifiers={SUBJECT_EMAIL})
        idx = index([at_cut])
        self.assertEqual(idx.history("account_1", CUT), [at_cut])
        self.assertIs(idx.state_at("account_1", CUT), at_cut)
        self.assertEqual([m.observation for m in idx.for_subject(profile(), CUT)], [at_cut])
        self.assertEqual(idx.states_by_store("account_1", CUT), {"-": at_cut})

    def test_state_at_ignores_a_later_contradicting_observation(self):
        idx = index([obs(observed_at=BEFORE, state="FOUND"), obs(observed_at=AFTER, state="NOT_FOUND")])
        self.assertEqual(idx.state_at("account_1", CUT).state, "FOUND")
        self.assertEqual(idx.state_at("account_1", LATE).state, "NOT_FOUND")

    def test_state_at_is_none_when_every_observation_is_later(self):
        idx = index([obs(observed_at=AFTER)])
        self.assertIsNone(idx.state_at("account_1", CUT))
        self.assertEqual(idx.history("account_1", CUT), [])

    def test_cutoff_is_compared_as_datetime_not_as_text(self):
        # 13:30+02:00 is 11:30Z: before the cutoff although the text sorts after it.
        offset = obs(observed_at="2026-09-01T13:30:00+02:00", source="offset")
        idx = index([offset])
        self.assertEqual(idx.history("account_1", CUT), [offset])
        for same_cut in ("2026-09-01T12:00:00+00:00", "2026-09-01 12:00:00", "2026-09-01T14:00:00+02:00"):
            with self.subTest(cutoff=same_cut):
                self.assertEqual(idx.history("account_1", same_cut), [offset])
        self.assertEqual(idx.history("account_1", "2026-09-01T11:29:59Z"), [])

    def test_no_method_ever_returns_an_observation_after_the_cutoff(self):
        rng = random.Random(20261025)
        resources = [f"account_{n}" for n in range(1, 6)] + ["search_7", "data_flow_8"]
        stores = [None, "primary", "read_replica"]
        observations, coverages = [], []
        for n in range(120):
            day, hour = rng.randint(1, 30), rng.randint(0, 23)
            observed = f"2026-09-{day:02d}T{hour:02d}:00:00Z"
            captured = f"2026-09-{min(30, day + rng.randint(0, 3)):02d}T{hour:02d}:00:00Z"
            rid = rng.choice(resources)
            observations.append(obs(
                rid, file_id=f"evidence_{n % 17 + 1}", observed_at=observed, captured_at=captured,
                identifiers={SUBJECT_EMAIL} if rng.random() < 0.3 else {OTHER_EMAIL},
                store=rng.choice(stores), source=f"row {n}",
                fields={"source_resource_id": rng.choice(resources)} if rng.random() < 0.2 else {}))
            if n % 10 == 0:
                coverages.append(cov(f"evidence_{n % 17 + 1}", system_for(rid), observed, captured))
        idx = index(observations, coverages)
        subject = profile()
        for day in (3, 10, 17, 24):
            cutoff = f"2026-09-{day:02d}T12:00:00Z"
            cut = parse_ts(cutoff)
            returned = [m.observation for m in idx.for_subject(subject, cutoff)]
            returned += [m.observation for m in idx.for_subject(subject, cutoff, strict=True)]
            returned += idx.with_token(SUBJECT_EMAIL, cutoff)
            for rid in resources:
                returned += idx.history(rid, cutoff) + idx.history(rid, cutoff, strict=True)
                returned += [o for o in (idx.state_at(rid, cutoff),) if o is not None]
                returned += list(idx.states_by_store(rid, cutoff).values())
            with self.subTest(cutoff=cutoff):
                self.assertTrue(returned, "the scenario should return something at this cutoff")
                self.assertEqual([o for o in returned if parse_ts(o.observed_at) > cut], [])
                for system_id in {system_for(r) for r in resources}:
                    self.assertTrue(all(parse_ts(c.observed_at) <= cut for c in idx.coverage(system_id, cutoff)))
                    for rid in resources:
                        hit = idx.absent_in(rid, system_id, cutoff)
                        if hit is not None:
                            self.assertLessEqual(parse_ts(hit.observed_at), cut)
                            self.assertLessEqual(parse_ts(hit.captured_at), cut)

    def test_default_mode_keeps_an_early_line_from_a_file_captured_after_cutoff(self):
        late_file = obs(observed_at=BEFORE, captured_at=AFTER, identifiers={SUBJECT_EMAIL})
        idx = index([late_file])
        self.assertEqual(idx.history("account_1", CUT), [late_file])
        self.assertEqual(len(idx.for_subject(profile(), CUT)), 1)

    def test_strict_drops_files_captured_after_cutoff_from_every_method(self):
        on_time = obs(file_id="evidence_01", observed_at=EARLY, captured_at=EARLY, store="primary",
                      identifiers={SUBJECT_EMAIL}, source="on time")
        late_file = obs(file_id="evidence_02", observed_at=BEFORE, captured_at=AFTER, store="read_replica",
                        identifiers={SUBJECT_EMAIL}, source="late file")
        idx = index([on_time, late_file])
        self.assertEqual(idx.history("account_1", CUT, strict=True), [on_time])
        self.assertIs(idx.state_at("account_1", CUT, strict=True), on_time)
        self.assertEqual(idx.states_by_store("account_1", CUT, strict=True), {"primary": on_time})
        self.assertEqual([m.observation for m in idx.for_subject(profile(), CUT, strict=True)], [on_time])

    def test_strict_keeps_a_file_captured_exactly_at_cutoff(self):
        at_cut = obs(observed_at=BEFORE, captured_at=CUT)
        self.assertEqual(index([at_cut]).history("account_1", CUT, strict=True), [at_cut])

    def test_strict_uses_the_manifest_capture_time_over_the_observation_copy(self):
        # The observation claims an early capture but the manifest says the file came later.
        o = obs(file_id="evidence_05", observed_at=BEFORE, captured_at=EARLY)
        manifest = [ManifestEntry("evidence_05", "records/x.json", "test", AFTER)]
        idx = index([o], manifest=manifest)
        self.assertEqual(idx.history("account_1", CUT, strict=True), [])
        self.assertEqual(idx.history("account_1", CUT), [o])


# --- citable(): the manifest captured_at decides which file may be cited ---

class CitableTest(unittest.TestCase):

    def test_picks_the_latest_file_captured_at_or_before_cutoff(self):
        idx = index([
            obs(file_id="evidence_01", observed_at="2026-08-01T00:00:00Z"),
            obs(file_id="evidence_02", observed_at="2026-08-15T00:00:00Z"),
            obs(file_id="evidence_03", observed_at="2026-09-05T00:00:00Z"),
        ])
        self.assertEqual(idx.citable("account_1", CUT), "evidence_02")
        self.assertEqual(idx.citable("account_1", LATE), "evidence_03")

    def test_ignores_a_later_captured_file_even_when_its_line_time_is_earlier(self):
        idx = index([
            obs(file_id="evidence_01", observed_at="2026-08-20T00:00:00Z", captured_at="2026-08-20T00:00:00Z"),
            obs(file_id="evidence_02", observed_at="2026-08-25T00:00:00Z", captured_at=AFTER),
        ])
        self.assertEqual(idx.citable("account_1", CUT), "evidence_01")

    def test_prefers_latest_capture_over_latest_line_time(self):
        # evidence_02 holds the newer line but evidence_01 is the newer file.
        idx = index([
            obs(file_id="evidence_01", observed_at="2026-08-01T00:00:00Z", captured_at="2026-08-30T00:00:00Z"),
            obs(file_id="evidence_02", observed_at="2026-08-20T00:00:00Z", captured_at="2026-08-21T00:00:00Z"),
        ])
        self.assertEqual(idx.citable("account_1", CUT), "evidence_01")

    def test_file_captured_exactly_at_cutoff_is_citable(self):
        idx = index([obs(file_id="evidence_03", observed_at=BEFORE, captured_at=CUT)])
        self.assertEqual(idx.citable("account_1", CUT), "evidence_03")

    def test_uses_the_manifest_capture_time(self):
        o = obs(file_id="evidence_05", observed_at=BEFORE, captured_at=EARLY)
        idx = index([o], manifest=[ManifestEntry("evidence_05", "records/x.json", "test", AFTER)])
        self.assertIsNone(idx.citable("account_1", CUT))
        self.assertEqual(idx.citable("account_1", LATE), "evidence_05")

    def test_returns_none_when_no_file_qualifies(self):
        idx = index([obs(file_id="evidence_01", observed_at=BEFORE, captured_at=AFTER)])
        self.assertIsNone(idx.citable("account_1", CUT))

    def test_returns_none_for_an_unknown_resource(self):
        self.assertIsNone(index([obs("account_1")]).citable("account_2", LATE))

    def test_only_files_mentioning_the_resource_are_considered(self):
        idx = index([obs("account_1", file_id="evidence_01", observed_at=EARLY),
                     obs("account_2", file_id="evidence_02", observed_at=BEFORE)])
        self.assertEqual(idx.citable("account_1", CUT), "evidence_01")

    def test_capture_time_tie_is_broken_deterministically_by_natural_file_order(self):
        rows = [obs(file_id=f, observed_at=BEFORE, source=f) for f in ("evidence_9", "evidence_10", "evidence_2")]
        answers = set()
        for seed in range(6):
            shuffled = rows[:]
            random.Random(seed).shuffle(shuffled)
            answers.add(index(shuffled).citable("account_1", CUT))
        # Natural order: evidence_10 is the highest (a plain string max would pick evidence_9).
        self.assertEqual(answers, {"evidence_10"})


# --- for_subject(): who the evidence is about ---

class ForSubjectTest(unittest.TestCase):

    def test_direct_email_match_has_reason_identifier(self):
        o = obs("account_1", identifiers={SUBJECT_EMAIL})
        result = index([o]).for_subject(profile(), CUT)
        self.assertEqual(result, [SubjectMatch(o, "identifier")])

    def test_every_kind_of_strong_identifier_links_directly(self):
        subject = profile(user_id="user_7", emails={SUBJECT_EMAIL}, phones={"+4915112345678"},
                          hashes={email_hash(SUBJECT_EMAIL)})
        for token in ("user_7", SUBJECT_EMAIL, "+4915112345678", email_hash(SUBJECT_EMAIL)):
            with self.subTest(token=token):
                result = index([obs(identifiers={token})]).for_subject(subject, CUT)
                self.assertEqual([m.reason for m in result], ["identifier"])

    def test_name_only_match_never_links(self):
        subject = profile(names={"name:ana subject"})
        idx = index([obs(identifiers={"name:ana subject"})])
        self.assertEqual(idx.for_subject(subject, CUT), [])

    def test_shared_name_with_another_persons_email_does_not_link(self):
        subject = profile(names={"name:ana subject"})
        idx = index([obs(identifiers={"name:ana subject", OTHER_EMAIL})])
        self.assertEqual(idx.for_subject(subject, CUT), [])

    def test_lookalike_identifiers_do_not_link(self):
        subject = profile(user_id="user_1", emails={"smartinez6461@example.com"})
        idx = index([
            obs("account_1", identifiers={"smartinez6461.jr@example.com"}),
            obs("account_2", identifiers={"user_10"}),
            obs("account_3", identifiers={"xsmartinez6461@example.com"}),
        ])
        self.assertEqual(idx.for_subject(subject, CUT), [])

    def test_hash_of_a_historical_email_links(self):
        old, new = "ana.old@example.com", "ana.new@example.com"
        identities = IdentityIndex(
            [UserIdentity("user_5", "Ana Subject", new, "+15550000005", email_hash(new), EARLY)],
            [AuditChange("audit_1", "user_5", EARLY, "email", old, new)])
        subject = identities.resolve(old)
        self.assertIsNotNone(subject)
        hashed = obs("analytics_9", identifiers={email_hash(old)})
        self.assertEqual(index([hashed]).for_subject(subject, CUT), [SubjectMatch(hashed, "identifier")])

    def test_resource_link_adds_other_observations_of_a_matched_resource(self):
        direct = obs("account_1", file_id="evidence_01", identifiers={SUBJECT_EMAIL}, source="direct")
        replica = obs("account_1", file_id="evidence_02", store="read_replica", source="replica")
        result = matched(index([direct, replica]).for_subject(profile(), CUT))
        self.assertEqual(result, {("evidence_01", "account_1", "direct"): "identifier",
                                  ("evidence_02", "account_1", "replica"): "resource_link"})

    def test_whole_field_value_equal_to_a_resource_id_is_a_field_reference(self):
        direct = obs("account_1", identifiers={SUBJECT_EMAIL}, source="direct")
        copy = obs("search_4", file_id="evidence_02", fields={"source_resource_id": "account_1"}, source="copy")
        result = matched(index([direct, copy]).for_subject(profile(), CUT))
        self.assertEqual(result[("evidence_02", "search_4", "copy")], "field_reference")

    def test_field_reference_is_found_inside_nested_fields(self):
        direct = obs("account_1", identifiers={SUBJECT_EMAIL})
        nested = obs("support_3", file_id="evidence_02",
                     fields={"links": [{"kind": "origin", "target": " account_1 "}], "notes": None})
        result = index([direct, nested]).for_subject(profile(), CUT)
        self.assertIn(SubjectMatch(nested, "field_reference"), result)

    def test_resource_id_inside_free_text_is_not_a_field_reference(self):
        direct = obs("account_1", identifiers={SUBJECT_EMAIL})
        prose = obs("search_4", file_id="evidence_02", fields={"note": "copied from account_1 yesterday"})
        self.assertEqual([m.observation for m in index([direct, prose]).for_subject(profile(), CUT)], [direct])

    def test_linkage_stops_after_one_hop(self):
        direct = obs("account_1", identifiers={SUBJECT_EMAIL}, source="direct")
        # Same resource, but carries someone else's email: linked by resource, yet that email must not spread.
        shared = obs("account_1", file_id="evidence_02", identifiers={OTHER_EMAIL}, source="shared")
        other_persons = obs("search_9", file_id="evidence_03", identifiers={OTHER_EMAIL}, source="two hops")
        referencing = obs("support_5", file_id="evidence_04", fields={"origin": "account_1"}, source="ref")
        same_as_ref = obs("support_5", file_id="evidence_05", source="ref sibling")
        ref_of_ref = obs("export_6", file_id="evidence_06", fields={"origin": "support_5"}, source="ref of ref")
        idx = index([direct, shared, other_persons, referencing, same_as_ref, ref_of_ref])
        result = matched(idx.for_subject(profile(), CUT))
        self.assertEqual(result, {
            ("evidence_01", "account_1", "direct"): "identifier",
            ("evidence_02", "account_1", "shared"): "resource_link",
            ("evidence_04", "support_5", "ref"): "field_reference",
        })

    def test_extra_link_marks_a_resource_as_matched(self):
        archive = obs("backup_7", source="archive row")
        result = index([archive]).for_subject(profile(), CUT, extra_links={"backup_7": {SUBJECT_EMAIL, "user_99"}})
        self.assertEqual(result, [SubjectMatch(archive, "extra_link")])

    def test_extra_link_tokens_are_compared_case_insensitively(self):
        archive = obs("backup_7")
        result = index([archive]).for_subject(profile(), CUT, extra_links={"backup_7": [" ANA.SUBJECT@EXAMPLE.COM "]})
        self.assertEqual([m.reason for m in result], ["extra_link"])

    def test_extra_link_needs_a_strong_token_of_the_subject(self):
        subject = profile(names={"name:ana subject"})
        idx = index([obs("backup_7")])
        for tokens in ({OTHER_EMAIL}, {"name:ana subject"}, set()):
            with self.subTest(tokens=tokens):
                self.assertEqual(idx.for_subject(subject, CUT, extra_links={"backup_7": tokens}), [])

    def test_extra_link_resource_also_gets_field_references(self):
        archive = obs("backup_7")
        restored = obs("search_8", file_id="evidence_02", fields={"restored_from": "backup_7"})
        result = matched(index([archive, restored]).for_subject(
            profile(), CUT, extra_links={"backup_7": {SUBJECT_EMAIL}}))
        self.assertEqual(sorted(result.values()), ["extra_link", "field_reference"])

    def test_identifier_reason_wins_over_weaker_reasons(self):
        o = obs("account_1", identifiers={SUBJECT_EMAIL}, fields={"source_resource_id": "account_1"})
        result = index([o]).for_subject(profile(), CUT, extra_links={"account_1": {SUBJECT_EMAIL}})
        self.assertEqual(result, [SubjectMatch(o, "identifier")])

    def test_direct_match_seen_only_after_cutoff_does_not_create_a_link(self):
        anonymous_before = obs("account_1", observed_at=BEFORE, source="before")
        named_after = obs("account_1", observed_at=AFTER, identifiers={SUBJECT_EMAIL}, source="after")
        idx = index([anonymous_before, named_after])
        self.assertEqual(idx.for_subject(profile(), CUT), [])
        self.assertEqual(len(idx.for_subject(profile(), LATE)), 2)

    def test_unrelated_observations_are_excluded(self):
        mine = obs("account_1", identifiers={SUBJECT_EMAIL})
        idx = index([mine, obs("account_2", identifiers={OTHER_EMAIL}), obs("search_3"),
                     obs("support_4", fields={"source_resource_id": "account_2"})])
        self.assertEqual([m.observation for m in idx.for_subject(profile(), CUT)], [mine])

    def test_results_are_in_history_order_and_independent_of_input_order(self):
        rows = [
            obs("account_1", file_id="evidence_3", observed_at="2026-08-03T00:00:00Z", identifiers={SUBJECT_EMAIL}),
            obs("account_1", file_id="evidence_1", observed_at="2026-08-01T00:00:00Z"),
            obs("search_2", file_id="evidence_2", observed_at="2026-08-02T00:00:00Z",
                fields={"source_resource_id": "account_1"}),
            obs("analytics_4", file_id="evidence_10", observed_at="2026-08-02T00:00:00Z", identifiers={"user_1"}),
            obs("analytics_4", file_id="evidence_9", observed_at="2026-08-02T00:00:00Z"),
        ]
        expected = None
        for seed in range(8):
            shuffled = rows[:]
            random.Random(seed).shuffle(shuffled)
            got = [(m.observation.file_id, m.reason) for m in index(shuffled).for_subject(profile(), CUT)]
            if expected is None:
                expected = got
            self.assertEqual(got, expected)
        self.assertEqual(expected, [("evidence_1", "resource_link"), ("evidence_2", "field_reference"),
                                    ("evidence_9", "resource_link"), ("evidence_10", "identifier"),
                                    ("evidence_3", "identifier")])

    def test_returns_named_subject_matches_with_known_reasons(self):
        self.assertEqual(set(REASONS), {"identifier", "resource_link", "extra_link", "field_reference"})
        idx = index([obs("account_1", identifiers={SUBJECT_EMAIL}), obs("account_1", file_id="evidence_2"),
                     obs("search_3", fields={"src": "account_1"}), obs("backup_4")])
        for m in idx.for_subject(profile(), CUT, extra_links={"backup_4": {SUBJECT_EMAIL}}):
            self.assertIsInstance(m, SubjectMatch)
            self.assertIsInstance(m.observation, Observation)
            self.assertIn(m.reason, REASONS)

    def test_strict_excludes_links_that_only_a_late_file_provides(self):
        early_anonymous = obs("account_1", file_id="evidence_01", observed_at=EARLY, source="early")
        late_named = obs("account_1", file_id="evidence_02", observed_at=BEFORE, captured_at=AFTER,
                         identifiers={SUBJECT_EMAIL}, source="late")
        idx = index([early_anonymous, late_named])
        self.assertEqual(len(idx.for_subject(profile(), CUT)), 2)
        self.assertEqual(idx.for_subject(profile(), CUT, strict=True), [])


# --- states_by_store(): replica drift ---

class StatesByStoreTest(unittest.TestCase):

    def test_latest_observation_per_store_with_none_store_keyed_dash(self):
        primary_old = obs(store="primary", observed_at=EARLY, state="FOUND", source="p old")
        primary_new = obs(store="primary", observed_at=BEFORE, state="NOT_FOUND", source="p new")
        replica = obs(store="read_replica", observed_at=EARLY, state="FOUND", source="replica")
        log_line = obs(store=None, observed_at=EARLY, state="FOUND", source="log")
        result = index([primary_old, primary_new, replica, log_line]).states_by_store("account_1", CUT)
        self.assertEqual(list(result), ["-", "primary", "read_replica"])
        self.assertIs(result["primary"], primary_new)
        self.assertEqual({k: v.state for k, v in result.items()},
                         {"-": "FOUND", "primary": "NOT_FOUND", "read_replica": "FOUND"})

    def test_respects_the_cutoff_per_store(self):
        primary_before = obs(store="primary", observed_at=BEFORE, state="FOUND", source="before")
        primary_after = obs(store="primary", observed_at=AFTER, state="NOT_FOUND", source="after")
        replica_after = obs(store="replica_eu_west", observed_at=AFTER, source="replica after")
        result = index([primary_before, primary_after, replica_after]).states_by_store("account_1", CUT)
        self.assertEqual(result, {"primary": primary_before})

    def test_unknown_resource_gives_empty_dict(self):
        self.assertEqual(index([obs()]).states_by_store("account_404", LATE), {})


# --- coverage() and absent_in(): proving absence from full-collection listings ---

class CoverageTest(unittest.TestCase):

    def test_lists_system_coverages_newest_first_up_to_cutoff(self):
        covs = [cov("evidence_1", "account_service", EARLY), cov("evidence_2", "account_service", CUT),
                cov("evidence_3", "account_service", AFTER), cov("evidence_4", "search_service", BEFORE)]
        result = index(coverages=covs).coverage("account_service", CUT)
        self.assertEqual([c.file_id for c in result], ["evidence_2", "evidence_1"])

    def test_unknown_system_gives_empty_list(self):
        self.assertEqual(index(coverages=[cov("evidence_1", "account_service", EARLY)]).coverage("x", LATE), [])


class AbsentInTest(unittest.TestCase):

    def test_returns_the_newest_coverage_whose_file_does_not_mention_the_resource(self):
        older = cov("evidence_1", "account_service", "2026-08-01T00:00:00Z")
        newer = cov("evidence_2", "account_service", "2026-08-20T00:00:00Z")
        idx = index([obs("account_7", file_id="evidence_9")], [older, newer])
        self.assertEqual(idx.absent_in("account_1", "account_service", CUT), newer)

    def test_none_when_the_covering_file_mentions_the_resource(self):
        listing = cov("evidence_1", "account_service", EARLY)
        idx = index([obs("account_1", file_id="evidence_1", observed_at=EARLY)], [listing])
        self.assertIsNone(idx.absent_in("account_1", "account_service", CUT))

    def test_none_when_there_is_no_coverage_for_the_system(self):
        idx = index(coverages=[cov("evidence_1", "search_service", EARLY)])
        self.assertIsNone(idx.absent_in("account_1", "account_service", CUT))

    def test_skips_a_coverage_observed_after_cutoff(self):
        idx = index(coverages=[cov("evidence_1", "account_service", AFTER)])
        self.assertIsNone(idx.absent_in("account_1", "account_service", CUT))

    def test_skips_a_coverage_whose_file_was_captured_after_cutoff(self):
        early_listing_late_file = cov("evidence_2", "account_service", BEFORE, captured_at=AFTER)
        on_time = cov("evidence_1", "account_service", EARLY)
        idx = index(coverages=[on_time, early_listing_late_file])
        self.assertEqual(idx.absent_in("account_1", "account_service", CUT), on_time)

    def test_uses_the_manifest_capture_time_for_coverages(self):
        listing = cov("evidence_5", "account_service", BEFORE, captured_at=BEFORE)
        idx = index(coverages=[listing], manifest=[ManifestEntry("evidence_5", "db/x.sql", "test", AFTER)])
        self.assertIsNone(idx.absent_in("account_1", "account_service", CUT))

    def test_coverage_exactly_at_cutoff_counts(self):
        at_cut = cov("evidence_1", "account_service", CUT)
        self.assertEqual(index(coverages=[at_cut]).absent_in("account_1", "account_service", CUT), at_cut)

    def test_an_older_listing_never_overrides_a_newer_listing_that_mentions_the_resource(self):
        older = cov("evidence_1", "account_service", EARLY)
        newer = cov("evidence_2", "account_service", BEFORE)
        idx = index([obs("account_1", file_id="evidence_2", observed_at=BEFORE)], [older, newer])
        self.assertIsNone(idx.absent_in("account_1", "account_service", CUT))

    def test_none_when_a_later_observation_shows_the_resource_present(self):
        listing = cov("evidence_1", "account_service", EARLY)
        later = obs("account_1", file_id="evidence_3", observed_at=BEFORE, state="FOUND")
        self.assertIsNone(index([later], [listing]).absent_in("account_1", "account_service", CUT))

    def test_a_later_not_found_observation_does_not_block_absence(self):
        listing = cov("evidence_1", "account_service", EARLY)
        later = obs("account_1", file_id="evidence_3", observed_at=BEFORE, state="NOT_FOUND")
        self.assertEqual(index([later], [listing]).absent_in("account_1", "account_service", CUT), listing)

    def test_presence_before_the_listing_or_after_cutoff_does_not_block_absence(self):
        listing = cov("evidence_1", "account_service", BEFORE)
        earlier = obs("account_1", file_id="evidence_3", observed_at=EARLY, state="FOUND")
        future = obs("account_1", file_id="evidence_4", observed_at=AFTER, state="FOUND")
        idx = index([earlier, future], [listing])
        self.assertEqual(idx.absent_in("account_1", "account_service", CUT), listing)


# --- history() / state_at(): sort order and tie-breaks ---

class HistoryOrderTest(unittest.TestCase):

    def test_history_is_oldest_first(self):
        rows = [obs(observed_at=t, source=t) for t in ("2026-08-03T00:00:00Z", "2026-08-01T00:00:00Z",
                                                        "2026-08-02T00:00:00Z")]
        self.assertEqual([o.source for o in index(rows).history("account_1", CUT)],
                         ["2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z", "2026-08-03T00:00:00Z"])

    def test_tie_on_observed_at_is_broken_by_capture_time(self):
        newer_file = obs(file_id="evidence_1", observed_at=EARLY, captured_at=BEFORE, state="NOT_FOUND")
        older_file = obs(file_id="evidence_2", observed_at=EARLY, captured_at=EARLY, state="FOUND")
        idx = index([newer_file, older_file])
        self.assertEqual([o.file_id for o in idx.history("account_1", CUT)], ["evidence_2", "evidence_1"])
        self.assertIs(idx.state_at("account_1", CUT), newer_file)

    def test_full_time_tie_is_broken_by_natural_file_order(self):
        e9 = obs(file_id="evidence_9", observed_at=EARLY, state="FOUND")
        e10 = obs(file_id="evidence_10", observed_at=EARLY, state="NOT_FOUND")
        idx = index([e10, e9])
        self.assertEqual([o.file_id for o in idx.history("account_1", CUT)], ["evidence_9", "evidence_10"])
        self.assertIs(idx.state_at("account_1", CUT), e10)

    def test_history_and_state_at_do_not_depend_on_input_order(self):
        rows = [obs(file_id=f"evidence_{n}", observed_at=f"2026-08-0{n % 3 + 1}T00:00:00Z",
                    captured_at=f"2026-08-0{n % 4 + 4}T00:00:00Z", source=f"s{n}") for n in range(1, 9)]
        baseline = [o.source for o in index(rows).history("account_1", CUT)]
        for seed in range(6):
            shuffled = rows[:]
            random.Random(seed).shuffle(shuffled)
            idx = index(shuffled)
            self.assertEqual([o.source for o in idx.history("account_1", CUT)], baseline)
            self.assertEqual(idx.state_at("account_1", CUT).source, baseline[-1])

    # Regression: same-time ties used to sort by the raw source string (records[10] before records[9]).
    def test_same_file_same_time_tie_follows_natural_locator_order(self):
        rec9 = obs(file_id="evidence_5", observed_at=EARLY, state="FOUND", source="json:records[9]")
        rec10 = obs(file_id="evidence_5", observed_at=EARLY, state="NOT_FOUND", source="json:records[10]")
        idx = index([rec9, rec10])
        self.assertEqual([o.source for o in idx.history("account_1", CUT)], ["json:records[9]", "json:records[10]"])
        self.assertIs(idx.state_at("account_1", CUT), rec10)


# --- Robustness: malformed input is reported, never silently dropped ---

class RobustnessTest(unittest.TestCase):

    def test_malformed_observed_at_is_skipped_and_warned(self):
        good = obs("account_1", source="good")
        bad = obs("account_2", file_id="evidence_44", observed_at="not a time", captured_at=EARLY, source="line 7")
        idx = index([bad, good])
        self.assertEqual(idx.resource_ids(), ["account_1"])
        self.assertEqual(len(idx.warnings), 1)
        self.assertIn("evidence_44", idx.warnings[0])
        self.assertIn("account_2", idx.warnings[0])

    def test_malformed_captured_at_without_manifest_is_skipped_and_warned(self):
        bad = obs("account_2", file_id="evidence_45", observed_at=EARLY, captured_at="31/12/2026")
        idx = index([bad, obs("account_1")])
        self.assertEqual(idx.resource_ids(), ["account_1"])
        self.assertEqual(len(idx.warnings), 1)
        self.assertIn("evidence_45", idx.warnings[0])

    def test_malformed_coverage_timestamp_is_skipped_and_warned(self):
        idx = index(coverages=[cov("evidence_46", "account_service", "yesterday"),
                               cov("evidence_1", "account_service", EARLY)])
        self.assertEqual([c.file_id for c in idx.coverage("account_service", LATE)], ["evidence_1"])
        self.assertEqual(len(idx.warnings), 1)
        self.assertIn("evidence_46", idx.warnings[0])

    def test_valid_manifest_time_rescues_a_malformed_observation_capture_value(self):
        o = obs("account_1", file_id="evidence_47", observed_at=EARLY, captured_at="garbage")
        idx = index([o], manifest=[ManifestEntry("evidence_47", "records/x.json", "test", EARLY)])
        self.assertEqual(idx.warnings, [])
        self.assertEqual(idx.citable("account_1", CUT), "evidence_47")

    # Regression: a malformed manifest captured_at used to raise without naming the file.
    def test_malformed_manifest_capture_time_is_reported_with_its_file_id(self):
        manifest = [ManifestEntry("evidence_77", "records/x.json", "test", "not-a-time")]
        try:
            idx = index([obs()], manifest=manifest)
        except ValueError as exc:
            self.assertIn("evidence_77", str(exc))
        else:
            self.assertTrue(any("evidence_77" in w for w in idx.warnings))

    def test_queries_on_an_empty_index_return_empty_results(self):
        idx = index()
        self.assertEqual(idx.history("account_1", LATE), [])
        self.assertIsNone(idx.state_at("account_1", LATE))
        self.assertEqual(idx.states_by_store("account_1", LATE), {})
        self.assertIsNone(idx.citable("account_1", LATE))
        self.assertEqual(idx.coverage("account_service", LATE), [])
        self.assertIsNone(idx.absent_in("account_1", "account_service", LATE))
        self.assertEqual(idx.for_subject(profile(), LATE), [])
        self.assertEqual(idx.resource_ids(), [])
        self.assertEqual(idx.warnings, [])

    def test_malformed_cutoff_raises(self):
        with self.assertRaises(ValueError):
            index([obs()]).history("account_1", "soon")


# --- Extras: in_file, with_token, resource_ids, natural_key, is_resource_id ---

class ExtrasTest(unittest.TestCase):

    def test_in_file_returns_every_observation_of_one_file_in_history_order(self):
        a = obs("account_1", file_id="evidence_5", observed_at=BEFORE, source="a")
        b = obs("search_2", file_id="evidence_5", observed_at=EARLY, source="b")
        c = obs("account_1", file_id="evidence_6", observed_at=EARLY, source="c")
        late = obs("account_3", file_id="evidence_5", observed_at=AFTER, source="late")
        idx = index([a, b, c, late])
        self.assertEqual([o.source for o in idx.in_file("evidence_5")], ["b", "a", "late"])
        self.assertEqual(idx.in_file("evidence_404"), [])

    def test_with_token_finds_strong_and_name_tokens_and_honours_cutoff(self):
        before = obs("account_1", identifiers={SUBJECT_EMAIL, "name:ana subject"}, observed_at=BEFORE)
        after = obs("account_2", identifiers={SUBJECT_EMAIL}, observed_at=AFTER)
        idx = index([after, before])
        self.assertEqual(idx.with_token(SUBJECT_EMAIL), [before, after])
        self.assertEqual(idx.with_token(SUBJECT_EMAIL, CUT), [before])
        self.assertEqual(idx.with_token("name:ana subject"), [before])
        self.assertEqual(idx.with_token("nobody@example.com"), [])

    def test_resource_ids_are_unique_and_naturally_sorted(self):
        idx = index([obs(r) for r in ("search_10", "search_9", "account_2", "search_9", "account_10")])
        self.assertEqual(idx.resource_ids(), ["account_2", "account_10", "search_9", "search_10"])

    def test_natural_key_orders_numbers_numerically(self):
        names = ["evidence_10", "evidence_9", "evidence_100", "evidence_01"]
        self.assertEqual(sorted(names, key=natural_key), ["evidence_01", "evidence_9", "evidence_10", "evidence_100"])

    def test_is_resource_id_accepts_resource_shaped_strings(self):
        for value in ("search_052", "data_flow_9522", "audit_record_9394", "account_4248", "tombstone_17"):
            with self.subTest(value=value):
                self.assertTrue(is_resource_id(value))

    def test_is_resource_id_rejects_user_ids_and_non_resources(self):
        for value in ("user_11", "", "search", "ORD-20263245", "Search_1", "account-12",
                      "a.b@example.com", "search_1x", None, 42, ["search_1"]):
            with self.subTest(value=value):
                self.assertFalse(is_resource_id(value))


# --- build(): parse a manifest through the parsers and index the result ---

class BuildTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        fixture = FIXTURES / "handover_fixture.txt"
        # An absolute file_path is joined unchanged onto dataset/, so the fixture needs no dataset.
        cls.manifest = [
            ManifestEntry("evidence_901", str(fixture), "test_fixture", "2026-09-01T13:00:00Z"),
            ManifestEntry("evidence_902", str(FIXTURES / "does_not_exist.txt"), "test_fixture", EARLY),
        ]
        cls.idx = EvidenceIndex.build(cls.manifest)

    def test_build_indexes_every_record_line(self):
        self.assertEqual(self.idx.resource_ids(), ["account_901", "account_903", "search_902"])
        self.assertEqual({o.file_id for r in self.idx.resource_ids() for o in self.idx.history(r, LATE)},
                         {"evidence_901"})

    def test_build_reports_a_missing_file_as_a_warning(self):
        self.assertTrue(any(w.startswith("evidence_902") for w in self.idx.warnings), self.idx.warnings)

    def test_built_index_applies_the_cutoff_and_normalised_identifiers(self):
        subject = profile(user_id="user_901", emails={"ana.fixture@example.com"})
        at_noon = {m.observation.resource_id for m in self.idx.for_subject(subject, CUT)}
        later = {m.observation.resource_id for m in self.idx.for_subject(subject, LATE)}
        self.assertEqual(at_noon, {"account_901"})
        self.assertEqual(later, {"account_901", "search_902"})

    def test_built_index_uses_manifest_capture_time_for_citations(self):
        self.assertIsNone(self.idx.citable("account_901", CUT))
        self.assertEqual(self.idx.citable("account_901", "2026-09-01T13:00:00Z"), "evidence_901")
        self.assertEqual(self.idx.state_at("account_901", CUT).state, "FOUND")


if __name__ == "__main__":
    unittest.main()
