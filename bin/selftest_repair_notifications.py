#!/usr/bin/env python3
"""No network; simulated time, temporary databases and fixed transport mocks."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import business_sync_state as state
import business_sync_supervisor as supervisor
import local_sync_repair as repair
import dingtalk_memory_automation as memory
from listener_retry_budget import ListenerBudget, BudgetBlocked


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'state.sqlite'
        state.initialize(self.db)
        self.now = datetime(2026, 9, 3, 3, tzinfo=timezone.utc)
        self.alert = state.open_alert('dingtalk_chat', 'sync_failure', '测试', '测试',
            severity='error', error_code='rollback_concurrent_change', database=self.db, at=self.now)

    def delivered(self, row):
        state.mark_delivery(row['alert_id'], 'macos', 'visible', database=self.db, at=self.now)
        state.mark_delivery(row['alert_id'], 'miso', 'delivered', event_id='miso_'+'1'*64,
                            receipt_hash='a'*64, database=self.db, at=self.now)

    def test_initial_six_hours_daily_and_acknowledgment(self):
        first = state.claim_delivery(self.alert['alert_id'], self.db, self.now)
        self.assertEqual(state.parse_time(first['next_reminder']), self.now + timedelta(hours=6))
        self.delivered(first)
        self.assertEqual(state.due_alerts(self.db, self.now+timedelta(hours=5, minutes=59)), [])
        six = state.claim_delivery(first['alert_id'], self.db, self.now+timedelta(hours=6))
        self.assertEqual((six['notification_revision'], six['notification_stage']), (1, 'escalation'))
        self.assertEqual(state.parse_time(six['next_reminder']), self.now+timedelta(hours=30))
        self.delivered(six)
        self.assertEqual(state.due_alerts(self.db, self.now+timedelta(hours=29)), [])
        daily = state.claim_delivery(first['alert_id'], self.db, self.now+timedelta(hours=30))
        self.assertEqual((daily['notification_revision'], daily['notification_stage']), (2, 'daily'))
        self.delivered(daily)
        state.acknowledge(first['alert_id'], self.db)
        self.assertEqual(state.due_alerts(self.db, self.now+timedelta(days=10)), [])
        self.assertEqual(state.alerts(self.db)[0]['status'], 'open')
        state.resolve_alert('dingtalk_chat', 'sync_failure', title='恢复', detail='独立证据', database=self.db, at=self.now+timedelta(days=11))
        rows = state.due_alerts(self.db, self.now+timedelta(days=11))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['category'], 'recovery')

    def test_repeated_fault_does_not_lose_event_id_or_receipt(self):
        row = state.claim_delivery(self.alert['alert_id'], self.db, self.now)
        state.mark_delivery(row['alert_id'], 'miso', 'pending', event_id='miso_'+'2'*64, database=self.db)
        second = state.open_alert('dingtalk_chat', 'sync_failure', '测试', '测试', severity='error', database=self.db)
        self.assertEqual(second['miso_event_id'], 'miso_'+'2'*64)
        self.assertEqual(second['notification_revision'], 0)

    def test_first_reminder_uses_fault_onset_not_delayed_delivery(self):
        delayed = self.now + timedelta(minutes=10)
        row = state.claim_delivery(self.alert['alert_id'], self.db, delayed)
        self.assertEqual(state.parse_time(row['next_reminder']), self.now + timedelta(hours=6))
        self.delivered(row)
        # Existing deployed records with a delivery-anchored deadline are
        # handled at the correct onset deadline without resetting old receipts.
        with state._connect(self.db) as db:
            db.execute('UPDATE sync_alerts SET next_reminder=? WHERE alert_id=?',
                       (state.now_iso(delayed + timedelta(hours=6)), row['alert_id']))
        due = state.due_alerts(self.db, self.now + timedelta(hours=6))
        self.assertEqual(len(due), 1)
        state.initialize(self.db)
        persisted = state.alerts(self.db)[0]
        self.assertEqual(state.parse_time(state.public_alert(persisted)['next_reminder']), self.now + timedelta(hours=6))
        self.assertEqual(persisted['miso_receipt_hash'], 'a' * 64)
        self.assertEqual(persisted['notification_revision'], 0)
        escalated = state.claim_delivery(row['alert_id'], self.db, self.now + timedelta(hours=6))
        self.assertEqual(escalated['notification_stage'], 'escalation')
        self.assertEqual(escalated['notification_revision'], 1)

    def test_first_notice_after_long_outage_is_one_escalation(self):
        current = self.now + timedelta(hours=8)
        row = state.claim_delivery(self.alert['alert_id'], self.db, current)
        self.assertEqual(row['notification_stage'], 'escalation')
        self.assertEqual(row['notification_revision'], 0)
        self.assertEqual(row['reminder_count'], 1)
        self.assertEqual(state.parse_time(row['next_reminder']), current + timedelta(hours=24))
        self.delivered(row)
        self.assertEqual(state.due_alerts(self.db, current + timedelta(minutes=1)), [])

    def test_exit_zero_without_receipt_is_not_delivery(self):
        with patch.object(supervisor.subprocess, 'run', return_value=subprocess.CompletedProcess([],0,'','')):
            supervisor.deliver_macos(self.alert, self.db)
        self.assertEqual(state.alerts(self.db)[0]['macos_status'], 'uncertain')

    def test_requested_is_polled_without_resending(self):
        state.mark_delivery(self.alert['alert_id'], 'macos', 'requested', database=self.db)
        row = state.alerts(self.db)[0]
        with patch.object(supervisor.subprocess, 'run', return_value=subprocess.CompletedProcess([],0,'{"status":"visible"}','')) as run:
            supervisor.deliver_macos(row, self.db)
        self.assertEqual(json.loads(run.call_args.kwargs['input'])['operation'], 'status')
        shown = state.public_alert(state.alerts(self.db)[0])['delivery']['macos']
        self.assertEqual(shown['status'], 'visible')
        self.assertFalse(shown['read_confirmed'])

    def test_permission_off_is_not_requested_or_retried(self):
        with patch.object(supervisor.subprocess, 'run', return_value=subprocess.CompletedProcess([],0,'{"status":"permission_off"}','')):
            supervisor.deliver_macos(self.alert, self.db)
        self.assertEqual(state.alerts(self.db)[0]['macos_status'], 'permission_off')

    def test_miso_missing_message_receipt_is_unverified(self):
        self.alert['miso_event_id'] = 'miso_'+'3'*64
        with patch.object(supervisor, 'run_miso_bridge', return_value={'ok':True,'status':'delivered'}):
            supervisor.deliver_miso(self.alert, self.db)
        row = state.alerts(self.db)[0]
        self.assertEqual(row['miso_status'], 'unverified')
        self.assertFalse(state.public_alert(row)['delivery']['miso']['receipt_confirmed'])

    def test_legacy_delivered_is_migrated_without_resending(self):
        with state._connect(self.db) as connection:
            connection.execute("UPDATE sync_alerts SET miso_status='delivered',miso_receipt_hash=NULL WHERE alert_id=?", (self.alert['alert_id'],))
        state.initialize(self.db)
        row = state.alerts(self.db)[0]
        self.assertEqual(row['miso_status'], 'unverified')
        self.assertEqual(row['miso_error_code'], 'legacy_receipt_missing')

    def test_new_delivery_without_hash_cannot_claim_delivered(self):
        state.mark_delivery(self.alert['alert_id'], 'miso', 'delivered', database=self.db)
        self.assertEqual(state.alerts(self.db)[0]['miso_status'], 'unverified')

    def test_immediate_dispatch_is_fixed_scoped_and_maintenance_safe(self):
        event = 'miso_' + 'a' * 64
        with patch.object(supervisor, 'maintenance_status', return_value={'active': False}), patch.object(supervisor.subprocess, 'Popen') as spawn:
            self.assertTrue(supervisor.dispatch_miso_event(event))
            self.assertEqual(spawn.call_args.args[0][1:], ['-m', 'miso_notifier.sync_delivery', '--event-id', event])
            self.assertFalse(supervisor.dispatch_miso_event('arbitrary_command'))
            self.assertFalse(supervisor.dispatch_miso_event(event, self.db))
            self.assertEqual(spawn.call_count, 1)
        with patch.object(supervisor, 'maintenance_status', return_value={'active': True}), patch.object(supervisor.subprocess, 'Popen') as spawn:
            self.assertFalse(supervisor.dispatch_miso_event(event))
            spawn.assert_not_called()

    def test_enqueued_alert_starts_immediate_single_event_dispatch(self):
        event = 'miso_' + 'b' * 64
        with patch.object(supervisor, 'run_miso_bridge', return_value={'ok': True, 'status': 'pending', 'event_id': event}), patch.object(supervisor, 'dispatch_miso_event') as dispatch:
            supervisor.deliver_miso(self.alert, self.db)
        dispatch.assert_called_once_with(event, self.db)

    def test_manual_persistent_failure_stops_scheduled_source(self):
        self.assertTrue(supervisor.hold_chat_failure('proposal_claim_relation', database=self.db, at=self.now))
        row = state.source('dingtalk_chat', self.db)
        self.assertEqual(row['status'], 'manual_required')
        self.assertIsNone(row['next_due'])
        self.assertFalse(supervisor.hold_chat_failure('codex_timeout', database=self.db, at=self.now))
        self.assertEqual(state.source('dingtalk_chat', self.db)['error_code'], 'proposal_claim_relation')

    def test_uncertain_transport_is_not_reemitted(self):
        state.claim_delivery(self.alert['alert_id'], self.db, self.now)
        state.mark_delivery(self.alert['alert_id'],'macos','visible',database=self.db)
        state.mark_delivery(self.alert['alert_id'],'miso','uncertain',event_id='miso_'+'4'*64,database=self.db)
        with patch.object(state,'now_dt',return_value=self.now+timedelta(minutes=1)), patch.object(supervisor,'run_miso_bridge') as bridge:
            supervisor.deliver_due_alerts({}, self.db)
        bridge.assert_not_called()

    def test_local_repair_uses_whitelist_and_lock_before_budget(self):
        self.assertTrue(repair.eligible(self.alert))
        for code in ('permission_denied','transport_unknown','arbitrary_command'):
            self.assertFalse(repair.eligible({**self.alert,'error_code':code}))
        self.assertFalse(repair.eligible({**self.alert,'source_id':'dingtalk_mail'}))
        self.assertFalse(repair.eligible({**self.alert,'source_id':'memory_core','category':'core_failure',
                                         'error_code':'validation_permission_denied'}))
        @contextmanager
        def locked():
            raise memory.AutomationError('already_running','fixture busy')
            yield
        with patch.object(repair.controller,'control_lock',locked), patch.object(repair.memory,'recover_apply_journals') as recovery:
            started=[]
            with self.assertRaises(memory.AutomationError):
                repair.perform({}, on_started=lambda: started.append(True))
        self.assertEqual(started, [])
        recovery.assert_not_called()

    def test_permission_reason_survives_an_older_generic_repair_failure(self):
        context = supervisor.sync_notification_context({**self.alert, 'source_id':'memory_core',
            'category':'core_failure', 'error_code':'validation_permission_denied',
            'repair_error_code':'refresh_failed', 'repair_status':'blocked'})
        self.assertEqual(context['reason'], 'permissions_required')
        self.assertTrue(context['requires_action'])

    def test_permission_denial_stops_before_recovery_writes(self):
        @contextmanager
        def unlocked(*args, **kwargs):
            yield
        with patch.object(repair.controller, 'control_lock', unlocked), patch.object(repair.memory, 'acquire_lock', unlocked), patch.object(repair.memory, 'assert_validation_access', side_effect=memory.AutomationError('validation_permission_denied','fixture')), patch.object(repair.memory, 'recover_apply_journals') as recovery:
            started = []
            with self.assertRaises(memory.AutomationError):
                repair.perform({}, on_started=lambda: started.append(True))
            self.assertEqual(started, [])
            recovery.assert_not_called()


class RepairIndexReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'state').mkdir()
        self.path = self.root / 'state/bridge.sqlite'
        with sqlite3.connect(self.path) as connection:
            connection.execute('CREATE TABLE maintenance_state(key TEXT PRIMARY KEY,value TEXT)')
            connection.executemany('INSERT INTO maintenance_state VALUES (?,?)', [
                ('index_dirty_generation', '2'), ('index_applied_generation', '1'), ('index_last_error', '')])
        self.root_patch = patch.object(repair, 'ROOT', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def update(self, key, value):
        with sqlite3.connect(self.path) as connection:
            connection.execute('UPDATE maintenance_state SET value=? WHERE key=?', (value, key))

    def test_index_in_flight_defers_before_writes_or_attempt(self):
        @contextmanager
        def unlocked(*args, **kwargs):
            yield
        with patch.object(repair.controller, 'control_lock', unlocked), patch.object(repair.memory, 'acquire_lock', unlocked), patch.object(repair.memory, 'assert_validation_access'), patch.object(repair.memory, 'recover_apply_journals') as recovery, patch.object(repair.memory, 'run_refresh_and_validate') as validate:
            started = []
            with self.assertRaises(memory.AutomationError) as error:
                repair.perform({}, on_started=lambda: started.append(True))
            self.assertEqual(error.exception.code, 'memory_index_update_pending')
            self.assertEqual(started, [])
            recovery.assert_not_called()
            validate.assert_not_called()

    def test_committed_index_allows_one_full_validation(self):
        self.update('index_applied_generation', '2')
        @contextmanager
        def unlocked(*args, **kwargs):
            yield
        with patch.object(repair.controller, 'control_lock', unlocked), patch.object(repair.memory, 'acquire_lock', unlocked), patch.object(repair.memory, 'assert_validation_access'), patch.object(repair.memory, 'recover_apply_journals', return_value=0), patch.object(repair.memory, 'recovery_conflicts', return_value=[]), patch.object(repair.memory, 'run_refresh_and_validate', return_value={'validation_attempts': 1}) as validate:
            started = []
            result = repair.perform({}, on_started=lambda: started.append(True))
            self.assertEqual(started, [True])
            self.assertTrue(result['validation_passed'])
            self.assertFalse(result['fault_cleared'])
            validate.assert_called_once_with({}, lock_held=True, attempts=3)

    def test_dispatch_waits_without_creating_runs_then_dispatches_once(self):
        database = self.root / 'alerts.sqlite'
        state.initialize(database)
        alert = state.open_alert('memory_core', 'core_failure', 'fixture', 'fixture',
                                 error_code='validation_failed', database=database)
        connect = state._connect
        @contextmanager
        def unlocked(*args, **kwargs):
            yield
        with patch.object(repair, 'maintenance_active', return_value=False), patch.object(state, 'initialize'), patch.object(state, '_connect', side_effect=lambda *args, **kw: connect(database, **kw)), patch.object(repair.syncv2, 'file_lock', unlocked), patch.object(repair.syncv2, 'start_action_run', return_value='fixture-run') as create_run, patch.object(repair.subprocess, 'Popen') as launch:
            for _ in range(3):
                result = repair.start(self.root / 'config.json', alert['alert_id'], automatic=True)
                self.assertEqual(result['status'], 'deferred')
            create_run.assert_not_called()
            launch.assert_not_called()
            with connect(database, readonly=True) as connection:
                row = connection.execute('SELECT repair_attempt_count,repair_status FROM sync_alerts WHERE alert_id=?', (alert['alert_id'],)).fetchone()
                self.assertEqual(tuple(row), (0, 'not_attempted'))
            self.update('index_applied_generation', '2')
            self.assertEqual(repair.start(self.root / 'config.json', alert['alert_id'], automatic=True)['status'], 'started')
            create_run.assert_called_once()
            launch.assert_not_called()

    def test_actual_index_failure_is_not_reclassified_as_waiting(self):
        self.update('index_last_error', 'PermissionError: fixture')
        with self.assertRaises(memory.AutomationError) as error:
            repair.require_index_ready()
        self.assertEqual(error.exception.code, 'memory_index_update_failed')

    def test_reversed_generation_stays_invalid(self):
        self.update('index_applied_generation', '3')
        with self.assertRaises(memory.AutomationError) as error:
            repair.require_index_ready()
        self.assertEqual(error.exception.code, 'memory_index_state_invalid')

    def test_missing_database_is_not_created_or_ignored(self):
        self.path.rename(self.path.with_suffix('.saved'))
        with self.assertRaises(memory.AutomationError) as error:
            repair.require_index_ready()
        self.assertEqual(error.exception.code, 'memory_index_state_unavailable')
        self.assertFalse(self.path.exists())

    def test_deferred_request_cannot_rearm_a_consumed_auto_budget(self):
        self.assertTrue(repair.automatic_budget_available({'repair_status': 'not_attempted', 'repair_attempt_count': 0}))
        self.assertFalse(repair.automatic_budget_available({'repair_status': 'not_attempted', 'repair_attempt_count': 1}))
        self.assertFalse(repair.automatic_budget_available({'repair_status': 'running', 'repair_attempt_count': 0}))
        self.assertEqual(repair.failure_repair_status(True, 0), 'not_attempted')
        self.assertEqual(repair.failure_repair_status(True, 1), 'blocked')
        self.assertEqual(repair.failure_repair_status(False, 0), 'blocked')


class RepairOwnerQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / 'alerts.sqlite'
        state.initialize(self.database)
        with sqlite3.connect(self.database) as db:
            db.execute('CREATE TABLE action_runs(run_id TEXT PRIMARY KEY,action_type TEXT NOT NULL,'
                       'trigger TEXT NOT NULL,status TEXT NOT NULL,result_json TEXT,error_code TEXT,'
                       'started_at TEXT NOT NULL,completed_at TEXT,updated_at TEXT NOT NULL)')
            db.execute('CREATE TABLE action_steps(run_id TEXT,status TEXT,error_code TEXT,'
                       'completed_at TEXT,updated_at TEXT)')
        self.connect = state._connect
        self.alert = state.open_alert('memory_core', 'core_failure', 'fixture', 'fixture',
                                      error_code='validation_failed', database=self.database)
        @contextmanager
        def unlocked(*args, **kwargs):
            yield
        def connection():
            db = sqlite3.connect(self.database)
            db.row_factory = sqlite3.Row
            return db
        replacements = [
            patch.object(state, 'initialize'), patch.object(repair.syncv2, 'initialize'),
            patch.object(state, '_connect', side_effect=lambda *a, **kw: self.connect(self.database, **kw)),
            patch.object(repair.syncv2, 'connect', side_effect=connection),
            patch.object(repair, 'maintenance_active', return_value=False),
            patch.object(repair, 'require_index_ready'), patch.object(repair.syncv2, 'file_lock', unlocked),
            patch.object(repair, 'supervisor_heartbeat', unlocked),
            patch.object(repair.memory, 'load_config', return_value={}),
        ]
        for replacement in replacements:
            replacement.start()
            self.addCleanup(replacement.stop)

    def row(self):
        with self.connect(self.database, readonly=True) as db:
            return dict(db.execute('SELECT * FROM sync_alerts WHERE alert_id=?', (self.alert['alert_id'],)).fetchone())

    def action_run(self):
        with self.connect(self.database, readonly=True) as db:
            return dict(db.execute('SELECT * FROM action_runs WHERE run_id=?', (self.row()['repair_run_id'],)).fetchone())

    def enqueue(self):
        return repair.start(self.root / 'config.json', self.alert['alert_id'], automatic=True)

    @staticmethod
    def succeed(config, *, on_started, **kwargs):
        on_started()
        return {'status': 'success', 'conflict_count': 0, 'recovered_journals': 0,
                'validation_passed': True, 'validation_attempts': 1, 'fault_cleared': False}

    def test_manual_action_queues_without_detaching_an_interpreter(self):
        with patch.object(repair, 'wake_supervisor', return_value=True) as wake, patch.object(repair.subprocess, 'Popen') as spawn:
            first = repair.start(self.root / 'config.json', self.alert['alert_id'])
            second = repair.start(self.root / 'config.json', self.alert['alert_id'])
        self.assertEqual(first['status'], 'started')
        self.assertTrue(first['queued'])
        self.assertEqual(second['status'], 'already_running')
        self.assertEqual(first['run_id'], second['run_id'])
        self.assertEqual(self.row()['repair_attempt_count'], 0)
        self.assertIsNone(self.row()['repair_pid'])
        spawn.assert_not_called()
        wake.assert_called_once_with()

    def test_automatic_request_executes_once_in_current_supervisor_process(self):
        with patch.object(repair, 'wake_supervisor') as wake:
            self.enqueue()
        wake.assert_not_called()
        with patch.object(repair, 'perform', side_effect=self.succeed) as perform:
            result = repair.execute_queued(self.root / 'config.json')
            self.assertEqual(repair.execute_queued(self.root / 'config.json')['status'], 'idle')
        self.assertEqual(result['execution_mode'], 'supervisor_in_process')
        self.assertEqual(result['execution_pid'], os.getpid())
        self.assertEqual(self.row()['repair_attempt_count'], 1)
        self.assertEqual(self.action_run()['status'], 'success')
        perform.assert_called_once()

    def test_permission_error_preserves_failed_run_and_stops_without_write_attempt(self):
        self.enqueue()
        with patch.object(repair, 'perform', side_effect=memory.AutomationError('validation_permission_denied', 'fixture')):
            result = repair.execute_queued(self.root / 'config.json')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.row()['repair_status'], 'blocked')
        self.assertEqual(self.row()['repair_attempt_count'], 0)
        self.assertEqual(self.action_run()['error_code'], 'validation_permission_denied')
        self.assertFalse(repair.automatic_budget_available(self.row()))

    def test_index_wait_preserves_same_unstarted_request_then_completes(self):
        request = self.enqueue()
        with patch.object(repair, 'perform', side_effect=memory.AutomationError('memory_index_update_pending', 'fixture')):
            for _ in range(3):
                self.assertEqual(repair.execute_queued(self.root / 'config.json')['status'], 'deferred')
        self.assertEqual(self.row()['repair_run_id'], request['run_id'])
        self.assertEqual(self.row()['repair_status'], 'queued')
        self.assertIsNone(self.row()['repair_pid'])
        self.assertEqual(self.row()['repair_attempt_count'], 0)
        self.assertEqual(self.action_run()['status'], 'running')
        with patch.object(repair, 'perform', side_effect=self.succeed):
            self.assertEqual(repair.execute_queued(self.root / 'config.json')['status'], 'success')
        self.assertEqual(self.row()['repair_attempt_count'], 1)

    def test_manual_wait_does_not_reset_preexisting_auto_attempt_count(self):
        repair._update(self.alert['alert_id'], repair_status='blocked', repair_attempt_count=1)
        with patch.object(repair, 'wake_supervisor', return_value=True):
            repair.start(self.root / 'config.json', self.alert['alert_id'])
        with patch.object(repair, 'perform', side_effect=memory.AutomationError('already_running', 'fixture')):
            repair.execute_queued(self.root / 'config.json')
        self.assertEqual(self.row()['repair_attempt_count'], 1)
        self.assertEqual(self.row()['repair_status'], 'queued')
        self.assertFalse(repair.automatic_budget_available(self.row()))

    def test_stale_claim_is_recorded_interrupted_without_replaying_writes(self):
        self.enqueue()
        repair._update(self.alert['alert_id'], repair_pid=999999)
        with patch.object(repair, 'owner_alive', return_value=False), patch.object(repair, 'perform') as perform:
            self.assertEqual(repair.execute_queued(self.root / 'config.json')['error_code'], 'repair_interrupted')
        self.assertEqual(self.action_run()['status'], 'interrupted')
        self.assertEqual(self.row()['repair_attempt_count'], 0)
        perform.assert_not_called()

    def test_crashed_running_owner_is_not_left_silently_running(self):
        self.enqueue()
        repair._update(self.alert['alert_id'], repair_status='running', repair_pid=999999, repair_attempt_count=1)
        with patch.object(repair, 'owner_alive', return_value=False), patch.object(repair, 'perform') as perform:
            self.assertEqual(repair.execute_queued(self.root / 'config.json')['error_code'], 'repair_interrupted')
        self.assertEqual(self.row()['repair_status'], 'blocked')
        self.assertEqual(self.row()['repair_attempt_count'], 1)
        self.assertEqual(self.action_run()['status'], 'interrupted')
        perform.assert_not_called()

    def test_live_or_unknown_owner_is_not_duplicated(self):
        self.enqueue()
        repair._update(self.alert['alert_id'], repair_pid=12345)
        with patch.object(repair, 'owner_alive', return_value=True), patch.object(repair, 'perform') as perform:
            self.assertEqual(repair.execute_queued(self.root / 'config.json')['status'], 'already_running')
        perform.assert_not_called()
        with patch.object(repair, 'owner_alive', side_effect=PermissionError()), patch.object(repair, 'perform') as perform:
            self.assertEqual(repair.execute_queued(self.root / 'config.json')['error_code'], 'repair_owner_unknown')
        perform.assert_not_called()

    def test_invalid_run_or_nonallowlisted_alert_never_executes(self):
        self.enqueue()
        with self.connect(self.database) as db:
            db.execute("UPDATE action_runs SET action_type='unrelated' WHERE run_id=?", (self.row()['repair_run_id'],))
        with patch.object(repair, 'perform') as perform:
            self.assertEqual(repair.execute_queued(self.root / 'config.json')['error_code'], 'repair_queue_invalid')
        self.assertEqual(self.row()['repair_status'], 'blocked')
        perform.assert_not_called()

    def test_maintenance_preserves_queued_request_without_running(self):
        self.enqueue()
        with patch.object(repair, 'maintenance_active', return_value=True), patch.object(repair, 'perform') as perform:
            self.assertEqual(repair.execute_queued(self.root / 'config.json')['error_code'], 'maintenance_active')
        self.assertEqual(self.row()['repair_status'], 'queued')
        perform.assert_not_called()

    def test_wake_is_fixed_nonrestarting_and_timeout_retains_request(self):
        with patch.object(repair.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as call:
            self.assertTrue(repair.wake_supervisor())
            self.assertEqual(call.call_args.args[0], ['/bin/launchctl', 'kickstart', f'gui/{os.getuid()}/com.example.business-sync-supervisor'])
        with patch.object(repair.subprocess, 'run', side_effect=subprocess.TimeoutExpired('fixture', 5)):
            result = repair.start(self.root / 'config.json', self.alert['alert_id'])
        self.assertFalse(result['supervisor_wake_requested'])
        self.assertEqual(self.row()['repair_status'], 'queued')
        self.assertEqual(self.row()['repair_attempt_count'], 0)


class RetryBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'budget.json'
        self.now=datetime(2026,9,3,tzinfo=timezone.utc)

    def budget(self):
        return ListenerBudget(self.path, clock=lambda:self.now)

    def test_zero_two_one_survive_process_restarts(self):
        for flag,total in ((False,1),(True,3),(None,2)):
            with self.subTest(retryable=flag):
                if self.path.exists(): self.path.unlink()
                for _ in range(total):
                    self.budget().reserve()
                    self.budget().failed(flag)
                    self.now+=timedelta(seconds=2)
                with self.assertRaises(BudgetBlocked): self.budget().reserve()
                self.assertEqual(self.budget().read()['attempt_count'],total)

    def test_crash_after_reservation_does_not_reset(self):
        self.budget().reserve()
        self.budget().reserve()
        with self.assertRaises(BudgetBlocked): self.budget().reserve()

    def test_hold_and_next_retry_cannot_be_bypassed(self):
        self.budget().reserve()
        due=self.now+timedelta(hours=1)
        self.budget().failed(True,guard='terminal_hold',next_retry_at=due.isoformat())
        for _ in range(3):
            with self.assertRaises(BudgetBlocked): self.budget().reserve()
        self.assertEqual(self.budget().read()['attempt_count'],1)
        self.now=due+timedelta(seconds=1)
        self.assertEqual(self.budget().reserve()['attempt_count'],2)

    def test_unknown_hold_is_fail_closed_and_failed_stop_does_not_reset(self):
        self.budget().reserve()
        self.budget().failed(False,guard='in_flight')
        self.budget().stopped()
        self.now+=timedelta(days=2)
        with self.assertRaises(BudgetBlocked): self.budget().reserve()

    def test_pre_ready_error_on_either_stream_and_false_is_strictest(self):
        consumer = memory.EventConsumer.__new__(memory.EventConsumer)
        consumer.stderr_lines = ['{', '"retryable": true, "retry_after_seconds": 30', '}']
        consumer.pre_ready = ['{"error":{"retryable":false}}']
        self.assertEqual(consumer._retry_info(), (False, 30))


if __name__=='__main__': unittest.main()
