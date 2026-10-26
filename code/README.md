# Ghost Data Still Exists: Deletion-Audit Agent

For every request in `dataset/deletion_requests.csv`, the agent decides `COMPLETE`, `INCOMPLETE`
or `NOT_APPLICABLE` and writes the ordered steps to `output.csv` (`case_id,deletion_status,steps_json`).

## Setup
- Python 3.13. The core uses only the standard library.
- `pdftotext` (poppler-utils) is the main PDF reader. If it is missing, install the optional
  backends with `pip install -r code/requirements.txt` (pypdf, beautifulsoup4).
- **Optional AI steps** use free OpenRouter models. Set `OPENROUTER_API_KEY` in the environment
  and never commit it. Defaults: intent `google/gemma-4-31b-it:free`, screenshots
  `nvidia/nemotron-3.5-super-vl:free`, with fallbacks `nvidia/nemotron-3-super-120b-a12b:free` and
  `google/gemma-4-31b-it:free`. Every answer is cached under `code/cache/`.

## Run (from the repository root)
```bash
python3 code/main.py                       # writes ./output.csv for all 518 cases (intent: AI cache if present, else keyword rules)
python3 code/main.py --intent rules        # deterministic keyword intent only (used for the submitted output.csv)
python3 code/main.py --requests sample_deletion_requests.csv --output /tmp/sample.csv
cd code && python3 -m ghostaudit.scorer --pred /tmp/sample.csv --gold ../dataset/sample_output.csv
cd code && python3 -m ghostaudit.ai_cli status|intent|vision   # optional AI cache filling (needs the key)
python3 -m unittest discover -s code/tests -t code                # 1,021 tests
```

## How it works
1. **Parse all evidence** (`ghostaudit/parsers/`): SQL snapshots, JSON, 7 log styles, text, PDF
   (pdftotext, bs4 or pypdf), and screenshot facts from the vision cache.
2. **Resolve the person** from the request's current or historical email: every email, phone,
   hash and `user_id` they have had. Names alone never link.
3. **Find their records as of `audit_at`.** A record's own time decides its state; the file's
   capture time decides whether it can be cited.
4. **Apply `deletion_rules.csv`** with category hints. Data flows and backups are recreation risks.
5. **Plan the steps** with a fixed decision matrix. Steps are sorted and validated against the
   output contract.

Design and every owner decision: `code/docs/engine_plan.md` (37 decisions), `code/docs/tools_spec.md`,
`code/docs/dataset_analysis.md`. Reports: `code/evaluation/`.
