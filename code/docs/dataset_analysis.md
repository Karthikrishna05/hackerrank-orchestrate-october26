# Dataset Analysis: Ghost Data Still Exists

This is a plain-language tour of everything in `dataset/`. Every number below was computed
directly from the files with Python scripts. Any reading of the data (not a raw count) is
marked **Interpretation**.

## 0. The big picture

The dataset describes a fictional online shop, **Northstar Shop**, that receives requests to
delete customer data. For each request you must decide whether the data is really gone.

| What | Where | Size |
|---|---|---|
| Requests to judge | `deletion_requests.csv` | 518 cases |
| Practice requests with answers | `sample_deletion_requests.csv` + `sample_output.csv` | 32 cases |
| Who each person is | `user_identities.csv` + `user_audit_log.csv` | 850 people, 306 identity changes |
| What the systems did | `system_activity.csv` | 4,011 events |
| What the policy requires | `deletion_rules.csv` | 15 rules |
| Index of evidence files | `evidence_files.csv` | 94 files |
| The evidence itself | `evidence/` (5 folders) | 24 SQL, 28 JSON, 1 TXT, 26 logs, 4 PDFs, 11 PNGs |
| Blank answer sheet | `output.csv` | header only |

How the pieces connect:

```
request email ──► user_identities / user_audit_log ──► every email, phone, hash and user_id the person ever had
                                                            │
                         evidence files (SQL, JSON, logs, PDFs, screenshots) mention these identifiers
                                                            │
                         each record found has a resource_id (account_4248, search_052, ...) and a state
                                                            │
system_activity mentions the same resource_ids, but almost never the person ──► extra context (claims)
                                                            │
                         deletion_rules says what each kind of record should look like ──► answer
```

---

## Part 1: The CSV files

### 1.1 `deletion_requests.csv`: the 518 cases to answer

**Columns:** `case_id, email, requested_at, reported_status, reported_at, audit_at, request_text`

- **Case IDs** run from `case_33` to `case_550` with no gaps (518 cases). The samples use `case_01`–`case_32`, so the two files never overlap.
- **Timeline:** every case follows `requested_at ≤ reported_at ≤ audit_at` (518 of 518).
  - The workflow reports a status **1 to 3 days** after the request (median 2 days).
  - The audit happens **1.5 to 28 days** after the report (median 17 days).
  - Requests span 5 Jan 2026 to 24 Sep 2026. Audits span 29 Jan 2026 to 1 Oct 2026.
- **What the workflow claims (`reported_status`):**

  | Claim | Cases |
  |---|---|
  | COMPLETE | 254 |
  | PARTIALLY_COMPLETE | 152 |
  | NOT_STARTED | 112 |

- **Which email is used:** 418 requests use the person's **current** email. **100 use an old email** that appears only in the identity history. 20 request emails contain uppercase letters.
- **People:** the 518 cases belong to 517 different people. One person, `ingrid.dahl@outlook.com`, has two cases (`case_33` and `case_52`).
- **Request text:**
  - There are 375 different texts, 86 to 252 characters long.
  - By a simple script check of the language: English 456, Portuguese 14, Spanish 12, French 12, Japanese 12, Korean 12.
  - **Exactly 10 texts do not ask for deletion** (`case_533`–`case_542`). For example: "Please stop the promotional emails…", "My surname is misspelled…", "Can you tell me what personal information you hold about me?", "Please change the email address used for this account."
  - Every other text is a clear deletion request built from a small set of opening sentences, e.g. "Please treat this message as a request for permanent account deletion."

**Watch out for:**
- **Old emails:** 1 in 5 requests uses an old email, so identity history is essential.
- **The claim isn't the truth:** the sample answers show that the claim says little about the real outcome (see 1.3).

### 1.2 `sample_deletion_requests.csv`: 32 practice cases

The same columns as above.

