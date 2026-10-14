#!/usr/bin/env python3
"""Single local-only controller for the five BUSINESS memory sources."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import argparse
import fcntl
import json
import os
import re
import time
import sqlite3
import subprocess
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import dingtalk_memory_automation as chat_memory
import dingtalk_sync_orchestrator as dingtalk
import dingtalk_sync_v2 as syncv2
from system_dashboard_status import DashboardPaths, SystemDashboard


os.umask(0o077)

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.json"
MAINTENANCE_STATE = ROOT / "state" / "maintenance-mode.json"
CONTROL_LOCK = ROOT / "state" / "sync-center-control.lock"
EMAIL_SYNC = ROOT / "bin" / "email_sync.py"
WIN11_SYNC = ROOT / "bin" / "business_sync_now.sh"
WECHAT_SYNC = ROOT / "bin" / "run_wechat_pipeline.sh"
WECHAT_STATUS = Path(
    _archive_path('/Users/example/AI/LocalData/BUSINESS-NonObsidian.nosync/wechat-chat/state/status.json')
)
AUTOMATIC_MODE = False

PUBLIC_TARGETS = frozenset({
    "dingtalk_chat",
    "dingtalk_minutes",
    "dingtalk_mail",
    "win11",
    "wechat",
    "all",
})
INTERNAL_TARGETS = PUBLIC_TARGETS | {"dingtalk_all"}

STEP_META = {
    "prepare": (10, "检查记忆库与控制锁"),
    "wait_review": (15, "等待当前钉钉二查结束"),
    "recording_scan": (20, "扫描钉钉 AI 听记"),
    "recording_fetch": (30, "同步听记文字与原始媒体"),
    "recording_source": (40, "整理听记来源"),
    "chat_reconcile": (50, "同步钉钉聊天"),
    "chat_memory": (60, "更新聊天业务记忆"),
    "recording_memory": (70, "更新听记业务记忆"),
    "decision_registration": (80, "登记待核验内容"),
    "dingtalk_validation": (90, "校验钉钉记忆"),
    "email_sync": (110, "同步钉钉邮件"),
    "win11_sync": (210, "同步 Win11 记忆"),
    "wechat_sync": (310, "同步微信记忆"),
}
DEFERRED_ERROR_CODES = frozenset({
    "already_running", "control_lock_unavailable", "maintenance_active",
    "chat_ai_priority", "recording_semantic_deferred",
    "maintenance_state_unknown", "memory_core_unknown", "memory_core_unavailable",
    "memory_core_unverified", "memory_core_busy", "waiting_power", "validation_inputs_changed",
    "memory_lock_busy", "win11_merge_memory_lock_timeout",
})


class ControllerError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        receipt: dict[str, Any] | None = None,
        global_stop: bool = False,
    ):
        super().__init__(code)
        self.code = re.sub(r"[^A-Za-z0-9_.:-]", "_", str(code))[:100] or "failed"
        self.receipt = receipt or {}
        self.global_stop = global_stop


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def example_bd50c44d55d3() -> bool:
    try:
        process = subprocess.run(
            ["/usr/bin/pmset", "-g", "ps"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return process.returncode == 0 and "AC Power" in f"{process.stdout}\n{process.stderr}"


def require_automatic_power() -> None:
    """Pause only between indivisible stages when an automatic run loses AC."""
    if AUTOMATIC_MODE and not example_bd50c44d55d3():
        raise ControllerError("waiting_power", global_stop=True)


@contextmanager
def control_lock(target: str | None = None) -> Iterator[Any]:
    # Independent sources may collect concurrently; writes still share the
    # existing downstream index lock. Component and AI locks are unchanged.
    # Chat/minutes share MANUAL_SYNC_FLAG, so they must share a source lock.
    source = {
        "dingtalk_mail": "mail",
        "win11": "win11",
        "dingtalk_chat": "dingtalk",
        "dingtalk_minutes": "dingtalk",
        "wechat": "wechat",
    }.get(target)
    # Legacy no-argument callers and combined all/dingtalk_all retain global
    # EX (unknown targets also fail conservatively into EX). Never upgrade:
    # acquire the gate first, then the source, both strictly nonblocking.
    requested = [(CONTROL_LOCK, fcntl.LOCK_SH if source else fcntl.LOCK_EX)]
    if source:
        requested.append((
            CONTROL_LOCK.with_name(f"{CONTROL_LOCK.stem}-{source}.lock"),
            fcntl.LOCK_EX,
        ))
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    # Closing descriptors releases flock locks in reverse order, including
    # the gate when opening/acquiring the source fails. Never unlink locks.
    with ExitStack() as handles:
        gate = None
        for path, mode in requested:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(path, flags, 0o600)
                try:
                    handle = os.fdopen(descriptor, "a+")
                except BaseException:
                    os.close(descriptor)
                    raise
                handles.enter_context(handle)
                fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
                # Apply permissions to the opened inode, not a replaced path.
                os.fchmod(handle.fileno(), 0o600)
            except BlockingIOError as exc:
                raise ControllerError("already_running", global_stop=True) from exc
            except OSError as exc:
                raise ControllerError("control_lock_unavailable", global_stop=True) from exc
            if gate is None:
                gate = handle
        yield gate


def action_status(run_id: str) -> str:
    connection = syncv2.connect_readonly()
    try:
        row = connection.execute(
            "SELECT status FROM action_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        return str(row["status"] if row else "missing")
    finally:
        connection.close()


def set_step(
    run_id: str,
    key: str,
    status: str,
    *,
    current: int | None = None,
    total: int | None = None,
    detail: dict[str, Any] | None = None,
    error_code: str | None = None,
) -> None:
    order, label = STEP_META[key]
    syncv2.set_action_step(
        run_id,
        key,
        order,
        label,
        status,
        current_count=current,
        total_count=total,
        detail=detail,
        error_code=error_code,
    )


def step_keys(target: str) -> tuple[str, ...]:
    common = ("prepare",)
    if target == "dingtalk_chat":
        return common + (
            "wait_review", "chat_reconcile", "chat_memory",
            "decision_registration", "dingtalk_validation",
        )
    if target == "dingtalk_minutes":
        return common + (
            "wait_review", "recording_scan", "recording_fetch", "recording_source",
            "recording_memory", "decision_registration", "dingtalk_validation",
        )
    if target == "dingtalk_all":
        return common + (
            "wait_review", "recording_scan", "recording_fetch", "recording_source",
            "chat_reconcile", "chat_memory", "recording_memory",
            "decision_registration", "dingtalk_validation",
        )
    if target == "dingtalk_mail":
        return common + ("email_sync",)
    if target == "win11":
        return common + ("win11_sync",)
    if target == "wechat":
        return common + ("wechat_sync",)
    return common + (
        "wait_review", "recording_scan", "recording_fetch", "recording_source",
        "chat_reconcile", "chat_memory", "recording_memory",
        "decision_registration", "dingtalk_validation",
        "email_sync", "win11_sync", "wechat_sync",
    )


def initialize_steps(run_id: str, target: str) -> None:
    for key in step_keys(target):
        set_step(run_id, key, "queued")


def mark_running_step_failed(run_id: str, error_code: str) -> None:
    for step in syncv2.list_action_steps(run_id):
        if step["status"] != "running":
            continue
        set_step(
            run_id,
            str(step["key"]),
            "deferred" if error_code in DEFERRED_ERROR_CODES else "failed",
            current=step.get("current"),
            total=step.get("total"),
            detail=step.get("detail") if isinstance(step.get("detail"), dict) else None,
            error_code=error_code,
        )


def global_safety_error(config: dict[str, Any]) -> str | None:
    maintenance = read_json(MAINTENANCE_STATE)
    if maintenance is None:
        return "maintenance_state_unknown"
    if maintenance.get("status") != "inactive":
        return "maintenance_active"
    try:
        state = SystemDashboard(DashboardPaths.from_config(config)).collect(config, force=True)
        core = next(item for item in state["pipelines"] if item["id"] == "core")
    except Exception:
        return "memory_core_unknown"
    display = str((core.get("display_status") or {}).get("code") or "unknown")
    acceptance = str((core.get("acceptance") or {}).get("code") or "unknown")
    if display != "failed" and acceptance == "pending":
        return "memory_core_unverified"
    if display in {"maintenance", "failed", "stale", "unknown"}:
        return "memory_core_unavailable"
    if acceptance != "accepted":
        return "memory_core_unverified"
    if str((core.get("activity") or {}).get("code") or "idle") == "writing":
        return "memory_core_busy"
    return None


def require_global_safety(config: dict[str, Any]) -> None:
    code = global_safety_error(config)
    if code:
        raise ControllerError(code, global_stop=True)


def wait_global_safety(config: dict[str, Any], timeout: float = 45) -> None:
    """A concurrent index validation may briefly hold the next source boundary."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            require_global_safety(config)
            return
        except ControllerError as exc:
            if exc.code not in {"memory_core_unverified", "memory_core_busy"} or time.monotonic() >= deadline:
                raise
            time.sleep(2)


