# Case Engine and AI Parts: Plan

This plan builds on the tools in `code/ghostaudit/` (see `tools_spec.md`) and on
`dataset_analysis.md`. Every number below was measured on the participant data with those
tools. **Interpretation** marks a reading of the data rather than a raw count.

---

## 1. What the measurements tell us (the facts that shape the design)

A gap analysis ran the existing tools on the 32 sample cases. For each case it resolved the
person, collected the records linked to them at `audit_at`, and compared the result with the
sample answer.

| Finding | Number |
|---|---|
| Records in the 32 sample answers | 111 records, 193 steps |
| Records the tools link to the person through an identifier (email, phone, hash, user_id) | **83 of 111**, covering **165 of 193 steps (85%)** |
| Of those 83, records whose tool-derived state equals the state implied by the answer | **83 of 83** |
| Records the tools link that the answer does not contain | 1 |
| Citations where the tools pick the same file as the answer | 74 of 75 |

**The 28 records the tools miss are all already-compliant records** (deleted accounts,
anonymized analytics, retained audit records). Each one contributes **exactly one
verification step and no finding**.

| How the record can be reached | Records | Example |
|---|---|---|
| T1: linked by an identifier | 83 | `search_052` carries `novak.tomasz@icloud.com` |
| T2: present in evidence, but with no identifier | 11 | `account_6440`: lookup returned `404, profile: null` |
| T3: only in activity rows | 14 | `account_5521`: only a "submitted primary profile" deletion row |
| T4: nowhere in participant data | 3 | `audit_record_6645`, `audit_record_5569`, `account_004` |

**Consequences for the design:**
1. **Status can be decided from identifier-linked evidence alone.** Every finding, and
   therefore every INCOMPLETE verdict, sits on a T1 record.
2. **T1 sets the step ceiling.** T1 alone can reach about 85% of sample steps. T2/T3 records
   add only "already compliant" checks, and T4 is unreachable. The engine must never invent IDs.
3. **The tools already get state and citations right.** The engine's job is mainly
   *choosing records*, *mapping policy* and *assembling* the answer.

**Evaluation set coverage:** 500 of 518 cases have at least one identifier-linked record. The 18
without one are exactly:
- the 9 non-deletion requests (`case_534`–`case_542`);
- 9 cases that depend on a screenshot or PDF (`case_33, 35, 39, 50, 52, 53, 55, 61, 63`).

Both groups are covered by the AI parts below.

---

## 2. Case engine (deterministic)

### 2.1 Pipeline

```
 once:  load CSVs ─► parse all evidence ─► EvidenceIndex, IdentityIndex, ActivityLog, PolicyTable
        + intent cache (AI part A1) + vision cache (AI part A2)

 per case:
  S1 Intent gate ───────── not a deletion request? ──► NOT_APPLICABLE, []
  S2 Identity ──────────── resolve(email) → every email, phone, hash and user_id the person had
  S3 Discovery ─────────── for_subject(profile, audit_at, activity links) → records (T1)
                           + optional linker for identifier-free records (T2/T3), see 2.4
  S4 State at audit_at ─── latest observation per store; any store present ⇒ present
  S5 Policy ────────────── category → required treatment (+ condition checks) → planner kind
  S6 Citation ──────────── citable() for findings and COMPLETE checks; absent_in() for absence
  S7 Plan + assemble ───── planner.plan() per record → assemble() → validate()
  S8 Output ────────────── write_output() → validate_output() → scorer on samples
```

### 2.2 Decision rules

**Category → treatment** (from `deletion_rules.csv`, loaded as data rather than hard-coded):

| Category | Planner kind | Notes |
|---|---|---|
| USER_PROFILE, SECONDARY_IDENTIFIER, SEARCH_INDEX_ENTRY, CACHE_ENTRY, SUPPORT_ATTACHMENT, USER_EXPORT, MARKETING_PROFILE | DELETE | |
| SUPPORT_MESSAGE, EMAIL_DELIVERY_LOG, ACCOUNT_TOMBSTONE | REDACT | A tombstone keeps `user_id` only |
| ANALYTICS_PROFILE, PRODUCT_TELEMETRY | ANONYMIZE | |
| SECURITY_AUDIT_LOG | RETAIN | Only while `retention_until > audit_at`; otherwise flag it |
| LEGAL_HOLD_RECORD | RETAIN | Only while the hold is active (`retained_until > audit_at`). There are 8 linked legal-hold records in the evaluation set and none in the samples, so this rule is untested on gold. |
| BACKUP_ARCHIVE | BACKUP_ARCHIVE | Restore allowed → block it; already blocked → verify |
| DATA_FLOW (no rule; a recreation path) | DATA_FLOW | Enabled → disable it; disabled → verify |