- **Claims:** COMPLETE 17, PARTIALLY_COMPLETE 5, NOT_STARTED 10.
- **Emails:** 29 current, 3 old.
- **Languages:** English 29, Spanish 1, French 1, Japanese 1.
- **Dates:** audits run from 3 Sep 2026 to 1 Oct 2026.
- **Every text is unique and conversational,** for example:
  - "I deleted this account and received a confirmation, but the same email address is still recognized."
  - "Can you verify that my account was deleted?"

  **Interpretation:** these look hand-written, unlike the templated evaluation texts, so the samples are not fully representative of the 518 cases.

### 1.3 `sample_output.csv`: the answers for the 32 practice cases

**Columns:** `case_id, deletion_status, steps_json`

- **Statuses:** COMPLETE 10, INCOMPLETE 22, **NOT_APPLICABLE 0**. There is no worked example of a non-deletion case.
- **Steps:** 193 in total, 1 to 14 per case (COMPLETE cases 1–6, INCOMPLETE cases 3–14).
- **Claim vs. truth:**

  | Workflow claimed | True status | Cases |
  |---|---|---|
  | COMPLETE | COMPLETE | 6 |
  | COMPLETE | **INCOMPLETE** | 11 |
  | PARTIALLY_COMPLETE | COMPLETE | 1 |
  | PARTIALLY_COMPLETE | INCOMPLETE | 4 |
  | NOT_STARTED | **COMPLETE** | 3 |
  | NOT_STARTED | INCOMPLETE | 7 |

- **Operations used:**

  | Operation | Count |
  |---|---|
  | VERIFY_RECORD_ABSENT | 50 |
  | VERIFY_RECORD_PRESENT | 24 |
  | REPORT_REMAINING_RECORD | 21 |
  | DELETE_RECORD | 21 |
  | VERIFY_RECORD_ANONYMIZED | 12 |
  | REPORT_EXPOSED_PII | 11 |
  | VERIFY_RECORD_REDACTED | 10 |
  | VERIFY_DATA_FLOW_DISABLED | 9 |
  | REPORT_ACTIVE_DATA_FLOW, DISABLE_DATA_FLOW, REDACT_RECORD, VERIFY_RESTORE_BLOCKED | 6 each |
  | ANONYMIZE_RECORD | 5 |
  | REPORT_RESTORE_RISK, BLOCK_RESTORE | 3 each |

- **Ordering:** every case follows one strict sort with zero exceptions: phase, then the operation's position in the official table, then `system_id`, then `resource_id`.
- **Citations:** all 75 cited evidence IDs are evidence files (never an activity or audit ID), and none was captured after the case's `audit_at`. The most cited file is `evidence_92` (36 times).
- **Per-record pattern:** every record follows one of two shapes:
  - already fine → one verification step;
  - not fine → finding, then fix, then verification.
- **JSON style:** compact. 24 rows list keys in schema order (`step_no, phase, …`); 8 rows list them alphabetically. The grader ignores key order.

### 1.4 `user_identities.csv`: who each person is today

**Columns:** `user_id, name, email, phone, email_hash, updated_at`

- **Rows:** 850 people, `user_01` to `user_850`. User IDs and emails are all unique, and no field is blank.
- **Email hash:** `email_hash` is always the SHA-256 of the lowercased email (true for 850 of 850).
- **Email domains** (7): gmail.com 354, outlook.com 142, yahoo.com 72, proton.me 72, zoho.com 70, icloud.com 70, fastmail.com 70.
- **Phones:** 16 country prefixes. The most common, with 64–66 people each, are +34, +46, +91, +55, +971, +27, +233, +81, +82, +33, +48 and +234. Then +1 and +44 (33 each), and +212 and +40 (1 each).
- **Last updated:** between 22 Jun 2023 and 30 Aug 2026.
- **Names are not unique:** three names belong to two people each, "Gabriel Costa", "Mateo Ríos" and "Thandiwe Ndlovu". A name alone can never identify a person.