def dingtalk_environment(run_id: str) -> dict[str, str]:
    return {
        **os.environ,
        "LC_ALL": "en_US.UTF-8",
        "DINGTALK_MANUAL_SYNC": "1",
        "DINGTALK_SUPPRESS_COMPLETION_OUTBOX": "1",
        "DINGTALK_LOCAL_ONLY": "1",
        "DINGTALK_ACTION_RUN_ID": run_id,
        "BUSINESS_SYNC_TRIGGER": "automatic" if AUTOMATIC_MODE else "manual",
    }


def run_minutes_part(
    config_path: Path,
    run_id: str,
    environment: dict[str, str],
    result: dict[str, Any],
) -> bool:
    set_step(run_id, "recording_scan", "running")
    require_automatic_power()
    try:
        minutes = dingtalk.run_json(
            [
                "/usr/bin/python3", str(dingtalk.MINUTES_SYNC),
                "--scheduled" if AUTOMATIC_MODE else "--manual",
            ],
            timeout=3600,
            environment=environment,
            component="recording_sync",
            allow_partial=True,
        )
    except dingtalk.OrchestratorError as exc:
        if not dingtalk.recording_media_partial(exc.receipt):
            raise
        minutes = exc.receipt
    selected = int(minutes.get("selected") or 0)
    set_step(run_id, "recording_scan", "success" if selected else "noop", current=selected, total=selected)
    media_partial = dingtalk.recording_media_partial(minutes)
    media_detail = dingtalk.recording_media_detail(minutes) if media_partial else {}
    result["recordings_new"] += int(minutes.get("created") or 0)
    result["recordings_updated"] += int(minutes.get("updated") or 0)
    result["recording_media_pending"] += int(minutes.get("media_missing") or 0) if media_partial else 0
    set_step(
        run_id,
        "recording_fetch",
        "partial" if media_partial else ("success" if selected else "noop"),
        current=selected,
        total=selected,
        detail=media_detail or None,
    )
    syncv2.set_component_health(
        "recording_media",
        not media_partial,
        "recording_media_pending" if media_partial else None,
        safe_detail=media_detail,
    )

    require_automatic_power()
    set_step(run_id, "recording_source", "running")
    try:
        source = dingtalk.run_json(
            [
                "/usr/bin/python3", str(dingtalk.MINUTES_MEMORY),
                "--config", str(config_path), "source-sync",
            ],
            timeout=1800,
            environment=environment,
            component="recording_source",
            allow_partial=True,
        )
    except dingtalk.OrchestratorError as exc:
        if not media_partial or not dingtalk.recording_source_media_partial(exc.receipt):
            raise
        source = exc.receipt
    source_writes = int(source.get("source_notes_written") or 0)
    source_partial = dingtalk.recording_source_media_partial(source) or source.get("status") == "waiting_source"
    result["recording_source_waiting_items"] = source.get("waiting_items", [])
    result["recording_source_waiting_count"] = int(source.get("waiting_count") or 0)
    result["memory_writes"] += source_writes
    set_step(
        run_id,
        "recording_source",
        "partial" if source_partial else ("success" if source_writes else "noop"),
        current=source_writes,
        total=source_writes,
        detail={**(media_detail or {}), "source_waiting_count": source.get("waiting_count", 0), "waiting_items": source.get("waiting_items", [])} if source_partial else None,
    )

    queue = dingtalk.recording_queue()
    set_step(
        run_id,
        "recording_memory",
        "running" if queue else "noop",
        current=0,
        total=len(queue),
    )
    for position, recording_id in enumerate(queue, start=1):
        for _ in range(100):
            if dingtalk.recording_status(recording_id) not in dingtalk.PROCESSABLE_RECORDING_STATES:
                break
            require_automatic_power()
            semantic = dingtalk.run_json(
                [
                    "/usr/bin/python3", str(dingtalk.MINUTES_MEMORY),
                    "--config", str(config_path), "semantic",
                ],
                timeout=3600,
                environment=environment,
                component="recording_semantic",
            )
            if semantic.get("status") == "deferred":
                code = dingtalk.semantic_deferred_code(semantic)
                set_step(run_id, "recording_memory", "deferred", current=position - 1,
                         total=len(queue), detail={"reason": code}, error_code=code)
                raise ControllerError(code, receipt=semantic)
            result["model_calls"] += int(semantic.get("model_calls") or 0)
            for item in semantic.get("results") or []:
                if isinstance(item, dict):
                    result["memory_writes"] += int(item.get("writes") or 0)
        if dingtalk.recording_status(recording_id) in dingtalk.PROCESSABLE_RECORDING_STATES:
            raise ControllerError("recording_queue_not_drained")
        set_step(run_id, "recording_memory", "running", current=position, total=len(queue))
    if queue:
        set_step(run_id, "recording_memory", "success", current=len(queue), total=len(queue))
    return media_partial or source_partial


