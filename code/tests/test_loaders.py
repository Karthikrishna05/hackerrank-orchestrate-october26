"""Tests for ghostaudit.loaders (tools_spec T1: dataset loader)."""

from __future__ import annotations

import csv
import io
import tempfile
import unittest
from dataclasses import astuple, fields
from pathlib import Path

from ghostaudit import loaders
from ghostaudit.models import Activity, AuditChange, ManifestEntry, Request, Rule, UserIdentity

DATASET = Path(__file__).resolve().parents[2] / "dataset"

REQUEST_HEADER = "case_id,email,requested_at,reported_status,reported_at,audit_at,request_text"
ACTIVITY_HEADER = ("activity_id,event_time,event_type,source_system,target_system,"
                   "resource_id,reported_result,details")


class TempDirTestCase(unittest.TestCase):
    """Gives each test a scratch directory and a helper that writes UTF-8 text files into it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def write(self, name: str, text: str, encoding: str = "utf-8") -> Path:
        path = self.dir / name
        path.write_text(text, encoding=encoding, newline="")
        return path


class TestLoadRequests(TempDirTestCase):
    def test_load_requests_reads_rows_in_file_order_as_request_dataclasses(self):
        path = self.write("req.csv", REQUEST_HEADER + "\n"
                          "case_7,b@x.com,2026-01-02T00:00:00Z,COMPLETE,2026-01-03T00:00:00Z,2026-01-04T00:00:00Z,Delete me\n"
                          "case_3,a@x.com,2026-01-01T00:00:00Z,NOT_STARTED,2026-01-02T00:00:00Z,2026-01-05T00:00:00Z,Remove\n")
        rows = loaders.load_requests(path=path)
        self.assertEqual([r.case_id for r in rows], ["case_7", "case_3"])
        self.assertIsInstance(rows[0], Request)
        self.assertEqual(rows[0], Request("case_7", "b@x.com", "2026-01-02T00:00:00Z", "COMPLETE",
                                          "2026-01-03T00:00:00Z", "2026-01-04T00:00:00Z", "Delete me"))

    def test_load_requests_uses_name_inside_a_given_dataset_dir(self):
        self.write("my_requests.csv", REQUEST_HEADER + "\ncase_1,a@x.com,t1,COMPLETE,t2,t3,hi\n")
        rows = loaders.load_requests("my_requests.csv", dataset_dir=self.dir)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].case_id, "case_1")

    def test_load_requests_strips_whitespace_from_every_value(self):
        path = self.write("req.csv", REQUEST_HEADER + "\n"
                          "  case_1 , A@X.COM ,2026-01-01T00:00:00Z , COMPLETE,t2,t3,  please delete  \n")
        row = loaders.load_requests(path=path)[0]
        self.assertEqual(row.case_id, "case_1")
        self.assertEqual(row.email, "A@X.COM")  # the loader strips but does not normalise case
        self.assertEqual(row.reported_status, "COMPLETE")
        self.assertEqual(row.request_text, "please delete")
        for value in astuple(row):
            self.assertIsInstance(value, str)

    def test_load_requests_keeps_quoted_commas_and_newlines_in_request_text(self):
        path = self.write("req.csv", REQUEST_HEADER + "\n"
                          'case_1,a@x.com,t1,COMPLETE,t2,t3,"Hi, please delete.\nThanks, Ana"\n')
        self.assertEqual(loaders.load_requests(path=path)[0].request_text, "Hi, please delete.\nThanks, Ana")

    def test_load_requests_preserves_japanese_korean_and_accented_text(self):
        texts = ["アカウントを削除してください。", "계정을 삭제해 주세요.", "Intenté eliminar mi cuenta, Mateo Ríos"]
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(REQUEST_HEADER.split(","))
        for i, text in enumerate(texts, 1):
            writer.writerow([f"case_{i}", "a@x.com", "t1", "COMPLETE", "t2", "t3", text])
        path = self.write("req.csv", buf.getvalue())
        self.assertEqual([r.request_text for r in loaders.load_requests(path=path)], texts)

    def test_load_requests_accepts_a_utf8_bom(self):
        path = self.write("req.csv", REQUEST_HEADER + "\ncase_1,a@x.com,t1,COMPLETE,t2,t3,x\n",
                          encoding="utf-8-sig")
        self.assertEqual(loaders.load_requests(path=path)[0].case_id, "case_1")

    def test_load_requests_ignores_extra_columns(self):
        path = self.write("req.csv", REQUEST_HEADER + ",note\ncase_1,a@x.com,t1,COMPLETE,t2,t3,x,ignored\n")
        self.assertEqual(loaders.load_requests(path=path)[0].request_text, "x")

    def test_load_requests_raises_value_error_naming_a_missing_column(self):
        path = self.write("req.csv", "case_id,email\ncase_1,a@x.com\n")
        with self.assertRaises(ValueError) as ctx:
            loaders.load_requests(path=path)
        self.assertIn("audit_at", str(ctx.exception))

    def test_load_requests_raises_value_error_with_line_number_for_short_row(self):
        path = self.write("req.csv", REQUEST_HEADER + "\n"
                          "case_1,a@x.com,t1,COMPLETE,t2,t3,ok\n"
                          "case_2,b@x.com,t1\n")
        with self.assertRaises(ValueError) as ctx:
            loaders.load_requests(path=path)
        self.assertIn("line 3", str(ctx.exception))

    def test_load_requests_raises_value_error_for_row_with_too_many_fields(self):
        path = self.write("req.csv", REQUEST_HEADER + "\ncase_1,a@x.com,t1,COMPLETE,t2,t3,text,unexpected\n")
        with self.assertRaises(ValueError):
            loaders.load_requests(path=path)

    def test_load_requests_raises_value_error_for_empty_file(self):
        path = self.write("req.csv", "")
        with self.assertRaises(ValueError):
            loaders.load_requests(path=path)

    def test_load_requests_header_only_gives_empty_list(self):
        path = self.write("req.csv", REQUEST_HEADER + "\n")
        self.assertEqual(loaders.load_requests(path=path), [])


class TestOtherLoaders(TempDirTestCase):
    def test_load_identities_returns_user_identity_rows(self):
        path = self.write("ids.csv", "user_id,name,email,phone,email_hash,updated_at\n"
                          "user_02,Mateo Ríos,mateo.rios@outlook.com,+34 612 847 593,abc,2026-04-24T07:44:00Z\n")
        rows = loaders.load_identities(path=path)
        self.assertEqual(rows, [UserIdentity("user_02", "Mateo Ríos", "mateo.rios@outlook.com",
                                             "+34 612 847 593", "abc", "2026-04-24T07:44:00Z")])

    def test_load_audit_log_keeps_file_order_and_empty_values(self):
        path = self.write("audit.csv", "audit_id,user_id,event_time,field_name,old_value,new_value\n"
                          "audit_2,user_1,2026-02-01T00:00:00Z,email,,new@x.com\n"
                          "audit_1,user_1,2026-01-01T00:00:00Z,phone,+1 555 0100,+1 555 0199\n")
        rows = loaders.load_audit_log(path=path)
        self.assertEqual([r.audit_id for r in rows], ["audit_2", "audit_1"])
        self.assertIsInstance(rows[0], AuditChange)
        self.assertEqual(rows[0].old_value, "")

    def test_load_rules_reads_quoted_condition_with_commas(self):
        path = self.write("rules.csv", "rule_id,data_category,condition,required_action\n"
                          'rule_05,SUPPORT_MESSAGE,"Shared customer, agent, or order context",REDACT\n')
        self.assertEqual(loaders.load_rules(path=path),
                         [Rule("rule_05", "SUPPORT_MESSAGE", "Shared customer, agent, or order context", "REDACT")])

    def test_load_manifest_keeps_file_path_relative(self):
        path = self.write("manifest.csv", "file_id,file_path,origin,captured_at\n"
                          "evidence_01,evidence/logs/a.log,central_export,2026-09-15T11:59:39Z\n")
        self.assertEqual(loaders.load_manifest(path=path),
                         [ManifestEntry("evidence_01", "evidence/logs/a.log", "central_export",
                                        "2026-09-15T11:59:39Z")])

    def test_each_loader_finds_its_default_file_name_in_dataset_dir(self):
        self.write("user_identities.csv", "user_id,name,email,phone,email_hash,updated_at\nu,n,e,p,h,t\n")
        self.write("user_audit_log.csv", "audit_id,user_id,event_time,field_name,old_value,new_value\n"
                   "audit_1,u,2026-01-01T00:00:00Z,phone,a,b\n")
        self.write("system_activity.csv", ACTIVITY_HEADER + "\nactivity_1,2026-01-01T00:00:00Z,E,s,t,r,OK,d\n")
        self.write("deletion_rules.csv", "rule_id,data_category,condition,required_action\nr,C,Always,DELETE\n")
        self.write("evidence_files.csv", "file_id,file_path,origin,captured_at\nevidence_1,p,o,t\n")
        self.write("deletion_requests.csv", REQUEST_HEADER + "\ncase_1,a,b,c,d,e,f\n")
        for loader in (loaders.load_identities, loaders.load_audit_log, loaders.load_activity,
                       loaders.load_rules, loaders.load_manifest, loaders.load_requests):
            with self.subTest(loader=loader.__name__):
                self.assertEqual(len(loader(dataset_dir=self.dir)), 1)


class TestLoadActivity(TempDirTestCase):
    def test_load_activity_sorts_rows_by_event_time(self):
        path = self.write("act.csv", ACTIVITY_HEADER + "\n"
                          "activity_1,2026-03-01T00:00:00Z,DELETE_RECORD,a,b,account_1,SUCCESS,later\n"
                          "activity_2,2026-01-01T00:00:00Z,INDEX_RECORD,a,b,search_1,SUCCESS,earliest\n"
                          "activity_3,2026-02-01T00:00:00Z,SYNC_RECORD,a,b,data_flow_9522,FAILED,middle\n")
        rows = loaders.load_activity(path=path)
        self.assertEqual([r.activity_id for r in rows], ["activity_2", "activity_3", "activity_1"])
        self.assertIsInstance(rows[0], Activity)

    def test_load_activity_breaks_time_ties_by_numeric_activity_id(self):
        same = "2026-01-01T00:00:00Z"
        path = self.write("act.csv", ACTIVITY_HEADER + "\n"
                          f"activity_10,{same},E,a,b,r,OK,d\n"
                          f"activity_9,{same},E,a,b,r,OK,d\n"
                          f"activity_100,{same},E,a,b,r,OK,d\n")
        rows = loaders.load_activity(path=path)
        # Numeric, not lexicographic ("activity_10" < "activity_9" as strings).
        self.assertEqual([r.activity_id for r in rows], ["activity_9", "activity_10", "activity_100"])

    def test_load_activity_compares_times_as_datetimes_not_strings(self):
        path = self.write("act.csv", ACTIVITY_HEADER + "\n"
                          "activity_1,2026-01-01T09:00:00Z,E,a,b,r,OK,nine utc\n"
                          "activity_2,2026-01-01T10:00:00+02:00,E,a,b,r,OK,eight utc\n")
        rows = loaders.load_activity(path=path)
        self.assertEqual([r.activity_id for r in rows], ["activity_2", "activity_1"])

    def test_load_activity_keeps_reported_result_and_details_verbatim(self):
        path = self.write("act.csv", ACTIVITY_HEADER + "\n"
                          'activity_1,2026-01-01T00:00:00Z,RESTORE,a,b,backup_1,SUCCESS,"Restore job repopulated downstream resource search_4882, as planned"\n')
        row = loaders.load_activity(path=path)[0]
        self.assertEqual(row.reported_result, "SUCCESS")
        self.assertEqual(row.details, "Restore job repopulated downstream resource search_4882, as planned")


@unittest.skipUnless(DATASET.is_dir(), "dataset not available")
class TestLoadersOnRealDataset(unittest.TestCase):
    """Read-only acceptance checks from tools_spec T1."""

    def test_row_counts_match_the_spec(self):
        self.assertEqual(len(loaders.load_requests()), 518)
        self.assertEqual(len(loaders.load_requests("sample_deletion_requests.csv")), 32)
        self.assertEqual(len(loaders.load_identities()), 850)
        self.assertEqual(len(loaders.load_audit_log()), 306)
        self.assertEqual(len(loaders.load_activity()), 4011)
        self.assertEqual(len(loaders.load_rules()), 15)
        self.assertEqual(len(loaders.load_manifest()), 94)

    def test_every_loaded_value_is_a_stripped_string(self):
        for loader in (loaders.load_requests, loaders.load_identities, loaders.load_audit_log,
                       loaders.load_activity, loaders.load_rules, loaders.load_manifest):
            with self.subTest(loader=loader.__name__):
                for row in loader():
                    for f in fields(row):
                        value = getattr(row, f.name)
                        self.assertIsInstance(value, str)
                        self.assertEqual(value, value.strip())

    def test_activity_is_sorted_even_though_the_raw_file_is_not(self):
        from ghostaudit.timeutil import parse_ts
        with open(DATASET / "system_activity.csv", encoding="utf-8", newline="") as fh:
            raw = [r["activity_id"] for r in csv.DictReader(fh)]
        rows = loaders.load_activity()
        keys = [(parse_ts(a.event_time), int(a.activity_id.rsplit("_", 1)[1])) for a in rows]
        self.assertEqual(keys, sorted(keys))
        self.assertNotEqual([a.activity_id for a in rows], raw)

    def test_japanese_and_korean_request_text_survives_a_csv_round_trip(self):
        rows = loaders.load_requests()
        cjk = [r for r in rows if any("぀" <= ch <= "ヿ" or "가" <= ch <= "힯"
                                      for ch in r.request_text)]
        self.assertTrue(cjk, "expected Japanese or Korean request texts in the dataset")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "round_trip.csv"
            with open(path, "w", encoding="utf-8", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow([f.name for f in fields(Request)])
                writer.writerows(astuple(r) for r in cjk)
            self.assertEqual(loaders.load_requests(path=path), cjk)


if __name__ == "__main__":
    unittest.main()
