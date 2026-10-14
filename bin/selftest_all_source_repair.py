"""Regression cases for source relocation, batching, and reusable mail proposals."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
import io
import zipfile
import threading
import time
from pathlib import Path
from unittest.mock import Mock, patch

import email_sync as mail
import static_attachment_review as attachments
import business_page_router as router
import receiver
import win11_memory_automation as win11
import listener_diagnostic as diagnostic
import dingtalk_conflict_recovery as recovery
import dingtalk_memory_automation as memory
from listener_retry_budget import BudgetBlocked
from selftest_email_sync import temporary_config

HERE = Path(__file__).resolve().parent


class Repairs(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="all-source-repair-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def ruby(self, module, expression, payload):
        result = subprocess.run(["/usr/bin/ruby", "-r", "json", "-r", str(HERE / module), "-e", expression],
                                input=json.dumps(payload), text=True, capture_output=True)
        if result.returncode:
            raise RuntimeError(result.stderr)
        return json.loads(result.stdout)

    def inventory(self, old, current):
        memory = self.root / "vault/00_业务记忆库"
        memory.mkdir(parents=True, exist_ok=True)
        path = self.root / "ledger/inventory.json"
        base = {"schema_version": 2, "records": [], "pruned_roots": ["00_业务记忆库"], "generated_at": "2020-01-02T00:00:00+08:00"}
        previous = {**base, "records": old, "record_count": len(old)}
        current = {**base, "records": current, "record_count": len(current)}
        result = self.ruby("source_inventory_maintenance.rb", "d=JSON.parse(STDIN.read); puts JSON.generate(SourceInventoryMaintenance.apply(*d))",
                           [str(memory.parent), str(memory), str(path), previous, current])
        return result, path

    def record(self, name, data=b"original"):
        return {"root": "vault", "path": name, "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data), "modified_at": "2020-01-02T00:00:00+08:00", "change_state": "unchanged"}

    def test_relocation_preserves_body_and_noop_preserves_mtime(self):
        vault = self.root / "vault"
        memory = vault / "00_业务记忆库"
        memory.mkdir(parents=True)
        (vault / "new.txt").write_bytes(b"original")
        body = "\nBody is unchanged.\n"
        note = memory / "note.md"
        note.write_text('---\nsource_ref: ' + json.dumps(str(vault / "old.txt")) + '\n---\n' + body)
        old, new = self.record("old.txt"), self.record("new.txt")
        result, path = self.inventory([old], [new])
        self.assertEqual(result["references_updated"], 1)
        self.assertIn(str(vault / "new.txt"), note.read_text())
        self.assertEqual(note.read_text().split("\n---\n", 1)[1], body)
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in [path, note]}
        result, _ = self.inventory([new], [new])
        self.assertEqual(result["status"], "no_change")
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before})

    def test_missing_is_retained_outside_current_snapshot(self):
        result, path = self.inventory([self.record("missing.xlsx")], [])
        self.assertEqual(result["missing"], 1)
        self.assertEqual(json.loads(path.read_text())["records"], [])
        status = json.loads(path.with_name("source-inventory-status.json").read_text())
        self.assertEqual(status["missing"][0]["path"], "missing.xlsx")
        self.assertEqual(len(list(path.parent.glob("source-inventory-history/*.json"))), 1)

    def test_ambiguous_duplicate_does_not_relocate(self):
        vault = self.root / "vault"
        vault.mkdir()
        for name in ["a.txt", "b.txt"]:
            (vault / name).write_bytes(b"original")
        result, path = self.inventory([self.record("missing.txt")], [self.record("a.txt"), self.record("b.txt")])
        self.assertEqual(result["relocated"], 0)
        self.assertEqual(json.loads(path.with_name("source-inventory-status.json").read_text())["missing"][0]["reason"], "ambiguous_relocation")

    def test_resume_refuses_concurrent_user_change(self):
        target = self.root / "target"
        target.write_text("user change")
        journal = self.root / "journal.json"
        journal.write_text(json.dumps({"state": "prepared", "writes": [{"path": str(target),
                          "before_sha256": hashlib.sha256(b"before").hexdigest(),
                          "after_sha256": hashlib.sha256(b"after").hexdigest(), "after": "after"}]}))
        with self.assertRaisesRegex(RuntimeError, "inventory_concurrent_change"):
            self.ruby("source_inventory_maintenance.rb", "SourceInventoryMaintenance.resume(JSON.parse(STDIN.read)); puts '{}'", str(journal))
        self.assertEqual(target.read_text(), "user change")

    def test_batch_keeps_source_groups_and_unchanged_limits(self):
        items = [{"source_entry_id": str(i), "analysis_required": True, "size_bytes": size,
                  "covered_event_ids": [str(i), str(i) + "a"]} for i, size in enumerate([3, 3, 6])]
        items.append({"source_entry_id": "meta", "analysis_required": False, "size_bytes": 0})
        result = self.ruby("win11_batch_selection.rb", "d=JSON.parse(STDIN.read); puts JSON.generate(Win11BatchSelection.select(*d))", [items, 25, 5])
        self.assertEqual(result["content_items"], 1)
        self.assertEqual(result["total_bytes"], 3)
        self.assertEqual(result["items"][0]["covered_event_ids"], ["0", "0a"])
        self.assertEqual(len(result["remaining"]), 1)
        self.assertEqual(len(result["oversized"]), 1)
        self.assertEqual(result["items"][-1]["source_entry_id"], "meta")

    def test_win11_model_only_reads_content_but_full_batch_still_validates(self):
        content = {"event_id": "content", "source_entry_id": "source", "operation": "upsert",
                   "relative_path": "fixture.txt", "analysis_required": True, "current_sha256": "0"*64}
        batch = {"run_id": "fixture", "source_id": "test", "items": [content,
                 {"event_id": "deleted", "analysis_required": False, "current_sha256": None},
                 {"event_id": "metadata", "analysis_required": False, "current_sha256": "1"*64}]}
        with patch.object(win11.reconciliation, "open_claim_context", return_value=[]):
            payload = win11.build_payload(batch)
        self.assertEqual([item['event_id'] for item in payload['items']], ['content'])
        self.assertEqual(payload['required_source_results_count'], 1)
        proposal = {"run_id": "fixture", "new_notes": [], "source_results": [{
            "event_id": "content", "disposition": "no_business_change", "new_note_refs": [],
            "existing_target_memory_ids": [], "note": "No new claim."}]}
        full = win11.include_administrative_results(batch, proposal)
        win11.validate_ai(batch, full)
        self.assertEqual(len(proposal['source_results']), 1)
        self.assertEqual(full['source_results'][1]['disposition'], 'tombstone_recorded')
        full['source_results'] = full['source_results'][1:]
        with self.assertRaisesRegex(win11.AutomationError, 'exactly once'):
            win11.validate_ai(batch, full)

    def proposal(self):
        return {"batch_id": "batch", "no_business_change": False,
                "source_summary": {"title": "Source note", "summary": "A draft is available.",
                                   "evidence_boundary": "Unverified communication.", "source_tokens": ["S001"]},
                "candidates": [], "claim_relations": [],
                "project_updates": [{"name": "Example", "progress": "Draft available", "status": "source_observed", "source_tokens": ["S001"]}]}

    def test_proposal_second_validation_keeps_evidence(self):
        config = temporary_config(self.root)
        document = {"batch_id": "batch", "messages": [{"source_token": "S001"}], "open_claims": []}
        with patch.object(mail.memory, "memory_records", return_value=[]):
            first = mail.validate_email_proposal(config, self.proposal(), document)
            second = mail.validate_email_proposal(config, first, document)
            self.assertEqual(first, second)
            del first["project_updates"][0]["source_tokens"]
            with self.assertRaises(mail.EmailSyncError):
                mail.validate_email_proposal(config, first, document)

    def test_email_raw_cache_survives_validation_failure_and_rejects_other_source(self):
        config = temporary_config(self.root)
        config['email_sync']['codex_bin'] = '/usr/bin/true'
        document = {'batch_id': 'fixture', 'mailbox_scope': 'enterprise_only',
                    'source_contract': 'fixture', 'messages': [{'source_token': 'S001'}],
                    'open_claims': [{'claim_handle': 'claim-'+'1'*20}]}
        raw = {'batch_id': 'fixture', 'bad_proposal': True}
        with patch.object(mail.memory, 'acquire_lock', return_value=Mock()), \
             patch.object(mail.subprocess, 'run', return_value=Mock(returncode=0, stdout=json.dumps(raw), stderr='')):
            self.assertEqual(mail.invoke_email_model(config, 'fixture', document), raw)
        run = mail.runtime_paths(config)['runs']/'fixture'
        cached = json.loads((run/'model/response.json').read_text())
        self.assertEqual(cached['response'], raw)
        self.assertEqual((run/'model/response.json').stat().st_mode & 0o777, 0o600)
        schema = json.loads((run/'model/output-schema.json').read_text())
        self.assertEqual(schema['properties']['claim_relations']['items']['properties']['claim_handle']['enum'],
                         ['claim-'+'1'*20])
        group = {'input_sha256': mail.semantic_input_sha(document)}
        with patch.object(mail, 'semantic_rows', return_value=[]), \
             patch.object(mail, 'build_semantic_document', return_value=(document, group['input_sha256'])), \
             patch.object(mail, 'validate_email_proposal', side_effect=mail.EmailSyncError('proposal_shape', 'fixture')), \
             patch.object(mail, 'invoke_email_model') as model:
            with self.assertRaises(mail.EmailSyncError):
                mail._process_semantic_group(config, group, semantic_id='fixture', handoff_receipt={})
            cached['input_sha256'] = 'different'
            (run/'model/response.json').write_text(json.dumps(cached))
            with self.assertRaisesRegex(mail.EmailSyncError, 'different source input'):
                mail._process_semantic_group(config, group, semantic_id='fixture', handoff_receipt={})
            model.assert_not_called()

    def test_diagnostic_is_one_session_and_cleans_partial_start(self):
        children = []
        class Consumer:
            def __init__(child, config, kind, *args):
                child.kind = kind; child.threads = []; child.stderr_lines = []; child.pre_ready = []
                child.ready = threading.Event(); child.subscription_ref = None
                child.process = Mock(); child.process.poll.return_value = None
                children.append(child)
            def start_with_budget(child):
                if child.kind == 'all-group':
                    raise BudgetBlocked('fixture_provider_refused')
                child.ready.set()
            def shutdown(child):
                child.process.poll.return_value = 0
        with patch.object(diagnostic.manual, 'directory', return_value=self.root), \
             patch.object(diagnostic.manual, 'legacy_path', side_effect=lambda config, kind: self.root/kind), \
             patch.object(diagnostic.manual, 'binding', return_value={}), \
             patch.object(diagnostic.manual, 'ManualBudget'), \
             patch.object(memory, 'acquire_lock', return_value=Mock()), \
             patch.object(memory, 'EventConsumer', Consumer):
            row = diagnostic.diagnose({}, 'authorized fixture')
            self.assertEqual(row['state'], 'failed')
            self.assertTrue(row['cleanup_complete'])
            self.assertTrue(row['scopes']['all-direct']['ready'])
            self.assertFalse(row['scopes']['all-group']['ready'])
            with self.assertRaises(BudgetBlocked):
                diagnostic.diagnose({}, 'authorized fixture')
            self.assertEqual(len(children), 2)
            original = (self.root / 'listener-diagnostic.json').read_bytes()
            extra = diagnostic.diagnose({}, 'one additional authorization', additional=True)
            self.assertEqual(extra['phase'], 'additional_diagnostic')
            self.assertEqual((self.root / 'listener-diagnostic.json').read_bytes(), original)
            self.assertTrue(extra['cleanup_complete'])
            with self.assertRaises(BudgetBlocked):
                diagnostic.diagnose({}, 'one additional authorization', additional=True)
            self.assertEqual(len(children), 4)

    def test_diagnostic_redacts_private_transport_and_keeps_plain_error(self):
        result = diagnostic.sanitized_stderr([
            'Error: event bus failed: permission denied',
            'Authorization: Bearer extremely-sensitive-value',
            'access_token=private-secret',
            'payload={"message":"private chat"}',
            'connect https://example.invalid?token=private',
            _archive_path('/Users/example/.dws/events/ABCDEFGHIJKLMNOPQRSTUVWX failed'),
            'contact fixture_0de1290b@example.invalid',
        ])
        output = '\n'.join(result)
        self.assertIn('permission denied', output)
        for secret in ('extremely-sensitive', 'private-secret', 'private chat', 'private@example', 'ABCDEFGHIJKLMNOPQRSTUVWX', '/Users/example'):
            self.assertNotIn(secret, output)

    def test_additional_diagnostic_preserves_provider_hold(self):
        with patch.object(diagnostic.manual, 'directory', return_value=self.root):
            (self.root / 'listener-diagnostic.json').write_text(json.dumps({'action_id':'old'}))
            (self.root / 'listener-diagnostic-supplement-01.json').write_text(json.dumps({'action_id':'extra'}))
            (self.root / 'listener-manual-extra-all-direct.json').write_text(json.dumps({'guard':'terminal_hold'}))
            self.assertTrue(diagnostic.manual.diagnostic_provider_hold({}))

    def test_event_runtime_private_and_consistent_for_status(self):
        with tempfile.TemporaryDirectory(prefix='evt-', dir='/tmp') as directory:
            root = Path(directory) / 'run'
            config = {'dingtalk_automation':{'event_runtime_root':str(root)}}
            previous = os.environ.get('XDG_RUNTIME_DIR')
            environment = memory.event_environment(config)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(environment['XDG_RUNTIME_DIR'], str(root))
            self.assertEqual(os.environ.get('XDG_RUNTIME_DIR'), previous)
            with patch.object(memory, 'dws_binary', return_value=Path('/fixture/dws')), \
                 patch.object(memory.subprocess, 'run', return_value=Mock(returncode=0,stdout='{}')) as command:
                memory.query_event_status(config)
                self.assertEqual(command.call_args.kwargs['env']['XDG_RUNTIME_DIR'], str(root))
            root.chmod(0o755)
            with self.assertRaisesRegex(memory.AutomationError, 'mode 0700'):
                memory.event_environment(config)
            self.assertEqual(root.stat().st_mode & 0o777, 0o755)

    def test_event_runtime_rejects_symlink_and_overlong_path(self):
        with tempfile.TemporaryDirectory(prefix='evt-', dir='/tmp') as directory:
            root = Path(directory); (root/'link').symlink_to(root, target_is_directory=True)
            for candidate in (root/'link', root/('x'*110)):
                with self.assertRaises(memory.AutomationError):
                    memory.event_environment({'dingtalk_automation':{'event_runtime_root':str(candidate)}})

    def test_plain_socket_permission_error_has_stable_code(self):
        result = memory.safe_event_failure(['Error: transport: socket runtime root is neither private nor sticky: /private/location'])
        self.assertEqual(result['error_codes'], ['event_socket_runtime_root_unsafe'])
        self.assertNotIn('/private/location', json.dumps(result))

    def test_static_inspection_preserves_protected_container(self):
        result = attachments.inspect(bytes.fromhex('887d1c') + b'encrypted bytes', '.xlsx')
        self.assertEqual(result['status'], 'protected_or_unknown_container')
        self.assertFalse(result['active_content_executed'])

    def test_static_office_skips_embedded_program_and_extracts_text(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.writestr('ppt/slides/slide1.xml', '<a:t>Source text</a:t>')
            archive.writestr('ppt/embeddings/evil.exe', b'MZ never execute')
        result = attachments.inspect(data.getvalue(), '.pptx')
        self.assertIn('Source text', result['text'])
        self.assertEqual(result['status'], 'partial_static')
        self.assertIn('embedded_content_not_opened', result['reasons'])

    def test_static_archive_refuses_traversal(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.writestr('../escape.txt', 'untrusted')
        self.assertEqual(attachments.inspect(data.getvalue(), '.zip')['status'], 'archive_path_risk')

    def test_batches_resume_in_order_without_splitting_events(self):
        items = [{'source_entry_id': str(i), 'analysis_required': True, 'size_bytes': 1,
                  'covered_event_ids': [str(i)+'a', str(i)+'b']} for i in range(27)]
        expression = 'd=JSON.parse(STDIN.read); puts JSON.generate(Win11BatchSelection.select(*d))'
        first = self.ruby('win11_batch_selection.rb', expression, [items, 25, 5242880])
        second = self.ruby('win11_batch_selection.rb', expression, [first['remaining'], 25, 5242880])
        self.assertEqual(len(first['items']), 25)
        self.assertEqual(first['items']+second['items'], items)
        self.assertFalse(second['remaining'])

    def test_pending_navigation_stays_bounded_without_changing_claims(self):
        rows = [{'claim_id': 'BUSINESS-20260907-'+str(i), 'relative_path': 'long/directory/page.md',
                 'title': 'A source question '+str(i), 'risk': 'high', 'state': 'pending'} for i in range(700)]
        original = json.dumps(rows)
        result = router.pending_index_text(self.root, rows)
        self.assertLessEqual(len(result), 27000)
        self.assertIn('total_records: 700', result)
        self.assertIn('完整条目及来源仍保留', result)
        self.assertEqual(json.dumps(rows), original)

    def test_receiver_never_exposes_a_new_generation_before_its_index(self):
        inbox, watch = self.root/'inbox', self.root/'watch'
        inbox.mkdir(); watch.mkdir()
        (inbox/'business-memory-fixture.zip').write_bytes(b'fixture')
        started = threading.Event()
        observed = []
        indexed = {'value': False}
        def competing_writer():
            started.wait(2)
            with receiver.acquire_memory_write_lock():
                observed.append(indexed['value'])
        def ingest(*args):
            started.set()
            time.sleep(0.04)
            self.assertFalse(observed)
        def refresh(*args, **kwargs):
            self.assertTrue(kwargs['force'])
            indexed['value'] = True
        with patch.object(receiver, 'INBOX', inbox), patch.object(receiver, 'WATCH_INBOX', watch), \
             patch.object(receiver, 'MEMORY_WRITE_LOCK_PATH', self.root/'memory.lock'), \
             patch.object(receiver, 'process_bundle', side_effect=ingest), \
             patch.object(receiver, 'validate_watch_inbox_security'), patch.object(receiver, 'retry_acks'), \
             patch.object(receiver, 'refresh_dirty_indexes', side_effect=refresh):
            worker = threading.Thread(target=competing_writer)
            worker.start()
            self.assertTrue(receiver.drain_incoming(None))
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(observed, [True])

    def test_unchanged_failure_does_not_call_model_again(self):
        config = temporary_config(self.root)
        config["email_sync"]["max_model_calls_per_run"] = 3
        group = {"semantic_id": "sample", "input_sha256": "input", "last_error_code": "proposal_source"}
        connection = Mock()
        connection.execute.return_value.fetchall.return_value = [group]
        with patch.object(mail, "connect_database", return_value=connection), \
             patch.object(mail, "mark_semantic_failure"), \
             patch.object(mail, "process_semantic_group", side_effect=mail.EmailSyncError("proposal_source", "invalid")) as worker:
            first = mail.process_pending_semantics(config)
            second = mail.process_pending_semantics(config)
        self.assertEqual(first["failed"], 1)
        self.assertEqual(second["suppressed_unchanged"], 1)
        self.assertEqual(worker.call_count, 1)

    def test_conflict_recovery_only_accepts_exact_preserved_media_reference(self):
        refs = json.dumps([{'type': 'mediaId', 'resourceId': 'fixture'}])
        self.assertEqual(recovery.source_placeholder_kind('[图片消息](mediaId=fixture)', refs),
                         'media_reference_only_content_not_inferred')
        self.assertIsNone(recovery.source_placeholder_kind('[图片消息](mediaId=other)', refs))
        self.assertIsNone(recovery.source_placeholder_kind('[图片消息](mediaId=fixture) confirmed business', refs))
        self.assertIsNone(recovery.source_placeholder_kind('ordinary business text', refs))

    def test_mail_waits_for_validation_lock_and_never_bypasses_it(self):
        lock = Mock()
        with patch.object(mail.memory, 'acquire_lock', side_effect=[memory.AutomationError('already_running', 'fixture'), lock]), \
             patch.object(mail.time, 'sleep'), patch.object(mail, 'maintenance_active', return_value=False), \
             patch.object(mail, 'apply_email_proposal_locked', return_value={'writes': 1}) as apply:
            self.assertEqual(mail.apply_email_proposal({}, 'id', 'group', 'ref', [], {'no_business_change': False}), {'writes': 1})
            self.assertEqual(apply.call_count, 1)
            lock.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