CHAT_FULL_INTERVAL_SECONDS = 24 * 3600
CHAT_FULL_TIMEOUT_SECONDS = 1800


def chat_full_due(config: dict[str, Any], now: float | None = None) -> bool:
    # Only a complete full read can see messages recalled or deleted at the source;
    # one attempt per day, counted from the last attempt so a failing read never loops.
    path = Path(config["external_root"]) / "dingtalk-chat/state/chat.sqlite"
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        row = connection.execute("SELECT MAX(started_at) FROM sync_runs WHERE mode='full'").fetchone()
    finally:
        connection.close()
    if not row or not row[0]:
        return True
    last = datetime.fromisoformat(str(row[0])).timestamp()
    return (now if now is not None else time.time()) - last >= CHAT_FULL_INTERVAL_SECONDS


def run_daily_chat_full(config_path: Path, environment: dict[str, str], config: dict[str, Any],
                        result: dict[str, Any]) -> None:
    """Advisory deletion check: never fails the chat source; hidden changes join the drain below."""
    if not AUTOMATIC_MODE:
        return
    try:
        if not chat_full_due(config):
            return
    except (KeyError, OSError, ValueError, sqlite3.Error):
        result["chat_full_read"] = {"status": "skipped", "error_code": "chat_full_state_unavailable"}
        return
    require_automatic_power()
    try:
        process = run_process(
            ["/usr/bin/python3", str(ROOT / "bin/sync_dingtalk_chat.py"), "--config", str(config_path),
             "--mode", "full", "--no-ledger"],
            timeout=CHAT_FULL_TIMEOUT_SECONDS, environment=environment,
        )
        receipt = json.loads(process.stdout) if process.stdout.strip() else {}
    except (ControllerError, ValueError) as exc:
        result["chat_full_read"] = {"status": "failed", "error_code": str(getattr(exc, "code", "chat_full_invalid_receipt"))}
        return
    result["chat_full_read"] = {
        "status": str(receipt.get("status") or f"exit_{process.returncode}"),
        "error_code": receipt.get("error_code"),
        "messages_hidden": int(receipt.get("messages_hidden") or 0),
        "conversations_incomplete": int(receipt.get("conversations_incomplete") or 0),
    }
    result["chat_messages_changed"] += (int(receipt.get("messages_inserted") or 0)
                                        + int(receipt.get("messages_updated") or 0)
                                        + int(receipt.get("messages_hidden") or 0))


