#!/usr/bin/env python3
"""Offline tests: synthetic receipts/temp roots and this test process only.

Run with python3 -B selftest_supervisor_liveness.py. No production, network,
launchctl, workers, notifications, model inputs or business content are used.
"""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import json
import os
import sys
sys.dont_write_bytecode = True
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import supervisor_liveness as live

BOOT = 'a2b81d52-6623-5666-90a1-f6551d14424d'
WALL = 1_800_000_000_000_000_000


def clock(active=1000, *, sleep=0, wall_shift=0, boot=BOOT):
    return dict(clock_kind='darwin_mach_v1', boot_id=boot,
                active_ns=int(active*1e9), continuous_ns=int((active+sleep)*1e9),
                wall_ns=WALL + int((active+sleep+wall_shift)*1e9))


def receipt(**changes):
    result = dict(schema_version=1, pid=42, ppid=1, process_identity='42:100:1', parent_identity='1:1:0',
                  sequence=1, heartbeat=clock(), stage='idle', workers=[],
                  current_source=None, cycle_started=None, cycle_finished=None)
    result.update(changes)
    return result


def service(**changes):
    result = dict(loaded=True, state='running', pid=42,
                  process_identity='42:100:1', last_exit_code=0)
    result.update(changes)
    return result


def install_offline_guard():
    """Audit before importing sentinel dependencies. Reject network/process work
    and any data access outside a freshly allocated test root. Code/stdlib reads
    remain available; every absolute write must be under an allowed temp root.
    """
    allowed = set()
    stage = Path(__file__).resolve().parent
    stdlib = Path(os.__file__).resolve().parent
    def audit(event, args):
        if event.startswith('socket.') or event == 'subprocess.Popen':
            raise AssertionError('offline_test_external_operation_forbidden')
        if event == 'sqlite3.connect':
            from urllib.parse import unquote, urlparse
            name = os.fsdecode(args[0])
            path = Path(unquote(urlparse(name).path) if name.startswith('file:') else name).resolve()
            if not any(path.is_relative_to(p) for p in allowed):
                raise AssertionError('offline_test_database_forbidden')
        if event != 'open' or not args or isinstance(args[0], int):
            return
        path = Path(os.fsdecode(args[0]))
        if not path.is_absolute():
            # Module's descriptor-relative access is pinned to an allowed root.
            if len(path.parts) == 1 and (path.name in {live.RECEIPT, live.OBSERVATION, 'supervisor-health-recovery.json', 'supervisor-health-recovery-anchor.json', 'state', 'ledger-recovery', 'supervisor-incidents'}
                                          or path.name.startswith('.liveness-')
                                          or (path.name.startswith('supervisor-health-recovery-') and path.name.endswith('.json') and len(path.name)==96)):
                return
            path = Path.cwd() / path
        flags = args[2] if len(args) > 2 else 0
        writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        if any(path.is_relative_to(p) or path.resolve().is_relative_to(p) for p in allowed):
            return
        if not writing and (path == Path('/dev/urandom') or
                            (path.is_relative_to(stage) and path.suffix in {'.py', '.pyc'}) or
                            path.is_relative_to(stdlib)):
            return
        # Directory traversal uses O_DIRECTORY, without reading data bodies.
        if not writing and flags & getattr(os, 'O_DIRECTORY', 0):
            return
        raise AssertionError('offline_test_path_forbidden: ' + str(path))
    sys.addaudithook(audit)
    return allowed


