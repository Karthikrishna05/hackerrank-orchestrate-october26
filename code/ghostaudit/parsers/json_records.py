"""JSON evidence: flat `records[]` lists, snapshot bundles, service resource exports, collection dumps."""

from __future__ import annotations

import json
import warnings
from pathlib import Path

from ..models import Coverage, ManifestEntry, Observation
from ..rules import category_for
from . import (ParseWarning, checked_state, identifiers_of, make_coverage, make_observation,
               record_state)

HISTORY_COLLECTION = "operational_record_history"  # lookup history, not a full listing
ITEM_TIME_FIELDS = ("observed_at", "recorded_at")
# service_resource_export section -> collection.
SECTION_COLLECTION = {
    "profile_lookups": "customer_profiles",
    "index_queries": "customer_search_documents",
    "profiles": "customer_analytics_profiles",
    "tickets": "support_records",
    "exports": "customer_exports",
    "connectors": "connector_subscriptions",
    "restore_entries": "backup_restore_entries",
    "audit_records": "security_audit_records",
}


def _item_time(rec: dict, default: str | None) -> str | None:
    return next((rec[f] for f in ITEM_TIME_FIELDS if rec.get(f)), default)


def _record_obs(entry, rec: dict, source: str, *, collection: str | None, system: str | None,
                default_time: str | None) -> Observation | None:
    rid = rec.get("resource_id")
    if not rid:
        warnings.warn(f"{entry.file_id} {source}: record without resource_id", ParseWarning)
        return None
    category = category_for(collection=collection, record_type=rec.get("record_type"))
    if "result" in rec:
        state = checked_state(str(rec["result"]), f"{entry.file_id} {source}: ")
    else:
        state = record_state(category, rec)
    return make_observation(entry, resource_id=rid, state=state, category=category,
                            identifiers=identifiers_of(rec), source=source, fields=dict(rec),
                            observed_at=_item_time(rec, default_time),
                            system=rec.get("system") or system)


def _service_export(entry, doc: dict) -> list[Observation]:
    out = []
    for system, sections in doc["service_exports"].items():
        for section, items in sections.items():
            collection = SECTION_COLLECTION.get(section)
            if collection is None:
                warnings.warn(f"{entry.file_id}: unknown service export section {section!r}", ParseWarning)
            for k, item in enumerate(items):
                source = f"json:service_exports.{system}.{section}[{k}]"
                obs = _record_obs(entry, item, source, collection=collection, system=system,
                                  default_time=doc.get("exported_at"))
                if obs is None:
                    continue
                if section == "profile_lookups":
                    obs.state = {404: "NOT_FOUND", 200: "FOUND"}.get(item.get("status_code"), "UNKNOWN")
                elif section == "index_queries":
                    total = (item.get("hits") or {}).get("total")
                    obs.state = "NOT_FOUND" if total == 0 else "FOUND" if total else "UNKNOWN"
                out.append(obs)
    return out


def _bundle(entry, doc: dict) -> tuple[list[Observation], list[Coverage]]:
    obs, cov = [], []
    for i, snap in enumerate(doc["snapshots"]):
        collection, system = snap.get("collection"), snap.get("data_store")
        snap_time = snap.get("captured_at") or doc.get("exported_at")
        if collection != HISTORY_COLLECTION:
            cov.append(make_coverage(entry, system=system, collection=collection, observed_at=snap_time))
        for j, rec in enumerate(snap.get("records", [])):
            o = _record_obs(entry, rec, f"json:snapshots[{i}].records[{j}]", collection=collection,
                            system=system, default_time=snap_time)
            if o is not None:
                obs.append(o)
    return obs, cov


def parse(entry: ManifestEntry, path: Path) -> tuple[list[Observation], list[Coverage]]:
    """Dispatch on the document shape; only bundle snapshots are full listings (coverage)."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    if "service_exports" in doc:
        return _service_export(entry, doc), []
    if "snapshots" in doc:
        return _bundle(entry, doc)
    if isinstance(doc.get("records"), list):
        default_time = doc.get("captured_at") or doc.get("exported_at")
        collection = doc.get("collection")
        obs = [_record_obs(entry, rec, f"json:records[{i}]", collection=collection, system=None,
                           default_time=default_time)
               for i, rec in enumerate(doc["records"])]
        return [o for o in obs if o is not None], []
    raise ValueError(f"{entry.file_id}: unrecognised JSON evidence shape (keys {sorted(doc)})")
