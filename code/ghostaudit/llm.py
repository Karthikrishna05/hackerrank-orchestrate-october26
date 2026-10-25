"""OpenRouter chat client (stdlib only): retries, model fallback, defensive JSON parsing, per-attempt usage log.

Settings come from the environment only: `OPENROUTER_API_KEY` (never logged, printed or stored),
`OPENROUTER_TEXT_MODEL` / `OPENROUTER_VISION_MODEL` (defaults: the pinned free models below),
`OPENROUTER_TEXT_FALLBACK_MODEL` / `OPENROUTER_VISION_FALLBACK_MODEL` (defaults: the fallbacks below) and
`OPENROUTER_BASE_URL` (default `https://openrouter.ai/api/v1`, overridable for offline tests).
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from .paths import repo_root

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_TEXT_MODEL = "google/gemma-4-31b-it:free"  # chosen by the project owner
DEFAULT_VISION_MODEL = "nvidia/nemotron-3.5-super-vl:free"  # chosen by the project owner
# Owner decision 29: the model each task kind switches to once its main model's tries are spent.
DEFAULT_TEXT_FALLBACK_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"
DEFAULT_VISION_FALLBACK_MODEL = "google/gemma-4-31b-it:free"
TASK_KINDS = ("text", "vision")
ATTEMPTS_PER_MODEL = 4  # owner decision 30: first try + 3 retries per model, so 8 at most with the fallback
APP_TITLE = "ghostaudit deletion audit"
AUTH_STATUS = frozenset({401, 402})  # bad key, no credit: fatal at once, no fallback (owner decision 31)
FATAL_STATUS = AUTH_STATUS | {403}  # forbidden
IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)
_THINK_RE = re.compile(r"<think>.*?</think>", re.S)
# OpenRouter's "model unavailable" errors ("No endpoints found for ..."); owner decision 31 retries them.
_UNAVAILABLE_RE = re.compile(r"\bno endpoints\b|\bmodel\b[^.]{0,40}\b(?:unavailable|not available)\b", re.I)


class LLMError(RuntimeError):
    """No valid model output. `fatal`: later calls will fail too; `rate_limited`: retry budget spent on 429s;
    `status`: the HTTP status of the failing response, when there was one."""

    def __init__(self, message: str, *, fatal: bool = False, rate_limited: bool = False, status: int | None = None):
        super().__init__(message)
        self.fatal = fatal
        self.rate_limited = rate_limited
        self.status = status


@dataclass(frozen=True)
class UsageRecord:
    """Totals for one chat_json call (every attempt that got a model response)."""

    task: str
    requested_model: str  # the model asked for in the successful attempt (the fallback, once switched)
    model: str | None  # actual model reported by the last response (the requested one if it names none)
    calls: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: int


def cache_root() -> Path:
    """`code/cache/`; created lazily by the first write, never by a read."""
    return repo_root() / "code" / "cache"


def usage_log_path() -> Path:
    return cache_root() / "usage.jsonl"


def write_json(path: Path, obj) -> None:
    """Stable JSON (sorted keys, UTF-8, indent 1) written atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def image_part(path: str | Path) -> dict:
    """OpenAI-style image content part with a base64 data URL."""
    p = Path(path)
    mime = IMAGE_MIME.get(p.suffix.lower())
    if mime is None:
        raise ValueError(f"unsupported image type: {p.suffix}")
    data = base64.b64encode(p.read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}


def parse_json_reply(content) -> dict:
    """First JSON object in a reply: think blocks and code fences stripped, surrounding prose ignored."""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("empty reply")
    text = _THINK_RE.sub("", content)
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in reply")
    try:
        obj, _ = json.JSONDecoder().raw_decode(text, start)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc.msg} at char {exc.pos}") from None
    return obj


def _retryable(status: int | None, error: str) -> bool:
    """Timeouts, 408/429, 5xx and "model unavailable" (404 or a no-endpoints message) are worth retrying."""
    if status in FATAL_STATUS:
        return False
    return (status is None or status in (404, 408, 429) or status >= 500
            or bool(_UNAVAILABLE_RE.search(error)))


def _retry_after(headers) -> float | None:
    """Seconds from a Retry-After header (delta seconds or HTTP date), if any."""
    value = headers.get("Retry-After") if headers is not None else None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return None


