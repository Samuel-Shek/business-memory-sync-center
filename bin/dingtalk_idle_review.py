#!/usr/bin/env python3
"""Drain ready DingTalk reviews while the Mac is locked and on AC power."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import argparse
import fcntl
import json
import os
import re
import subprocess
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

import dingtalk_memory_automation as chat_memory
import dingtalk_sync_v2 as syncv2


os.umask(0o077)

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.json"
DISPATCHER = Path(_archive_path('/Users/example/AI/LocalData/DingTalk-AI-Minutes.nosync/核对/.pipeline/review_dispatch.py'))
DISPATCH_STATUS = Path(_archive_path('/Users/example/AI/LocalData/DingTalk-AI-Minutes.nosync/核对/.state/dispatch-status.json'))
DISPATCH_LOCK = Path(_archive_path('/Users/example/AI/LocalData/DingTalk-AI-Minutes.nosync/核对/.state/dispatch.lock'))
STALE_RECOVERY = ROOT / "bin" / "dingtalk_decision_center.py"
MAINTENANCE_STATE = ROOT / "state" / "maintenance-mode.json"
MAX_REVIEW_ITEM_SECONDS = 4 * 3600
DISPATCH_TIMEOUT_GRACE_SECONDS = 10 * 60


class IdleReviewError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def screen_is_locked() -> bool:
    try:
        result = subprocess.run(
            ["/usr/sbin/ioreg", "-n", "Root", "-d1"],
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IdleReviewError("screen_lock_state_unavailable") from exc
    match = re.search(r'"IOConsoleLocked"\s*=\s*(Yes|No)', result.stdout)
    if result.returncode != 0 or not match:
        raise IdleReviewError("screen_lock_state_unavailable")
    return match.group(1) == "Yes"


def power_allows_review() -> bool:
    try:
        result = subprocess.run(
            ["/usr/bin/pmset", "-g", "batt"],
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IdleReviewError("power_state_unavailable") from exc
    text = result.stdout
    if result.returncode != 0:
        raise IdleReviewError("power_state_unavailable")
    return "AC Power" in text or "No batteries" in text


def automatic_gate_reason(review_config: dict[str, Any]) -> str:
    if maintenance_active():
        return "maintenance_active"
    if syncv2.MANUAL_SYNC_FLAG.is_file():
        return "manual_sync_priority"
    if not screen_is_locked():
        return "screen_unlocked"
    if bool(review_config.get("require_ac_power", True)) and not power_allows_review():
        return "not_on_ac_power"
    return ""


def read_dispatch_status() -> dict[str, Any]:
    try:
        payload = json.loads(DISPATCH_STATUS.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def maintenance_active() -> bool:
    if not MAINTENANCE_STATE.exists():
        return False
    try:
        payload = json.loads(MAINTENANCE_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IdleReviewError("maintenance_state_unavailable") from exc
    if not isinstance(payload, dict) or payload.get("status") not in {
        "inactive", "entering", "active", "blocked", "exiting",
    }:
        raise IdleReviewError("maintenance_state_unavailable")
    return payload.get("status") != "inactive"


def decode_json_receipt(output: str) -> dict[str, Any]:
    for line in reversed((output or "").splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise IdleReviewError("review_dispatch_invalid_receipt")


def policy_snapshot() -> dict[str, Any]:
    process = subprocess.run(
        ['/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12', str(DISPATCHER), "--policy-only"],
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
        env={**os.environ, "LC_ALL": "en_US.UTF-8"},
    )
    if process.returncode != 0:
        raise IdleReviewError("review_policy_refresh_failed")
    return decode_json_receipt(process.stdout)


def recover_stale_runtime() -> dict[str, Any]:
    process = subprocess.run(
        [
            '/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12',
            str(STALE_RECOVERY),
            "--recover-stale-only",
            "--ignore-review-pid",
            str(os.getpid()),
        ],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
        env={**os.environ, "LC_ALL": "en_US.UTF-8"},
    )
    if process.returncode != 0:
        raise IdleReviewError("review_recovery_failed")
    receipt = decode_json_receipt(process.stdout)
    if receipt.get("status") != "success":
        raise IdleReviewError("review_recovery_failed")
    return receipt


def automatic_slots_available() -> bool:
    """Preflight all shared lanes before creating an automatic activity row."""
    lock_paths = (
        syncv2.REVIEW_LOCK,
        syncv2.SOURCE_LOCK,
        syncv2.SEMANTIC_LOCK,
        syncv2.AI_LOCK,
        chat_memory.HOURLY_LOCK,
        DISPATCH_LOCK,
    )
    try:
        with ExitStack() as stack:
            for path in lock_paths:
                stack.enter_context(syncv2.file_lock(path, blocking=False))
    except syncv2.SyncV2Error as exc:
        if exc.code == "already_running":
            return False
        raise
    return True


@contextmanager
def keep_awake(enabled: bool):
    """Prevent ordinary idle sleep only while an automatic review session runs."""
    process = None
    if enabled:
        try:
            process = subprocess.Popen(
                ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise IdleReviewError("caffeinate_unavailable") from exc
    try:
        yield
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


REVIEW_STEPS = (
    ("review_check", 10, "检查二查条件"),
    ("review_wait_lock", 15, "等待同步或记忆任务结束"),
    ("review_queue", 20, "读取二查队列"),
    ("review_media", 30, "读取录音媒体"),
    ("review_asr", 40, "Qwen ASR 转写"),
    ("review_compare", 50, "Codex 双稿核对"),
    ("review_memory", 60, "回写核对结果"),
    ("review_complete", 70, "完成本轮二查"),
)


def initialize_review_steps(run_id: str) -> None:
    for key, order, label in REVIEW_STEPS:
        syncv2.set_action_step(run_id, key, order, label, "queued")


def review_progress(run_id: str, key: str, status: str, *, detail: dict[str, Any] | None = None) -> None:
    order, label = next((order, label) for step, order, label in REVIEW_STEPS if step == key)
    syncv2.set_action_step(run_id, key, order, label, status, detail=detail)


def run_review(
    config_path: Path,
    run_id: str,
    *,
    manual: bool,
    handoff: bool = False,
) -> dict[str, Any]:
    config = chat_memory.load_config(config_path)
    review_config = config.get("dingtalk_review") if isinstance(config.get("dingtalk_review"), dict) else {}
    initialize_review_steps(run_id)
    review_progress(run_id, "review_check", "running")
    if syncv2.MANUAL_SYNC_FLAG.is_file():
        review_progress(run_id, "review_check", "deferred", detail={"reason": "manual_sync_priority"})
        result = {"status": "deferred", "reason": "manual_sync_priority", "processed": 0}
        syncv2.finish_action_run(run_id, "deferred", result=result)
        return result
    handoff = bool(handoff or (not manual and syncv2.minutes_review_handoff_pending()))
    if handoff:
        for step in ("review_media", "review_asr", "review_compare"):
            review_progress(run_id, step, "skipped")
    if not manual and not handoff:
        reason = automatic_gate_reason(review_config)
        if reason:
            review_progress(run_id, "review_check", "deferred", detail={"reason": reason})
            result = {"status": "deferred", "reason": reason, "processed": 0}
            syncv2.finish_action_run(run_id, "deferred", result=result)
            return result
    limit = 1 if handoff else int(
        review_config.get("manual_limit" if manual else "batch_limit") or 2
    )
    limit = 1 if handoff else min(2, max(1, limit))
    drain = bool(review_config.get("drain_while_locked", True)) and not manual and not handoff
    lock_paths = [
        syncv2.REVIEW_LOCK,
        syncv2.SOURCE_LOCK,
        syncv2.SEMANTIC_LOCK,
        syncv2.AI_LOCK,
        chat_memory.HOURLY_LOCK,
    ]
    review_progress(run_id, "review_check", "success")
    totals = {"done": 0, "failed": 0, "deferred": 0, "waiting_audio": 0}
    last = {}
    paused_reason = ""
    with keep_awake(not manual and not handoff):
        while True:
            if not manual and not handoff:
                paused_reason = automatic_gate_reason(review_config)
                if paused_reason:
                    break
            review_progress(run_id, "review_wait_lock", "running")
            try:
                with ExitStack() as stack:
                    lock_handles = {}
                    for path in lock_paths:
                        lock_handles[path] = stack.enter_context(
                            syncv2.file_lock(path, blocking=manual)
                        )
                    ai_lock_handle = lock_handles.get(syncv2.AI_LOCK)
                    if ai_lock_handle is None:
                        raise IdleReviewError("shared_ai_lock_handle_missing")
                    ai_lock_fd = int(ai_lock_handle.fileno())
                    review_progress(run_id, "review_wait_lock", "success")
                    command = [
                        '/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12',
                        str(DISPATCHER),
                        "--limit",
                        str(limit),
                        "--shared-ai-lock-fd",
                        str(ai_lock_fd),
                    ]
                    if handoff:
                        command.append("--reconcile-deferred-only")
                    elif not manual:
                        command.append("--automatic")
                    process = subprocess.run(
                        command,
                        text=True,
                        capture_output=True,
                        timeout=limit * MAX_REVIEW_ITEM_SECONDS + DISPATCH_TIMEOUT_GRACE_SECONDS,
                        check=False,
                        env={**os.environ, "LC_ALL": "en_US.UTF-8", "DINGTALK_ACTION_RUN_ID": run_id},
                        pass_fds=(ai_lock_fd,),
                    )
                    if process.returncode != 0:
                        raise IdleReviewError("review_dispatch_failed")
                    last = read_dispatch_status()
            except syncv2.SyncV2Error as exc:
                if exc.code == "already_running":
                    review_progress(run_id, "review_wait_lock", "deferred", detail={"reason": "sync_or_review_busy"})
                    if sum(totals.values()) == 0:
                        result = {"status": "deferred", "reason": "sync_or_review_busy", "processed": 0}
                        syncv2.finish_action_run(run_id, "deferred", result=result)
                        return result
                    paused_reason = "sync_or_review_busy"
                    break
                raise
            batch_processed = 0
            for key in totals:
                value = int(last.get(key) or 0)
                totals[key] += value
                if key != "waiting_audio":
                    batch_processed += value
            if not drain or batch_processed == 0 or bool(last.get("gate_paused")):
                break
            # Leaving the ExitStack above releases the review, source, semantic,
            # shared-AI and memory locks before the next batch is considered.

    done = totals["done"]
    failed = totals["failed"]
    deferred = totals["deferred"]
    processed = done + failed + deferred
    status = "partial" if failed else ("success" if done else ("deferred" if deferred else "noop"))
    result = {
        "status": status,
        "mode": "handoff" if handoff else ("manual" if manual else "locked"),
        "processed": processed,
        "done": done,
        "deferred": deferred,
        "failed": failed,
        "waiting_audio": totals["waiting_audio"],
        "candidates": int(last.get("candidates") or 0),
        "paused_reason": paused_reason,
    }
    review_progress(run_id, "review_complete", status)
    syncv2.finish_action_run(run_id, status, result=result)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lock-screen-gated DingTalk transcription review")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--manual", action="store_true")
    mode.add_argument("--handoff", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run-id")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        in_maintenance = maintenance_active()
    except IdleReviewError as exc:
        print(json.dumps({
            "status": "failed",
            "error_code": exc.code,
            "processed": 0,
            "activity_recorded": False,
        }, ensure_ascii=False, sort_keys=True))
        return 2
    if in_maintenance:
        print(json.dumps({
            "status": "deferred",
            "reason": "maintenance_active",
            "processed": 0,
            "done": 0,
            "deferred": 0,
            "failed": 0,
        }, ensure_ascii=False, sort_keys=True))
        return 0
    syncv2.initialize()
    if not args.manual:
        try:
            recover_stale_runtime()
        except (IdleReviewError, syncv2.SyncV2Error) as exc:
            print(json.dumps({
                "status": "failed",
                "error_code": getattr(exc, "code", "review_recovery_failed"),
                "processed": 0,
                "activity_recorded": False,
            }, ensure_ascii=False, sort_keys=True))
            return 2
    handoff = bool(args.handoff or (not args.manual and syncv2.minutes_review_handoff_pending()))
    if not args.manual and not handoff:
        try:
            config = chat_memory.load_config(args.config)
            review_config = config.get("dingtalk_review") if isinstance(config.get("dingtalk_review"), dict) else {}
            reason = automatic_gate_reason(review_config)
            if reason:
                print(json.dumps({
                    "status": "deferred",
                    "reason": reason,
                    "processed": 0,
                    "activity_recorded": False,
                }, ensure_ascii=False, sort_keys=True))
                return 0
            if not automatic_slots_available():
                print(json.dumps({
                    "status": "deferred",
                    "reason": "sync_or_review_busy",
                    "processed": 0,
                    "activity_recorded": False,
                }, ensure_ascii=False, sort_keys=True))
                return 0
            snapshot = policy_snapshot()
            if int(snapshot.get("review_ready") or 0) <= 0:
                print(json.dumps({
                    "status": "noop",
                    "reason": "no_ready_reviews",
                    "processed": 0,
                    "activity_recorded": False,
                    "review_waiting_source": int(snapshot.get("review_waiting_source") or 0),
                    "review_exempt": int(snapshot.get("review_exempt") or 0),
                }, ensure_ascii=False, sort_keys=True))
                return 0
        except IdleReviewError as exc:
            print(json.dumps({
                "status": "failed",
                "error_code": exc.code,
                "processed": 0,
                "activity_recorded": False,
            }, ensure_ascii=False, sort_keys=True))
            return 2
    action_type = "review_now" if args.manual else "locked_review"
    run_id = str(args.run_id or syncv2.start_action_run(action_type))
    try:
        result = run_review(
            args.config,
            run_id,
            manual=bool(args.manual),
            handoff=handoff,
        )
        print(json.dumps({"run_id": run_id, **result}, ensure_ascii=False, sort_keys=True))
        return 0
    except (IdleReviewError, syncv2.SyncV2Error) as exc:
        code = str(getattr(exc, "code", "failed"))
        syncv2.finish_action_run(run_id, "failed", error_code=code)
        print(json.dumps({"status": "failed", "run_id": run_id, "error_code": code}, ensure_ascii=False))
        return 2
    except Exception as exc:
        code = type(exc).__name__
        syncv2.finish_action_run(run_id, "failed", error_code=code)
        print(json.dumps({"status": "failed", "run_id": run_id, "error_code": code}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    import owner_retirement
    owner_retirement.require_running()
    raise SystemExit(main())
