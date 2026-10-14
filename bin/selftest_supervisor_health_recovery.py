#!/usr/bin/env python3
"""Offline fault sequences; temp records, fake launchd; no production/network writes."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import sys
sys.dont_write_bytecode=True
import tempfile,json,os,subprocess,unittest
from pathlib import Path
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
import selftest_supervisor_liveness as fixtures
import types
sys.modules['dingtalk_minutes_review_notice']=types.ModuleType('dingtalk_minutes_review_notice')
TEMP_DIRECTORY=Path(tempfile.gettempdir()).resolve()
allowed=fixtures.install_offline_guard()
import supervisor_liveness as live
import supervisor_health_recovery as recovery
import business_sync_supervisor_health as health
import business_sync_supervisor as supervisor
import ledger_recovery as ledger

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=TEST_PARENT);self.root=Path(self.temp.name).resolve()
        self.clock=fixtures.clock();self.now=datetime.fromtimestamp(self.clock['wall_ns']/1e9,timezone.utc)
        self.snap=fixtures.receipt(stage='cycle_failed')
        self.service=fixtures.service(state='not running',pid=None,process_identity=None)
        self.bad={'status':'failed','error_code':'supervisor_finished_receipt_required','can_resolve':False}
        self.alert={'alert_id':'fixture-alert','source_id':'system','category':'supervisor_health','error_code':'supervisor_finished_receipt_required','status':'open','repair_status':'not_attempted'}
        self.calls=[]
    def tearDown(self):self.temp.cleanup()
    def runner(self,args,**kw):self.calls.append(args);return subprocess.CompletedProcess(args,0)
    def step(self,**kw):
        args=dict(allow_request=True,probe=lambda:dict(self.service),runner=self.runner,gone=lambda pid:True,worker_probe=lambda *a:True,alert=self.alert)
        args.update(kw)
        return recovery.tick(self.root,self.bad,self.service,self.snap,self.clock,self.now,**args)
    def test_one_request_survives_restart_and_changed_reason(self):
        first=self.step();self.bad['error_code']='supervisor_exit_nonzero';second=self.step()
        self.assertEqual(first['phase'],'requested');self.assertEqual(second['request_count'],1)
        self.assertEqual((datetime.fromisoformat(first['next_check_at'])-self.now).total_seconds(),300)
        self.assertEqual(len(self.calls),1);self.assertNotIn('-k',self.calls[0])
        self.assertEqual(recovery.view(self.alert,self.root)['request_count'],1)
    def test_running_or_racing_service_never_requested(self):
        self.service=fixtures.service();self.step();self.assertEqual(self.calls,[])
        self.service=fixtures.service(state='not running',pid=None,process_identity=None)
        self.step(probe=lambda:fixtures.service());self.assertEqual(self.calls,[])
    def test_unknown_service_owner_or_worker_never_requested(self):
        for overrides in ({'gone':lambda pid:False},{'worker_probe':lambda *a:False}):
            self.step(**overrides)
        self.service['loaded']=None;self.step();self.assertEqual(self.calls,[])
    def test_corrupt_record_never_rearms_budget_or_closes(self):
        self.step();p=self.root/'state'/recovery.RECEIPT;p.write_text('{}');p.chmod(0o600)
        r=self.step();self.assertFalse(r['closed']);self.assertEqual(r['reason'],'recovery_record_invalid');self.assertEqual(len(self.calls),1)
    def test_missing_initialized_record_never_rearms(self):
        self.step();(self.root/'state'/recovery.RECEIPT).unlink()
        r=self.step();self.assertEqual(len(self.calls),1);self.assertFalse(r['closed']);self.assertEqual(r['reason'],'recovery_record_invalid')
    def test_anchor_commit_crash_before_primary_record_blocks(self):
        real=live._write_json
        def interrupted(root,name,value):
            if name==recovery.RECEIPT:raise OSError('fixture')
            return real(root,name,value)
        with patch.object(live,'_write_json',side_effect=interrupted):
            with self.assertRaises(OSError):self.step()
        r=self.step();self.assertFalse(r['closed']);self.assertEqual(self.calls,[])
    def test_timeout_is_one_actual_attempt_and_never_resent(self):
        def timeout(*a,**kw):self.calls.append(a);raise subprocess.TimeoutExpired('fixture',5)
        a=self.step(runner=timeout);b=self.step();self.assertEqual(a['phase'],'uncertain');self.assertEqual(b['phase'],'uncertain');self.assertEqual(len(self.calls),1)
        self.assertEqual(recovery.view(self.alert,self.root)['request_count'],1)
    def test_crash_after_budget_reservation_has_unknown_actual_count(self):
        def crash(*a,**kw):raise SystemExit()
        with self.assertRaises(SystemExit):self.step(runner=crash)
        self.step();self.assertEqual(self.calls,[])
        v=recovery.view(self.alert,self.root);self.assertIsNone(v['request_count']);self.assertIn('预算已占用',v['summary'])
    def test_persist_failure_prevents_call(self):
        with patch.object(live,'_write_json',side_effect=OSError('fixture')):
            with self.assertRaises(OSError):self.step()
        self.assertEqual(self.calls,[])
    def test_missing_finish_requires_new_finish_not_running_pulse(self):
        self.step();self.clock=fixtures.clock(1010);self.now+=timedelta(seconds=10)
        self.service=fixtures.service();self.snap=fixtures.receipt(heartbeat=fixtures.clock(1009))
        self.bad={'status':'healthy','can_resolve':True};r=self.step();self.assertFalse(r['closed'])
        self.snap=fixtures.receipt(stage='cycle_finished',heartbeat=fixtures.clock(1009),cycle_started=fixtures.clock(1008),cycle_finished=fixtures.clock(1009))
        r=self.step();self.assertTrue(r['closed']);self.assertEqual(r['phase'],'verified')
        self.bad={'status':'failed','error_code':'supervisor_exit_nonzero','can_resolve':False};self.service=fixtures.service(state='not running',pid=None,process_identity=None);self.snap=fixtures.receipt(stage='cycle_failed');r=self.step()
        self.assertFalse(r['closed']);self.assertEqual(len(self.calls),2)
    def test_old_finish_never_closes_or_rearms(self):
        self.step();self.bad={'status':'healthy','can_resolve':True}
        self.snap=fixtures.receipt(stage='cycle_finished',cycle_started=fixtures.clock(999),cycle_finished=fixtures.clock());r=self.step();self.assertFalse(r['closed'])
    def test_legacy_counts_unknown_and_projection_read_only(self):
        before=list(self.root.iterdir());v=recovery.view(self.alert,self.root)
        self.assertEqual(before,list(self.root.iterdir()));self.assertIsNone(v['request_count']);self.assertIn('未记录',v['summary'])
    def test_archived_episode_keeps_own_count_and_recovery_notification(self):
        self.step();self.clock=fixtures.clock(1010);self.now+=timedelta(seconds=10)
        self.snap=fixtures.receipt(stage='cycle_finished',heartbeat=fixtures.clock(1009),cycle_started=fixtures.clock(1008),cycle_finished=fixtures.clock(1009))
        self.bad={'status':'healthy','can_resolve':True};self.step()
        old=dict(self.alert,status='resolved')
        new=dict(self.alert,alert_id='second-fixture')
        self.bad={'status':'failed','error_code':'supervisor_exit_nonzero','can_resolve':False};self.snap=fixtures.receipt(stage='cycle_failed');self.step(alert=new,allow_request=False)
        self.assertEqual(recovery.view(old,self.root)['request_count'],1)
        self.assertEqual(recovery.view(new,self.root)['request_count'],0)
        closure=dict(old,alert_id='closure-fixture',category='recovery',dedupe_key='system:supervisor_health:recovery:fixture-alert')
        self.assertEqual(recovery.view(closure,self.root)['phase'],'verified')
        with patch.object(supervisor,'ROOT',self.root):body=supervisor.macos_notification_body(closure)
        self.assertIn('后台恢复运行',body);self.assertIn('来源处理结果另行核验',body)
    def test_controller_mutex_and_unknown_workers_block(self):
        (self.root/'state').mkdir();p=self.root/'state'/'sync-center-control.lock';p.touch()
        self.assertTrue(recovery.workers_absent(self.root,self.snap,lambda pid:True))
        import fcntl
        with p.open('r') as f:
            fcntl.flock(f,fcntl.LOCK_SH|fcntl.LOCK_NB);self.assertFalse(recovery.workers_absent(self.root,self.snap,lambda pid:True))
        self.snap=fixtures.receipt(workers=[dict(source_id='win11',started_at=None,progress_at=None,progress=None)])
        self.assertFalse(recovery.workers_absent(self.root,self.snap,recovery.process_gone))
    def test_notification_uses_same_projection_and_no_fake_zero(self):
        self.step()
        with patch.object(supervisor,'ROOT',self.root):body=supervisor.macos_notification_body(self.alert)
        self.assertIn(recovery.view(self.alert,self.root)['summary'],body)
        self.assertIn('同步中心后台检查程序',body);self.assertNotIn('本地修复 0 次',body)

class DeferredTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=TEST_PARENT);self.root=Path(self.temp.name).resolve()
        self.db=self.root/'sync.sqlite';self.clock=fixtures.clock();self.now=datetime.fromtimestamp(self.clock['wall_ns']/1e9,timezone.utc)
    def tearDown(self):self.temp.cleanup()
    def record(self,**kw):
        d=dict(schema_version=ledger.VERSION,phase='initialize',identity='',status='retry_wait',error_code='sqlite_database_busy',binding=ledger.LOADED_BINDING,consecutive_failures=1,last_failure_at=self.now.isoformat(),next_retry_at=(self.now+timedelta(seconds=60)).isoformat());d.update(kw)
        p=ledger.path_for(self.db,'initialize');p.parent.mkdir(exist_ok=True);p.write_text(json.dumps(d));return d
    def test_only_current_future_busy_episode_is_wait(self):
        self.record();self.assertIsNotNone(ledger.verified_wait(self.db,'initialize',self.now))
        for changes in [dict(status='blocked'),dict(binding={}),dict(error_code='unknown'),dict(consecutive_failures=True),dict(last_failure_at=(self.now+timedelta(seconds=10)).isoformat()),dict(next_retry_at=self.now.isoformat()),dict(next_retry_at=(self.now+timedelta(hours=1)).isoformat())]:
            self.record(**changes);self.assertIsNone(ledger.verified_wait(self.db,'initialize',self.now))
    def test_main_loop_distinguishes_valid_wait_from_failure(self):
        e=supervisor.Supervisor.__new__(supervisor.Supervisor);e.database=self.db;e.clock=lambda:self.now
        calls=[];e.pulse_owner=lambda *a,**kw:calls.append((a,kw));e._run_once=lambda:(_ for _ in ()).throw(ledger.Deferred('initialize'))
        self.record();self.assertEqual(e.run_once()['status'],'retry_wait');self.assertEqual(calls[-1][0],('cycle_deferred',))
        self.record(status='blocked')
        with self.assertRaises(ledger.Deferred):e.run_once()
        self.assertEqual(calls[-1][0],('cycle_failed',))
    def test_v2_deferred_terminal_is_not_business_completion(self):
        with patch.object(live,'clock_sample',return_value=self.clock),patch.object(live,'process_identity',side_effect=lambda p:f'{p}:100:1'):
            live.pulse(self.root,stage='cycle_started')
            snap=live.pulse(self.root,stage='cycle_deferred',workers=[],defer_reason='ledger_retry_wait',next_retry_at=(self.now+timedelta(seconds=60)).isoformat())
        stopped=fixtures.service(state='not running',pid=None,process_identity=None)
        r=live.evaluate(snap,stopped,fixtures.clock(1001));self.assertTrue(r['can_resolve']);self.assertEqual(r['business_status'],'not_evaluated');self.assertEqual(r['cycle_outcome'],'deferred')
        for key,val in [('schema_version',1),('defer_reason','unknown'),('next_retry_at',self.now.isoformat())]:
            self.assertFalse(live.evaluate(dict(snap,**{key:val}),stopped,fixtures.clock(1001))['can_resolve'])
        self.assertEqual(live.evaluate(snap,stopped,fixtures.clock(1500))['status'],'stale')
    def test_missing_finish_health_fence_requires_terminal(self):
        def write(stage,clock,**kw):
            with patch.object(live,'clock_sample',return_value=clock),patch.object(live,'process_identity',side_effect=lambda p:f'{p}:100:1'):
                snap=live.pulse(self.root,stage=stage,**kw)
            os.utime(self.root/'state'/live.RECEIPT,ns=(clock['wall_ns'],clock['wall_ns']))
            return snap
        svc=fixtures.service(state='not running',pid=None,process_identity=None)
        write('cycle_started',self.clock)
        a=health.owner_health(self.root,svc,fixtures.clock(1001));self.assertEqual(a['error_code'],'supervisor_finished_receipt_required')
        write('cycle_started',fixtures.clock(1002));svc=fixtures.service(pid=os.getpid(),process_identity=f'{os.getpid()}:100:1')
        a=health.owner_health(self.root,svc,fixtures.clock(1003),open_alert=True);self.assertFalse(a['can_resolve'])
        write('cycle_finished',fixtures.clock(1004),workers=[])
        a=health.owner_health(self.root,svc,fixtures.clock(1005),open_alert=True);self.assertTrue(a['can_resolve'])

if __name__=='__main__':
    import uuid
    TEST_PARENT=TEMP_DIRECTORY/('health-recovery-tests-'+uuid.uuid4().hex)
    allowed.add(TEST_PARENT);TEST_PARENT.mkdir(mode=0o700)
    try:unittest.main(verbosity=2)
    finally:TEST_PARENT.rmdir()
