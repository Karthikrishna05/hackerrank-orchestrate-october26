"""T10: per-resource decision matrix and case assembly."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace

from .models import Step
from .steps import make_step, sort_and_number

KINDS = ("DELETE", "REDACT", "ANONYMIZE", "RETAIN", "DATA_FLOW", "BACKUP_ARCHIVE")

# (kind, current state) -> (finding, remediation, verification); None means "already compliant".
_FIX = {
    "DELETE": ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"),
    "REDACT": ("REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED"),
    "ANONYMIZE": ("REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"),
    "DATA_FLOW": ("REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED"),
    "BACKUP_ARCHIVE": ("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED"),
}
MATRIX: dict[tuple[str, str], tuple[str | None, str | None, str]] = {
    ("DELETE", "FOUND"): _FIX["DELETE"],
    ("DELETE", "IDENTIFIABLE"): _FIX["DELETE"],
    ("DELETE", "NOT_FOUND"): (None, None, "VERIFY_RECORD_ABSENT"),
    ("REDACT", "IDENTIFIABLE"): _FIX["REDACT"],
    ("REDACT", "REDACTED"): (None, None, "VERIFY_RECORD_REDACTED"),
    ("ANONYMIZE", "IDENTIFIABLE"): _FIX["ANONYMIZE"],
    ("ANONYMIZE", "ANONYMIZED"): (None, None, "VERIFY_RECORD_ANONYMIZED"),
    ("RETAIN", "FOUND"): (None, None, "VERIFY_RECORD_PRESENT"),
    ("DATA_FLOW", "ACTIVE"): _FIX["DATA_FLOW"],
    ("DATA_FLOW", "DISABLED"): (None, None, "VERIFY_DATA_FLOW_DISABLED"),
    ("BACKUP_ARCHIVE", "ACTIVE"): _FIX["BACKUP_ARCHIVE"],
    ("BACKUP_ARCHIVE", "BLOCKED"): (None, None, "VERIFY_RESTORE_BLOCKED"),
}

NOT_APPLICABLE: tuple[str, list[Step]] = ("NOT_APPLICABLE", [])


@dataclass
class ResourcePlan:
    """The steps one resource contributes to a case."""

    findings: list[Step] = field(default_factory=list)
    remediation: list[Step] = field(default_factory=list)
    verification: list[Step] = field(default_factory=list)


def plan(system_id: str, resource_id: str, kind: str, current_state: str,
         evidence_id: str = "") -> ResourcePlan:
    """Apply the T10 decision matrix to one resource; raises ValueError for undefined combinations."""
    if kind not in KINDS:
        raise ValueError(f"{resource_id}: unknown kind {kind!r}")
    row = MATRIX.get((kind, current_state))
    if row is None:
        raise ValueError(f"{resource_id}: no decision for kind {kind} in state {current_state!r}")
    find_op, fix_op, verify_op = row
    if find_op is None:
        return ResourcePlan(verification=[make_step(verify_op, system_id, resource_id, evidence_id)])
    return ResourcePlan(
        findings=[make_step(find_op, system_id, resource_id, evidence_id)],
        remediation=[make_step(fix_op, system_id, resource_id)],
        verification=[make_step(verify_op, system_id, resource_id)],
    )


def assemble(plans: Iterable[ResourcePlan]) -> tuple[str, list[Step]]:
    """Merge resource plans into (status, numbered steps): INCOMPLETE if any finding, else COMPLETE."""
    plans = list(plans)
    if not plans:
        raise ValueError("assemble needs at least one resource plan")
    findings = [s for p in plans for s in p.findings]
    remediation = [s for p in plans for s in p.remediation]
    verification = [s for p in plans for s in p.verification]
    if findings:
        blank = [replace(s, evidence_id="") for s in remediation + verification]
        return "INCOMPLETE", sort_and_number(findings + blank)
    if remediation:
        raise ValueError("remediation steps without any finding")
    return "COMPLETE", sort_and_number(verification)
