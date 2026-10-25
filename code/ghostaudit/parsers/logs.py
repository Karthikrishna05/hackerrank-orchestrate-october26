"""Service logs in every line style of the dataset, mapped to resource states with EVENT_STATE_RULES."""

from __future__ import annotations

import json
import re
import warnings
from pathlib import Path

from ..models import STATES, Coverage, ManifestEntry, Observation
from ..rules import category_for
from ..timeutil import to_iso
from . import ParseWarning, identifiers_of, is_null, make_observation, support_exposed, truthy

_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))\s+(.*)$")
_KV = re.compile(r'([A-Za-z_][\w.]*)=("(?:[^"\\]|\\.)*"|[^\s"]*)')
_SYSLOG = re.compile(r"^<\d+>\d+\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*(.*)$")
_BRACKET = re.compile(r"^\[(\w+)\]\s+\[([^\]]+)\]\s+\[([^\]]+)\]\s+(\w+):\s*(.*)$")
_LEVEL_FIRST = re.compile(r"^(DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|CRITICAL)\s+(.*)$")

# Operational lines that say nothing about customer records.
NOISE_EVENTS = frozenset({
    "export_started", "export_completed", "batch_started", "checkpoint_saved", "cache_lookup",
    "cache_refresh", "connection_acquired", "connection_pool_pressure", "dependency_retry",
    "dependency_recovered", "delivery_abandoned", "lease_extended", "lease_renewed",
    "flag_snapshot_loaded", "properties_reloaded", "pool_sample", "slow_call_rate",
    "telemetry_flushed", "certificate_checked", "disk_latency", "timer_tick", "consumer_lag",
    "thumbnail_failed",
})


def _unquote(v: str) -> str:
    if len(v) >= 2 and v[0] == v[-1] == '"':
        return re.sub(r"\\(.)", r"\1", v[1:-1])
    return v


def _kv(text: str) -> dict | None:
    """logfmt key=value pairs (quoted values may hold escapes); None if anything else remains."""
    if _KV.sub("", text).strip():
        return None
    return {k: _unquote(v) for k, v in _KV.findall(text)}


def _style_of_logfmt(kv: dict) -> str:
    trace, service = kv.get("trace_id") or "", kv.get("service") or ""
    if trace.startswith("rb-") or service == "readback_collector":
        return "readback"
    if trace.startswith("tr-rp-") or service == "support_replica":
        return "support_replica"
    return "logfmt"


def parse_log_line(line: str) -> dict | None:
    """Split a line into ts, level, service, trace_id, event, kv and style; None if unparseable."""
    m = _TS.match(line.strip())
    if not m:
        return None
    ts, rest = m.groups()
    level = service = trace = None
    if rest.startswith("{"):
        try:
            kv = json.loads(rest)
        except ValueError:
            return None
        if not isinstance(kv, dict):
            return None
        style, level, trace = "json", kv.get("severity"), kv.get("trace_id")
        service = kv.get("service") or kv.get("logger")
    elif m := _SYSLOG.match(rest):
        kv = _kv(m.group(6))
        style, service, trace = "syslog", m.group(2), m.group(3) if m.group(3) != "-" else None
    elif m := _BRACKET.match(rest):
        kv = _kv(m.group(5))
        if kv is not None:
            kv.setdefault("event", m.group(4))
        style, level, service, trace = "bracket", m.group(1), m.group(2), m.group(3)
    elif rest.startswith("| ") and len(parts := rest[2:].split(" | ", 4)) == 5:
        level, service, trace, event, tail = (p.strip() for p in parts)
        kv = _kv(tail)
        if kv is not None:
            kv.setdefault("event", event)
        style = "pipe"
    elif rest.startswith("requestId="):
        outer = _kv(rest)
        inner = _kv(outer.pop("message", "")) if outer is not None else None
        kv = None if inner is None else {**outer, **inner}
        style = "cloud"
        if kv is not None:
            level, service, trace = outer.get("level"), outer.get("function"), outer.get("requestId")
    elif m := _LEVEL_FIRST.match(rest):
        kv, style, level = _kv(m.group(2)), "record_lookup", m.group(1)
    else:
        kv = _kv(rest)
        style = _style_of_logfmt(kv) if kv is not None else "logfmt"
    if not kv or not kv.get("event"):
        return None
    return {
        "ts": to_iso(ts),
        "level": level or kv.get("level") or kv.get("severity"),
        "service": service or kv.get("service"),
        "trace_id": trace or kv.get("trace_id"),
        "event": kv["event"],
        "kv": kv,
        "style": style,
    }


def _int(v) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _rows(kv: dict) -> str:
    rows = _int(kv.get("rows"))
    return "UNKNOWN" if rows is None else "FOUND" if rows > 0 else "NOT_FOUND"


def _profile(kv: dict) -> str:
    rows, status = _int(kv.get("rows")), _int(kv.get("http_status"))
    if rows == 0 or status == 404:
        return "NOT_FOUND"
    return "FOUND" if (rows or 0) >= 1 or status == 200 else "UNKNOWN"


def _index(kv: dict) -> str:
    result, hits = str(kv.get("result", "")).lower(), _int(kv.get("total_hits"))
    if result == "hit" or (hits or 0) > 0:
        return "FOUND"
    return "NOT_FOUND" if result == "miss" or hits == 0 else "UNKNOWN"


def _analytics(kv: dict) -> str:
    direct = _int(kv.get("direct_identifiers")) or 0
    fields = ("customer_reference", "user_id", "email", "phone", "email_hash", "matched_value")
    return "IDENTIFIABLE" if direct > 0 or any(not is_null(kv.get(f)) for f in fields) else "ANONYMIZED"


