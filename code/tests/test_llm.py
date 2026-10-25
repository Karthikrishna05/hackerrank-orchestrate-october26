"""Tests for ghostaudit.llm (engine_plan.md §3): OpenRouter client, retries, JSON parsing, usage log.

No real API call is ever made. Requests go to a local fake server on 127.0.0.1 (FakeOpenRouter) or to a
mocked `urlopen`; the API key is a dummy set only inside each test; the cache root is a temp directory;
backoff sleeps are injected. Every model reply below is synthetic and lives only in this file.

The helpers (FakeOpenRouter, openrouter_env, TempCacheMixin, RepoCacheGuard, chat_body, ...) are shared by
test_intent_llm.py, test_vision.py and test_ai_cli.py.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import socket
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from ghostaudit import llm
from ghostaudit.paths import repo_root

# --------------------------------------------------------------------------------------------------
# Shared helpers (not TestCases, so importing them from other test modules adds no tests)
# --------------------------------------------------------------------------------------------------

DUMMY_KEY = "sk-or-v1-DUMMY-TEST-KEY-0000-not-a-real-key"
ENV_NAMES = ("OPENROUTER_API_KEY", "OPENROUTER_BASE_URL", "OPENROUTER_TEXT_MODEL", "OPENROUTER_VISION_MODEL")
SYNTH_MODEL = "synthetic/fake-model-a"  # what the fake server reports as the actual model
REPO_CACHE = repo_root() / "code" / "cache"


@contextlib.contextmanager
def openrouter_env(base_url: str | None = None, key: str | None = DUMMY_KEY, **overrides):
    """os.environ with only the given OpenRouter settings; everything is restored on exit."""
    with mock.patch.dict(os.environ):
        for name in ENV_NAMES:
            os.environ.pop(name, None)
        if key is not None:
            os.environ["OPENROUTER_API_KEY"] = key
        if base_url is not None:
            os.environ["OPENROUTER_BASE_URL"] = base_url
        os.environ.update(overrides)
        for name in ("no_proxy", "NO_PROXY"):  # the fake server must never be reached through a proxy
            os.environ[name] = ",".join(filter(None, [os.environ.get(name, ""), "127.0.0.1", "localhost"]))
        yield


def chat_body(content, *, model: str = SYNTH_MODEL, prompt_tokens: int = 11, completion_tokens: int = 7) -> dict:
    """A synthetic OpenAI-style chat-completions response body."""
    return {"id": "gen-synthetic-0001", "model": model, "provider": "SyntheticProvider",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                      "total_tokens": prompt_tokens + completion_tokens}}


def reply(content, status: int = 200, headers: dict | None = None, **kw):
    return status, headers or {}, chat_body(content, **kw)


def error_reply(status: int, message: str = "synthetic error", headers: dict | None = None):
    return status, headers or {}, {"error": {"code": status, "message": message}}


class FakeOpenRouter:
    """Local stand-in for OpenRouter on 127.0.0.1 (random port). Records every request.

    Replies to POST .../chat/completions come from `script` (a queue of (status, headers, body)) and,
    once it is empty, from `responder(request) -> (status, headers, body)`. GET .../models answers 200.
    """

    def __init__(self, responder=None, script=None):
        self.requests: list[dict] = []
        self.script = list(script or [])
        self.responder = responder or (lambda req: reply('{"ok": true}'))
        self._lock = threading.Lock()

    def __enter__(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output quiet
                pass

            def do_GET(self):
                server._handle(self, "GET")

            def do_POST(self):
                server._handle(self, "POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01},
                                       daemon=True)
        self.thread.start()
        self.root = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.url = self.root + "/api/v1"
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)

    def _handle(self, handler, method: str) -> None:
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            body = None
        req = {"method": method, "path": handler.path, "raw": raw, "body": body,
               "headers": {k.lower(): v for k, v in handler.headers.items()}}
        with self._lock:
            self.requests.append(req)
            if method == "GET":
                status, headers, payload = 200, {}, {"data": []}
            elif self.script:
                status, headers, payload = self.script.pop(0)
            else:
                status, headers, payload = self.responder(req)
        if isinstance(payload, bytes):
            data = payload
        elif isinstance(payload, str):
            data = payload.encode("utf-8")
        else:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler.send_response(status)
        for k, v in headers.items():
            handler.send_header(k, v)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    @property
    def chat_requests(self) -> list[dict]:
        return [r for r in self.requests if r["method"] == "POST" and r["path"].endswith("/chat/completions")]


def closed_port_url() -> str:
    """A base URL on 127.0.0.1 where nothing listens (connection refused)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}/api/v1"


