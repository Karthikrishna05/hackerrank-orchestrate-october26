"""Independent tests for ghostaudit.intent_rules (keyword intent baseline, engine_plan.md §3 A1).

The texts below are written for these tests; none is copied from the dataset. The real-data class at
the end checks the participant request files only for the counts the plan states (§1, E13).
"""

from __future__ import annotations

import csv
import re
import unittest

from ghostaudit import intent_rules, paths
from ghostaudit.intent_rules import classify, detect_language
from ghostaudit.models import IntentResult

# models.IntentResult documents these request types.
REQUEST_TYPES = {"deletion", "deletion_verification", "access", "export", "correction",
                 "marketing_opt_out", "account_help", "other"}
DELETION_TYPES = {"deletion", "deletion_verification"}

DELETION_BY_LANGUAGE = {
    "en": "Please delete my account and every piece of personal data you keep about me.",
    "pt": "Quero excluir a minha conta e todos os meus dados pessoais, obrigado.",
    "es": "Por favor, eliminen mi cuenta y todos mis datos personales.",
    "fr": "Veuillez supprimer mon compte et toutes mes données personnelles.",
    "ja": "私のアカウントと個人データをすべて削除してください。",
    "ko": "제 계정과 개인정보를 모두 삭제해 주세요.",
}

OTHER_DELETION_WORDING = (
    "Please erase everything you store about me.",
    "I want my profile closed and my data wiped.",
    "Please forget me.",
    "I no longer use this account, please delete it.",
    "Please delete my account and do not keep any of my data.",
    "Please delete my profile. Do not send me any more emails.",
    "I want to delete my account. Also unsubscribe me from marketing.",
)

# A request to verify or audit a deletion counts as deletion (plan §3 A1; the samples prove it).
VERIFICATION_REQUESTS = (
    "Can you verify that my account was deleted last month?",
    "Please audit the deletion of my data that you reported as finished.",
    "Could you confirm that my profile is gone?",
    "Tell me whether my records were erased.",
    "Please check that my old account no longer exists.",
)

# (text, expected request_type or None when only "non-deletion" is settled)
NON_DELETION = (
    ("Please stop sending me marketing emails.", "marketing_opt_out"),
    ("Please remove me from your newsletter list.", "marketing_opt_out"),
    ("Unsubscribe me from promotional messages.", "marketing_opt_out"),
    ("Can you tell me what personal data you hold about me?", "access"),
    ("Please send me a copy of my personal data.", "access"),
    ("I would like to download my data in a portable format.", "export"),
    ("My last name is misspelled on the profile, please correct it.", "correction"),
    ("I forgot my password and cannot access my account.", "account_help"),
    ("Please send me the invoice for my last order.", None),
    ("Please change the email address on my account to my new one.", None),
)

# Negation wins (plan §3 A1 definition; intent_rules rule 1).
NEGATIONS = (
    "I am not asking to delete my account; just stop the newsletters.",
    "Please do not close the account, I only need a password reset.",
    "This is not a deletion request. I want a copy of my data.",
    "I still need the account, so do not delete anything.",
    "Please keep my account active and fix my phone number.",
    "Can you tell me what you hold about me? This is not a request to erase it.",
    "Não quero excluir minha conta, só corrigir o meu nome.",
    "No quiero eliminar mi cuenta; solo cambien mi correo.",
    "Je ne veux pas supprimer mon compte, merci de corriger mon adresse.",
    "退会はしないでください。パスワードをリセットしたいです。",
    "계정은 탈퇴하지 마세요. 비밀번호만 바꾸고 싶어요.",
)


class DeletionWordingTest(unittest.TestCase):

    def test_deletion_in_all_six_languages(self):
        for lang, text in DELETION_BY_LANGUAGE.items():
            with self.subTest(lang=lang):
                r = classify(text)
                self.assertTrue(r.authorizes_deletion, r)
                self.assertEqual(r.request_type, "deletion")
                self.assertEqual(r.language, lang)

    def test_other_deletion_wording(self):
        for text in OTHER_DELETION_WORDING:
            with self.subTest(text=text):
                r = classify(text)
                self.assertTrue(r.authorizes_deletion, r)
                self.assertIn(r.request_type, DELETION_TYPES)

    def test_verify_or_audit_my_deletion_counts_as_deletion(self):
        for text in VERIFICATION_REQUESTS:
            with self.subTest(text=text):
                r = classify(text)
                self.assertTrue(r.authorizes_deletion, r)
                self.assertIn(r.request_type, DELETION_TYPES)

    def test_scope_limit_is_not_a_refusal(self):
        # The person still asks for the account to be deleted; keeping order history only limits scope.
        r = classify("Please delete my account, but do not remove my order history.")
        self.assertTrue(r.authorizes_deletion, r)

    def test_quote_is_taken_from_the_text(self):
        for text in (*DELETION_BY_LANGUAGE.values(), *VERIFICATION_REQUESTS):
            with self.subTest(text=text):
                r = classify(text)
                self.assertTrue(r.quote)
                self.assertIn(r.quote, text)


