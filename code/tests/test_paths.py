"""Tests for ghostaudit.paths (tools_spec T1: repo paths resolved from the package file)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from ghostaudit import paths

# Independent oracle: this test file lives at <repo>/code/tests/test_paths.py.
EXPECTED_ROOT = Path(__file__).resolve().parents[2]


class TestRepoRoot(unittest.TestCase):
    def test_repo_root_is_an_absolute_existing_directory(self):
        root = paths.repo_root()
        self.assertIsInstance(root, Path)
        self.assertTrue(root.is_absolute())
        self.assertTrue(root.is_dir())

    def test_repo_root_is_the_directory_holding_agents_md_and_the_package(self):
        root = paths.repo_root()
        self.assertEqual(root, EXPECTED_ROOT)
        self.assertTrue((root / "AGENTS.md").is_file())
        self.assertTrue((root / "code" / "ghostaudit" / "paths.py").is_file())

    def test_repo_root_does_not_depend_on_the_working_directory(self):
        before = paths.repo_root()
        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                during = paths.repo_root()
            finally:
                os.chdir(old_cwd)
        self.assertEqual(before, during)


class TestDatasetDir(unittest.TestCase):
    def test_dataset_dir_is_dataset_under_repo_root(self):
        self.assertEqual(paths.dataset_dir(), paths.repo_root() / "dataset")

    def test_dataset_dir_is_absolute(self):
        self.assertTrue(paths.dataset_dir().is_absolute())


class TestEvidencePath(unittest.TestCase):
    def test_evidence_path_joins_manifest_path_relative_to_dataset(self):
        got = paths.evidence_path("evidence/logs/service_events.log")
        self.assertEqual(got, paths.dataset_dir() / "evidence" / "logs" / "service_events.log")

    def test_evidence_path_does_not_require_the_file_to_exist(self):
        got = paths.evidence_path("evidence/records/does_not_exist_123.json")
        self.assertFalse(got.exists())
        self.assertTrue(got.is_absolute())


if __name__ == "__main__":
    unittest.main()