def run_chat_part(
    config_path: Path,
    run_id: str,
    environment: dict[str, str],
    config: dict[str, Any],
    result: dict[str, Any],
) -> None:
    set_step(run_id, "chat_reconcile", "running", current=0, total=1)
    require_automatic_power()
    receipt = dingtalk.chat_reconcile_once(config_path, environment)
    incremental = receipt.get("incremental") if isinstance(receipt.get("incremental"), dict) else {}
    result.update(dingtalk.chat_reconcile_context(receipt))
    result["chat_messages_changed"] += (
        int(incremental.get("messages_inserted") or 0)
        + int(incremental.get("messages_updated") or 0)
    )
    result["memory_writes"] += int(receipt.get("writes") or 0)
    result["model_calls"] += int(bool(receipt.get("model_called")))
    complete = dingtalk.chat_reconcile_complete(receipt)
    set_step(run_id, "chat_reconcile", "success" if complete else "partial", current=1, total=1,
             error_code=None if complete else dingtalk.chat_reconcile_error(receipt))
    run_daily_chat_full(config_path, environment, config, result)

    queued = chat_memory.eligible_queue_count(config)
    set_step(run_id, "chat_memory", "running" if queued else "noop", current=0, total=queued)
    processed = 0
    for _ in range(50):
        remaining = chat_memory.eligible_queue_count(config)
        if remaining == 0:
            break
        require_automatic_power()
        drained = dingtalk.run_json(
            [
                "/usr/bin/python3", str(dingtalk.CHAT_MEMORY),
                "--config", str(config_path), "hourly", "--manual", "--skip-reconcile",
            ],
            timeout=7200,
            environment=environment,
            component="chat_memory",
        )
        result["memory_writes"] += int(drained.get("writes") or 0)
        result["model_calls"] += int(bool(drained.get("model_called")))
        now_remaining = chat_memory.eligible_queue_count(config)
        processed = max(processed, queued - now_remaining)
        set_step(
            run_id,
            "chat_memory",
            "running",
            current=processed,
            total=max(queued, processed + now_remaining),
        )
    if chat_memory.eligible_queue_count(config) != 0:
        raise ControllerError("chat_queue_not_drained")
    if queued:
        set_step(run_id, "chat_memory", "success", current=queued, total=queued)
    if not complete:
        raise ControllerError(dingtalk.chat_reconcile_error(receipt), receipt={**receipt, **result})
    if (config.get("dingtalk_attachment_handoff") or {}).get("enabled") is True:
        # Extend this existing source cycle, not a new service or timer. An
        # attachment failure must not erase completed chat work or stop peers.
        try:
            attachments = dingtalk.run_json(
                ["/usr/bin/python3", str(dingtalk.CHAT_MEMORY), "--config", str(config_path), "attachments-once"],
                timeout=3600, environment=environment, component="dingtalk_attachments")
        except Exception as exc:
            attachments = {"status": "blocked", "error_code": getattr(exc, "code", type(exc).__name__)}
        result["dingtalk_attachments"] = attachments
        result["memory_writes"] += int(attachments.get("writes") or 0)
        result["model_calls"] += int(attachments.get("model_calls") or 0)