**State per record:**
- Use the latest observation at or before `audit_at` (`observed_at`).
- If **any store** (primary or any replica) still shows the record, it is present. This is the
  ghost-copy rule: all 13 drift rows are account copies left in a replica.
- A state the planner does not define for a kind is mapped explicitly. For example, FOUND for a
  REDACT record means checking whether identifiers are still in its fields. Anything that can't
  be decided is flagged, never guessed silently.

**Ordering, citations, format:** already handled by the tools (`sort_and_number`, `citable`,
`absent_in`, `to_json`, `validate`).

### 2.3 Recreation signals (activity is a claim, evidence decides)
- A data flow counts as active only when **evidence** shows it enabled (connector config,
  heartbeat, inventory row or screenshot). "Remains enabled" activity rows (89) only corroborate.
- A backup is a restore risk when evidence shows `restore_allowed=true` / eligible, corroborated
  by "can still be restored" rows (28).
- "Repopulated downstream resource" rows (31 connector, 1 restore job) mark records that came
  back after deletion. The engine re-checks those records' state after the repopulation time.

### 2.4 Identifier-free records (T2/T3): best effort, precision first
These are already-deleted accounts and anonymized analytics with no identifier left. The only
link to the person is the deletion workflow's own activity rows. What the data shows:
- **Constraint 1 (window):** 262 of 271 known "submitted primary profile" deletion rows fall
  inside their own case's `[requested_at, reported_at]` window.
- **Constraint 2 (claim consistency):** the case's claimed status matches the row's claimed
  result in 266 of 271 known pairs: COMPLETE↔SUCCESS 140/143, NOT_STARTED↔FAILED 56/58,
  PARTIALLY_COMPLETE↔PARTIAL 70/70.
- **Not enough on its own:** 302 of the 550 cases have no identifier-linked account among the
  532 accounts that have a "submitted primary profile" row. After both constraints and removing
  rows already claimed through identifiers, most of those cases still have 2–6 candidates. A one-to-one matching recovers only about 5 of 20 sample accounts.

**Plan:** solve it once for all 550 cases as a one-to-one assignment. A record is emitted only
when it is the single consistent candidate; otherwise the engine abstains. Because these
records only ever add a VERIFY step, abstaining costs a little recall but never changes a
status. The threshold is chosen by scoring both options on the samples.

---

## 3. AI parts

The LLM is used only where rules cannot work. Every output is cached on disk and committed, so
runs are reproducible and the final run needs no API calls unless the cache is missing.

| Part | Job | Calls | Model (to confirm at build time) |
|---|---|---|---|
| **A1 Intent** | Does the request authorize deletion? | 375 distinct evaluation texts + 32 sample texts, sent in batches of about 25, so about 17 calls | `claude-sonnet-5-5` (or `claude-haiku-5-5` if accuracy on test cases is equal) |
| **A2 Vision** | Read the facts shown in the 11 screenshots | 11 calls, one per image | `claude-sonnet-5-5` |
| **A3 Category check** (optional) | Settle a record whose category hints conflict | expected to be a handful | `claude-sonnet-5-5` |

### A1 Intent classifier
- **Output (JSON):** `{authorizes_deletion, request_type, language, confidence, quote}`.
  `request_type` is one of: deletion, deletion_verification, access, export, correction,
  marketing_opt_out, account_help, other.
- **Definition given to the model:** a request to *verify or audit a prior deletion* counts as
  deletion. The samples prove this: "Can you verify that my account was deleted?" is COMPLETE,
  not NOT_APPLICABLE. "Stop marketing", "send me a copy", "fix my name" and "reset my login"
  do not count.
- **Settings and caching:** temperature 0, cache keyed by a SHA-256 of the text.
- **Cross-checks** (any disagreement is reported, never silently overridden):
  - (a) the 10 `CLASSIFY_REQUEST` activity rows, mapped account → person;
  - (b) a simple keyword baseline in all 6 languages.
- **Without an API key** the keyword baseline runs alone, clearly labelled.

### A2 Screenshot reader
- **What it extracts:** only the facts on screen (resource IDs, emails, phones, user IDs,
  names, status labels, timestamps), returned as JSON in the schema `parsers/images.py`
  already reads (`code/cache/vision/<file_id>.json`).
