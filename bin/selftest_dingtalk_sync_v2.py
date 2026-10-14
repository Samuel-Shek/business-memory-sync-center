#!/usr/bin/env python3
"""Offline acceptance tests for DingTalk sync v2 state and delivery semantics."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import json
import os
import sqlite3
import tempfile
import uuid
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import dingtalk_sync_v2 as syncv2


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "dingtalk-sync-v2"
        syncv2.ROOT = root
        syncv2.STATE_DIR = root / "state"
        syncv2.RUNS_DIR = root / "runs"
        syncv2.ARCHIVE_DIR = root / "decision-archive"
        syncv2.DATABASE = syncv2.STATE_DIR / "sync.sqlite"
        syncv2.AI_LOCK = syncv2.STATE_DIR / "ai.lock"
        syncv2.SOURCE_LOCK = syncv2.STATE_DIR / "source.lock"
        syncv2.SEMANTIC_LOCK = syncv2.STATE_DIR / "semantic.lock"
        syncv2.DECISION_LOCK = syncv2.STATE_DIR / "decision.lock"
        syncv2.initialize()
        check(syncv2.DATABASE.stat().st_mode & 0o777 == 0o600, "database is private")
        connection = syncv2.connect_readonly()
        try:
            schema_stamp_before = str(connection.execute(
                "SELECT updated_at FROM metadata WHERE key='case_conversation_schema_version'"
            ).fetchone()[0])
        finally:
            connection.close()
        syncv2.initialize()
        connection = syncv2.connect_readonly()
        try:
            schema_stamp_after = str(connection.execute(
                "SELECT updated_at FROM metadata WHERE key='case_conversation_schema_version'"
            ).fetchone()[0])
        finally:
            connection.close()
        check(schema_stamp_after == schema_stamp_before, "repeated initialization preserves schema evidence timestamp")
        reader = syncv2.connect_readonly()
        try:
            check(reader.execute("PRAGMA query_only").fetchone()[0] == 1, "WAL reader is query-only after opening required sidecars")
            try:
                reader.execute("INSERT INTO metadata(key,value) VALUES('forbidden','1')")
                raise AssertionError("query-only reader accepted a write")
            except sqlite3.OperationalError:
                pass
        finally:
            reader.close()
        check(True, "query-only reader rejects writes")

        first, created = syncv2.emit_outbox("recording_synced", "same-event", {"count": 1})
        second, duplicate_created = syncv2.emit_outbox("recording_synced", "same-event", {"count": 1})
        check(created and not duplicate_created and first == second, "outbox deduplicates one logical event")
        syncv2.mark_outbox(first, "delivered", receipt_hash="receipt")
        syncv2.mark_outbox(first, "uncertain", error_code="late_timeout")
        connection = syncv2.connect()
        try:
            row = connection.execute("SELECT status,receipt_hash,attempts FROM outbox WHERE event_id=?", (first,)).fetchone()
        finally:
            connection.close()
        check(row["status"] == "delivered" and row["receipt_hash"] == "receipt", "late uncertainty cannot downgrade delivery")
        check(int(row["attempts"]) == 1, "ignored stale transition does not increment attempts")

        connection = syncv2.connect_readonly()
        try:
            outbox_before = int(connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])
        finally:
            connection.close()
        os.environ["DINGTALK_LOCAL_ONLY"] = "1"
        try:
            local_event, local_created = syncv2.emit_outbox(
                "sync_cycle_completed", "manual-local-only", {"count": 9}
            )
        finally:
            os.environ.pop("DINGTALK_LOCAL_ONLY", None)
        connection = syncv2.connect_readonly()
        try:
            outbox_after = int(connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])
        finally:
            connection.close()
        check(
            local_event.startswith("local-only-") and not local_created and outbox_after == outbox_before,
            "manual local-only mode cannot add any DingTalk outbox event",
        )

        failure = syncv2.set_component_health(
            "recording_source",
            False,
            "source_down",
            safe_detail={
                "phase": "source_check",
                "reason": "source check exceeded its time budget",
                "auto_repair": "retry_exhausted",
                "attempts": 3,
                "timeout_seconds": 30,
                "next_retry_at": "2020-01-02T09:44:44+08:00",
                "ignored": "must not leave the allowlist",
            },
        )
        repeat = syncv2.set_component_health(
            "recording_source",
            False,
            "source_down",
            safe_detail={"attempts": 4, "next_retry_at": "2020-01-02T11:44:44+08:00"},
        )
        refreshed_failure = syncv2.component_health_state("recording_source")
        recovery = syncv2.set_component_health("recording_source", True)
        check(bool(failure) and repeat is None and bool(recovery), "health emits first failure and first recovery only")
        check(
            refreshed_failure["attempts"] == 4
            and refreshed_failure["next_retry_at"] == "2020-01-02T11:44:44+08:00"
            and refreshed_failure["reason"] == "source check exceeded its time budget",
            "repeated failure refreshes retry details without losing the original safe cause",
        )
        connection = syncv2.connect_readonly()
        try:
            failure_payload = json.loads(
                connection.execute("SELECT safe_payload_json FROM outbox WHERE event_id=?", (failure,)).fetchone()[0]
            )
            recovery_payload = json.loads(
                connection.execute("SELECT safe_payload_json FROM outbox WHERE event_id=?", (recovery,)).fetchone()[0]
            )
        finally:
            connection.close()
        check(
            failure_payload["phase"] == "source_check"
            and failure_payload["attempts"] == 3
            and failure_payload["next_retry_at"] == "2020-01-02T09:44:44+08:00"
            and "ignored" not in failure_payload,
            "failure notification carries bounded cause and retry details",
        )
        check(
            recovery_payload["recovered_from_error_code"] == "source_down"
            and recovery_payload["repair_result"] == "recovered",
            "recovery notification explains which failure was repaired",
        )
        check(
            recovery_payload["reason"] == "source check exceeded its time budget"
            and recovery_payload["timeout_seconds"] == 30,
            "recovery notification preserves the safe cause and timeout",
        )
        media_failure = syncv2.set_component_health(
            "recording_media",
            False,
            "recording_media_pending",
            safe_detail={"phase": "media_lookup", "auto_repair": "scheduled_retry"},
        )
        mismatched = syncv2.recover_component_if_error("recording_media", "different_error")
        matched = syncv2.recover_component_if_error("recording_media", "recording_media_pending")
        check(bool(media_failure) and mismatched is None and bool(matched), "matching recovery cannot clear an unrelated incident")
        check(syncv2.component_health_state("recording_media")["status"] == "healthy", "matching recovery updates component health")

        incident = syncv2.record_incident("recording_not_visible_to_source_api", "window-1", {"start": "a", "end": "b"})
        check(bool(incident), "incident is recorded")
        run_id = syncv2.start_action_run("sync_now")
        syncv2.set_action_step(run_id, "prepare", 10, "准备同步", "running", current_count=0, total_count=2)
        syncv2.set_action_step(run_id, "prepare", 10, "准备同步", "success", current_count=2, total_count=2)
        syncv2.set_action_step(run_id, "fetch", 20, "同步内容", "queued")
        steps = syncv2.list_action_steps(run_id)
        check(steps[0]["status"] == "success" and steps[0]["current"] == 2, "action steps persist exact progress")
        syncv2.finish_action_run(run_id, "interrupted", error_code="test_interrupt")
        steps = syncv2.list_action_steps(run_id)
        check(steps[1]["status"] == "skipped", "interrupted action marks remaining steps skipped")
        automatic_run = syncv2.start_action_run("dingtalk_chat", trigger="automatic")
        connection = syncv2.connect_readonly()
        try:
            automatic_trigger = connection.execute(
                "SELECT trigger FROM action_runs WHERE run_id=?", (automatic_run,)
            ).fetchone()[0]
        finally:
            connection.close()
        check(automatic_trigger == "automatic", "action runs persist manual versus automatic trigger evidence")
        syncv2.set_action_trigger(automatic_run, "manual")
        connection = syncv2.connect_readonly()
        try:
            updated_trigger = connection.execute(
                "SELECT trigger FROM action_runs WHERE run_id=?", (automatic_run,)
            ).fetchone()[0]
        finally:
            connection.close()
        check(updated_trigger == "manual", "a pre-created action receives the controller's verified trigger")
        syncv2.finish_action_run(automatic_run, "noop")
        import bounded_repair_actions as repair
        import listener_manual_acceptance as listener
        with patch.object(repair, 'LOCK', root/'repair.lock'), \
             patch.object(example-host.invalid, 'maintenance_active', return_value=False), \
             patch.object(example-host.invalid.memory, 'load_config', return_value={}), \
             patch.object(example-host.invalid, 'wake_supervisor', return_value=False), \
             patch.object(example-host.invalid, 'supervisor_heartbeat', side_effect=nullcontext), \
             patch.object(example-host.invalid.controller, 'control_lock', side_effect=nullcontext), \
             patch.object(listener, 'gate', return_value={'available':True}), \
             patch.object(listener, 'approve', side_effect=RuntimeError('fixture blocked before subscription')):
            action_id, requested, alert_id = [str(uuid.uuid4()) for _ in range(3)]
            first = repair.start(root/'config.json', 'manual_listener_acceptance', alert_id, action_id, requested)
            repeated = repair.start(root/'config.json', 'manual_listener_acceptance', alert_id, action_id, requested)
            check(first['status']=='started' and repeated['status']=='already_running', 'manual listener request is registered once')
            result = repair.execute_queued(root/'config.json')
            check(result['status']=='blocked', 'provider startup is not called when approval fails')
            with syncv2.connect_readonly() as connection:
                status = connection.execute('SELECT status FROM action_runs WHERE run_id=?',(requested,)).fetchone()[0]
            check(status=='deferred', 'blocked repair finishes its action instead of leaving a phantom running task')
        connection = syncv2.connect_readonly()
        try:
            tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            connection.close()
        check({"action_steps", "decision_messages", "decision_drafts"}.issubset(tables), "sync center 2.0 tables are migrated")
        snapshot = syncv2.status_snapshot()
        check(snapshot["database_integrity"] == "ok" and snapshot["secure_delete"] == 1, "database safety checks pass")

        with syncv2.file_lock(syncv2.AI_LOCK):
            try:
                with syncv2.file_lock(syncv2.AI_LOCK):
                    raise AssertionError("second lock unexpectedly acquired")
            except syncv2.SyncV2Error as exc:
                check(exc.code == "already_running", "shared AI lock is nonblocking and exclusive")

    print('{"verdict":"pass","checks":23}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
