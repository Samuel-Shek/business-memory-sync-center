"""Regression cases for strict failures, atomic recovery and receipt privacy."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from datetime import timedelta
import validation_retry as retry
import business_sync_state as state
import validation_failure_receipts as history
import dingtalk_memory_automation as memory
from selftest_validation_retry import RetryClosureTests
from selftest_listener_restart import RestartTests


class StrictRecoveryTests(RetryClosureTests):
    def setUp(self):
        super().setUp()
        with state._connect(self.db) as c:
            c.execute("UPDATE action_runs SET error_code='validation_failed'")
            c.execute("UPDATE action_steps SET error_code='validation_failed' WHERE step_key='dingtalk_validation'")
            c.execute("UPDATE supervisor_sources SET error_code='validation_failed' WHERE source_id='dingtalk_chat'")
            c.execute("UPDATE sync_alerts SET error_code='validation_failed' WHERE alert_id=?",(self.alert['alert_id'],))

    # Base waiting-specific cases are already run in their original suite.
    test_stale_receipt_or_missing_stage_does_not_close = None
    test_newer_source_run_and_newer_failure_both_prevent_recovery = None
    test_required_generation_and_receipt_time_are_checked = None
    test_wait_notification_is_once_after_two_hours = None

    def test_strict_failure_is_not_enabled_for_other_sources(self):
        for source in ['dingtalk_mail', 'dingtalk_minutes', 'win11']:
            self.assertFalse(retry.pending({'source_id': source, 'error_code': 'validation_failed'}))
        self.assertTrue(retry.pending({'source_id': 'dingtalk_minutes', 'error_code': retry.CHANGE}))

    def test_source_failure_newer_than_success_does_not_close(self):
        state.update_source('dingtalk_chat',self.db,last_failure=state.now_iso(self.now))
        self.reconcile()
        self.assertEqual(state.source('dingtalk_chat',self.db)['status'],'manual_required')

    def test_strict_failure_remains_strict_without_fresh_proof(self):
        self.valid.return_value=False
        self.reconcile()
        self.assertEqual(state.source('dingtalk_chat',self.db)['status'],'manual_required')
        self.assertFalse((self.root/'state/validation-request.json').exists())

    def test_missing_stage_and_later_stage_do_not_close(self):
        with state._connect(self.db) as c:
            c.execute("UPDATE action_steps SET status='failed' WHERE step_key='chat_memory'")
        self.reconcile()
        self.assertEqual(state.source('dingtalk_chat',self.db)['status'],'manual_required')
        with state._connect(self.db) as c:
            c.execute("UPDATE action_steps SET status='success' WHERE step_key='chat_memory'")
            c.execute("INSERT INTO action_steps VALUES(?,?,?,?,?,?,?)",(self.run_id,'late_step',99,'failed','other',state.now_iso(self.now),'{}'))
        self.reconcile()
        self.assertEqual(state.source('dingtalk_chat',self.db)['status'],'manual_required')

    def test_observation_refresh_is_not_a_new_failure(self):
        with state._connect(self.db) as c:
            c.execute('UPDATE sync_alerts SET last_seen=? WHERE alert_id=?',(state.now_iso(self.now),self.alert['alert_id']))
        self.reconcile()
        self.assertEqual(state.source('dingtalk_chat',self.db)['status'],'healthy')

    def test_other_sources_and_listener_remain_unchanged(self):
        original={sid:state.source(sid,self.db) for sid in ['dingtalk_mail','dingtalk_minutes','win11']}
        other=state.open_alert('dingtalk_chat','listener_failure','test','test',error_code='validation_failed',database=self.db,at=self.since)
        self.reconcile()
        self.assertEqual(original,{sid:state.source(sid,self.db) for sid in original})
        self.assertEqual(next(a for a in state.alerts(self.db) if a['alert_id']==other['alert_id'])['status'],'open')

    def test_newer_run_during_prior_run_blocks(self):
        with state._connect(self.db) as c:
            c.execute('INSERT INTO action_runs VALUES(?,?,?,?,?,?)',('newer','dingtalk_chat','failed','real_failure',state.now_iso(self.since-timedelta(seconds=30)),state.now_iso(self.since)))
        self.reconcile()
        self.assertEqual(state.source('dingtalk_chat',self.db)['status'],'manual_required')

    def test_newer_failure_blocks(self):
        (self.root/'state/last-memory-validation-failure.json').write_text(json.dumps({'recorded_at':state.now_iso(self.now),'error_code':'validation_failed'}))
        self.reconcile()
        self.assertEqual(state.source('dingtalk_chat',self.db)['status'],'manual_required')

    def test_transaction_failure_rolls_back_alert_and_receipt(self):
        with state._connect(self.db) as c:
            c.execute("CREATE TRIGGER crash_recovery BEFORE UPDATE ON supervisor_sources WHEN NEW.status='healthy' BEGIN SELECT RAISE(ABORT, 'simulated crash'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.reconcile()
        self.assertEqual(state.source('dingtalk_chat',self.db)['status'],'manual_required')
        self.assertEqual(state.alerts(self.db,status='open')[0]['status'],'open')
        with state._connect(self.db) as c:c.execute('DROP TRIGGER crash_recovery')
        self.reconcile()
        self.assertEqual(state.source('dingtalk_chat',self.db)['status'],'healthy')

    def test_export_recovers_after_commit_crash(self):
        actual=retry.validation.atomic_json
        def crash(path, payload):
            if path.parent.name=='validation-recovery' and payload.get('state')=='resolved':raise OSError('simulated export interruption')
            return actual(path,payload)
        with patch.object(retry.validation,'atomic_json',side_effect=crash):
            with self.assertRaises(OSError):self.reconcile()
        self.assertEqual(state.source('dingtalk_chat',self.db)['status'],'healthy')
        self.reconcile()
        result=json.loads((self.root/'state/validation-recovery'/f'{self.run_id}.json').read_text())
        self.assertEqual(result['state'],'resolved')
        self.assertEqual(sum(a['category']=='recovery' for a in state.alerts(self.db)),1)


class FailureHistoryTests(unittest.TestCase):
    def test_append_compatibility_and_no_source_text(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'last-memory-validation-failure.json'
            first={'recorded_at':'2020-01-02T04:14:07+08:00','error_code':'validation_failed','errors':['/private/customer-A.md: source hash mismatch SECRET-TEXT']}
            history.publish(p,first)
            history.publish(p,{'recorded_at':'2020-01-02T05:00:00+08:00','errors':['database invalid'],'error_code':'validation_failed'})
            docs=list((p.parent/'validation-failures').glob('*.json'))
            self.assertGreaterEqual(len(docs),2)
            text=''.join(f.read_text() for f in docs)
            for secret in ['/private/','customer-A','SECRET-TEXT']:self.assertNotIn(secret,text)
            self.assertIn('mismatch',text)
            self.assertEqual(json.loads(p.read_text())['errors'],['database invalid'])


class ListenerAdmissionTests(RestartTests):
    def test_exhausted_scope_does_not_spawn_or_rewrite_status(self):
        b=self.budget();b.reserve();b.reserve();b.failed(None)
        self.now += timedelta(seconds=3)
        b.clock=lambda:self.now
        consumers=[SimpleNamespace(budget=b)]
        self.assertIn(memory.listener_admission(consumers),{'event_retry_budget_exhausted','event_subscription_cooldown'})
        before=b.path.read_bytes()
        for i in range(3):memory.listener_admission(consumers)
        self.assertEqual(before,b.path.read_bytes())
        self.assert_preserved()

    def test_ready_and_stopped_remain_restartable(self):
        b=self.budget();b.reserve();b.ready();b.stopped()
        self.assertIsNone(memory.listener_admission([SimpleNamespace(budget=b)]))

if __name__=='__main__':unittest.main(verbosity=2)
