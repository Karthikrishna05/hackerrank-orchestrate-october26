"""T6 evidence parsers: turn every manifest file into Observation and Coverage objects.

Each format module exposes `parse(entry, path) -> (observations, coverages)` and reports
malformed input with `warnings.warn(..., ParseWarning)`; `parse_all` collects those warnings.
"""

from __future__ import annotations

import warnings
from pathlib import Path

from ..loaders import load_manifest
from ..models import Coverage, ManifestEntry, Observation, STATES
from ..normalize import extract_identifiers, name_token, natural_key, system_for
from ..paths import evidence_path
from ..rules import DATA_FLOW
from ..timeutil import to_iso


class ParseWarning(UserWarning):
    """A malformed or unrecognised piece of evidence (file_id and locator in the message)."""


NAME_FIELDS = frozenset({"full_name", "display_name", "requester_name", "name"})
NON_IDENTITY_FIELDS = frozenset({"integrity_seal"})  # zero-padded sha256 values, not identity
ANALYTICS_ID_FIELDS = ("user_id", "customer_reference", "email", "phone", "email_hash")
TOMBSTONE_ID_FIELDS = ("name", "email", "phone", "email_hash")
SUPPORT_REQUESTER_FIELDS = ("requester_name", "requester_email", "requester_phone")
SUPPORT_TEXT_FIELDS = ("message_text", "message_excerpt")


def is_null(v) -> bool:
    """None, empty, or the literal `null`/`none` used by text formats."""
    return v is None or (isinstance(v, str) and v.strip().lower() in ("", "null", "none"))


def truthy(v) -> bool | None:
    """Boolean from bool/int/str forms (`true`, `1`, ...); None when undecidable."""
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v != 0
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "yes"):
            return True
        if s in ("false", "0", "no"):
            return False
    return None


def identifiers_of(value) -> frozenset[str]:
    """Strong tokens in every string value (recursively) plus name tokens of name fields."""
    found: set[str] = set()

    def walk(v, key=None):
        if key in NON_IDENTITY_FIELDS:
            return
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, k)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x, key)
        elif isinstance(v, str) and not is_null(v):
            found.update(extract_identifiers(v))
            if key in NAME_FIELDS:
                found.add(name_token(v))

    walk(value)
    return frozenset(found)


def support_exposed(rec: dict) -> bool:
    """Requester fields set, or a strong identifier left in the message text."""
    if any(not is_null(rec.get(f)) for f in SUPPORT_REQUESTER_FIELDS):
        return True
    return any(isinstance(rec.get(f), str) and extract_identifiers(rec[f]) for f in SUPPORT_TEXT_FIELDS)


def record_state(category: str | None, rec: dict) -> str:
    """State of a listed record: explicit `result` wins, otherwise derived from its fields."""
    result = rec.get("result")
    if isinstance(result, str) and result.strip():
        return checked_state(result)
    if category == "ANALYTICS_PROFILE":
        return "IDENTIFIABLE" if any(not is_null(rec.get(f)) for f in ANALYTICS_ID_FIELDS) else "ANONYMIZED"
    if category == "ACCOUNT_TOMBSTONE":
        return "IDENTIFIABLE" if any(not is_null(rec.get(f)) for f in TOMBSTONE_ID_FIELDS) else "REDACTED"
    if category == "SUPPORT_MESSAGE":
        return "IDENTIFIABLE" if support_exposed(rec) else "REDACTED"
    if category == DATA_FLOW:
        return {True: "ACTIVE", False: "DISABLED"}.get(truthy(rec.get("enabled")), "UNKNOWN")
    if category == "BACKUP_ARCHIVE":
        return {True: "ACTIVE", False: "BLOCKED"}.get(truthy(rec.get("restore_allowed")), "UNKNOWN")
    return "FOUND"


def checked_state(state: str, where: str = "") -> str:
    """Upper-cased state if it is one of STATES, else UNKNOWN with a warning."""
    s = state.strip().upper()
    if s in STATES:
        return s
    warnings.warn(f"{where}unknown state {state!r}", ParseWarning, stacklevel=2)
    return "UNKNOWN"


def make_observation(entry: ManifestEntry, *, resource_id: str, state: str, category: str | None,
                     identifiers, source: str, fields: dict, observed_at: str | None = None,
                     system: str | None = None, store: str | None = None) -> Observation:
    """Observation with manifest times; system from the resource prefix, else the declared one."""
    return Observation(
        file_id=entry.file_id,
        captured_at=to_iso(entry.captured_at),
        observed_at=to_iso(observed_at or entry.captured_at),
        system_id=system_for(resource_id) or system or "",
        resource_id=resource_id,
        state=state,
        category=category,
        identifiers=frozenset(identifiers),
        store=store,
        source=source,
        fields=fields,
    )


def make_coverage(entry: ManifestEntry, *, system: str, collection: str, observed_at: str | None,
                  store: str | None = None) -> Coverage:
    return Coverage(file_id=entry.file_id, captured_at=to_iso(entry.captured_at),
                    observed_at=to_iso(observed_at or entry.captured_at),
                    system_id=system, store=store, collection=collection)


def _module_for(entry: ManifestEntry):
    from . import images, json_records, logs, pdf_text, sql_snapshot, text_records

    ext = Path(entry.file_path).suffix.lower()
    modules = {".sql": sql_snapshot, ".json": json_records, ".log": logs, ".txt": text_records,
               ".pdf": pdf_text, ".png": images, ".jpg": images, ".jpeg": images}
    if ext not in modules:
        raise ValueError(f"{entry.file_id}: unsupported evidence format {ext!r}")
    return modules[ext]


def parse_file(entry: ManifestEntry) -> tuple[list[Observation], list[Coverage]]:
    """Parse one manifest entry with the parser for its extension (modules check content shape)."""
    path = evidence_path(entry.file_path)
    if not path.is_file():
        raise FileNotFoundError(f"{entry.file_id}: missing file {entry.file_path}")
    return _module_for(entry).parse(entry, path)


def obs_sort_key(o: Observation) -> tuple:
    return (natural_key(o.file_id), natural_key(o.source), natural_key(o.resource_id), o.observed_at, o.state)


def cov_sort_key(c: Coverage) -> tuple:
    return (natural_key(c.file_id), c.system_id, c.store or "", c.collection, c.observed_at)


def parse_all(manifest: list[ManifestEntry] | None = None
              ) -> tuple[list[Observation], list[Coverage], list[str]]:
    """Parse every manifest entry; one bad file becomes a warning instead of a crash."""
    entries = load_manifest() if manifest is None else manifest
    observations: list[Observation] = []
    coverages: list[Coverage] = []
    messages: list[str] = []
    for entry in entries:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                obs, cov = parse_file(entry)
                observations.extend(obs)
                coverages.extend(cov)
            except Exception as exc:  # reported as a message, never silent
                messages.append(f"{entry.file_id}: {type(exc).__name__}: {exc}")
        for w in caught:
            msg = str(w.message)
            messages.append(msg if msg.startswith(entry.file_id) else f"{entry.file_id}: {msg}")
    observations.sort(key=obs_sort_key)
    coverages.sort(key=cov_sort_key)
    return observations, coverages, messages


__all__ = ["ParseWarning", "identifiers_of", "parse_all", "parse_file", "record_state"]
