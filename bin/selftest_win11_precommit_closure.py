"""Synthetic closure requests; never uses production data or providers."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import fcntl
import hashlib
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
import win11_precommit_closure as closure


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); (self.root / 'state').mkdir()
        self.run = 'win11-20261001T121327-a01c316b'
        self.runs = self.root / 'external/win11-semantic-merge/runs'
        self.batch = self.runs / self.run / 'batch.json'; self.batch.parent.mkdir(parents=True)
        self.batch.write_text('{}'); self.sha = hashlib.sha256(self.batch.read_bytes()).hexdigest()
        self.db = self.root / 'semantic.sqlite'
        with sqlite3.connect(self.db) as c:
            c.executescript('CREATE TABLE runs(run_id TEXT,status TEXT,batch_sha256 TEXT,transaction_id TEXT,writes_count INTEGER); CREATE TABLE applied_transactions(run_id TEXT);')
            c.execute('INSERT INTO runs VALUES(?,?,?,?,?)', (self.run,'prepared',self.sha,None,0))
        (self.root / 'config.json').write_text(json.dumps({'external_root':str(self.root/'external'),'semantic_database':str(self.db)}))
        (self.root / 'state/maintenance-mode.json').write_text('{"status":"inactive"}')
        closure.request(self.root,self.run,self.sha,'merge_memory_lock_timeout')
        self.path = self.root / 'state/win11-precommit-closures' / (self.run+'.json')
        self.calls = []
    def runner(self, command, **kw):
        self.calls.append((command,kw))
        with sqlite3.connect(self.db) as c:c.execute("UPDATE runs SET status='failed'")
        return subprocess.CompletedProcess(command,0,json.dumps({'status':'failed','run_id':self.run}), '')
    def drain(self):return closure.drain(self.root,runner=self.runner)
    def test_closes_once_retains_source_candidate_and_request(self):
        candidate=self.batch.parent/'ai_raw.json';candidate.write_text('candidate');before=self.batch.read_bytes()
        self.assertEqual(self.drain()['closed'],1);self.assertEqual(self.drain()['status'],'idle')
        self.assertEqual(len(self.calls),1);self.assertEqual(candidate.read_text(),'candidate');self.assertEqual(self.batch.read_bytes(),before)
        self.assertEqual(json.loads(self.path.read_text())['status'],'closed')
        self.assertEqual(self.calls[0][0][2],'fail');self.assertEqual(self.calls[0][1]['env']['BUSINESS_MEMORY_LOCK_TIMEOUT_SECONDS'],'1')
    def test_writer_lock_does_not_call_or_change_request(self):
        before=self.path.read_bytes()
        for name in ['memory-index.lock','win11_memory_merge.lock']:
            with (self.root/'state'/name).open('a+') as f:
                fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);self.assertEqual(self.drain()['status'],'deferred')
            self.assertEqual(self.path.read_bytes(),before)
        self.assertFalse(self.calls)
    def test_committed_or_written_or_journalled_run_is_blocked(self):
        for column,value in [('status','completed'),('transaction_id','txn'),('writes_count',1)]:
            with sqlite3.connect(self.db) as c:c.execute('UPDATE runs SET '+column+'=?',(value,))
            self.assertEqual(self.drain()['error_code'],'win11_closure_commit_evidence')
            with sqlite3.connect(self.db) as c:c.execute("UPDATE runs SET status='prepared',transaction_id=NULL,writes_count=0")
        with sqlite3.connect(self.db) as c:c.execute('INSERT INTO applied_transactions VALUES(?)',(self.run,))
        self.assertEqual(self.drain()['status'],'blocked')
        with sqlite3.connect(self.db) as c:c.execute('DELETE FROM applied_transactions')
        (self.batch.parent/'transaction.json').write_text('{}');self.assertEqual(self.drain()['status'],'blocked')
        self.assertFalse(self.calls)
    def test_changed_batch_and_malformed_request_fail_closed(self):
        self.batch.write_text('changed');self.assertEqual(self.drain()['error_code'],'win11_closure_binding_changed')
        self.batch.write_text('{}');self.path.write_text('{"status":"pending"}');self.assertEqual(self.drain()['error_code'],'win11_closure_state_invalid')
        self.assertFalse(self.calls)
    def test_lock_race_retains_pending(self):
        before=self.path.read_bytes()
        result=closure.drain(self.root,runner=lambda *a,**k:subprocess.CompletedProcess(a,2,'{"code":"memory_lock_timeout"}',''))
        self.assertEqual(result['status'],'deferred');self.assertEqual(self.path.read_bytes(),before)
    def test_maintenance_and_changed_request_binding_preserve_pending(self):
        (self.root/'state/maintenance-mode.json').write_text('{"status":"active"}')
        self.assertEqual(self.drain()['status'],'deferred');self.assertFalse(self.calls)
        with self.assertRaises(ValueError):closure.request(self.root,self.run,'0'*64,'changed')
    def test_no_unrequested_orphan_is_closed(self):
        other=self.root/'other';other.mkdir();self.assertEqual(closure.drain(other)['status'],'idle')


if __name__ == '__main__':unittest.main()
