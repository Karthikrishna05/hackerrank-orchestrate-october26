"""Timestamp parsing and comparison. Every datetime returned is timezone-aware UTC."""

from __future__ import annotations

import re
from datetime import datetime, timezone

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_MONTH_NAMES = {
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "sept", "october", "november", "december",
} | set(_MONTHS)

# "25 Aug 2026 04 03 UTC", "18 Jun 2026 10     24 UTC" (pdftotext -layout), "28 May 2033".
_HUMAN_DATE = re.compile(
    r"(?<!\d)(\d{1,2})[ \t]+([A-Za-z]{3,9})\.?[ \t]+(\d{4})(?!\d)"
    r"(?:[ \t]+(\d{1,2})(?:[ \t]+|:)(\d{2})(?:(?:[ \t]+|:)(\d{2}))?(?!\d))?"
)


def parse_ts(s: str | datetime) -> datetime:
    """Parse an ISO-8601 timestamp (`...Z`, `+00:00`, `T` or space separator) to aware UTC."""
    if isinstance(s, datetime):
        dt = s
    else:
        text = s.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"unparseable timestamp: {s!r}") from exc
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def at_or_before(ts: str | datetime, cutoff: str | datetime) -> bool:
    """True when `ts` is at or before `cutoff` (equal counts as before)."""
    return parse_ts(ts) <= parse_ts(cutoff)


def to_iso(dt: str | datetime) -> str:
    """Format as `YYYY-MM-DDTHH:MM:SSZ` (UTC, whole seconds)."""
    return parse_ts(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_human_date(s: str) -> datetime | None:
    """First `DD Mon YYYY [HH MM[ SS]] [UTC]` date in `s`, as aware UTC; None when absent."""
    for m in _HUMAN_DATE.finditer(s):
        day, month, year, hh, mm, ss = m.groups()
        if month.casefold() not in _MONTH_NAMES:
            continue
        try:
            return datetime(int(year), _MONTHS[month.casefold()[:3]], int(day),
                            int(hh or 0), int(mm or 0), int(ss or 0), tzinfo=timezone.utc)
        except ValueError:
            continue
    return None
