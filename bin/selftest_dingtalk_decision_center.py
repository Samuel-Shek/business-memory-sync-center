#!/usr/bin/env python3
"""Offline write/rollback-shape test for the local DingTalk decision center."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import json
import tempfile
import uuid
from pathlib import Path
from unittest import mock

import dingtalk_decision_center as center
import dingtalk_memory_automation as memory
import dingtalk_sync_v2 as syncv2


CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(message)
    CHECKS += 1


def note(memory_id: str, *, status: str, fact_status: str, source_check: str, source_ref: str, body: str) -> str:
    return memory.render_note(
        [
            ("memory_id", memory_id),
            ("category", "business_cognition"),
            ("memory_scope", "internal_nonpublic"),
            ("memory_kind", "status"),
            ("origin_type", "internal_communication"),
            ("status", status),
            ("evidence_level", "internal_source" if status == "active" else "unverified"),
            ("sensitivity", "confidential"),
            ("source_system", "dingtalk"),
            ("source_check", source_check),
            ("fact_status", fact_status),
            ("source_ref", source_ref),
            ("observed_at", "2020-01-02T10:00:00+08:00"),
            ("reviewed_at", "2020-01-02T10:00:00+08:00" if status == "active" else None),
            ("valid_until", None),
            ("supersedes", []),
            ("content_hash", ""),
            ("applies_to", ["project:business-energy"]),
        ],
        body,
    )


def main() -> int:
    sample_scan = " 100 /usr/bin/python3 /bin/dingtalk_idle_review.py\n 200 unrelated\n"
    check(
        not center.review_process_present_in_scan(sample_scan, {100})
        and center.review_process_present_in_scan(sample_scan)
        and center.review_process_present_in_scan(
            " 300 /usr/bin/python3 /AI/shared-skills/transcription-review/scripts/transcribe_dashscope.py\n"
        ),
        "stale recovery can ignore only its known scheduler caller",
    )
    available_state = {
        "global_action": {"available": True, "running": False, "target": "all"},
        "sources": [
            {"id": "win11", "control": {"available": True, "running": False, "target": "win11"}},
        ],
    }
    check(center.control_action_gate(available_state, "all") is None, "eligible global sync passes the backend gate")
    blocked_state = {
        "global_action": {
            "available": False, "running": False, "reason_code": "memory_core_unavailable",
            "reason": "记忆库核心尚未处于可写状态", "target": "all",
        },
        "sources": [],
    }
    try:
        center.control_action_gate(blocked_state, "all")
        raise AssertionError("unwritable memory core passed the backend gate")
    except center.DecisionError as exc:
        check(exc.code == "memory_core_unavailable" and exc.status == 409, "memory-core eligibility is enforced by the backend")
    running_id = '4c613550-c64d-538a-9a1f-37a3ae432cb5'
    running_state = {
        "global_action": {
            "available": False, "running": True, "run_id": running_id,
            "reason_code": "already_running", "reason": "已有同步正在运行", "target": "all",
        },
        "sources": [],
    }
    running_gate = center.control_action_gate(running_state, "all")
    check(running_gate and running_gate["status"] == "already_running" and running_gate["run_id"] == running_id, "backend returns the existing run for progress recovery")
    try:
        center.control_action_gate({}, "all")
        raise AssertionError("missing eligibility state did not fail closed")
    except center.DecisionError as exc:
        check(exc.code == "control_state_unknown" and exc.status == 503, "missing control state fails closed")
    with tempfile.TemporaryDirectory() as control_directory:
        control_root = Path(control_directory)
        maintenance_state = control_root / "maintenance-mode.json"
        maintenance_lock = control_root / "maintenance-mode.lock"
        with mock.patch.object(center, "MAINTENANCE_STATE", maintenance_state), mock.patch.object(
            center, "MAINTENANCE_LOCK", maintenance_lock
        ):
            maintenance_state.write_text('{"status":"active"}\n', encoding="utf-8")
            try:
                with center.maintenance_write_guard():
                    raise AssertionError("active maintenance allowed a UI write")
            except center.DecisionError as exc:
                check(exc.code == "maintenance_active", "active maintenance blocks every UI mutation")
            maintenance_state.write_text('{"status":"inactive"}\n', encoding="utf-8")
            allowed = False
            with center.maintenance_write_guard():
                allowed = True
            check(allowed, "inactive maintenance permits UI mutations")
    with mock.patch.object(
        center.email_sync, "confirm_read_safety",
        return_value={"cross_device_confirmed": True, "verdict": "pass"},
    ), mock.patch.object(center.syncstate, "update_source") as update_source:
        confirmed = center.confirm_email_read_safety({}, True)
    check(
        confirmed["status"] == "confirmed"
        and "新成功证据" in confirmed["next_action"]
        and update_source.call_args.kwargs["status"] == "pending",
        "explicit positive mail-safety confirmation schedules a retry without closing the incident",
    )
    with mock.patch.object(
        center.email_sync, "confirm_read_safety",
        return_value={"cross_device_confirmed": False, "verdict": "failed"},
    ), mock.patch.object(center.syncstate, "update_source") as update_source, mock.patch.object(
        center.syncstate, "open_alert"
    ) as open_alert, mock.patch.object(
        center.syncstate, "source", return_value={"attempt_count": 14}
    ):
        rejected = center.confirm_email_read_safety({}, False)
    check(
        rejected["status"] == "not_confirmed"
        and update_source.call_args.kwargs["status"] == "manual_required"
        and open_alert.call_args.kwargs["severity"] == "error",
        "a changed unread state keeps mail paused and the serious incident open",
    )
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        memory_root = root / "memory"
        for directory in ("01_关键业务认知", "02_内部业务资料", "03_外部沟通", "04_对话来源摘要", "90_待确认", "99_规则与模板"):
            (memory_root / directory).mkdir(parents=True, exist_ok=True)
        existing_path = memory_root / "01_关键业务认知" / "20260826-旧状态.md"
        candidate_path = memory_root / "90_待确认" / "20260826-新状态.md"
        existing_path.write_text(
            note(
                "BUSINESS-20260826-900",
                status="active",
                fact_status="confirmed",
                source_check="matched",
                source_ref='dingtalk-batch:8a21ae61-066d-546b-b54e-fa10a0cced7c',
                body="## 记忆内容\n\n旧状态。\n\n## 更新记录\n\n- 建立。\n",
            ),
            encoding="utf-8",
        )
        candidate_path.write_text(
            note(
                "BUSINESS-20260826-901",
                status="review_required",
                fact_status="conflicting",
                source_check="discrepant",
                source_ref='dingtalk-minutes-batch:e2b869dc-64d9-5c3e-87ab-f675ac45e9e2',
                body="## 记忆内容\n\n新状态。\n\n## 证据边界\n\n需要 Example Owner 确认。\n\n## 更新记录\n\n- 建立。\n",
            ),
            encoding="utf-8",
        )

        state_root = root / "state-root"
        syncv2.ROOT = state_root
        syncv2.STATE_DIR = state_root / "state"
        syncv2.RUNS_DIR = state_root / "runs"
        syncv2.ARCHIVE_DIR = state_root / "archive"
        syncv2.DATABASE = syncv2.STATE_DIR / "sync.sqlite"
        syncv2.AI_LOCK = syncv2.STATE_DIR / "ai.lock"
        syncv2.SOURCE_LOCK = syncv2.STATE_DIR / "source.lock"
        syncv2.SEMANTIC_LOCK = syncv2.STATE_DIR / "semantic.lock"
        syncv2.DECISION_LOCK = syncv2.STATE_DIR / "decision.lock"
        center.STATIC_ROOT = root / "static"
        center.CONTROL_LOCK = syncv2.STATE_DIR / "sync-center-control.lock"
        center.CONTROL_START_LOCK = syncv2.STATE_DIR / "sync-center-start.lock"
        memory.MEMORY_LOCK = root / "memory.lock"
        syncv2.initialize()
        with mock.patch.object(center, "maintenance_active", return_value=False), \
                mock.patch.object(center, "recover_stale_action_runs", return_value=0), \
                mock.patch.object(center.subprocess, "Popen") as popen:
            started = center.start_control_action(root / "config.json", "dingtalk_chat")
            duplicate = center.start_control_action(root / "config.json", "win11")
        check(
            started["status"] == "started"
            and started["target"] == "dingtalk_chat"
            and started["local_only"] is True,
            "a source action starts through the unified local-only controller",
        )
        check(
            duplicate["status"] == "already_running"
            and duplicate["run_id"] == started["run_id"]
            and popen.call_count == 1,
            "a second source action returns the active run instead of queueing a duplicate",
        )
        launched = popen.call_args.args[0]
        check(
            str(center.SYNC_CONTROLLER) in launched
            and launched[-1] == "dingtalk_chat"
            and launched[launched.index("--run-id") + 1] == started["run_id"],
            "the selected source and run identifier reach the controller unchanged",
        )
        syncv2.set_action_step(
            started["run_id"], "source", 1, "钉钉聊天", "success",
            current_count=1, total_count=1, item_label="不得进入顶层阶段字段的脱敏项目",
        )
        syncv2.finish_action_run(started["run_id"], "success", result={"local_only": True})
        completed_detail = center.action_detail(started["run_id"])
        check(
            completed_detail["target"] == "dingtalk_chat"
            and completed_detail["stage"] == {"key": "source", "label": "钉钉聊天", "status": "success"}
            and completed_detail["progress"] == {"current": 1, "total": 1, "unit": "items"}
            and completed_detail["error_code"] is None,
            "action detail exposes a redacted target, stage, progress, status, and error code",
        )
        failed_run = syncv2.start_action_run("all")
        syncv2.set_action_step(
            failed_run, "chat_reconcile", 1, "同步钉钉聊天", "failed",
            error_code="chat_reconcile_rollback_concurrent_change",
        )
        syncv2.set_action_step(
            failed_run, "dingtalk_validation", 2, "钉钉记忆校验", "failed",
            error_code="validation_failed",
        )
        syncv2.set_action_step(failed_run, "email", 3, "同步企业邮件", "skipped")
        syncv2.set_action_step(failed_run, "win11", 4, "同步 Win11 资料", "skipped")
        syncv2.finish_action_run(failed_run, "failed", error_code="validation_failed")
        failed_detail = center.action_detail(failed_run)
        check(
            failed_detail["status"] == "failed"
            and [step["status"] for step in failed_detail["steps"]] == ["failed", "failed", "skipped", "skipped"]
            and failed_detail["progress"] == {"current": 4, "total": 4, "unit": "steps"},
            "failed action detail preserves the chat failure, validation failure, and skipped downstream steps",
        )
        review_run = syncv2.start_action_run("locked_review")
        with mock.patch.object(center, "maintenance_active", return_value=False), \
                mock.patch.object(center, "recover_stale_action_runs", return_value=0), \
                mock.patch.object(center.subprocess, "Popen") as review_busy_popen:
            review_busy = center.start_control_action(root / "config.json", "dingtalk_minutes")
        check(
            review_busy["status"] == "already_running"
            and review_busy["run_id"] == review_run
            and review_busy_popen.call_count == 0,
            "an existing automatic review blocks manual sync instead of queueing behind its locks",
        )
        syncv2.finish_action_run(review_run, "success", result={"local_only": True})
        with mock.patch.object(center, "maintenance_active", return_value=False), \
                mock.patch.object(center, "recover_stale_action_runs", return_value=0), \
                mock.patch.object(center, "control_lock_available", return_value=False), \
                mock.patch.object(center.subprocess, "Popen") as blocked_popen:
            busy = center.start_control_action(root / "config.json", "win11")
        check(
            busy == {"status": "already_running", "reason": "control_lock_busy"}
            and blocked_popen.call_count == 0,
            "a busy control lock is nonblocking and never creates a queued process",
        )
        try:
            center.start_control_action(root / "config.json", "chatgpt")
            raise AssertionError("unsupported source was accepted")
        except center.DecisionError as exc:
            check(exc.code == "target_invalid", "the control API rejects sources outside the fixed five")
        center.update_preferences({"appearance": "dark"})
        check(center.preferences()["appearance"] == "dark", "appearance preference persists locally")
        syncv2.emit_outbox(
            "sync_recovery",
            "selftest-manual-sync-recovery",
            {
                "component": "manual_sync",
                "recovered_from_error_code": "chat_reconcile_memory_daily_summary_collision",
                "repair_result": "recovered",
            },
        )
        connection = syncv2.connect_readonly()
        try:
            recovery = center.latest_component_recovery(connection, "manual_sync")
        finally:
            connection.close()
        check(
            recovery is not None
            and recovery["error_code"] == "chat_reconcile_memory_daily_summary_collision",
            "latest manual-sync recovery is exposed without rewriting the failed action",
        )

        minutes_root = root / "minutes"
        center.MINUTES_ROOT = minutes_root
        center.MINUTES_NOTES_ROOT = minutes_root / "听记"
        center.MINUTES_RAW_ROOT = minutes_root / ".raw"
        center.MINUTES_SYNC_STATE = minutes_root / ".state" / "sync-state.json"
        center.MINUTES_REMOTE_INVENTORY = minutes_root / ".state" / "remote-inventory.json"
        center.REVIEW_LEDGER = minutes_root / "核对" / ".state" / "review-ledger.json"
        center.MINUTES_SYNC_STATE.parent.mkdir(parents=True)
        center.REVIEW_LEDGER.parent.mkdir(parents=True)
        center.MINUTES_SYNC_STATE.write_text(
            json.dumps(
                {
                    "last_sync_at": "2020-01-02T18:10:42+08:00",
                    "entries": {"recording": {"media_storage": "pssd"}},
                }
            ),
            encoding="utf-8",
        )
        center.REVIEW_LEDGER.write_text(json.dumps({"recording": {"status": "done"}}), encoding="utf-8")
        minutes_config = {"dingtalk_minutes_memory": {"cutover_at": "2020-01-02T18:00:00+08:00"}}
        with mock.patch.object(center.review_policy, "load_recording_rows", return_value={}):
            minutes_state = center.minutes_operational_state(minutes_config)
        check(
            minutes_state["last_sync_at"] == "2020-01-02T18:10:42+08:00"
            and minutes_state["reviewed"] == 1
            and minutes_state["storage"]["pssd"] == 1,
            "status page uses the real recording sync timestamp and governed storage state",
        )
        center.REVIEW_LEDGER.write_text(
            json.dumps({"recording": {"status": "review_done_reconcile_deferred"}}),
            encoding="utf-8",
        )
        with mock.patch.object(center.review_policy, "load_recording_rows", return_value={}):
            deferred_state = center.minutes_operational_state(minutes_config)
        check(
            deferred_state["pending_review"] == 1 and deferred_state["review_failed"] == 0,
            "deferred reconciliation is pending rather than a review failure",
        )

        candidate = {
            "action": "conflict",
            "dedupe_key": "status-change",
            "title": "新状态",
            "category": "business_cognition",
            "memory_kind": "status",
            "summary": "新状态。",
            "evidence_boundary": "需要 Example Owner 确认。",
            "related_memory_ids": ["BUSINESS-20260826-900"],
            "conflict_reason": "与旧状态不一致。",
        }
        candidate_json = syncv2.canonical_json(candidate)
        decision_id = str(uuid.uuid4())
        connection = syncv2.connect()
        try:
            connection.execute(
                """
                INSERT INTO decisions(
                  decision_id,recording_id,source_ref,candidate_json,candidate_hash,candidate_memory_id,
                  candidate_path,candidate_file_hash,related_memory_ids_json,related_hashes_json,status,created_at
                ) VALUES(?,NULL,?,?,?,?,?,?,?,?, 'pending',?)
                """,
                (
                    decision_id,
                    candidate["dedupe_key"],
                    candidate_json,
                    syncv2.sha256_text(candidate_json),
                    "BUSINESS-20260826-901",
                    str(candidate_path),
                    syncv2.sha256_file(candidate_path),
                    syncv2.canonical_json(candidate["related_memory_ids"]),
                    syncv2.canonical_json({"BUSINESS-20260826-900": syncv2.sha256_file(existing_path)}),
                    syncv2.now_iso(),
                ),
            )
            connection.commit()
        finally:
            connection.close()

        config = {
            "external_root": str(root / "external"),
            "memory_root": str(memory_root),
            "dingtalk_minutes_memory": {"cutover_at": "2020-01-02T18:00:00+08:00"},
        }
        old_task = "review-task"
        old_start = 1787800000000
        center.MINUTES_NOTES_ROOT.mkdir(parents=True)
        (center.MINUTES_NOTES_ROOT / "legacy.md").write_text("legacy\n", encoding="utf-8")
        (center.MINUTES_RAW_ROOT / old_task).mkdir(parents=True)
        (center.MINUTES_RAW_ROOT / old_task / "transcript.json").write_text(
            json.dumps({
                "complete": True,
                "taskUuid": old_task,
                "paragraphCount": 1,
                "paragraphList": [{"paragraph": "正文"}],
            }), encoding="utf-8"
        )
        archived_entry = {
            "title": "历史录音",
            "filename": "legacy.md",
            "start_time": old_start,
            "last_synced_at": "2020-01-02T10:00:00+08:00",
            "missing_artifacts": ["todos"],
            "media_storage": "pssd",
            "media_filename": "recording.mp3",
            "media_archive_relative_path": "Archive/recording.mp3",
            "media_archive_volume_uuid": "volume-uuid",
            "media_sha256": "a" * 64,
            "media_bytes": 10,
        }
        center.MINUTES_SYNC_STATE.write_text(
            json.dumps({"last_sync_at": "2020-01-02T10:00:00+08:00", "entries": {old_task: archived_entry}}),
            encoding="utf-8",
        )
        center.MINUTES_REMOTE_INVENTORY.write_text(
            json.dumps({
                "last_attempt_status": "complete",
                "last_attempt_at": "2020-01-02T10:00:00+08:00",
                "last_complete_at": "2020-01-02T10:00:00+08:00",
                "last_complete_count": 1,
                "items": {old_task: {"title": "历史录音", "start_time": old_start}},
            }),
            encoding="utf-8",
        )
        center.REVIEW_LEDGER.write_text(
            json.dumps({old_task: {"status": "review_done_reconcile_deferred"}}),
            encoding="utf-8",
        )
        deferred_items, _inventory = center.recording_attention(config, {"online": False})
        deferred_item = next(item for item in deferred_items if item.get("queue_state") == "reconcile_deferred")
        check(
            deferred_item["severity"] == "warning"
            and deferred_item["stage"] == "二查完成，记忆回写等待 AI 通路",
            "decision center shows deferred reconciliation as a recoverable warning",
        )
        check(
            next(stage for stage in deferred_item["stages"] if stage["label"] == "已二查")["status"] == "done",
            "deferred reconciliation keeps the completed transcript-review stage green",
        )
        center.REVIEW_LEDGER.write_text("{}", encoding="utf-8")
        recording_items, _inventory = center.recording_attention(config, {"online": False})
        check(
            any(item["category"] == "unsynced" and item["stage"] == "媒体在 T9，等待连接" for item in recording_items),
            "offline T9 media remains a neutral waiting-source item",
        )
        check(
            not any(item["category"] == "unreviewed" for item in recording_items),
            "waiting-source media is not counted again as unreviewed",
        )
        historical_start = 1787187600000
        archived_entry["start_time"] = historical_start
        center.MINUTES_SYNC_STATE.write_text(
            json.dumps({"last_sync_at": "2020-01-02T10:00:00+08:00", "entries": {old_task: archived_entry}}),
            encoding="utf-8",
        )
        center.MINUTES_REMOTE_INVENTORY.write_text(
            json.dumps({
                "last_attempt_status": "complete",
                "last_attempt_at": "2020-01-02T10:00:00+08:00",
                "last_complete_at": "2020-01-02T10:00:00+08:00",
                "last_complete_count": 1,
                "items": {old_task: {"title": "历史录音", "start_time": historical_start}},
            }),
            encoding="utf-8",
        )
        center.REVIEW_LEDGER.write_text(json.dumps({
            old_task: {
                "status": "pending",
                "policy_version": 2,
                "review_required": False,
                "requirement_reason": "historical_pre_cutover",
            }
        }), encoding="utf-8")
        historical_items, _inventory = center.recording_attention(config, {"online": False})
        check(
            not any(item["category"] in {"unreviewed", "unverified"} for item in historical_items),
            "historical review exemption does not reopen an old review or verification item",
        )

        chat_database = memory.chat_sync.connect_database(memory.runtime_paths(config)["database"])
        try:
            chat_database.execute(
                "INSERT INTO sync_runs(run_id,started_at,completed_at,status) VALUES('run','2020-01-02T18:00:00+08:00','2020-01-02T18:01:00+08:00','partial')"
            )
            chat_database.execute(
                """
                INSERT INTO conversations(
                  conversation_id,conversation_name,first_seen_at,last_seen_at,last_scan_at,
                  last_complete_scan_at,last_scan_status,visible_message_count
                ) VALUES('conversation','会话','2020-01-02T18:00:00+08:00','2020-01-02T18:00:00+08:00',
                  '2020-01-02T18:00:00+08:00','2020-01-02T23:45:00+08:00','incomplete',0)
                """
            )
            chat_database.execute(
                """
                INSERT INTO conversation_scans(
                  run_id,conversation_id,started_at,completed_at,status,returned_count,complete,
                  partial,has_more,pages_fetched,failed_count,truncated,normalization_failures
                ) VALUES('run','conversation','2020-01-02T18:00:00+08:00','2020-01-02T18:01:00+08:00',
                  'incomplete',0,0,1,1,1,0,0,0)
                """
            )
            chat_database.commit()
        finally:
            chat_database.close()
        uncovered = center.chat_attention(config)
        check(len(uncovered) == 1 and uncovered[0]["stage"] == "分页尚未完成", "hasMore chat scan is never shown as covered")
        original_verifier = memory.run_refresh_and_validate
        memory.run_refresh_and_validate = lambda _config, **_kwargs: {"refresh_exit": 0, "validator": {"verdict": "pass"}}
        try:
            draft = center.save_decision_draft(decision_id, {"kind": "note", "content": "待补充背景"})
            check(draft["content"] == "待补充背景", "decision draft is saved locally")
            preview = center.create_preview(config, center.decision_row(decision_id), "accept_new")
            check(preview["recoverable"] is True and preview.get("preview_token"), "preview is token-bound")
            tamper_rejected = False
            try:
                center.apply_decision(
                    config,
                    decision_id,
                    "reject",
                    preview_token=str(preview["preview_token"]),
                )
            except center.DecisionError as exc:
                tamper_rejected = exc.code == "preview_stale"
            check(tamper_rejected, "preview token rejects a changed action")
            note_message = center.append_decision_message(root / "config.json", decision_id, {"kind": "note", "content": "这是一条本机备注。"})
            check(note_message["kind"] == "note" and note_message["status"] == "completed", "note is saved without an AI action")
            connection = syncv2.connect_readonly()
            try:
                question_runs_before = int(connection.execute("SELECT COUNT(*) FROM action_runs WHERE action_type='decision_question'").fetchone()[0])
            finally:
                connection.close()
            check(question_runs_before == 0, "note never calls the model")
            try:
                center.apply_decision(
                    config,
                    decision_id,
                    "accept_new",
                    preview_token=str(preview["preview_token"]),
                )
                raise AssertionError("preview survived a new local note")
            except center.DecisionError as exc:
                check(exc.code in {"preview_invalid", "preview_stale"}, "new discussion invalidates the old preview")
            candidate_path.write_text(candidate_path.read_text(encoding="utf-8") + "\n<!-- revised candidate -->\n", encoding="utf-8")
            refreshed = syncv2.register_decision(
                recording_id=None,
                source_ref=candidate["dedupe_key"],
                candidate=candidate,
                candidate_memory_id="BUSINESS-20260826-901",
                candidate_path=candidate_path,
                related_memory_ids=candidate["related_memory_ids"],
                related_hashes={"BUSINESS-20260826-900": syncv2.sha256_file(existing_path)},
            )
            check(refreshed["updated"] and refreshed["revision"] == 2, "candidate file change increments its decision revision")
            try:
                center.apply_decision(
                    config,
                    decision_id,
                    "accept_new",
                    preview_token=str(preview["preview_token"]),
                )
                raise AssertionError("stale preview survived candidate update")
            except center.DecisionError as exc:
                check(exc.code in {"preview_invalid", "preview_stale"}, "candidate update invalidates its old preview")
            preview = center.create_preview(config, center.decision_row(decision_id), "accept_new")
            before_question_files = (candidate_path.read_bytes(), existing_path.read_bytes())
            with mock.patch.object(center.subprocess, "Popen"):
                question = center.append_decision_message(
                    root / "config.json",
                    decision_id,
                    {"kind": "question", "content": "新旧状态的证据差别是什么？"},
                )
            with mock.patch.object(center, "invoke_decision_question", return_value={"answer": "新内容仍需要责任人确认。"}):
                answered = center.answer_decision_question(config, question["message_id"])
            check(answered["status"] == "success", "AI question produces a governed local answer")
            check((candidate_path.read_bytes(), existing_path.read_bytes()) == before_question_files, "AI question never modifies memory files")
            try:
                center.apply_decision(
                    config,
                    decision_id,
                    "accept_new",
                    preview_token=str(preview["preview_token"]),
                )
                raise AssertionError("preview survived an AI answer")
            except center.DecisionError as exc:
                check(exc.code in {"preview_invalid", "preview_stale"}, "AI answer invalidates the old preview")
            preview = center.create_preview(config, center.decision_row(decision_id), "accept_new")
            result = center.apply_decision(
                config,
                decision_id,
                "accept_new",
                preview_token=str(preview["preview_token"]),
            )
        finally:
            memory.run_refresh_and_validate = original_verifier

        target = memory_root / "01_关键业务认知" / candidate_path.name
        new_record = memory.parse_memory_note(target, strict=True)
        old_record = memory.parse_memory_note(existing_path, strict=True)
        check(result["status"] == "accepted" and result["validated"], "accepted decision reports validation")
        check(new_record and new_record["status"] == "active" and new_record["fact_status"] == "confirmed", "new candidate becomes confirmed active")
        check(old_record and old_record["status"] == "superseded", "related conflicting memory is superseded")
        check(not candidate_path.exists(), "pending candidate leaves the decision directory")
        journals = list((syncv2.RUNS_DIR / "decisions" / decision_id).glob("*/journal.json"))
        check(len(journals) == 1, "decision has one transaction journal")
        journal = journals[0]
        check(json.loads(journal.read_text(encoding="utf-8"))["state"] == "committed", "decision has a committed recovery journal")
        connection = syncv2.connect()
        try:
            status = connection.execute("SELECT status FROM decisions WHERE decision_id=?", (decision_id,)).fetchone()[0]
            transaction_state = connection.execute("SELECT state FROM decision_transactions").fetchone()[0]
        finally:
            connection.close()
        check(status == "accepted", "decision state is committed after files")
        check(transaction_state == "committed", "transaction records file, database and notification completion")

    print(json.dumps({"verdict": "pass", "checks": CHECKS}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
