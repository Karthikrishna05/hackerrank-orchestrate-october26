# Usage Report: Final Full-Dataset Run

## The run that produced `output.csv`
The submitted `output.csv` (sha256 `e254d0e7…`) was produced by `python3 code/main.py --intent rules`
over all 518 requests: the deterministic engine with **keyword-rule intent** and **no model calls**.
Running `python3 code/main.py --intent auto` with the partial AI cache below gives the
**identical file** (same sha256), because every AI answer so far agrees with the keyword rules.

## AI intent run (session 2, partial, not used for `output.csv`)
`python -m ghostaudit.ai_cli intent` was run with OpenRouter free models. It was stopped by the
owner after about 20 minutes so the submission could go out on time. Totals come from
`code/cache/usage.jsonl` (one line per attempt; never the key or the prompt text).

| Provider | Requested model | Actual model | Role | Attempts | OK responses | Input tokens | Output tokens | Total tokens | Cost |
|---|---|---|---|---|---|---|---|---|---|
| OpenRouter (free) | `google/gemma-4-31b-it:free` | `google/gemma-4-31b-it:free` | Intent (main) | 105 | 2 | 3,433 | 990 | 4,423 | $0.00 |
| OpenRouter (free) | `nvidia/nemotron-3-super-120b-a12b:free` | none (HTTP 404) | Intent (configured fallback) | 16 | 0 | 0 | 0 | 0 | $0.00 |
| OpenRouter (free) | `nvidia/nemotron-3-ultra-550b-a55b:free` | `nvidia/nemotron-3-ultra-550b-a55b:free` | Intent (fallback, owner decision 38) | 27 | 17 | 48,018 | 30,907 | 78,925 | $0.00 |
| OpenRouter (free) | `nvidia/nemotron-3.5-super-vl:free` | — | Screenshots | 0 (not run) | 0 | 0 | 0 | 0 | $0.00 |
| **Overall** | | | | **148** | **19** | **51,451** | **31,897** | **83,348** | **$0.00** |

- **Per request:** 83,348 / 518 ≈ **161 tokens** on average; estimated cost **$0.00** (free models).
- **Failures:** 103 attempts were HTTP 429 on Gemma ("temporarily rate-limited upstream", a shared
  free pool). 16 were HTTP 404 on the configured fallback (NVIDIA-side "Function not found").
  10 Nemotron Ultra replies were invalid (malformed JSON or no `results` list); those batches stayed uncached for a later run.
  Failed attempts used no billed tokens.
- **Coverage:** 190 distinct texts are cached, covering 271 of 550 request rows (518 evaluation +
  32 sample). 4 were classified as non-deletion: `case_533`, `case_536`, `case_538`, `case_539`.
- **Agreement with the keyword baseline:** 0 disagreements on all 271 covered rows.
- **Not run:** the screenshot reader (11 images), stopped for time. Every screenshot-only record
  keeps the interim link-row fallback (owner decision 10).
- **Diagnostics not in the table:** 10 one-line "reply {ok:true}" probes were sent to check
  which free models were up. 3 succeeded, using 59 + 65 + 16 = 140 tokens.
- **Runtime of the final run:** about 1–2 s for all 518 cases.
