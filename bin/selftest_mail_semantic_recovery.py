#!/usr/bin/env python3
"""Queue recovery fixtures: in-memory SQLite/files, no production imports or IO.

Run with python3 -B selftest_mail_semantic_recovery.py. The existing SQL schema
and semantic hash function are parsed from source; email_sync is never imported.
The verified-input boundary is a strict fixture, with explicit race/failure hooks.
"""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import ast
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import Any
import unittest
from unittest import mock
import uuid

import mail_semantic_recovery as recovery


HERE = Path(__file__).resolve().parent


def source_function(filename, name):
    tree = ast.parse((HERE / filename).read_text())
    return next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)


def schema(filename):
    node = source_function(filename, "initialize")
    call = next(n for n in ast.walk(node) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and n.func.attr == "executescript")
    return ast.literal_eval(call.args[0])


def pure_function(name):
    namespace = {"Any": Any, "json": json, "canonical_json": recovery._json,
                 "sha256_text": lambda v: hashlib.sha256(v.encode()).hexdigest()}
    exec(compile(ast.Module(body=[source_function("email_sync.py", name)], type_ignores=[]),
                 "<isolated source function>", "exec"), namespace)
    return namespace[name]


class InputError(RuntimeError):
    code = "archive_fixture_invalid"


class MemoryPath:
    """Minimal Path contract for read-only run/journal evidence fixtures."""
    def __init__(self, entries, name="runs"):
        self.entries, self.name = entries, name

    def __truediv__(self, other):
        return MemoryPath(self.entries, self.name + "/" + str(other))

    def exists(self):
        return self.name in self.entries

    def is_symlink(self):
        return self.entries.get(self.name) == ("symlink", None)

    def is_dir(self):
        return self.entries.get(self.name) == ("dir", None)

    def is_file(self):
        return self.entries.get(self.name, (None,))[0] == "file"

    def read_text(self, **kwargs):
        if not self.is_file():
            raise OSError("missing fixture file")
        return self.entries[self.name][1]


