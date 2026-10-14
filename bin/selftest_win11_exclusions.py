#!/usr/bin/env python3
"""Isolated regression checks for scoped Win11 file exclusions."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
import uuid

BIN = Path(__file__).resolve().parent
LIVE_BIN = Path(_archive_path('/Users/example/AI/LocalData/BUSINESS-Memory-Bridge.nosync/bin'))
sys.path.insert(0, str(LIVE_BIN))

def load(name):
    spec = importlib.util.spec_from_file_location('test_' + name, BIN / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

class ExclusionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='business-exclusion-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.w = load('folder_watcher')
        for key, value in {'STATE_ROOT': 'state', 'LOG_PATH': 'logs/watcher.jsonl',
                           'HEALTH_PATH': 'state/health.json'}.items():
            setattr(self.w, key, self.root / value)
        self.source = self.root / 'share/source'
        self.source.mkdir(parents=True)
        self.config = dict(source_id='win11_business_memory', root=str(self.source),
                           mount_root=str(self.source.parent), expected_fs='smbfs',
                           expected_share='DeskShare', expected_server='fixture',
                           excluded_relative_paths=['同步看板.html'])
        self.events, self.captures = [], []
        self.w.queue_event = lambda manifest, payload=None: self.events.append(manifest)
        self.w.pending_watch_bundle_count = lambda: 0
        self.database = dict(source_known=True, entries={}, path_generations={})
        self.w.read_database_state = lambda source_id: self.database

    def planner(self, config=None):
        def capture(source, destination, metadata):
            self.captures.append(source.relative_to(self.source).as_posix())
            return self.w._capture_file(source, destination, metadata['size_bytes'], metadata['mtime_ns'], 10000)
        return self.w.FolderWatcher(config or self.config, 10000,
                                    mount_checker=lambda source: True,
                                    scanner=self.w._scan_tree, capturer=capture)

    def old(self, relative):
        f = self.source / relative
        st = f.stat()
        return dict(source_entry_id=str(uuid.uuid4()), current_relative_path=relative,
                    path_key=self.w.path_key(relative), current_sha256=hashlib.sha256(f.read_bytes()).hexdigest(),
                    source_mtime_ns=st.st_mtime_ns, source_modified_at=self.w.mtime_iso(st.st_mtime_ns),
                    size_bytes=st.st_size, version_number=1, storage_class='quarantine',
                    version_ref='external:fixture', mirror_relative=None, last_event_id=str(uuid.uuid4()),
                    last_event_status='quarantined', transition_sequence=1)

    def test_config_defaults_and_invalid_paths(self):
        plain = dict(self.config); plain.pop('excluded_relative_paths')
        self.assertEqual(self.w.validate_source_config(plain)['excluded_relative_paths'], [])
        for value in ['同步看板.html', None, [None], ['../a'], ['/a'], ['a//b'], ['a/'], ['a\\b']]:
            with self.subTest(value=value), self.assertRaises(self.w.WatcherError):
                self.w.validate_source_config(dict(self.config, excluded_relative_paths=value))

    def test_refresh_never_captures_or_false_deletes_existing_dashboard(self):
        board = self.source / '同步看板.html'; board.write_text('before')
        old = self.old(board.name); self.database['entries'][board.name] = old
        planner = self.planner()
        planner.state['missing_counts'][old['source_entry_id']] = 1
        planner.state['replacement_counts'][old['source_entry_id']] = 1
        planner.state['pending_readds'][board.name] = {'relative_path': board.name}
        for n in range(6):
            board.write_text('refresh-' + str(n))
            planner.cycle(observed_at=n * 65, force_full=True)
        self.assertEqual(self.captures, []); self.assertEqual(self.events, [])
        self.assertEqual(planner.state['missing_counts'], {})
        self.assertEqual(planner.state['replacement_counts'], {})
        self.assertEqual(planner.state['pending_readds'], {})
        self.assertEqual(self.database['entries'][board.name], old)

    def test_exclusion_is_full_path_and_other_html_is_collected(self):
        for relative in ['同步看板.html', '资料/同步看板.html', '业务.html']:
            f = self.source / relative; f.parent.mkdir(exist_ok=True); f.write_text('fixture')
        planner = self.planner()
        for n in range(3): planner.cycle(observed_at=n * 65, force_full=True)
        self.assertEqual(set(self.captures), {'资料/同步看板.html', '业务.html'})
        self.assertEqual({e['source_relative_path'] for e in self.events}, set(self.captures))

    def test_removing_exclusion_resumes_change_detection(self):
        board = self.source / '同步看板.html'; board.write_text('before')
        self.database['entries'][board.name] = self.old(board.name)
        board.write_text('after')
        planner = self.planner(dict(self.config, excluded_relative_paths=[]))
        for n in range(3): planner.cycle(observed_at=n * 65, force_full=True)
        self.assertEqual([e['operation'] for e in self.events], ['modified'])

    def test_pending_dashboard_uses_real_prepare_and_commit_without_ai(self):
        root = self.root / 'bridge'; (root / 'state').mkdir(parents=True)
        (root / 'bin').mkdir(); memory = root / 'vault/00_业务记忆库'; memory.mkdir(parents=True)
        external = root / 'external'; external.mkdir(); notes = external / 'notes'; notes.mkdir()
        config = dict(memory_root=str(memory), external_root=str(external), watch_notes_root=str(notes),
                      semantic_database=str(root / 'state/semantic.sqlite'),
                      semantic_runs_root=str(root / 'runs'), watch_sources=[self.config],
                      semantic_merge=dict(source_id='win11_business_memory', manual_manifest=str(root/'seed.sha256'),
                                          manual_ledger=str(root/'seed.md'), max_content_items=25, max_total_bytes=5242880))
        (root / 'config.json').write_text(json.dumps(config))
        (root / 'bin/refresh_memory_indexes.rb').write_text('puts "refreshed"\n')
        (root / 'bin/validate_memory.rb').write_text('puts \'{"verdict":"pass","errors":[]}\'\n')
        db = sqlite3.connect(root / 'state/bridge.sqlite')
        db.executescript('''PRAGMA user_version=3;
          CREATE TABLE watch_versions (placeholder TEXT);
          CREATE TABLE watch_entries(source_id TEXT,source_entry_id TEXT,current_relative_path TEXT,active INTEGER,current_sha256 TEXT,version_ref TEXT,size_bytes INTEGER,version_number INTEGER);
          CREATE TABLE source_events(event_id TEXT,source_id TEXT,source_entry_id TEXT,source_relative_path TEXT,operation TEXT,status TEXT,processed_at TEXT,transition_sequence INTEGER,current_sha256 TEXT);
        ''')
        entry_id, event_id = str(uuid.uuid4()), str(uuid.uuid4())
        db.execute('INSERT INTO watch_entries VALUES (?,?,?,?,?,?,?,?)',
                   ('win11_business_memory', entry_id, '同步看板.html', 1, 'a'*64, 'external:intentionally-missing.html', 10, 1))
        db.execute('INSERT INTO source_events VALUES (?,?,?,?,?,?,?,?,?)',
                   (event_id, 'win11_business_memory', entry_id, '同步看板.html', 'modified', 'quarantined', '2020-01-02T12:00:00+08:00', 1, 'a'*64))
        db.commit(); db.close()
        (notes / ('Win11文件夹监控-'+entry_id+'.md')).write_text(
            f'---\nmemory_id: BUSINESS-WIN-{entry_id}\nsource_system: windows_folder_watch\nsource_event_id: {event_id}\nsource_operation: modified\n---\n\nFixture.\n')
        env = dict(os.environ, BUSINESS_BRIDGE_ROOT=str(root), LANG='en_US.UTF-8', LC_ALL='en_US.UTF-8')
        def merge(*args, **kwargs):
            result = subprocess.run(['/usr/bin/ruby', str(BIN/'win11_semantic_merge.rb'), *args], env=env,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)
        automation = load('win11_memory_automation')
        automation.run_merge = merge
        automation.append_log = lambda *args, **kwargs: None
        automation.reconciliation = types.SimpleNamespace(**vars(automation.reconciliation))
        automation.reconciliation.open_claim_context = lambda *a, **k: []
        automation.reconciliation.submit_relations = lambda *a, **k: {}
        automation.reconciliation.refresh_index = lambda *a, **k: {}
        automation.invoke_codex = lambda *a, **k: self.fail('excluded source invoked AI')
        outcome = automation.run_once(config, False)
        self.assertEqual(outcome['new_notes'], 0)
        self.assertEqual(outcome['dispositions'], {'no_business_change': 1})
        sem = sqlite3.connect(root / 'state/semantic.sqlite')
        self.assertEqual(sem.execute('select status,disposition from items').fetchall(), [('completed','no_business_change')])
        sem.close()
        self.assertEqual(merge('prepare')['status'], 'noop')
        self.assertFalse(list(memory.rglob('*.md')))

if __name__ == '__main__':
    unittest.main(verbosity=2)
