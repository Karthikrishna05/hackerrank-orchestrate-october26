"""Tests for ghostaudit.parsers (__init__): dispatch, shared helpers, parse_all and T6 acceptance."""

from __future__ import annotations

import re
import shutil
import tempfile
import unittest
import warnings
from collections import defaultdict
from pathlib import Path

from ghostaudit.loaders import load_manifest
from ghostaudit.models import NAME_PREFIX, STATES, ManifestEntry
from ghostaudit.parsers import identifiers_of, images, parse_all, parse_file, record_state
from ghostaudit.parsers.logs import EVENT_STATE_RULES, NOISE_EVENTS, is_start_event, parse_log_line
from ghostaudit.paths import dataset_dir, evidence_path

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "parsers"
HAVE_DATASET = (dataset_dir() / "evidence_files.csv").is_file()
ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
SEAL = "sha256:000000000000000000000000000000000000000000000000000000000000040d"


def fixture_entry(name: str, file_id: str, captured_at: str = "2026-09-30T00:00:00Z") -> ManifestEntry:
    return ManifestEntry(file_id=file_id, file_path=str(FIXTURES / name), origin="test_fixture",
                         captured_at=captured_at)


def quietly(fn, *args):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = fn(*args)
    return result, [str(w.message) for w in caught]


class IdentifiersOfTests(unittest.TestCase):

    def test_collects_strong_tokens_from_nested_values(self):
        record = {"document": {"email": "A.B@EXAMPLE.COM", "refs": ["user_12", "+44 7700 900123"]}}
        self.assertEqual(identifiers_of(record),
                         frozenset({"a.b@example.com", "user_12", "+447700900123"}))

    def test_name_fields_become_weak_name_tokens(self):
        record = {"full_name": "Mateo  Ríos", "display_name": "Jonas Berg",
                  "requester_name": "Ana Varga", "name": "Kofi Agyeman"}
        self.assertEqual(identifiers_of(record), frozenset({
            NAME_PREFIX + "mateo ríos", NAME_PREFIX + "jonas berg", NAME_PREFIX + "ana varga",
            NAME_PREFIX + "kofi agyeman"}))

    def test_names_in_other_fields_are_not_identifiers(self):
        self.assertEqual(identifiers_of({"message_text": "Hello, I am Jonas Berg"}), frozenset())

    def test_integrity_seal_is_never_an_identifier(self):
        self.assertEqual(identifiers_of({"integrity_seal": SEAL, "subject_reference": "user_3"}),
                         frozenset({"user_3"}))

    def test_nested_integrity_seal_is_skipped_too(self):
        self.assertEqual(identifiers_of({"meta": {"integrity_seal": SEAL}}), frozenset())

    def test_real_hashes_outside_the_seal_are_identifiers(self):
        h = "44a65154bfd8e408852578fbd76813d094d1944f255ac1769ee81ba90492f10e"
        self.assertEqual(identifiers_of({"correlation_key": h.upper()}), frozenset({h}))

    def test_null_like_values_are_ignored(self):
        self.assertEqual(identifiers_of({"email": None, "phone": "null", "name": "", "x": 3}),
                         frozenset())


