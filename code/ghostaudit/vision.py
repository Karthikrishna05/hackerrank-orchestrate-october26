"""A2: read screenshots with a free OpenRouter vision model, then map the raw facts to observation states.

The model only transcribes (title, records, labels, fields, identifiers, timestamps) into
`<file_id>.raw.json`. Deterministic tables below turn labels into states and write
`<file_id>.json` in the schema `parsers/images.py` reads. Each screenshot is cross-checked
against its data-catalog link row; conflicts are flagged, never resolved here.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path

from . import llm
from .loaders import load_activity, load_manifest
from .models import Activity, ManifestEntry
from .normalize import extract_identifiers, natural_key, norm_name, system_for
from .parsers import images
from .paths import evidence_path
from .timeutil import parse_ts, to_iso

PROMPT_VERSION = "vision-v1"
IMAGE_EXTS = frozenset(llm.IMAGE_MIME)
RESOURCE_ID_RE = re.compile(r"^[a-z_]+_\d+$")
_EMBEDDED_ID_RE = re.compile(r"(?<![\w-])[a-z_]+_\d+(?![\w-])")
LINK_EVENT = "LINK_RESOURCE_IDENTITY"
LINK_WINDOW = timedelta(minutes=10)  # catalog link rows observed 7 min 1 s after each capture
LIST_KEYS = ("emails", "phones", "user_ids", "names", "timestamps")

# Resource prefix -> data category (the categories of deletion_rules.csv, plus DATA_FLOW).
CATEGORY_BY_PREFIX = {
    "account": "USER_PROFILE", "search": "SEARCH_INDEX_ENTRY", "analytics": "ANALYTICS_PROFILE",
    "support": "SUPPORT_MESSAGE", "export": "USER_EXPORT", "data_flow": "DATA_FLOW",
    "backup": "BACKUP_ARCHIVE", "audit_record": "SECURITY_AUDIT_LOG", "tombstone": "ACCOUNT_TOMBSTONE",
    "legal_hold": "LEGAL_HOLD_RECORD", "cache": "CACHE_ENTRY", "email_delivery": "EMAIL_DELIVERY_LOG",
    "marketing": "MARKETING_PROFILE",
}
# Status labels -> state, in precedence order (first state with a matching label wins).
LABEL_STATES = {
    "DATA_FLOW": (("DISABLED", {"disabled", "paused", "stopped"}),
                  ("ACTIVE", {"healthy", "continuous", "enabled"})),
    "USER_EXPORT": (("NOT_FOUND", {"expired", "deleted"}),
                    ("FOUND", {"ready", "collecting", "packaging", "processing"})),
}
LABEL_FIELD_WORDS = ("status", "mode", "state", "sync", "health")  # fields whose values act as labels
RESTORE_BLOCK_WORDS = {"blocked", "disabled", "denied", "locked", "prohibited"}
RESTORE_ALLOW_WORDS = {"allowed", "enabled", "eligible", "available", "restorable"}
YES, NO = {"yes", "true", "on"}, {"no", "false", "off"}
NEGATIONS = {"not", "no", "non"}
# Categories whose policy is REDACT/ANONYMIZE: visible strong identifiers mean IDENTIFIABLE.
EXPOSURE_CATEGORIES = frozenset({"ANALYTICS_PROFILE", "PRODUCT_TELEMETRY", "SUPPORT_MESSAGE",
                                 "EMAIL_DELIVERY_LOG", "ACCOUNT_TOMBSTONE"})

SYSTEM_PROMPT = ("You transcribe screenshots of internal admin tools into JSON. Copy text exactly as "
                 "shown. Do not guess, infer or judge anything, and never add records that are not visible.")
USER_PROMPT = """\
Read this screenshot and return one JSON object:
{"screen_title": "<page heading as shown>",
 "records": [{
  "resource_id": "<the record's own ID exactly as shown, such as widget_42; if it shows no such ID, its name or reference exactly as shown>",
  "record_label": "<what the screen calls the record: connector, export job, archive, profile, search hit, ticket, event, reference, file, ...>",
  "status": "<the status, badge or mode label shown for this record, verbatim, or empty string>",
  "fields": [{"name": "<column or field label>", "value": "<value exactly as shown>"}],
  "emails": [], "phones": [], "user_ids": ["<IDs like user_5>"], "names": ["<person names>"],
  "timestamps": ["<dates or times shown for this record, verbatim>"]}]}
