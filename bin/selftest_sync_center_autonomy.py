
from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import sync_center_autonomy as a
import sync_repair_contract as c
class Contract(unittest.TestCase):
 def setUp(self):self.good=Path(__file__).with_name('sync_repair_rules.py').read_text()
 def test_all_contracts(self):self.assertTrue(c.check(self.good)['passed'])
 def test_new_fault_reproduced_and_fixed(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t).resolve();(root/'bin').mkdir()
   for name in a.CONTROL:(root/'bin'/name).write_text('fixed verifier fixture')
   bad=self.good.replace("record.get('memory_kind') == 'source_index'", "record.get('memory_kind') != 'source_index'")
   (root/'bin'/a.RULE).write_text(bad)
   request={'policy':a.POLICY,'probe_revision':'v1','binding':a.binding(root)}
   candidate={'policy':a.POLICY,'probe_revision':'v1','content':self.good}
   proof=a.validate_candidate(root,candidate,request)
   self.assertFalse(proof['old']['passed']);self.assertTrue(proof['new']['passed'])
   (root/'bin'/a.RULE).write_text(self.good)
   with self.assertRaises(ValueError):a.validate_candidate(root,candidate,request)
 def test_forbidden_import_mutation_recursion(self):
  for code in (self.good+'\nimport os',self.good.replace("return record.get", "record['source_ref']='changed'\n    return record.get"),self.good.replace("return record.get", "return email_source(record,source_ref) and record.get")):
   with self.assertRaises(ValueError):c.load(code)
 def test_cannot_self_set_tests_or_authority(self):
  with self.assertRaises(ValueError):c.load(self.good+'\nPOLICY="allow-all"')
 def test_correct_code_not_a_fault(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t).resolve();(root/'bin').mkdir()
   for name in a.CONTROL:(root/'bin'/name).write_text('fixed verifier')
   (root/'bin'/a.RULE).write_text(self.good)
   request={'probe_revision':'v1','binding':a.binding(root)}
   with self.assertRaisesRegex(ValueError,'reproduction'):a.validate_candidate(root,{'policy':a.POLICY,'probe_revision':'v1','content':self.good},request)
 def test_interrupted_publish_rolls_back_code_only(self):
  import hashlib
  with tempfile.TemporaryDirectory() as t:
   root=Path(t).resolve();(root/'bin').mkdir();job=root/'state/sync-autonomy/repair-x/check-1';job.mkdir(parents=True)
   old='old-code';new='new-code';(root/'bin'/a.RULE).write_text(new);(root/'business.txt').write_text('new-business-data')
   backup=job/'before.py';backup.write_text(old)
   receipt={'phase':'publishing','backup':str(backup),'proof':{'candidate_sha256':a.sha(new),'binding':{a.RULE:a.sha(old)}}}
   (job/'receipt.json').write_text(json.dumps(receipt));a.reconcile_interrupted(root)
   self.assertEqual((root/'bin'/a.RULE).read_text(),old);self.assertEqual((root/'business.txt').read_text(),'new-business-data')
   self.assertEqual(json.loads((job/'receipt.json').read_text())['phase'],'rolled_back')
 def test_lock_wait_does_not_remove_owner_lock(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t).resolve();(root/'state').mkdir()
   with a.control(root):
    with self.assertRaises(BlockingIOError):
     with a.control(root):pass
   self.assertTrue((root/'state/sync-center-control.lock').is_file())

if __name__=='__main__':unittest.main()