class NonDeletionTest(unittest.TestCase):

    def test_non_deletion_request_types(self):
        for text, expected in NON_DELETION:
            with self.subTest(text=text):
                r = classify(text)
                self.assertFalse(r.authorizes_deletion, r)
                self.assertIn(r.request_type, REQUEST_TYPES - DELETION_TYPES)
                if expected is not None:
                    self.assertEqual(r.request_type, expected)

    def test_negation_wins_over_deletion_words(self):
        for text in NEGATIONS:
            with self.subTest(text=text):
                r = classify(text)
                self.assertFalse(r.authorizes_deletion, r)
                self.assertNotIn(r.request_type, DELETION_TYPES)
                self.assertTrue(r.quote, "a refusal verdict quotes the refusal")
                self.assertIn(r.quote, intent_rules._norm(text))

    def test_empty_or_unrelated_text_does_not_authorize_deletion(self):
        for text in ("", "   ", "Hello there."):
            with self.subTest(text=text):
                r = classify(text)
                self.assertFalse(r.authorizes_deletion)
                self.assertEqual(r.request_type, "other")

    # Regression: not wanting to keep using the service is no refusal of deletion (owner decision 20).
    def test_not_wanting_to_keep_using_the_service_is_not_a_refusal(self):
        for text in ("Please delete my account. I do not want to keep using the service.",
                     "I do not want to continue using this account, please close it."):
            with self.subTest(text=text):
                self.assertTrue(classify(text).authorizes_deletion, text)

    # Regression: a negated wish to keep the account (es, fr) is no refusal of deletion (owner decision 20).
    def test_negated_wish_to_keep_the_account_is_not_a_refusal(self):
        for text in ("No quiero que mi cuenta siga activa; por favor eliminen mi cuenta.",
                     "Je ne souhaite pas garder mon compte : merci de le supprimer."):
            with self.subTest(text=text):
                self.assertTrue(classify(text).authorizes_deletion, text)

    # Regression: an account closure asked next to a marketing stop is deletion (owner decision 20).
    def test_account_closure_next_to_marketing_words_is_deletion(self):
        self.assertTrue(classify("Please close my account and stop the marketing emails.").authorizes_deletion)


class ResultShapeTest(unittest.TestCase):

    def test_source_is_rules_and_shape_is_valid(self):
        texts = [*DELETION_BY_LANGUAGE.values(), *VERIFICATION_REQUESTS,
                 *(t for t, _ in NON_DELETION), *NEGATIONS, ""]
        for text in texts:
            with self.subTest(text=text):
                r = classify(text)
                self.assertIsInstance(r, IntentResult)
                self.assertEqual(r.source, "rules")
                self.assertEqual(r.source, intent_rules.SOURCE)
                self.assertIn(r.request_type, REQUEST_TYPES)
                self.assertEqual(r.authorizes_deletion, r.request_type in DELETION_TYPES)
                self.assertGreaterEqual(r.confidence, 0.0)
                self.assertLessEqual(r.confidence, 1.0)

    def test_deterministic(self):
        for text in (*DELETION_BY_LANGUAGE.values(), *NEGATIONS):
            with self.subTest(text=text):
                self.assertEqual(classify(text), classify(text))

    def test_case_and_curly_apostrophe_do_not_change_the_verdict(self):
        self.assertEqual(classify("PLEASE DELETE MY ACCOUNT.").authorizes_deletion, True)
        self.assertFalse(classify("Please don’t delete my account, just fix my name.").authorizes_deletion)
        self.assertFalse(classify("Please don't delete my account, just fix my name.").authorizes_deletion)


class DetectLanguageTest(unittest.TestCase):

    def test_scripts_and_latin_cues(self):
        cases = {
            "제 계정을 삭제해 주세요.": "ko",
            "アカウントを削除してください。": "ja",
            "Quero excluir a minha conta, obrigado.": "pt",
            "Quiero eliminar mi cuenta y mi correo.": "es",
            "Je veux supprimer mon compte, merci.": "fr",
            "Please delete my account.": "en",
        }
        for text, lang in cases.items():
            with self.subTest(text=text):
                self.assertEqual(detect_language(text), lang)

    def test_english_is_the_default(self):
        for text in ("", "Hello there.", "12345"):
            with self.subTest(text=text):
                self.assertEqual(detect_language(text), "en")

    def test_classify_reports_the_detected_language(self):
        for lang, text in DELETION_BY_LANGUAGE.items():
            with self.subTest(lang=lang):
                self.assertEqual(classify(text).language, detect_language(text))


_DATA = paths.dataset_dir()
DATASET_OK = (_DATA / "deletion_requests.csv").is_file() and (_DATA / "sample_deletion_requests.csv").is_file()


def _texts(name: str) -> dict[str, str]:
    with open(_DATA / name, encoding="utf-8-sig", newline="") as fh:
        return {row["case_id"]: row["request_text"] for row in csv.DictReader(fh)}


def _num(case_id: str) -> int:
    return int(re.fullmatch(r"case_(\d+)", case_id).group(1))


@unittest.skipUnless(DATASET_OK, "participant dataset not present")
class RealDataTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.evaluation = _texts("deletion_requests.csv")
        cls.samples = _texts("sample_deletion_requests.csv")

    def test_exactly_ten_evaluation_texts_are_non_deletion(self):
        self.assertEqual(len(self.evaluation), 518)
        non_deletion = sorted((c for c, t in self.evaluation.items() if not classify(t).authorizes_deletion),
                              key=_num)
        self.assertEqual(non_deletion, [f"case_{n}" for n in range(533, 543)])

    def test_all_samples_are_deletion(self):
        self.assertEqual(len(self.samples), 32)
        refused = {c: classify(t) for c, t in self.samples.items() if not classify(t).authorizes_deletion}
        self.assertEqual(refused, {})

    def test_every_real_text_is_labelled_rules_with_a_known_language(self):
        for case_id, text in {**self.evaluation, **self.samples}.items():
            r = classify(text)
            self.assertEqual(r.source, "rules", case_id)
            self.assertIn(r.language, {"en", "pt", "es", "fr", "ja", "ko"}, case_id)


if __name__ == "__main__":
    unittest.main()
