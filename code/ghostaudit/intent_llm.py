"""A1: does a request text authorize deletion? Batched calls to a free OpenRouter model, cached per text.

Cache: `code/cache/intent/<sha256>.json`, one file per distinct text, valid only for the current
PROMPT_VERSION. Cache hits make no call; failed batches are left uncached and retried on the next run.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from . import llm
from .models import IntentResult
from .normalize import text_key

PROMPT_VERSION = "intent-v1"
REQUEST_TYPES = ("deletion", "deletion_verification", "access", "export", "correction",
                 "marketing_opt_out", "account_help", "other")
DELETION_TYPES = frozenset({"deletion", "deletion_verification"})
MAX_QUOTE = 160

SYSTEM_PROMPT = """\
You classify customer privacy requests sent to an online shop. For each request, decide whether \
the person authorizes deletion of their account or personal data.

authorizes_deletion = true when the person asks to delete, erase, close, remove or forget their \
account, profile or personal data (request_type "deletion"). This INCLUDES asking you to verify, \
check, audit or finish a deletion that was requested or reported earlier, or to remove copies \
left behind (request_type "deletion_verification").

authorizes_deletion = false for every other request:
- stop marketing, newsletters or promotional messages -> "marketing_opt_out"
- ask what data is held, or ask for a copy of it -> "access"
- download, export or port data -> "export"
- fix or update a name, phone, address or email address -> "correction"
- login, password or other help using the account -> "account_help"
- anything else (invoices, receipts, order questions, ...) -> "other"

Negation wins: if the person says not to delete, close or remove anything ("keep my account", \
"this is not a deletion request"), authorizes_deletion is false even when deletion words appear.
Requests may be written in English, Portuguese, Spanish, French, Japanese or Korean. Judge the \
meaning, not single keywords.

