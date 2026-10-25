"""Tests for ghostaudit.identity (tools_spec T4: identity resolver)."""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from ghostaudit.identity import IdentityIndex
from ghostaudit.models import AuditChange, SubjectProfile, UserIdentity
from ghostaudit.normalize import name_token, norm_email, norm_phone

DATASET = Path(__file__).resolve().parents[2] / "dataset"


def h(email: str) -> str:
    """Independent oracle for the email hash."""
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


def ident(user_id, name, email, phone, email_hash=None):
    return UserIdentity(user_id, name, email, phone, email_hash if email_hash is not None else h(email),
                        "2026-01-01T00:00:00Z")


OLD_EMAIL_1 = "mateo.r.old@outlook.com"

IDENTITIES = [
    ident("user_1", "Mateo Ríos", "mateo.rios@outlook.com", "+34 612 847 593"),
    ident("user_2", "Mateo Ríos", "mrios9507@fastmail.com", "+34 720 474 294"),   # shares the name
    ident("user_3", "Jonas Berg", "jonas.berg@outlook.com", "+46 70 482 91 37"),
    ident("user_4", "Jonas Berglund", "jonas.berglund@outlook.com", "+46 70 111 22 33"),  # decoy name
    ident("user_5", "Sofía Martínez", "smartinez6461@gmail.com", "+34 744 502 860"),
]

# Deliberately not in chronological order.
AUDIT = [
    AuditChange("audit_3", "user_1", "2026-03-01T00:00:00Z", "email", OLD_EMAIL_1, "mateo.rios@outlook.com"),
    AuditChange("audit_4", "user_1", "2026-03-01T00:00:00Z", "email_hash",
                "SHA256:" + h(OLD_EMAIL_1).upper(), h("mateo.rios@outlook.com")),
    AuditChange("audit_1", "user_1", "2025-01-01T00:00:00Z", "phone", "+34 600 000 001", "+34 612 847 593"),
    AuditChange("audit_2", "user_3", "2025-06-01T00:00:00Z", "email", "J.BERG.OLD@OUTLOOK.COM",
                "jonas.berg@outlook.com"),
]


def build(identities=IDENTITIES, audit=AUDIT) -> IdentityIndex:
    return IdentityIndex(identities, audit)


class TestResolve(unittest.TestCase):
    def setUp(self):
        self.index = build()

    def test_resolve_current_email_returns_the_profile(self):
        profile = self.index.resolve("mateo.rios@outlook.com")
        self.assertIsInstance(profile, SubjectProfile)
        self.assertEqual(profile.user_id, "user_1")

    def test_resolve_accepts_historical_email(self):
        self.assertEqual(self.index.resolve(OLD_EMAIL_1).user_id, "user_1")

    def test_resolve_accepts_historical_email_in_any_case(self):
        self.assertEqual(self.index.resolve("j.berg.old@outlook.com").user_id, "user_3")
        self.assertEqual(self.index.resolve("MATEO.R.OLD@OUTLOOK.COM").user_id, "user_1")

    def test_resolve_ignores_surrounding_whitespace(self):
        self.assertEqual(self.index.resolve("  Jonas.Berg@Outlook.com  ").user_id, "user_3")

    def test_resolve_unknown_email_returns_none(self):
        self.assertIsNone(self.index.resolve("nobody@example.com"))

    def test_resolve_does_not_match_decoy_email_with_extra_suffix(self):
        self.assertEqual(self.index.resolve("smartinez6461@gmail.com").user_id, "user_5")
        self.assertIsNone(self.index.resolve("smartinez6461.jr@gmail.com"))

    def test_resolve_does_not_match_a_substring_of_a_known_email(self):
        self.assertIsNone(self.index.resolve("rios@outlook.com"))
        self.assertIsNone(self.index.resolve("jonas.berg@outlook.co"))

    def test_resolve_raises_lookup_error_when_two_users_held_the_email(self):
        audit = AUDIT + [AuditChange("audit_9", "user_2", "2026-04-01T00:00:00Z", "email",
                                     "mateo.rios@outlook.com", "mrios9507@fastmail.com")]
        index = build(audit=audit)
        with self.assertRaises(LookupError):
            index.resolve("mateo.rios@outlook.com")