### 1.5 `user_audit_log.csv`: how identities changed over time

**Columns:** `audit_id, user_id, event_time, field_name, old_value, new_value`

- **Rows:** 306 changes affecting 196 people, made between 14 Dec 2024 and 3 Jun 2026.
- **What changed:** email 107, email_hash 107, phone 92. Names never change.
- **Email and hash change together:** every email change has a matching hash change at the same moment (107 of 107).
- **Who changed email:** 105 people changed their email, and 2 of them changed it twice. For 105 of the 107 email changes, the new value is the person's current email; the other 2 are middle steps in a two-step change.
- **Changes per person:** 91 people have 1 change, 102 have 2, 1 has 3, 2 have 4.

**Why it matters:** an old email or old hash can still sit in older systems. For example, one log line identifies a record only by the hash of `user_12`'s *previous* email.

### 1.6 `system_activity.csv`: what the systems say they did

**Columns:** `activity_id, event_time, event_type, source_system, target_system, resource_id, reported_result, details`

- **Size:** 4,011 events, from 4 Jan 2026 to 1 Oct 2026, covering 3,948 different resources. No resource has more than 2 events.
- **Not in time order:** the last 12 rows (`activity_4000`–`activity_4011`) are dated between Feb and Sep but sit at the end of the file.
- **Results claimed:** SUCCESS 2,914, FAILED 721, PARTIAL 376.
- **Event types:**

  | Event type | Total | SUCCESS / FAILED / PARTIAL |
  |---|---|---|
  | DELETE_RECORD | 890 | 302 / 368 / 220 |
  | UPDATE_RECORD | 672 | 587 / 53 / 32 |
  | SYNC_RECORD | 447 | 381 / 20 / 46 |
  | INDEX_RECORD | 400 | 348 / 32 / 20 |
  | SEND_EMAIL | 368 | 333 / 21 / 14 |
  | REFRESH_CACHE | 284 | 246 / 22 / 16 |
  | RETAIN_RECORD | 206 | 185 / 15 / 6 |
  | ANONYMIZE_RECORD | 204 | 132 / 64 / 8 |
  | REDACT_RECORD | 173 | 47 / 117 / 9 |
  | CREATE_EXPORT | 155 | 141 / 9 / 5 |
  | CHECK_DATA_FLOW | 89 | all SUCCESS |
  | DISABLE_DATA_FLOW, BLOCK_RESTORE | 36 each | all SUCCESS |
  | CHECK_RESTORE_ACCESS | 28 | all SUCCESS |
  | LINK_RESOURCE_IDENTITY | 12 | all SUCCESS |
  | CLASSIFY_REQUEST | 10 | all SUCCESS |
  | RESTORE_BACKUP | 1 | SUCCESS |

- **People are almost never named.** Only 12 of 4,011 rows contain an email or user ID: the `LINK_RESOURCE_IDENTITY` rows. Every other row mentions only resource IDs, so activity connects to a person only through resource IDs found in the evidence.
- **The `details` text is templated** (about 36 sentence patterns). The ones that matter most:
  - "Connector status check confirmed that data flow … remains enabled." (89): a live sync.
  - "Restore-access check confirmed that archive … can still be restored." (28): a restore risk.
  - "Active connector repopulated downstream resource …" (31) and "Restore job repopulated downstream resource search_4882." (1): deleted data came back.
  - "Privacy intake classified request for account_… as non-deletion" (10).
  - "Data catalog linked <resource> to <email> and <user_id>." (12): the only bridge from screenshots and PDFs to people (see 2.5).
- **Remember:** results such as SUCCESS are claims. The task says to prove the state from evidence.

### 1.7 `deletion_rules.csv`: what the policy requires

