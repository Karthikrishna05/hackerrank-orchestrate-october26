"""Unit tests for ghostaudit.parsers.logs (tools_spec.md T6, logs row and EVENT_STATE_RULES).

Step 1: `parse_log_line` for every line style in the dataset.
Step 2: the `*_started` -> result join on trace_id across interleaved traces.
Step 3: each EVENT_STATE_RULES row, applied to kv dicts shaped like parsed lines.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
import warnings
from pathlib import Path

from ghostaudit.models import ManifestEntry
from ghostaudit.parsers import logs
from ghostaudit.parsers.logs import EVENT_STATE_RULES, NOISE_EVENTS, parse_log_line

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "parsers"
MANIFEST_CAPTURED_AT = "2026-09-15T11:59:39Z"
OLD_EMAIL_HASH = hashlib.sha256(b"old.address@example.com").hexdigest()

# One realistic line per style (shapes copied from dataset/evidence/logs, identities fictional).
LOGFMT = ('2026-09-15T08:51:45Z level=DEBUG service=search_service instance=search-2 pod=search-12cc '
          'region=ap-south-1 trace_id=tr-5887496 event=index_query_started '
          'match_key="g.silvano@example.com" attempt=1')
JSON_STYLE = ('2026-09-10T08:56:17Z {"severity":"DEBUG","logger":"account_service",'
              '"service":"account_service","instance":"account-6","pod":"account-ca1e",'
              '"region":"us-east-2","trace_id":"tr-5563868","event":"profile_lookup_started",'
              '"lookup_key":"g.collins@example.com","attempt":"1"}')
SYSLOG = ('2026-08-22T10:51:17Z <15>1 node-1 account_service tr-4973247 - - level=DEBUG '
          'instance="account-2" pod="account-0525" region="eu-west-1" resource_id="account_1785" '
          'event="profile_lookup_completed" rows="1" http_status="200" duration_ms="50"')
SYSLOG_NO_TRACE = ('2026-03-25T08:52:56Z <15>1 node-8 systemd - - - level=DEBUG instance="node-7" '
                   'event="timer_tick" unit="logrotate.timer"')
BRACKET = ('2026-07-28T05:51:18Z [DEBUG] [account_service] [tr-6074163] profile_lookup_started: '
           'instance="account-5" pod="account-886d" region="eu-west-1" '
           'lookup_key="l.nascimento@example.com" attempt="1"')
PIPE = ('2026-07-03T07:51:32Z | DEBUG | security_service | tr-5156005 | audit_record_read_completed | '
        'instance="security-6" pod="security-7e9b" region="eu-central-1" '
        'resource_id="audit_record_4413" integrity_check="passed" rows="1" correlation_key="user_211"')
CLOUD = ('2026-06-08T06:52:09Z requestId=tr-1108860 level=INFO function=search_service '
         'message="instance=\\"search-3\\" pod=\\"search-0696\\" region=\\"us-east-2\\" '
         'resource_id=\\"search_021\\" event=\\"index_query_completed\\" index=\\"customer_directory_v3\\" '
         'result=\\"hit\\" shard=\\"9\\" took_ms=\\"24\\" correlation_key=\\"m.rios.jr@example.com\\""')
CLOUD_REDACTED = ('2026-06-08T09:51:47Z requestId=tr-0310343 level=INFO function=support_platform '
                  'message="instance=\\"support-5\\" resource_id=\\"support_9996\\" '
                  'event=\\"message_read\\" subject_ref=\\"user_262\\" message_text=\\"[REDACTED]\\" '
                  'note=\\"two words\\""')
RECORD_LOOKUP = ('2026-03-15T07:00:00Z INFO service=downstream_delivery_service event=record_lookup '
                 'seq=1 system="account_service" resource_id="account_022" record_type="USER_PROFILE" '
                 'result="NOT_FOUND" subject_reference="z.nowak@example.com"')
READBACK = ('2026-02-18T05:47:12Z level=INFO service=integration_service trace_id=rb-054712-001 '
            'event=connector_config_read_completed resource_id=data_flow_3669 enabled=true '
            'last_delivery_status=delivered connector_name=analytics_identity_bridge '
            'customer_email=t.oliva@example.com customer_reference=null')
SUPPORT_REPLICA = ('2026-09-17T22:00:06Z level=WARN service=support_replica instance=support-eu-03 '
                   'pod=replica-7c9f trace_id=tr-rp-002 event=deletion_marker_check '
                   'resource_id=support_002 marker_found=false last_write=2026-09-07T10:01:00Z')


def entry_for(path: Path, file_id: str = "evidence_t3",
              captured_at: str = MANIFEST_CAPTURED_AT) -> ManifestEntry:
    return ManifestEntry(file_id=file_id, file_path=str(path), origin="test_fixture",
                         captured_at=captured_at)


def parse_log(path: Path, **entry_kw):
    """Parse one log file; return (observations, coverages, warning messages)."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        obs, cov = logs.parse(entry_for(path, **entry_kw), path)
    return obs, cov, [str(w.message) for w in caught]


