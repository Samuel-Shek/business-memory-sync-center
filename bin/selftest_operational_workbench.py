#!/usr/bin/env python3
"""Isolated contracts. Never import a production config or contact any provider."""

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()

import hashlib
import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import dingtalk_sync_v2 as syncv2
import business_sync_state as states
import email_sync
import operational_workbench as work
import business_page_integrity as page_integrity
import alert_workbench as alerts
import dingtalk_decision_center as center
import claim_reconciliation as cases
import case_conversation as conversations
import codex_handoff
import video_hold


def fixture(
    root: Path,
    *,
    seed=True,
    claim_failures=0,
    claim_suffix_failures=0,
    claim_missing_failures=0,
):
    syncv2.ROOT = root
    syncv2.STATE_DIR = root / "state"
    syncv2.DATABASE = root / "state" / "sync.sqlite"
    syncv2.RUNS_DIR = root / "runs"
    syncv2.ARCHIVE_DIR = root / "archive"
    syncv2.AI_LOCK = root / "ai.lock"
    syncv2.DECISION_LOCK = root / "decision.lock"
    states.DATABASE = syncv2.DATABASE
    cases.DATABASE = root / "case.sqlite"
    cases.RESOLUTION_ARCHIVE = root / "archive.jsonl"
    cases.RELATION_OUTBOX = root / "outbox"
    cases.MAINTENANCE_STATE = root / "maintenance.json"
    center.MAINTENANCE_STATE = root / "maintenance.json"
    center.MAINTENANCE_LOCK = root / "maintenance.lock"
    center.CONTROL_LOCK = root / "control.lock"
    center.CONTROL_START_LOCK = root / "control-start.lock"
    alerts.LOCK = root / "alerts.lock"
    work.MINUTES_STATE_PATH = root / "minutes-state.json"
    codex_handoff.QUEUE_PATH = root / "codex-handoff" / "queue.sqlite"
    email_sync.RUN_LOCK = root / "mail.lock"
    work.VALIDATION_SUCCESS_PATH = root / "last-memory-validation-success.json"
    video_hold.DATABASE = root / "video-holds.sqlite"
    memory_root = root / "vault" / "00_业务记忆库"
    memory_root.mkdir(parents=True, exist_ok=True)
    config = {"external_root": str(root / "external"), "memory_root": str(memory_root),
              "case_workbench": {"ai_enabled": False, "actions_enabled": True, "memory_write_enabled": False},
              "email_sync": {"state_root": str(root / "email"), "archive_root": str(root / "originals"), "preapplied_message_keys": [],
                             "read_safety_receipt": str(root / "safety.json"), "target_map_path": str(root / "target.json"),
                             "feishu_profile_root": str(root / "profile")}}
    config_path = root / "config.json"
    config_path.write_text(json.dumps(config))
    center.MAINTENANCE_STATE.write_text('{"status":"inactive"}')
    syncv2.initialize()
    states.initialize()
    cases.initialize()
    email_sync.initialize(config)
    control = {"sources": [{"id": name, "control": {"available": True, "running": False, "reason": "仅隔离验收"}}
                           for name in states.SOURCE_IDS], "global_action": {"available": True}}
    if not seed:
        previous = states.alerts(limit=200)
        return SimpleNamespace(config=config, config_path=config_path, control=control,
                               manual=next(r for r in previous if r["dedupe_key"] == "dingtalk_mail:manual_attention"),
                               technical=next(r for r in previous if r["dedupe_key"] == "dingtalk_mail:sync_failure"))
    connection = email_sync.connect_database(config)
    code_risks = [("archive_not_opened", "archive", ".zip"), ("archive_not_opened", "archive", ".zip"),
                  ("archive_invalid", "archive_risk", ".xlsx"), ("archive_invalid", "archive_risk", ".docx"),
                  ("blocked_active_content", "hazardous", ".exe"), ("pdf_extract_failed", "ordinary", ".pdf")]
    for index, (code, risk, suffix) in enumerate(code_risks):
        key = "synthetic-" + str(index)
        archive = root / "originals" / key
        archive.mkdir(parents=True)
        name = "sample" + suffix
        data = ("%PDF-1.7\nsynthetic PDF" if suffix == ".pdf" else "synthetic data").encode()
        (archive / name).write_bytes(data)
        connection.execute("\n          INSERT INTO messages(message_key,mailbox,message_id,folder_id,direction,subject,subject_hash,\n             archive_path,archive_state,memory_state,first_batch_id,discovered_at,updated_at)\n          VALUES(?, 'fixture_a45406cf@example.invalid', ?, '2','incoming',?,'hash',?,'archived','applied','batch',?,?)\n        ", (key, key, "合成邮件 " + str(index), str(archive), syncv2.now_iso(), syncv2.now_iso()))
        connection.execute("""
          INSERT INTO attachments(message_key,attachment_id,original_name,stored_name,actual_size,sha256,risk,extract_status,status)
          VALUES(?,? ,?,?,?,?,?,?,'needs_review')
        """, (key, str(index), "合成附件 " + str(index) + suffix, name, len(data), hashlib.sha256(data).hexdigest(), risk, code))
    for i in range(4):
        key = "failed-" + str(i)
        connection.execute("\n          INSERT INTO messages(message_key,mailbox,message_id,folder_id,direction,subject,subject_hash,\n             archive_state,memory_state,last_error_code,archive_attempts,first_batch_id,discovered_at,updated_at)\n          VALUES(?,'fixture_a45406cf@example.invalid',?,'2','incoming',?,'hash','failed','pending','body_length_mismatch',3,'batch',?,?)\n        ", (key, key, "合成正文校验失败 " + str(i), syncv2.now_iso(), syncv2.now_iso()))
    connection.commit()
    connection.close()
    states.update_source("dingtalk_mail", status="persistent_failure", error_code="email_items_failed", attempt_count=16)
    manual = states.open_alert("dingtalk_mail", "manual_attention", "钉钉邮件需要人工处理", "合成：6 个附件")
    technical = states.open_alert("dingtalk_mail", "sync_failure", "钉钉邮件持续失败", "合成：正文归档失败", severity="error", attempts=16)
    for index in range(claim_failures):
        page = root / "vault" / "客户及展会" / f"测试客户{index}" / "项目说明.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        marked = page_integrity.ensure_claim_base(f"项目正文 {index}。\n")
        state = page_integrity.claim_base_state(marked)
        assert state is not None
        drifted = marked[: state[1][0]] + "绕过写入。\n\n" + marked[state[1][0] :]
        with page.open("w", encoding="utf-8", newline="") as handle:
            handle.write(drifted)
    for index in range(claim_suffix_failures):
        page = root / "vault" / "客户及展会" / f"尾部绕过客户{index}" / "项目说明.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        marked = page_integrity.ensure_claim_base(f"项目正文 {index}。\n")
        drifted = marked.rstrip() + "\n\n绕过写入的尾部正文。\n"
        with page.open("w", encoding="utf-8", newline="") as handle:
            handle.write(drifted)
    for index in range(claim_missing_failures):
        page = root / "vault" / "客户及展会" / f"缺少基线客户{index}" / "项目说明.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        claim_id = f"BUSINESS-20990101-{index + 1:03d}"
        payload = page_integrity.encode_payload(
            {"schema_version": 2, "claim_id": claim_id}
        )
        text = (
            "项目正文。\n\n## 核验说明\n\n"
            f"<!-- business-claim:v2 {payload} -->\n"
            "> [!warning] 来源待核｜合成\n"
            f"<!-- /business-claim:{claim_id} -->\n"
        )
        with page.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
    work.reconcile(config)
    control = {"sources": [{"id": name, "control": {"available": True, "running": False, "reason": "仅隔离验收"}}
                           for name in states.SOURCE_IDS], "global_action": {"available": True}}
    return SimpleNamespace(config=config, config_path=config_path, control=control, manual=manual, technical=technical)


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = fixture(self.root)
        self.control = mock.patch.object(center, "current_control_state", return_value=self.app.control)
        self.control.start()

    def tearDown(self):
        self.control.stop()
        self.tmp.cleanup()

    def items(self, kind=None):
        rows = work.listing(self.app.config, include_business=False)["items"]
        return [r for r in rows if kind is None or r["kind"] == kind]

    def groups(self, error_code=None):
        rows = work.listing(self.app.config, include_business=False)["groups"]
        return [r for r in rows if error_code is None or r["error_code"] == error_code]

    def action(self, row, name, **extra):
        detail = work.detail(self.app.config, row["issue_id"], control=self.app.control)
        payload = {"action_id": str(uuid.uuid4()), "action": name, "revision": detail["revision"], **extra}
        return work.execute_action(self.app, row["issue_id"], payload), payload

    def _generic_mail_failure(self):
        connection = email_sync.connect_database(self.app.config)
        with connection:
            connection.execute(
                "UPDATE messages SET last_error_code='win11_network_unavailable',updated_at=? WHERE message_key='failed-0'",
                ("2099-01-01T00:00:00+08:00",),
            )
        connection.close()
        work.reconcile(self.app.config)
        item = next(r for r in self.items("mail_message")
                    if r['observation']['error_code'] == 'win11_network_unavailable')
        group = self.groups('win11_network_unavailable')[0]
        return item, group

    def _advance_generic_mail(self, *, applied=False, error=None):
        connection = email_sync.connect_database(self.app.config)
        with connection:
            connection.execute(
                "UPDATE messages SET archive_state='archived',memory_state=?,memory_applied_at=?,"
                "last_error_code=?,updated_at=? WHERE message_key='failed-0'",
                ('applied' if applied else 'pending', '2099-01-01T00:00:02+08:00' if applied else None,
                 error, '2099-01-01T00:00:02+08:00' if applied else '2099-01-01T00:00:01+08:00'),
            )
        connection.close()

    def test_generic_mail_recovery_waits_for_memory_receipt(self):
        item, group = self._generic_mail_failure()
        self._advance_generic_mail()
        work.reconcile(self.app.config)
        pending = work.raw_issue(item['issue_id'])
        self.assertEqual(pending['status'], 'open')
        self.assertEqual(work.raw_group(group['group_id'])['status'], 'open')
        self.assertEqual(pending['attention_state'], 'waiting')
        self.assertEqual(json.loads(pending['observation_json'])['current_stage'], 'memory')
        self._advance_generic_mail(applied=True)
        connection = email_sync.connect_database(self.app.config)
        with connection:
            connection.execute("UPDATE messages SET memory_applied_at=NULL WHERE message_key='failed-0'")
        connection.close()
        work.reconcile(self.app.config)
        self.assertEqual(work.raw_issue(item['issue_id'])['status'], 'open')
        self._advance_generic_mail(applied=True)
        work.reconcile(self.app.config)
        complete = work.raw_issue(item['issue_id'])
        self.assertEqual(complete['status'], 'closed')
        self.assertEqual(complete['resolution_kind'], 'recovered')
        self.assertEqual(json.loads(complete['evidence_json'])['memory_applied_at'], '2099-01-01T00:00:02+08:00')
        self.assertEqual(work.raw_group(group['group_id'])['status'], 'closed')

    def test_premature_generic_mail_recovery_is_retracted_without_new_episode(self):
        item, group = self._generic_mail_failure()
        self._advance_generic_mail()
        connection = syncv2.connect()
        with connection:
            work._finish(connection, item['issue_id'], 'recovered', {
                'reason': '该邮件已通过正文归档校验；下游记忆接收另行验收。',
                'observed_at': '2099-01-01T00:00:01+08:00',
            }, '2099-01-01T00:00:01+08:00')
            work._reconcile_groups(connection, '2099-01-01T00:00:01+08:00')
        connection.close()
        work.reconcile(self.app.config)
        work.reconcile(self.app.config)
        self.assertEqual(work.raw_issue(item['issue_id'])['status'], 'open')
        self.assertEqual(work.raw_group(group['group_id'])['status'], 'open')
        self.assertEqual(len(self.groups('win11_network_unavailable')), 1)
        connection = syncv2.connect()
        count = connection.execute(
            "SELECT COUNT(*) FROM operational_issue_events WHERE issue_id=? AND event_type='recovery_retracted'",
            (item['issue_id'],),
        ).fetchone()[0]
        connection.close()
        self.assertEqual(count, 1)

    def test_later_memory_receipt_corrects_premature_closed_evidence(self):
        item, group = self._generic_mail_failure()
        self._advance_generic_mail(applied=True)
        connection = syncv2.connect()
        with connection:
            work._finish(connection, item['issue_id'], 'recovered', {
                'reason': '该邮件已通过正文归档校验；下游记忆接收另行验收。',
                'observed_at': '2099-01-01T00:00:01+08:00',
            }, '2099-01-01T00:00:01+08:00')
            work._reconcile_groups(connection, '2099-01-01T00:00:01+08:00')
        connection.close()
        work.reconcile(self.app.config)
        fixed = work.raw_issue(item['issue_id'])
        self.assertEqual(fixed['status'], 'closed')
        self.assertEqual(json.loads(fixed['evidence_json'])['memory_applied_at'], '2099-01-01T00:00:02+08:00')
        self.assertEqual(work.raw_group(group['group_id'])['status'], 'closed')
        self.assertEqual(len(self.groups('win11_network_unavailable')), 1)

    def test_generic_mail_new_memory_failure_supersedes_archive_episode(self):
        item, _ = self._generic_mail_failure()
        self._advance_generic_mail(error='proposal_source')
        work.reconcile(self.app.config)
        self.assertEqual(work.raw_issue(item['issue_id'])['resolution_kind'], 'superseded')
        self.assertTrue(any(r['status'] == 'open' and r['observation']['error_code'] == 'proposal_source'
                            for r in self.items('mail_message')))

    def test_transient_mail_waits_do_not_request_an_unnecessary_user_decision(self):
        connection = email_sync.connect_database(self.app.config)
        with connection:
            connection.execute(
                "UPDATE messages SET last_error_code='win11_network_unavailable',"
                "archive_failure_class='transient',archive_failure_attempts=1,"
                "archive_next_retry_at='2099-01-01T00:01:00+08:00' WHERE message_key='failed-0'")
        connection.close()
        for source_status, code, expected in (
            ("deferred", "memory_core_unverified", "waiting"),
            ("deferred", "validation_inputs_changed", "waiting"),
            ("waiting_idle", "email_items_failed", "waiting"),
            ("waiting_power", "waiting_power", "waiting"),
            ("persistent_failure", "email_items_failed", "waiting"),
            ("retrying", "email_items_failed", "automatic"),
            ("running", "email_items_failed", "automatic"),
            ("manual_required", "read_safety_unconfirmed", "needs_user"),
        ):
            with self.subTest(status=source_status, code=code):
                states.update_source("dingtalk_mail", status=source_status, error_code=code,
                                     next_due="2099-01-01T00:02:00+08:00")
                work.reconcile(self.app.config)
                row = next(r for r in self.items("mail_message")
                           if r["observation"]["error_code"] == "win11_network_unavailable")
                self.assertEqual(row["attention_state"], expected)
                self.assertEqual(row["status"], "open")
        connection = email_sync.connect_database(self.app.config)
        with connection:
            connection.execute("UPDATE messages SET archive_retry_exhausted_at=? WHERE message_key='failed-0'",
                               ("2099-01-01T00:03:00+08:00",))
        connection.close()
        states.update_source("dingtalk_mail", status="retrying", error_code="email_items_failed")
        work.reconcile(self.app.config)
        row = next(r for r in self.items("mail_message")
                   if r["observation"]["error_code"] == "win11_network_unavailable")
        self.assertEqual(row["attention_state"], "automatic")
        self.assertEqual(row["observation"]["retry_state"], "scheduled")
        self.assertIn("6 小时", row["observation"]["recovery_boundary"])
        self.assertEqual(row["status"], "open")

    def test_itemization_is_stable_and_read_only(self):
        self.assertEqual(len(self.items("attachment")), 6)
        self.assertEqual(len(self.items("mail_message")), 4)
        self.assertEqual({r["observation"]["error_code"] for r in self.items("attachment")},
                         {"archive_not_opened", "archive_invalid", "blocked_active_content", "pdf_extract_failed"})
        connection = syncv2.connect_readonly()
        before = [tuple(r) for r in connection.execute("SELECT * FROM operational_issues")]
        connection.close()
        first = self.items()
        for row in first:
            work.detail(self.app.config, row["issue_id"], control=self.app.control)
        connection = syncv2.connect_readonly()
        self.assertEqual(before, [tuple(r) for r in connection.execute("SELECT * FROM operational_issues")])
        connection.close()
        work.reconcile(self.app.config)
        self.assertEqual([r["issue_id"] for r in first], [r["issue_id"] for r in self.items()])

    def test_same_root_is_one_group_with_all_members_preserved(self):
        listing = work.listing(self.app.config, include_business=False)
        self.assertEqual(listing["schema_version"], 2)
        self.assertEqual(listing["total"], 10)
        self.assertEqual(listing["group_total"], 5)
        self.assertEqual(listing["affected_total"], 10)
        mail_group = self.groups("body_length_mismatch")
        self.assertEqual(len(mail_group), 1)
        self.assertEqual(mail_group[0]["affected_count"], 4)
        self.assertEqual(set(mail_group[0]["member_issue_ids"]),
                         {row["issue_id"] for row in self.items("mail_message")})
        self.assertEqual(len(self.groups("archive_not_opened")), 1)
        self.assertEqual(self.groups("archive_not_opened")[0]["affected_count"], 2)
        self.assertNotEqual(self.groups("archive_not_opened")[0]["group_id"],
                            self.groups("archive_invalid")[0]["group_id"])

        detail = work.group_detail(self.app.config, mail_group[0]["group_id"], control=self.app.control)
        self.assertEqual(len(detail["members"]), 4)
        self.assertEqual(detail["total_attempts"], 12)
        self.assertEqual({action["id"] for action in detail["actions"]},
                         {"retry_source", "recheck", "diagnostics"})
        self.assertNotIn("dispose", {action["id"] for action in detail["actions"]})

    def test_claim_base_pages_group_by_root_and_recur_as_a_new_episode(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = fixture(root, claim_failures=4)
            with mock.patch.object(center, "current_control_state", return_value=app.control):
                groups = lambda code: [
                    row
                    for row in work.listing(app.config, include_business=False)["groups"]
                    if row["error_code"] == code
                ]
                group = groups("claim_base_mismatch")
                self.assertEqual(len(group), 1)
                self.assertEqual(group[0]["affected_count"], 4)
                self.assertNotEqual(group[0]["group_id"], groups("body_length_mismatch")[0]["group_id"])
                detail = work.group_detail(app.config, group[0]["group_id"], control=app.control)
                self.assertTrue(all(member["object_label"] for member in detail["members"]))

                for page in (root / "vault" / "客户及展会").rglob("项目说明.md"):
                    text = page_integrity.read_text_exact(page)
                    state = page_integrity.claim_base_state(text)
                    assert state is not None
                    rebased = page_integrity.rebase_text(
                        text, expected_old_base_sha256=state[0]["base_sha256"]
                    )
                    with page.open("w", encoding="utf-8", newline="") as handle:
                        handle.write(rebased)
                work.VALIDATION_SUCCESS_PATH.write_text(
                    json.dumps(
                        {
                            "schema_version": 2,
                            "full_validation": True,
                            "verdict": "pass",
                            "recorded_at": "2099-01-01T00:00:00+08:00",
                        }
                    ),
                    encoding="utf-8",
                )
                work.reconcile(app.config)
                self.assertEqual(work.raw_group(group[0]["group_id"])["status"], "closed")

                page = next((root / "vault" / "客户及展会").rglob("项目说明.md"))
                text = page_integrity.read_text_exact(page)
                state = page_integrity.claim_base_state(text)
                assert state is not None
                drifted = text[: state[1][0]] + "再次绕过。\n\n" + text[state[1][0] :]
                with page.open("w", encoding="utf-8", newline="") as handle:
                    handle.write(drifted)
                work.reconcile(app.config)
                current = [item for item in groups("claim_base_mismatch") if item["status"] == "open"]
                self.assertEqual(len(current), 1)
                self.assertNotEqual(current[0]["group_id"], group[0]["group_id"])

    def test_different_claim_integrity_errors_form_separate_groups(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = fixture(
                root,
                claim_failures=4,
                claim_suffix_failures=2,
                claim_missing_failures=1,
            )
            with mock.patch.object(center, "current_control_state", return_value=app.control):
                groups = work.listing(app.config, include_business=False)["groups"]
                mismatch = [row for row in groups if row["error_code"] == "claim_base_mismatch"]
                suffix = [row for row in groups if row["error_code"] == "claim_base_untracked_suffix"]
                self.assertEqual(len(mismatch), 1)
                self.assertEqual(mismatch[0]["affected_count"], 4)
                self.assertEqual(len(suffix), 1)
                self.assertEqual(suffix[0]["affected_count"], 2)
                self.assertNotEqual(mismatch[0]["group_id"], suffix[0]["group_id"])
                missing = [row for row in groups if row["error_code"] == "claim_base_marker_missing"]
                self.assertEqual(len(missing), 1)
                self.assertEqual(missing[0]["affected_count"], 1)

    def test_group_action_is_idempotent_and_never_bulk_closes_members(self):
        group = self.groups("body_length_mismatch")[0]
        detail = work.group_detail(self.app.config, group["group_id"], control=self.app.control)
        payload = {"action_id": str(uuid.uuid4()), "action": "diagnostics", "revision": detail["revision"]}
        result = work.execute_group_action(self.app, group["group_id"], payload)
        replay = work.execute_group_action(self.app, group["group_id"], payload)
        self.assertEqual(result["state"], "completed")
        self.assertTrue(replay["idempotent_replay"])
        self.assertTrue(all(work.raw_issue(issue_id)["status"] == "open"
                            for issue_id in group["member_issue_ids"]))

    def test_archive_recovery_closes_body_group_and_memory_faults_form_new_groups(self):
        original = self.groups("body_length_mismatch")[0]
        # Simulate a V1 runtime that reused the old open issue and replaced its
        # displayed error before stage tracking was introduced.
        legacy_issue = original["member_issue_ids"][0]
        connection = syncv2.connect()
        with connection:
            row = connection.execute(
                "SELECT observation_json FROM operational_issues WHERE issue_id=?", (legacy_issue,)
            ).fetchone()
            observation = json.loads(row["observation_json"])
            observation["error_code"] = "proposal_claim_relation"
            observation["item_attempt_count"] = 9
            observation.pop("failure_stage", None)
            connection.execute(
                "UPDATE operational_issues SET observation_json=? WHERE issue_id=?",
                (json.dumps(observation, ensure_ascii=False, sort_keys=True), legacy_issue),
            )
        connection.close()
        connection = email_sync.connect_database(self.app.config)
        with connection:
            connection.execute(
                "UPDATE messages SET archive_state='archived',memory_state='applied',last_error_code=NULL,updated_at=? "
                "WHERE message_key IN ('failed-0','failed-1')",
                ("2099-01-01T00:00:00+08:00",),
            )
            connection.execute(
                "UPDATE messages SET archive_state='archived',memory_state='pending',last_error_code='proposal_claim_relation',updated_at=? "
                "WHERE message_key IN ('failed-2','failed-3')",
                ("2099-01-01T00:00:01+08:00",),
            )
        connection.close()
        work.reconcile(self.app.config)
        self.assertEqual(work.raw_group(original["group_id"])["status"], "closed")
        self.assertTrue(all(work.raw_issue(issue_id)["status"] == "closed"
                            for issue_id in original["member_issue_ids"]))
        closed = next(group for group in self.groups("body_length_mismatch")
                      if group["group_id"] == original["group_id"])
        self.assertEqual(closed["total_attempts"], 12)
        self.assertEqual(closed["recovery_boundary"], work.REASONS["body_length_mismatch"][1])
        repaired = json.loads(work.raw_issue(legacy_issue)["observation_json"])
        self.assertEqual(repaired["failure_stage"], "archive")
        self.assertEqual(repaired["item_attempt_count"], 3)
        current = [group for group in self.groups("proposal_claim_relation") if group["status"] == "open"]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["affected_count"], 2)
        self.assertEqual(len(current[0]["member_issue_ids"]), 2)

    def test_closed_group_recurrence_creates_a_new_episode(self):
        original = self.groups("body_length_mismatch")[0]
        connection = syncv2.connect()
        with connection:
            connection.execute(
                "UPDATE operational_issues SET status='closed',attention_state='closed',resolved_at=?,revision=revision+1 "
                "WHERE issue_id IN (SELECT issue_id FROM operational_issue_group_members WHERE group_id=?)",
                (syncv2.now_iso(), original["group_id"]),
            )
        connection.close()
        work.reconcile(self.app.config)
        self.assertEqual(work.raw_group(original["group_id"])["status"], "closed")

        connection = email_sync.connect_database(self.app.config)
        with connection:
            connection.execute(
                "UPDATE messages SET updated_at=? WHERE message_key='failed-0'",
                ("2099-01-01T00:00:00+08:00",),
            )
        connection.close()
        work.reconcile(self.app.config)
        current = [group for group in self.groups("body_length_mismatch") if group["status"] == "open"]
        self.assertEqual(len(current), 1)
        self.assertNotEqual(current[0]["group_id"], original["group_id"])

    def test_changed_mail_error_supersedes_old_group_and_recurrence_gets_new_episode(self):
        connection = email_sync.connect_database(self.app.config)
        with connection:
            connection.execute(
                "UPDATE messages SET archive_state='archived',memory_state='pending',"
                "last_error_code='proposal_source',updated_at=? WHERE message_key='failed-0'",
                ("2099-01-01T00:00:00+08:00",),
            )
        connection.close()
        work.reconcile(self.app.config)
        target_key = "mail:" + work.digest("failed-0")
        first = next(
            row for row in self.items("mail_message")
            if row["status"] == "open"
            and work.raw_issue(row["issue_id"])["object_key"] == target_key
        )
        first_group = next(
            group for group in self.groups("proposal_source") if group["status"] == "open"
        )

        connection = email_sync.connect_database(self.app.config)
        with connection:
            connection.execute(
                "UPDATE messages SET last_error_code='validation_failed',updated_at=? "
                "WHERE message_key='failed-0'",
                ("2099-01-01T00:00:01+08:00",),
            )
        connection.close()
        work.reconcile(self.app.config)
        current = [
            row for row in self.items("mail_message")
            if row["status"] == "open"
            and work.raw_issue(row["issue_id"])["object_key"] == target_key
        ]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["observation"]["error_code"], "validation_failed")
        self.assertEqual(work.raw_issue(first["issue_id"])["resolution_kind"], "superseded")
        self.assertEqual(work.raw_group(first_group["group_id"])["status"], "closed")

        connection = email_sync.connect_database(self.app.config)
        with connection:
            connection.execute(
                "UPDATE messages SET last_error_code='proposal_source',updated_at=? "
                "WHERE message_key='failed-0'",
                ("2099-01-01T00:00:02+08:00",),
            )
        connection.close()
        work.reconcile(self.app.config)
        recurrent = [
            row for row in self.items("mail_message")
            if row["status"] == "open"
            and work.raw_issue(row["issue_id"])["object_key"] == target_key
        ]
        self.assertEqual(len(recurrent), 1)
        self.assertEqual(recurrent[0]["observation"]["error_code"], "proposal_source")
        recurrent_group = next(
            group for group in self.groups("proposal_source") if group["status"] == "open"
        )
        self.assertNotEqual(recurrent_group["group_id"], first_group["group_id"])

    def test_verified_video_disposition_does_not_require_local_original(self):
        reading = {"video_waiting": True, "non_video_complete": True,
                   "receipt_verified": True, "status": "waiting_instruction", "mail_status": "已归档", "summary": "视频等待指令", "status_label": "视频待命"}
        with mock.patch.object(work, "_attachment_reading", return_value=reading):
            work.reconcile(self.app.config)
            row = self.items("attachment")[0]
            with mock.patch.object(work, "safe_original", side_effect=AssertionError("must not open original")):
                result, payload = self.action(row, "dispose", confirmed=True)
            self.assertEqual(result["state"], "completed")
            self.assertEqual(result["result"]["resolution_kind"], "disposed")
            work.reconcile(self.app.config)
            self.assertEqual(work.raw_issue(row["issue_id"])["status"], "closed")
            c = email_sync.connect_database_readonly(self.app.config)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM attachments WHERE status='needs_review'").fetchone()[0], 6)
            c.close()

    def test_partial_and_last_disposition_do_not_close_technical_fault(self):
        original = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.root / "originals").rglob("sample*")}
        files = self.items("attachment")
        for index, row in enumerate(files):
            result, request = self.action(row, "dispose", confirmed=True)
            self.assertEqual(result["state"], "completed")
            replay = work.execute_action(self.app, row["issue_id"], request)
            self.assertTrue(replay["idempotent_replay"])
            connection = email_sync.connect_database_readonly(self.app.config)
            self.assertEqual(len(work.pending_attachment_rows(connection)), 5-index)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM attachments WHERE status='needs_review'").fetchone()[0], 6)
            connection.close()
            opened = states.alerts(status="open")
            self.assertTrue(any(r["alert_id"] == self.app.technical["alert_id"] for r in opened))
            self.assertEqual(any(r["alert_id"] == self.app.manual["alert_id"] for r in opened), index < 5)
        self.assertEqual(len([r for r in self.items("attachment") if r["resolution_kind"] == "disposed"]), 6)
        self.assertEqual(original, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in original})
        work.reconcile(self.app.config)
        self.assertEqual(len([r for r in self.items("attachment") if r["status"] == "open"]), 0)

    def test_explicit_confirmation_and_stale_revision(self):
        row = self.items("attachment")[0]
        with self.assertRaisesRegex(work.WorkbenchError, "明确确认"):
            self.action(row, "dispose")
        detail = work.detail(self.app.config, row["issue_id"], control=self.app.control)
        ref = json.loads(work.raw_issue(row["issue_id"])["source_ref_json"])
        connection = email_sync.connect_database(self.app.config)
        connection.execute("UPDATE attachments SET sha256=? WHERE message_key=? AND attachment_id=?", ("f"*64, ref["message_key"], ref["attachment_id"]))
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(work.WorkbenchError, "变化"):
            work.execute_action(self.app, row["issue_id"], {"action_id": str(uuid.uuid4()), "action": "dispose",
                                                         "confirmed": True, "revision": detail["revision"]})
        work.reconcile(self.app.config)
        self.assertEqual(len(self.items("attachment")), 7)
        self.assertEqual(work.raw_issue(row["issue_id"])["resolution_kind"], "superseded")

    def test_unknown_input_never_clears(self):
        with mock.patch.object(work, "_mail", side_effect=OSError("synthetic")):
            work.reconcile(self.app.config)
            self.assertEqual(len([r for r in self.items("attachment") if r["status"] == "open"]), 6)
            self.assertTrue(any(not r["available"] for r in work.listing(self.app.config, include_business=False)["health"]))
        self.assertTrue(any(a["alert_id"] == self.app.manual["alert_id"] for a in states.alerts(status="open")))

    def test_checks_notes_and_chat_never_resolve(self):
        row = self.items("attachment")[0]
        self.action(row, "recheck")
        self.action(row, "review_note", note="已查看，仍需进一步处理。")
        self.assertEqual(work.raw_issue(row["issue_id"])["status"], "open")
        draft = alerts.save_draft(row["issue_id"], {"content": "关闭后保留", "revision": 0})
        self.assertEqual(draft["draft"], "关闭后保留")
        payload = {"client_message_id": str(uuid.uuid4()), "content": "确认已经好了",
                   "ai_profile": {"model": "deepseek-flash", "reasoning_effort": "high", "speed": "standard"}}
        with mock.patch.object(conversations.memory, "load_config", return_value=self.app.config):
            first = alerts.submit_message(self.app.config_path, row["issue_id"], payload)
            again = alerts.submit_message(self.app.config_path, row["issue_id"], payload)
        self.assertTrue(first["saved"])
        self.assertTrue(again["idempotent_replay"])
        self.assertEqual(work.raw_issue(row["issue_id"])["status"], "open")
        self.assertEqual(len(alerts.conversation(row["issue_id"])["messages"]), 1)
        self.assertEqual(alerts.conversation(self.items("attachment")[1]["issue_id"])["messages"], [])

    def test_source_error_code_change_closes_superseded_group(self):
        def fail(code):
            states.open_alert("win11", "sync_failure", "合成故障", "合成错误",
                              severity="error", error_code=code)
            work.reconcile(self.app.config)

        def open_groups(code):
            return [g for g in self.groups(code) if g["status"] == "open" and g["source_id"] == "win11"]

        fail("synthetic_first")
        first = open_groups("synthetic_first")
        self.assertEqual(len(first), 1)
        issue_id = next(r for r in self.items("source") if r["source_id"] == "win11")["issue_id"]
        fail("synthetic_second")
        self.assertEqual(work.raw_group(first[0]["group_id"])["status"], "closed")
        self.assertEqual(len(open_groups("synthetic_second")), 1)
        self.assertEqual(work.raw_issue(issue_id)["status"], "open")
        connection = syncv2.connect()
        try:
            superseded = connection.execute(
                "SELECT detail_json FROM operational_issue_events WHERE issue_id=? AND event_type='group_superseded'",
                (issue_id,),
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(len(superseded), 1)
        self.assertEqual(json.loads(superseded[0][0])["to_error_code"], "synthetic_second")
        # Moving back is a recurrence: a new episode, the old one stays closed.
        fail("synthetic_first")
        again = open_groups("synthetic_first")
        self.assertEqual(len(again), 1)
        self.assertNotEqual(again[0]["group_id"], first[0]["group_id"])
        self.assertEqual(open_groups("synthetic_second"), [])
        links = work.alert_links()
        linked = [row["group_id"] for rows in links.values() for row in rows if row["issue_id"] == issue_id]
        self.assertEqual(linked, [again[0]["group_id"]])

    def test_positive_recovery_and_recurrence_keep_old_link(self):
        fault = states.open_alert("win11", "sync_failure", "合成故障", "合成错误", severity="error")
        work.reconcile(self.app.config)
        first = next(r for r in self.items("source") if r["source_id"] == "win11")
        states.acknowledge(fault["alert_id"])
        work.reconcile(self.app.config)
        self.assertEqual(work.raw_issue(first["issue_id"])["status"], "open")
        states.resolve_alert("win11", "sync_failure", title="合成恢复", detail="隔离回执")
        work.reconcile(self.app.config)
        self.assertEqual(work.raw_issue(first["issue_id"])["resolution_kind"], "recovered")
        later = states.open_alert("win11", "sync_failure", "合成复发", "新的失败", severity="error")
        work.reconcile(self.app.config)
        self.assertNotEqual(fault["alert_id"], later["alert_id"])
        self.assertEqual(alerts.raw_alert(fault["alert_id"])["status"], "resolved")
        self.assertEqual(len([r for r in self.items("source") if r["source_id"] == "win11"]), 2)

    def test_media_disposition_hides_only_current_recordings_and_preserves_history(self):
        def media_state(ids):
            work.MINUTES_STATE_PATH.write_text(json.dumps({"entries": {
                key: {"missing_artifacts": ["media"], "title": "合成听记"} for key in ids
            }}))
        media_state(["recording-a", "recording-b"])
        states.update_source("dingtalk_minutes", status="waiting_source", error_code="media_pending")
        fault = states.open_alert("dingtalk_minutes", "waiting_source", "合成媒体等待", "2 条待补")
        work.reconcile(self.app.config)
        issue = next(row for row in self.items("source") if row["source_id"] == "dingtalk_minutes")
        with self.assertRaises(work.WorkbenchError):
            self.action(issue, "dismiss_media")
        result, payload = self.action(issue, "dismiss_media", confirmed=True, note="这两条无需继续处理。")
        self.assertEqual(result["result"]["resolution_kind"], "disposed")
        self.assertTrue(work.execute_action(self.app, issue["issue_id"], payload)["idempotent_replay"])
        work.reconcile(self.app.config)
        self.assertEqual(work.raw_issue(issue["issue_id"])["status"], "closed")
        # Source diagnostics retain their actual state; all current UI counts use the projection.
        self.assertEqual(alerts.raw_alert(fault["alert_id"])["status"], "open")
        before = alerts.listing(self.app.config)
        shown = next(row for row in before["alerts"] if row["alert_id"] == fault["alert_id"])
        self.assertEqual(shown["status"], "resolved")
        self.assertEqual(shown["resolution_kind"], "disposed")
        self.assertNotIn(fault["alert_id"], {r["alert_id"] for r in alerts.listing(self.app.config, status="open")["alerts"]})
        self.assertIn(fault["alert_id"], {r["alert_id"] for r in alerts.listing(self.app.config, status="resolved")["alerts"]})
        media_state(["recording-a", "recording-b", "recording-c"])
        work.reconcile(self.app.config)
        new = [row for row in self.items("source") if row["source_id"] == "dingtalk_minutes" and row["status"] == "open"]
        self.assertEqual(len(new), 1)
        self.assertNotEqual(new[0]["issue_id"], issue["issue_id"])
        after = alerts.listing(self.app.config)
        self.assertEqual(after["open_total"], before["open_total"] + 1)
        self.assertEqual(next(r for r in after["alerts"] if r["alert_id"] == fault["alert_id"])["status"], "open")
        self.assertEqual(work.raw_issue(issue["issue_id"])["resolution_kind"], "disposed")

    def test_alert_remains_current_without_complete_disposition_evidence(self):
        row = self.items("attachment")[0]
        self.action(row, "dispose", confirmed=True)
        current = alerts.listing(self.app.config, status="open")
        self.assertIn(self.app.manual["alert_id"], {r["alert_id"] for r in current["alerts"]})
        unlinked = states.open_alert("win11", "sync_failure", "尚未建立关联", "保留未核验告警")
        self.assertIn(unlinked["alert_id"], {r["alert_id"] for r in alerts.listing(self.app.config, status="open")["alerts"]})

    def test_pagination_counts_include_every_item(self):
        result = work.listing(self.app.config, limit=1, include_business=False)
        self.assertEqual(result["total"], 10)
        self.assertEqual(result["counts"]["needs_user"], 0)
        self.assertEqual(result["counts"]["automatic"], 10)
        self.assertEqual(sum(result["counts"][k] for k in ("needs_user","automatic","waiting","closed")), 10)
        self.assertEqual(len(result["items"]), 1)
        self.assertTrue(result["has_more"])

    def test_safe_original_and_diagnostics(self):
        row = self.items("attachment")[0]
        raw = work.raw_issue(row["issue_id"])
        original = work.safe_original(self.app.config, raw)
        original.write_bytes(b"modified synthetic")
        with self.assertRaisesRegex(work.WorkbenchError, "指纹不一致"):
            work.safe_original(self.app.config, raw)
        diagnostic = work.chat_context(self.app.config, row["issue_id"])
        value = json.dumps(diagnostic, ensure_ascii=False)
        self.assertNotIn(str(self.root), value)
        self.assertNotIn('fixture_a45406cf@example.invalid', value)
        self.assertNotIn("合成附件", value)

    def test_pdf_result_only_in_local_artifact(self):
        row = next(r for r in self.items("attachment") if r["observation"]["error_code"] == "pdf_extract_failed")
        with mock.patch.object(work.subprocess, "Popen", return_value=SimpleNamespace(pid=999999)):
            result, request = self.action(row, "retry_extract")
        self.assertEqual(result["state"], "running")
        with mock.patch.object(email_sync, "extract_attachment_text", return_value=("synthetic text", "extracted", "ordinary")):
            work.extract_action(self.app.config, request["action_id"])
        self.assertEqual(work.raw_issue(row["issue_id"])["resolution_kind"], "recovered")
        self.assertTrue((syncv2.STATE_DIR / "workbench-extractions" / (request["action_id"] + ".txt")).is_file())
        connection = email_sync.connect_database_readonly(self.app.config)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM attachments WHERE extract_status='pdf_extract_failed'").fetchone()[0], 1)
        connection.close()

    def test_verified_static_extraction_closes_only_the_same_attachment(self):
        row = self.items('attachment')[0]
        ref = json.loads(work.raw_issue(row['issue_id'])['source_ref_json'])
        connection = email_sync.connect_database(self.app.config)
        connection.execute("UPDATE attachments SET status='verified',extract_status='extracted_static' WHERE message_key=? AND attachment_id=?",
                           (ref['message_key'], ref['attachment_id']))
        connection.commit(); connection.close()
        work.reconcile(self.app.config)
        self.assertEqual(work.raw_issue(row['issue_id'])['resolution_kind'], 'recovered')
        self.assertEqual(len([x for x in self.items('attachment') if x['status']=='open']), 5)

    def test_pdf_failure_keeps_problem(self):
        row = next(r for r in self.items("attachment") if r["observation"]["error_code"] == "pdf_extract_failed")
        with mock.patch.object(work.subprocess, "Popen", return_value=SimpleNamespace(pid=999999)):
            _, request = self.action(row, "retry_extract")
        with mock.patch.object(email_sync, "extract_attachment_text", return_value=("", "pdf_extract_failed", "ordinary")):
            work.extract_action(self.app.config, request["action_id"])
        self.assertEqual(work.raw_issue(row["issue_id"])["status"], "open")

    def test_disposition_verifies_actual_file_not_only_metadata(self):
        row = self.items("attachment")[0]
        original = work.safe_original(self.app.config, work.raw_issue(row["issue_id"]))
        original.write_bytes(b"changed after metadata was recorded")
        result, _ = self.action(row, "dispose", confirmed=True)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["result"]["error_code"], "original_hash_mismatch")
        self.assertEqual(work.raw_issue(row["issue_id"])["status"], "open")

    def test_pdf_spawn_failure_finishes_run_without_closing_issue(self):
        row = next(r for r in self.items("attachment") if r["observation"]["error_code"] == "pdf_extract_failed")
        with mock.patch.object(work, "spawn_extract_worker", side_effect=OSError("synthetic spawn failure")):
            result, _ = self.action(row, "retry_extract")
        self.assertEqual(result["state"], "failed")
        connection = syncv2.connect_readonly()
        self.assertEqual(connection.execute("SELECT status FROM action_runs WHERE run_id=?", (result["run_id"],)).fetchone()[0], "failed")
        connection.close()
        self.assertEqual(work.raw_issue(row["issue_id"])["status"], "open")

    def test_missing_attachment_row_is_not_recovery(self):
        row = self.items("attachment")[0]
        ref = json.loads(work.raw_issue(row["issue_id"])["source_ref_json"])
        connection = email_sync.connect_database(self.app.config)
        connection.execute("DELETE FROM attachments WHERE message_key=? AND attachment_id=?", (ref["message_key"], ref["attachment_id"]))
        connection.commit()
        connection.close()
        work.reconcile(self.app.config)
        self.assertEqual(work.raw_issue(row["issue_id"])["status"], "open")
        self.assertEqual(len([r for r in self.items("attachment") if r["status"] == "open"]), 6)

    def test_recover_committed_disposition_receipt(self):
        row = self.items("attachment")[0]
        _, payload = self.action(row, "dispose", confirmed=True)
        connection = syncv2.connect()
        with connection:
            connection.execute("UPDATE operational_actions SET state='prepared',result_json='{}' WHERE action_id=?", (payload["action_id"],))
        connection.close()
        work.recover_actions()
        result = work.execute_action(self.app, row["issue_id"], payload)
        self.assertEqual(result["state"], "completed")
        self.assertTrue(result["idempotent_replay"])


if __name__ == "__main__":
    unittest.main()