| Rule | Data category | Condition | Action |
|---|---|---|---|
| rule_01 | USER_PROFILE | Always | DELETE |
| rule_02 | SECONDARY_IDENTIFIER | Always | DELETE |
| rule_03 | SEARCH_INDEX_ENTRY | Always | DELETE |
| rule_04 | CACHE_ENTRY | Always | DELETE |
| rule_05 | SUPPORT_MESSAGE | Conversation contains shared customer, agent, or order context that must be retained | REDACT |
| rule_06 | SUPPORT_ATTACHMENT | Attachment belongs only to the deletion subject | DELETE |
| rule_07 | ANALYTICS_PROFILE | Record is still attributable to a user | ANONYMIZE |
| rule_08 | PRODUCT_TELEMETRY | Record is still attributable to a user | ANONYMIZE |
| rule_09 | USER_EXPORT | Always | DELETE |
| rule_10 | MARKETING_PROFILE | Always | DELETE |
| rule_11 | EMAIL_DELIVERY_LOG | Log contains a user email address | REDACT |
| rule_12 | SECURITY_AUDIT_LOG | Record is within the supplied retention period | RETAIN |
| rule_13 | LEGAL_HOLD_RECORD | A supplied legal hold is active | RETAIN |
| rule_14 | BACKUP_ARCHIVE | Restore access can be blocked | RETAIN |
| rule_15 | ACCOUNT_TOMBSTONE | Account is deleted and only a stable internal user_id is needed… | REDACT |

In total: 7 DELETE, 3 REDACT, 2 ANONYMIZE, 3 RETAIN. There is **no rule for data flows (syncs)**; they are handled as recreation risks.

### 1.8 `evidence_files.csv`: the index of evidence

**Columns:** `file_id, file_path, origin, captured_at`

- **Rows:** 94 files. Every listed path exists, and there are no unlisted files on disk.
- **By folder:** records 29, logs 26, databases 24, images 11, documents 4.
- **Capture dates:** 29 Jan 2026 to 1 Oct 2026, with 26 files captured in September alone.
- **Origins:** 30 different labels, e.g. `central_observability_export`, `service_resource_export`, `customer_data_inventory_capture`.
- **The manifest never says which person a file is about.** You only find that by reading the file.

### 1.9 `output.csv`: the answer sheet

It contains only the header `case_id,deletion_status,steps_json`. The final answer goes in a new `output.csv` at the repo root.

---

## Part 2: The evidence files

### 2.1 `evidence/databases/`: 24 SQL snapshot files

These are SQLite-compatible text dumps that can be loaded with Python's built-in `sqlite3`. They come in three shapes.

**A. "Point-in-time snapshots" with a primary table and copies (11 files).**
- The header says when the file was captured and what kind of copy it holds.
- Each table has the columns `resource_id, origin_system, collection_name, record_json, updated_at`. The real content is a JSON document in `record_json`.

| Layout | Files | Tables |
|---|---|---|
| read_replica | 5 (`evidence_05, 10, 15, 20, 24`) | primary + read replica |
| regional_replicas | 3 (`evidence_29, 33, 38`) | primary + `replica_ap_south` + `replica_eu_west` |
| reporting_replica | 2 (`evidence_43, 47`) | primary + reporting replica |
| failover_replica | 1 (`evidence_52`) | primary + failover replica |

The 11 kinds of records inside `record_json`: customer profiles, search documents, analytics reports, support conversations, ticket attachments, export jobs, connector configurations, backup catalog, security audit events, retained order records (legal) and deleted-customer references (tombstones).

**B. Small typed snapshots (2 files).**
- `evidence_54`, `customer_accounts_failover_replica_20260902.sql`: a primary and a failover table of profiles. It includes people who are not in `user_identities.csv` (e.g. "Karim El-Masri", "Mateo Rios Gil").
- `evidence_57`, `customer_search_primary_20260910.sql`: 3 search documents for **Jonas Berg**, **Jonas Berglund** and **Joana Berg**, deliberately similar names.

