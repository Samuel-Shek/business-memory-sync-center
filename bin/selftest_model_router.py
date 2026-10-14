#!/usr/bin/env python3
"""Self-test for model_router: order, validation failover, final DeepSeek retry, report.

Every path the router writes (health, log, rejected answers, Feishu throttle)
is pointed at a temp dir, and no provider is really called. A test that marks
providers as failing must never reach the live health file: on 2020-01-02 an
ad-hoc check did, and put GPT into a 30-minute cooldown for production.
"""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import json
import tempfile
import unittest
from pathlib import Path

import feishu_notify
import model_router as router

CONFIG = {"model_routing": {}}


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = {name: getattr(router, name) for name in (
            "STATE_DIR", "HEALTH_PATH", "HEALTH_LOCK", "LOG_PATH", "REJECTED_DIR",
            "_call_http", "_call_claude", "_call_codex")}
        self.saved_send = feishu_notify.send
        self.saved_feishu_paths = (feishu_notify.STATE_PATH, feishu_notify.LOCK_PATH)
        router.STATE_DIR = root
        router.HEALTH_PATH = root / "health.json"
        router.HEALTH_LOCK = root / "health.lock"
        router.LOG_PATH = root / "router.jsonl"
        router.REJECTED_DIR = root / "rejected"
        feishu_notify.STATE_PATH = root / "feishu.json"
        feishu_notify.LOCK_PATH = root / "feishu.lock"
        self.calls: list[str] = []
        self.reports: list[str] = []
        feishu_notify.send = lambda config, text, **kw: (self.reports.append(text), {"status": "delivered"})[1]

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(router, name, value)
        feishu_notify.send = self.saved_send
        feishu_notify.STATE_PATH, feishu_notify.LOCK_PATH = self.saved_feishu_paths
        self.tmp.cleanup()

    def providers(self, answers):
        """answers: provider -> text to return, or a failure category to raise."""
        def reply(name):
            self.calls.append(name)
            value = answers[name]
            if value in router.DEFAULT_ROUTING["cooldown_seconds"]:
                raise router._AttemptFailed(value, "synthetic")
            return value
        router._call_http = lambda provider, *a, **k: reply(provider)
        router._call_claude = lambda *a, **k: reply("claude")
        router._call_codex = lambda *a, **k: reply("gpt")

    def test_chain_uses_k3_and_highest_thinking(self):
        cfg = router.routing(CONFIG)
        self.assertEqual(cfg["text_chain"][0], "deepseek:deepseek-flash")
        self.assertEqual(cfg["text_chain"][1], "kimi:k3")
        self.assertEqual(router.provider_options("kimi", cfg)["reasoning_effort"], "max")
        self.assertEqual(router.provider_options("deepseek", cfg)["reasoning_effort"], "high")
        self.assertEqual(router.PROVIDERS["claude"]["effort"], "high")

    def test_rejected_answer_moves_to_next_provider(self):
        self.providers({"deepseek": "not json", "kimi": '{"ok": 1}', "claude": "x", "gpt": "x"})
        result = router.complete(CONFIG, prompt="p", purpose="test", validate=json.loads)
        self.assertEqual(self.calls, ["deepseek", "kimi"])
        self.assertEqual((result.provider, result.value), ("kimi", {"ok": 1}))
        self.assertEqual(len(list(router.REJECTED_DIR.glob("*.txt"))), 1)
        self.assertEqual(self.reports, [])

    def test_full_failure_returns_to_deepseek_then_reports_once(self):
        self.providers({"deepseek": "server", "kimi": "quota", "claude": "timeout", "gpt": "quota"})
        with self.assertRaises(router.RouterExhausted) as caught:
            router.complete(CONFIG, prompt="p", purpose="minutes_semantic")
        self.assertEqual(self.calls, ["deepseek", "kimi", "deepseek"])
        self.assertEqual(router.legacy_error_code(caught.exception), "model_chain_exhausted")
        self.assertEqual(len(self.reports), 1)
        self.assertIn("钉钉听记整理", self.reports[0])

    def test_chain_cut_by_time_budget_is_not_reported(self):
        import time as _time
        def slow_fail(provider, *a, **k):
            self.calls.append(provider)
            _time.sleep(0.05)
            raise router._AttemptFailed("server", "synthetic")
        router._call_http = slow_fail
        router._call_claude = lambda *a, **k: slow_fail("claude")
        router._call_codex = lambda *a, **k: slow_fail("gpt")
        with self.assertRaises(router.RouterExhausted):
            router.complete(CONFIG, prompt="p", purpose="test", timeout=0.03)
        self.assertEqual(self.calls, ["deepseek"])
        self.assertEqual(self.reports, [])

    def test_final_retry_on_deepseek_can_succeed(self):
        attempts = iter(["server", '{"ok": 2}'])
        self.providers({"kimi": "quota", "claude": "server", "gpt": "quota", "deepseek": None})
        base = router._call_http

        def http(provider, *a, **k):
            if provider == "deepseek":
                self.calls.append("deepseek")
                value = next(attempts)
                if value == "server":
                    raise router._AttemptFailed("server", "synthetic")
                return value
            return base(provider, *a, **k)
        router._call_http = http
        result = router.complete(CONFIG, prompt="p", purpose="test", validate=json.loads)
        self.assertEqual(self.calls, ["deepseek", "kimi", "deepseek"])
        self.assertEqual(result.value, {"ok": 2})
        self.assertEqual(self.reports, [])

    def test_invalid_output_does_not_cool_the_provider_down(self):
        self.providers({"deepseek": "not json", "kimi": '{"ok": 1}', "claude": "x", "gpt": "x"})
        router.complete(CONFIG, prompt="p", purpose="test", validate=json.loads)
        self.assertEqual(router._cooldown_remaining("deepseek"), 0.0)

    def test_feishu_throttle_counts_only_accepted_messages(self):
        feishu_notify._record_sent("k")
        self.assertTrue(feishu_notify._throttled("k", 3600))
        self.assertFalse(feishu_notify._throttled("other", 3600))


if __name__ == "__main__":
    unittest.main()
