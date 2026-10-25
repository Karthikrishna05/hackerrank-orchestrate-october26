"""Identifier normalisation, extraction and resource-id helpers.

Matching is exact on normalised tokens; never use substring or fuzzy matching.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

from .models import NAME_PREFIX

SYSTEM_BY_PREFIX = {
    "account": "account_service",
    "search": "search_service",
    "analytics": "analytics_platform",
    "support": "support_platform",
    "export": "export_service",
    "data_flow": "integration_service",
    "backup": "backup_service",
    "audit_record": "security_service",
    "tombstone": "identity_service",
    "legal_hold": "legal_service",
    "cache": "cache_service",
    "email_delivery": "email_delivery_service",
    "marketing": "marketing_platform",
}

_EMAIL_RE = re.compile(
    r"(?<![\w.%+\-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}(?![A-Za-z0-9\-])"
)
_PHONE_RE = re.compile(r"(?<![\w+])\+\d+(?:[ \-]?\(\d+\)[ \-]?\d+|[ \-]\d+)*")
_HASH_RE = re.compile(r"(?<![0-9A-Za-z])(?:(?i:sha256):)?([0-9a-fA-F]{64})(?![0-9A-Za-z])")
_USER_RE = re.compile(r"(?<![\w])(?i:user)_(\d+)(?![\w])")
_RESOURCE_RE = re.compile(r"([a-z][a-z_]*?)_(\d+)")
_WS_RE = re.compile(r"\s+")
_DIGITS_RE = re.compile(r"(\d+)")


def norm_email(s: str) -> str:
    """Strip and lowercase."""
    return s.strip().lower()


def norm_phone(s: str) -> str:
    """Keep a leading `+`, then digits only: `+1 (212) 555-0171` -> `+12125550171`."""
    s = s.strip()
    digits = "".join(c for c in s if c.isdigit())
    return "+" + digits if s.startswith("+") else digits


def norm_name(s: str) -> str:
    """NFC, casefold and collapse whitespace; accents are kept."""
    return _WS_RE.sub(" ", unicodedata.normalize("NFC", s).casefold()).strip()


def norm_hash(s: str) -> str:
    """Lowercase hex without a `sha256:` prefix."""
    s = s.strip().lower()
    return s[len("sha256:"):] if s.startswith("sha256:") else s


def email_hash(email: str) -> str:
    """sha256 hex digest of the normalised email."""
    return hashlib.sha256(norm_email(email).encode("utf-8")).hexdigest()


def name_token(name: str) -> str:
    """Weak identifier token for a person's name: `name:<normalised name>`."""
    return NAME_PREFIX + norm_name(name)


def extract_identifiers(text: str) -> set[str]:
    """Normalised emails, phones, sha256 hashes and `user_N` ids found in `text`. Never names."""
    found: set[str] = set()
    for m in _EMAIL_RE.finditer(text):
        found.add(norm_email(m.group()))
    rest = _EMAIL_RE.sub(lambda m: " " * len(m.group()), text)
    for m in _HASH_RE.finditer(rest):
        found.add(m.group(1).lower())
    for m in _USER_RE.finditer(rest):
        found.add("user_" + m.group(1))
    for m in _PHONE_RE.finditer(rest):
        phone = norm_phone(m.group())
        if 7 <= len(phone) - 1 <= 15:
            found.add(phone)
    return found


def natural_key(s: str) -> tuple:
    """Sort key that orders `evidence_9` before `evidence_10`."""
    return tuple(int(p) if p.isdigit() else p for p in _DIGITS_RE.split(s))


def split_resource_id(rid: str) -> tuple[str, int]:
    """`data_flow_9522` -> (`data_flow`, 9522). Raises ValueError when there is no numeric suffix."""
    m = _RESOURCE_RE.fullmatch(rid.strip())
    if not m:
        raise ValueError(f"not a resource id: {rid!r}")
    return m.group(1), int(m.group(2))


def system_for(resource_id: str) -> str | None:
    """Canonical system for a resource id, or None for an unknown prefix or malformed id."""
    try:
        prefix, _ = split_resource_id(resource_id)
    except ValueError:
        return None
    return SYSTEM_BY_PREFIX.get(prefix)