**C. Inventory snapshots (11 files, `customer_data_inventory_*`, `evidence_63`–`evidence_93`).**
- No header comment. A `snapshot_scope` table lists which systems were captured and when.
- 8 typed tables: profiles, search documents, analytics profiles, support records, exports, connector subscriptions, backup restore entries and security audit records.
- 208 data rows in total, 17 to 26 per file. `evidence_90` holds **0 rows** and covers only `account_service`.

**Replica drift.** Comparing each copy with its primary:
- **13 records exist only in a copy, and all 13 are account profiles.**
- No copy is ever missing a record that the primary has, and no shared record has different values.

**Interpretation:** this is the "ghost" pattern from the story. The account was deleted from the primary, but a replica still holds it.

**Other details:**
- Emails sometimes appear in uppercase (e.g. `K.KOWALSKI36@GMAIL.COM`).
- "Subject" fields hold either a user ID (`user_259`) or an email.
- Anonymized analytics rows have every identifier set to null, and redacted tombstones have null name, email, phone and hash.
- `integrity_seal` fields look like hashes (`sha256:000…040d`) but are not personal data.

### 2.2 `evidence/records/`: 28 JSON files and 1 text file

| Family | Files | What's inside |
|---|---|---|
| Snapshot bundles (`customer_data_snapshot_bundle_*`) | 12 | A list of snapshots, each with **its own** capture time, a data store and records. 4–75 records per file. Includes "operational record history" lists with an explicit `record_type` and `result`. 3 files contain an **empty** snapshot (`evidence_07, 31, 51`), which proves something is absent. |
| Service resource exports (`service_resource_export_*`) | 11 | Lookups grouped by system: account profile lookups, analytics profiles, backup restore entries, exports, connectors, search queries, security audit records, support tickets. 23–36 items per file, each with its own `observed_at`, spread over the 2–3 hours before the export time. |
| Flat record lists with explicit result | 3 | `evidence_13` (39 records; named "connector_configurations" but mixing 6 record types), `evidence_36` export manifest (46 records, 9 record types), `evidence_50` security audit ledger (20 records). Each record has `system, resource_id, record_type, result, subject_reference`. |
| Collection dumps | 2 | `evidence_56` analytics report (2 records), `evidence_59` export jobs (2 records). |
| Shift handover (text) | 1 | `evidence_03`, 41 lines of `time \| system \| resource \| type \| result \| subject`, all at 1 Oct 2026 12:00. Some subjects are uppercase emails. |

**Result words used across these files:** FOUND, NOT_FOUND, IDENTIFIABLE, ANONYMIZED, REDACTED, ACTIVE, DISABLED, BLOCKED.

### 2.3 `evidence/logs/`: 26 log files (2,211 lines)

There are 7 distinct line styles. All describe the same kinds of events.

| Style | Files | Example start of a line |
|---|---|---|
| logfmt `key=value` | 14 (2 service events, 11 resource-lookup traces, 1 support replica) | `2026-09-15T08:51:45Z level=DEBUG service=search_service … event=index_query_started match_key="…"` |
| JSON after the timestamp | 2 | `2026-09-10T08:56:17Z {"severity":"DEBUG","service":"account_service",…}` |
| Syslog (RFC 5424) | 2 | `2026-08-22T10:51:11Z <15>1 node-1 account_service tr-4973247 - - level=DEBUG …` |
| Bracketed trace | 2 | `2026-07-28T05:51:18Z [DEBUG] [account_service] [tr-6074163] profile_lookup_started: …` |
| Pipe-delimited | 2 | `2026-07-03T07:51:32Z \| DEBUG \| security_service \| tr-5156005 \| audit_record_read_completed \| …` |
| Cloud runtime (nested, escaped quotes) | 2 | `2026-06-08T06:52:09Z requestId=tr-1108860 … message="… resource_id=\"search_021\" …"` |
| Record lookup with explicit result | 2 | `… event=record_lookup system="account_service" resource_id="account_8473" record_type="USER_PROFILE" result="FOUND" subject_reference="…"` |

