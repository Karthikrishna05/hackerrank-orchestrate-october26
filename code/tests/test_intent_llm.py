"""Tests for ghostaudit.intent_llm (engine_plan.md §3 A1): batched, cached intent classification.

Runs against the local fake OpenRouter server from test_llm.py with a dummy key and a temp cache root.
All model replies are synthetic and are written only to the temp directory.
"""

from __future__ import annotations

import hashlib
import json
import re
import unittest

from ghostaudit import intent_llm, llm
from ghostaudit.models import IntentResult
from ghostaudit.paths import dataset_dir
from tests.test_llm import (FakeOpenRouter, RepoCacheGuard, SleepRecorder, TempCacheMixin, chat_body, error_reply,
                            make_client, openrouter_env, repo_cache_snapshot, reply)

_SNAPSHOT_AT_IMPORT = repo_cache_snapshot()

INTENT_MODEL = "synthetic/fake-intent-model"
TEXTS = [  # synthetic request texts; "ERASE-ME" marks the ones the fake model calls deletions
    "ERASE-ME synthetic text one: please remove everything about me.",
    "Synthetic text two: send me a copy of my data.",
    "ERASE-ME synthetic text three: verify the earlier deletion.",
    "Synthetic text four: update my postal address.",
    "Synthetic text five: stop the newsletter.",
]


def batch_items(req: dict) -> list[dict]:
    """The {"id", "text"} items of the batch in a fake-server request (the last batch prompt)."""
    for msg in reversed(req["body"]["messages"]):
        if msg["role"] == "user" and isinstance(msg["content"], str) and msg["content"].startswith("Classify each"):
            return json.loads(msg["content"].split("\n", 1)[1])
    raise AssertionError("no batch prompt in request")


def result_for(item: dict) -> dict:
    deletion = "ERASE-ME" in item["text"]
    return {"id": item["id"], "authorizes_deletion": deletion,
            "request_type": "deletion" if deletion else "access", "language": "EN",
            "confidence": 0.9, "quote": f"synthetic quote {item['id']}"}


def intent_responder(model: str = INTENT_MODEL, mutate=None):
    """Fake model: classifies every batch item; `mutate(results)` may corrupt the reply."""
    def respond(req):
        results = [result_for(i) for i in batch_items(req)]
        if mutate:
            results = mutate(results)
        return 200, {}, chat_body(json.dumps({"results": results}), model=model)
    return respond


class CacheKeyTests(unittest.TestCase):

    def test_sha256_of_stripped_utf8_text(self):
        text = "  Bitte löschen Sie mein Konto. 削除してください\n"
        self.assertEqual(intent_llm.cache_key(text), hashlib.sha256(text.strip().encode("utf-8")).hexdigest())

    def test_stable_and_whitespace_insensitive_at_the_ends(self):
        self.assertEqual(intent_llm.cache_key("delete me"), intent_llm.cache_key("\t delete me \n"))
        self.assertEqual(intent_llm.cache_key("delete me"), intent_llm.cache_key("delete me"))
        self.assertNotEqual(intent_llm.cache_key("delete me"), intent_llm.cache_key("Delete me"))
        self.assertRegex(intent_llm.cache_key("x"), r"^[0-9a-f]{64}$")

    def test_cache_path_is_under_cache_root_intent(self):
        key = intent_llm.cache_key("x")
        self.assertEqual(intent_llm.cache_path(key), llm.cache_root() / "intent" / f"{key}.json")


