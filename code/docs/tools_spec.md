# Deterministic Tools Specification (v1, pending approval)

Scope: the rule-based building blocks of the deletion-audit agent. These tools read the
dataset, resolve identities, parse evidence, filter by time, apply policy tables, and build,
sort, serialise and score steps. They contain **no LLM calls**. The AI parts (request intent,
screenshot reading, ambiguous category judgement) come later and plug into these tools.

## 1. Ground rules for every tool

| Rule | Detail |
|---|---|
| Language | Python 3.13, standard library (`csv`, `json`, `sqlite3`, `hashlib`, `re`, `datetime`, `subprocess`). Exceptions, PDFs only: the `pdftotext` binary (poppler), and `beautifulsoup4` for the second PDF backend (listed in `code/requirements.txt`). |
| Location | Package `code/ghostaudit/`; tests `code/tests/`; small synthetic fixtures `code/tests/fixtures/`. |
| Paths | Resolve the repo root relative to the package file. Never hardcode absolute paths. |
| Allowed hardcoding | File-format schemas, regexes, field names, lookup tables (resource prefix → system, collection → category, rule action → operations), the operation order table, sort keys. |
| Forbidden hardcoding | Case IDs, expected statuses, per-case resource lists, per-case evidence choices, anything copied from `sample_output.csv` into logic. (AGENTS.md §4.3, §6.7.) |
| Time | All timestamps are ISO-8601 UTC (`...Z`). Compare as `datetime`, never as raw strings from mixed formats. |
| Claims | `reported_status`, activity `reported_result` and log/system messages are recorded as claims. Tools report them; they never turn them into a verdict. |
| Determinism | The same input must give the same output. Sort every list that comes out of a set or dict. |
| Errors | A malformed file or line raises or logs a clear warning with file_id and line number. Never skip silently. |

## 2. Shared data model (`ghostaudit/models.py`)

All tools exchange these dataclasses. Builders must use these exact names and fields.

```python
Request(case_id, email, requested_at, reported_status, reported_at, audit_at, request_text)
UserIdentity(user_id, name, email, phone, email_hash, updated_at)
AuditChange(audit_id, user_id, event_time, field_name, old_value, new_value)
Activity(activity_id, event_time, event_type, source_system, target_system,
         resource_id, reported_result, details)
Rule(rule_id, data_category, condition, required_action)
ManifestEntry(file_id, file_path, origin, captured_at)

SubjectProfile(user_id, names: set, emails: set, phones: set, email_hashes: set,
               history: list[AuditChange])          # all values normalised

Observation(                     # one fact about one resource from one evidence item
    file_id: str,                # evidence_XX
    captured_at: str,            # manifest captured_at: decides citability
    observed_at: str,            # record or line timestamp: decides state as of audit_at
    system_id: str,              # canonical, from SYSTEM_BY_PREFIX
    resource_id: str,
    state: str,                  # FOUND | NOT_FOUND | IDENTIFIABLE | ANONYMIZED | REDACTED
                                 # | ACTIVE | DISABLED | BLOCKED | UNKNOWN
    category: str | None,        # deletion_rules data_category when determinable
    identifiers: frozenset[str], # normalised emails, phones, sha256 hashes, user_N ids, names
    store: str | None,           # primary | read_replica | replica_eu_west | inventory | ...
    source: str,                 # parser + locator, e.g. "log:line 28", "sql:customer_data_primary"
    fields: dict)                # raw parsed fields kept for later rules

Coverage(file_id, captured_at, observed_at, system_id, store, collection)
    # "this evidence item lists the full contents of this collection", so a missing
    # resource there is evidence of absence (gold cites evidence_54 and evidence_51 this way)

Step(step_no, phase, system_id, resource_id, operation, state, evidence_id)
```

## 3. Tools

### T1 `paths.py` + `loaders.py`: dataset loader
- `repo_root()`, `dataset_dir()`. Both resolved from `__file__`.
- `load_requests(name="deletion_requests.csv")`, `load_identities()`, `load_audit_log()`,
  `load_activity()`, `load_rules()`, `load_manifest()`. Each returns a list of the dataclasses above.
- `load_activity()` returns rows sorted by `(event_time, numeric activity_id)`. The raw file is **not** time-ordered.
- **Accept when:** row counts are 518 / 32 / 850 / 306 / 4,011 / 15 / 94; every value is a stripped string; UTF-8 text in Japanese and Korean survives a round trip.

