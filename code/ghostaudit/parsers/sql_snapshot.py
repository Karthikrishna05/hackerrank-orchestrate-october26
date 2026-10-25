"""SQLite snapshot exports: generic `customer_data_<store>`, typed `<collection>_<store>`, inventory."""

from __future__ import annotations

import json
import re
import sqlite3
import warnings
from pathlib import Path

from ..models import Coverage, ManifestEntry, Observation
from ..normalize import system_for
from ..rules import category_for
from . import ParseWarning, identifiers_of, make_coverage, make_observation, record_state

_CAPTURED = re.compile(r"^--\s*Captured at:\s*(\S+)", re.M)
_COMPLETED = re.compile(r"^--\s*snapshot_completed_at=(\S+)", re.M)
_STORE = re.compile(r"_(primary|read_replica|reporting_replica|failover_replica|replica_[a-z]+_[a-z]+)$")

GENERIC_BASE = "customer_data"
# Typed table base -> collection (the store is the table suffix).
TYPED_BASES = {"customer_profiles": "customer_profiles", "search_documents": "customer_search_documents"}
# Collection -> owning system, for typed and inventory tables.
COLLECTION_SYSTEM = {
    "customer_profiles": "account_service",
    "customer_search_documents": "search_service",
    "customer_analytics_profiles": "analytics_platform",
    "support_records": "support_platform",
    "customer_exports": "export_service",
    "connector_subscriptions": "integration_service",
    "backup_restore_entries": "backup_service",
    "security_audit_records": "security_service",
}
SYSTEM_COLLECTION = {s: c for c, s in COLLECTION_SYSTEM.items()}
SCOPE_TABLE = "snapshot_scope"
ROW_TIME_FIELDS = ("updated_at", "indexed_at")


def _rows(con: sqlite3.Connection, table: str) -> list[dict]:
    cur = con.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _row_time(row: dict) -> str | None:
    return next((row[f] for f in ROW_TIME_FIELDS if row.get(f)), None)


def parse(entry: ManifestEntry, path: Path) -> tuple[list[Observation], list[Coverage]]:
    """One observation per row, one coverage per table (per scope system for inventories)."""
    text = path.read_text(encoding="utf-8")
    header = _CAPTURED.search(text) or _COMPLETED.search(text)
    header_ts = header.group(1) if header else None
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(text)
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        data = {t: _rows(con, t) for t in tables}
    finally:
        con.close()

    observations: list[Observation] = []
    coverages: list[Coverage] = []
    inventory = SCOPE_TABLE in data
    generic_pairs: set[tuple[str, str]] = set()
    generic_tables: list[tuple[str, str]] = []

    for table in tables:
        if table == SCOPE_TABLE:
            continue
        m = _STORE.search(table)
        base, store = (table[:m.start()], m.group(1)) if m else (table, None)
        if inventory and table in COLLECTION_SYSTEM:
            store, collection = "inventory", table
        elif base == GENERIC_BASE and store:
            generic_tables.append((table, store))
            collection = None
        elif base in TYPED_BASES and store:
            collection = TYPED_BASES[base]
            coverages.append(make_coverage(entry, system=COLLECTION_SYSTEM[collection],
                                           collection=collection, observed_at=header_ts, store=store))
        else:
            warnings.warn(f"{entry.file_id}: unrecognised table {table!r}", ParseWarning)
            continue
        for row in data[table]:
            rid = str(row.get("resource_id") or "")
            if not rid:
                warnings.warn(f"{entry.file_id} sql:{table}: row without resource_id", ParseWarning)
                continue
            if collection is None:
                rec = json.loads(row["record_json"])
                coll = row.get("collection_name")
                system = row.get("origin_system")
                generic_pairs.add((system_for(rid) or system, coll))
                fields = {k: v for k, v in row.items() if k != "record_json"} | rec
            else:
                rec, coll, system, fields = row, collection, COLLECTION_SYSTEM[collection], dict(row)
            category = category_for(collection=coll, record_type=rec.get("record_type"))
            observations.append(make_observation(
                entry, resource_id=rid, state=record_state(category, rec), category=category,
                identifiers=identifiers_of(rec), source=f"sql:{table}", fields=fields,
                observed_at=_row_time(row) or header_ts, system=system, store=store))

    # A generic table holds the whole customer_data store, so it covers every
    # (system, collection) the snapshot shows in any of its replicas.
    for table, store in generic_tables:
        for system, coll in sorted(generic_pairs):
            coverages.append(make_coverage(entry, system=system, collection=coll,
                                           observed_at=header_ts, store=store))
    for row in data.get(SCOPE_TABLE, []):
        system = row["origin_system"]
        coverages.append(make_coverage(entry, system=system,
                                       collection=SYSTEM_COLLECTION.get(system, system),
                                       observed_at=row.get("captured_at") or header_ts,
                                       store="inventory"))
    return observations, coverages
