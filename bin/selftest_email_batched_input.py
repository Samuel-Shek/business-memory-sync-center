
from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import copy
import json
import tempfile
import unittest
from pathlib import Path

import email_batched_input as b


class BatchTests(unittest.TestCase):
    def doc(self, text, complete=False):
        return {'batch_id': 'source', 'messages': [{'source_token': 'S001', 'body': 'mail',
                'attachments': [{'name': 'report.xlsx', 'extracted_text': text,
                                 'full_read_confirmed': complete, 'extract_status': 'incomplete_content'}]}],
                'current_memory': [], 'open_claims': []}

    def prepare(self, doc, root, invoke):
        def write(p, d):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(d))
        def read(p, default):
            return json.loads(p.read_text()) if p.exists() else default
        def validate(raw, _):
            if raw != {'source_summary': 'seen'}:
                raise ValueError('invalid proposal')
            return raw
        return b.prepare(doc, root, invoke, validate, write, read,
                         lambda d: b.sha(json.dumps(d)), 'test-contract')

    def test_exact_threshold_and_no_splitting_to_evade_approval(self):
        b.require_file_permission('x' * 10_000_000, 'f')
        with self.assertRaisesRegex(b.BatchInputError, '10000001'):
            b.require_file_permission('x' * 10_000_001, 'f')

    def test_chunk_coverage_is_exact_with_unicode_and_newlines(self):
        text = ('中😀\nabc\n' * 121001)
        chunks = list(b.text_chunks(text))
        self.assertEqual(''.join(x[2] for x in chunks), text)
        self.assertEqual(chunks[0][0], 0)
        self.assertEqual(chunks[-1][1], len(text))

    def test_resume_reuses_validated_fragments_and_preserves_source_limits(self):
        text = ('a12345\n' * 150001)
        doc = self.doc(text)
        before = copy.deepcopy(doc)
        seen = []
        def first(_, child):
            if seen:
                raise RuntimeError('simulated interruption')
            seen.append(child['messages'][0]['attachments'][0]['extracted_text'])
            return {'source_summary': 'seen'}
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            with self.assertRaisesRegex(RuntimeError, 'interruption'):
                self.prepare(doc, root, first)
            def resume(_, child):
                self.assertLessEqual(b.serialized_size(child), b.REQUEST_BUDGET)
                seen.append(child['messages'][0]['attachments'][0]['extracted_text'])
                return {'source_summary': 'seen'}
            result = self.prepare(doc, root, resume)
            self.assertEqual(''.join(seen), text)
            self.assertEqual(doc, before)
            self.assertFalse(result['messages'][0]['attachments'][0]['full_read_confirmed'])
            coverage = json.loads((root/'batch-coverage.json').read_text())
            self.assertEqual(coverage['status'], 'complete')
            self.assertEqual(sum(c['chars'] for c in coverage['files'][0]['chunks']), len(text))
            self.prepare(doc, root, lambda *_: self.fail('cached run must not invoke model'))

    def test_small_input_is_unchanged_and_no_model_called(self):
        doc = self.doc('normal text')
        with tempfile.TemporaryDirectory() as t:
            result = self.prepare(doc, Path(t), lambda *_: self.fail('unneeded model call'))
            self.assertIs(result, doc)


if __name__ == '__main__':
    unittest.main()
