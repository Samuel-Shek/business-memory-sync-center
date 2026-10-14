"""Isolated fault injection; no provider or business writes."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import hashlib,json,sqlite3,tempfile,unittest
from pathlib import Path
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
import ledger_recovery as r
import business_sync_state as s
import business_sync_supervisor as supervisor
import supervisor_incidents as incidents
import automatic_memory_validation as validation
import validation_evidence
import media_actions

def isolate_incident_writes(case):
 # The ledger records incident files beside its module even with a temporary DB.
 # Redirect that root and reject any escaped retain before it can write.
 original = incidents.retain
 def guarded_retain(root, evidence):
  if Path(root).resolve() != case.root.resolve():
   raise AssertionError("test incident write escaped temporary root")
  return original(root, evidence)
 for obj, name, value in [(r, '__file__', str(case.root.resolve()/'bin/ledger_recovery.py')),
                          (incidents, 'retain', guarded_retain)]:
  guard=patch.object(obj,name,value);guard.start();case.addCleanup(guard.stop)

class Recovery(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.db=self.root/'ledger.sqlite'
  isolate_incident_writes(self)
  r.atomic(self.root/'state/ledger-selfheal-policy.json',{'test_fixture':True})
  p=patch.object(r,'_NATIVE',False);p.start();self.addCleanup(p.stop)
  s.initialize(self.db);self.a=s.open_alert('system','fixture','fixture','fixture',database=self.db)
 def test_fault_injection_incident_stays_in_temporary_root(self):
  r.failure(self.db,'fixture',sqlite3.OperationalError('database is locked'))
  events=list(incidents.inbox(self.root).glob('*.json'))
  self.assertEqual(len(events),1)
  self.assertEqual(r.read(events[0])['evidence']['sqlite']['operation'],'fixture')
 def test_escaped_incident_write_is_rejected_before_write(self):
  with self.assertRaisesRegex(AssertionError,'escaped temporary root'):
   incidents.retain(self.root.parent/'forbidden-external-root',{'reason':'fixture'})

 def row(self):return s.alerts(self.db)[0]
 def test_delivery_paths_use_bounded_transaction(self):
  with patch.object(s,'_write',wraps=s._write) as w:
   s.claim_delivery(self.a['alert_id'],self.db);s.mark_delivery(self.a['alert_id'],'macos','visible',database=self.db)
  self.assertEqual([x.args[1] for x in w.call_args_list],['claim_delivery','mark_delivery'])
 def pending(self):
  with patch.object(s,'_write',side_effect=sqlite3.OperationalError('database is locked')):
   with self.assertRaises(sqlite3.Error):s.mark_delivery(self.a['alert_id'],'macos','visible',database=self.db)
 def test_spool_survives_db_failure_and_replays_once(self):
  self.pending();self.assertEqual(self.row()['macos_status'],'pending');self.assertEqual(r.flush(self.db),1);self.assertEqual(r.flush(self.db),0)
  with s._connect(self.db,readonly=True) as c:self.assertEqual(c.execute('SELECT count(*) FROM alert_delivery_audit').fetchone()[0],1)
  self.assertEqual(self.row()['macos_attempt_count'],1)
 def test_old_revision_never_overwrites_current(self):
  self.pending()
  with s._connect(self.db) as c:c.execute('UPDATE sync_alerts SET notification_revision=notification_revision+1')
  r.flush(self.db);self.assertEqual(self.row()['macos_status'],'pending')
 def test_uncertain_send_only_queries(self):
  s.mark_delivery(self.a['alert_id'],'macos','uncertain',database=self.db)
  class Result:returncode=0;stdout='{"status":"visible"}'
  with patch.object(supervisor.subprocess,'run',return_value=Result()) as send:supervisor.deliver_macos(self.row(),self.db)
  self.assertEqual(json.loads(send.call_args.kwargs['input'])['operation'],'status')
 def test_shared_sender_lock(self):
  with r.locked(self.db.with_suffix('.delivery.lock')):
   with patch.object(supervisor,'_deliver_due_alerts',side_effect=AssertionError('double send')):self.assertEqual(supervisor.deliver_due_alerts({},self.db),0)
 def test_backoff_escalation_and_restart(self):
  at=datetime(2026,9,20,tzinfo=timezone.utc)
  for seconds in (60,120,300,300):
   r.failure(self.db,'claim_delivery',sqlite3.OperationalError('database is locked'),'x',at=at);d=r.read(r.path_for(self.db,'claim_delivery','x'))
   self.assertEqual((datetime.fromisoformat(d['next_retry_at'])-at).total_seconds(),seconds);self.assertFalse(r.due(self.db,'claim_delivery','x',at=at));at+=timedelta(seconds=seconds)
  self.assertTrue(d['escalated']);self.assertEqual(d['attempts'],4)
 def test_ambiguous_commit_and_nonbusy_not_retried(self):
  e=sqlite3.OperationalError('database is locked');e.safe_sqlite_evidence={'sqlite_code':5,'commit_outcome_ambiguous':True};self.assertFalse(r.busy(e));self.assertFalse(r.busy(sqlite3.OperationalError('disk I/O error')))
 def test_fast_initialization_no_sql_write(self):
  original=s._connect;statements=[]
  def connect(*a,**kw):
   c=original(*a,**kw);c.set_trace_callback(statements.append);return c
  with patch.object(s,'_connect',side_effect=connect):s.initialize(self.db)
  self.assertFalse(any(x.split()[0].upper() in {'INSERT','UPDATE','CREATE','ALTER','DELETE'} for x in statements))
 def test_prepared_media_has_no_external_reads(self):
  import operational_workbench as work
  with s._connect(self.db) as c:work.initialize(c)
  snapshot=dict(signatures={},holds=[],saved={},cards={},summaries={},revisions={})
  with s._connect(self.db) as c:
   c.execute('BEGIN IMMEDIATE')
   with patch.object(media_actions.video_hold,'rows',side_effect=AssertionError('external read')),patch.object(media_actions,'decisions',side_effect=AssertionError('external read')):self.assertTrue(media_actions.reconcile(c,{},s.now_iso(),prepared=snapshot))
   c.rollback()
 def test_cards_reuse_classification_snapshot_in_source_description(self):
  import media_sources,media_classification
  held={'hold_id':'h','version':'v','kind':'video','source_id':'dingtalk_chat','first_seen':'2020-01-02','name':'x'}
  snapshot={'h':{'version':'v'}}
  with patch.object(media_classification,'effective',return_value='video'),patch.object(media_sources,'describe',return_value={'available':True,'kind_label':'视频','source_binding':'fixture'}) as describe:
   media_actions.cards_for({'kind':'video_hold','issue_id':'i'},[held],classifications=snapshot,saved={})
  self.assertIs(describe.call_args.kwargs['classifications'],snapshot)
 def test_changed_media_not_applied(self):
  f=self.root/'source';f.write_text('v1');snapshot={'signatures':media_actions.snapshot_signatures([f])};f.write_text('v2')
  with s._connect(self.db) as c:self.assertFalse(media_actions.reconcile(c,{},s.now_iso(),prepared=snapshot))

 def test_commit_before_spool_ack_is_idempotent(self):
  with patch.object(r,'finish_spool',side_effect=OSError('simulated crash')):
   with self.assertRaises(OSError):s.mark_delivery(self.a['alert_id'],'macos','visible',database=self.db)
  r.flush(self.db)
  with s._connect(self.db,readonly=True) as c:self.assertEqual(c.execute('SELECT count(*) FROM alert_delivery_audit').fetchone()[0],1)
  self.assertEqual(self.row()['macos_attempt_count'],1)
 def test_known_revision_receipt_persists_even_when_read_connection_unavailable(self):
  with patch.object(s,'_connect',side_effect=sqlite3.OperationalError('database is locked')):
   with self.assertRaises(sqlite3.Error):s.mark_delivery(self.a['alert_id'],'macos','visible',database=self.db,notification_revision=0)
  self.assertEqual(len(list((r.directory(self.db)/'delivery-outbox').glob('*.json'))),1)
 def test_recurrence_resets_backoff_but_keeps_history(self):
  at=r.now()
  r.failure(self.db,'fixture',sqlite3.OperationalError('database is locked'),at=at)
  r.failure(self.db,'fixture',sqlite3.OperationalError('database is locked'),at=at)
  r.success(self.db,'fixture',at=at+timedelta(seconds=1))
  r.failure(self.db,'fixture',sqlite3.OperationalError('database is locked'),at=at+timedelta(seconds=2))
  d=r.read(r.path_for(self.db,'fixture'));self.assertEqual(d['consecutive_failures'],1);self.assertTrue(d['escalated'])

 def test_recovered_outage_does_not_hide_recurrence(self):
  exc=sqlite3.OperationalError('database is locked')
  with patch.object(s,'now_iso',return_value='2020-01-02T12:00:00+00:00'):
   first=incidents.retain_database_outage(self.root,exc,self.db)
  path=incidents.inbox(self.root)/(first['incident_id']+'.json')
  d=r.read(path);d['recovery_status']='verified';r.atomic(path,d)
  with patch.object(s,'now_iso',return_value='2020-01-02T12:01:00+00:00'):
   second=incidents.retain_database_outage(self.root,exc,self.db)
  self.assertNotEqual(first['incident_id'],second['incident_id'])

 def test_source_retry_does_not_block_another_source(self):
  first,second=s.SOURCE_IDS[:2]
  r.failure(self.db,'update_source',sqlite3.OperationalError('database is locked'),identity=first)
  with self.assertRaises(r.Deferred):s.update_source(first,database=self.db)
  self.assertEqual(s.update_source(second,database=self.db)['source_id'],second)

 def test_only_registered_launchd_health_pid_is_native(self):
  import subprocess
  entry=str(Path(r.__file__).parent/'business_sync_supervisor_health.py')
  with patch.object(r.sys,'argv',[entry]),patch.object(r.os,'getppid',return_value=1),patch.object(r.os,'getpid',return_value=77):
   with patch.object(subprocess,'check_output',return_value='job {\n pid = 77\n}'):
    self.assertTrue(r.native_owner())
   with patch.object(subprocess,'check_output',return_value='job {\n pid = 88\n}'):
    self.assertFalse(r.native_owner())

 def test_other_writer_cannot_suppress_native_operation_receipt(self):
  at=r.now()
  with patch.object(r,'_NATIVE',False):r.success(self.db,'workbench_items',at=at)
  with patch.object(r,'_NATIVE',True):r.success(self.db,'workbench_items',at=at+timedelta(seconds=1))
  d=r.read(r.path_for(self.db,'workbench_items'));self.assertTrue(d['last_native_success']['native'])

class Acceptance(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);(self.root/'state').mkdir();self.db=self.root/'state/sync.sqlite'
  isolate_incident_writes(self)
  s.initialize(self.db)
  self.at=datetime(2026,9,20,12,tzinfo=timezone.utc);self.binding={n:'a'*64 for n in r.BINDING_NAMES};self.binding['validation_evidence.py']='b'*64
  native=self.root/'bin/BUSINESS Sync Supervisor.app/Contents/MacOS/business-sync-supervisor-launcher';native.parent.mkdir(parents=True);native.write_bytes(b'test native');h=hashlib.sha256(native.read_bytes()).hexdigest()
  log=self.root/'tests.log';log.write_text('pass')
  self.save('ledger-selfheal-policy.json',dict(schema_version=1,deployed_at=r.stamp(self.at),capability_binding={'ledger_recovery.py':'a'*64},native_binary_sha256=h,test_evidence=[dict(path=str(log),sha256=hashlib.sha256(log.read_bytes()).hexdigest())]))
  self.save('maintenance-mode.json',{'status':'inactive'})
  self.event=incidents.retain(self.root,{'recorded_at':r.stamp(self.at-timedelta(minutes=5)),'reason':'sqlite_database_busy','error_type':'OperationalError','sqlite':{'sqlite_code':5,'operation':'claim_delivery'}});incidents.register_pending(self.root,self.db);self.save('sync-supervisor-last-error.json',self.event['evidence'])
  self.cycles=[dict(pid=i,checked_at=r.stamp(self.at+timedelta(minutes=i)),code_binding=self.binding,execution_owner='launchd_native_supervisor',attention_reconciliation='passed',native_binary_sha256=h) for i in (1,12)];self.save('supervisor-attention-receipts.json',{'receipts':self.cycles});self.save('last-memory-validation-success.json',{'recorded_at':r.stamp(self.at+timedelta(minutes=10))})
  r.atomic(r.path_for(self.db,'claim_delivery'),dict(phase='claim_delivery',status='observing',last_native_success=dict(at=r.stamp(self.at+timedelta(minutes=2)),binding={n:self.binding[n] for n in r.BINDING_NAMES})))
  for obj,name,v in [(incidents,'code_binding',self.binding),(validation,'index_state',dict(dirty=1,applied=1)),(validation_evidence,'receipt_is_current',True),(r,'now',self.at+timedelta(minutes=13))]:
   p=patch.object(obj,name,return_value=v);p.start();self.addCleanup(p.stop)
 def save(self,n,v):r.atomic(self.root/'state'/n,v)
 def test_complete_evidence_closes_preserves_original(self):
  self.assertEqual(r.verify_incidents(self.root,self.db),1);d=r.read(incidents.inbox(self.root)/(self.event['incident_id']+'.json'));self.assertEqual(d['evidence'],self.event['evidence']);self.assertEqual(d['root_cause_status'],'pending');self.assertEqual(d['recovery_status'],'verified');self.assertEqual(incidents.unresolved(self.root),0)
 def test_missing_path_never_closes(self):
  r.atomic(r.path_for(self.db,'claim_delivery'),{'phase':'claim_delivery'});self.assertEqual(r.verify_incidents(self.root,self.db),0)
 def test_changed_code_needs_new_cycles(self):
  with patch.object(incidents,'code_binding',return_value={**self.binding,'validation_evidence.py':'c'*64}):self.assertEqual(r.verify_incidents(self.root,self.db),0)
 def test_newer_failure_blocks(self):
  self.save('last-memory-validation-failure.json',{'recorded_at':r.stamp(self.at+timedelta(minutes=11))});self.assertEqual(r.verify_incidents(self.root,self.db),0)
 def test_maintenance_and_short_observation_block(self):
  self.save('maintenance-mode.json',{'status':'active'});self.assertEqual(r.verify_incidents(self.root,self.db),0);self.save('maintenance-mode.json',{'status':'inactive'});self.cycles[-1]['checked_at']=r.stamp(self.at+timedelta(minutes=5));self.save('supervisor-attention-receipts.json',{'receipts':self.cycles});self.assertEqual(r.verify_incidents(self.root,self.db),0)
 def test_partial_success_updates_progress_without_closing_remaining(self):
  other=incidents.retain(self.root,{'recorded_at':r.stamp(self.at-timedelta(minutes=4)),'reason':'sqlite_database_busy','error_type':'OperationalError','sqlite':{'sqlite_code':5,'operation':'mark_delivery'}})
  self.assertEqual(r.verify_incidents(self.root,self.db),1)
  progress=r.read(self.root/'state/supervisor-recovery-status.json')
  self.assertEqual(progress['pending_incidents'],1);self.assertEqual(progress['missing_paths'],['mark_delivery'])
  self.assertEqual(progress['check_blocker'],'operation_receipt_pending');self.assertIsNotNone(progress['next_check_at'])
  self.assertEqual(incidents.unresolved(self.root),1)
  self.assertNotEqual(r.read(incidents.inbox(self.root)/(other['incident_id']+'.json')).get('recovery_status'),'verified')
 def test_manual_only_pending_does_not_block_other_proven_path(self):
  other=incidents.retain(self.root,{'recorded_at':r.stamp(self.at-timedelta(minutes=4)),'reason':'sqlite_database_busy','error_type':'OperationalError','sqlite':{'sqlite_code':5,'operation':'mark_all_due'}})
  r.atomic(r.path_for(self.db,'mark_all_due'),dict(phase='mark_all_due',status='retry_wait',last_failure_at=r.stamp(self.at-timedelta(minutes=3)),next_retry_at=r.stamp(self.at)))
  before=r.read(r.path_for(self.db,'mark_all_due'))
  with patch.object(s,'mark_all_due',side_effect=AssertionError('must not replay manual sync')):
   self.assertEqual(r.verify_incidents(self.root,self.db),1)
  self.assertEqual(r.read(r.path_for(self.db,'mark_all_due')),before)
  self.assertEqual(incidents.unresolved(self.root),1)
  self.assertEqual(r.read(self.root/'state/supervisor-recovery-status.json')['missing_paths'],['mark_all_due'])
  self.assertNotEqual(r.read(incidents.inbox(self.root)/(other['incident_id']+'.json')).get('recovery_status'),'verified')
 def test_pending_same_path_cannot_borrow_prior_success(self):
  p=r.path_for(self.db,'claim_delivery');d=r.read(p);d.update(status='retry_wait');r.atomic(p,d)
  self.assertEqual(r.verify_incidents(self.root,self.db),0);self.assertEqual(incidents.unresolved(self.root),1)
 def test_unregistered_pending_path_still_blocks_group(self):
  r.atomic(r.path_for(self.db,'unknown'),dict(phase='unknown',status='blocked'))
  self.assertEqual(r.verify_incidents(self.root,self.db),0)
  self.assertEqual(r.read(self.root/'state/supervisor-recovery-status.json')['check_blocker'],'writes_pending')
 def test_stale_validation_stays_open_and_explains_next_check(self):
  with patch.object(validation_evidence,'receipt_is_current',return_value=False):self.assertEqual(r.verify_incidents(self.root,self.db),0)
  progress=r.read(self.root/'state/supervisor-recovery-status.json')
  self.assertEqual(progress['check_blocker'],'validation_changed');self.assertFalse(progress['full_validation_current'])
  self.assertEqual(progress['pending_incidents'],1);self.assertEqual(progress['observation_seconds'],660)
 def test_natural_supervisor_checks_after_validation_and_cycle(self):
  from contextlib import ExitStack
  import operational_workbench as work,validation_retry
  engine=supervisor.Supervisor.__new__(supervisor.Supervisor)
  engine.database=None;engine.config_path=supervisor.CONFIG_PATH;engine.clock=lambda:self.at
  order=[]
  with ExitStack() as stack:
   stack.enter_context(patch.object(engine,'schedule_full_validation',side_effect=lambda config:order.append('validation')))
   for name in ('_reconcile_attention','reconcile_chat_recovery','schedule_local_repair'):stack.enter_context(patch.object(engine,name))
   for obj,name in ((work,'reconcile'),(work,'recover_actions'),(validation_retry,'reconcile')):stack.enter_context(patch.object(obj,name))
   stack.enter_context(patch.object(incidents,'record_attention_success',side_effect=lambda root, **kwargs:order.append('cycle')))
   stack.enter_context(patch.object(incidents,'verify_correction',side_effect=lambda root:order.append('verify')))
   engine.reconcile_attention({},self.at)
  self.assertEqual(order,['validation','cycle','verify'])
 def test_unknown_error_not_classified_as_busy(self):self.assertIsNone(r.incident_phase({'reason':'sqlite_database_busy','frames':[{'function':'claim_delivery'}]}))

if __name__=='__main__':unittest.main()
