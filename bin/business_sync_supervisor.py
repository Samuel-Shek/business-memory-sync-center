#!/usr/bin/env python3
"""Power-aware, persistent supervisor for the four local memory sources."""

from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import time
import supervisor_worker_progress
import supervisor_worker_pool
import supervisor_liveness
import chat_read_recovery
import argparse
import ctypes
import fcntl
import hashlib
import marshal
import json
import math
import os
import re
import random
import sqlite3
import ledger_recovery
import sqlite_attribution
import subprocess
import sys
import tempfile
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

import email_sync
import business_sync_state as state
from system_dashboard_status import DashboardPaths, SystemDashboard


os.umask(0o077)

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.json"
CONTROLLER = ROOT / "bin" / "business_sync_controller.py"
MAINTENANCE_STATE = ROOT / "state" / "maintenance-mode.json"
SUPERVISOR_LOCK = ROOT / "state" / "sync-supervisor.lock"
MISO_BRIDGE = ROOT / "bin" / "miso_health_bridge.py"
MINUTES_AUTOMATION_STATE = Path(
    _archive_path('/Users/example/AI/LocalData/DingTalk-AI-Minutes.nosync/.state/automation-state.json')
)
SOURCE_ORDER = ("dingtalk_chat", "dingtalk_minutes", "dingtalk_mail", "win11")
SOURCE_NAMES = {
    "dingtalk_chat": "钉钉聊天",
    "dingtalk_minutes": "钉钉 AI 听记",
    "dingtalk_mail": "钉钉邮件",
    "win11": "Win11",
    "memory_core": "记忆库核心",
    "system": "同步中心后台检查程序",
}
NORMAL_INTERVAL = timedelta(minutes=5)
RETRY_DELAYS = (
    timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15), timedelta(minutes=60)
)
PERSISTENT_PROBE = timedelta(hours=6)
CHAT_CONCURRENT_RETRY_LIMIT = 3
MEDIA_DELAYS = (
    timedelta(minutes=30), timedelta(hours=2), timedelta(hours=6), timedelta(hours=24)
)
MEDIA_DAILY_PROBE = timedelta(hours=24)
BLOCK_ALERT_AFTER = timedelta(hours=2)
# A recovery notice that could not be sent within this window is stale. Sending
# it late only adds noise, and an undeliverable one at the head of the queue
# starved every newer alert from 2020-01-02 until 2020-01-02.
STALE_RECOVERY_NOTICE = timedelta(hours=6)
POWER_ALERT_AFTER = 5
WAKE_GAP = timedelta(minutes=3)
IDLE_REQUIRED_SECONDS = 180
# Pure pointer motion is intentionally excluded.  Some mice and USB receivers
# generate tiny motion events while nobody is using the Mac, which otherwise
# keeps IOHIDSystem's aggregate idle counter near zero forever.  Clicks,
# drags, scrolling, keys and modifier changes remain meaningful activity.
MEANINGFUL_EVENT_TYPES = (1, 3, 6, 7, 10, 12, 22, 25, 27)
COREGRAPHICS_FRAMEWORK = (
    "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
)
CG_EVENT_SOURCE_HID_SYSTEM_STATE = 1
DEFERRED_CODES = frozenset({
    "already_running", "control_lock_unavailable", "maintenance_active",
    "chat_ai_priority", "recording_semantic_deferred",
    "memory_core_busy", "waiting_power", "validation_inputs_changed",
    "memory_lock_busy", "win11_merge_memory_lock_timeout",
})
UNKNOWN_BLOCK_CODES = frozenset({
    "maintenance_state_unknown", "memory_core_unknown", "memory_core_unavailable",
    "memory_core_unverified", "controller_unavailable", "component_invalid_receipt",
})
PRESERVE_WHILE_WAITING = frozenset({
    "manual_required", "waiting_source", "retrying", "persistent_failure", "deferred",
})
MAIL_READ_SAFETY_CODES = frozenset({
    "read_safety_unconfirmed", "read_safety_failed", "read_safety_server_unverified",
    "read_safety_receipt_unsafe",
})


LOADED_SUPERVISOR_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class SupervisorError(RuntimeError):
    pass


def now_dt() -> datetime:
    return datetime.now().astimezone()


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def record_runtime_error(exc: Exception, *, root: Path = ROOT, at: datetime | None = None, database: Path | None = None) -> dict[str, Any]:
    from index_incident_evidence import runtime_event_guard
    with runtime_event_guard(root):
        return _record_runtime_error(exc, root=root, at=at, database=database)