def only(observations, resource_id):
    found = [o for o in observations if o.resource_id == resource_id]
    if len(found) != 1:
        raise AssertionError(f"expected one observation of {resource_id}, got {len(found)}")
    return found[0]


def state_of(event: str, **kv) -> str:
    """Apply the EVENT_STATE_RULES rule of `event` to string kv values, as a parsed line has."""
    _, rule = EVENT_STATE_RULES[event]
    return rule({"event": event, **kv})


class ParseLogLineStyleTests(unittest.TestCase):
    """Step 1: every line style splits into ts, level, service, trace_id, event, kv."""

    def assertLine(self, line, **expected):
        rec = parse_log_line(line)
        self.assertIsNotNone(rec, "line was not parsed")
        for key, value in expected.items():
            with self.subTest(field=key):
                self.assertEqual(rec[key], value)
        return rec

    def test_logfmt_line(self):
        rec = self.assertLine(LOGFMT, ts="2026-09-15T08:51:45Z", level="DEBUG",
                              service="search_service", trace_id="tr-5887496",
                              event="index_query_started", style="logfmt")
        self.assertEqual(rec["kv"]["match_key"], "g.silvano@example.com")

    def test_json_after_timestamp_line(self):
        rec = self.assertLine(JSON_STYLE, ts="2026-09-10T08:56:17Z", level="DEBUG",
                              service="account_service", trace_id="tr-5563868",
                              event="profile_lookup_started", style="json")
        self.assertEqual(rec["kv"]["lookup_key"], "g.collins@example.com")

    def test_rfc5424_syslog_line(self):
        rec = self.assertLine(SYSLOG, ts="2026-08-22T10:51:17Z", level="DEBUG",
                              service="account_service", trace_id="tr-4973247",
                              event="profile_lookup_completed", style="syslog")
        self.assertEqual((rec["kv"]["resource_id"], rec["kv"]["rows"]), ("account_1785", "1"))

    def test_syslog_dash_procid_means_no_trace(self):
        self.assertLine(SYSLOG_NO_TRACE, service="systemd", trace_id=None, event="timer_tick")

    def test_bracketed_trace_line(self):
        rec = self.assertLine(BRACKET, ts="2026-07-28T05:51:18Z", level="DEBUG",
                              service="account_service", trace_id="tr-6074163",
                              event="profile_lookup_started", style="bracket")
        self.assertEqual(rec["kv"]["lookup_key"], "l.nascimento@example.com")

    def test_pipe_delimited_line(self):
        rec = self.assertLine(PIPE, ts="2026-07-03T07:51:32Z", service="security_service",
                              trace_id="tr-5156005", event="audit_record_read_completed",
                              style="pipe")
        self.assertEqual(rec["kv"]["correlation_key"], "user_211")

    # BUG: logs.parse_log_line keeps the leading "| " of the pipe style in the first field
    # (rest = "| DEBUG | service | ..."), so level is "| DEBUG" on every pipe line
    # (231 lines in evidence_21 and evidence_48). Suggested fix: strip the leading "|" before
    # splitting, e.g. rest.lstrip("| ").split(" | ", 4).
    @unittest.expectedFailure
    def test_pipe_delimited_line_level(self):
        self.assertLine(PIPE, level="DEBUG")

    # BUG: logs.parse_log_line splits the pipe style with rest.split(" | ") and requires exactly
    # 5 parts, so a quoted kv value containing " | " makes the whole line unparseable.
    # Suggested fix: rest.split(" | ", 4).
    @unittest.expectedFailure
    def test_pipe_line_with_a_pipe_inside_a_quoted_value(self):
        line = ('2026-07-03T07:52:00Z | INFO | support_platform | tr-5156006 | message_read | '
                'resource_id="support_1" message_text="order A | order B"')
        rec = self.assertLine(line, event="message_read", style="pipe")
        self.assertEqual(rec["kv"]["message_text"], "order A | order B")

    def test_cloud_runtime_line_with_escaped_nested_message(self):
        rec = self.assertLine(CLOUD, ts="2026-06-08T06:52:09Z", level="INFO",
                              service="search_service", trace_id="tr-1108860",
                              event="index_query_completed", style="cloud")
        self.assertEqual(rec["kv"]["resource_id"], "search_021")
        self.assertEqual(rec["kv"]["result"], "hit")
        self.assertEqual(rec["kv"]["correlation_key"], "m.rios.jr@example.com")
        self.assertNotIn("message", rec["kv"])

    def test_cloud_runtime_nested_values_keep_brackets_and_spaces(self):
        rec = self.assertLine(CLOUD_REDACTED, event="message_read")
        self.assertEqual(rec["kv"]["message_text"], "[REDACTED]")
        self.assertEqual(rec["kv"]["note"], "two words")

    def test_record_lookup_line_with_explicit_result(self):
        rec = self.assertLine(RECORD_LOOKUP, ts="2026-03-15T07:00:00Z", level="INFO",
                              service="downstream_delivery_service", event="record_lookup",
                              style="record_lookup")
        self.assertEqual((rec["kv"]["system"], rec["kv"]["result"]), ("account_service", "NOT_FOUND"))

    def test_readback_line_with_rb_trace(self):
        rec = self.assertLine(READBACK, level="INFO", service="integration_service",
                              trace_id="rb-054712-001", event="connector_config_read_completed",
                              style="readback")
        self.assertEqual(rec["kv"]["enabled"], "true")

    def test_support_replica_line(self):
        rec = self.assertLine(SUPPORT_REPLICA, level="WARN", service="support_replica",
                              trace_id="tr-rp-002", event="deletion_marker_check",
                              style="support_replica")
        self.assertEqual(rec["kv"]["marker_found"], "false")

    def test_timestamp_with_offset_and_fraction_is_normalised_to_utc(self):
        line = '2026-09-15T10:51:45.250+02:00 level=INFO service=x trace_id=tr-1 event=timer_tick'
        self.assertLine(line, ts="2026-09-15T08:51:45Z")

    def test_line_without_timestamp_is_unparseable(self):
        self.assertIsNone(parse_log_line("level=INFO service=x event=timer_tick"))

    def test_free_text_after_timestamp_is_unparseable(self):
        self.assertIsNone(parse_log_line("2026-09-15T08:51:45Z something went wrong here"))

    def test_json_array_after_timestamp_is_unparseable(self):
        self.assertIsNone(parse_log_line('2026-09-15T08:51:45Z ["event", "timer_tick"]'))

    def test_line_without_an_event_is_unparseable(self):
        self.assertIsNone(parse_log_line("2026-09-15T08:51:45Z level=INFO service=x rows=1"))


