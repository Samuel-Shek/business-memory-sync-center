"""Native/worker SQLite interoperability; synthetic files only."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import json,subprocess,tempfile,unittest,sqlite3,fcntl
from pathlib import Path
from unittest import mock
import email_sync as s
import mail_auto_recovery as r
from selftest_email_sync import temporary_config

FRAMEWORK='/Library/Frameworks/Python.framework/Versions/3.12/bin/python3'

class StorageRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        self.config=temporary_config(self.root);self.paths=s.runtime_paths(self.config)
        self.path=self.paths['database'];self.path.parent.mkdir(parents=True)
        self.lock=self.path.parent/'mail-test.lock'
        self.patch=mock.patch.object(s,'RUN_LOCK',self.lock);self.patch.start()
        self.create()
    def tearDown(self):self.patch.stop();self.tmp.cleanup()
    def create(self):
        code="import sqlite3,sys;c=sqlite3.connect(sys.argv[1]);c.execute('PRAGMA journal_mode=WAL');c.execute('CREATE TABLE messages(message_key TEXT)');c.execute(\"INSERT INTO messages VALUES('fixture')\");c.commit();c.close()"
        subprocess.run([FRAMEWORK,'-c',code,str(self.path)],check=True,capture_output=True)
    def test_native_recreates_missing_sidecars_without_data_change(self):
        before=s.sha256_file(self.path)
        result=r.repair_storage(self.config)
        self.assertEqual('verified',result['status'],result)
        self.assertEqual(before,s.sha256_file(self.path));self.assertTrue(result['readonly_verified'])
        self.assertEqual('healthy',r.repair_storage(self.config)['status'])
        receipt=s.read_json(self.path.parent/'mail-storage-recovery.json',{})
        self.assertEqual(1,receipt['repair_count'])
        with sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True) as c:
            self.assertEqual([('fixture',)],c.execute('SELECT * FROM messages').fetchall())
    def test_active_writer_leaves_database_and_sidecars_alone(self):
        with self.lock.open('a+') as f:
            fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            result=r.repair_storage(self.config)
        self.assertEqual('waiting',result['status'])
        self.assertFalse(Path(str(self.path)+'-wal').exists())
    def test_disable_does_not_repair(self):
        self.config['email_sync']['auto_recovery_enabled']=False
        self.assertEqual('disabled',r.repair_storage(self.config)['status'])
        self.assertFalse(Path(str(self.path)+'-wal').exists())
    def test_existing_sidecar_or_permission_issue_is_not_repaired(self):
        # Force only the exact native open error; no production path is used.
        Path(str(self.path)+'-wal').write_bytes(b'fixture')
        with mock.patch.object(r.sqlite3,'connect',side_effect=sqlite3.OperationalError('unable to open database file')):
            self.assertEqual('mail_storage_sidecars_present',r.repair_storage(self.config)['error_code'])
        self.assertEqual(b'fixture',Path(str(self.path)+'-wal').read_bytes())
    def test_unknown_failure_is_not_repaired(self):
        with mock.patch.object(r.sqlite3,'connect',side_effect=sqlite3.OperationalError('database disk image is malformed')):
            self.assertEqual('blocked',r.repair_storage(self.config)['status'])
        self.assertFalse(Path(str(self.path)+'-wal').exists())
    def test_audit_symlink_cannot_redirect_write(self):
        target=self.root/'unrelated';target.write_text('unchanged')
        (self.path.parent/'mail-storage-recovery.jsonl').symlink_to(target)
        self.assertEqual('mail_storage_unsafe_path',r.repair_storage(self.config)['error_code'])
        self.assertEqual('unchanged',target.read_text())
        self.assertFalse(Path(str(self.path)+'-wal').exists())
    def test_ancestor_symlink_cannot_redirect_database(self):
        link=self.root/'alias';link.symlink_to(self.paths['state_root'],target_is_directory=True)
        self.config['email_sync']['state_root']=str(link)
        self.assertEqual('mail_storage_unsafe_path',r.repair_storage(self.config)['error_code'])
        self.assertFalse(Path(str(self.path)+'-wal').exists())
    def test_non_operational_sqlite_error_does_not_escape_supervisor(self):
        with mock.patch.object(r.sqlite3,'connect',side_effect=sqlite3.DatabaseError('fixture')):
            self.assertEqual('blocked',r.repair_storage(self.config)['status'])

if __name__=='__main__':unittest.main()
