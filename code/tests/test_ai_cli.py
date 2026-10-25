"""Tests for ghostaudit.ai_cli (engine_plan.md §3): `python -m ghostaudit.ai_cli {status,intent,vision}`.

The CLI runs in-process (`ai_cli.main(argv)`) with a patched environment, the local fake OpenRouter server from
test_llm.py, a dummy key and a temp cache root. Model replies are synthetic and only reach the temp cache.
A subprocess smoke test checks the `python -m` entry point on the no-key paths, which write nothing by design.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

from ghostaudit import ai_cli, intent_llm, vision
from ghostaudit.loaders import load_requests
from ghostaudit.paths import dataset_dir, repo_root
from tests.test_llm import (DUMMY_KEY, ENV_NAMES, FakeOpenRouter, RepoCacheGuard, TempCacheMixin, chat_body,
                            closed_port_url, openrouter_env, repo_cache_snapshot, reply)

_SNAPSHOT_AT_IMPORT = repo_cache_snapshot()

HAVE_DATASET = all((dataset_dir() / n).is_file() for n in
                   ("deletion_requests.csv", "sample_deletion_requests.csv", "evidence_files.csv",
                    "system_activity.csv"))
CLI_MODEL = "synthetic/fake-cli-model"


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ai_cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def distinct_texts(name: str) -> int:
    return len({intent_llm.cache_key(r.request_text) for r in load_requests(name)})


def all_deletion_responder(req):
    """Synthetic intent model: every batch item is labelled a deletion request."""
    content = req["body"]["messages"][-1]["content"]
    if not content.startswith("Classify each"):  # a retry message; the batch is two messages earlier
        content = req["body"]["messages"][-3]["content"]
    items = json.loads(content.split("\n", 1)[1])
    results = [{"id": i["id"], "authorizes_deletion": True, "request_type": "deletion", "language": "en",
                "confidence": 0.5, "quote": "synthetic"} for i in items]
    return 200, {}, chat_body(json.dumps({"results": results}), model=CLI_MODEL)


@unittest.skipUnless(HAVE_DATASET, "dataset/ not available")
class NoKeyTests(TempCacheMixin, unittest.TestCase):
    """Without OPENROUTER_API_KEY every command exits 2, makes no model call and writes nothing."""

    def assert_nothing_written(self, srv):
        self.assertEqual(list(self.cache_dir.iterdir()), [])
        self.assertEqual(srv.chat_requests, [])
        for r in srv.requests:
            self.assertNotIn("authorization", r["headers"])

    def test_status_prints_missing_and_exits_2(self):
        with FakeOpenRouter() as srv, openrouter_env(base_url=srv.url, key=None):
            code, out, err = run_cli("status")
        self.assertEqual(code, ai_cli.NOT_READY)
        self.assertEqual(code, 2)
        self.assertIn("API key (OPENROUTER_API_KEY): missing", out)
        self.assertIn("google/gemma-4-31b-it:free", out)
        self.assertIn("nvidia/nemotron-3.5-super-vl:free", out)
        self.assertIn(f"deletion_requests.csv: 0/{distinct_texts('deletion_requests.csv')} distinct texts", out)
        self.assertIn("0/11 screenshots mapped, 0 raw extractions", out)
        self.assert_nothing_written(srv)

    def test_intent_exits_2_with_a_clear_message(self):
        with FakeOpenRouter() as srv, openrouter_env(base_url=srv.url, key=None):
            code, out, err = run_cli("intent", "--requests", "sample_deletion_requests.csv")
        self.assertEqual(code, 2)
        self.assertIn("OPENROUTER_API_KEY is not set", err)
        self.assertIn("Nothing was written", err)
        self.assertEqual(srv.requests, [])
        self.assert_nothing_written(srv)

    def test_vision_exits_2_with_a_clear_message(self):
        with FakeOpenRouter() as srv, openrouter_env(base_url=srv.url, key=None):
            code, out, err = run_cli("vision")
        self.assertEqual(code, 2)
        self.assertIn("OPENROUTER_API_KEY is not set", err)
        self.assertIn("Nothing was written", err)
        self.assertEqual(srv.requests, [])
        self.assert_nothing_written(srv)

    def test_unreachable_base_url_exits_2(self):
        with openrouter_env(base_url=closed_port_url()):
            code, out, err = run_cli("intent", "--requests", "sample_deletion_requests.csv")
        self.assertEqual(code, 2)
        self.assertIn("unreachable", err)
        self.assertNotIn(DUMMY_KEY, out + err)
        self.assertEqual(list(self.cache_dir.iterdir()), [])


@unittest.skipUnless(HAVE_DATASET, "dataset/ not available")
class FakeServerTests(TempCacheMixin, unittest.TestCase):

    def test_status_with_key_reports_present_and_never_prints_the_key(self):
        with FakeOpenRouter() as srv, openrouter_env(base_url=srv.url):
            code, out, err = run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("API key (OPENROUTER_API_KEY): present", out)
        self.assertIn("reachable", out)
        self.assertNotIn(DUMMY_KEY, out + err)
        self.assertEqual(list(self.cache_dir.iterdir()), [])

    def test_intent_fills_the_cache_and_status_reports_coverage(self):
        n_eval, n_sample = distinct_texts("deletion_requests.csv"), distinct_texts("sample_deletion_requests.csv")
        n_all = len({intent_llm.cache_key(r.request_text) for name in ai_cli.DEFAULT_REQUESTS
                     for r in load_requests(name)})
        with FakeOpenRouter(responder=all_deletion_responder) as srv, openrouter_env(base_url=srv.url):
            code, out, err = run_cli("intent")
            self.assertEqual(code, 0, out + err)
            batches = -(-n_all // 10)
            self.assertEqual(len(srv.chat_requests), batches)
            self.assertIn(f"Intent: {n_all} distinct texts, 0 from cache, {n_all} classified in {batches} model "
                          f"responses, 0 failed", out)
            self.assertEqual(len(list((self.cache_dir / "intent").glob("*.json"))), n_all)
            code, out, err = run_cli("status")
            self.assertEqual(code, 0)
            self.assertIn(f"deletion_requests.csv: {n_eval}/{n_eval} distinct texts", out)
            self.assertIn(f"sample_deletion_requests.csv: {n_sample}/{n_sample} distinct texts", out)
            # a rerun is served from the cache: zero model calls
            code, out, err = run_cli("intent")
            self.assertEqual(code, 0)
            self.assertEqual(len(srv.chat_requests), batches)
            self.assertIn(f"{n_all} from cache, 0 classified", out)
        rec = json.loads(next((self.cache_dir / "intent").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual((rec["model"], rec["prompt_version"]), (CLI_MODEL, intent_llm.PROMPT_VERSION))
        usage = self.usage_lines()
        self.assertEqual(len(usage), batches)
        self.assertNotIn(DUMMY_KEY, (self.cache_dir / "usage.jsonl").read_text(encoding="utf-8"))

    def test_intent_failures_exit_1_and_cache_nothing(self):
        with FakeOpenRouter(responder=lambda req: reply("synthetic non-json", model=CLI_MODEL)) as srv, \
                openrouter_env(base_url=srv.url):
            code, out, err = run_cli("intent", "--requests", "sample_deletion_requests.csv", "--batch-size", "16")
        self.assertEqual(code, 1)
        self.assertIn("32 failed", out)
        self.assertEqual(len(srv.chat_requests), 4)  # 2 batches x (1 call + 1 retry)
        self.assertFalse((self.cache_dir / "intent").exists())

    def test_vision_fills_the_cache_and_status_reports_it(self):
        extraction = {"screen_title": "Synthetic", "records": []}
        with FakeOpenRouter(responder=lambda req: reply(json.dumps(extraction), model=CLI_MODEL)) as srv, \
                openrouter_env(base_url=srv.url):
            code, out, err = run_cli("vision")
            self.assertEqual(code, 0, out + err)
            self.assertEqual(len(srv.chat_requests), 11)
            self.assertIn("Vision: 11 screenshots read, 0 re-mapped from cache, 0 failed", out)
            for r in srv.chat_requests:
                parts = r["body"]["messages"][1]["content"]
                self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/png;base64,"))
            code, out, err = run_cli("status")
            self.assertIn("11/11 screenshots mapped, 11 raw extractions", out)
            code, out, err = run_cli("vision")
            self.assertEqual(code, 0)
            self.assertEqual(len(srv.chat_requests), 11)
            self.assertIn("0 screenshots read, 11 re-mapped from cache", out)
        self.assertEqual(len(list((self.cache_dir / "vision").glob("*.raw.json"))), 11)


@unittest.skipUnless(HAVE_DATASET, "dataset/ not available")
class EntryPointTests(unittest.TestCase):
    """`python -m ghostaudit.ai_cli` from code/: no-key paths only (they never write)."""

    def run_module(self, *args, base_url):
        env = {k: v for k, v in os.environ.items() if k not in ENV_NAMES}
        env.update(OPENROUTER_BASE_URL=base_url, no_proxy="127.0.0.1,localhost", NO_PROXY="127.0.0.1,localhost")
        return subprocess.run([sys.executable, "-m", "ghostaudit.ai_cli", *args], cwd=repo_root() / "code",
                              env=env, capture_output=True, text=True, timeout=120)

    def test_status_and_intent_without_key_exit_2(self):
        with FakeOpenRouter() as srv:
            status = self.run_module("status", base_url=srv.url)
            # --force: needs the model whatever the repo cache holds, so the no-key exit is deterministic
            intent = self.run_module("intent", "--force", "--requests", "sample_deletion_requests.csv",
                                     base_url=srv.url)
        self.assertEqual(status.returncode, 2, status.stderr)
        self.assertIn("missing", status.stdout)
        self.assertEqual(intent.returncode, 2, intent.stderr)
        self.assertIn("OPENROUTER_API_KEY is not set", intent.stderr)
        self.assertEqual(srv.chat_requests, [])


class ZZRepoCacheGuardTests(RepoCacheGuard, unittest.TestCase):
    snapshot_at_import = _SNAPSHOT_AT_IMPORT


if __name__ == "__main__":
    unittest.main()
