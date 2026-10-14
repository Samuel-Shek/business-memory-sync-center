"""Synthetic native-process tests. Never read production configuration/data."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import sqlite_attribution as attribution
import sqlite_write_transactions as tx


class Attribution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name)/'fixture.sqlite'
        with sqlite3.connect(self.db) as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.execute('CREATE TABLE counter(value)')
            c.execute('INSERT INTO counter VALUES(0)')

    def tearDown(self):
        self.tmp.cleanup()

    def connect(self, path=None, timeout=.01):
        return attribution.attach(sqlite3.connect(path or self.db, timeout=timeout), path or self.db)

    def write(self, action, **kw):
        return tx.write_transaction(self.db, action, connector=self.connect, phase='test',
                                    budget=1, attempt_wait=.02, **kw)

    def events(self):
        return attribution.recent(self.db, 0, limit=1000)['events']

    def test_external_uninstrumented_owner_identified(self):
        code = ('import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); '
                'c.execute("BEGIN IMMEDIATE"); print("ready",flush=True); '
                'sys.stdin.readline(); c.rollback(); c.close()')
        p = subprocess.Popen([sys.executable, '-c', code, str(self.db)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(p.stdout.readline().strip(), 'ready')
            snap = attribution.snapshot(self.db)
            self.assertEqual(snap['lock_owner'], p.pid)
            self.assertEqual(snap['status'], 'lock_holder_observed')
            self.assertNotEqual(snap['os_probe']['probe_pid'], os.getpid())
            self.assertTrue(any(r.get('process_started_at') for r in snap['os_probe']['processes']))
        finally:
            p.communicate('\n', timeout=5)

    def test_probe_cannot_release_owning_process_locks(self):
        c = self.connect(); c.execute('BEGIN IMMEDIATE')
        try:
            for _ in range(3):
                self.assertEqual(attribution.snapshot(self.db)['lock_owner'], os.getpid())
                d = sqlite3.connect(self.db, timeout=.001)
                try:
                    with self.assertRaises(sqlite3.OperationalError): d.execute('BEGIN IMMEDIATE')
                finally: d.close()
        finally: c.rollback(); c.close()

    def test_reader_is_not_mislabeled_as_writer(self):
        c = self.connect(); c.execute('BEGIN'); c.execute('SELECT * FROM counter').fetchall()
        try:
            snap = attribution.snapshot(self.db)
            self.assertEqual(snap['lock_owner'], 'unknown')
            self.assertNotEqual(snap['status'], 'lock_holder_observed')
        finally: c.close()

    def test_released_owner_is_not_reported_as_current(self):
        c = self.connect(); c.execute('BEGIN IMMEDIATE'); c.rollback(); c.close()
        snap = attribution.snapshot(self.db)
        self.assertEqual(snap['lock_owner'], 'unknown')
        self.assertTrue(snap.get('gap'))

    def test_failed_retry_has_owner_and_shared_transaction_id(self):
        blocker = self.connect(); blocker.execute('BEGIN IMMEDIATE')
        try:
            with self.assertRaises(sqlite3.OperationalError) as error:
                self.write(lambda c: c.execute('UPDATE counter SET value=value+1').rowcount)
            evidence = error.exception.safe_sqlite_evidence
            self.assertTrue(evidence['transaction_id'])
            rows = [json.loads(r) for r in (self.db.parent/'sqlite-write-contention.jsonl').read_text().splitlines()]
            self.assertEqual(len({r['transaction_id'] for r in rows}), 1)
            self.assertEqual(rows[0]['attribution']['lock_owner'], os.getpid())
        finally: blocker.close()

    def test_trace_preserves_commit_and_rollback_behavior(self):
        c = self.connect()
        with c: c.execute('UPDATE counter SET value=1')
        try:
            with c:
                c.cursor().execute('UPDATE counter SET value=2')
                raise ValueError('fixture')
        except ValueError: pass
        self.assertEqual(c.execute('SELECT value FROM counter').fetchone()[0], 1)
        c.close()
        kinds = {r.get('statement_kind') for r in self.events()}
        self.assertTrue({'BEGIN', 'UPDATE', 'COMMIT', 'ROLLBACK'} <= kinds)

    def test_trace_does_not_extend_connection_lifetime(self):
        import gc
        was_enabled = gc.isenabled()
        gc.disable()
        try:
            # Trace must not create a connection -> callback -> connection cycle.
            c = attribution.attach(sqlite3.connect(self.db, cached_statements=0), self.db)
            c.execute('BEGIN IMMEDIATE')
            del c
            d = sqlite3.connect(self.db, timeout=.01)
            try: d.execute('BEGIN IMMEDIATE'); d.rollback()
            finally: d.close()
        finally:
            if was_enabled: gc.enable()

    def test_no_sql_parameters_business_text_or_paths_logged(self):
        secret = 'PRIVATE-business-body-and-token-123'
        c = self.connect(); c.execute('INSERT INTO counter VALUES(?)', (secret,)); c.commit(); c.close()
        text = attribution.journal_path(self.db).read_text()
        self.assertNotIn(secret, text)
        self.assertNotIn(str(self.db), text)
        self.assertNotIn('INSERT INTO counter', text)
        self.assertTrue(any(f.get('loaded_code_sha256') for r in self.events() for f in r['frames']))

    def test_trace_callback_failure_does_not_change_result(self):
        with patch.object(attribution, 'record', side_effect=OSError('fixture')):
            c = self.connect(); c.execute('UPDATE counter SET value=4'); c.commit(); c.close()
        with sqlite3.connect(self.db) as c:
            self.assertEqual(c.execute('SELECT value FROM counter').fetchone()[0], 4)

    def test_journal_write_error_cannot_replay_success(self):
        with patch.object(attribution.os, 'open', side_effect=PermissionError('fixture')):
            self.write(lambda c: c.execute('UPDATE counter SET value=value+1').rowcount)
        with sqlite3.connect(self.db) as c:
            self.assertEqual(c.execute('SELECT value FROM counter').fetchone()[0], 1)

    def test_diagnostic_lock_never_blocks_database(self):
        path = str(attribution.journal_path(self.db))+'.lock'
        with open(path, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            started = time.monotonic()
            self.write(lambda c: c.execute('UPDATE counter SET value=7').rowcount)
            self.assertLess(time.monotonic()-started, .5)

    def test_rotation_is_bounded_and_retains_recent_records(self):
        with patch.object(attribution, 'MAX_BYTES', 300):
            for n in range(70): attribution.record(self.db, {'event': 'fixture', 'n': n})
        p = attribution.journal_path(self.db)
        self.assertTrue(p.exists())
        self.assertFalse(Path(str(p)+'.4').exists())
        self.assertEqual(self.events()[-1]['n'], 69)
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)

    def test_probe_unavailable_keeps_explicit_evidence_gap(self):
        with patch.object(attribution.subprocess, 'run', side_effect=subprocess.TimeoutExpired('probe', .01)):
            snap = attribution.snapshot(self.db)
        self.assertEqual(snap['status'], 'incomplete')
        self.assertEqual(snap['lock_owner'], 'unknown')
        self.assertIn('timed_out', snap['gap'])

    def test_unexpected_probe_error_cannot_mask_original_sqlite_failure(self):
        with patch.object(attribution.subprocess, 'run', side_effect=RuntimeError('private diagnostic error')):
            c = self.connect(); c.execute('BEGIN IMMEDIATE')
            try:
                with self.assertRaises(sqlite3.OperationalError) as caught:
                    self.write(lambda db: db.execute('UPDATE counter SET value=5'))
                self.assertEqual(caught.exception.safe_sqlite_evidence['sqlite_code'], 5)
                self.assertEqual(caught.exception.safe_sqlite_evidence['attribution']['status'], 'incomplete')
                self.assertNotIn('private diagnostic error', json.dumps(caught.exception.safe_sqlite_evidence))
            finally: c.close()

    def test_runtime_incident_preserves_loaded_code_and_process_identity(self):
        import business_sync_supervisor as supervisor
        import supervisor_incidents as incidents
        root = Path(self.tmp.name)
        try:
            raise ValueError('private business text')
        except ValueError as exc:
            receipt = supervisor.record_runtime_error(exc, root=root, database=self.db)
        self.assertTrue(receipt['frames'][-1]['loaded_code_sha256'])
        self.assertTrue(receipt['process_context']['process_token'])
        incidents.import_history(root)
        stored = [json.loads(p.read_text()) for p in (root/'state/supervisor-incidents').glob('*.json')]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]['evidence']['frames'][-1]['loaded_code_sha256'], receipt['frames'][-1]['loaded_code_sha256'])
        self.assertNotIn('private business text', json.dumps(stored))

    def test_budget_exhaustion_does_not_spawn_probe(self):
        with patch.object(attribution.subprocess, 'run', side_effect=AssertionError('must not start')):
            snap = attribution.snapshot(self.db, timeout=0)
        self.assertEqual(snap['gap'], 'diagnostic_time_budget_exhausted')

    def test_cached_descriptor_follows_rotation_by_other_process(self):
        attribution.record(self.db, {'event': 'parent_before'})
        code = ('import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); '
                'import sqlite_attribution as a; a.MAX_BYTES=300; '
                '[a.record(Path(sys.argv[2]), {"event":"child","n":n}) for n in range(40)]')
        subprocess.run([sys.executable, '-c', code, str(Path(attribution.__file__).parent), str(self.db)], check=True, timeout=5)
        attribution.record(self.db, {'event': 'parent_after'})
        self.assertEqual(self.events()[-1]['event'], 'parent_after')

    def test_cached_diagnostic_descriptors_are_bounded(self):
        for n in range(20):
            attribution.record(self.db.parent/('fixture-'+str(n)+'.sqlite'), {'event': 'fixture'})
        self.assertLessEqual(len(attribution._journal_handles), 8)

    def test_fork_reinitializes_diagnostic_lock_and_process_token(self):
        attribution.record(self.db, {'event': 'before_fork'})
        token = attribution.PROCESS_TOKEN
        reader, writer = os.pipe()
        pid = os.fork()
        if pid == 0:
            try:
                os.close(reader)
                ok = attribution.record(self.db, {'event': 'fork_child'})
                os.write(writer, json.dumps({'ok': ok, 'new_token': attribution.PROCESS_TOKEN != token}).encode())
            finally: os._exit(0)
        os.close(writer)
        try: result = json.loads(os.read(reader, 1024))
        finally: os.close(reader); os.waitpid(pid, 0)
        self.assertTrue(result['ok']); self.assertTrue(result['new_token'])
        self.assertTrue(attribution.record(self.db, {'event': 'parent_survived'}))

    def test_malformed_diagnostic_schema_cannot_fail_committed_write(self):
        p = attribution.journal_path(self.db)
        p.write_text('{"recorded_at_ns":"invalid"}\n')
        snap = attribution.recent(self.db, 0)
        self.assertIn('invalid_journal_schema', snap['gaps'])
        self.assertEqual(self.write(lambda c: c.execute('UPDATE counter SET value=1').rowcount), 1)

    def test_one_commit_event_and_matching_transaction_timeline(self):
        calls = []
        def action(c):
            calls.append(1); c.execute('UPDATE counter SET value=value+1')
            if len(calls) == 1: raise sqlite3.OperationalError('database is locked')
            return 1
        self.assertEqual(self.write(action), 1)
        events = [r for r in self.events() if r.get('transaction_id')]
        self.assertEqual(len({r['transaction_id'] for r in events}), 1)
        self.assertEqual(sum(r.get('outcome') == 'committed' for r in events), 1)
        self.assertEqual(sum(r.get('outcome') == 'rolled_back' for r in events), 1)


if __name__ == '__main__': unittest.main()
