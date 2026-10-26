"""Repository paths, resolved from this file so any checkout location works."""

from __future__ import annotations

from pathlib import Path


def repo_root() -> Path:
    """The repository root (two levels above the package directory)."""
    return Path(__file__).resolve().parents[2]


def dataset_dir() -> Path:
    """The participant-facing `dataset/` directory."""
    return repo_root() / "dataset"


def evidence_path(file_path: str) -> Path:
    """Absolute path of a manifest `file_path` (relative to `dataset/`)."""
    return dataset_dir() / file_path