Reply with JSON only, no markdown:
{"results": [{"id": <input id>, "authorizes_deletion": <true|false>, "request_type": <one of \
the types above>, "language": <ISO 639-1 code>, "confidence": <0.0-1.0>, "quote": <the shortest \
phrase from the request, at most 12 words, that decides it>}]}
Return exactly one result for every input id. authorizes_deletion must be true exactly when \
request_type is "deletion" or "deletion_verification"."""

# Synthetic few-shot examples (written for this prompt, not taken from the dataset).
FEW_SHOT = [
    ("Quiero que borren mi cuenta y todos mis datos personales de forma definitiva.",
     True, "deletion", "es", "borren mi cuenta y todos mis datos personales"),
    ("You confirmed last month that my profile was erased, but I still get your 'welcome back' "
     "emails. Please check that nothing of mine is left.",
     True, "deletion_verification", "en", "check that nothing of mine is left"),
    ("탈퇴 처리가 정말 완료되었는지 확인하고, 남아 있는 개인정보가 있다면 모두 지워 주세요.",
     True, "deletion_verification", "ko", "남아 있는 개인정보가 있다면 모두 지워 주세요"),
    ("Unsubscribe me from the newsletter, but keep my account open. This is not a deletion request.",
     False, "marketing_opt_out", "en", "This is not a deletion request"),
    ("Please prepare a downloadable file with all my order history. I'm keeping the account.",
     False, "export", "en", "prepare a downloadable file with all my order history"),
    ("Gostaria de saber quais dados pessoais vocês guardam sobre mim. Não quero apagar nada.",
     False, "access", "pt", "Não quero apagar nada"),
    ("登録している住所が古いので更新してください。アカウントは削除しないでください。",
     False, "correction", "ja", "アカウントは削除しないでください"),
    ("Je n'arrive plus à me connecter : aidez-moi à réinitialiser mon mot de passe, sans fermer le compte.",
     False, "account_help", "fr", "réinitialiser mon mot de passe, sans fermer le compte"),
]


def cache_key(text: str) -> str:
    """SHA-256 hex digest of the stripped UTF-8 text (shared with engine.text_key, owner decision 26)."""
    return text_key(text)


def cache_path(key: str) -> Path:
    return llm.cache_root() / "intent" / f"{key}.json"


def _from_record(rec: dict) -> IntentResult:
    r = rec["result"]
    return IntentResult(authorizes_deletion=r["authorizes_deletion"], request_type=r["request_type"],
                        language=r["language"], confidence=r["confidence"],
                        source=f"llm:{rec.get('model') or rec.get('requested_model') or 'unknown'}",  # decision 28
                        quote=r.get("quote", ""))


def cached(text: str) -> IntentResult | None:
    """The cached result for `text` under the current PROMPT_VERSION, or None."""
    path = cache_path(cache_key(text))
    if not path.is_file():
        return None
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
        if rec.get("prompt_version") != PROMPT_VERSION:
            return None
        return _from_record(rec)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _batch_prompt(texts: list[str]) -> str:
    items = [{"id": i, "text": t} for i, t in enumerate(texts, 1)]
    return ("Classify each request below. Return {\"results\": [...]} with exactly one entry per id.\n"
            + json.dumps(items, ensure_ascii=False, indent=1))


def build_messages(texts: list[str]) -> list[dict]:
    """System definition, one synthetic few-shot exchange, then the batch."""
    shots = [{"id": i, "authorizes_deletion": ok, "request_type": rt, "language": lang,
              "confidence": 0.95, "quote": quote}
             for i, (_, ok, rt, lang, quote) in enumerate(FEW_SHOT, 1)]
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _batch_prompt([t for t, *_ in FEW_SHOT])},
        {"role": "assistant", "content": json.dumps({"results": shots}, ensure_ascii=False)},
        {"role": "user", "content": _batch_prompt(texts)},
    ]


def validate(data: dict, n: int) -> dict[int, dict]:
    """Normalised results by id (1..n); raises ValueError naming the first problem."""
    results = data.get("results")
    if not isinstance(results, list):
        raise ValueError('missing "results" list')
    out: dict[int, dict] = {}
    for pos, r in enumerate(results):
        if not isinstance(r, dict):
            raise ValueError(f"results[{pos}] is not an object")
        rid = r.get("id")
        if isinstance(rid, str) and rid.strip().isdigit():
            rid = int(rid)
        if not isinstance(rid, int) or isinstance(rid, bool) or not 1 <= rid <= n:
            raise ValueError(f"results[{pos}]: id {rid!r} is not one of 1..{n}")
        if rid in out:
            raise ValueError(f"id {rid} returned more than once")
        ok, rtype, conf = r.get("authorizes_deletion"), r.get("request_type"), r.get("confidence")
        lang = r.get("language")
        if not isinstance(ok, bool):
            raise ValueError(f"id {rid}: authorizes_deletion must be true or false")
        if rtype not in REQUEST_TYPES:
            raise ValueError(f"id {rid}: request_type must be one of {', '.join(REQUEST_TYPES)}")
        if ok != (rtype in DELETION_TYPES):
            raise ValueError(f"id {rid}: authorizes_deletion must be true exactly for deletion types")
        if not isinstance(conf, (int, float)) or isinstance(conf, bool) or not 0 <= conf <= 1:
            raise ValueError(f"id {rid}: confidence must be a number from 0 to 1")
        if not isinstance(lang, str) or not lang.strip():
            raise ValueError(f"id {rid}: language must be a non-empty string")
        quote = r.get("quote")
        out[rid] = {"authorizes_deletion": ok, "request_type": rtype, "language": lang.strip().lower()[:10],
                    "confidence": round(float(conf), 3),
                    "quote": quote.strip()[:MAX_QUOTE] if isinstance(quote, str) else ""}
    missing = sorted(set(range(1, n + 1)) - set(out))
    if missing:
        raise ValueError(f"ids missing from results: {missing}")
    return out


def classify_all(texts: Iterable[str], *, client: llm.OpenRouterClient | None = None, batch_size: int = 10,
                 force: bool = False, report: dict | None = None) -> dict[str, IntentResult]:
    """Results keyed by cache_key(text). Misses are sent in batches sorted by key and cached per text.
    A failed batch is skipped (reported in `report["failed"]`); a fatal or rate-limit error stops the run."""
    keyed = {cache_key(t): t.strip() for t in texts if t and t.strip()}
    results: dict[str, IntentResult] = {}
    misses = []
    for key in sorted(keyed):
        hit = None if force else cached(keyed[key])
        if hit is None:
            misses.append(key)
        else:
            results[key] = hit
    stats = {"texts": len(keyed), "cached": len(results), "classified": 0, "calls": 0, "failed": [],
             "errors": [], "stopped": None, "stopped_status": None}
    if misses:
        client = client or llm.OpenRouterClient()
        if not client.has_key():
            raise llm.LLMError("OPENROUTER_API_KEY is not set", fatal=True)
    for start in range(0, len(misses), batch_size):
        batch = misses[start:start + batch_size]
        batch_texts = [keyed[k] for k in batch]
        try:
            data, usage = client.chat_json(build_messages(batch_texts), model=client.text_model, task="intent",
                                           kind="text", schema_check=lambda d, n=len(batch): validate(d, n))
        except llm.LLMError as exc:
            stats["failed"].extend(batch)
            stats["errors"].append(str(exc))
            if exc.fatal or exc.rate_limited:
                stats["failed"].extend(misses[start + batch_size:])
                stats["stopped"], stats["stopped_status"] = str(exc), exc.status
                break
            continue
        stats["calls"] += usage.calls
        by_id = validate(data, len(batch))
        for i, key in enumerate(batch, 1):
            rec = {"cache_key": key, "prompt_version": PROMPT_VERSION, "requested_model": usage.requested_model,
                   "model": usage.model, "result": by_id[i]}
            llm.write_json(cache_path(key), rec)
            results[key] = _from_record(rec)
            stats["classified"] += 1
    if report is not None:
        report.update(stats)
    return results