class EvaluationTests(unittest.TestCase):
    def test_old_exit_does_not_override_current_owner(self):
        result = live.evaluate(receipt(), service(last_exit_code=3), clock(1015))
        self.assertEqual(result['status'], 'healthy')
        self.assertTrue(result['can_resolve'])
        self.assertEqual(result['business_status'], 'not_evaluated')

    def test_sleep_is_not_recovery_and_awake_stall_still_fails(self):
        result = live.evaluate(receipt(), service(), clock(1015, sleep=3600))
        self.assertEqual(result['status'], 'wake_pending')
        self.assertEqual(result['sleep_seconds'], 3600)
        self.assertFalse(result['can_resolve'])
        self.assertEqual(live.evaluate(receipt(), service(), clock(1500, sleep=3600))['status'], 'stale')

    def test_real_post_wake_receipt_recovers(self):
        snap = receipt(heartbeat=clock(1016, sleep=3600), sequence=2)
        now = clock(1017, sleep=3600)
        now['recovery_after'] = clock(1015, sleep=3600)
        self.assertEqual(live.evaluate(snap, service(), now)['status'], 'healthy')
        self.assertEqual(live.evaluate(receipt(), service(), now)['status'], 'wake_pending')

    def test_recovery_requires_later_receipt(self):
        now = clock(1015)
        now['recovery_after'] = clock(1010)
        self.assertEqual(live.evaluate(receipt(), service(), now)['status'], 'recovery_pending')
        self.assertEqual(live.evaluate(receipt(heartbeat=clock(1011)), service(), now)['status'], 'healthy')

    def test_missing_receipt_never_falls_back(self):
        snap = dict(read_status='missing', heartbeat_at=datetime.now(timezone.utc).isoformat(), status='idle')
        self.assertEqual(live.evaluate(snap, service(), clock())['status'], 'migration_pending')

    def test_pid_reuse_and_previous_boot(self):
        for svc in (service(pid=43), service(process_identity='42:101:1')):
            self.assertEqual(live.evaluate(receipt(), svc, clock())['status'], 'stale')
        self.assertEqual(live.evaluate(receipt(), service(), clock(boot=BOOT[:-1]+'2'))['status'], 'stale')

    def test_native_launcher_parent_and_reused_parent_pid(self):
        native = service(pid=1, process_identity='1:1:0', last_exit_code=3)
        self.assertEqual(live.evaluate(receipt(), native, clock())['status'], 'healthy')
        native['process_identity'] = '1:2:0'
        self.assertEqual(live.evaluate(receipt(), native, clock())['error_code'], 'supervisor_owner_mismatch')

    def test_clean_run_once_between_cycles(self):
        finished = receipt(stage='cycle_finished', cycle_started=clock(999), cycle_finished=clock())
        stopped = service(state='not running', pid=None, process_identity=None)
        result = live.evaluate(finished, stopped, clock(1015))
        self.assertEqual(result['status'], 'healthy')
        self.assertEqual(result['activity'], 'between_cycles')
        self.assertFalse(result['owner_active'])
        self.assertTrue(result['can_resolve'])
        self.assertFalse(live.evaluate(receipt(), stopped, clock(1015))['can_resolve'])
        self.assertEqual(live.evaluate(finished, stopped, clock(1500))['status'], 'stale')
        self.assertEqual(live.evaluate(finished, stopped, clock(1015, sleep=3600))['status'], 'wake_pending')
        self.assertEqual(live.evaluate(finished, stopped, dict(clock(1015), recovery_after=clock(1010)))['status'], 'recovery_pending')
        self.assertEqual(live.evaluate(finished, stopped, clock(boot=BOOT[:-1]+'2'))['status'], 'stale')
        fresh = receipt(stage='cycle_finished', heartbeat=clock(1012), cycle_started=clock(1011), cycle_finished=clock(1012))
        self.assertTrue(live.evaluate(fresh, stopped, dict(clock(1015), recovery_after=clock(1010)))['can_resolve'])

    def test_unknown_future_clocks_fail_closed(self):
        for now in ({}, clock(999), clock(wall_shift=3600), clock(wall_shift=-3600),
                    dict(clock(), active_ns=float('nan'))):
            self.assertFalse(live.evaluate(receipt(), service(), now)['can_resolve'])
        for svc in ({}, service(loaded=None), service(process_identity=None), service(state='unknown')):
            self.assertFalse(live.evaluate(receipt(), svc, clock())['can_resolve'])

    def test_known_failure(self):
        for snap, svc in ((receipt(stage='failed'), service()),
                          (receipt(), service(loaded=False)),
                          (receipt(), service(state='not running', last_exit_code=3))):
            self.assertEqual(live.evaluate(snap, svc, clock())['status'], 'failed')

    def test_malformed_receipts(self):
        for fields in ({'schema_version': True}, {'pid': True}, {'ppid': 0}, {'workers': None},
                       {'sequence': 0}, {'heartbeat': {}}, {'stage': 'model input'},
                       {'cycle_started': clock(2000)}, {'process_identity': '99:100:1'},
                       {'workers': [None]}):
            self.assertFalse(live.evaluate(receipt(**fields), service(), clock())['can_resolve'])

    def test_file_time_must_match_receipt(self):
        snap = receipt(_file_mtime_ns=WALL)
        self.assertEqual(live.evaluate(snap, service(), clock())['error_code'],
                         'supervisor_receipt_file_time_mismatch')

    def test_long_worker_not_a_whole_cycle_timeout(self):
        snap = receipt(heartbeat=clock(20000), workers=[dict(source_id='win11',
            started_at='2020-01-02T00:00:00+08:00', progress_at=None, progress=None,
            observed_progress_active_ns=None)])
        result = live.evaluate(snap, service(), clock(20001))
        self.assertEqual(result['status'], 'healthy')
        self.assertEqual(result['workers'][0]['status'], 'unknown')
        snap['workers'][0]['observed_progress_active_ns'] = int(19999e9)
        self.assertEqual(live.evaluate(snap, service(), clock(20001))['workers'][0]['status'], 'progressing')
        snap['workers'][0]['observed_progress_active_ns'] = int(1000e9)
        self.assertEqual(live.evaluate(snap, service(), clock(20001))['workers'][0]['status'], 'stale')

    def test_read_worker_schema_is_validated_as_strictly_as_write(self):
        worker = dict(source_id='win11', started_at=None, progress_at=None, progress='unknown')
        for field in (dict(covered_chars=-1), dict(fragment_stage_complete='yes'),
                      dict(run_id='../raw'), dict(pid=True), dict(model_input='forbidden')):
            snap = receipt(workers=[dict(worker, **field)])
            self.assertFalse(live.evaluate(snap, service(), clock())['can_resolve'])


class FileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='liveness-offline-', dir=TEST_PARENT)
        self.root = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def pulse(self, active=1000, **kwargs):
        with patch.object(live, 'clock_sample', return_value=clock(active)), \
             patch.object(live, 'process_identity', side_effect=lambda pid: f'{pid}:100:1'):
            return live.pulse(self.root, stage=kwargs.pop('stage', 'idle'), **kwargs)

    def test_read_missing_is_noncreating(self):
        self.assertEqual(live.read(self.root), {'read_status': 'missing'})
        self.assertFalse((self.root/'state').exists())

    def test_first_pulse_creates_state_directory(self):
        self.assertFalse((self.root/'state').exists())
        self.pulse(stage='cycle_started')
        self.assertEqual(live.read(self.root)['read_status'], 'ok')
        self.assertEqual((self.root/'state').stat().st_mode & 0o777, 0o700)

    def test_read_detects_atomic_replacement_race(self):
        self.pulse()
        original = os.stat
        def replaced(name, **kwargs):
            info = original(name, **kwargs)
            if name == live.RECEIPT and 'dir_fd' in kwargs:
                from types import SimpleNamespace
                return SimpleNamespace(st_ino=info.st_ino+1, st_dev=info.st_dev)
            return info
        with patch.object(live.os, 'stat', side_effect=replaced):
            self.assertEqual(live.read(self.root)['read_status'], 'invalid')

    def test_pulse_schema_cycle_and_read_only(self):
        first = self.pulse(stage='cycle_started', workers=[])
        second = self.pulse(1015, stage='cycle_finished')
        self.assertEqual(second['sequence'], 2)
        self.assertEqual(second['cycle_started'], first['heartbeat'])
        self.assertEqual(second['cycle_finished'], second['heartbeat'])
        path = self.root/'state'/live.RECEIPT
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        for _ in range(3):
            self.assertEqual(live.read(self.root)['sequence'], 2)
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_worker_wait_is_not_progress(self):
        worker = dict(source_id='dingtalk_mail', started_at='2020-01-02T00:00:00+08:00',
                      progress_at=None, progress='network_wait')
        self.pulse(workers=[worker])
        second = self.pulse(1015, workers=[dict(worker, progress_at='2020-01-02T00:01:00+08:00')])
        self.assertIsNone(second['workers'][0]['observed_progress_active_ns'])
        third = self.pulse(1030, workers=[dict(worker, progress=1)])
        self.assertEqual(third['workers'][0]['observed_progress_active_ns'], int(1030e9))
        fourth = self.pulse(1045, workers=[dict(worker, progress=1)])
        self.assertEqual(fourth['workers'][0]['observed_progress_active_ns'], int(1030e9))
        self.assertEqual(self.pulse(1060, workers=[])['workers'], [])

    def test_four_workers_and_restart_progress_reset(self):
        workers = [dict(source_id=s, started_at='2020-01-02T00:00:00+08:00', progress=1)
                   for s in sorted(live.SOURCES)[:4]]
        self.pulse(workers=workers)
        changed = [dict(w, progress=2) for w in workers]
        self.assertTrue(all(w['observed_progress_active_ns'] for w in self.pulse(1015, workers=changed)['workers']))
        changed[0]['started_at'] = '2020-01-02T00:10:00+08:00'
        self.assertIsNone(self.pulse(1030, workers=changed)['workers'][0]['observed_progress_active_ns'])

    def test_real_progress_reader_to_pulse(self):
        import sqlite3
        import supervisor_worker_progress
        database = self.root/'fixture.sqlite'
        started = '2020-01-02T00:00:00+08:00'
        with sqlite3.connect(database) as connection:
            connection.executescript('''
                CREATE TABLE action_runs(run_id TEXT, action_type TEXT, started_at TEXT, updated_at TEXT);
                CREATE TABLE action_steps(run_id TEXT, step_key TEXT, started_at TEXT, updated_at TEXT, step_order INTEGER);
            ''')
            connection.execute('INSERT INTO action_runs VALUES(?,?,?,?)',
                               ('test-run-1', 'dingtalk_chat', started, started))
            connection.execute('INSERT INTO action_steps VALUES(?,?,?,?,?)',
                               ('test-run-1', 'collect', started, started, 1))
        first = supervisor_worker_progress.read(database, {}, 'dingtalk_chat', started)
        self.assertEqual(first['progress'], 'observed')
        one = self.pulse(stage='workers_running', workers=[first])
        self.assertEqual(one['workers'][0]['run_id'], 'test-run-1')
        self.assertIsNone(one['workers'][0]['observed_progress_active_ns'])
        with sqlite3.connect(database) as connection:
            connection.execute('UPDATE action_steps SET updated_at=?', ('2020-01-02T00:00:30+08:00',))
        second = supervisor_worker_progress.read(database, {}, 'dingtalk_chat', started)
        two = self.pulse(1030, stage='workers_running', workers=[second])
        self.assertEqual(two['workers'][0]['observed_progress_active_ns'], int(1030e9))
        # All mail fragment metadata fields pass the same controlled boundary.
        fragments = dict(second, validated_chunks=2, covered_chars=1024, observed_files=1,
                         fragment_stage_complete=True, stage='email_fragments')
        third = self.pulse(1045, workers=[fragments])
        self.assertTrue(third['workers'][0]['fragment_stage_complete'])
        unavailable = dict(fragments, progress='unavailable')
        self.assertIsNone(self.pulse(1060, workers=[unavailable])['workers'][0]['observed_progress_active_ns'])
        for key, bad in [('validated_chunks', -1), ('covered_chars', True),
                         ('fragment_stage_complete', 'yes'), ('run_id', '../private'), ('stage', 'raw text')]:
            with self.assertRaises(ValueError):
                self.pulse(workers=[dict(fragments, **{key: bad})])

    def test_main_thread_only(self):
        errors = []
        def target():
            try:
                live.pulse(self.root, stage='idle')
            except RuntimeError as exc:
                errors.append(str(exc))
        thread = threading.Thread(target=target)
        thread.start()
        thread.join()
        self.assertEqual(errors, ['owner_main_thread_required'])

    def test_content_and_unknown_fields_rejected(self):
        for worker in ({'source_id': 'win11', 'body': 'forbidden'},
                       {'source_id': 'win11', 'progress': {'payload': 1}},
                       {'source_id': 'win11', 'started_at': '2020-01-02'},
                       {'source_id': 'win11', 'progress': 'arbitrary text'},
                       {'source_id': '../outside'}):
            with self.assertRaises(ValueError):
                self.pulse(workers=[worker])

    def test_symlink_fifo_oversize_corrupt_private_file(self):
        directory = self.root/'state'
        directory.mkdir()
        path = directory/live.RECEIPT
        outside = self.root/'unrelated'
        outside.write_text('untouched')
        path.symlink_to(outside)
        self.assertNotEqual(live.read(self.root)['read_status'], 'ok')
        with self.assertRaises(ValueError):
            self.pulse()
        self.assertEqual(outside.read_text(), 'untouched')
        path.unlink()
        os.mkfifo(path, 0o600)
        self.assertEqual(live.read(self.root)['read_status'], 'invalid')
        path.unlink()
        for content in ('x' * (live.MAX_BYTES + 1), '{', '[]'):
            path.write_text(content)
            path.chmod(0o600)
            self.assertEqual(live.read(self.root)['read_status'], 'invalid')
        path.write_text('{}')
        path.chmod(0o644)
        self.assertEqual(live.read(self.root)['read_status'], 'invalid')

    def test_state_symlink_and_hardlink_rejected(self):
        target = self.root/'other'
        target.mkdir()
        (self.root/'state').symlink_to(target)
        self.assertNotEqual(live.read(self.root)['read_status'], 'ok')
        with self.assertRaises((ValueError, OSError)):
            self.pulse()
        (self.root/'state').unlink()
        target.rmdir()
        self.pulse()
        os.link(self.root/'state'/live.RECEIPT, self.root/'duplicate')
        self.assertEqual(live.read(self.root)['read_status'], 'invalid')

    @unittest.skipUnless(sys.platform == 'darwin', 'Darwin kernel evidence')
    def test_native_kernel_clocks_and_cross_process_read(self):
        # A separate process reads this test owner's receipt and OS clocks.
        # It cannot steal the live owner's receipt. No subprocess is launched.
        snap = live.pulse(self.root, stage='idle', workers=[])
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            try:
                os.close(read_fd)
                child_clock = live.clock_sample()
                svc = service(pid=os.getppid(), process_identity=live.process_identity(os.getppid()))
                result = live.evaluate(live.read(self.root), svc, child_clock)
                try:
                    live.pulse(self.root, stage='idle')
                    result['stolen'] = True
                except RuntimeError:
                    result['stolen'] = False
                os.write(write_fd, json.dumps(result).encode())
            finally:
                os._exit(0)
        os.close(write_fd)
        raw = os.read(read_fd, 10000)
        os.close(read_fd)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(status, 0)
        result = json.loads(raw)
        self.assertEqual(result['status'], 'healthy')
        self.assertFalse(result['stolen'])
        self.assertEqual(live.read(self.root)['sequence'], snap['sequence'])


TEST_PARENT = None


if __name__ == '__main__':
    # Allocate before enforcing the guard so tempfile's own /tmp probe is not
    # mistaken for an application data access. No test writes to stage/bin.
    with tempfile.TemporaryDirectory(prefix='owner-liveness-suite-') as parent:
        TEST_PARENT = str(Path(parent).resolve())
        allowed = install_offline_guard()
        allowed.add(Path(TEST_PARENT))
        unittest.main(verbosity=2)
