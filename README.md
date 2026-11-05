# HackerRank Orchestrate - October 2026

## Ghost Data Still Exists

Build an AI agent that audits whether a user's personal data was actually deleted.

A deletion workflow may report **Complete** while data still exists in databases, logs, support attachments, exports, search indexes, archives, or backups. Sync and restore processes may also bring deleted data back.

For each request, the agent must first determine whether deletion was actually requested. It then returns `COMPLETE`, `INCOMPLETE`, or `NOT_APPLICABLE`, reports any remaining data or recreation risk, and produces the ordered remediation and verification steps required to finish an authorized deletion safely.

Read [`problem_statement.md`](./problem_statement.md) for the complete challenge contract.

---

## Solution: An Evidence-First Deletion-Audit Agent

The solution is a **deterministic, evidence-first audit engine with a small AI layer**:
- The engine reads all 94 evidence files and finds every record of the requester.
- It establishes each record's state *as of the audit time*, applies the deletion policy, and builds the answer steps.
- AI is used only where rules cannot work, reading request intent in 6 languages and reading admin screenshots, and every AI answer is cached so runs are reproducible.

**Why this shape:** the measured data showed the expected answers are mechanical. All 32 sample answers follow one sort order, and every record follows one of two step patterns. So code owns the rigid output contract, and AI is confined to language and images.

### Results (32 public samples, same settings as the submitted `output.csv`)

| Metric | Value |
|---|---|
| Case status accuracy | **90.62%** (29/32) |
| Step precision / recall / F1 | **100.00% / 85.49% / 92.18%** (165 of 165 predicted steps correct, 193 expected) |
| Field accuracy / exact sequence match | 72.88% / 34.38% (lower only because missing steps shift positions) |
| Submitted `output.csv` | 518 valid rows: 357 INCOMPLETE, 151 COMPLETE, 10 NOT_APPLICABLE |

**Recall ceiling.** Of the 193 expected sample steps:
- **165** belong to records that carry the person's email, phone, hash or user ID, and the engine finds all of them;
- **25** belong to records whose identifiers were wiped (11 in evidence, 14 only in activity rows);
- **3** belong to records that appear nowhere in the participant data.

Every missed step is a simple "already compliant" check, so it never changes a case's status.

### How a case is decided

```
dataset/ ─► parsers (SQL, JSON, 7 log styles, text, PDF, screenshot cache) ─► EvidenceIndex
         ─► IdentityIndex (current + historical emails, phones, hashes, user_id)
         ─► ActivityLog (activity rows parsed as claims, links and signals)

per request:  1 Intent gate ─► 2 Identity ─► 3 Discovery ─► 4 State at audit_at
              ─► 5 Policy and category ─► 6 Citation ─► 7 Plan + validate ─► output.csv
```

1. **Intent:** does the text authorize deletion? Uses the cached AI answer if present, else keyword rules. A request to *verify* a deletion counts as deletion; non-deletion requests return `NOT_APPLICABLE` with `[]`.
2. **Identity:** the request email (current or historical) resolves to every identifier the person ever had, including hashes of past emails. Names never link a record on their own, because the data contains lookalikes and shared names.
3. **Discovery:** collect the records that carry those identifiers, plus records linked through catalog rows inside the case's own request window.
4. **State:** use each record's latest observation at or before `audit_at`. A copy left in a database replica counts as still present (a "ghost copy").
5. **Policy:** map each record to `deletion_rules.csv` (delete / redact / anonymize / retain), with data flows and backups treated as recreation risks.
6. **Citation:** cite the latest file **that existed at audit time** and actually **shows the decided state**. Proof of absence comes from a full listing that does not contain the record.
7. **Plan:** an already-compliant record gets one verification; a non-compliant one gets *finding → fix → verification*. Syncs are disabled and restores blocked **before** records are deleted, so data cannot be recreated mid-fix. Every row is validated against the output contract before it is written.

### Code layout (`code/`)