def _scan(clean: str):
    def rule(kv: dict) -> str:
        hit = not is_null(kv.get("matched_value")) or (_int(kv.get("direct_identifiers")) or 0) > 0
        return "IDENTIFIABLE" if hit else clean
    return rule


def _support(kv: dict) -> str:
    return "IDENTIFIABLE" if support_exposed(kv) else "REDACTED"


def _message(kv: dict) -> str:
    """Redacted only when the text carries the [REDACTED] marker and no identifier remains."""
    text = kv.get("message_text")
    if is_null(text):
        return "UNKNOWN"
    return "REDACTED" if "[REDACTED]" in str(text) and not support_exposed(kv) else "IDENTIFIABLE"


def _flag(field: str, yes: str, no: str):
    def rule(kv: dict) -> str:
        return {True: yes, False: no}.get(truthy(kv.get(field)), "UNKNOWN")
    return rule


def _restore_policy(kv: dict) -> str:
    if truthy(kv.get("archive_present")) is False:
        return "NOT_FOUND"
    return _flag("restore_allowed", "ACTIVE", "BLOCKED")(kv)


def _restore_candidate(kv: dict) -> str:
    eligible, blocked = truthy(kv.get("eligible")), truthy(kv.get("deletion_block"))
    if eligible is None and blocked is None:
        return "UNKNOWN"
    return "ACTIVE" if eligible and blocked is False else "BLOCKED"


def _restore_auth(kv: dict) -> str:
    return {"deny": "BLOCKED", "allow": "ACTIVE"}.get(str(kv.get("decision", "")).lower(), "UNKNOWN")


def _result(kv: dict) -> str:
    result = str(kv.get("result", "")).strip().upper()
    return result if result in STATES else "UNKNOWN"


# event -> (collection used for the category, state rule)
EVENT_STATE_RULES = {
    "profile_lookup_completed": ("customer_profiles", _profile),
    "index_query_completed": ("customer_search_documents", _index),
    "analytics_profile_read": ("customer_analytics_profiles", _analytics),
    "analytics_profile_read_completed": ("customer_analytics_profiles", _analytics),
    "analytics_identity_scan": ("customer_analytics_profiles", _scan("ANONYMIZED")),
    "message_pii_scan": ("support_conversations", _scan("REDACTED")),
    "attachment_pii_scan": ("ticket_attachments", _scan("FOUND")),
    "message_read": ("support_conversations", _message),
    "support_ticket_read_completed": ("support_records", _support),
    "connector_config_read_completed": ("connector_configurations", _flag("enabled", "ACTIVE", "DISABLED")),
    "connector_heartbeat": ("connector_configurations", _flag("enabled", "ACTIVE", "DISABLED")),
    "connector_config_loaded": ("connector_configurations", _flag("enabled", "ACTIVE", "DISABLED")),
    "restore_policy_read_completed": ("backup_catalog", _restore_policy),
    "restore_candidate_evaluated": ("backup_catalog", _restore_candidate),
    "restore_authorization_completed": ("backup_catalog", _restore_auth),
    "audit_record_read_completed": ("security_audit_events", _rows),
    "export_download_check_completed": ("customer_export_jobs", _flag("available", "FOUND", "NOT_FOUND")),
    "export_metadata_read_completed": ("customer_export_jobs", _flag("available", "FOUND", "NOT_FOUND")),
    "retained_order_loaded": ("retained_order_records", lambda kv: "FOUND"),
    "contact_lookup_completed": (None, _rows),
    "contact_query_completed": (None, _rows),
    "deletion_marker_check": (None, _flag("marker_found", "NOT_FOUND", "FOUND")),
    "record_lookup": (None, _result),
}


def is_start_event(event: str) -> bool:
    return event.endswith("_started") and event not in NOISE_EVENTS


def parse(entry: ManifestEntry, path: Path) -> tuple[list[Observation], list[Coverage]]:
    """One observation per state-bearing line; noise and start lines only feed the join."""
    parsed: list[tuple[int, dict]] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        rec = parse_log_line(line)
        if rec is None:
            warnings.warn(f"{entry.file_id} line {n}: unparseable log line", ParseWarning)
            continue
        parsed.append((n, rec))

    # Traces are interleaved, so each result line is joined to its `*_started` line on trace_id
    # and also carries the lookup key of its request.
    started: dict[str, dict] = {}
    for _, rec in parsed:
        if is_start_event(rec["event"]) and rec["trace_id"]:
            started.setdefault(rec["trace_id"], {}).update(rec["kv"])

    observations = []
    for n, rec in parsed:
        event, kv = rec["event"], rec["kv"]
        if event in NOISE_EVENTS or is_start_event(event):
            continue
        if event not in EVENT_STATE_RULES:
            warnings.warn(f"{entry.file_id} line {n}: unrecognised event {event!r}", ParseWarning)
            continue
        rid = kv.get("resource_id")
        if is_null(rid):
            warnings.warn(f"{entry.file_id} line {n}: {event} without resource_id", ParseWarning)
            continue
        collection, rule = EVENT_STATE_RULES[event]
        fields = dict(kv)
        for key in ("level", "service", "trace_id"):
            if rec[key] is not None:
                fields.setdefault(key, rec[key])
        identifiers = set(identifiers_of(kv))
        start = started.get(rec["trace_id"] or "")
        if start:
            fields["started"] = start
            identifiers |= identifiers_of(start)
        state = rule(kv)
        if state == "UNKNOWN":
            warnings.warn(f"{entry.file_id} line {n}: {event} state undeterminable", ParseWarning)
        observations.append(make_observation(
            entry, resource_id=rid, state=state,
            category=category_for(collection=collection, record_type=kv.get("record_type")),
            identifiers=identifiers, source=f"log:L{n}", fields=fields, observed_at=rec["ts"],
            system=kv.get("system") or rec["service"]))
    return observations, []
