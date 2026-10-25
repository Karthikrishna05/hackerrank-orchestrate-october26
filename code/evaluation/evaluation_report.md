# Evaluation Report

All metrics come from `ghostaudit.scorer`, run on the 32 public samples
(`dataset/sample_output.csv`) with the same pipeline and settings that produced the submitted
`output.csv` (`python3 code/main.py --intent rules`).

## Case-level status accuracy
**90.62% (29/32).** The 3 misses (`case_01`, `case_02`, `case_04`) are hand-made samples whose
only record is an account ID that appears nowhere in participant evidence (see Failures), so they
fall back to the no-evidence row.

## Step-level and field-level accuracy
| Metric | Value |
|---|---|
| Step precision / recall / F1 | **100.00% / 85.49% / 92.18%** (TP 165, predicted 165, gold 193) |
| The same, ignoring evidence_id | 100.00% / 85.49% / 92.18% |
| Field accuracy, all fields (position-aligned) | 72.88% |
| Per field | phase 85.49%, system_id 63.73%, resource_id 63.73%, operation 69.43%, state 69.43%, evidence_id 85.49% |

Every predicted step is correct. The field and sequence scores are lower only because missing
steps shift the positions of later ones.

## Sequence accuracy
| Metric | Value |
|---|---|
| Exact sequence match | 34.38% (11/32) |
| Mean normalised Levenshtein similarity | 75.57% |

## Reachability ceiling (why recall is 85%)
| How the gold record can be reached | Records | Steps |
|---|---|---|
| Linked by an identifier (email, phone, hash, user_id) | 83 | 165 |
| In evidence, but with no identifier | 11 | 11 |
| Only in activity rows | 14 | 14 |
| Nowhere in participant data | 3 | 3 |

The engine reaches **all 165** identifier-linked steps. Every finding in gold sits on an
identifier-linked record, so statuses are decided from those records. Identifier-free records
are added only when unambiguous (owner decision); in sample tests, workflow-timing matching
recovered only about 5 of 20.

## Custom evaluation cases (written by us, run as unit tests in `code/tests/`)
| # | Case | Expected | Result |
|---|---|---|---|
| 1 | A non-deletion request in each of 6 languages, plus negation ("I am not asking for deletion"), and "verify my deletion" | NOT_APPLICABLE for non-deletion; verify-style counts as deletion | Pass (`test_intent_rules.py`) |
| 2 | Evidence dated after `audit_at` is added to a case | Output unchanged | Pass (metamorphic test, `test_engine.py`) |
| 3 | An account present only in a replica store (a ghost copy) | INCOMPLETE with REPORT_REMAINING_RECORD | Pass (`test_engine.py`) |
| 4 | A record seen present before the audit in a file exported after it | Finding cites the failed deletion activity row (decision 6) | Pass; real case `case_126` |
| 5 | An already-deleted record that belongs to the person's earlier request | Skipped (decision 5) | Pass; sample `case_17` now matches gold exactly |
| 6 | An email held by two users | A safe fallback row instead of a crash | Pass (`test_engine.py`) |
| 7 | No-hardcoding guard | No literal case, evidence or activity IDs in product code | Pass |

The full suite runs **1,021 tests, all passing**.

## Failures found and improvements made
| Found | Fix | Effect |
|---|---|---|
| A record from the person's earlier request added as a wrong extra step (sample `case_17`) | Owner decision 5 | Step precision 99.4% → **100%** |
| A present record dropped because its only sighting was in a late-exported file (`case_126`) | Decision 6 | Now INCOMPLETE, citing the failed deletion row |
| Pipe-style log levels kept "`\| `"; a pipe inside a value broke a line | Parser fix | Clean levels on 231 lines |
| The absence proof could fall back to an older listing that contradicts newer evidence | Index fix | Absence is never claimed against newer evidence |
| A blank resource ID linked unrelated PDF facts | Index fix plus a regression test | Cleaner linking |
| A citation could show a state different from the decided one | Engine fix (decision 20) | Citations always match the decided state |
| Endless retry when the server says "retry after 0 seconds" | Growing waits, a cap, and a fallback model | Bounded: 4 tries + 4 on the fallback |
| Keyword intent false refusals ("I don't want to keep my account") | General negation patterns | Intent correct on all 550 texts |
| 3 samples unreachable (records absent from participant data) | Accepted as the ceiling; no invented IDs | Documented |

Intent classification on all 550 texts (518 evaluation + 32 sample) gives exactly the 10
non-deletion requests (`case_533`–`case_542`). This agrees with the 10 classification rows in
the activity log, though only one of those rows links to its person by the audit date.
