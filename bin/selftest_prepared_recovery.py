
from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import copy,hashlib,io,json,sqlite3,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import dingtalk_prepared_recovery as r
import dingtalk_batch_contract as c
import business_page_integrity as i
from selftest_business_page_integrity import claim_block

class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  self.old='64903b1e-a936-5c85-8607-ed41464746c0';self.d=self.root/self.old;self.d.mkdir();(self.d/'backup').mkdir()
  self.before=i.ensure_claim_base('Original prose.\n').rstrip()+'\n\n'+claim_block('BUSINESS-20260914-001')+'\n'
  self.current=self.before+'\n'+claim_block('BUSINESS-20260914-002')+'\n'
  self.target=self.root/'page.md';self.target.write_text(self.current);self.backup=self.d/'backup/page.md';self.backup.write_text(self.before)
  self.db=self.root/'chat.sqlite'
  def connect(_):
   x=sqlite3.connect(self.db);x.row_factory=sqlite3.Row;return x
  h=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
  self.m=SimpleNamespace(runtime_paths=lambda cfg:{'runs':self.root,'memory_root':self.root,'status':self.root/'automation_status.json'},connect_database=connect,sha256_path=h,sha256_text=lambda s:hashlib.sha256(s.encode()).hexdigest(),validate_proposal=lambda cfg,b,p,**kw:p,batch_source_refs=lambda cfg,b:['source'],safe_target=lambda base,p:p.is_relative_to(base),acquire_lock=lambda *a,**k:io.StringIO(),AutomationError=type('AutomationError',(Exception,),{}),MEMORY_LOCK=self.root/'lock',now_iso=lambda:'2020-01-02T16:00:00+08:00',atomic_write=lambda p,s:Path(p).write_text(s))
  self.patch=patch.object(r,'memory',return_value=self.m);self.patch.start();self.addCleanup(self.patch.stop)
  scanner=patch.object(r.guard,'id_conflicts',return_value=[]);scanner.start();self.addCleanup(scanner.stop)
  with connect({}) as db:
   db.executescript('CREATE TABLE semantic_recovery_holds(batch_id TEXT PRIMARY KEY,reason_code TEXT,created_at TEXT); CREATE TABLE semantic_batches(batch_id TEXT PRIMARY KEY,created_at TEXT,status TEXT,error_code TEXT,message_count INTEGER,text_chars INTEGER,proposal_sha256 TEXT,input_manifest_sha256 TEXT); CREATE TABLE messages(message_id TEXT PRIMARY KEY,content_sha256 TEXT,current_visible INTEGER);CREATE TABLE semantic_queue(queue_id INTEGER PRIMARY KEY,message_id TEXT,content_sha256 TEXT,change_kind TEXT,state TEXT,batch_id TEXT,last_error_code TEXT);CREATE TABLE batch_messages(batch_id TEXT,queue_id INTEGER,ordinal INTEGER,source_token TEXT,PRIMARY KEY(batch_id,queue_id));')
   db.execute("INSERT INTO messages VALUES('msg','hash',1)");db.execute("INSERT INTO semantic_queue VALUES(1,'msg','hash','insert','failed',NULL,'proposal_commit_content_conflict')")
   db.execute('INSERT INTO batch_messages VALUES(?,1,1,?)',(self.old,'S001'))
   db.execute("INSERT INTO semantic_recovery_holds VALUES(?,'proposal_commit_content_conflict','2020-01-02')",(self.old,))
   self.proposal={'batch_id':self.old,'candidates':[{'action':'annotate_page'}],'source_summary':None,'claim_relations':[]}
   c.save(self.d/'proposal.json',self.proposal)
   db.execute("INSERT INTO semantic_batches VALUES(?,?,'failed','proposal_commit_content_conflict',1,7,?,NULL)",(self.old,'2020-01-02',h(self.d/'proposal.json')))
   mh=c.freeze(self.d,db,self.old,{'batch_id':self.old,'messages':[{'source_token':'S001'}]});db.execute('UPDATE semantic_batches SET input_manifest_sha256=?',(mh,))
  planned={'writes':{str(self.target):self.before+' planned'}};c.save(self.d/'planned-writes.json',planned)
  c.save(self.d/'commit-receipt.json',{'batch_id':self.old,'phase':'prepared','proposal_sha256':c.digest(self.proposal),'planned_writes_sha256':c.digest(planned),'before_hashes':{str(self.target):h(self.backup)},'file_hashes':{str(self.target):self.m.sha256_text(planned['writes'][str(self.target)])}})
  c.save(self.d/'apply_journal.json',{'batch_id':self.old,'state':'rolled_back','validation_status':'passed','source_refs':['source'],'paths':[{'target':str(self.target),'backup':str(self.backup),'before_sha256':h(self.backup),'existed':True}]})
 def change(self,name,update):
  p=self.d/name;d=json.loads(p.read_text());d.update(update);c.save(p,d)
 def reject(self):
  with self.assertRaises(Exception):r.inspect({},self.old)
  with self.m.connect_database({}) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM semantic_batches').fetchone()[0],1)
 def test_additive_pass_and_same_input_noop(self):
  self.assertTrue(r.prove_additive(self.before,self.current));self.assertFalse(r.prove_additive(self.before,self.before))
 def test_changed_prose_fails(self):
  self.target.write_text(self.current.replace('Original','Changed'));self.reject()
 def test_approved_changed_prose_still_fails(self):
  self.target.write_text(i.prepare_base_update(self.current,self.current.replace('Original','Changed'),allow_base_change=True));self.reject()
 def test_changed_old_claim_fails(self):
  self.target.write_text(self.current.replace('待核说明','changed',1));self.reject()
 def test_removed_old_claim_fails(self):
  self.target.write_text(i.ensure_claim_base('Original prose.\n').rstrip()+'\n\n'+claim_block('BUSINESS-20260914-002')+'\n');self.reject()
 def test_reordered_claims_fail(self):
  before=self.current;new=i.ensure_claim_base('Original prose.\n').rstrip()+'\n\n'+claim_block('BUSINESS-20260914-002')+'\n'+claim_block('BUSINESS-20260914-001')+'\n'+claim_block('BUSINESS-20260914-003')+'\n'
  with self.assertRaises(r.RecoveryBlocked):r.prove_additive(before,new)
 def test_backup_corrupt(self):self.backup.write_text('bad');self.reject()
 def test_source_drift(self):
  with self.m.connect_database({}) as db:db.execute("UPDATE messages SET content_sha256='new'")
  self.reject()
 def test_queue_owned_elsewhere(self):
  with self.m.connect_database({}) as db:db.execute("UPDATE semantic_queue SET batch_id='other'")
  self.reject()
 def test_committed_not_rebased(self):self.change('apply_journal.json',{'state':'committed'});self.reject()
 def test_rollback_unverified(self):self.change('apply_journal.json',{'validation_status':'pending'});self.reject()
 def test_plan_corrupt(self):self.change('planned-writes.json',{'writes':{}});self.reject()
 def test_second_generation_blocked(self):c.save(self.d/'prepared-recovery-origin.json',{});self.reject()
 def test_prepare_preserves_original_and_reuses_queue(self):
  original={p:p.read_bytes() for p in self.d.rglob('*') if p.is_file()};result=r.prepare({},self.old)
  self.assertFalse(result['model_called']);self.assertEqual(self.target.read_text(),self.current)
  for p,b in original.items():self.assertEqual(p.read_bytes(),b)
  with self.m.connect_database({}) as db:
   self.assertEqual(db.execute('SELECT COUNT(*) FROM semantic_queue').fetchone()[0],1)
   q=db.execute('SELECT * FROM semantic_queue').fetchone();self.assertEqual(q['batch_id'],result['child_batch_id']);self.assertEqual(q['state'],'claimed')
   self.assertEqual(db.execute('SELECT status FROM semantic_batches WHERE batch_id=?',(self.old,)).fetchone()[0],'failed')
   self.assertIsNotNone(c.load(self.root/result['child_batch_id'],db,result['child_batch_id']))
  with self.assertRaises(r.RecoveryBlocked):r.prepare({},self.old)
 def test_file_failure_rolls_back_and_retry_is_safe(self):
  with patch.object(self.m,'atomic_write',side_effect=OSError('crash')):
   with self.assertRaises(OSError):r.prepare({},self.old)
  with self.m.connect_database({}) as db:
   self.assertEqual(db.execute('SELECT COUNT(*) FROM semantic_batches').fetchone()[0],1);self.assertIsNone(db.execute('SELECT batch_id FROM semantic_queue').fetchone()[0])
  self.assertEqual(r.prepare({},self.old)['status'],'prepared')
 def test_target_changed_before_transaction(self):
  real=r.inspect
  def changed(*args):
   result=real(*args);self.target.write_text(self.current+'new');return result
  with patch.object(r,'inspect',side_effect=changed):
   with self.assertRaises(r.RecoveryBlocked):r.prepare({},self.old)
 def test_pending_prepares_once_then_reports_existing_child(self):
  first=r.prepare_pending({});self.assertEqual(first['batches'][self.old]['status'],'child_pending')
  second=r.prepare_pending({});self.assertEqual(second['batches'][self.old]['status'],'child_pending')
  with self.m.connect_database({}) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM semantic_batches').fetchone()[0],2)
 def test_blocked_evidence_never_calls_prepare(self):
  self.target.write_text(self.current.replace('Original','Changed'))
  with patch.object(r,'prepare') as prepare:
   result=r.prepare_pending({});r.prepare_pending({});prepare.assert_not_called()
  self.assertEqual(result['batches'][self.old]['status'],'blocked')
  self.assertEqual(r.pending_summary({})['pending_batches'],1)
 def test_technical_failure_is_held_across_invocations(self):
  with patch.object(r,'prepare',side_effect=OSError('private detail')) as prepare:
   result=r.prepare_pending({});r.prepare_pending({});self.assertEqual(prepare.call_count,1)
  self.assertEqual(result['batches'][self.old]['status'],'technical_failure')
  self.assertNotIn('private detail',json.dumps(result))
 def test_crash_intent_is_not_retried(self):
  c.save(self.root/'prepared-recovery-status.json',{'schema_version':1,'batches':{self.old:{'status':'preparing'}}})
  with patch.object(r,'prepare') as prepare:r.prepare_pending({});prepare.assert_not_called()
 def complete_child(self):
  result=r.prepare({},self.old);child=result['child_batch_id'];d=self.root/child
  with self.m.connect_database({}) as db:
   db.execute("UPDATE semantic_batches SET status='applied' WHERE batch_id=?",(child,))
   db.execute("UPDATE semantic_queue SET state='applied' WHERE batch_id=?",(child,))
  proposal=json.loads((d/'proposal.json').read_text())
  planned={'batch_id':child,'writes':{str(self.target):self.current+' added'}}
  c.save(d/'planned-writes.json',planned)
  h=self.m.sha256_text(planned['writes'][str(self.target)])
  c.save(d/'apply_journal.json',{'batch_id':child,'state':'committed','source_refs':['source'],'paths':[{'target':str(self.target),'expected_applied_sha256':h,'applied_sha256':h}]})
  c.save(d/'commit-receipt.json',{'batch_id':child,'phase':'complete','proposal_sha256':c.digest(proposal),'planned_writes_sha256':c.digest(planned),'file_hashes':{str(self.target):h},'before_hashes':{str(self.target):'before'},'write_count':1,'result':{'validated':True},'verification':{'validator':{'verdict':'pass'}}})
  return child,d
 def test_recovered_requires_complete_receipt_and_exact_queue(self):
  child,d=self.complete_child();self.assertEqual(r.linked_recovery({},self.old)['status'],'recovered')
  with self.m.connect_database({}) as db:db.execute("UPDATE semantic_queue SET batch_id='other'")
  with self.assertRaises(r.RecoveryBlocked):r.linked_recovery({},self.old)
 def test_applied_without_completion_is_not_recovery(self):
  child,d=self.complete_child();commit=json.loads((d/'commit-receipt.json').read_text());commit['phase']='prepared';c.save(d/'commit-receipt.json',commit)
  with self.assertRaises(r.RecoveryBlocked):r.linked_recovery({},self.old)
 def test_recovered_parent_preserves_original_evidence(self):
  child,d=self.complete_child();before={p:p.read_bytes() for p in self.d.rglob('*') if p.is_file()}
  self.assertEqual(r.prepare_pending({})['batches'][self.old]['status'],'recovered')
  for p,b in before.items():self.assertEqual(p.read_bytes(),b)
  self.assertEqual(r.pending_summary({})['pending_batches'],0)
 def test_conflicted_original_excluded_from_both_selectors(self):
  import dingtalk_memory_automation as real
  with patch.object(real,'runtime_paths',self.m.runtime_paths),patch.object(real,'connect_database',self.m.connect_database),patch.object(r,'prepare_pending',return_value={}):
   self.assertIsNone(real.claim_reusable_proposal({}))
   self.assertEqual(real.eligible_queue_count({}),0)
  with self.m.connect_database({}) as db:
   db.executescript("ALTER TABLE semantic_queue ADD COLUMN queued_at TEXT; ALTER TABLE messages ADD COLUMN conversation_id TEXT; ALTER TABLE messages ADD COLUMN create_time TEXT; ALTER TABLE messages ADD COLUMN sender_name TEXT; ALTER TABLE messages ADD COLUMN text_redacted TEXT; CREATE TABLE conversations(conversation_id TEXT,conversation_name TEXT); CREATE TABLE recovery_resolutions(old_batch_id TEXT,state TEXT);")
   db.execute("INSERT INTO conversations VALUES('c','Test')");db.execute("UPDATE messages SET conversation_id='c',text_redacted='text',create_time='2020-01-02',sender_name='Test'");db.execute("UPDATE semantic_queue SET queued_at='2020-01-02'")
   self.assertEqual(real.select_batch_rows(db,10,100)[0],[])
   self.assertEqual(db.execute('SELECT state FROM semantic_queue').fetchone()[0],'failed')
 def test_valid_child_is_reusable_without_new_model_batch(self):
  child=r.prepare({},self.old)['child_batch_id']
  import dingtalk_memory_automation as real
  with self.m.connect_database({}) as db:
   db.execute('ALTER TABLE semantic_queue ADD COLUMN attempt_count INTEGER DEFAULT 0');db.execute('ALTER TABLE semantic_queue ADD COLUMN last_attempt_at TEXT');db.execute('ALTER TABLE messages ADD COLUMN create_time TEXT');db.execute('ALTER TABLE semantic_batches ADD COLUMN error_detail TEXT')
  with patch.object(real,'runtime_paths',self.m.runtime_paths),patch.object(real,'connect_database',self.m.connect_database),patch.object(real,'validate_proposal',self.m.validate_proposal),patch.object(r,'prepare_pending',return_value={}):
   value=real.claim_reusable_proposal({});self.assertEqual(value[0]['batch_id'],child)
 def test_changed_error_cannot_release_original_hold(self):
  import dingtalk_memory_automation as real
  with self.m.connect_database({}) as db:db.execute("UPDATE semantic_batches SET error_code='technical_error'")
  with patch.object(real,'runtime_paths',self.m.runtime_paths),patch.object(real,'connect_database',self.m.connect_database),patch.object(r,'prepare_pending',return_value={}):
   self.assertIsNone(real.claim_reusable_proposal({}));self.assertEqual(real.eligible_queue_count({}),0)
   with self.assertRaises(real.AutomationError) as ctx:real.assert_batch_execution({},self.old)
   self.assertEqual(ctx.exception.code,'prepared_recovery_original_held')
 def test_late_parent_cannot_submit_after_child_transfer(self):
  child=r.prepare({},self.old)['child_batch_id']
  import dingtalk_memory_automation as real
  with patch.object(real,'connect_database',self.m.connect_database):
   with self.assertRaises(real.AutomationError):real.assert_batch_execution({},self.old)
   real.assert_batch_execution({},child)
   with self.m.connect_database({}) as db:db.execute("UPDATE semantic_queue SET batch_id='other'")
   with self.assertRaises(real.AutomationError):real.assert_batch_execution({},child)
 def test_completion_plan_tamper_rejected(self):
  child,d=self.complete_child();c.save(d/'planned-writes.json',{'batch_id':child,'writes':{}})
  with self.assertRaises(r.RecoveryBlocked):r.linked_recovery({},self.old)
 def test_missing_file_transaction_cannot_pass(self):
  child,d=self.complete_child();c.save(d/'apply_journal.json',{'batch_id':child,'state':'committed','source_refs':['source'],'paths':[]})
  with self.assertRaises(r.RecoveryBlocked):r.linked_recovery({},self.old)
 def test_duplicate_queue_same_source_version_remains_held(self):
  import dingtalk_memory_automation as real
  with self.m.connect_database({}) as db:db.execute("INSERT INTO semantic_queue VALUES(2,'msg','hash','insert','pending',NULL,NULL)")
  with patch.object(real,'connect_database',self.m.connect_database):self.assertEqual(real.eligible_queue_count({}),0)
if __name__=='__main__':unittest.main()