### T2 `timeutil.py`: time handling
- `parse_ts(s) -> datetime` (aware, UTC). Accepts `...Z`, `+00:00`, and `YYYY-MM-DD HH:MM:SS`.
- `at_or_before(ts, cutoff) -> bool`. Equality counts as "before": the gold cites evidence_03, captured exactly at audit_at.
- `parse_human_date(s)` for PDF text such as `25 Aug 2026 04 03 UTC` and `05 Sep 2026 06 30 UTC`.
- **Accept when:** round-trip and ordering tests pass; equal timestamps compare as at-or-before.

### T3 `normalize.py`: identifier normalisation and extraction
- `norm_email(s)`: strip and lowercase. `norm_phone(s)`: keep a leading `+` and digits only.
  `norm_name(s)`: casefold and collapse whitespace. `email_hash(email)`: `sha256(norm_email(email))` hex.
- `extract_identifiers(text) -> set[str]`. Uses regex with token boundaries for emails, `+`-prefixed phone numbers, 64-hex hashes (including the `sha256:` prefix form) and `user_\d+`.
- `SYSTEM_BY_PREFIX` table: `account→account_service`, `search→search_service`,
  `analytics→analytics_platform`, `support→support_platform`, `export→export_service`,
  `data_flow→integration_service`, `backup→backup_service`, `audit_record→security_service`,
  `tombstone→identity_service`, `legal_hold→legal_service`, `cache→cache_service`,
  `email_delivery→email_delivery_service`, `marketing→marketing_platform`.
  `split_resource_id("data_flow_9522") -> ("data_flow", 9522)`.
- **Matching is exact on normalised tokens. Never use substring or fuzzy matching.** The data contains decoys:
  `Jonas Berg` / `Jonas Berglund` / `Joana Berg`, `smartinez6461.jr@...` vs `smartinez6461@...`.
- **Accept when:** `email_hash` matches the dataset for 850/850 users; decoy pairs do not match; uppercase emails such as `K.KOWALSKI36@GMAIL.COM` match their lowercase form.

### T4 `identity.py`: identity resolver
- `IdentityIndex(identities, audit_log)`.
  - `.resolve(email) -> SubjectProfile | None` accepts a current or historical email, in any case.
  - `.owner_of(token) -> user_id | None` is a reverse index over every normalised identifier.
- The profile contains the current values plus every audit-log `old_value`/`new_value`, plus the computed hash of every email it has ever had.
- **Accept when:**
  - `novak.tomasz@icloud.com` resolves to `user_32`.
  - `minjun.kim@gmail.com` resolves to `user_12`, and its hashes include `44a65154bfd8e408…` (the hash of the old email `minjun.kim90@gmail.com`).
  - All 518 request emails resolve: 418 current, 100 historical.
  - Emails, phones and hashes each map to exactly one user.
- **Names are weak identifiers.** Three names belong to two users each: `Gabriel Costa`, `Mateo Ríos`, `Thandiwe Ndlovu`.
  `.owner_of()` therefore returns a set for names, and a name match alone never links a record to a subject.
  Accents are kept, so `Mateo Ríos` ≠ `Mateo Rios Gil`.

### T5 `rules.py`: policy table and category mapping
- `PolicyTable(rules)`. `.action(category) -> DELETE | REDACT | ANONYMIZE | RETAIN`.
- `CATEGORY_BY_COLLECTION`:
  - `customer_profiles→USER_PROFILE`, `customer_search_documents→SEARCH_INDEX_ENTRY`
  - `customer_analytics_reports`, `customer_analytics_profiles→ANALYTICS_PROFILE`
  - `support_conversations`, `support_records→SUPPORT_MESSAGE`, `ticket_attachments→SUPPORT_ATTACHMENT`
  - `customer_export_jobs`, `customer_exports→USER_EXPORT`
  - `connector_configurations`, `connector_subscriptions→DATA_FLOW`
  - `backup_catalog`, `backup_restore_entries→BACKUP_ARCHIVE`
  - `security_audit_events`, `security_audit_records→SECURITY_AUDIT_LOG`
  - `retained_order_records→LEGAL_HOLD_RECORD`, `deleted_customer_references→ACCOUNT_TOMBSTONE`
- An explicit `record_type` in the evidence wins over the collection mapping.
- `DATA_FLOW` is not a rule category. It is a recreation path; the planner (T10) handles it.
- **Accept when:** all 15 rules load; every collection seen in the evidence maps to a category; an unknown collection returns `None`, not a guess.