**How a typical lookup reads:**
1. A `…_started` line holds the search key (an email, phone, user ID or hash).
2. A few unrelated lines follow (retries, cache misses, DNS refreshes).
3. A `…_completed` line holds the `resource_id` and the outcome (`rows=0` / `http_status=404` means not found, `rows=1` means found, `enabled=true`, `restore_allowed=true`, and so on).

The started and completed lines share a **trace ID** and are interleaved with other traces, so they must be joined by that ID.

**Notable facts:**
- **Times can predate the capture by days.** No log line is timestamped after its file's capture time, but several files cover several days, e.g. `evidence_06` (10–18 Sep) and `evidence_44` (22 Feb–2 Mar). So a line can be from before a case's audit time even when the file was captured after it.
- **The biggest log is mostly unrelated people.** `evidence_53` (`cloud_runtime_export_af_south_20260930.log`) contains **300 profile lookups** of accounts with 5-digit IDs (e.g. `account_66777`), a format found nowhere else. Every email in it is a known user's current email, but **none of those 300 people has a request**. **Interpretation:** bulk noise that a careless search would trip over.
- **Redaction marker:** redacted support messages appear as `message_text="[REDACTED]"`.
- **Old-hash link:** one line (`contact_lookup_completed` for `analytics_9914`, with `email_opt_in=true`) identifies the person only by `correlation_key = 44a65154…`, the hash of `user_12`'s previous email.

### 2.4 `evidence/documents/`: 4 PDF files

All four have extractable text (checked with `pdftotext` and `pypdf`); no OCR is needed. Each has 1–2 pages.

