#!/usr/bin/env python3
"""Offline sentinel regression: temporary DBs only, all delivery/probes replaced.

Legacy evaluate_health stays covered as a pure compatibility helper; run_once
must use only owner receipts. This suite does not launch the supervisor.
"""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import sys
sys.dont_write_bytecode = True

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import selftest_supervisor_liveness as fixtures
import supervisor_liveness as live

# Install the same filesystem/network/SQLite audit boundary before loading any
# sentinel/state dependencies. The parent is registered below in __main__.
TEMP_DIRECTORY = Path(tempfile.gettempdir()).resolve()
allowed = fixtures.install_offline_guard()
import business_sync_state as state
import business_sync_supervisor_health as health

TEST_PARENT = None


class LegacyTests(unittest.TestCase):
    def test_legacy_pure_function_contract(self):
        current = datetime(2026, 9, 21, tzinfo=timezone.utc)
        fresh = dict(status='idle', heartbeat_at=(current-timedelta(seconds=30)).isoformat())
        good = dict(loaded=True, last_exit_code=0)
        self.assertEqual(health.evaluate_health(fresh, good, current)['status'], 'healthy')
        self.assertEqual(health.evaluate_health(dict(fresh, status='failed'), good, current)['error_code'],
                         'supervisor_runtime_failed')
        self.assertEqual(health.evaluate_health(fresh, dict(good, last_exit_code=3), current)['error_code'],
                         'supervisor_exit_nonzero')
        self.assertEqual(health.evaluate_health(dict(fresh, heartbeat_at=(current-health.HEARTBEAT_GRACE-timedelta(seconds=1)).isoformat()),
                                              good, current)['status'], 'stale')

    def test_launchd_parser_native_pid_and_unknown_error(self):
        with patch.object(live, 'process_identity', return_value='55:100:1'):
            result = health.launchd_status(lambda *a, **kw: subprocess.CompletedProcess([], 0,
                'state = running\n pid = 55\n last exit code = 3\n', ''))
        self.assertEqual(result['pid'], 55)
        self.assertEqual(result['process_identity'], '55:100:1')
        self.assertEqual(result['last_exit_code'], 3)
        unknown = health.launchd_status(lambda *a, **kw: subprocess.CompletedProcess([], 1, '', 'permission denied'))
        self.assertIsNone(unknown['loaded'])
        missing = health.launchd_status(lambda *a, **kw: subprocess.CompletedProcess([], 113, '', 'Could not find service'))
        self.assertFalse(missing['loaded'])


class SentinelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sentinel-offline-', dir=TEST_PARENT)
        self.root = Path(self.temp.name).resolve()
        self.database = self.root/'state'/'sync.sqlite'
        self.config = self.root/'config.json'
        self.config.write_text('{}')
        self.maintenance = self.root/'state'/'maintenance-mode.json'
        self.current = datetime(2026, 9, 21, tzinfo=timezone.utc)
        self.clock = fixtures.clock()
        self.service = fixtures.service(pid=os.getpid(), process_identity=f'{os.getpid()}:100:1')
        self.delivery_calls = 0
        # The old runtime function is forbidden, even when the DB is populated.
        self.runtime_guard = patch.object(state, 'runtime', side_effect=AssertionError('DB runtime must not be read'))
        self.runtime_guard.start()
        self.addCleanup(self.runtime_guard.stop)
        self.native_guard = patch.object(health.ledger_recovery, 'native', return_value=False)
        self.native_guard.start()
        self.addCleanup(self.native_guard.stop)

    def tearDown(self):
        self.temp.cleanup()

    def deliver(self, config, database):
        self.assertEqual(database, self.database)
        self.delivery_calls += 1
        return 0  # no real notification API is imported/called

    def run_check(self):
        return health.run_once(self.config, database=self.database, current=self.current,
                               maintenance_path=self.maintenance, service_probe=lambda: dict(self.service),
                               delivery=self.deliver, clock_probe=lambda: dict(self.clock))

    def pulse(self, active=1000, *, sleep=0, stage='idle', **kw):
        sample = fixtures.clock(active, sleep=sleep)
        with patch.object(live, 'clock_sample', return_value=sample), \
             patch.object(live, 'process_identity', side_effect=lambda pid: f'{pid}:100:1'):
            result = live.pulse(self.root, stage=stage, **kw)
        # Simulate the clock at the time this fixture file was written.
        path = self.root/'state'/live.RECEIPT
        os.utime(path, ns=(sample['wall_ns'], sample['wall_ns']))
        return result

    def existing_alert(self):
        state.initialize(self.database)
        state.open_alert('system', 'supervisor_health', 'fixture', 'fixture',
                         severity='error', database=self.database, at=self.current)

    def test_maintenance_skips_everything(self):
        self.maintenance.parent.mkdir()
        self.maintenance.write_text('{"status":"active"}')
        result = self.run_check()
        self.assertEqual(result['status'], 'maintenance')
        self.assertFalse(self.database.exists())
        self.assertEqual(self.delivery_calls, 0)

    def test_migration_never_closes_from_polluted_db_heartbeat(self):
        self.existing_alert()
        with patch.object(state, 'runtime', return_value={}):
            state.update_runtime(self.database, status='idle', heartbeat_at=self.current.isoformat())
        result = self.run_check()
        self.assertEqual(result['status'], 'migration_pending')
        self.assertEqual(state.open_alert_count(self.database), 1)
        self.assertFalse((self.root/'state'/live.RECEIPT).exists())

    def test_failure_dedup_and_real_pulse_single_recovery(self):
        self.pulse(stage='failed')
        one = self.run_check()
        self.clock = fixtures.clock(1010)
        self.current += timedelta(seconds=10)
        two = self.run_check()
        self.assertEqual((one['status'], two['status']), ('failed', 'failed'))
        rows = state.alerts(self.database, status='open')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['occurrence_count'], 2)
        self.pulse(1015)
        self.clock = fixtures.clock(1016)
        self.assertTrue(self.run_check()['can_resolve'])
        self.clock = fixtures.clock(1017)
        self.assertTrue(self.run_check()['can_resolve'])
        self.assertEqual(state.open_alert_count(self.database), 0)
        self.assertEqual(sum(r['category'] == 'recovery' for r in state.alerts(self.database)), 1)

    def test_sleep_never_closes_until_new_post_wake_owner(self):
        self.existing_alert()
        self.pulse()
        path = self.root/'state'/live.RECEIPT
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        self.clock = fixtures.clock(1010, sleep=3600)
        self.assertEqual(self.run_check()['status'], 'wake_pending')
        self.clock = fixtures.clock(1011, sleep=3600)
        self.assertEqual(self.run_check()['status'], 'wake_pending')
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))
        self.assertEqual(state.open_alert_count(self.database), 1)
        self.pulse(1015, sleep=3600)
        self.clock = fixtures.clock(1016, sleep=3600)
        self.assertEqual(self.run_check()['status'], 'healthy')
        self.assertEqual(state.open_alert_count(self.database), 0)

    def test_legacy_alert_requires_new_receipt_after_observation(self):
        self.existing_alert()
        self.pulse()
        self.clock = fixtures.clock(1010)
        self.assertEqual(self.run_check()['status'], 'recovery_pending')
        self.clock = fixtures.clock(1011)
        self.assertEqual(self.run_check()['status'], 'recovery_pending')
        self.pulse(1015)
        self.clock = fixtures.clock(1016)
        self.assertEqual(self.run_check()['status'], 'healthy')

    def test_service_recovers_but_no_owner_pulse_keeps_fault(self):
        self.pulse()
        self.service['loaded'] = None
        self.clock = fixtures.clock(1010)
        self.assertEqual(self.run_check()['status'], 'unknown')
        self.service['loaded'] = True
        self.clock = fixtures.clock(1011)
        self.assertEqual(self.run_check()['status'], 'recovery_pending')
        self.assertEqual(state.open_alert_count(self.database), 1)

    def test_unknown_clock_cannot_close(self):
        self.existing_alert()
        self.pulse()
        self.clock = {}
        self.assertEqual(self.run_check()['status'], 'unknown')
        self.assertEqual(state.open_alert_count(self.database), 1)
        self.clock = fixtures.clock(1010)
        self.assertEqual(self.run_check()['status'], 'recovery_pending')

    def test_later_clock_failure_cannot_reuse_an_earlier_recovery_pulse(self):
        self.existing_alert()
        self.pulse()
        self.clock = fixtures.clock(1001)
        self.run_check()
        self.pulse(1015)
        self.clock = fixtures.clock(1016)
        self.assertEqual(self.run_check()['status'], 'healthy')
        self.clock = {}
        self.assertEqual(self.run_check()['status'], 'unknown')
        self.clock = fixtures.clock(1017)
        self.assertEqual(self.run_check()['status'], 'recovery_pending')
        self.pulse(1030)
        self.clock = fixtures.clock(1031)
        self.assertEqual(self.run_check()['status'], 'healthy')

    def test_unknown_progress_is_not_business_health(self):
        self.pulse(workers=[dict(source_id='dingtalk_mail', started_at=self.current,
                                progress_at=None, progress='network_wait')])
        self.clock = fixtures.clock(1010)
        result = self.run_check()
        self.assertEqual(result['status'], 'healthy')
        self.assertEqual(result['workers'][0]['status'], 'unknown')
        self.assertEqual(result['business_status'], 'not_evaluated')
        self.assertTrue(result['owner_active'])

    def test_native_parent_old_exit_and_clean_between_cycles(self):
        self.pulse(stage='cycle_started')
        self.service.update(pid=os.getppid(), process_identity=f'{os.getppid()}:100:1', last_exit_code=3)
        self.clock = fixtures.clock(1001)
        self.assertEqual(self.run_check()['status'], 'healthy')
        self.pulse(1010, stage='cycle_finished', workers=[])
        self.service.update(state='not running', pid=None, process_identity=None, last_exit_code=0)
        self.clock = fixtures.clock(1011)
        self.assertEqual(self.run_check()['status'], 'healthy')
        self.pulse(1015, stage='cycle_started')
        self.clock = fixtures.clock(1016)
        self.assertEqual(self.run_check()['status'], 'failed')

    def test_corrupt_fence_and_receipt_hold_alert(self):
        self.existing_alert()
        self.pulse()
        path = self.root/'state'/live.OBSERVATION
        path.write_text('{')
        path.chmod(0o600)
        self.clock = fixtures.clock(1010)
        result = self.run_check()
        self.assertFalse(result['can_resolve'])
        self.assertEqual(path.read_text(), '{')
        self.assertEqual(state.open_alert_count(self.database), 1)

    def test_custom_config_requires_explicit_database(self):
        with self.assertRaisesRegex(ValueError, 'explicit_database_required'):
            health.run_once(self.config, maintenance_path=self.maintenance, delivery=self.deliver)
        self.assertFalse(self.database.exists())


if __name__ == '__main__':
    # tempfile's initial probe is completed before the read/write boundary.
    import uuid
    TEST_PARENT = str(TEMP_DIRECTORY/('sentinel-suite-'+uuid.uuid4().hex))
    allowed.add(Path(TEST_PARENT))
    Path(TEST_PARENT).mkdir(mode=0o700)
    try:
        unittest.main(verbosity=2)
    finally:
        Path(TEST_PARENT).rmdir()