def finalize_dingtalk(
    config_path: Path,
    run_id: str,
    environment: dict[str, str],
    config: dict[str, Any],
    result: dict[str, Any],
) -> None:
    require_automatic_power()
    set_step(run_id, "decision_registration", "running")
    registration = dingtalk.run_json(
        [
            "/usr/bin/python3", str(dingtalk.DECISION_CENTER),
            "--config", str(config_path), "--sync-only",
        ],
        timeout=300,
        environment=environment,
        component="decision_registration",
    )
    registered = registration.get("registered") if isinstance(registration.get("registered"), dict) else {}
    result["decision_created"] += int(registered.get("created") or 0)
    result["decision_updated"] += int(registered.get("updated") or 0)
    decisions = result["decision_created"] + result["decision_updated"]
    set_step(
        run_id,
        "decision_registration",
        "success" if decisions else "noop",
        current=decisions,
        total=decisions,
    )
    changed = any(
        int(result.get(key) or 0) > 0
        for key in ("recordings_new", "recordings_updated", "chat_messages_changed", "memory_writes")
    )
    require_automatic_power()
    set_step(run_id, "dingtalk_validation", "running")
    try:
        verification = chat_memory.run_refresh_and_validate(config, refresh_indexes=changed, run_id=run_id)
    except chat_memory.AutomationError as exc:
        if exc.code == "validation_inputs_changed":
            import automatic_memory_validation as validation
            generation = validation.index_state(config_path.parent)["dirty"]
            point = {"pending_phase": "validation", "required_generation": generation,
                     "source_run_id": run_id, "sync_completed": True, "pending_since": chat_memory.now_iso()}
            import validation_retry
            validation_retry.request_validation(config_path.parent, point)
            set_step(run_id, "dingtalk_validation", "deferred", detail=point, error_code=exc.code)
            raise ControllerError(exc.code, receipt=point, global_stop=True) from exc
        raise
    if (verification.get("validator") or {}).get("verdict") != "pass":
        raise ControllerError("memory_validation_failed", global_stop=True)
    set_step(run_id, "dingtalk_validation", "success")


def run_dingtalk_scopes(config_path: Path, run_id: str, scopes: tuple[str, ...]) -> dict[str, Any]:
    os.environ["DINGTALK_LOCAL_ONLY"] = "1"
    config = chat_memory.load_config(config_path)
    environment = dingtalk_environment(run_id)
    result: dict[str, Any] = {
        "recordings_new": 0,
        "recordings_updated": 0,
        "recording_media_pending": 0,
        "chat_messages_changed": 0,
        "memory_writes": 0,
        "model_calls": 0,
        "decision_created": 0,
        "decision_updated": 0,
        "local_only": True,
    }
    failures: list[dict[str, str]] = []
    media_partial = False
    with syncv2.file_lock(syncv2.MANUAL_SYNC_LOCK):
        chat_memory.atomic_write(
            syncv2.MANUAL_SYNC_FLAG,
            json.dumps({"run_id": run_id, "started_at": syncv2.now_iso()}, ensure_ascii=False) + "\n",
            mode=0o600,
        )
        try:
            set_step(run_id, "wait_review", "running")
            try:
                # A manual run may deliberately wait for the operator's active
                # review to finish.  The periodic supervisor must never occupy
                # its worker for hours behind that lock: defer this source and
                # let the next scheduled probe try again instead.
                with syncv2.file_lock(syncv2.REVIEW_LOCK, blocking=not AUTOMATIC_MODE):
                    pass
            except syncv2.SyncV2Error as exc:
                if AUTOMATIC_MODE and exc.code == "already_running":
                    set_step(
                        run_id,
                        "wait_review",
                        "deferred",
                        detail={"reason": "review_busy"},
                        error_code="already_running",
                    )
                    raise ControllerError("already_running") from exc
                raise
            set_step(run_id, "wait_review", "success")
            for scope in scopes:
                try:
                    require_automatic_power()
                    if scope == "minutes":
                        media_partial = run_minutes_part(config_path, run_id, environment, result)
                    elif scope == "chat":
                        run_chat_part(config_path, run_id, environment, config, result)
                    else:
                        raise ControllerError("dingtalk_scope_invalid")
                except Exception as exc:
                    code = str(getattr(exc, "code", type(exc).__name__))
                    mark_running_step_failed(run_id, code)
                    failures.append({"target": f"dingtalk_{scope}", "error_code": code})
                    exc.receipt = {**getattr(exc, "receipt", {}), **result}
                    if len(scopes) == 1:
                        raise
            if len(failures) == len(scopes):
                raise ControllerError(failures[0]["error_code"], receipt=result)
            finalize_dingtalk(config_path, run_id, environment, config, result)
            result["failures"] = failures
            result["status"] = "partial" if failures or media_partial else (
                "success" if any(int(result.get(key) or 0) for key in (
                    "recordings_new", "recordings_updated", "chat_messages_changed", "memory_writes"
                )) else "noop"
            )
            syncv2.set_component_health(
                "manual_sync",
                not failures,
                failures[0]["error_code"] if failures else None,
            )
            return result
        finally:
            if syncv2.MANUAL_SYNC_FLAG.is_file() and not syncv2.MANUAL_SYNC_FLAG.is_symlink():
                syncv2.MANUAL_SYNC_FLAG.unlink(missing_ok=True)