- **Rules map labels to states:** the model reports labels; deterministic rules turn labels
  into states (e.g. connector "Continuous / Healthy" → ACTIVE, export "Ready" → FOUND), so
  the reasoning stays auditable.
- **Validation:** every extracted ID must match the resource-ID pattern. Each screenshot is
  checked against its catalog link row (12 rows, each 7 min 1 s after the capture).
- **Conflicts are flagged:** the marketing screen shows "Soo-jin Park", but its link row says
  `user_09` (`case_63`). The engine links through the catalog row and flags the case.

### Usage logging
A thin client wrapper records model, input and output tokens per call in
`code/cache/usage.jsonl`. `evaluation/usage_report.md` is generated from it, with costs
computed from the published price list at run time rather than hard-coded. The API key is read
only from the `ANTHROPIC_API_KEY` environment variable and is never logged.

---

## 4. Exception cases found in the data, and how to handle them

| # | Exception | Evidence in the data | Risk if ignored | Mitigation (and how it generalizes) |
|---|---|---|---|---|
| E1 | **Workflow claims are wrong** | 11 of 17 sample "COMPLETE" claims are really incomplete; 3 "NOT_STARTED" are complete | Copying the claim | Evidence-first. Claims are used only as a weak *linking* feature (E8), never as a verdict |
| E2 | **Old identifiers** | 100 of 518 requests use an old email; `analytics_9914` is linked only by the hash of `user_12`'s *previous* email | Missed records | Full identity history plus computed hashes of every past email, for any identifier type |
| E3 | **Decoys and lookalikes** | `g.silva76.jr@gmail.com`, Jonas Berg / Berglund / Joana Berg, Mateo Rios Gil vs Mateo Ríos, 300 unrelated lookups in `evidence_53` | Wrong person's data | Exact matching on normalised tokens only; never fuzzy. Unknown people are ignored |
| E4 | **Shared names** | 3 names belong to 2 users each | False links | Names never link alone; they can only corroborate |
| E5 | **Two clocks** | 175 evaluation cases have evidence about the person dated after `audit_at`. Log files span days (`evidence_06`: 10–18 Sep). Some records are observed *after* their file's export (`account_1417`, `account_5043`) | Using future facts | `observed_at` decides state, the manifest `captured_at` decides citability; `strict` mode is available |
| E6 | **Ghost copies in replicas** | 13 account rows exist only in a replica (`account_994` in `evidence_05`) | Calling a deletion complete | Per-store state: any store present ⇒ remaining. `system_id` comes from the resource prefix |
| E7 | **Proving absence** | Empty snapshots and full listings are the only proof; `case_04` cites `evidence_54` while the tools pick the newer `evidence_90` | Uncited or wrong checks | `absent_in` uses only the newest listing and never lets an older one override newer evidence. Absence proofs are ranked by specificity (same store/collection) and that is tested on samples |
| E8 | **Records with no identifiers** | 28 of 111 sample records are T2–T4 (25 reachable, 3 not); 302 of 550 cases have no identifier-linked account with a deletion-workflow row | Lower step recall | Status from T1 only; a global one-to-one assignment under window and claim constraints; abstain when ambiguous |
| E9 | **Unreachable answers** | 3 sample records appear nowhere in participant data | Chasing the impossible | Accept the ceiling and document it in the evaluation report; never invent IDs |
| E10 | **Category edge cases** | `analytics_9914` (`email_opt_in=true`, contact lookup) is DELETE in gold, not ANONYMIZE; `support_001`/`support_002` (attachment, legacy contact index) are DELETE, not REDACT | Wrong treatment | Explicit `record_type` > collection > field hints (opt-in → marketing profile, file attachment → attachment, legacy contact copy → secondary identifier). A3 handles anything still unresolved. The hint table is data, so new systems need no code change |
| E11 | **Policy conditions** | Retention dates, legal holds, "attachment belongs only to the subject" (`activity_4007`), "shared context" in support messages | Deleting what must be kept | Evaluate each condition from evidence at `audit_at`. If a condition can't be proven, apply a documented default and flag it. A missing retained record has no allowed operation, so it is reported in the run log, not invented |
| E12 | **Screenshots and PDFs** | 12 evaluation cases hinge on an image or PDF; one screenshot conflicts with its link row (`case_63`) | 9 cases with no evidence | A2 vision cache plus catalog link rows; conflicts flagged. PDFs are already read by 3 identical backends |
| E13 | **Intent subtleties** | 10 non-deletion texts, none in the samples; verify-style requests count as deletion; 6 languages | Wrong NOT_APPLICABLE | A1 with an explicit definition, tricky few-shot examples, a cross-check with `CLASSIFY_REQUEST` rows and a keyword baseline |
| E14 | **Data comes back** | 112 linked flows still ACTIVE at audit; 25 restorable backups; "repopulated" rows | Calling it complete too early | Recreation rules (2.3): disable or block before deleting, which the planner order already enforces |
| E15 | **Same person, several cases** | `ingrid.dahl@outlook.com` has `case_33` and `case_52`; `user_17` has sample `case_17` and evaluation `case_46` | Leaking state across cases | Every case is recomputed from its own `audit_at`; nothing is cached per person |
| E16 | **Unrepresentative samples** | `case_01`–`08` and `case_10` are hand-made (`account_00N`, `evidence_54`); there is no NOT_APPLICABLE sample | Overfitting | Hold-out evaluation, custom test cases, metamorphic tests (section 5) |
| E17 | **Format traps** | Strict sort order, schema key order, numeric case order, `resource_id` sorted as text | Lost points on format | Already enforced by `steps.validate` and `output.validate_output` |