| Path | What it does |
|---|---|
| [`code/main.py`](./code/main.py) | CLI: audits every request, writes and validates `output.csv` |
| [`code/ghostaudit/engine.py`](./code/ghostaudit/engine.py) | The case engine (stages 1–7 above) |
| [`code/ghostaudit/parsers/`](./code/ghostaudit/parsers) | Evidence parsers: SQL snapshots, JSON records, logs, text, PDF (pdftotext / BeautifulSoup / pypdf backends), screenshot cache |
| [`code/ghostaudit/evidence_index.py`](./code/ghostaudit/evidence_index.py) | Time-aware evidence index: linking, state at a cutoff, citation choice, absence proof |
| [`code/ghostaudit/identity.py`](./code/ghostaudit/identity.py), [`normalize.py`](./code/ghostaudit/normalize.py) | Identity resolution and exact identifier matching |
| [`code/ghostaudit/activity.py`](./code/ghostaudit/activity.py) | Activity-log templates, catalog links, recreation signals |
| [`code/ghostaudit/planner.py`](./code/ghostaudit/planner.py), [`steps.py`](./code/ghostaudit/steps.py), [`output.py`](./code/ghostaudit/output.py) | Decision matrix, step sorting, validation and serialisation |
| [`code/ghostaudit/intent_rules.py`](./code/ghostaudit/intent_rules.py) | Keyword intent baseline (6 languages) |
| [`code/ghostaudit/llm.py`](./code/ghostaudit/llm.py), [`intent_llm.py`](./code/ghostaudit/intent_llm.py), [`vision.py`](./code/ghostaudit/vision.py), [`ai_cli.py`](./code/ghostaudit/ai_cli.py) | AI layer on free OpenRouter models: client, intent classifier, screenshot reader, CLI |
| [`code/ghostaudit/scorer.py`](./code/ghostaudit/scorer.py) | Local evaluator (status, step, field and sequence metrics) |
| [`code/tests/`](./code/tests) | 1,021 unit tests in 27 files |

### Key design decisions

- **Evidence first; claims are only clues.** In the samples, 11 of 17 "COMPLETE" workflow claims were actually incomplete. Claims never decide a status.
- **Two clocks per fact.** A record's own time decides its state at `audit_at`; a file's capture time decides whether it may be cited. Evidence created after the audit never changes the answer, and a test proves it.
- **Exact matching only.** Lookalike emails (`g.silva76.jr@…`) and shared names exist in the data, so identifiers are compared exactly after normalisation.
- **Precision over guessing.** Records with no identifier left are added only when they are the single possible candidate; timing-based guessing recovered only about 5 of 20 sample accounts.
- **Standard library only.** Runs anywhere without installs. PDF backends and AI calls are optional extras.
- **Owner-approved rules.** Every design choice beyond the plan was approved by the project owner: 38 numbered decisions, recorded in [`code/docs/engine_plan.md`](./code/docs/engine_plan.md).

### AI layer (free models via OpenRouter)

| Task | Main model | Fallback |
|---|---|---|
| Request intent | `google/gemma-4-31b-it:free` | `nvidia/nemotron-3-super-120b-a12b:free` (the final run used `nvidia/nemotron-3-ultra-550b-a55b:free` via an environment variable) |
| Screenshot reading | `nvidia/nemotron-3.5-super-vl:free` | `google/gemma-4-31b-it:free` |

- **Retries:** waits grow 2 s → 4 s → 8 s, 4 tries per model, then the fallback model. Failed items are left for the next run, never guessed.
- **Determinism:** temperature 0 and answers cached per text hash and prompt version. Reruns make no API calls.
- **Secrets:** the API key is read only from `OPENROUTER_API_KEY` and is never logged or committed.
- **The actual run:** 271 of 550 request rows were classified (148 attempts, 83,348 tokens, $0.00) before it was stopped for the deadline, with **0 disagreements** with the keyword rules. `output.csv` is therefore identical with either intent source. Details: [`code/evaluation/usage_report.md`](./code/evaluation/usage_report.md).

### Run it

```bash
python3 code/main.py --intent rules                     # writes ./output.csv (the submitted file)
python3 code/main.py --requests sample_deletion_requests.csv --output /tmp/sample.csv
cd code && python3 -m ghostaudit.scorer --pred /tmp/sample.csv --gold ../dataset/sample_output.csv && cd ..
cd code && python3 -m ghostaudit.ai_cli status && cd ..  # AI cache status (intent/vision need OPENROUTER_API_KEY)
python3 -m unittest discover -s code/tests -t code       # 1,021 tests
```