### T6 `parsers/`: evidence parsers (one module per format)
Each parser exposes `parse(entry: ManifestEntry, path) -> (list[Observation], list[Coverage])`.
`parsers/__init__.py` dispatches on extension and content shape.

| Module | Formats found in the data | Key behaviour |
|---|---|---|
| `sql_snapshot.py` (24 files) | (a) generic `customer_data_<store>` tables with `record_json`; (b) typed `customer_profiles_<store>`, `search_documents_primary`; (c) inventory schema with `snapshot_scope` plus 8 typed tables | Load into in-memory `sqlite3`. Read `Captured at` and `topology` from the header comments. Emit one observation per row, with `store` taken from the table suffix. Emit a `Coverage` for every table, and for every `snapshot_scope` system (inventory). Derive state: identifiers present → `IDENTIFIABLE`/`FOUND`, all null → `ANONYMIZED`/`REDACTED`, `enabled` → `ACTIVE`/`DISABLED`, `restore_allowed` → `ACTIVE`/`BLOCKED`. |
| `json_records.py` (28 files) | (a) flat `records[]` with `system`, `record_type`, `result`, `subject_reference`; (b) snapshot bundles `snapshots[]`, each with its own `captured_at`, including `operational_record_history` (`lookup_value`); (c) `service_resource_export` → `service_exports.{system}.{section}[]` with `observed_at`; search `hits.total == 0` → `NOT_FOUND`; (d) collection dumps (`customer_analytics_report`, `customer_export_jobs`) | Use the per-snapshot or per-item timestamp as `observed_at`. Use `result` as the state when present. An empty `records: []` snapshot produces a `Coverage`. |
| `logs.py` (26 files) | 10 line styles: logfmt, JSON-after-timestamp, RFC5424 syslog, bracketed trace, pipe-delimited, cloud-runtime with an escaped nested `message`, `record_lookup` (explicit result), readback trace, `support_replica` | Step 1: parse each line into `(ts, level, service, trace_id, event, kv)`. Step 2: join `*_started` (`lookup_key`/`match_key`/`subject_key`/`scan_key`/`owner_key`/`subject_ref`) to `*_completed` on `trace_id`, because the lines are interleaved with other traces. Step 3: map the event to a state with a single table `EVENT_STATE_RULES` (below). Keep `correlation_key` as an identifier; it can be a user id, an email, or an email hash. |
| `text_records.py` (1 file) | Handover lines `ts \| system \| resource \| record_type \| result \| subject` | Direct mapping; uppercase subjects are normalised. |
| `pdf_text.py` (4 files) | Statement, attachment, legal register, compliance extract | Two backends behind one interface, `extract_lines(path, backend="pdftotext" \| "bs4") -> list[str]`. (1) **pdftotext:** `pdftotext -layout`. (2) **bs4:** `pdftotext -bbox-layout` XHTML parsed with BeautifulSoup (`html.parser`); words are rebuilt into lines from their coordinates. Shared fact extraction then regexes out resource IDs, `user_N`, emails, phones, `ORD-` refs and dates, and emits fact-style observations (state `FOUND`, category from the document type) with the full text kept in `fields`. Both backends must yield identical facts on all 4 PDFs. A missing binary or library gives a clear error. Selenium is not used: a browser renders PDFs but cannot extract their text. |
| `images.py` (11 files) | Admin-console screenshots | **Not decodable without vision.** Reads `code/cache/vision/<file_id>.json` (schema: list of `{system_id, resource_id, state, identifiers[], observed_at}`). Returns nothing, with a warning, when the cache is missing. Filling the cache is the later AI step. |

`EVENT_STATE_RULES` (initial; builders must verify each row against the real lines):

| Event | State |
|---|---|
| `profile_lookup_completed` | `rows=0` or `http_status=404` → NOT_FOUND; `rows≥1` → FOUND |
| `index_query_completed` | `result=hit` or `total_hits>0` → FOUND; miss/0 → NOT_FOUND |
| `analytics_profile_read(_completed)`, `support_ticket_read_completed` | any direct identifier non-null → IDENTIFIABLE, else ANONYMIZED (analytics) / REDACTED (support) |
| `*_pii_scan`, `analytics_identity_scan` with `matched_value` | IDENTIFIABLE |
| `message_read` | `message_text="[REDACTED]"` → REDACTED, else IDENTIFIABLE |
| `connector_config_read_completed`, `connector_heartbeat`, `connector_config_loaded` | `enabled=true` → ACTIVE, `false` → DISABLED |
| `restore_policy_read_completed` | `restore_allowed=true` → ACTIVE, `false` → BLOCKED |
| `restore_candidate_evaluated` | `eligible=true` and `deletion_block=false` → ACTIVE, else BLOCKED |
| `restore_authorization_completed` | `decision=deny` → BLOCKED, `allow` → ACTIVE |
| `audit_record_read_completed`, `export_*_completed`, `contact_*_completed` | `rows≥1` / `available=true` → FOUND |
| `record_lookup` | explicit `result` field |
| `deletion_marker_check` | `marker_found=false` → FOUND (record still present) |

