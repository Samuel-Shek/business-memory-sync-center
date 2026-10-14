"""Daily full chat read inside the supervisor's chat step; provider never runs."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import json
import sqlite3
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import business_sync_controller as controller


class DailyFullTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = {"external_root": self.tmp.name}
        state = Path(self.tmp.name) / "dingtalk-chat/state"
        state.mkdir(parents=True)
        self.db = state / "chat.sqlite"
        with sqlite3.connect(self.db) as c:
            c.execute("CREATE TABLE sync_runs(run_id TEXT, started_at TEXT, status TEXT, mode TEXT)")
        self.result = {"chat_messages_changed": 0}

    def tearDown(self):
        self.tmp.cleanup()

    def add_full(self, hours_ago, status="success"):
        stamp = (datetime.now().astimezone() - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
        with sqlite3.connect(self.db) as c:
            c.execute("INSERT INTO sync_runs VALUES('r',?,?,'full')", (stamp, status))
            c.execute("INSERT INTO sync_runs VALUES('i',?,'success','incremental')", (datetime.now().astimezone().isoformat(),))

    def run_full(self, automatic=True, **process):
        completed = subprocess.CompletedProcess([], process.get("returncode", 0), process.get("stdout", ""), "")
        with mock.patch.object(controller, "AUTOMATIC_MODE", automatic), \
                mock.patch.object(controller, "require_automatic_power"), \
                mock.patch.object(controller, "run_process", return_value=completed,
                                  side_effect=process.get("error")) as runner:
            controller.run_daily_chat_full(Path("/cfg.json"), {}, self.config, self.result)
        return runner

    def test_due_after_a_day_counting_failed_attempts_too(self):
        self.assertTrue(controller.chat_full_due(self.config))
        self.add_full(30, "partial")
        self.assertTrue(controller.chat_full_due(self.config))
        self.add_full(2, "failed")
        self.assertFalse(controller.chat_full_due(self.config), "a failed attempt still waits a day")

    def test_runs_full_without_ledger_and_feeds_hidden_into_drain(self):
        receipt = {"status": "success", "messages_hidden": 3, "messages_inserted": 1, "messages_updated": 0,
                   "conversations_incomplete": 0}
        runner = self.run_full(stdout=json.dumps(receipt))
        command = runner.call_args.args[0]
        self.assertIn("full", command)
        self.assertIn("--no-ledger", command)
        self.assertEqual(self.result["chat_full_read"]["messages_hidden"], 3)
        self.assertEqual(self.result["chat_messages_changed"], 4)

    def test_recent_full_or_manual_run_never_reads(self):
        self.add_full(1)
        self.assertFalse(self.run_full().called)
        self.add_full(48)
        with sqlite3.connect(self.db) as c:
            c.execute("DELETE FROM sync_runs WHERE started_at>?", ((datetime.now().astimezone() - timedelta(hours=40)).isoformat(),))
        self.assertFalse(self.run_full(automatic=False).called)

    def test_failures_are_recorded_not_raised(self):
        self.run_full(error=controller.ControllerError("component_timeout"))
        self.assertEqual(self.result["chat_full_read"], {"status": "failed", "error_code": "component_timeout"})
        self.result.clear()
        self.result["chat_messages_changed"] = 0
        self.run_full(returncode=2, stdout="not json")
        self.assertEqual(self.result["chat_full_read"]["status"], "failed")

    def test_unreadable_state_skips(self):
        self.config = {}
        self.assertFalse(self.run_full().called)
        self.assertEqual(self.result["chat_full_read"]["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