### Quality assurance

- **Independent testers.** Five test agents wrote 1,021 tests, never for code they wrote themselves. They caught real bugs, all fixed:
  - an absence proof that could ignore newer evidence;
  - an endless retry on "retry after 0 seconds";
  - citations that could contradict the decided state.
- **Fingerprint check.** A fingerprint of all real-data output proved the cleanup refactor changed no behaviour.
- **No-hardcoding guard.** A test fails if product code contains a literal case, evidence or activity ID.

### Limitations

- Records whose identifiers were wiped are mostly not recovered; this is the main source of lost recall.
- The screenshot reader was not run. Screenshot-only records use the approved stand-in: assume the record is present and cite its catalog link row.
- Three sample answers reference records that exist nowhere in the participant data.

### More detail

- [`code/README.md`](./code/README.md): setup and run.
- [`code/docs/system_explanation.pdf`](./code/docs/system_explanation.pdf): end-to-end walkthrough of every module and function.
- [`code/docs/engine_plan.md`](./code/docs/engine_plan.md): design and owner decisions.
- [`code/docs/dataset_analysis.md`](./code/docs/dataset_analysis.md): verified data findings.
- [`code/evaluation/evaluation_report.md`](./code/evaluation/evaluation_report.md): evaluation results.

---

## Quick Start

Clone the repository and move into the project directory:

```bash
git clone https://github.com/interviewstreet/hackerrank-orchestrate-october26.git
cd hackerrank-orchestrate-october26
```

Start your AI coding tool from the **repository root** so it automatically reads [`AGENTS.md`](./AGENTS.md) and logs your session turns to `log.txt` in the root directory.

Build your solution in `code/main.py`, or use another language and document its setup and run command clearly in your README.

Your solution must:

- Read the input files from `dataset/`
- Generate one prediction row for every request in `dataset/deletion_requests.csv`
- Write the final predictions to `output.csv` in the repository root

Run the starter Python entry point with:

```bash
python3 code/main.py
```

The blank template at `dataset/output.csv` is provided as a reference. Your final generated file for submission must be the root-level `output.csv`.


## Dataset

All participant-facing files are in `dataset/`:

| File | Contents |
| --- | --- |
| `deletion_requests.csv` | 518 requests |
| `sample_deletion_requests.csv` | 32 sample requests |
| `sample_output.csv` | Correct sample answers |
| `user_identities.csv` | Current user identities |
| `user_audit_log.csv` | Historical identity changes |
| `system_activity.csv` | Activity across services and data processes |
| `deletion_rules.csv` | Required treatment for each data category |
| `evidence_files.csv` | Manifest of supplied evidence files |
| `output.csv` | Blank output template |

Evidence is stored under `dataset/evidence/` and may include PDFs, images, logs, JSON, plain text, or SQL snapshot exports. SQL captures may contain a primary database, regional replicas, a reporting replica, or a failover replica so replication drift can be investigated directly. The `.sql` files are readable as text and executable with SQLite.

Only `deletion_requests.csv` requires answers. The other files provide evidence and context.

---

## Output

Create `output.csv` with these columns in this exact order:

```text
case_id,deletion_status,steps_json
```

Produce exactly one row per request. `deletion_status` is `COMPLETE`, `INCOMPLETE`, or `NOT_APPLICABLE`. `steps_json` is an ordered JSON array containing findings, remediation actions, and verification checks. Use an empty array (`[]`) for `NOT_APPLICABLE`. Evidence references may be a `file_id`, `activity_id`, or `audit_id`. Determine the resource state at `audit_at` from the available evidence; reported workflow statuses and operation results are claims. Use `dataset/sample_output.csv` as the format reference.

The final file must contain 518 data rows sorted by `case_id` (plus the header row). The grader parses `steps_json` as JSON, so object-key order and whitespace do not matter; array order does. Order findings, remediation, and verification in that phase order. Within a phase, follow the operation order in the problem statement, then sort by `system_id` and `resource_id`.

---

## Evaluation

`output.csv` will be compared with hidden canonical answers. Scoring considers:

- deletion assessment accuracy;
- current and historical identity resolution;
- discovery of remaining data and recreation risks;
- correct policy application and evidence references; and
- completeness and ordering of remediation and verification steps.

Your `code.zip` must include:

```text
code/evaluation/evaluation_report.md
code/evaluation/usage_report.md
```

- `evaluation_report.md` must cover case, row, field, and sequence accuracy, at least three additional evaluation cases created by you, failures found, and improvements made.
- `usage_report.md` must cover the final full-dataset run, including model providers and names, model calls, input and output tokens, total and average tokens per request, and estimated total and per-request cost. Include per-model and overall totals when using multiple models.

**Important:** Place `evaluation_report.md` and `usage_report.md` inside `code/evaluation/` (matching `code/evaluation/main.py` in this starter layout), write your complete evaluation metrics, custom evaluation test cases, and token/cost breakdowns directly inside those Markdown files, and keep prompts in source code or Markdown files. Automated code evaluation ignores raw data files (`.csv`, `.tsv`, `.json`, `.jsonl`, `.sql`, `.log`), so content stored only in those formats will not be visible to the code judge or AI interviewer.

---

## Chat Transcript

This repository includes [`AGENTS.md`](./AGENTS.md), which instructs compatible AI coding tools to append conversation summaries to `log.txt` in the repository root:

| Platform | Path |
| --- | --- |
| macOS / Linux | `<repo root>/log.txt` |
| Windows | `<repo root>\log.txt` |

- Always launch your AI coding agent from the repository root (or explicitly ask it to read `./AGENTS.md` first) so every turn is logged to `log.txt` with the required `User Prompt`, `Agent Response Summary`, `Actions`, and `Context` sections.
- Upload `log.txt` directly as your `chat_transcript` submission on HackerRank.
- Do not include API keys, tokens, credentials, or secrets in prompts, code, configuration, or `log.txt`.

---

## Packaging `code.zip` and Submission

| Item | Description |
| --- | --- |
| `code.zip` | Runnable solution, prompts, configuration, `README.md`, and `evaluation/` reports |
| `output.csv` | One answer row for each of the 518 requests in `dataset/deletion_requests.csv` |
| `chat_transcript` | The root `log.txt` file generated by your coding agent |
| AI interview | Live explanation of your architecture, approach, and evaluation |

You may use any programming language, model, framework, or retrieval approach. Include clear setup and execution instructions in `README.md` inside `code.zip`.

To package `code.zip` from the repository root without bundling the dataset:

**macOS / Linux:**

```bash
zip -r code.zip README.md code -x "*/__pycache__/*" "*/.venv/*" "*/venv/*" "*/node_modules/*" "*/.git/*" "*/.DS_Store"
```

**Windows (PowerShell):**

```powershell
Compress-Archive -Path README.md, code -DestinationPath code.zip -Force
```

When packaging `code.zip`:

- Keep your evaluation reports in `code/evaluation/` so they are bundled automatically with `code` (or add `evaluation` to the archive command above if you placed them in a top-level `evaluation/` directory).
- Do **not** include `dataset/`, virtual environments (`.venv`, `venv`, `node_modules`), or build caches (`__pycache__`). On Windows, keep `.venv` at the repository root (outside `code/`) and remove any `code/__pycache__` folders before running `Compress-Archive`.
- Do **not** include nested archives (`.zip`, `.tar`, `.gz`, `.tgz`, `.7z`, `.rar`) inside `code.zip` — submissions containing nested archive files will be rejected.

Before submitting, confirm that:

- `output.csv` has the exact header `case_id,deletion_status,steps_json` and contains all 518 data rows sorted by `case_id`;
- every `steps_json` value is a valid JSON array with consecutive step numbers starting at `1`;
- findings cite valid evidence IDs, and `COMPLETE` verification steps cite evidence proving the current state;
- proposed remediation and future verification steps use an empty `evidence_id` (`""`);
- `code.zip` includes your runnable code, `README.md`, `code/evaluation/evaluation_report.md`, and `code/evaluation/usage_report.md`;
- `log.txt` follows the `AGENTS.md` turn format and is uploaded to the `chat_transcript` slot; and
- no API keys, credentials, real personal data, or sensitive configuration values are included.