class RecordStateTests(unittest.TestCase):

    def test_explicit_result_wins_and_is_upper_cased(self):
        self.assertEqual(record_state("ANALYTICS_PROFILE", {"result": "found", "email": None}), "FOUND")

    def test_unknown_explicit_result_is_unknown(self):
        state, warns = quietly(record_state, "USER_PROFILE", {"result": "MAYBE"})
        self.assertEqual(state, "UNKNOWN")
        self.assertEqual(len(warns), 1)

    def test_analytics_all_null_is_anonymized(self):
        rec = {"user_id": None, "customer_reference": None, "email": "null", "phone": None,
               "email_hash": None, "metrics": {"sessions_30d": 3}}
        self.assertEqual(record_state("ANALYTICS_PROFILE", rec), "ANONYMIZED")

    def test_analytics_with_any_identifier_is_identifiable(self):
        for key in ("user_id", "customer_reference", "email", "phone", "email_hash"):
            with self.subTest(field=key):
                self.assertEqual(record_state("ANALYTICS_PROFILE", {key: "x"}), "IDENTIFIABLE")

    def test_tombstone_keeping_only_the_stable_reference_is_redacted(self):
        rec = {"subject_reference": "user_543", "name": None, "email": None, "phone": None,
               "email_hash": None, "account_status": "DELETED"}
        self.assertEqual(record_state("ACCOUNT_TOMBSTONE", rec), "REDACTED")

    def test_tombstone_with_a_direct_identifier_is_identifiable(self):
        for key in ("name", "email", "phone", "email_hash"):
            with self.subTest(field=key):
                self.assertEqual(record_state("ACCOUNT_TOMBSTONE",
                                              {"subject_reference": "user_1", key: "v"}),
                                 "IDENTIFIABLE")

    def test_support_message_states(self):
        self.assertEqual(record_state("SUPPORT_MESSAGE", {"message_text": "[REDACTED]"}), "REDACTED")
        self.assertEqual(record_state("SUPPORT_MESSAGE", {"message_text": "mail a@b.example.com"}),
                         "IDENTIFIABLE")
        self.assertEqual(record_state("SUPPORT_MESSAGE", {"requester_email": "a@b.example.com",
                                                          "message_excerpt": "Customer asked."}),
                         "IDENTIFIABLE")

    def test_data_flow_enabled_forms(self):
        for value, state in ((True, "ACTIVE"), (1, "ACTIVE"), ("true", "ACTIVE"), (False, "DISABLED"),
                             (0, "DISABLED"), ("false", "DISABLED"), (None, "UNKNOWN")):
            with self.subTest(enabled=value):
                self.assertEqual(record_state("DATA_FLOW", {"enabled": value}), state)

    def test_backup_restore_allowed_forms(self):
        for value, state in ((True, "ACTIVE"), (1, "ACTIVE"), (False, "BLOCKED"), (0, "BLOCKED"),
                             (None, "UNKNOWN")):
            with self.subTest(restore_allowed=value):
                self.assertEqual(record_state("BACKUP_ARCHIVE", {"restore_allowed": value}), state)

    def test_plain_listed_records_are_found(self):
        for category in ("USER_PROFILE", "SEARCH_INDEX_ENTRY", "USER_EXPORT", "SECURITY_AUDIT_LOG",
                         "LEGAL_HOLD_RECORD", None):
            with self.subTest(category=category):
                self.assertEqual(record_state(category, {"email": "a@b.example.com"}), "FOUND")


