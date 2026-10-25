"""T8: activity stream tools. Details and `reported_result` are claims; nothing here decides deletion status."""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from .evidence_index import is_resource_id, natural_key
from .models import Activity
from .normalize import extract_identifiers
from .timeutil import parse_ts

SIGNALS = ("flow_enabled", "restore_possible", "repopulated", "non_deletion", "identity_link")

# (template name, details text with placeholders, signal). {R} resource id, {S} system,
# {E} email, {U} user id, {X} secondary id in parentheses.
_TEMPLATES = (
    ("final_result", "Deletion worker received the final result for {R} from {S}.", None),
    ("operation_completed", "Deletion worker reported the requested operation as completed. Target resource: {R}.", None),
    ("unconfirmed_state", "Deletion worker completed some internal work but could not confirm the final state. "
                          "Target resource: {R}.", None),
    ("worker_error", "Deletion worker stopped after the target system returned an error. Target resource: {R}.", None),
    ("profile_submitted", "Deletion worker submitted primary profile {R} to {S}.", None),
    ("policy_action_completed", "Policy action completed for {R}.", None),
    ("flow_enabled_check", "Connector status check confirmed that data flow {R} remains enabled.", "flow_enabled"),
    ("restore_access_check", "Restore-access check confirmed that archive {R} can still be restored.",
     "restore_possible"),
    ("classified_non_deletion", "Privacy intake classified request for {R} as non-deletion; "
                                "no destructive action was authorized.", "non_deletion"),
    ("catalog_link", "Data catalog linked {R} ({X}) to {E} and {U}.", "identity_link"),
    ("attachment_link", "Attachment catalog confirmed {R} ({X}) belongs only to {E} and {U}; "
                        "no other subject is linked.", "identity_link"),
    ("connector_repopulated", "Active connector repopulated downstream resource {R}.", "repopulated"),
    ("restore_repopulated", "Restore job repopulated downstream resource {R}.", "repopulated"),
    ("export_processed", "Scheduled account export processed {R}.", None),
    ("index_refresh", "Customer search-index refresh processed {R}.", None),
    ("cache_refresh", "Profile cache refresh processed {R}.", None),
    ("security_archival", "Security-event archival processed {R}.", None),
    ("email_delivery", "Transactional email delivery processed {R}.", None),
    ("profile_sync", "Customer profile synchronization processed {R}.", None),
    ("replication_checkpoint", "Replication worker reported its final checkpoint for {R}.", None),
    ("replication_checkpoint_target", "Replication worker reported its final checkpoint. Target resource: {R}.", None),
    ("preference_update", "Profile preference update processed {R}.", None),
    ("support_index_update", "Support-case index update processed {R}.", None),
)

_PLACEHOLDERS = {
    "{R}": r"[a-z][a-z_]*_\d+",
    "{S}": r"[a-z][a-z_]*",
    "{E}": r"[^\s@()]+@[^\s@()]+?",
    "{U}": r"user_\d+",
    "{X}": r"(?P<secondary>[^()]+)",
}


def _compile(text: str) -> re.Pattern:
    parts = re.split(r"(\{[A-Z]\})", text)
    return re.compile("".join(_PLACEHOLDERS.get(p, re.escape(p)) for p in parts))


_COMPILED = tuple((name, _compile(text), signal) for name, text, signal in _TEMPLATES)

# A resource id inside free text; not part of an email or a file name such as `x_19.pdf`.
_RESOURCE_IN_TEXT = re.compile(r"(?<![\w.%+\-@])[a-z][a-z_]*_\d+(?![\w\-@]|\.\w)")


@dataclass(frozen=True)
class ActivityDetails:
    """What a details string says. `claimed_result` is the row's reported_result: a claim, not a state."""

    template: str
    resource_ids: tuple[str, ...]
    signal: str | None
    claimed_result: str
    identifiers: frozenset[str] = frozenset()
    secondary: str | None = None