class SleepRecorder:
    """Injected instead of time.sleep: records delays, never sleeps; optionally aborts a runaway loop."""

    def __init__(self, abort_after: int | None = None):
        self.calls: list[float] = []
        self.abort_after = abort_after

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self.abort_after is not None and len(self.calls) >= self.abort_after:
            raise RuntimeError(f"retry loop still running after {len(self.calls)} sleeps")


class TempCacheMixin:
    """Redirect every cache write (llm.cache_root and the vision cache path) to a temp directory."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(prefix="ghostaudit-test-cache-")
        self.addCleanup(tmp.cleanup)
        self.cache_dir = Path(tmp.name)
        patches = [mock.patch("ghostaudit.llm.cache_root", lambda: self.cache_dir),
                   mock.patch("ghostaudit.parsers.images.cache_path",
                              lambda file_id: self.cache_dir / "vision" / f"{file_id}.json")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def cache_files(self) -> list[str]:
        return sorted(str(p.relative_to(self.cache_dir)) for p in self.cache_dir.rglob("*") if p.is_file())

    def usage_lines(self) -> list[dict]:
        path = self.cache_dir / "usage.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def repo_cache_snapshot() -> tuple[bool, list[tuple[str, int, int]]]:
    """(code/cache exists, [(file, size, mtime_ns)]): detects any write into the repo's cache."""
    if not REPO_CACHE.exists():
        return False, []
    return True, sorted((str(p.relative_to(REPO_CACHE)), p.stat().st_size, p.stat().st_mtime_ns)
                        for p in REPO_CACHE.rglob("*") if p.is_file())


class RepoCacheGuard:
    """Mixin for a module's last TestCase: the repo's code/cache/ must be unchanged since import."""

    snapshot_at_import: tuple = ()

    def test_repo_code_cache_untouched(self):
        self.assertEqual(repo_cache_snapshot(), self.snapshot_at_import,
                         "a test wrote into the repository's code/cache/")


_SNAPSHOT_AT_IMPORT = repo_cache_snapshot()


def make_client(sleep=None, **kw) -> llm.OpenRouterClient:
    """A client built from the current (patched) environment with an injected sleep."""
    return llm.OpenRouterClient(sleep=sleep or SleepRecorder(), **kw)


USER_MSG = [{"role": "user", "content": "PROMPT-MARKER-7f3a classify this synthetic text"}]


# --------------------------------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------------------------------

