"""Tests for ghostaudit.parsers.pdf_text (tools_spec.md T6, PDF row)."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import unittest
import warnings
from pathlib import Path
from unittest import mock

from ghostaudit.models import ManifestEntry
from ghostaudit.parsers import pdf_text
from ghostaudit.parsers.pdf_text import document_category, extract_facts, extract_lines
from ghostaudit.paths import dataset_dir

DOCS = dataset_dir() / "evidence" / "documents"
PDF_NAMES = ("account_history_report_20260718.pdf", "delivery_correction_attachment.pdf",
             "legal_retention_register_20260529.pdf", "security_retention_extract.pdf")
HAVE_PDFS = all((DOCS / name).is_file() for name in PDF_NAMES)
HAVE_PDFTOTEXT = shutil.which("pdftotext") is not None
HAVE_BS4 = importlib.util.find_spec("bs4") is not None
HAVE_PYPDF = importlib.util.find_spec("pypdf") is not None


def pdf(name: str) -> Path:
    return DOCS / name


def entry_for(name: str, file_id: str, captured_at: str, origin: str = "test_fixture") -> ManifestEntry:
    return ManifestEntry(file_id=file_id, file_path=str(pdf(name)), origin=origin,
                         captured_at=captured_at)


def parse_pdf(entry: ManifestEntry):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        obs, cov = pdf_text.parse(entry, Path(entry.file_path))
    return obs, cov, [str(w.message) for w in caught]


@unittest.skipUnless(HAVE_PDFS, "dataset PDFs not present")
@unittest.skipUnless(HAVE_PDFTOTEXT, "pdftotext binary not installed")
class PdftotextBackendTests(unittest.TestCase):
    """Facts from the default backend on each real document."""

    @classmethod
    def setUpClass(cls):
        cls.lines = {name: extract_lines(pdf(name), "pdftotext") for name in PDF_NAMES}
        cls.facts = {name: extract_facts(lines) for name, lines in cls.lines.items()}

    def test_lines_are_non_empty_and_right_stripped(self):
        for name, lines in self.lines.items():
            with self.subTest(pdf=name):
                self.assertTrue(lines)
                self.assertTrue(all(ln.strip() and ln == ln.rstrip() for ln in lines))

    def test_legal_register_lists_six_orders(self):
        self.assertEqual(self.facts["legal_retention_register_20260529.pdf"]["orders"],
                         ["ORD-20264118", "ORD-20264207", "ORD-20264319", "ORD-20264402",
                          "ORD-20264511", "ORD-20264600"])

    def test_legal_register_lists_six_users_and_no_resource_ids(self):
        facts = self.facts["legal_retention_register_20260529.pdf"]
        self.assertEqual(facts["user_ids"],
                         ["user_08", "user_12", "user_17", "user_21", "user_24", "user_29"])
        self.assertEqual(facts["resource_ids"], [])

    def test_security_extract_names_audit_record_003_for_user_03(self):
        facts = self.facts["security_retention_extract.pdf"]
        self.assertEqual(facts["resource_ids"], ["audit_record_003"])
        self.assertEqual(facts["user_ids"], ["user_03"])

    def test_security_extract_dates_include_closure_time_despite_layout_gaps(self):
        self.assertIn("2026-08-25T04:03:00Z", self.facts["security_retention_extract.pdf"]["dates"])

    def test_attachment_names_ticket_contact_and_order(self):
        facts = self.facts["delivery_correction_attachment.pdf"]
        self.assertEqual(facts["resource_ids"], ["support_001"])
        self.assertEqual(facts["emails"], ["priya.raman@gmail.com"])
        self.assertEqual(facts["phones"], ["+919876573142"])
        self.assertEqual(facts["orders"], ["ORD-20264827"])
        self.assertIn("CS-4827", facts["references"])

    def test_statement_names_user_email_and_order(self):
        facts = self.facts["account_history_report_20260718.pdf"]
        self.assertEqual(facts["user_ids"], ["user_18"])
        self.assertEqual(facts["emails"], ["ingrid.dahl@outlook.com"])
        self.assertEqual(facts["orders"], ["ORD-20261260"])

    def test_document_categories(self):
        expected = {"account_history_report_20260718.pdf": "USER_EXPORT",
                    "delivery_correction_attachment.pdf": "SUPPORT_ATTACHMENT",
                    "legal_retention_register_20260529.pdf": "LEGAL_HOLD_RECORD",
                    "security_retention_extract.pdf": "SECURITY_AUDIT_LOG"}
        for name, category in expected.items():
            with self.subTest(pdf=name):
                self.assertEqual(document_category(self.lines[name]), category)


@unittest.skipUnless(HAVE_PDFS, "dataset PDFs not present")
class BackendAgreementTests(unittest.TestCase):
    """Spec T6: every backend must yield identical facts on all 4 PDFs."""

    def assertSameAsPdftotext(self, backend: str):
        for name in PDF_NAMES:
            with self.subTest(pdf=name, backend=backend):
                reference = extract_lines(pdf(name), "pdftotext")
                lines = extract_lines(pdf(name), backend)
                self.assertEqual(extract_facts(lines), extract_facts(reference))
                self.assertEqual(document_category(lines), document_category(reference))

    @unittest.skipUnless(HAVE_PDFTOTEXT and HAVE_BS4, "needs pdftotext and beautifulsoup4")
    def test_bs4_backend_matches_pdftotext(self):
        self.assertSameAsPdftotext("bs4")

    @unittest.skipUnless(HAVE_PDFTOTEXT and HAVE_PYPDF, "needs pdftotext and pypdf")
    def test_pypdf_backend_matches_pdftotext(self):
        self.assertSameAsPdftotext("pypdf")


@unittest.skipUnless(HAVE_PDFS, "dataset PDFs not present")
@unittest.skipUnless(HAVE_PDFTOTEXT or HAVE_PYPDF, "no PDF backend installed")
class PdfParseTests(unittest.TestCase):
    """`parse` turns facts into FOUND observations."""

    def test_security_extract_gives_one_found_audit_observation(self):
        obs, cov, warns = parse_pdf(entry_for("security_retention_extract.pdf", "evidence_t5",
                                              "2026-09-05T06:30:00Z", origin="security_service"))
        self.assertEqual((cov, warns), ([], []))
        self.assertEqual(len(obs), 1)
        o = obs[0]
        self.assertEqual((o.resource_id, o.state, o.category, o.system_id),
                         ("audit_record_003", "FOUND", "SECURITY_AUDIT_LOG", "security_service"))
        self.assertEqual(o.identifiers, frozenset({"user_03"}))
        self.assertEqual((o.file_id, o.captured_at), ("evidence_t5", "2026-09-05T06:30:00Z"))

    def test_document_time_label_gives_observed_at(self):
        obs, _, _ = parse_pdf(entry_for("security_retention_extract.pdf", "evidence_t5",
                                        "2026-09-06T00:00:00Z"))
        self.assertEqual(obs[0].observed_at, "2026-09-05T06:30:00Z")
        self.assertEqual(obs[0].captured_at, "2026-09-06T00:00:00Z")

    def test_document_without_resource_ids_gives_one_document_level_observation(self):
        obs, _, _ = parse_pdf(entry_for("legal_retention_register_20260529.pdf", "evidence_t6",
                                        "2026-05-29T08:00:00Z"))
        self.assertEqual([o.resource_id for o in obs], [""])
        self.assertEqual((obs[0].category, obs[0].system_id),
                         ("LEGAL_HOLD_RECORD", "legal_service"))
        self.assertEqual(obs[0].identifiers, frozenset(
            {"user_08", "user_12", "user_17", "user_21", "user_24", "user_29"}))

    def test_fields_keep_the_full_text_and_facts(self):
        obs, _, _ = parse_pdf(entry_for("delivery_correction_attachment.pdf", "evidence_t7",
                                        "2026-09-12T07:30:00Z"))
        fields = obs[0].fields
        self.assertIn("ORD-20264827", fields["text"])
        self.assertEqual(fields["facts"]["resource_ids"], ["support_001"])
        self.assertIn(fields["backend"], ("pdftotext", "pypdf"))
        self.assertEqual(obs[0].source, f"pdf:{fields['backend']}")

    @unittest.skipUnless(HAVE_PYPDF, "pypdf not installed")
    def test_parse_falls_back_to_pypdf_without_pdftotext(self):
        entry = entry_for("security_retention_extract.pdf", "evidence_t5", "2026-09-05T06:30:00Z")
        with mock.patch.object(pdf_text.shutil, "which", return_value=None):
            obs, _, _ = parse_pdf(entry)
        self.assertEqual(obs[0].source, "pdf:pypdf")
        self.assertEqual(obs[0].resource_id, "audit_record_003")


class MissingBackendTests(unittest.TestCase):
    """A missing binary or library gives a clear RuntimeError (spec T6)."""

    FAKE = Path("does_not_matter.pdf")

    def test_unknown_backend_name_raises_value_error(self):
        with self.assertRaisesRegex(ValueError, "unknown PDF backend"):
            extract_lines(self.FAKE, "tesseract")

    def test_missing_pdftotext_binary_raises_runtime_error(self):
        with (mock.patch.object(pdf_text.shutil, "which", return_value=None),
              self.assertRaisesRegex(RuntimeError, "pdftotext")):
            extract_lines(self.FAKE, "pdftotext")

    @unittest.skipUnless(HAVE_BS4, "beautifulsoup4 not installed")
    def test_bs4_backend_without_pdftotext_binary_raises_runtime_error(self):
        with (mock.patch.object(pdf_text.shutil, "which", return_value=None),
              self.assertRaisesRegex(RuntimeError, "pdftotext")):
            extract_lines(self.FAKE, "bs4")

    def test_missing_bs4_library_raises_runtime_error(self):
        with mock.patch.dict(sys.modules, {"bs4": None}), self.assertRaisesRegex(RuntimeError, "beautifulsoup4"):
            extract_lines(self.FAKE, "bs4")

    def test_missing_pypdf_library_raises_runtime_error(self):
        with mock.patch.dict(sys.modules, {"pypdf": None}), self.assertRaisesRegex(RuntimeError, "pypdf"):
            extract_lines(self.FAKE, "pypdf")

    def test_failing_pdftotext_run_raises_runtime_error(self):
        failed = subprocess.CompletedProcess(["pdftotext"], 1, stdout="", stderr="Syntax Error")
        with (mock.patch.object(pdf_text.shutil, "which", return_value="/usr/bin/pdftotext"),
              mock.patch.object(pdf_text.subprocess, "run", return_value=failed),
              self.assertRaisesRegex(RuntimeError, "Syntax Error")):
            extract_lines(self.FAKE, "pdftotext")

    def test_parse_without_any_backend_raises_runtime_error_naming_the_file(self):
        entry = ManifestEntry("evidence_t8", str(self.FAKE), "test_fixture", "2026-09-05T06:30:00Z")
        with (mock.patch.object(pdf_text.shutil, "which", return_value=None),
              mock.patch.dict(sys.modules, {"pypdf": None}),
              self.assertRaisesRegex(RuntimeError, "evidence_t8")):
            pdf_text.parse(entry, self.FAKE)


class ExtractFactsTests(unittest.TestCase):
    """Shared fact extraction on synthetic lines (no PDF needed)."""

    LINES = [
        "SUPPORT ATTACHMENT - DELIVERY ADDRESS CORRECTION",
        "Ticket CS-4827   resource support_001   linked flow data_flow_9522",
        "Record audit_record_003 and hold legal_hold_8017; not a resource: xsupport_12",
        "Customer: Rua Tevita <R.TEVITA@EXAMPLE.COM>  phone +91 98765 00042",
        "Account user_07 order ORD-20264827 statement NST-2026-0718-1842",
        "Hash sha256:9D58794F0080FD7D4891524495324132C9DC4F22BCD642F9FE7916A4ECC50FE9",
        "Captured 12 Sep 2026 07 30 UTC   retain until 28 May 2033",
        "Ticket CS-4827 repeated",
    ]

    @classmethod
    def setUpClass(cls):
        cls.facts = extract_facts(cls.LINES)

    def test_resource_ids_match_whole_prefixed_tokens_only(self):
        self.assertEqual(self.facts["resource_ids"],
                         ["audit_record_003", "data_flow_9522", "legal_hold_8017", "support_001"])

    def test_orders_are_separate_from_other_references(self):
        self.assertEqual(self.facts["orders"], ["ORD-20264827"])
        self.assertEqual(self.facts["references"], ["CS-4827", "NST-2026-0718-1842"])

    def test_emails_are_lowercased(self):
        self.assertEqual(self.facts["emails"], ["r.tevita@example.com"])

    def test_phones_are_normalised(self):
        self.assertEqual(self.facts["phones"], ["+919876500042"])

    def test_user_ids(self):
        self.assertEqual(self.facts["user_ids"], ["user_07"])

    def test_prefixed_hash_is_lowercased_without_prefix(self):
        self.assertEqual(self.facts["hashes"],
                         ["9d58794f0080fd7d4891524495324132c9dc4f22bcd642f9fe7916a4ecc50fe9"])

    def test_human_dates_become_iso_utc(self):
        self.assertEqual(self.facts["dates"], ["2026-09-12T07:30:00Z", "2033-05-28T00:00:00Z"])

    def test_layout_whitespace_does_not_change_dates(self):
        self.assertEqual(extract_facts(["issued 18 Jun 2026 10     24 UTC"])["dates"],
                         ["2026-06-18T10:24:00Z"])

    def test_no_lines_give_empty_facts(self):
        self.assertEqual(extract_facts([]), {key: [] for key in (
            "resource_ids", "user_ids", "emails", "phones", "hashes", "orders", "references",
            "dates")})


class DocumentCategoryTests(unittest.TestCase):

    def test_attachment(self):
        self.assertEqual(document_category(["Support ticket attachment", "Ticket CS-1"]),
                         "SUPPORT_ATTACHMENT")

    def test_legal_register(self):
        self.assertEqual(document_category(["Disputed orders and tax records register"]),
                         "LEGAL_HOLD_RECORD")

    def test_compliance_journal(self):
        self.assertEqual(document_category(["Compliance journal - signed record extract"]),
                         "SECURITY_AUDIT_LOG")

    def test_customer_statement(self):
        self.assertEqual(document_category(["Northstar Shop - Customer statement"]), "USER_EXPORT")

    def test_unrelated_document_has_no_category(self):
        self.assertIsNone(document_category(["Quarterly marketing newsletter"]))


if __name__ == "__main__":
    unittest.main()
