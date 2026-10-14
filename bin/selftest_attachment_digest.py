"""Owner-bound "key points only" merge; no live services, no model calls."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import attachment_analysis as aa
import attachment_digest as digest
import email_batched_input as batching
import email_sync

GROUP = '07709987-5cfb-5781-a4ca-ba71f46c9b94'


class DigestMergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.text = ''.join(f'第{i:05d}行 客诉记录 型号A{i % 7} 处理中\n' for i in range(1600))
        self.assertGreaterEqual(len(self.text), aa.MIN_CHARS)
        self.quotes = [self.text[i * 1500:i * 1500 + 1400] for i in range(20)]
        self.value = {'observations': [{'quote': q, 'observation': f'要点{i}'} for i, q in enumerate(self.quotes)],
                      'uncertainties': ['部分单元格为空']}
        self.doc = {'messages': [{'attachments': [{'name': 'list.xlsx', 'extracted_text': self.text,
                                                   'full_read_confirmed': False}]}]}
        self.calls = []

    def invoke(self, *args):
        self.calls.append(1)
        return self.value

    def prepare(self, run, grant):
        return aa.prepare(self.doc, self.root / run, self.root / 'cache', ['scope'], self.invoke, digest_grant=grant)

    def test_without_owner_decision_overflow_still_stops(self):
        with patch.object(batching, 'REQUEST_BUDGET', 20000):
            with self.assertRaises(batching.BatchInputError) as error:
                self.prepare('run', None)
        self.assertEqual(error.exception.code, 'attachment_analysis_merge_capacity')
        coverage = json.loads((self.root / 'run/source-analysis-coverage.json').read_text())
        self.assertEqual(coverage['status'], 'waiting_merge_capacity')
        self.assertNotIn('owner_digest', coverage)

    def test_owner_decision_keeps_every_observation_with_position_and_leaves_cache_intact(self):
        with patch.object(batching, 'REQUEST_BUDGET', 20000):
            result = self.prepare('run', {'policy': digest.POLICY})
        content = json.loads(result['messages'][0]['attachments'][0]['extracted_text'])
        self.assertEqual(content['representation'], 'owner_digest_observations_quotes_in_local_cache')
        observations = content['fragments'][0]['observations']
        self.assertEqual([o['observation'] for o in observations], [f'要点{i}' for i in range(20)])
        for item, quote in zip(observations, self.quotes):
            self.assertNotIn('quote', item)
            self.assertEqual(self.text[item['source_offset']:item['source_offset'] + item['quote_chars']], quote)
            self.assertEqual(item['quote_excerpt'], quote[:digest.EXCERPT_CHARS])
        self.assertEqual(content['fragments'][0]['uncertainties'], ['部分单元格为空'])
        self.assertLessEqual(batching.serialized_size(result), 20000)
        capacity = json.loads((self.root / 'run/source-analysis-capacity.json').read_text())
        self.assertTrue(capacity['merge_ready'] and capacity['owner_digest'] and capacity['quotes_preserved_in_local_cache'])
        self.assertFalse(capacity['automatic_split_or_truncation'])
        self.assertGreater(capacity['serialized_chars'], 20000)
        self.assertEqual(json.loads((self.root / 'run/source-analysis-coverage.json').read_text())['status'],
                         'complete_owner_digest')
        cached = json.loads(next((self.root / 'cache').rglob('*.json')).read_text())
        self.assertEqual(cached['result'], self.value)
        self.assertEqual(len(self.calls), 1)

    def test_owner_decision_does_not_change_a_merge_that_already_fits(self):
        result = self.prepare('run', {'policy': digest.POLICY})
        content = json.loads(result['messages'][0]['attachments'][0]['extracted_text'])
        self.assertEqual(content['representation'], 'source_observations_not_business_decisions')
        self.assertEqual(content['fragments'][0]['source_observations'], self.value)

    def test_digest_that_still_overflows_keeps_stopping(self):
        with patch.object(batching, 'REQUEST_BUDGET', 500):
            with self.assertRaises(batching.BatchInputError) as error:
                self.prepare('run', {'policy': digest.POLICY})
        self.assertEqual(error.exception.code, 'attachment_analysis_merge_capacity')


class DigestGrantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.database = self.root / 'email.sqlite'
        with sqlite3.connect(self.database) as c:
            c.execute('CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            c.execute('CREATE TABLE semantic_groups(semantic_id TEXT PRIMARY KEY, status TEXT, input_sha256 TEXT, last_error_code TEXT)')
            c.execute("INSERT INTO semantic_groups VALUES(?, 'failed', 'input-a', 'attachment_analysis_merge_capacity')", (GROUP,))
            c.execute('''CREATE TABLE mail_recovery_tasks(
              task_key TEXT PRIMARY KEY, object_key TEXT NOT NULL, stage TEXT NOT NULL,
              input_version TEXT NOT NULL, error_code TEXT NOT NULL, category TEXT NOT NULL,
              state TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
              probe_count INTEGER NOT NULL DEFAULT 0, next_due TEXT, receipt_json TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
            for key, version in (('current', 'input-a'), ('older', 'input-old')):
                c.execute("INSERT INTO mail_recovery_tasks VALUES(?,?,'semantic',?,'model_request_uncertain','maintenance',"
                          "'needs_maintenance',1,0,NULL,NULL,'t','t')", (key, GROUP, version))
        self.rows = [{'message_key': 'm1'}, {'message_key': 'm2'}]
        self.receipt = self.root / 'tests.txt'
        self.receipt.write_text('OK\n')
        self.tests = [{'path': str(self.receipt), 'sha256': email_sync.sha256_file(self.receipt), 'passed': True}]
        def connect(_config=None):
            connection = sqlite3.connect(self.database)
            connection.row_factory = sqlite3.Row
            return connection
        for name, value in (('runtime_paths', lambda _config: {'database': self.database}),
                            ('connect_database_readonly', connect), ('connect_database', connect),
                            ('semantic_rows', lambda _config, _sid: self.rows)):
            patcher = patch.object(email_sync, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tasks(self):
        with sqlite3.connect(self.database) as c:
            return dict(c.execute('SELECT task_key,state FROM mail_recovery_tasks'))

    def test_no_decision_means_no_grant(self):
        self.assertIsNone(digest.grant({}, GROUP, self.rows))

    def test_authorize_records_owner_decision_and_releases_only_current_task(self):
        value = digest.authorize({}, GROUP, authorization='Example Owner：客诉清单只记要点', test_receipts=self.tests)
        self.assertEqual(value['authorization_source'], 'current_owner_request')
        self.assertEqual(value['executor'], 'claude-code')
        self.assertEqual(digest.grant({}, GROUP, self.rows)['input_sha256'], 'input-a')
        self.assertEqual(self.tasks(), {'current': 'pending', 'older': 'needs_maintenance'})
        with sqlite3.connect(self.database) as c:
            events = c.execute('SELECT task_key,state,error_code FROM mail_recovery_events').fetchall()
        self.assertEqual(events, [('current', 'tested_fix_granted', 'owner_bound_attachment_digest')])

    def test_changed_input_or_members_or_tests_fail_closed(self):
        digest.authorize({}, GROUP, authorization='Example Owner：客诉清单只记要点', test_receipts=self.tests)
        with self.assertRaises(email_sync.EmailSyncError) as error:
            digest.grant({}, GROUP, self.rows[:1])
        self.assertEqual(error.exception.code, 'attachment_digest_binding_changed')
        self.receipt.write_text('changed\n')
        with self.assertRaises(email_sync.EmailSyncError) as error:
            digest.grant({}, GROUP, self.rows)
        self.assertEqual(error.exception.code, 'attachment_digest_tests_changed')
        with sqlite3.connect(self.database) as c:
            c.execute("UPDATE semantic_groups SET input_sha256='input-b'")
        with self.assertRaises(email_sync.EmailSyncError) as error:
            digest.grant({}, GROUP, self.rows)
        self.assertEqual(error.exception.code, 'attachment_digest_binding_changed')

    def test_only_a_group_stopped_at_merge_capacity_can_be_authorized(self):
        with sqlite3.connect(self.database) as c:
            c.execute("UPDATE semantic_groups SET last_error_code='model_request_uncertain'")
        with self.assertRaises(ValueError):
            digest.authorize({}, GROUP, authorization='Example Owner：客诉清单只记要点', test_receipts=self.tests)
        with self.assertRaises(ValueError):
            digest.authorize({}, GROUP, authorization='', test_receipts=self.tests)
        self.assertIsNone(digest.grant({}, GROUP, self.rows))
        self.assertEqual(self.tasks(), {'current': 'needs_maintenance', 'older': 'needs_maintenance'})


if __name__ == '__main__':
    unittest.main()
