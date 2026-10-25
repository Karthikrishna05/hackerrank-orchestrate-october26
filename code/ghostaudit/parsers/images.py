"""Screenshots: read facts from the vision cache `code/cache/vision/<file_id>.json`, if filled."""

from __future__ import annotations

import json
import warnings
from pathlib import Path

from ..models import Coverage, ManifestEntry, Observation
from ..normalize import extract_identifiers, name_token
from ..paths import repo_root
from . import ParseWarning, checked_state, make_observation


def cache_path(file_id: str) -> Path:
    return repo_root() / "code" / "cache" / "vision" / f"{file_id}.json"


def _identifiers(values) -> set[str]:
    """Strong tokens of each value; a value with none is kept as a weak name token."""
    out: set[str] = set()
    for v in values or []:
        if not isinstance(v, str) or not v.strip():
            continue
        found = extract_identifiers(v)
        out |= found or {name_token(v)}
    return out


def parse(entry: ManifestEntry, path: Path) -> tuple[list[Observation], list[Coverage]]:
    """Observations from the cached vision items; empty with one warning when no cache exists."""
    cache = cache_path(entry.file_id)
    if not cache.is_file():
        warnings.warn(f"{entry.file_id}: no vision cache at {cache.relative_to(repo_root())}; "
                      "screenshot not read", ParseWarning)
        return [], []
    items = json.loads(cache.read_text(encoding="utf-8"))
    observations = []
    for i, item in enumerate(items):
        source = f"vision:[{i}]"
        if not item.get("resource_id"):
            warnings.warn(f"{entry.file_id} {source}: item without resource_id", ParseWarning)
            continue
        observations.append(make_observation(
            entry, resource_id=item["resource_id"],
            state=checked_state(str(item.get("state", "")), f"{entry.file_id} {source}: "),
            category=item.get("category"), identifiers=_identifiers(item.get("identifiers")),
            source=source, fields=dict(item), observed_at=item.get("observed_at"),
            system=item.get("system_id")))
    return observations, []
