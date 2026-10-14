#!/usr/bin/env python3
"""Offline validation lifecycle tests; all writes are in disposable fixtures."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import fcntl
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import automatic_memory_validation as validation
import dingtalk_memory_automation as memory
import local_sync_repair as repair
import validation_evidence as evidence


class AutomaticValidationTests(unittest.TestCase):
    def setUp(self):
        # Existing lifecycle checks isolate debounce; explicit stability tests below use 60s.
        patcher = patch.object(validation, "STABLE_SECONDS", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        boot = patch.object(validation, "boot_identity", return_value="test-boot")
        boot.start()
        self.addCleanup(boot.stop)
        self.temporary = tempfile.TemporaryDirectory(prefix="business-auto-validation-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.current = datetime(2026, 9, 3, 9, tzinfo=timezone.utc)
        self.memory = self.root / "vault/memory"
        self.memory.mkdir(parents=True)
        self.note = self.memory / "fixture.md"
        self.note.write_text("first business version", encoding="utf-8")
        self.config = {"memory_root": str(self.memory), "external_root": str(self.root / "external"),
                       "dingtalk_automation": {"memory_validation_timeout_seconds": 120}}
        validation.atomic_json(self.root / "config.json", self.config)
        validation.atomic_json(self.root / "state/maintenance-mode.json", {"status": "inactive"})
        for name in evidence.CODE_PATHS:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("isolated program fixture", encoding="utf-8")
        with sqlite3.connect(self.root / "state/bridge.sqlite") as connection:
            connection.execute("CREATE TABLE maintenance_state(key TEXT PRIMARY KEY,value TEXT)")
            connection.executemany("INSERT INTO maintenance_state VALUES(?,?)", [
                ("index_dirty_generation", "1"), ("index_applied_generation", "1"), ("index_last_error", "")])
        self.calls = 0
        self.worker = validation.AutomaticValidation(self.root, clock=lambda: self.current, runner=self.succeed)

    def change_generation(self, dirty, applied=None):
        with sqlite3.connect(self.root / "state/bridge.sqlite") as connection:
            connection.execute("UPDATE maintenance_state SET value=? WHERE key='index_dirty_generation'", (str(dirty),))
            connection.execute("UPDATE maintenance_state SET value=? WHERE key='index_applied_generation'", (str(dirty if applied is None else applied),))

    def receipt(self, run_id):
        index = validation.index_state(self.root)
        return {"schema_version": 2, "full_validation": True, "verdict": "pass", "run_id": run_id,
                "recorded_at": validation.stamp(self.current), "index_dirty_generation": index["dirty"],
                "index_applied_generation": index["applied"], "validation_context": evidence.context(self.root)}

    def succeed(self, config, run_id, descriptors):
        self.calls += 1
        self.assertEqual(config, self.config)
        self.assertEqual(len(descriptors), 2)
        self.current += timedelta(seconds=10)
        validation.atomic_json(self.worker.success_path, self.receipt(run_id))
        return subprocess.CompletedProcess([], 0, json.dumps({"verdict": "pass", "errors": []}), "")

    def fail_validation(self, _config, _run_id, _descriptors):
        self.calls += 1
        self.current += timedelta(seconds=10)
        return subprocess.CompletedProcess([], 1, json.dumps({"verdict": "fail", "errors": ["fixture check failed"]}), "")

    def timeout(self, _config, _run_id, _descriptors):
        self.calls += 1
        self.current += timedelta(seconds=120)
        raise subprocess.TimeoutExpired("fixed validator", 120)

    def test_invalid_independent_settings_do_not_run_or_consume(self):
        self.config["automatic_memory_validation"] = {"full_validation_timeout_seconds": "300"}
        validation.atomic_json(self.root / "config.json", self.config)
        result = self.worker.tick()
        self.assertEqual(result["error_code"], "automatic_validation_config_invalid")
        self.assertEqual(self.calls, 0)
        self.assertEqual(result["attempt_count"], 0)

    def test_161_second_runner_keeps_single_attempt_and_receipt(self):
        def slow(config, run_id, descriptors):
            self.current += timedelta(seconds=151)
            job = validation.read_object(self.worker.state_path)
            self.worker.save(job, validation_slow=True, slow_since=validation.stamp(self.current))
            return self.succeed(config, run_id, descriptors)
        self.worker.runner = slow
        result = self.worker.tick()
        self.assertEqual(result["status"], "verified")
        self.assertTrue(result["validation_slow"])
        self.assertEqual(result["timeout_count"], 0)
        self.assertEqual(result["attempt_count"], 1)
        self.assertEqual(self.calls, 1)
        self.assertTrue(evidence.receipt_is_current(self.root, validation.read_object(self.worker.success_path), 1, 1))

    def test_mid_scan_change_retains_accounting_and_waits(self):
        self.worker.tick()
        old = validation.read_object(self.worker.state_path)
        with patch.object(evidence, "context", side_effect=evidence.InputsChanged("validation_inputs_changed")):
            result = self.worker.tick()
        self.assertEqual(result["status"], "waiting_stable")
        self.assertEqual(result["attempt_count"], old["attempt_count"])
        self.assertEqual(self.calls, 1)
        self.assertTrue(validation.valid_accounting(result))

    def test_new_committed_data_validates_without_refresh_or_repair(self):
        with patch.object(memory, "recover_apply_journals") as recovery, patch.object(memory, "run_refresh_and_validate") as legacy:
            first = self.worker.tick()
            self.assertEqual(first["status"], "verified")
            self.assertEqual(first["attempt_count"], 1)
            self.assertEqual(self.note.read_text(), "first business version")
            self.change_generation(2)
            self.note.write_text("new committed business version")
            second = self.worker.tick()
            self.assertEqual(second["status"], "verified")
            self.assertEqual(second["index_generation"], 2)
            self.assertNotEqual(first["input_key"], second["input_key"])
            recovery.assert_not_called()
            legacy.assert_not_called()
        self.assertEqual(self.calls, 2)

    def test_duplicate_poll_does_not_repeat_or_rewrite_state(self):
        self.worker.tick()
        before = self.worker.state_path.read_bytes(), self.worker.history_path.read_bytes()
        for _ in range(3):
            self.current += timedelta(minutes=1)
            self.assertEqual(self.worker.tick()["status"], "verified")
        self.assertEqual(self.calls, 1)
        self.assertEqual(before, (self.worker.state_path.read_bytes(), self.worker.history_path.read_bytes()))

    def test_pending_index_and_maintenance_do_not_consume_attempt(self):
        self.change_generation(2, 1)
        self.assertEqual(self.worker.tick()["error_code"], "memory_index_update_pending")
        self.assertFalse(self.worker.state_path.exists())
        self.change_generation(2)
        validation.atomic_json(self.root / "state/maintenance-mode.json", {"status": "active"})
        self.assertEqual(self.worker.tick()["error_code"], "maintenance_active")
        self.assertFalse(self.worker.state_path.exists())
        self.assertEqual(self.calls, 0)

    def test_generation_is_rechecked_after_taking_writer_lock(self):
        with patch.object(validation, "index_state", side_effect=[
            {"dirty": 1, "applied": 1, "ready": True}, {"dirty": 2, "applied": 1, "ready": False}]):
            self.assertEqual(self.worker.tick()["error_code"], "memory_index_update_pending")
        self.assertFalse(self.worker.state_path.exists())
        self.assertEqual(self.calls, 0)

    def test_busy_writer_and_duplicate_worker_leave_job_unchanged(self):
        self.worker.runner = self.timeout
        self.worker.tick()
        before = self.worker.state_path.read_bytes()
        self.current += timedelta(minutes=1)
        for name, code in (("memory-index.lock", "memory_core_busy"),
                           ("automatic-memory-validation.lock", "validation_already_running")):
            with validation.try_lock(self.root / "state" / name) as handle:
                self.assertIsNotNone(handle)
                self.assertEqual(self.worker.tick()["error_code"], code)
            self.assertEqual(before, self.worker.state_path.read_bytes())
        self.assertEqual(self.calls, 1)

    def test_timeout_budget_survives_restart_and_stops_at_three(self):
        self.worker.runner = self.timeout
        for attempt in (1, 2, 3):
            worker = validation.AutomaticValidation(self.root, clock=lambda: self.current, runner=self.timeout)
            result = worker.tick()
            self.assertEqual(result["attempt_count"], attempt)
            self.assertEqual(result["status"], "retry_wait" if attempt < 3 else "failed")
            self.assertEqual(worker.tick()["attempt_count"], attempt)
            self.current += timedelta(seconds=60)
        before = self.worker.state_path.read_bytes()
        self.current += timedelta(days=2)
        self.assertEqual(self.worker.tick()["attempt_count"], 3)
        self.assertEqual(before, self.worker.state_path.read_bytes())
        self.assertEqual(self.calls, 3)
        self.assertFalse(repair.eligible({"status": "open", "source_id": "memory_core", "category": "core_failure",
                                         "error_code": "automatic_validation_timeout"}))

    def test_persistent_failure_stops_until_real_input_changes(self):
        self.worker.runner = self.fail_validation
        first = self.worker.tick()
        before = self.worker.failure_path.read_bytes()
        self.current += timedelta(days=1)
        self.assertEqual(self.worker.tick(), first)
        self.assertEqual(self.calls, 1)
        self.assertEqual(before, self.worker.failure_path.read_bytes())
        self.note.write_text("genuinely new input")
        second = self.worker.tick()
        self.assertEqual(self.calls, 2)
        self.assertEqual(second["attempt_count"], 1)
        self.assertNotEqual(first["input_key"], second["input_key"])

    def test_source_and_code_changes_trigger_even_without_generation_change(self):
        first = self.worker.tick()
        self.note.write_text("later external edit")
        second = self.worker.tick()
        (self.root / "bin/validate_memory.rb").write_text("new validator version")
        third = self.worker.tick()
        self.assertEqual(len({job["input_key"] for job in (first, second, third)}), 3)
        self.assertEqual(self.calls, 3)

    def test_changed_inputs_during_failed_check_are_deferred_not_corruption(self):
        def changed(*args):
            result = self.fail_validation(*args)
            self.note.write_text("user edited while checking")
            return result
        self.worker.runner = changed
        result = self.worker.tick()
        self.assertEqual(result["status"], "waiting_stable")
        self.assertFalse(self.worker.failure_path.exists())
        self.worker.runner = self.succeed
        self.current += timedelta(seconds=60)
        self.assertEqual(self.worker.tick()["status"], "verified")
        self.assertEqual(self.calls, 2)

    def test_data_change_after_success_publication_never_claims_current(self):
        def changed(*args):
            result = self.succeed(*args)
            self.note.write_text("new content after the published check")
            return result
        self.worker.runner = changed
        result = self.worker.tick()
        self.assertEqual(result["status"], "waiting_stable")
        self.assertFalse(evidence.receipt_is_current(self.root, validation.read_object(self.worker.success_path), 1, 1))
        self.assertFalse(self.worker.failure_path.exists())

    def test_explicit_deferred_result_waits_for_a_different_snapshot(self):
        def deferred(*_args):
            self.calls += 1
            return subprocess.CompletedProcess([], 2, '{"verdict":"deferred","error_code":"validation_inputs_changed","errors":[]}', '')
        self.worker.runner = deferred
        self.worker.tick()
        self.worker.tick()
        self.assertEqual(self.calls, 1)
        self.assertFalse(self.worker.failure_path.exists())

    def test_exit_zero_without_this_runs_full_receipt_is_failure(self):
        def no_receipt(*_args):
            self.calls += 1
            return subprocess.CompletedProcess([], 0, '{"verdict":"pass","errors":[]}', '')
        self.worker.runner = no_receipt
        self.assertEqual(self.worker.tick()["error_code"], "validation_receipt_unproven")
        self.worker.tick()
        self.assertEqual(self.calls, 1)

    def test_invalid_validator_output_is_a_persistent_visible_failure(self):
        for output in ("not JSON", "[]", "null"):
            self.note.write_text("separate input for " + output)
            self.worker.runner = lambda *_args: subprocess.CompletedProcess([], 0, output, "")
            result = self.worker.tick()
            self.assertEqual((result["status"], result["error_code"]), ("failed", "validation_output"))
            self.assertEqual(validation.read_object(self.worker.failure_path)["error_code"], "validation_output")

    def test_permission_failure_from_validator_has_no_automatic_retry(self):
        def denied(*_args):
            self.calls += 1
            return subprocess.CompletedProcess([], 1, '{"verdict":"fail","error_code":"validation_permission_denied","errors":[]}', '')
        self.worker.runner = denied
        result = self.worker.tick()
        self.assertEqual((result["status"], result["attempt_count"]), ("failed", 1))
        self.current += timedelta(days=1)
        self.worker.tick()
        self.assertEqual(self.calls, 1)

    def test_other_runs_receipt_cannot_prove_this_run_completed(self):
        def wrong_id(config, _run_id, descriptors):
            return self.succeed(config, "different-run", descriptors)
        self.worker.runner = wrong_id
        self.assertEqual(self.worker.tick()["error_code"], "validation_receipt_unproven")
        self.worker.tick()
        self.assertEqual(self.calls, 1)

    def test_crash_after_reservation_keeps_attempt_and_requires_attention(self):
        def crash(*_args):
            self.calls += 1
            raise SystemExit("simulated owner interruption")
        self.worker.runner = crash
        with self.assertRaises(SystemExit):
            self.worker.tick()
        reserved = validation.read_object(self.worker.state_path)
        self.assertEqual((reserved["status"], reserved["attempt_count"]), ("running", 1))
        self.worker.runner = self.succeed
        stopped = self.worker.tick()
        self.assertEqual((stopped["status"], stopped["attempt_count"]), ("interrupted", 1))
        self.worker.tick()
        self.assertEqual(self.calls, 1)

    def test_crash_after_genuine_success_can_adopt_the_current_full_receipt(self):
        def crash_after_receipt(*args):
            self.succeed(*args)
            raise SystemExit("interrupted after receipt")
        self.worker.runner = crash_after_receipt
        with self.assertRaises(SystemExit):
            self.worker.tick()
        self.assertEqual(self.worker.tick()["status"], "verified")
        self.assertEqual(self.calls, 1)

    def test_permission_denial_is_explicit_and_never_runs_or_consumes(self):
        with patch.object(evidence, "context", side_effect=PermissionError):
            for _ in range(2):
                result = self.worker.tick()
                self.assertEqual(result["error_code"], "validation_permission_denied")
                self.assertEqual(result["attempt_count"], 0)
        self.assertEqual(self.calls, 0)
        self.current += timedelta(minutes=1)
        self.assertEqual(self.worker.tick()["status"], "verified")
        self.assertEqual(self.calls, 1)

    def test_corrupt_or_impossible_accounting_is_preserved_and_fail_closed(self):
        for damaged in ('{not valid JSON', '{}', '{"schema_version":1,"attempt_count":4,"status":"failed"}',
                        '{"schema_version":1,"attempt_count":"not-a-number"}',
                        '{"schema_version":1,"attempt_count":1,"status":"running"}'):
            self.worker.state_path.write_text(damaged)
            for _ in range(2):
                self.current += timedelta(minutes=1)
                self.assertEqual(self.worker.tick()["error_code"], "automatic_validation_state_invalid")
                self.assertEqual(self.worker.state_path.read_text(), damaged)
        self.assertEqual(self.calls, 0)

    def test_validation_state_receipts_and_history_do_not_invalidate_input(self):
        before = evidence.context(self.root)
        self.worker.tick()
        after = evidence.context(self.root)
        self.assertEqual(before, after)
        self.assertEqual(os.stat(self.worker.state_path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.worker.history_path).st_mode & 0o777, 0o600)

    def test_current_manual_receipt_can_recover_without_resetting_failure_history(self):
        self.worker.runner = self.fail_validation
        self.worker.tick()
        previous = self.worker.failure_path.read_bytes()
        self.current += timedelta(minutes=1)
        validation.atomic_json(self.worker.success_path, self.receipt("explicit-manual-validation"))
        self.assertEqual(self.worker.tick()["status"], "verified")
        self.assertEqual(self.worker.failure_path.read_bytes(), previous)
        self.assertEqual(self.calls, 1)

    def test_real_command_is_fixed_read_only_and_inherits_both_locks(self):
        with patch.object(validation.validation_process, "run", return_value=subprocess.CompletedProcess([], 0, '{}', '')) as command:
            self.worker.run_validator(self.config, "fixture-run", (10, 11))
        args, options = command.call_args
        self.assertEqual(args[0], ["/usr/bin/ruby", str(self.root / "bin/validate_memory.rb"), str(self.memory), self.config["external_root"]])
        self.assertEqual(options["pass_fds"], (10, 11))
        self.assertEqual(options["env"]["BUSINESS_VALIDATION_RUN_ID"], "fixture-run")
        self.assertEqual(options["env"]["BUSINESS_MEMORY_INDEX_LOCK_HELD"], "1")
        self.assertNotIn("BUSINESS_SEMANTIC_WRITE_IN_PROGRESS", options["env"])

    def test_child_retains_locks_if_parent_descriptors_close_without_unlock(self):
        paths = [self.root / "state/automatic-memory-validation.lock", self.root / "state/memory-index.lock"]
        descriptors = tuple(os.open(path, os.O_RDWR | os.O_CREAT, 0o600) for path in paths)
        for descriptor in descriptors:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        child = subprocess.Popen(["/usr/bin/python3", "-c", "import sys; sys.stdin.buffer.read()"],
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 pass_fds=descriptors)
        for descriptor in descriptors:
            os.close(descriptor)
        try:
            for path in paths:
                with validation.try_lock(path) as acquired:
                    self.assertIsNone(acquired)
        finally:
            child.stdin.close()
            child.wait(timeout=5)
        for path in paths:
            with validation.try_lock(path) as acquired:
                self.assertIsNotNone(acquired)

    def test_stability_requires_two_samples_and_releases_locks(self):
        with patch.object(validation, "STABLE_SECONDS", 60):
            first = self.worker.tick()
            self.assertEqual(first["status"], "waiting_stable")
            self.assertEqual(self.calls, 0)
            with validation.try_lock(self.root / "state/memory-index.lock") as lock:
                self.assertIsNotNone(lock)
            self.current += timedelta(seconds=40)
            self.note.write_text("another committed version")
            second = self.worker.tick()
            self.assertEqual(second["pending_since"], first["pending_since"])
            self.current += timedelta(seconds=59)
            self.assertEqual(self.worker.tick()["status"], "waiting_stable")
            self.current += timedelta(seconds=1)
            self.assertEqual(self.worker.tick()["status"], "verified")
            self.assertEqual(self.calls, 1)

    def test_same_fingerprint_after_churn_is_retried_with_backoff(self):
        with patch.object(validation, "STABLE_SECONDS", 60):
            def changed(*_args):
                self.calls += 1
                return subprocess.CompletedProcess([], 2, '{"verdict":"deferred","error_code":"validation_inputs_changed"}', '')
            self.worker.runner = changed
            self.worker.tick()
            self.current += timedelta(seconds=60)
            first = self.worker.tick()
            self.assertEqual(first["defer_count"], 1)
            self.assertEqual(first["timeout_count"], 0)
            self.worker.runner = self.succeed
            # The inner check saw A->B->A, even though the outer fingerprint is A.
            self.worker.tick()
            self.current += timedelta(seconds=60)
            self.assertEqual(self.worker.tick()["status"], "verified")
            self.assertEqual(self.calls, 2)

    def test_churn_backoff_and_episode_survive_changed_inputs(self):
        def changed(*_args):
            self.calls += 1
            self.note.write_text(str(self.calls))
            return subprocess.CompletedProcess([], 2, '{"verdict":"deferred","error_code":"validation_inputs_changed"}', '')
        self.worker.runner = changed
        since = None
        for count, seconds in enumerate((60, 120, 300, 600, 600), 1):
            result = self.worker.tick()
            self.assertEqual(result["defer_count"], count)
            self.assertEqual(validation.parsed(result["next_retry_at"]) - self.current, timedelta(seconds=seconds))
            since = since or result["pending_since"]
            self.assertEqual(result["pending_since"], since)
            self.current += timedelta(seconds=seconds)
        self.assertEqual(self.calls, 5)
        self.assertFalse(self.worker.failure_path.exists())

    def test_reboot_resamples_without_resetting_budget(self):
        with patch.object(validation, "STABLE_SECONDS", 60):
            self.worker.tick()
            self.current += timedelta(seconds=60)
            self.worker.runner = self.timeout
            result = self.worker.tick()
            self.assertEqual(result["timeout_count"], 1)
            self.current += timedelta(seconds=60)
            self.worker.boot_id = "new-boot"
            self.assertEqual(self.worker.tick()["status"], "waiting_stable")
            self.assertEqual(self.calls, 1)
            self.current += timedelta(seconds=60)
            result = self.worker.tick()
            self.assertEqual(result["timeout_count"], 2)
            self.assertEqual(self.calls, 2)

    def test_timeout_budget_not_reset_by_churn(self):
        self.worker.runner = self.timeout
        self.worker.tick()
        self.current += timedelta(seconds=60)
        self.note.write_text("new source input during retry wait")
        self.assertEqual(self.worker.tick()["timeout_count"], 2)
        self.current += timedelta(seconds=60)
        self.assertEqual(self.worker.tick()["status"], "failed")
        self.assertEqual(self.calls, 3)

    def test_v1_superseded_migrates_without_new_input(self):
        self.worker.tick()
        job = validation.read_object(self.worker.state_path)
        self.worker.success_path.unlink()
        job.update(schema_version=1, status="superseded", error_code="validation_inputs_changed")
        validation.atomic_json(self.worker.state_path, job)
        result = self.worker.tick()
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(self.calls, 2)

    def test_request_after_same_input_success_forces_fresh_validation(self):
        self.worker.tick()
        self.current += timedelta(seconds=30)
        validation.atomic_json(self.root / "state/validation-request.json", {"schema_version": 1, "requested_at": validation.stamp(self.current), "source_run_id": "later-source-run"})
        with patch.object(validation, "STABLE_SECONDS", 60):
            self.assertEqual(self.worker.tick()["status"], "waiting_stable")
            self.current += timedelta(seconds=60)
            self.assertEqual(self.worker.tick()["status"], "verified")
        self.assertEqual(self.calls, 2)

    def test_inventory_waits_for_stability_and_is_not_repeated(self):
        self.config["source_governance"] = {"inventory_auto_refresh": True}
        validation.atomic_json(self.root / "config.json", self.config)
        def inventory(*args, **kwargs):
            self.note.write_text("controlled inventory change")
            return subprocess.CompletedProcess([], 0, '{"references_updated":false}', '')
        with patch.object(validation, "STABLE_SECONDS", 60), patch.object(validation.subprocess, "run", side_effect=inventory) as scan:
            self.assertEqual(self.worker.tick()["status"], "waiting_stable")
            scan.assert_not_called()
            self.current += timedelta(seconds=60)
            self.assertEqual(self.worker.tick()["status"], "waiting_stable")
            self.assertEqual(scan.call_count, 1)
            self.current += timedelta(seconds=60)
            self.assertEqual(self.worker.tick()["status"], "verified")
            self.current += timedelta(seconds=60)
            self.assertEqual(self.worker.tick()["status"], "verified")
            self.assertEqual(scan.call_count, 1)

    def test_inventory_timeout_stops_until_real_input_changes(self):
        self.config["source_governance"] = {"inventory_auto_refresh": True}
        validation.atomic_json(self.root / "config.json", self.config)
        with patch.object(validation.subprocess, "run", side_effect=subprocess.TimeoutExpired("inventory", 120)) as scan:
            self.assertEqual(self.worker.tick()["error_code"], "source_inventory_refresh_timeout")
            self.current += timedelta(minutes=5)
            self.assertEqual(self.worker.tick()["status"], "failed")
            self.assertEqual(scan.call_count, 1)
            self.assertEqual(self.calls, 0)

    def test_legacy_wrapper_does_not_publish_failure_for_changed_inputs(self):
        result = subprocess.CompletedProcess([], 2, '{"verdict":"deferred","error_code":"validation_inputs_changed","errors":[]}', '')
        with patch.object(memory, "assert_validation_access"), patch.object(memory, "run_local_verification_command", return_value=(result, 1)), patch.object(memory, "atomic_write") as write:
            with self.assertRaises(memory.AutomationError) as failure:
                memory.run_refresh_and_validate(self.config, refresh_indexes=False)
            self.assertEqual(failure.exception.code, "validation_inputs_changed")
            write.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
