"""Isolated recovery acceptance. No live provider calls or production state writes."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import hashlib,json,os,sqlite3,tempfile,unittest
from pathlib import Path
from datetime import datetime,timedelta
from contextlib import ExitStack
from unittest.mock import patch,Mock
import index_runtime_recovery as r
import index_incident_evidence as e
import automatic_memory_validation as v
import supervisor_incidents as inc
import business_sync_supervisor as sup
import business_sync_state as state

class Recovery(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.db=self.root/'sync.sqlite'
  self.now=datetime.now().astimezone();self.after=(self.now-timedelta(minutes=20)).isoformat();self.event_at=(self.now-timedelta(minutes=15)).isoformat()
  e.atomic(self.root/'state/maintenance-mode.json',{'status':'inactive'})
  self.policy={'schema_version':1,'deployed_at':self.after,'legacy_incidents':{}}
  self.evidence={'error_type':'ValidationFault','fault_code':'memory_index_update_failed','recorded_at':self.event_at,
   'frames':[{'file':'bin/validation_retry.py','function':'_reconcile'},{'file':'bin/automatic_memory_validation.py','function':'index_state','line':154,'loaded_code_sha256':'a'*64}]}
  self.p,self.d=self.event(self.evidence)
  e.atomic(self.root/'state/sync-supervisor-last-error.json',self.evidence)
  e.atomic(self.root/'state/last-memory-validation-success.json',{'recorded_at':(self.now-timedelta(seconds=1)).isoformat(),'full_validation':True,'verdict':'pass','validation_context':{'v':1}})
  c=sqlite3.connect(self.root/'state/bridge.sqlite');c.execute('create table maintenance_state(key text,value text)');c.executemany('insert into maintenance_state values(?,?)',[('index_dirty_generation','8'),('index_applied_generation','8'),('index_last_error','')]);c.commit();c.close()
  self.stack=ExitStack();self.addCleanup(self.stack.close)
  for obj,name,value in [(r,'binding',lambda root:{'code':'one'}),(inc,'code_binding',lambda root:{'code':'one'}),(r.liveness,'process_identity',lambda pid:str(pid)+':birth'),(r,'policy_current',lambda *args:True),(r.ledger,'native_owner',lambda:True),(r.validation_evidence,'receipt_is_current',lambda *args:True),(v,'boot_identity',lambda:'test-boot')]:self.stack.enter_context(patch.object(obj,name,value))
  self.close=self.stack.enter_context(patch.object(state,'resolve_alert',return_value=None))
  self.clock=self.stack.enter_context(patch.object(r,'system_monotonic',return_value=1000))
  self.pid=self.stack.enter_context(patch.object(r.os,'getpid',return_value=10))
 def event(self,ev):
  ident=e.digest(ev);p=self.root/'state/supervisor-incidents'/(ident+'.json');d={'incident_id':ident,'evidence':ev,'root_cause_status':'pending','status':'registered'};e.atomic(p,d);return p,d
 def run_once(self):
  e.atomic(self.root/'state/index-recovery-policy.json',self.policy)
  e.atomic(self.root/'state/supervisor-attention-receipts.json',{'receipts':[{'pid':self.pid.return_value,'process_identity':str(self.pid.return_value)+':birth','loaded_supervisor_sha256':None,'attention_reconciliation':'passed','code_binding':{'code':'one'},'execution_owner':'launchd_native_supervisor'}]})
  return r._verify(self.root,self.db,self.policy)
 def second(self):self.clock.return_value=1601;self.pid.return_value=11;return self.run_once()
 def test_recovers_original_with_two_native_cycles_and_keeps_cause_unknown(self):
  self.assertEqual(self.run_once(),0);self.assertEqual(self.second(),1);d=r.read(self.p)
  self.assertEqual(d['evidence'],self.evidence);self.assertEqual(d['root_cause_status'],'pending');self.assertEqual(d['recovery_status'],'verified');self.close.assert_called_once()
  receipt=r.read(Path(d['recovery_evidence']['receipt']));self.assertFalse(receipt['business_operations_replayed']);self.assertEqual(len(receipt['natural_cycles']),2)
 def test_one_pid_cannot_close(self):
  self.run_once();self.clock.return_value=2000;self.run_once();self.close.assert_not_called()
 def test_observation_too_short_cannot_close(self):
  self.run_once();self.pid.return_value=11;self.clock.return_value=1599;self.run_once();self.close.assert_not_called()
 def test_old_process_relative_clock_not_reused(self):
  self.run_once();p=self.root/'state/index-recovery-status.json';d=r.read(p);d['cycles'][0].pop('clock');e.atomic(p,d);self.second();self.close.assert_not_called()
 def test_stale_receipt_cannot_close(self):
  d=r.read(self.root/'state/last-memory-validation-success.json');d['recorded_at']=self.after;e.atomic(self.root/'state/last-memory-validation-success.json',d);self.run_once();self.second();self.close.assert_not_called()
 def test_new_failure_resets_observation(self):
  self.run_once();new={**self.evidence,'recorded_at':(self.now+timedelta(seconds=1)).isoformat()};self.event(new);self.second();self.close.assert_not_called()
 def test_changed_data_receipt_cannot_close(self):
  self.run_once()
  with patch.object(r.validation_evidence,'receipt_is_current',return_value=False):self.second()
  self.close.assert_not_called()
 def test_context_change_restarts_observation(self):
  self.run_once();p=self.root/'state/last-memory-validation-success.json';d=r.read(p);d['validation_context']={'v':2};e.atomic(p,d);self.second();self.close.assert_not_called()
 def test_actual_index_failure_cannot_close(self):
  c=sqlite3.connect(self.root/'state/bridge.sqlite');c.execute("update maintenance_state set value='private secret' where key='index_last_error'");c.commit();c.close()
  self.run_once();self.second();self.close.assert_not_called();self.assertNotIn('private secret',(self.root/'state/index-recovery-status.json').read_text())
 def test_locked_index_waits_without_clearing_or_closing(self):
  with v.try_lock(self.root/'state/memory-index.lock'):
   self.run_once()
  self.assertEqual(r.read(self.root/'state/index-recovery-status.json')['check_blocker'],'index_busy');self.close.assert_not_called()
 def test_unknown_retained_and_not_user_decision(self):
  ev={**self.evidence,'error_type':'RuntimeError','fault_code':'unknown'};p,d=self.event(ev);self.run_once();self.second();self.assertNotIn('recovery_status',r.read(p));self.close.assert_not_called()
  s=r.read(self.root/'state/index-recovery-status.json');self.assertTrue(s['technical_investigation_required']);self.assertFalse(s['requires_user'])
 def test_tampered_event_never_classified(self):
  d=r.read(self.p);d['evidence']['recorded_at']=self.after;e.atomic(self.p,d);self.run_once();self.second();self.close.assert_not_called()
 def test_legacy_requires_exact_manifest_and_frames(self):
  ev={k:v for k,v in self.evidence.items() if k!='fault_code'};p,d=self.event(ev)
  self.assertIsNone(r.classification(p,d,self.policy))
  self.policy['legacy_incidents'][p.stem]={'evidence_sha256':p.stem,'frames_sha256':e.digest(ev['frames']),'function_sha256':'a'*64,'fault_code':'memory_index_update_failed'}
  self.assertEqual(r.classification(p,d,self.policy),'memory_index_update_failed')
  self.policy['legacy_incidents'][p.stem]['frames_sha256']='wrong';self.assertIsNone(r.classification(p,d,self.policy))
 def test_closure_failure_keeps_receipt_and_resume_only_registers(self):
  self.run_once();self.close.side_effect=sqlite3.OperationalError('fixture')
  with self.assertRaises(sqlite3.Error):self.second()
  self.assertEqual(r.read(self.p)['recovery_status'],'verified');self.assertTrue(Path(r.read(self.p)['recovery_evidence']['receipt']).is_file())
  self.close.side_effect=None;self.run_once();self.assertEqual(r.read(self.root/'state/index-recovery-status.json')['phase'],'verified')
 def test_non_native_does_not_observe(self):
  with patch.object(r.ledger,'native_owner',return_value=False):self.run_once();self.second()
  self.close.assert_not_called()
 def test_new_failure_after_recovery_is_distinct(self):
  self.run_once();self.second();ev={**self.evidence,'recorded_at':(self.now+timedelta(seconds=1)).isoformat()};p,d=self.event(ev);self.run_once();self.assertNotIn('recovery_status',r.read(p))
 def test_evidence_changes_before_commit_cannot_close(self):
  self.run_once();original=r.failure_snapshot;n=[0]
  def changed(*args):
   n[0]+=1;return original(*args) if n[0]==1 else 'changed'
  with patch.object(r,'failure_snapshot',side_effect=changed):self.second()
  self.close.assert_not_called()
 def test_policy_changed_before_commit_cannot_close(self):
  self.run_once()
  with patch.object(r,'policy_current',return_value=False):self.second()
  self.close.assert_not_called()
 def test_failed_validation_supersedes_success(self):
  self.run_once();e.atomic(self.root/'state/last-memory-validation-failure.json',{'recorded_at':self.now.isoformat()});self.second();self.close.assert_not_called()
 def test_count_separates_old_operations(self):
  e.atomic(self.db.parent/'ledger-recovery/old.json',{'attempts':52});self.run_once();s=r.read(self.root/'state/index-recovery-status.json');self.assertEqual(s['historical_database_attempts'],52);self.assertEqual(s['event_verification_count'],1);self.assertEqual(s['actual_repair_count'],0)
 def test_source_lock_preserves_problem(self):
  with v.try_lock(self.root/'state/sync-center-control.lock'):self.run_once()
  self.assertEqual(r.read(self.root/'state/index-recovery-status.json')['check_blocker'],'source_busy')

class Evidence(unittest.TestCase):
 def test_error_content_never_serialized_and_history_not_overwritten(self):
  with tempfile.TemporaryDirectory() as tmp:
   for _ in range(2):e.record_failure(Path(tmp),'index_refresh',9,RuntimeError('secret token business text'))
   ps=list((Path(tmp)/'state/index-failures').glob('*.json'));self.assertEqual(len(ps),2)
   for p in ps:self.assertNotIn('secret',p.read_text())
 def test_code_and_cause_allowlist(self):
  try:
   try:raise sqlite3.OperationalError('secret')
   except Exception as cause:raise v.ValidationFault('memory_index_state_unavailable') from cause
  except Exception as exc:
   d=e.safe_exception(exc);self.assertEqual(d['fault_code'],'memory_index_state_unavailable');self.assertEqual(d['cause_type'],'OperationalError');self.assertNotIn('secret',json.dumps(d))
 def test_import_preserves_typed_fault(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);(root/'state').mkdir();p=root/'state/sync-supervisor-errors.jsonl';p.write_text(json.dumps({'recorded_at':datetime.now().astimezone().isoformat(),'error_type':'ValidationFault','reason':'ValidationFault','fault_code':'memory_index_update_failed','cause_type':'OperationalError','private':'secret'})+'\n')
   inc.import_history(root);d=json.loads(next((root/'state/supervisor-incidents').glob('*.json')).read_text());self.assertEqual(d['evidence']['fault_code'],'memory_index_update_failed');self.assertNotIn('secret',json.dumps(d))
 def test_ui_reports_wait_and_separate_counters(self):
  d=r.workbench_view({'check_blocker':'index_busy','evidence_gaps':['恢复证据缺失'],'event_verification_count':2,'historical_database_attempts':52,'actual_repair_count':0});self.assertIn('等待索引访问锁',d['current_reason']);self.assertNotIn('索引正在更新',d['current_reason']);self.assertFalse(d['decision_required'])


class PolicyAdmission(unittest.TestCase):
 def test_policy_requires_frozen_tests_code_and_legacy_proof(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);binary=root/'bin/BUSINESS Sync Supervisor.app/Contents/MacOS/business-sync-supervisor-launcher';binary.parent.mkdir(parents=True);binary.write_bytes(b'fixture-not-executable')
   proof=root/'legacy.json';e.atomic(proof,{'legacy_incidents':{'fixture':'value'}})
   log=root/'tests.log';log.write_text('isolated tests passed')
   sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
   policy={'schema_version':1,'deployed_at':datetime.now().astimezone().isoformat(),'code_binding':{'code':'one'},'test_evidence':[{'path':str(log),'sha256':sha(log)}],
           'legacy_incidents':{'fixture':'value'},'legacy_verification':{'path':str(proof),'sha256':sha(proof)},'native_binary_sha256':sha(binary)}
   with patch.object(r,'binding',return_value={'code':'one'}),patch.object(r,'LOADED',{'code':'one'}):
    self.assertTrue(r.policy_current(root,policy));policy['legacy_incidents']={'fixture':'changed'};self.assertFalse(r.policy_current(root,policy));policy['legacy_incidents']={'fixture':'value'}
    log.write_text('changed evidence');self.assertFalse(r.policy_current(root,policy))
 def test_runtime_event_lock_prevents_concurrent_verification(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   with e.runtime_event_guard(root):
    with patch.object(r,'policy_current',side_effect=AssertionError('must not enter')):self.assertEqual(r.verify(root,root/'sync.sqlite'),0)

class NativeClock(unittest.TestCase):
 def test_real_child_clocks_share_parent_epoch(self):
  import subprocess,sys
  before=r.system_monotonic()
  program="import sys;sys.path.insert(0,sys.argv[1]);import index_runtime_recovery as r;print(r.system_monotonic())"
  values=[float(subprocess.check_output([sys.executable,'-c',program,str(Path(r.__file__).parent)],text=True)) for _ in range(2)]
  after=r.system_monotonic()
  self.assertTrue(before <= values[0] <= values[1] <= after)
  self.assertLess(after-before,30)

if __name__=='__main__':unittest.main()
