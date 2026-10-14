
from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import unittest,tempfile,json,sqlite3,os
from pathlib import Path
from unittest.mock import patch
import action_run_owner as own
import dingtalk_sync_v2 as sync

class Owners(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);(self.root/'state').mkdir();(self.root/'config.json').write_text('{}')
  self.db=self.root/'db.sqlite';c=sqlite3.connect(self.db)
  c.executescript('CREATE TABLE action_runs(run_id TEXT PRIMARY KEY,action_type TEXT,status TEXT,result_json TEXT,error_code TEXT,started_at TEXT,completed_at TEXT,updated_at TEXT); CREATE TABLE action_steps(run_id TEXT,status TEXT,error_code TEXT,completed_at TEXT,updated_at TEXT);');c.close()
  def con():
   c=sqlite3.connect(self.db);c.row_factory=sqlite3.Row;return c
  self.patches=[patch.object(sync,'connect',con),patch.object(sync,'connect_readonly',con),patch.object(own,'worker_present',return_value=False),patch.object(sync,'SOURCE_LOCK',self.root/'source.lock'),patch.object(sync,'SEMANTIC_LOCK',self.root/'semantic.lock')]
  for p in self.patches:p.start()
  self.id='64903b1e-a936-5c85-8607-ed41464746c0';c=con();c.execute('insert into action_runs values(?,?,?,?,?,?,?,?)',(self.id,'dingtalk_minutes','running',None,None,'2000-01-01T00:00:00+00:00',None,'2000-01-01T00:00:00+00:00'));c.execute("insert into action_steps values(?,'running',NULL,NULL,NULL)",(self.id,));c.commit();c.close()
 def tearDown(self):
  for p in reversed(self.patches):p.stop()
  self.temp.cleanup()
 def status(self):
  c=sqlite3.connect(self.db);v=c.execute('select status from action_runs').fetchone()[0];c.close();return v
 def owner(self):
  own.write(own.owner_path(self.root,self.id),{'run_id':self.id,'target':'dingtalk_minutes','pid':os.getpid(),'process_identity':'x'})
 def test_legacy_unknown_preserved(self):
  self.assertEqual(own.recover(self.root)['recovered'],0);self.assertEqual(self.status(),'running')
 def test_legacy_idle_interrupts_not_success(self):
  own.write(own.owner_path(self.root,self.id),{'run_id':self.id,'target':'dingtalk_minutes','state':'legacy_boundary_verified','original_updated_at':'2000-01-01T00:00:00+00:00','migration_receipt':'synthetic'})
  self.assertEqual(own.recover(self.root)['recovered'],1);self.assertEqual(self.status(),'interrupted')
  self.assertEqual(own.recover(self.root)['recovered'],0)
 def test_live_worker_preserved(self):
  with patch.object(own,'worker_present',return_value=True):self.assertEqual(own.recover(self.root)['recovered'],0)
  self.assertEqual(self.status(),'running')
 def test_unknown_or_live_owner_preserved(self):
  self.owner()
  for state in ['alive','unknown']:
   with patch.object(own,'process_state',return_value=state):self.assertEqual(own.recover(self.root)['recovered'],0)
 def test_pid_reuse_is_not_same_owner(self):
  self.owner()
  with patch.object(own,'process_state',return_value='replaced'):self.assertEqual(own.recover(self.root)['recovered'],1)
 def test_active_control_lock_preserved(self):
  import fcntl
  with (self.root/'state/sync-center-control.lock').open('a+') as f:
   fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);self.assertEqual(own.recover(self.root)['status'],'busy')
 def test_appearance_between_scans_preserved(self):
  with patch.object(own,'worker_present',side_effect=[False,True]):self.assertEqual(own.recover(self.root)['recovered'],0)
 def test_stale_recovery_cannot_overwrite_completed_result(self):
  sync.finish_action_run(self.id,'success',result={'real':True});sync.finish_action_run(self.id,'interrupted',expected_status='running');self.assertEqual(self.status(),'success')
 def test_lease_records_birth_and_exit(self):
  with patch.object(own.life,'process_identity',return_value='pid:birth'),patch.object(own.life,'clock_sample',return_value={'boot_id':'boot'}):
   with own.lease(self.root,self.id,'dingtalk_minutes'):
    self.assertEqual(json.loads(own.owner_path(self.root,self.id).read_text())['state'],'running')
  self.assertEqual(json.loads(own.owner_path(self.root,self.id).read_text())['state'],'executor_exited')
if __name__=='__main__':unittest.main()
