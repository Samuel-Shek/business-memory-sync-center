
from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import json,unittest
from unittest.mock import patch
import dingtalk_memory_automation as m
import dingtalk_batch_contract as c
from selftest_remaining_sync_repair import ChatFixture
from selftest_dingtalk_memory_automation import insert_message
class Refresh(ChatFixture):
 def fixture(self):
  insert_message(self.config,'a','conv','first','2020-01-02 23:00:00');insert_message(self.config,'b','conv','second','2020-01-02 23:00:01')
  batch=m.prepare_batch(self.config);d=self.paths['runs']/batch['batch_id'];c.save(d/'proposal.json',{'old':'never reused'})
  target=self.paths['memory_root']/'04_对话来源摘要/new.md'
  c.save(d/'apply_journal.json',{'batch_id':batch['batch_id'],'state':'rolled_back','validation_status':'passed','paths':[{'target':str(target),'before_sha256':None}]})
  with m.connect_database(self.config) as db:
   db.execute("UPDATE semantic_batches SET status='failed',proposal_sha256=?,error_code='rollback_validation_context_unavailable' WHERE batch_id=?",(m.sha256_path(d/'proposal.json'),batch['batch_id']))
   db.execute("UPDATE semantic_queue SET state='failed',batch_id=NULL")
   db.execute("UPDATE messages SET content_sha256='new-source' WHERE message_id='a'")
   db.execute("INSERT INTO semantic_queue(message_id,content_sha256,change_kind,queued_at,state) VALUES('a','new-source','update',?,'pending')",(m.now_iso(),))
  return batch['batch_id'],d,target
 def test_source_change_requeues_without_reusing_old_model_or_resetting_attempts(self):
  old,d,target=self.fixture();before={n:(d/n).read_bytes() for n in ('proposal.json','input-manifest.json','apply_journal.json')}
  with m.connect_database(self.config) as db:
   attempts=[r[0] for r in db.execute('SELECT attempt_count FROM semantic_queue')]
   self.assertTrue(m.refresh_rolled_back_sources(self.config,db,old));self.assertFalse(m.refresh_rolled_back_sources(self.config,db,old))
   self.assertEqual(attempts,[r[0] for r in db.execute('SELECT attempt_count FROM semantic_queue')])
   rows,_=m.select_batch_rows(db,100,10000);self.assertEqual(len(rows),2);self.assertEqual({r['content_sha256'] for r in rows},{'new-source',db.execute("SELECT content_sha256 FROM messages WHERE message_id='b'").fetchone()[0]})
  for n,data in before.items():self.assertEqual((d/n).read_bytes(),data)
  self.assertFalse(target.exists());self.assertEqual(json.loads((d/'source-refresh.json').read_text())['business_writes'],0)
 def test_later_business_change_blocks(self):
  old,d,target=self.fixture();target.write_text('later business edit')
  with m.connect_database(self.config) as db:self.assertFalse(m.refresh_rolled_back_sources(self.config,db,old))
  self.assertEqual(target.read_text(),'later business edit')
 def test_unverified_rollback_blocks(self):
  old,d,target=self.fixture();p=d/'apply_journal.json';v=json.loads(p.read_text());v['validation_status']='pending';c.save(p,v)
  with m.connect_database(self.config) as db:self.assertFalse(m.refresh_rolled_back_sources(self.config,db,old))
 def test_missing_current_version_blocks(self):
  old,d,target=self.fixture()
  with m.connect_database(self.config) as db:
   db.execute("DELETE FROM semantic_queue WHERE content_sha256='new-source'");db.commit();self.assertFalse(m.refresh_rolled_back_sources(self.config,db,old))
if __name__=='__main__':unittest.main()
