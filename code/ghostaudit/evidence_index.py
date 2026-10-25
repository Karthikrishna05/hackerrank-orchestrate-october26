"""T7: evidence index and time filter over parsed observations.

`observed_at` decides the state at a cutoff; the manifest `captured_at` decides citability.
Equal timestamps count as at-or-before.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import NamedTuple

from .models import Coverage, ManifestEntry, Observation, SubjectProfile
from .normalize import split_resource_id
from .timeutil import parse_ts

REASONS = ("identifier", "resource_link", "extra_link", "field_reference")

_DIGITS = re.compile(r"(\d+)")


class SubjectMatch(NamedTuple):
    """An observation linked to a subject and why: one of REASONS."""

    observation: Observation
    reason: str


def natural_key(s: str) -> tuple:
    """Sort key that orders `evidence_9` before `evidence_10`."""
    return tuple(int(p) if p.isdigit() else p for p in _DIGITS.split(s))


def is_resource_id(value: object) -> bool:
    """True for a resource-shaped string such as `search_052`; `user_N` ids are identifiers, not resources."""
    if not isinstance(value, str):
        return False
    try:
        prefix, _ = split_resource_id(value)
    except ValueError:
        return False
    return prefix != "user"


def _referenced_ids(value: object, own: str) -> set[str]:
    """Resource ids held as whole field values anywhere in a nested record, except `own`."""
    if isinstance(value, Mapping):
        return set().union(*(_referenced_ids(v, own) for v in value.values()))
    if isinstance(value, (list, tuple)):
        return set().union(*(_referenced_ids(v, own) for v in value))
    if is_resource_id(value) and value.strip() != own:
        return {value.strip()}
    return set()


class EvidenceIndex:
    """Observations indexed by resource id, identifier token, file id and field references."""

    def __init__(self, observations: Iterable[Observation], coverages: Iterable[Coverage],
                 manifest: Iterable[ManifestEntry] = ()):
        self.warnings: list[str] = []
        self._captured = {}
        for m in manifest:
            try:
                self._captured[m.file_id] = parse_ts(m.captured_at)
            except ValueError as exc:
                raise ValueError(f"{m.file_id}: bad manifest captured_at: {exc}") from exc
        rows = []
        for o in observations:
            try:
                rows.append((parse_ts(o.observed_at), self._capture_ts(o.file_id, o.captured_at), o))
            except ValueError as exc:
                self.warnings.append(f"{o.file_id} {o.source}: skipped observation of {o.resource_id}: {exc}")
        rows.sort(key=lambda r: (r[0], r[1], natural_key(r[2].file_id), natural_key(r[2].source)))
        self._obs = [r[2] for r in rows]
        self._obs_ts = [r[0] for r in rows]
        self._cap_ts = [r[1] for r in rows]
        self._by_resource: dict[str, list[int]] = defaultdict(list)
        self._by_token: dict[str, list[int]] = defaultdict(list)
        self._by_file: dict[str, list[int]] = defaultdict(list)
        self._refs: dict[str, list[int]] = defaultdict(list)
        for i, o in enumerate(self._obs):
            self._by_resource[o.resource_id].append(i)
            self._by_file[o.file_id].append(i)
            for token in o.identifiers:
                self._by_token[token].append(i)
            for rid in _referenced_ids(o.fields, o.resource_id):
                self._refs[rid].append(i)
        covs = []
        for c in coverages:
            try:
                covs.append((parse_ts(c.observed_at), self._capture_ts(c.file_id, c.captured_at), c))
            except ValueError as exc:
                self.warnings.append(f"{c.file_id}: skipped coverage of {c.system_id}/{c.collection}: {exc}")
        covs.sort(key=lambda r: (r[0], r[1], natural_key(r[2].file_id), r[2].store or "", r[2].collection),
                  reverse=True)
        self._coverages = covs

    @classmethod
    def build(cls, manifest: list[ManifestEntry] | None = None) -> EvidenceIndex:
        """Parse every manifest file once (via `parsers.parse_all`) and index the result."""
        from .loaders import load_manifest
        from .parsers import parse_all

        manifest = load_manifest() if manifest is None else manifest
        observations, coverages, warnings = parse_all(manifest)
        index = cls(observations, coverages, manifest)
        index.warnings = list(warnings) + index.warnings
        return index

    def _capture_ts(self, file_id: str, fallback: str) -> datetime:
        return self._captured.get(file_id) or parse_ts(fallback)

    def _visible(self, i: int, cut: datetime, strict: bool) -> bool:
        return self._obs_ts[i] <= cut and (not strict or self._cap_ts[i] <= cut)

    def for_subject(self, profile: SubjectProfile, cutoff: str,
                    extra_links: Mapping[str, Iterable[str]] | None = None, *,
                    strict: bool = False) -> list[SubjectMatch]:
        """Observations linked to the subject at `cutoff`, in history order, each with its reason.

        Direct matches share a strong identifier (names never link). One hop then adds every
        observation of a matched resource, and observations whose fields reference one.
        `extra_links` ({resource_id: tokens}) marks a resource as matched when a token hits the profile.
        `strict` also requires the file to be captured at or before `cutoff`.
        """
        cut = parse_ts(cutoff)
        strong = profile.strong_identifiers()
        reasons: dict[int, str] = {}
        direct: set[str] = set()
        for token in sorted(strong):
            for i in self._by_token.get(token, ()):
                if self._visible(i, cut, strict):
                    reasons[i] = "identifier"
                    direct.add(self._obs[i].resource_id)
        extra = {rid for rid, tokens in (extra_links or {}).items()
                 if {t.strip().lower() for t in tokens} & strong} - direct
        for rids, reason in ((direct, "resource_link"), (extra, "extra_link")):
            for rid in sorted(rids):
                for i in self._by_resource.get(rid, ()):
                    if self._visible(i, cut, strict):
                        reasons.setdefault(i, reason)
        for rid in sorted(direct | extra):
            for i in self._refs.get(rid, ()):
                if self._visible(i, cut, strict):
                    reasons.setdefault(i, "field_reference")
        return [SubjectMatch(self._obs[i], reasons[i]) for i in sorted(reasons)]

    def history(self, resource_id: str, cutoff: str, *, strict: bool = False) -> list[Observation]:
        """Observations of a resource with `observed_at <= cutoff`, oldest first."""
        cut = parse_ts(cutoff)
        return [self._obs[i] for i in self._by_resource.get(resource_id, ()) if self._visible(i, cut, strict)]

    def state_at(self, resource_id: str, cutoff: str, *, strict: bool = False) -> Observation | None:
        """The latest observation of a resource at or before `cutoff`."""
        hist = self.history(resource_id, cutoff, strict=strict)
        return hist[-1] if hist else None

    def states_by_store(self, resource_id: str, cutoff: str, *,
                        strict: bool = False) -> dict[str, Observation]:
        """The latest observation per store (None keyed `"-"`), for replica drift."""
        latest: dict[str, Observation] = {}
        for o in self.history(resource_id, cutoff, strict=strict):
            latest[o.store or "-"] = o
        return dict(sorted(latest.items()))

    def citable(self, resource_id: str, cutoff: str) -> str | None:
        """File with the latest manifest `captured_at <= cutoff` among files observing the resource."""
        cut = parse_ts(cutoff)
        best = max(((self._cap_ts[i], natural_key(self._obs[i].file_id), self._obs[i].file_id)
                    for i in self._by_resource.get(resource_id, ()) if self._cap_ts[i] <= cut),
                   default=None)
        return best[2] if best else None

    def coverage(self, system_id: str, cutoff: str) -> list[Coverage]:
        """Full-collection listings for a system with `observed_at <= cutoff`, newest first."""
        cut = parse_ts(cutoff)
        return [c for ts, _, c in self._coverages if c.system_id == system_id and ts <= cut]

    def absent_in(self, resource_id: str, system_id: str, cutoff: str) -> Coverage | None:
        """The newest citable coverage of `system_id`, if it proves the resource absent.

        Returns None when that listing's file mentions the resource, or when a later observation
        (up to `cutoff`) shows the resource present: an older listing never overrides newer evidence.
        """
        cut = parse_ts(cutoff)
        mentions = self._by_resource.get(resource_id, ())
        newest = next(((ts, c) for ts, cap, c in self._coverages
                       if c.system_id == system_id and ts <= cut and cap <= cut), None)
        if newest is None or any(self._obs[i].file_id == newest[1].file_id for i in mentions):
            return None
        seen_later = any(newest[0] < self._obs_ts[i] <= cut and self._obs[i].state != "NOT_FOUND"
                         for i in mentions)
        return None if seen_later else newest[1]

    def in_file(self, file_id: str) -> list[Observation]:
        """Every observation from one evidence file, in history order."""
        return [self._obs[i] for i in self._by_file.get(file_id, ())]

    def with_token(self, token: str, cutoff: str | None = None) -> list[Observation]:
        """Observations carrying an identifier token (strong or `name:`), optionally up to `cutoff`."""
        idx = self._by_token.get(token, ())
        if cutoff is None:
            return [self._obs[i] for i in idx]
        cut = parse_ts(cutoff)
        return [self._obs[i] for i in idx if self._obs_ts[i] <= cut]

    def resource_ids(self) -> list[str]:
        """Every indexed resource id, sorted."""
        return sorted(self._by_resource, key=natural_key)
