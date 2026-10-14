#!/usr/bin/env python3
"""Synthetic evidence tests. No production files or native tasks are touched."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_handoff as h
import handoff_context as c


class EvidenceContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "state").mkdir()
        self.config = {"memory_root": str(self.root / "private vault" / "memory")}
        self.source = self.root / "private vault" / "private-customer.json"
        self.source.parent.mkdir()
        self.source.write_text('{"private_body":"never transmit"}')
        self.detail = {"kind": "group", "source_id": "memory_core", "source_label": "记忆库核心",
                       "title": "记忆库核心需要处理", "status": "open", "attention_state": "needs_user",
                       "first_seen": "2020-01-02T10:12:00+08:00", "total_attempts": 0,
                       "error_code": "validation_failed", "members": [],
                       "alert_health": [{"repair_run_id": "repair-1", "repair_status": "blocked", "repair_attempt_count": 1}],
                       "completion_rule": "完整校验成功后才可恢复", "actions": []}
        self.write("last-memory-validation-failure.json", {"verdict": "fail", "recorded_at": "2020-01-02T10:38:00+08:00",
                   "errors": [str(self.source) + ": forbidden script/code/config file type .json"]})
        self.write("last-memory-validation-success.json", {"verdict": "pass", "full_validation": True, "recorded_at": "2020-01-02T10:04:00+08:00"})
        self.write("automatic-memory-validation.json", {"status": "waiting_stable", "attempt_count": 0})
        rows = [{"run_id": "validation-1", "status": state, "updated_at": stamp}
                for state, stamp in [("running", "2020-01-02T10:13:00+08:00"), ("failed", "2020-01-02T10:14:00+08:00")]]
        self.history = self.root / "state/automatic-memory-validation-history.jsonl"
        self.history.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, value):
        (self.root / "state" / name).write_text(json.dumps(value))

    def test_zero_workbench_count_keeps_real_backend_attempts_and_specific_cause(self):
        result = c.operational(self.config, self.detail, self.root)
        self.assertEqual(result["workbench_attempt_count"], 0)
        evidence = result["diagnostics"]
        self.assertEqual(evidence["validation_history"]["observed_run_count"], 1)
        self.assertEqual(evidence["validation_history"]["runs"][0]["status"], "failed")
        self.assertEqual(evidence["repair_history"][0]["repair_attempt_count"], 1)
        self.assertTrue(evidence["latest_failure"]["errors"][0]["file_still_present"])
        self.assertIn(".json", evidence["latest_failure"]["errors"][0]["summary"])
        self.assertFalse(result["decision"]["required"])
        self.assertIn("保留原件", result["recommended_next_step"])

    def test_missing_or_partial_history_is_not_zero_known_attempts(self):
        self.history.unlink()
        evidence = c.core_evidence(self.root, self.config, self.detail)
        self.assertIsNone(evidence["validation_history"]["observed_run_count"])
        self.assertTrue(evidence["evidence_gaps"])
        self.history.write_text('{bad json}\n')
        self.assertFalse(c.core_evidence(self.root, self.config, self.detail)["validation_history"]["complete"])

    def test_history_is_bounded_and_deduplicated(self):
        row = {"run_id": "validation-2", "status": "failed", "updated_at": "2020-01-02T10:15:00+08:00"}
        self.history.write_text((json.dumps(row) + "\n") * 3000)
        result = c.core_evidence(self.root, self.config, self.detail)["validation_history"]
        self.assertFalse(result["complete"])
        self.assertEqual(result["observed_run_count"], 1)

    def test_reference_package_does_not_expose_paths_customer_name_or_raw_content(self):
        result = h.clean({"problem": c.operational(self.config, self.detail, self.root)})
        text = json.dumps(result, ensure_ascii=False)
        for private in (str(self.root), "private vault", "private-customer", "never transmit"):
            self.assertNotIn(private, text)
        self.assertNotIn("[内容已省略]", text)

    def test_preview_does_not_change_files_or_create_a_queue(self):
        import operational_workbench as w
        import alert_workbench as a
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.rglob("*") if p.is_file()}
        with patch.object(h, "ROOT", self.root), patch.object(w, "group_detail", return_value=self.detail), \
             patch.object(w, "legacy_conversation", return_value=[]), patch.object(a, "conversation", return_value={"messages": []}), \
             patch.object(a, "conversation_thread", return_value="group:test"), patch.object(h, "Queue", side_effect=AssertionError("must not create queue")):
            result = h.preview(self.config, "workbench/group-" + "a" * 24)
        self.assertEqual(result["context_version"], 2)
        after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_closed_issue_does_not_recommend_replaying_old_failure(self):
        self.detail["status"] = "closed"
        result = c.operational(self.config, self.detail, self.root)
        self.assertFalse(result["decision"]["required"])
        self.assertIn("不要重复执行", result["recommended_next_step"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