def decode_json_receipt(stdout: str) -> dict[str, Any]:
    try:
        payload = dingtalk.decode_component_receipt(stdout)
    except Exception as exc:
        raise ControllerError("component_invalid_receipt") from exc
    return payload


def run_process(
    command: list[str],
    *,
    timeout: int,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
            env=environment or {**os.environ, "LC_ALL": "en_US.UTF-8"},
        )
    except subprocess.TimeoutExpired as exc:
        raise ControllerError("component_timeout") from exc
    except OSError as exc:
        raise ControllerError("component_unavailable") from exc


def run_email(config_path: Path, run_id: str) -> dict[str, Any]:
    require_automatic_power()
    set_step(run_id, "email_sync", "running")
    process = run_process(
        ["/usr/bin/python3", str(EMAIL_SYNC), "--config", str(config_path), "sync-local"],
        timeout=10800,
    )
    payload = decode_json_receipt(process.stdout)
    status = str(payload.get("status") or "failed")
    reason = str(payload.get("reason_code") or "")
    if process.returncode != 0 or status in {"failed", "blocked"} or (status == "noop" and reason):
        raise ControllerError(reason or f"email_exit_{process.returncode}", receipt=payload)
    if payload.get("report_policy") not in {None, "local_only"} or payload.get("local_only") not in {None, True}:
        raise ControllerError("email_local_only_unconfirmed")
    archive = payload.get("archive") or {}
    archive_failed = int(archive.get("unresolved", archive.get("failed", 0)) or 0)
    archive_frozen = int(archive.get("frozen") or 0)
    archive_deferred = int(archive.get("deferred") or 0)
    semantic_failed = int((payload.get("semantic") or {}).get("failed") or 0)
    delivery_failed = sum(
        int((payload.get("delivery") or {}).get(key) or 0)
        for key in ("failed", "uncertain")
    )
    failed_items = archive_failed + semantic_failed + delivery_failed
    result_status = "partial" if failed_items else ("noop" if status == "noop" else "success")
    set_step(
        run_id,
        "email_sync",
        result_status,
        detail={
            "failed_items": failed_items,
            "archive_frozen": archive_frozen,
            "archive_deferred": archive_deferred,
        } if failed_items else None,
        error_code="email_items_failed" if failed_items else None,
    )
    return {
        "status": result_status,
        "discovered": int(payload.get("discovered") or 0),
        "memory_writes": int((payload.get("semantic") or {}).get("memory_writes",
                            (payload.get("semantic") or {}).get("writes", 0)) or 0),
        "reports_queued": int(payload.get("reports_queued") or 0),
        "delivery_count": sum(int(value or 0) for value in (payload.get("delivery") or {}).values()),
        "failed_items": failed_items,
        "archive_frozen": archive_frozen,
        "archive_deferred": archive_deferred,
        "recovery": payload.get("recovery") or {},
        "automatic_repair": payload.get("automatic_repair") or {},
        "collection_only": payload.get("collection_only") is True,
        "phases": payload.get("phases") or {},
        "heavy_deferred": payload.get("heavy_deferred") is True,
        "heavy_pending": int(payload.get("heavy_pending") or 0),
        "deferred_reason": str(payload.get("deferred_reason") or "") or None,
        "error_code": "email_items_failed" if failed_items else None,
        "failures": (
            [{"target": "dingtalk_mail", "error_code": "email_items_failed"}]
            if failed_items else []
        ),
        "local_only": True,
    }


def run_win11(run_id: str) -> dict[str, Any]:
    require_automatic_power()
    set_step(run_id, "win11_sync", "running")
    command = [str(WIN11_SYNC)]
    if AUTOMATIC_MODE:
        command.append("--hourly")
    started = time.time()
    process = run_process(command, timeout=10800)
    health = read_json(ROOT / "state/win11_memory_health.json") or {}
    if process.returncode != 0:
        # The shell wrapper discards the merge step's stderr; its health file
        # carries the real failure code (merge_targets, privacy, ...). Only
        # trust it when this run wrote it.
        detail = str(health.get("detail") or "")
        fresh = (ROOT / "state/win11_memory_health.json").stat().st_mtime >= started if health else False
        if fresh and health.get("status") == "failed" and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,59}", detail):
            raise ControllerError(f"win11_{detail}")
        raise ControllerError(f"win11_exit_{process.returncode}")
    pending = int(health.get("pending_events") or 0)
    blocked = int(health.get("blocked_items") or 0)
    outcome = "partial" if pending or blocked else "success"
    set_step(run_id, "win11_sync", outcome)
    return {"status": outcome, "local_notification": True, "pending_events": pending,
            "remaining_batches": int(health.get("remaining_batches") or 0), "blocked_items": blocked}


