"""Deterministic case engine (engine_plan.md §2): one request in, one evidence-backed output row out.

S1 intent gate -> S2 identity -> S3 discovery -> S4 state at audit_at -> S5 policy -> S6 citation
-> S7 plan, assemble, validate. Every rule below is a small table; anything the tables cannot decide
is skipped and recorded in `CaseResult.notes` ("kind: detail"), never guessed silently.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from . import intent_rules, loaders
from .activity import ActivityDetails, ActivityLog
from .evidence_index import EvidenceIndex
from .identity import IdentityIndex
from .models import (
    NAME_PREFIX,
    Activity,
    IntentResult,
    Observation,
    Request,
    Step,
    SubjectProfile,
)
from .normalize import natural_key, split_resource_id, system_for
from .planner import MATRIX, NOT_APPLICABLE, assemble, plan
from .rules import DATA_FLOW, PolicyTable
from .steps import sort_and_number, validate
from .timeutil import parse_ts

INTENT_MODES = ("auto", "rules", "cache")

# ---------------------------------------------------------------- data tables

# S5: category when no observation names one, from the resource-id prefix.
CATEGORY_BY_PREFIX = {
    "account": "USER_PROFILE",
    "search": "SEARCH_INDEX_ENTRY",
    "analytics": "ANALYTICS_PROFILE",
    "support": "SUPPORT_MESSAGE",
    "export": "USER_EXPORT",
    "data_flow": DATA_FLOW,
    "backup": "BACKUP_ARCHIVE",
    "audit_record": "SECURITY_AUDIT_LOG",
    "tombstone": "ACCOUNT_TOMBSTONE",
    "legal_hold": "LEGAL_HOLD_RECORD",
    "marketing": "MARKETING_PROFILE",
    "cache": "CACHE_ENTRY",
    "email_delivery": "EMAIL_DELIVERY_LOG",
}

# S5: category of a resource known only from a link row whose prefix is unknown, by link template.
CATEGORY_BY_LINK_TEMPLATE = {"attachment_link": "SUPPORT_ATTACHMENT"}


@dataclass(frozen=True)
class CategoryHint:
    """Turn a `base` category into `category` when one `when` clause matches a single observation.

    A clause is a tuple of (field-name regex, value regex) pairs; each pair must fullmatch some
    flattened field of the observation. The pseudo-field `@category` is the observation's category.
    """

    name: str
    base: frozenset[str]
    category: str
    when: tuple[tuple[tuple[str, str], ...], ...]


_PDF_FILE = ("file|file_name|filename|attachment_name", r"(?i)\S+\.pdf")

# S5: field hints, applied in order over every observation at or before audit_at (first match wins).
CATEGORY_HINTS = (
    CategoryHint("email_opt_in", frozenset({"ANALYTICS_PROFILE"}), "MARKETING_PROFILE",
                 ((("email_opt_in|marketing_opt_in", r".+"),),)),
    CategoryHint("attachment_marker", frozenset({"SUPPORT_MESSAGE"}), "SUPPORT_ATTACHMENT",
                 ((_PDF_FILE, ("storage_class", r".+")),
                  (_PDF_FILE, ("event", r"attachment_\w+")),
                  (("@category", "SUPPORT_ATTACHMENT"),))),
    CategoryHint("legacy_contact_copy", frozenset({"SUPPORT_MESSAGE"}), "SECONDARY_IDENTIFIER",
                 ((("source", "legacy_contact_index"),), (("event", "deletion_marker_check"),))),
)

# S5: a RETAIN rule holds only while these dates (when present) are after audit_at.
RETENTION_FIELDS = ("retention_until", "retained_until")

# S4: observed state -> planner state for (kind, state) pairs the decision matrix lacks.
# "@identifiers": IDENTIFIABLE if a strong identifier is left in the observation, else COMPLIANT_STATE.
STATE_RULES = {
    ("REDACT", "FOUND"): "@identifiers",
    ("ANONYMIZE", "FOUND"): "@identifiers",
    ("RETAIN", "IDENTIFIABLE"): "FOUND",
    ("RETAIN", "REDACTED"): "FOUND",
    ("RETAIN", "ANONYMIZED"): "FOUND",
}
COMPLIANT_STATE = {"REDACT": "REDACTED", "ANONYMIZE": "ANONYMIZED"}
# Identifier types a category may keep (a tombstone keeps its stable user_id).
PERMITTED_IDENTIFIERS = {"ACCOUNT_TOMBSTONE": re.compile(r"user_\d+")}

# S3 fallback: a linked resource with no observation is treated as present (fail-safe), per kind.
PRESENT_STATE = {"DELETE": "FOUND", "REDACT": "IDENTIFIABLE", "ANONYMIZE": "IDENTIFIABLE",
                 "RETAIN": "FOUND", DATA_FLOW: "ACTIVE", "BACKUP_ARCHIVE": "ACTIVE"}

# The only valid row that needs no citation; used when a deletion case has nothing usable.
SAFEST_ROW = NOT_APPLICABLE

# S6: reported results of a deletion attempt that did not succeed (owner decision 6).
UNSUCCESSFUL_RESULTS = frozenset({"FAILED", "PARTIAL"})


# ---------------------------------------------------------------- results and context

@dataclass
class CaseResult:
    """One output row plus the notes explaining how it was reached."""

    status: str
    steps: list[Step]
    notes: list[str] = field(default_factory=list)
    case_id: str = ""
    intent: IntentResult | None = None

    def row(self) -> tuple[str, str, list[Step]]:
        """(case_id, status, steps) for output.write_output."""
        return self.case_id, self.status, self.steps


def note_kind(note: str) -> str:
    """The kind prefix of a note ("link_fallback: ..." -> "link_fallback")."""
    return note.split(":", 1)[0]


def text_key(text: str) -> str:
    """Cache key of a request text: sha256 of its UTF-8 bytes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def llm_cache_lookup() -> Callable[[str], IntentResult | None] | None:
    """`intent_llm.cached` (cache-only, never calls the API), or None when that module is absent."""
    try:
        from . import intent_llm
    except ImportError:
        return None
    fn = getattr(intent_llm, "cached", None)
    return fn if callable(fn) else None