Rules:
- One entry per record: every table row, plus any record shown in a page header or detail panel. \
If a header or detail panel shows the same ID as a table row, merge it into that row's entry.
- Include every row, including records about other people, staff and non-personal items.
- List a value only for the record it is shown with; use empty lists when nothing is shown.
- Return JSON only, no markdown."""


def raw_path(file_id: str) -> Path:
    return images.cache_path(file_id).with_name(f"{file_id}.raw.json")


def screenshots(manifest: list[ManifestEntry] | None = None) -> list[ManifestEntry]:
    """Image entries of the manifest, in natural file_id order."""
    entries = load_manifest() if manifest is None else manifest
    return sorted((e for e in entries if Path(e.file_path).suffix.lower() in IMAGE_EXTS),
                  key=lambda e: natural_key(e.file_id))


def messages_for(path: Path) -> list[dict]:
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [{"type": "text", "text": USER_PROMPT}, llm.image_part(path)]}]


def check_extraction(data: dict) -> None:
    """schema_check for the model reply: a records list whose entries each carry a resource_id."""
    records = data.get("records")
    if not isinstance(records, list):
        raise ValueError('missing "records" list')
    for i, r in enumerate(records):
        if not isinstance(r, dict) or not isinstance(r.get("resource_id"), str) or not r["resource_id"].strip():
            raise ValueError(f"records[{i}] needs a non-empty string resource_id")


def _strings(v) -> list[str]:
    items = v if isinstance(v, list) else [v]
    return [str(x).strip() for x in items if isinstance(x, (str, int, float)) and str(x).strip()]


def normalize_extraction(data: dict) -> dict:
    """Coerce the model reply to the raw schema (strings and string lists only)."""
    records = []
    for r in data.get("records", []):
        fields = [{"name": str(f.get("name", "")).strip(), "value": str(f.get("value", "")).strip()}
                  for f in r.get("fields") or [] if isinstance(f, dict)]
        rec = {"resource_id": r["resource_id"].strip().strip("`'\""), "fields": fields,
               "record_label": " ".join(_strings(r.get("record_label"))), "status": " ".join(_strings(r.get("status")))}
        rec.update({k: _strings(r.get(k) or []) for k in LIST_KEYS})
        records.append(rec)
    return {"screen_title": " ".join(_strings(data.get("screen_title"))), "records": records}


def links_for(entry: ManifestEntry, activity: list[Activity]) -> list[dict]:
    """Data-catalog link rows timestamped shortly after this capture."""
    captured = parse_ts(entry.captured_at)
    out = []
    for a in activity:
        if a.event_type != LINK_EVENT or not timedelta(0) < parse_ts(a.event_time) - captured <= LINK_WINDOW:
            continue
        alias = re.search(re.escape(a.resource_id) + r" \(([^)]*)\)", a.details)
        out.append({"activity_id": a.activity_id, "resource_id": a.resource_id, "target_system": a.target_system,
                    "alias": alias.group(1).strip() if alias else "",
                    "identifiers": sorted(extract_identifiers(a.details))})
    return out


def _words(s: str) -> set[str]:
    return set(re.sub(r"[^\w]+", " ", s.casefold()).split())


def _restore_state(name: str, value: str) -> str | None:
    """BLOCKED / ACTIVE from one restore field or label; None when it states neither."""
    n, v = _words(name), _words(value)
    rules = ((RESTORE_BLOCK_WORDS, "BLOCKED", "ACTIVE"), (RESTORE_ALLOW_WORDS, "ACTIVE", "BLOCKED"))
    for words, hit, miss in rules:
        if n & words:  # the field names the condition: "Restore blocked: Yes"
            return hit if v & YES else miss if v & NO else None
    for words, hit, miss in rules:
        if v & words:  # the value states it: "Blocked", "Not allowed"
            return miss if v & NEGATIONS else hit
    return None


def map_state(category: str | None, rec: dict, strong: set[str]) -> tuple[str, str]:
    """(state, rule applied) for one merged raw record, from the deterministic tables."""
    labels = [s for s in rec["statuses"] if s]
    labels += [f["value"] for f in rec["fields"] if any(w in f["name"].casefold() for w in LABEL_FIELD_WORDS)]
    if category in LABEL_STATES:
        for state, words in LABEL_STATES[category]:
            for label in labels:
                w = _words(label)
                if w & words and not w & NEGATIONS:
                    return state, f"label {label!r} -> {state}"
        return "UNKNOWN", f"no rule for labels {labels}"
    if category == "BACKUP_ARCHIVE":
        # A restore label that blocks or disallows restore -> BLOCKED; one that allows it -> ACTIVE.
        # Owner decision 7: an archive listed with no restore label is a restore risk -> ACTIVE.
        restore = [(f["name"], f["value"]) for f in rec["fields"] if "restor" in f["name"].casefold()]
        restore += [("", s) for s in rec["statuses"] if "restor" in s.casefold()]
        found = {s for s in (_restore_state(n, v) for n, v in restore) if s}
        for state in ("BLOCKED", "ACTIVE"):
            if state in found:
                return state, f"restore label -> {state}"
        return "ACTIVE", "archive listed, no restore label -> ACTIVE"
    if category in EXPOSURE_CATEGORIES:
        exposed = {t for t in strong if not (category == "ACCOUNT_TOMBSTONE" and t.startswith("user_"))}
        if exposed or (category == "ACCOUNT_TOMBSTONE" and rec["names"]):
            return "IDENTIFIABLE", "direct identifiers shown -> IDENTIFIABLE"
    return "FOUND", ("record shown -> FOUND" if category else "record shown (unknown category) -> FOUND")


def map_extraction(entry: ManifestEntry, extraction: dict, links: list[dict]) -> tuple[list[dict], dict]:
    """Observation items for parsers/images.py, plus a report (aliases, dropped records, cross-check)."""
    aliases, dropped, merged = [], [], {}
    by_alias = {l["alias"].casefold(): l for l in links if l["alias"]}
    for r in extraction["records"]:
        ref = rid = r["resource_id"]
        link = by_alias.get(ref.casefold())
        embedded = {t for t in _EMBEDDED_ID_RE.findall(ref) if system_for(t)}
        if system_for(ref) is None and link and link["resource_id"] != ref:
            rid = link["resource_id"]  # the catalog row names this reference as the resource's alias
            aliases.append({"from": ref, "to": rid, "activity_id": link["activity_id"]})
        elif not RESOURCE_ID_RE.fullmatch(ref) and len(embedded) == 1:
            rid = embedded.pop()  # e.g. "TCK-12 / support_77"
            aliases.append({"from": ref, "to": rid, "activity_id": ""})
        if not RESOURCE_ID_RE.fullmatch(rid):
            dropped.append({"resource_id": ref, "reason": "not a resource id"})
            continue
        if rid.startswith("user_"):
            dropped.append({"resource_id": ref, "reason": "a user id, not a resource"})
            continue
        m = merged.setdefault(rid, {"refs": [], "statuses": [], "fields": [], **{k: [] for k in LIST_KEYS}})
        m["refs"].append(ref)
        m["statuses"].append(r["status"])
        m["fields"].extend(r["fields"])
        for k in LIST_KEYS:
            m[k].extend(r[k])
    items = []
    link_system = {l["resource_id"]: l["target_system"] for l in links}
    for rid in sorted(merged, key=natural_key):
        m = merged[rid]
        values = m["refs"] + m["emails"] + m["phones"] + m["user_ids"] + [f["value"] for f in m["fields"]]
        strong = set().union(*(extract_identifiers(v) for v in values))
        names = set()
        for n in m["names"]:
            found = extract_identifiers(n)
            strong |= found
            if not found:
                names.add(norm_name(n))
        category = CATEGORY_BY_PREFIX.get(rid.rsplit("_", 1)[0])
        state, rule = map_state(category, m, strong)
        items.append({"system_id": system_for(rid) or link_system.get(rid, ""), "resource_id": rid,
                      "state": state, "category": category, "identifiers": sorted(strong) + sorted(names),
                      "observed_at": to_iso(entry.captured_at), "labels": [s for s in m["statuses"] if s],
                      "rule": rule})
    return items, {"aliases": aliases, "dropped": dropped, "cross_check": cross_check(items, links)}


def cross_check(items: list[dict], links: list[dict]) -> list[dict]:
    """Per link row: is its resource extracted, and do the identifiers shown agree with the link?"""
    if not links:
        return [{"status": "no_link_row"}]
    by_rid = {i["resource_id"]: i for i in items}
    out = []
    for l in links:
        item = by_rid.get(l["resource_id"])
        shown = sorted(t for t in (item["identifiers"] if item else []) if extract_identifiers(t))
        if item is None:
            status = "resource_missing"
        elif set(shown) & set(l["identifiers"]):
            status = "confirmed"
        elif shown:
            status = "conflict"  # the screen shows other strong identifiers than the catalog link
        else:
            status = "no_identifiers"
        out.append({"activity_id": l["activity_id"], "resource_id": l["resource_id"], "status": status,
                    "link_identifiers": l["identifiers"], "shown_identifiers": shown})
    return out


def _write(entry: ManifestEntry, raw: dict, links: list[dict]) -> dict:
    items, report = map_extraction(entry, raw["extraction"], links)
    llm.write_json(raw_path(entry.file_id), {**raw, **report})
    llm.write_json(images.cache_path(entry.file_id), items)
    return {"records": len(items), **report}


def _cached_raw(file_id: str) -> dict | None:
    path = raw_path(file_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, ValueError):
        return None
    return raw if raw and raw.get("prompt_version") == PROMPT_VERSION and "extraction" in raw else None


def pending(manifest: list[ManifestEntry] | None = None, force: bool = False) -> list[ManifestEntry]:
    """Screenshots that need a model call (no current raw extraction cached)."""
    return [e for e in screenshots(manifest) if force or _cached_raw(e.file_id) is None]


def fill_cache(manifest: list[ManifestEntry] | None = None, *, client: llm.OpenRouterClient | None = None,
               force: bool = False, activity: list[Activity] | None = None) -> dict:
    """One call per screenshot without a cached raw extraction (all of them with `force`); cached raw
    extractions are re-mapped without a call. Returns a per-file summary; failures are reported, not raised,
    unless the error is fatal or a spent rate-limit budget (then the remaining files are left for the next run)."""
    entries = screenshots(manifest)
    activity = load_activity() if activity is None else activity
    summary = {"called": 0, "remapped": 0, "failed": {}, "files": {}, "stopped": None}
    todo = {e.file_id for e in pending(entries, force)}
    if todo:
        client = client or llm.OpenRouterClient()
        if not client.has_key():
            raise llm.LLMError("OPENROUTER_API_KEY is not set", fatal=True)
    for entry in entries:
        links = links_for(entry, activity)
        if entry.file_id not in todo:
            summary["files"][entry.file_id] = _write(entry, _cached_raw(entry.file_id), links)
            summary["remapped"] += 1
            continue
        if summary["stopped"]:
            summary["failed"][entry.file_id] = "not attempted: " + summary["stopped"]
            continue
        try:
            data, usage = client.chat_json(messages_for(evidence_path(entry.file_path)), model=client.vision_model,
                                           task="vision", schema_check=check_extraction)
        except llm.LLMError as exc:
            summary["failed"][entry.file_id] = str(exc)
            if exc.fatal or exc.rate_limited:
                summary["stopped"] = str(exc)
            continue
        raw = {"file_id": entry.file_id, "captured_at": to_iso(entry.captured_at), "prompt_version": PROMPT_VERSION,
               "requested_model": usage.requested_model, "model": usage.model, "extraction": normalize_extraction(data)}
        summary["files"][entry.file_id] = _write(entry, raw, links)
        summary["called"] += 1
    return summary