class Recovery(unittest.TestCase):
    def setUp(self):
        self.uri = "file:recovery-fixture-" + uuid.uuid4().hex + "?mode=memory&cache=shared"
        self.anchor = self.connect()
        self.anchor.executescript(schema("email_sync.py"))
        self.anchor.executescript(schema("email_phases.py"))
        for column in ("phase_fingerprint", "phase_applied_fingerprint", "body_memory_applied_at"):
            self.anchor.execute("ALTER TABLE messages ADD COLUMN " + column + " TEXT")
        self.config = {"email_sync": {"preapplied_message_keys": []}}
        self.files = {"runs": ("dir", None)}
        self.root = MemoryPath(self.files)
        self.bodies = {}
        self.phase = {}
        self.invalid = set()
        self.calls = 0
        self.hook = None
        self.live_memory = []
        self.api = SimpleNamespace(
            connect_database=self.connect,
            connect_database_readonly=self.readonly,
            conversation_key=pure_function("conversation_key"),
            semantic_input_sha=pure_function("semantic_input_sha"),
            build_semantic_document=self.build,
            email_phases=SimpleNamespace(assert_current=self.assert_current),
            runtime_paths=lambda config: {"runs": self.root},
            now_iso=lambda: "2020-01-02T21:35:26+08:00",
        )
        self.patch = mock.patch.object(recovery, "api", return_value=self.api)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.anchor.close)
        self.seed("a")

    def reconcile(self):
        self.last_result = recovery.reconcile(self.config)
        self.assertIsInstance(self.last_result['blocked_semantic_ids'], list)
        self.assertIsInstance(self.last_result['blocked_scopes'], list)
        self.assertEqual(self.last_result['blocked'], len(self.last_result['blocked_scopes']))
        for scope in self.last_result['blocked_scopes']:
            self.assertEqual({'group_key', 'error_code', 'semantic_ids'}, set(scope))
            self.assertRegex(scope['group_key'], r'^(?:[0-9a-f]{32}|[0-9a-f]{64})$')
            self.assertNotIn('fixture_a45406cf@example.invalid', json.dumps(scope))
        return {key: self.last_result[key] for key in ('created', 'superseded', 'blocked')}

    def connect(self, config=None):
        c = sqlite3.connect(self.uri, uri=True)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        return c

    def readonly(self, config=None):
        c = self.connect()
        c.execute("PRAGMA query_only=ON")
        return c

    def sql(self, query, args=()):
        with self.connect() as c:
            return c.execute(query, args).fetchall()

    def seed(self, key, conversation="c", mailbox='fixture_a45406cf@example.invalid', batch="batch", body="fixture"):
        self.sql("INSERT OR IGNORE INTO batches(batch_id,mode,created_at) VALUES(?,'incremental','2020-01-02')", (batch,))
        self.sql("INSERT INTO messages(message_key,mailbox,message_id,conversation_id,folder_id,direction,subject_hash,"
                 "received_at,archive_state,memory_state,body_sha256,manifest_sha256,archive_path,first_batch_id,discovered_at,updated_at) "
                 "VALUES(?,?,?,?,'2','incoming','fixture','2020-01-02','archived','pending',?,'manifest',?,?, 'now','now')",
                 (key, mailbox, key, conversation, recovery._hash(body), "fixture/" + key, batch))
        self.sql("INSERT INTO batch_messages VALUES(?,?)", (batch, key))
        self.bodies[key] = body

    def rows(self):
        return [dict(r) for r in self.sql("SELECT * FROM messages ORDER BY received_at,message_key")]

    def assert_current(self, config, rows):
        for r in rows:
            if r.get("phase_fingerprint") != self.phase.get(r["message_key"]):
                raise recovery.Blocked("semantic_input_changed")

    def build(self, config, rows, batch):
        self.calls += 1
        if self.hook:
            self.hook(self.calls)
        if not rows or any(r["message_key"] in self.invalid for r in rows):
            raise InputError("fixture archive validation rejected")
        messages = []
        for i, row in enumerate(rows, 1):
            jobs = self.sql("SELECT * FROM attachment_jobs WHERE message_key=? ORDER BY attachment_id_hash", (row["message_key"],))
            items = []
            for job in jobs:
                ignored = job["state"] == "ignored"
                items.append({"extract_status": "ignored_by_user" if ignored else "extracted",
                              "extracted_text": "" if ignored else "synthetic attachment",
                              "processing_complete": not ignored, "full_read_confirmed": not ignored,
                              "supplement_sha256": None if ignored else job["result_sha256"]})
            messages.append({"source_token": f"S{i:03d}", "body": self.bodies[row["message_key"]], "attachments": items})
        document = {"batch_id": batch, "mailbox_scope": "enterprise_only", "source_contract": "fixture",
                    "messages": messages, "current_memory": self.live_memory}
        return document, self.api.semantic_input_sha(document)

    def group(self, sid="old", status="failed", stale=True, members=None, key=None):
        rows = self.rows() if members is None else [r for r in self.rows() if r["message_key"] in members]
        scope = recovery._scope(rows[0]) if rows else recovery._scope(self.rows()[0])
        input_sha = "old-input" if stale else self.build(self.config, rows, sid)[1]
        self.sql("INSERT INTO semantic_groups(semantic_id,semantic_key,group_key,source_ref,batch_id,status,input_sha256,"
                 "attempts,last_error_code,created_at) VALUES(?,?,?,'fixture','batch',?,?,4,'semantic_input_changed','old')",
                 (sid, key or "legacy-" + sid, recovery._group_key(scope), status, input_sha))
        for i, row in enumerate(rows, 1):
            self.sql("INSERT INTO semantic_group_messages VALUES(?,?,?)", (sid, row["message_key"], f"S{i:03d}"))

    def journal(self, sid, state="prepared", **updates):
        self.files["runs/" + sid] = ("dir", None)
        self.files["runs/" + sid + "/memory-transaction"] = ("dir", None)
        value = {"schema_version": 1, "semantic_id": sid, "state": state, **updates}
        self.files["runs/" + sid + "/memory-transaction/transaction.json"] = ("file", json.dumps(value))

    def groups(self):
        return [dict(r) for r in self.sql("SELECT * FROM semantic_groups ORDER BY semantic_id")]

    def reason(self):
        return json.loads(self.sql("SELECT value FROM meta ORDER BY key")[-1][0])["reason_code"]

    def test_orphan_pending_mail_created_once_with_worker_compatible_hash(self):
        before = self.rows()
        self.assertEqual({"created": 1, "superseded": 0, "blocked": 0}, self.reconcile())
        group = self.groups()[0]
        self.assertEqual(self.build(self.config, self.rows(), group["semantic_id"])[1], group["input_sha256"])
        self.assertTrue(group["semantic_key"].startswith(recovery.VERSION + ":"))
        self.assertEqual({"created": 0, "superseded": 0, "blocked": 0}, self.reconcile())
        self.assertEqual(before, self.rows())

    def test_drift_retires_old_atomically_without_cache_reuse(self):
        self.group()
        self.files["runs/old"] = ("dir", None)
        self.files["runs/old/proposal.json"] = ("file", "old expensive answer")
        files_before = dict(self.files)
        self.assertEqual({"created": 1, "superseded": 1, "blocked": 0}, self.reconcile())
        old = next(g for g in self.groups() if g["semantic_id"] == "old")
        new = next(g for g in self.groups() if g["status"] == "pending")
        self.assertEqual("superseded", old["status"])
        self.assertEqual("old-input", old["input_sha256"])
        self.assertEqual(4, old["attempts"])
        self.assertNotEqual("old", new["semantic_id"])
        self.assertEqual(files_before, self.files)
        self.assertEqual(0, self.reconcile()["created"])

    def test_same_input_failed_budget_and_valid_legacy_task_preserved(self):
        self.group(stale=False)
        before = self.groups()
        for _ in range(3):
            self.assertEqual({"created": 0, "superseded": 0, "blocked": 0}, self.reconcile())
        self.assertEqual(before, self.groups())

    def test_duplicate_valid_groups_keep_one_without_resetting_attempts(self):
        self.group("a-old", stale=False)
        self.group("b-old", stale=False)
        self.assertEqual({"created": 0, "superseded": 1, "blocked": 0}, self.reconcile())
        self.assertEqual(1, len([g for g in self.groups() if g["status"] in recovery.ACTIVE]))
        self.assertTrue(all(g["attempts"] == 4 for g in self.groups()))

    def test_conversation_expansion_and_identical_new_body_changes_version(self):
        self.assertEqual(1, self.reconcile()["created"])
        first = self.groups()[0]["semantic_key"]
        self.seed("b", body="fixture")
        self.assertEqual({"created": 1, "superseded": 1, "blocked": 0}, self.reconcile())
        current = next(g for g in self.groups() if g["status"] == "pending")
        self.assertNotEqual(first, current["semantic_key"])
        self.assertEqual(2, len(self.sql("SELECT * FROM semantic_group_messages WHERE semantic_id=?", (current["semantic_id"],))))

    def test_batch_randomness_and_live_memory_do_not_change_version(self):
        self.reconcile()
        before = self.groups()
        self.sql("INSERT INTO batches(batch_id,mode,created_at) VALUES('other','incremental','later')")
        self.sql("UPDATE messages SET first_batch_id='other'")
        self.live_memory = [{"fixture": "unrelated new memory"}]
        self.assertEqual({"created": 0, "superseded": 0, "blocked": 0}, self.reconcile())
        self.assertEqual(before, self.groups())

    def test_mailbox_isolation(self):
        self.seed("b", mailbox='fixture_efa236d8@example.invalid')
        result = self.reconcile()
        self.assertEqual(2, result["created"])
        self.assertEqual(2, len({g["group_key"] for g in self.groups()}))
        self.assertTrue(all(len(self.sql("SELECT * FROM semantic_group_messages WHERE semantic_id=?", (g["semantic_id"],))) == 1 for g in self.groups()))

    def test_superseded_only_or_unlinked_old_task_can_create_current(self):
        self.group(status="superseded", members=[])
        before = self.groups()[0]
        self.assertEqual({"created": 1, "superseded": 0, "blocked": 0}, self.reconcile())
        self.assertEqual(before, next(g for g in self.groups() if g["semantic_id"] == "old"))

    def test_applied_history_unchanged_when_new_input_arrives(self):
        self.group(status="applied", stale=False)
        before = self.groups()[0]
        self.sql("UPDATE messages SET memory_state='applied',memory_applied_at='earlier'")
        self.journal("old", "committed")
        self.seed("b", body="new fixture")
        self.assertEqual({"created": 1, "superseded": 0, "blocked": 0}, self.reconcile())
        self.assertEqual(before, next(g for g in self.groups() if g["semantic_id"] == "old"))

    def test_applied_same_input_with_pending_mail_is_blocked(self):
        self.group(status="applied", stale=False)
        before = self.groups()
        self.assertEqual(1, self.reconcile()["blocked"])
        self.assertEqual("semantic_applied_version_pending", self.reason())
        self.assertEqual(before, self.groups())

    def test_transaction_states_fail_closed_including_committed_not_applied(self):
        self.group()
        before = self.groups()
        for state in ("prepared", "applying", "restored_pending_validation", "committed", "unknown"):
            with self.subTest(state=state):
                self.journal("old", state)
                self.assertEqual({"created": 0, "superseded": 0, "blocked": 1}, self.reconcile())
                self.assertEqual(before, self.groups())

    def test_proven_rollback_allows_new_input_but_does_not_restore_files(self):
        self.group()
        self.journal("old", "rolled_back_after_recovery")
        before = dict(self.files)
        self.assertEqual(1, self.reconcile()["created"])
        self.assertEqual(before, self.files)

    def test_malformed_mismatched_and_symlink_journal_are_blocked(self):
        self.group()
        path = "runs/old/memory-transaction/transaction.json"
        for contents in ("not-json", "[]", json.dumps({"schema_version": 1, "semantic_id": "wrong", "state": "rolled_back"})):
            self.journal("old")
            self.files[path] = ("file", contents)
            self.assertEqual(1, self.reconcile()["blocked"])
        self.files[path] = ("symlink", None)
        self.assertEqual(1, self.reconcile()["blocked"])

    def test_missing_journal_with_transaction_directory_is_blocked(self):
        self.group()
        self.files["runs/old"] = ("dir", None)
        self.files["runs/old/memory-transaction"] = ("dir", None)
        self.assertEqual(1, self.reconcile()["blocked"])

    def test_orphan_new_run_cache_is_never_reused(self):
        scope = recovery._scope(self.rows()[0])
        snapshot = recovery._read_snapshot(self.config, scope)
        sid = recovery._input(self.config, scope, snapshot)[3]
        self.files["runs/" + sid] = ("dir", None)
        self.assertEqual(1, self.reconcile()["blocked"])
        self.assertEqual("semantic_orphan_run_directory", self.reason())
        self.assertEqual([], self.groups())

    def test_safety_failure_preserves_queue_and_isolated_other_scope_progresses(self):
        self.group()
        before = self.groups()[0]
        self.invalid.add("a")
        self.seed("b", conversation="other")
        result = self.reconcile()
        self.assertEqual({"created": 1, "superseded": 0, "blocked": 1}, result)
        self.assertEqual(before, next(g for g in self.groups() if g["semantic_id"] == "old"))

    def test_message_change_between_read_and_commit_blocks(self):
        self.group()
        self.calls = 0
        def race(n):
            if n == 1:
                self.sql("UPDATE messages SET body_sha256='changed-same-timestamp'")
        self.hook = race
        self.assertEqual(1, self.reconcile()["blocked"])
        self.assertEqual("semantic_db_versions_changed", self.reason())
        self.assertEqual(1, len(self.groups()))
        self.assertEqual("failed", self.groups()[0]["status"])

    def test_membership_added_during_input_read_blocks(self):
        self.calls = 0
        self.hook = lambda n: self.seed("b") if n == 1 else None
        self.assertEqual(1, self.reconcile()["blocked"])
        self.assertEqual("semantic_db_versions_changed", self.reason())
        self.assertEqual([], self.groups())

    def test_group_status_changed_to_applied_before_commit_not_overwritten(self):
        self.group()
        self.calls = 0
        self.hook = lambda n: self.sql("UPDATE semantic_groups SET status='applied' WHERE semantic_id='old'") if n == 1 else None
        self.assertEqual(1, self.reconcile()["blocked"])
        self.assertEqual("applied", self.groups()[0]["status"])

    def test_file_input_drift_on_second_validation_blocks(self):
        self.group()
        self.calls = 0
        def drift(n):
            if n == 2:
                self.bodies["a"] = "changed fixture source"
        self.hook = drift
        self.assertEqual(1, self.reconcile()["blocked"])
        self.assertEqual("semantic_input_changed_during_recovery", self.reason())
        self.assertEqual("failed", self.groups()[0]["status"])

    def test_transaction_appearing_just_before_commit_blocks(self):
        self.group()
        self.calls = 0
        self.hook = lambda n: self.journal("old") if n == 2 else None
        self.assertEqual(1, self.reconcile()["blocked"])
        self.assertEqual("semantic_transaction_unfinished", self.reason())

    def test_insert_failure_rolls_back_new_and_old_groups(self):
        self.group()
        before = self.groups()
        self.sql("CREATE TRIGGER reject_new_member BEFORE INSERT ON semantic_group_messages "
                 "BEGIN SELECT RAISE(ABORT,'fixture insertion failure'); END")
        self.assertEqual({"created": 0, "superseded": 0, "blocked": 1}, self.reconcile())
        self.assertEqual(before, self.groups())

    def test_retirement_failure_rolls_back_creation(self):
        self.group()
        before = self.groups()
        self.sql("CREATE TRIGGER reject_retirement BEFORE UPDATE ON semantic_groups "
                 "BEGIN SELECT RAISE(ABORT,'fixture retirement failure'); END")
        self.assertEqual({"created": 0, "superseded": 0, "blocked": 1}, self.reconcile())
        self.assertEqual(before, self.groups())

    def attachment(self):
        self.sql("INSERT INTO attachments(message_key,attachment_id,original_name,stored_name) VALUES('a','att','fixture.png','fixture')")
        self.sql("INSERT INTO attachment_jobs(job_key,message_key,attachment_id_hash,item_json,kind,state,result_sha256,created_at,updated_at) "
                 "VALUES('job','a','att','{}','image','ready','result','now','now')")
        self.phase["a"] = "phase-ready"
        self.sql("UPDATE messages SET phase_fingerprint='phase-ready'")

    def test_ignored_decision_changes_version_and_remains_ignored(self):
        self.attachment()
        self.reconcile()
        old = self.groups()[0]
        self.sql("UPDATE attachment_jobs SET state='ignored',last_error_code='ignored_by_user'")
        self.sql("UPDATE attachments SET status='ignored',extract_status='ignored_by_user'")
        self.sql("UPDATE messages SET phase_fingerprint='phase-ignored'")
        self.phase["a"] = "phase-ignored"
        before_jobs = [dict(r) for r in self.sql("SELECT * FROM attachment_jobs")]
        self.assertEqual({"created": 1, "superseded": 1, "blocked": 0}, self.reconcile())
        new = next(g for g in self.groups() if g["status"] == "pending")
        self.assertNotEqual(old["semantic_key"], new["semantic_key"])
        attachment = self.build(self.config, self.rows(), new["semantic_id"])[0]["messages"][0]["attachments"][0]
        self.assertEqual("", attachment["extracted_text"])
        self.assertFalse(attachment["full_read_confirmed"])
        self.assertEqual(before_jobs, [dict(r) for r in self.sql("SELECT * FROM attachment_jobs")])

    def test_phase_changed_before_phase_reconcile_is_blocked(self):
        self.attachment()
        self.phase["a"] = "new-phase-without-reconcile"
        self.assertEqual(1, self.reconcile()["blocked"])
        self.assertEqual("semantic_input_changed", self.reason())

    def test_ignored_race_without_timestamp_change_is_detected(self):
        self.attachment()
        self.calls = 0
        self.hook = lambda n: self.sql("UPDATE attachment_jobs SET state='ignored'") if n == 1 else None
        self.assertEqual(1, self.reconcile()["blocked"])
        self.assertEqual("semantic_db_versions_changed", self.reason())

    def test_lease_retry_timing_not_a_new_semantic_version(self):
        self.attachment()
        self.reconcile()
        before = self.groups()
        self.sql("UPDATE attachment_jobs SET attempts=20,lease_token='worker',next_retry_at='later'")
        self.assertEqual(0, self.reconcile()["created"])
        self.assertEqual(before, self.groups())

    def test_pending_source_with_retired_content_version_is_not_replayed(self):
        self.reconcile()
        self.sql("UPDATE semantic_groups SET status='superseded'")
        before = self.groups()
        self.assertEqual(1, self.reconcile()["blocked"])
        self.assertEqual("semantic_version_retired_or_inconsistent", self.reason())
        self.assertEqual(before, self.groups())

    def test_preapplied_and_unarchived_sources_are_not_enqueued(self):
        self.config["email_sync"]["preapplied_message_keys"] = ["a"]
        self.seed("b", conversation="other")
        self.sql("UPDATE messages SET archive_state='failed' WHERE message_key='b'")
        self.assertEqual({"created": 0, "superseded": 0, "blocked": 0}, self.reconcile())

    def test_missing_all_source_members_preserves_orphan_for_review(self):
        self.group(members=[])
        self.sql("UPDATE semantic_groups SET group_key='unknown-group'")
        self.sql("UPDATE messages SET memory_state='applied'")
        before = self.groups()
        self.assertEqual(1, self.reconcile()["blocked"])
        self.assertEqual("semantic_orphan_sources_unavailable", self.reason())
        self.assertEqual(before, self.groups())

    def test_missing_member_does_not_silently_shrink_conversation(self):
        self.seed("b")
        self.group()
        self.sql("UPDATE messages SET archive_state='failed' WHERE message_key='b'")
        self.assertEqual(1, self.reconcile()["blocked"])
        self.assertEqual("semantic_members_unavailable", self.reason())

    def test_empty_database_does_not_initialize_schema_or_create_files(self):
        with mock.patch.object(self.api, "connect_database_readonly", side_effect=sqlite3.OperationalError("missing")), \
             mock.patch.object(self.api, "connect_database", side_effect=AssertionError("must not create db")):
            with self.assertLogs(recovery.LOG, level="WARNING"):
                self.assertEqual({"created": 0, "superseded": 0, "blocked": 1}, self.reconcile())
                self.assertTrue(self.last_result['block_all'])

    def test_schema2_proven_rollback_is_terminal(self):
        self.group()
        self.journal('old', 'rolled_back', schema_version=2)
        self.assertEqual({'created': 1, 'superseded': 1, 'blocked': 0}, self.reconcile())

    def test_schema2_committed_applied_history_preserved(self):
        self.group(status='applied', stale=False)
        self.journal('old', 'committed', schema_version=2)
        self.sql("UPDATE messages SET memory_state='applied'")
        self.seed('b', body='new input')
        self.assertEqual({'created': 1, 'superseded': 0, 'blocked': 0}, self.reconcile())
        self.assertEqual('applied', next(g for g in self.groups() if g['semantic_id'] == 'old')['status'])

    def test_schema2_unfinished_blocks_just_related_semantic_ids(self):
        self.group()
        self.group('old-duplicate')
        self.journal('old', 'applying', schema_version=2)
        self.seed('other', conversation='other')
        self.assertEqual({'created': 1, 'superseded': 0, 'blocked': 1}, self.reconcile())
        self.assertEqual(['old', 'old-duplicate'], self.last_result['blocked_semantic_ids'])
        self.assertEqual('semantic_transaction_unfinished', self.last_result['blocked_scopes'][0]['error_code'])
        self.assertFalse(self.last_result['block_all'])

    def test_invalid_schema_rejected_and_schema2_unacknowledged_commit_blocks(self):
        self.group()
        for schema_value in (True, 3, '2', 2):
            with self.subTest(schema=schema_value):
                self.journal('old', 'committed', schema_version=schema_value)
                self.assertEqual(1, self.reconcile()['blocked'])
                self.assertEqual(['old'], self.last_result['blocked_semantic_ids'])

    def test_superseded_history_without_pending_mail_is_not_read(self):
        self.group(status='superseded')
        self.journal('old', 'applying')
        self.sql("UPDATE messages SET memory_state='applied'")
        with mock.patch.object(self.api, 'build_semantic_document', side_effect=AssertionError('historical input read')):
            self.assertEqual({'created': 0, 'superseded': 0, 'blocked': 0}, self.reconcile())

    def test_block_list_includes_task_inserted_during_snapshot_race(self):
        self.group()
        self.calls = 0
        self.hook = lambda n: self.group('concurrent') if n == 1 else None
        self.assertEqual(1, self.reconcile()['blocked'])
        self.assertEqual(['concurrent', 'old'], self.last_result['blocked_semantic_ids'])

    def test_blocked_pending_mail_without_group_returns_opaque_scope(self):
        self.invalid.add('a')
        self.assertEqual(1, self.reconcile()['blocked'])
        self.assertEqual([], self.last_result['blocked_semantic_ids'])
        self.assertEqual('archive_fixture_invalid', self.last_result['blocked_scopes'][0]['error_code'])


if __name__ == "__main__":
    unittest.main(verbosity=2)