**Accept when:**
- All 94 manifest files are dispatched: 83 parse fully; the 11 PNGs return empty with a warning.
- `evidence_03` yields 41 observations, including `search_052 FOUND` and `data_flow_9522 ACTIVE`.
- Every line in the 26 logs is either parsed or explicitly classed as noise (0 unparsed lines).
- `audit_record_4902` is found in `evidence_06` with `observed_at=2026-09-16T08:52:59Z` and `captured_at=2026-09-18T11:55:24Z`.
- `analytics_9914` carries identifier `44a65154…`.

### T7 `evidence_index.py`: evidence index and time filter
- `EvidenceIndex.build(manifest)` parses everything once and indexes observations by `resource_id`, identifier token and `file_id`.
- `.for_subject(profile, cutoff) -> list[Observation]`. Direct matches come from any profile identifier (including `user_id`). Then one linkage hop: other observations with the same `resource_id`, or one that references it (e.g. `source_resource_id`).
- `.state_at(resource_id, cutoff) -> Observation | None`: the latest observation with `observed_at ≤ cutoff`.
- `.citable(resource_id, cutoff) -> file_id | None`: the latest **manifest** `captured_at ≤ cutoff` among files that mention the resource. None of the 75 gold citations is after audit_at. The "latest file" part is a hypothesis to confirm on the samples.
- `.coverage(system_id, cutoff)`: lists the files that prove absence by listing a full collection.
- **Accept when:** no observation with `observed_at > cutoff` is ever returned; the `citable()` rule picks the same file as gold for every sample step whose file names the resource.

### T8 `activity.py`: activity stream tools
- `ActivityLog(rows)`. Methods: `.for_resources(ids, until)`, `.window(start, end)`, `.links()` (from `LINK_RESOURCE_IDENTITY`: resource → email, user_id), `.non_deletion_flags()` (from `CLASSIFY_REQUEST`: resource → flag).
- `parse_details(activity)` uses about 35 fixed templates. It extracts extra resource IDs (e.g. `Restore job repopulated downstream resource search_4882`) and recreation signals (`remains enabled`, `can still be restored`, `Active connector repopulated`).
- Every row keeps `reported_result` as a claim field. No method returns a verdict.
- **Accept when:** every template in the file is recognised (0 unmatched); `activity_4000` links `account_5762` to `haruka.s1838@zoho.com` / `user_11`.

### T9 `steps.py`: step builder, sorter and serialiser
- `OPERATIONS` table: operation → `(phase, state, rank)`, in the problem-statement order.
- `make_step(operation, system_id, resource_id, evidence_id="")`, with phase and state filled automatically.
- `sort_and_number(steps)`: sort key `(phase rank, op rank, system_id, resource_id)`, drop exact duplicates, renumber from 1.
- `validate(status, steps)` enforces:
  - only allowed operation/state pairs;
  - `NOT_APPLICABLE` → `[]`;
  - `COMPLETE` → verification steps only, each with evidence;
  - `INCOMPLETE` → findings cite evidence, while remediation and verification use `""`.
- `to_json(steps)`: compact separators `(",", ":")`, `ensure_ascii=False`, keys in schema order
  `step_no, phase, system_id, resource_id, operation, state, evidence_id`. The gold uses this
  order in 24 of 32 rows and alphabetical order in the 8 hand-made rows; the grader ignores key order.
- **Accept when:** re-sorting and re-serialising every gold sample gives JSON equal to gold (32/32), and the output is byte-identical for the 24 schema-ordered rows.

### T10 `planner.py`: per-resource decision matrix
`plan(resource, required_action, current_state, evidence_id) -> ResourcePlan(findings, remediation, verification)`.
Derived from all 193 gold steps; every resource in the samples follows one of these rows.

