#!/usr/bin/env python3
"""Offline regression of the exact historical supervisor failure paths."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import bounded_repair_actions as bounded
import business_sync_state as state
import supervisor_incidents as incidents


@contextmanager
def held_lock(path):
    child = subprocess.Popen([
        sys.executable, "-c",
        'import fcntl,sys; f=open(sys.argv[1],"a+"); '
        'fcntl.flock(f,fcntl.LOCK_EX); print("held",flush=True); sys.stdin.read()', str(path)
    ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        if child.stdout.readline().strip() != "held":
            raise AssertionError("fixture_lock_not_held")
        yield
    finally:
        child.communicate("", timeout=5)


class RuntimeRecovery(unittest.TestCase):
    def test_queue_contention_defers_without_opening_database_or_consuming_attempt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with held_lock(root / "repair.lock"), \
                    patch.object(bounded, "LOCK", root / "repair.lock"), \
                    patch.object(example-host.invalid, "maintenance_active", return_value=False), \
                    patch.object(bounded.syncv2, "connect", side_effect=AssertionError("unexpected_database_write")):
                result = bounded.execute_queued(root / "config.json")
            self.assertEqual(result["status"], "already_running")
            self.assertEqual(result["error_code"], "repair_action_lock_held")
            self.assertIs(result["attempt_consumed"], False)
            self.assertIs(result["fault_cleared"], False)

    def test_released_queue_lock_reaches_normal_idle_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with held_lock(root / "repair.lock"):
                pass
            with patch.object(bounded, "LOCK", root / "repair.lock"), \
                    patch.object(example-host.invalid, "maintenance_active", return_value=False), \
                    patch.object(bounded.syncv2, "connect", side_effect=lambda: sqlite3.connect(root / "queue.sqlite")):
                self.assertEqual(bounded.execute_queued(root / "config.json"), {"status": "idle"})

    def test_non_contention_sync_error_remains_visible(self):
        with patch.object(example-host.invalid, "maintenance_active", return_value=False), \
                patch.object(bounded.syncv2, "file_lock", side_effect=bounded.syncv2.SyncV2Error("database_missing", "fixture")):
            with self.assertRaises(bounded.syncv2.SyncV2Error):
                bounded.execute_queued(Path("unused"))

    def test_permission_error_remains_visible(self):
        with patch.object(example-host.invalid, "maintenance_active", return_value=False), \
                patch.object(bounded.syncv2, "file_lock", side_effect=PermissionError("fixture")):
            with self.assertRaises(PermissionError):
                bounded.execute_queued(Path("unused"))

    def test_maintenance_does_not_acquire_queue_lock(self):
        with patch.object(example-host.invalid, "maintenance_active", return_value=True), \
                patch.object(bounded.syncv2, "file_lock", side_effect=AssertionError("unexpected_lock")):
            self.assertEqual(bounded.execute_queued(Path("unused")), {"status": "maintenance"})

    def test_missing_action_contract_restored_in_fresh_runtime(self):
        result = subprocess.run([
            sys.executable, "-c",
            'import bounded_repair_actions as b, dingtalk_sync_v2 as s; '
            'assert b.ACTIONS is s.BOUNDED_REPAIR_ACTION_TYPES; '
            'assert b.ACTIONS == frozenset({"preserve_current", "manual_listener_acceptance"}); '
            'print("contract_verified")'
        ], cwd=Path(__file__).resolve().parent, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "contract_verified")

    def test_current_receipts_bind_repair_module_and_its_action_contract(self):
        binding = incidents.code_binding(Path(__file__).resolve().parents[1])
        self.assertIn("bounded_repair_actions.py", binding)
        self.assertIn("dingtalk_sync_v2.py", binding)

    def test_verified_database_outage_does_not_suppress_a_new_episode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(state, "now_iso", return_value="2020-01-02T10:00:00+08:00"):
                first = incidents.retain_database_outage(root, sqlite3.OperationalError("database is locked"), root / "missing.sqlite")
            same = incidents.retain_database_outage(root, sqlite3.OperationalError("database is locked"), root / "missing.sqlite")
            self.assertEqual(first["incident_id"], same["incident_id"])
            first["root_cause_status"] = "verified"
            incidents.atomic_json(incidents.inbox(root) / (first["incident_id"] + ".json"), first)
            with patch.object(state, "now_iso", return_value="2020-01-02T10:01:00+08:00"):
                later = incidents.retain_database_outage(root, sqlite3.OperationalError("database is locked"), root / "missing.sqlite")
            self.assertNotEqual(first["incident_id"], later["incident_id"])
            self.assertEqual(later["root_cause_status"], "pending")

    def test_sqlite_busy_is_reproduced_and_original_paths_recover_after_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "ledger.sqlite"
            state.initialize(database)
            original = state._connect

            def short_wait(path=None, **kwargs):
                connection = original(path, **kwargs)
                connection.execute("PRAGMA busy_timeout=25")
                return connection

            operations = [
                lambda: state.mark_all_due(database=database),
                lambda: state.open_alert("system", "fixture_busy", "fixture", "fixture", database=database),
            ]
            blocker = sqlite3.connect(database)
            try:
                blocker.execute("BEGIN IMMEDIATE")
                # The already-initialized schema now uses a read-only fast path.
                state.initialize(database)
                with patch.object(state, "_connect", side_effect=short_wait):
                    for operation in operations:
                        with self.assertRaises(sqlite3.OperationalError) as caught:
                            operation()
                        self.assertEqual(str(caught.exception), "database is locked")
            finally:
                blocker.rollback()
                blocker.close()
            import ledger_recovery
            from datetime import timedelta
            resumed = ledger_recovery.now() + timedelta(minutes=6)
            with patch.object(ledger_recovery, "now", return_value=resumed):
                for operation in operations:
                    operation()
            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
                self.assertEqual(connection.execute("SELECT count(*) FROM sync_alerts WHERE category='fixture_busy'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