def _record_runtime_error(exc: Exception, *, root: Path = ROOT, at: datetime | None = None, database: Path | None = None) -> dict[str, Any]:
    """Keep a diagnosable failure even when launchd discards stdout/stderr.

    Deliberately omit exception messages, source lines, locals and arbitrary
    paths: they may contain source content or credentials.
    """
    reason = type(exc).__name__
    if isinstance(exc, sqlite3.Error):
        message = str(exc).lower()
        if "locked" in message or "busy" in message:
            reason = "sqlite_database_busy"
        elif "unable to open database" in message:
            reason = "sqlite_database_unavailable"
    frames = []
    for frame in traceback.extract_tb(exc.__traceback__):
        path = Path(frame.filename)
        frames.append({"file": str(path.relative_to(root)) if path.is_relative_to(root) else "external_dependency",
                       "line": frame.lineno, "function": frame.name})
    tb = exc.__traceback__
    for frame in frames:
        if tb is not None:
            frame["loaded_code_sha256"] = hashlib.sha256(marshal.dumps(tb.tb_frame.f_code)).hexdigest()
            tb = tb.tb_next
    payload = {"schema_version": 1, "recorded_at": state.now_iso(at), "pid": os.getpid(),
               "error_type": type(exc).__name__, "reason": reason, "frames": frames[-16:]}
    payload["process_context"] = sqlite_attribution.origin()
    if isinstance(exc, sqlite3.Error) and not isinstance(getattr(exc, "safe_sqlite_evidence", None), dict):
        from sqlite_read_diagnostics import error_evidence
        error_database = database or (state.DATABASE if root == ROOT else root / "state/sync.sqlite")
        payload["sqlite"] = error_evidence(exc, error_database, "supervisor_runtime")
        if payload["sqlite"]["sqlite_code"] == 5:
            payload["sqlite"]["attribution"] = sqlite_attribution.snapshot(error_database)
    if isinstance(getattr(exc, "safe_sqlite_evidence", None), dict):
        payload["sqlite"] = exc.safe_sqlite_evidence
    from index_incident_evidence import safe_exception
    if type(exc).__name__ == 'ValidationFault':
        payload.update(safe_exception(exc))
    directory = root / "state"
    temporary = None
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix=".supervisor-error-", dir=directory)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, directory / "sync-supervisor-last-error.json")
        with (directory / "sync-supervisor-errors.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        pass
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    return payload


def maintenance_status() -> dict[str, Any]:
    payload = read_json(MAINTENANCE_STATE)
    if payload is None:
        return {"active": True, "status": "unknown", "exited_at": None}
    status = str(payload.get("status") or "unknown")
    return {
        "active": status != "inactive",
        "status": status,
        "exited_at": payload.get("exited_at"),
    }


def maintenance_active() -> bool:
    return bool(maintenance_status()["active"])


@contextmanager
def supervisor_lock(path: Path = SUPERVISOR_LOCK) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    handle = os.fdopen(descriptor, "a+")
    acquired = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            acquired = False
        yield acquired
    finally:
        if acquired:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def default_command_runner(arguments: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
        env={**os.environ, "LC_ALL": "en_US.UTF-8"},
    )


def power_state(
    runner: Callable[[list[str], int], subprocess.CompletedProcess[str]] = default_command_runner,
) -> dict[str, Any]:
    errors: list[str] = []
    probes = (
        (["/usr/bin/pmset", "-g", "ps"], "pmset_ps"),
        (["/usr/bin/pmset", "-g", "batt"], "pmset_batt"),
        (["/usr/sbin/ioreg", "-r", "-c", "AppleSmartBattery", "-d", "1"], "ioreg_battery"),
    )
    for arguments, probe in probes:
        try:
            result = runner(arguments, 5)
        except (OSError, subprocess.TimeoutExpired):
            errors.append(f"{probe}_failed")
            continue
        text = f"{result.stdout}\n{result.stderr}"
        if result.returncode != 0:
            errors.append(f"{probe}_failed")
            continue
        if "AC Power" in text or re.search(r'"ExternalConnected"\s*=\s*(?:Yes|true|1)', text, re.IGNORECASE):
            return {"source": "ac", "on_ac": True, "known": True, "probe": probe, "error_code": None}
        if "Battery Power" in text or re.search(r'"ExternalConnected"\s*=\s*(?:No|false|0)', text, re.IGNORECASE):
            return {"source": "battery", "on_ac": False, "known": True, "probe": probe, "error_code": None}
        errors.append(f"{probe}_unknown")
    return {
        "source": "unknown", "on_ac": None, "known": False, "probe": None,
        "error_code": "power_probe_unknown" if any(value.endswith("_unknown") for value in errors) else "power_probe_failed",
    }


def meaningful_idle_seconds(query: Callable[[int], float]) -> int:
    values: list[float] = []
    for event_type in MEANINGFUL_EVENT_TYPES:
        value = float(query(event_type))
        if math.isfinite(value) and 0 <= value < 1_000_000_000:
            values.append(value)
    # DBL_MAX means that this session has never received that event type.  If
    # all meaningful types are absent, there is no user activity to delay.
    return int(min(values)) if values else IDLE_REQUIRED_SECONDS


def coregraphics_idle_seconds() -> int | None:
    try:
        framework = ctypes.CDLL(COREGRAPHICS_FRAMEWORK)
        query = framework.CGEventSourceSecondsSinceLastEventType
        query.argtypes = [ctypes.c_int32, ctypes.c_uint32]
        query.restype = ctypes.c_double
        return meaningful_idle_seconds(
            lambda event_type: query(CG_EVENT_SOURCE_HID_SYSTEM_STATE, event_type)
        )
    except (OSError, TypeError, ValueError, AttributeError):
        return None


def idle_state(
    runner: Callable[[list[str], int], subprocess.CompletedProcess[str]] = default_command_runner,
) -> dict[str, Any]:
    # The live LaunchAgent runs in the logged-in GUI session, so CoreGraphics
    # can distinguish real interaction from noisy HID hardware.  Injected
    # runners keep the portable IOHID fallback used by deterministic tests.
    if runner is default_command_runner:
        seconds = coregraphics_idle_seconds()
        if seconds is not None:
            return {
                "seconds": seconds,
                "ready": seconds >= IDLE_REQUIRED_SECONDS,
                "error_code": None,
            }
    try:
        result = runner(["/usr/sbin/ioreg", "-c", "IOHIDSystem", "-d", "4"], 5)
    except (OSError, subprocess.TimeoutExpired):
        return {"seconds": None, "ready": False, "error_code": "idle_probe_failed"}
    match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', result.stdout or "")
    if result.returncode != 0 or not match:
        return {"seconds": None, "ready": False, "error_code": "idle_probe_failed"}
    seconds = max(0, int(match.group(1)) // 1_000_000_000)
    return {"seconds": seconds, "ready": seconds >= IDLE_REQUIRED_SECONDS, "error_code": None}


def retry_delay(attempt: int) -> timedelta:
    if attempt <= len(RETRY_DELAYS):
        return RETRY_DELAYS[max(0, attempt - 1)]
    return PERSISTENT_PROBE


# Codes that mean "the model chain did not produce a usable answer this time".
# Every provider is tried again on the next run, so parking the source on Big
# King only guaranteed it stayed parked: dingtalk_chat sat in manual_required
# from 2020-01-02 to 09-19 over one GPT quota failure.
MODEL_CHAIN_CODES = frozenset({
    "codex_subscription_limit", "codex_subscription_auth", "codex_model_unavailable",
    "codex_invalid_json", "model_output_invalid", "model_chain_exhausted", "codex_failed",
    "model_quota_unavailable", "model_auth_unavailable", "model_timeout", "model_unavailable", "model_source_changed",
})


def is_model_chain_code(code: str) -> bool:
    """Orchestrator codes carry a component prefix (chat_reconcile_..., chat_memory_...)."""
    return any(code == known or code.endswith("_" + known) for known in MODEL_CHAIN_CODES)


def chat_manual_required(code: str) -> bool:
    """Only explicit source auth, damaged structure or conflicts stop retries."""
    codes = {"chat_read_auth_required", "auth_required", "unauthorized", "forbidden",
             "journal_invalid", "journal_conflict", "rollback_concurrent_change",
             "recovery_conflict_requires_review", "checkpoint_invalid", "checkpoint_corrupt",
             "chat_checkpoint_invalid", "chat_checkpoint_corrupt",
             "chat_reconcile_checkpoint_invalid", "chat_reconcile_checkpoint_corrupt",
             "database_corrupt", "database_malformed"}
    return any(code == item or code.endswith("_" + item) for item in codes)


def chat_read_failure(code: str) -> bool:
    return (chat_read_recovery.scheduled_read_recovery(code)
            or code.startswith(("chat_read_", "chat_checkpoint_", "chat_reconcile_checkpoint_")))


def chat_read_notice_class(code: str) -> str:
    """Stable notice categories; cooldown/progress aliases are not new faults."""
    if any(part in code for part in ("auth", "permission", "login", "forbidden")):
        return "auth"
    if any(part in code for part in ("conflict", "rollback_concurrent_change")):
        return "conflict"
    if any(part in code for part in ("checkpoint_invalid", "checkpoint_corrupt", "journal_invalid", "database_corrupt")):
        return "structure"
    if code.endswith("chat_read_blocked"):
        return "blocked"
    if "timeout" in code or code.endswith("chat_read_transient"):
        return "transient"
    if code.endswith(("chat_reconcile_incomplete", "incremental_partial", "chat_read_cooldown", "chat_checkpoint_busy")):
        return "progress"
    return "unclassified"


def chat_read_notice_key(alert_id: str, reason_class: str) -> str:
    return hashlib.sha256(f"chat-read-notice:{alert_id}:{reason_class}".encode()).hexdigest()


def is_chat_read_alert(alert: dict[str, Any]) -> bool:
    return (alert.get("source_id") == "dingtalk_chat"
            and alert.get("category") in {"sync_failure", "sync_block"}
            and chat_read_failure(str(alert.get("error_code") or "")))


def chat_checkpoint_progress(config: dict[str, Any]) -> dict[str, Any]:
    # Workbench shares this read-only adapter; there is no second retry ledger.
    from operational_workbench import read_chat_checkpoint
    return read_chat_checkpoint(config)


def failure_schedule(source_id: str, error_code: str, attempt: int) -> tuple[bool, timedelta]:
    if source_id == "dingtalk_chat":
        if chat_manual_required(error_code):
            return True, PERSISTENT_PROBE
        if is_model_chain_code(error_code):
            return attempt >= 3, PERSISTENT_PROBE if attempt >= 3 else timedelta(minutes=5)
        # Fallback only when no checkpoint owns this read. Never exhaust it.
        if attempt <= 3:
            seconds = (60, 300, 900)[max(0, attempt - 1)]
            return False, timedelta(seconds=seconds + random.uniform(0, seconds * .1))
        return True, PERSISTENT_PROBE
    persistent = attempt > len(RETRY_DELAYS)
    return persistent, retry_delay(attempt)


def media_delay(probe: int) -> timedelta:
    if probe <= len(MEDIA_DELAYS):
        return MEDIA_DELAYS[max(0, probe - 1)]
    return MEDIA_DAILY_PROBE


def parse_controller_receipt(stdout: str) -> dict[str, Any]:
    for line in reversed((stdout or "").splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("run_id"):
            return payload
    return {"status": "failed", "error_code": "component_invalid_receipt"}


def source_receipt(payload: dict[str, Any], source_id: str) -> dict[str, Any]:
    value = (payload.get("sources") or {}).get(source_id)
    return value if isinstance(value, dict) else {}


def classify_receipt(payload: dict[str, Any], source_id: str) -> dict[str, Any]:
    status = str(payload.get("status") or "failed")
    item = source_receipt(payload, source_id)
    failures = payload.get("failures") if isinstance(payload.get("failures"), list) else []
    error_code = str(payload.get("error_code") or "")
    if not error_code and failures:
        first = failures[0] if isinstance(failures[0], dict) else {}
        error_code = str(first.get("error_code") or "")
    if not error_code:
        error_code = str(item.get("error_code") or "")
    media_pending = int(item.get("recording_media_pending") or 0)
    source_pending = int(item.get("recording_source_waiting_count") or 0)
    if source_id == "dingtalk_minutes" and not failures and not error_code and (media_pending or source_pending or status == "partial"):
        return {"kind": "waiting_source", "count": max(1, media_pending, source_pending), "error_code": "source_incomplete" if source_pending else "media_pending"}
    if source_id == "dingtalk_mail" and error_code in MAIL_READ_SAFETY_CODES:
        return {"kind": "manual_required", "error_code": error_code}
    if source_id == "dingtalk_chat" and (error_code == "chat_checkpoint_busy" or error_code.endswith("_chat_checkpoint_busy")):
        return {"kind": "deferred", "error_code": error_code}
    if source_id == "dingtalk_chat" and status in {"success", "noop"}:
        if (not payload.get("run_id") or not item or item.get("status") not in {"success", "noop"}
                or failures or item.get("failures") or error_code):
            return {"kind": "deferred", "error_code": error_code or "component_invalid_receipt"}
    if status in {"success", "noop"}:
        return {"kind": "success", "changed": status == "success", "error_code": None}
    if status == "partial" and source_id == "dingtalk_chat" and not error_code:
        return {"kind": "deferred", "error_code": "chat_reconcile_incomplete"}
    if status == "partial" and not failures and not error_code:
        return {"kind": "success", "changed": True, "error_code": None}
    if status == "deferred" or error_code in DEFERRED_CODES or error_code in UNKNOWN_BLOCK_CODES:
        return {"kind": "deferred", "error_code": error_code or "deferred"}
    return {"kind": "failure", "error_code": error_code or "unknown_failure"}


def sync_notification_context(alert: dict[str, Any]) -> dict[str, Any]:
    source = str(alert.get("source_id") or "system")
    if is_chat_read_alert(alert):
        # Current read evidence overrides historical local-repair diagnostics in
        # this projection only. Never rewrite those audit fields on the alert.
        code = str(alert.get("error_code") or "")
        reason_class = chat_read_notice_class(code)
        manual = reason_class in {"auth", "structure", "conflict", "blocked"}
        stage = str(alert.get("notification_stage") or "initial")
        reason = {"auth": "permissions_required", "structure": "journal_invalid",
                  "conflict": "rollback_conflict"}.get(reason_class, "source_failure")
        if stage == "test":
            reason = "notification_test"
        elif stage not in {"initial", "escalation", "daily", "recovery"}:
            stage = "initial"
        return {"component": source, "phase": "chat_sync", "reason": reason,
                "repair": "not_applicable" if manual else "running", "stage": stage,
                "attempts": max(0, int(alert.get("attempts") or 0)), "requires_action": manual}
    code = str(alert.get("repair_error_code") or alert.get("error_code") or "")
    category = str(alert.get("category") or "")
    stage = str(alert.get("notification_stage") or "initial")
    reason, phase = "source_failure", "sync_service"
    if source == "memory_core":
        reason, phase = "validation_failed", "validation"
    if "rollback" in code or "recovery_conflict" in code or category == "recovery_conflict":
        reason, phase = "rollback_conflict", "journal_recovery"
    if any(value in code for value in ("backup_", "journal_invalid", "unsafe_target")):
        reason, phase = "journal_invalid", "journal_recovery"
    if "timeout" in code:
        reason = "local_timeout"
    if category == "listener_failure" or code == "event_not_ready":
        reason, phase = "listener_not_ready", "listener"
    if "proposal_" in code:
        reason, phase = "proposal_invalid", "chat_sync"
    if code == "proposal_relation_registration_pending":
        reason, phase = "relation_pending", "chat_sync"
    if category in {'supervisor_runtime_error','supervisor_health'}:
        reason, phase = "supervisor_runtime_failure", "sync_service"
    if any(value in (code + ' ' + str(alert.get('error_code') or '')).lower() for value in ("permission", "auth", "login")):
        reason = "permissions_required"
    if category == "recovery":
        reason, stage = "recovery_verified", "recovery"
    if stage == "test":
        reason = "notification_test"
    repair = str(alert.get("repair_status") or "not_attempted")
    if repair not in {"not_attempted", "running", "verified", "partial", "blocked", "failed", "not_applicable"}:
        repair = "not_attempted"
    return {"component": source if source in state.ALERT_SOURCE_IDS else "system", "phase": phase,
            "reason": reason, "repair": repair, "stage": stage,
            "attempts": max(0, int(alert.get("repair_attempt_count") or 0)),
            "requires_action": reason in {"rollback_conflict", "journal_invalid", "permissions_required"} or repair in {"blocked", "failed", "partial"}}


def macos_notification_body(alert: dict[str, Any]) -> str:
    context = sync_notification_context(alert)
    causes = {"rollback_conflict": "后来修改的内容与历史回退冲突，已保留原文", "validation_failed": "完整记忆校验未通过",
              "journal_invalid": "回退备份或记录无法验证", "local_timeout": "本机校验超时", "listener_not_ready": "实时监听尚未就绪",
              "permissions_required": "需要权限或登录确认", "source_failure": "当前原因待核实", "recovery_verified": "对应项目已取得恢复证据",
              "notification_test": "这是一条实际通道验收通知，不代表业务恢复",
              "proposal_invalid": "聊天提案的证据关联检查未通过，尚未写入业务记忆",
              "relation_pending": "文件阶段可能已完成，但证据关联未收口；消息仍待处理",
              "supervisor_runtime_failure": "后台发生过处理异常，可能影响及时检查与提醒；原错误仍待核实"}
    repairs = {"not_attempted": "尚未执行本地修复", "running": "正在修复", "verified": "本地修复与校验已通过",
               "partial": "仅部分恢复，冲突保留", "blocked": "已停止自动修复", "failed": "修复未成功", "not_applicable": "无需修复"}
    if context["component"] == "memory_core" and context["reason"] == "permissions_required":
        causes["permissions_required"] = "后台无法完整读取业务资料，需要你允许相关目录访问"
    stage = {"initial": "故障提醒", "escalation": "未解决满 6 小时", "daily": "每日未解决提醒", "recovery": "恢复通知", "test": "通道验收测试"}[context["stage"]]
    body = (f"【{stage}】{SOURCE_NAMES.get(str(alert.get('source_id')), '同步来源')}\n"
            f"原因：{causes.get(context['reason'], '状态需核对')}。\n"
            f"处理：{repairs[context['repair']]}（本地修复 {context['attempts']} 次）。\n"
            f"{'需要你在问题工作台确认。' if context['requires_action'] else '暂不需要你操作；未把通知当作已读。'}")
    if is_chat_read_alert(alert) and context["stage"] != "test":
        read_causes = {"auth": "聊天来源认证或权限需要确认", "structure": "聊天补查结构或台账损坏需要核对",
                       "conflict": "聊天补查存在内容冲突，已保留原文", "blocked": "聊天来源读取受阻，需要核对具体原因",
                       "transient": "聊天来源读取暂时失败", "unclassified": "聊天来源读取失败，具体原因仍待核验",
                       "progress": "固定补查窗口仍未完整"}
        read_reason = read_causes[chat_read_notice_class(str(alert.get("error_code") or ""))]
        handling = "保留消息与失败证据，等待来源问题处理" if context["requires_action"] else "自动恢复中，按补查台账安排续跑"
        body = (f"【{stage}】钉钉聊天\n原因：{read_reason}。\n"
                f"处理：{handling}（累计读取 {context['attempts']} 次）。\n"
                f"{'需要你在问题工作台确认当前原因。' if context['requires_action'] else '暂不需要你操作；未把通知当作已读。'}")
    import supervisor_health_recovery
    progress=supervisor_health_recovery.view(alert, ROOT)
    if progress:
        body=(f"【{stage}】{SOURCE_NAMES['system']}\n原因：{progress['reason_label']}。\n处理：{progress['summary']}\n"
              + ('需要你确认权限或登录。' if progress['requires_user'] else '暂不需要你操作；通知不代表已读。'))
    elif not (int(alert.get('repair_attempt_count') or 0)>0 or alert.get('repair_run_id') or alert.get('repair_started_at')) and not is_chat_read_alert(alert):
        body=body.replace(f"处理：{repairs[context['repair']]}（本地修复 {context['attempts']} 次）。",
                          '处理：后台继续核查；尚未进入专门本地修复流程。' if context['repair']=='not_attempted' else f"处理：{repairs[context['repair']]}；本地修复次数未记录。")
    return body


def deliver_macos(alert: dict[str, Any], database: Path | None = None) -> None:
    body=macos_notification_body(alert)
    identifier = "sync-" + hashlib.sha256(f"{alert['alert_id']}:{alert.get('notification_revision', 0)}".encode()).hexdigest()[:40]
    was_uncertain = alert.get("macos_status") == "uncertain"
    request = {"identifier": identifier, "operation": "status" if alert.get("macos_status") in {"requested", "uncertain"} else "send", "body": body}
    if request["operation"] == "send":
        # Reserve before starting the helper. A crash/timeout between the OS
        # request and our receipt must never turn into a blind second send.
        def reserve(connection):
            return connection.execute(
                "UPDATE sync_alerts SET macos_status='uncertain',macos_error_code='notification_in_flight',"
                "macos_requested_at=?,macos_attempt_count=macos_attempt_count+1 "
                "WHERE alert_id=? AND notification_revision=? AND macos_status NOT IN ('uncertain','requested','visible') "
                "AND (category='recovery' OR (status='open' AND acknowledged_at IS NULL))",
                (state.now_iso(), alert["alert_id"], int(alert.get("notification_revision") or 0)),
            ).rowcount
        changed = state._write(database, "reserve_notification", reserve, identity=identifier)
        if not changed:
            return
    try:
        result = subprocess.run(
            [_archive_path('/Users/example/Applications/业务记忆同步中心.app/Contents/MacOS/DingTalkSyncCenter'), "--sync-notification"],
            input=json.dumps(request, ensure_ascii=False), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=9, check=False,
        )
        receipt = json.loads(result.stdout) if result.returncode == 0 else {}
        status = str(receipt.get("status") or "uncertain")
        code = receipt.get("error_code")
        if status not in {"visible", "requested", "permission_off", "permission_required", "failed", "uncertain"}:
            status, code = "uncertain", "notification_invalid_receipt"
        if was_uncertain and status == "requested":
            status, code = "uncertain", "notification_not_in_center"
    except OSError:
        status, code = "failed", "notification_helper_unavailable"
    except (subprocess.TimeoutExpired, ValueError, AttributeError):
        status, code = "uncertain", "notification_receipt_missing"
    if request["operation"] == "status" and status == alert.get("macos_status") and code == alert.get("macos_error_code"):
        return
    state.mark_delivery(str(alert["alert_id"]), "macos", status, error_code=code, database=database, count_attempt=False, notification_revision=int(alert.get('notification_revision') or 0))


def macos_authorization_status() -> dict[str, Any]:
    try:
        result = subprocess.run(
            [_archive_path('/Users/example/Applications/业务记忆同步中心.app/Contents/MacOS/DingTalkSyncCenter'), "--sync-notification"],
            input=json.dumps({"identifier": "sync-" + "0" * 40, "operation": "status"}),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=9, check=False,
        )
        receipt = json.loads(result.stdout) if result.returncode == 0 else {}
        status = str(receipt.get("status") or "uncertain")
        return {"status": "authorized" if status in {"requested", "visible"} else status,
                "read_confirmed": False}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {"status": "uncertain", "read_confirmed": False}


def recheck_macos_notifications(database: Path | None = None, *, alert_id: str | None = None,
                                probe: Callable[[], dict[str, Any]] | None = None) -> dict[str, Any]:
    if probe is None and database is not None and Path(database) != state.DATABASE:
        return {"status": "isolated", "resumed": 0}
    authorization = (probe or macos_authorization_status)()
    if authorization.get("status") != "authorized":
        return {**authorization, "resumed": 0, "fault_cleared": False}
    if database is None and maintenance_status()["active"]:
        return {"status": "maintenance", "resumed": 0, "fault_cleared": False}
    resumed = 0
    # Only definitely unsent, still-open and unacknowledged notifications.
    # Requested/uncertain/visible and historic recoveries are never replayed.
    for row in state.alerts(database, status="open", limit=200):
        if alert_id and row["alert_id"] != alert_id:
            continue
        if row.get("acknowledged_at") or row.get("macos_status") not in {"permission_off", "permission_required"}:
            continue
        connection = state._connect(database)
        try:
            changed = connection.execute(
                "UPDATE sync_alerts SET macos_status='pending',macos_error_code=NULL WHERE alert_id=? "
                "AND notification_revision=? AND macos_status IN ('permission_off','permission_required') "
                "AND status='open' AND acknowledged_at IS NULL AND category!='recovery'",
                (row["alert_id"], row["notification_revision"]),
            ).rowcount
            connection.commit()
        finally:
            connection.close()
        if changed:
            deliver_macos({**row, "macos_status": "pending", "macos_error_code": None}, database)
            resumed += 1
        if resumed >= 5:
            break
    return {"status": "authorized", "resumed": resumed, "fault_cleared": False, "read_confirmed": False}


def miso_health_transition(alert: dict[str, Any]) -> dict[str, str]:
    source_id = str(alert.get("source_id") or "system")
    category = {
        "memory_core": "storage",
        "dingtalk_chat": "retrieval",
        "dingtalk_minutes": "retrieval",
        "dingtalk_mail": "retrieval",
        "win11": "retrieval",
        "system": "access_control",
    }.get(source_id, "quality")
    if str(alert.get("category") or "") == "recovery":
        dedupe = str(alert.get("dedupe_key") or "")
        previous = "review_required" if ":manual_attention:" in dedupe else (
            "attention" if ":waiting_source:" in dedupe or ":power_probe:" in dedupe else "critical"
        )
        return {"previous": previous, "current": "healthy", "category": category, "category_status": "healthy"}
    current = "critical" if str(alert.get("severity") or "warning") == "error" else (
        "review_required" if str(alert.get("category") or "") == "manual_attention" else "attention"
    )
    previous = "attention" if current == "critical" and alert.get("miso_attempted_at") else "healthy"
    return {"previous": previous, "current": current, "category": category, "category_status": current}


def run_miso_bridge(arguments: list[str]) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["/usr/bin/python3", str(MISO_BRIDGE), *arguments],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"ok": False, "reason": "miso_bridge_unavailable"}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "reason": "miso_bridge_invalid_receipt"}
    if result.returncode != 0 or not isinstance(payload, dict) or payload.get("ok") is not True:
        return {
            "ok": False,
            "reason": str((payload if isinstance(payload, dict) else {}).get("reason") or "miso_bridge_failed"),
        }
    return payload


def dispatch_miso_event(event_id: str, database: Path | None = None) -> bool:
    """Start the existing governed sender for one typed event, never a collector."""
    if database is not None and Path(database) != state.DATABASE:
        return False
    if not re.fullmatch(r"miso_[0-9a-f]{64}", event_id):
        return False
    if maintenance_status()["active"]:
        return False
    root = Path(_archive_path('/Users/example/.hermes/miso-memory-notifier'))
    try:
        subprocess.Popen(
            [_archive_path('/Users/example/.hermes/hermes-agent/venv/bin/python'), "-m", "miso_notifier.sync_delivery", "--event-id", event_id],
            cwd=root, env={**os.environ, "PYTHONPATH": str(root / "app"), "MISO_NOTIFIER_REAL_SEND": "enabled"},
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except OSError:
        return False


def hold_chat_failure(code: str, *, database: Path | None = None, at: datetime | None = None) -> bool:
    """Manual and daemon failures share explicit safety gates."""
    if not chat_manual_required(code):
        return False
    stamp = state.now_iso(at)
    state.update_source("dingtalk_chat", database, status="manual_required", next_due=None,
                        error_code=code, last_failure=stamp)
    return True


def deliver_miso(alert: dict[str, Any], database: Path | None = None) -> None:
    alert_id = str(alert["alert_id"])
    event_id = str(alert.get("miso_event_id") or "")
    if event_id:
        payload = run_miso_bridge(["status", "--event-id", event_id])
        if not payload.get("ok"):
            reason = str(payload.get("reason") or "miso_status_probe_failed")
            if str(alert.get("miso_status") or "") == "pending" and str(alert.get("miso_error_code") or "") == reason:
                return
            state.mark_delivery(
                alert_id, "miso", "pending", event_id=event_id,
                error_code=reason, database=database,
             notification_revision=int(alert.get('notification_revision') or 0))
            return
        status = str(payload.get("status") or "unknown")
        if status == "pending" and int(payload.get("attempt_count") or 0) == 0:
            dispatch_miso_event(event_id, database)
        error_code = str(payload.get("error_code") or "") or None
        receipt_hash = payload.get("receipt_hash")
        if status == "delivered" and not re.fullmatch(r"[0-9a-f]{64}", str(receipt_hash or "")):
            status, error_code = "unverified", "legacy_receipt_missing"
        if (status == str(alert.get("miso_status") or "") and (error_code or "") == str(alert.get("miso_error_code") or "")
                and receipt_hash == alert.get("miso_receipt_hash")):
            return
        state.mark_delivery(
            alert_id, "miso", status if status in {
                "pending", "sending", "delivered", "blocked", "uncertain", "superseded", "unverified"
            } else "blocked",
            event_id=event_id,
            receipt_hash=receipt_hash,
            error_code=error_code,
            database=database,
         notification_revision=int(alert.get('notification_revision') or 0))
        return

    transition = miso_health_transition(alert)
    payload = run_miso_bridge([
        "emit", "--observed-at", str(alert.get("first_seen") or state.now_iso()),
        "--previous-status", transition["previous"],
        "--current-status", transition["current"],
        "--category", transition["category"],
        "--category-status", transition["category_status"],
        "--sync-context", json.dumps(sync_notification_context(alert), ensure_ascii=False),
        "--incident-ref", hashlib.sha256(alert_id.encode()).hexdigest(),
        "--revision", str(int(alert.get("notification_revision") or 0)),
    ])
    if not payload.get("ok"):
        state.mark_delivery(
            alert_id, "miso", "failed",
            error_code=str(payload.get("reason") or "miso_enqueue_failed"), database=database,
         notification_revision=int(alert.get('notification_revision') or 0))
        return
    status = str(payload.get("status") or "pending")
    if status == "delivered" and not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("receipt_hash") or "")):
        status = "unverified"
    state.mark_delivery(
        alert_id, "miso",
        status if status in {"pending", "sending", "delivered", "blocked", "uncertain", "superseded", "unverified"} else "blocked",
        event_id=str(payload.get("event_id") or "") or None,
        receipt_hash=payload.get("receipt_hash"),
        database=database,
     notification_revision=int(alert.get('notification_revision') or 0))
    if status == "pending":
        dispatch_miso_event(str(payload.get("event_id") or ""), database)


