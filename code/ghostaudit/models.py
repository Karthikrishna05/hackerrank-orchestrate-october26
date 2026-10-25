"""Shared data model for the deletion-audit tools. See code/docs/tools_spec.md §2."""

from __future__ import annotations

from dataclasses import dataclass, field

# Normalised resource states produced by the evidence parsers.
STATES = (
    "FOUND",
    "NOT_FOUND",
    "IDENTIFIABLE",
    "ANONYMIZED",
    "REDACTED",
    "ACTIVE",
    "DISABLED",
    "BLOCKED",
    "UNKNOWN",
)

# Names are weak identifiers (shared by several users), so they are kept apart
# from strong tokens inside Observation.identifiers: "name:<casefolded name>".
NAME_PREFIX = "name:"


@dataclass(frozen=True)
class Request:
    case_id: str
    email: str
    requested_at: str
    reported_status: str
    reported_at: str
    audit_at: str
    request_text: str


@dataclass(frozen=True)
class UserIdentity:
    user_id: str
    name: str
    email: str
    phone: str
    email_hash: str
    updated_at: str


@dataclass(frozen=True)
class AuditChange:
    audit_id: str
    user_id: str
    event_time: str
    field_name: str
    old_value: str
    new_value: str


@dataclass(frozen=True)
class Activity:
    activity_id: str
    event_time: str
    event_type: str
    source_system: str
    target_system: str
    resource_id: str
    reported_result: str
    details: str


@dataclass(frozen=True)
class Rule:
    rule_id: str
    data_category: str
    condition: str
    required_action: str


@dataclass(frozen=True)
class ManifestEntry:
    file_id: str
    file_path: str
    origin: str
    captured_at: str


@dataclass
class SubjectProfile:
    """Every identifier a user has had. Values are normalised (see normalize.py)."""

    user_id: str
    names: set[str] = field(default_factory=set)
    emails: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)
    email_hashes: set[str] = field(default_factory=set)
    history: list[AuditChange] = field(default_factory=list)

    def strong_identifiers(self) -> frozenset[str]:
        """Tokens that identify exactly one user: user_id, emails, phones, hashes."""
        return frozenset({self.user_id} | self.emails | self.phones | self.email_hashes)


@dataclass
class Observation:
    """One fact about one resource, taken from one evidence item."""

    file_id: str
    captured_at: str  # manifest captured_at: decides whether the file may be cited
    observed_at: str  # record/line timestamp: decides the state as of audit_at
    system_id: str
    resource_id: str
    state: str  # one of STATES
    category: str | None
    identifiers: frozenset[str]  # strong tokens plus NAME_PREFIX-tagged names
    store: str | None
    source: str
    fields: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Coverage:
    """The evidence item lists the full contents of a collection, so absence there proves absence."""

    file_id: str
    captured_at: str
    observed_at: str
    system_id: str
    store: str | None
    collection: str


@dataclass(frozen=True)
class Step:
    step_no: int
    phase: str
    system_id: str
    resource_id: str
    operation: str
    state: str
    evidence_id: str