@dataclass
class Context:
    """Everything a run loads once: identities, evidence, activity, policy and the intent source."""

    identities: IdentityIndex
    evidence: EvidenceIndex
    activity: ActivityLog
    policy: PolicyTable
    intent_mode: str = "rules"
    llm_lookup: Callable[[str], IntentResult | None] | None = None

    @classmethod
    def build(cls, intent_mode: str = "auto") -> Context:
        """Load every participant-facing file. `cache` mode fails here when intent_llm is absent."""
        if intent_mode not in INTENT_MODES:
            raise ValueError(f"intent mode must be one of {INTENT_MODES}, not {intent_mode!r}")
        lookup = llm_cache_lookup() if intent_mode != "rules" else None
        if intent_mode == "cache" and lookup is None:
            raise RuntimeError("--intent cache needs ghostaudit.intent_llm.cached, which is not available")
        return cls(IdentityIndex(loaders.load_identities(), loaders.load_audit_log()),
                   EvidenceIndex.build(loaders.load_manifest()),
                   ActivityLog(loaders.load_activity()),
                   PolicyTable(loaders.load_rules()), intent_mode, lookup)

    def intent(self, text: str) -> IntentResult:
        """Intent from the LLM cache (auto, cache) or the keyword baseline (auto fallback, rules)."""
        if self.llm_lookup is not None:
            cached = self.llm_lookup(text)
            if cached is not None:
                return cached
        if self.intent_mode == "cache":
            raise LookupError(f"no cached LLM intent for text sha256={text_key(text)}")
        return intent_rules.classify(text)


# ---------------------------------------------------------------- per-case helpers

@dataclass
class _Resource:
    """A resource discovered for the subject, with its evidence up to audit_at (oldest first)."""

    rid: str
    system_id: str
    history: list[Observation]
    link: Activity | None = None
    link_template: str | None = None


def _obs_key(o: Observation) -> tuple:
    return parse_ts(o.observed_at), parse_ts(o.captured_at), natural_key(o.file_id), natural_key(o.source)


def _prefix(rid: str) -> str | None:
    try:
        return split_resource_id(rid)[0]
    except ValueError:
        return None


def _flatten(value, key: str = ""):
    """(field name, scalar) pairs of a nested record; the bulky `text` field is skipped."""
    if isinstance(value, dict):
        for k, v in value.items():
            if k != "text":
                yield from _flatten(v, k)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _flatten(v, key)
    elif value is not None:
        yield key, str(value)


def _clause_matches(clause: tuple[tuple[str, str], ...], o: Observation) -> bool:
    pairs = list(_flatten(o.fields)) + [("@category", o.category or "")]
    return all(any(re.fullmatch(k, name) and re.fullmatch(v, val) for name, val in pairs) for k, v in clause)