class ValidateTests(unittest.TestCase):

    def good(self, n=2):
        return {"results": [{"id": i, "authorizes_deletion": True, "request_type": "deletion", "language": "en",
                             "confidence": 0.8, "quote": "q"} for i in range(1, n + 1)]}

    def test_valid_results_are_normalised(self):
        data = self.good()
        data["results"][0].update(id="1", language=" PT-BR ", confidence=1, quote="  x" * 200)
        out = intent_llm.validate(data, 2)
        self.assertEqual(sorted(out), [1, 2])
        self.assertEqual(out[1]["language"], "pt-br")
        self.assertEqual(out[1]["confidence"], 1.0)
        self.assertLessEqual(len(out[1]["quote"]), intent_llm.MAX_QUOTE)

    def test_rejections(self):
        cases = {
            "missing id": lambda d: d["results"].pop(),
            "duplicate id": lambda d: d["results"][1].update(id=1),
            "id out of range": lambda d: d["results"][1].update(id=3),
            "bool id": lambda d: d["results"][0].update(id=True),
            "deletion flag disagrees with type": lambda d: d["results"][0].update(request_type="access"),
            "non-deletion type flagged true": lambda d: d["results"][0].update(
                authorizes_deletion=False, request_type="deletion_verification"),
            "unknown type": lambda d: d["results"][0].update(request_type="delete_everything"),
            "flag not bool": lambda d: d["results"][0].update(authorizes_deletion="true"),
            "confidence > 1": lambda d: d["results"][0].update(confidence=1.5),
            "confidence not number": lambda d: d["results"][0].update(confidence="high"),
            "blank language": lambda d: d["results"][0].update(language=" "),
            "results not list": lambda d: d.update(results={}),
        }
        for name, corrupt in cases.items():
            data = self.good()
            corrupt(data)
            with self.assertRaises(ValueError, msg=name):
                intent_llm.validate(data, 2)

    def test_few_shot_reply_is_itself_valid(self):
        msgs = intent_llm.build_messages(["x"])
        shots = intent_llm.validate(json.loads(msgs[2]["content"]), len(intent_llm.FEW_SHOT))
        for i, (_, ok, rtype, lang, quote) in enumerate(intent_llm.FEW_SHOT, 1):
            self.assertEqual((shots[i]["authorizes_deletion"], shots[i]["request_type"]), (ok, rtype))


class PromptTests(unittest.TestCase):

    def test_prompt_version_is_the_approved_one(self):
        self.assertEqual(intent_llm.PROMPT_VERSION, "intent-v1")  # owner decision 11

    def test_message_layout(self):
        msgs = intent_llm.build_messages(["first synthetic", "second synthetic"])
        self.assertEqual([m["role"] for m in msgs], ["system", "user", "assistant", "user"])
        self.assertEqual(msgs[0]["content"], intent_llm.SYSTEM_PROMPT)
        self.assertEqual(json.loads(msgs[-1]["content"].split("\n", 1)[1]),
                         [{"id": 1, "text": "first synthetic"}, {"id": 2, "text": "second synthetic"}])

    def test_definition_counts_verification_as_deletion(self):
        self.assertIn("deletion_verification", intent_llm.SYSTEM_PROMPT)
        self.assertEqual(intent_llm.DELETION_TYPES, frozenset({"deletion", "deletion_verification"}))
        self.assertEqual(set(intent_llm.REQUEST_TYPES), {"deletion", "deletion_verification", "access", "export",
                                                         "correction", "marketing_opt_out", "account_help", "other"})


@unittest.skipUnless((dataset_dir() / "deletion_requests.csv").is_file(), "dataset/ not available")
class FewShotAreSyntheticTests(unittest.TestCase):

    def test_no_few_shot_text_is_a_dataset_request_text(self):
        from ghostaudit.loaders import load_requests

        def norm(s):
            return re.sub(r"\s+", " ", s).strip().casefold()

        dataset_texts = set()
        for name in ("deletion_requests.csv", "sample_deletion_requests.csv"):
            if (dataset_dir() / name).is_file():
                dataset_texts |= {norm(r.request_text) for r in load_requests(name)}
        self.assertGreater(len(dataset_texts), 0)
        for text, *_ in intent_llm.FEW_SHOT:
            self.assertNotIn(norm(text), dataset_texts, text)
            for d in dataset_texts:  # nor a long verbatim excerpt either way
                if len(d) >= 25:
                    self.assertFalse(d in norm(text) or norm(text) in d, (text, d))