def _pending_delivery_alerts(database):
    result=set()
    for p in (ledger_recovery.directory(Path(database or state.DATABASE))/'delivery-outbox').glob('*.json'):
        d=ledger_recovery.read(p)
        if d.get('status')=='pending':result.add(d['payload']['alert_id'])
    return result


def _delivery_failure(exc,database):
    if isinstance(exc,ledger_recovery.Deferred):return
    root=ROOT if database is None or Path(database)==state.DATABASE else Path(database).parent
    record_runtime_error(exc,root=root,database=database)


def deliver_due_alerts(config: dict[str, Any], database: Path | None = None) -> int:
    path=Path(database or state.DATABASE)
    with ledger_recovery.locked(path.with_suffix('.delivery.lock'),blocking=False) as acquired:
        if not acquired:return 0
        return _deliver_due_alerts(config,database)


def retire_stale_recovery(alert: dict[str, Any], database: Path | None = None, now: datetime | None = None) -> bool:
    """Stop delivering a recovery notice that is too old to be useful; never sends."""
    if alert.get("category") != "recovery":
        return False
    seen = state.parse_time(alert.get("resolved_at") or alert.get("first_seen"))
    if seen is None or (now or state.now_dt()) - seen < STALE_RECOVERY_NOTICE:
        return False
    alert_id = str(alert["alert_id"])
    revision = int(alert.get("notification_revision") or 0)
    event_id = str(alert.get("miso_event_id") or "")
    miso_unsent = alert.get("miso_status") == "pending"
    if miso_unsent and event_id:
        # Only provably unsent Miso events may be retired; anything else keeps its own probe.
        miso_unsent = run_miso_bridge(["retire", "--event-id", event_id]).get("retired_unsent") is True
    if miso_unsent:
        state.mark_delivery(alert_id, "miso", "superseded", event_id=event_id or None,
                            error_code="stale_recovery_notice", database=database, notification_revision=revision)
    if alert.get("macos_status") in {"pending", "retrying"}:
        state.mark_delivery(alert_id, "macos", "superseded", error_code="stale_recovery_notice",
                            database=database, notification_revision=revision)
    return True


