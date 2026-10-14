
from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import unittest,tempfile,json,shutil,sqlite3,contextlib
from pathlib import Path
from unittest.mock import patch
import workbench_read_recovery as recovery,ledger_recovery as ledger,model_call_ledger as calls
class ReadRecovery(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.db=self.root/'sync.sqlite'
  (self.root/'bin').mkdir();(self.root/'state/sync-autonomy').mkdir(parents=True)
  for n in recovery.NAMES:shutil.copy2(Path(recovery.__file__).parent/n,self.root/'bin'/n)
  self.fault={'recorded_at':'2020-01-02T19:55:29+08:00','sqlite':{'sqlite_code':14,'operation':'workbench_items','phase':'execute','outcome':'failed','commit_outcome_ambiguous':False},'frames':[{'file':'bin/model_call_ledger.py','function':'report'}]}
  ledger.atomic(self.root/'state/sync-supervisor-last-error.json',self.fault)
  ledger.atomic(ledger.path_for(self.db,'workbench_items'),{'status':'blocked','last_failure_at':self.fault['recorded_at'],'attempts':15})
  log=self.root/'selftest_workbench_read_recovery.log';log.write_text('synthetic fixture test evidence')
  self.release={'tests_passed':True,'code_binding':{n:recovery.sha(self.root/'bin'/n) for n in recovery.NAMES},'tests':[{'path':str(log),'sha256':recovery.sha(log)}]}
  ledger.atomic(self.root/'state/sync-autonomy/release.json',self.release)
 def tearDown(self):self.tmp.cleanup()
 def test_exact_read_fault_is_allowed_for_existing_verifier(self):self.assertEqual(ledger.incident_phase(self.fault),'workbench_items')
 def test_other_cantopen_or_unknown_commit_remains_blocked(self):
  for changes in ({'commit_outcome_ambiguous':True},{'phase':'commit'},{'operation':'business_write'},{'outcome':'unknown'}):
   fault={**self.fault,'sqlite':{**self.fault['sqlite'],**changes}};self.assertIsNone(ledger.incident_phase(fault))
  self.assertIsNone(ledger.incident_phase({**self.fault,'frames':[]}))
 def test_current_release_proof_required(self):
  self.assertIsNotNone(recovery.evidence(self.root,self.db));(self.root/'bin/model_call_ledger.py').write_text('changed')
  self.assertIsNone(recovery.evidence(self.root,self.db))
 def test_missing_specific_tests_denies_grant(self):
  self.release['tests']=[];ledger.atomic(self.root/'state/sync-autonomy/release.json',self.release)
  self.assertIsNone(recovery.evidence(self.root,self.db))
 def test_no_manual_bypass(self):
  with patch.object(ledger,'native',return_value=False),patch.object(ledger,'run') as run:
   with self.assertRaises(ledger.Deferred):recovery.retry_projection(self.root,self.db,lambda c:None)
  run.assert_not_called()
 def test_native_success_preserves_budget_and_uses_one_grant(self):
  with patch.object(ledger,'native',return_value=True),patch.object(calls,'report',return_value={'operations':[]}),patch.object(ledger,'run',return_value='done') as run:
   self.assertEqual(recovery.retry_projection(self.root,self.db,lambda c:None),'done')
   with self.assertRaises(ledger.Deferred):recovery.retry_projection(self.root,self.db,lambda c:None)
  self.assertEqual(run.call_count,1);self.assertEqual(ledger.read(ledger.path_for(self.db,'workbench_items'))['attempts'],15)
  receipt=next((self.root/'state/workbench-read-recovery').glob('*.json'));row=json.loads(receipt.read_text());self.assertEqual(row['state'],'projection_reconciled');self.assertFalse(row['budget_reset'])
 def test_failed_attempt_is_not_replayed(self):
  with patch.object(ledger,'native',return_value=True),patch.object(calls,'report',return_value={}),patch.object(ledger,'run',side_effect=sqlite3.OperationalError('fixture failure')) as run:
   with self.assertRaises(sqlite3.OperationalError):recovery.retry_projection(self.root,self.db,lambda c:None)
   with self.assertRaises(ledger.Deferred):recovery.retry_projection(self.root,self.db,lambda c:None)
  self.assertEqual(run.call_count,1)
 def test_lock_wait_does_not_consume_grant(self):
  @contextlib.contextmanager
  def busy(*args,**kwargs):yield False
  with patch.object(ledger,'native',return_value=True),patch.object(ledger,'locked',busy):
   with self.assertRaises(ledger.Deferred):recovery.retry_projection(self.root,self.db,lambda c:None)
  self.assertEqual(list((self.root/'state/workbench-read-recovery').glob('*.json')),[])
 def test_missing_database_is_not_created(self):
  root=self.root/'missing';self.assertEqual(calls.report(root)['operations'],[]);self.assertFalse(root.exists())
 def test_readonly_wal_report_has_no_sql_write_capability(self):
  root=self.root/'calls';db=calls.connect(root);db.close();real=sqlite3.connect;seen=[]
  def connect(*args,**kwargs):
   conn=real(*args,**kwargs);seen.append((args,conn));return conn
  with patch.object(calls.sqlite3,'connect',side_effect=connect):result=calls.report(root)
  self.assertEqual(result['operations'],[]);conn=seen[0][1]
  self.assertEqual(conn.execute('pragma query_only').fetchone()[0],1)
  with self.assertRaises(sqlite3.OperationalError):conn.execute('create table forbidden(id)')
  self.assertIn('mode=rw',seen[0][0][0]);conn.close()
if __name__=='__main__':unittest.main()
