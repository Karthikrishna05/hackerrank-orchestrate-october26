# Usage Report: Final Full-Dataset Run

The submitted `output.csv` was produced by `python3 code/main.py --intent rules` over all 518
requests. That run used the deterministic engine with **keyword-rule intent** and made **no
model calls**. The OpenRouter key was configured, but it was not visible in the build session,
since new secrets only reach new sessions. The AI steps are built and tested (1,021 passing tests
with a local fake server) and run with `python -m ghostaudit.ai_cli intent|vision`.

| Provider | Model | Role | Calls | Input tokens | Output tokens | Total tokens | Cost |
|---|---|---|---|---|---|---|---|
| OpenRouter (free) | `google/gemma-4-31b-it:free` | Intent (main) | 0 | 0 | 0 | 0 | $0.00 |
| OpenRouter (free) | `nvidia/nemotron-3-super-120b-a12b:free` | Intent (fallback) | 0 | 0 | 0 | 0 | $0.00 |
| OpenRouter (free) | `nvidia/nemotron-3.5-super-vl:free` | Screenshots (main) | 0 | 0 | 0 | 0 | $0.00 |
| OpenRouter (free) | `google/gemma-4-31b-it:free` | Screenshots (fallback) | 0 | 0 | 0 | 0 | $0.00 |
| **Overall** | | | **0** | **0** | **0** | **0** | **$0.00** |

- **Per request:** 0 tokens on average (0 / 518) and $0.00 estimated cost.
- **Runtime:** about 1–2 s for all 518 cases. Parsing all 94 evidence files takes about 0.5 s.
- **Planned AI usage, when run:** about 41 intent calls (batches of 10 distinct texts) and 11
  screenshot calls. Free models cost $0. Tokens and the actual model are logged per attempt in
  `code/cache/usage.jsonl` (never the key or prompt text), from which this table is filled.