def wechat_available(config: dict[str, Any]) -> bool:
    status = read_json(WECHAT_STATUS)
    section = config.get("wechat_chat") if isinstance(config.get("wechat_chat"), dict) else {}
    fingerprint = str(section.get("account_fingerprint") or "")
    return bool(
        status
        and status.get("status") in {"success", "idle", "ok"}
        and status.get("error_code") != "enrollment_required"
        and fingerprint not in {"", "enrollment_required"}
    )


def run_wechat(config: dict[str, Any], run_id: str, *, allow_skip: bool) -> dict[str, Any]:
    if not wechat_available(config):
        set_step(
            run_id,
            "wechat_sync",
            "skipped" if allow_skip else "deferred",
            detail={"reason": "enrollment_required"},
            error_code=None if allow_skip else "enrollment_required",
        )
        if allow_skip:
            return {"status": "skipped", "reason": "enrollment_required"}
        raise ControllerError("enrollment_required")
    set_step(run_id, "wechat_sync", "running")
    process = run_process([str(WECHAT_SYNC)], timeout=10800)
    if process.returncode != 0:
        raise ControllerError(f"wechat_exit_{process.returncode}")
    set_step(run_id, "wechat_sync", "success")
    return {"status": "success"}


def execute(config_path: Path, run_id: str, target: str) -> dict[str, Any]:
    config = chat_memory.load_config(config_path)
    initialize_steps(run_id, target)
    set_step(run_id, "prepare", "running")
    require_global_safety(config)
    require_automatic_power()
    set_step(run_id, "prepare", "success")

    outcomes: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, str]] = []
    completed = 0

    if target in {"dingtalk_chat", "dingtalk_minutes", "dingtalk_all"}:
        scopes = {
            "dingtalk_chat": ("chat",),
            "dingtalk_minutes": ("minutes",),
            "dingtalk_all": ("minutes", "chat"),
        }[target]
        result = run_dingtalk_scopes(config_path, run_id, scopes)
        outcomes[target] = result
        failures.extend(result.get("failures") or [])
        completed += 1
    elif target == "dingtalk_mail":
        outcomes[target] = run_email(config_path, run_id)
        failures.extend(outcomes[target].get("failures") or [])
        completed += 1
    elif target == "win11":
        outcomes[target] = run_win11(run_id)
        completed += 1
    elif target == "wechat":
        outcomes[target] = run_wechat(config, run_id, allow_skip=False)
        completed += 1
    elif target == "all":
        tasks = (
            ("dingtalk_all", lambda: run_dingtalk_scopes(config_path, run_id, ("minutes", "chat"))),
            ("dingtalk_mail", lambda: run_email(config_path, run_id)),
            ("win11", lambda: run_win11(run_id)),
            ("wechat", lambda: run_wechat(config, run_id, allow_skip=True)),
        )
        for source, operation in tasks:
            if source == "wechat" and not wechat_available(config):
                outcomes[source] = operation()
                continue
            try:
                wait_global_safety(config)
                require_automatic_power()
            except ControllerError as exc:
                failures.append({"target": "global", "error_code": exc.code})
                outcomes["global"] = {"status": "failed", "error_code": exc.code}
                break
            try:
                value = operation()
                outcomes[source] = value
                failures.extend(value.get("failures") or [])
                completed += int(value.get("status") != "skipped")
            except Exception as exc:
                code = str(getattr(exc, "code", type(exc).__name__))
                mark_running_step_failed(run_id, code)
                failures.append({"target": source, "error_code": code})
                outcomes[source] = {**dingtalk.chat_reconcile_context(getattr(exc, "receipt", {})),
                                    "status": "failed", "error_code": code}
                if bool(getattr(exc, "global_stop", False)):
                    break
    else:
        raise ControllerError("target_invalid")

    changed = any(
        value.get("status") == "success"
        or any(int(value.get(key) or 0) > 0 for key in ("discovered", "memory_writes", "recordings_new", "chat_messages_changed"))
        for value in outcomes.values()
    )
    source_partial = any(value.get("status") == "partial" for value in outcomes.values())
    if failures or source_partial:
        status = "partial" if completed else "failed"
    else:
        status = "success" if changed else "noop"
    return {
        "status": status,
        "target": target,
        "local_only": True,
        "completed_sources": completed,
        "failures": failures,
        "sources": outcomes,
        "trigger": "automatic" if AUTOMATIC_MODE else "manual",
    }