---

## 5. Generalization strategy (beyond this dataset)

1. **Rules as data.** The policy table, collection → category map, field hints and log event
   rules are tables, not code paths. A new system or category means a table row.
2. **Evidence first, claims as features.** Claims can help *link* records; they can never
   *decide* a status.
3. **Confidence and abstention.** Each linked record carries why it was linked (identifier,
   resource link, catalog link, workflow assignment). Low-confidence links can only add a
   harmless verification, or are dropped.
4. **No hard-coding, enforced by a test.** A unit test fails if product code contains a literal
   case ID or evidence file ID.
5. **Metamorphic tests.** The output must stay the same when evidence files are reordered or
   email case changes. Renaming IDs consistently must rename the output the same way.
   Information dated after `audit_at` must never change the answer.
6. **Hold-out discipline.** Rules are tuned on sample cases 01–16 and checked on 17–32; the
   final numbers are reported on all 32 plus the custom cases.
7. **Graceful failure.** Unknown formats produce warnings, not crashes. A case that can't be
   decided falls back to the safest valid answer and is listed in the run report.

---

## 6. Evaluation plan

- **Metrics** (`scorer.py`): status accuracy, step precision/recall/F1 (with and without
  evidence), field accuracy and sequence accuracy on the 32 samples. Also a "T1 ceiling"
  showing the best score possible without identifier-free records.
- **Custom cases (at least 3, required):**
  1. a non-deletion request in Japanese;
  2. a historical email with a replica-only account;
  3. evidence dated after `audit_at` that must be ignored;
  4. the screenshot-conflict pattern.
- **Ablations:** no identity history, no replica rule, no vision, no workflow linker. These
  show what each part contributes.
- **Reports:** `evaluation/evaluation_report.md` and `evaluation/usage_report.md`, generated
  from the final full run that writes `output.csv`.

---

## 7. Build plan (same agent workflow as the tools)

| Phase | Work | Accept when |
|---|---|---|
| E1 | Engine core (S1–S8) with intent from a cache or the keyword baseline; `code/main.py` CLI writes `output.csv` | 518 valid rows; sample status accuracy and step F1 reported; T1 ceiling reached on samples |
| E2 | AI clients: A1 intent and A2 vision, with caching and usage logging (needs the API key) | Caches committed; reruns make 0 API calls; vision facts validated against link rows |
| E3 | Identifier-free record linker (2.4) with an abstain threshold | Sample step F1 does not drop; status unchanged |
| E4 | Evaluation report, usage report, custom cases, README | Required sections present |
| E5 | Tests (unit, metamorphic, no-hard-coding) and cleanup | Full suite green; fingerprint-style before/after check |

## 8. Decisions needed
1. **API key:** add `ANTHROPIC_API_KEY` in the environment settings (Network secrets, or as an
   environment variable). Without it, A1 runs on the keyword baseline and A2 can't fill the
   screenshot cache.
2. **Models:** Sonnet 5.5 for both A1 and A2 (recommended), or Haiku 5.5 for A1 to cut cost.
3. **Identifier-free records:** abstain unless unambiguous (recommended), or always emit the
   best guess.
