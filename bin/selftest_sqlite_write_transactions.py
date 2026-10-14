"""Synthetic contention tests. No production config, provider, or memory writes."""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import concurrent.futures
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import sqlite_write_transactions as tx
import business_sync_state as state
import operational_workbench as work
from selftest_operational_workbench import fixture


class Transactions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "ledger.sqlite"
        with sqlite3.connect(self.db) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("CREATE TABLE counter(value INTEGER)")
            c.execute("INSERT INTO counter VALUES(0)")
            c.execute("CREATE TABLE events(id INTEGER PRIMARY KEY)")

    def tearDown(self):
        self.tmp.cleanup()

    def connect(self, path, timeout):
        return sqlite3.connect(path, timeout=timeout)

    def run_write(self, callback, **kwargs):
        return tx.write_transaction(self.db, callback, connector=kwargs.pop("connector", self.connect),
                                    phase="fixture", budget=1, attempt_wait=.025, **kwargs)

    def value(self):
        with sqlite3.connect(self.db) as c:
            return c.execute("SELECT value FROM counter").fetchone()[0]

    def test_short_contention_releases_then_applies_once(self):
        blocker = sqlite3.connect(self.db, check_same_thread=False)
        blocker.execute("BEGIN IMMEDIATE")
        thread = threading.Thread(target=lambda: (time.sleep(.18), blocker.rollback(), blocker.close()))
        thread.start()
        try:
            self.run_write(lambda c: c.execute("UPDATE counter SET value=value+1").rowcount)
        finally:
            thread.join()
        self.assertEqual(self.value(), 1)
        rows = [json.loads(r) for r in (self.root / "sqlite-write-contention.jsonl").read_text().splitlines()]
        self.assertEqual(rows[-1]["outcome"], "recovered")
        self.assertLessEqual(rows[-1]["attempt"], 3)

    def test_persistent_lock_is_bounded_and_readers_still_work(self):
        blocker = sqlite3.connect(self.db)
        blocker.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        try:
            self.assertEqual(self.value(), 0)
            with self.assertRaises(sqlite3.OperationalError) as caught:
                self.run_write(lambda c: c.execute("UPDATE counter SET value=99"))
            self.assertGreaterEqual(caught.exception.safe_sqlite_evidence["attempt"], 1)
            self.assertLessEqual(caught.exception.safe_sqlite_evidence["attempt"], 3)
            self.assertEqual(caught.exception.safe_sqlite_evidence["outcome"], "failed")
        finally:
            blocker.rollback()
            blocker.close()
        self.assertLess(time.monotonic() - started, 1.2)
        self.assertEqual(self.value(), 0)

    def test_rollback_prevents_partial_or_duplicate_writes(self):
        calls = []
        def action(c):
            calls.append(1)
            c.execute("UPDATE counter SET value=value+1")
            c.execute("INSERT INTO events VALUES(1)")
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
            return c.execute("SELECT value FROM counter").fetchone()[0]
        self.assertEqual(self.run_write(action), 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.value(), 1)
        with sqlite3.connect(self.db) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM events").fetchone()[0], 1)

    def test_non_busy_errors_are_not_retried_or_partially_committed(self):
        for message in ["database table is locked", "database disk image is malformed",
                        "attempt to write a readonly database", "disk I/O error", "private fixture value"]:
            calls = []
            def action(c):
                calls.append(1)
                c.execute("UPDATE counter SET value=value+1")
                raise sqlite3.OperationalError(message)
            with self.assertRaises(sqlite3.OperationalError):
                self.run_write(action)
            self.assertEqual(len(calls), 1)
            self.assertEqual(self.value(), 0)
        self.assertNotIn("private fixture value", (self.root / "sqlite-write-contention.jsonl").read_text())

    def test_ambiguous_commit_is_never_replayed(self):
        calls = []
        class Connection:
            def __init__(self, c): self.c = c
            def __getattr__(self, name): return getattr(self.c, name)
            def commit(self):
                self.c.commit()
                raise sqlite3.OperationalError("database is locked")
        def action(c):
            calls.append(1)
            c.execute("UPDATE counter SET value=value+1")
        with self.assertRaises(sqlite3.OperationalError):
            self.run_write(action, connector=lambda path, timeout: Connection(self.connect(path, timeout)))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.value(), 1)

    def test_diagnostic_failure_cannot_replay_committed_transaction(self):
        count = []
        def action(c):
            count.append(1)
            c.execute("UPDATE counter SET value=value+1")
            if len(count) == 1:
                raise sqlite3.OperationalError("database is locked")
        with patch.object(tx.os, "open", side_effect=PermissionError("fixture")):
            self.run_write(action)
        self.assertEqual(self.value(), 1)
        self.assertEqual(len(count), 2)

    def test_concurrent_transactions_preserve_all_increments(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.run_write(
                lambda c: c.execute("UPDATE counter SET value=value+1").rowcount), range(24)))
        self.assertEqual(sum(results), 24)
        self.assertEqual(self.value(), 24)

    def test_retry_begins_with_fresh_shared_state(self):
        calls = []
        def action(c):
            observed = c.execute("SELECT value FROM counter").fetchone()[0]
            calls.append(observed)
            if len(calls) == 1: raise sqlite3.OperationalError("database is locked")
            c.execute("UPDATE counter SET value=?", (observed + 1,))
        def between(_):
            with sqlite3.connect(self.db) as c: c.execute("UPDATE counter SET value=10")
        self.run_write(action, sleeper=between)
        self.assertEqual(calls, [0, 10])
        self.assertEqual(self.value(), 11)

    def test_total_wait_deadline_is_shared_across_connect_and_begin(self):
        clock = [0.0]
        timeouts = []
        def connector(path, timeout):
            timeouts.append(timeout)
            clock[0] += timeout
            raise sqlite3.OperationalError("database is locked")
        with self.assertRaises(sqlite3.OperationalError):
            tx.write_transaction(self.db, lambda c: None, connector=connector, phase="fixture",
                                 clock=lambda: clock[0], sleeper=lambda seconds: clock.__setitem__(0, clock[0]+seconds))
        self.assertLessEqual(clock[0], 15)
        self.assertEqual(len(timeouts), 3)


