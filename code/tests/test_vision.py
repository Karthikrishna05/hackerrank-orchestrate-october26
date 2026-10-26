"""Tests for ghostaudit.vision (engine_plan.md §3 A2): screenshot transcription, label->state rules, link check.

Model calls go to the local fake OpenRouter server from test_llm.py (dummy key, temp cache). The screenshot
sent is a tiny PNG built in the test; every extraction below is synthetic and uses synthetic identifiers.
"""

from __future__ import annotations

import base64
import csv
import json
import struct
import tempfile
import unittest
import warnings
import zlib
from pathlib import Path
from unittest import mock

from ghostaudit import llm, vision
from ghostaudit.models import STATES, Activity, ManifestEntry
from ghostaudit.parsers import images
from ghostaudit.paths import dataset_dir
from tests.test_llm import (FakeOpenRouter, RepoCacheGuard, TempCacheMixin, chat_body, error_reply, make_client,
                            openrouter_env, repo_cache_snapshot, reply)

_SNAPSHOT_AT_IMPORT = repo_cache_snapshot()

CAPTURED = "2026-08-07T08:00:00Z"
VISION_MODEL = "synthetic/fake-vision-model"
SYN_EMAIL = "synthetic.person@example.test"


def tiny_png() -> bytes:
    """A valid 1x1 RGB PNG built in memory."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\x10\x20\x30")) + chunk(b"IEND", b""))


def entry(file_id="evidence_901", path="evidence/images/synthetic_connector_console.png", captured=CAPTURED):
    return ManifestEntry(file_id=file_id, file_path=path, origin="synthetic_admin", captured_at=captured)


def link_row(activity_id="activity_9901", time="2026-08-07T08:07:01Z", resource="data_flow_901", alias=None,
             details=None, target="integration_service", event="LINK_RESOURCE_IDENTITY"):
    details = details or (f"Data catalog linked {resource} ({alias or resource}) to {SYN_EMAIL} and user_901.")
    return Activity(activity_id=activity_id, event_time=time, event_type=event, source_system="data_catalog",
                    target_system=target, resource_id=resource, reported_result="SUCCESS", details=details)


def link(resource, alias="", identifiers=(SYN_EMAIL, "user_901"), activity_id="activity_9901", target=""):
    return {"activity_id": activity_id, "resource_id": resource, "target_system": target, "alias": alias,
            "identifiers": sorted(identifiers)}


def raw_rec(resource_id, status="", fields=(), **lists):
    rec = {"resource_id": resource_id, "record_label": "", "status": status,
           "fields": [{"name": n, "value": v} for n, v in fields]}
    rec.update({k: list(lists.get(k, [])) for k in vision.LIST_KEYS})
    return rec


def merged(statuses=(), fields=(), names=()):
    """A merged record as map_state receives it."""
    return {"statuses": list(statuses), "fields": [{"name": n, "value": v} for n, v in fields], "names": list(names)}


def state(category, statuses=(), fields=(), names=(), strong=()):
    return vision.map_state(category, merged(statuses, fields, names), set(strong))[0]


class LabelTableTests(unittest.TestCase):
    """engine_plan.md §3 A2: the model reports labels; deterministic rules turn labels into states."""

    def test_table_rows(self):
        table = {cat: {s: set(words) for s, words in rows} for cat, rows in vision.LABEL_STATES.items()}
        self.assertEqual(table["DATA_FLOW"], {"ACTIVE": {"healthy", "continuous", "enabled"},
                                              "DISABLED": {"disabled", "paused", "stopped"}})
        self.assertEqual(table["USER_EXPORT"], {"FOUND": {"ready", "collecting", "packaging", "processing"},
                                                "NOT_FOUND": {"expired", "deleted"}})

    def test_connector_labels(self):
        for label in ("Healthy", "Continuous", "Enabled", "HEALTHY", "Sync: continuous"):
            self.assertEqual(state("DATA_FLOW", [label]), "ACTIVE", label)
        for label in ("Disabled", "Paused", "Stopped", "paused"):
            self.assertEqual(state("DATA_FLOW", [label]), "DISABLED", label)

    def test_connector_label_from_a_status_like_field(self):
        self.assertEqual(state("DATA_FLOW", fields=[("Sync mode", "Continuous")]), "ACTIVE")
        self.assertEqual(state("DATA_FLOW", fields=[("Status", "Stopped")]), "DISABLED")
        self.assertEqual(state("DATA_FLOW", fields=[("Owner", "Disabled team")]), "UNKNOWN")  # not a label field

    def test_export_labels(self):
        for label in ("Ready", "Collecting", "Packaging", "Processing"):
            self.assertEqual(state("USER_EXPORT", [label]), "FOUND", label)
        for label in ("Expired", "Deleted"):
            self.assertEqual(state("USER_EXPORT", [label]), "NOT_FOUND", label)

    def test_unknown_or_missing_labels_are_unknown(self):
        for category in ("DATA_FLOW", "USER_EXPORT"):
            self.assertEqual(state(category, ["Queued for review"]), "UNKNOWN", category)
            self.assertEqual(state(category, []), "UNKNOWN", category)
            self.assertEqual(state(category, [""]), "UNKNOWN", category)

    def test_rule_names_the_label(self):
        st, rule = vision.map_state("DATA_FLOW", merged(["Healthy"]), set())
        self.assertEqual(st, "ACTIVE")
        self.assertIn("Healthy", rule)


class BackupStateTests(unittest.TestCase):

    def test_owner_decision_7_backup_without_restore_label_is_active(self):
        self.assertEqual(state("BACKUP_ARCHIVE"), "ACTIVE")
        self.assertEqual(state("BACKUP_ARCHIVE", fields=[("Region", "eu-west-1"), ("Encryption", "AES-256"),
                                                         ("Checksum", "Verified")]), "ACTIVE")
        self.assertIn("no restore label", vision.map_state("BACKUP_ARCHIVE", merged(), set())[1])

    def test_restore_allowed_is_active(self):
        self.assertEqual(state("BACKUP_ARCHIVE", ["Restore allowed"]), "ACTIVE")
        self.assertEqual(state("BACKUP_ARCHIVE", fields=[("Restore", "Allowed")]), "ACTIVE")
        self.assertEqual(state("BACKUP_ARCHIVE", fields=[("Restore allowed", "Yes")]), "ACTIVE")
        self.assertEqual(state("BACKUP_ARCHIVE", fields=[("Restore eligibility", "Eligible")]), "ACTIVE")

    def test_restore_blocked_or_not_allowed_is_blocked(self):
        self.assertEqual(state("BACKUP_ARCHIVE", ["Restore blocked"]), "BLOCKED")
        self.assertEqual(state("BACKUP_ARCHIVE", ["Restore not allowed"]), "BLOCKED")
        self.assertEqual(state("BACKUP_ARCHIVE", fields=[("Restore", "Not allowed")]), "BLOCKED")
        self.assertEqual(state("BACKUP_ARCHIVE", fields=[("Restore", "Blocked")]), "BLOCKED")
        self.assertEqual(state("BACKUP_ARCHIVE", fields=[("Restore blocked", "Yes")]), "BLOCKED")
        self.assertEqual(state("BACKUP_ARCHIVE", fields=[("Restore allowed", "No")]), "BLOCKED")


class ExposureStateTests(unittest.TestCase):

    def test_redact_and_anonymize_categories_with_identifiers_are_identifiable(self):
        for category in ("ANALYTICS_PROFILE", "SUPPORT_MESSAGE", "EMAIL_DELIVERY_LOG"):
            self.assertEqual(state(category, strong={SYN_EMAIL}), "IDENTIFIABLE", category)
            self.assertEqual(state(category), "FOUND", category)

    def test_tombstone_keeps_user_id_only(self):
        self.assertEqual(state("ACCOUNT_TOMBSTONE", strong={"user_903"}), "FOUND")
        self.assertEqual(state("ACCOUNT_TOMBSTONE", strong={"user_903", SYN_EMAIL}), "IDENTIFIABLE")
        self.assertEqual(state("ACCOUNT_TOMBSTONE", names=["Synthetic Person"], strong={"user_903"}), "IDENTIFIABLE")

    def test_other_categories_are_found(self):
        self.assertEqual(state("USER_PROFILE", strong={SYN_EMAIL}), "FOUND")
        self.assertEqual(state(None), "FOUND")

    def test_every_mapped_state_is_a_known_observation_state(self):
        for st in ("ACTIVE", "DISABLED", "BLOCKED", "FOUND", "NOT_FOUND", "IDENTIFIABLE", "UNKNOWN"):
            self.assertIn(st, STATES)


class ExtractionSchemaTests(unittest.TestCase):

    def test_check_extraction(self):
        vision.check_extraction({"records": []})
        vision.check_extraction({"records": [{"resource_id": "account_1"}]})
        for bad in ({}, {"records": {}}, {"records": ["account_1"]}, {"records": [{"resource_id": ""}]},
                    {"records": [{"resource_id": 5}]}, {"records": [{"status": "Ready"}]}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                vision.check_extraction(bad)

    def test_normalize_extraction_coerces_types(self):
        out = vision.normalize_extraction({"screen_title": ["Synthetic", "console"], "records": [
            {"resource_id": "  `backup_905` ", "status": ["Restore", "allowed"], "record_label": None,
             "fields": [{"name": "Size", "value": 84.7}, "not a field"], "emails": SYN_EMAIL, "user_ids": None,
             "timestamps": [None, "26 Apr 02:00", ""]}]})
        self.assertEqual(out["screen_title"], "Synthetic console")
        (r,) = out["records"]
        self.assertEqual(r["resource_id"], "backup_905")
        self.assertEqual(r["status"], "Restore allowed")
        self.assertEqual(r["record_label"], "")
        self.assertEqual(r["fields"], [{"name": "Size", "value": "84.7"}])
        self.assertEqual(r["emails"], [SYN_EMAIL])
        self.assertEqual(r["user_ids"], [])
        self.assertEqual(r["timestamps"], ["26 Apr 02:00"])
        self.assertEqual((r["phones"], r["names"]), ([], []))


class LinksForTests(unittest.TestCase):

    def test_window_after_capture(self):
        rows = [link_row("activity_1", "2026-08-07T08:07:01Z"),
                link_row("activity_2", "2026-08-07T08:00:00Z"),  # same instant: not after the capture
                link_row("activity_3", "2026-08-07T08:10:00Z"),  # exactly 10 min: inside
                link_row("activity_4", "2026-08-07T08:10:01Z"),  # outside
                link_row("activity_5", "2026-08-07T07:59:00Z"),  # before
                link_row("activity_6", "2026-08-07T08:05:00Z", event="DELETE_RECORD")]
        got = vision.links_for(entry(), rows)
        self.assertEqual([l["activity_id"] for l in got], ["activity_1", "activity_3"])

    def test_alias_and_identifiers(self):
        (l,) = vision.links_for(entry(), [link_row(resource="export_902", alias="NST-SYN-0902",
                                                   target="export_service")])
        self.assertEqual(l, {"activity_id": "activity_9901", "resource_id": "export_902",
                             "target_system": "export_service", "alias": "NST-SYN-0902",
                             "identifiers": [SYN_EMAIL, "user_901"]})


class MapExtractionTests(unittest.TestCase):

    def mapped(self, records, links=()):
        return vision.map_extraction(entry(), {"screen_title": "t", "records": records}, list(links))

    def test_identifiers_are_normalised(self):
        items, _ = self.mapped([raw_rec("analytics_906", emails=["Synthetic.Person@Example.TEST"],
                                        phones=["+1 (555) 010-0199"], user_ids=["USER_901"],
                                        names=["  Synthetic   PERSON "])])
        (item,) = items
        self.assertEqual(item["identifiers"], ["+15550100199", SYN_EMAIL, "user_901", "synthetic person"])
        self.assertEqual(item["state"], "IDENTIFIABLE")

    def test_item_schema_system_category_and_observed_at(self):
        items, _ = self.mapped([raw_rec("data_flow_901", status="Healthy")])
        self.assertEqual(items, [{"system_id": "integration_service", "resource_id": "data_flow_901",
                                  "state": "ACTIVE", "category": "DATA_FLOW", "identifiers": [],
                                  "observed_at": CAPTURED, "labels": ["Healthy"],
                                  "rule": items[0]["rule"]}])

    def test_observed_at_is_the_manifest_capture_time_in_iso(self):
        items, _ = vision.map_extraction(entry(captured="2026-08-07 08:00:00+00:00"),
                                         {"records": [raw_rec("account_907")]}, [])
        self.assertEqual(items[0]["observed_at"], CAPTURED)

    def test_invalid_resource_ids_are_dropped_and_reported(self):
        items, report = self.mapped([raw_rec("Synthetic summary card"), raw_rec("user_5"),
                                     raw_rec("account_1 / account_2"), raw_rec("account_908")])
        self.assertEqual([i["resource_id"] for i in items], ["account_908"])
        self.assertEqual(report["dropped"], [
            {"resource_id": "Synthetic summary card", "reason": "not a resource id"},
            {"resource_id": "user_5", "reason": "a user id, not a resource"},
            {"resource_id": "account_1 / account_2", "reason": "not a resource id"}])

    def test_single_embedded_id_is_used_and_reported_as_alias(self):
        items, report = self.mapped([raw_rec("TCK-12 / support_77")])
        self.assertEqual([i["resource_id"] for i in items], ["support_77"])
        self.assertEqual(report["aliases"], [{"from": "TCK-12 / support_77", "to": "support_77", "activity_id": ""}])

    def test_catalog_alias_maps_reference_to_resource(self):
        links = [link("export_902", alias="NST-SYN-0902", activity_id="activity_9902", target="export_service"),
                 link("tombstone_903", alias="user_903", activity_id="activity_9903", identifiers=("user_903",))]
        items, report = self.mapped([raw_rec("NST-SYN-0902", status="Ready"), raw_rec("user_903")], links)
        self.assertEqual([(i["resource_id"], i["state"]) for i in items],
                         [("export_902", "FOUND"), ("tombstone_903", "FOUND")])
        self.assertEqual(report["aliases"], [{"from": "NST-SYN-0902", "to": "export_902", "activity_id": "activity_9902"},
                                             {"from": "user_903", "to": "tombstone_903", "activity_id": "activity_9903"}])
        self.assertEqual(report["dropped"], [])

    def test_same_id_in_header_and_row_is_merged(self):
        items, _ = self.mapped([raw_rec("backup_909", emails=[SYN_EMAIL]),
                                raw_rec("backup_909", fields=[("Restore", "Blocked")])])
        (item,) = items
        self.assertEqual(item["state"], "BLOCKED")
        self.assertEqual(item["identifiers"], [SYN_EMAIL])

    def test_unknown_prefix_takes_the_link_system(self):
        items, _ = self.mapped([raw_rec("object_904")], [link("object_904", target="object_storage")])
        self.assertEqual((items[0]["system_id"], items[0]["category"], items[0]["state"]),
                         ("object_storage", None, "FOUND"))

    def test_items_in_natural_order(self):
        items, _ = self.mapped([raw_rec("account_10"), raw_rec("account_9"), raw_rec("account_100")])
        self.assertEqual([i["resource_id"] for i in items], ["account_9", "account_10", "account_100"])


class CrossCheckTests(unittest.TestCase):

    def statuses(self, records, links):
        _, report = vision.map_extraction(entry(), {"records": records}, links)
        return [(c.get("resource_id"), c["status"]) for c in report["cross_check"]]

    def test_no_link_row(self):
        self.assertEqual(self.statuses([raw_rec("account_910")], []), [(None, "no_link_row")])

    def test_confirmed(self):
        self.assertEqual(self.statuses([raw_rec("account_910", emails=["SYNTHETIC.person@example.test"])],
                                       [link("account_910")]), [("account_910", "confirmed")])

    def test_conflict_when_screen_shows_other_strong_identifiers(self):
        recs = [raw_rec("marketing_911", emails=["someone.else@example.test"], phones=["+82 10-0000-0001"],
                        names=["Other Synthetic Name"])]
        _, report = vision.map_extraction(entry(), {"records": recs}, [link("marketing_911")])
        (c,) = report["cross_check"]
        self.assertEqual(c["status"], "conflict")
        self.assertEqual(c["shown_identifiers"], ["+821000000001", "someone.else@example.test"])
        self.assertEqual(c["link_identifiers"], [SYN_EMAIL, "user_901"])

    def test_no_identifiers_when_only_a_name_is_shown(self):
        self.assertEqual(self.statuses([raw_rec("account_912", names=["Synthetic Person"])], [link("account_912")]),
                         [("account_912", "no_identifiers")])

    def test_resource_missing(self):
        self.assertEqual(self.statuses([raw_rec("account_913")], [link("account_999")]),
                         [("account_999", "resource_missing")])


class ParseRoundTripTests(TempCacheMixin, unittest.TestCase):
    """The mapped file is the schema parsers/images.py reads."""

    def test_mapped_items_load_through_images_parse(self):
        e = entry()
        recs = [raw_rec("data_flow_901", status="Paused", user_ids=["user_901"]),
                raw_rec("analytics_906", names=["Synthetic Person"], emails=[SYN_EMAIL]),
                raw_rec("backup_909"), raw_rec("export_902", status="Expired"), raw_rec("export_914", status="?")]
        items, _ = vision.map_extraction(e, {"records": recs}, [])
        llm.write_json(images.cache_path(e.file_id), items)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            obs, cov = images.parse(e, Path("unused.png"))
        self.assertEqual([str(w.message) for w in caught], [])
        self.assertEqual(cov, [])
        by_id = {o.resource_id: o for o in obs}
        self.assertEqual({rid: o.state for rid, o in by_id.items()},
                         {"data_flow_901": "DISABLED", "analytics_906": "IDENTIFIABLE", "backup_909": "ACTIVE",
                          "export_902": "NOT_FOUND", "export_914": "UNKNOWN"})
        for o in obs:
            self.assertEqual((o.observed_at, o.captured_at, o.file_id), (CAPTURED, CAPTURED, e.file_id))
        self.assertEqual(by_id["data_flow_901"].system_id, "integration_service")
        self.assertEqual(by_id["analytics_906"].identifiers, frozenset({SYN_EMAIL, "name:synthetic person"}))
        self.assertEqual(by_id["analytics_906"].category, "ANALYTICS_PROFILE")


class ScreenshotsTests(unittest.TestCase):

    def test_only_image_entries_in_natural_order(self):
        manifest = [entry("evidence_10", "evidence/images/b.PNG"), entry("evidence_2", "evidence/documents/a.pdf"),
                    entry("evidence_9", "evidence/images/a.png"), entry("evidence_3", "evidence/images/c.jpg"),
                    entry("evidence_4", "evidence/logs/x.log")]
        self.assertEqual([e.file_id for e in vision.screenshots(manifest)], ["evidence_3", "evidence_9", "evidence_10"])

    @unittest.skipUnless((dataset_dir() / "evidence_files.csv").is_file(), "dataset/ not available")
    def test_real_manifest_has_eleven_png_screenshots(self):
        shots = vision.screenshots()
        with open(dataset_dir() / "evidence_files.csv", encoding="utf-8", newline="") as fh:
            expected = [r["file_id"] for r in csv.DictReader(fh) if r["file_path"].lower().endswith(".png")]
        self.assertEqual(len(shots), 11)
        self.assertEqual(sorted(e.file_id for e in shots), sorted(expected))
        self.assertTrue(all(e.file_path.lower().endswith(".png") for e in shots))
        self.assertEqual([e.file_id for e in shots], sorted(expected, key=lambda f: int(f.rsplit("_", 1)[1])))


SYNTH_EXTRACTION = {"screen_title": "Synthetic connector console", "records": [
    {"resource_id": "data_flow_901", "record_label": "connector", "status": "Healthy",
     "fields": [{"name": "Sync mode", "value": "Continuous"}, {"name": "Owner", "value": "Synthetic.Person@Example.test"}],
     "emails": [], "phones": [], "user_ids": ["user_901"], "names": [], "timestamps": ["07 Aug 2026 07:59"]},
    {"resource_id": "Synthetic throughput card", "record_label": "panel", "status": "", "fields": [],
     "emails": [], "phones": [], "user_ids": [], "names": [], "timestamps": []}]}


def vision_responder(extraction=SYNTH_EXTRACTION, model=VISION_MODEL):
    return lambda req: reply(json.dumps(extraction), model=model)


class FillCacheTests(TempCacheMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.png_bytes = tiny_png()
        shots = tempfile.TemporaryDirectory(prefix="ghostaudit-test-shots-")
        self.addCleanup(shots.cleanup)
        self.png = Path(shots.name) / "synthetic_shot.png"
        self.png.write_bytes(self.png_bytes)
        p = mock.patch("ghostaudit.vision.evidence_path", lambda file_path: self.png)
        p.start()
        self.addCleanup(p.stop)
        self.manifest = [entry(), entry("evidence_902", "evidence/documents/synthetic.pdf"),
                         entry("evidence_903", "evidence/images/synthetic_second.png", "2026-08-08T09:00:00Z")]
        self.activity = [link_row()]

    def fill(self, srv, *, force=False, key="default", **env):
        kw = {} if key == "default" else {"key": key}
        with openrouter_env(base_url=srv.url, **kw, **env):
            return vision.fill_cache(self.manifest, client=make_client(), force=force, activity=self.activity)

    def test_request_carries_the_image_data_url_and_vision_model(self):
        with FakeOpenRouter(responder=vision_responder()) as srv:
            self.fill(srv)
        self.assertEqual(len(srv.chat_requests), 2)  # two PNG entries, the PDF is skipped
        body = srv.chat_requests[0]["body"]
        self.assertEqual(body["model"], llm.DEFAULT_VISION_MODEL)
        self.assertEqual(body["temperature"], 0)
        system, user = body["messages"]
        self.assertEqual(system, {"role": "system", "content": vision.SYSTEM_PROMPT})
        self.assertEqual(user["content"][0], {"type": "text", "text": vision.USER_PROMPT})
        self.assertEqual(user["content"][1], {"type": "image_url", "image_url": {
            "url": "data:image/png;base64," + base64.b64encode(self.png_bytes).decode("ascii")}})

    def test_vision_model_env_override(self):
        with FakeOpenRouter(responder=vision_responder()) as srv:
            self.fill(srv, OPENROUTER_VISION_MODEL="synthetic/vision-override")
        self.assertEqual({r["body"]["model"] for r in srv.chat_requests}, {"synthetic/vision-override"})
        self.assertEqual({l["task"] for l in self.usage_lines()}, {"vision"})

    def test_writes_raw_and_mapped_files(self):
        with FakeOpenRouter(responder=vision_responder()) as srv:
            summary = self.fill(srv)
        self.assertEqual(self.cache_files(), ["usage.jsonl", "vision/evidence_901.json", "vision/evidence_901.raw.json",
                                              "vision/evidence_903.json", "vision/evidence_903.raw.json"])
        self.assertEqual((summary["called"], summary["remapped"], summary["failed"], summary["stopped"]),
                         (2, 0, {}, None))
        raw = json.loads(vision.raw_path("evidence_901").read_text(encoding="utf-8"))
        self.assertEqual((raw["file_id"], raw["captured_at"], raw["prompt_version"]),
                         ("evidence_901", CAPTURED, vision.PROMPT_VERSION))
        self.assertEqual((raw["requested_model"], raw["model"]), (llm.DEFAULT_VISION_MODEL, VISION_MODEL))
        self.assertEqual(raw["extraction"], vision.normalize_extraction(SYNTH_EXTRACTION))
        self.assertEqual(raw["dropped"], [{"resource_id": "Synthetic throughput card", "reason": "not a resource id"}])
        mapped = json.loads(images.cache_path("evidence_901").read_text(encoding="utf-8"))
        self.assertEqual([(i["resource_id"], i["state"], i["system_id"], i["observed_at"]) for i in mapped],
                         [("data_flow_901", "ACTIVE", "integration_service", CAPTURED)])
        self.assertEqual(mapped[0]["identifiers"], [SYN_EMAIL, "user_901"])
        # link-row cross-check: evidence_901 has the synthetic link row 7 min 1 s after capture
        self.assertEqual([c["status"] for c in summary["files"]["evidence_901"]["cross_check"]], ["confirmed"])
        self.assertEqual(summary["files"]["evidence_903"]["cross_check"], [{"status": "no_link_row"}])
        # and the mapped file loads through the image parser with the manifest capture time
        obs, _ = images.parse(self.manifest[0], self.png)
        self.assertEqual([(o.resource_id, o.state, o.observed_at) for o in obs], [("data_flow_901", "ACTIVE", CAPTURED)])

    def test_cached_files_are_skipped_unless_forced(self):
        with FakeOpenRouter(responder=vision_responder()) as srv:
            self.fill(srv)
            self.assertEqual(len(srv.chat_requests), 2)
            again = self.fill(srv)
            self.assertEqual(len(srv.chat_requests), 2)
            self.assertEqual((again["called"], again["remapped"]), (0, 2))
            self.assertEqual(vision.pending(self.manifest), [])
            forced = self.fill(srv, force=True)
            self.assertEqual(len(srv.chat_requests), 4)
            self.assertEqual((forced["called"], forced["remapped"]), (2, 0))

    def test_cached_files_remap_without_a_key(self):
        with FakeOpenRouter(responder=vision_responder()) as srv:
            self.fill(srv)
            n = len(srv.requests)
            summary = self.fill(srv, key=None)
        self.assertEqual(len(srv.requests), n)
        self.assertEqual(summary["remapped"], 2)

    def test_old_prompt_version_is_pending_again(self):
        with FakeOpenRouter(responder=vision_responder()) as srv:
            self.fill(srv)
        raw = json.loads(vision.raw_path("evidence_901").read_text(encoding="utf-8"))
        llm.write_json(vision.raw_path("evidence_901"), dict(raw, prompt_version="vision-v0"))
        self.assertEqual([e.file_id for e in vision.pending(self.manifest)], ["evidence_901"])

    def test_pending_without_key_raises_and_writes_nothing(self):
        with FakeOpenRouter() as srv:
            with self.assertRaises(llm.LLMError) as cm:
                self.fill(srv, key=None)
        self.assertTrue(cm.exception.fatal)
        self.assertEqual(srv.requests, [])
        self.assertEqual(self.cache_files(), [])

    def test_invalid_extraction_is_reported_not_raised(self):
        with FakeOpenRouter(responder=lambda req: reply('{"screen_title": "x"}', model=VISION_MODEL)) as srv:
            summary = self.fill(srv)
        self.assertEqual(sorted(summary["failed"]), ["evidence_901", "evidence_903"])
        self.assertIsNone(summary["stopped"])
        self.assertEqual(len(srv.chat_requests), 4)  # one retry each
        self.assertFalse((self.cache_dir / "vision").exists())

    def test_fatal_error_stops_remaining_files(self):
        with FakeOpenRouter(responder=lambda req: error_reply(401)) as srv:
            summary = self.fill(srv)
        self.assertEqual(len(srv.chat_requests), 1)
        self.assertIsNotNone(summary["stopped"])
        self.assertTrue(summary["failed"]["evidence_903"].startswith("not attempted"))
        self.assertFalse((self.cache_dir / "vision").exists())


class MessagesTests(unittest.TestCase):

    def test_messages_for_builds_system_text_and_image_parts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.png"
            path.write_bytes(tiny_png())
            msgs = vision.messages_for(path)
        self.assertEqual([m["role"] for m in msgs], ["system", "user"])
        self.assertEqual([p["type"] for p in msgs[1]["content"]], ["text", "image_url"])
        self.assertTrue(msgs[1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_prompt_version(self):
        self.assertEqual(vision.PROMPT_VERSION, "vision-v1")


class ZZRepoCacheGuardTests(RepoCacheGuard, unittest.TestCase):
    snapshot_at_import = _SNAPSHOT_AT_IMPORT


if __name__ == "__main__":
    unittest.main()