def subject_links(request: Request, profile: SubjectProfile,
                  log: ActivityLog) -> list[tuple[Activity, ActivityDetails]]:
    """LINK_RESOURCE_IDENTITY rows naming the subject, logged in [requested_at, audit_at].

    A catalog link is a lookup made while a request is processed; one older than the request
    belongs to an earlier request of the same person.
    """
    strong = profile.strong_identifiers()
    rows = []
    for a in log.window(request.requested_at, request.audit_at):
        if a.event_type == "LINK_RESOURCE_IDENTITY":
            d = log.details(a)
            if d.identifiers & strong:
                rows.append((a, d))
    return rows


def _documents_citing(reference: str, ctx: Context, cutoff: str) -> list[Observation]:
    """Document-level observations (no resource id) whose facts or text contain `reference`."""
    rx = re.compile(r"(?<![\w-])" + re.escape(reference) + r"(?![\w-])")
    return [o for o in ctx.evidence.history("", cutoff) if rx.search(json.dumps(o.fields, ensure_ascii=False))]


def identifier_free_resources(request: Request, profile: SubjectProfile, ctx: Context) -> list[str]:
    """Hook for records with no identifier left (plan §2.4, T2/T3). Out of scope for now: no resources."""
    return []


def _cross_check(intent: IntentResult, accounts: Iterable[str], request: Request, ctx: Context,
                 notes: list[str]) -> None:
    """Compare the intent with CLASSIFY_REQUEST rows for the subject's accounts; the text decides."""
    flags = ctx.activity.non_deletion_flags(until=request.audit_at)
    flagged = [f"{flags[rid]} on {rid}" for rid in sorted(accounts, key=natural_key) if rid in flags]
    if intent.authorizes_deletion and flagged:
        notes.append(f"intent_conflict: text authorizes deletion but {', '.join(flagged)} says non-deletion")
    elif not intent.authorizes_deletion:
        notes.append(f"intent_corroborated: {', '.join(flagged)}" if flagged
                     else "intent_uncorroborated: no CLASSIFY_REQUEST row for the subject's accounts")


def _discover(request: Request, profile: SubjectProfile, ctx: Context,
              notes: list[str]) -> tuple[list[_Resource], list[str]]:
    """S3: identifier-linked resources plus link-row resources; also returns linked account ids."""
    audit = request.audit_at
    links = subject_links(request, profile, ctx.activity)
    extra = {a.resource_id: d.identifiers for a, d in links}
    matches = ctx.evidence.for_subject(profile, audit, extra)
    rids: set[str] = set()
    documents: dict[str, str | None] = {}
    for m in matches:
        o = m.observation
        if o.resource_id:
            rids.add(o.resource_id)
        elif m.reason == "identifier":
            documents.setdefault(o.file_id, o.category)
    for file_id, category in sorted(documents.items(), key=lambda d: natural_key(d[0])):
        notes.append(f"document_fact: {file_id} names the subject but has no resource id ({category})")
    rids.update(identifier_free_resources(request, profile, ctx))
    resources = {rid: _Resource(rid, "", ctx.evidence.history(rid, audit)) for rid in rids}
    for a, d in links:
        res = resources.get(a.resource_id)
        if res is None:
            res = resources[a.resource_id] = _Resource(a.resource_id, "", [])
            notes.append(f"link_fallback: {a.resource_id} known only from {a.activity_id}")
        elif res.history and parse_ts(res.history[-1].observed_at) < parse_ts(a.event_time):
            notes.append(f"link_newer_than_evidence: {a.activity_id} after {res.history[-1].file_id} "
                         f"for {a.resource_id}")
        res.link, res.link_template = a, d.template
        if d.secondary and d.secondary.strip() != a.resource_id:
            docs = _documents_citing(d.secondary.strip(), ctx, audit)
            if docs:
                res.history = sorted(res.history + docs, key=_obs_key)
                notes.append(f"link_document: {a.resource_id} evidenced by "
                             f"{', '.join(sorted({o.file_id for o in docs}, key=natural_key))} via {d.secondary}")
    accounts = [rid for rid in rids if _prefix(rid) == "account"]
    out = []
    for rid in sorted(resources, key=natural_key):
        res = resources[rid]
        own = [o.system_id for o in res.history if o.resource_id == rid and o.system_id]
        res.system_id = (own[-1] if own else system_for(rid)) or (res.link.target_system if res.link else "")
        if not res.system_id:
            notes.append(f"unknown_system: {rid} skipped")
            continue
        out.append(res)
    return out, accounts