class OpenRouterClient:
    """Chat-completions client for OpenRouter's OpenAI-compatible endpoint."""

    def __init__(self, *, timeout: float = 120.0, max_wait_s: float = 60.0, base_delay_s: float = 2.0,
                 sleep: Callable[[float], None] = time.sleep):
        self._key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        self.base_url = (os.environ.get("OPENROUTER_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.text_model = os.environ.get("OPENROUTER_TEXT_MODEL") or DEFAULT_TEXT_MODEL
        self.vision_model = os.environ.get("OPENROUTER_VISION_MODEL") or DEFAULT_VISION_MODEL
        self.text_fallback_model = os.environ.get("OPENROUTER_TEXT_FALLBACK_MODEL") or DEFAULT_TEXT_FALLBACK_MODEL
        self.vision_fallback_model = (os.environ.get("OPENROUTER_VISION_FALLBACK_MODEL")
                                      or DEFAULT_VISION_FALLBACK_MODEL)
        self.timeout = timeout
        self.max_wait_s = max_wait_s
        self.base_delay_s = base_delay_s
        self._sleep = sleep

    def __repr__(self) -> str:  # never shows the key
        return f"OpenRouterClient(base_url={self.base_url!r}, key={'set' if self._key else 'missing'})"

    def has_key(self) -> bool:
        return bool(self._key)

    def reachable(self, timeout: float = 5.0) -> tuple[bool, str]:
        """Whether the base URL answers at all (any HTTP status counts); no key is sent."""
        try:
            with urllib.request.urlopen(self.base_url + "/models", timeout=timeout) as resp:
                resp.read(1)
                return True, f"HTTP {resp.status}"
        except urllib.error.HTTPError as exc:
            return True, f"HTTP {exc.code}"
        except (urllib.error.URLError, OSError) as exc:
            reason = str(getattr(exc, "reason", exc))
            hint = " (the environment's network policy blocks this host)" if "403" in reason else ""
            return False, reason + hint

    def available(self) -> tuple[bool, str]:
        """(True, detail) when a key is set and the base URL is reachable."""
        if not self._key:
            return False, "OPENROUTER_API_KEY is not set"
        ok, detail = self.reachable()
        return ok, ("ready: " if ok else "unreachable: ") + f"{self.base_url} {detail}"

    def chat_json(self, messages: list[dict], *, model: str | None = None, task: str,
                  schema_check: Callable[[dict], None] | None = None,
                  kind: str = "text") -> tuple[dict, UsageRecord]:
        """One JSON reply at temperature 0 from `model` (default: the main model of `kind`, text or vision).

        Timeouts, 429, 5xx and "model unavailable" get ATTEMPTS_PER_MODEL tries per model, each wait at
        least the growing backoff (2 s, 4 s, 8 s with the default base) and at least Retry-After; then
        `kind`'s fallback model gets the same tries, and LLMError follows (owner decisions 16, 29-31). The
        total wait stays capped at `max_wait_s`. 401/402 are fatal at once. Invalid JSON or a failed
        `schema_check` (raises ValueError) gets one retry on the same model with the error appended."""
        if kind not in TASK_KINDS:
            raise ValueError(f"kind must be one of {TASK_KINDS}, not {kind!r}")
        if not self._key:
            raise LLMError("OPENROUTER_API_KEY is not set", fatal=True)
        vision = kind == "vision"
        main = model or (self.vision_model if vision else self.text_model)
        fallback = self.vision_fallback_model if vision else self.text_fallback_model
        models = [main] + ([fallback] if fallback and fallback != main else [])
        payload = {"model": main, "messages": list(messages), "temperature": 0,
                   "response_format": {"type": "json_object"}}
        attempt = failures = calls = latency_total = 0  # failures: retryable failures of the current model
        waited = 0.0
        tokens = [0, 0, 0]
        revalidated = False
        while True:
            attempt += 1
            model = payload["model"]
            status, headers, body, error, latency = self._send(payload)
            latency_total += latency
            log = dict(task=task, requested_model=model, latency_ms=latency, attempt=attempt)
            if status is None and error.startswith("network:"):
                self._log(**log, ok=False, error=error)
                raise LLMError(error, fatal=True)
            if status == 400 and "response_format" in payload:
                self._log(**log, ok=False, error=error)
                del payload["response_format"]  # some free models reject it; ask for JSON in text only
                continue
            if _retryable(status, error):
                self._log(**log, ok=False, error=error)
                failures += 1
                if failures >= ATTEMPTS_PER_MODEL:
                    if model != models[-1]:  # owner decision 31: switch once this model's tries are spent
                        payload["model"], failures = models[models.index(model) + 1], 0
                        continue
                    raise LLMError(f"gave up after {attempt} attempts on {', '.join(models)} ({error})",
                                   rate_limited=status == 429, status=status)
                # Owner decision 16: never shorter than the growing backoff, whatever Retry-After says.
                delay = max(_retry_after(headers) or 0.0, self.base_delay_s * 2 ** (failures - 1))
                if waited + delay > self.max_wait_s:
                    raise LLMError(f"gave up after {attempt} attempts ({error})", rate_limited=status == 429,
                                   status=status)
                self._sleep(delay)
                waited += delay
                continue
            if status != 200:
                self._log(**log, ok=False, error=error)
                raise LLMError(error, fatal=status in FATAL_STATUS, status=status)
            usage = body.get("usage") or {}
            actual = body.get("model") or model  # owner decision 28: a reply naming no model ran the requested one
            calls += 1
            for i, k in enumerate(("prompt_tokens", "completion_tokens", "total_tokens")):
                tokens[i] += usage.get(k) or 0
            log.update(model=actual, usage=usage, provider=body.get("provider"))
            content = None
            try:
                content = body["choices"][0]["message"]["content"]
                data = parse_json_reply(content)
                if not isinstance(data, dict):
                    raise ValueError("reply is not a JSON object")
                if schema_check:
                    schema_check(data)
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                problem = f"invalid output: {exc}"[:300]
                self._log(**log, ok=False, error=problem)
                if revalidated:
                    raise LLMError(f"{problem} (after one retry)") from None
                revalidated = True
                payload["messages"] = list(messages) + [
                    {"role": "assistant", "content": (content if isinstance(content, str) else "")[:4000]},
                    {"role": "user", "content": f"Your reply was invalid: {problem}. Reply again with only "
                                                "the JSON object in the requested format."}]
                continue
            self._log(**log, ok=True, error=None)
            return data, UsageRecord(task, model, actual, calls, *tokens, latency_total)

    image_part = staticmethod(image_part)

    # -- internals -------------------------------------------------------------------------------

    def _send(self, payload: dict):
        """POST once. Returns (status | None, headers, body dict, error text, latency_ms)."""
        req = urllib.request.Request(
            self.base_url + "/chat/completions", method="POST",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
                     "X-Title": APP_TITLE})
        t0 = time.monotonic()
        status, headers, body, error = None, None, {}, ""
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status, headers, raw = resp.status, resp.headers, resp.read()
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                status, error = None, "malformed response body"
            if isinstance(body, dict) and body.get("error") and not body.get("choices"):
                code = body["error"].get("code") if isinstance(body["error"], dict) else None
                status = code if isinstance(code, int) else 502
                error = f"HTTP {status}: {self._message(body)}"
        except urllib.error.HTTPError as exc:
            status, headers = exc.code, exc.headers
            try:
                body = json.loads(exc.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, OSError):
                body = {}
            error = f"HTTP {status}: {self._message(body)}"
        except ConnectionRefusedError as exc:
            error = f"network: {exc}"
        except (TimeoutError, ConnectionError, http.client.HTTPException) as exc:
            error = f"transient: {type(exc).__name__}"
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, ConnectionError)) and not isinstance(exc.reason, ConnectionRefusedError):
                error = f"transient: {type(exc.reason).__name__}"
            else:
                error = f"network: {exc.reason}"
        except OSError as exc:  # e.g. an SSL error while reading the body
            error = f"transient: {type(exc).__name__}"
        latency = int((time.monotonic() - t0) * 1000)
        return status, headers, body if isinstance(body, dict) else {}, self._scrub(error), latency

    @staticmethod
    def _message(body) -> str:
        err = body.get("error") if isinstance(body, dict) else None
        msg = err.get("message") if isinstance(err, dict) else err
        return str(msg or "no error message")[:200]

    def _scrub(self, text: str) -> str:
        return text.replace(self._key, "[REDACTED]") if self._key and text else text

    def _log(self, *, task, requested_model, latency_ms, attempt, ok, error, model=None, usage=None,
             provider=None) -> None:
        """Append one usage line per attempt. Never the prompt text, never the key."""
        usage = usage or {}
        rec = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "task": task,
               "requested_model": requested_model, "model": model, "provider": provider,
               "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
               "total_tokens": usage.get("total_tokens"), "latency_ms": latency_ms, "attempt": attempt,
               "ok": ok, "error": self._scrub(error) if error else None}
        path = usage_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n")