class DefaultsAndSettingsTests(unittest.TestCase):

    def test_defaults_are_the_owner_chosen_free_models(self):
        self.assertEqual(llm.DEFAULT_TEXT_MODEL, "google/gemma-4-31b-it:free")
        self.assertEqual(llm.DEFAULT_VISION_MODEL, "nvidia/nemotron-3.5-super-vl:free")
        self.assertEqual(llm.DEFAULT_BASE_URL, "https://openrouter.ai/api/v1")

    def test_client_uses_defaults_without_env(self):
        with openrouter_env(key=None):
            c = llm.OpenRouterClient()
        self.assertEqual((c.text_model, c.vision_model, c.base_url),
                         (llm.DEFAULT_TEXT_MODEL, llm.DEFAULT_VISION_MODEL, llm.DEFAULT_BASE_URL))

    def test_env_vars_override_models_and_base_url(self):
        with openrouter_env(base_url="http://127.0.0.1:9/custom/v1/", OPENROUTER_TEXT_MODEL="openrouter/free",
                            OPENROUTER_VISION_MODEL="synthetic/vision-override:free"):
            c = llm.OpenRouterClient()
        self.assertEqual(c.text_model, "openrouter/free")
        self.assertEqual(c.vision_model, "synthetic/vision-override:free")
        self.assertEqual(c.base_url, "http://127.0.0.1:9/custom/v1")  # trailing slash stripped

    def test_empty_env_values_fall_back_to_defaults(self):
        with openrouter_env(OPENROUTER_TEXT_MODEL="", OPENROUTER_VISION_MODEL="", OPENROUTER_BASE_URL=""):
            c = llm.OpenRouterClient()
        self.assertEqual((c.text_model, c.vision_model, c.base_url),
                         (llm.DEFAULT_TEXT_MODEL, llm.DEFAULT_VISION_MODEL, llm.DEFAULT_BASE_URL))

    def test_key_is_read_from_env_and_whitespace_only_counts_as_missing(self):
        with openrouter_env():
            self.assertTrue(llm.OpenRouterClient().has_key())
        with openrouter_env(key="   "):
            self.assertFalse(llm.OpenRouterClient().has_key())
        with openrouter_env(key=None):
            self.assertFalse(llm.OpenRouterClient().has_key())

    def test_repr_hides_the_key(self):
        with openrouter_env():
            c = llm.OpenRouterClient()
        self.assertNotIn(DUMMY_KEY, repr(c))
        self.assertNotIn(DUMMY_KEY, str(c))
        self.assertIn("key=set", repr(c))
        with openrouter_env(key=None):
            self.assertIn("key=missing", repr(llm.OpenRouterClient()))

    def test_cache_root_is_code_cache_and_is_not_created_by_reading_it(self):
        existed = REPO_CACHE.exists()
        self.assertEqual(llm.cache_root(), repo_root() / "code" / "cache")
        self.assertEqual(llm.usage_log_path(), repo_root() / "code" / "cache" / "usage.jsonl")
        self.assertEqual(REPO_CACHE.exists(), existed)


class AvailabilityTests(unittest.TestCase):

    def test_available_without_key_is_false_with_reason_and_makes_no_request(self):
        with FakeOpenRouter() as srv, openrouter_env(base_url=srv.url, key=None):
            ok, reason = llm.OpenRouterClient().available()
        self.assertFalse(ok)
        self.assertIn("OPENROUTER_API_KEY", reason)
        self.assertEqual(srv.requests, [])

    def test_available_with_key_and_reachable_server(self):
        with FakeOpenRouter() as srv, openrouter_env(base_url=srv.url):
            ok, detail = llm.OpenRouterClient().available()
        self.assertTrue(ok)
        self.assertTrue(detail.startswith("ready"), detail)
        self.assertNotIn(DUMMY_KEY, detail)

    def test_reachable_calls_models_without_sending_the_key(self):
        with FakeOpenRouter() as srv, openrouter_env(base_url=srv.url):
            ok, detail = llm.OpenRouterClient().reachable()
        self.assertEqual((ok, detail), (True, "HTTP 200"))
        self.assertEqual([r["path"] for r in srv.requests], ["/api/v1/models"])
        self.assertNotIn("authorization", srv.requests[0]["headers"])

    def test_unreachable_base_url(self):
        with openrouter_env(base_url=closed_port_url()):
            c = llm.OpenRouterClient()
            self.assertFalse(c.reachable(timeout=2)[0])
            ok, detail = c.available()
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("unreachable"), detail)


