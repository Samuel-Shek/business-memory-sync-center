#!/usr/bin/env python3
"""Isolated rollback and receipt regressions. No provider or production writes."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import json
import tempfile
import unittest
import sqlite3
from pathlib import Path
from unittest.mock import patch
import dingtalk_memory_automation as m
import validation_evidence as evidence


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = self.root / 'vault' / 'memory'
        self.memory.mkdir(parents=True)
        self.config = {'memory_root': str(self.memory), 'external_root': str(self.root / 'external'), 'dingtalk_automation': {}}
        self.paths = m.runtime_paths(self.config)
        self.note = self.memory / '02_内部业务资料' / 'note.md'
        self.note.parent.mkdir(parents=True)
        self.note.write_text('before')
        self.journal = self.paths['runs'] / 'old' / 'apply_journal.json'
        backup = self.journal.parent / 'backup' / 'note.md'
        backup.parent.mkdir(parents=True)
        backup.write_text('before')
        self.entry = {'target': str(self.note), 'backup': str(backup), 'existed': True,
                      'before_sha256': m.sha256_text('before'), 'applied_sha256': m.sha256_text('applied')}
        self.payload = {'state': 'applying', 'batch_id': 'old', 'source_refs': ['old-source'], 'paths': [self.entry]}
        self.save()

    def save(self):
        self.journal.write_text(json.dumps(self.payload))

    def restore(self, verifier=None):
        with patch.object(m, 'run_refresh_and_validate', verifier or (lambda *a, **kw: {'pass': True})):
            m.restore_from_journal(self.config, self.journal)

    def test_idempotent_validation_checkpoint(self):
        with self.assertRaises(m.AutomationError):
            self.restore(lambda *a, **kw: (_ for _ in ()).throw(m.AutomationError('validation_failed', 'test')))
        self.assertEqual(json.loads(self.journal.read_text())['state'], 'rollback_pending_validation')
        self.restore()
        self.assertEqual(self.note.read_text(), 'before')

    def test_restore_applied_and_restart(self):
        self.note.write_text('applied')
        with self.assertRaises(m.AutomationError):
            self.restore(lambda *a, **kw: (_ for _ in ()).throw(m.AutomationError('validation_failed', 'test')))
        self.assertEqual(self.note.read_text(), 'before')
        self.assertEqual(json.loads(self.journal.read_text())['state'], 'rollback_pending_validation')
        self.restore()
        self.assertEqual(json.loads(self.journal.read_text())['state'], 'rolled_back')
        self.note.write_text('new user edit')
        self.restore()
        self.assertEqual(self.note.read_text(), 'new user edit')

    def test_already_restored_and_derived_index_drift(self):
        index = self.memory / '00_记忆总览.md'
        index.write_text('new index')
        backup = self.journal.parent / 'backup' / 'index.md'
        backup.write_text('old index')
        self.payload['paths'].append({'target': str(index), 'backup': str(backup), 'existed': True,
            'before_sha256': m.sha256_text('old index'), 'applied_sha256': m.sha256_text('transaction index')})
        self.save()
        self.restore()
        self.assertEqual(index.read_text(), 'new index')
        self.assertEqual(self.note.read_text(), 'before')

    def test_conflict_preserves_entire_batch_and_guard(self):
        self.note.write_text('later edit')
        with self.assertRaisesRegex(m.AutomationError, 'changed'):
            self.restore()
        self.assertEqual(self.note.read_text(), 'later edit')
        self.assertEqual(json.loads(self.journal.read_text())['state'], 'recovery_conflict')
        other = self.memory / '02_内部业务资料' / 'other.md'
        with patch.object(m, 'batch_source_refs', return_value=['new-source']):
            m.ensure_recovery_isolation(self.config, 'new', [other])
            with self.assertRaises(m.AutomationError):
                m.ensure_recovery_isolation(self.config, 'new', [self.note])
        for refs in (None, ['old-source']):
            with patch.object(m, 'batch_source_refs', return_value=refs), self.assertRaises(m.AutomationError):
                m.ensure_recovery_isolation(self.config, 'new', [other])

    def test_bad_backup_does_not_write(self):
        Path(self.entry['backup']).write_text('damaged')
        self.note.write_text('applied')
        with self.assertRaises(m.AutomationError):
            self.restore()
        self.assertEqual(self.note.read_text(), 'applied')

    def test_crash_before_applied_snapshot(self):
        del self.entry['applied_sha256']
        self.entry['expected_applied_sha256'] = m.sha256_text('applied')
        self.save()
        self.note.write_text('applied')
        self.restore()
        self.assertEqual(self.note.read_text(), 'before')

    def test_uncommitted_new_file(self):
        self.entry.update(existed=False, backup=None, before_sha256=None)
        self.save()
        self.note.write_text('applied')
        self.restore()
        self.assertFalse(self.note.exists())
        self.restore()

    def test_legacy_missing_snapshot_is_not_overwritten(self):
        del self.entry['applied_sha256']
        self.save()
        self.note.write_text('unknown edit')
        with self.assertRaises(m.AutomationError):
            self.restore()
        self.assertEqual(self.note.read_text(), 'unknown edit')

    def test_receipt_binds_data_code_and_generation(self):
        for name in evidence.CODE_PATHS:
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text('test')
        (self.root / 'config.json').write_text(json.dumps(self.config))
        receipt = {'schema_version': 2, 'verdict': 'pass', 'full_validation': True, 'index_dirty_generation': '1',
                   'index_applied_generation': '1', 'validation_context': evidence.context(self.root)}
        self.assertTrue(evidence.receipt_is_current(self.root, receipt, '1', '1'))
        self.assertFalse(evidence.receipt_is_current(self.root, receipt, '2', '2'))
        self.note.write_text('later edit')
        self.assertFalse(evidence.receipt_is_current(self.root, receipt, '1', '1'))
        receipt['validation_context'] = evidence.context(self.root)
        (self.root / 'bin' / 'validate_memory.rb').write_text('new validator')
        self.assertFalse(evidence.receipt_is_current(self.root, receipt, '1', '1'))

    def test_sqlite_read_sidecars_are_not_business_changes(self):
        for name in evidence.CODE_PATHS:
            (self.root/name).parent.mkdir(parents=True,exist_ok=True)
            (self.root/name).write_text('test')
        ledger=self.root/'machine-ledgers'
        ledger.mkdir()
        (ledger/'对话同步台账.json').write_text('{}')
        database=ledger/'semantic.sqlite'
        with sqlite3.connect(database) as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('CREATE TABLE runs(run_id TEXT,status TEXT)')
            connection.execute("INSERT INTO runs VALUES('fixture','blocked')")
        self.config.update(machine_ledger_root=str(ledger),semantic_database=str(database))
        (self.root/'config.json').write_text(json.dumps(self.config))
        before=evidence.context(self.root)
        sidecar=ledger/'semantic.sqlite-shm'
        self.assertTrue(sidecar.exists())
        sidecar.touch()
        after=evidence.context(self.root)
        self.assertEqual(before,after)
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE runs SET status='failed'")
        self.assertNotEqual(after,evidence.context(self.root))

    def test_directory_permission_failure_is_not_silently_omitted(self):
        for name in evidence.CODE_PATHS:
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text('test')
        (self.root / 'config.json').write_text(json.dumps(self.config))
        receipt = {'schema_version': 2, 'verdict': 'pass', 'full_validation': True,
                   'index_dirty_generation': '1', 'index_applied_generation': '1',
                   'validation_context': evidence.context(self.root)}
        def denied_walk(base, **options):
            options['onerror'](PermissionError(1, 'Operation not permitted', str(base)))
            return iter(())
        with patch.object(evidence.os, 'walk', side_effect=denied_walk):
            with self.assertRaises(PermissionError):
                evidence.context(self.root)
            result = evidence.receipt_status(self.root, receipt, '2', '1')
            self.assertFalse(result['current'])
            self.assertEqual(result['error_code'], 'validation_permission_denied')

    def test_permission_preflight_prevents_refresh_or_validator_invocation(self):
        config_file = self.root / 'config.json'
        config_file.write_text(json.dumps(self.config))
        with patch.object(m, 'CONFIG_PATH', config_file), patch.object(evidence, 'context', side_effect=PermissionError), patch.object(m.subprocess, 'run') as command:
            with self.assertRaises(m.AutomationError) as failure:
                m.run_refresh_and_validate(self.config)
            self.assertEqual(failure.exception.code, 'validation_permission_denied')
            command.assert_not_called()


if __name__ == '__main__':
    unittest.main()