def _deliver_due_alerts(config: dict[str, Any], database: Path | None = None) -> int:
    del config
    ledger_recovery.flush(Path(database or state.DATABASE))
    try:
        recheck_macos_notifications(database)
    except (sqlite3.Error, ledger_recovery.Deferred) as exc:
        _delivery_failure(exc,database)
    delivered = 0
    for alert in state.due_alerts(database):
        if str(alert['alert_id']) in _pending_delivery_alerts(database):
            continue
        try:
            if retire_stale_recovery(alert, database):
                continue
            read_incident = is_chat_read_alert(alert)
            if not read_incident or not alert.get("last_notified_at"):
                alert = state.claim_delivery(str(alert["alert_id"]), database)
            if read_incident and alert:
                # Disable time-based escalation only for retrieval incidents. Pending
                # delivery receipts may still be checked without a new event/revision.
                def quiet_read(connection):
                    connection.execute("UPDATE sync_alerts SET next_reminder=?,notification_stage='initial' WHERE alert_id=?",
                                       ("9999-12-31T00:00:00+00:00", alert["alert_id"]))
                    connection.execute("INSERT OR IGNORE INTO supervisor_incident_receipts VALUES(?,?,?,?)",
                                       (chat_read_notice_key(str(alert["alert_id"]), chat_read_notice_class(str(alert.get("error_code") or ""))),
                                        alert["alert_id"], state.now_iso(), state.now_iso()))
                state._write(database, "chat_read_notification", quiet_read)
                alert = {**alert, "notification_stage": "initial"}
                # Legacy due-alert queries may still select the six-hour onset.
                # Do not claim a reminder or count a completed notice as new work.
                if alert.get("last_notified_at") and not (
                        alert.get("macos_status") in {"pending", "retrying", "requested"}
                        or alert.get("miso_status") in {"pending", "sending", "blocked", "uncertain"}):
                    continue
            if not alert:
                continue
            if alert.get("previous_miso_event_id"):
                run_miso_bridge(["retire", "--event-id", alert["previous_miso_event_id"]])
            acknowledged = bool(alert.get("acknowledged_at")) and alert.get("category") != "recovery"
            retired = alert.get("status") != "open" and alert.get("category") != "recovery"
            if (acknowledged or retired) and alert.get("miso_event_id") and alert.get("miso_status") in {"pending", "blocked"}:
                run_miso_bridge(["retire", "--event-id", alert["miso_event_id"]])
            if alert.get("macos_status") in {"pending", "retrying", "requested"} and not acknowledged and not retired:
                deliver_macos(alert, database)
            if alert.get("miso_status") in {"pending", "sending", "blocked", "uncertain"} and (not (acknowledged or retired) or alert.get("miso_event_id")):
                deliver_miso(alert, database)
            delivered += 1
        except (sqlite3.Error, ledger_recovery.Deferred) as exc:
            _delivery_failure(exc,database)
            continue
    return delivered