def _current(res: _Resource, ctx: Context, audit: str, notes: list[str]) -> Observation | None:
    """S4: latest observation; an absence is overridden by a copy the same file shows in another store."""
    if not res.history:
        return None
    latest = res.history[-1]
    if latest.state == "NOT_FOUND":
        ghosts = [o for o in ctx.evidence.states_by_store(res.rid, audit).values()
                  if o.file_id == latest.file_id and o.state != "NOT_FOUND"]
        if ghosts:
            notes.append(f"ghost_copy: {res.rid} still in {ghosts[-1].store} of {latest.file_id}")
            return ghosts[-1]
    return latest


def _category(res: _Resource, notes: list[str]) -> str | None:
    """S5: latest named category, else prefix or link-template table, then the field-hint table."""
    named = [o.category for o in res.history if o.category]
    base = named[-1] if named else (CATEGORY_BY_PREFIX.get(_prefix(res.rid) or "")
                                    or CATEGORY_BY_LINK_TEMPLATE.get(res.link_template or ""))
    for hint in CATEGORY_HINTS:
        if base in hint.base and any(_clause_matches(c, o) for o in res.history for c in hint.when):
            notes.append(f"category_hint: {res.rid} {base} -> {hint.category} ({hint.name})")
            return hint.category
    return base


def _retention_expired(res: _Resource, audit: str, notes: list[str]) -> bool:
    """True when the latest retention date on record is at or before audit_at."""
    dates = [v for o in res.history for k, v in _flatten(o.fields) if k in RETENTION_FIELDS]
    if not dates:
        return False
    try:
        expired = parse_ts(dates[-1]) <= parse_ts(audit)
    except ValueError:
        notes.append(f"retention_unparsed: {res.rid} {dates[-1]!r}; RETAIN kept")
        return False
    if expired:
        notes.append(f"retain_expired: {res.rid} retention ended {dates[-1]}; skipped")
    return expired


def _exposed(o: Observation, category: str) -> bool:
    """A strong identifier (not a name, not one the category may keep) is left in the observation."""
    keep = PERMITTED_IDENTIFIERS.get(category)
    return any(not t.startswith(NAME_PREFIX) and not (keep and keep.fullmatch(t)) for t in o.identifiers)


def _planner_state(kind: str, category: str, current: Observation | None, res: _Resource,
                   notes: list[str]) -> str | None:
    """S4 mapping: the matrix state for this resource, or None (with a note) when undecidable."""
    if current is None:
        if res.link is None:
            notes.append(f"state_undecidable: {res.rid} {kind} has no evidence at audit_at")
            return None
        state = PRESENT_STATE[kind]
        notes.append(f"link_assumed_present: {res.rid} treated as {state} ({kind}) citing {res.link.activity_id}")
        return state
    state = current.state
    if (kind, state) in MATRIX:
        return state
    rule = STATE_RULES.get((kind, state))
    if rule == "@identifiers":
        rule = "IDENTIFIABLE" if _exposed(current, category) else COMPLIANT_STATE[kind]
    if rule and (kind, rule) in MATRIX:
        notes.append(f"state_mapped: {res.rid} {kind} {state} -> {rule}")
        return rule
    if kind == "RETAIN" and state == "NOT_FOUND":
        notes.append(f"retained_missing: {res.rid} must be retained but is absent at audit_at")
    else:
        notes.append(f"state_undecidable: {res.rid} {kind} {state} in {current.file_id}")
    return None


def _citation(res: _Resource, ctx: Context, audit: str, absent: bool) -> str:
    """S6: latest file captured by audit_at that observes the resource; for an absence, a full listing."""
    cut = parse_ts(audit)
    # Candidates: document evidence matched through a link row, plus the index's own choice
    # (any observation of that file carries its captured_at).
    found = [o for o in res.history if o.resource_id != res.rid and parse_ts(o.captured_at) <= cut]
    best = ctx.evidence.citable(res.rid, audit)
    if best:
        found += ctx.evidence.in_file(best)[:1]
    if found:
        return max(found, key=lambda o: (parse_ts(o.captured_at), natural_key(o.file_id))).file_id
    if absent:
        cov = ctx.evidence.absent_in(res.rid, res.system_id, audit)
        if cov:
            return cov.file_id
    return ""


