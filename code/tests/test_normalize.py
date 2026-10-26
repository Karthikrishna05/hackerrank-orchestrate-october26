"""Tests for ghostaudit.normalize (tools_spec T3: identifier normalisation and extraction)."""

from __future__ import annotations

import hashlib
import unicodedata
import unittest
from pathlib import Path

from ghostaudit.models import NAME_PREFIX
from ghostaudit.normalize import (
    SYSTEM_BY_PREFIX,
    email_hash,
    extract_identifiers,
    name_token,
    norm_email,
    norm_hash,
    norm_name,
    norm_phone,
    split_resource_id,
    system_for,
)

DATASET = Path(__file__).resolve().parents[2] / "dataset"

HASH_A = "d4e1f59abcf9a1e0cfdc7c27ad6c07ed4ff88b326a16ed5eed5ec6bc31618c93"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TestNormEmail(unittest.TestCase):
    def test_norm_email_strips_and_lowercases(self):
        self.assertEqual(norm_email("  K.KOWALSKI36@GMAIL.COM \t"), "k.kowalski36@gmail.com")

    def test_norm_email_is_idempotent(self):
        once = norm_email(" Mixed.Case@Example.ORG ")
        self.assertEqual(norm_email(once), once)

    def test_norm_email_keeps_plus_dots_and_hyphens(self):
        self.assertEqual(norm_email("Jules-Laurent+tag@Proton.Me"), "jules-laurent+tag@proton.me")


class TestNormPhone(unittest.TestCase):
    def test_norm_phone_keeps_leading_plus_and_digits_only(self):
        self.assertEqual(norm_phone("+1 (212) 555-0171"), "+12125550171")

    def test_norm_phone_handles_spaces_and_hyphens(self):
        self.assertEqual(norm_phone("+55 11 97284-3165"), "+5511972843165")
        self.assertEqual(norm_phone("+46 70 482 91 37"), "+46704829137")

    def test_norm_phone_strips_surrounding_whitespace_before_checking_plus(self):
        self.assertEqual(norm_phone("  +34 612 847 593 "), "+34612847593")

    def test_norm_phone_without_plus_gives_digits_only(self):
        self.assertEqual(norm_phone("0034 612-847-593"), "0034612847593")

    def test_norm_phone_of_empty_string_is_empty(self):
        self.assertEqual(norm_phone(""), "")

    def test_differently_formatted_numbers_normalise_to_the_same_token(self):
        self.assertEqual(norm_phone("+33 6 24 34 80 16"), norm_phone("+33-6-24-34-80-16"))


class TestNormName(unittest.TestCase):
    def test_norm_name_casefolds_and_collapses_whitespace(self):
        self.assertEqual(norm_name("  Jonas   BERG\t"), "jonas berg")

    def test_norm_name_keeps_accents_so_rios_and_ríos_differ(self):
        self.assertEqual(norm_name("Mateo Ríos"), "mateo ríos")
        self.assertNotEqual(norm_name("Mateo Ríos"), norm_name("Mateo Rios"))

    def test_norm_name_treats_composed_and_decomposed_accents_alike(self):
        decomposed = unicodedata.normalize("NFD", "Mateo Ríos")
        self.assertNotEqual(decomposed, "Mateo Ríos")
        self.assertEqual(norm_name(decomposed), norm_name("Mateo Ríos"))

    def test_norm_name_does_not_merge_decoy_names(self):
        self.assertNotEqual(norm_name("Jonas Berg"), norm_name("Jonas Berglund"))
        self.assertNotEqual(norm_name("Jonas Berg"), norm_name("Joana Berg"))

    def test_name_token_prefixes_the_normalised_name(self):
        self.assertEqual(name_token(" Mateo  RÍOS "), NAME_PREFIX + "mateo ríos")


