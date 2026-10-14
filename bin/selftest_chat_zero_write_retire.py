
from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import json,unittest
from unittest.mock import patch
import dingtalk_memory_automation as m
import dingtalk_batch_contract as c
from selftest_remaining_sync_repair import ChatFixture
from selftest_dingtalk_memory_automation import insert_message
class Retire(ChatFixture):
 def fixture(self,writes=0,relation='noop',relations=(),error='ContractError'):
  for i,mid in enumerate('abc'):insert_message(self.config,mid,'conv','原始消息 '+mid,f'2020-01-02 23:3{i}:00')
  batch=m.prepare_batch(self.config);old=batch['batch_id'];d=self.paths['runs']/old
  c.save(d/'proposal.json',{'batch_id':old,'source_summary':None,'candidates':[],'claim_relations':list(relations),'no_business_change':True})
  planned={'schema_version':1,'batch_id':old,'writes':{}};c.save(d/'planned-writes.json',planned)
  c.save(d/'commit-receipt.json',{'schema_version':1,'batch_id':old,'phase':'validation_complete','write_count':writes,
         'file_hashes':{},'planned_writes_sha256':c.digest(planned),
         'files_result':{'validated':False,'writes':writes},'relation_receipt':{'status':relation,'recorded':0}})
  with m.connect_database(self.config) as db:
   # The 10-01 shape: the final source check failed after validation; release_batch_failure froze it.
   db.execute("UPDATE semantic_batches SET status='failed',proposal_sha256=?,error_code=? WHERE batch_id=?",(m.sha256_path(d/'proposal.json'),error,old))
   db.execute("UPDATE semantic_queue SET state='failed',batch_id=NULL,last_error_code='ContractError' WHERE batch_id=?",(old,))
  return old,d
 def supersede_a(self):
  # The collector's own convention retires the failed member when its message gets a new version.
  insert_message(self.config,'a','conv','图片元数据变化后的新版本','2020-01-02 23:30:00')
 def state(self,db,old):
  return {r[0]:(r[1],r[2]) for r in db.execute("SELECT m.message_id,q.state,q.last_error_code FROM batch_messages b JOIN semantic_queue q USING(queue_id) JOIN messages m USING(message_id) WHERE b.batch_id=?",(old,))}
 def test_superseded_member_releases_the_rest_without_reusing_old_proposal(self):
  old,d=self.fixture();self.supersede_a();before={n:(d/n).read_bytes() for n in ('proposal.json','input-manifest.json','commit-receipt.json')}
  with m.connect_database(self.config) as db:
   rows,_=m.select_batch_rows(db,100,10000)
   self.assertEqual(len(rows),1);self.assertEqual(m.eligible_queue_count(self.config),3)  # the stuck shape: counted, never selected
   attempts=sorted(r[0] for r in db.execute('SELECT attempt_count FROM semantic_queue'))
   self.assertTrue(m.retire_unreusable_zero_write_batch(self.config,db,old));self.assertFalse(m.retire_unreusable_zero_write_batch(self.config,db,old))
   self.assertEqual(self.state(db,old),{'a':('no_business_change','superseded_before_processing'),'b':('pending','source_refresh_required'),'c':('pending','source_refresh_required')})
   self.assertEqual(db.execute('SELECT status FROM semantic_batches WHERE batch_id=?',(old,)).fetchone()[0],'rolled_back')
   self.assertEqual(attempts,sorted(r[0] for r in db.execute('SELECT attempt_count FROM semantic_queue')))
   rows,_=m.select_batch_rows(db,100,10000);self.assertEqual(len(rows),3)
  self.assertEqual(m.eligible_queue_count(self.config),3)
  for n,data in before.items():self.assertEqual((d/n).read_bytes(),data)
  receipt=json.loads((d/'source-refresh.json').read_text())
  self.assertEqual((receipt['phase'],receipt['business_writes'],receipt['already_settled'],receipt['unchanged_sources_requeued']),('requeued',0,1,2))
 def test_claim_loop_retires_instead_of_skipping(self):
  old,d=self.fixture();self.supersede_a()
  self.assertIsNone(m.claim_reusable_proposal(self.config,recover=False))
  with m.connect_database(self.config) as db:self.assertEqual(db.execute('SELECT status FROM semantic_batches WHERE batch_id=?',(old,)).fetchone()[0],'rolled_back')
 def test_flapped_back_source_production_shape(self):
  # 10-01 23:50: image metadata changed and changed back; bindings match again, but a member is settled.
  old,d=self.fixture();self.supersede_a();insert_message(self.config,'a','conv','原始消息 a','2020-01-02 23:30:00')
  self.assertIsNone(m.claim_reusable_proposal(self.config,recover=False))
  with m.connect_database(self.config) as db:
   self.assertEqual(db.execute('SELECT status FROM semantic_batches WHERE batch_id=?',(old,)).fetchone()[0],'rolled_back')
   self.assertEqual({k:v[0] for k,v in self.state(db,old).items()},{'a':'no_business_change','b':'pending','c':'pending'})
   rows,_=m.select_batch_rows(db,100,10000);self.assertEqual(len(rows),3)
 def test_reusable_batch_is_left_for_reuse(self):
  old,d=self.fixture()
  with m.connect_database(self.config) as db:
   self.assertFalse(m.retire_unreusable_zero_write_batch(self.config,db,old))
   self.assertEqual({v[0] for v in self.state(db,old).values()},{'failed'})
 def test_business_writes_block(self):
  old,d=self.fixture(writes=1);self.supersede_a()
  with m.connect_database(self.config) as db:
   self.assertFalse(m.retire_unreusable_zero_write_batch(self.config,db,old))
   self.assertEqual(db.execute('SELECT status FROM semantic_batches WHERE batch_id=?',(old,)).fetchone()[0],'failed')
 def test_registered_relations_block(self):
  old,d=self.fixture(relation='applied');self.supersede_a()
  with m.connect_database(self.config) as db:self.assertFalse(m.retire_unreusable_zero_write_batch(self.config,db,old))
 def test_file_transaction_belongs_to_rollback_recovery(self):
  old,d=self.fixture();self.supersede_a();c.save(d/'apply_journal.json',{'batch_id':old,'state':'committed'})
  with m.connect_database(self.config) as db:self.assertFalse(m.retire_unreusable_zero_write_batch(self.config,db,old))
 def test_in_flight_member_blocks(self):
  old,d=self.fixture();self.supersede_a()
  with m.connect_database(self.config) as db:
   db.execute("UPDATE semantic_queue SET state='claimed',batch_id=? WHERE message_id='b' AND state='failed'",(old,));db.commit()
   self.assertFalse(m.retire_unreusable_zero_write_batch(self.config,db,old))
 def test_proposed_relations_block_once_files_phase_may_have_run(self):
  old,d=self.fixture(relations=[{'claim_handle':'h1','relation':'supports','source_tokens':['s1'],'reason':'r'}]);self.supersede_a()
  with m.connect_database(self.config) as db:self.assertFalse(m.retire_unreusable_zero_write_batch(self.config,db,old))
 def test_unresolved_conflict_stays_with_conflict_recovery(self):
  old,d=self.fixture(error='recovery_conflict_requires_review');self.supersede_a()
  with m.connect_database(self.config) as db:
   self.assertFalse(m.retire_unreusable_zero_write_batch(self.config,db,old))
   self.assertEqual(db.execute('SELECT status FROM semantic_batches WHERE batch_id=?',(old,)).fetchone()[0],'failed')
 def test_planned_writes_mismatch_raises(self):
  old,d=self.fixture();self.supersede_a();c.save(d/'planned-writes.json',{'schema_version':1,'batch_id':old,'writes':{'/x.md':'y'}})
  with m.connect_database(self.config) as db:
   with self.assertRaises(m.AutomationError) as raised:m.retire_unreusable_zero_write_batch(self.config,db,old)
   self.assertEqual(raised.exception.code,'proposal_recovery_evidence_invalid')
 def test_journal_created_while_waiting_for_lock_blocks(self):
  old,d=self.fixture();self.supersede_a();real=m.acquire_lock
  def racing(path,blocking=False):
   c.save(d/'apply_journal.json',{'batch_id':old,'state':'committed'});return real(path,blocking)
  with m.connect_database(self.config) as db,patch.object(m,'acquire_lock',side_effect=racing):
   self.assertFalse(m.retire_unreusable_zero_write_batch(self.config,db,old))
 def test_drift_after_retire_check_is_retired_not_fatal(self):
  # Reusable at the retire check; the collector supersedes a member before validation reads the manifest.
  old,d=self.fixture();real=m.validate_proposal
  def drifting(*args,**kwargs):
   self.supersede_a();return real(*args,**kwargs)
  with patch.object(m,'validate_proposal',side_effect=drifting):
   self.assertIsNone(m.claim_reusable_proposal(self.config,recover=False))
  with m.connect_database(self.config) as db:self.assertEqual(db.execute('SELECT status FROM semantic_batches WHERE batch_id=?',(old,)).fetchone()[0],'rolled_back')
 def test_changed_proposal_evidence_raises(self):
  old,d=self.fixture();self.supersede_a();c.save(d/'proposal.json',{'tampered':True})
  with m.connect_database(self.config) as db:
   with self.assertRaises(m.AutomationError) as raised:m.retire_unreusable_zero_write_batch(self.config,db,old)
   self.assertEqual(raised.exception.code,'proposal_recovery_evidence_invalid')
   self.assertEqual(db.execute('SELECT status FROM semantic_batches WHERE batch_id=?',(old,)).fetchone()[0],'failed')
if __name__=='__main__':unittest.main()
