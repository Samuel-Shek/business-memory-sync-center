#!/usr/bin/env python3
"""User-triggered DingTalk incremental sync and memory completion pipeline."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import argparse
import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

import dingtalk_memory_automation as chat_memory
import dingtalk_sync_v2 as syncv2


os.umask(0o077)

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.json"
MAINTENANCE_STATE = ROOT / "state" / "maintenance-mode.json"
MINUTES_ROOT = Path(_archive_path('/Users/example/AI/LocalData/DingTalk-AI-Minutes.nosync'))
MINUTES_SYNC = MINUTES_ROOT / "sync_minutes.py"
MINUTES_MEMORY = ROOT / "bin" / "dingtalk_minutes_memory.py"
CHAT_MEMORY = ROOT / "bin" / "dingtalk_memory_automation.py"
DECISION_CENTER = ROOT / "bin" / "dingtalk_decision_center.py"


class OrchestratorError(RuntimeError):
    def __init__(self, code: str, *, receipt: dict[str, Any] | None = None):
        super().__init__(code)
        self.code = code
        self.receipt = receipt or {}


def maintenance_active() -> bool:
    try:
        value = json.loads(MAINTENANCE_STATE.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return False
    return isinstance(value, dict) and value.get("status") == "active"


def decode_component_receipt(stdout: str) -> dict[str, Any]:
    """Accept a final JSON receipt after human-readable progress lines."""
    positions = [0]
    offset = 0
    for line in stdout.splitlines(keepends=True):
        if line.startswith("{"):
            positions.append(offset)
        offset += len(line)
    for position in reversed(list(dict.fromkeys(positions))):
        try:
            payload = json.loads(stdout[position:].strip())
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise OrchestratorError("component_invalid_receipt")


def run_json(
    command: list[str],
    *,
    timeout: int,
    environment: dict[str, str],
    component: str,
    allow_partial: bool = False,
) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise OrchestratorError(f"{component}_timeout") from exc
    except OSError as exc:
        raise OrchestratorError(f"{component}_unavailable") from exc
    try:
        payload = decode_component_receipt(result.stdout)
    except OrchestratorError as exc:
        code = f"{component}_exit_{result.returncode}" if result.returncode else f"{component}_invalid_receipt"
        raise OrchestratorError(code) from exc
    payload_status = str(payload.get("status") or "")
    if result.returncode != 0 or payload_status == "failed" or (payload_status == "partial" and not allow_partial):
        failures = payload.get("failures") or []
        detail = str(payload.get("error_code") or (failures[0] if failures else None) or f"exit_{result.returncode}")
        raise OrchestratorError(f"{component}_{detail}", receipt=payload)
    return payload


def recording_media_partial(receipt: dict[str, Any]) -> bool:
    return (
        str(receipt.get("status") or "") == "partial"
        and int(receipt.get("failed") or 0) == 0
        and int(receipt.get("media_missing") or 0) > 0
    )


def recording_media_detail(receipt: dict[str, Any]) -> dict[str, Any]:
    return syncv2.safe_health_detail(
        {
            "phase": receipt.get("phase") or "media_download",
            "reason": receipt.get("reason") or "钉钉原始录音暂未就绪，文字内容已保留并继续后续处理。",
            "auto_repair": receipt.get("auto_repair") or "scheduled_retry",
            "attempts": int(receipt.get("attempts") or 1),
            "next_retry_at": receipt.get("next_retry_at"),
            "original_error_code": receipt.get("original_error_code") or receipt.get("error_code") or "media_pending",
        }
    )


def recording_source_media_partial(receipt: dict[str, Any]) -> bool:
    failures = {
        str(code)
        for code in (receipt.get("failures") or [])
        if str(code)
    }
    return str(receipt.get("status") or "") == "partial" and bool(failures) and failures <= {"media_incomplete"}


def chat_reconcile_complete(receipt: dict[str, Any]) -> bool:
    return (
        receipt.get("status") in {"success", "noop", "deferred"}
        and chat_memory.incremental_reconcile_complete(receipt.get("incremental"))
    )


def chat_reconcile_once(config_path: Path, environment: dict[str, str]) -> dict[str, Any]:
    """Read once; only source failures may continue through the governed queue drain."""
    try:
        return run_json(
            ["/usr/bin/python3", str(CHAT_MEMORY), "--config", str(config_path), "hourly", "--manual"],
            timeout=7200, environment=environment, component="chat_reconcile", allow_partial=True,
        )
    except OrchestratorError as exc:
        receipt = exc.receipt
        incremental = receipt.get("incremental")
        if not isinstance(incremental, dict) or incremental.get("status") not in {"partial", "failed"}:
            raise
        source_code = chat_reconcile_error(receipt)
        if not (chat_memory.chat_read_recovery.scheduled_read_recovery(source_code)
                or source_code in {"chat_read_auth_required", "chat_read_blocked", "chat_checkpoint_invalid", "chat_checkpoint_busy"}):
            raise
        # A validation/index/conflict failure must never be relabelled as a read failure.
        if (receipt.get("status") == "failed" and not receipt.get("error_code")) or receipt.get("error_code") not in {None, "", source_code, incremental.get("error_code")}:
            raise
        return receipt


def chat_reconcile_context(receipt: dict[str, Any]) -> dict[str, Any]:
    """Carry aggregate collector evidence across CLI/action boundaries."""
    if not isinstance(receipt.get("incremental"), dict):
        return {}
    incremental = chat_memory.safe_hourly_receipt(receipt)["incremental"]
    return {"incremental": incremental, "reconciliation": incremental.get("reconciliation") or {},
            "read_attempted": incremental.get("read_attempted"),
            "read_recovery": incremental.get("read_recovery") or {}}


def chat_reconcile_error(receipt: dict[str, Any]) -> str:
    incremental = receipt.get("incremental") or {}
    return str((incremental.get("read_recovery") or {}).get("error_code")
               or incremental.get("error_code") or "chat_reconcile_incomplete")


PROCESSABLE_RECORDING_STATES = frozenset({
    "discovered",
    "source_written",
    "chunking",
    "ready_finalize",
    "independent_ready",
    "comparison_refresh_pending",
})


def recover_abandoned_manual_runs(exclude_run_id: str | None = None) -> int:
    try:
        with syncv2.file_lock(syncv2.MANUAL_SYNC_LOCK):
            if syncv2.MANUAL_SYNC_FLAG.is_symlink():
                raise OrchestratorError("manual_sync_flag_unsafe")
            connection = syncv2.connect()
            try:
                abandoned = connection.execute(
                    """
                    SELECT run_id FROM action_runs
                    WHERE action_type='sync_now' AND status='running'
                      AND (? IS NULL OR run_id<>?)
                    """,
                    (exclude_run_id, exclude_run_id),
                ).fetchall()
            finally:
                connection.close()
            for row in abandoned:
                syncv2.finish_action_run(
                    str(row["run_id"]),
                    "interrupted",
                    result={"reason": "process_exit"},
                    error_code="interrupted_after_process_exit",
                )
            recovered = len(abandoned)
            if syncv2.MANUAL_SYNC_FLAG.is_file():
                syncv2.MANUAL_SYNC_FLAG.unlink(missing_ok=True)
            return recovered
    except syncv2.SyncV2Error as exc:
        if exc.code == "already_running":
            return 0
        raise


def recording_queue() -> list[str]:
    connection = syncv2.connect_readonly()
    try:
        return [
            str(row["recording_id"])
            for row in connection.execute(
                """
                SELECT recording_id FROM recordings
                WHERE status IN (
                  'discovered','source_written','chunking','ready_finalize',
                  'independent_ready','comparison_refresh_pending'
                )
                ORDER BY start_time,created_at
                """
            )
        ]
    finally:
        connection.close()


def recording_status(recording_id: str) -> str:
    connection = syncv2.connect_readonly()
    try:
        row = connection.execute(
            "SELECT status FROM recordings WHERE recording_id=?",
            (recording_id,),
        ).fetchone()
        return str(row["status"] if row else "missing")
    finally:
        connection.close()


SYNC_STEPS = (
    ("prepare", 10, "准备同步"),
    ("wait_review", 20, "等待当前二查结束"),
    ("recording_scan", 30, "扫描钉钉录音"),
    ("recording_fetch", 40, "同步录音内容"),
    ("recording_source", 50, "写入录音来源"),
    ("chat_reconcile", 60, "核对聊天覆盖"),
    ("chat_memory", 70, "更新聊天长期记忆"),
    ("recording_memory", 80, "更新录音业务记忆"),
    ("decision_registration", 90, "登记待决策内容"),
    ("memory_validation", 100, "校验业务记忆"),
    ("notification", 110, "确认飞书回执"),
)


def initialize_sync_steps(run_id: str) -> None:
    for key, order, label in SYNC_STEPS:
        syncv2.set_action_step(run_id, key, order, label, "queued")


def progress(
    run_id: str,
    key: str,
    status: str,
    *,
    current: int | None = None,
    total: int | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    order, label = next((order, label) for step, order, label in SYNC_STEPS if step == key)
    syncv2.set_action_step(
        run_id,
        key,
        order,
        label,
        status,
        current_count=current,
        total_count=total,
        detail=detail,
    )


def run_sync_now(config_path: Path, run_id: str) -> dict[str, Any]:
    os.environ["DINGTALK_LOCAL_ONLY"] = "1"
    syncv2.initialize()
    initialize_sync_steps(run_id)
    progress(run_id, "prepare", "running")
    environment = {
        **os.environ,
        "LC_ALL": "en_US.UTF-8",
        "DINGTALK_MANUAL_SYNC": "1",
        "DINGTALK_SUPPRESS_COMPLETION_OUTBOX": "1",
        "DINGTALK_LOCAL_ONLY": "1",
        "DINGTALK_ACTION_RUN_ID": run_id,
    }
    config = chat_memory.load_config(config_path)
    result: dict[str, Any] = {
        "recordings_new": 0,
        "recordings_updated": 0,
        "recording_media_pending": 0,
        "chat_messages_changed": 0,
        "memory_writes": 0,
        "model_calls": 0,
        "decision_created": 0,
        "decision_updated": 0,
    }
    progress(run_id, "prepare", "success")
    with syncv2.file_lock(syncv2.MANUAL_SYNC_LOCK):
        chat_memory.atomic_write(
            syncv2.MANUAL_SYNC_FLAG,
            json.dumps({"run_id": run_id, "started_at": syncv2.now_iso()}, ensure_ascii=False) + "\n",
            mode=0o600,
        )
        try:
            # 已经付费提交的单条二查允许结束；manual flag 会阻止新的二查阶段进入。
            progress(run_id, "wait_review", "running")
            with syncv2.file_lock(syncv2.REVIEW_LOCK, blocking=True):
                pass
            progress(run_id, "wait_review", "success")
            try:
                minutes = run_json(
                    ["/usr/bin/python3", str(MINUTES_SYNC), "--manual"],
                    timeout=3600,
                    environment=environment,
                    component="recording_sync",
                    allow_partial=True,
                )
            except OrchestratorError as exc:
                if not recording_media_partial(exc.receipt):
                    raise
                minutes = exc.receipt
            media_partial_detail: dict[str, Any] = {}
            if recording_media_partial(minutes):
                media_partial_detail = recording_media_detail(minutes)
                result["recording_media_pending"] = int(minutes.get("media_missing") or 0)
                progress(
                    run_id,
                    "recording_fetch",
                    "partial",
                    current=int(minutes.get("selected") or 0),
                    total=int(minutes.get("selected") or 0),
                    detail=media_partial_detail,
                )
                syncv2.set_component_health(
                    "recording_media",
                    False,
                    "recording_media_pending",
                    safe_detail=media_partial_detail,
                )
            else:
                syncv2.set_component_health("recording_media", True)
            result["recordings_new"] = int(minutes.get("created") or 0)
            result["recordings_updated"] = int(minutes.get("updated") or 0)

            progress(run_id, "recording_source", "running")
            try:
                source = run_json(
                    ["/usr/bin/python3", str(MINUTES_MEMORY), "--config", str(config_path), "source-sync"],
                    timeout=1800,
                    environment=environment,
                    component="recording_source",
                    allow_partial=True,
                )
            except OrchestratorError as exc:
                if not media_partial_detail or not recording_source_media_partial(exc.receipt):
                    raise
                source = exc.receipt
            result["memory_writes"] += int(source.get("source_notes_written") or 0)
            source_writes = int(source.get("source_notes_written") or 0)
            source_media_partial = recording_source_media_partial(source)
            progress(
                run_id,
                "recording_source",
                "partial" if source_media_partial else ("success" if source_writes else "noop"),
                current=source_writes,
                total=source_writes,
                detail=media_partial_detail if source_media_partial else None,
            )

            progress(run_id, "chat_reconcile", "running", current=0, total=1)
            chat_first = chat_reconcile_once(config_path, environment)
            incremental = chat_first.get("incremental") if isinstance(chat_first.get("incremental"), dict) else {}
            result.update(chat_reconcile_context(chat_first))
            result["chat_messages_changed"] += int(incremental.get("messages_inserted") or 0) + int(incremental.get("messages_updated") or 0)
            result["memory_writes"] += int(chat_first.get("writes") or 0)
            result["model_calls"] += int(bool(chat_first.get("model_called")))
            chat_complete = chat_reconcile_complete(chat_first)
            progress(run_id, "chat_reconcile", "success" if chat_complete else "partial", current=1, total=1)

            chat_queued = chat_memory.eligible_queue_count(config)
            progress(run_id, "chat_memory", "running" if chat_queued else "noop", current=0, total=chat_queued)
            chat_processed = 0
            for _ in range(50):
                remaining = chat_memory.eligible_queue_count(config)
                if remaining == 0:
                    break
                drained = run_json(
                    [
                        "/usr/bin/python3",
                        str(CHAT_MEMORY),
                        "--config",
                        str(config_path),
                        "hourly",
                        "--manual",
                        "--skip-reconcile",
                    ],
                    timeout=7200,
                    environment=environment,
                    component="chat_memory",
                )
                result["memory_writes"] += int(drained.get("writes") or 0)
                result["model_calls"] += int(bool(drained.get("model_called")))
                now_remaining = chat_memory.eligible_queue_count(config)
                chat_processed = max(chat_processed, chat_queued - now_remaining)
                progress(run_id, "chat_memory", "running", current=chat_processed, total=max(chat_queued, chat_processed + now_remaining))
            if chat_memory.eligible_queue_count(config) != 0:
                raise OrchestratorError("chat_queue_not_drained")
            if chat_queued:
                progress(run_id, "chat_memory", "success", current=chat_queued, total=chat_queued)

            if not chat_complete:
                raise OrchestratorError(chat_reconcile_error(chat_first), receipt=chat_first)

            initial_recordings = recording_queue()
            progress(
                run_id,
                "recording_memory",
                "running" if initial_recordings else "noop",
                current=0,
                total=len(initial_recordings),
            )
            for recording_position, recording_id in enumerate(initial_recordings, start=1):
                for _ in range(100):
                    if recording_status(recording_id) not in PROCESSABLE_RECORDING_STATES:
                        break
                    semantic = run_json(
                        ["/usr/bin/python3", str(MINUTES_MEMORY), "--config", str(config_path), "semantic"],
                        timeout=3600,
                        environment=environment,
                        component="recording_semantic",
                    )
                    if semantic.get("status") == "deferred":
                        code = semantic_deferred_code(semantic)
                        progress(run_id, "recording_memory", "deferred",
                                 current=recording_position - 1, total=len(initial_recordings),
                                 detail={"reason": code})
                        raise OrchestratorError(code, receipt=semantic)
                    result["model_calls"] += int(semantic.get("model_calls") or 0)
                    for item in semantic.get("results") or []:
                        if isinstance(item, dict):
                            result["memory_writes"] += int(item.get("writes") or 0)
                if recording_status(recording_id) in PROCESSABLE_RECORDING_STATES:
                    raise OrchestratorError("recording_queue_not_drained")
                progress(run_id, "recording_memory", "running", current=recording_position, total=len(initial_recordings))
            if initial_recordings:
                progress(run_id, "recording_memory", "success", current=len(initial_recordings), total=len(initial_recordings))

            progress(run_id, "decision_registration", "running")
            registration = run_json(
                ["/usr/bin/python3", str(DECISION_CENTER), "--config", str(config_path), "--sync-only"],
                timeout=300,
                environment=environment,
                component="decision_registration",
            )
            registered = registration.get("registered") if isinstance(registration.get("registered"), dict) else {}
            result["decision_created"] = int(registered.get("created") or 0)
            result["decision_updated"] = int(registered.get("updated") or 0)
            decision_changes = result["decision_created"] + result["decision_updated"]
            progress(
                run_id,
                "decision_registration",
                "success" if decision_changes else "noop",
                current=decision_changes,
                total=decision_changes,
            )

            changed = any(
                int(result[key]) > 0
                for key in ("recordings_new", "recordings_updated", "chat_messages_changed", "memory_writes")
            )
            progress(run_id, "memory_validation", "running")
            verification = chat_memory.run_refresh_and_validate(config, refresh_indexes=changed)
            if (verification.get("validator") or {}).get("verdict") != "pass":
                raise OrchestratorError("memory_validation_failed")
            progress(run_id, "memory_validation", "success")
            recording_partial = str(minutes.get("status") or "") == "partial"
            final_status = "partial" if recording_partial else ("success" if changed else "noop")
            progress(
                run_id,
                "notification",
                "skipped",
                detail={
                    "reason": "recording_media_retry_queued" if media_partial_detail else "local_only"
                },
            )
            syncv2.set_component_health("manual_sync", True)
            syncv2.finish_action_run(
                run_id,
                final_status,
                result=result,
                error_code="recording_media_pending" if media_partial_detail else None,
            )
            return {"status": final_status, "run_id": run_id, **result, "memory_validation": "pass"}
        except Exception as exc:
            exc.receipt = {**getattr(exc, "receipt", {}), **chat_reconcile_context(result)}
            code = str(getattr(exc, "code", type(exc).__name__))
            safe_detail = syncv2.safe_health_detail(getattr(exc, "receipt", {}))
            deferred = code in {"chat_ai_priority", "recording_semantic_deferred"}
            if not deferred:
                syncv2.set_component_health("manual_sync", False, code, safe_detail=safe_detail)
            syncv2.finish_action_run(run_id, "deferred" if deferred else "failed", result=result, error_code=code)
            raise
        finally:
            if syncv2.MANUAL_SYNC_FLAG.is_file() and not syncv2.MANUAL_SYNC_FLAG.is_symlink():
                syncv2.MANUAL_SYNC_FLAG.unlink(missing_ok=True)



def semantic_deferred_code(receipt: dict[str, Any]) -> str:
    # Explicit wait receipts are not completed work and must not be spun on.
    return "chat_ai_priority" if receipt.get("reason") == "chat_ai_priority" else "recording_semantic_deferred"

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a complete DingTalk incremental sync cycle")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--run-id")
    parser.add_argument("command", choices=("sync-now",))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    requested_run_id = str(args.run_id) if args.run_id else None
    if maintenance_active():
        if requested_run_id:
            syncv2.initialize()
            initialize_sync_steps(requested_run_id)
            progress(
                requested_run_id,
                "prepare",
                "deferred",
                detail={"reason": "maintenance_active"},
            )
            syncv2.finish_action_run(
                requested_run_id,
                "deferred",
                result={"reason": "maintenance_active"},
                error_code="maintenance_active",
            )
        print(json.dumps({"status": "deferred", "run_id": requested_run_id, "reason": "maintenance_active"}, ensure_ascii=False))
        return 0
    syncv2.initialize()
    recover_abandoned_manual_runs(exclude_run_id=requested_run_id)
    run_id = str(requested_run_id or syncv2.start_action_run("sync_now"))
    try:
        result = run_sync_now(args.config, run_id)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OrchestratorError, syncv2.SyncV2Error, chat_memory.AutomationError) as exc:
        code = getattr(exc, "code", "failed")
        deferred = code in {"chat_ai_priority", "recording_semantic_deferred"}
        print(json.dumps({**chat_reconcile_context(getattr(exc, "receipt", {})),
                          "status": "deferred" if deferred else "failed", "run_id": run_id, "error_code": code}, ensure_ascii=False))
        return 0 if deferred else 2
    except Exception as exc:
        print(json.dumps({**chat_reconcile_context(getattr(exc, "receipt", {})),
                          "status": "failed", "run_id": run_id, "error_code": type(exc).__name__}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    import owner_retirement
    owner_retirement.require_running()
    raise SystemExit(main())