def parse_details(activity: Activity) -> ActivityDetails:
    """Match the details text against the known templates; unknown text gives `template="unknown"`."""
    text = activity.details.strip()
    template, signal, secondary = "unknown", None, None
    for name, rx, sig in _COMPILED:
        m = rx.fullmatch(text)
        if m:
            template, signal, secondary = name, sig, m.groupdict().get("secondary")
            break
    found = [activity.resource_id] if is_resource_id(activity.resource_id) else []
    found += [r for r in _RESOURCE_IN_TEXT.findall(text) if is_resource_id(r)]
    return ActivityDetails(template=template, resource_ids=tuple(dict.fromkeys(found)), signal=signal,
                           claimed_result=activity.reported_result,
                           identifiers=frozenset(extract_identifiers(text)), secondary=secondary)


def _row_key(a: Activity) -> tuple:
    try:
        return parse_ts(a.event_time), natural_key(a.activity_id)
    except ValueError as exc:
        raise ValueError(f"{a.activity_id}: bad event_time: {exc}") from exc


class ActivityLog:
    """The global activity stream, time-ordered, with parsed details and a resource index."""

    def __init__(self, rows: Iterable[Activity]):
        self.rows: list[Activity] = sorted(rows, key=_row_key)
        self._ts = [parse_ts(a.event_time) for a in self.rows]
        self._details = [parse_details(a) for a in self.rows]
        self._pos = {a.activity_id: i for i, a in enumerate(self.rows)}
        self._by_resource: dict[str, list[int]] = defaultdict(list)
        for i, d in enumerate(self._details):
            for rid in d.resource_ids:
                self._by_resource[rid].append(i)

    def details(self, activity: Activity | str) -> ActivityDetails:
        """Parsed details for an activity or activity_id."""
        aid = activity if isinstance(activity, str) else activity.activity_id
        return self._details[self._pos[aid]]

    def unknown(self) -> list[str]:
        """Activity ids whose details match no known template."""
        return [a.activity_id for a, d in zip(self.rows, self._details) if d.template == "unknown"]

    def _limit(self, until: str | None) -> int:
        return len(self.rows) if until is None else bisect_right(self._ts, parse_ts(until))

    def for_resources(self, ids: str | Iterable[str], until: str | None = None) -> list[Activity]:
        """Rows whose resource_id or details mention any of `ids`, at or before `until`, in time order."""
        wanted = {ids} if isinstance(ids, str) else set(ids)
        end = self._limit(until)
        hits = {i for rid in wanted for i in self._by_resource.get(rid, ()) if i < end}
        return [self.rows[i] for i in sorted(hits)]

    def window(self, start: str | None = None, end: str | None = None) -> list[Activity]:
        """Rows with start <= event_time <= end (either bound may be None)."""
        lo = 0 if start is None else bisect_left(self._ts, parse_ts(start))
        return self.rows[lo:self._limit(end)]

    def with_signal(self, signal: str, until: str | None = None) -> list[Activity]:
        """Rows carrying a details signal (see SIGNALS), at or before `until`."""
        return [self.rows[i] for i in range(self._limit(until)) if self._details[i].signal == signal]

    def links(self, until: str | None = None) -> dict[str, set[str]]:
        """resource_id -> identifier tokens from LINK_RESOURCE_IDENTITY rows (emails, user ids,
        and the parenthesised secondary id when it is a resource id)."""
        out: dict[str, set[str]] = defaultdict(set)
        for i in range(self._limit(until)):
            a, d = self.rows[i], self._details[i]
            if a.event_type != "LINK_RESOURCE_IDENTITY":
                continue
            tokens = set(d.identifiers)
            if d.secondary and is_resource_id(d.secondary):
                tokens.add(d.secondary.strip())
            out[a.resource_id] |= tokens
        return dict(sorted(out.items()))

    def non_deletion_flags(self, until: str | None = None) -> dict[str, str]:
        """resource_id -> latest CLASSIFY_REQUEST activity_id marking the request as non-deletion."""
        out: dict[str, str] = {}
        for i in range(self._limit(until)):
            a, d = self.rows[i], self._details[i]
            if a.event_type == "CLASSIFY_REQUEST" and d.signal == "non_deletion":
                for rid in d.resource_ids:
                    out[rid] = a.activity_id
        return dict(sorted(out.items()))
