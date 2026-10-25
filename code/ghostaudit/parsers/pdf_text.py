"""PDF business documents: text via pdftotext, pdftotext+BeautifulSoup or pypdf, then shared facts."""

from __future__ import annotations

import re
import shutil
import subprocess
import unicodedata
from pathlib import Path

from ..models import Coverage, ManifestEntry, Observation
from ..normalize import SYSTEM_BY_PREFIX, extract_identifiers
from ..timeutil import parse_human_date, to_iso
from . import make_observation

BACKENDS = ("pdftotext", "bs4", "pypdf")
_POPPLER_HINT = "install poppler-utils (apt install poppler-utils / brew install poppler)"
_LINE_TOLERANCE = 3.0  # points: words this close vertically share a visual line

_RESOURCE = re.compile(r"(?<![\w])(?:%s)_\d+(?![\w])" % "|".join(
    sorted(map(re.escape, SYSTEM_BY_PREFIX), key=len, reverse=True)))
_ORDER = re.compile(r"(?<![\w-])ORD-\d+(?![\w])")
_REFERENCE = re.compile(r"(?<![\w-])(?!ORD-)[A-Z]{2,4}(?:-\d+)+(?![\w])")
_DATE = re.compile(r"(?<!\d)\d{1,2} [A-Za-z]{3,9}\.? \d{4}(?: \d{1,2}(?: |:)\d{2}(?:(?: |:)\d{2})?(?!\d))?")
_TIME_LABEL = re.compile(r"\b(?:captured|generated|issued)\s+(.*)$", re.I)

# (keyword pattern on the upper-cased text, category); first match wins.
DOC_CATEGORY_RULES = (
    (re.compile(r"\bATTACHMENT\b"), "SUPPORT_ATTACHMENT"),
    (re.compile(r"\bRETENTION REGISTER\b|\bDISPUTED ORDERS\b|\bLEGAL HOLD\b"), "LEGAL_HOLD_RECORD"),
    (re.compile(r"\bCOMPLIANCE JOURNAL\b|\bAUDIT\b"), "SECURITY_AUDIT_LOG"),
    (re.compile(r"\bSTATEMENT\b|\bACCOUNT HISTORY\b"), "USER_EXPORT"),
)
CATEGORY_SYSTEM = {"SUPPORT_ATTACHMENT": "support_platform", "LEGAL_HOLD_RECORD": "legal_service",
                   "SECURITY_AUDIT_LOG": "security_service", "USER_EXPORT": "export_service"}


def _pdftotext(*args: str) -> str:
    if shutil.which("pdftotext") is None:
        raise RuntimeError(f"pdftotext binary not found: {_POPPLER_HINT}")
    proc = subprocess.run(["pdftotext", *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"pdftotext failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


def _lines_bs4(path: Path) -> list[str]:
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise RuntimeError("beautifulsoup4 not installed: pip install -r code/requirements.txt") from exc
    soup = BeautifulSoup(_pdftotext("-bbox-layout", str(path), "-"), "html.parser")
    lines: list[str] = []
    for page in soup.find_all("page"):
        words = sorted((float(w["ymin"]), float(w["xmin"]), w.get_text())
                       for line in page.find_all("line") for w in line.find_all("word"))
        row: list[tuple[float, str]] = []
        row_y = None
        for y, x, text in words:
            if row_y is not None and y - row_y > _LINE_TOLERANCE:
                lines.append(" ".join(t for _, t in sorted(row)))
                row = []
            if not row:
                row_y = y
            row.append((x, text))
        if row:
            lines.append(" ".join(t for _, t in sorted(row)))
    return lines


def _lines_pypdf(path: Path) -> list[str]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("pypdf not installed: pip install -r code/requirements.txt") from exc
    reader = PdfReader(str(path))
    return [ln for page in reader.pages for ln in page.extract_text(extraction_mode="layout").splitlines()]


def extract_lines(path: str | Path, backend: str = "pdftotext") -> list[str]:
    """Non-empty text lines of a PDF in reading order, from the chosen backend."""
    path = Path(path)
    if backend == "pdftotext":
        raw = _pdftotext("-layout", str(path), "-").splitlines()
    elif backend == "bs4":
        raw = _lines_bs4(path)
    elif backend == "pypdf":
        raw = _lines_pypdf(path)
    else:
        raise ValueError(f"unknown PDF backend {backend!r}; choose one of {BACKENDS}")
    return [ln.rstrip() for ln in raw if ln.strip()]


def _normalise(line: str) -> str:
    """Backend-neutral text: NFKC, form feeds removed, whitespace runs collapsed."""
    return " ".join(unicodedata.normalize("NFKC", line).split())


def _dates(line: str) -> list[str]:
    out = []
    for m in _DATE.finditer(line):
        dt = parse_human_date(m.group())
        if dt is not None:
            out.append(to_iso(dt))
    return out


def extract_facts(lines: list[str]) -> dict:
    """Sorted resource IDs, user ids, emails, phones, hashes, ORD refs, other refs and ISO dates."""
    text = [_normalise(ln) for ln in lines]
    ids = set().union(*(extract_identifiers(t) for t in text)) if text else set()
    return {
        "resource_ids": sorted({m for t in text for m in _RESOURCE.findall(t)}),
        "user_ids": sorted(i for i in ids if i.startswith("user_")),
        "emails": sorted(i for i in ids if "@" in i),
        "phones": sorted(i for i in ids if i.startswith("+")),
        "hashes": sorted(i for i in ids if len(i) == 64 and "@" not in i),
        "orders": sorted({m for t in text for m in _ORDER.findall(t)}),
        "references": sorted({m for t in text for m in _REFERENCE.findall(t)}),
        "dates": sorted({d for t in text for d in _dates(t)}),
    }


def document_category(lines: list[str]) -> str | None:
    text = _normalise(" ".join(lines)).upper()
    return next((cat for pattern, cat in DOC_CATEGORY_RULES if pattern.search(text)), None)


def _document_time(lines: list[str]) -> str | None:
    for ln in map(_normalise, lines):
        m = _TIME_LABEL.search(ln)
        if m and (dates := _dates(m.group(1))):
            return dates[0]
    return None


def parse(entry: ManifestEntry, path: Path) -> tuple[list[Observation], list[Coverage]]:
    """FOUND fact observations: one per resource ID, or one document-level (resource_id '')."""
    errors = []
    for backend in ("pdftotext", "pypdf"):
        try:
            lines = extract_lines(path, backend)
            break
        except RuntimeError as exc:
            errors.append(str(exc))
    else:
        raise RuntimeError(f"{entry.file_id}: no PDF backend available: {'; '.join(errors)}")
    facts = extract_facts(lines)
    category = document_category(lines)
    identifiers = set(facts["user_ids"]) | set(facts["emails"]) | set(facts["phones"]) | set(facts["hashes"])
    fields = {"backend": backend, "title": _normalise(lines[0]) if lines else "",
              "facts": facts, "text": "\n".join(lines)}
    observed_at = _document_time(lines)
    system = CATEGORY_SYSTEM.get(category) or entry.origin
    return [make_observation(entry, resource_id=rid, state="FOUND", category=category,
                             identifiers=identifiers, source=f"pdf:{backend}", fields=fields,
                             observed_at=observed_at, system=system)
            for rid in facts["resource_ids"] or [""]], []