class EventStateRulesTests(unittest.TestCase):
    """Step 3: each row of the EVENT_STATE_RULES table in tools_spec.md T6."""

    SPEC_EVENTS = (
        "profile_lookup_completed", "index_query_completed", "analytics_profile_read",
        "analytics_profile_read_completed", "support_ticket_read_completed",
        "analytics_identity_scan", "message_pii_scan", "attachment_pii_scan", "message_read",
        "connector_config_read_completed", "connector_heartbeat", "connector_config_loaded",
        "restore_policy_read_completed", "restore_candidate_evaluated",
        "restore_authorization_completed", "audit_record_read_completed",
        "export_download_check_completed", "export_metadata_read_completed",
        "contact_lookup_completed", "contact_query_completed", "record_lookup",
        "deletion_marker_check",
    )

    def test_every_spec_event_has_a_rule(self):
        for event in self.SPEC_EVENTS:
            with self.subTest(event=event):
                self.assertIn(event, EVENT_STATE_RULES)

    def test_no_event_is_both_noise_and_state_bearing(self):
        self.assertEqual(NOISE_EVENTS & set(EVENT_STATE_RULES), set())

    def test_profile_lookup_zero_rows_is_not_found(self):
        self.assertEqual(state_of("profile_lookup_completed", rows="0", http_status="200"), "NOT_FOUND")

    def test_profile_lookup_http_404_is_not_found(self):
        self.assertEqual(state_of("profile_lookup_completed", http_status="404"), "NOT_FOUND")

    def test_profile_lookup_one_row_is_found(self):
        self.assertEqual(state_of("profile_lookup_completed", rows="1", http_status="200"), "FOUND")

    def test_index_query_hit_is_found(self):
        self.assertEqual(state_of("index_query_completed", result="hit"), "FOUND")

    def test_index_query_positive_total_hits_is_found(self):
        self.assertEqual(state_of("index_query_completed", total_hits="2"), "FOUND")

    def test_index_query_miss_is_not_found(self):
        self.assertEqual(state_of("index_query_completed", result="miss"), "NOT_FOUND")

    def test_index_query_zero_total_hits_is_not_found(self):
        self.assertEqual(state_of("index_query_completed", total_hits="0"), "NOT_FOUND")

    def test_analytics_read_without_identifiers_is_anonymized(self):
        self.assertEqual(state_of("analytics_profile_read", direct_identifiers="0",
                                  email="null", phone="null"), "ANONYMIZED")
        self.assertEqual(state_of("analytics_profile_read_completed", customer_reference="null",
                                  email="null", phone="null", email_hash="null"), "ANONYMIZED")

    def test_analytics_read_with_an_identifier_is_identifiable(self):
        self.assertEqual(state_of("analytics_profile_read", direct_identifiers="1",
                                  email="a.b@example.com", phone="null"), "IDENTIFIABLE")
        self.assertEqual(state_of("analytics_profile_read_completed", customer_reference="user_5",
                                  email="null", phone="null", email_hash="null"), "IDENTIFIABLE")

    def test_support_ticket_read_with_requester_email_is_identifiable(self):
        self.assertEqual(state_of("support_ticket_read_completed", customer_reference="null",
                                  requester_name="null", requester_email="r.rao@example.com",
                                  requester_phone="null"), "IDENTIFIABLE")

    def test_support_ticket_read_without_requester_details_is_redacted(self):
        self.assertEqual(state_of("support_ticket_read_completed", customer_reference="null",
                                  requester_name="null", requester_email="null",
                                  requester_phone="null"), "REDACTED")

    def test_scans_with_matched_value_are_identifiable(self):
        for event in ("analytics_identity_scan", "message_pii_scan", "attachment_pii_scan"):
            with self.subTest(event=event):
                self.assertEqual(state_of(event, matched_value="n.farr@example.com",
                                          detector="direct_identifier"), "IDENTIFIABLE")

    def test_message_read_with_redaction_marker_is_redacted(self):
        self.assertEqual(state_of("message_read", subject_ref="user_461",
                                  message_text="[REDACTED]"), "REDACTED")

    def test_message_read_with_an_email_in_the_text_is_identifiable(self):
        self.assertEqual(state_of("message_read", message_text="Write to me at o.kay@example.com"),
                         "IDENTIFIABLE")

    # BUG: logs.EVENT_STATE_RULES["message_read"] uses support_exposed(), which only looks for
    # strong identifiers, so an unredacted message naming the customer is reported REDACTED.
    # Spec T6: message_read -> message_text="[REDACTED]" -> REDACTED, else IDENTIFIABLE.
    @unittest.expectedFailure
    def test_message_read_with_unredacted_text_is_identifiable(self):
        self.assertEqual(state_of("message_read", subject_ref="user_461",
                                  message_text="Hi, this is Ingrid Dahlberg, please call me back"),
                         "IDENTIFIABLE")

    def test_connector_events_enabled_true_is_active(self):
        for event in ("connector_config_read_completed", "connector_heartbeat",
                      "connector_config_loaded"):
            with self.subTest(event=event):
                self.assertEqual(state_of(event, enabled="true"), "ACTIVE")

    def test_connector_events_enabled_false_is_disabled(self):
        for event in ("connector_config_read_completed", "connector_heartbeat",
                      "connector_config_loaded"):
            with self.subTest(event=event):
                self.assertEqual(state_of(event, enabled="false"), "DISABLED")

    def test_restore_policy_allowed_is_active_and_denied_is_blocked(self):
        self.assertEqual(state_of("restore_policy_read_completed", archive_present="true",
                                  restore_allowed="true"), "ACTIVE")
        self.assertEqual(state_of("restore_policy_read_completed", archive_present="true",
                                  restore_allowed="false"), "BLOCKED")

    def test_restore_candidate_eligible_and_unblocked_is_active(self):
        self.assertEqual(state_of("restore_candidate_evaluated", eligible="true",
                                  deletion_block="false"), "ACTIVE")

    def test_restore_candidate_otherwise_is_blocked(self):
        for eligible, block in (("true", "true"), ("false", "false"), ("false", "true")):
            with self.subTest(eligible=eligible, deletion_block=block):
                self.assertEqual(state_of("restore_candidate_evaluated", eligible=eligible,
                                          deletion_block=block), "BLOCKED")

    def test_restore_authorization_deny_is_blocked_and_allow_is_active(self):
        self.assertEqual(state_of("restore_authorization_completed", decision="deny"), "BLOCKED")
        self.assertEqual(state_of("restore_authorization_completed", decision="allow"), "ACTIVE")

    def test_audit_record_read_with_rows_is_found(self):
        self.assertEqual(state_of("audit_record_read_completed", rows="1",
                                  integrity_check="passed"), "FOUND")

    def test_export_checks_with_available_true_are_found(self):
        for event in ("export_download_check_completed", "export_metadata_read_completed"):
            with self.subTest(event=event):
                self.assertEqual(state_of(event, available="true"), "FOUND")

    def test_contact_lookups_with_rows_are_found(self):
        for event in ("contact_lookup_completed", "contact_query_completed"):
            with self.subTest(event=event):
                self.assertEqual(state_of(event, rows="1"), "FOUND")

    def test_record_lookup_uses_the_explicit_result(self):
        for result in ("FOUND", "NOT_FOUND", "REDACTED", "ACTIVE"):
            with self.subTest(result=result):
                self.assertEqual(state_of("record_lookup", result=result), result)

    def test_deletion_marker_not_found_means_record_still_present(self):
        self.assertEqual(state_of("deletion_marker_check", marker_found="false"), "FOUND")

    def test_deletion_marker_found_means_record_absent(self):
        self.assertEqual(state_of("deletion_marker_check", marker_found="true"), "NOT_FOUND")