def _unsuccessful_attempt(rid: str, ctx: Context, audit: str) -> str:
    """Latest activity row for `rid` at or before audit_at reported FAILED or PARTIAL, else ""."""
    rows = [a for a in ctx.activity.for_resources(rid, until=audit)
            if a.resource_id == rid and a.reported_result in UNSUCCESSFUL_RESULTS]
    return rows[-1].activity_id if rows else ""


def _safest(case_id: str, intent: IntentResult, notes: list[str], reason: str) -> CaseResult:
    notes.append(f"safest_row: {reason}")
    status, steps = SAFEST_ROW
    return CaseResult(status, list(steps), notes, case_id, intent)


# ---------------------------------------------------------------- public API

def audit_case(request: Request, ctx: Context) -> CaseResult:
    """Decide one request: NOT_APPLICABLE, or the evidence-backed COMPLETE/INCOMPLETE plan."""
    notes: list[str] = []
    audit = request.audit_at
    intent = ctx.intent(request.request_text)
    profile = ctx.identities.resolve(request.email)
    if profile is None:
        if not intent.authorizes_deletion:
            return CaseResult(NOT_APPLICABLE[0], [], notes, request.case_id, intent)
        return _safest(request.case_id, intent, notes, "request email matches no known identity")
    resources, accounts = _discover(request, profile, ctx, notes)
    _cross_check(intent, accounts, request, ctx, notes)
    if not intent.authorizes_deletion:
        return CaseResult(NOT_APPLICABLE[0], [], [n for n in notes if note_kind(n).startswith("intent")],
                          request.case_id, intent)

    plans = []
    requested = parse_ts(request.requested_at)
    for res in resources:
        category = _category(res, notes)
        kind = category if category in (DATA_FLOW, "BACKUP_ARCHIVE") else ctx.policy.action(category)
        if kind is None:
            notes.append(f"category_unknown: {res.rid} ({category}) skipped")
            continue
        if kind == "RETAIN" and _retention_expired(res, audit, notes):
            continue
        current = _current(res, ctx, audit, notes)
        state = _planner_state(kind, category, current, res, notes)
        if state is None:
            continue
        # Owner decision 5: gone, with all evidence older than this request and no link row in its window
        # (res.link is only set inside [requested_at, audit_at]): it belongs to an earlier request.
        if state == "NOT_FOUND" and res.link is None and all(
                parse_ts(o.observed_at) < requested for o in res.history):
            notes.append(f"earlier_request_record: {res.rid} absent, all evidence before requested_at; skipped")
            continue
        finding = MATRIX[(kind, state)][0] is not None
        evidence = _citation(res, ctx, audit, absent=state == "NOT_FOUND")
        if finding and not evidence:
            if current is None and res.link is not None:
                evidence = res.link.activity_id
            elif current is not None and kind not in (DATA_FLOW, "BACKUP_ARCHIVE"):
                # Owner decision 6: a record seen present by audit_at only in a file captured later
                # cites the workflow's own failed or partial deletion attempt, never the late file.
                evidence = _unsuccessful_attempt(res.rid, ctx, audit)
                if evidence:
                    notes.append(f"late_export_cited_activity: {res.rid} {state} seen only in a file captured "
                                 f"after audit_at; citing {evidence}")
            if not evidence:
                notes.append(f"uncitable_finding: {res.rid} {state} has no evidence captured by audit_at; skipped")
                continue
        plans.append(plan(res.system_id, res.rid, kind, state, evidence))

    if not plans:
        return _safest(request.case_id, intent, notes, "no usable resource for a deletion request")
    status, steps = assemble(plans)
    if status == "COMPLETE":
        uncited = [s.resource_id for s in steps if not s.evidence_id]
        if uncited:
            notes.append(f"uncited_verification: dropped {', '.join(uncited)} (COMPLETE steps must cite)")
            steps = sort_and_number(s for s in steps if s.evidence_id)
        if not steps:
            return _safest(request.case_id, intent, notes, "COMPLETE case with no citable verification")
    errors = validate(status, steps)
    if errors:
        return _safest(request.case_id, intent, notes, f"invalid row ({errors[0]})")
    return CaseResult(status, steps, notes, request.case_id, intent)


def run(requests: Sequence[Request], ctx: Context) -> list[CaseResult]:
    """Audit every request in order."""
    return [audit_case(r, ctx) for r in requests]