class ImagePartTests(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_png_data_url(self):
        data = b"\x89PNG\r\n\x1a\nsynthetic-bytes"
        path = self.dir / "shot.png"
        path.write_bytes(data)
        part = llm.image_part(path)
        self.assertEqual(part, {"type": "image_url", "image_url": {
            "url": "data:image/png;base64," + base64.b64encode(data).decode("ascii")}})
        self.assertEqual(llm.OpenRouterClient.image_part(str(path)), part)

    def test_mime_types_by_suffix_case_insensitive(self):
        for name, mime in (("a.PNG", "image/png"), ("b.jpg", "image/jpeg"), ("c.JPEG", "image/jpeg"),
                           ("d.webp", "image/webp")):
            path = self.dir / name
            path.write_bytes(b"x")
            url = llm.image_part(path)["image_url"]["url"]
            self.assertTrue(url.startswith(f"data:{mime};base64,"), (name, url))
            self.assertEqual(base64.b64decode(url.split(",", 1)[1]), b"x")

    def test_unsupported_type_raises(self):
        path = self.dir / "doc.gif"
        path.write_bytes(b"GIF89a")
        with self.assertRaises(ValueError):
            llm.image_part(path)


class WriteJsonTests(unittest.TestCase):

    def test_stable_utf8_atomic_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "x.json"
            llm.write_json(path, {"b": 1, "a": "Soo-jin 박"})
            text = path.read_text(encoding="utf-8")
            self.assertEqual(json.loads(text), {"a": "Soo-jin 박", "b": 1})
            self.assertLess(text.index('"a"'), text.index('"b"'))  # sorted keys
            self.assertIn("박", text)  # not \u-escaped
            self.assertTrue(text.endswith("\n"))
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["x.json"])  # no .tmp left


class ParseJsonReplyTests(unittest.TestCase):

    def test_plain_json(self):
        self.assertEqual(llm.parse_json_reply('{"a": 1, "b": [true]}'), {"a": 1, "b": [True]})

    def test_fenced_json(self):
        for fence in ("```json\n{\"a\": 1}\n```", "```JSON\n{\"a\": 1}\n```", "```\n{\"a\": 1}\n```",
                      "Here you go:\n```json\n{\"a\": 1}\n```\nDone."):
            self.assertEqual(llm.parse_json_reply(fence), {"a": 1}, fence)

    def test_json_embedded_in_prose(self):
        self.assertEqual(llm.parse_json_reply('Sure! The answer is {"a": {"b": 2}} - hope it helps.'),
                         {"a": {"b": 2}})

    def test_think_block_is_ignored(self):
        self.assertEqual(llm.parse_json_reply('<think>maybe {"wrong": 1}</think>\n{"right": 2}'), {"right": 2})

    def test_errors(self):
        for bad in ("", "   ", None, ["not", "a", "string"], "no braces here", '{"a": 1,,}', '{"a": '):
            with self.assertRaises(ValueError, msg=repr(bad)):
                llm.parse_json_reply(bad)


class RequestShapeTests(TempCacheMixin, unittest.TestCase):

    def test_request_shape(self):
        with FakeOpenRouter(script=[reply('{"ok": true}')]) as srv, openrouter_env(base_url=srv.url):
            data, usage = make_client().chat_json(USER_MSG, model="synthetic/requested-model", task="unit")
        self.assertEqual(data, {"ok": True})
        (req,) = srv.requests
        self.assertEqual((req["method"], req["path"]), ("POST", "/api/v1/chat/completions"))
        self.assertEqual(req["headers"]["authorization"], f"Bearer {DUMMY_KEY}")
        self.assertEqual(req["headers"]["content-type"], "application/json")
        self.assertEqual(req["body"]["model"], "synthetic/requested-model")
        self.assertEqual(req["body"]["temperature"], 0)
        self.assertEqual(req["body"]["messages"], USER_MSG)
        self.assertEqual(req["body"]["response_format"], {"type": "json_object"})

    def test_default_model_is_the_text_model(self):
        with FakeOpenRouter() as srv, openrouter_env(base_url=srv.url, OPENROUTER_TEXT_MODEL="synthetic/text-x"):
            make_client().chat_json(USER_MSG, task="unit")
        self.assertEqual(srv.chat_requests[0]["body"]["model"], "synthetic/text-x")

    def test_without_key_raises_fatal_and_sends_nothing(self):
        with FakeOpenRouter() as srv, openrouter_env(base_url=srv.url, key=None):
            with self.assertRaises(llm.LLMError) as cm:
                make_client().chat_json(USER_MSG, task="unit")
        self.assertTrue(cm.exception.fatal)
        self.assertEqual(srv.requests, [])
        self.assertEqual(self.cache_files(), [])

    def test_usage_record(self):
        with FakeOpenRouter(script=[reply('{"ok": 1}', model="synthetic/actual-b", prompt_tokens=30,
                                          completion_tokens=5)]) as srv, openrouter_env(base_url=srv.url):
            _, usage = make_client().chat_json(USER_MSG, model="openrouter/free", task="unit")
        self.assertIsInstance(usage, llm.UsageRecord)
        self.assertEqual((usage.task, usage.requested_model, usage.model, usage.calls),
                         ("unit", "openrouter/free", "synthetic/actual-b", 1))
        self.assertEqual((usage.prompt_tokens, usage.completion_tokens, usage.total_tokens), (30, 5, 35))

    def test_parses_fenced_and_prose_replies_end_to_end(self):
        for content in ('```json\n{"v": 1}\n```', 'Result: {"v": 1}. Thanks'):
            with FakeOpenRouter(script=[reply(content)]) as srv, openrouter_env(base_url=srv.url):
                data, _ = make_client().chat_json(USER_MSG, task="unit")
            self.assertEqual(data, {"v": 1})
            self.assertEqual(len(srv.chat_requests), 1)