class DispatchTests(unittest.TestCase):
    """parse_file picks the parser from the extension; parse_all never crashes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_absolute_fixture_path_resolves_through_evidence_path(self):
        self.assertEqual(evidence_path(str(FIXTURES / "handover.txt")), FIXTURES / "handover.txt")

    def test_each_extension_is_dispatched_to_its_parser(self):
        cases = {"generic_read_replica.sql": "sql:", "flat_records.json": "json:",
                 "interleaved_traces.log": "log:", "handover.txt": "txt:"}
        for name, prefix in cases.items():
            with self.subTest(fixture=name):
                (obs, _), _ = quietly(parse_file, fixture_entry(name, "evidence_t9"))
                self.assertTrue(obs)
                self.assertTrue(all(o.source.startswith(prefix) for o in obs))

    def test_extension_match_is_case_insensitive(self):
        upper = self.tmp / "HANDOVER.TXT"
        shutil.copy(FIXTURES / "handover.txt", upper)
        entry = ManifestEntry("evidence_t9", str(upper), "test_fixture", "2026-10-01T12:00:00Z")
        (obs, _), _ = quietly(parse_file, entry)
        self.assertEqual(len(obs), 4)

    def test_unsupported_extension_raises_value_error(self):
        path = self.tmp / "notes.csv"
        path.write_text("a,b\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "evidence_t9"):
            parse_file(ManifestEntry("evidence_t9", str(path), "test_fixture", "2026-01-01T00:00:00Z"))

    def test_missing_file_raises_file_not_found(self):
        missing = ManifestEntry("evidence_t9", str(self.tmp / "gone.sql"), "x", "2026-01-01T00:00:00Z")
        with self.assertRaisesRegex(FileNotFoundError, "evidence_t9"):
            parse_file(missing)

    def test_parse_all_turns_a_failing_file_into_a_message(self):
        manifest = [fixture_entry("handover.txt", "evidence_t10"),
                    ManifestEntry("evidence_t11", str(self.tmp / "gone.sql"), "x", "2026-01-01T00:00:00Z")]
        obs, _, messages = parse_all(manifest)
        self.assertEqual(len(obs), 4)
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0].startswith("evidence_t11: FileNotFoundError"), messages)

    def test_parse_all_prefixes_warnings_with_the_file_id(self):
        _, _, messages = parse_all([fixture_entry("unparseable.log", "evidence_t12")])
        self.assertEqual(len(messages), 3)
        self.assertTrue(all(m.startswith("evidence_t12") for m in messages), messages)

    def test_parse_all_sorts_by_natural_file_id(self):
        manifest = [fixture_entry("handover.txt", "evidence_10"),
                    fixture_entry("flat_records.json", "evidence_9")]
        obs, _, _ = parse_all(manifest)
        self.assertEqual([o.file_id for o in obs], ["evidence_9"] * 4 + ["evidence_10"] * 4)

    def test_parse_all_sorts_lines_by_natural_line_number(self):
        obs, _, _ = parse_all([fixture_entry("interleaved_traces.log", "evidence_t13")])
        self.assertEqual([o.source for o in obs], ["log:L5", "log:L8", "log:L9"])


@unittest.skipUnless(HAVE_DATASET, "participant dataset not present")
class RealDatasetAcceptanceTests(unittest.TestCase):
    """T6 acceptance criteria on the real evidence (parsed once for speed)."""

    @classmethod
    def setUpClass(cls):
        cls.manifest = load_manifest()
        cls.by_file_id = {e.file_id: e for e in cls.manifest}
        cls.obs, cls.cov, cls.messages = parse_all(cls.manifest)
        cls.log_entries = [e for e in cls.manifest if e.file_path.endswith(".log")]
        cls.png_entries = [e for e in cls.manifest if e.file_path.lower().endswith(".png")]

    def in_file(self, file_id):
        return [o for o in self.obs if o.file_id == file_id]

    def test_manifest_has_94_entries(self):
        self.assertEqual(len(self.manifest), 94)

    def test_only_the_11_screenshots_without_vision_cache_warn(self):
        self.assertEqual(len(self.png_entries), 11)
        uncached = {e.file_id for e in self.png_entries if not images.cache_path(e.file_id).is_file()}
        warned = {m.split(":", 1)[0] for m in self.messages}
        self.assertEqual(warned, uncached)
        self.assertEqual(len(self.messages), len(uncached))
        self.assertTrue(all("vision cache" in m for m in self.messages), self.messages)

    def test_no_file_crashes(self):
        crashed = [m for m in self.messages if re.match(r"^evidence_\d+: \w+(Error|Exception)\b", m)]
        self.assertEqual(crashed, [])

    def test_every_non_image_file_yields_observations_or_coverage(self):
        files = {o.file_id for o in self.obs} | {c.file_id for c in self.cov}
        non_images = {e.file_id for e in self.manifest} - {e.file_id for e in self.png_entries}
        self.assertEqual(len(non_images), 83)
        self.assertEqual(non_images - files, set())

    def test_evidence_03_gives_41_observations(self):
        self.assertEqual(len(self.in_file("evidence_03")), 41)

    def test_evidence_03_has_search_052_found_and_data_flow_9522_active(self):
        states = {o.resource_id: o.state for o in self.in_file("evidence_03")}
        self.assertEqual(states["search_052"], "FOUND")
        self.assertEqual(states["data_flow_9522"], "ACTIVE")

    def test_audit_record_4902_times_in_evidence_06(self):
        hits = [o for o in self.in_file("evidence_06") if o.resource_id == "audit_record_4902"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].observed_at, "2026-09-16T08:52:59Z")
        self.assertEqual(hits[0].captured_at, "2026-09-18T11:55:24Z")

    def test_analytics_9914_carries_the_old_email_hash(self):
        tokens = {t for o in self.obs if o.resource_id == "analytics_9914" for t in o.identifiers}
        self.assertTrue(any(t.startswith("44a65154bfd8e408") and len(t) == 64 for t in tokens), tokens)

    def test_account_994_in_evidence_05_is_only_in_the_read_replica(self):
        stores = [o.store for o in self.in_file("evidence_05") if o.resource_id == "account_994"]
        self.assertEqual(stores, ["read_replica"])

    def test_exactly_13_account_rows_exist_only_in_non_primary_tables(self):
        stores = defaultdict(set)
        files_with_primary = {o.file_id for o in self.obs if o.store == "primary"}
        for o in self.obs:
            if o.source.startswith("sql:") and o.resource_id.startswith("account_") \
                    and o.file_id in files_with_primary:
                stores[(o.file_id, o.resource_id)].add(o.store)
        ghosts = sorted(k for k, v in stores.items() if "primary" not in v)
        self.assertEqual(len(ghosts), 13, ghosts)

    def test_no_non_primary_table_misses_a_primary_row(self):
        tables = defaultdict(set)
        for o in self.obs:
            if o.source.startswith("sql:") and o.store not in (None, "inventory"):
                tables[(o.file_id, o.store)].add(o.resource_id)
        for (file_id, store), rids in tables.items():
            if store != "primary" and (file_id, "primary") in tables:
                with self.subTest(file_id=file_id, store=store):
                    self.assertEqual(tables[(file_id, "primary")] - rids, set())

    def test_evidence_53_has_300_profile_lookups(self):
        events = [o.fields.get("event") for o in self.in_file("evidence_53")]
        self.assertEqual(events.count("profile_lookup_completed"), 300)

    def test_evidence_51_and_90_give_account_service_coverage(self):
        for file_id in ("evidence_51", "evidence_90"):
            with self.subTest(file_id=file_id):
                self.assertIn("account_service",
                              {c.system_id for c in self.cov if c.file_id == file_id})

    def test_empty_inventory_evidence_90_covers_only_account_service(self):
        self.assertEqual([(c.system_id, c.store) for c in self.cov if c.file_id == "evidence_90"],
                         [("account_service", "inventory")])
        self.assertEqual(self.in_file("evidence_90"), [])

    def test_there_are_26_logs(self):
        self.assertEqual(len(self.log_entries), 26)

    def test_every_log_line_is_parsed(self):
        unparsed = []
        for entry in self.log_entries:
            lines = evidence_path(entry.file_path).read_text(encoding="utf-8").splitlines()
            unparsed += [(entry.file_id, n) for n, line in enumerate(lines, 1)
                         if line.strip() and parse_log_line(line) is None]
        self.assertEqual(unparsed, [])

    def test_every_log_event_is_state_bearing_start_or_noise(self):
        unknown = set()
        for entry in self.log_entries:
            for line in evidence_path(entry.file_path).read_text(encoding="utf-8").splitlines():
                rec = parse_log_line(line) if line.strip() else None
                if rec and rec["event"] not in EVENT_STATE_RULES and rec["event"] not in NOISE_EVENTS \
                        and not is_start_event(rec["event"]):
                    unknown.add(rec["event"])
        self.assertEqual(unknown, set())

    def test_noise_log_lines_carry_no_identifiers(self):
        leaking = []
        for entry in self.log_entries:
            for n, line in enumerate(evidence_path(entry.file_path).read_text("utf-8").splitlines(), 1):
                rec = parse_log_line(line) if line.strip() else None
                if rec and rec["event"] in NOISE_EVENTS and identifiers_of(rec["kv"]):
                    leaking.append((entry.file_id, n))
        self.assertEqual(leaking, [])

    # Regression: pipe-style levels used to keep the leading "| " (see test_parsers_logs).
    def test_every_parsed_log_level_is_a_plain_level_word(self):
        levels = set()
        for entry in self.log_entries:
            for line in evidence_path(entry.file_path).read_text(encoding="utf-8").splitlines():
                rec = parse_log_line(line) if line.strip() else None
                if rec:
                    levels.add(rec["level"])
        self.assertLessEqual(levels, {"DEBUG", "INFO", "NOTICE", "WARN", "WARNING", "ERROR",
                                      "CRITICAL"})

    def test_every_state_is_a_model_state(self):
        self.assertEqual({o.state for o in self.obs} - set(STATES), set())

    def test_no_observation_has_an_empty_file_id_or_captured_at(self):
        self.assertEqual([o for o in self.obs if not o.file_id or not o.captured_at], [])

    def test_captured_at_matches_the_manifest(self):
        mismatched = [(o.file_id, o.source) for o in self.obs
                      if o.captured_at != self.by_file_id[o.file_id].captured_at]
        self.assertEqual(mismatched, [])

    def test_timestamps_are_iso_utc_whole_seconds(self):
        bad = [(o.file_id, o.source) for o in self.obs
               if not ISO_Z.match(o.observed_at) or not ISO_Z.match(o.captured_at)]
        bad += [(c.file_id, c.collection) for c in self.cov
                if not ISO_Z.match(c.observed_at) or not ISO_Z.match(c.captured_at)]
        self.assertEqual(bad, [])

    def test_no_log_line_is_timestamped_after_its_capture(self):
        late = [(o.file_id, o.source) for o in self.obs
                if o.source.startswith("log:") and o.observed_at > o.captured_at]
        self.assertEqual(late, [])

    def test_every_observation_has_a_system(self):
        self.assertEqual([(o.file_id, o.source) for o in self.obs if not o.system_id], [])

    def test_two_runs_give_identical_output(self):
        again = parse_all(self.manifest)
        self.assertEqual(again, (self.obs, self.cov, self.messages))


if __name__ == "__main__":
    unittest.main()