class TestProfileContents(unittest.TestCase):
    def setUp(self):
        self.profile = build().resolve("mateo.rios@outlook.com")

    def test_profile_holds_current_and_historical_emails_normalised(self):
        self.assertEqual(self.profile.emails, {"mateo.rios@outlook.com", OLD_EMAIL_1})

    def test_profile_holds_current_and_historical_phones_normalised(self):
        self.assertEqual(self.profile.phones, {"+34612847593", "+34600000001"})

    def test_profile_hashes_include_the_computed_hash_of_every_email_ever_held(self):
        self.assertIn(h("mateo.rios@outlook.com"), self.profile.email_hashes)
        self.assertIn(h(OLD_EMAIL_1), self.profile.email_hashes)

    def test_profile_hashes_from_the_audit_log_are_normalised(self):
        for value in self.profile.email_hashes:
            self.assertEqual(value, value.lower())
            self.assertFalse(value.startswith("sha256:"))
            self.assertEqual(len(value), 64)

    def test_profile_hash_of_historical_email_without_hash_audit_row(self):
        jonas = build().resolve("jonas.berg@outlook.com")
        self.assertIn(h("j.berg.old@outlook.com"), jonas.email_hashes)

    def test_profile_names_are_weak_name_tokens(self):
        self.assertEqual(self.profile.names, {name_token("Mateo Ríos")})

    def test_profile_history_is_in_chronological_then_numeric_audit_order(self):
        self.assertEqual([c.audit_id for c in self.profile.history], ["audit_1", "audit_3", "audit_4"])

    def test_strong_identifiers_exclude_names(self):
        strong = self.profile.strong_identifiers()
        self.assertIn("user_1", strong)
        self.assertIn("+34612847593", strong)
        self.assertNotIn(name_token("Mateo Ríos"), strong)

    def test_empty_audit_values_are_ignored(self):
        audit = [AuditChange("audit_1", "user_5", "2025-01-01T00:00:00Z", "phone", "", "+34 744 502 860")]
        profile = build(audit=audit).profile("user_5")
        self.assertEqual(profile.phones, {"+34744502860"})
        self.assertNotIn("", profile.phones)


class TestOwnerOf(unittest.TestCase):
    def setUp(self):
        self.index = build()

    def test_owner_of_email_token_is_exactly_one_user(self):
        self.assertEqual(self.index.owner_of("mateo.rios@outlook.com"), {"user_1"})

    def test_owner_of_historical_email_token(self):
        self.assertEqual(self.index.owner_of(OLD_EMAIL_1), {"user_1"})

    def test_owner_of_normalised_phone(self):
        self.assertEqual(self.index.owner_of(norm_phone("+46 70 482 91 37")), {"user_3"})
        self.assertEqual(self.index.owner_of(norm_phone("+34 600 000 001")), {"user_1"})

    def test_owner_of_email_hash(self):
        self.assertEqual(self.index.owner_of(h(OLD_EMAIL_1)), {"user_1"})

    def test_owner_of_user_id(self):
        self.assertEqual(self.index.owner_of("user_4"), {"user_4"})

    def test_shared_name_returns_several_owners(self):
        self.assertEqual(self.index.owner_of(name_token("Mateo Ríos")), {"user_1", "user_2"})

    def test_name_lookup_is_exact_so_accents_matter(self):
        self.assertEqual(self.index.owner_of(name_token("Mateo Rios")), set())

    def test_decoy_names_do_not_match_each_other(self):
        self.assertEqual(self.index.owner_of(name_token("Jonas Berg")), {"user_3"})
        self.assertEqual(self.index.owner_of(name_token("Jonas Berglund")), {"user_4"})
        self.assertEqual(self.index.owner_of(name_token("Joana Berg")), set())

    def test_unknown_token_gives_empty_set(self):
        self.assertEqual(self.index.owner_of("ghost@example.com"), set())

    def test_returned_set_is_a_copy(self):
        owners = self.index.owner_of("user_1")
        owners.add("user_999")
        self.assertEqual(self.index.owner_of("user_1"), {"user_1"})


