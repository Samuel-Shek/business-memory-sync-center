#!/usr/bin/env python3
"""Synthetic inputs only; no providers, business receiver or production DB."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import fcntl
import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
import validation_evidence as evidence
import validation_process as process
import validation_progress as progress

class TimingTests(unittest.TestCase):
    def test_group_permission_denial_is_unknown_until_exit_proven(self):
        with patch.object(process.os, 'killpg', side_effect=[PermissionError(), ProcessLookupError()]):
            self.assertTrue(process.group_exists(812345))
            self.assertFalse(process.group_exists(812345))

    def test_settings_are_independent_and_strict(self):
        self.assertEqual(process.settings({'dingtalk_automation': {'memory_validation_timeout_seconds': 20}}), (300,120))
        for v in [None, [], '300', {'full_validation_timeout_seconds':True}, {'slow_after_seconds':300}, {'full_validation_timeout_seconds':601}, {'slow_after_seconds':-1}]:
            with self.subTest(v=v), self.assertRaises(ValueError): process.settings({'automatic_memory_validation':v})

    def fake_run(self, duration, interrupt=False):
        now = [0.]
        child = Mock(pid=812345, returncode=0, stdin=None, stdout=None, stderr=None)
        def communicate(timeout):
            now[0] += timeout
            if interrupt: raise KeyboardInterrupt()
            if now[0] < duration: raise subprocess.TimeoutExpired('fixture', timeout)
            return ('{"verdict":"pass"}', '')
        child.communicate.side_effect = communicate
        p = Mock()
        with patch.object(process.os, 'getpgid', return_value=812345), patch.object(process, 'group_exists', return_value=False), patch.object(process, 'cleanup') as cleanup, patch.object(progress,'current',return_value=p):
            if duration>300:
                with self.assertRaises(subprocess.TimeoutExpired): process.run(['fixture'],timeout=300,slow_after=120,env={},pass_fds=(),clock=lambda:now[0],popen=lambda *a,**k:child)
                cleanup.assert_called_once()
            elif interrupt:
                with self.assertRaises(KeyboardInterrupt): process.run(['fixture'],timeout=300,slow_after=120,env={},pass_fds=(),clock=lambda:now[0],popen=lambda *a,**k:child)
                cleanup.assert_called_once()
            else:
                result=process.run(['fixture'],timeout=300,slow_after=120,env={},pass_fds=(),clock=lambda:now[0],popen=lambda *a,**k:child)
                self.assertEqual(result.returncode,0);cleanup.assert_not_called()
        return now[0],p

    def test_161_success_warns_once_without_retry(self):
        elapsed,p=self.fake_run(161)
        self.assertEqual(elapsed,161)
        slow=[c for c in p.emit.call_args_list if c.args[0]=='slow_validation']
        self.assertEqual(len(slow),1);self.assertEqual(slow[0].kwargs['validator_seconds'],120)

    def test_300_deadline_cleanup(self):
        elapsed,_=self.fake_run(999);self.assertEqual(elapsed,300)

    def test_interrupt_cleanup(self): self.fake_run(161,True)

    def test_real_ruby_descendant_group_and_lock_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);lock=root/'writer.lock';pidfile=root/'reader.pid'
            fd=os.open(lock,os.O_RDWR|os.O_CREAT,0o600);fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            script=root/'child.rb'
            script.write_text("Process.setpgid(0,0)\npid=spawn('/usr/bin/python3','-c','import time; time.sleep(90)', close_others: false)\nFile.write(ARGV[0],pid.to_s)\nsleep 90\n")
            start=time.monotonic()
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.run(['/usr/bin/ruby',str(script),str(pidfile)],timeout=5,slow_after=.2,env=os.environ.copy(),pass_fds=(fd,))
            finally: os.close(fd)
            self.assertLess(time.monotonic()-start,12)
            pid=int(pidfile.read_text())
            with self.assertRaises(ProcessLookupError): os.kill(pid,0)
            with lock.open('a+') as f: fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)

    def test_diagnostic_io_failure_does_not_break_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=progress.Progress(tmp,'fixture')
            with patch.object(progress.os,'open',side_effect=PermissionError):
                p.emit('stage_started');p.emit('stage_finished')
            self.assertEqual(p.dropped,2)

    def test_progress_payload_has_only_counts_and_no_source_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            with progress.Progress(tmp,'fixture','fixture-run'):
                with progress.stage('files'):
                    progress.count('file_reads',3)
            rows=[json.loads(s) for s in (Path(tmp)/'state/validation-progress.jsonl').read_text().splitlines()]
            self.assertTrue(any(r['event']=='stage_finished' and r['counts']['file_reads']==3 for r in rows))
            self.assertTrue(all(r['run_id']=='fixture-run' and r['code_sha256'] for r in rows))
            self.assertNotIn('source_text',str(rows))

    def test_sigterm_cleans_group_and_preserves_native_parent_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);pidfile=root/'reader.pid';lock=root/'writer.lock';script=root/'reader.rb'
            script.write_text("Process.setpgid(0,0)\npid=spawn('/usr/bin/python3','-c','import time; time.sleep(90)', close_others: false)\nFile.write(ARGV[0],pid.to_s)\nsleep 90\n")
            source="import sys,os,fcntl; import validation_process as p; fd=os.open(sys.argv[3],os.O_RDWR|os.O_CREAT,0o600); fcntl.flock(fd,fcntl.LOCK_EX); p.run(['/usr/bin/ruby',sys.argv[1],sys.argv[2]], timeout=60,slow_after=20,env=os.environ.copy(),pass_fds=(fd,))"
            env=dict(os.environ,PYTHONPATH=str(Path(__file__).parent))
            parent=subprocess.Popen(['/usr/bin/python3','-c',source,str(script),str(pidfile),str(lock)],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                until=time.monotonic()+15
                while not pidfile.exists() and time.monotonic()<until: time.sleep(.02)
                self.assertTrue(pidfile.exists())
                descendant=int(pidfile.read_text())
                parent.terminate();out,err=parent.communicate(timeout=10)
                self.assertEqual(parent.returncode,143,err.decode())
                with self.assertRaises(ProcessLookupError): os.kill(descendant,0)
                with lock.open('a+') as f: fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            finally:
                if parent.poll() is None: parent.kill();parent.communicate()

    def test_journal_rotation_reopens_other_writer_and_closes_descriptors(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=progress.Progress(tmp,'first');q=progress.Progress(tmp,'second')
            with p, q:
                p.emit('first')
                old_fd=p.journal_fd
                with patch.object(progress,'LIMIT',1): q.emit('rotate')
                p.emit('after_rotation')
                path=Path(tmp)/'state/validation-progress.jsonl'
                self.assertIn('after_rotation',path.read_text())
                self.assertEqual(os.fstat(p.journal_fd).st_ino,path.stat().st_ino)
                descriptors=[p.lock_fd,p.journal_fd,q.lock_fd,q.journal_fd]
            for fd in descriptors:
                with self.assertRaises(OSError):os.fstat(fd)

    def test_ruby_progress_records_stages_without_changing_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'state').mkdir()
            library=str(Path(__file__).parent/'validation_progress.rb')
            result=subprocess.run(['/usr/bin/ruby','-r',library,'-e',"p=ValidationProgress.new(ARGV[0]); p.stage('files', {files: 7}); p.finish; puts '{}'",str(root)],capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0);self.assertEqual(json.loads(result.stdout),{})
            rows=[json.loads(line) for line in (root/'state/validation-progress.jsonl').read_text().splitlines()]
            self.assertTrue(any(row['phase']=='files' and row['counts']=={'files':7} for row in rows))

class CacheTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name);self.p=self.root/'note.md';self.p.write_text('original');self.cache={}

    def test_cache_reduces_reads_and_preserves_result(self):
        with progress.Progress(self.root,'fixture') as p:
            one=evidence.file_binding(self.p,self.cache);two=evidence.file_binding(self.p,self.cache)
        self.assertEqual(one,two);self.assertEqual(p.counts['file_reads'],1);self.assertEqual(p.counts['cache_hits'],1)

    def test_replaced_by_symlink_during_open_defers(self):
        original = evidence.os.open
        def replaced(path, flags):
            target=self.root/'other.md';target.write_text('original')
            self.p.unlink();self.p.symlink_to(target)
            return original(path, flags)
        with patch.object(evidence.os, 'open', side_effect=replaced), self.assertRaises(evidence.InputsChanged):
            evidence.file_binding(self.p, self.cache)

    def test_same_size_replacement_invalidates_cache(self):
        one=evidence.file_binding(self.p,self.cache);other=self.root/'replacement';other.write_text('modified');os.replace(other,self.p)
        two=evidence.file_binding(self.p,self.cache);self.assertNotEqual(one,two)

    def test_same_size_in_place_change_even_with_restored_mtime(self):
        one=evidence.file_binding(self.p,self.cache);st=self.p.stat();self.p.write_text('modified');os.utime(self.p,ns=(st.st_atime_ns,st.st_mtime_ns))
        self.assertNotEqual(one,evidence.file_binding(self.p,self.cache))

    def test_cache_hit_rechecks_permission(self):
        evidence.file_binding(self.p,self.cache)
        with patch.object(evidence.os,'open',side_effect=PermissionError),self.assertRaises(PermissionError): evidence.file_binding(self.p,self.cache)

    def test_file_disappearing_after_enumeration_is_changed_input(self):
        self.p.unlink()
        with self.assertRaises(evidence.InputsChanged): evidence.file_binding(self.p,self.cache)
        self.assertFalse(self.cache)

    def test_change_during_hash_never_yields_binding(self):
        original=evidence.hashlib.sha256
        def digest():
            inner=original()
            class Changing:
                def update(_,chunk): inner.update(chunk);self.p.write_text('modified')
                def hexdigest(_): return inner.hexdigest()
            return Changing()
        with patch.object(evidence.hashlib,'sha256',side_effect=digest),self.assertRaises(evidence.InputsChanged): evidence.file_binding(self.p,self.cache)
        self.assertFalse(self.cache)

    def fixture_context(self):
        memory=self.root/'vault/memory';memory.mkdir(parents=True);(memory/'note.md').write_text('fixture')
        extra=self.root/'vault/ledger.json';extra.write_text('{}')
        config={'memory_root':str(memory),'source_governance':{'inventory':str(extra)},'external_root':str(self.root/'external')}
        (self.root/'config.json').write_text(json.dumps(config))
        for name in evidence.CODE_PATHS:
            target=self.root/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_text('fixture')
        return memory

    def test_no_cache_between_snapshots_and_diagnostics_outside_snapshot(self):
        memory=self.fixture_context()
        with progress.Progress(self.root,'fixture') as p: before=evidence.context(self.root)
        self.assertEqual(p.counts['file_reads'],2);self.assertEqual(p.counts['cache_hits'],1)
        self.assertEqual(before,evidence.context(self.root))
        (memory/'note.md').write_text('changed')
        self.assertNotEqual(before,evidence.context(self.root))

    def test_display_digest_reuse_still_detects_changes_and_permissions(self):
        memory=self.fixture_context(); cache={}
        before=evidence.context(self.root,file_cache=cache)
        with progress.Progress(self.root,'fixture') as p:
            self.assertEqual(before,evidence.context(self.root,file_cache=cache))
        self.assertEqual(p.counts.get('file_reads',0),0)
        note=memory/'note.md';st=note.stat();note.write_text('changed');os.utime(note,ns=(st.st_atime_ns,st.st_mtime_ns))
        current=evidence.context(self.root,file_cache=cache)
        self.assertNotEqual(before,current);self.assertEqual(current,evidence.context(self.root))
        with patch.object(evidence.os,'open',side_effect=PermissionError):
            with self.assertRaises(PermissionError):evidence.context(self.root,file_cache=cache)

    def test_unstable_file_rejects_snapshot_but_observes_remaining_files(self):
        memory=self.fixture_context();(memory/'second.md').write_text('second')
        old=evidence.file_binding;seen=[]
        def binding(path,cache):
            seen.append(path.name)
            if path.name=='note.md':raise evidence.InputsChanged('fixture')
            return old(path,cache)
        with patch.object(evidence,'file_binding',side_effect=binding):
            with self.assertRaises(evidence.InputsChanged):evidence.context(self.root)
        self.assertIn('second.md',seen);self.assertIn('ledger.json',seen)
        self.assertIsInstance(evidence.context(self.root),dict)

    def test_old_new_semantic_bindings_equal(self):
        self.fixture_context()
        # Old algorithm copied literally as a local reference, no production files.
        def old_binding(path):
            st=path.lstat()
            if path.is_symlink(): return (str(path),st.st_mode,'symlink',os.readlink(path))
            if path.suffix.lower() in {'.md','.json','.jsonl','.txt'}:
                return (str(path),st.st_mode,hashlib.sha256(path.read_bytes()).hexdigest())
            return (str(path),st.st_size,st.st_mtime_ns,st.st_ino,st.st_mode)
        new=evidence.context(self.root)
        with patch.object(evidence,'file_binding',side_effect=lambda path,cache:old_binding(path)):
            old=evidence.context(self.root)
        self.assertEqual(new,old)

if __name__=='__main__': unittest.main(verbosity=2)