class TraceJoinTests(unittest.TestCase):
    """Step 2: started lines join their result line by trace_id across interleaved traces."""

    @classmethod
    def setUpClass(cls):
        cls.obs, cls.cov, cls.warnings = parse_log(FIXTURES / "interleaved_traces.log")

    def test_parses_without_warnings(self):
        self.assertEqual(self.warnings, [])

    def test_logs_give_no_coverage(self):
        self.assertEqual(self.cov, [])

    def test_only_state_bearing_lines_become_observations(self):
        self.assertEqual([o.resource_id for o in self.obs],
                         ["account_7023", "search_1874", "analytics_9914"])

    def test_started_lookup_key_joins_into_identifiers(self):
        self.assertIn("g.silvano@example.com", only(self.obs, "search_1874").identifiers)
        self.assertIn("+447700900123", only(self.obs, "account_7023").identifiers)

    def test_join_does_not_cross_into_other_traces(self):
        self.assertNotIn("+447700900123", only(self.obs, "search_1874").identifiers)
        self.assertNotIn("g.silvano@example.com", only(self.obs, "account_7023").identifiers)
        self.assertNotIn("started", only(self.obs, "analytics_9914").fields)

    def test_started_line_kv_is_kept_in_fields(self):
        started = only(self.obs, "search_1874").fields["started"]
        self.assertEqual(started["match_key"], "G.SILVANO@EXAMPLE.COM")

    def test_states_follow_the_event_rules(self):
        self.assertEqual([o.state for o in self.obs], ["NOT_FOUND", "FOUND", "FOUND"])

    def test_observed_at_is_the_line_time(self):
        self.assertEqual(only(self.obs, "account_7023").observed_at, "2026-09-15T08:51:48Z")

    def test_captured_at_is_the_manifest_time(self):
        self.assertEqual({o.captured_at for o in self.obs}, {MANIFEST_CAPTURED_AT})

    def test_correlation_key_hash_becomes_a_hash_identifier(self):
        self.assertEqual(only(self.obs, "analytics_9914").identifiers, frozenset({OLD_EMAIL_HASH}))

    def test_source_is_the_physical_line_number(self):
        self.assertEqual(only(self.obs, "account_7023").source, "log:L5")
        self.assertEqual(only(self.obs, "search_1874").source, "log:L8")  # after a blank line

    def test_system_comes_from_the_resource_prefix(self):
        self.assertEqual(only(self.obs, "account_7023").system_id, "account_service")

    def test_fields_carry_level_service_and_trace(self):
        fields = only(self.obs, "search_1874").fields
        self.assertEqual((fields["level"], fields["service"], fields["trace_id"]),
                         ("INFO", "search_service", "tr-1000001"))


class LogFileEdgeCaseTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, *lines: str) -> Path:
        path = self.tmp / "service.log"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_bad_lines_warn_with_file_and_line_number(self):
        obs, _, warns = parse_log(FIXTURES / "unparseable.log")
        self.assertEqual(obs, [])
        self.assertEqual(len(warns), 3)
        self.assertTrue(all(w.startswith("evidence_t3 line ") for w in warns), warns)
        self.assertIn("line 2", warns[0])  # no timestamp
        self.assertIn("mystery_event", warns[1])  # unrecognised event
        self.assertIn("without resource_id", warns[2])

    def test_noise_line_with_a_resource_id_gives_no_observation_or_warning(self):
        path = self.write('2026-09-17T22:00:08Z level=ERROR service=attachment_preview '
                          'instance=preview-eu-02 event=thumbnail_failed resource_id=support_7731 '
                          'reason=unsupported_media retry=false')
        self.assertEqual(parse_log(path), ([], [], []))

    def test_every_noise_event_is_recognised_silently(self):
        lines = [f"2026-09-15T08:{i:02d}:00Z level=DEBUG service=x trace_id=tr-{i} event={event}"
                 for i, event in enumerate(sorted(NOISE_EVENTS))]
        self.assertEqual(parse_log(self.write(*lines)), ([], [], []))

    def test_started_line_after_its_result_still_joins(self):
        path = self.write(
            '2026-09-15T08:00:02Z level=INFO service=account_service trace_id=tr-7 '
            'resource_id=account_1 event=profile_lookup_completed rows=1 http_status=200',
            '2026-09-15T08:00:01Z level=DEBUG service=account_service trace_id=tr-7 '
            'event=profile_lookup_started lookup_key="late.key@example.com"')
        obs, _, warns = parse_log(path)
        self.assertEqual(warns, [])
        self.assertIn("late.key@example.com", only(obs, "account_1").identifiers)

    def test_line_without_trace_keeps_its_own_lookup_key(self):
        path = self.write('2026-09-17T22:00:11Z level=INFO service=support_replica '
                          'event=contact_query_completed resource_id=support_5914 '
                          'lookup_key=n.popa@example.com rows=1 source=case_archive')
        obs, _, _ = parse_log(path)
        self.assertEqual(only(obs, "support_5914").identifiers, frozenset({"n.popa@example.com"}))

    def test_event_collections_give_spec_categories(self):
        path = self.write(
            '2026-02-28T10:57:57Z [INFO] [support_platform] [tr-1] message_read: '
            'resource_id="support_2659" message_text="[REDACTED]"',
            '2026-02-03T05:59:27Z | WARN | support_platform | tr-2 | attachment_pii_scan | '
            'resource_id="support_4533" matched_value="k.ion@example.com"',
            '2026-07-28T05:57:08Z [WARN] [integration_service] [tr-3] connector_heartbeat: '
            'resource_id="data_flow_7004" enabled="true"',
            '2026-02-28T10:51:46Z [INFO] [backup_service] [tr-4] restore_authorization_completed: '
            'resource_id="backup_7400" decision="deny"',
            '2026-07-03T07:51:32Z | DEBUG | security_service | tr-5 | audit_record_read_completed | '
            'resource_id="audit_record_4413" rows="1"',
            '2026-07-03T07:57:01Z | INFO | legal_service | tr-6 | retained_order_loaded | '
            'resource_id="legal_hold_4961" access="allowed"')
        obs, _, warns = parse_log(path)
        self.assertEqual(warns, [])
        self.assertEqual({o.resource_id: (o.category, o.state) for o in obs}, {
            "support_2659": ("SUPPORT_MESSAGE", "REDACTED"),
            "support_4533": ("SUPPORT_ATTACHMENT", "IDENTIFIABLE"),
            "data_flow_7004": ("DATA_FLOW", "ACTIVE"),
            "backup_7400": ("BACKUP_ARCHIVE", "BLOCKED"),
            "audit_record_4413": ("SECURITY_AUDIT_LOG", "FOUND"),
            "legal_hold_4961": ("LEGAL_HOLD_RECORD", "FOUND"),
        })

    def test_record_lookup_record_type_wins_over_collection(self):
        obs, _, _ = parse_log(self.write(RECORD_LOOKUP))
        o = only(obs, "account_022")
        self.assertEqual((o.category, o.state, o.system_id),
                         ("USER_PROFILE", "NOT_FOUND", "account_service"))
        self.assertEqual(o.identifiers, frozenset({"z.nowak@example.com"}))


if __name__ == "__main__":
    unittest.main()