class TestProfileLookup(unittest.TestCase):
    def test_profile_by_user_id(self):
        self.assertEqual(build().profile("user_2").emails, {"mrios9507@fastmail.com"})

    def test_profile_unknown_user_id_is_none(self):
        self.assertIsNone(build().profile("user_404"))


class TestConstructionErrors(unittest.TestCase):
    def test_duplicate_user_id_raises_value_error(self):
        with self.assertRaises(ValueError):
            IdentityIndex(IDENTITIES + [ident("user_1", "X", "x@x.com", "+1 555 0100")], [])

    def test_audit_change_for_unknown_user_raises_value_error(self):
        with self.assertRaises(ValueError):
            IdentityIndex(IDENTITIES, [AuditChange("audit_1", "user_404", "2025-01-01T00:00:00Z",
                                                   "email", "a@x.com", "b@x.com")])

    def test_unsupported_audit_field_raises_value_error(self):
        with self.assertRaises(ValueError):
            IdentityIndex(IDENTITIES, [AuditChange("audit_1", "user_1", "2025-01-01T00:00:00Z",
                                                   "shoe_size", "40", "41")])

    def test_empty_inputs_give_an_empty_index(self):
        index = IdentityIndex([], [])
        self.assertIsNone(index.resolve("a@x.com"))
        self.assertEqual(index.owner_of("user_1"), set())


@unittest.skipUnless(DATASET.is_dir(), "dataset not available")
class TestIdentityOnRealDataset(unittest.TestCase):
    """Read-only acceptance checks from tools_spec T4."""

    @classmethod
    def setUpClass(cls):
        from ghostaudit import loaders
        cls.identities = loaders.load_identities()
        cls.index = IdentityIndex(cls.identities, loaders.load_audit_log())
        cls.requests = loaders.load_requests()

    def test_novak_resolves_to_user_32(self):
        self.assertEqual(self.index.resolve("novak.tomasz@icloud.com").user_id, "user_32")

    def test_minjun_kim_includes_hash_of_old_email(self):
        profile = self.index.resolve("minjun.kim@gmail.com")
        self.assertEqual(profile.user_id, "user_12")
        self.assertTrue(any(x.startswith("44a65154bfd8e408") for x in profile.email_hashes))
        self.assertIn(h("minjun.kim90@gmail.com"), profile.email_hashes)
        self.assertTrue(h("minjun.kim90@gmail.com").startswith("44a65154bfd8e408"))

    def test_uppercase_request_email_matches_lowercase_identity(self):
        self.assertEqual(self.index.resolve("K.KOWALSKI36@GMAIL.COM").user_id, "user_253")

    def test_all_518_request_emails_resolve_418_current_and_100_historical(self):
        current_emails = {norm_email(u.email) for u in self.identities}
        resolved = [self.index.resolve(r.email) for r in self.requests]
        self.assertNotIn(None, resolved)
        current = sum(norm_email(r.email) in current_emails for r in self.requests)
        self.assertEqual((current, len(self.requests) - current), (418, 100))

    def test_emails_phones_and_hashes_each_map_to_exactly_one_user(self):
        for u in self.identities:
            profile = self.index.profile(u.user_id)
            for token in profile.strong_identifiers():
                with self.subTest(user_id=u.user_id, token=token):
                    self.assertEqual(self.index.owner_of(token), {u.user_id})

    def test_exactly_three_names_are_shared_each_by_two_users(self):
        shared = {}
        for u in self.identities:
            owners = self.index.owner_of(name_token(u.name))
            if len(owners) > 1:
                shared[u.name] = owners
        self.assertEqual(set(shared), {"Gabriel Costa", "Mateo Ríos", "Thandiwe Ndlovu"})
        self.assertTrue(all(len(o) == 2 for o in shared.values()))

    def test_mateo_rios_does_not_match_mateo_rios_gil(self):
        rios = self.index.owner_of(name_token("Mateo Ríos"))
        self.assertEqual(len(rios), 2)
        self.assertTrue(rios.isdisjoint(self.index.owner_of(name_token("Mateo Rios Gil"))))


if __name__ == "__main__":
    unittest.main()