class LedgerPaths(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = fixture(self.root)
        self.db = self.root / "state/sync.sqlite"

    def tearDown(self): self.tmp.cleanup()

    def test_alert_retry_rolls_back_count_and_incident_registration(self):
        original = state._write
        attempts = []
        def inject(database, phase, operation, **kwargs):
            def fail_once(c):
                result = operation(c)
                attempts.append(1)
                if len(attempts) == 1: raise sqlite3.OperationalError("database is locked")
                return result
            return original(database, phase, fail_once, **kwargs)
        with patch.object(state, "_write", side_effect=inject):
            result = state.open_alert("system", "fixture", "fixture", "fixture", incident_id="a"*64)
        same = state.open_alert("system", "fixture", "fixture", "fixture", incident_id="a"*64)
        self.assertEqual(result["alert_id"], same["alert_id"])
        self.assertEqual(same["occurrence_count"], 1)
        with sqlite3.connect(self.db) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM supervisor_incident_receipts WHERE incident_id=?", ("a"*64,)).fetchone()[0], 1)

    def test_post_commit_read_failure_does_not_replay_update(self):
        with patch.object(state, "runtime", side_effect=sqlite3.OperationalError("database is locked")), \
                patch.object(state, "_write", wraps=state._write) as writer:
            with self.assertRaises(sqlite3.OperationalError):
                state.update_runtime(status="waiting_idle")
        self.assertEqual(writer.call_count, 1)
        with sqlite3.connect(self.db) as c:
            self.assertEqual(c.execute("SELECT status FROM supervisor_runtime").fetchone()[0], "waiting_idle")

    def test_workbench_retry_preserves_business_steps_and_reads_outside_write_lock(self):
        def business_snapshot():
            with sqlite3.connect(self.db) as c:
                return {t: c.execute("SELECT * FROM "+t).fetchall() for t in
                        ["action_runs", "action_steps", "supervisor_sources", "operational_actions"]}
        before = business_snapshot()
        original = work._upsert
        calls = []
        def fail_once(*args):
            result = original(*args)
            calls.append(1)
            if len(calls) == 1: raise sqlite3.OperationalError("database is locked")
            return result
        def unlocked_media():
            with sqlite3.connect(self.db, timeout=.001) as c:
                c.execute("BEGIN IMMEDIATE")
                c.rollback()
            return None
        with patch.object(work, "_upsert", side_effect=fail_once), \
                patch.object(work, "_mail", wraps=work._mail) as mail, \
                patch.object(work, "pending_media_ids", side_effect=unlocked_media) as media, \
                patch.object(work.subprocess, "Popen", side_effect=AssertionError("unexpected external action")):
            work.reconcile(self.app.config)
        self.assertEqual(mail.call_count, 1)
        self.assertEqual(media.call_count, 1)
        self.assertEqual(before, business_snapshot())
        with sqlite3.connect(self.db) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM operational_issue_events WHERE event_type='detected'").fetchone()[0],
                             c.execute("SELECT count(*) FROM operational_issues").fetchone()[0])

    def test_exact_three_paths_recover_after_transient_lock(self):
        operations = [
            lambda: state.open_alert("dingtalk_minutes", "fixture_minutes", "fixture", "fixture"),
            lambda: state.open_alert("dingtalk_chat", "fixture_chat", "fixture", "fixture"),
            lambda: work.reconcile(self.app.config),
        ]
        for operation in operations:
            blocker = sqlite3.connect(self.db, check_same_thread=False)
            blocker.execute("BEGIN IMMEDIATE")
            worker = threading.Thread(target=lambda: (time.sleep(.1), blocker.rollback(), blocker.close()))
            worker.start()
            try: operation()
            finally: worker.join()
        with sqlite3.connect(self.db) as c:
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchall(), [("ok",)])


if __name__ == "__main__": unittest.main()
