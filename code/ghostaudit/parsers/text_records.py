"""Plain-text handover records: `- ts | system | resource | record_type | result | subject`."""

from __future__ import annotations

import re
import warnings
from pathlib import Path

from ..models import Coverage, ManifestEntry, Observation
from ..normalize import extract_identifiers
from ..rules import category_for
from . import ParseWarning, checked_state, make_observation

_SNAPSHOT = re.compile(r"^\s*Snapshot:\s*(\S+)", re.M)
FIELD_NAMES = ("recorded_at", "system", "resource_id", "record_type", "result", "subject_reference")
_ROW = re.compile(r"^\s*-\s+" + r"\s*\|\s*".join([r"([^|\s][^|]*?)"] * len(FIELD_NAMES)) + r"\s*$")


def parse(entry: ManifestEntry, path: Path) -> tuple[list[Observation], list[Coverage]]:
    """One observation per record line; the subject is matched as normalised identifiers."""
    text = path.read_text(encoding="utf-8")
    snap = _SNAPSHOT.search(text)
    observations = []
    for n, line in enumerate(text.splitlines(), 1):
        m = _ROW.match(line)
        if not m:
            if line.lstrip().startswith("- ") or line.count("|") >= 3:
                warnings.warn(f"{entry.file_id} line {n}: malformed record line", ParseWarning)
            continue
        fields = dict(zip(FIELD_NAMES, m.groups()))
        source = f"txt:L{n}"
        observations.append(make_observation(
            entry, resource_id=fields["resource_id"],
            state=checked_state(fields["result"], f"{entry.file_id} {source}: "),
            category=category_for(record_type=fields["record_type"]),
            identifiers=extract_identifiers(fields["subject_reference"]), source=source, fields=fields,
            observed_at=fields["recorded_at"] or (snap.group(1) if snap else None),
            system=fields["system"]))
    if not observations:
        raise ValueError(f"{entry.file_id}: no handover record lines found")
    return observations, []