class CachedTests(TempCacheMixin, unittest.TestCase):

    def write_record(self, text, **over):
        rec = {"cache_key": intent_llm.cache_key(text), "prompt_version": intent_llm.PROMPT_VERSION,
               "requested_model": "openrouter/free", "model": "synthetic/actual-model",
               "result": {"authorizes_deletion": False, "request_type": "export", "language": "en",
                          "confidence": 0.7, "quote": "q"}}
        rec.update(over)
        llm.write_json(intent_llm.cache_path(intent_llm.cache_key(text)), rec)

    def test_hit_under_current_prompt_version(self):
        self.write_record("synthetic cached text")
        hit = intent_llm.cached("  synthetic cached text ")
        self.assertEqual(hit, IntentResult(False, "export", "en", 0.7, "llm:synthetic/actual-model", "q"))

    def test_old_prompt_version_is_ignored(self):
        self.write_record("synthetic old text", prompt_version="intent-v0")
        self.assertIsNone(intent_llm.cached("synthetic old text"))

    def test_missing_or_corrupt_entries_are_misses(self):
        self.assertIsNone(intent_llm.cached("never cached"))
        path = intent_llm.cache_path(intent_llm.cache_key("corrupt"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(intent_llm.cached("corrupt"))
        self.write_record("no result", result=None)
        self.assertIsNone(intent_llm.cached("no result"))


class ClassifyAllTests(TempCacheMixin, unittest.TestCase):

    def run_classify(self, texts, srv, *, batch_size=10, force=False, **env):
        report = {}
        with openrouter_env(base_url=srv.url, **env):
            results = intent_llm.classify_all(texts, client=make_client(), batch_size=batch_size, force=force,
                                              report=report)
        return results, report

    def test_results_keyed_by_sha256_with_actual_model_source(self):
        with FakeOpenRouter(responder=intent_responder(model="synthetic/routed-model-x")) as srv:
            results, report = self.run_classify(TEXTS, srv, OPENROUTER_TEXT_MODEL="openrouter/free")
        self.assertEqual(set(results), {intent_llm.cache_key(t) for t in TEXTS})
        r1 = results[intent_llm.cache_key(TEXTS[0])]
        self.assertEqual((r1.authorizes_deletion, r1.request_type, r1.language), (True, "deletion", "en"))
        self.assertEqual(r1.source, "llm:synthetic/routed-model-x")
        self.assertFalse(results[intent_llm.cache_key(TEXTS[1])].authorizes_deletion)
        self.assertEqual(srv.chat_requests[0]["body"]["model"], "openrouter/free")
        self.assertEqual((report["texts"], report["cached"], report["classified"], report["calls"]), (5, 0, 5, 1))
        self.assertEqual(report["failed"], [])
        # one cache file per text, recording the requested and the actual model
        for t in TEXTS:
            rec = json.loads(intent_llm.cache_path(intent_llm.cache_key(t)).read_text(encoding="utf-8"))
            self.assertEqual(rec["prompt_version"], intent_llm.PROMPT_VERSION)
            self.assertEqual((rec["requested_model"], rec["model"]), ("openrouter/free", "synthetic/routed-model-x"))
            self.assertEqual(rec["cache_key"], intent_llm.cache_key(t))

    def test_cache_hits_make_zero_requests_even_without_a_key(self):
        with FakeOpenRouter(responder=intent_responder()) as srv:
            first, _ = self.run_classify(TEXTS, srv)
            n = len(srv.requests)
            with openrouter_env(base_url=srv.url, key=None):
                report = {}
                second = intent_llm.classify_all(TEXTS, client=make_client(), report=report)
        self.assertEqual(len(srv.requests), n)
        self.assertEqual(second, first)
        self.assertEqual((report["cached"], report["calls"], report["classified"]), (5, 0, 0))

    def test_force_reclassifies(self):
        with FakeOpenRouter(responder=intent_responder()) as srv:
            self.run_classify(TEXTS, srv)
            _, report = self.run_classify(TEXTS, srv, force=True)
        self.assertEqual(len(srv.chat_requests), 2)
        self.assertEqual(report["classified"], 5)

    def test_batches_are_sorted_by_key_and_independent_of_input_order(self):
        def batches(texts):
            with FakeOpenRouter(responder=intent_responder()) as srv:
                self.run_classify(texts, srv, batch_size=2, force=True)
            return [[i["text"] for i in batch_items(r)] for r in srv.chat_requests]

        expected = [t for _, t in sorted((intent_llm.cache_key(t), t) for t in TEXTS)]
        got = batches(TEXTS)
        self.assertEqual([len(b) for b in got], [2, 2, 1])
        self.assertEqual([t for b in got for t in b], expected)
        self.assertEqual(batches(list(reversed(TEXTS))), got)

    def test_blank_and_duplicate_texts_are_collapsed(self):
        with FakeOpenRouter(responder=intent_responder()) as srv:
            results, report = self.run_classify([TEXTS[0], "  " + TEXTS[0] + "\n", "", "   ", TEXTS[1]], srv)
        self.assertEqual(len(results), 2)
        self.assertEqual(report["texts"], 2)
        self.assertEqual(len(batch_items(srv.chat_requests[0])), 2)

    def test_invalid_batches_are_rejected_and_left_uncached(self):
        corruptions = {
            "missing id": lambda rs: rs[:-1],
            "duplicate id": lambda rs: rs + [dict(rs[0])],
            "flag disagrees with type": lambda rs: [dict(r, request_type="access") if r["authorizes_deletion"]
                                                    else r for r in rs],
        }
        for name, mutate in corruptions.items():
            with self.subTest(name):
                with FakeOpenRouter(responder=intent_responder(mutate=mutate)) as srv:
                    results, report = self.run_classify(TEXTS, srv)
                self.assertEqual(results, {})
                self.assertEqual(sorted(report["failed"]), sorted(intent_llm.cache_key(t) for t in TEXTS))
                self.assertEqual(len(report["errors"]), 1)
                self.assertIsNone(report["stopped"])
                self.assertEqual(len(srv.chat_requests), 2)  # one retry with the error appended
                self.assertIn("Your reply was invalid", srv.chat_requests[1]["body"]["messages"][-1]["content"])
                self.assertFalse((self.cache_dir / "intent").exists())

    def test_one_bad_reply_then_a_good_one_succeeds(self):
        script = [reply('{"results": []}', model=INTENT_MODEL)]
        with FakeOpenRouter(responder=intent_responder(), script=script) as srv:
            results, report = self.run_classify(TEXTS, srv)
        self.assertEqual(len(results), 5)
        self.assertEqual(report["calls"], 2)

    def test_failed_batch_does_not_stop_later_batches(self):
        state = {"n": 0}
        good = intent_responder()

        def respond(req):
            state["n"] += 1
            if state["n"] <= 2:  # both attempts for the first batch are invalid
                return reply("synthetic garbage", model=INTENT_MODEL)
            return good(req)

        with FakeOpenRouter(responder=respond) as srv:
            results, report = self.run_classify(TEXTS, srv, batch_size=2)
        self.assertEqual(len(results), 3)
        self.assertEqual(len(report["failed"]), 2)
        self.assertEqual(len(list((self.cache_dir / "intent").glob("*.json"))), 3)

    def test_fatal_error_stops_the_run(self):
        with FakeOpenRouter(responder=lambda req: error_reply(401)) as srv:
            results, report = self.run_classify(TEXTS, srv, batch_size=2)
        self.assertEqual(results, {})
        self.assertEqual(len(srv.chat_requests), 1)
        self.assertEqual(len(report["failed"]), 5)
        self.assertIsNotNone(report["stopped"])
        self.assertEqual(self.cache_files(), ["usage.jsonl"])

    def test_spent_rate_limit_stops_the_run(self):
        with FakeOpenRouter(responder=lambda req: error_reply(429, headers={"Retry-After": "999"})) as srv:
            results, report = self.run_classify(TEXTS, srv, batch_size=2)
        self.assertEqual(results, {})
        self.assertEqual(len(srv.chat_requests), 1)
        self.assertEqual(len(report["failed"]), 5)
        self.assertIsNotNone(report["stopped"])

    def test_misses_without_a_key_raise_and_write_nothing(self):
        with FakeOpenRouter() as srv, openrouter_env(base_url=srv.url, key=None):
            with self.assertRaises(llm.LLMError) as cm:
                intent_llm.classify_all(TEXTS, client=make_client())
        self.assertTrue(cm.exception.fatal)
        self.assertEqual(srv.requests, [])
        self.assertEqual(self.cache_files(), [])

    def test_batch_messages_use_the_text_model_and_task_intent(self):
        with FakeOpenRouter(responder=intent_responder()) as srv:
            self.run_classify(TEXTS[:1], srv, OPENROUTER_TEXT_MODEL="synthetic/text-model")
        body = srv.chat_requests[0]["body"]
        self.assertEqual(body["model"], "synthetic/text-model")
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["messages"][0]["content"], intent_llm.SYSTEM_PROMPT)
        self.assertEqual([l["task"] for l in self.usage_lines()], ["intent"])


class ZZRepoCacheGuardTests(RepoCacheGuard, unittest.TestCase):
    snapshot_at_import = _SNAPSHOT_AT_IMPORT


if __name__ == "__main__":
    unittest.main()