class TestHashes(unittest.TestCase):
    def test_email_hash_is_sha256_of_the_normalised_email(self):
        self.assertEqual(email_hash("amina.elmasri@gmail.com"), sha256("amina.elmasri@gmail.com"))

    def test_email_hash_ignores_case_and_surrounding_whitespace(self):
        self.assertEqual(email_hash("  AMINA.ElMasri@Gmail.com "), email_hash("amina.elmasri@gmail.com"))

    def test_email_hash_matches_a_known_vector(self):
        self.assertEqual(email_hash("amina.elmasri@gmail.com"), HASH_A)

    def test_email_hash_differs_for_decoy_emails(self):
        self.assertNotEqual(email_hash("smartinez6461.jr@gmail.com"), email_hash("smartinez6461@gmail.com"))

    def test_norm_hash_lowercases_and_drops_sha256_prefix(self):
        self.assertEqual(norm_hash(" SHA256:" + HASH_A.upper() + " "), HASH_A)
        self.assertEqual(norm_hash(HASH_A), HASH_A)


class TestExtractIdentifiers(unittest.TestCase):
    def test_extracts_and_normalises_an_email(self):
        self.assertEqual(extract_identifiers("lookup email=K.KOWALSKI36@GMAIL.COM rows=1"),
                         {"k.kowalski36@gmail.com"})

    def test_email_followed_by_sentence_period_is_extracted_without_it(self):
        self.assertEqual(extract_identifiers("Contact mateo.rios@outlook.com."), {"mateo.rios@outlook.com"})

    def test_email_inside_json_and_angle_brackets(self):
        self.assertEqual(extract_identifiers('{"email":"a.b@gmail.com"} <C.D@ZOHO.COM>'),
                         {"a.b@gmail.com", "c.d@zoho.com"})

    def test_decoy_email_with_suffix_does_not_yield_the_shorter_email(self):
        got = extract_identifiers("subject smartinez6461.jr@gmail.com requested export")
        self.assertEqual(got, {"smartinez6461.jr@gmail.com"})
        self.assertNotIn("smartinez6461@gmail.com", got)

    def test_never_returns_names(self):
        self.assertEqual(extract_identifiers("Customer Jonas Berg (aka Jonas Berglund) and Mateo Ríos"), set())

    def test_names_next_to_identifiers_are_not_returned(self):
        got = extract_identifiers("name=Jonas Berg email=jonas.berg@outlook.com")
        self.assertEqual(got, {"jonas.berg@outlook.com"})
        self.assertFalse(any(t.startswith(NAME_PREFIX) or t == "berg" for t in got))

    def test_extracts_bare_64_hex_hash_lowercased(self):
        self.assertEqual(extract_identifiers("hash " + HASH_A.upper()), {HASH_A})

    def test_extracts_hash_with_sha256_prefix_without_the_prefix(self):
        self.assertEqual(extract_identifiers("email_hash=sha256:" + HASH_A), {HASH_A})

    def test_hex_runs_of_wrong_length_are_not_hashes(self):
        self.assertEqual(extract_identifiers("x " + HASH_A[:63] + " y"), set())
        self.assertEqual(extract_identifiers("x " + HASH_A + "0 y"), set())

    def test_hash_embedded_in_a_longer_token_is_not_extracted(self):
        self.assertEqual(extract_identifiers("ref_" + HASH_A + "z"), set())

    def test_extracts_user_ids_case_insensitively(self):
        self.assertEqual(extract_identifiers("subject_ref=user_12, owner USER_7"), {"user_12", "user_7"})

    def test_user_id_keeps_its_zero_padding(self):
        self.assertEqual(extract_identifiers("user_01 user_012"), {"user_01", "user_012"})

    def test_user_id_inside_a_longer_word_is_not_extracted(self):
        self.assertEqual(extract_identifiers("superuser_12 user_12x"), set())

    def test_user_id_in_an_email_local_part_is_not_extracted_separately(self):
        self.assertEqual(extract_identifiers("from user_12@example.com"), {"user_12@example.com"})

    def test_extracts_plus_prefixed_phone_with_spaces(self):
        self.assertEqual(extract_identifiers("phone +44 20 7946 0958 updated"), {"+442079460958"})

    def test_extracts_phone_with_parentheses_and_hyphen(self):
        self.assertEqual(extract_identifiers("phone=+1 (212) 555-0171;"), {"+12125550171"})

    def test_extracts_phone_with_hyphen_groups(self):
        self.assertEqual(extract_identifiers("tel:+55 11 97284-3165"), {"+5511972843165"})

    def test_phone_without_plus_is_not_extracted(self):
        self.assertEqual(extract_identifiers("phone 020 7946 0958"), set())

    def test_too_short_plus_number_is_not_a_phone(self):
        self.assertEqual(extract_identifiers("delta +12345 units"), set())

    def test_timestamp_offset_is_not_mistaken_for_a_phone(self):
        self.assertEqual(extract_identifiers("ts=2026-09-15T11:59:39+00:00 level=info"), set())
        self.assertEqual(extract_identifiers("2026-09-15 11:59:39 +0000 svc"), set())

    def test_extracts_every_kind_from_one_log_line(self):
        line = (f'2026-09-15T11:59:39Z svc=search event=hit subject=user_32 email="Novak.Tomasz@iCloud.com" '
                f'phone="+48 601 623 422" hash=sha256:{HASH_A}')
        self.assertEqual(extract_identifiers(line),
                         {"user_32", "novak.tomasz@icloud.com", "+48601623422", HASH_A})

    def test_empty_text_gives_empty_set(self):
        self.assertEqual(extract_identifiers(""), set())

    def test_returns_a_set(self):
        self.assertIsInstance(extract_identifiers("user_1"), set)


