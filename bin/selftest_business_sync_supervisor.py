#!/usr/bin/env python3
"""Offline acceptance tests for the power-aware sync supervisor."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import json
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import business_sync_state as state
import business_sync_supervisor as supervisor


class FakeDashboard:
    mail_attention = 0
    mail_technical = 0
    core_status = "healthy"
    core_last_success = None
    core_validation_error = None
    core_acceptance = None

    def __init__(self, _paths):
        pass

    def collect(self, _config, force=False, now=None):
        healthy = self.core_status in {"healthy", "recovered"}
        return {
            "memory_core": {
                "display_status": {
                    "code": self.core_status,
                    "detail": "写入、索引或校验尚未恢复" if self.core_status == "failed" else "运行正常",
                },
                "health": {
                    "code": self.core_status,
                    "detail": "写入、索引或校验尚未恢复" if self.core_status == "failed" else "运行正常",
                },
                "acceptance": {"code": self.core_acceptance or ("accepted" if healthy else "blocked")},
                "last_success": self.core_last_success or (
                    state.now_iso(now) if healthy and now is not None else None
                ),
                "validation_error_code": self.core_validation_error,
            },
            "sources": [
                {
                    "id": "dingtalk_mail",
                    "attention": self.mail_attention,
                    "attention_breakdown": {
                        "technical_failed": self.mail_technical,
                        "manual_review": max(0, self.mail_attention - self.mail_technical),
                        "pending": 0,
                    },
                },
            ]
        }


class FakeRunner:
    def __init__(self, *, ac=True, idle_seconds=600, failures=None):
        self.ac = ac
        self.idle_seconds = idle_seconds
        self.failures = failures or {}
        self.targets = []

    def __call__(self, arguments, _timeout):
        if arguments[:3] == ["/usr/bin/pmset", "-g", "ps"]:
            source = "AC Power" if self.ac else "Battery Power"
            return subprocess.CompletedProcess(arguments, 0, f"Now drawing from '{source}'\n", "")
        if arguments and arguments[0] == "/usr/sbin/ioreg":
            return subprocess.CompletedProcess(
                arguments, 0, f'    "HIDIdleTime" = {self.idle_seconds * 1_000_000_000}\n', ""
            )
        if arguments and arguments[0] == "/usr/bin/caffeinate":
            target = arguments[-1]
            self.targets.append(target)
            code = self.failures.get(target)
            payload = {
                "run_id": f"00000000-0000-4000-8000-{len(self.targets):012d}",
                "status": "failed" if code else "noop",
                "target": target,
            }
            if code:
                payload["error_code"] = code
            elif target == "dingtalk_chat":
                # Current controller acceptance requires its component receipt.
                payload["sources"] = {target: {"status": "noop"}}
            return subprocess.CompletedProcess(arguments, 2 if code else 0, json.dumps(payload), "")
        return subprocess.CompletedProcess(arguments, 1, "", "")


class ModelChainPolicyTests(unittest.TestCase):
    """A model-chain failure keeps the chat source retrying (2020-01-02)."""

    def test_model_chain_codes_are_transient_for_chat(self):
        for code in ("codex_subscription_limit", "chat_reconcile_codex_subscription_limit",
                     "chat_memory_model_output_invalid", "model_chain_exhausted"):
            self.assertTrue(supervisor.is_model_chain_code(code), code)
            persistent, delay = supervisor.failure_schedule("dingtalk_chat", code, 1)
            self.assertFalse(persistent, code)
            self.assertEqual(delay, timedelta(minutes=5))

    def test_other_chat_failures_still_stop_for_a_person(self):
        self.assertFalse(supervisor.is_model_chain_code("proposal_claim_relation_x"))
        persistent, _ = supervisor.failure_schedule("dingtalk_chat", "journal_conflict", 1)
        self.assertTrue(persistent)


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="business-supervisor-test-")
        self.root = Path(self.temporary.name)
        self.database = self.root / "state.sqlite"
        self.config = self.root / "config.json"
        self.config.write_text("{}\n", encoding="utf-8")
        self.minutes_state = self.root / "minutes-automation.json"
        self.minutes_state.write_text('{"schema_version":2,"items":{}}\n', encoding="utf-8")
        self.current = datetime.fromisoformat("2020-01-02T18:00:00+08:00")
        FakeDashboard.mail_attention = 0
        FakeDashboard.mail_technical = 0
        FakeDashboard.core_validation_error = None
        FakeDashboard.core_acceptance = None
        FakeDashboard.core_status = "healthy"
        FakeDashboard.core_last_success = None
        state.initialize(self.database)

    def tearDown(self):
        self.temporary.cleanup()

    def make_supervisor(self, runner, clock=None):
        return supervisor.Supervisor(
            self.config,
            database=self.database,
            runner=runner,
            dashboard_factory=FakeDashboard,
            clock=clock or (lambda: self.current),
            notification_delivery=lambda _config, _database: 0,
        )

    @staticmethod
    def inactive_maintenance():
        return {"active": False, "status": "inactive", "exited_at": None}

    def test_additive_migration_preserves_legacy_alert_and_receipts(self):
        legacy = self.root / "legacy.sqlite"
        connection = sqlite3.connect(legacy)
        connection.executescript(
            "\n            CREATE TABLE supervisor_runtime(\n              runtime_id TEXT PRIMARY KEY,status TEXT NOT NULL,power_source TEXT NOT NULL,\n              on_ac INTEGER NOT NULL,idle_ready INTEGER NOT NULL,pending_count INTEGER NOT NULL,\n              blocked_reason TEXT,next_eligible_at TEXT,updated_at TEXT NOT NULL\n            );\n            CREATE TABLE supervisor_sources(\n              source_id TEXT PRIMARY KEY,status TEXT NOT NULL,next_due TEXT,updated_at TEXT NOT NULL\n            );\n            CREATE TABLE sync_alerts(\n              alert_id TEXT PRIMARY KEY,dedupe_key TEXT NOT NULL UNIQUE,source_id TEXT NOT NULL,\n              category TEXT NOT NULL,severity TEXT NOT NULL,title TEXT NOT NULL,detail TEXT NOT NULL,\n              status TEXT NOT NULL,occurrence_count INTEGER NOT NULL DEFAULT 1,attempts INTEGER NOT NULL DEFAULT 0,\n              first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,next_reminder TEXT,resolved_at TEXT,\n              acknowledged_at TEXT,macos_status TEXT NOT NULL DEFAULT 'pending',macos_requested_at TEXT,\n              macos_error_code TEXT,feishu_status TEXT NOT NULL DEFAULT 'pending',feishu_attempted_at TEXT,\n              feishu_receipt_hash TEXT,feishu_error_code TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL\n            );\n            INSERT INTO supervisor_runtime VALUES('singleton','idle','ac',1,1,0,NULL,NULL,'2020-01-02T18:00:00+08:00');\n            INSERT INTO sync_alerts VALUES(\n              '2b0d45d5-bdb1-5933-9d30-3471d417210d','dingtalk_mail:sync_failure','dingtalk_mail',\n              'sync_failure','error','旧告警','旧回执必须保留','open',14,14,\n              '2020-01-02T18:00:00+08:00','2020-01-02T18:00:00+08:00',NULL,NULL,NULL,\n              'failed','2020-01-02T18:00:00+08:00','notification_request_failed',\n              'uncertain','2020-01-02T18:00:00+08:00',NULL,'transport_timeout',\n              '2020-01-02T18:00:00+08:00','2020-01-02T18:00:00+08:00'\n            );\n            "
        )
        connection.commit()
        connection.close()
        state.initialize(legacy)
        migrated = state.alerts(legacy)[0]
        self.assertEqual(migrated["status"], "open")
        self.assertEqual(migrated["attempts"], 14)
        self.assertEqual(migrated["macos_status"], "failed")
        self.assertEqual(migrated["feishu_status"], "uncertain")
        self.assertEqual(migrated["miso_status"], "pending")
        reconfirmed = state.open_alert(
            "dingtalk_mail", "sync_failure", "旧告警", "当前复检仍未恢复",
            severity="error", attempts=14, database=legacy, at=self.current,
        )
        self.assertEqual(reconfirmed["macos_status"], "retrying")
        self.assertEqual(reconfirmed["miso_status"], "pending")
        runtime = state.runtime(legacy)
        self.assertEqual(runtime["power_probe_failures"], 0)
        connection = sqlite3.connect(legacy)
        try:
            audit_table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='alert_delivery_audit'"
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(audit_table)

    def test_background_mail_runs_while_other_sources_wait(self):
        self.config.write_text(json.dumps({"email_sync": {"background_collection_while_active": True}}))
        runner = FakeRunner(ac=True, idle_seconds=0)
        engine = self.make_supervisor(runner)
        with patch.object(supervisor.email_sync, "read_safety_status", return_value={"binding_valid":True,"server_side_pass":True,"cross_device_confirmed":True}), patch.object(supervisor, "maintenance_status", side_effect=self.inactive_maintenance), patch.object(supervisor, "MINUTES_AUTOMATION_STATE", self.minutes_state):
            result = engine.run_once()
            again = engine.run_once()
            self.current += timedelta(minutes=6)
            later = engine.run_once()
        self.assertEqual(result["checked"], 1)
        self.assertEqual(again["checked"], 0)
        self.assertEqual(later["checked"], 1)
        self.assertEqual(runner.targets, ["dingtalk_mail", "dingtalk_mail"])

    def test_background_mail_does_not_bypass_power_or_maintenance(self):
        self.config.write_text(json.dumps({"email_sync": {"background_collection_while_active": True}}))
        runner = FakeRunner(ac=False, idle_seconds=0)
        engine = self.make_supervisor(runner)
        with patch.object(supervisor, "maintenance_status", side_effect=self.inactive_maintenance), patch.object(supervisor, "MINUTES_AUTOMATION_STATE", self.minutes_state):
            self.assertEqual(engine.run_once()["checked"], 0)
        runner.ac = True
        with patch.object(supervisor, "maintenance_status", return_value={"active": True}), patch.object(supervisor, "MINUTES_AUTOMATION_STATE", self.minutes_state):
            self.assertEqual(engine.run_once()["checked"], 0)
        self.assertEqual(runner.targets, [])

    def test_mail_collection_does_not_claim_heavy_work_finished(self):
        engine = self.make_supervisor(FakeRunner())
        old = state.source("dingtalk_mail", self.database).get("last_success")
        row = engine.record_outcome("dingtalk_mail", {"status":"success", "run_id":"collection-run", "sources":{"dingtalk_mail":{"heavy_deferred":True, "deferred_reason":"waiting_win11_power"}}}, self.current)
        self.assertEqual(row["status"], "waiting_power")
        self.assertEqual(row["error_code"], "waiting_win11_power")
        self.assertEqual(row.get("last_success"), old)
        self.assertEqual(row["last_run_id"], "collection-run")

    def test_ac_idle_runs_four_sources_in_order_without_duplicate_queue(self):
        runner = FakeRunner(ac=True, idle_seconds=600)
        engine = self.make_supervisor(runner)
        with patch.object(supervisor, "maintenance_status", side_effect=self.inactive_maintenance), patch.object(
            supervisor, "MINUTES_AUTOMATION_STATE", self.minutes_state
        ):
            result = engine.run_once()
            again = engine.run_once()
        self.assertEqual(result["checked"], 4)
        self.assertEqual(runner.targets, list(supervisor.SOURCE_ORDER))
        self.assertEqual(again["checked"], 0)

    def test_validation_pending_never_replays_completed_source(self):
        runner = FakeRunner(ac=True, idle_seconds=600)
        engine = self.make_supervisor(runner)
        state.update_source("dingtalk_chat", self.database, status="deferred", error_code="validation_inputs_changed", next_due=state.now_iso(self.current))
        with patch.object(supervisor, "maintenance_status", side_effect=self.inactive_maintenance), patch.object(supervisor, "MINUTES_AUTOMATION_STATE", self.minutes_state), patch.object(engine, "reconcile_attention"):
            engine.run_once()
            self.current += timedelta(hours=1)
            engine.run_once()
        self.assertNotIn("dingtalk_chat", runner.targets)
        self.assertEqual(state.source("dingtalk_chat", self.database)["attempt_count"], 0)

    def test_first_source_failure_releases_worker_and_does_not_starve_later_sources(self):
        runner = FakeRunner(ac=True, idle_seconds=600, failures={"dingtalk_chat": "network_timeout"})
        engine = self.make_supervisor(runner)
        with patch.object(supervisor, "maintenance_status", side_effect=self.inactive_maintenance), patch.object(
            supervisor, "MINUTES_AUTOMATION_STATE", self.minutes_state
        ):
            result = engine.run_once()
        self.assertEqual(result["checked"], 4)
        self.assertEqual(runner.targets, list(supervisor.SOURCE_ORDER))
        self.assertEqual(state.source("dingtalk_chat", self.database)["status"], "retrying")
        self.assertTrue(all(
            state.source(source_id, self.database)["status"] == "healthy"
            for source_id in supervisor.SOURCE_ORDER[1:]
        ))

    def test_completion_heartbeat_prevents_long_run_from_being_mistaken_for_wake(self):
        current_clock = [self.current]
        runner = FakeRunner(ac=True, idle_seconds=600)
        def timed_runner(arguments, timeout):
            result = runner(arguments, timeout)
            if arguments and arguments[0] == "/usr/bin/caffeinate":
                current_clock[0] += timedelta(minutes=1)
            return result
        engine = self.make_supervisor(timed_runner, clock=lambda: current_clock[0])
        with patch.object(supervisor, "classify_receipt", return_value={"kind":"success"}), patch.object(supervisor, "chat_checkpoint_progress", return_value={"status":"complete"}), patch.object(supervisor, "maintenance_status", side_effect=self.inactive_maintenance), patch.object(
            supervisor, "MINUTES_AUTOMATION_STATE", self.minutes_state
        ):
            first = engine.run_once()
            current_clock[0] = self.current + timedelta(minutes=5, seconds=30)
            second = engine.run_once()
        self.assertEqual(first["checked"], 4)
        self.assertEqual(second["checked"], 0)
        self.assertEqual(
            state.runtime(self.database)["heartbeat_at"],
            state.now_iso(self.current + timedelta(minutes=5, seconds=30)),
        )
        self.assertEqual(runner.targets, list(supervisor.SOURCE_ORDER))

    def test_battery_and_recent_activity_defer_without_counting_failure(self):
        battery = self.make_supervisor(FakeRunner(ac=False, idle_seconds=600))
        with patch.object(supervisor, "maintenance_status", side_effect=self.inactive_maintenance), patch.object(
            supervisor, "MINUTES_AUTOMATION_STATE", self.minutes_state
        ):
            result = battery.run_once()
        self.assertEqual(result["status"], "waiting_power")
        self.assertTrue(all(row["attempt_count"] == 0 for row in state.sources(self.database)))

        state.mark_all_due(self.current, self.database)
        idle = self.make_supervisor(FakeRunner(ac=True, idle_seconds=30))
        with patch.object(supervisor, "maintenance_status", side_effect=self.inactive_maintenance), patch.object(
            supervisor, "MINUTES_AUTOMATION_STATE", self.minutes_state
        ):
            result = idle.run_once()
        self.assertEqual(result["status"], "waiting_idle")
        self.assertTrue(all(row["attempt_count"] == 0 for row in state.sources(self.database)))

    def test_meaningful_idle_ignores_pure_pointer_motion(self):
        observed = []

        def query(event_type):
            observed.append(event_type)
            return 600

        self.assertEqual(supervisor.meaningful_idle_seconds(query), 600)
        self.assertNotIn(5, observed)
        self.assertIn(10, observed)
        self.assertIn(22, observed)

    def test_transient_backoff_becomes_persistent_then_keeps_six_hour_probe(self):
        engine = self.make_supervisor(FakeRunner())
        delays = []
        for index in range(5):
            current = self.current + timedelta(hours=index * 2)
            row = engine.record_outcome(
                "win11", {"run_id": f"00000000-0000-4000-8000-{index:012d}", "status": "failed", "error_code": "network_timeout"}, current
            )
            delays.append(state.parse_time(row["next_due"]) - current)
        self.assertEqual(delays[:4], list(supervisor.RETRY_DELAYS))
        self.assertEqual(delays[4], supervisor.PERSISTENT_PROBE)
        self.assertEqual(row["status"], "persistent_failure")
        self.assertEqual(state.open_alert_count(self.database), 1)

    def test_chat_concurrent_change_stops_repeated_source_attempts(self):
        engine = self.make_supervisor(FakeRunner())
        delays = []
        for index in range(4):
            current = self.current + timedelta(minutes=index * 5)
            row = engine.record_outcome(
                "dingtalk_chat",
                {
                    "run_id": f"40000000-0000-4000-8000-{index:012d}",
                    "status": "failed",
                    "error_code": "chat_reconcile_rollback_concurrent_change",
                },
                current,
            )
            delays.append(row["next_due"])
        self.assertEqual(delays, [None] * 4)
        self.assertEqual(row["status"], "manual_required")
        self.assertEqual(row["attempt_count"], 4)

    def test_twelve_and_five_consecutive_failures_are_both_red_open_alerts(self):
        engine = self.make_supervisor(FakeRunner())
        state.update_source("dingtalk_mail", self.database, status="retrying", attempt_count=11)
        state.update_source("win11", self.database, status="retrying", attempt_count=4)
        mail = engine.record_outcome(
            "dingtalk_mail",
            {"run_id": '772ae0b4-63f9-5650-bd68-0d87e082f2dc', "status": "failed", "error_code": "fixture_failure"},
            self.current,
        )
        win11 = engine.record_outcome(
            "win11",
            {"run_id": '06a4752d-063d-553f-9400-3f3477dc5889', "status": "failed", "error_code": "fixture_failure"},
            self.current,
        )
        alerts = state.alerts(self.database, status="open")
        self.assertEqual(mail["status"], "persistent_failure")
        self.assertEqual(mail["attempt_count"], 12)
        self.assertEqual(win11["status"], "persistent_failure")
        self.assertEqual(win11["attempt_count"], 5)
        self.assertEqual({row["source_id"] for row in alerts}, {"dingtalk_mail", "win11"})
        self.assertTrue(all(row["severity"] == "error" for row in alerts))

    def test_media_waits_30m_2h_6h_24h_then_recovers(self):
        engine = self.make_supervisor(FakeRunner())
        delays = []
        for index in range(4):
            current = self.current + timedelta(days=index)
            row = engine.record_outcome(
                "dingtalk_minutes",
                {
                    "run_id": f"10000000-0000-4000-8000-{index:012d}", "status": "partial",
                    "sources": {"dingtalk_minutes": {"recording_media_pending": 2}},
                },
                current,
            )
            delays.append(state.parse_time(row["next_due"]) - current)
        self.assertEqual(delays, list(supervisor.MEDIA_DELAYS))
        self.assertEqual(row["status"], "waiting_source")
        self.assertEqual(state.open_alert_count(self.database), 1)
        recovered = engine.record_outcome(
            "dingtalk_minutes",
            {"run_id": '0cc79393-060a-54e5-ad45-2a28c8e4c5f5', "status": "noop"},
            self.current + timedelta(days=5),
        )
        self.assertEqual(recovered["status"], "healthy")
        self.assertEqual(state.open_alert_count(self.database), 0)
        self.assertTrue(any(row["category"] == "recovery" for row in state.alerts(self.database)))

    def test_manual_email_attention_is_not_retried_and_ack_does_not_resolve(self):
        FakeDashboard.mail_attention = 6
        engine = self.make_supervisor(FakeRunner())
        engine.reconcile_attention({}, self.current)
        row = state.source("dingtalk_mail", self.database)
        self.assertEqual(row["status"], "manual_required")
        alert = state.alerts(self.database, status="open")[0]
        acknowledged = state.acknowledge(alert["alert_id"], self.database)
        self.assertIsNotNone(acknowledged["acknowledged_at"])
        self.assertEqual(acknowledged["status"], "open")
        FakeDashboard.mail_attention = 0

    def test_read_safety_failure_becomes_manual_gate_until_fresh_confirmation(self):
        engine = self.make_supervisor(FakeRunner())
        row = engine.record_outcome(
            "dingtalk_mail",
            {
                "run_id": 'e500851f-e0b5-506a-b98c-48e6312bde23',
                "status": "failed",
                "error_code": "read_safety_unconfirmed",
            },
            self.current,
        )
        self.assertEqual(row["status"], "manual_required")
        self.assertEqual(row["error_code"], "read_safety_unconfirmed")
        alert = state.alerts(self.database, status="open")[0]
        self.assertIn("人工安全确认", alert["title"])
        with patch.object(
            supervisor.email_sync, "read_safety_status",
            return_value={"binding_valid": True, "server_side_pass": True, "cross_device_confirmed": True},
        ):
            engine.reconcile_read_safety({"email_sync": {}}, self.current + timedelta(minutes=1))
        ready = state.source("dingtalk_mail", self.database)
        self.assertEqual(ready["status"], "pending")
        self.assertIsNone(ready["error_code"])
        self.assertEqual(state.open_alert_count(self.database), 1)

    def test_read_safety_gate_cannot_be_overwritten_by_attachment_attention(self):
        engine = self.make_supervisor(FakeRunner())
        engine.record_outcome(
            "dingtalk_mail",
            {
                "run_id": '445e1eb6-8de5-5f38-9fe1-969b732f2629',
                "status": "failed",
                "error_code": "read_safety_unconfirmed",
            },
            self.current,
        )
        FakeDashboard.mail_attention = 6
        engine.reconcile_attention({}, self.current + timedelta(minutes=1))
        with_attachments = state.source("dingtalk_mail", self.database)
        self.assertEqual(with_attachments["status"], "manual_required")
        self.assertEqual(with_attachments["error_code"], "read_safety_unconfirmed")
        self.assertEqual(with_attachments["attention_count"], 6)
        published = {
            row["category"]: state.public_alert(row, source_state=with_attachments)
            for row in state.alerts(self.database, status="open")
        }
        self.assertEqual(
            published["sync_failure"]["action_required"],
            "email_read_safety_confirmation",
        )
        self.assertIsNone(published["manual_attention"]["action_required"])

        FakeDashboard.mail_attention = 0
        engine.reconcile_attention({}, self.current + timedelta(minutes=2))
        without_attachments = state.source("dingtalk_mail", self.database)
        self.assertEqual(without_attachments["status"], "manual_required")
        self.assertEqual(without_attachments["error_code"], "read_safety_unconfirmed")

    def test_invalid_mail_safety_proactively_overrides_stale_technical_state(self):
        engine = self.make_supervisor(FakeRunner())
        state.update_source(
            "dingtalk_mail", self.database, status="persistent_failure",
            attempt_count=15, error_code="email_items_failed",
        )
        with patch.object(
            supervisor.email_sync, "read_safety_status",
            return_value={
                "binding_valid": False,
                "server_side_pass": False,
                "cross_device_confirmed": False,
            },
        ):
            engine.reconcile_read_safety({"email_sync": {}}, self.current)
        mail = state.source("dingtalk_mail", self.database)
        alert = state.alerts(self.database, status="open")[0]
        self.assertEqual(mail["status"], "manual_required")
        self.assertEqual(mail["error_code"], "read_safety_unconfirmed")
        self.assertEqual(mail["attempt_count"], 15)
        self.assertIn("人工安全确认", alert["title"])

    def test_phase_attachment_attention_does_not_park_mail(self):
        FakeDashboard.mail_attention = 2
        engine = self.make_supervisor(FakeRunner())
        state.update_source("dingtalk_mail", self.database, status="manual_required", error_code="attachment_review_required")
        engine._reconcile_attention({"email_sync":{"phased_processing":True}}, self.current)
        row = state.source("dingtalk_mail", self.database)
        self.assertEqual("healthy", row["status"])
        self.assertEqual(2, row["attention_count"])

    def test_manual_attention_stays_visible_while_heavy_work_waits_for_idle(self):
        FakeDashboard.mail_attention = 6
        engine = self.make_supervisor(FakeRunner(ac=True, idle_seconds=30))
        with patch.object(supervisor, "maintenance_status", side_effect=self.inactive_maintenance), patch.object(
            supervisor, "MINUTES_AUTOMATION_STATE", self.minutes_state
        ):
            result = engine.run_once()
        self.assertEqual(result["status"], "waiting_idle")
        self.assertEqual(state.source("dingtalk_mail", self.database)["status"], "manual_required")
        self.assertEqual(state.open_alert_count(self.database), 1)
        FakeDashboard.mail_attention = 0

    def test_technical_email_failure_cannot_be_overwritten_by_manual_attention(self):
        FakeDashboard.mail_attention = 4
        FakeDashboard.mail_technical = 3
        engine = self.make_supervisor(FakeRunner())
        engine.reconcile_attention({}, self.current)
        first = state.source("dingtalk_mail", self.database)
        engine.reconcile_attention({}, self.current + timedelta(minutes=1))
        repeated = state.source("dingtalk_mail", self.database)
        self.assertEqual(first["status"], "retrying")
        self.assertEqual(first["error_code"], "email_items_failed")
        self.assertEqual(first["attention_count"], 4)
        self.assertEqual(repeated["attempt_count"], 0)
        self.assertEqual(repeated["last_attempt"], first["last_attempt"])
        self.assertEqual(repeated["next_due"], first["next_due"])
        FakeDashboard.mail_attention = 0
        FakeDashboard.mail_technical = 0

    def test_shared_ai_wait_preserves_failure_budget(self):
        engine = self.make_supervisor(FakeRunner())
        failed = engine.record_outcome("dingtalk_minutes", {
            "run_id": "failure", "status": "failed", "error_code": "recording_queue_not_drained",
        }, self.current)
        for index in range(1, 6):
            deferred = engine.record_outcome("dingtalk_minutes", {
                "run_id": f"shared-wait-{index}", "status": "deferred", "error_code": "chat_ai_priority",
            }, self.current + timedelta(minutes=index))
            self.assertEqual(deferred["attempt_count"], failed["attempt_count"])
            self.assertEqual(deferred["last_failure"], failed["last_failure"])

    def test_old_mail_failure_does_not_consume_retries_while_validation_waits(self):
        FakeDashboard.mail_attention = FakeDashboard.mail_technical = 1
        engine = self.make_supervisor(FakeRunner())
        failed = engine.record_outcome("dingtalk_mail", {
            "run_id": "actual-mail-failure", "status": "failed",
            "error_code": "email_items_failed",
        }, self.current)
        for index in range(1, 6):
            current = self.current + timedelta(minutes=index * 2)
            deferred = engine.record_outcome("dingtalk_mail", {
                "run_id": f"validation-wait-{index}", "status": "deferred",
                "error_code": "memory_core_unverified",
            }, current)
            for offset in (0, 10, 30):
                engine._reconcile_attention({}, current + timedelta(seconds=offset))
            observed = state.source("dingtalk_mail", self.database)
            for key in ("status", "error_code", "next_due", "last_attempt", "last_run_id", "blocked_since"):
                self.assertEqual(observed[key], deferred[key], key)
            self.assertEqual(observed["attempt_count"], 1)
            self.assertEqual(observed["last_failure"], failed["last_failure"])
            self.assertEqual(observed["attention_count"], 1)
        self.assertFalse(any(a["category"] == "sync_failure" for a in state.alerts(self.database)))

    def test_mail_observation_preserves_waiting_and_active_run_receipts(self):
        FakeDashboard.mail_attention = FakeDashboard.mail_technical = 1
        engine = self.make_supervisor(FakeRunner())
        for status in ("running", "waiting_power", "waiting_idle", "retrying", "persistent_failure"):
            with self.subTest(status=status):
                before = state.update_source("dingtalk_mail", self.database,
                    status=status, attempt_count=3, error_code="email_items_failed",
                    last_run_id="actual-run", last_attempt=state.now_iso(self.current),
                    last_failure=state.now_iso(self.current),
                    next_due=state.now_iso(self.current + timedelta(minutes=15)))
                for minutes in (1, 2):
                    engine._reconcile_attention({}, self.current + timedelta(minutes=minutes))
                after = state.source("dingtalk_mail", self.database)
                for key in ("status", "attempt_count", "error_code", "last_run_id", "last_attempt", "last_failure", "next_due"):
                    self.assertEqual(after[key], before[key], key)

    def test_actual_mail_failures_still_back_off_after_snapshot_refresh(self):
        FakeDashboard.mail_attention = FakeDashboard.mail_technical = 1
        engine = self.make_supervisor(FakeRunner())
        for attempt, delay in enumerate((1, 5, 15, 60, 360), 1):
            current = self.current + timedelta(hours=attempt)
            actual = engine.record_outcome("dingtalk_mail", {
                "run_id": f"actual-failure-{attempt}", "status": "failed",
                "error_code": "email_items_failed",
            }, current)
            engine._reconcile_attention({}, current + timedelta(seconds=10))
            observed = state.source("dingtalk_mail", self.database)
            self.assertEqual(observed["attempt_count"], attempt)
            self.assertEqual(state.parse_time(observed["next_due"]), current + timedelta(minutes=delay))
            self.assertEqual(observed["last_attempt"], actual["last_attempt"])
        self.assertTrue(any(a["category"] == "sync_failure" for a in state.alerts(self.database)))

    def test_memory_core_failure_is_one_red_alert_and_recovery_is_explicit(self):
        engine = self.make_supervisor(FakeRunner())
        FakeDashboard.core_status = "failed"
        engine.reconcile_attention({}, self.current)
        engine.reconcile_attention({}, self.current + timedelta(minutes=1))
        open_alerts = state.alerts(self.database, status="open")
        self.assertEqual(len(open_alerts), 1)
        self.assertEqual(open_alerts[0]["source_id"], "memory_core")
        self.assertEqual(open_alerts[0]["category"], "core_failure")
        self.assertEqual(open_alerts[0]["severity"], "error")
        self.assertEqual(open_alerts[0]["occurrence_count"], 2)

        FakeDashboard.core_status = "healthy"
        FakeDashboard.core_last_success = state.now_iso(self.current - timedelta(minutes=1))
        engine.reconcile_attention({}, self.current + timedelta(minutes=2))
        self.assertEqual(
            state.open_alert_count(self.database), 1,
            "an old green receipt must not close a newer core incident",
        )
        FakeDashboard.core_last_success = state.now_iso(self.current + timedelta(seconds=30))
        engine.reconcile_attention({}, self.current + timedelta(minutes=2, seconds=10))
        self.assertEqual(state.open_alert_count(self.database), 1,
                         'a receipt after the first fault but before the latest failed observation cannot recover')
        FakeDashboard.core_last_success = state.now_iso(self.current + timedelta(minutes=3))
        engine.reconcile_attention({}, self.current + timedelta(minutes=3))
        self.assertEqual(state.open_alert_count(self.database), 0)
        recovery = next(row for row in state.alerts(self.database) if row["category"] == "recovery")
        self.assertEqual(recovery["title"], "记忆库核心已恢复")

    def test_core_alert_is_maintenance_pending_without_losing_history(self):
        alert = state.open_alert(
            "memory_core", "core_failure", "核心失败", "等待复验",
            severity="error", database=self.database, at=self.current,
        )
        public = state.public_alert(alert, maintenance_active=True)
        self.assertEqual(public["effective_state"], "maintenance_pending")
        self.assertEqual(alert["status"], "open")
        pending = state.public_alert(alert, post_maintenance_pending=True)
        self.assertEqual(pending["effective_state"], "maintenance_pending")
        active = state.public_alert(alert, maintenance_active=False)
        self.assertEqual(active["effective_state"], "critical")

    def test_core_permission_failure_is_blocked_without_automatic_attempt(self):
        engine = self.make_supervisor(FakeRunner())
        FakeDashboard.core_status = 'failed'
        FakeDashboard.core_validation_error = 'validation_permission_denied'
        engine.reconcile_attention({}, self.current)
        row = state.alerts(self.database, status='open')[0]
        self.assertEqual(row['error_code'], 'validation_permission_denied')
        self.assertEqual(row['repair_status'], 'blocked')
        self.assertEqual(row['repair_attempt_count'], 0)
        engine.reconcile_attention({}, self.current + timedelta(minutes=1))
        self.assertEqual(state.open_alert_count(self.database), 1)

    def test_normal_validation_pending_does_not_open_or_renew_a_fault(self):
        engine = self.make_supervisor(FakeRunner())
        FakeDashboard.core_status = "unknown"
        FakeDashboard.core_acceptance = "pending"
        engine.reconcile_attention({}, self.current)
        self.assertEqual(state.open_alert_count(self.database), 0)
        alert = state.open_alert("memory_core", "core_failure", "existing fault", "retained",
                                 database=self.database, at=self.current - timedelta(minutes=1))
        engine.reconcile_attention({}, self.current + timedelta(minutes=1))
        row = state.alerts(self.database, status="open")[0]
        self.assertEqual(row["last_seen"], alert["last_seen"])
        self.assertEqual(row["occurrence_count"], alert["occurrence_count"])
        self.assertEqual(row["status"], "open")

    def test_automatic_timeout_preserves_its_owned_failure_code(self):
        engine = self.make_supervisor(FakeRunner())
        FakeDashboard.core_status = "failed"
        FakeDashboard.core_validation_error = "automatic_validation_timeout"
        engine.reconcile_attention({}, self.current)
        row = state.alerts(self.database, status="open")[0]
        self.assertEqual(row["error_code"], "automatic_validation_timeout")
        self.assertEqual(row["repair_attempt_count"], 0)

    def test_claim_base_mismatch_preserves_its_owned_failure_code(self):
        engine = self.make_supervisor(FakeRunner())
        FakeDashboard.core_status = "failed"
        FakeDashboard.core_validation_error = "claim_base_mismatch"
        engine.reconcile_attention({}, self.current)
        row = state.alerts(self.database, status="open")[0]
        self.assertEqual(row["error_code"], "claim_base_mismatch")
        self.assertNotEqual(row["error_code"], "automatic_validation_timeout")

    def test_claim_base_suffix_failure_preserves_its_owned_failure_code(self):
        engine = self.make_supervisor(FakeRunner())
        FakeDashboard.core_status = "failed"
        FakeDashboard.core_validation_error = "claim_base_untracked_suffix"
        engine.reconcile_attention({}, self.current)
        row = state.alerts(self.database, status="open")[0]
        self.assertEqual(row["error_code"], "claim_base_untracked_suffix")
        self.assertNotEqual(row["error_code"], "validation_failed")

    def test_full_validation_precedes_attention_and_is_skipped_for_fixtures(self):
        import automatic_memory_validation
        engine = self.make_supervisor(FakeRunner())
        config = {"memory_root": "fixture", "external_root": "fixture", "dingtalk_automation": {"enabled": True}}
        with patch.object(automatic_memory_validation, "run_once") as actual:
            self.assertIsNone(engine.schedule_full_validation(config))
            actual.assert_not_called()
        sequence = []
        def validate(_config):
            sequence.append("validate")
            self.current += timedelta(seconds=30)
            return {"status": "verified"}
        with patch.object(engine, "schedule_full_validation", side_effect=validate), \
                patch.object(engine, "_reconcile_attention", side_effect=lambda _config, at: sequence.append(("attention", at))):
            engine.reconcile_attention({}, self.current)
        self.assertEqual(sequence, ["validate", ("attention", self.current)])

    def test_runtime_error_receipt_is_diagnostic_without_exception_content(self):
        secret = "do-not-store-private-message-or-token"
        try:
            raise sqlite3.OperationalError("database is locked " + secret)
        except sqlite3.Error as exc:
            result = supervisor.record_runtime_error(exc, root=self.root, at=self.current)
        self.assertEqual(result["reason"], "sqlite_database_busy")
        self.assertTrue(result["frames"])
        receipt = self.root / "state/sync-supervisor-last-error.json"
        history = self.root / "state/sync-supervisor-errors.jsonl"
        self.assertNotIn(secret, receipt.read_text() + history.read_text())
        self.assertNotIn("raise sqlite3", receipt.read_text())
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)

    def test_exhausted_media_migrates_to_daily_source_wait_without_being_hidden_by_idle(self):
        self.minutes_state.write_text(
            '{"schema_version":2,"items":{"safe-item":{"media_retry_exhausted":true}}}\n',
            encoding="utf-8",
        )
        engine = self.make_supervisor(FakeRunner(ac=True, idle_seconds=30))
        with patch.object(supervisor, "maintenance_status", side_effect=self.inactive_maintenance), patch.object(
            supervisor, "MINUTES_AUTOMATION_STATE", self.minutes_state
        ):
            result = engine.run_once()
        row = state.source("dingtalk_minutes", self.database)
        self.assertEqual(result["status"], "waiting_idle")
        self.assertEqual(row["status"], "waiting_source")
        self.assertEqual(row["probe_count"], 4)
        self.assertEqual(state.alerts(self.database, status="open")[0]["category"], "waiting_source")

    def test_media_source_wait_closes_old_execution_failure_without_claiming_full_sync(self):
        state.open_alert('dingtalk_minutes', 'sync_failure', 'old failure', 'fixture',
                         database=self.database, at=self.current)
        engine = self.make_supervisor(FakeRunner(ac=True, idle_seconds=300))
        row = engine.record_outcome('dingtalk_minutes', {'status': 'partial', 'sources': {
            'dingtalk_minutes': {'recording_media_pending': 2}}}, self.current)
        self.assertEqual(row['status'], 'waiting_source')
        self.assertEqual(row['attention_count'], 2)
        self.assertFalse(any(x['category'] == 'sync_failure' for x in state.alerts(self.database, status='open')))

    def test_alerts_are_deduplicated_and_each_incident_is_delivered_once(self):
        first = state.open_alert(
            "dingtalk_chat", "sync_failure", "持续失败", "安全详情", database=self.database, at=self.current
        )
        second = state.open_alert(
            "dingtalk_chat", "sync_failure", "持续失败", "安全详情", database=self.database,
            at=self.current + timedelta(minutes=5),
        )
        self.assertEqual(first["alert_id"], second["alert_id"])
        self.assertEqual(second["occurrence_count"], 2)
        state.claim_delivery(first["alert_id"], self.database, self.current)
        state.mark_delivery(first["alert_id"], "macos", "visible", database=self.database, at=self.current)
        state.mark_delivery(
            first["alert_id"], "miso", "delivered",
            event_id="miso_" + "1" * 64, receipt_hash="a"*64, database=self.database, at=self.current,
        )
        self.assertEqual(state.due_alerts(self.database, self.current + timedelta(hours=5)), [])
        self.assertEqual(len(state.due_alerts(self.database, self.current + timedelta(hours=6))), 1)
        self.assertTrue(state.public_alert(state.alerts(self.database)[0])["delivery"]["miso"]["receipt_confirmed"])

    def test_interrupted_delivery_resumes_only_the_pending_channel(self):
        alert = state.open_alert(
            "dingtalk_chat", "sync_failure", "持续失败", "安全详情",
            database=self.database, at=self.current,
        )
        state.claim_delivery(alert["alert_id"], self.database, self.current)
        state.mark_delivery(alert["alert_id"], "macos", "visible", database=self.database, at=self.current)
        calls = []

        def mark_miso(row, database):
            calls.append("miso")
            state.mark_delivery(
                row["alert_id"], "miso", "delivered",
                event_id="miso_" + "2" * 64, database=database,
            )

        with patch.object(state, "now_dt", return_value=self.current+timedelta(minutes=10)), patch.object(supervisor, "deliver_macos", side_effect=lambda *_args: calls.append("macos")), patch.object(
            supervisor, "deliver_miso", side_effect=mark_miso
        ):
            supervisor.deliver_due_alerts({}, self.database)
        self.assertEqual(calls, ["miso"])
        self.assertEqual(state.due_alerts(self.database, self.current + timedelta(hours=5)), [])

    def test_delivery_loop_preserves_owner_runtime_heartbeat(self):
        alert = state.open_alert(
            "dingtalk_chat", "sync_failure", "持续失败", "安全详情",
            database=self.database, at=self.current,
        )
        state.update_runtime(
            self.database, heartbeat_at=state.now_iso(self.current - timedelta(hours=1))
        )

        def mark_macos(row, database):
            state.mark_delivery(row["alert_id"], "macos", "requested", database=database)

        def mark_miso(row, database):
            state.mark_delivery(
                row["alert_id"], "miso", "delivered",
                event_id="miso_" + "6" * 64, database=database,
            )

        with patch.object(supervisor, "deliver_macos", side_effect=mark_macos), patch.object(
            supervisor, "deliver_miso", side_effect=mark_miso
        ):
            supervisor.deliver_due_alerts({}, self.database)
        heartbeat = state.parse_time(state.runtime(self.database)["heartbeat_at"])
        self.assertIsNotNone(heartbeat)
        self.assertEqual(heartbeat, self.current - timedelta(hours=1))

    def test_incident_does_not_repeat_before_six_hours(self):
        alert = state.open_alert(
            "win11", "sync_failure", "持续失败", "安全详情",
            database=self.database, at=self.current,
        )
        state.claim_delivery(alert["alert_id"], self.database, self.current)
        state.mark_delivery(alert["alert_id"], "macos", "visible", database=self.database, at=self.current)
        state.mark_delivery(
            alert["alert_id"], "miso", "delivered",
            event_id="miso_" + "3" * 64, database=self.database, at=self.current,
        )
        calls = []

        with patch.object(supervisor.state, "now_dt", return_value=self.current + timedelta(hours=5)), patch.object(
            supervisor, "deliver_macos", side_effect=lambda *_args: calls.append("macos")
        ), patch.object(supervisor, "deliver_miso", side_effect=lambda *_args: calls.append("miso")):
            supervisor.deliver_due_alerts({}, self.database)
        self.assertEqual(calls, [])

    def test_warning_escalation_redelivers_both_channels_once(self):
        warning = state.open_alert(
            "system", "power_probe", "电源警告", "安全详情",
            severity="warning", database=self.database, at=self.current,
        )
        state.mark_delivery(
            warning["alert_id"], "macos", "requested", database=self.database, at=self.current,
        )
        state.mark_delivery(
            warning["alert_id"], "miso", "delivered",
            event_id="miso_" + "5" * 64, database=self.database, at=self.current,
        )
        escalated = state.open_alert(
            "system", "power_probe", "电源严重故障", "安全详情",
            severity="error", database=self.database, at=self.current + timedelta(minutes=5),
        )
        self.assertEqual(escalated["alert_id"], warning["alert_id"])
        self.assertEqual(escalated["macos_status"], "pending")
        self.assertEqual(escalated["macos_attempt_count"], 0)
        self.assertEqual(escalated["miso_status"], "pending")
        self.assertIsNone(escalated["miso_event_id"])
        self.assertEqual(supervisor.miso_health_transition(escalated)["previous"], "attention")

    def test_macos_notification_keeps_chinese_text(self):
        alert = state.open_alert(
            "dingtalk_chat", "sync_failure", "持续失败", "安全详情",
            database=self.database, at=self.current,
        )
        with patch.object(supervisor.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, '{"status":"requested"}', "")
            supervisor.deliver_macos(alert, self.database)
        script = run.call_args.kwargs["input"]
        self.assertIn("钉钉聊天", script)
        self.assertEqual(run.call_args.args[0][-1], "--sync-notification")
        self.assertNotIn("示例企业", script)
        self.assertNotIn("\\u", script)

    def test_macos_missing_receipt_is_uncertain_and_not_blindly_resent(self):
        alert = state.open_alert(
            "dingtalk_chat", "sync_failure", "持续失败", "安全详情",
            database=self.database, at=self.current,
        )
        failed_result = subprocess.CompletedProcess([], 1, "", "request failed")
        with patch.object(supervisor.subprocess, "run", return_value=failed_result):
            supervisor.deliver_macos(alert, self.database)
            after_first = state.alerts(self.database)[0]
            self.assertEqual(after_first["macos_status"], "uncertain")
        after_second = state.alerts(self.database)[0]
        self.assertEqual(after_second["macos_status"], "uncertain")
        self.assertEqual(after_second["macos_attempt_count"], 1)

    def test_uncertain_miso_result_is_never_blindly_retried(self):
        alert = state.open_alert(
            "win11", "sync_failure", "持续失败", "安全详情",
            database=self.database, at=self.current,
        )
        state.mark_delivery(alert["alert_id"], "macos", "requested", database=self.database)
        state.mark_delivery(
            alert["alert_id"], "miso", "uncertain",
            event_id="miso_" + "4" * 64, error_code="transport_timeout", database=self.database,
        )
        due = state.due_alerts(self.database)
        self.assertEqual(len(due), 1)
        with patch.object(
            supervisor, "run_miso_bridge",
            return_value={"ok": True, "status": "uncertain", "error_code": "transport_timeout"},
        ) as bridge:
            supervisor.deliver_miso(due[0], self.database)
        self.assertEqual(bridge.call_args.args[0][0], "status")
        self.assertNotIn("emit", bridge.call_args.args[0])
        public = state.public_alert(state.alerts(self.database)[0])
        self.assertEqual(public["delivery"]["summary"], "failed")
        self.assertEqual(public["delivery"]["miso"]["status"], "uncertain")

    def test_unexpected_miso_enqueue_error_gets_a_terminal_receipt(self):
        alert = state.open_alert(
            "dingtalk_chat", "sync_failure", "持续失败", "安全详情",
            database=self.database, at=self.current,
        )
        with patch.object(
            supervisor, "run_miso_bridge", return_value={"ok": False, "reason": "miso_enqueue_failed"}
        ):
            supervisor.deliver_miso(alert, self.database)
        row = state.alerts(self.database)[0]
        self.assertEqual(row["miso_status"], "failed")
        self.assertEqual(row["miso_error_code"], "miso_enqueue_failed")
        self.assertEqual(len(state.due_alerts(self.database, self.current + timedelta(hours=24))), 1)

    def test_power_probe_uses_backup_and_unknown_is_not_battery(self):
        def fallback_runner(arguments, _timeout):
            if arguments[:3] == ["/usr/bin/pmset", "-g", "ps"]:
                return subprocess.CompletedProcess(arguments, 0, "localized output", "")
            if arguments[:3] == ["/usr/bin/pmset", "-g", "batt"]:
                return subprocess.CompletedProcess(arguments, 0, "Now drawing from 'AC Power'", "")
            return subprocess.CompletedProcess(arguments, 1, "", "")

        detected = supervisor.power_state(fallback_runner)
        self.assertTrue(detected["known"])
        self.assertTrue(detected["on_ac"])
        self.assertEqual(detected["probe"], "pmset_batt")

        def unknown_runner(arguments, _timeout):
            return subprocess.CompletedProcess(arguments, 0, "unrecognized", "")

        unknown = supervisor.power_state(unknown_runner)
        self.assertFalse(unknown["known"])
        self.assertIsNone(unknown["on_ac"])
        self.assertEqual(unknown["source"], "unknown")

    def test_unknown_power_recovery_to_ac_immediately_marks_sources_due(self):
        future = self.current + timedelta(hours=6)
        state.update_runtime(
            self.database, power_source="unknown", on_ac=False,
            power_error_code="power_probe_unknown", power_probe_failures=5,
        )
        for source_id in supervisor.SOURCE_ORDER:
            state.update_source(
                source_id, self.database, status="healthy", next_due=state.now_iso(future)
            )
        engine = self.make_supervisor(FakeRunner(ac=True, idle_seconds=30))
        with patch.object(
            supervisor, "maintenance_status", side_effect=self.inactive_maintenance
        ), patch.object(supervisor, "MINUTES_AUTOMATION_STATE", self.minutes_state):
            result = engine.run_once()
        self.assertEqual(result["status"], "waiting_idle")
        self.assertEqual(state.runtime(self.database)["pending_count"], len(supervisor.SOURCE_ORDER))
        self.assertTrue(all(
            state.parse_time(row["next_due"]) == self.current for row in state.sources(self.database)
        ))

    def test_persistent_unknown_power_reprobes_each_minute_and_opens_warning(self):
        class UnknownPowerRunner:
            def __call__(self, arguments, _timeout):
                if arguments[:4] == ["/usr/sbin/ioreg", "-c", "IOHIDSystem", "-d"]:
                    return subprocess.CompletedProcess(
                        arguments, 0, f'"HIDIdleTime" = {600 * 1_000_000_000}', ""
                    )
                return subprocess.CompletedProcess(arguments, 0, "unrecognized power data", "")

        clock_values = iter(
            value
            for index in range(5)
            for value in (
                self.current + timedelta(minutes=index),
                self.current + timedelta(minutes=index),
            )
        )
        engine = self.make_supervisor(UnknownPowerRunner(), clock=lambda: next(clock_values))
        with patch.object(supervisor, "maintenance_status", side_effect=self.inactive_maintenance), patch.object(
            supervisor, "MINUTES_AUTOMATION_STATE", self.minutes_state
        ):
            results = [engine.run_once() for _ in range(5)]
        self.assertTrue(all(result["status"] == "power_unknown" for result in results))
        runtime = state.runtime(self.database)
        self.assertEqual(runtime["power_source"], "unknown")
        self.assertFalse(runtime["on_ac"])
        self.assertEqual(runtime["power_probe_failures"], 5)
        alert = state.alerts(self.database, status="open")[0]
        self.assertEqual(alert["source_id"], "system")
        self.assertEqual(alert["category"], "power_probe")
        self.assertEqual(alert["severity"], "warning")
        self.assertTrue(all(row["status"] != "waiting_power" for row in state.sources(self.database)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