class InvalidOutputRetryTests(TempCacheMixin, unittest.TestCase):

    def test_invalid_json_gets_one_retry_with_the_error_appended(self):
        script = [reply("this is not json"), reply('{"fixed": true}')]
        with FakeOpenRouter(script=script) as srv, openrouter_env(base_url=srv.url):
            data, usage = make_client().chat_json(USER_MSG, task="unit")
        self.assertEqual(data, {"fixed": True})
        self.assertEqual(usage.calls, 2)
        first, second = (r["body"]["messages"] for r in srv.chat_requests)
        self.assertEqual(first, USER_MSG)
        self.assertEqual(second[:1], USER_MSG)
        self.assertEqual(second[1], {"role": "assistant", "content": "this is not json"})
        self.assertEqual(second[2]["role"], "user")
        self.assertIn("invalid", second[2]["content"].lower())
        self.assertIn("no JSON object", second[2]["content"])

    def test_two_invalid_replies_raise_llm_error(self):
        script = [reply("nope"), reply("still nope")]
        with FakeOpenRouter(script=script) as srv, openrouter_env(base_url=srv.url):
            with self.assertRaises(llm.LLMError) as cm:
                make_client().chat_json(USER_MSG, task="unit")
        self.assertEqual(len(srv.chat_requests), 2)
        self.assertFalse(cm.exception.fatal)
        self.assertFalse(cm.exception.rate_limited)

    def test_schema_check_failure_retried_once_with_its_message(self):
        def check(d):
            if "answer" not in d:
                raise ValueError("SCHEMA-PROBLEM-answer-missing")
        script = [reply('{"other": 1}'), reply('{"answer": 42}')]
        with FakeOpenRouter(script=script) as srv, openrouter_env(base_url=srv.url):
            data, _ = make_client().chat_json(USER_MSG, task="unit", schema_check=check)
        self.assertEqual(data, {"answer": 42})
        self.assertIn("SCHEMA-PROBLEM-answer-missing", srv.chat_requests[1]["body"]["messages"][-1]["content"])

    def test_schema_check_failing_twice_raises(self):
        def check(d):
            raise ValueError("never valid")
        with FakeOpenRouter() as srv, openrouter_env(base_url=srv.url):
            with self.assertRaises(llm.LLMError):
                make_client().chat_json(USER_MSG, task="unit", schema_check=check)
        self.assertEqual(len(srv.chat_requests), 2)

    def test_missing_choices_or_empty_content_is_invalid_output(self):
        script = [(200, {}, {"model": SYNTH_MODEL, "choices": []}),
                  reply(None), ]
        with FakeOpenRouter(script=script) as srv, openrouter_env(base_url=srv.url):
            with self.assertRaises(llm.LLMError):
                make_client().chat_json(USER_MSG, task="unit")
        self.assertEqual(len(srv.chat_requests), 2)


