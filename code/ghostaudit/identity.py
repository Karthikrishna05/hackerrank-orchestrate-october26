"""Identity resolver: current snapshot plus audit-log history, with a reverse token index."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from .models import AuditChange, SubjectProfile, UserIdentity
from .normalize import email_hash, name_token, norm_email, norm_hash, norm_phone
from .timeutil import parse_ts


def _audit_key(change: AuditChange) -> tuple:
    return parse_ts(change.event_time), int(change.audit_id.rsplit("_", 1)[-1])


class IdentityIndex:
    """Every identifier each user has held. `names` hold `name:` tokens (see models.NAME_PREFIX)."""

    def __init__(self, identities: Iterable[UserIdentity], audit_log: Iterable[AuditChange]):
        self._profiles: dict[str, SubjectProfile] = {}
        self._owners: dict[str, set[str]] = defaultdict(set)
        for ident in identities:
            if ident.user_id in self._profiles:
                raise ValueError(f"duplicate user_id in identities: {ident.user_id}")
            p = SubjectProfile(user_id=ident.user_id)
            self._profiles[ident.user_id] = p
            self._add(p, "name", ident.name)
            self._add(p, "email", ident.email)
            self._add(p, "phone", ident.phone)
            self._add(p, "email_hash", ident.email_hash)
        for change in sorted(audit_log, key=_audit_key):
            p = self._profiles.get(change.user_id)
            if p is None:
                raise ValueError(f"{change.audit_id}: unknown user_id {change.user_id}")
            p.history.append(change)
            self._add(p, change.field_name, change.old_value, change.audit_id)
            self._add(p, change.field_name, change.new_value, change.audit_id)
        for p in self._profiles.values():
            for token in {p.user_id} | p.names | p.emails | p.phones | p.email_hashes:
                self._owners[token].add(p.user_id)

    @staticmethod
    def _add(p: SubjectProfile, field_name: str, value: str, audit_id: str = "") -> None:
        if not value:
            return
        if field_name == "email":
            p.emails.add(norm_email(value))
            p.email_hashes.add(email_hash(value))
        elif field_name == "phone":
            p.phones.add(norm_phone(value))
        elif field_name == "email_hash":
            p.email_hashes.add(norm_hash(value))
        elif field_name == "name":
            p.names.add(name_token(value))
        else:
            raise ValueError(f"{audit_id or p.user_id}: unsupported identity field {field_name!r}")

    def resolve(self, email: str) -> SubjectProfile | None:
        """Profile of the user who holds or held `email` (any case); None when unknown."""
        owners = self._owners.get(norm_email(email), set())
        if len(owners) > 1:
            raise LookupError(f"email {email!r} belongs to several users: {sorted(owners)}")
        return self._profiles[next(iter(owners))] if owners else None

    def owner_of(self, token: str) -> set[str]:
        """User ids holding a normalised token; strong tokens give at most one, names may give several."""
        return set(self._owners.get(token, ()))

    def profile(self, user_id: str) -> SubjectProfile | None:
        """Profile for a user id, or None."""
        return self._profiles.get(user_id)
