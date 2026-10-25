"""Unit tests for ghostaudit.parsers.images (tools_spec.md T6, screenshots row).

Screenshots are read only through the vision cache `code/cache/vision/<file_id>.json`
(a list of {system_id, resource_id, state, identifiers[], observed_at}).
"""

from __future__ import annotations

import json
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

from ghostaudit.models import ManifestEntry
from ghostaudit.parsers import images
from ghostaudit.paths import repo_root

MANIFEST_CAPTURED_AT = "2026-09-26T11:00:00Z"
SCREENSHOT = Path("evidence/images/account_admin_console.png")  # never opened by the parser


def entry_for(file_id: str) -> ManifestEntry:
    return ManifestEntry(file_id=file_id, file_path=str(SCREENSHOT), origin="account_admin_portal",
                         captured_at=MANIFEST_CAPTURED_AT)


def parse_image(entry: ManifestEntry):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        obs, cov = images.parse(entry, SCREENSHOT)
    return obs, cov, [str(w.message) for w in caught]


class CachePathTests(unittest.TestCase):

    def test_cache_lives_under_code_cache_vision(self):
        self.assertEqual(images.cache_path("evidence_04"),
                         repo_root() / "code" / "cache" / "vision" / "evidence_04.json")


class MissingCacheTests(unittest.TestCase):

    def test_missing_cache_returns_nothing_with_one_warning(self):
        file_id = "evidence_test_no_such_cache"
        self.assertFalse(images.cache_path(file_id).exists())
        obs, cov, warns = parse_image(entry_for(file_id))
        self.assertEqual((obs, cov), ([], []))
        self.assertEqual(len(warns), 1)
        self.assertIn(file_id, warns[0])

    # Regression (fixed): images.parse builds its warning with cache.relative_to(repo_root()), which raises
    # ValueError when the cache directory is outside the repository (e.g. a relocated or
    # patched cache_path), so a missing cache crashes instead of warning. Suggested fix:
    # fall back to the absolute path, e.g. use os.path.relpath or a try/except around it.
    def test_missing_cache_in_a_relocated_directory_still_only_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(images, "cache_path", lambda fid: Path(tmp) / f"{fid}.json"):
                obs, cov, warns = parse_image(entry_for("evidence_04"))
        self.assertEqual((obs, cov, len(warns)), ([], [], 1))


class VisionCacheTests(unittest.TestCase):
    """A filled cache file becomes Observations with manifest capture time."""

    ITEMS = [
        {"system_id": "account_service", "resource_id": "account_5762", "state": "FOUND",
         "identifiers": ["HARUKA.S@EXAMPLE.COM", "+81 90 7610 0007", "user_11", "Haruka  Satou"],
         "observed_at": "2026-09-26T10:55:00Z"},
        {"system_id": "integration_service", "resource_id": "data_flow_1121", "state": "active",
         "identifiers": [], "category": "DATA_FLOW"},
        {"system_id": "object_storage", "resource_id": "object_19", "state": "FOUND",
         "identifiers": ["delivery_confirmation_19.pdf"], "observed_at": "2026-09-26T10:00:00+02:00"},
        {"system_id": "marketing_platform", "resource_id": "marketing_001", "state": "OPTED_IN",
         "identifiers": ["sj.parker@example.com"]},
        {"system_id": "support_platform", "state": "FOUND", "identifiers": ["user_7"]},
    ]

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        (tmp / "evidence_04.json").write_text(json.dumps(cls.ITEMS), encoding="utf-8")
        with mock.patch.object(images, "cache_path", lambda fid: tmp / f"{fid}.json"):
            cls.obs, cls.cov, cls.warnings = parse_image(entry_for("evidence_04"))
        cls.by_id = {o.resource_id: o for o in cls.obs}

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_items_with_a_resource_id_become_observations_in_order(self):
        self.assertEqual([o.resource_id for o in self.obs],
                         ["account_5762", "data_flow_1121", "object_19", "marketing_001"])

    def test_screenshots_give_no_coverage(self):
        self.assertEqual(self.cov, [])

    def test_item_without_resource_id_is_skipped_with_a_warning(self):
        self.assertTrue(any("vision:[4]" in w and "resource_id" in w for w in self.warnings),
                        self.warnings)

    def test_unknown_state_becomes_unknown_with_a_warning(self):
        self.assertEqual(self.by_id["marketing_001"].state, "UNKNOWN")
        self.assertTrue(any("OPTED_IN" in w for w in self.warnings), self.warnings)

    def test_exactly_two_warnings(self):
        self.assertEqual(len(self.warnings), 2, self.warnings)

    def test_state_is_upper_cased(self):
        self.assertEqual(self.by_id["data_flow_1121"].state, "ACTIVE")

    def test_strong_identifiers_are_normalised_and_names_are_weak_tokens(self):
        self.assertEqual(self.by_id["account_5762"].identifiers, frozenset({
            "haruka.s@example.com", "+819076100007", "user_11", "name:haruka satou"}))

    def test_observed_at_comes_from_the_item_in_utc(self):
        self.assertEqual(self.by_id["account_5762"].observed_at, "2026-09-26T10:55:00Z")
        self.assertEqual(self.by_id["object_19"].observed_at, "2026-09-26T08:00:00Z")

    def test_item_without_observed_at_uses_the_manifest_capture_time(self):
        self.assertEqual(self.by_id["data_flow_1121"].observed_at, MANIFEST_CAPTURED_AT)

    def test_manifest_fields_are_copied(self):
        for o in self.obs:
            with self.subTest(resource_id=o.resource_id):
                self.assertEqual((o.file_id, o.captured_at), ("evidence_04", MANIFEST_CAPTURED_AT))

    def test_system_comes_from_the_resource_prefix(self):
        self.assertEqual(self.by_id["account_5762"].system_id, "account_service")

    def test_unknown_prefix_keeps_the_declared_system(self):
        self.assertEqual(self.by_id["object_19"].system_id, "object_storage")

    def test_category_is_passed_through(self):
        self.assertEqual(self.by_id["data_flow_1121"].category, "DATA_FLOW")
        self.assertIsNone(self.by_id["account_5762"].category)

    def test_source_and_fields_locate_the_cache_item(self):
        o = self.by_id["data_flow_1121"]
        self.assertEqual(o.source, "vision:[1]")
        self.assertEqual(o.fields, self.ITEMS[1])
        self.assertIsNone(o.store)


if __name__ == "__main__":
    unittest.main()