class TransientRetryTests(TempCacheMixin, unittest.TestCase):

    def test_429_honours_retry_after_with_injected_sleep(self):
        sleep = SleepRecorder()
        script = [error_reply(429, headers={"Retry-After": "7"}), reply('{"ok": 1}')]
        with FakeOpenRouter(script=script) as srv, openrouter_env(base_url=srv.url):
            data, usage = make_client(sleep=sleep).chat_json(USER_MSG, task="unit")
        self.assertEqual(data, {"ok": 1})
        self.assertEqual(sleep.calls, [7.0])
        self.assertEqual(len(srv.chat_requests), 2)
        self.assertEqual(usage.calls, 1)  # only responses that reached the model count as calls

    def test_503_uses_exponential_backoff(self):
        sleep = SleepRecorder()
        script = [error_reply(503), error_reply(503), reply('{"ok": 1}')]
        with FakeOpenRouter(script=script) as srv, openrouter_env(base_url=srv.url):
            make_client(sleep=sleep, base_delay_s=2.0).chat_json(USER_MSG, task="unit")
        self.assertEqual(sleep.calls, [2.0, 4.0])
        self.assertEqual(len(srv.chat_requests), 3)

    def test_other_transient_statuses_are_retried(self):
        for status in (408, 500, 502, 504):
            sleep = SleepRecorder()
            with FakeOpenRouter(script=[error_reply(status), reply('{"ok": 1}')]) as srv, \
                    openrouter_env(base_url=srv.url):
                make_client(sleep=sleep).chat_json(USER_MSG, task="unit")
            self.assertEqual((len(srv.chat_requests), len(sleep.calls)), (2, 1), status)

    def test_total_wait_budget_stops_retries(self):
        sleep = SleepRecorder()
        with FakeOpenRouter(responder=lambda req: error_reply(503)) as srv, openrouter_env(base_url=srv.url):
            with self.assertRaises(llm.LLMError) as cm:
                make_client(sleep=sleep, base_delay_s=2.0, max_wait_s=5.0).chat_json(USER_MSG, task="unit")
        self.assertEqual(sleep.calls, [2.0])  # the next delay (4 s) would exceed the 5 s budget
        self.assertEqual(len(srv.chat_requests), 2)
        self.assertFalse(cm.exception.fatal)
        self.assertFalse(cm.exception.rate_limited)

    def test_rate_limit_budget_spent_sets_rate_limited(self):
        sleep = SleepRecorder()
        with FakeOpenRouter(responder=lambda req: error_reply(429, headers={"Retry-After": "3"})) as srv, \
                openrouter_env(base_url=srv.url):
            with self.assertRaises(llm.LLMError) as cm:
                make_client(sleep=sleep, max_wait_s=10.0).chat_json(USER_MSG, task="unit")
        self.assertEqual(sleep.calls, [3.0, 3.0, 3.0])
        self.assertEqual(len(srv.chat_requests), 4)
        self.assertTrue(cm.exception.rate_limited)
        self.assertFalse(cm.exception.fatal)

    def test_retry_after_beyond_budget_gives_up_without_sleeping(self):
        sleep = SleepRecorder()
        with FakeOpenRouter(script=[error_reply(429, headers={"Retry-After": "120"})]) as srv, \
                openrouter_env(base_url=srv.url):
            with self.assertRaises(llm.LLMError) as cm:
                make_client(sleep=sleep, max_wait_s=60.0).chat_json(USER_MSG, task="unit")
        self.assertEqual(sleep.calls, [])
        self.assertEqual(len(srv.chat_requests), 1)
        self.assertTrue(cm.exception.rate_limited)

    def test_error_inside_a_200_body_is_treated_by_its_code(self):
        sleep = SleepRecorder()
        script = [(200, {}, {"error": {"code": 429, "message": "synthetic upstream rate limit"}}),
                  reply('{"ok": 1}')]
        with FakeOpenRouter(script=script) as srv, openrouter_env(base_url=srv.url):
            data, _ = make_client(sleep=sleep).chat_json(USER_MSG, task="unit")
        self.assertEqual(data, {"ok": 1})
        self.assertEqual(len(sleep.calls), 1)

    def test_malformed_200_body_is_retried(self):
        sleep = SleepRecorder()
        script = [(200, {}, b"<html>synthetic gateway page</html>"), reply('{"ok": 1}')]
        with FakeOpenRouter(script=script) as srv, openrouter_env(base_url=srv.url):
            data, _ = make_client(sleep=sleep).chat_json(USER_MSG, task="unit")
        self.assertEqual(data, {"ok": 1})
        self.assertEqual(len(srv.chat_requests), 2)

    def test_timeouts_are_transient(self):
        class Resp:
            status, headers = 200, {}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(chat_body('{"ok": 1}')).encode()

        sleep = SleepRecorder()
        with openrouter_env(base_url="http://127.0.0.1:9/api/v1"), \
                mock.patch("urllib.request.urlopen",
                           side_effect=[TimeoutError("timed out"), urllib.error.URLError(TimeoutError("t")),
                                        Resp()]) as urlopen:
            data, _ = make_client(sleep=sleep).chat_json(USER_MSG, task="unit")
        self.assertEqual(data, {"ok": 1})
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(len(sleep.calls), 2)

    # BUG: a server that keeps answering 429 with "Retry-After: 0" (or an HTTP date in the past) makes
    # chat_json retry forever: each delay is 0, so the wait budget (max_wait_s) is never spent and there is
    # no attempt cap or minimum backoff. The plan requires exponential backoff and a bounded retry budget.
    # Suggested fix: cap attempts (e.g. 6) or use max(retry_after, base_delay_s * 2 ** (n - 1)) as the delay.
    @unittest.expectedFailure
    def test_retry_after_zero_does_not_retry_forever(self):
        sleep = SleepRecorder(abort_after=25)  # aborts the runaway loop instead of hanging the suite
        with FakeOpenRouter(responder=lambda req: error_reply(429, headers={"Retry-After": "0"})) as srv, \
                openrouter_env(base_url=srv.url):
            with self.assertRaises(llm.LLMError) as cm:
                make_client(sleep=sleep, max_wait_s=60.0).chat_json(USER_MSG, task="unit")
        self.assertTrue(cm.exception.rate_limited)
        self.assertLess(len(srv.chat_requests), 25)


