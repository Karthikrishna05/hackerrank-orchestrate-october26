"""T9: step builder, sorter, validator and JSON serialiser."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import replace
from typing import NamedTuple

from .models import Step

PHASES = ("FINDING", "REMEDIATION", "VERIFICATION")
STATUSES = ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")

# Schema key order of a serialised step.
KEYS = ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id")

# A citation is a manifest file_id, an activity_id or an audit_id.
EVIDENCE_ID_RE = re.compile(r"(?:evidence|activity|audit)_\d+")


class OpSpec(NamedTuple):
    phase: str
    state: str
    rank: int


_TABLE = (
    ("REPORT_REMAINING_RECORD", "FINDING", "PRESENT"),
    ("REPORT_EXPOSED_PII", "FINDING", "PRESENT"),
    ("REPORT_ACTIVE_DATA_FLOW", "FINDING", "ACTIVE"),
    ("REPORT_RESTORE_RISK", "FINDING", "ACTIVE"),
    ("DISABLE_DATA_FLOW", "REMEDIATION", "DISABLED"),
    ("BLOCK_RESTORE", "REMEDIATION", "BLOCKED"),
    ("DELETE_RECORD", "REMEDIATION", "ABSENT"),
    ("REDACT_RECORD", "REMEDIATION", "REDACTED"),
    ("ANONYMIZE_RECORD", "REMEDIATION", "ANONYMIZED"),
    ("VERIFY_RECORD_ABSENT", "VERIFICATION", "ABSENT"),
    ("VERIFY_RECORD_PRESENT", "VERIFICATION", "PRESENT"),
    ("VERIFY_RECORD_REDACTED", "VERIFICATION", "REDACTED"),
    ("VERIFY_RECORD_ANONYMIZED", "VERIFICATION", "ANONYMIZED"),
    ("VERIFY_DATA_FLOW_DISABLED", "VERIFICATION", "DISABLED"),
    ("VERIFY_RESTORE_BLOCKED", "VERIFICATION", "BLOCKED"),
)

# Operation -> (phase, state, rank); rank is the problem-statement table position.
OPERATIONS: dict[str, OpSpec] = {op: OpSpec(ph, st, i) for i, (op, ph, st) in enumerate(_TABLE)}


def make_step(operation: str, system_id: str, resource_id: str, evidence_id: str = "") -> Step:
    """A Step with phase and state taken from OPERATIONS; step_no is 0 until numbered."""
    spec = OPERATIONS.get(operation)
    if spec is None:
        raise ValueError(f"unknown operation {operation!r}")
    return Step(0, spec.phase, system_id, resource_id, operation, spec.state, evidence_id)


def step_key(step: Step) -> tuple[str, str, str, str, str, str]:
    """Every field except step_no: (phase, system_id, resource_id, operation, state, evidence_id)."""
    return (step.phase, step.system_id, step.resource_id, step.operation, step.state, step.evidence_id)


def _sort_key(step: Step) -> tuple:
    spec = OPERATIONS.get(step.operation)
    if spec is None or step.phase not in PHASES:
        raise ValueError(f"cannot sort step with phase {step.phase!r}, operation {step.operation!r}")
    return (PHASES.index(step.phase), spec.rank, step.system_id, step.resource_id, step.evidence_id)


def sort_and_number(steps: Iterable[Step]) -> list[Step]:
    """Drop exact duplicates, sort by (phase, operation rank, system_id, resource_id), number from 1."""
    unique: dict[tuple, Step] = {}
    for s in steps:
        unique.setdefault(step_key(s), s)
    ordered = sorted(unique.values(), key=_sort_key)
    return [replace(s, step_no=i) for i, s in enumerate(ordered, 1)]


def validate(status: str, steps: Sequence[Step]) -> list[str]:
    """Contract violations for one output row; an empty list means valid."""
    errors: list[str] = []
    if status not in STATUSES:
        errors.append(f"invalid deletion_status {status!r}")
    last_phase = -1
    seen: set[tuple] = set()
    for i, s in enumerate(steps, 1):
        tag = f"step {i}"
        if not isinstance(s.step_no, int) or isinstance(s.step_no, bool) or s.step_no != i:
            errors.append(f"{tag}: step_no {s.step_no!r}, expected {i}")
        if not all(isinstance(v, str) for v in step_key(s)):
            errors.append(f"{tag}: every field except step_no must be a string")
            continue
        if not s.system_id or not s.resource_id:
            errors.append(f"{tag}: empty system_id or resource_id")
        spec = OPERATIONS.get(s.operation)
        if spec is None:
            errors.append(f"{tag}: unknown operation {s.operation!r}")
        else:
            if s.phase != spec.phase:
                errors.append(f"{tag}: {s.operation} belongs to phase {spec.phase}, not {s.phase!r}")
            if s.state != spec.state:
                errors.append(f"{tag}: {s.operation} requires state {spec.state}, not {s.state!r}")
        if s.phase not in PHASES:
            errors.append(f"{tag}: unknown phase {s.phase!r}")
        else:
            rank = PHASES.index(s.phase)
            if rank < last_phase:
                errors.append(f"{tag}: phase {s.phase} after {PHASES[last_phase]}")
            last_phase = max(last_phase, rank)
        if s.evidence_id and not EVIDENCE_ID_RE.fullmatch(s.evidence_id):
            errors.append(f"{tag}: evidence_id {s.evidence_id!r} is not a file, activity or audit id")
        if step_key(s) in seen:
            errors.append(f"{tag}: duplicate step")
        seen.add(step_key(s))
        if status == "COMPLETE":
            if s.phase != "VERIFICATION":
                errors.append(f"{tag}: COMPLETE allows only VERIFICATION steps, got {s.phase}")
            elif not s.evidence_id:
                errors.append(f"{tag}: COMPLETE verification must cite evidence")
        elif status == "INCOMPLETE":
            if s.phase == "FINDING" and not s.evidence_id:
                errors.append(f"{tag}: finding must cite evidence")
            if s.phase in ("REMEDIATION", "VERIFICATION") and s.evidence_id:
                errors.append(f"{tag}: INCOMPLETE {s.phase} must have empty evidence_id")
    if status == "NOT_APPLICABLE" and steps:
        errors.append("NOT_APPLICABLE requires steps_json=[]")
    if status == "COMPLETE" and not steps:
        errors.append("COMPLETE requires at least one verification step")
    if status == "INCOMPLETE" and not any(s.phase == "FINDING" for s in steps):
        errors.append("INCOMPLETE requires at least one FINDING step")
    return errors


def to_json(steps: Iterable[Step]) -> str:
    """Compact JSON array with keys in schema order and non-ASCII kept as is."""
    return json.dumps([{k: getattr(s, k) for k in KEYS} for s in steps],
                      separators=(",", ":"), ensure_ascii=False)


def from_json(text: str) -> list[Step]:
    """Parse a steps_json array; raises ValueError on malformed JSON or step objects."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError("steps_json must be a JSON array")
    steps = []
    for i, obj in enumerate(data, 1):
        if not isinstance(obj, dict) or set(obj) != set(KEYS):
            got = sorted(obj) if isinstance(obj, dict) else type(obj).__name__
            raise ValueError(f"step {i}: expected keys {list(KEYS)}, got {got}")
        steps.append(Step(**{k: obj[k] for k in KEYS}))
    return steps