class TestSystemTable(unittest.TestCase):
    def test_system_by_prefix_matches_the_spec_table_exactly(self):
        self.assertEqual(SYSTEM_BY_PREFIX, {
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
        })


class TestSplitResourceId(unittest.TestCase):
    def test_split_multi_underscore_prefix(self):
        self.assertEqual(split_resource_id("data_flow_9522"), ("data_flow", 9522))
        self.assertEqual(split_resource_id("audit_record_4902"), ("audit_record", 4902))
        self.assertEqual(split_resource_id("email_delivery_12"), ("email_delivery", 12))

    def test_split_single_word_prefix(self):
        self.assertEqual(split_resource_id("search_052"), ("search", 52))

    def test_number_is_returned_as_int(self):
        prefix, number = split_resource_id("account_005")
        self.assertEqual(prefix, "account")
        self.assertIsInstance(number, int)
        self.assertEqual(number, 5)

    def test_surrounding_whitespace_is_ignored(self):
        self.assertEqual(split_resource_id("  backup_3120 "), ("backup", 3120))

    def test_raises_value_error_without_a_numeric_suffix(self):
        for bad in ("", "account", "account_", "_5", "5", "data_flow_95x", "data_flow_9522_extra"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                split_resource_id(bad)


class TestSystemFor(unittest.TestCase):
    def test_system_for_maps_every_prefix(self):
        for prefix, system in SYSTEM_BY_PREFIX.items():
            with self.subTest(prefix=prefix):
                self.assertEqual(system_for(f"{prefix}_123"), system)

    def test_system_for_unknown_prefix_is_none(self):
        self.assertIsNone(system_for("object_77"))

    def test_system_for_malformed_id_is_none(self):
        self.assertIsNone(system_for("not a resource"))


@unittest.skipUnless(DATASET.is_dir(), "dataset not available")
class TestNormalizeOnRealDataset(unittest.TestCase):
    def setUp(self):
        from ghostaudit import loaders
        self.identities = loaders.load_identities()
        self.audit = loaders.load_audit_log()

    def test_email_hash_matches_the_dataset_for_all_850_users(self):
        matches = sum(email_hash(u.email) == u.email_hash for u in self.identities)
        self.assertEqual((matches, len(self.identities)), (850, 850))

    def test_audit_log_email_hash_changes_match_the_email_changes(self):
        emails = {(c.user_id, c.event_time): c for c in self.audit if c.field_name == "email"}
        hashes = [c for c in self.audit if c.field_name == "email_hash"]
        self.assertTrue(hashes)
        for h in hashes:
            e = emails.get((h.user_id, h.event_time))
            if e is None:
                continue
            with self.subTest(audit_id=h.audit_id):
                self.assertEqual(email_hash(e.old_value), norm_hash(h.old_value))
                self.assertEqual(email_hash(e.new_value), norm_hash(h.new_value))

    def test_extract_identifiers_recovers_every_identity_email_and_phone(self):
        for u in self.identities:
            with self.subTest(user_id=u.user_id):
                self.assertEqual(extract_identifiers(u.email), {norm_email(u.email)})
                self.assertEqual(extract_identifiers(u.phone), {norm_phone(u.phone)})


if __name__ == "__main__":
    unittest.main()
