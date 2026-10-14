"""Isolated retry, evidence, scheduling and queue-continuation regressions."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import json
import os
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch
from contextlib import ExitStack
from types import SimpleNamespace
import chat_read_recovery as policy
import sync_dingtalk_chat as scan
import dingtalk_memory_automation as memory
import business_sync_state as state
import business_sync_supervisor as supervisor
import business_sync_controller as controller


class PolicyTests(unittest.TestCase):
    def test_auth_and_nonretryable_override_transient_hint(self):
        for failure in ({'retryable':True,'message':'permission denied timeout'}, {'retryable':False,'message':'timeout'}, {'category':'auth','retryable':True}):
            self.assertIn(policy.failure_evidence({'failures':[failure]})['error_code'], {'chat_read_auth_required','chat_read_blocked'})

    def test_blocked_classification_survives_component_handoff(self):
        self.assertEqual(policy.failure_evidence({}, 'chat_read_blocked', 'generic failure')['error_code'],'chat_read_blocked')

    def test_absolute_retry_time_is_respected(self):
        due = (datetime.now().astimezone() + timedelta(hours=3)).isoformat()
        value = policy.failure_evidence({'error':{'retryable':True,'next_retry_at':due}})
        self.assertGreaterEqual(value['retry_after_seconds'],10799)

    def test_body_is_not_a_failure_or_diagnostic(self):
        value=policy.failure_evidence({'messages':[{'text':'permission denied secret-token'}], 'failures':[{'error':{'reason':'timeout','retryable':True,'retry_after_seconds':42}}]})
        self.assertEqual(value['error_code'],'chat_read_transient')
        self.assertEqual(value['retry_after_seconds'],42)
        self.assertNotIn('secret-token',json.dumps(value))

    def test_cooldown_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state.json'
            path.write_text(json.dumps({'read_recovery':policy.summarize_failures([{'error_code':'chat_read_transient','retry_after_seconds':36000}])}))
            self.assertTrue(policy.cooldown_active(path))

    def test_conversation_failure_persists_cooldown_across_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg=root/'config.json'
            cfg.write_text(json.dumps({'external_root':str(root/'external'),'memory_root':str(root/'vault')}))
            args=SimpleNamespace(config=cfg,dws_bin='/usr/bin/true',external_root=None,memory_root=None,database=None,mode='incremental',start=None,end=None,no_ledger=True)
            failure={'complete':False,'error':{'retryable':True,'next_retry_at':(datetime.now().astimezone()+timedelta(hours=2)).isoformat()}}
            with patch.object(scan,'run_dws',return_value=(failure,1,'read_failure','')):
                with self.assertRaises(scan.SyncError):scan.run_sync(args)
            path=root/'external/dingtalk-chat/state/incremental_status.json'
            self.assertTrue(policy.cooldown_active(path))
            with patch.object(scan,'run_dws') as reader:
                with self.assertRaises(scan.SyncError) as raised:scan.run_sync(args)
                self.assertEqual(raised.exception.code,'chat_read_cooldown');reader.assert_not_called()

    def test_strict_and_listener_codes_are_not_scheduled_reads(self):
        for code in ['event_retry_budget_exhausted','validation_failed','proposal_claim_relation','chat_read_auth_required','chat_read_blocked']:
            self.assertFalse(policy.scheduled_read_recovery(code))

    def test_incomplete_messages_or_nonempty_failures_cannot_pass(self):
        good={'complete':True,'messages':[]}
        self.assertTrue(scan.message_payload_complete(good,0))
        for row in ({'complete':True}, {**good,'failures':[{'error':'failure'}]}, {**good,'hasMore':True}):
            self.assertFalse(scan.message_payload_complete(row,0))


class ReadRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.log=Path(self.tmp.name)/'events.jsonl'
        self.good=({'complete':True,'messages':[]},0,'','')
    def tearDown(self):self.tmp.cleanup()
    def invoke(self):
        return scan.read_messages_with_recovery(Path('/synthetic'),['chat','+chat-messages'],10,self.log,'run','private-conversation')
    def failure(self,**kw):
        return ({'complete':False,'messages':[{'messageId':'m1','text':'private-text'}],'failures':[{'error':kw}]},1,'read_failure','')
    def test_exact_read_retried_once_and_audited(self):
        with patch.object(scan,'run_dws',side_effect=[self.failure(retryable=True),self.good]) as run,patch.object(scan.time,'sleep'):
            self.assertEqual(self.invoke(),self.good)
            self.assertEqual(run.call_count,2)
            self.assertEqual(run.call_args_list[0],run.call_args_list[1])
        log=self.log.read_text();self.assertIn('"recovered":true',log)
        self.assertNotIn('private-text',log);self.assertNotIn('private-conversation',log)
    def test_unknown_false_auth_and_long_cooldown_have_no_immediate_retry(self):
        for kw in ({},{'retryable':False},{'retryable':True,'message':'oauth denied'},{'retryable':True,'retry_after_seconds':999}):
            with patch.object(scan,'run_dws',return_value=self.failure(**kw)) as run,patch.object(scan.time,'sleep') as sleep:
                result=self.invoke();self.assertEqual(run.call_count,1);sleep.assert_not_called()
                self.assertFalse(scan.message_payload_complete(result[0],result[1]))
    def test_retry_exception_preserves_partial_messages(self):
        with patch.object(scan,'run_dws',side_effect=[self.failure(retryable=True),scan.SyncError('dws_timeout','timed out')]),patch.object(scan.time,'sleep'):
            result=self.invoke();self.assertEqual(result[0]['messages'][0]['messageId'],'m1')
            self.assertFalse(scan.message_payload_complete(result[0],result[1]))


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.db=self.root/'state.sqlite'
        self.config=self.root/'config.json';self.config.write_text('{}')
        state.initialize(self.db);self.now=datetime.now().astimezone()
        self.service=supervisor.Supervisor(self.config,database=self.db,clock=lambda:self.now)
    def tearDown(self):self.tmp.cleanup()
    def test_read_failure_backs_off_without_user_parking_or_count_reset(self):
        for n in range(1,5):
            row=self.service.record_outcome('dingtalk_chat',{'status':'failed','error_code':'chat_read_transient'},self.now)
            self.assertEqual(row['attempt_count'],n)
            self.assertNotEqual(row['status'],'manual_required')
            self.assertIsNotNone(row['next_due'])
        self.assertEqual(state.parse_time(row['next_due'])-self.now.replace(microsecond=0),timedelta(hours=6))
    def test_auth_and_validation_stay_held(self):
        for code in ['chat_read_auth_required','journal_conflict']:
            row=self.service.record_outcome('dingtalk_chat',{'status':'failed','error_code':code},self.now)
            self.assertEqual(row['status'],'manual_required')
    def test_legacy_read_hold_resumes_without_rewriting_history(self):
        state.update_source('dingtalk_chat',self.db,status='manual_required',error_code='chat_reconcile_incomplete',attempt_count=2,last_failure=state.now_iso(self.now))
        with patch.object(memory,'runtime_paths',return_value={'status':self.root/'absent'}),patch.object(policy,'listener_view',return_value={}),patch.object(memory,'recovery_conflicts',side_effect=OSError('synthetic')):
            self.service.reconcile_chat_recovery({'external_root':str(self.root),'dingtalk_automation':{'enabled':True}},self.now)
        row=state.source('dingtalk_chat',self.db)
        self.assertEqual(row['status'],'retrying');self.assertEqual(row['attempt_count'],2)
        self.assertEqual(row['last_failure'],state.now_iso(self.now))


class QueueTests(unittest.TestCase):
    def test_source_failure_does_not_strand_already_ingested_messages(self):
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            root=Path(tmp); paths={'log':root/'log'}
            replacements={'acquire_lock':type('Lock',(),{'close':lambda self:None})(), 'runtime_paths':paths,
                'recover_apply_journals':None,'recover_stale_batches':None,'hydrate_pending_events':{'status':'success'},
                'raise_alert':{},'update_live_acceptance':{},'claim_reusable_proposal':None,
                'eligible_queue_count':2,'semantic_model_called_this_slot':False,
                'prepare_batch':{'batch_id':'synthetic','message_count':2},'invoke_codex':{},
                'apply_proposal':{'writes':0,'conflicts':[],'target_memory_ids':[]},'register_pending_decisions':{},
                'status_update':None,'resolve_alert':None,'append_log':None,'handoff_minutes_after_chat_model':None}
            mocks={name:stack.enter_context(patch.object(memory,name,return_value=value)) for name,value in replacements.items()}
            stack.enter_context(patch.object(memory,'run_incremental_sync',side_effect=memory.AutomationError('business_error','source unavailable')))
            stack.enter_context(patch.object(memory.syncv2,'set_component_health'))
            result=memory.run_hourly(root/'config',{},manual=True)
            self.assertEqual(result['messages'],2);self.assertEqual(result['incremental']['status'],'failed')
            mocks['apply_proposal'].assert_called_once()
            self.assertNotIn(unittest.mock.call({},'incremental-partial'),mocks['resolve_alert'].call_args_list)


class ListenerViewTests(unittest.TestCase):
    def test_alive_ready_with_only_direct_is_waiting_not_recovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'dingtalk-chat/state/automation_status.json';p.parent.mkdir(parents=True)
            p.write_text(json.dumps({'listener_state':'ready','listener_pid':os.getpid(),'direct_ready':True,'group_ready':True,'listener_ready_at':'2020-01-02T14:36:37+08:00','live_event_receipts':{'direct':{'received_at':'2020-01-02T17:33:00+08:00'}},'live_acceptance':'live_acceptance_pending'}))
            view=policy.listener_view({'external_root':tmp})
            self.assertIn('群聊',view['current_reason']);self.assertEqual(view['current_blocker'],'live_acceptance_pending')
            self.assertFalse(view['incoming_by_kind']['group'])
    def test_dead_pid_and_missing_ready_are_not_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'dingtalk-chat/state/automation_status.json';p.parent.mkdir(parents=True)
            p.write_text(json.dumps({'listener_state':'ready','listener_pid':9999999,'direct_ready':True,'group_ready':True,'listener_ready_at':'2020-01-02T14:36:37+08:00'}))
            self.assertEqual(policy.listener_view({'external_root':tmp}),{})


if __name__=='__main__':unittest.main(verbosity=2)
