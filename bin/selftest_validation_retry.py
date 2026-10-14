#!/usr/bin/env python3
"""Isolated regression checks: no production provider, source or database writes."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import automatic_memory_validation as validation
import validation_retry as retry
import business_sync_state as state
import business_sync_supervisor as supervisor
import business_sync_controller as controller
import dingtalk_memory_automation as memory
import local_sync_repair as repair


class RetryClosureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'sync.sqlite'
        self.now = datetime.fromisoformat('2020-01-02T10:00:00+08:00')
        self.since = self.now - timedelta(minutes=5)
        self.service = SimpleNamespace(config_path=self.root/'config.json', database=self.db)
        state.initialize(self.db)
        self.run_id = '64903b1e-a936-5c85-8607-ed41464746c0'
        with state._connect(self.db) as c:
            c.executescript('CREATE TABLE action_runs(run_id TEXT PRIMARY KEY, action_type TEXT, status TEXT, error_code TEXT, started_at TEXT, completed_at TEXT); CREATE TABLE action_steps(run_id TEXT,step_key TEXT,step_order INTEGER,status TEXT,error_code TEXT,completed_at TEXT,safe_detail_json TEXT);')
            c.execute('INSERT INTO action_runs VALUES(?,?,?,?,?,?)',(self.run_id,'dingtalk_chat','failed',retry.CHANGE,state.now_iso(self.since-timedelta(minutes=1)),state.now_iso(self.since)))
            for i,key in enumerate(('prepare','wait_review','chat_reconcile','chat_memory','decision_registration','dingtalk_validation')):
                last=key=='dingtalk_validation'
                c.execute('INSERT INTO action_steps VALUES(?,?,?,?,?,?,?)',(self.run_id,key,i,'failed' if last else 'success',retry.CHANGE if last else None,state.now_iso(self.since),'{}'))
        state.update_source('dingtalk_chat',self.db,status='manual_required',error_code=retry.CHANGE,last_failure=state.now_iso(self.since),last_run_id=self.run_id,attempt_count=1)
        self.alert=state.open_alert('dingtalk_chat','sync_failure','旧错误','旧内容',severity='error',error_code=retry.CHANGE,database=self.db,at=self.since)
        self.success={'schema_version':2,'full_validation':True,'verdict':'pass','run_id':'1db2ae02-b5b7-588f-ac35-696bbe806480','recorded_at':state.now_iso(self.now-timedelta(minutes=1)),'validation_context':{'fixture':'current'}}
        validation.atomic_json(self.root/'state/last-memory-validation-success.json',self.success)
        validation.atomic_json(self.root/'state/automatic-memory-validation.json',{'status':'verified'})
        index=patch.object(validation,'index_state',return_value={'dirty':7,'applied':7,'ready':True});index.start();self.addCleanup(index.stop)
        self.valid=patch.object(retry.evidence,'receipt_is_current',return_value=True).start()
        self.addCleanup(patch.stopall)

    def reconcile(self):
        with patch.object(controller,'run_dingtalk_scopes',side_effect=AssertionError('source replay forbidden')) as provider:
            retry.reconcile(self.service,{},self.now)
            provider.assert_not_called()

    def test_only_matching_wait_closes_and_history_is_unchanged(self):
        independent=state.open_alert('dingtalk_chat','listener_failure','listener','unverified',error_code='event_not_ready',database=self.db,at=self.since)
        self.reconcile()
        row=state.source('dingtalk_chat',self.db)
        self.assertEqual(row['status'],'healthy')
        self.assertEqual(row['last_run_id'],self.run_id)
        alerts={a['alert_id']:a for a in state.alerts(self.db)}
        self.assertEqual(alerts[self.alert['alert_id']]['status'],'resolved')
        self.assertEqual(alerts[independent['alert_id']]['status'],'open')
        with state._connect(self.db,readonly=True) as c:
            self.assertEqual(c.execute('SELECT status FROM action_runs WHERE run_id=?',(self.run_id,)).fetchone()[0],'failed')
        self.assertEqual(json.loads((self.root/'state/validation-recovery'/f'{self.run_id}.json').read_text())['validation_run_id'],self.success['run_id'])
        self.reconcile()
        self.assertEqual(sum(a['category']=='recovery' for a in state.alerts(self.db)),1)

    def test_stale_receipt_or_missing_stage_does_not_close(self):
        self.valid.return_value=False
        self.reconcile()
        self.assertEqual(state.source('dingtalk_chat',self.db)['status'],'deferred')
        self.assertEqual(state.source('dingtalk_chat',self.db)['attempt_count'],1)
        self.valid.return_value=True
        with state._connect(self.db) as c:
            c.execute("UPDATE action_steps SET status='failed' WHERE step_key='chat_memory'")
        self.reconcile()
        self.assertEqual(state.source('dingtalk_chat',self.db)['status'],'deferred')
        self.assertEqual(state.alerts(self.db,status='open')[0]['status'],'open')

    def test_newer_source_run_and_newer_failure_both_prevent_recovery(self):
        validation.atomic_json(self.root/'state/last-memory-validation-failure.json',{'recorded_at':state.now_iso(self.now),'error_code':'validation_failed'})
        self.reconcile()
        self.assertEqual(state.source('dingtalk_chat',self.db)['status'],'deferred')
        with state._connect(self.db) as c:
            c.execute('INSERT INTO action_runs VALUES(?,?,?,?,?,?)',('newer','dingtalk_chat','failed','real_failure',state.now_iso(self.now),state.now_iso(self.now)))
        (self.root/'state/last-memory-validation-failure.json').unlink()
        self.reconcile()
        self.assertEqual(state.source('dingtalk_chat',self.db)['status'],'deferred')

    def test_required_generation_and_receipt_time_are_checked(self):
        with state._connect(self.db) as c:
            c.execute("UPDATE action_steps SET safe_detail_json=? WHERE step_key='dingtalk_validation'",(json.dumps({'required_generation':8}),))
        self.reconcile()
        self.assertEqual(state.source('dingtalk_chat',self.db)['status'],'deferred')
        self.success['recorded_at']=state.now_iso(self.since-timedelta(minutes=1))
        validation.atomic_json(self.root/'state/last-memory-validation-success.json',self.success)
        self.reconcile()
        self.assertEqual(state.source('dingtalk_chat',self.db)['status'],'deferred')

    def test_wait_notification_is_once_after_two_hours(self):
        self.valid.return_value=False
        self.reconcile()
        self.assertEqual(state.due_alerts(self.db,self.now),[])
        later=self.since+timedelta(hours=2)
        self.assertEqual(len(state.due_alerts(self.db,later)),1)
        claimed=state.claim_delivery(self.alert['alert_id'],self.db,later)
        self.assertEqual(claimed['notification_revision'],0)
        with state._connect(self.db) as c:
            c.execute("UPDATE sync_alerts SET macos_status='visible',miso_status='delivered' WHERE alert_id=?",(self.alert['alert_id'],))
        self.assertEqual(state.due_alerts(self.db,later+timedelta(days=2)),[])
        claimed=state.claim_delivery(self.alert['alert_id'],self.db,later+timedelta(days=2))
        self.assertEqual(claimed['notification_revision'],0)

    def test_controller_saves_completed_stage_checkpoint(self):
        result = {"decision_created": 0, "decision_updated": 0}
        with patch.object(controller, "require_automatic_power"), patch.object(controller, "set_step") as step, patch.object(controller.dingtalk, "run_json", return_value={}), patch.object(memory, "run_refresh_and_validate", side_effect=memory.AutomationError(retry.CHANGE, "changed")):
            with self.assertRaises(controller.ControllerError) as caught:
                controller.finalize_dingtalk(self.root / "config.json", self.run_id, {}, {}, result)
        self.assertEqual(caught.exception.receipt["required_generation"], 7)
        self.assertTrue(caught.exception.receipt["sync_completed"])
        self.assertEqual(step.call_args.args[2], "deferred")

    def test_busy_source_lock_defers_closure(self):
        with validation.try_lock(self.root / "state/sync-center-control.lock"):
            self.reconcile()
        self.assertEqual(state.source("dingtalk_chat", self.db)["status"], "manual_required")
        self.reconcile()
        self.assertEqual(state.source("dingtalk_chat", self.db)["status"], "healthy")

    def use_validation_failure(self, code='validation_failed'):
        with state._connect(self.db) as c:
            c.execute('UPDATE action_runs SET error_code=? WHERE run_id=?', (code, self.run_id))
            c.execute("UPDATE action_steps SET error_code=? WHERE step_key='dingtalk_validation'", (code,))
            c.execute('UPDATE sync_alerts SET error_code=? WHERE alert_id=?', (code, self.alert['alert_id']))
        state.update_source('dingtalk_chat', self.db, error_code=code)

    def test_real_validation_failure_recovers_only_from_new_current_full_receipt(self):
        self.use_validation_failure()
        independent=state.open_alert('dingtalk_chat','listener_failure','listener','unverified',
                                    error_code='event_retry_budget_exhausted',database=self.db,at=self.since)
        self.reconcile()
        self.assertEqual(state.source('dingtalk_chat', self.db)['status'], 'healthy')
        alerts={a['alert_id']:a for a in state.alerts(self.db)}
        self.assertEqual(alerts[independent['alert_id']]['status'], 'open')
        proof=json.loads((self.root/'state/validation-recovery'/f'{self.run_id}.json').read_text())
        self.assertEqual(proof['failure_code'], 'validation_failed')
        with state._connect(self.db, readonly=True) as c:
            self.assertEqual(c.execute('SELECT status FROM action_runs WHERE run_id=?',(self.run_id,)).fetchone()[0],'failed')

    def test_real_failure_stays_blocked_for_stale_receipt_or_incomplete_source(self):
        self.use_validation_failure()
        self.valid.return_value=False
        self.reconcile()
        self.assertEqual(state.source('dingtalk_chat',self.db)['status'],'manual_required')
        self.assertEqual(state.source('dingtalk_chat',self.db)['error_code'],'validation_failed')
        self.valid.return_value=True
        with state._connect(self.db) as c:
            c.execute("UPDATE action_steps SET status='failed' WHERE step_key='chat_memory'")
        self.reconcile()
        self.assertEqual(state.source('dingtalk_chat',self.db)['status'],'manual_required')

    def test_real_failure_requires_source_steps_before_validation(self):
        self.use_validation_failure()
        with state._connect(self.db) as c:
            c.execute("UPDATE action_steps SET step_order=10 WHERE step_key='chat_memory'")
        self.reconcile()
        self.assertEqual(state.source('dingtalk_chat',self.db)['status'],'manual_required')
        self.assertIsNone(retry.checkpoint(self.run_id, 'dingtalk_chat', self.db))

    def test_real_failure_rejects_later_failure_and_mismatched_source_error(self):
        self.use_validation_failure()
        validation.atomic_json(self.root/'state/last-memory-validation-failure.json',
                               {'recorded_at':state.now_iso(self.now),'error_code':'validation_failed'})
        self.reconcile()
        self.assertEqual(state.source('dingtalk_chat',self.db)['status'],'manual_required')
        (self.root/'state/last-memory-validation-failure.json').unlink()
        state.update_source('dingtalk_chat',self.db,error_code='memory_validation_failed')
        self.reconcile()
        self.assertEqual(state.source('dingtalk_chat',self.db)['status'],'manual_required')

    def test_other_failures_and_other_sources_are_not_released(self):
        for code in ['journal_invalid','validation_permission_denied','event_retry_budget_exhausted']:
            self.use_validation_failure(code)
            self.reconcile()
            self.assertEqual(state.source('dingtalk_chat',self.db)['status'],'manual_required')
        self.assertFalse(retry.pending({'source_id':'dingtalk_minutes','error_code':'validation_failed'}))
        self.assertFalse(retry.pending({'source_id':'dingtalk_mail','error_code':'validation_failed'}))

    def test_all_failure_entrypoints_classify_churn_as_waiting(self):
        self.assertEqual(supervisor.classify_receipt({'status':'failed','error_code':retry.CHANGE},'dingtalk_chat')['kind'],'deferred')
        self.assertFalse(supervisor.hold_chat_failure(retry.CHANGE,database=self.db))
        self.assertFalse(repair.eligible({'status':'open','source_id':'memory_core','category':'core_failure','error_code':retry.CHANGE}))
        self.assertFalse(memory.forward_sync_alert({},'failure','critical',retry.CHANGE)['notified'])
        self.assertFalse(memory.raise_alert({},'failure','critical','test','title',retry.CHANGE)['notified'])


if __name__=='__main__':
    unittest.main(verbosity=2)