def action_type_for_target(target: str) -> str:
    return "sync_now" if target == "dingtalk_all" else target


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one local-only BUSINESS memory source control")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--run-id")
    parser.add_argument("--automatic", action="store_true")
    parser.add_argument("target", choices=sorted(INTERNAL_TARGETS))
    return parser.parse_args()


def record_manual_chat_result(config_path: Path, target: str, receipt: dict[str, Any]) -> None:
    """Feed fresh local receipts back to the existing supervisor for each source."""
    if AUTOMATIC_MODE or (receipt.get("status") == "deferred" and receipt.get("error_code") != "validation_inputs_changed"):
        return
    try:
        import business_sync_supervisor as supervisor
        service = supervisor.Supervisor(config_path)
        outcomes = receipt.get("sources") or ({target: receipt} if target in {
            "dingtalk_chat", "dingtalk_minutes", "dingtalk_mail", "win11"} else {})
        rows = dict(outcomes)
        combined = rows.pop("dingtalk_all", None)
        if combined and combined.get("status") in {"success", "noop", "partial"}:
            for source_id in ("dingtalk_chat", "dingtalk_minutes"):
                errors = [x for x in combined.get("failures", []) if x.get("target") == source_id]
                value = {**combined, "failures": errors}
                if errors:
                    value.update(status="failed", error_code=errors[0]["error_code"], recording_media_pending=0)
                else:
                    value["status"] = "partial" if source_id == "dingtalk_minutes" and (combined.get("recording_media_pending") or combined.get("recording_source_waiting_count")) else "success"
                rows[source_id] = value
        for source_id, value in rows.items():
            if source_id not in supervisor.SOURCE_NAMES or value.get("status") == "skipped" or (value.get("status") == "deferred" and value.get("error_code") != "validation_inputs_changed"):
                continue
            service.record_outcome(source_id, {
                **value, "run_id": receipt.get("run_id"), "sources": {source_id: value},
            }, supervisor.now_dt())
        # local_only never invokes notification delivery. Existing background
        # notification policy remains owned by the supervisor.
    except Exception as exc:
        # A notification/state handoff problem must not change the already
        # committed business result or discard its action-run receipt.
        config = chat_memory.load_config(config_path)
        chat_memory.append_log(chat_memory.runtime_paths(config)["log"],
                               "manual_result_handoff_failed", error_code=type(exc).__name__)


def main() -> int:
    global AUTOMATIC_MODE
    args = parse_args()
    AUTOMATIC_MODE = bool(args.automatic)
    syncv2.initialize()
    trigger = "automatic" if AUTOMATIC_MODE else "manual"
    run_id = str(
        args.run_id
        or syncv2.start_action_run(action_type_for_target(args.target), trigger=trigger)
    )
    syncv2.set_action_trigger(run_id, trigger)
    result: dict[str, Any]
    try:
        import action_run_owner
        with action_run_owner.lease(ROOT, run_id, args.target):
            with control_lock(args.target):
                result = execute(args.config, run_id, args.target)
        syncv2.finish_action_run(
            run_id,
            str(result["status"]),
            result=result,
            error_code=(result.get("failures") or [{}])[0].get("error_code") if result.get("failures") else None,
        )
        record_manual_chat_result(args.config, args.target, {"run_id": run_id, **result})
        print(json.dumps({"run_id": run_id, **result}, ensure_ascii=False, sort_keys=True))
        return 0 if result["status"] in {"success", "noop", "partial"} else 2
    except Exception as exc:
        code = str(getattr(exc, "code", type(exc).__name__))
        try:
            mark_running_step_failed(run_id, code)
            if action_status(run_id) == "running":
                final_status = "deferred" if code in DEFERRED_ERROR_CODES else "failed"
                syncv2.finish_action_run(
                    run_id,
                    final_status,
                    result={"target": args.target, "local_only": True, "trigger": trigger, **getattr(exc, "receipt", {})},
                    error_code=code,
                )
        except Exception:
            pass
        final_status = "deferred" if code in DEFERRED_ERROR_CODES else "failed"
        context = dingtalk.chat_reconcile_context(getattr(exc, "receipt", {}))
        record_manual_chat_result(args.config, args.target,
                                  {**context, "status": final_status, "run_id": run_id, "error_code": code})
        print(json.dumps({
            **context,
            "status": final_status,
            "run_id": run_id,
            "target": args.target,
            "error_code": re.sub(r"[^A-Za-z0-9_.:-]", "_", code)[:100],
            "local_only": True,
            "trigger": "automatic" if AUTOMATIC_MODE else "manual",
        }, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    import owner_retirement
    owner_retirement.require_running()
    raise SystemExit(main())
