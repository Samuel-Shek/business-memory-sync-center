#!/usr/bin/env python3
"""Isolated remaining-repair checks. No live accounts, notifications or models."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

import dingtalk_memory_automation as memory
import dingtalk_batch_contract as contract
import dingtalk_conflict_recovery as recovery
import claim_reconciliation as relations
import business_sync_state as state
import business_sync_supervisor as supervisor
import business_sync_supervisor_health as health
import supervisor_incidents as incidents
import sqlite_read_diagnostics as diagnostics
import listener_manual_acceptance as manual
from listener_retry_budget import BudgetBlocked
from selftest_dingtalk_memory_automation import insert_message, make_note


class Crash(BaseException):
    pass


class Temporary(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="remaining-sync-repair-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def patched(self, target, name, **kwargs):
        item = patch.object(target, name, **kwargs)
        value = item.start()
        self.addCleanup(item.stop)
        return value


class DatabaseTests(Temporary):
    def test_wal_read_is_query_only_and_keeps_main_database(self):
        database = self.root / "sync.sqlite"
        state.initialize(database)
        state.open_alert("system", "test", "test", "test", database=database)
        before = database.read_bytes()
        self.assertEqual(len(state.alerts(database)), 1)
        connection = state._connect(database, readonly=True)
        try:
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM sync_alerts")
        finally:
            connection.close()
        self.assertEqual(before, database.read_bytes())

    def test_busy_has_three_total_attempts(self):
        connect = Mock(side_effect=sqlite3.OperationalError("database is locked"))
        sleep = Mock()
        with self.assertRaises(sqlite3.Error) as caught:
            diagnostics.read_rows(self.root / "db", "SELECT 1", (), connector=connect, sleeper=sleep)
        self.assertEqual(connect.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(caught.exception.safe_sqlite_evidence["attempt"], 3)

    def test_cannot_open_and_corruption_never_retry_or_return_empty(self):
        for message in ("unable to open database file", "database disk image is malformed", "arbitrary private failure"):
            with self.subTest(message=message):
                connect = Mock(side_effect=sqlite3.OperationalError(message))
                with self.assertRaises(sqlite3.Error) as caught:
                    diagnostics.read_rows(self.root / "db", "SELECT 1", (), connector=connect, sleeper=Mock())
                self.assertEqual(connect.call_count, 1)
                self.assertNotIn("arbitrary private", json.dumps(caught.exception.safe_sqlite_evidence))

    def test_history_remains_open_when_new_runtime_is_idle(self):
        database = self.root / "sync.sqlite"
        state.initialize(database)
        stamp = state.now_iso()
        state.update_runtime(database, status="idle", heartbeat_at=stamp)
        config = self.root / "config.json"
        config.write_text("{}")
        (self.root / "state").mkdir()
        historic = {"recorded_at": stamp, "error_type": "OperationalError", "reason": "sqlite_database_unavailable",
                    "frames": [{"file": "bin/business_sync_state.py", "function": "alerts", "line": 1}], "secret": "must-not-copy"}
        (self.root / "state/sync-supervisor-errors.jsonl").write_text(json.dumps(historic) + "\n")
        kwargs = {"database": database, "maintenance_path": self.root / "not-maintenance.json",
                  "service_probe": lambda: {"loaded": True, "last_exit_code": 0}, "delivery": lambda *a: 0}
        first = health.run_once(config, **kwargs)
        second = health.run_once(config, **kwargs)
        self.assertEqual((first["status"], second["status"]), ("attention", "attention"))
        self.assertEqual(incidents.unresolved(self.root), 1)
        self.assertEqual(len(state.alerts(database, status="open")), 1)
        self.assertNotIn("must-not-copy", next(incidents.inbox(self.root).glob("*.json")).read_text())

    def test_sentinel_db_down_keeps_independent_durable_record(self):
        config = self.root / "config.json"
        config.write_text("{}")
        delivery = Mock()
        with patch.object(state, "initialize", side_effect=sqlite3.OperationalError("unable to open database file")):
            result = health.run_once(config, database=self.root / "sync.sqlite", maintenance_path=self.root / "off", delivery=delivery)
        self.assertEqual(result["notification_state"], "durably_pending")
        self.assertEqual(incidents.unresolved(self.root), 1)
        delivery.assert_not_called()

    def test_correction_requires_two_natural_current_code_cycles(self):
        (self.root / "bin").mkdir()
        for name in ("business_sync_supervisor.py", "business_sync_state.py", "sqlite_read_diagnostics.py", "supervisor_incidents.py",
                     "bounded_repair_actions.py", "dingtalk_sync_v2.py", "operational_workbench.py", "sqlite_write_transactions.py", "sqlite_attribution.py", "sqlite_lock_probe.py", "automatic_memory_validation.py", "validation_evidence.py", "validate_memory.rb", "validation_progress.py", "validation_process.py", "validation_progress.rb"):
            (self.root / "bin" / name).write_text("fixture")
        now = datetime.now(timezone.utc)
        incident = incidents.retain(self.root, {"recorded_at": (now - timedelta(minutes=10)).isoformat(),
                    "reason": "sqlite_database_unavailable", "frames": [{"function": "alerts"}]})
        database = self.root / "sync.sqlite"
        state.initialize(database)
        incidents.register_pending(self.root, database)
        proof = {"schema_version": 1, "reason": "wal_read_connection_corrected", "code_binding": incidents.code_binding(self.root),
                 "isolated_reproduction": True, "query_only_verified": True, "live_integrity_check": "ok", "evidence_sha256": "a" * 64,
                 "deployed_at": (now - timedelta(minutes=3)).isoformat(), "incident_ids": [incident["incident_id"]], "native_binary_sha256": "b" * 64}
        incidents.atomic_json(self.root / "state/supervisor-correction-proof.json", proof)
        rows = [{"checked_at": (now - timedelta(minutes=2)).isoformat(), "pid": 101, "ppid": 100,
                 "execution_owner": "launchd_native_supervisor", "native_binary_sha256": "b" * 64,
                 "attention_reconciliation": "passed", "code_binding": proof["code_binding"]}]
        receipt_path = self.root / "state/supervisor-attention-receipts.json"
        incidents.atomic_json(receipt_path, {"receipts": rows})
        self.assertEqual(incidents.verify_correction(self.root, database), 0)
        rows.append({**rows[0], "pid": 102, "checked_at": now.isoformat()})
        incidents.atomic_json(receipt_path, {"receipts": rows})
        self.assertEqual(incidents.verify_correction(self.root, database), 1)
        self.assertEqual(incidents.unresolved(self.root), 0)
        self.assertEqual(incidents.verify_correction(self.root, database), 0)


class ChatFixture(Temporary):
    def setUp(self):
        super().setUp()
        self.config = {"schema_version": 2, "memory_root": str(self.root / "vault/00_业务记忆库"), "external_root": str(self.root / "external"),
                       "dingtalk_chat": {"dws_bin": str(self.root / "dws"), "max_text_chars": 20000},
                       "dingtalk_automation": {"scope": "all_visible_direct_and_group_chats", "max_batch_messages": 100,
                                               "max_batch_chars": 30000, "context_messages": 0}}
        self.paths = memory.runtime_paths(self.config)
        for name in ("01_关键业务认知", "02_内部业务资料", "03_外部沟通", "04_对话来源摘要", "90_待确认", "99_规则与模板"):
            (self.paths["memory_root"] / name).mkdir(parents=True)
        (self.root / "dws").write_text("fixture executable, never run")
        (self.root / "dws").chmod(0o700)
        (self.paths["memory_root"] / "01_关键业务认知/fixture.md").write_text(make_note("BUSINESS-20260903-001", "合成测试上下文"))
        self.patched(memory, "MEMORY_LOCK", new=self.root / "memory.lock")
        self.patched(memory, "HOURLY_LOCK", new=self.root / "hourly.lock")
        self.patched(relations, "open_claim_context", return_value=[])
        self.patched(relations, "claim_context_hashes", return_value={})
        self.validation = self.patched(memory, "run_refresh_and_validate", return_value={"validator": {"verdict": "pass"}, "validation_attempts": 1})
        self.relation = self.patched(relations, "submit_relations", return_value={"status": "noop", "recorded": 0})

    def prepare(self):
        insert_message(self.config, "msg", "conv", "合成测试消息", "2020-01-02 23:00:00")
        batch = memory.prepare_batch(self.config)
        proposal = {"batch_id": batch["batch_id"], "source_summary": None, "candidates": [], "claim_relations": [], "no_business_change": True}
        return batch, proposal

    def queue_states(self):
        connection = memory.connect_database(self.config)
        try:
            return [row[0] for row in connection.execute("SELECT state FROM semantic_queue")]
        finally:
            connection.close()


class BatchTests(ChatFixture):
    def test_manifest_is_private_and_rejects_source_drift(self):
        batch, proposal = self.prepare()
        path = self.paths["runs"] / batch["batch_id"] / "input-manifest.json"
        self.assertEqual(path.stat().st_mode & 0o077, 0)
        memory.validate_proposal(self.config, batch["batch_id"], proposal)
        connection = memory.connect_database(self.config)
        connection.execute("UPDATE messages SET content_sha256='changed'")
        connection.commit()
        connection.close()
        with self.assertRaises(memory.AutomationError) as error:
            memory.validate_proposal(self.config, batch["batch_id"], proposal)
        self.assertEqual(error.exception.code, "proposal_source_drift")

    def test_unknown_duplicate_and_unknown_source_relations_are_distinct(self):
        document = {"open_claims": [{"claim_handle": "known"}]}
        row = {"claim_handle": "known", "relation": "supports", "source_tokens": ["S001"], "reason": "fixture"}
        for value, code in (([{**row, "claim_handle": "unknown"}], "proposal_claim_unknown"),
                            ([row, row], "proposal_relation_duplicate"),
                            ([{**row, "source_tokens": ["S999"]}], "proposal_relation_source"),
                            (None, "proposal_relation_shape")):
            with self.subTest(code=code), self.assertRaises(contract.ContractError) as caught:
                contract.check_relations(value, document, {"S001"})
            self.assertEqual(caught.exception.code, code)

    def test_dynamic_schema_binds_identifiers_and_empty_claims(self):
        batch, _proposal = self.prepare()
        schema = contract.output_schema(json.loads(memory.SCHEMA_PATH.read_text()), batch["document"])
        self.assertEqual(schema["properties"]["batch_id"]["enum"], [batch["batch_id"]])
        self.assertEqual(schema["properties"]["claim_relations"]["maxItems"], 0)

    def test_noop_has_full_validation_before_queue_completion(self):
        batch, proposal = self.prepare()
        def validate(*args, **kwargs):
            self.assertEqual(self.queue_states(), ["claimed"])
            return {"validator": {"verdict": "pass"}}
        self.validation.side_effect = validate
        result = memory.apply_proposal(self.config, batch, proposal)
        self.assertTrue(result["validated"])
        self.assertEqual(self.queue_states(), ["no_business_change"])

    def test_relation_deferred_never_completes_queue(self):
        batch, proposal = self.prepare()
        self.relation.return_value = {"status": "deferred", "error_code": "relation_stale"}
        with self.assertRaises(memory.AutomationError) as caught:
            memory.apply_proposal(self.config, batch, proposal)
        self.assertEqual(caught.exception.code, "proposal_relation_registration_pending")
        self.assertEqual(self.queue_states(), ["claimed"])

    def test_every_durable_commit_boundary_resumes_without_duplicate_file_write(self):
        for phase in ("prepared", "files_complete", "relations_complete", "validation_complete", "complete"):
            with self.subTest(phase=phase):
                message_id = str(uuid.uuid4())
                insert_message(self.config, message_id, "conv", "合成消息 " + phase, "2020-01-02 23:00:00")
                batch = memory.prepare_batch(self.config)
                proposal = {"batch_id": batch["batch_id"], "source_summary": None, "candidates": [], "claim_relations": [], "no_business_change": False}
                target = self.paths["memory_root"].parent / "公司资料/commit-fixture" / (phase + ".md")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("保留的现版\n")
                original = contract.save
                interrupted = False
                def save(path, value):
                    nonlocal interrupted
                    original(path, value)
                    if path.name == "commit-receipt.json" and value.get("phase") == phase and not interrupted:
                        interrupted = True
                        raise Crash()
                with patch.object(memory, "build_memory_changes", return_value=({target: "保留的现版\n缺项\n"}, [], [])) as build, \
                     patch.object(memory, "apply_changes", wraps=memory.apply_changes) as apply:
                    with patch.object(contract, "save", side_effect=save), self.assertRaises(Crash):
                        memory.apply_proposal(self.config, batch, proposal)
                    memory.apply_proposal(self.config, batch, proposal)
                    self.assertEqual(apply.call_count, 1)
                    self.assertEqual(build.call_count, 1)
                self.assertEqual(target.read_text(), "保留的现版\n缺项\n")
                self.assertNotIn("claimed", self.queue_states())

    def test_later_edits_are_not_overwritten_on_resume(self):
        batch, proposal = self.prepare()
        target = self.paths["memory_root"] / "02_内部业务资料/x.md"
        target.write_text("before")
        self.relation.return_value = {"status": "deferred"}
        with patch.object(memory, "build_memory_changes", return_value=({target: "applied"}, [], [])):
            with self.assertRaises(memory.AutomationError):
                memory.apply_proposal(self.config, batch, proposal)
        target.write_text("later user edit")
        with self.assertRaises(memory.AutomationError) as error:
            memory.apply_proposal(self.config, batch, proposal)
        self.assertEqual(error.exception.code, "proposal_commit_content_conflict")
        self.assertEqual(target.read_text(), "later user edit")


class ConflictTests(ChatFixture):
    def conflict(self):
        insert_message(self.config, "notice", "conv", "发到聊天里的文件已保存（合成自测）", "2020-01-02 23:00:00")
        insert_message(self.config, "file", "conv", "合成文件引用", "2020-01-02 23:00:01")
        batch = memory.prepare_batch(self.config)
        old = batch["batch_id"]
        proposal = {"batch_id": old, "source_summary": None, "no_business_change": False, "claim_relations": [],
                    "candidates": [{"action": "annotate_page", "category": "internal_material", "memory_kind": "status", "dedupe_key": "fixture-file-reference",
                                    "title": "合成文件引用", "summary": "仅补充一条合成文件引用。", "evidence_boundary": "没有核验文件正文或审批。",
                                    "source_tokens": ["S002"], "existing_memory_id": None, "conflict_reason": None}]}
        old_target = self.paths["memory_root"].parent / "公司资料/fixture/02_旧目录/doc.md"
        current = old_target.parent.parent / "20_现目录/doc.md"
        current.parent.mkdir(parents=True)
        current.write_text("后来继续编辑的真实正文（合成自测）。\n")
        index = old_target.parent.parent / "00_索引.md"
        index.write_text("| 当前入口 | [[20_现目录/doc.md]] |\n")
        run = self.paths["runs"] / old
        backup = run / "backup/business/doc.md"
        backup.parent.mkdir(parents=True)
        backup.write_text("旧正文")
        source_refs = memory.batch_source_refs(self.config, old)
        payload = {"schema_version": 2, "state": "recovery_conflict", "batch_id": old, "source_refs": source_refs,
                   "paths": [{"target": str(old_target), "backup": str(backup), "existed": True, "before_sha256": memory.sha256_path(backup),
                              "applied_sha256": "a" * 64, "recovery_status": "conflict_preserved"}]}
        contract.save(run / "apply_journal.json", payload)
        text = json.dumps(proposal, ensure_ascii=False) + "\n"
        (run / "proposal.json").write_text(text)
        connection = memory.connect_database(self.config)
        connection.execute("UPDATE semantic_batches SET status='failed',proposal_sha256=?,error_code='recovery_conflict_requires_review' WHERE batch_id=?", (memory.sha256_text(text), old))
        connection.execute("UPDATE semantic_queue SET state='deferred',last_error_code='recovery_conflict_requires_review' WHERE batch_id=?", (old,))
        connection.commit()
        connection.close()
        return old, run, backup, current, index

    def test_preserve_current_child_completes_both_sources_and_keeps_original_journal(self):
        old, run, _backup, current, _index = self.conflict()
        original, body = (run / "apply_journal.json").read_bytes(), current.read_text()
        result = recovery.run(self.config, str(uuid.uuid4()))
        self.assertEqual(result["messages"], 2)
        self.assertTrue(current.read_text().startswith(body))
        self.assertEqual((run / "apply_journal.json").read_bytes(), original)
        self.assertTrue(recovery.completion_verified(self.config, old))
        self.assertEqual(self.queue_states(), ["applied", "applied"])
        after = current.read_bytes()
        self.assertEqual(recovery.run(self.config, str(uuid.uuid4()))["status"], "noop")
        self.assertEqual(current.read_bytes(), after)

    def completion_fixture(self):
        old, run, _backup, _current, _index = self.conflict()
        recovery.run(self.config, str(uuid.uuid4()))
        current = datetime.now(timezone.utc) + timedelta(seconds=2)
        alert = {'alert_id': str(uuid.uuid4()), 'first_seen': (current - timedelta(minutes=10)).isoformat(),
                 'last_seen': (current - timedelta(minutes=5)).isoformat()}
        import automatic_memory_validation as validation
        import validation_evidence
        (self.root / 'state').mkdir(exist_ok=True)
        contract.save(self.root / 'state/last-memory-validation-success.json', {'recorded_at': current.isoformat()})
        self.patched(validation, 'index_state', return_value={'dirty': 7, 'applied': 7, 'ready': True})
        self.current_receipt = self.patched(validation_evidence, 'receipt_is_current', return_value=True)
        return old, run, current, alert

    def test_conflict_closure_requires_completion_and_current_validation(self):
        old, run, current, alert = self.completion_fixture()
        self.assertTrue(recovery.closure_evidence(self.config, alert, self.root, current)['current_validation'])
        self.current_receipt.return_value = False
        self.assertIsNone(recovery.closure_evidence(self.config, alert, self.root, current))
        self.current_receipt.return_value = True
        contract.save(self.root / 'state/last-memory-validation-failure.json', {'recorded_at': current.isoformat()})
        self.assertIsNone(recovery.closure_evidence(self.config, alert, self.root, current))

    def test_old_completion_or_changed_queue_cannot_close_new_conflict(self):
        old, run, current, alert = self.completion_fixture()
        self.assertIsNone(recovery.closure_evidence(self.config, {**alert, 'last_seen': current.isoformat()}, self.root, current))
        connection = memory.connect_database(self.config)
        connection.execute("UPDATE semantic_queue SET state='deferred'")
        connection.commit()
        connection.close()
        self.assertIsNone(recovery.closure_evidence(self.config, alert, self.root, current))

    def test_unreadable_index_does_not_close_or_crash_the_supervisor(self):
        old, run, current, alert = self.completion_fixture()
        import automatic_memory_validation as validation
        with patch.object(validation, 'index_state', side_effect=validation.ValidationFault('memory_index_state_unavailable')):
            self.assertIsNone(recovery.closure_evidence(self.config, alert, self.root, current))

    def test_corrupt_completion_cannot_close_conflict(self):
        old, run, current, alert = self.completion_fixture()
        path = run / 'preserve-current-result.json'
        result = json.loads(path.read_text())
        result['full_validation_passed'] = False
        contract.save(path, result)
        self.assertTrue(memory.recovery_conflicts(self.config))
        self.assertIsNone(recovery.closure_evidence(self.config, alert, self.root, current))

    def test_supervisor_closes_only_proven_conflict_once_and_retains_listener(self):
        old, run, current, fixture = self.completion_fixture()
        database = self.root / 'sync.sqlite'
        state.initialize(database)
        at = current - timedelta(minutes=5)
        alert = state.open_alert('dingtalk_chat', 'recovery_conflict', 'fixture', 'fixture', database=database, at=at)
        state.open_alert('dingtalk_chat', 'listener_failure', 'fixture', 'fixture', database=database, at=at)
        config_path = self.root / 'config.json'
        contract.save(config_path, self.config)
        service = supervisor.Supervisor(config_path, database=database)
        original = (run / 'apply_journal.json').read_bytes()
        service.reconcile_chat_recovery(self.config, current)
        service.reconcile_chat_recovery(self.config, current)
        opened = state.alerts(database, status='open')
        self.assertEqual([row['category'] for row in opened], ['listener_failure'])
        all_rows = state.alerts(database)
        self.assertEqual(len([row for row in all_rows if row['category'] == 'recovery']), 1)
        self.assertEqual((run / 'apply_journal.json').read_bytes(), original)
        receipt = json.loads((self.root / 'state/recovery-closure' / (alert['alert_id'] + '.json')).read_text())
        self.assertEqual(receipt['state'], 'resolved')

    def test_bad_backup_or_ambiguous_mapping_never_writes_current(self):
        old, _run, backup, current, index = self.conflict()
        body = current.read_text()
        index.write_text("| 历史 | [[20_现目录/doc.md]] |\n")
        with self.assertRaises(recovery.RecoveryError):
            recovery.approve_current(self.config, old, str(uuid.uuid4()))
        backup.write_text("broken")
        with self.assertRaises(recovery.RecoveryError):
            recovery.approve_current(self.config, old, str(uuid.uuid4()))
        self.assertEqual(current.read_text(), body)
        self.assertEqual(self.queue_states(), ["deferred", "deferred"])

    def test_orphan_approval_after_crash_is_repeatable_only_for_same_action(self):
        old, _run, _backup, _current, _index = self.conflict()
        action = str(uuid.uuid4())
        original = contract.save
        def save(path, value):
            original(path, value)
            if path.name == "preserve-current.json":
                raise Crash()
        with patch.object(contract, "save", side_effect=save), self.assertRaises(Crash):
            recovery.approve_current(self.config, old, action)
        with self.assertRaises(recovery.RecoveryError):
            recovery.approve_current(self.config, old, str(uuid.uuid4()))
        approved = recovery.approve_current(self.config, old, action)
        self.assertEqual(recovery.load_approval(self.config, old), approved)


class RelationRetryTests(Temporary):
    def setUp(self):
        super().setUp()
        self.patched(relations, "RELATION_OUTBOX", new=self.root / "outbox")
        self.patched(relations, "validate_write_scope")
        self.record = self.patched(relations, "record_relations", return_value={"recorded": 1, "resolved": 0})
        self.request = str(uuid.uuid4())
        self.kw = {"source_system": "dingtalk_chat", "source_material": {"S001": "safe fixture"},
                   "request_id": self.request, "observed_at": "2020-01-02T20:00:00+08:00"}
        self.rows = [{"claim_handle": "fixture", "relation": "supports", "source_tokens": ["S001"], "reason": "fixture"}]

    def test_delivered_request_reuses_receipt_without_reapplying(self):
        first = relations.submit_relations({}, self.rows, **self.kw)
        second = relations.submit_relations({}, self.rows, **self.kw)
        self.assertEqual(first, second)
        self.assertEqual(self.record.call_count, 1)

    def test_persistent_relation_error_is_not_retried_by_drain(self):
        self.record.side_effect = relations.ReconciliationError("relation_stale", "fixture")
        relations.submit_relations({}, self.rows, **self.kw)
        relations.submit_relations({}, self.rows, **self.kw)
        relations.drain_relation_outbox({})
        self.assertEqual(self.record.call_count, 1)

    def test_timeout_budget_is_shared_with_outbox_and_is_three_total(self):
        self.record.side_effect = relations.ReconciliationError("relation_timeout", "fixture")
        relations.submit_relations({}, self.rows, **self.kw)
        for _ in range(5):
            relations.drain_relation_outbox({})
        self.assertEqual(self.record.call_count, 3)


class ListenerTests(ChatFixture):
    def test_unknown_cause_does_not_consume_the_manual_attempt(self):
        self.assertFalse(manual.gate(self.config)["available"])
        with self.assertRaises(BudgetBlocked):
            manual.approve(self.config, str(uuid.uuid4()))
        self.assertFalse((manual.directory(self.config) / "listener-manual-acceptance.json").exists())

    def test_manual_attempt_is_independent_and_never_resets_legacy(self):
        legacy = manual.legacy_path(self.config, "all-direct")
        contract.save(legacy, {"schema_version": 1, "attempt_count": 2, "max_attempts": 2, "state": "failed"})
        before = legacy.read_bytes()
        proof = {"schema_version": 1, "cause_identified": True, "corrected": True, "verification": "read_only_preflight",
                 "before_error_code": "fixture_correctable_error", "binding": manual.binding(self.config), "corrected_at": manual.now(),
                 "diagnosis_sha256": "a" * 64, "correction_sha256": "b" * 64}
        contract.save(manual.directory(self.config) / "listener-cause-correction.json", proof)
        manual.approve(self.config, str(uuid.uuid4()))
        receipt = manual.take(self.config)
        budget = manual.ManualBudget(self.config, "all-direct", receipt)
        budget.reserve()
        budget.failed(True)
        budget.stopped()
        with self.assertRaises(BudgetBlocked):
            budget.reserve()
        self.assertIsNone(manual.take(self.config))
        self.assertEqual(legacy.read_bytes(), before)

    def test_diagnostics_do_not_save_error_prose_or_credentials(self):
        result = memory.safe_event_failure([json.dumps({"error": {"code": "event_transport_failure", "message": "sensitive secret text", "retryable": False}})])
        self.assertEqual(result["error_codes"], ["event_transport_failure"])
        self.assertNotIn("sensitive", json.dumps(result))

    def test_backfill_or_own_message_cannot_pass_both_live_scopes(self):
        ready_at = "2020-01-02T21:00:00+08:00"
        memory.status_update(self.config, listener_state="ready", direct_ready=True, group_ready=True, listener_ready_at=ready_at,
                             direct_subscription_ref=memory.sha256_text("direct"), group_subscription_ref=memory.sha256_text("group"))
        event_status = {"identity": {"user_id": "self"}, "subscriptions": [{"subscribe_id": "direct"}, {"subscribe_id": "group"}]}
        self.patched(memory, "query_event_status", return_value=event_status)
        insert_message(self.config, "backfill", "conv", "历史补同步", "2020-01-02 22:00:00")
        self.assertEqual(memory.update_live_acceptance(self.config)["status"], "live_acceptance_pending")
        for kind, sender in (("direct", "other"), ("group", "self")):
            memory.ingest_event(self.config, kind, {"type": "message", "event_id": kind, "message_id": kind,
                "conversation_id": kind, "sender_open_dingtalk_id": sender, "sender": "fixture", "content": "合成实时消息", "create_time": "2020-01-02 22:00:00"})
        self.assertEqual(memory.update_live_acceptance(self.config)["status"], "live_acceptance_pending")
        memory.ingest_event(self.config, "group", {"type": "message", "event_id": "real-group", "message_id": "real-group",
            "conversation_id": "group", "sender_open_dingtalk_id": "other", "sender": "fixture", "content": "合成新群消息", "create_time": "2020-01-02 22:00:01"})
        self.assertEqual(memory.update_live_acceptance(self.config)["status"], "passed")

    def test_empty_listing_requires_matching_live_consumers_and_external_events(self):
        memory.status_update(self.config, listener_state="ready", direct_ready=True, group_ready=True,
            listener_ready_at="2020-01-02T21:00:00+08:00", direct_subscription_ref=memory.sha256_text("direct"),
            group_subscription_ref=memory.sha256_text("group"))
        observed = {"identity":{"user_id":"self","identity_hash":"fixture"},"subscriptions":[],
            "bus":{"entry":{"state":"running","identity_hash":"fixture","holder_pid":100},
                   "live":{"bus":{"pid":100,"identity_hash":"fixture"},"source_state":{"state":"connected"},
                           "consumers":[{"subscribe_id":kind,"pid":pid,"received":1,"dropped":0,
                              "event_types":[event_key]} for kind,pid,event_key in [
                               ("direct",101,"user_im_message_receive_o2o_all"),("group",102,"user_im_message_receive_group_all")]]}}}
        query = self.patched(memory, "query_event_status", return_value=observed)
        self.patched(memory.os, "kill", return_value=None)
        self.assertEqual(memory.update_live_acceptance(self.config)["status"], "live_acceptance_pending")
        for kind,sender in (("direct","other"),("group","self")):
            memory.ingest_event(self.config, kind, {"event_id":kind,"message_id":kind,"conversation_id":kind,
                "sender_open_dingtalk_id":sender,"content":"合成测试","create_time":"2020-01-02 22:00:00"})
        self.assertEqual(memory.update_live_acceptance(self.config)["status"], "live_acceptance_pending")
        memory.ingest_event(self.config,"group",{"event_id":"external-group","message_id":"external-group","conversation_id":"group",
            "sender_open_dingtalk_id":"other","content":"合成测试","create_time":"2020-01-02 22:00:01"})
        self.assertEqual(memory.update_live_acceptance(self.config)["status"], "passed")
        current=json.loads(self.paths['status'].read_text())
        self.assertEqual(current['subscription_verification'],'live_delivery')
        self.assertEqual(current['server_subscription_listing'],'empty')
        self.assertEqual(set(current['live_subscription_receipts']),{'direct','group'})
        def changed(case):
            value=json.loads(json.dumps(observed)); live=value['bus']['live']
            if case=='wrong_identity': value['identity']['identity_hash']='different'
            elif case=='stopped_bus': value['bus']['entry']['state']='not_running'
            elif case=='disconnected': live['source_state']['state']='disconnected'
            elif case=='wrong_subscription': live['consumers'][1]['subscribe_id']='different'
            elif case=='wrong_event_key': live['consumers'][1]['event_types']=['unrelated']
            elif case=='no_delivery': live['consumers'][1]['received']=0
            elif case=='dropped': live['consumers'][1]['dropped']=1
            elif case=='conflicting_server_listing': value['subscriptions']=[{'subscribe_id':'different'}]
            return value
        for case in ['wrong_identity','stopped_bus','disconnected','wrong_subscription','wrong_event_key','no_delivery','dropped','conflicting_server_listing']:
            with self.subTest(case=case):
                query.return_value=changed(case)
                self.assertEqual(memory.update_live_acceptance(self.config)["status"], "live_acceptance_pending")

    def test_live_subscription_evidence_rejects_dead_process(self):
        observed={'identity':{'identity_hash':'fixture'},'bus':{'entry':{'state':'running','identity_hash':'fixture','holder_pid':100},
                  'live':{'bus':{'identity_hash':'fixture','pid':100},'source_state':{'state':'connected'}}}}
        with patch.object(memory.os,'kill',side_effect=ProcessLookupError):
            self.assertEqual(memory.live_subscription_receipts(observed,{}),{})


class MacNotificationTests(Temporary):
    def setUp(self):
        super().setUp()
        self.database = self.root / "sync.sqlite"
        state.initialize(self.database)
        self.alert = state.open_alert("dingtalk_chat", "sync_failure", "fixture", "fixture", database=self.database)

    def set_status(self, status):
        state.mark_delivery(self.alert["alert_id"], "macos", status, database=self.database)

    def test_permission_recheck_only_sends_definitely_unsent_current_alert(self):
        self.set_status("permission_off")
        with patch.object(supervisor, "deliver_macos") as send:
            result = supervisor.recheck_macos_notifications(self.database, probe=lambda: {"status": "authorized"})
        self.assertEqual(result["resumed"], 1)
        send.assert_called_once()

    def test_off_acknowledged_or_uncertain_is_not_resent(self):
        for condition in ("permission_off", "ack", "uncertain", "requested", "visible"):
            with self.subTest(condition=condition):
                self.set_status("permission_off" if condition == "ack" else condition)
                if condition == "ack":
                    state.acknowledge(self.alert["alert_id"], self.database)
                with patch.object(supervisor, "deliver_macos") as send:
                    supervisor.recheck_macos_notifications(self.database, probe=lambda: {"status": "permission_off" if condition == "permission_off" else "authorized"})
                send.assert_not_called()

    def test_crash_after_send_reservation_does_not_blindly_send_again(self):
        with patch.object(supervisor.subprocess, "run", side_effect=Crash()), self.assertRaises(Crash):
            supervisor.deliver_macos(self.alert, self.database)
        row = next(row for row in state.alerts(self.database) if row["alert_id"] == self.alert["alert_id"])
        self.assertEqual(row["macos_status"], "uncertain")
        with patch.object(supervisor.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, '{"status":"requested"}', '')) as run:
            supervisor.deliver_macos(row, self.database)
        self.assertEqual(json.loads(run.call_args.kwargs["input"])["operation"], "status")
        row = next(row for row in state.alerts(self.database) if row["alert_id"] == self.alert["alert_id"])
        self.assertEqual(row["macos_status"], "uncertain")
        self.assertEqual(row["macos_attempt_count"], 1)


class ProtectedActionTests(Temporary):
    def setUp(self):
        super().setUp()
        import alert_workbench as alerts
        import dingtalk_decision_center as center
        import bounded_repair_actions as actions
        self.alerts = alerts
        self.database = self.root / "actions.sqlite"
        def connect():
            connection = sqlite3.connect(self.database)
            connection.row_factory = sqlite3.Row
            return connection
        connection = connect()
        alerts.initialize(connection)
        connection.execute("CREATE TABLE action_runs(run_id TEXT, status TEXT,error_code TEXT,updated_at TEXT)")
        connection.commit()
        connection.close()
        self.patched(alerts.syncv2, "connect", side_effect=connect)
        self.patched(alerts, "LOCK", new=self.root / "lock")
        self.patched(center, "current_control_state", return_value={})
        self.patched(center, "maintenance_active", return_value=False)
        self.identifier = str(uuid.uuid4())
        self.current = {"thread_id": "fixture", "source_id": "dingtalk_chat", "revision": "current", "status": "open",
                        "actions": [{"id": "preserve_current", "available": True}]}
        self.patched(alerts, "detail", return_value=self.current)
        self.start = self.patched(actions, "start", side_effect=lambda *a: {"status": "started", "run_id": a[-1]})
        self.app = SimpleNamespace(config={}, config_path=self.root / "config.json")
        self.payload = {"action_id": str(uuid.uuid4()), "action": "preserve_current", "revision": "current"}

    def test_action_is_idempotent_and_rejects_arbitrary_target_fields(self):
        first = self.alerts.execute_action(self.app, self.identifier, self.payload)
        second = self.alerts.execute_action(self.app, self.identifier, self.payload)
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(self.start.call_count, 1)
        with self.assertRaises(self.alerts.AlertError):
            self.alerts.execute_action(self.app, self.identifier, {**self.payload, "target": "/arbitrary"})
        self.assertEqual(self.start.call_count, 1)

    def test_stale_revision_or_unavailable_action_never_dispatches(self):
        with self.assertRaises(self.alerts.AlertError) as error:
            self.alerts.execute_action(self.app, self.identifier, {**self.payload, "revision": "old"})
        self.assertEqual(error.exception.code, "alert_stale")
        self.current["actions"][0]["available"] = False
        with self.assertRaises(self.alerts.AlertError):
            self.alerts.execute_action(self.app, self.identifier, self.payload)
        self.start.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
