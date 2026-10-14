"""Source validity, merge capacity and immutable local replay; no live services."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import attachment_analysis as aa
import email_batched_input as batching
import model_call_ledger as ledger
import model_router as router
import dingtalk_sync_v2 as sync


class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.text = 'x' * 30000
        self.doc = {'messages': [{'attachments': [{'name': 'x.txt', 'extracted_text': self.text,
                                                   'full_read_confirmed': False}]}]}

    def test_exact_long_result_survives_in_full(self):
        value = {'observations': [{'quote': self.text[:1600], 'observation': str(i)} for i in range(49)],
                 'uncertainties': ['限制未消除']}
        before = copy.deepcopy(value)
        calls = []
        def invoke(*args):
            calls.append(1)
            return value
        result = aa.prepare(self.doc, self.root/'run', self.root/'cache', ['scope'], invoke)
        content = json.loads(result['messages'][0]['attachments'][0]['extracted_text'])
        self.assertEqual(content['fragments'][0]['source_observations'], before)
        self.assertGreater(len(ledger.canonical(value)), 20000)
        self.assertFalse(content['source_extraction_complete'])
        self.assertEqual(value, before)
        aa.prepare(self.doc, self.root/'restart', self.root/'cache', ['scope'], invoke)
        self.assertEqual(len(calls), 1)
        self.assertTrue(json.loads((self.root/'run/source-analysis-capacity.json').read_text())['merge_ready'])

    def test_merge_overflow_preserves_all_results_and_reuses_them(self):
        value = {'observations': [{'quote': 'x', 'observation': '中' * 850000}], 'uncertainties': []}
        calls = []
        def invoke(*args):
            calls.append(1)
            return value
        for run in ('one', 'restart'):
            with self.assertRaises(batching.BatchInputError) as error:
                aa.prepare(self.doc, self.root/run, self.root/'cache', ['scope'], invoke)
            self.assertEqual(error.exception.code, 'attachment_analysis_merge_capacity')
            coverage = json.loads((self.root/run/'source-analysis-coverage.json').read_text())
            self.assertEqual(coverage['status'], 'waiting_merge_capacity')
            self.assertTrue(coverage['source_analysis_complete'])
            self.assertFalse(coverage['merge_ready'])
            self.assertFalse(coverage['business_memory_applied'])
        self.assertEqual(len(calls), 1)
        cached = json.loads(next((self.root/'cache').rglob('*.json')).read_text())
        self.assertEqual(cached['result'], value)

    def test_bad_item_rejects_whole_result_even_if_other_items_are_valid(self):
        value = {'observations': [{'quote': 'x', 'observation': 'valid'},
                                  {'quote': 'not in source', 'observation': 'invalid'}], 'uncertainties': []}
        with self.assertRaises(batching.BatchInputError) as error:
            aa.prepare(self.doc, self.root/'run', self.root/'cache', ['scope'], lambda *args: value)
        self.assertEqual(error.exception.code, 'attachment_analysis_evidence')
        self.assertFalse(list((self.root/'cache').rglob('*.json')))

    def test_long_reference_preserves_complete_contiguous_source(self):
        text = ('中文\t否定条件\r\n' * 10000)
        segments = aa.reference_segments(text)
        raw = {'observations': [{'source_span': [1, len(segments)], 'observation': '来源材料含条件'}], 'uncertainties': []}
        value, spans = aa.resolve_references(raw, text, segments)
        self.assertEqual(value['observations'][0]['quote'], text)
        self.assertEqual(spans[0]['end'], len(text))
        with self.assertRaises(batching.BatchInputError):
            aa.validate(value, text[:-1] + '错')

    def test_long_quotes_overflow_only_merge_capacity_and_survive_restart(self):
        value = {'observations': [{'quote': self.text, 'observation': str(i)} for i in range(30)], 'uncertainties': []}
        calls = []
        def invoke(*args):
            calls.append(1)
            return value
        for run in ('long', 'restartlong'):
            with self.assertRaises(batching.BatchInputError) as ctx:
                aa.prepare(self.doc, self.root/run, self.root/'longcache', ['scope'], invoke)
            self.assertEqual(ctx.exception.code, 'attachment_analysis_merge_capacity')
        self.assertEqual(len(calls), 1)
        cached = json.loads(next((self.root/'longcache').rglob('*.json')).read_text())
        self.assertEqual(cached['result'], value)

    def test_current_prompt_has_no_fixed_total_or_item_limit(self):
        self.assertNotIn('20000', aa.CAPACITY_REFERENCE_PROMPT)
        self.assertNotIn('最多12', aa.CAPACITY_REFERENCE_PROMPT)
        self.assertNotIn('不超过2000字符', aa.CAPACITY_REFERENCE_PROMPT)
        self.assertIn('较长引用仍完整保存', aa.CAPACITY_REFERENCE_PROMPT)


class LocalReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        router.isolate_for_tests(self.root)
        self.config = {'email_sync': {'mailbox': 'test', 'model_timeout_seconds': 1},
                       'model_routing': {'persistent_guard': True}}
        self.text = 'x' * 30000
        self.rows = [{'message_key': 'synthetic'}]
        self.doc = {'messages': [{'attachments': [{'name': 'x.txt', 'extracted_text': self.text}]}]}
        self.scope = ledger.digest([ledger.digest(['test', 'synthetic']), 'x.txt', None])
        self.binding = {'scope': self.scope, 'text_sha256': ledger.digest(self.text), 'start': 0, 'end': len(self.text),
                        'chunk_sha256': ledger.digest(self.text), 'original_text_sha256': ledger.digest(self.text),
                        'contract': aa.CONTRACT}
        self.context = {'scope': self.scope, 'source_version': ledger.digest(self.binding), 'contract': aa.CONTRACT}
        self.value = {'observations': [{'source_span': [1, 4], 'observation': '支持' + str(i)} for i in range(49)],
                      'uncertainties': ['未批准']}

    def seed(self, protocol='old', *, uncertain=False, incomplete=False, bad=False):
        segments = aa.reference_segments(self.text)
        payload = '\n' + ledger.canonical({'reference_version': aa.REFERENCE_VERSION, 'range': [0, len(self.text)],
                    'source_segments': [{'id': s['id'], 'text': s['text']} for s in segments]})
        if protocol == 'legacy':
            prompt = aa.PROMPT + '\n' + ledger.canonical({'range': [0, len(self.text)], 'text': self.text})
            raw = {'observations': [{'quote': 'x'*1600, 'observation': str(i)} for i in range(49)], 'uncertainties': []}
        else:
            prompt = (aa.BOUNDED_REFERENCE_PROMPT if protocol == 'bounded' else aa.LEGACY_CAPACITY_REFERENCE_PROMPT if protocol == 'oldcapacity' else aa.REFERENCE_PROMPT) + payload
            raw = copy.deepcopy(self.value)
        if bad:
            raw['observations'][-1] = {'source_span': [99999, 99999], 'observation': 'bad'}
        admission = ledger.Admission(self.root/'model-calls', prompt, [], 'email_attachment_analysis', self.context)
        with admission as guard:
            aid = guard.begin(0, 'mimo', 'synthetic')
            guard.received(aid, router.ProviderReply(ledger.canonical(raw), finish_error='empty' if incomplete else None))
            guard.settle(aid, 'uncertain' if uncertain else 'invalid_output')
            guard.status('uncertain' if uncertain else 'exhausted')
        return admission, aid

    def history(self):
        with sqlite3.connect(self.root/'model-calls/usage.sqlite') as connection:
            return (connection.execute('SELECT * FROM operations').fetchall(),
                    connection.execute('SELECT * FROM attempts').fetchall(),
                    connection.execute('SELECT * FROM events').fetchall())

    def run_local(self, run='run'):
        with patch.object(router, '_call_http', side_effect=AssertionError('no paid calls')), \
             patch.object(router, 'complete', side_effect=AssertionError('no router history changes')), \
             patch.object(sync, 'AI_LOCK', self.root/'ai.lock'):
            return aa.prepare_email(self.config, 'task', self.doc, self.root/run, self.rows, allow_provider_calls=False)

    def test_old_reference_reply_recovered_without_history_change(self):
        admission, _ = self.seed()
        before = self.history()
        self.run_local()
        self.run_local('restart')
        self.assertEqual(self.history(), before)
        proof = json.loads((self.root/'run/reference-receipts'/(admission.key+'.json')).read_text())
        self.assertEqual(proof['request_sha256'], admission.request_sha)
        self.assertEqual(proof['saved_response']['historical_attempt_status'], 'invalid_output')
        cached = json.loads(next((self.root/'attachment-analysis-v1').rglob('*.json')).read_text())
        self.assertEqual(len(cached['result']['observations']), 49)
        self.assertGreater(len(ledger.canonical(cached['result'])), 20000)

    def test_old_capacity_request_long_quote_recovered_with_frozen_request(self):
        self.value = {'observations': [{'source_span': [1, len(aa.reference_segments(self.text))], 'observation': '范围说明'}], 'uncertainties': []}
        admission, _ = self.seed('oldcapacity')
        before = self.history()
        self.run_local('longold')
        self.run_local('longoldrestart')
        self.assertEqual(before, self.history())
        proof = json.loads((self.root/'longold/reference-receipts'/(admission.key+'.json')).read_text())
        self.assertEqual(proof['request_sha256'], admission.request_sha)
        cached = json.loads(next((self.root/'attachment-analysis-v1').rglob('*.json')).read_text())
        self.assertEqual(cached['result']['observations'][0]['quote'], self.text)

    def test_bounded_request_stays_frozen_but_all_valid_observations_survive(self):
        admission, _ = self.seed('bounded')
        before = self.history()
        self.run_local()
        self.assertEqual(self.history(), before)
        proof = json.loads((self.root/'run/reference-receipts'/(admission.key+'.json')).read_text())
        self.assertEqual(proof['request_sha256'], admission.request_sha)
        self.assertEqual(proof['budget_contract'], aa.BUDGET_CONTRACT)
        self.assertEqual(len(proof['spans']), 49)

    def test_quote_only_reply_recovered_without_forging_reference_selectors(self):
        self.seed('legacy')
        before = self.history()
        self.run_local()
        self.assertEqual(self.history(), before)
        cached = json.loads(next((self.root/'attachment-analysis-v1').rglob('*.json')).read_text())
        self.assertNotIn('reference_receipt', cached)
        self.assertIn('local_revalidation_receipt', cached)

    def test_invalid_quote_is_never_repaired_by_dropping_item(self):
        self.seed(bad=True)
        before = self.history()
        with self.assertRaises(batching.BatchInputError) as error:
            self.run_local()
        self.assertEqual(error.exception.code, 'attachment_analysis_saved_reply_unavailable')
        self.assertEqual(self.history(), before)
        self.assertFalse(list((self.root/'attachment-analysis-v1').rglob('*.json')))

    def test_uncertain_reply_is_not_assumed_completed(self):
        self.seed(uncertain=True)
        before = self.history()
        with self.assertRaises(batching.BatchInputError):
            self.run_local()
        self.assertEqual(self.history(), before)

    def test_incomplete_response_is_not_accepted(self):
        self.seed(incomplete=True)
        with self.assertRaises(batching.BatchInputError):
            self.run_local()

    def test_response_tampering_fails_before_any_call(self):
        _, aid = self.seed()
        path = self.root/'model-calls/responses'/(aid+'.json')
        obj = json.loads(path.read_text())
        obj['text'] += ' '
        path.write_text(json.dumps(obj))
        before = self.history()
        with self.assertRaises(batching.BatchInputError) as error:
            self.run_local()
        self.assertEqual(error.exception.code, 'model_guard_cache_corrupt')
        self.assertEqual(self.history(), before)

    def test_missing_reply_never_creates_a_paid_operation_in_local_mode(self):
        with self.assertRaises(batching.BatchInputError):
            self.run_local()
        self.assertFalse((self.root/'model-calls/usage.sqlite').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