| File | ID | Captured | What it contains |
|---|---|---|---|
| `account_history_report_20260718.pdf` | evidence_18 | 18 Jul 2026 | A customer statement `NST-2026-0718-1842` for **Ingrid Dahl**, `user_18`, `ingrid.dahl@outlook.com`: order `ORD-20261260` (smart desk lamp), card ending 4821, a delivery address in Trondheim. |
| `delivery_correction_attachment.pdf` | evidence_58 | 12 Sep 2026 | A support attachment for ticket `CS-4827` / `support_001`: **Priya Raman**, `priya.raman@gmail.com`, `+91 98765 73142` (matching `user_07`'s record), order `ORD-20264827`, a request to change an apartment number. |
| `legal_retention_register_20260529.pdf` | evidence_27 | 29 May 2026 | A register of 6 disputed orders for users `user_08, user_12, user_17, user_21, user_24, user_29`, each "Retain until … 2033". It has no resource IDs. |
| `security_retention_extract.pdf` | evidence_55 | 5 Sep 2026 | A signed compliance record `audit_record_003` for `user_03`: "Account closure recorded" on 25 Aug 2026 04:03 UTC, retain until 25 Aug 2027. |

### 2.5 `evidence/images/`: 11 PNG screenshots

All 11 are clean screenshots of Northstar Shop admin tools. They carry real facts (resource IDs, emails, statuses), but only an image-reading model can extract them.

| ID | Screen | What it shows |
|---|---|---|
| evidence_04 | Customer Operations | Profile of **Haruka Sato**, `user_11`, `account_5762`, `haruka.s1838@zoho.com`, `+81 90 7610 2507`; 2 orders; support ticket `support_9538`. |
| evidence_09 | Search Console | A search for `g.silva76@gmail.com` returns 2 hits: `search_428` (Gabriel Silva, `user_13`) and `search_1874` (`g.silva76.jr@gmail.com`, **not a known user**, a lookalike). |
| evidence_14 | Connector management | 4 connectors: `data_flow_1121` (selector `jain.aditi@gmail.com`, continuous sync, healthy) and `data_flow_9846` (`lucia-torres@gmail.com`), plus 2 non-personal flows. |
| evidence_19 | Pulse Analytics | Analytics profile of **Omar Darwish**, `user_20`, `analytics_7438`, `darwish.omar@icloud.com`, with 90 days of activity. |
| evidence_23 | Customer Download Center | 4 export jobs; `export_1260` belongs to `ingrid.dahl@outlook.com` (Ready, expires 25 Jun 2026). The other 3 owners are staff addresses (`@northstar.shop`). |
| evidence_28 | Helpline Console | Ticket `CS-5034` / `support_1034` from **Kofi Agyeman**, `kagyeman8947@fastmail.com`. |
| evidence_32 | Vault Recovery Manager | 4 backup archives; `backup_9287` belongs to `areed1433@gmail.com` (Amelia Reed, `user_16`), retention end 26 Jul 2026. |
| evidence_37 | Sentinel (security) | 6 signed account events for `user_03` (`audit_record_001`–`006`), including "Account closure recorded" on 25 Aug 04:03. |
| evidence_42 | Customer Reference Registry | `user_17` marked "Retained reference", with 4 linked records (`ORD-20263245`, `CS-3245`, `CS-3378`, `FIN-7742`). No name or email is shown. |
| evidence_46 | Object storage browser | Bucket `northstar-customer-data`, folder `support-attachments/2026/02`, 8 files including `delivery_confirmation_19.pdf`. No person is shown. |
| evidence_60 | Audience Operations (marketing) | `marketing_001` showing "Soo-jin Park", `sj.park@naver.com`, `+82 10-5837-9451`, email opt-in **Yes**. |

**How screenshots and PDFs connect to people.**
- Twelve `LINK_RESOURCE_IDENTITY` activity rows link one resource each to an email and user ID. They cover all 11 screenshots plus the statement PDF (`evidence_18`). Each row is timestamped exactly **7 minutes 1 second** after its file's capture time (e.g. `evidence_04` captured 26 Sep 11:00:00 → `activity_4000` at 11:07:01 links `account_5762` to `user_11`).
- `activity_4007` confirms `object_19` (`delivery_confirmation_19.pdf`) "belongs only to" `user_07`.
- `activity_4011` links `tombstone_17` to `user_17`.
- **Each linked person has one evaluation case audited about one day after the capture:** `case_33`, 34, 35, 39, 45, 46, 50, 52, 53, 55, 61 and 63. **Interpretation:** these 12 cases depend on reading an image or PDF.

**One conflict.** The marketing screenshot (`evidence_60`) shows "Soo-jin Park / `sj.park@naver.com`", but **no such person exists** in the identity files. `activity_4008` links `marketing_001` to `kwame-mensah@proton.me` / `user_09` (Kwame Mensah, `case_63`). The two sources disagree, and this case needs careful handling.

---

## Part 3: Key takeaways

1. **The claimed status says little.** In the samples, 11 of 17 "COMPLETE" claims were really incomplete, and 3 "NOT_STARTED" cases were already complete.
2. **Identity history matters.** 100 of 518 requests use an old email, and some evidence names a person only by an old hash.
3. **Use exact matching only.** Names repeat, and lookalikes exist (`g.silva76.jr@…`, Jonas Berg/Berglund, Mateo Rios Gil vs Mateo Ríos).
4. **Ghost copies live in replicas.** All 13 drift differences are account records that exist only in a copy.
5. **There are two different times:** a file's capture time (decides whether it may be cited) and each record's own time (decides its state at the audit). Log files can span several days.
6. **Absence must be proven.** Empty snapshots and full table listings are the evidence that something is gone.
7. **Activity rows are claims without names.** They connect to people only through resource IDs, except the 12 link rows.
8. **Most of each file is about other people.** Files are shared across many people, and `evidence_53` alone has 300 lookups of people who have no case.
9. **12 cases need image or PDF reading,** and one of them (`case_63`) involves a conflicting screenshot.
