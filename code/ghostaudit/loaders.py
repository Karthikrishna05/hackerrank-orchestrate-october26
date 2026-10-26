"""T1: load the participant-facing CSV files into the shared dataclasses."""

from __future__ import annotations

import csv
from dataclasses import fields
from pathlib import Path
from typing import TypeVar

from . import paths
from .models import Activity, AuditChange, ManifestEntry, Request, Rule, UserIdentity
from .timeutil import parse_ts

T = TypeVar("T")


def _resolve(name: str, path: str | Path | None, dataset_dir: str | Path | None) -> Path:
    if path is not None:
        return Path(path)
    return Path(dataset_dir) / name if dataset_dir is not None else paths.dataset_dir() / name


def _read(cls: type[T], path: Path) -> list[T]:
    """Read a CSV whose header holds every field of `cls`; values are stripped strings."""
    names = [f.name for f in fields(cls)]
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = [n for n in names if n not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path.name}: missing columns {missing}")
        rows = []
        for row in reader:
            if None in row or any(row[n] is None for n in names):
                raise ValueError(f"{path.name} line {reader.line_num}: wrong number of fields")
            rows.append(cls(**{n: row[n].strip() for n in names}))
    return rows


def load_requests(name: str = "deletion_requests.csv", path: str | Path | None = None,
                  dataset_dir: str | Path | None = None) -> list[Request]:
    """Deletion requests in file order."""
    return _read(Request, _resolve(name, path, dataset_dir))


def load_identities(path: str | Path | None = None,
                    dataset_dir: str | Path | None = None) -> list[UserIdentity]:
    """Current identity snapshot."""
    return _read(UserIdentity, _resolve("user_identities.csv", path, dataset_dir))


def load_audit_log(path: str | Path | None = None,
                   dataset_dir: str | Path | None = None) -> list[AuditChange]:
    """Identity field changes in file order."""
    return _read(AuditChange, _resolve("user_audit_log.csv", path, dataset_dir))


def load_activity(path: str | Path | None = None,
                  dataset_dir: str | Path | None = None) -> list[Activity]:
    """Global activity stream sorted by (event_time, numeric activity_id suffix)."""
    rows = _read(Activity, _resolve("system_activity.csv", path, dataset_dir))
    return sorted(rows, key=lambda a: (parse_ts(a.event_time), int(a.activity_id.rsplit("_", 1)[-1])))


def load_rules(path: str | Path | None = None,
               dataset_dir: str | Path | None = None) -> list[Rule]:
    """Deletion policy rules."""
    return _read(Rule, _resolve("deletion_rules.csv", path, dataset_dir))


def load_manifest(path: str | Path | None = None,
                  dataset_dir: str | Path | None = None) -> list[ManifestEntry]:
    """Evidence manifest in file order; `file_path` is relative to `dataset/`."""
    return _read(ManifestEntry, _resolve("evidence_files.csv", path, dataset_dir))