class Supervisor:
    def __init__(
        self,
        config_path: Path = CONFIG_PATH,
        *,
        database: Path | None = None,
        runner: Callable[[list[str], int], subprocess.CompletedProcess[str]] = default_command_runner,
        dashboard_factory: Callable[[DashboardPaths], SystemDashboard] = SystemDashboard,
        clock: Callable[[], datetime] = now_dt,
        notification_delivery: Callable[[dict[str, Any], Path | None], int] | None = None,
        parallel_workers: bool | None = None,
    ):
        self.config_path = config_path
        self.database = database
        self.runner = runner
        self.dashboard_factory = dashboard_factory
        self.clock = clock
        self.notification_delivery = notification_delivery
        self.parallel_workers = (runner is default_command_runner) if parallel_workers is None else parallel_workers
        self._worker_started = {}

    def config(self) -> dict[str, Any]:
        payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise SupervisorError("config_invalid")
        return payload

    def pulse_owner(self, stage, active=(), **terminal):
        # Only this scheduling thread may renew the owner lease. Notification
        # delivery and worker progress never substitute for this evidence.
        current = self.clock() if active else None
        now_mono = time.monotonic()
        if (not hasattr(self, "_progress_cache") or now_mono - self._progress_cache[0] >= 30
                or {row["source_id"] for row in self._progress_cache[1]} != set(active)):
            config = self.config()
            workers = [supervisor_worker_progress.read(
                self.database or state.DATABASE, config, source, self._worker_started[source])
                for source in active if source in self._worker_started]
            self._progress_cache = (now_mono, workers)
        else:
            workers = [row for row in self._progress_cache[1] if row["source_id"] in active]
        supervisor_liveness.pulse(self.config_path.parent.resolve(), stage=stage, workers=workers, **terminal)
        if active:
            labels = "、".join(SOURCE_NAMES[source] for source in active)
            state.update_runtime(self.database, status="running", heartbeat_at=state.now_iso(current),
                                 current_source=active[0] if len(active) == 1 else None,
                                 current_stage=labels + "处理中；后台持续巡检")

    def parallel_eligible(self, candidates, active, config):
        if maintenance_status()["active"]:
            return []
        power = power_state(self.runner)
        if not power.get("known") or not power.get("on_ac"):
            return []
        idle = idle_state(self.runner)
        ready = []
        for source in SOURCE_ORDER:
            if source not in candidates or source in active:
                continue
            # Chat and minutes share their entire reconciliation flag/lock.
            if source in {"dingtalk_chat", "dingtalk_minutes"} and (
                    set(active) | set(ready)) & {"dingtalk_chat", "dingtalk_minutes"}:
                continue
            if not idle["ready"] and not (source == "dingtalk_mail" and
                    config.get("email_sync", {}).get("background_collection_while_active") is True):
                row = state.source(source, self.database)
                if row.get("status") not in PRESERVE_WHILE_WAITING:
                    state.update_source(source, self.database, status="waiting_idle")
                continue
            ready.append(source)
        return ready

    def run_parallel_sources(self, due_ids, config):
        pending_initial = set(due_ids)
        def prepare(source):
            pending_initial.discard(source)
            stamp = state.now_iso(self.clock())
            self._worker_started[source] = stamp
            state.update_source(source, self.database, status="running", last_attempt=stamp)
            state.update_runtime(self.database, last_scan_at=stamp)

        def record(source, receipt):
            row = self.record_outcome(source, receipt, self.clock())
            self._worker_started.pop(source, None)
            return {"source_id": source, "status": row.get("status"), "run_id": row.get("last_run_id")}

        def eligible(active):
            # Do not run reconcile_attention here: it may perform a full
            # validation. Keep the owner thread available while workers run.
            current = self.clock()
            fresh_config = self.config()
            candidates = set()
            for row in state.sources(self.database):
                source = row["source_id"]
                if source in active or (state.parse_time(row.get("next_due")) or current) > current:
                    continue
                if row.get("error_code") == "validation_inputs_changed":
                    continue
                if source == "dingtalk_chat" and (row.get("status") == "manual_required" or
                        chat_checkpoint_progress(fresh_config).get("status") == "unavailable"):
                    continue
                candidates.add(source)
            pending_initial.update(candidates)
            state.update_runtime(self.database, last_scan_at=state.now_iso(current),
                                 next_scan_at=state.now_iso(current + timedelta(minutes=1)))
            return self.parallel_eligible(candidates, active, fresh_config)

        def continuations(active):
            current = self.clock()
            fresh_config = self.config()
            valid = {row["source_id"] for row in state.sources(self.database)
                     if row["source_id"] not in active
                     and (state.parse_time(row.get("next_due")) or current) <= current
                     and row.get("error_code") != "validation_inputs_changed"
                     and not (row["source_id"] == "dingtalk_chat" and (
                         row.get("status") == "manual_required" or
                         chat_checkpoint_progress(fresh_config).get("status") == "unavailable"))}
            pending_initial.intersection_update(valid)
            return self.parallel_eligible(pending_initial, active, fresh_config)

        initial = self.parallel_eligible(due_ids, (), config)
        return supervisor_worker_pool.run(initial, execute=self.run_worker, record=record,
            eligible=eligible, prepare=prepare, pulse=lambda active: self.pulse_owner("workers_running", active),
            continuations=continuations)

    def migrate_existing_minutes(self, current: datetime) -> None:
        row = state.source("dingtalk_minutes", self.database)
        probe_count = int(row.get("probe_count") or 0)
        if probe_count:
            if row.get("error_code") == "media_pending" and row.get("status") in {
                "pending", "waiting_idle", "waiting_power", "deferred",
            }:
                state.update_source(
                    "dingtalk_minutes", self.database, status="waiting_source",
                    next_due=row.get("next_due") or state.now_iso(current),
                )
            if row.get("error_code") == "media_pending" and probe_count >= 4:
                count = max(1, int(row.get("attention_count") or 1))
                state.open_alert(
                    "dingtalk_minutes", "waiting_source", "钉钉 AI 听记等待来源",
                    f"{count} 条原始媒体尚未就绪；文字内容已保留，将每天继续探测。",
                    attempts=probe_count, database=self.database, at=current,
                )
            return
        if row.get("status") != "pending":
            return
        payload = read_json(MINUTES_AUTOMATION_STATE) or {}
        items = payload.get("items") if isinstance(payload.get("items"), dict) else {}
        count = sum(
            1 for item in items.values()
            if isinstance(item, dict) and item.get("media_retry_exhausted") is True
        )
        if count:
            state.update_source(
                "dingtalk_minutes", self.database, status="waiting_source", probe_count=4,
                attention_count=count, error_code="media_pending", next_due=state.now_iso(current),
            )
            state.open_alert(
                "dingtalk_minutes", "waiting_source", "钉钉 AI 听记等待来源",
                f"{count} 条原始媒体尚未就绪；文字内容已保留，将每天继续探测。",
                attempts=4, database=self.database, at=current,
            )

    def run_worker(self, source_id: str) -> dict[str, Any]:
        command = [
            "/usr/bin/caffeinate", "-i", "-s", "/usr/bin/python3", str(CONTROLLER),
            "--config", str(self.config_path), "--automatic", source_id,
        ]
        try:
            runner = supervisor_worker_pool.command if self.runner is default_command_runner else self.runner
            result = runner(command, 6 * 60 * 60)
        except subprocess.TimeoutExpired:
            return {"status": "failed", "error_code": "component_timeout"}
        except OSError:
            return {"status": "failed", "error_code": "controller_unavailable"}
        payload = parse_controller_receipt(result.stdout)
        if result.returncode != 0 and payload.get("status") not in {"failed", "deferred"}:
            payload["status"] = "failed"
            payload["error_code"] = payload.get("error_code") or f"controller_exit_{result.returncode}"
        return payload

    def mark_recovery(self, source_id: str, previous: dict[str, Any], current: datetime) -> None:
        recovered = False
        for category in ("sync_failure", "sync_block", "waiting_source"):
            alert = state.resolve_alert(
                source_id, category, title=f"{SOURCE_NAMES[source_id]}已恢复",
                detail="后台探测已成功；将继续按周期检查。", database=self.database, at=current,
            )
            recovered = recovered or alert is not None
        if recovered or int(previous.get("attempt_count") or 0) > 0:
            state.update_source(source_id, self.database, recovery_at=state.now_iso(current))
        if not recovered and int(previous.get("attempt_count") or 0) > 0:
            state.record_recovery(
                source_id, str(previous.get("last_failure") or previous.get("error_code") or state.now_iso(current)),
                title=f"{SOURCE_NAMES[source_id]}已恢复",
                detail="自动重试已经成功；将继续按周期检查。",
                attempts=int(previous.get("attempt_count") or 0), database=self.database, at=current,
            )

    def chat_read_alert(self, code: str, attempt: int, current: datetime, *, manual: bool = False) -> None:
        previous = next((a for a in state.alerts(self.database, status="open")
                         if a["source_id"] == "dingtalk_chat" and a["category"] == "sync_failure"), {})
        # Progress updates must not alternate warning/error through open_alert:
        # that API rearms delivery on escalation, even for the same read episode.
        if previous:
            def update_progress(connection):
                connection.execute("UPDATE sync_alerts SET attempts=?,last_seen=?,error_code=?,updated_at=?,title=? WHERE alert_id=?",
                                   (attempt, state.now_iso(current), code, state.now_iso(current),
                                    "聊天读取需要处理" if manual else "聊天读取持续失败，自动恢复中", previous["alert_id"]))
            state._write(self.database, "chat_read_progress", update_progress)
            alert = previous
        else:
            alert = state.open_alert(
                "dingtalk_chat", "sync_failure", "聊天读取需要处理" if manual else "聊天读取持续失败，自动恢复中",
                "来源认证、结构或冲突需要处理；原始失败和已取得消息保留。" if manual
                else "固定补查窗口尚未完整；只续跑未完成会话，持续失败每 6 小时探测。",
                severity="error", attempts=attempt, error_code=code, database=self.database, at=current)
        previous_class = chat_read_notice_class(str(previous.get("error_code") or ""))
        current_class = chat_read_notice_class(code)
        meaningful_change = current_class != previous_class and current_class != "progress"
        if previous and previous.get("last_notified_at") and meaningful_change:
            def rearm(connection):
                # These additive receipts dedupe notice categories, not reads or
                # delivery success. Preserve all historical repair evidence.
                connection.execute("INSERT OR IGNORE INTO supervisor_incident_receipts VALUES(?,?,?,?)",
                                   (chat_read_notice_key(str(alert["alert_id"]), previous_class), alert["alert_id"],
                                    state.now_iso(current), state.now_iso(current)))
                inserted = connection.execute("INSERT OR IGNORE INTO supervisor_incident_receipts VALUES(?,?,?,?)",
                                              (chat_read_notice_key(str(alert["alert_id"]), current_class), alert["alert_id"],
                                               state.now_iso(current), state.now_iso(current))).rowcount
                if not inserted:
                    return
                connection.execute("UPDATE sync_alerts SET last_notified_at=NULL,next_reminder=?,"
                                   "notification_stage='initial',notification_revision=notification_revision+1,"
                                   "acknowledged_at=NULL,macos_status='pending',macos_attempt_count=0,macos_requested_at=NULL,"
                                   "miso_status='pending',miso_event_id=NULL,miso_error_code=NULL WHERE alert_id=?",
                                   (state.now_iso(current), alert["alert_id"]))
            state._write(self.database, "chat_read_reason_change", rearm)

    def project_chat_checkpoint(self, progress: dict[str, Any], current: datetime,
                                **common: Any) -> dict[str, Any]:
        previous = state.source("dingtalk_chat", self.database)
        phase = progress.get("status")
        if phase == "unavailable":
            return previous
        code = str(progress.get("error_code") or "chat_reconcile_incomplete")
        # The checkpoint owns the read classification as well as its schedule.
        # Fallback/legacy errors are classified separately by chat_manual_required.
        manual = phase == "manual_required"
        # Counts are projections, never a second budget. Preserve legacy history.
        attempt = max(int(previous.get("attempt_count") or 0), int(progress.get("attempt_count") or 0))
        probe = max(int(previous.get("probe_count") or 0), int(progress.get("probe_count") or 0))
        row = state.update_source(
            "dingtalk_chat", self.database, **common,
            status="manual_required" if manual else "persistent_failure" if phase == "probing" else "retrying",
            error_code=code, attempt_count=attempt, probe_count=probe,
            next_due=None if manual else progress.get("next_due") or previous.get("next_due") or state.now_iso(current),
            attention_count=int(progress.get("conversations_incomplete") or 0))
        if manual or phase == "probing":
            self.chat_read_alert(code, attempt, current, manual=manual)
        return row

    def chat_local_work_due(self, config: dict[str, Any], source: dict[str, Any], current: datetime) -> bool:
        """Read-only queue probe. Controller must honor read next_due independently."""
        if not config.get("external_root"):
            return False
        progress = chat_checkpoint_progress(config)
        if not progress or progress.get("status") in {"complete", "unavailable"}:
            return False
        code = str(progress.get("error_code") or "")
        if (progress.get("status") == "manual_required" or chat_manual_required(code)) and not code.endswith("auth_required"):
            return False
        if source.get("error_code") == "validation_inputs_changed":
            return False
        last = state.parse_time(source.get("last_attempt"))
        if last and current < last + NORMAL_INTERVAL:
            return False
        path = Path(config["external_root"]) / "dingtalk-chat/state/chat.sqlite"
        try:
            connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
            try:
                return bool(connection.execute("SELECT 1 FROM semantic_queue WHERE state IN ('pending','failed','deferred') LIMIT 1").fetchone())
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            return False

    def record_outcome(
        self, source_id: str, receipt: dict[str, Any], current: datetime,
    ) -> dict[str, Any]:
        previous = state.source(source_id, self.database)
        classification = classify_receipt(receipt, source_id)
        run_id = str(receipt.get("run_id") or "") or None
        common = {"last_attempt": state.now_iso(current), "last_probe_at": state.now_iso(current), "last_run_id": run_id}
        kind = classification["kind"]
        mail_result = source_receipt(receipt, source_id)
        progress: dict[str, Any] = {}
        if source_id == "dingtalk_chat":
            progress = chat_checkpoint_progress(self.config())
            if progress.get("status") == "unavailable":
                # A busy progress reader supplies no success/failure evidence.
                if previous.get("status") == "running":
                    return state.update_source(source_id, self.database, **common, status="deferred")
                return previous
            code = str(classification.get("error_code") or "")
            if (progress and progress.get("status") != "complete"
                    and (kind in {"success", "deferred"} or chat_read_failure(code))
                    and code != "validation_inputs_changed"):
                return self.project_chat_checkpoint(progress, current, **common)
        if source_id == "dingtalk_mail" and kind == "success" and mail_result.get("heavy_deferred") is True:
            # Everything light was processed; only big mail is left, waiting for
            # the Win11 attachment reader to be on power (2020-01-02 rule).
            return state.update_source(
                source_id, self.database, **common, status="waiting_power",
                next_due=state.now_iso(current + NORMAL_INTERVAL),
                error_code=str(mail_result.get("deferred_reason") or "waiting_win11_power"),
            )
        if kind == "success":
            # Checkpoint attempts include successful chunks and probes. They are
            # cumulative observations, not consecutive failures to reset on success.
            checkpoint_counts = source_id == "dingtalk_chat" and bool(progress)
            recovery_previous = previous
            if checkpoint_counts and (previous.get("status") == "healthy" or not previous.get("last_failure")):
                recovery_previous = {**previous, "attempt_count": 0}
            self.mark_recovery(source_id, recovery_previous, current)
            return state.update_source(
                source_id, self.database, **common, status="healthy",
                attempt_count=max(int(previous.get("attempt_count") or 0), int(progress.get("attempt_count") or 0)) if checkpoint_counts else 0,
                probe_count=max(int(previous.get("probe_count") or 0), int(progress.get("probe_count") or 0)) if checkpoint_counts else 0,
                last_success=state.now_iso(current), next_due=state.now_iso(current + NORMAL_INTERVAL),
                blocked_since=None, error_code=None, attention_count=0,
            )
        if kind == "waiting_source":
            # A successful local pass with media unavailable at the source is
            # new evidence that an older execution failure is no longer current.
            for category in ("sync_failure", "sync_block"):
                state.resolve_alert(
                    source_id, category, title=f"{SOURCE_NAMES[source_id]}本地同步已恢复",
                    detail="本地处理已通过；原始媒体仍等待来源提供，尚非完整同步。",
                    database=self.database, at=current,
                )
            probe = max(1, int(previous.get("probe_count") or 0) + 1)
            count = max(1, int(classification.get("count") or 1))
            row = state.update_source(
                source_id, self.database, **common, status="waiting_source", probe_count=probe,
                attempt_count=0, last_success=state.now_iso(current), next_due=state.now_iso(current + media_delay(probe)),
                blocked_since=None, error_code="media_pending", attention_count=count,
            )
            if probe >= 4:
                state.open_alert(
                    source_id, "waiting_source", f"{SOURCE_NAMES[source_id]}等待来源",
                    f"{count} 条原始媒体尚未就绪；文字内容已保留，将每天继续探测。",
                    attempts=probe, database=self.database, at=current,
                )
            return row
        code = str(classification.get("error_code") or "unknown_failure")
        if kind == "manual_required":
            attempt = int(previous.get("attempt_count") or 0) + 1
            row = state.update_source(
                source_id, self.database, **common, status="manual_required",
                attempt_count=attempt, last_failure=state.now_iso(current),
                next_due=state.now_iso(current + timedelta(minutes=1)),
                blocked_since=previous.get("blocked_since") or state.now_iso(current),
                error_code=code,
            )
            state.open_alert(
                source_id, "sync_failure", f"{SOURCE_NAMES[source_id]}需要人工安全确认",
                "跨设备未读安全凭证已失效；请完成一次人工确认，确认通过后后台会自动续跑。",
                severity="error", attempts=attempt, database=self.database, at=current,
            )
            return row
        if kind == "deferred":
            blocked_since = state.parse_time(previous.get("blocked_since")) or current
            row = state.update_source(
                source_id, self.database, **common,
                status="waiting_power" if code == "waiting_power" else "deferred",
                next_due=state.now_iso(current + timedelta(minutes=1)), blocked_since=state.now_iso(blocked_since),
                error_code=code,
            )
            if code in UNKNOWN_BLOCK_CODES and current - blocked_since >= BLOCK_ALERT_AFTER:
                state.open_alert(
                    source_id, "sync_block", f"{SOURCE_NAMES[source_id]}持续阻断",
                    "未知阻断已持续超过 2 小时；后台仍会继续检查。",
                    attempts=int(previous.get("attempt_count") or 0), database=self.database, at=current,
                )
            return row
        attempt = int(previous.get("attempt_count") or 0) + 1
        persistent, delay = failure_schedule(source_id, code, attempt)
        # Every model was already tried and Example Owner told on Feishu; parking the
        # chat source on a person would only stop the next retry.
        park_chat = source_id == "dingtalk_chat" and chat_manual_required(code)
        row = state.update_source(
            source_id, self.database, **common,
            status="manual_required" if park_chat else "persistent_failure" if persistent else "retrying", attempt_count=attempt,
            last_failure=state.now_iso(current), next_due=None if park_chat else state.now_iso(current + delay),
            blocked_since=None, error_code=code,
        )
        if source_id == "dingtalk_chat" and chat_read_failure(code):
            if persistent or park_chat:
                self.chat_read_alert(code, attempt, current, manual=park_chat)
            return row
        if persistent or source_id == "dingtalk_chat":
            state.open_alert(
                source_id, "sync_failure", f"{SOURCE_NAMES[source_id]}{'持续失败' if persistent else '同步失败'}",
                (f"已失败 {attempt} 次；停止重复来源尝试，保留原因并交给受限本地修复。" if park_chat
                 else ("后台检查仍有未完成项；实际处理次数与下次恢复时间见逐项记录。" if source_id == "dingtalk_mail" else f"已连续失败 {attempt} 次；将按有限重试策略处理。")),
                severity="error", attempts=attempt, database=self.database, at=current, error_code=code,
            )
            self.schedule_local_repair(self.config())
        return row

    def schedule_local_repair(self, config: dict[str, Any]) -> int:
        # Fixture configs and unrelated sources never launch a production helper.
        if self.database is not None or not all(config.get(key) for key in ("memory_root", "external_root", "dingtalk_automation")):
            return 0
        import bounded_repair_actions
        explicit = bounded_repair_actions.execute_queued(self.config_path)
        if explicit.get("status") != "idle":
            return int(explicit.get("status") in {"success", "noop", "already_running"})
        import local_sync_repair
        pending = local_sync_repair.execute_queued(self.config_path)
        if pending.get("status") != "idle":
            return int(pending.get("status") in {"success", "partial", "already_running"})
        for alert in state.alerts(self.database, status="open"):
            if alert.get("source_id") == "dingtalk_chat" and chat_read_failure(str(alert.get("error_code") or "")):
                continue
            if local_sync_repair.eligible(alert) and alert.get("repair_status") == "not_attempted":
                result = local_sync_repair.start(self.config_path, str(alert["alert_id"]), automatic=True)
                if result.get("status") == "started":
                    local_sync_repair.execute_queued(self.config_path)
                return int(result.get("status") in {"started", "already_running"})
        return 0

    def schedule_media_recovery(self) -> None:
        # Same native scheduler, same media mutex. Closing the UI cannot strand work.
        if self.database is not None or self.config_path.resolve()!=CONFIG_PATH.resolve() or maintenance_active():
            return
        import media_classification
        media_classification.spawn(self.config_path)

    def schedule_full_validation(self, config: dict[str, Any]) -> dict[str, Any] | None:
        if (self.database is not None or self.config_path.resolve() != CONFIG_PATH.resolve()
                or not all(config.get(key) for key in ("memory_root", "external_root", "dingtalk_automation"))):
            return None
        import automatic_memory_validation
        return automatic_memory_validation.run_once()

    def reconcile_read_safety(self, config: dict[str, Any], current: datetime) -> None:
        if not isinstance(config.get("email_sync"), dict):
            return
        previous = state.source("dingtalk_mail", self.database)
        error_code = str(previous.get("error_code") or "")
        try:
            safety = email_sync.read_safety_status(config)
        except Exception:
            safety = {
                "binding_valid": False,
                "server_side_pass": False,
                "cross_device_confirmed": False,
            }
        ready = bool(
            safety.get("binding_valid")
            and safety.get("server_side_pass")
            and safety.get("cross_device_confirmed")
        )
        if ready:
            if error_code in MAIL_READ_SAFETY_CODES:
                state.update_source(
                    "dingtalk_mail", self.database, status="pending",
                    next_due=state.now_iso(current), blocked_since=None, error_code=None,
                )
            return
        first_manual_gate = str(previous.get("status") or "") != "manual_required"
        state.update_source(
            "dingtalk_mail", self.database, status="manual_required",
            next_due=state.now_iso(current + timedelta(minutes=1)),
            blocked_since=previous.get("blocked_since") or state.now_iso(current),
            error_code="read_safety_unconfirmed",
        )
        if first_manual_gate:
            state.open_alert(
                "dingtalk_mail", "sync_failure", "钉钉邮件需要人工安全确认",
                "跨设备未读安全凭证已失效；请完成一次人工确认，确认通过后后台会自动续跑。",
                severity="error", attempts=int(previous.get("attempt_count") or 0),
                database=self.database, at=current,
            )

    def reconcile_power_probe(
        self,
        power: dict[str, Any],
        previous_runtime: dict[str, Any],
        current: datetime,
    ) -> int:
        previous_failures = int(previous_runtime.get("power_probe_failures") or 0)
        if not power.get("known"):
            failures = previous_failures + 1
            if failures >= POWER_ALERT_AFTER:
                state.open_alert(
                    "system", "power_probe", "电源状态持续无法确认",
                    "电源状态未知，不会冒充电池状态；系统每分钟重新检测。",
                    attempts=failures, database=self.database, at=current,
                )
            return failures
        state.resolve_alert(
            "system", "power_probe", title="电源检测已恢复",
            detail="已重新确认当前电源状态；历史探测错误仍保留。",
            database=self.database, at=current,
        )
        return 0

    def reconcile_attention(self, config: dict[str, Any], current: datetime) -> None:
        if config.get('email_sync'):
            import mail_auto_recovery
            mail_auto_recovery.repair_storage(config)
        # New committed data needs validation even when an unrelated historical
        # content conflict has exhausted/blocked the incident repair path.
        validation = self.schedule_full_validation(config)
        if validation is not None:
            current = self.clock()
        self._reconcile_attention(config, current)
        self.reconcile_chat_recovery(config, current)
        import validation_retry
        import automatic_memory_validation
        try:
            validation_retry.reconcile(self, config, current)
        except automatic_memory_validation.ValidationFault as exc:
            from index_incident_evidence import CODES
            if exc.code not in CODES:
                raise
            import index_runtime_recovery
            index_runtime_recovery.retain_expected_failure(self, exc)
        import operational_workbench
        operational_workbench.reconcile(config, database=self.database, at=current)
        operational_workbench.recover_actions(database=self.database)
        self.schedule_local_repair(config)
        if self.database is None and self.config_path.resolve() == CONFIG_PATH.resolve():
            import supervisor_incidents
            supervisor_incidents.record_attention_success(ROOT, loaded_supervisor_sha256=LOADED_SUPERVISOR_SHA256)
            # Check while the just-completed full validation is still current.
            # The health sentinel remains an independent fallback.
            supervisor_incidents.verify_correction(ROOT)

    def reconcile_chat_recovery(self, config: dict[str, Any], current: datetime) -> None:
        source = state.source("dingtalk_chat", self.database)
        progress = chat_checkpoint_progress(config)
        if progress.get("status") == "unavailable":
            return
        source_code = str(source.get("error_code") or "")
        if (progress and progress.get("status") != "complete"
                and (not source_code or chat_read_failure(source_code) or source_code == "waiting_power")):
            source = self.project_chat_checkpoint(progress, current)
        elif (progress.get("status") == "complete" and chat_read_failure(source_code)
              and (source.get("status") == "manual_required"
                   or (state.parse_time(progress.get("updated_at")) or current)
                   > (state.parse_time(source.get("last_attempt")) or current - NORMAL_INTERVAL))):
            # A repaired/completed read still needs a controller acceptance run.
            source = state.update_source("dingtalk_chat", self.database, status="retrying",
                                         next_due=state.now_iso(current))
        elif source.get("status") == "manual_required" and chat_read_failure(source_code) and not chat_manual_required(source_code):
            # Include legacy parked reads without resetting historical attempts.
            source = state.update_source("dingtalk_chat", self.database, status="retrying",
                                         next_due=source.get("next_due") or state.now_iso(current))
        if not config.get("dingtalk_automation") or not config.get("external_root"):
            return
        import dingtalk_memory_automation as memory
        import dingtalk_conflict_recovery as recovery
        paths = memory.runtime_paths(config)
        live = read_json(paths["status"]) or {}
        failed_at = state.parse_time(live.get("last_hourly_at"))
        succeeded_at = state.parse_time(source.get("last_success"))
        code = str(live.get("last_hourly_error") or "")
        checkpoint_at = state.parse_time(progress.get("updated_at")) if progress else None
        if (live.get("last_hourly_status") == "failed" and failed_at and code
                and (not checkpoint_at or failed_at > checkpoint_at)
                and chat_manual_required(code) and (not succeeded_at or failed_at > succeeded_at)):
            if source.get("status") != "manual_required" or source.get("error_code") != code:
                hold_chat_failure(code, database=self.database, at=failed_at)
        if live.get("listener_state") == "failed":
            alert = state.open_alert("dingtalk_chat", "listener_failure", "钉钉实时监听未完成验收",
                             "实时监听未就绪；遵守跨重启重试上限，历史补同步不算监听恢复。",
                             severity="error", error_code=str(live.get("listener_error_code") or "event_not_ready"),
                             database=self.database, at=current)
            with state._connect(self.database) as connection:
                connection.execute("UPDATE sync_alerts SET repair_status='blocked',repair_error_code=error_code "
                                   "WHERE alert_id=? AND repair_status='not_attempted'", (alert["alert_id"],))
        elif live.get("listener_state") == "ready" and live.get("live_acceptance") == "passed" and live.get("subscriptions_valid"):
            accepted = state.parse_time(live.get("live_acceptance_at"))
            failed = next((a for a in state.alerts(self.database, status="open") if a["category"] == "listener_failure"), {})
            if accepted and accepted > (state.parse_time(failed.get("first_seen")) or accepted - timedelta(seconds=1)):
                state.resolve_alert("dingtalk_chat", "listener_failure", title="钉钉实时监听已恢复",
                                    detail="订阅有效、监听就绪，且已收到就绪后的真实新消息。", database=self.database, at=current)
        try:
            conflicts = memory.recovery_conflicts(config)
        except (memory.AutomationError, recovery.RecoveryError, OSError, ValueError, KeyError, sqlite3.Error):
            state.open_alert("dingtalk_chat", "recovery_conflict", "回退记录暂时无法核验",
                             "相关写入已停止；原件与失败记录保留，需要检查回退证据。", severity="error",
                             error_code="journal_invalid", database=self.database, at=current)
            return
        if conflicts:
            alert = state.open_alert("dingtalk_chat", "recovery_conflict", "历史回退存在内容冲突",
                                     "后来的业务修改与失败批次均已保留；只有来源和写入目标均独立的批次可继续。",
                                     severity="error", error_code="recovery_conflict_requires_review", database=self.database, at=current)
            if alert.get("repair_status") == "not_attempted":
                with state._connect(self.database) as connection:
                    connection.execute("UPDATE sync_alerts SET repair_status='blocked',repair_error_code='recovery_conflict_requires_review' WHERE alert_id=?", (alert["alert_id"],))
        else:
            alert = next((row for row in state.alerts(self.database, status="open")
                          if row.get("source_id") == "dingtalk_chat" and row.get("category") == "recovery_conflict"), None)
            if alert is None:
                return
            try:
                evidence = recovery.closure_evidence(config, alert, self.config_path.parent, current)
            except (memory.AutomationError, recovery.RecoveryError, OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                # A damaged/missing receipt never clears the incident and never
                # turns an unverified repair into a supervisor process failure.
                return
            if evidence:
                receipt_path = self.config_path.parent / 'state/recovery-closure' / (alert['alert_id'] + '.json')
                recovery.contract.save(receipt_path, {**evidence, 'state': 'validated_pending_closure'})
                with state._connect(self.database) as connection:
                    connection.execute("UPDATE sync_alerts SET repair_status='verified',repair_error_code=NULL,repair_finished_at=? "
                                       "WHERE alert_id=? AND status='open'", (state.now_iso(current), alert['alert_id']))
                resolved = state.resolve_alert(
                    "dingtalk_chat", "recovery_conflict", title="历史内容冲突已按保留现版处理",
                    detail="现版和原失败记录保留；关联消息已完成处理，当前完整校验通过。实时监听仍单独验收。",
                    database=self.database, at=current,
                )
                if resolved:
                    recovery.contract.save(receipt_path, {**evidence, 'state': 'resolved', 'recovery_alert_id': resolved['alert_id']})

    def _reconcile_attention(self, config: dict[str, Any], current: datetime) -> None:
        try:
            snapshot = self.dashboard_factory(DashboardPaths.from_config(config)).collect(config, force=True, now=current)
        except Exception:
            return
        core = snapshot.get("memory_core") if isinstance(snapshot.get("memory_core"), dict) else {}
        core_display = core.get("display_status") if isinstance(core.get("display_status"), dict) else {}
        core_health = core.get("health") if isinstance(core.get("health"), dict) else {}
        core_acceptance = core.get("acceptance") if isinstance(core.get("acceptance"), dict) else {}
        # The public display deliberately keeps an open severe incident red until
        # this reconciler closes it.  Using that display here would make the alert
        # feed itself forever.  Reconcile against the core's independent health
        # and acceptance evidence instead, and only close an existing incident
        # when the same core source has a newer success timestamp.
        core_status = str(core_health.get("code") or core_display.get("code") or "unknown")
        acceptance_status = str(core_acceptance.get("code") or "unknown")
        open_core_alert = next(
            (
                row for row in state.alerts(self.database, status="open")
                if str(row.get("source_id") or "") == "memory_core"
                and str(row.get("category") or "") == "core_failure"
            ),
            None,
        )
        if core_status in {"failed", "blocked"} or acceptance_status == "blocked":
            core_error = str(core.get("validation_error_code") or "validation_failed")
            if core_error not in {"validation_permission_denied", "validation_context_unavailable",
                                  "automatic_validation_timeout", "automatic_validation_interrupted",
                                  "automatic_validation_state_invalid", "validation_output",
                                  "validation_receipt_unproven", "validation_command_unavailable",
                                  "memory_index_state_invalid", "memory_index_state_unavailable", "memory_index_update_failed",
                                  "claim_base_mismatch"} and not re.fullmatch(
                                      r"claim_base_[a-z0-9_]{1,64}", core_error
                                  ):
                core_error = "validation_failed"
            alert = state.open_alert(
                "memory_core", "core_failure", "记忆库核心需要处理",
                str(core_health.get("detail") or core_display.get("detail") or "写入、索引或校验尚未恢复。"),
                severity="error", attempts=1, database=self.database, at=current, error_code=core_error,
            )
            if core_error == "validation_permission_denied":
                with state._connect(self.database) as connection:
                    connection.execute("UPDATE sync_alerts SET repair_status='blocked',repair_error_code=? "
                                       "WHERE alert_id=? AND repair_status='not_attempted'", (core_error, alert["alert_id"]))
        elif core_status in {"healthy", "recovered"} and acceptance_status == "accepted":
            success_at = state.parse_time(core.get("last_success"))
            failure_times = [state.parse_time((open_core_alert or {}).get(key)) for key in ("first_seen", "last_seen")]
            incident_at = max((value for value in failure_times if value is not None), default=None)
            same_source_has_new_success = bool(
                success_at and (incident_at is None or success_at > incident_at)
            )
            if open_core_alert is None or same_source_has_new_success:
                state.resolve_alert(
                    "memory_core", "core_failure", title="记忆库核心已恢复",
                    detail="写入、索引与校验已经恢复；历史失败记录仍保留。",
                    database=self.database, at=current,
                )

        sources = {str(item.get("id")): item for item in snapshot.get("sources") or [] if isinstance(item, dict)}
        mail_source = sources.get("dingtalk_mail") or {}
        mail_attention = int(mail_source.get("attention") or 0)
        breakdown = (
            mail_source.get("attention_breakdown")
            if isinstance(mail_source.get("attention_breakdown"), dict)
            else {}
        )
        if not all(isinstance(breakdown.get(key), (int, float)) and breakdown[key] >= 0
                   for key in ("technical_failed", "manual_review")):
            # A missing/locked input is not an empty queue or a recovery receipt.
            return
        technical_attention = int(breakdown.get("technical_failed") or 0)
        manual_attention = int(breakdown.get("manual_review") or 0)
        video_waiting = int(breakdown.get("video_waiting") or 0)
        previous = state.source("dingtalk_mail", self.database)
        safety_gated = str(previous.get("error_code") or "") in MAIL_READ_SAFETY_CODES

        if manual_attention:
            state.open_alert(
                "dingtalk_mail", "manual_attention", "钉钉邮件需要人工处理",
                f"{manual_attention} 个附件问题已安全保留，等待人工查看。",
                attempts=0, database=self.database, at=current,
            )
        elif not video_waiting:
            state.resolve_alert(
                "dingtalk_mail", "manual_attention", title="钉钉邮件人工项已结束",
                detail="附件已逐项恢复或明确处置；不代表其他邮件技术故障恢复。", database=self.database, at=current,
            )

        if technical_attention:
            # A dashboard observation is not a worker attempt. In particular,
            # re-reading an old failed item while validation/power/idle is
            # pending must preserve the actual run's counters and retry time.
            if not safety_gated and previous.get("status") not in {
                "failed", "retrying", "persistent_failure", "deferred",
                "waiting_power", "waiting_idle", "running",
            }:
                state.update_source(
                    "dingtalk_mail", self.database, status="retrying",
                    error_code="email_items_failed", next_due=state.now_iso(current),
                )
            state.update_source(
                "dingtalk_mail", self.database, attention_count=mail_attention,
            )
            return

        if manual_attention and config.get("email_sync", {}).get("phased_processing") is True and not safety_gated:
            # Per-attachment attention stays visible without parking mail discovery.
            fields = {"attention_count": mail_attention}
            if previous.get("status") == "manual_required" and previous.get("error_code") == "attachment_review_required":
                fields.update(status="healthy", error_code=None)
            state.update_source("dingtalk_mail", self.database, **fields)
        elif manual_attention:
            if safety_gated:
                state.update_source(
                    "dingtalk_mail", self.database, attention_count=mail_attention,
                )
            else:
                state.update_source(
                    "dingtalk_mail", self.database, status="manual_required",
                    attention_count=mail_attention, error_code="attachment_review_required",
                )
        elif previous.get("status") == "manual_required" and not safety_gated:
            state.update_source(
                "dingtalk_mail", self.database, status="healthy", attention_count=mail_attention,
                error_code=None, recovery_at=state.now_iso(current),
            )

    def deliver(self, config: dict[str, Any]) -> int:
        if self.notification_delivery:
            return self.notification_delivery(config, self.database)
        return deliver_due_alerts(config, self.database)

    def deliver_with_heartbeat(self, config: dict[str, Any]) -> int:
        processed = self.deliver(config)
        state.update_runtime(
            self.database, heartbeat_at=state.now_iso(self.clock())
        )
        return processed

    def run_once(self) -> dict[str, Any]:
        self.pulse_owner("cycle_started")
        try:
            result = self._run_once()
        except ledger_recovery.Deferred as exc:
            waiting = ledger_recovery.verified_wait(self.database or state.DATABASE, str(exc), at=self.clock())
            if waiting:
                self.pulse_owner('cycle_deferred', defer_reason=waiting['reason'], next_retry_at=waiting['next_retry_at'])
                return {'status':'retry_wait', 'error_code':waiting['reason'], 'next_retry_at':waiting['next_retry_at'], 'checked':0}
            self.pulse_owner('cycle_failed')
            raise
        except Exception:
            self.pulse_owner("cycle_failed")
            raise
        self.pulse_owner("cycle_finished")
        return result

    def _run_once(self) -> dict[str, Any]:
        current = self.clock()
        state.initialize(self.database)
        config = self.config()
        previous_runtime = state.runtime(self.database)
        power = power_state(self.runner)
        idle = idle_state(self.runner)
        maintenance = maintenance_status()
        if self.database is None and not maintenance['active']:
            import action_run_owner
            action_run_owner.recover(ROOT)
        previous_heartbeat = state.parse_time(previous_runtime.get("heartbeat_at"))
        prior_ac = bool(previous_runtime.get("on_ac")) and str(previous_runtime.get("power_source")) == "ac"
        woke = bool(previous_heartbeat and current - previous_heartbeat > WAKE_GAP)
        reconnected = bool(
            power.get("known")
            and power.get("on_ac")
            and str(previous_runtime.get("power_source") or "unknown") != "ac"
        )
        maintenance_exited = bool(
            not maintenance["active"]
            and maintenance.get("exited_at")
            and maintenance.get("exited_at") != previous_runtime.get("last_maintenance_exit_at")
        )
        if woke or reconnected or maintenance_exited:
            chat_before_wake = state.source("dingtalk_chat", self.database)
            state.mark_all_due(current, self.database)
            if (chat_read_failure(str(chat_before_wake.get("error_code") or ""))
                    or chat_checkpoint_progress(config).get("status") == "unavailable"):
                state.update_source("dingtalk_chat", self.database, next_due=chat_before_wake.get("next_due"))
        self.migrate_existing_minutes(current)
        in_maintenance = bool(maintenance["active"])
        # Manual attachment classification is a local, read-only status probe.
        # Keep it current even while heavier source work waits for user idle.
        if not in_maintenance:
            self.reconcile_read_safety(config, current)
            self.reconcile_attention(config, current)
            try:
                import sync_center_autonomy
                sync_center_autonomy.tick(ROOT, self.database or state.DATABASE)
                import mail_auto_recovery
                mail_auto_recovery.release_repairs(config)
                self.schedule_media_recovery()
            except (OSError, ValueError, sqlite3.Error) as exc:
                # Failure remains technical; never prevent normal source work.
                sync_center_autonomy.atomic(ROOT / "state/sync-autonomy/error.json", {"phase":"investigating","reason":type(exc).__name__,"at":state.now_iso(current)})


        power_probe_failures = self.reconcile_power_probe(power, previous_runtime, current)

        rows = state.sources(self.database)
        background_mail = config.get("email_sync", {}).get("background_collection_while_active") is True
        chat_progress_available = chat_checkpoint_progress(config).get("status") != "unavailable"
        if config.get('email_sync'):
            try:
                import mail_auto_recovery
                hint = mail_auto_recovery.scheduling_hint(config)
                mail = next((r for r in rows if r['source_id'] == 'dingtalk_mail'), {})
                hint_due = state.parse_time((hint or {}).get('next_due'))
                prior_due = state.parse_time(mail.get('next_due'))
                if hint_due and str(mail.get('error_code') or '') not in MAIL_READ_SAFETY_CODES and (not prior_due or hint_due < prior_due):
                    state.update_source('dingtalk_mail', self.database, next_due=state.now_iso(hint_due))
                    rows = state.sources(self.database)
            except (OSError, ValueError, KeyError, sqlite3.Error):
                pass  # Missing/locked metadata is not proof of recovery.
        due = [
            row for row in rows
            if (row["source_id"] != "dingtalk_chat" or chat_progress_available)
            and (state.parse_time(row.get("next_due")) or current) <= current
            and row.get("error_code") != "validation_inputs_changed"
            and not (row["source_id"] == "dingtalk_chat" and row.get("status") == "manual_required")
        ]
        chat_source = next((row for row in rows if row["source_id"] == "dingtalk_chat"), {})
        if (not in_maintenance and not any(row["source_id"] == "dingtalk_chat" for row in due)
                and self.chat_local_work_due(config, chat_source, current)):
            due.append(chat_source)
        next_due = state.earliest_due(rows)
        runtime_status = "maintenance" if in_maintenance else (
            "power_unknown" if not power.get("known") else
            "waiting_power" if not power["on_ac"] else "waiting_idle" if due and not idle["ready"] else "idle"
        )
        blocked_reason = {
            "maintenance": "maintenance_active",
            "power_unknown": str(power.get("error_code") or "power_probe_unknown"),
            "waiting_power": "waiting_power",
            "waiting_idle": "waiting_idle",
        }.get(runtime_status)
        next_eligible_at = next_due if runtime_status == "idle" else None
        previous_power_known = str(previous_runtime.get("power_source") or "") in {"ac", "battery"}
        power_changed = bool(
            power.get("known") and previous_power_known
            and prior_ac != bool(power.get("on_ac"))
        )
        state.update_runtime(
            self.database, status=runtime_status, heartbeat_at=state.now_iso(current),
            power_source=power["source"], on_ac=bool(power.get("on_ac")), idle_seconds=idle["seconds"],
            idle_ready=idle["ready"], current_source=None, current_stage=None,
            pending_count=len(due), last_power_change_at=state.now_iso(current) if power_changed else previous_runtime.get("last_power_change_at"),
            last_wake_at=state.now_iso(current) if woke else previous_runtime.get("last_wake_at"),
            next_scan_at=next_due, blocked_reason=blocked_reason,
            next_eligible_at=next_eligible_at,
            power_error_code=power.get("error_code"), power_probe_failures=power_probe_failures,
            last_power_probe_at=state.now_iso(current),
            last_maintenance_exit_at=maintenance.get("exited_at") or previous_runtime.get("last_maintenance_exit_at"),
        )
        if in_maintenance:
            self.deliver_with_heartbeat(config)
            return {"status": "maintenance", "checked": 0}
        if not power.get("known"):
            self.deliver_with_heartbeat(config)
            return {"status": "power_unknown", "checked": 0, "error_code": power.get("error_code")}
        if not power["on_ac"]:
            for row in due:
                if row.get("status") not in PRESERVE_WHILE_WAITING:
                    state.update_source(str(row["source_id"]), self.database, status="waiting_power")
            self.deliver_with_heartbeat(config)
            return {"status": "waiting_power", "checked": 0}
        if due and not idle["ready"] and not (background_mail and any(row["source_id"] == "dingtalk_mail" for row in due)):
            for row in due:
                if row.get("status") not in PRESERVE_WHILE_WAITING:
                    state.update_source(str(row["source_id"]), self.database, status="waiting_idle")
            self.deliver_with_heartbeat(config)
            return {"status": "waiting_idle", "checked": 0}

        outcomes: list[dict[str, Any]] = []
        due_ids = {str(row["source_id"]) for row in due}
        if self.parallel_workers:
            outcomes = self.run_parallel_sources(due_ids, config)
        else:
            for source_id in SOURCE_ORDER:
                if source_id not in due_ids:
                    continue
                refreshed_power = power_state(self.runner)
                if not refreshed_power.get("known"):
                    previous = state.source(source_id, self.database)
                    if previous.get("status") not in PRESERVE_WHILE_WAITING:
                        state.update_source(
                            source_id, self.database, status="deferred",
                            next_due=state.now_iso(self.clock() + timedelta(minutes=1)),
                            error_code=str(refreshed_power.get("error_code") or "power_probe_unknown"),
                        )
                    break
                if not refreshed_power["on_ac"]:
                    state.update_source(source_id, self.database, status="waiting_power", error_code="waiting_power")
                    break
                refreshed_idle = idle_state(self.runner)
                if not refreshed_idle["ready"] and not (background_mail and source_id == "dingtalk_mail"):
                    if state.source(source_id, self.database).get("status") not in PRESERVE_WHILE_WAITING:
                        state.update_source(source_id, self.database, status="waiting_idle")
                    continue
                state.update_runtime(
                    self.database, status="running", current_source=source_id,
                    current_stage="同步处理中", last_scan_at=state.now_iso(current),
                    blocked_reason=None, next_eligible_at=state.now_iso(current),
                )
                state.update_source(source_id, self.database, status="running", last_attempt=state.now_iso(current))
                receipt = self.run_worker(source_id)
                completed_at = self.clock()
                row = self.record_outcome(source_id, receipt, completed_at)
                outcomes.append({"source_id": source_id, "status": row.get("status"), "run_id": row.get("last_run_id")})
                if row.get("status") in {"manual_required", "persistent_failure", "retrying"}:
                    self.deliver_with_heartbeat(config)
        self.pulse_owner("reconciling")
        self.reconcile_attention(config, current)
        rows = state.sources(self.database)
        next_scan = state.earliest_due(rows)
        finished_at = self.clock()
        final_power = power_state(self.runner)
        final_idle = idle_state(self.runner)
        pending_count = sum(
            int((state.parse_time(row.get("next_due")) or finished_at) <= finished_at
                and row.get("error_code") != "validation_inputs_changed"
            and not (row["source_id"] == "dingtalk_chat" and row.get("status") == "manual_required")) for row in rows
        )
        final_status = (
            "power_unknown" if not final_power.get("known")
            else "waiting_power" if pending_count and not final_power["on_ac"]
            else "waiting_idle" if pending_count and not final_idle["ready"]
            else "idle"
        )
        final_blocked_reason = {
            "power_unknown": str(final_power.get("error_code") or "power_probe_unknown"),
            "waiting_power": "waiting_power",
            "waiting_idle": "waiting_idle",
        }.get(final_status)
        state.update_runtime(
            self.database, status=final_status, heartbeat_at=state.now_iso(finished_at),
            current_source=None, current_stage=None, last_scan_at=state.now_iso(current),
            next_scan_at=next_scan, pending_count=pending_count,
            power_source=final_power["source"], on_ac=bool(final_power.get("on_ac")),
            idle_seconds=final_idle["seconds"], idle_ready=final_idle["ready"],
            blocked_reason=final_blocked_reason,
            next_eligible_at=next_scan if final_status == "idle" else None,
            power_error_code=final_power.get("error_code"),
            power_probe_failures=power_probe_failures if not final_power.get("known") else 0,
            last_power_probe_at=state.now_iso(finished_at),
        )
        delivered = self.deliver_with_heartbeat(config)
        return {"status": "success", "checked": len(outcomes), "outcomes": outcomes, "alerts_processed": delivered}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the BUSINESS power-aware sync supervisor")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("command", nargs="?", choices=("run-once", "status"), default="run-once")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        state.initialize()
    except ledger_recovery.Deferred:
        print(json.dumps({"status":"retry_wait","error_code":"ledger_retry_wait"}))
        return 0
    except Exception as exc:
        record_runtime_error(exc)
        print(json.dumps({"status": "failed", "error_code": "supervisor_state_unavailable"}))
        return 2
    if args.command == "status":
        print(json.dumps({
            "runtime": state.runtime(), "sources": state.sources(),
            "open_alerts": state.open_alert_count(),
        }, ensure_ascii=False, sort_keys=True))
        return 0
    with supervisor_lock() as acquired:
        if not acquired:
            print(json.dumps({"status": "already_running"}, ensure_ascii=False))
            return 0
        try:
            result = Supervisor(args.config).run_once()
        except ledger_recovery.Deferred:
            print(json.dumps({'status':'failed','error_code':'ledger_wait_unverified'}))
            return 2
        except Exception as exc:
            record_runtime_error(exc)
            try:
                state.update_runtime(
                    status="failed", heartbeat_at=state.now_iso(), current_source=None,
                    current_stage=None, blocked_reason="supervisor_exception", next_eligible_at=None,
                )
            except (sqlite3.Error, OSError, ledger_recovery.Deferred):
                pass  # The independent durable error inbox remains authoritative.
            print(json.dumps({"status": "failed", "error_code": re.sub(r"[^A-Za-z0-9_.:-]", "_", str(getattr(exc, "code", type(exc).__name__)))[:80]}, ensure_ascii=False))
            return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    import owner_retirement
    owner_retirement.require_running()
    raise SystemExit(main())