class NonTransientStatusTests(TempCacheMixin, unittest.TestCase):

    def test_400_is_retried_once_without_response_format(self):
        script = [error_reply(400, "response_format not supported"), reply('{"ok": 1}')]
        sleep = SleepRecorder()
        with FakeOpenRouter(script=script) as srv, openrouter_env(base_url=srv.url):
            data, _ = make_client(sleep=sleep).chat_json(USER_MSG, task="unit")
        self.assertEqual(data, {"ok": 1})
        first, second = (r["body"] for r in srv.chat_requests)
        self.assertIn("response_format", first)
        self.assertNotIn("response_format", second)
        self.assertEqual(second["messages"], USER_MSG)
        self.assertEqual(sleep.calls, [])

    def test_second_400_raises_non_fatal(self):
        with FakeOpenRouter(responder=lambda req: error_reply(400)) as srv, openrouter_env(base_url=srv.url):
            with self.assertRaises(llm.LLMError) as cm:
                make_client().chat_json(USER_MSG, task="unit")
        self.assertEqual(len(srv.chat_requests), 2)
        self.assertFalse(cm.exception.fatal)

    def test_401_402_403_404_are_fatal_and_not_retried(self):
        for status in (401, 402, 403, 404):
            sleep = SleepRecorder()
            with FakeOpenRouter(responder=lambda req, s=status: error_reply(s)) as srv, \
                    openrouter_env(base_url=srv.url):
                with self.assertRaises(llm.LLMError) as cm:
                    make_client(sleep=sleep).chat_json(USER_MSG, task="unit")
            self.assertTrue(cm.exception.fatal, status)
            self.assertEqual(len(srv.chat_requests), 1, status)
            self.assertEqual(sleep.calls, [], status)
            self.assertIn(str(status), str(cm.exception))

    def test_connection_refused_is_fatal_with_a_clear_error(self):
        sleep = SleepRecorder()
        with openrouter_env(base_url=closed_port_url()):
            with self.assertRaises(llm.LLMError) as cm:
                make_client(sleep=sleep, timeout=5).chat_json(USER_MSG, task="unit")
        self.assertTrue(cm.exception.fatal)
        self.assertTrue(str(cm.exception).startswith("network:"), str(cm.exception))
        self.assertIn("refused", str(cm.exception).lower())
        self.assertEqual(sleep.calls, [])
        (line,) = self.usage_lines()
        self.assertFalse(line["ok"])

    def test_key_echoed_by_server_is_redacted_from_error_and_log(self):
        with FakeOpenRouter(script=[error_reply(401, f"invalid key {DUMMY_KEY}")]) as srv, \
                openrouter_env(base_url=srv.url):
            with self.assertRaises(llm.LLMError) as cm:
                make_client().chat_json(USER_MSG, task="unit")
        self.assertNotIn(DUMMY_KEY, str(cm.exception))
        self.assertIn("[REDACTED]", str(cm.exception))
        log_text = (self.cache_dir / "usage.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(DUMMY_KEY, log_text)


class UsageLogTests(TempCacheMixin, unittest.TestCase):

    def run_scenario(self):
        script = [error_reply(503), reply("not json at all", model="synthetic/actual-b", prompt_tokens=20,
                                          completion_tokens=3),
                  reply('{"ok": true}', model="synthetic/actual-c", prompt_tokens=25, completion_tokens=4)]
        with FakeOpenRouter(script=script) as srv, openrouter_env(base_url=srv.url):
            _, usage = make_client().chat_json(USER_MSG, model="openrouter/free", task="intent")
        return usage

    def test_one_line_per_attempt_with_models_tokens_and_outcome(self):
        usage = self.run_scenario()
        lines = self.usage_lines()
        self.assertEqual(len(lines), 3)
        self.assertEqual([l["attempt"] for l in lines], [1, 2, 3])
        self.assertEqual({l["task"] for l in lines}, {"intent"})
        self.assertEqual({l["requested_model"] for l in lines}, {"openrouter/free"})
        self.assertEqual([l["model"] for l in lines], [None, "synthetic/actual-b", "synthetic/actual-c"])
        self.assertEqual([l["ok"] for l in lines], [False, False, True])
        self.assertIn("503", lines[0]["error"])
        self.assertIn("invalid output", lines[1]["error"])
        self.assertIsNone(lines[2]["error"])
        self.assertEqual([(l["prompt_tokens"], l["completion_tokens"], l["total_tokens"]) for l in lines],
                         [(None, None, None), (20, 3, 23), (25, 4, 29)])
        for l in lines:
            self.assertIsInstance(l["latency_ms"], int)
            self.assertRegex(l["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        # The returned record sums every attempt that reached the model; its model is the last one.
        self.assertEqual((usage.calls, usage.prompt_tokens, usage.completion_tokens, usage.total_tokens),
                         (2, 45, 7, 52))
        self.assertEqual(usage.model, "synthetic/actual-c")

    def test_log_never_contains_the_key_or_prompt_text(self):
        self.run_scenario()
        text = (self.cache_dir / "usage.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(DUMMY_KEY, text)
        self.assertNotIn("PROMPT-MARKER-7f3a", text)
        self.assertNotIn("classify this synthetic text", text)
        self.assertNotIn("not json at all", text)  # the model's reply is not logged either
        for line in self.usage_lines():
            self.assertNotIn("messages", line)

    def test_log_goes_under_the_cache_root(self):
        self.run_scenario()
        self.assertEqual(self.cache_files(), ["usage.jsonl"])


class ZZRepoCacheGuardTests(RepoCacheGuard, unittest.TestCase):
    snapshot_at_import = _SNAPSHOT_AT_IMPORT


if __name__ == "__main__":
    unittest.main()