| Policy / kind | Current state | Finding → Remediation → Verification |
|---|---|---|
| DELETE | FOUND / IDENTIFIABLE | REPORT_REMAINING_RECORD → DELETE_RECORD → VERIFY_RECORD_ABSENT |
| DELETE | NOT_FOUND | VERIFY_RECORD_ABSENT only |
| REDACT (incl. tombstone) | IDENTIFIABLE | REPORT_EXPOSED_PII → REDACT_RECORD → VERIFY_RECORD_REDACTED |
| REDACT | REDACTED | VERIFY_RECORD_REDACTED only |
| ANONYMIZE | IDENTIFIABLE | REPORT_EXPOSED_PII → ANONYMIZE_RECORD → VERIFY_RECORD_ANONYMIZED |
| ANONYMIZE | ANONYMIZED | VERIFY_RECORD_ANONYMIZED only |
| RETAIN (security audit) | FOUND | VERIFY_RECORD_PRESENT only |
| DATA_FLOW | ACTIVE | REPORT_ACTIVE_DATA_FLOW → DISABLE_DATA_FLOW → VERIFY_DATA_FLOW_DISABLED |
| DATA_FLOW | DISABLED | VERIFY_DATA_FLOW_DISABLED only |
| BACKUP_ARCHIVE | ACTIVE (restorable) | REPORT_RESTORE_RISK → BLOCK_RESTORE → VERIFY_RESTORE_BLOCKED |
| BACKUP_ARCHIVE | BLOCKED | VERIFY_RESTORE_BLOCKED only |

`assemble(plans) -> (status, steps)`:
- If any finding exists → `INCOMPLETE`. Findings keep their evidence; remediation and verification get `""`.
- Otherwise → `COMPLETE`. Verification steps keep their evidence.
- Then call `sort_and_number`.

**Accept when:** given the gold (resource, action, state, evidence) tuples, `assemble` reproduces all 32 gold rows exactly.
(Choosing *which* resources and *which* state is the engine's job, not this tool's.)

### T11 `output.py`: output writer and validator
- `write_output(rows, path=repo_root()/"output.csv")` writes the header `case_id,deletion_status,steps_json` with standard CSV quoting. Rows follow the input order of `deletion_requests.csv` (numeric order).
- `validate_output(path, requests)` checks: exactly one row per request (518), allowed statuses, parseable JSON, consecutive `step_no`, phase order, and the T9 rules.
- Also runnable as a CLI: `python -m ghostaudit.output --check output.csv`.

### T12 `scorer.py`: local evaluator (feeds `evaluation_report.md`)
- `score(predicted_csv, gold_csv) -> Report` reports:
  - **case status accuracy**;
  - **step-level** precision, recall and F1 over the multiset of `(phase, system_id, resource_id, operation, state, evidence_id)`;
  - **field-level** accuracy per field on aligned steps;
  - **sequence accuracy** (exact array match), plus a normalised edit-distance score;
  - per-case diffs.
- CLI: `python -m ghostaudit.scorer --pred X --gold dataset/sample_output.csv`.
- **Accept when:** gold vs gold scores 100% on every metric; one hand-made mutation per metric lowers exactly that metric.

## 4. Out of scope for this phase
- Request-intent classification (AI step: 375 distinct texts in six languages).
- Reading the 11 screenshots (AI vision step that fills `code/cache/vision/`).
- The case engine: choosing which resources belong to the subject, resolving category edge cases (e.g. the `email_opt_in` analytics record, support attachment vs message), and deciding which legal holds apply.

## 5. Proposed agent execution plan (after approval)

| Phase | Agents | Files (disjoint, so no conflicts) |
|---|---|---|
| 0 | Main session writes `models.py` (the shared contract) | `models.py` |
| 1a: build | Coder **A**: T1–T5 (the leaf modules every other tool imports) | `paths, loaders, timeutil, normalize, identity, rules` |
| 1b: build | 3 coders in parallel: **B** T6 parsers; **C** T7, T8; **D** T9–T12 | as listed |
| 2: test | 3 **fresh** test writers in parallel (none of them wrote the code under test), stdlib `unittest`, synthetic fixtures plus read-only real-data checks from the acceptance criteria | `code/tests/test_<module>.py` |
| 3: clean | 1 cleanup agent: remove dead code and redundant comments, keep docstrings short; the full test suite must stay green | all of `ghostaudit/` |
| 4 | Main session: runs all tests and acceptance checks, logs, commits, pushes | — |

Agents never commit; the main session reviews diffs and commits after each phase.
Run tests with `python -m unittest discover -s code/tests -t code`.
